"""Paths and environment for the web app.

Mirrors pipeline/common.py's layout conventions but stays independent of
the pipeline package (webapp and pipeline are launched separately: uvicorn
vs. a subprocess), so nothing here imports from pipeline/.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
DOCS_DATA_DIR = DOCS_DIR / "data"

# Deliberately outside docs/ — nothing here is ever publishable.
VAR_DIR = REPO_ROOT / "var"
LOG_DIR = VAR_DIR / "logs"
LOCK_PATH = VAR_DIR / "refresh.lock"
JOBS_PATH = VAR_DIR / "jobs.json"
LAST_AUTH_OK_PATH = VAR_DIR / "last_auth_ok"
LAST_AUTH_FAIL_PATH = VAR_DIR / "last_auth_fail"

# GarminDB's own default config location — not under the repo, matches
# garmin_connect_config_manager.py's `homedir + '.GarminDb'`.
GARMIN_CONFIG_DIR = Path.home() / ".GarminDb"
GARMIN_CONFIG_FILE = GARMIN_CONFIG_DIR / "GarminConnectConfig.json"
# garmindb >=3.8.0 caches DI OAuth2 tokens here (garminconnect-based adapter).
# Was `garth_session` under the older, now-deprecated garth-based auth —
# confirmed live on the actual instance during rollout, fixed here too.
GARMIN_SESSION_FILE = GARMIN_CONFIG_DIR / "garmin_tokens.json"

RACE_CONFIG_FILE = REPO_ROOT / "config" / "race_config.yaml"

VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
REFRESH_SCRIPT = REPO_ROOT / "pipeline" / "refresh.py"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DASHBOARD_USER = _env("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD_HASH = _env("DASHBOARD_PASSWORD_HASH")
SESSION_SECRET_KEY = _env("SESSION_SECRET_KEY")

# Cookies are Secure by default (required — this app only ever runs behind
# Tailscale's HTTPS via `tailscale serve`). Only relax for local dev over
# plain http, and only via an explicit opt-out, never a default.
COOKIE_SECURE = _env("WEBAPP_COOKIE_INSECURE", "").lower() not in ("1", "true", "yes")

SESSION_MAX_AGE_S = 12 * 3600  # 12h — re-login once a day is fine for a single-user tool

# Login lockout
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_S = 5 * 60
LOGIN_LOCKOUT_S = 15 * 60

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

ALLOWED_REFRESH_FLAGS = {"--no-llm", "--replan", "--no-sync"}  # --note is handled separately (free text)


def ensure_var_dirs() -> None:
    VAR_DIR.mkdir(mode=0o700, exist_ok=True)
    LOG_DIR.mkdir(mode=0o700, exist_ok=True)
