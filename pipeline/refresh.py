#!/usr/bin/env python3
"""One-command refresh: sync Garmin data, recompute metrics, revise the plan,
regenerate all dashboard JSON, and (optionally) push to GitHub Pages.

Usage:
    python pipeline/refresh.py            # full run, asks before pushing
    python pipeline/refresh.py --push     # push without asking
    python pipeline/refresh.py --no-sync  # skip the GarminDB download step
    python pipeline/refresh.py --no-llm   # deterministic only, no DeepSeek calls
    python pipeline/refresh.py --no-git   # don't commit/push
    python pipeline/refresh.py --replan   # regenerate the plan from scratch
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (CONFIG_DIR, REPO_ROOT, fmt_hms, load_race_config,
                    load_race_info, load_yaml, log, parse_time_hms, read_json,
                    setup_logging, write_json)
import garmin_extract
import metrics as metrics_mod
import plan_generator
import pace_strategy
from deepseek_client import DeepSeekClient, validate_segments

HALF_MI, FULL_MI = 13.109, 26.219
KM_PER_MILE = 1.609344


def load_dotenv():
    try:
        from dotenv import load_dotenv as _ld
        _ld(REPO_ROOT / ".env")
    except ImportError:
        pass


# ------------------------------------------------------------------ steps

def resolve_garmindb_cli() -> list[str]:
    """Resolve GARMINDB_CLI to a runnable argv prefix.

    garmindb_cli.py is typically installed as a plain .py file (no .exe
    wrapper on Windows), so it can't be exec'd directly via subprocess —
    Windows raises WinError 193 ("not a valid Win32 application"). Detect a
    .py target and always launch it through the current Python interpreter,
    which works cross-platform.
    """
    cli = os.environ.get("GARMINDB_CLI", "garmindb_cli.py").strip()
    resolved = shutil.which(cli) or cli
    if resolved.lower().endswith(".py"):
        return [sys.executable, resolved]
    return [resolved]


def sync_garmin():
    cmd = resolve_garmindb_cli() + ["--activities", "--download", "--import",
                                    "--analyze", "--latest"]
    log.info("Syncing GarminDB: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        log.error("garmindb_cli.py not found — set GARMINDB_CLI in .env to its "
                  "full path (e.g. C:\\...\\Scripts\\garmindb_cli.py) or install "
                  "GarminDB (pip install garmindb). Continuing with existing data.")
    except subprocess.CalledProcessError as e:
        log.error("GarminDB sync failed (%s). Continuing with existing data.", e)


def compare_plan_actual(plan: dict, activities: list[dict], today_iso: str) -> dict:
    """Mark past plan days done/missed/partial against actual runs, and build
    a 14-day summary used for LLM revision context.

    Today counts as "past" (compared against actual, cross-referenced with
    a done/missed/partial mark) only if a run is already logged for today —
    e.g. refreshing in the evening after today's run. Otherwise today stays
    an upcoming/prescribed day, same as tomorrow, since it hasn't happened
    yet. Either way the rolling weekly volume used for plan review is
    unaffected — that always sums the 7 complete days ending yesterday.
    """
    by_date: dict[str, list] = {}
    for a in activities:
        if a.get("date"):
            by_date.setdefault(a["date"], []).append(a)

    today_has_run = bool(by_date.get(today_iso))

    recent = []
    for day in plan.get("days", []):
        is_past = day["date"] < today_iso or (today_has_run and day["date"] == today_iso)
        if not is_past:
            continue
        actual = by_date.get(day["date"], [])
        dist = sum(float(x.get("distance") or 0) for x in actual)
        planned = day.get("distance") or 0
        if day["type"] in ("rest", "cross"):
            day["actual"] = {"distance": round(dist, 1)} if dist else None
            day["completion"] = "done"  # rest/cross can't be "missed" meaningfully
        elif not actual and planned:
            day["actual"] = None
            day["completion"] = "missed"
        elif planned and dist < planned * 0.7:
            day["actual"] = _actual_summary(actual)
            day["completion"] = "partial"
        else:
            day["actual"] = _actual_summary(actual)
            day["completion"] = "done"
        day["status"] = "past"
        if (date.fromisoformat(today_iso) - date.fromisoformat(day["date"])).days <= 14:
            recent.append({"date": day["date"], "planned_type": day["type"],
                           "planned_distance": planned,
                           "completion": day["completion"],
                           "actual": day.get("actual")})

    done = sum(1 for r in recent if r["completion"] == "done")
    missed = [r for r in recent if r["completion"] == "missed"
              and r["planned_type"] not in ("rest", "cross")]
    planned_total = sum(r["planned_distance"] or 0 for r in recent)
    actual_total = sum((r["actual"] or {}).get("distance", 0) for r in recent)
    return {
        "days": recent,
        "summary": {
            "days_done": done, "days_missed": len(missed),
            "planned_distance_14d": round(planned_total, 1),
            "actual_distance_14d": round(actual_total, 1),
            "adherence_pct": round(actual_total / planned_total * 100)
            if planned_total else None,
        },
    }


def _actual_summary(acts: list[dict]) -> dict:
    dist = sum(float(a.get("distance") or 0) for a in acts)
    hrs = [a["avg_hr"] for a in acts if a.get("avg_hr")]
    paces = [a["avg_pace_s"] for a in acts if a.get("avg_pace_s")]
    from common import fmt_pace
    return {"distance": round(dist, 1),
            "avg_hr": int(sum(hrs) / len(hrs)) if hrs else None,
            "avg_pace_s": int(sum(paces) / len(paces)) if paces else None,
            "avg_pace": fmt_pace(sum(paces) / len(paces)) if paces else None}


def append_revision(note: str, source: str) -> list:
    log_data = read_json("revision_log.json", default=[]) or []
    log_data.append({"date": datetime.now().isoformat(timespec="seconds"),
                     "source": source, "note": note})
    write_json("revision_log.json", log_data)
    return log_data


def build_race_json(race_cfg, race_info, plan, mx) -> dict:
    race = plan["race"]
    goal_s = race.get("target_time_s")
    dist_type = race["distance_type"]
    units = plan["units"]
    fit = mx.get("fitness") or {}
    proj = fit.get("projected_full_s" if dist_type == "full" else "projected_half_s")
    return {
        "race": race,
        "race_info": race_info,
        "units": units,
        "goal_time": fmt_hms(goal_s) if goal_s else "",
        "projected_time": fmt_hms(proj) if proj else "",
        "projected_time_s": proj,
        "goal_assessment": plan["goal_assessment"],
        "tier": plan["tier"],
        "tier_reason": plan["tier_reason"],
    }


def git_publish(push: bool, ask: bool):
    def run(*args, **kw):
        return subprocess.run(["git", *args], cwd=REPO_ROOT, **kw)

    run("add", "docs/data")
    diff = run("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        log.info("No data changes to commit.")
        return
    msg = f"Refresh dashboard data {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    run("commit", "-m", msg, check=True)
    log.info("Committed: %s", msg)
    if not push and ask:
        try:
            push = input("Push to GitHub (updates the live site)? [y/N] ").strip().lower() == "y"
        except EOFError:
            push = False
    if push:
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=REPO_ROOT, capture_output=True, text=True
                                ).stdout.strip()
        run("push", "-u", "origin", branch, check=True)
        log.info("Pushed to origin/%s — GitHub Pages will update shortly.", branch)
    else:
        log.info("Not pushed. Run `git push` when ready.")


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--push", action="store_true", help="push without asking")
    ap.add_argument("--no-sync", action="store_true", help="skip GarminDB download")
    ap.add_argument("--no-llm", action="store_true", help="skip all DeepSeek calls")
    ap.add_argument("--no-git", action="store_true", help="skip commit/push")
    ap.add_argument("--replan", action="store_true",
                    help="discard the existing plan and regenerate from scratch "
                         "(current fitness, program selection, paces)")
    ap.add_argument("--note", metavar="TEXT",
                    help="free-text instruction for the DeepSeek plan revision, "
                         "e.g. \"move this week's long run to Saturday\"")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    load_dotenv()
    today = date.today()
    today_iso = today.isoformat()

    race_cfg = load_race_config()
    race_info = load_race_info()
    units = race_cfg["preferences"]["units"]

    # 1. Sync
    if not args.no_sync:
        sync_garmin()

    # 2. Extract + metrics
    activities_since = race_cfg["preferences"].get("activities_since") or None
    result = garmin_extract.extract_runs(start_date=activities_since)
    activities = result["activities"]
    log.info("Extracted %d running activities%s", len(activities),
             f" (since {activities_since})" if activities_since else "")

    db_units = garmin_extract.detect_garmindb_units()
    if db_units != units:
        log.info("GarminDB stores distances in %s but the dashboard displays "
                 "%s — converting.", db_units, units)
        garmin_extract.convert_units(activities, db_units, units)

    mx = metrics_mod.compute_all(activities, units, today)
    mx["missing_fields"] = result["missing_fields"]

    # 3. Compare actuals against the existing plan (if any)
    old_plan = read_json("plan.json")
    llm = DeepSeekClient() if not args.no_llm else None

    race = race_cfg.get("race", {})
    config_changed = bool(old_plan) and (
        old_plan["race"].get("race_date") != str(race.get("race_date") or "")
        or old_plan["race"].get("target_time") != (race.get("target_time") or "")
        or old_plan["race"].get("distance_type") != (race.get("distance_type") or "half")
        or old_plan.get("units") != units
    )
    if args.replan and old_plan:
        config_changed = True

    if old_plan and not config_changed:
        comparison = compare_plan_actual(old_plan, activities, today_iso)
        plan = old_plan
        # 4. LLM revision of remaining days
        revised = None
        if llm and llm.available:
            log.info("Asking DeepSeek to revise the remaining plan%s...",
                     " (with your note)" if args.note else "")
            revised = llm.revise_plan(plan, mx, race_cfg, comparison, today_iso,
                                      note=args.note)
        elif args.note:
            log.warning("--note given but DeepSeek is unavailable (%s) — the note "
                        "cannot be applied; fixed rest days/blocked dates from "
                        "race_config.yaml are still enforced.",
                        "--no-llm" if args.no_llm else "DEEPSEEK_API_KEY not set")
        elif llm and not llm.available:
            log.warning("DEEPSEEK_API_KEY not set — keeping deterministic plan")
        if revised:
            plan["days"] = revised["days"]
            plan["source"] = "llm-revised"
            plan["generated_at"] = datetime.now().isoformat(timespec="seconds")
            append_revision(f"Plan updated on {today_iso}: {revised['revision_note']}",
                            "deepseek")
        elif llm and llm.available:
            append_revision(
                f"{today_iso}: DeepSeek revision failed validation — keeping the "
                f"existing plan unchanged. Metrics were refreshed.", "error")
    else:
        reason = ("--replan requested" if args.replan and old_plan
                  else "race configuration or units changed" if config_changed
                  else "no existing plan")
        log.info("Generating a fresh plan (%s)", reason)
        plan = plan_generator.generate_plan(race_cfg, mx["fitness"], mx["rolling_weekly"],
                                            mx["long_runs"], today)
        comparison = compare_plan_actual(plan, activities, today_iso)
        append_revision(
            f"Plan generated on {today_iso} ({reason}): {plan['tier']} tier — "
            f"{plan['tier_reason']}.", "generator")

    # Hard backstop: fixed rest days / blocked dates hold no matter where the
    # plan came from (fresh generation, kept plan, or LLM revision).
    plan_generator.enforce_fixed_rest(plan["days"], race_cfg["preferences"],
                                      today_iso, plan["race"]["race_date"])

    plan["comparison"] = comparison["summary"]

    # 5. Race strategy (elevation-aware)
    goal_s = plan["race"].get("target_time_s")
    race_dist = (FULL_MI if plan["race"]["distance_type"] == "full" else HALF_MI)
    if units == "km":
        race_dist *= KM_PER_MILE
    segments, source = None, "manual"
    if llm and llm.available:
        segments = llm.read_elevation_image(CONFIG_DIR / "course_elevation.png",
                                            race_dist, units)
        if segments:
            source = "vision"
    if not segments:
        manual = load_yaml(CONFIG_DIR / "course_segments.yaml").get("segments") or []
        segments = validate_segments(manual, race_dist) if manual else None
        if manual and not segments:
            log.warning("config/course_segments.yaml is invalid — check start/end/grade")
    strategy = pace_strategy.build_strategy(segments, plan["race"], race_info,
                                            goal_s, race_dist, units, source)

    # 6. Write everything
    write_json("plan.json", plan)
    write_json("metrics.json", mx)
    write_json("activities.json", {"units": units, "runs": mx["recent_runs"]})
    write_json("race_strategy.json", strategy)
    write_json("race.json", build_race_json(race_cfg, race_info, plan, mx))
    rev_log = read_json("revision_log.json", default=[])
    write_json("revision_log.json", rev_log)
    write_json("meta.json", {"refreshed_at": datetime.now().isoformat(timespec="seconds")})

    # 7. Publish
    if not args.no_git:
        git_publish(args.push, ask=not args.push)

    print("\nRefresh complete.")
    if plan.get("goal_assessment", {}).get("notes"):
        print("Goal assessment:")
        for n in plan["goal_assessment"]["notes"]:
            print(f"  - {n}")


if __name__ == "__main__":
    main()
