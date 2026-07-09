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
        system = (
            "You read race course elevation charts. Reply ONLY with JSON: "
            '{"segments": [{"name": str, "start": number, "end": number, '
            '"avg_grade_pct": number}]}. Split the course into 5-12 logical '
            "segments by elevation character. start/end are distances in "
            f"{units}; the course is {race_distance:.1f} {units} total, so the "
            "last segment must end there. avg_grade_pct is the average grade "
            "in percent (positive uphill)."
        )
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
                    comparison: dict, today_iso: str) -> dict | None:
        """Ask the model to revise remaining plan days. Returns
        {'days': [...], 'revision_note': str} validated, or None on failure."""
        remaining = [d for d in plan["days"] if d["date"] >= today_iso]
        system = (
            "You are a running coach revising a training plan. Reply ONLY with "
            'JSON: {"revision_note": str, "changed_days": [{"date": "YYYY-MM-DD", '
            '"type": str, "distance": number|null, "pace": "M:SS"|null, '
            '"description": str}]}. Rules: only include days you actually change; '
            "never change days before " + today_iso + "; keep the race-day entry; "
            'allowed types: easy, tempo, intervals, race_pace, long, cross, rest, race. '
            "Respect the 10% weekly mileage growth guideline and keep the taper. "
            "revision_note must be 1-3 human-readable sentences explaining WHAT "
            "changed and WHY, citing the data (e.g. missed mileage, elevated HR, "
            "load ratio)."
        )
        user = json.dumps({
            "today": today_iso,
            "race": plan.get("race"),
            "units": plan.get("units"),
            "training_paces": plan.get("paces"),
            "plan_vs_actual_last_14_days": comparison,
            "fitness_and_load": {
                "fitness": metrics.get("fitness"),
                "load_ratio": {k: v for k, v in (metrics.get("load_ratio") or {}).items()
                               if k != "series"},
                "aerobic_efficiency_note": (metrics.get("aerobic_efficiency") or {}).get("note"),
                "weekly_mileage": metrics.get("weekly"),
            },
            "remaining_plan_days": remaining,
        }, default=str)
        try:
            data = self.chat_json(system, user)
        except DeepSeekError as e:
            log.warning("Plan revision failed: %s", e)
            return None
        return validate_revision(data, plan, today_iso)


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
            if not 0 <= dist <= 30:
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
