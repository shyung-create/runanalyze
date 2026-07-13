"""CSRF protection only — no app-level login.

Tailscale's tailnet-only reachability (`tailscale serve`, no public
exposure) is the access control for this app; anyone who can reach it is
trusted, matching the single-user, tailnet-controlled deployment model.

CSRF protection stays regardless: without it, a malicious page loaded on
*any* tailnet-connected device could silently POST new Garmin credentials
or trigger actions here via a blind cross-origin request — that's a
different threat than "who can load the page," and Tailscale reachability
alone doesn't stop it. A signed, HttpOnly cookie is minted automatically
on first visit (no password involved); state-changing POSTs must echo its
embedded token back as a header, which a cross-origin page cannot read.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

_serializer = URLSafeTimedSerializer(config.SESSION_SECRET_KEY or secrets.token_hex(32), salt="runanalyze-csrf")

CSRF_COOKIE = "runanalyze_csrf"
CSRF_COOKIE_MAX_AGE_S = 180 * 24 * 3600  # long-lived — nothing to "log out" of anymore


def _mint_cookie() -> tuple[str, str]:
    """Return (cookie_value, csrf_token)."""
    csrf_token = secrets.token_urlsafe(32)
    return _serializer.dumps({"csrf": csrf_token}), csrf_token


def _read_cookie(request: Request) -> dict | None:
    raw = request.cookies.get(CSRF_COOKIE)
    if not raw:
        return None
    try:
        return _serializer.loads(raw, max_age=CSRF_COOKIE_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None


def _apply_cookie(response: Response, cookie_value: str) -> None:
    response.set_cookie(
        CSRF_COOKIE, cookie_value, max_age=CSRF_COOKIE_MAX_AGE_S,
        secure=config.COOKIE_SECURE, httponly=True, samesite="strict",
    )


def ensure_csrf(request: Request, response: Response) -> dict:
    """FastAPI dependency form — ONLY correct for routes that return a
    plain value (dict/Pydantic model) that FastAPI wraps itself, e.g. our
    JSON API routes. Never rejects a request; just guarantees a
    CSRF-bearing cookie exists, minting one on first visit if needed.

    Verified empirically (not assumed): FastAPI merges cookies set on this
    injected `response` into the final response only when the endpoint
    does NOT return its own Response object. A route that builds and
    returns e.g. FileResponse(...) directly gets a DIFFERENT response
    object sent to the client — cookies set here would be silently
    dropped. Those routes must use apply_csrf_cookie() on the actual
    object they return instead; see main.py's static-file routes.
    """
    payload = _read_cookie(request)
    if payload is None:
        cookie_value, csrf_token = _mint_cookie()
        _apply_cookie(response, cookie_value)
        payload = {"csrf": csrf_token}
    return payload


def apply_csrf_cookie(request: Request, response: Response) -> None:
    """For routes that construct and return their own Response object
    (FileResponse, etc.) — call this on that object directly, after
    building it, rather than relying on the `ensure_csrf` dependency
    (which cannot work in that case; see its docstring)."""
    if _read_cookie(request) is not None:
        return
    cookie_value, _csrf_token = _mint_cookie()
    _apply_cookie(response, cookie_value)


def require_csrf(request: Request, payload: dict) -> None:
    """Double-submit style check: header token must match the value
    embedded in the *signed* cookie, so it can't be forged without the
    server's secret key — and a cross-origin page can't read an HttpOnly
    cookie to produce a matching header in the first place."""
    header_token = request.headers.get("x-csrf-token", "")
    cookie_token = payload.get("csrf", "")
    if not header_token or not hmac.compare_digest(header_token, cookie_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
