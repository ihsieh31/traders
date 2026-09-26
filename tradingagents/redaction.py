"""Shared credential redaction for persisted logs and reports."""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Optional


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

# Well-known credential SHAPES found inside free text (error messages,
# URLs, prompts). Only high-entropy prefixed tokens — never bare numbers
# or ids — to keep false positives off hashes and timestamps.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)

# One-time snapshot of the exact secret VALUES the process was configured
# with (os.environ includes the loaded .env). Exact-value replacement is
# precise — no false positives — and covers whatever key shapes the
# operator actually uses, e.g. FRED's unprefixed alphanumeric keys.
_CONFIGURED_SECRET_VALUES: Optional[tuple[str, ...]] = None


def _configured_secret_values() -> tuple[str, ...]:
    global _CONFIGURED_SECRET_VALUES
    if _CONFIGURED_SECRET_VALUES is None:
        values = []
        for key, value in os.environ.items():
            if not value:
                continue
            upper = key.upper()
            if any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                cleaned = value.strip()
                if len(cleaned) >= 16 and not cleaned.startswith("$"):
                    values.append(cleaned)
        _CONFIGURED_SECRET_VALUES = tuple(values)
    return _CONFIGURED_SECRET_VALUES


def _scrub_secret_values(text: str, *, redacted: str = "***") -> str:
    # Keep the "api_key=" prefix readable so the scrubbed message stays
    # diagnosable, then blanket-redact the bare token shapes.
    text = re.sub(
        r"(?i)(api[_-]?key=)([A-Za-z0-9._~-]{16,})",
        lambda match: f"{match.group(1)}{redacted}",
        text,
    )
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(redacted, text)
    for value in _configured_secret_values():
        if value in text:
            text = text.replace(value, redacted)
    return text
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
    """Recursively redact secret keys and secret-looking values, sanitize URLs.

    Two redaction layers:
    - key-based: any sensitive-keyed entry is redacted wholesale;
    - value-based: string values are scrubbed for well-known credential
      shapes and for the EXACT values of key/token/secret/password entries
      currently configured in the process environment (loaded .env
      included). An exception string that embeds the credential in its
      message — e.g. an HTTP error echoing the request URL — therefore
      cannot leak it through a non-sensitive key.
    """
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
    if isinstance(obj, str):
        return _scrub_secret_values(obj, redacted=redacted)
    return obj
