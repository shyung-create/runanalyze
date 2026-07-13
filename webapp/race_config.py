"""Read/write config/race_config.yaml's preferences.rest_days and
preferences.blocked_dates.

Deliberately NOT a full YAML parse-and-redump: race_config.yaml carries
extensive human-written comments (field docs, valid plan_id list) that
yaml.safe_load()/yaml.dump() would silently discard on every save. Instead
this does a targeted single-line regex replace on the one line that
matters, leaving every comment and everything else in the file untouched.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from datetime import date

import yaml

from . import config

VALID_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


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


def _write_flow_list(pattern: re.Pattern, field_name: str, values: list[str]) -> None:
    path = config.RACE_CONFIG_FILE
    text = path.read_text(encoding="utf-8")
    new_value = "[" + ", ".join(values) + "]"
    new_text, count = pattern.subn(lambda m: m.group(1) + new_value + m.group(3), text, count=1)
    if count != 1:
        raise RaceConfigError(
            f"could not find a '{field_name}:' line to update in race_config.yaml — "
            "the file may have been hand-edited into a different format; edit it directly instead"
        )
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
