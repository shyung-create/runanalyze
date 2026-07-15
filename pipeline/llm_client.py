"""Factory selecting the configured LLM provider (DeepSeek or Claude).

refresh.py treats whatever this returns as an opaque object satisfying the
shared contract: .available, .revise_plan(), .read_elevation_image(),
.generate_plan_denovo(), .generate_plan_blended().
"""

from __future__ import annotations

from claude_client import ClaudeClient
from common import log
from deepseek_client import DeepSeekClient

VALID_PROVIDERS = ("deepseek", "claude")


def get_llm_client(provider: str):
    provider = (provider or "deepseek").strip().lower()
    if provider == "claude":
        return ClaudeClient()
    if provider != "deepseek":
        log.warning("Unknown llm_provider %r — falling back to deepseek", provider)
    return DeepSeekClient()
