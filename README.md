# Running Dashboard

A personalized running dashboard that analyzes your Garmin activity data
(synced locally via [GarminDB](https://github.com/tcgoetz/GarminDB)) and
generates an adaptive, [Hal Higdon](https://www.halhigdon.com/)-principled
training plan for an upcoming race. The dashboard is a static site on
**GitHub Pages**; a local Python pipeline refreshes the data and revises the
plan (via the DeepSeek API) whenever you run it.

```
Garmin watch ──▶ GarminDB (local SQLite) ──▶ pipeline/refresh.py ──▶ docs/data/*.json ──▶ GitHub Pages
                                                    │
                                             DeepSeek V4 API
                                        (plan revision, elevation vision)
```

Everything sensitive stays on your machine: the published site is pure
HTML/CSS/JS reading static JSON — **no API keys, no raw health data, no GPS
coordinates** ever leave your computer.

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

Edit `config/race_config.yaml`:

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

### 4. Enable GitHub Pages

Repo → Settings → Pages → Source: **Deploy from a branch**, branch
`main` (or your default), folder **`/docs`**. The site appears at
`https://<user>.github.io/<repo>/`.

The repo ships with **sample data** in `docs/data/` so the site renders
before your first real refresh (the header shows "sample data").
Regenerate it anytime with `make sample`.

### 5. Refresh workflow

After a run (or whenever you want the plan revised):

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
6. Commits the new JSON and asks before pushing (use `make refresh-push` or
   `--push` to skip the prompt)

Useful flags: `--no-sync` (skip Garmin download), `--no-llm` (deterministic
only), `--no-git` (don't commit), `-v` (debug logging).

Preview locally with `make serve` → http://localhost:8000.

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
pipeline/         local Python pipeline (never runs in CI or on Pages)
docs/             GitHub Pages root — static dashboard
  data/           generated JSON (plan, metrics, activities, strategy, log)
  assets/         css/js (Chart.js vendored — no CDN, works offline)
```

## Privacy

- `.env`, `*.db`, and raw health-data files are gitignored.
- The pipeline never reads GPS columns; published JSON contains only derived,
  aggregated summaries (dates, distances, paces, HR averages, lap splits).
- Review `docs/data/*.json` before making the repo public if you're unsure.
