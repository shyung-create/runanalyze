"""Dashboard login: argon2 password verify, signed session cookies, CSRF,
and login rate limiting. No plaintext password is ever stored — only its
argon2id hash, read from the 0600 env file.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

_hasher = PasswordHasher()
_serializer = URLSafeTimedSerializer(config.SESSION_SECRET_KEY or secrets.token_hex(32), salt="runanalyze-session")

SESSION_COOKIE = "runanalyze_session"

# In-memory login attempt tracker. A single uvicorn worker (Type=simple,
# not multiple processes) makes this sufficient for a single-user tool —
# no shared store needed.
_attempts: dict[str, list[float]] = defaultdict(list)
_locked_until: dict[str, float] = {}


def verify_dashboard_password(password: str) -> bool:
    if not config.DASHBOARD_PASSWORD_HASH:
        return False
    try:
        _hasher.verify(config.DASHBOARD_PASSWORD_HASH, password)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request) -> None:
    """Raise 429 if this client is locked out. Call before verifying a password."""
    key = _client_key(request)
    now = time.time()
    locked_until = _locked_until.get(key)
    if locked_until and now < locked_until:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")


def record_login_result(request: Request, success: bool) -> None:
    key = _client_key(request)
    now = time.time()
    if success:
        _attempts.pop(key, None)
        _locked_until.pop(key, None)
        return
    window_start = now - config.LOGIN_WINDOW_S
    attempts = [t for t in _attempts[key] if t > window_start]
    attempts.append(now)
    _attempts[key] = attempts
    if len(attempts) >= config.LOGIN_MAX_ATTEMPTS:
        _locked_until[key] = now + config.LOGIN_LOCKOUT_S
        _attempts[key] = []


def create_session_cookie() -> tuple[str, str]:
    """Return (cookie_value, csrf_token). The CSRF token is embedded in the
    signed payload so verifying it doesn't need a server-side session store."""
    csrf_token = secrets.token_urlsafe(32)
    payload = {"user": config.DASHBOARD_USER, "csrf": csrf_token}
    return _serializer.dumps(payload), csrf_token


def _read_session(request: Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _serializer.loads(raw, max_age=config.SESSION_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None


def require_session(request: Request) -> dict:
    """FastAPI dependency: 401 if there's no valid session. Used on every
    route except /login and /health."""
    session = _read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


def require_csrf(request: Request, session: dict) -> None:
    """Double-submit style CSRF check: header token must match the value
    embedded in the *signed* session cookie, so it can't be forged without
    the server's secret key."""
    header_token = request.headers.get("x-csrf-token", "")
    session_token = session.get("csrf", "")
    if not header_token or not hmac.compare_digest(header_token, session_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
