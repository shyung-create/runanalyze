"""Read/write config/race_config.yaml's race section and preferences.

Deliberately NOT a full YAML parse-and-redump: race_config.yaml carries
extensive human-written comments (field docs, valid plan_id list) that
yaml.safe_load()/yaml.dump() would silently discard on every save. Instead
this does a targeted single-line regex replace on the one line that
matters, leaving every comment and everything else in the file untouched.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from datetime import date, datetime

import yaml

from . import config

VALID_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
VALID_DISTANCE_TYPES = ["half", "full"]
VALID_LLM_PROVIDERS = ["deepseek", "claude"]


def _plan_catalog() -> list[dict]:
    return json.loads(config.PLANS_CATALOG_FILE.read_text(encoding="utf-8"))["plans"]


def _plan_ids_for(distance_type: str) -> set[str]:
    return {p["id"] for p in _plan_catalog() if p["distance_type"] == distance_type}


def plan_catalog_by_distance_type() -> dict[str, list[str]]:
    """For populating the plan_id dropdown client-side, grouped and sorted
    per distance_type so the UI can show only the ids relevant to whichever
    distance_type is currently selected."""
    result: dict[str, list[str]] = {}
    for p in _plan_catalog():
        result.setdefault(p["distance_type"], []).append(p["id"])
    for ids in result.values():
        ids.sort()
    return result


def _flow_list_re(key: str) -> re.Pattern:
    """Matches e.g. "  rest_days: []         # weekdays that are ALWAYS rest, ...".
    Group 1: everything up to and including the opening bracket's preceding
    whitespace. Group 2: the flow-style list itself, e.g. "[]" or
    "[friday, monday]" — matched non-greedily up to the FIRST "]", so a "]"
    inside a trailing comment (both fields' comments show an example list)
    is never mistaken for the value's closing bracket. Group 3: everything
    after, comment included, untouched."""
    return re.compile(rf"^(\s*{re.escape(key)}:\s*)(\[[^\]]*\])(.*)$", re.MULTILINE)


_REST_DAYS_RE = _flow_list_re("rest_days")
_BLOCKED_DATES_RE = _flow_list_re("blocked_dates")


def _quoted_scalar_re(key: str) -> re.Pattern:
    """Matches an active (uncommented) e.g. '  name: "SF Marathon"    # ...'.
    Group 1: indent + key + ": ". Group 2: the quoted value's inner text.
    Group 3: everything after the closing quote, trailing comment included.
    Doesn't match a '#'-commented line — only plan_id is ever commented out
    by default, and that field gets its own regex below."""
    return re.compile(rf'^(\s*{re.escape(key)}:\s*)"([^"]*)"(.*)$', re.MULTILINE)


_NAME_RE = _quoted_scalar_re("name")
_DISTANCE_TYPE_RE = _quoted_scalar_re("distance_type")
_RACE_DATE_RE = _quoted_scalar_re("race_date")
_TARGET_TIME_RE = _quoted_scalar_re("target_time")
_LONG_RUN_DAY_RE = _quoted_scalar_re("long_run_day")
_LLM_PROVIDER_RE = _quoted_scalar_re("llm_provider")


def _int_scalar_re(key: str) -> re.Pattern:
    """Matches an unquoted integer scalar, e.g. '  activities_weeks_back: 0  # ...'.
    Same group shape as _quoted_scalar_re (indent+key, value, trailing comment)
    but without quotes — no existing preference used a bare numeric scalar
    before this one (max_run_days_per_week has no reader/writer here today)."""
    return re.compile(rf"^(\s*{re.escape(key)}:\s*)(-?\d+)(.*)$", re.MULTILINE)


_ACTIVITIES_WEEKS_BACK_RE = _int_scalar_re("activities_weeks_back")

# plan_id is commented out by default ("# plan_id: ...") since an unset value
# means "auto-select" — unlike every other field here, writing it has to be
# able to toggle the leading "# " on or off, not just replace the value.
_PLAN_ID_RE = re.compile(r'^(?P<indent>\s*)(?:#\s*)?plan_id:\s*"[^"]*"(?P<rest>.*)$', re.MULTILINE)


class RaceConfigError(Exception):
    pass


def _read_preference(key: str) -> list:
    cfg = yaml.safe_load(config.RACE_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    return list((cfg.get("preferences") or {}).get(key) or [])


def _atomic_write_text(path, new_text: str) -> None:
    # Same atomicity guarantee as webapp/garmin_config.py: temp file in the
    # same directory (required for os.replace() to be atomic), fsync
    # before the swap, no reader ever sees a partial write.
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".race_config.yaml.tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _not_found_error(field_name: str) -> RaceConfigError:
    return RaceConfigError(
        f"could not find a '{field_name}:' line to update in race_config.yaml — "
        "the file may have been hand-edited into a different format; edit it directly instead"
    )


def _write_flow_list(pattern: re.Pattern, field_name: str, values: list[str]) -> None:
    path = config.RACE_CONFIG_FILE
    text = path.read_text(encoding="utf-8")
    new_value = "[" + ", ".join(values) + "]"
    new_text, count = pattern.subn(lambda m: m.group(1) + new_value + m.group(3), text, count=1)
    if count != 1:
        raise _not_found_error(field_name)
    _atomic_write_text(path, new_text)


def _write_quoted_scalar(pattern: re.Pattern, field_name: str, value: str) -> None:
    path = config.RACE_CONFIG_FILE
    text = path.read_text(encoding="utf-8")
    new_text, count = pattern.subn(lambda m: m.group(1) + f'"{value}"' + m.group(3), text, count=1)
    if count != 1:
        raise _not_found_error(field_name)
    _atomic_write_text(path, new_text)


def _write_int_scalar(pattern: re.Pattern, field_name: str, value: int) -> None:
    path = config.RACE_CONFIG_FILE
    text = path.read_text(encoding="utf-8")
    new_text, count = pattern.subn(lambda m: m.group(1) + str(value) + m.group(3), text, count=1)
    if count != 1:
        raise _not_found_error(field_name)
    _atomic_write_text(path, new_text)


def _write_plan_id(value: str) -> None:
    path = config.RACE_CONFIG_FILE
    text = path.read_text(encoding="utf-8")

    def repl(m: re.Match) -> str:
        indent, rest = m.group("indent"), m.group("rest")
        if value:
            return f'{indent}plan_id: "{value}"{rest}'
        return f'{indent}# plan_id: ""{rest}'

    new_text, count = _PLAN_ID_RE.subn(repl, text, count=1)
    if count != 1:
        raise _not_found_error("plan_id")
    _atomic_write_text(path, new_text)


def read_rest_days() -> list[str]:
    return _read_preference("rest_days")


def write_rest_days(days: list[str]) -> None:
    days = [str(d).strip().lower() for d in days]
    invalid = sorted(set(days) - set(VALID_DAYS))
    if invalid:
        raise RaceConfigError(f"not valid weekday names: {invalid}")
    _write_flow_list(_REST_DAYS_RE, "rest_days", days)


def read_blocked_dates() -> list[str]:
    return sorted(str(d) for d in _read_preference("blocked_dates"))


def write_blocked_dates(dates: list[str]) -> None:
    dates = [str(d).strip() for d in dates]
    invalid = []
    for d in dates:
        try:
            date.fromisoformat(d)
        except ValueError:
            invalid.append(d)
    if invalid:
        raise RaceConfigError(f"not valid YYYY-MM-DD dates: {invalid}")
    dates = sorted(set(dates))  # dedupe, stable order
    _write_flow_list(_BLOCKED_DATES_RE, "blocked_dates", dates)


# ------------------------------------------------------------ race details

def read_race_details() -> dict:
    cfg = yaml.safe_load(config.RACE_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    race = cfg.get("race") or {}
    prefs = cfg.get("preferences") or {}
    return {
        "name": str(race.get("name") or ""),
        "distance_type": str(race.get("distance_type") or ""),
        "race_date": str(race.get("race_date") or ""),
        "target_time": str(race.get("target_time") or ""),
        "long_run_day": str(prefs.get("long_run_day") or ""),
        # "" means unset — plan_generator.py auto-selects a program.
        "plan_id": str(prefs.get("plan_id") or ""),
        "activities_weeks_back": int(prefs.get("activities_weeks_back") or 0),
        "llm_provider": str(prefs.get("llm_provider") or "deepseek"),
    }


def write_race_details(*, name: str, distance_type: str, race_date: str,
                        target_time: str, long_run_day: str, plan_id: str,
                        activities_weeks_back: int, llm_provider: str) -> None:
    name = name.strip()
    distance_type = distance_type.strip().lower()
    race_date = race_date.strip()
    target_time = target_time.strip()
    long_run_day = long_run_day.strip().lower()
    plan_id = plan_id.strip()
    llm_provider = llm_provider.strip().lower()

    # Validate everything before writing anything, so a bad field never
    # leaves the file half-updated.
    if not name or '"' in name or "\n" in name:
        raise RaceConfigError("race name must be non-empty and must not contain a double-quote")
    if distance_type not in VALID_DISTANCE_TYPES:
        raise RaceConfigError(f"distance_type must be one of {VALID_DISTANCE_TYPES}")
    try:
        date.fromisoformat(race_date)
    except ValueError:
        raise RaceConfigError(f"race_date {race_date!r} is not a valid YYYY-MM-DD date")
    try:
        datetime.strptime(target_time, "%H:%M:%S")
    except ValueError:
        raise RaceConfigError(f"target_time {target_time!r} is not a valid HH:MM:SS time")
    if long_run_day not in VALID_DAYS:
        raise RaceConfigError(f"long_run_day must be one of {VALID_DAYS}")
    if plan_id and plan_id not in _plan_ids_for(distance_type):
        raise RaceConfigError(
            f"plan_id {plan_id!r} is not a valid plan for distance_type {distance_type!r} — "
            f"valid ids: {sorted(_plan_ids_for(distance_type))}"
        )
    if activities_weeks_back < 0:
        raise RaceConfigError("activities_weeks_back must be >= 0")
    if llm_provider not in VALID_LLM_PROVIDERS:
        raise RaceConfigError(f"llm_provider must be one of {VALID_LLM_PROVIDERS}")

    _write_quoted_scalar(_NAME_RE, "name", name)
    _write_quoted_scalar(_DISTANCE_TYPE_RE, "distance_type", distance_type)
    _write_quoted_scalar(_RACE_DATE_RE, "race_date", race_date)
    _write_quoted_scalar(_TARGET_TIME_RE, "target_time", target_time)
    _write_quoted_scalar(_LONG_RUN_DAY_RE, "long_run_day", long_run_day)
    _write_plan_id(plan_id)
    _write_int_scalar(_ACTIVITIES_WEEKS_BACK_RE, "activities_weeks_back", activities_weeks_back)
    _write_quoted_scalar(_LLM_PROVIDER_RE, "llm_provider", llm_provider)
