"""FastAPI app: Garmin credential form, refresh trigger, job polling, and
the existing static docs/ dashboard — served unchanged.

No app-level login — Tailscale's tailnet-only reachability is the access
control (see webapp/auth.py's module docstring for the tradeoff this
accepts). CSRF protection still applies to every state-changing POST.
Nothing here ever echoes the Garmin password back, logs it, or exposes it
via any response model — response models for credential endpoints simply
have no password field, so it's structurally impossible to leak it that
way, not just a matter of remembering to omit it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import auth, config, garmin_config, jobs, race_config

log = logging.getLogger("webapp")

# debug=False is load-bearing, not a default to leave alone: FastAPI's
# debug mode echoes request data and stack locals in its error pages.
app = FastAPI(debug=False, title="runanalyze")


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


# ------------------------------------------------------------ health + csrf plumbing

@app.get("/health")
def health():
    return {"status": "ok"}


def require_csrf_post(request: Request, payload: dict = Depends(auth.ensure_csrf)) -> dict:
    # No `response: Response` param needed here — auth.ensure_csrf's own
    # declaration is what makes FastAPI merge its cookie into the final
    # response for these plain-dict-returning routes.
    auth.require_csrf(request, payload)
    return payload


# ------------------------------------------------------------ dashboard (existing, unchanged)
#
# These three routes return FileResponse directly, so they use
# apply_csrf_cookie() on that actual object rather than the ensure_csrf
# dependency — see auth.py's docstrings for why the dependency form can't
# work here (verified empirically: FastAPI drops dependency-set cookies
# when the endpoint returns its own Response object).

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    resp = FileResponse(_safe_file(config.DOCS_DIR, "index.html"))
    auth.apply_csrf_cookie(request, resp)
    return resp


@app.get("/assets/{rel_path:path}")
def assets(rel_path: str, request: Request):
    _reject_forbidden_suffix(rel_path)
    resp = FileResponse(_safe_file(config.DOCS_DIR / "assets", rel_path))
    auth.apply_csrf_cookie(request, resp)
    return resp


@app.get("/data/{rel_path:path}")
def data(rel_path: str, request: Request):
    _reject_forbidden_suffix(rel_path)
    path = _safe_file(config.DOCS_DATA_DIR, rel_path)
    # Never let a cache serve stale JSON after a refresh.
    resp = FileResponse(path, headers={"Cache-Control": "no-store"})
    auth.apply_csrf_cookie(request, resp)
    return resp


# ------------------------------------------------------------ garmin credentials

class CredentialsIn(BaseModel):
    username: str
    password: str


class OkOut(BaseModel):
    ok: bool
    # Deliberately no other fields — see module docstring.


@app.post("/api/garmin/credentials", response_model=OkOut)
def set_garmin_credentials(body: CredentialsIn, payload: dict = Depends(require_csrf_post)):
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
def garmin_status(payload: dict = Depends(auth.ensure_csrf)):
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
    no_sync: bool = False
    note: Optional[str] = None


@app.post("/api/refresh")
async def trigger_refresh(body: RefreshIn, payload: dict = Depends(require_csrf_post)):
    flags = []
    if body.no_llm:
        flags.append("--no-llm")
    if body.replan:
        flags.append("--replan")
    if body.no_sync:
        flags.append("--no-sync")
    result = await jobs.start_refresh(flags, body.note)
    status_code = 200 if result["started"] else 409
    return JSONResponse(result, status_code=status_code)


# ------------------------------------------------------------ AI plan (de novo / blended)

class AIPlanGenerateIn(BaseModel):
    mode: str  # "denovo" or "blended"


@app.post("/api/ai-plan/generate")
async def trigger_ai_plan(body: AIPlanGenerateIn, payload: dict = Depends(require_csrf_post)):
    if body.mode not in ("denovo", "blended"):
        raise HTTPException(status_code=400, detail="mode must be 'denovo' or 'blended'")
    result = await jobs.start_ai_plan_job(body.mode)
    status_code = 200 if result["started"] else 409
    return JSONResponse(result, status_code=status_code)


# ------------------------------------------------------------ rest days

class RaceConfigOut(BaseModel):
    rest_days: list[str]
    blocked_dates: list[str]
    name: str
    distance_type: str
    race_date: str
    target_time: str
    long_run_day: str
    plan_id: str
    activities_weeks_back: int
    plan_catalog: dict[str, list[str]]


class RestDaysIn(BaseModel):
    days: list[str]


class BlockedDatesIn(BaseModel):
    dates: list[str]


class RaceDetailsIn(BaseModel):
    name: str
    distance_type: str
    race_date: str
    target_time: str
    long_run_day: str
    plan_id: str = ""  # "" = auto-select, see race_config.read_race_details()
    activities_weeks_back: int = 0  # 0 = disabled, falls back to activities_since


def _race_config_out() -> RaceConfigOut:
    return RaceConfigOut(
        rest_days=race_config.read_rest_days(),
        blocked_dates=race_config.read_blocked_dates(),
        plan_catalog=race_config.plan_catalog_by_distance_type(),
        **race_config.read_race_details(),
    )


@app.get("/api/race-config", response_model=RaceConfigOut)
def get_race_config(payload: dict = Depends(auth.ensure_csrf)):
    return _race_config_out()


@app.post("/api/race-config/blocked-dates", response_model=RaceConfigOut)
def set_blocked_dates(body: BlockedDatesIn, payload: dict = Depends(require_csrf_post)):
    try:
        race_config.write_blocked_dates(body.dates)
    except race_config.RaceConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log.info("blocked_dates updated: %s", body.dates)
    return _race_config_out()


@app.post("/api/race-config/rest-days", response_model=RaceConfigOut)
def set_rest_days(body: RestDaysIn, payload: dict = Depends(require_csrf_post)):
    try:
        race_config.write_rest_days(body.days)
    except race_config.RaceConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log.info("rest_days updated: %s", body.days)
    return _race_config_out()


@app.post("/api/race-config/details", response_model=RaceConfigOut)
def set_race_details(body: RaceDetailsIn, payload: dict = Depends(require_csrf_post)):
    try:
        race_config.write_race_details(
            name=body.name, distance_type=body.distance_type, race_date=body.race_date,
            target_time=body.target_time, long_run_day=body.long_run_day, plan_id=body.plan_id,
            activities_weeks_back=body.activities_weeks_back,
        )
    except race_config.RaceConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log.info("race details updated: distance_type=%s race_date=%s plan_id=%s",
              body.distance_type, body.race_date, body.plan_id or "(auto)")
    return _race_config_out()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, payload: dict = Depends(auth.ensure_csrf)):
    jobs.reap_stale_external_jobs()
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    return job


@app.get("/api/csrf-token")
def csrf_token(payload: dict = Depends(auth.ensure_csrf)):
    return {"csrf_token": payload.get("csrf", "")}


# ------------------------------------------------------------ generic error handling

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # exc_info logs the traceback's frames/line numbers, never local
    # variable values — Python tracebacks don't include those by default,
    # so this can't leak a request body even for the credentials endpoint.
    log.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=True)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)
