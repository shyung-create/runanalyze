"""Read/write config/race_config.yaml's preferences.rest_days.

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

import yaml

from . import config

VALID_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Matches e.g. "  rest_days: []         # weekdays that are ALWAYS rest, ..."
# Group 1: everything up to and including the opening bracket's preceding
# whitespace. Group 2: the flow-style list itself, e.g. "[]" or
# "[friday, monday]" — matched non-greedily up to the FIRST "]", so a "]"
# inside the trailing comment (the line has an example list in a comment)
# is never mistaken for the value's closing bracket. Group 3: everything
# after, comment included, untouched.
_REST_DAYS_RE = re.compile(r"^(\s*rest_days:\s*)(\[[^\]]*\])(.*)$", re.MULTILINE)


class RaceConfigError(Exception):
    pass


def read_rest_days() -> list[str]:
    cfg = yaml.safe_load(config.RACE_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    return list((cfg.get("preferences") or {}).get("rest_days") or [])


def write_rest_days(days: list[str]) -> None:
    days = [str(d).strip().lower() for d in days]
    invalid = sorted(set(days) - set(VALID_DAYS))
    if invalid:
        raise RaceConfigError(f"not valid weekday names: {invalid}")

    path = config.RACE_CONFIG_FILE
    text = path.read_text(encoding="utf-8")
    new_value = "[" + ", ".join(days) + "]"
    new_text, count = _REST_DAYS_RE.subn(lambda m: m.group(1) + new_value + m.group(3), text, count=1)
    if count != 1:
        raise RaceConfigError(
            "could not find a 'rest_days:' line to update in race_config.yaml — "
            "the file may have been hand-edited into a different format; edit it directly instead"
        )

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
