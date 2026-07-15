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


def test_rejects_invalid_mode(isolated_var_dir):
    with pytest.raises(ValueError):
        asyncio.run(jobs.start_ai_plan_job("both"))


def test_spawn_failure_marks_job_failed_not_stuck_running(isolated_var_dir):
    with pytest.raises(RuntimeError):
        asyncio.run(jobs.start_ai_plan_job("denovo"))

    job_id = jobs.current_job_id()
    assert job_id is None, "a failed spawn must not leave a job stuck in 'running'"

    all_jobs = jobs._load_jobs()
    assert len(all_jobs) == 1
    (only_job,) = all_jobs.values()
    assert only_job["status"] == "failed"
    assert only_job["trigger"] == "ai-plan"
    assert only_job["flags"] == ["denovo"]


def test_ai_plan_job_shares_single_flight_with_refresh(isolated_var_dir):
    # A refresh job "running" in the job table blocks a new AI-plan job from
    # starting, same single-flight guarantee refresh-vs-refresh already has.
    jobs._save_jobs({"existing-refresh": {"status": "running", "trigger": "web",
                                          "started_at": 0, "finished_at": None,
                                          "exit_code": None, "flags": []}})
    result = asyncio.run(jobs.start_ai_plan_job("blended"))
    assert result == {"started": False, "job_id": "existing-refresh", "reason": "already_running"}
