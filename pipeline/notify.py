"""Telegram failure notifier — adapted from the acuity project's
notifications.py::send_telegram (same shape, vendored here since acuity
lives in a separate repo and can't be imported directly).
"""

from __future__ import annotations

import os

import requests

from common import log

TELEGRAM_API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 10) -> bool:
    url = TELEGRAM_API_TEMPLATE.format(token=token)
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=timeout)
        if resp.status_code != 200:
            log.error("Telegram send failed: HTTP %s %s", resp.status_code, resp.text[:300])
            return False
        return True
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
        return False


def notify_failure(reason: str) -> None:
    """Best-effort failure alert. Never raises — a broken notifier must not
    mask the original failure it's trying to report."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping failure alert: %s", reason)
        return
    try:
        send_telegram(token, chat_id, f"⚠️ runanalyze: {reason}")
    except Exception:
        log.exception("notify_failure itself raised — swallowing so it never masks the real error")
