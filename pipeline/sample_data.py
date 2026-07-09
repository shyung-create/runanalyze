#!/usr/bin/env python3
"""Generate realistic sample dashboard data without GarminDB or an API key.

Exercises the real metrics / plan / strategy code paths against synthetic
activities, so the static dashboard can be developed and the GitHub Pages
site renders before the first real refresh. Run: python pipeline/sample_data.py
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import fmt_hms, write_json
import metrics as metrics_mod
import plan_generator
import pace_strategy
from deepseek_client import validate_segments

random.seed(42)
TODAY = date.today()


def synth_activities() -> list[dict]:
    """~16 weeks of a runner building from 20 to 30 mi/wk, easy pace ~9:40
    trending to ~9:20, long runs 8 -> 12 mi."""
    acts = []
    for week in range(16, 0, -1):
        monday = TODAY - timedelta(days=TODAY.weekday(), weeks=week - 1)
        progress = (16 - week) / 16
        easy_pace = 580 - 20 * progress + random.uniform(-10, 10)
        for dow, kind in ((1, "easy"), (3, "tempo"), (5, "easy"), (6, "long")):
            d = monday + timedelta(days=dow)
            if d > TODAY or random.random() < 0.08:  # occasional missed day
                continue
            if kind == "long":
                dist = 8 + 4.5 * progress + random.uniform(-0.5, 0.5)
                pace = easy_pace + 10
                hr = 148 + random.uniform(-4, 4)
            elif kind == "tempo":
                dist = 5 + 1.5 * progress
                pace = easy_pace - 65
                hr = 164 + random.uniform(-4, 4)
            else:
                dist = 4 + 2 * progress + random.uniform(-0.5, 0.5)
                pace = easy_pace
                hr = 143 + random.uniform(-5, 5)
            dur = dist * pace
            acts.append({
                "activity_id": f"sample-{d}-{kind}",
                "name": f"{kind.title()} run",
                "sport": "running",
                "start_time": f"{d} 06:30:00",
                "date": d.isoformat(),
                "distance": round(dist, 2),
                "moving_time_s": dur,
                "elapsed_time_s": dur * 1.02,
                "avg_pace_s": pace,
                "avg_hr": int(hr),
                "max_hr": int(hr + 15),
                "avg_steps_per_min": 172 + random.uniform(-3, 3),
                "ascent": int(dist * random.uniform(15, 45)),
                "training_effect": round(2.5 + progress, 1),
                "anaerobic_training_effect": 0.3 if kind != "tempo" else 1.2,
                "laps": [
                    {"lap": i + 1, "distance": 1.0,
                     "moving_time_s": pace + random.uniform(-8, 8),
                     "elapsed_time_s": pace + random.uniform(-8, 8),
                     "avg_hr": int(hr + random.uniform(-3, 3))}
                    for i in range(int(dist))
                ],
            })
    return acts


def main():
    units = "miles"
    race_date = TODAY + timedelta(weeks=10)
    race_cfg = {
        "race": {"name": "Sample City Half Marathon", "distance_type": "half",
                 "race_date": race_date.isoformat(), "target_time": "1:55:00"},
        "preferences": {"units": units, "long_run_day": "sunday",
                        "max_run_days_per_week": 5},
    }
    race_info = {
        "location": "Sample City — Riverside Park start",
        "expected_temperature": "48-58F",
        "humidity": None,
        "elevation_map_image": "",
        "hydration_points": [
            {"mile_or_km": 2.0, "type": "water"},
            {"mile_or_km": 4.5, "type": "water"},
            {"mile_or_km": 6.5, "type": "electrolyte"},
            {"mile_or_km": 8.5, "type": "gel"},
            {"mile_or_km": 10.5, "type": "water"},
            {"mile_or_km": 12.0, "type": "water"},
        ],
        "course_notes": "Rolling first half; the mile 7 climb is the crux. "
                        "Fast final 5K along the river.",
    }

    acts = synth_activities()
    mx = metrics_mod.compute_all(acts, units, TODAY)
    mx["missing_fields"] = []

    plan = plan_generator.generate_plan(race_cfg, mx["fitness"], mx["weekly"],
                                        mx["long_runs"], TODAY)

    # Mark a bit of plan history as done/missed for the calendar demo
    import refresh as refresh_mod
    comparison = refresh_mod.compare_plan_actual(plan, acts, TODAY.isoformat())
    plan["comparison"] = comparison["summary"]

    seg_raw = [
        {"name": "Start — downtown flats", "start": 0, "end": 3.0, "avg_grade_pct": 0.2},
        {"name": "Rolling hills", "start": 3.0, "end": 6.0, "avg_grade_pct": 1.0},
        {"name": "The mile-7 climb", "start": 6.0, "end": 7.5, "avg_grade_pct": 3.0},
        {"name": "Ridge descent", "start": 7.5, "end": 9.5, "avg_grade_pct": -2.2},
        {"name": "River flats", "start": 9.5, "end": 12.0, "avg_grade_pct": -0.3},
        {"name": "Finish kick", "start": 12.0, "end": 13.1, "avg_grade_pct": 0.4},
    ]
    segments = validate_segments(seg_raw, 13.109)
    strategy = pace_strategy.build_strategy(
        segments, plan["race"], race_info, plan["race"]["target_time_s"],
        13.109, units, "manual")

    rev_log = [
        {"date": (TODAY - timedelta(days=21)).isoformat() + "T07:02:11",
         "source": "generator",
         "note": f"Plan generated: {plan['tier']} tier — {plan['tier_reason']}."},
        {"date": (TODAY - timedelta(days=7)).isoformat() + "T06:48:33",
         "source": "deepseek",
         "note": "Plan updated: long run pushed back 2 days because last week's "
                 "mileage was 18% below plan and Tuesday's run showed elevated HR "
                 "at easy pace."},
        {"date": TODAY.isoformat() + "T07:15:02", "source": "deepseek",
         "note": "Plan updated: adherence back to 95% and aerobic efficiency "
                 "improving — tempo distance increased by 1 mile from next week."},
    ]

    write_json("plan.json", plan)
    write_json("metrics.json", mx)
    write_json("activities.json", {"units": units, "runs": mx["recent_runs"]})
    write_json("race_strategy.json", strategy)
    write_json("race.json", refresh_mod.build_race_json(race_cfg, race_info, plan, mx))
    write_json("revision_log.json", rev_log)
    write_json("meta.json", {"refreshed_at": datetime.now().isoformat(timespec="seconds"),
                             "sample": True})

    print(f"\nSample data written. Tier: {plan['tier']} | "
          f"weeks: {plan['weeks']} | goal: {plan['race']['target_time']} | "
          f"projected: {fmt_hms(mx['fitness'].get('projected_half_s'))}")
    for n in plan["goal_assessment"]["notes"]:
        print(f"  - {n}")


if __name__ == "__main__":
    main()
