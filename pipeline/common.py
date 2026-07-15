"""Shared helpers for the running-dashboard pipeline."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import yaml

log = logging.getLogger("pipeline")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "docs" / "data"

KM_PER_MILE = 1.609344


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def load_yaml(path: Path) -> dict:
    if not path.exists():
        log.warning("Config file not found: %s", path)
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_race_config() -> dict:
    cfg = load_yaml(CONFIG_DIR / "race_config.yaml")
    cfg.setdefault("race", {})
    cfg.setdefault("preferences", {})
    cfg["preferences"].setdefault("units", "miles")
    cfg["preferences"].setdefault("long_run_day", "sunday")
    cfg["preferences"].setdefault("rest_days", [])
    cfg["preferences"].setdefault("blocked_dates", [])
    cfg["preferences"].setdefault("max_run_days_per_week", 5)
    cfg["preferences"].setdefault("activities_since", "")
    cfg["preferences"].setdefault("activities_weeks_back", 0)
    cfg["preferences"].setdefault("llm_provider", "deepseek")
    return cfg


def load_race_info() -> dict:
    cfg = load_yaml(CONFIG_DIR / "race_info.yaml")
    info = cfg.get("race_info") or {}
    info.setdefault("location", "")
    info.setdefault("expected_temperature", None)
    info.setdefault("humidity", None)
    info.setdefault("elevation_map_image", "")
    info.setdefault("hydration_points", [])
    info.setdefault("course_notes", "")
    return info


def write_json(name: str, payload) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("Wrote %s", path.relative_to(REPO_ROOT))
    return path


def read_json(name: str, default=None):
    path = DATA_DIR / name
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------- time utils

def parse_duration_s(value) -> float | None:
    """Parse a GarminDB duration into seconds.

    GarminDB stores durations as 'HH:MM:SS' / 'HH:MM:SS.ffffff' strings,
    but numeric seconds also appear in some schema versions.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        parts = s.split(":")
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        return float(s)
    except ValueError:
        log.debug("Could not parse duration %r", value)
        return None


def parse_time_hms(value: str) -> int | None:
    """Parse 'HH:MM:SS' goal time into seconds."""
    secs = parse_duration_s(value)
    return int(secs) if secs else None


def fmt_hms(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pace(sec_per_unit: float | None) -> str:
    """Format seconds-per-mile(or km) as M:SS."""
    if not sec_per_unit or sec_per_unit <= 0:
        return ""
    m, s = divmod(int(round(sec_per_unit)), 60)
    return f"{m}:{s:02d}"


def parse_date(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        log.debug("Could not parse date %r", value)
        return None


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
