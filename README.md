# Running Dashboard

A personalized running dashboard that analyzes your Garmin activity data
(synced locally via [GarminDB](https://github.com/tcgoetz/GarminDB)) and
generates an adaptive, [Hal Higdon](https://www.halhigdon.com/)-principled
training plan for an upcoming race. It runs as a small always-on web app
(`webapp/`, FastAPI) on a server you control — reachable only over your own
[Tailscale](https://tailscale.com/) network, no public inbound ports — with
an Admin tab for entering Garmin credentials, editing race details, and
triggering a refresh from the browser. A background pipeline
(`pipeline/refresh.py`) does the actual sync/analysis/plan-revision work,
either on a nightly systemd timer or on demand from the Admin tab.

```
Garmin watch ──▶ GarminDB (local SQLite) ──▶ pipeline/refresh.py ──▶ docs/data/*.json ──▶ webapp/ (FastAPI, served over Tailscale)
                                                    │
                                             DeepSeek V4 API
                                        (plan revision, elevation vision)
```

Everything sensitive stays on the box: the Garmin password lives only in
one 0600 file the web app can write but never read back, `docs/data/*.json`
is derived/aggregated only (no GPS), and nothing is reachable outside your
tailnet. See `deploy/RUNBOOK.md` for the operational deep-dive and
`CLAUDE.md` for the constraints this design won't compromise on.

## Setup

### 1. Install GarminDB and do the initial import

```bash
pip install garmindb
# Configure ~/.GarminDb/GarminConnectConfig.json with your Garmin credentials
# (see the GarminDB README), then do the first full import:
garmindb_cli.py --all --download --import --analyze
```

This creates the SQLite databases in `~/HealthData/DBs/` (`garmin_activities.db`
etc.). Verify the pipeline can see your runs:

```bash
python pipeline/garmin_extract.py
```

It prints how many running activities it found, the last 10 runs, and any
fields your device/schema version doesn't provide (handled gracefully).

### 2. Install pipeline dependencies

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env
```

Fill in `.env`:

- `DEEPSEEK_API_KEY` — your key from platform.deepseek.com. **Never committed**
  (`.env` is gitignored). Optional: without it the pipeline still produces a
  full deterministic plan, just no LLM refinement/revision notes.
- `DEEPSEEK_VISION_MODEL` — set only if your plan tier includes a
  vision-capable model (used to read the course elevation screenshot). If
  unset, fill in `config/course_segments.yaml` by hand instead.
- `GARMINDB_DIR` — only if your GarminDB databases aren't in `~/HealthData/DBs`.

### 3. Configure your race

`config/race_config.yaml` is gitignored (it's your personal race config, not
tracked in git — see `webapp/race_config.py`'s module docstring for why).
Copy the template once, then edit your copy:

```bash
cp config/race_config.yaml.example config/race_config.yaml
```

```yaml
race:
  name: "My Half Marathon"
  distance_type: "half"      # "half" or "full"
  race_date: "2026-10-18"
  target_time: "1:55:00"
```

Set `preferences.units` to match your GarminDB measurement setting
(`miles` or `km`).

Later, add race-day details in `config/race_info.yaml` (location, expected
weather, hydration points, course notes) and drop a screenshot of the course
elevation map at `config/course_elevation.png` — the dashboard shows
"Not yet configured" until then.

### 4. Run the dashboard

For real deployment (a server you control, reachable over Tailscale), see
`deploy/RUNBOOK.md` — it walks through `deploy/bootstrap.sh`, placing
secrets, first Garmin auth, and starting the systemd units.

For local development, run the FastAPI app directly:

```bash
uvicorn webapp.main:app --reload
```

`docs/data/*.json` is generated, not committed — a fresh clone has none of
it, so the dashboard has nothing to show until you run `make sample` (demo
data, no GarminDB or API key needed) or a real refresh. A plain
`make serve` (static `http.server`) still works for previewing the
read-only tabs, but the Admin tab needs the real backend (`uvicorn`) to do
anything.

### 5. Refresh workflow

After a run (or whenever you want the plan revised), either use the Admin
tab's "Refresh now" button, or run it directly:

```bash
make refresh          # or: python pipeline/refresh.py
```

This:
1. Syncs latest activities (`garmindb_cli.py --activities --download --import --analyze --latest`)
2. Re-extracts runs and recomputes metrics (weekly mileage, long-run
   progression, aerobic efficiency, acute:chronic load ratio, fitness projection)
3. Compares actual training vs. the current plan (done / partial / missed)
4. Asks DeepSeek to revise the **remaining** plan days (past days never change),
   validates the JSON response, and falls back to the existing plan if invalid
5. Writes a human-readable revision note to `docs/data/revision_log.json`
6. Writes `docs/data/*.json`, which the running web app picks up immediately
   (no commit/push step — see `CLAUDE.md`'s Ground Truth)

Useful flags: `--no-sync` (skip Garmin download), `--no-llm` (deterministic
only), `-v` (debug logging).

## How the plan works

- **Real published programs** — the plan tables come from
  [hoovercj/time-to-run](https://github.com/hoovercj/time-to-run) (MIT),
  which encodes 18 programs: Hal Higdon marathon Novice 1/2, Intermediate
  1/2, Advanced 1/2; Hansons beginner/advanced (marathon and half); and
  Pfitzinger 12/18-week marathon (55/70/85 mi) and half (63/84 mi) schedules.
  The tables are companions to the authors' books — support them.
  Converted to `pipeline/plans_catalog.json` by `pipeline/convert_plans.py`.
- **Program selection** — the program is anchored so its final day is race
  day; with less time than its full length you join mid-program, so
  selection tests your recent weekly volume and longest run against the
  demands of the *joining week*, picking the most advanced program you
  clear. Override with `preferences.plan_id` in `config/race_config.yaml`.
- **Compression / extension** — joining mid-program drops the early base
  weeks (peak weeks and taper stay as published); extra time repeats the
  program's week 1 as a base phase. Either way the compromise is stated on
  the dashboard, and pace targets from your current fitness are attached to
  every workout.
- **Goal assessment** — your target is sanity-checked against a Riegel
  projection from your best recent effort and the 10% weekly mileage
  guideline. An unrealistic goal is flagged, not silently ramped to.
- **Race strategy** — the course is split into elevation segments (vision
  model or `config/course_segments.yaml`); each segment gets an even-effort
  pace (≈ +12 s/mi per 1% uphill, −7 s/mi per 1% downhill, capped) normalized
  so the total equals your goal time, with hydration/fueling overlaid.

## Repository layout

```
config/           race + race-info + course-segment configuration
pipeline/         sync/analysis/plan pipeline (pipeline/refresh.py is the entrypoint)
webapp/           FastAPI app — serves docs/, the Admin tab's API, credential handling
deploy/           systemd units, bootstrap.sh, update.sh, RUNBOOK.md
docs/             dashboard front end
  data/           generated JSON (plan, metrics, activities, strategy, log) — gitignored
  assets/         css/js (Chart.js vendored — no CDN, works offline)
```

## Privacy

- `.env`, `*.db`, raw health-data files, and `docs/data/*.json` are gitignored.
- The pipeline never reads GPS columns; the JSON the dashboard reads contains only
  derived, aggregated summaries (dates, distances, paces, HR averages, lap splits).
- The Garmin password lives only in `~/.GarminDb/GarminConnectConfig.json` (0600),
  written by the Admin tab's form but never read back by it — see `CLAUDE.md`.
- No public inbound exposure — the web app is reachable only over Tailscale.
