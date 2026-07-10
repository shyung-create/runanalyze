"""Training-plan generator backed by real published program tables.

Plans come from pipeline/plans_catalog.json — converted (see convert_plans.py)
from https://github.com/hoovercj/time-to-run (MIT), which encodes programs by
Hal Higdon, Pete Pfitzinger et al., and the Hansons Marathon Method. The
generator selects the right program from recent Garmin data, anchors it so
the program's final day lands on race day, compresses by dropping early weeks
when time is short, and attaches pace targets derived from current fitness.

A valid plan is always produced even if the LLM refinement step fails.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from common import fmt_hms, fmt_pace, log, parse_time_hms

DOW = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

HALF_DIST_MI = 13.109
FULL_DIST_MI = 26.219
KM_PER_MILE = 1.609344

CATALOG_PATH = Path(__file__).resolve().parent / "plans_catalog.json"

# workout type -> which training pace applies
TYPE_PACE = {"easy": "easy", "recovery": "easy", "long": "long", "tempo": "tempo",
             "intervals": "intervals", "race_pace": "race_pace", "race": "goal_pace"}


def load_catalog() -> dict:
    with open(CATALOG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------- selection

def localize_program_name(name: str, units: str) -> str:
    """Published program titles (Pfitzinger) embed literal 'NN miles per
    week' / 'NN to MM miles per week' figures — the catalog is miles-based
    (its source publishes in miles), so convert those embedded numbers to km
    for display when the dashboard is in km. Titles with no mileage figure
    (Higdon, Hansons) pass through unchanged."""
    if units != "km":
        return name

    def convert(m):
        nums = [round(float(g) * KM_PER_MILE) for g in m.groups() if g]
        return (f"{nums[0]} to {nums[1]} km per week" if len(nums) == 2
                else f"{nums[0]} km per week")

    return re.sub(r"(\d+(?:\.\d+)?)(?:\s*to\s*(\d+(?:\.\d+)?))?\s*miles per week",
                  convert, name)


def select_plan(catalog: dict, distance_type: str, avg_weekly_mi: float,
                recent_long_mi: float, weeks_available: int,
                plan_id: str | None = None, units: str = "miles") -> tuple[dict, str]:
    """Pick the program whose demands are the closest match to the athlete's
    current training, unbiased toward any author — Higdon, Hansons, and
    Pfitzinger tiers all compete on equal footing.

    Minimize |joining-week volume - current volume| rather than maximizing
    joining-week volume: otherwise selection mechanically gravitates to
    whichever author's catalog has the highest ceiling (Pfitzinger's tiers
    run up to 85 mi/wk, well past Higdon's or Hansons') any time volume
    clears its entry bar, regardless of fit.

    Because a program is anchored to race day, less time than its full length
    means joining mid-program — so the entry test is against the *joining
    week* (volume within ~25% above current weekly volume, longest run no
    more than ~30% beyond the recent longest run), not week 1.
    Explicit override via preferences.plan_id in race_config.yaml.

    avg_weekly_mi/recent_long_mi are always in miles (matching the catalog,
    which is miles-based) — units only controls how the reason text is
    formatted for display.
    """
    plans = [p for p in catalog["plans"] if p["distance_type"] == distance_type]
    if plan_id:
        forced = next((p for p in plans if p["id"] == plan_id), None)
        if forced:
            return forced, f"Plan '{plan_id}' forced via preferences.plan_id"
        log.warning("preferences.plan_id %r not found for %s — selecting "
                    "automatically. Valid ids: %s", plan_id, distance_type,
                    ", ".join(p["id"] for p in plans))

    def join_week(p):  # 0-based index of the week you'd join at
        return max(p["weeks"] - weeks_available, 0)

    def join_vol(p):
        return p["weekly_volume"][join_week(p)]

    def join_long(p):
        wk = join_week(p) + 1
        return max((d["distance"] or 0 for d in p["days"] if d["week"] == wk), default=0)

    def entry_ok(p):
        # upper bound: don't ask for an unsafe jump; lower bound: don't call a
        # program "eligible" when its joining-week demand is well under half
        # of current volume — without this a high-volume runner joining a
        # compressed timeline can match against a plan's early low-volume
        # week, since that week technically never exceeds the upper bound
        return (avg_weekly_mi * 0.5 <= join_vol(p) <= max(avg_weekly_mi * 1.25, avg_weekly_mi + 3)
                and join_long(p) <= max(recent_long_mi * 1.3, recent_long_mi + 1.5))

    def closest_fit(pool):
        return min(pool, key=lambda p: (abs(join_vol(p) - avg_weekly_mi),
                                        p["peak_volume"]))

    disp = (lambda mi: mi * KM_PER_MILE) if units == "km" else (lambda mi: mi)
    vol_unit = "km/wk" if units == "km" else "mi/wk"
    dist_unit = "km" if units == "km" else "mi"

    eligible = [p for p in plans if entry_ok(p)]
    if eligible:
        chosen = closest_fit(eligible)
        wk = join_week(chosen) + 1
        reason = (
            f"Recent volume ~{disp(avg_weekly_mi):.0f} {vol_unit} with a "
            f"{disp(recent_long_mi):.1f} {dist_unit} longest run is the closest match "
            f"to this program's demands where you'd join it (week {wk}: "
            f"{disp(join_vol(chosen)):.0f} {dist_unit}, longest run "
            f"{disp(join_long(chosen)):.0f} {dist_unit}) among {len(eligible)} "
            f"eligible programs")
    else:
        chosen = min(plans, key=join_vol)
        reason = (
            f"Recent volume ~{disp(avg_weekly_mi):.0f} {vol_unit} is below every "
            f"program's entry demands for the time remaining — using the gentlest "
            f"available ({disp(join_vol(chosen)):.0f} {dist_unit} at the joining "
            f"week); build carefully and let refreshes adjust")
    return chosen, reason


# ------------------------------------------------------------------- paces

def training_paces(fitness: dict, goal_time_s: int | None, distance_type: str,
                   units: str) -> dict:
    """Derive workout paces (sec/unit) from current fitness, with goal pace."""
    race_dist = (FULL_DIST_MI if distance_type == "full" else HALF_DIST_MI)
    if units == "km":
        race_dist *= KM_PER_MILE
    goal_pace = goal_time_s / race_dist if goal_time_s else None

    threshold = fitness.get("threshold_pace_s")
    easy = fitness.get("easy_pace_s")
    if not threshold:
        # No fitness data: anchor everything to the goal pace conservatively
        base = goal_pace or (600 if units == "miles" else 373)
        threshold = base * 0.95
        easy = base * 1.15

    per_mile = 1.0 if units == "miles" else 1 / KM_PER_MILE
    return {
        "easy": int(easy),
        "long": int(easy + 15 * per_mile),
        "tempo": int(threshold),
        "intervals": int(threshold - 25 * per_mile),
        "race_pace": int(goal_pace) if goal_pace else int(threshold + 20 * per_mile),
        "goal_pace": int(goal_pace) if goal_pace else None,
    }


# --------------------------------------------------------------- assessment

def assess_goal(fitness: dict, goal_time_s: int | None, distance_type: str,
                avg_weekly: float, joining_week_volume: float,
                units: str) -> dict:
    """Honest feasibility check — never silently prescribe an unsafe ramp."""
    notes, verdict = [], "realistic"
    proj = fitness.get("projected_full_s" if distance_type == "full"
                       else "projected_half_s")
    if goal_time_s and proj:
        gap_pct = (proj - goal_time_s) / goal_time_s * 100
        if gap_pct <= 0:
            notes.append(f"Current fitness already projects {fmt_hms(proj)} — "
                         f"faster than the {fmt_hms(goal_time_s)} goal. Goal is realistic.")
        elif gap_pct <= 5:
            notes.append(f"Projected {fmt_hms(proj)} vs goal {fmt_hms(goal_time_s)} "
                         f"({gap_pct:.0f}% gap) — achievable with consistent training.")
        elif gap_pct <= 12:
            verdict = "stretch"
            notes.append(f"Projected {fmt_hms(proj)} vs goal {fmt_hms(goal_time_s)} "
                         f"({gap_pct:.0f}% gap) — a stretch goal; possible only if the "
                         f"remaining weeks go very well.")
        else:
            verdict = "unrealistic"
            notes.append(f"Projected {fmt_hms(proj)} is {gap_pct:.0f}% slower than the "
                         f"{fmt_hms(goal_time_s)} goal. This goal looks unrealistic for "
                         f"race day; consider revising the target or treating it as a "
                         f"long-term goal.")
    elif goal_time_s:
        notes.append("No recent quality efforts to project fitness from — goal "
                     "feasibility unknown. The plan uses goal pace conservatively.")

    # 10% guideline: is the week you're joining at a big jump from current volume?
    if avg_weekly > 0 and joining_week_volume > avg_weekly * 1.25:
        if verdict == "realistic":
            verdict = "stretch"
        jump = (joining_week_volume / avg_weekly - 1) * 100
        notes.append(
            f"The program week you are joining calls for ~{joining_week_volume:.0f} "
            f"{units}/wk vs your current ~{avg_weekly:.0f} {units}/wk (+{jump:.0f}%). "
            f"That exceeds the 10% weekly increase guideline — treat the first weeks "
            f"as targets to build toward, and let the next refresh re-plan around "
            f"what you actually run.")
    return {"verdict": verdict, "notes": notes}


# ---------------------------------------------------- schedule constraints

# fields that move when two days trade workouts (date/dow/week stay put)
_PAYLOAD_KEYS = ("type", "distance", "pace", "pace_s", "description", "phase")


def _swap_workouts(a: dict, b: dict) -> None:
    for k in _PAYLOAD_KEYS:
        a[k], b[k] = b.get(k), a.get(k)


def _constraint_sets(prefs: dict) -> tuple[set, set, str]:
    rest_days = {str(x).lower() for x in (prefs.get("rest_days") or [])}
    blocked = {str(x) for x in (prefs.get("blocked_dates") or [])}
    long_day = str(prefs.get("long_run_day") or "").lower()
    if long_day in rest_days:
        log.warning("long_run_day %r is also a fixed rest day — ignoring the "
                    "long-run placement preference", long_day)
        long_day = ""
    return rest_days, blocked, long_day


def apply_schedule_constraints(days: list[dict], prefs: dict,
                               race_date_iso: str) -> list[str]:
    """Reorder workouts within each calendar week (Mon-Sun) so fixed rest
    days, one-off blocked dates, and the preferred long-run day are honored.

    Swap-based: a workout landing on a fixed rest day trades places with a
    rest/cross day in the same week, so weekly volume and session content
    are preserved — only their weekday placement changes. Race day never
    moves. Returns human-readable notes for anything that couldn't be
    honored cleanly.
    """
    notes: list[str] = []
    rest_days, blocked, long_day = _constraint_sets(prefs)
    if not rest_days and not blocked and not long_day:
        return notes

    def must_rest(d):
        return d["dow"] in rest_days or d["date"] in blocked

    weeks: dict[tuple, list[dict]] = {}
    for d in days:
        iso = date.fromisoformat(d["date"]).isocalendar()
        weeks.setdefault((iso.year, iso.week), []).append(d)

    for group in weeks.values():
        movable = [d for d in group if d["date"] != race_date_iso]
        # 1. fixed rest days / blocked dates: swap the workout out
        for d in movable:
            if must_rest(d) and d["type"] not in ("rest", "cross"):
                partners = [p for p in movable if p is not d and not must_rest(p)
                            and p["type"] in ("rest", "cross")]
                if partners:
                    dd = date.fromisoformat(d["date"])
                    partner = min(partners, key=lambda p: abs(
                        (date.fromisoformat(p["date"]) - dd).days))
                    _swap_workouts(d, partner)
                else:
                    notes.append(
                        f"{d['date']}: planned {d['type']} dropped — the day is a "
                        f"fixed rest day and no rest day was left in that week to "
                        f"swap with.")
                    d.update(type="rest", distance=None, pace="", pace_s=None,
                             description="Rest (fixed rest day)")
        # 2. long run onto the preferred day
        if long_day:
            longs = [d for d in movable if d["type"] == "long"]
            target = next((p for p in movable if p["dow"] == long_day), None)
            if longs and target is not None and not must_rest(target) \
                    and target["type"] != "race":
                lr = max(longs, key=lambda d: d.get("distance") or 0)
                if target is not lr:
                    _swap_workouts(lr, target)
    return notes


def enforce_fixed_rest(days: list[dict], prefs: dict, today_iso: str,
                       race_date_iso: str) -> list[str]:
    """Hard backstop used after LLM plan revisions (which are asked, but not
    guaranteed, to respect constraints): any future non-race day on a fixed
    rest day or blocked date becomes rest outright. No swapping here — a
    revision's placement intent is unknowable, so this only enforces,
    it doesn't reshuffle."""
    rest_days, blocked, _ = _constraint_sets(prefs)
    changed = []
    for d in days:
        if (d["date"] >= today_iso and d["date"] != race_date_iso
                and (d["dow"] in rest_days or d["date"] in blocked)
                and d["type"] not in ("rest", "cross")):
            changed.append(f"{d['date']} ({d['type']} → rest)")
            d.update(type="rest", distance=None, pace="", pace_s=None,
                     description="Rest (fixed rest day)")
    if changed:
        log.info("Fixed rest days enforced on: %s", ", ".join(changed))
    return changed


