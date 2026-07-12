"""Read-merge-atomic-write for ~/.GarminDb/GarminConnectConfig.json.

This is the single place in the app that touches the Garmin password. The
write path is the security-critical one: the file must never be visible at
the wrong permissions, never be partially written, and the temp file used
to get there must never be observable by anything but the owning process.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from . import config

# Mirrors garmindb/GarminConnectConfig.json.example (verified against the
# real package source) minus the credential values, which callers fill in.
_DEFAULT_CONFIG: dict[str, Any] = {
    "db": {"type": "sqlite"},
    "garmin": {"domain": "garmin.com"},
    "credentials": {"user": "", "secure_password": False, "password": "", "password_file": None},
    "data": {
        "weight_start_date": "12/31/2019", "sleep_start_date": "12/31/2019",
        "rhr_start_date": "12/31/2019", "hrv_start_date": "12/31/2019",
        "monitoring_start_date": "12/31/2019",
        "download_latest_activities": 25, "download_all_activities": 1000,
    },
    "directories": {"relative_to_home": True, "base_dir": "HealthData", "mount_dir": "/Volumes/GARMIN"},
    "enabled_stats": {
        "monitoring": True, "steps": True, "itime": True, "sleep": True,
        "rhr": True, "hrv": True, "weight": True, "activities": True,
    },
    "course_views": {"steps": []},
    "modes": {},
    "activities": {"display": []},
    "settings": {"metric": False, "default_display_activities": ["walking", "running", "cycling"]},
    "checkup": {"look_back_days": 90},
}


class GarminConfigError(Exception):
    pass


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write `data` to `path` atomically, 0600 throughout, no partial reads.

    The temp file MUST live in the same directory as `path`: os.replace()
    is only atomic within a single filesystem, and the same-directory temp
    file is what makes that guarantee hold (this also means PrivateTmp on
    the systemd unit is irrelevant here by design — the temp file was never
    going in /tmp).
    """
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Directory perms don't depend on umask timing the way the file's do
    # (it never holds secret bytes), but pin it explicitly anyway.
    os.chmod(directory, 0o700)

    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".GarminConnectConfig.json.tmp-")
    tmp_path = Path(tmp_name)
    try:
        # GUARANTEE: the file is 0600 before a single secret byte is written.
        # tempfile.mkstemp already creates with 0600, but we pin it
        # explicitly and *before* any write() call so the guarantee holds
        # even if the creation mechanism ever changes.
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            # GUARANTEE: bytes are durable on disk before the rename makes
            # them visible at the real path — no torn write survives a crash
            # between here and the replace() below.
            os.fsync(f.fileno())
        # GUARANTEE: atomic swap. No reader ever observes a partially
        # written file, and no reader ever observes the file at the wrong
        # permissions — the already-0600 temp file's identity moves to the
        # target path in one filesystem operation.
        os.replace(tmp_path, path)
    except BaseException:
        # Never leave a stray secret-bearing temp file behind on failure.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise

    # GUARANTEE (defensive, not load-bearing): confirm what actually landed
    # on disk matches what we intended, rather than trusting the happy path.
    st = path.stat()
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise GarminConfigError(f"{path} ended up at mode {oct(stat.S_IMODE(st.st_mode))}, expected 0600")
    if st.st_uid != os.getuid():
        raise GarminConfigError(f"{path} is owned by uid {st.st_uid}, expected {os.getuid()}")


def read_config() -> dict:
    """Return the existing config, or a copy of the schema default if none exists yet."""
    if not config.GARMIN_CONFIG_FILE.exists():
        return json.loads(json.dumps(_DEFAULT_CONFIG))  # deep copy without importing copy
    with open(config.GARMIN_CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def write_credentials(username: str, password: str) -> None:
    """Merge username/password into the existing config and write it atomically.

    Every non-credential key already in the file (data dirs, date ranges,
    settings.metric, enabled_stats, ...) passes through untouched.
    """
    if not username or not password:
        raise GarminConfigError("username and password are both required")
    existing = read_config()
    existing.setdefault("credentials", {})
    existing["credentials"]["user"] = username
    existing["credentials"]["password"] = password
    existing["credentials"].setdefault("secure_password", False)
    existing["credentials"].setdefault("password_file", None)
    _atomic_write_json(config.GARMIN_CONFIG_FILE, existing)


def status() -> dict:
    """Non-secret view of the config: whether it's set up, and as whom."""
    if not config.GARMIN_CONFIG_FILE.exists():
        return {"configured": False, "username": None}
    cfg = read_config()
    creds = cfg.get("credentials") or {}
    user = creds.get("user") or None
    password_set = bool(creds.get("password")) or bool(creds.get("password_file"))
    return {"configured": bool(user and password_set), "username": user}
