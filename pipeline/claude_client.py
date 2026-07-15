"""Claude (Anthropic) API client — alternative to DeepSeekClient, same public
contract (.available, .revise_plan(), .read_elevation_image(),
.generate_plan_denovo(), .generate_plan_blended()) so pipeline/llm_client.py's
factory can hand either one to refresh.py interchangeably.

Deliberately reuses deepseek_client.py's prompts/context-builders/validators
(the underscore-prefixed helpers and validate_*() functions) rather than
duplicating them — those are provider-agnostic; only how the request is
built/sent and the response parsed differs between the two APIs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from common import log
from deepseek_client import (_BLENDED_SYSTEM_PROMPT, _DENOVO_SYSTEM_PROMPT,
                             _elevation_system_prompt, _plan_generation_context,
                             _revise_context, _revise_system_prompt,
                             validate_full_plan, validate_revision, validate_segments)

try:
    import anthropic
except ImportError:
    anthropic = None

_MIME_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "gif": "image/gif", "webp": "image/webp"}


class ClaudeError(Exception):
    pass


class ClaudeClient:
    def __init__(self):
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5").strip()
        # Claude's main models are natively multimodal — no separate
        # vision-model env var needed, unlike DEEPSEEK_VISION_MODEL.
        self._client = anthropic.Anthropic(api_key=self.api_key) if (anthropic and self.api_key) else None

    @property
    def available(self) -> bool:
        if self.api_key and not anthropic:
            log.warning("ANTHROPIC_API_KEY is set but the 'anthropic' package "
                       "isn't installed — pip install anthropic")
        return bool(self._client)

    def chat_json(self, system: str, user, model: str | None = None,
                  max_tokens: int = 8000) -> dict:
        """One JSON-mode-style completion (Claude has no response_format
        flag — the shared prompts already instruct "reply ONLY with JSON",
        and this strips a markdown code fence defensively in case one slips
        through). `user` is a string or a content-block list (for vision).
        Raises ClaudeError on failure or non-JSON output."""
        if not self.available:
            raise ClaudeError("ANTHROPIC_API_KEY not set or 'anthropic' package missing")
        try:
            resp = self._client.messages.create(
                model=model or self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                temperature=0.2,
            )
        except anthropic.APIError as e:
            raise ClaudeError(f"Claude API error: {e}")
        text = "".join(block.text for block in resp.content if block.type == "text")
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit("```", 1)[0].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ClaudeError(f"LLM returned invalid JSON: {e}. Head: {text[:200]}")

    # ------------------------------------------------------------- vision

    def read_elevation_image(self, image_path: Path, race_distance: float,
                             units: str) -> list[dict] | None:
        if not image_path.exists():
            log.info("No course elevation image at %s", image_path)
            return None
        import base64
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        suffix = image_path.suffix.lstrip(".").lower() or "png"
        media_type = _MIME_TYPES.get(suffix, "image/png")
        system = _elevation_system_prompt(race_distance, units)
        user = [
            {"type": "text", "text": "Segment this course elevation profile."},
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        ]
        try:
            data = self.chat_json(system, user)
        except ClaudeError as e:
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
        system = _revise_system_prompt(today_iso)
        user = json.dumps(_revise_context(plan, metrics, race_cfg, comparison,
                                          today_iso, note), default=str)
        try:
            data = self.chat_json(system, user)
        except ClaudeError as e:
            log.warning("Plan revision failed: %s", e)
            return None
        return validate_revision(data, plan, today_iso)

    # ------------------------------------------------ de novo / blended plans

    def generate_plan_denovo(self, race_cfg: dict, metrics: dict, today) -> dict | None:
        user = json.dumps(_plan_generation_context(race_cfg, metrics, today), default=str)
        try:
            data = self.chat_json(_DENOVO_SYSTEM_PROMPT, user, max_tokens=16000)
        except ClaudeError as e:
            log.warning("De novo plan generation failed: %s", e)
            return None
        return validate_full_plan(data, race_cfg, today.isoformat())

    def generate_plan_blended(self, race_cfg: dict, metrics: dict, today,
                              catalog_plan: dict) -> dict | None:
        context = _plan_generation_context(race_cfg, metrics, today)
        context["catalog_plan"] = {
            "source_program": catalog_plan.get("plan_id") or catalog_plan.get("tier"),
            "days": [d for d in catalog_plan.get("days", []) if d["date"] >= today.isoformat()],
        }
        user = json.dumps(context, default=str)
        try:
            data = self.chat_json(_BLENDED_SYSTEM_PROMPT, user, max_tokens=16000)
        except ClaudeError as e:
            log.warning("Blended plan generation failed: %s", e)
            return None
        return validate_full_plan(data, race_cfg, today.isoformat())
