"""DeepSeek API client (OpenAI-compatible chat completions).

Only ever called from the local pipeline. The key comes from .env /
environment and never reaches the published site. All LLM responses are
required to be JSON and are schema-validated before use; callers fall back
to deterministic output when validation fails.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests

from common import log


class DeepSeekError(Exception):
    pass


class DeepSeekClient:
    def __init__(self):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        self.base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()
        self.vision_model = os.environ.get("DEEPSEEK_VISION_MODEL", "").strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, payload: dict, timeout: int = 180) -> dict:
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise DeepSeekError(f"DeepSeek API {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def chat_json(self, system: str, user, model: str | None = None,
                  max_tokens: int = 8000) -> dict:
        """One JSON-mode chat completion. `user` is a string or a content list
        (for vision). Raises DeepSeekError on failure or non-JSON output."""
        if not self.available:
            raise DeepSeekError("DEEPSEEK_API_KEY not set")
        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        data = self._post(payload)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise DeepSeekError(f"Unexpected API response shape: {e}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise DeepSeekError(f"LLM returned invalid JSON: {e}. Head: {text[:200]}")

    # ------------------------------------------------------------- vision

    def read_elevation_image(self, image_path: Path, race_distance: float,
                             units: str) -> list[dict] | None:
        """Ask a vision model to turn the course elevation screenshot into
        segments. Returns a validated segment list, or None if no vision
        model is configured / the call fails (caller falls back to
        config/course_segments.yaml)."""
        if not self.vision_model:
            log.info("No DEEPSEEK_VISION_MODEL configured — skipping image analysis. "
                     "Fill in config/course_segments.yaml manually instead.")
            return None
        if not image_path.exists():
            log.info("No course elevation image at %s", image_path)
            return None
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        suffix = image_path.suffix.lstrip(".").lower() or "png"
        system = _elevation_system_prompt(race_distance, units)
        user = [
            {"type": "text", "text": "Segment this course elevation profile."},
            {"type": "image_url",
             "image_url": {"url": f"data:image/{suffix};base64,{b64}"}},
        ]
        try:
            data = self.chat_json(system, user, model=self.vision_model)
        except DeepSeekError as e:
            log.warning("Vision analysis failed (%s). Falling back to "
                        "config/course_segments.yaml", e)
            return None
        segments = validate_segments(data.get("segments"), race_distance)
        if segments is None:
            log.warning("Vision model returned unusable segments — falling back to "
                        "config/course_segments.yaml")
        return segments

    # -------------------------------------------------------- plan revision

    def revise_plan(self, plan: dict, metrics: dict, race_cfg: dict,
                    comparison: dict, today_iso: str,
                    note: str | None = None) -> dict | None:
        """Ask the model to revise remaining plan days. Returns
        {'days': [...], 'revision_note': str} validated, or None on failure.

        `note` is free-text from the athlete (refresh.py --note "...") — the
        interactive channel for one-off adjustments like "move this week's
        long run to Saturday, I have a wedding Sunday"."""
        system = _revise_system_prompt(today_iso)
        user = json.dumps(_revise_context(plan, metrics, race_cfg, comparison,
                                          today_iso, note), default=str)
        try:
            data = self.chat_json(system, user)
        except DeepSeekError as e:
            log.warning("Plan revision failed: %s", e)
            return None
        return validate_revision(data, plan, today_iso)

    # ------------------------------------------------ de novo / blended plans

    def generate_plan_denovo(self, race_cfg: dict, metrics: dict, today) -> dict | None:
        """Author a full week-by-week plan from scratch (no published-program
        skeleton at all) — the "AI Plan" tab's de novo mode. Returns
        {'days': [...], 'generation_note': str} validated, or None on failure."""
        system = _DENOVO_SYSTEM_PROMPT
        user = json.dumps(_plan_generation_context(race_cfg, metrics, today), default=str)
        try:
            data = self.chat_json(system, user, max_tokens=16000)
        except DeepSeekError as e:
            log.warning("De novo plan generation failed: %s", e)
            return None
        return validate_full_plan(data, race_cfg, today.isoformat())

    def generate_plan_blended(self, race_cfg: dict, metrics: dict, today,
                              catalog_plan: dict) -> dict | None:
        """Sweeping revision of an already catalog-anchored plan (unlike
        revise_plan()'s small day-by-day diff) — the "AI Plan" tab's blended
        mode. `catalog_plan` is the deterministic plan_generator.generate_plan()
        output whose program/day skeleton this may restructure freely from
        today onward, while keeping its overall program identity. Returns
        {'days': [...], 'generation_note': str} validated, or None on failure."""
        system = _BLENDED_SYSTEM_PROMPT
        context = _plan_generation_context(race_cfg, metrics, today)
        context["catalog_plan"] = {
            "source_program": catalog_plan.get("plan_id") or catalog_plan.get("tier"),
            "days": [d for d in catalog_plan.get("days", []) if d["date"] >= today.isoformat()],
        }
        user = json.dumps(context, default=str)
        try:
            data = self.chat_json(system, user, max_tokens=16000)
        except DeepSeekError as e:
            log.warning("Blended plan generation failed: %s", e)
            return None
        return validate_full_plan(data, race_cfg, today.isoformat())


