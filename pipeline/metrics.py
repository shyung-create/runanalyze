"""Training metrics derived from extracted running activities.

All functions take the normalized activity dicts produced by garmin_extract
(distance in the athlete's preferred unit, avg_pace_s in seconds per unit).
Every metric degrades gracefully when fields are missing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from common import fmt_pace, log, parse_date

# Riegel exponent for projecting a performance across distances
RIEGEL_EXP = 1.06


def _act_date(a) -> date | None:
    d = parse_date(a.get("date") or a.get("start_time"))
    return d.date() if d else None


def compute_all(activities: list[dict], units: str, today: date | None = None) -> dict:
    today = today or date.today()
    runs = [a for a in activities if _act_date(a) and (a.get("distance") or 0) > 0]
    runs.sort(key=_act_date)

    return {
        "computed_at": today.isoformat(),
        "units": units,
        "total_runs_analyzed": len(runs),
        "weekly": weekly_mileage(runs, today),
        "rolling_weekly": rolling_weekly_volume(runs, today),
        "long_runs": long_run_progression(runs, today),
        "aerobic_efficiency": pace_at_hr_trend(runs, today),
        "load_ratio": acute_chronic_ratio(runs, today),
        "fitness": estimate_fitness(runs, today, units),
        "recent_runs": [_summary(a) for a in runs[-30:]],
    }


def _summary(a: dict) -> dict:
    """Publishable per-run summary — no GPS, no raw records."""
    dur = a.get("moving_time_s") or a.get("elapsed_time_s")
    return {
        "date": a.get("date"),
        "name": a.get("name"),
        "distance": round(float(a["distance"]), 2) if a.get("distance") else None,
        "duration_s": int(dur) if dur else None,
        "avg_pace_s": int(a["avg_pace_s"]) if a.get("avg_pace_s") else None,
        "avg_hr": a.get("avg_hr"),
        "max_hr": a.get("max_hr"),
        "cadence_spm": _cadence_spm(a),
        "ascent": a.get("ascent"),
        "training_effect": a.get("training_effect"),
        "anaerobic_training_effect": a.get("anaerobic_training_effect"),
        "laps": [
            {
                "lap": l.get("lap"),
                "distance": round(float(l["distance"]), 2) if l.get("distance") else None,
                "time_s": int(l["moving_time_s"] or l["elapsed_time_s"])
                if (l.get("moving_time_s") or l.get("elapsed_time_s")) else None,
                "avg_hr": l.get("avg_hr"),
            }
            for l in (a.get("laps") or [])
        ],
    }


def _cadence_spm(a: dict):
    """Garmin reports running cadence as rpm (single leg) in some schemas."""
    c = a.get("avg_steps_per_min") or a.get("avg_cadence")
    if c is None:
        return None
    c = float(c)
    return int(c * 2) if c < 130 else int(c)  # rpm -> spm heuristic


# ------------------------------------------------------------------ weekly

def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def weekly_mileage(runs, today, weeks: int = 12) -> list[dict]:
    buckets: dict[date, dict] = {}
    first = week_start(today) - timedelta(weeks=weeks - 1)
    for i in range(weeks):
        buckets[first + timedelta(weeks=i)] = {"distance": 0.0, "runs": 0, "time_s": 0}
    for a in runs:
        ws = week_start(_act_date(a))
        if ws in buckets:
            b = buckets[ws]
            b["distance"] += float(a["distance"])
            b["runs"] += 1
            b["time_s"] += int(a.get("moving_time_s") or a.get("elapsed_time_s") or 0)
    return [
        {"week_start": ws.isoformat(), "distance": round(b["distance"], 1),
         "runs": b["runs"], "time_s": b["time_s"]}
        for ws, b in sorted(buckets.items())
    ]


def rolling_weekly_volume(runs, today, weeks: int = 4) -> list[dict]:
    """Trailing 7-day windows ending the day *before* `today` (the refresh
    date) — NOT aligned to calendar Mon-Sun weeks. Window 0 (last in the
    returned list) covers today-7..today-1; window 1 covers the 7 days
    before that, etc. E.g. refreshing on Jul 9 gives windows Jul2-Jul8,
    Jun25-Jul1, ...

    Used for plan-review recalculation (tier selection, goal assessment) so
    'this week's volume' means exactly the 7 complete days leading into the
    refresh, recomputed fresh each time refresh.py runs, rather than
    snapping to whichever calendar week happens to contain today. Ending on
    the day before (not today itself) means every window is always fully
    elapsed even if today's run hasn't happened yet, so — unlike
    calendar-week buckets — there's no partial "week in progress" to
    exclude before averaging.
    """
    out = []
    for i in range(weeks):
        end = today - timedelta(days=1 + 7 * i)
        start = end - timedelta(days=6)
        window = [a for a in runs if start <= _act_date(a) <= end]
        dist = sum(float(a["distance"]) for a in window)
        out.append({"window_start": start.isoformat(), "window_end": end.isoformat(),
                    "distance": round(dist, 1), "runs": len(window)})
    return list(reversed(out))  # oldest first, matches weekly_mileage ordering


def long_run_progression(runs, today, weeks: int = 12) -> list[dict]:
    out = []
    first = week_start(today) - timedelta(weeks=weeks - 1)
    for i in range(weeks):
        ws = first + timedelta(weeks=i)
        week_runs = [a for a in runs if week_start(_act_date(a)) == ws]
        if week_runs:
            longest = max(week_runs, key=lambda a: float(a["distance"]))
            out.append({
                "week_start": ws.isoformat(),
                "distance": round(float(longest["distance"]), 1),
                "date": longest.get("date"),
                "avg_pace_s": int(longest["avg_pace_s"]) if longest.get("avg_pace_s") else None,
            })
        else:
            out.append({"week_start": ws.isoformat(), "distance": 0, "date": None,
                        "avg_pace_s": None})
    return out


# ---------------------------------------------------- aerobic efficiency

def pace_at_hr_trend(runs, today, weeks: int = 12) -> dict:
    """Pace trend at comparable HR: efficiency = speed / HR on steady runs.

    We use EF (yards-or-meters-per-beat style ratio): (1/pace_s) / HR * 1e5,
    restricted to runs 25–180 min with HR data, which approximates aerobic
    efficiency without needing zones. Rising EF at similar HR = fitter.
    """
    cutoff = today - timedelta(weeks=weeks)
    points = []
    for a in runs:
        d = _act_date(a)
        hr, pace = a.get("avg_hr"), a.get("avg_pace_s")
        dur = a.get("moving_time_s") or a.get("elapsed_time_s") or 0
        if d and d >= cutoff and hr and pace and 1500 <= dur <= 10800:
            ef = (1.0 / float(pace)) / float(hr) * 1e5
            points.append({"date": d.isoformat(), "avg_hr": int(hr),
                           "avg_pace_s": int(pace), "efficiency": round(ef, 2)})
    if len(points) < 3:
        return {"points": points, "trend": None,
                "note": "Not enough HR data for an efficiency trend"}
    half = len(points) // 2
    first = sum(p["efficiency"] for p in points[:half]) / half
    second = sum(p["efficiency"] for p in points[half:]) / (len(points) - half)
    pct = (second - first) / first * 100 if first else 0
    return {"points": points, "trend": round(pct, 1),
            "note": f"Aerobic efficiency {'improved' if pct >= 0 else 'declined'} "
                    f"{abs(round(pct, 1))}% over the period"}


# ------------------------------------------------------------- load ratio

def acute_chronic_ratio(runs, today) -> dict:
    """Acute:chronic workload ratio — 7-day load vs 28-day average weekly load.

    Load per run = distance × (avg_HR / 150) when HR is available (a simple
    intensity weighting), otherwise plain distance. ACWR > 1.5 flags
    injury risk; < 0.8 suggests detraining.
    """
    def load(a):
        dist = float(a["distance"])
        hr = a.get("avg_hr")
        return dist * (float(hr) / 150.0) if hr else dist

    daily: dict[date, float] = {}
    for a in runs:
        d = _act_date(a)
        daily[d] = daily.get(d, 0) + load(a)

    series = []
    for i in range(28):
        day = today - timedelta(days=27 - i)
        acute = sum(v for d, v in daily.items() if 0 <= (day - d).days < 7)
        chronic = sum(v for d, v in daily.items() if 0 <= (day - d).days < 28) / 4.0
        ratio = round(acute / chronic, 2) if chronic > 0 else None
        series.append({"date": day.isoformat(), "acwr": ratio})

    current = series[-1]["acwr"]
    if current is None:
        flag, msg = "no-data", "Not enough recent training data for a load ratio"
    elif current > 1.5:
        flag, msg = "high", f"ACWR {current} — training load is spiking; elevated injury risk"
    elif current > 1.3:
        flag, msg = "caution", f"ACWR {current} — load is ramping quickly; monitor recovery"
    elif current < 0.8:
        flag, msg = "low", f"ACWR {current} — load well below recent norm (taper or detraining)"
    else:
        flag, msg = "ok", f"ACWR {current} — training load in the safe range"
    return {"series": series, "current": current, "flag": flag, "note": msg}


# ---------------------------------------------------------------- fitness

def riegel_project(time_s: float, dist_from: float, dist_to: float) -> float:
    return time_s * (dist_to / dist_from) ** RIEGEL_EXP


def estimate_fitness(runs, today, units: str) -> dict:
    """Estimate current fitness from best recent sustained efforts.

    Scans the last 8 weeks for the fastest pace held over >= 3 miles (or 5 km)
    and projects race capability with Riegel. Falls back to recent easy pace
    + rules of thumb when nothing hard shows up.
    """
    cutoff = today - timedelta(weeks=8)
    min_dist = 3.0 if units == "miles" else 5.0
    half_dist = 13.109 if units == "miles" else 21.0975
    full_dist = 26.219 if units == "miles" else 42.195

    candidates = [
        a for a in runs
        if _act_date(a) and _act_date(a) >= cutoff
        and (a.get("distance") or 0) >= min_dist and a.get("avg_pace_s")
    ]
    if not candidates:
        return {"method": "none", "note": "No qualifying efforts in the last 8 weeks",
                "best_effort": None, "projected_half_s": None, "projected_full_s": None,
                "threshold_pace_s": None, "easy_pace_s": None}

    best = min(candidates, key=lambda a: a["avg_pace_s"])
    best_time = float(best["avg_pace_s"]) * float(best["distance"])
    proj_half = riegel_project(best_time, float(best["distance"]), half_dist)
    proj_full = riegel_project(best_time, float(best["distance"]), full_dist)

    # Threshold ~ pace sustainable for one hour, via Riegel from the best effort
    one_hr_dist = float(best["distance"]) * (3600.0 / best_time) ** (1 / RIEGEL_EXP)
    threshold_pace = 3600.0 / one_hr_dist
    easy_pace = threshold_pace + (75 if units == "miles" else 47)

    return {
        "method": "riegel-best-effort",
        "best_effort": {
            "date": best.get("date"),
            "distance": round(float(best["distance"]), 2),
            "avg_pace_s": int(best["avg_pace_s"]),
            "avg_hr": best.get("avg_hr"),
        },
        "projected_half_s": int(proj_half),
        "projected_full_s": int(proj_full),
        "threshold_pace_s": int(threshold_pace),
        "easy_pace_s": int(easy_pace),
        "note": (
            f"Best recent effort: {best['distance']:.1f} {units} @ "
            f"{fmt_pace(best['avg_pace_s'])} on {best.get('date')}. "
            f"Riegel projection — half: {_hms(proj_half)}, full: {_hms(proj_full)}."
        ),
    }


def _hms(s):
    from common import fmt_hms
    return fmt_hms(s)
