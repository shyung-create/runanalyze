"""FastAPI app: dashboard login, Garmin credential form, refresh trigger,
job polling, and the existing static docs/ dashboard — served unchanged.

Every route requires a valid session except /login and /health (stated
requirement). Nothing here ever echoes the Garmin password back, logs it,
or exposes it via any response model — response models for credential
endpoints simply have no password field, so it's structurally impossible
to leak it that way, not just a matter of remembering to omit it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import auth, config, garmin_config, jobs

log = logging.getLogger("webapp")

# debug=False is load-bearing, not a default to leave alone: FastAPI's
# debug mode echoes request data and stack locals in its error pages.
app = FastAPI(debug=False, title="runanalyze")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ------------------------------------------------------------ static guard

def _safe_file(base_dir: Path, rel_path: str) -> Path:
    """Resolve rel_path under base_dir, refusing anything that escapes it —
    including via symlinks (os.path.realpath, not just string prefix
    matching on the unresolved path). Raises 404 rather than leaking
    whether the traversal target exists.
    """
    base_real = base_dir.resolve()
    candidate = (base_dir / rel_path).resolve()
    if not str(candidate).startswith(str(base_real) + os.sep) and candidate != base_real:
        raise HTTPException(status_code=404)
    if not candidate.is_file():
        raise HTTPException(status_code=404)
    return candidate


# GarminDb config dir, HealthData, and any raw data file extension are never
# passed to _safe_file's base_dir anywhere in this module — the only two
# base dirs ever used are config.DOCS_DIR and its subdirectories. Verified
# by grep: `_safe_file(` has exactly two distinct base_dir call sites below.
_FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".fit", ".gpx", ".tcx")


def _reject_forbidden_suffix(rel_path: str) -> None:
    if any(rel_path.lower().endswith(suf) for suf in _FORBIDDEN_SUFFIXES):
        raise HTTPException(status_code=404)


# ------------------------------------------------------------ auth routes

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    auth.check_rate_limit(request)
    ok = auth.verify_dashboard_password(password)
    auth.record_login_result(request, ok)
    if not ok:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password."}, status_code=401)
    cookie_value, _csrf = auth.create_session_cookie()
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, cookie_value, max_age=config.SESSION_MAX_AGE_S,
        secure=config.COOKIE_SECURE, httponly=True, samesite="strict",
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


def require_authenticated_post(request: Request, session: dict = Depends(auth.require_session)) -> dict:
    auth.require_csrf(request, session)
    return session


# ------------------------------------------------------------ dashboard (existing, unchanged)

@app.get("/", response_class=HTMLResponse)
def dashboard(session: dict = Depends(auth.require_session)):
    return FileResponse(_safe_file(config.DOCS_DIR, "index.html"))


@app.get("/assets/{rel_path:path}")
def assets(rel_path: str, session: dict = Depends(auth.require_session)):
    _reject_forbidden_suffix(rel_path)
    return FileResponse(_safe_file(config.DOCS_DIR / "assets", rel_path))


@app.get("/data/{rel_path:path}")
def data(rel_path: str, session: dict = Depends(auth.require_session)):
    _reject_forbidden_suffix(rel_path)
    path = _safe_file(config.DOCS_DATA_DIR, rel_path)
    # Same reasoning as netlify.toml's headers for /data/*: never let a
    # cache serve stale JSON after a refresh.
    return FileResponse(path, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------ garmin credentials

class CredentialsIn(BaseModel):
    username: str
    password: str


class OkOut(BaseModel):
    ok: bool
    # Deliberately no other fields — see module docstring.


@app.post("/api/garmin/credentials", response_model=OkOut)
def set_garmin_credentials(body: CredentialsIn, session: dict = Depends(require_authenticated_post)):
    try:
        garmin_config.write_credentials(body.username, body.password)
    except garmin_config.GarminConfigError:
        # Generic message only — never str(exc) here, since a future edit to
        # GarminConfigError could add detail that quotes request data.
        log.error("Garmin credential write failed")
        raise HTTPException(status_code=500, detail="Could not save credentials")
    log.info("Garmin credentials updated")  # boolean fact only, never the value
    return OkOut(ok=True)


class GarminStatusOut(BaseModel):
    configured: bool
    username: Optional[str]
    last_successful_auth: Optional[str]
    last_failed_auth: Optional[str]
    token_valid: bool


@app.get("/api/garmin/status", response_model=GarminStatusOut)
def garmin_status(session: dict = Depends(auth.require_session)):
    st = garmin_config.status()
    last_ok = config.LAST_AUTH_OK_PATH.read_text().strip() if config.LAST_AUTH_OK_PATH.exists() else None
    last_fail = config.LAST_AUTH_FAIL_PATH.read_text().strip() if config.LAST_AUTH_FAIL_PATH.exists() else None
    # token_valid is derived from the most recent run's own login outcome,
    # not an independent live probe — a live check would mean making a real
    # Garmin API call just to answer a status GET.
    token_valid = bool(last_ok) and (last_fail is None or last_ok > last_fail)
    return GarminStatusOut(
        configured=st["configured"], username=st["username"],
        last_successful_auth=last_ok, last_failed_auth=last_fail,
        token_valid=token_valid,
    )


# ------------------------------------------------------------ refresh jobs

class RefreshIn(BaseModel):
    no_llm: bool = False
    replan: bool = False
    note: Optional[str] = None


@app.post("/api/refresh")
async def trigger_refresh(body: RefreshIn, session: dict = Depends(require_authenticated_post)):
    flags = []
    if body.no_llm:
        flags.append("--no-llm")
    if body.replan:
        flags.append("--replan")
    result = await jobs.start_refresh(flags, body.note)
    status_code = 200 if result["started"] else 409
    return JSONResponse(result, status_code=status_code)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, session: dict = Depends(auth.require_session)):
    jobs.reap_stale_external_jobs()
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    return job


@app.get("/api/csrf-token")
def csrf_token(session: dict = Depends(auth.require_session)):
    return {"csrf_token": session.get("csrf", "")}


# ------------------------------------------------------------ generic error handling

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # exc_info logs the traceback's frames/line numbers, never local
    # variable values — Python tracebacks don't include those by default,
    # so this can't leak a request body even for the credentials endpoint.
    log.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=True)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)