# -------------------------------------- prompts / context builders

def _elevation_system_prompt(race_distance: float, units: str) -> str:
    return (
        "You read race course elevation charts. Reply ONLY with JSON: "
        '{"segments": [{"name": str, "start": number, "end": number, '
        '"avg_grade_pct": number}]}. Split the course into 5-12 logical '
        "segments by elevation character. start/end are distances in "
        f"{units}; the course is {race_distance:.1f} {units} total, so the "
        "last segment must end there. avg_grade_pct is the average grade "
        "in percent (positive uphill)."
    )


def _revise_system_prompt(today_iso: str) -> str:
    return (
        "You are a running coach revising a training plan. Reply ONLY with "
        'JSON: {"revision_note": str, "changed_days": [{"date": "YYYY-MM-DD", '
        '"type": str, "distance": number|null, "pace": "M:SS"|null, '
        '"description": str}]}. Rules: only include days you actually change; '
        "never change days before " + today_iso + "; keep the race-day entry; "
        'allowed types: easy, tempo, intervals, race_pace, long, cross, rest, race. '
        "Respect the 10% weekly mileage growth guideline and keep the taper. "
        "HARD constraints from the athlete's config (never violate): weekdays in "
        "constraints.fixed_rest_days and dates in constraints.blocked_dates must "
        "be rest; prefer placing weekly long runs on "
        "constraints.preferred_long_run_day. If athlete_note is non-empty it is "
        "a direct instruction from the athlete — honor it (within safety) and "
        "acknowledge it in revision_note. "
        "revision_note must be 1-3 human-readable sentences explaining WHAT "
        "changed and WHY, citing the data (e.g. missed mileage, elevated HR, "
        "load ratio)."
    )


def _revise_context(plan: dict, metrics: dict, race_cfg: dict, comparison: dict,
                    today_iso: str, note: str | None) -> dict:
    remaining = [d for d in plan["days"] if d["date"] >= today_iso]
    prefs = race_cfg.get("preferences", {})
    return {
        "today": today_iso,
        "race": plan.get("race"),
        "units": plan.get("units"),
        "training_paces": plan.get("paces"),
        "constraints": {
            "fixed_rest_days": prefs.get("rest_days") or [],
            "blocked_dates": prefs.get("blocked_dates") or [],
            "preferred_long_run_day": prefs.get("long_run_day") or "",
        },
        "athlete_note": (note or "").strip(),
        "plan_vs_actual_last_14_days": comparison,
        "fitness_and_load": {
            "fitness": metrics.get("fitness"),
            "load_ratio": {k: v for k, v in (metrics.get("load_ratio") or {}).items()
                          if k != "series"},
            "aerobic_efficiency_note": (metrics.get("aerobic_efficiency") or {}).get("note"),
            "weekly_mileage": metrics.get("weekly"),
        },
        "remaining_plan_days": remaining,
    }


