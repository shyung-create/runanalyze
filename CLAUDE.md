# CLAUDE.md — runanalyze

## What this project is
A personalized running dashboard. `pipeline/refresh.py` syncs Garmin activity data
(via GarminDB, local SQLite), recomputes metrics, revises a Hal Higdon–principled
training plan via the DeepSeek API, writes `docs/data/*.json`, and git-commits +
pushes. `docs/` is a pure static site (vendored Chart.js, no CDN) that reads that JSON.

## Current work: migrating to Oracle Cloud as a live web dashboard
Target: Oracle Always Free (Ampere A1, aarch64, ~2 OCPU / 12 GB, Ubuntu), serving a
web UI where I enter Garmin credentials, trigger a live pull, and watch the plan update.

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
- Entrypoint is `pipeline/refresh.py`. Flags: `--push`, `--no-sync`, `--no-llm`,
  `--no-git`, `--replan`, `--note`, `-v`. `make refresh` wraps it.
- **Publishing is a git push, not an rsync.** `git_publish()` runs `git commit` then
  `git push -u origin <branch>` from REPO_ROOT — the published dashboard *is* this repo.
  So the instance needs a git clone with a **write-capable deploy key**; "deploy" is
  `git pull`. This inverts the deploy model used in my other Oracle projects.
- **Unattended runs must pass `--push`.** Without a tty, `git_publish` hits an `input()`
  prompt and silently does not push.
- GarminDB is a separate install (`pip install garmindb`), shelled out to as
  `garmindb_cli.py --activities --download --import --analyze --latest`. It reads
  `~/.GarminDb/GarminConnectConfig.json` and writes SQLite DBs to `~/HealthData/DBs/`.
- `docs/` has **no backend and no interactivity today** — every dynamic feature is net-new.
  Extend the existing dashboard; do not rewrite it.
- Deps are light: python-dotenv, PyYAML, requests (+ garmindb). DeepSeek over plain
  `requests` against an OpenAI-compatible endpoint.
- Cost per refresh is ~1 LLM call; `--no-llm` still yields a full deterministic plan.
  The hard problems are **auth, exposure, and job orchestration** — not API spend.

## Conventions carried over from my other Oracle deployments
- systemd unit + timer, EnvironmentFile (0600), journald for logs, venv on the host.
- Reuse the Telegram notifier from the acuity project for failure alerts — don't write a
  new one.

## How I want you to work
- Plan before code. Present the plan and **stop** for my approval before writing files.
- Verify claims by reading the code; don't take my summary on faith. Correct me when I'm wrong.
- State assumptions explicitly. Flag uncertainty instead of guessing.
