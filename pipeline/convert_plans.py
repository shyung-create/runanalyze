#!/usr/bin/env python3
"""One-time converter: hoovercj/time-to-run plan tables -> plans_catalog.json.

Source: https://github.com/hoovercj/time-to-run (MIT License, (c) 2020 Cody
Hoover), which encodes published training programs by Hal Higdon, Pete
Pfitzinger (Advanced Marathoning / Faster Road Racing), and the Hansons
Marathon Method. Buy the books — the tables are companions to them.

Usage:
    python pipeline/convert_plans.py /path/to/time-to-run

Regenerates pipeline/plans_catalog.json (committed to the repo, so this
script only needs to run again if upstream plans change).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "plans_catalog.json"

# ---------------------------------------------------------------- parsing


STR = r'"(?:[^"\\]|\\.)*"'  # a TS double-quoted string, escapes allowed


def parse_ts_plan(path: Path) -> dict | None:
    """Extract the fields we need from the 'export default {…}' TS files
    with targeted regexes (full JSON conversion trips on colons in titles)."""
    text = path.read_text()
    if "as BuiltInPlan" not in text:
        return None

    def field(name):
        m = re.search(rf'\b{name}:\s*({STR})', text)
        return json.loads(m.group(1)) if m else None

    workouts = []
    for m in re.finditer(
            rf'\{{\s*(?:description:\s*({STR})\s*,\s*totalDistance:\s*([\d.]+)'
            rf'|totalDistance:\s*([\d.]+)\s*,\s*description:\s*({STR}))', text):
        desc = m.group(1) or m.group(4)
        dist = m.group(2) or m.group(3)
        workouts.append({"description": json.loads(desc),
                         "totalDistance": float(dist)})
    if not workouts:
        return None
    return {"title": field("title"), "raceType": field("raceType"),
            "units": field("units"), "workouts": workouts}


# ------------------------------------------------------------ classification

WORKOUT_TYPES = ("rest", "cross", "easy", "recovery", "long", "tempo",
                 "intervals", "race_pace", "race")


def classify(description: str, distance: float, week_max: float,
             is_race: bool) -> str:
    d = description.lower()
    if is_race:
        return "race"
    if distance == 0:
        return "cross" if "cross" in d else "rest"
    if "recovery" in d:
        return "recovery"
    if re.search(r"\d+\s*x\s*\d|tune-up race|speed workout|5k-10k pace|hill repeats", d):
        return "intervals"
    if re.search(r"@ ?hmp|@ ?mp\b|marathon (race )?pace|race pace|dress rehearsal|pace run", d):
        return "race_pace"
    if re.search(r"threshold|tempo|15k to half marathon pace", d):
        return "tempo"
    if re.search(r"long run|medium-long|endurance", d):
        return "long"
    # Higdon writes long runs as bare "N miles" (weekday runs are "N miles run")
    if (re.fullmatch(r"[\d.]+ miles?", d.strip()) and week_max > 0
            and distance >= 0.9 * week_max and distance >= 6):
        return "long"
    return "easy"


RACE_TYPE_MAP = {"Marathon": "full", "Half Marathon": "half"}


def convert(src_dir: Path) -> dict:
    plans = []
    for path in sorted((src_dir / "src/workouts/plans").glob("*.ts")):
        if path.name.startswith("template"):
            continue
        raw = parse_ts_plan(path)
        if not raw:
            print(f"  skip (unparsable): {path.name}")
            continue
        race_type = RACE_TYPE_MAP.get(raw.get("raceType"))
        if not race_type:
            continue  # Base / 5K programs aren't race plans for this dashboard
        workouts = raw["workouts"]
        assert len(workouts) % 7 == 0, f"{path.name}: {len(workouts)} not weeks"
        weeks = len(workouts) // 7

        days, weekly_vol = [], []
        for w in range(weeks):
            chunk = workouts[w * 7:(w + 1) * 7]
            week_max = max(x["totalDistance"] for x in chunk)
            weekly_vol.append(round(sum(x["totalDistance"] for x in chunk), 1))
            for i, x in enumerate(chunk):
                is_race = (w == weeks - 1 and i == 6)
                desc = x["description"].replace("\n", " · ")
                wtype = classify(x["description"], x["totalDistance"],
                                 week_max, is_race)
                days.append({"week": w + 1, "dow": i,  # 0 = Monday
                             "type": wtype,
                             "distance": x["totalDistance"] or None,
                             "description": desc})
        # entry requirement proxy: longest single run in week 1
        week1_long = max(x["totalDistance"] for x in workouts[:7])

        plans.append({
            "id": path.stem,
            "name": raw["title"],
            "distance_type": race_type,
            "units": raw.get("units", "miles"),
            "weeks": weeks,
            "week1_volume": weekly_vol[0],
            "week1_long": week1_long,
            "peak_volume": max(weekly_vol),
            "peak_long": max(d["distance"] or 0 for d in days if d["type"] == "long"),
            "weekly_volume": weekly_vol,
            "days": days,
        })
        print(f"  {path.stem}: {weeks}wk, week1 {weekly_vol[0]}mi "
              f"(long {week1_long}), peak {max(weekly_vol)}mi")

    return {
        "source": "https://github.com/hoovercj/time-to-run",
        "license": "MIT (c) 2020 Cody Hoover; plans encode published programs "
                   "by Hal Higdon, Pete Pfitzinger/Scott Douglas/Philip Latter, "
                   "and Keith & Kevin Hanson — support the original books",
        "converted": date.today().isoformat(),
        "plans": plans,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    catalog = convert(Path(sys.argv[1]))
    OUT.write_text(json.dumps(catalog, indent=1))
    print(f"\nWrote {OUT} ({len(catalog['plans'])} plans)")