# ------------------------------------------------ plan-generation prompts

_DENOVO_SYSTEM_PROMPT = (
    "You are an expert running coach authoring a complete marathon/half-marathon "
    'training plan from scratch. Reply ONLY with JSON: {"generation_note": str, '
    '"days": [{"date": "YYYY-MM-DD", "type": str, "distance": number|null, '
    '"pace": "M:SS"|null, "description": str}]}. '
    'Allowed types: easy, tempo, intervals, race_pace, long, cross, rest, race, recovery. '
    "The days list MUST cover every single date from today through race day inclusive, "
    "with no gaps and no duplicate dates, sorted ascending. The last day MUST be race "
    "day with type \"race\". Respect the 10% weekly mileage growth guideline, include a "
    "taper in the final 1-3 weeks (declining weekly volume), and honor the HARD "
    "constraints given (fixed rest weekdays, blocked one-off dates, preferred long-run "
    "weekday) — never schedule a run on a constrained rest day. generation_note must be "
    "1-3 sentences explaining the overall structure and why it fits the athlete's "
    "current fitness and goal."
)

_BLENDED_SYSTEM_PROMPT = (
    "You are an expert running coach adapting an existing published training program "
    "to an athlete's actual fitness and constraints — more freely than a small revision, "
    'but still recognizably based on catalog_plan\'s program. Reply ONLY with JSON: '
    '{"generation_note": str, "days": [{"date": "YYYY-MM-DD", "type": str, '
    '"distance": number|null, "pace": "M:SS"|null, "description": str}]}. '
    'Allowed types: easy, tempo, intervals, race_pace, long, cross, rest, race, recovery. '
    "The days list MUST cover every date in catalog_plan.days (today through race day "
    "inclusive) with no gaps and no duplicates, sorted ascending — you may restructure "
    "workout types/distances/paces/order relative to catalog_plan, but the date range "
    "must match exactly and the last day MUST be race day with type \"race\". Respect "
    "the 10% weekly mileage growth guideline, keep a taper in the final 1-3 weeks, and "
    "honor the HARD constraints given (fixed rest weekdays, blocked one-off dates, "
    "preferred long-run weekday). generation_note must be 1-3 sentences explaining how "
    "and why this diverges from catalog_plan."
)


def _plan_generation_context(race_cfg: dict, metrics: dict, today) -> dict:
    prefs = race_cfg.get("preferences", {})
    race = race_cfg.get("race", {})
    return {
        "today": today.isoformat(),
        "race": race,
        "units": prefs.get("units", "miles"),
        "constraints": {
            "fixed_rest_days": prefs.get("rest_days") or [],
            "blocked_dates": prefs.get("blocked_dates") or [],
            "preferred_long_run_day": prefs.get("long_run_day") or "",
        },
        "fitness_and_load": {
            "fitness": metrics.get("fitness"),
            "load_ratio": {k: v for k, v in (metrics.get("load_ratio") or {}).items()
                          if k != "series"},
            "aerobic_efficiency_note": (metrics.get("aerobic_efficiency") or {}).get("note"),
            "weekly_mileage": metrics.get("weekly"),
        },
    }


# ------------------------------------------------------------- validators

ALLOWED_TYPES = {"easy", "tempo", "intervals", "race_pace", "long", "cross",
                 "rest", "race", "recovery"}


