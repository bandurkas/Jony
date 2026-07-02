"""Telegram notifications tagged [Jony] (multi-bot discipline: every message
says which bot it came from). Failures never break the loop."""
from __future__ import annotations

import requests

from . import config


def notify(text: str) -> None:
    token = config.TELEGRAM_BOT_TOKEN
    chats = [c.strip() for c in config.TELEGRAM_CHAT_ID.split(",") if c.strip()]
    if not token or not chats:
        return
    for chat_id in chats:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"[{config.BOT_TAG}] {text}"},
                timeout=10)
        except Exception as e:
            print(f"[telegram] send failed: {e}", flush=True)
