"""Background refresh jobs: single-flight locking, subprocess spawn, and
status/log tracking for the polling API.

Mutual exclusion is enforced by pipeline/refresh.py itself (a non-blocking
flock as the first thing main() does) — that's the authoritative guarantee
and covers the systemd-timer-triggered path too, which this module never
sees directly. The best-effort probe here exists only to give the UI a fast
"already running" response instead of spawning a subprocess that's certain
to lose the race.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from . import config

_LOG_TAIL_LINES = 200

# Redact anything that looks like it could be a credential value, in case
# verbose logging is ever accidentally enabled upstream (GarminDB itself
# has a known DEBUG-level line that logs the password — see recon notes in
# pipeline/notify.py). Defense in depth; INFO-level runs shouldn't hit this.
_SCRUB_RE = re.compile(r"(password['\"]?\s*[:=]\s*)\S+|(login:\s*\S+\s+)\S+", re.IGNORECASE)


def _scrub(line: str) -> str:
    return _SCRUB_RE.sub(lambda m: (m.group(1) or m.group(2) or "") + "[redacted]", line)


def _load_jobs() -> dict:
    if not config.JOBS_PATH.exists():
        return {}
    try:
        with open(config.JOBS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_jobs(jobs: dict) -> None:
    config.ensure_var_dirs()
    tmp = config.JOBS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2)
    os.replace(tmp, config.JOBS_PATH)


def _probe_lock_held() -> bool:
    """Best-effort, non-blocking check of whether the flock is currently
    held by anyone (us, a web-spawned job, or the timer). Never blocks."""
    config.ensure_var_dirs()
    config.LOCK_PATH.touch(exist_ok=True)
    fd = os.open(config.LOCK_PATH, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)  # we got it — release immediately, we're only probing
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def current_job_id() -> Optional[str]:
    jobs = _load_jobs()
    for job_id, job in jobs.items():
        if job.get("status") == "running":
            return job_id
    return None


def get_job(job_id: str) -> Optional[dict]:
    jobs = _load_jobs()
    job = jobs.get(job_id)
    if job is None:
        return None
    return {**job, "log_tail": _read_log_tail(job_id)}


def _read_log_tail(job_id: str) -> list[str]:
    log_path = config.LOG_DIR / f"{job_id}.log"
    if not log_path.exists():
        return []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return [_scrub(line.rstrip("\n")) for line in lines[-_LOG_TAIL_LINES:]]


async def start_refresh(flags: list[str], note: Optional[str]) -> dict:
    """Try to start a refresh job. Returns {"started": bool, "job_id": str}."""
    config.ensure_var_dirs()

    existing = current_job_id()
    if existing is not None:
        return {"started": False, "job_id": existing, "reason": "already_running"}

    if _probe_lock_held():
        # Something (almost certainly the nightly timer) holds the lock
        # even though we have no job record for it — surface a synthetic
        # entry so /api/jobs/<id> has something sensible to report.
        synthetic_id = f"external-{int(time.time())}"
        jobs = _load_jobs()
        jobs[synthetic_id] = {
            "status": "running", "started_at": time.time(), "finished_at": None,
            "exit_code": None, "trigger": "external", "flags": [],
        }
        _save_jobs(jobs)
        return {"started": False, "job_id": synthetic_id, "reason": "already_running"}

    job_id = uuid.uuid4().hex[:12]
    cmd = [str(config.VENV_PYTHON), str(config.REFRESH_SCRIPT), "--push"]
    for flag in flags:
        if flag not in config.ALLOWED_REFRESH_FLAGS:
            raise ValueError(f"flag not allowed: {flag}")
        cmd.append(flag)
    if note:
        cmd += ["--note", note]

    log_path = config.LOG_DIR / f"{job_id}.log"
    jobs = _load_jobs()
    jobs[job_id] = {
        "status": "running", "started_at": time.time(), "finished_at": None,
        "exit_code": None, "trigger": "web", "flags": flags, "note": bool(note),
    }
    _save_jobs(jobs)

    # NOTE: `note` (free text) may end up in argv here, but never a
    # credential — the Garmin username/password are never CLI arguments
    # anywhere in this codebase (GarminDB itself always reads them from
    # ~/.GarminDb/GarminConnectConfig.json, never argv).
    try:
        with open(log_path, "wb") as log_file:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=log_file, stderr=asyncio.subprocess.STDOUT,
                cwd=str(config.REPO_ROOT),
            )
    except OSError as e:
        # Found live: if spawn itself fails (missing venv, disk full,
        # permissions), the job record above was already saved as
        # "running" — without this, it would stay stuck "running" forever,
        # permanently blocking every future refresh via the single-flight
        # check even though nothing is actually running.
        jobs = _load_jobs()
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["finished_at"] = time.time()
        jobs[job_id]["exit_code"] = None
        _save_jobs(jobs)
        raise RuntimeError(f"could not start refresh subprocess: {e}") from e

    asyncio.create_task(_await_completion(job_id, process))
    return {"started": True, "job_id": job_id, "reason": None}


async def _await_completion(job_id: str, process: asyncio.subprocess.Process) -> None:
    exit_code = await process.wait()
    jobs = _load_jobs()
    if job_id in jobs:
        jobs[job_id]["status"] = "succeeded" if exit_code == 0 else "failed"
        jobs[job_id]["finished_at"] = time.time()
        jobs[job_id]["exit_code"] = exit_code
        _save_jobs(jobs)


def reap_stale_external_jobs() -> None:
    """Mark synthetic 'external' job records finished once the lock frees up.
    Called opportunistically on status polls; not load-bearing for safety,
    just keeps /api/jobs responses honest."""
    jobs = _load_jobs()
    changed = False
    for job_id, job in list(jobs.items()):
        if job.get("trigger") == "external" and job.get("status") == "running" and not _probe_lock_held():
            job["status"] = "succeeded"  # unknown exit code from outside; assume success, timer alerts on real failure
            job["finished_at"] = time.time()
            changed = True
    if changed:
        _save_jobs(jobs)
