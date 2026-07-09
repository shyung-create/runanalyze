"""Deterministic training-plan generator based on Hal Higdon's structural
principles (not copied tables): weekly long-run progression, step-back weeks
every 3rd week, tier-appropriate midweek quality, and a 2–3 week taper.

A valid plan is always produced even if the LLM refinement step fails.
Distances are in the athlete's preferred unit (miles by default).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from common import fmt_hms, fmt_pace, log, parse_time_hms

DOW = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

HALF_DIST_MI = 13.109
FULL_DIST_MI = 26.219
KM_PER_MILE = 1.609344

# Tier parameters. Distances in miles; scaled to km if needed.
TIERS = {
    "half": {
        "novice1":       {"weeks": 12, "peak_long": 10, "run_days": 4, "quality": [],                  "min_weekly": 0,  "min_long": 0},
        "novice2":       {"weeks": 12, "peak_long": 12, "run_days": 4, "quality": ["race_pace"],       "min_weekly": 12, "min_long": 5},
        "intermediate1": {"weeks": 12, "peak_long": 12, "run_days": 5, "quality": ["tempo"],           "min_weekly": 18, "min_long": 7},
        "intermediate2": {"weeks": 12, "peak_long": 14, "run_days": 5, "quality": ["tempo", "intervals"], "min_weekly": 24, "min_long": 9},
        "advanced":      {"weeks": 12, "peak_long": 15, "run_days": 6, "quality": ["tempo", "intervals"], "min_weekly": 32, "min_long": 11},
    },
    "full": {
        "novice1":       {"weeks": 18, "peak_long": 20, "run_days": 4, "quality": [],                  "min_weekly": 0,  "min_long": 0},
        "novice2":       {"weeks": 18, "peak_long": 20, "run_days": 4, "quality": ["race_pace"],       "min_weekly": 15, "min_long": 8},
        "intermediate1": {"weeks": 18, "peak_long": 20, "run_days": 5, "quality": ["race_pace"],       "min_weekly": 22, "min_long": 10},
        "intermediate2": {"weeks": 18, "peak_long": 20, "run_days": 5, "quality": ["tempo", "race_pace"], "min_weekly": 30, "min_long": 12},
        "advanced":      {"weeks": 18, "peak_long": 20, "run_days": 6, "quality": ["tempo", "intervals"], "min_weekly": 38, "min_long": 14},
    },
}

TAPER_WEEKS = {"half": 2, "full": 3}


def select_tier(distance_type: str, avg_weekly: float, recent_long: float,
                units: str) -> tuple[str, str]:
    """Pick the highest tier whose entry requirements the athlete meets."""
    scale = KM_PER_MILE if units == "km" else 1.0
    chosen = "novice1"
    for name, t in TIERS[distance_type].items():
        if avg_weekly >= t["min_weekly"] * scale and recent_long >= t["min_long"] * scale:
            chosen = name
    reason = (
        f"Recent average weekly volume {avg_weekly:.0f} {units} and longest recent "
        f"run {recent_long:.1f} {units} meet the entry requirements for "
        f"{chosen.replace('1', ' 1').replace('2', ' 2').title()}"
    )
    return chosen, reason


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


def assess_goal(fitness: dict, goal_time_s: int | None, distance_type: str,
                avg_weekly: float, peak_weekly_needed: float, weeks_available: int,
                units: str) -> dict:
    """Honest feasibility check — never silently generate an unsafe ramp."""
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

    # 10% rule: can we reach the needed peak volume without unsafe ramping?
    if avg_weekly > 0 and peak_weekly_needed > avg_weekly:
        safe_weeks = 0
        v = avg_weekly
        build_weeks = max(weeks_available - TAPER_WEEKS[distance_type], 1)
        while v < peak_weekly_needed and safe_weeks < 60:
            v *= 1.10
            safe_weeks += 1
        if safe_weeks > build_weeks:
            if verdict == "realistic":
                verdict = "stretch"
            notes.append(
                f"Reaching the plan's peak volume (~{peak_weekly_needed:.0f} {units}/wk) "
                f"from the current ~{avg_weekly:.0f} {units}/wk within the 10% weekly "
                f"increase guideline needs ~{safe_weeks} build weeks but only "
                f"{build_weeks} are available. The plan caps weekly growth at 10%, so "
                f"peak volume has been reduced accordingly.")
    return {"verdict": verdict, "notes": notes}


def generate_plan(race_cfg: dict, fitness: dict, weekly: list[dict],
                  long_runs: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    race = race_cfg.get("race", {})
    prefs = race_cfg.get("preferences", {})
    units = prefs.get("units", "miles")
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

    recent = [w["distance"] for w in weekly[-4:] if w["distance"] > 0] or [0]
    avg_weekly = sum(recent) / len(recent)
    recent_long = max((l["distance"] for l in long_runs[-6:]), default=0)

    tier, tier_reason = select_tier(distance_type, avg_weekly, recent_long, units)
    t = TIERS[distance_type][tier]
    scale = KM_PER_MILE if units == "km" else 1.0
    paces = training_paces(fitness, goal_time_s, distance_type, units)

    days_until = (race_date - today).days
    weeks_available = max(days_until // 7, 1)
    std_weeks = t["weeks"]
    taper = TAPER_WEEKS[distance_type]
    compressed = weeks_available < std_weeks
    compromises: list[str] = []

    # Long-run schedule: progress from current ability to peak, step-back
    # every 3rd week, then taper. Compression prioritizes long-run
    # progression + taper and drops early base weeks.
    peak_long = t["peak_long"] * scale
    start_long = max(min(recent_long if recent_long > 0 else 4 * scale, peak_long), 3 * scale)
    build_weeks = max(weeks_available - taper, 1)
    if compressed:
        compromises.append(
            f"Only {weeks_available} weeks until race day vs the standard "
            f"{std_weeks}-week {tier} program — early base weeks were dropped; "
            f"long-run progression and the {taper}-week taper are preserved.")

    long_sched: list[float] = []
    lr = start_long
    growth = (peak_long - start_long) / max(build_weeks - 1, 1)
    growth = min(growth, 2.0 * scale)  # never jump the long run > 2 mi/wk
    if growth * (build_weeks - 1) + start_long < peak_long - 0.5:
        achieved = start_long + growth * (build_weeks - 1)
        compromises.append(
            f"Peak long run reduced to {achieved:.0f} {units} (standard: "
            f"{peak_long:.0f}) to respect safe weekly progression in the time available.")
        peak_long = achieved
    for w in range(build_weeks):
        stepback = (w % 3 == 2) and w < build_weeks - 1
        this = min(lr, peak_long)
        long_sched.append(round(this * (0.75 if stepback else 1.0), 1))
        if not stepback:
            lr += growth

    # Taper long runs
    if distance_type == "full":
        taper_longs = [12 * scale, 8 * scale, 0][:taper]
    else:
        taper_longs = [8 * scale, 0][:taper]
    long_sched += [round(x, 1) for x in taper_longs[: weeks_available - build_weeks] or []]
    while len(long_sched) < weeks_available:
        long_sched.append(0)

    # Weekly volume target: long run is ~40-50% of weekly volume, capped by 10% growth
    prev_vol = max(avg_weekly, 6 * scale)
    days = []
    long_day_idx = DOW.index(prefs.get("long_run_day", "sunday"))
    quality = t["quality"]
    run_days = min(t["run_days"], int(prefs.get("max_run_days_per_week", 5)))

    week_templates = _week_template(run_days, long_day_idx, quality)

    cur = today
    week_num = 0
    while cur < race_date:
        week_start_d = cur - timedelta(days=cur.weekday())
        week_idx = ((week_start_d - (today - timedelta(days=today.weekday()))).days) // 7
        week_idx = min(week_idx, weeks_available - 1)
        long_dist = long_sched[week_idx]
        in_taper = week_idx >= build_weeks
        vol_target = min(prev_vol * 1.10, max(long_dist / 0.45, long_dist + 4 * scale))
        if in_taper:
            vol_target = prev_vol * (0.7 if week_idx == build_weeks else 0.5)
        other_total = max(vol_target - long_dist, 0)

        spec = week_templates[cur.weekday()]
        day = _make_day(cur, spec, long_dist, other_total, run_days, paces, units,
                        in_taper, week_idx + 1)
        # Race week: final days are short shakeouts
        if (race_date - cur).days <= 3 and day["type"] not in ("rest", "race"):
            day.update(type="easy", distance=round(2 * scale, 1),
                       description="Shakeout — very easy, stay loose")
        days.append(day)
        if cur.weekday() == 6:  # completed a calendar week
            week_dists = [d["distance"] or 0 for d in days if d["week"] == week_idx + 1]
            prev_vol = max(sum(week_dists), prev_vol * 0.9)
            week_num = week_idx + 1
        cur += timedelta(days=1)

    race_dist = (FULL_DIST_MI if distance_type == "full" else HALF_DIST_MI) * scale
    days.append({
        "date": race_date.isoformat(), "week": weeks_available, "dow": DOW[race_date.weekday()],
        "type": "race", "distance": round(race_dist, 1),
        "pace_s": paces.get("goal_pace") or paces["race_pace"],
        "pace": fmt_pace(paces.get("goal_pace") or paces["race_pace"]),
        "description": f"RACE DAY — {race.get('name') or distance_type + ' marathon'}"
                       + (f", goal {fmt_hms(goal_time_s)}" if goal_time_s else ""),
        "status": "planned",
    })

    peak_weekly_needed = max((long_sched[i] / 0.45 if long_sched[i] else 0)
                             for i in range(len(long_sched)))
    goal = assess_goal(fitness, goal_time_s, distance_type, avg_weekly,
                       peak_weekly_needed, weeks_available, units)
    if compressed:
        goal["notes"] += compromises

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "deterministic",
        "race": {
            "name": race.get("name") or "",
            "distance_type": distance_type,
            "race_date": race_date.isoformat(),
            "target_time": race.get("target_time") or "",
            "target_time_s": goal_time_s,
        },
        "units": units,
        "tier": tier,
        "tier_reason": tier_reason,
        "weeks": weeks_available,
        "compressed": compressed,
        "compromises": compromises,
        "paces": {k: {"seconds": v, "display": fmt_pace(v)} if v else None
                  for k, v in paces.items()},
        "goal_assessment": goal,
        "days": days,
    }


def _week_template(run_days: int, long_day: int, quality: list[str]) -> dict[int, str]:
    """Map weekday index -> workout slot, Higdon-style around the long-run day."""
    tmpl = {i: "rest" for i in range(7)}
    tmpl[long_day] = "long"
    before, after = (long_day - 1) % 7, (long_day + 1) % 7
    tmpl[after] = "rest" if run_days < 6 else "recovery"
    slots = [d for d in range(7) if tmpl[d] == "rest" and d != after]
    # order: mid-week first for quality, spread easy runs
    order = sorted(slots, key=lambda d: abs(d - 2))
    remaining = run_days - 1
    q = list(quality)
    for d in order:
        if remaining <= 0:
            break
        if q and abs((d - long_day) % 7) not in (1, 6):
            tmpl[d] = q.pop(0)
        else:
            tmpl[d] = "easy"
        remaining -= 1
    if run_days <= 4:
        tmpl[before] = "cross" if tmpl[before] == "rest" else tmpl[before]
    return tmpl


def _make_day(d: date, slot: str, long_dist: float, other_total: float,
              run_days: int, paces: dict, units: str, in_taper: bool,
              week: int) -> dict:
    per = max(other_total / max(run_days - 1, 1), 2.0)
    base = {"date": d.isoformat(), "week": week, "dow": DOW[d.weekday()],
            "status": "planned"}
    u = units
    if slot == "long":
        return {**base, "type": "long", "distance": long_dist or None,
                "pace_s": paces["long"], "pace": fmt_pace(paces["long"]),
                "description": f"Long run — conversational effort"
                if long_dist else "Rest (no long run scheduled)"} \
            if long_dist else {**base, "type": "rest", "distance": None, "pace_s": None,
                               "pace": "", "description": "Rest"}
    if slot == "rest":
        return {**base, "type": "rest", "distance": None, "pace_s": None, "pace": "",
                "description": "Rest — recovery is training"}
    if slot == "cross":
        return {**base, "type": "cross", "distance": None, "pace_s": None, "pace": "",
                "description": "Cross-train 30–60 min (bike, swim, strength) or rest"}
    if slot == "recovery":
        return {**base, "type": "easy", "distance": round(min(per * 0.7, 4), 1),
                "pace_s": paces["easy"], "pace": fmt_pace(paces["easy"]),
                "description": "Recovery jog — truly easy"}
    if slot == "tempo" and not in_taper:
        return {**base, "type": "tempo", "distance": round(per, 1),
                "pace_s": paces["tempo"], "pace": fmt_pace(paces["tempo"]),
                "description": f"Tempo — 10-15 min easy, then sustained comfortably-hard "
                               f"@ {fmt_pace(paces['tempo'])}/{u[:2]}, cool down"}
    if slot == "intervals" and not in_taper:
        return {**base, "type": "intervals", "distance": round(per, 1),
                "pace_s": paces["intervals"], "pace": fmt_pace(paces["intervals"]),
                "description": f"Intervals — e.g. 6×800m @ {fmt_pace(paces['intervals'])}"
                               f"/{u[:2]} with equal jog recovery"}
    if slot == "race_pace" and not in_taper:
        return {**base, "type": "race_pace", "distance": round(per, 1),
                "pace_s": paces["race_pace"], "pace": fmt_pace(paces["race_pace"]),
                "description": f"Goal-pace run @ {fmt_pace(paces['race_pace'])}/{u[:2]}"}
    return {**base, "type": "easy", "distance": round(per, 1),
            "pace_s": paces["easy"], "pace": fmt_pace(paces["easy"]),
            "description": "Easy run — conversational"}