# ---------------------------------------------------------------- generate

def generate_plan(race_cfg: dict, fitness: dict, rolling_weekly: list[dict],
                  long_runs: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    catalog = load_catalog()
    race = race_cfg.get("race", {})
    prefs = race_cfg.get("preferences", {})
    units = prefs.get("units", "miles")
    to_units = KM_PER_MILE if units == "km" else 1.0   # catalog is miles
    to_miles = 1.0 / to_units
    distance_type = (race.get("distance_type") or "half").lower()
    if distance_type not in ("half", "full"):
        distance_type = "half"

    race_date = None
    if race.get("race_date"):
        try:
            race_date = datetime.strptime(str(race["race_date"]), "%Y-%m-%d").date()
        except ValueError:
            log.warning("Invalid race_date %r", race["race_date"])
    if not race_date or race_date <= today:
        log.warning("No valid future race_date — generating a placeholder 12-week plan")
        race_date = today + timedelta(weeks=12)

    goal_time_s = parse_time_hms(race.get("target_time") or "")

    # rolling_weekly is trailing 7-day windows ending on `today` (the refresh
    # date), not calendar Mon-Sun weeks — every window is fully elapsed by
    # construction, so nothing needs to be excluded as "in progress" here.
    recent = [w["distance"] for w in rolling_weekly if w["distance"] > 0] or [0]
    avg_weekly = sum(recent) / len(recent)
    recent_long = max((l["distance"] for l in long_runs[-6:]), default=0)

    days_until = (race_date - today).days
    weeks_available = max(-(-days_until // 7), 1)  # ceil

    plan_def, reason = select_plan(
        catalog, distance_type, avg_weekly * to_miles, recent_long * to_miles,
        weeks_available, prefs.get("plan_id"), units)
    plan_name = localize_program_name(plan_def["name"], units)
    paces = training_paces(fitness, goal_time_s, distance_type, units)

    plan_days = plan_def["days"]
    n = len(plan_days)
    plan_start = race_date - timedelta(days=n - 1)  # final plan day = race day
    compromises: list[str] = []
    compressed = plan_start < today
    if compressed:
        join_week = (today - plan_start).days // 7 + 1
        compromises.append(
            f"{weeks_available} weeks remain but {plan_name} is "
            f"{plan_def['weeks']} weeks — you join at week {join_week}; the "
            f"earlier base weeks are dropped while the peak weeks and taper "
            f"are preserved as published.")
    base_filler = plan_start > today
    if base_filler:
        compromises.append(
            f"More time available ({weeks_available} weeks) than the "
            f"{plan_def['weeks']}-week program — days before "
            f"{plan_start.isoformat()} repeat the program's week 1 as a base "
            f"phase.")

    days = []
    d = today
    while d <= race_date:
        offset = (d - plan_start).days
        if offset < 0:
            src = plan_days[offset % 7]        # week-1 pattern, weekday-aligned
            week_no = 0
            phase = "base"
        else:
            src = plan_days[offset]
            week_no = src["week"]
            phase = "plan"
        wtype = src["type"]
        dist = src["distance"]
        if dist is not None:
            dist = round(dist * to_units, 1)
        pace_key = TYPE_PACE.get(wtype)
        pace_s = paces.get(pace_key) if pace_key else None
        desc = src["description"]
        if phase == "base" and wtype == "race":   # never duplicate race day
            wtype, dist, pace_s, desc = "rest", None, None, "Rest"
        if wtype == "race":
            desc = (f"RACE DAY — {race.get('name') or plan_name}"
                    + (f", goal {fmt_hms(goal_time_s)}" if goal_time_s else ""))
        days.append({
            "date": d.isoformat(), "week": week_no, "dow": DOW[d.weekday()],
            "type": wtype, "distance": dist,
            "pace_s": int(pace_s) if pace_s else None,
            "pace": fmt_pace(pace_s) if pace_s else "",
            "description": desc, "phase": phase, "status": "planned",
        })
        d += timedelta(days=1)

    compromises += apply_schedule_constraints(days, prefs, race_date.isoformat())

    joining_offset = max((today - plan_start).days, 0)
    joining_week_vol = plan_def["weekly_volume"][min(joining_offset // 7,
                                                     plan_def["weeks"] - 1)] * to_units
    goal = assess_goal(fitness, goal_time_s, distance_type, avg_weekly,
                       joining_week_vol, units)
    goal["notes"] += compromises

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "catalog",
        "race": {
            "name": race.get("name") or "",
            "distance_type": distance_type,
            "race_date": race_date.isoformat(),
            "target_time": race.get("target_time") or "",
            "target_time_s": goal_time_s,
        },
        "units": units,
        "plan_id": plan_def["id"],
        "tier": plan_name,
        "tier_reason": reason,
        "plan_attribution": catalog["license"],
        "weeks": weeks_available,
        "compressed": compressed,
        "compromises": compromises,
        "paces": {k: {"seconds": v, "display": fmt_pace(v)} if v else None
                  for k, v in paces.items()},
        "goal_assessment": goal,
        "days": days,
    }
