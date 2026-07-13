import asyncio

import pytest

from webapp import config, jobs


@pytest.fixture
def isolated_var_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VAR_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_PATH", tmp_path / "refresh.lock")
    monkeypatch.setattr(config, "JOBS_PATH", tmp_path / "jobs.json")
    # Point at a python that doesn't exist, forcing create_subprocess_exec
    # to fail exactly like a corrupted/missing venv would in production.
    monkeypatch.setattr(config, "VENV_PYTHON", tmp_path / "nonexistent" / "python")
    return tmp_path


def test_spawn_failure_marks_job_failed_not_stuck_running(isolated_var_dir):
    with pytest.raises(RuntimeError):
        asyncio.run(jobs.start_refresh([], None))

    job_id = jobs.current_job_id()
    assert job_id is None, "a failed spawn must not leave a job stuck in 'running'"

    all_jobs = jobs._load_jobs()
    assert len(all_jobs) == 1
    (only_job,) = all_jobs.values()
    assert only_job["status"] == "failed"
    assert only_job["finished_at"] is not None


def test_spawn_failure_does_not_block_next_attempt(isolated_var_dir):
    with pytest.raises(RuntimeError):
        asyncio.run(jobs.start_refresh([], None))
    # A second attempt (still pointed at the nonexistent python) must be
    # allowed to try again, not rejected as "already running".
    with pytest.raises(RuntimeError):
        asyncio.run(jobs.start_refresh([], None))
    all_jobs = jobs._load_jobs()
    assert len(all_jobs) == 2
    assert all(j["status"] == "failed" for j in all_jobs.values())
