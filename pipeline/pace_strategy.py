"""Elevation-aware race pace strategy.

Takes course segments (from DeepSeek vision analysis of the elevation
screenshot, or the manual config/course_segments.yaml fallback), assigns an
even-effort pace per segment that nets out to the goal time, and overlays
hydration/fueling from race_info.yaml.

Grade adjustment model (per 1% average grade):
  uphill:   +12 s per mile  (+7.5 s per km)
  downhill:  -7 s per mile  (-4.4 s per km), capped — steep descents don't
             give back what climbs cost.
"""

from __future__ import annotations

from datetime import datetime

from common import fmt_hms, fmt_pace, log

UP_COST_PER_MI = 12.0
DOWN_GAIN_PER_MI = 7.0
KM_PER_MILE = 1.609344


def build_strategy(segments: list[dict] | None, race: dict, race_info: dict,
                   goal_time_s: int | None, race_distance: float, units: str,
                   source: str) -> dict:
    """Returns the race_strategy.json payload. Handles the not-configured case."""
    if not goal_time_s:
        return _empty("No target_time set in race_config.yaml")
    if not segments:
        return _empty(
            "No course segments available. Upload config/course_elevation.png "
            "(with a vision model configured) or fill in config/course_segments.yaml.")

    per_mi = 1.0 if units == "miles" else 1 / KM_PER_MILE
    up, down = UP_COST_PER_MI * per_mi, DOWN_GAIN_PER_MI * per_mi

    # Even-effort: pace_i = base + adj(grade_i); solve base so total = goal.
    def adj(grade):
        if grade >= 0:
            return min(grade * up, 90 * per_mi)
        return max(grade * down, -35 * per_mi)

    total_dist = sum(s["end"] - s["start"] for s in segments)
    weighted_adj = sum(adj(s["avg_grade_pct"]) * (s["end"] - s["start"])
                       for s in segments)
    base = (goal_time_s - weighted_adj) / total_dist

    hydration = _normalize_hydration(race_info.get("hydration_points") or [])
    out_segments, cum_time = [], 0.0
    for s in segments:
        dist = s["end"] - s["start"]
        pace = base + adj(s["avg_grade_pct"])
        seg_time = pace * dist
        seg_points = [h for h in hydration if s["start"] <= h["at"] < s["end"]]
        for h in seg_points:
            h["eta_s"] = int(cum_time + (h["at"] - s["start"]) * pace)
            h["eta"] = fmt_hms(h["eta_s"])
        out_segments.append({
            **s,
            "distance": round(dist, 2),
            "target_pace_s": int(pace),
            "target_pace": fmt_pace(pace),
            "segment_time_s": int(seg_time),
            "cumulative_time_s": int(cum_time + seg_time),
            "cumulative_time": fmt_hms(cum_time + seg_time),
            "effort_note": _effort_note(s["avg_grade_pct"]),
        })
        cum_time += seg_time

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "configured": True,
        "source": source,  # "vision" | "manual"
        "note": "",
        "units": units,
        "goal_time_s": goal_time_s,
        "goal_time": fmt_hms(goal_time_s),
        "base_pace_s": int(base),
        "base_pace": fmt_pace(base),
        "strategy": "even-effort: slower on climbs, controlled on descents, "
                    "netting out to the goal time",
        "segments": out_segments,
        "hydration": hydration,
        "fueling_plan": _fueling_plan(hydration, goal_time_s, race_distance, base, units),
        "elevation_profile": _profile_points(segments, units),
    }


def _empty(note: str) -> dict:
    log.info("Race strategy not generated: %s", note)
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "configured": False, "note": note, "segments": [], "hydration": [],
            "fueling_plan": [], "elevation_profile": []}


def _effort_note(grade: float) -> str:
    if grade >= 2.5:
        return "Big climb — shorten stride, effort steady, let pace drift"
    if grade >= 1.0:
        return "Uphill — hold effort, not pace"
    if grade <= -2.5:
        return "Steep descent — quick light steps, don't brake, don't sprint"
    if grade <= -1.0:
        return "Downhill — relax and let gravity work, stay controlled"
    return "Flat/rolling — settle into rhythm"


def _normalize_hydration(points: list) -> list[dict]:
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        at = p.get("mile_or_km", p.get("at"))
        try:
            at = float(at)
        except (TypeError, ValueError):
            continue
        out.append({"at": at, "type": str(p.get("type") or "water")})
    return sorted(out, key=lambda p: p["at"])


def _fueling_plan(hydration: list[dict], goal_time_s: int, race_distance: float,
                  base_pace: float, units: str) -> list[dict]:
    """Simple fueling guidance: gel every ~40 min from minute 40, water at
    every station, electrolytes on the back half — mapped to real stations
    where they exist."""
    plan = []
    gel_stops = set()
    gel_interval = 40 * 60
    t = gel_interval
    while t < goal_time_s - 15 * 60:
        at = round(t / base_pace, 1)
        station = next((h for h in hydration if abs(h["at"] - at) <= 1.0), None)
        if station:
            gel_stops.add(station["at"])
        plan.append({
            "at": station["at"] if station else at,
            "time": station.get("eta", fmt_hms(t)) if station else fmt_hms(t),
            "action": "Take a gel" + (" (station here — wash it down with water)"
                                      if station else " (carry it — no station nearby)"),
        })
        t += gel_interval
    for h in hydration:
        kind = h["type"].lower()
        if h["at"] in gel_stops:
            continue  # already covered by a gel entry at this station
        back_half = h["at"] > race_distance / 2
        if kind == "water":
            action = "Drink water — a few sips minimum"
        elif kind == "electrolyte":
            action = "Take electrolytes" + (" — important on the back half" if back_half else "")
        elif kind == "gel":
            action = "Gel station — take one if due, or bank it"
        else:
            action = f"Station: {kind}"
        plan.append({"at": h["at"], "time": h.get("eta", ""), "action": action})
    return sorted(plan, key=lambda p: p["at"])


def _profile_points(segments: list[dict], units: str) -> list[dict]:
    """Approximate elevation trace from segment grades for charting
    (relative elevation in ft or m, arbitrary zero)."""
    unit_len = 5280.0 if units == "miles" else 1000.0  # ft per mile / m per km
    pts, elev = [], 0.0
    for s in segments:
        pts.append({"distance": round(s["start"], 2), "elevation": round(elev, 1)})
        dist = s["end"] - s["start"]
        elev += s["avg_grade_pct"] / 100.0 * dist * unit_len
    pts.append({"distance": round(segments[-1]["end"], 2), "elevation": round(elev, 1)})
    return pts
