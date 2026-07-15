# CLAUDE.md — runanalyze

## What this project is
A personalized running dashboard. `pipeline/refresh.py` syncs Garmin activity data
(via GarminDB, local SQLite), recomputes metrics, revises a Hal Higdon–principled
training plan via the DeepSeek API, and writes `docs/data/*.json`. `docs/` is the
dashboard front end (vendored Chart.js, no CDN); `webapp/` is a FastAPI app that serves
it, exposes an Admin tab (Garmin credential form, race/rest-day editing, refresh
trigger + live job status), and reads `docs/data/*.json` straight off disk — there is
no git-publish step, see Ground Truth below.

## Current state: live on Oracle Cloud
Runs on Oracle Always Free (Ampere A1, aarch64, ~2 OCPU / 12 GB, Ubuntu), reachable
only over Tailscale (`tailscale serve`, no public inbound ports). `deploy/` has the
systemd units, `bootstrap.sh`, `update.sh`, and `RUNBOOK.md` for operating it. The
credential entry, live-refresh trigger, and deployment layer described below are
built and running, not aspirational — treat changes here as edits to a live system,
not greenfield design.

## Credential model (deliberate — do not "improve" this without asking)
The real Garmin Connect username and password live in
**`~/.GarminDb/GarminConnectConfig.json`**, mode **0600**, owned by the dedicated service
user. This is GarminDB's native config format and it is what the tool requires. Storing it
lets an unattended run **re-authenticate itself on token expiry** instead of silently
breaking until I intervene. That is the tradeoff I have chosen.

Because the password is at rest, these controls are non-negotiable:
- **Exactly one location.** The password lives in that one 0600 file and NOWHERE else:
  not in the repo, not in git, not in a log or journald entry, not in an exception
  traceback, not in a subprocess argv or `/proc/<pid>/cmdline`, not in a URL or query
  string, not in an env var, not in a tempfile left behind, not in a backup.
- **Write-only in the UI.** The web app may WRITE the credential file. It must never read
  the password back, echo it to the client, or expose it via any endpoint. Status = a
  boolean ("configured" / "last auth OK at <time>"), never the value.
- Writes to that file are **atomic** (temp file in the same dir → `chmod 0600` → rename)
  and **preserve the other keys** already in GarminConnectConfig.json.
- **Never generate a real credential.** Placeholders only. I place secrets by hand.
- The web app must never serve `~/.GarminDb`, `~/HealthData`, or any `.db/.fit/.gpx/.tcx`
  path. Explicit guard + a test.

## Other non-negotiable constraints
- **Raw health data stays on the box**: `~/HealthData/**`, `*.db`, `*.fit/tcx/gpx` are
  never committed and never served. Published JSON is derived/aggregated only — no GPS.
- No containers. Native systemd. Scheduling via a systemd **timer**, not cron.
- Runs as a dedicated non-root service user. Secrets only in chmod 600 files.
- ARM64-native deps — the venv is built on the instance, never copied from my laptop.
- Fail loud: non-zero exit on fatal error. Never publish a stale refresh after a failed
  Garmin sync.

## Ground truth (verified — confirm, don't rediscover)
- Entrypoint is `pipeline/refresh.py`. Flags: `--no-sync`, `--no-llm`, `--replan`,
  `--note`, `-v`. `make refresh` wraps it.
- **There is no git-publish step.** `refresh.py` only writes `docs/data/*.json` to
  disk; it never commits or pushes. `webapp/main.py` serves that JSON straight off
  the instance's filesystem, so a git push would publish nothing a live refresh
  doesn't already. `docs/data/*.json` is gitignored (generated, not versioned) —
  don't reintroduce committing it without a reason. "Deploy" is `deploy/update.sh`
  (`git fetch` + rebase of the app's own *code*, unrelated to dashboard data).
- GarminDB is a separate install (`pip install "garmindb>=3.8.0"`, pinned in
  `bootstrap.sh` — earlier versions use the deprecated `garth` auth library with a
  different session-file location), shelled out to as `garmindb_cli.py --activities
  --download --import --analyze --latest`. It reads
  `~/.GarminDb/GarminConnectConfig.json` and writes SQLite DBs to `~/HealthData/DBs/`.
- `webapp/` is the backend: FastAPI + uvicorn, CSRF-protected (no app-level login —
  Tailscale reachability is the access control, see `webapp/auth.py`), single-flight
  refresh jobs guarded by the same `flock` the systemd timer uses
  (`pipeline/refresh.py`'s `LOCK_PATH`, probed but not owned by `webapp/jobs.py`).
- Deps: `python-dotenv`, `PyYAML`, `requests`, `anthropic` (+ `garmindb`) for the
  pipeline; `fastapi`, `uvicorn`, `itsdangerous`, `httpx`, `pytest` for `webapp/`.
- **Two LLM providers, chosen per-athlete, not per-deployment**: DeepSeek (plain
  `requests` against an OpenAI-compatible endpoint) or Claude (`anthropic` SDK,
  `pipeline/claude_client.py`), selected via `preferences.llm_provider` in
  `race_config.yaml` — an Admin-tab field, not an env var — through
  `pipeline/llm_client.get_llm_client()`. Both clients share the same prompts/
  validators (defined in `deepseek_client.py`, imported by `claude_client.py`).
- The "AI Plan" tab is a separate comparison view (`docs/data/llm_plan.json`,
  written by `pipeline/generate_ai_plan.py`) — de novo (LLM authors a full plan
  from scratch) or blended (sweeping revision of the auto-selected catalog
  program). Never touches `plan.json`; not part of the nightly timer, triggered
  on demand only, and reuses the last refresh's `metrics.json` rather than
  re-syncing Garmin.
- Cost per refresh is ~1 LLM call; `--no-llm` still yields a full deterministic plan.

## Conventions carried over from my other Oracle deployments
- systemd unit + timer, EnvironmentFile (0600), journald for logs, venv on the host.
- Reuse the Telegram notifier from the acuity project for failure alerts — don't write a
  new one.

## How I want you to work
- Plan before code. Present the plan and **stop** for my approval before writing files.
- Verify claims by reading the code; don't take my summary on faith. Correct me when I'm wrong.
- State assumptions explicitly. Flag uncertainty instead of guessing.
