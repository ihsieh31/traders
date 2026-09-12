"""Shared credential redaction for persisted logs and reports."""

from __future__ import annotations

import urllib.parse
from typing import Any


SAFE_TOKEN_COUNT_KEYS = {
    "cached_tokens",
    "completion_tokens",
    "daily_llm_token_budget",
    "input_tokens",
    "llm_tokens",
    "max_output_tokens",
    "output_tokens",
    "prompt_tokens",
    "reasoning_tokens",
    "report_context_budget_tokens",
    "total_llm_input_tokens",
    "total_llm_output_tokens",
    "total_llm_tokens",
    "total_tokens",
    "total_tokens_estimate",
    "unpriced_tokens",
}

_SECRET_SUBSTRINGS = ("api_key", "secret_key", "token", "secret", "password")
_SENSITIVE_EXACT = {
    "alert_telegram_chat_id",
    "alert_webhook_url",
    "telegram_chat_id",
    "webhook_url",
}
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_api_secret",
    "_bot_token",
    "_chat_id",
    "_client_secret",
    "_password",
    "_secret_key",
)


def sanitize_url(url: Any) -> str:
    """Strip query and userinfo, which may contain credentials."""
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(str(url))
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:
        return str(url).split("?", 1)[0].split("#", 1)[0]


def is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower()
    if normalized in SAFE_TOKEN_COUNT_KEYS:
        return False
    return (
        normalized in _SENSITIVE_EXACT
        or any(part in normalized for part in _SECRET_SUBSTRINGS)
        or normalized.endswith(_SENSITIVE_SUFFIXES)
    )


def sanitize_for_log(obj: Any, *, redacted: str = "***") -> Any:
    """Recursively redact secret values and sanitize non-secret URLs."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            normalized = str(key).strip().lower()
            if is_sensitive_key(key):
                out[key] = redacted
            elif normalized.endswith("_url") or normalized in {"backend_url", "endpoint"}:
                out[key] = sanitize_url(value)
            else:
                out[key] = sanitize_for_log(value, redacted=redacted)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [sanitize_for_log(value, redacted=redacted) for value in obj]
    return obj
