"""Extract running activities from local GarminDB SQLite databases.

GarminDB schemas vary by device and package version, so every query is built
from the columns that actually exist (via PRAGMA table_info). Missing fields
are logged once and returned as None instead of crashing.

Run directly to print a summary of what was found:
    python pipeline/garmin_extract.py
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from common import log, parse_date, parse_duration_s, KM_PER_MILE

# field name we expose  ->  candidate column names in GarminDB, in priority order
ACTIVITY_FIELDS = {
    "activity_id": ["activity_id"],
    "name": ["name"],
    "sport": ["sport"],
    "sub_sport": ["sub_sport"],
    "start_time": ["start_time"],
    "distance": ["distance"],
    "elapsed_time": ["elapsed_time"],
    "moving_time": ["moving_time"],
    "avg_hr": ["avg_hr"],
    "max_hr": ["max_hr"],
    "avg_cadence": ["avg_rpms", "avg_cadence"],
    "max_cadence": ["max_rpms", "max_cadence"],
    "ascent": ["ascent"],
    "descent": ["descent"],
    "calories": ["calories"],
    "training_effect": ["training_effect"],
    "anaerobic_training_effect": ["anaerobic_training_effect"],
    "avg_speed": ["avg_speed"],
    "self_eval_feel": ["self_eval_feel"],
}

STEPS_FIELDS = {
    "avg_pace": ["avg_pace"],
    "max_pace": ["max_pace"],
    "avg_steps_per_min": ["avg_steps_per_min"],
    "avg_step_length": ["avg_step_length"],
    "vo2_max": ["vo2_max"],
}

LAP_FIELDS = {
    "activity_id": ["activity_id"],
    "lap": ["lap"],
    "distance": ["distance"],
    "elapsed_time": ["elapsed_time"],
    "moving_time": ["moving_time"],
    "avg_hr": ["avg_hr"],
    "max_hr": ["max_hr"],
    "ascent": ["ascent"],
    "descent": ["descent"],
}


def db_dir() -> Path:
    return Path(os.environ.get("GARMINDB_DIR", "~/HealthData/DBs")).expanduser()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    except sqlite3.Error:
        return set()


def _build_select(conn, table: str, field_map: dict) -> tuple[str, list[str], list[str]]:
    """Return (select clause, selected field names, missing field names)."""
    cols = _columns(conn, table)
    selects, fields, missing = [], [], []
    for field, candidates in field_map.items():
        col = next((c for c in candidates if c in cols), None)
        if col:
            selects.append(f"{col} AS {field}")
            fields.append(field)
        else:
            missing.append(field)
    return ", ".join(selects), fields, missing


def extract_runs(limit_days: int | None = None, start_date: str | None = None) -> dict:
    """Return {'activities': [...], 'missing_fields': [...], 'db_path': str}.

    Each activity dict has the keys of ACTIVITY_FIELDS + STEPS_FIELDS + 'laps',
    with None for anything the local schema doesn't provide. Distances/paces
    are returned in the unit GarminDB was configured with (see README).
    GPS coordinates are intentionally never read — nothing location-derived
    is published.

    start_date (YYYY-MM-DD, optional) excludes activities before that date —
    useful to ignore older history entirely (e.g. old devices, bad FIT
    parses) without needing GarminDB to re-import a trimmed set. This is a
    string comparison against start_time's 'YYYY-MM-DD ...' text format,
    which sorts correctly lexicographically.
    """
    path = db_dir() / "garmin_activities.db"
    if not path.exists():
        raise FileNotFoundError(
            f"GarminDB activities database not found at {path}. "
            "Set GARMINDB_DIR in .env or run the GarminDB initial import first."
        )

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    all_missing: list[str] = []
    try:
        if "activities" not in _tables(conn):
            raise RuntimeError(f"No 'activities' table in {path}")

        sel, _, missing = _build_select(conn, "activities", ACTIVITY_FIELDS)
        all_missing += missing
        conditions = []
        if "sport" not in missing:
            conditions.append("lower(sport) = 'running'")
        else:
            log.warning("No 'sport' column — returning ALL activities, filter manually")
        if limit_days and "start_time" not in missing:
            conditions.append(f"start_time >= date('now', '-{int(limit_days)} day')")
        if start_date and "start_time" not in missing:
            conditions.append("start_time >= ?")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params = [start_date] if (start_date and "start_time" not in missing) else []
        order = "ORDER BY start_time" if "start_time" not in missing else ""
        rows = conn.execute(f"SELECT {sel} FROM activities {where} {order}", params).fetchall()
        activities = [dict(r) for r in rows]

        # Running-specific fields live in steps_activities in most versions
        steps = {}
        if "steps_activities" in _tables(conn):
            sel2, fields2, missing2 = _build_select(conn, "steps_activities", STEPS_FIELDS)
            all_missing += missing2
            if fields2:
                for r in conn.execute(
                    f"SELECT activity_id, {sel2} FROM steps_activities"
                ).fetchall():
                    steps[r["activity_id"]] = dict(r)
        else:
            all_missing += list(STEPS_FIELDS)
            log.warning("No steps_activities table — pace/cadence detail unavailable")

        laps = _extract_laps(conn)
        for a in activities:
            a.update(steps.get(a.get("activity_id"), {k: None for k in STEPS_FIELDS}))
            a["laps"] = laps.get(a.get("activity_id"), [])
            _normalize(a)

        if all_missing:
            log.info("Fields not found in this GarminDB schema: %s",
                     ", ".join(sorted(set(all_missing))))
        return {
            "activities": activities,
            "missing_fields": sorted(set(all_missing)),
            "db_path": str(path),
        }
    finally:
        conn.close()


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _extract_laps(conn) -> dict[str, list[dict]]:
    table = next((t for t in ("activity_laps", "laps") if t in _tables(conn)), None)
    if not table:
        log.info("No lap table found — splits unavailable")
        return {}
    sel, fields, _ = _build_select(conn, table, LAP_FIELDS)
    if "activity_id" not in fields:
        return {}
    out: dict[str, list[dict]] = {}
    for r in conn.execute(f"SELECT {sel} FROM {table} ORDER BY activity_id, lap"):
        d = dict(r)
        d["elapsed_time_s"] = parse_duration_s(d.get("elapsed_time"))
        d["moving_time_s"] = parse_duration_s(d.get("moving_time"))
        out.setdefault(d["activity_id"], []).append(d)
    return out


def _normalize(a: dict) -> None:
    """Add parsed convenience fields to an activity dict, in place.

    Pace is always DERIVED from duration / distance rather than read from
    GarminDB's stored avg_pace column: that column's per-mile vs per-km
    basis doesn't reliably track the distance unit (observed storing
    min/mile alongside km distances), which poisoned every downstream
    fitness/pace calculation. Duration and distance are trustworthy, so
    the ratio always is too; the stored value is only a last resort when
    no duration exists.
    """
    for k in ACTIVITY_FIELDS:
        a.setdefault(k, None)
    dt = parse_date(a.get("start_time"))
    a["date"] = dt.strftime("%Y-%m-%d") if dt else None
    a["elapsed_time_s"] = parse_duration_s(a.get("elapsed_time"))
    a["moving_time_s"] = parse_duration_s(a.get("moving_time"))
    a["avg_pace_s"] = None
    dur = a["moving_time_s"] or a["elapsed_time_s"]
    if dur and a.get("distance"):
        try:
            a["avg_pace_s"] = dur / float(a["distance"])
        except (TypeError, ZeroDivisionError, ValueError):
            pass
    if a["avg_pace_s"] is None:  # last resort: stored column, basis unknown
        a["avg_pace_s"] = parse_duration_s(a.get("avg_pace"))


def detect_garmindb_units() -> str:
    """Best-effort detection of the units GarminDB itself stored distances
    in, from its own config file (~/.GarminDb/GarminConnectConfig.json ->
    settings.metric). This is independent of race_config.yaml's
    preferences.units, which only controls dashboard *display* — without
    this, a metric GarminDB install silently mislabels km as miles.
    Override with GARMINDB_UNITS=km|miles in .env if detection is wrong
    or the config lives elsewhere.
    """
    override = os.environ.get("GARMINDB_UNITS", "").strip().lower()
    if override in ("km", "miles"):
        return override
    cfg_path = Path.home() / ".GarminDb" / "GarminConnectConfig.json"
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        metric = cfg.get("settings", {}).get("metric")
        if metric is not None:
            return "km" if metric else "miles"
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError):
        pass
    log.warning("Could not detect GarminDB units from %s — assuming miles. "
               "If your Garmin data is actually in km, set GARMINDB_UNITS=km "
               "in .env (or fix settings.metric in that file).", cfg_path)
    return "miles"


FEET_PER_METER = 3.28084


def convert_units(activities: list[dict], db_units: str, target_units: str) -> None:
    """Convert distance/pace/elevation fields between km+m and miles+ft, in place."""
    if db_units == target_units:
        return
    f = 1 / KM_PER_MILE if target_units == "miles" else KM_PER_MILE
    # elevation: db_units km implies meters stored, miles implies feet stored
    ef = FEET_PER_METER if target_units == "miles" else (1 / FEET_PER_METER)
    for a in activities:
        if a.get("distance") is not None:
            a["distance"] = float(a["distance"]) * f
        if a.get("avg_pace_s"):
            a["avg_pace_s"] = a["avg_pace_s"] / f
        for key in ("ascent", "descent"):
            if a.get(key) is not None:
                a[key] = float(a[key]) * ef
        for lap in a.get("laps", []):
            if lap.get("distance") is not None:
                lap["distance"] = float(lap["distance"]) * f
            for key in ("ascent", "descent"):
                if lap.get(key) is not None:
                    lap[key] = float(lap[key]) * ef


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = extract_runs()
    acts = result["activities"]
    print(f"\nDatabase: {result['db_path']}")
    print(f"Running activities found: {len(acts)}")
    if result["missing_fields"]:
        print(f"Fields missing from schema: {', '.join(result['missing_fields'])}")
    for a in acts[-10:]:
        from common import fmt_pace
        print(f"  {a['date']}  {a.get('distance') or 0:6.2f}  "
              f"pace {fmt_pace(a.get('avg_pace_s')) or '--'}  "
              f"HR {a.get('avg_hr') or '--'}  laps {len(a['laps'])}")


if __name__ == "__main__":
    main()