def validate_segments(segments, race_distance: float) -> list[dict] | None:
    if not isinstance(segments, list) or not segments:
        return None
    out = []
    for s in segments:
        try:
            seg = {"name": str(s.get("name") or f"Segment {len(out)+1}"),
                   "start": float(s["start"]), "end": float(s["end"]),
                   "avg_grade_pct": float(s.get("avg_grade_pct", 0))}
        except (KeyError, TypeError, ValueError):
            return None
        if seg["end"] <= seg["start"] or abs(seg["avg_grade_pct"]) > 30:
            return None
        out.append(seg)
    out.sort(key=lambda s: s["start"])
    if abs(out[-1]["end"] - race_distance) > race_distance * 0.15:
        return None
    return out


# A single day's distance must fit within this bound, or validation rejects
# the whole response — generous enough to cover a full marathon (42.2 km /
# 26.2 mi) as a legitimate race-day (or long-run) distance in either unit
# system, while still catching genuinely bad LLM output (typos, wrong units).
_MAX_DAILY_DISTANCE = {"km": 50.0, "miles": 31.0}


def _max_daily_distance(units: str | None) -> float:
    return _MAX_DAILY_DISTANCE.get((units or "miles").lower(), 31.0)


def validate_revision(data, plan: dict, today_iso: str) -> dict | None:
    """Validate LLM plan-revision JSON against the plan schema. Returns
    {'days', 'revision_note'} with the full merged day list, or None."""
    if not isinstance(data, dict):
        return None
    note = data.get("revision_note")
    changed = data.get("changed_days")
    if not isinstance(note, str) or not note.strip() or not isinstance(changed, list):
        return None

    by_date = {d["date"]: dict(d) for d in plan["days"]}
    race_date = plan["race"]["race_date"]
    max_dist = _max_daily_distance(plan.get("units"))
    from common import parse_duration_s
    for c in changed:
        if not isinstance(c, dict):
            return None
        date_s = str(c.get("date", ""))
        if date_s not in by_date:
            log.warning("Revision references unknown date %s — dropped", date_s)
            continue
        if date_s < today_iso:
            log.warning("Revision tried to change past day %s — dropped", date_s)
            continue
        if date_s == race_date and c.get("type") != "race":
            log.warning("Revision tried to remove race day — dropped")
            continue
        wtype = str(c.get("type", "")).lower()
        if wtype not in ALLOWED_TYPES:
            return None
        dist = c.get("distance")
        if dist is not None:
            try:
                dist = round(float(dist), 1)
            except (TypeError, ValueError):
                return None
            if not 0 <= dist <= max_dist:
                return None
        pace = c.get("pace")
        pace_s = parse_duration_s(pace) if pace else None
        day = by_date[date_s]
        day.update(type=wtype, distance=dist,
                   description=str(c.get("description") or day.get("description") or ""),
                   pace=str(pace) if pace else "",
                   pace_s=int(pace_s) if pace_s else None,
                   revised=True)
    days = sorted(by_date.values(), key=lambda d: d["date"])
    return {"days": days, "revision_note": note.strip()}


