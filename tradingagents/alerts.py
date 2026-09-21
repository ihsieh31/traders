"""Operational alerts: Telegram and generic webhook, stdlib only.

Design rules learned from production alerting:
- **Deduplication**: a tripped circuit breaker blocks every subsequent
  order; without a cooldown that is one identical message per attempt.
  Alerts are keyed (default: the subject) and suppressed for
  ``cooldown_seconds`` after a successful send.
- **Isolation**: alert delivery must never affect trading. Every network
  failure is swallowed and reported in the return value only.
- **Secrets**: the Telegram token comes from config/env and is never
  echoed into logs or reports.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from tradingagents.app_identity import get_env

_DEDUPE_LOCK = threading.Lock()
_LAST_SENT: Dict[str, float] = {}


@dataclass
class AlertConfig:
    enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""
    cooldown_seconds: float = 900.0

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "AlertConfig":
        cfg = config or {}
        return cls(
            enabled=bool(cfg.get("alerts_enabled", True)),
            telegram_bot_token=str(
                cfg.get("alert_telegram_bot_token")
                or get_env("ALERT_TELEGRAM_BOT_TOKEN", "")
            ),
            telegram_chat_id=str(
                cfg.get("alert_telegram_chat_id")
                or get_env("ALERT_TELEGRAM_CHAT_ID", "")
            ),
            webhook_url=str(
                cfg.get("alert_webhook_url") or get_env("ALERT_WEBHOOK_URL", "")
            ),
            cooldown_seconds=float(cfg.get("alert_cooldown_seconds", 900.0) or 900.0),
        )


def reset_alert_dedupe() -> None:
    """Clear the cooldown map (tests and manual ops)."""
    with _DEDUPE_LOCK:
        _LAST_SENT.clear()


def _post_json(url: str, payload: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def send_alert(
    subject: str,
    body: str,
    key: Optional[str] = None,
    config: Optional[AlertConfig] = None,
    transport: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    """Dispatch an alert to every configured channel.

    Returns {"sent": bool, "deduped": bool, "channels": [...]}; never raises.
    `transport(url, payload)` is injectable for tests.
    """
    config = config or AlertConfig.from_config(None)
    result = {"sent": False, "deduped": False, "channels": []}
    if not config.enabled:
        return result

    dedupe_key = key or subject
    now = time.monotonic()
    with _DEDUPE_LOCK:
        last = _LAST_SENT.get(dedupe_key)
        if last is not None and now - last < config.cooldown_seconds:
            result["deduped"] = True
            return result

    post = transport or _post_json
    channels: List[str] = []

    if config.telegram_bot_token and config.telegram_chat_id:
        try:
            post(
                f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
                {
                    "chat_id": config.telegram_chat_id,
                    "text": f"{subject}\n\n{body}",
                },
            )
            channels.append("telegram")
        except Exception as exc:
            print(
                f"[ALERTS] Telegram delivery failed "
                f"({type(exc).__name__}); credential-bearing URL suppressed"
            )

    if config.webhook_url:
        try:
            post(
                config.webhook_url,
                {"subject": subject, "body": body, "sent_at": time.time()},
            )
            channels.append("webhook")
        except Exception as exc:
            print(
                f"[ALERTS] Webhook delivery failed "
                f"({type(exc).__name__}); URL suppressed"
            )

    if channels:
        with _DEDUPE_LOCK:
            _LAST_SENT[dedupe_key] = now
        result["sent"] = True
        result["channels"] = channels
    return result


def notify_safety_block(
    symbol: str,
    reasons: List[str],
    config: Optional[AlertConfig] = None,
    transport: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    """Alert that the safety layer blocked order flow.

    Keyed by the reasons (not the symbol), so one tripped breaker produces
    one alert per cooldown window no matter how many orders it blocks.
    """
    reasons = [str(r) for r in (reasons or [])]
    return send_alert(
        subject="🛑 Safety layer blocked order flow",
        body=f"Symbol: {symbol}\n" + "\n".join(f"- {r}" for r in reasons),
        key="safety_block|" + "|".join(sorted(reasons)),
        config=config,
        transport=transport,
    )


def notify_kill_switch(reason: str, config: Optional[AlertConfig] = None) -> dict:
    """Alert that the kill switch was engaged."""
    return send_alert(
        subject="🔴 Kill switch engaged",
        body=reason or "manual halt",
        key="kill_switch_engaged",
        config=config,
    )