def validate_full_plan(data, race_cfg: dict, today_iso: str) -> dict | None:
    """Validate a full LLM-authored plan (de novo or blended) — unlike
    validate_revision(), there's no existing skeleton to anchor to, so this
    checks structural completeness (contiguous daily coverage from today
    through race day, race day present and correctly typed) in addition to
    the same per-day shape checks. Softer coaching-quality issues (10% rule,
    taper) are logged, not rejected — this codebase's philosophy throughout
    is to degrade gracefully rather than block on a heuristic judgment call.
    Returns {'days', 'generation_note'}, or None on structural failure."""
    if not isinstance(data, dict):
        log.warning("LLM plan response was not a JSON object (got %s)", type(data).__name__)
        return None
    note = data.get("generation_note")
    days_in = data.get("days")
    if not isinstance(note, str) or not note.strip():
        log.warning("LLM plan response missing/empty 'generation_note' (got %r)", note)
        return None
    if not isinstance(days_in, list) or not days_in:
        log.warning("LLM plan response missing/empty 'days' list (got %s, len=%s)",
                   type(days_in).__name__, len(days_in) if isinstance(days_in, list) else "n/a")
        return None

    race_date = str(race_cfg.get("race", {}).get("race_date") or "")
    max_dist = _max_daily_distance(race_cfg.get("preferences", {}).get("units"))
    from common import parse_duration_s

    days = []
    seen_dates = set()
    for i, d in enumerate(days_in):
        if not isinstance(d, dict):
            log.warning("LLM plan day[%d] is not an object (got %s)", i, type(d).__name__)
            return None
        date_s = str(d.get("date", ""))
        try:
            datetime.strptime(date_s, "%Y-%m-%d")
        except ValueError:
            log.warning("LLM plan day[%d] has an unparseable date %r", i, date_s)
            return None
        if date_s < today_iso:
            log.warning("LLM plan day[%d] date %s is before today (%s)", i, date_s, today_iso)
            return None
        if date_s in seen_dates:
            log.warning("LLM plan day[%d] duplicates date %s", i, date_s)
            return None
        wtype = str(d.get("type", "")).lower()
        if wtype not in ALLOWED_TYPES:
            log.warning("LLM plan day[%d] (%s) has disallowed type %r — allowed: %s",
                       i, date_s, d.get("type"), sorted(ALLOWED_TYPES))
            return None
        dist = d.get("distance")
        if dist is not None:
            try:
                dist = round(float(dist), 1)
            except (TypeError, ValueError):
                log.warning("LLM plan day[%d] (%s) has a non-numeric distance %r",
                           i, date_s, d.get("distance"))
                return None
            if not 0 <= dist <= max_dist:
                log.warning("LLM plan day[%d] (%s) distance %.1f is out of the 0-%.0f range",
                           i, date_s, dist, max_dist)
                return None
        pace = d.get("pace")
        pace_s = parse_duration_s(pace) if pace else None
        seen_dates.add(date_s)
        days.append({
            "date": date_s, "type": wtype, "distance": dist,
            "description": str(d.get("description") or ""),
            "pace": str(pace) if pace else "", "pace_s": int(pace_s) if pace_s else None,
        })

    days.sort(key=lambda d: d["date"])

    # Structural checks: contiguous daily coverage from today through race
    # day, no gaps, race day present and correctly typed.
    expected = today_iso
    for d in days:
        if d["date"] != expected:
            log.warning("LLM plan has a gap or non-contiguous date at %s (expected %s)",
                       d["date"], expected)
            return None
        expected = (datetime.strptime(expected, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    if days[-1]["date"] != race_date:
        log.warning("LLM plan's last day %s doesn't match race day %s", days[-1]["date"], race_date)
        return None
    if days[-1]["type"] != "race":
        log.warning("LLM plan's race day isn't typed 'race' — dropped")
        return None

    _log_plan_quality_warnings(days)
    return {"days": days, "generation_note": note.strip()}


def _log_plan_quality_warnings(days: list[dict]) -> None:
    """Soft coaching-quality checks (10% weekly growth, taper) — logged only,
    never rejects a structurally valid plan on a heuristic judgment call."""
    weekly: dict[int, float] = {}
    for i, d in enumerate(days):
        weekly[i // 7] = weekly.get(i // 7, 0) + (d["distance"] or 0)
    weeks = [weekly[k] for k in sorted(weekly)]
    for i in range(1, len(weeks)):
        if weeks[i - 1] > 0 and weeks[i] > weeks[i - 1] * 1.15:
            log.warning("LLM plan week %d (%.1f) grows >15%% over week %d (%.1f) — "
                       "exceeds the usual 10%% guideline", i + 1, weeks[i], i, weeks[i - 1])
    if len(weeks) >= 2 and weeks[-1] > max(weeks[:-1] or [0]) * 0.9:
        log.warning("LLM plan's final week (%.1f) doesn't show a clear taper "
                   "(peak week was %.1f)", weeks[-1], max(weeks[:-1] or [0]))
