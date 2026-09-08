"""F15: one common token-usage accounting path.

Every provider-reported token count must reach the daily SafetyGuard LLM
token budget exactly once, and (best-effort) the active audit run exactly
once. This module provides:

- :func:`normalize_usage_map` — normalize provider usage into
  ``{"input_tokens", "output_tokens", "total_tokens"}``.
- :func:`extract_langchain_usage` — extract usage from the supported
  LangChain result shapes, in priority order: message ``usage_metadata``,
  provider ``response_metadata`` token usage, then ``LLMResult.llm_output``
  token usage.
- :class:`UsageAccountingCallback` — the LangChain callback that records
  one ``llm_call`` audit event per successful model call through
  ``RunAuditLogger`` (which owns the SafetyGuard increment).
- :func:`record_direct_openai_usage` — the same accounting for the direct
  OpenAI SDK dataflow calls that bypass ``create_llm_client``.

Rules: only provider-reported tokens count; missing usage is never
estimated from character counts; accounting failures never crash model
execution.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from langchain_core.callbacks import BaseCallbackHandler

_START_TIMES: Dict[str, float] = {}
_START_TIMES_LOCK = threading.Lock()


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def normalize_usage_map(raw: Any) -> Dict[str, int]:
    """Normalize one provider usage object/dict into the common shape.

    Handles the OpenAI Responses usage, the OpenAI chat-completions usage,
    the Anthropic usage, and the Google token-count naming. The total is
    provider-reported when present and input+output only when it is absent
    (never estimated from characters).
    """
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if raw is None:
        return usage

    if isinstance(raw, dict):
        def _get(key: str) -> Any:
            return raw.get(key)
    else:
        def _get(key: str) -> Any:
            return getattr(raw, key, None)

    input_tokens = _positive_int(
        _get("input_tokens") or _get("prompt_tokens") or _get("prompt_token_count")
    )
    output_tokens = _positive_int(
        _get("output_tokens")
        or _get("completion_tokens")
        or _get("candidates_token_count")
    )
    total = _positive_int(_get("total_tokens") or _get("total_token_count"))
    usage["input_tokens"] = input_tokens
    usage["output_tokens"] = output_tokens
    usage["total_tokens"] = total if total else input_tokens + output_tokens
    return usage


def extract_langchain_usage(response: Any) -> Tuple[Dict[str, int], Optional[str]]:
    """Return (usage, model_name) from one LLMResult, best effort."""
    usage: Dict[str, int] = {}
    model_name: Optional[str] = None

    for generation in (getattr(response, "generations", None) or []):
        message = getattr(generation, "message", None)
        if message is None:
            continue
        usage_metadata = getattr(message, "usage_metadata", None)
        if usage_metadata:
            usage = normalize_usage_map(usage_metadata)
            break
        response_metadata = getattr(message, "response_metadata", None)
        if isinstance(response_metadata, dict):
            if not model_name:
                model_name = str(
                    response_metadata.get("model_name")
                    or response_metadata.get("model")
                    or ""
                ).strip() or None
            for key in ("token_usage", "usage"):
                if response_metadata.get(key):
                    usage = normalize_usage_map(response_metadata[key])
                    break
            if not usage and any(
                key in response_metadata
                for key in ("prompt_token_count", "candidates_token_count", "total_token_count")
            ):
                usage = normalize_usage_map(response_metadata)
            if usage:
                break

    if not usage:
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict):
            if not model_name:
                model_name = str(llm_output.get("model_name") or "").strip() or None
            for key in ("token_usage", "usage"):
                if llm_output.get(key):
                    usage = normalize_usage_map(llm_output[key])
                    break

    return usage, model_name


class UsageAccountingCallback(BaseCallbackHandler):
    """Record provider-reported token usage exactly once per model call.

    Attached once by ``create_llm_client`` to every constructed LLM (next
    to any caller/UI callbacks). The audit event goes through
    ``RunAuditLogger.log_event`` so the SafetyGuard daily budget is fed in
    exactly one place.
    """

    def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id") or "")
        if run_id:
            with _START_TIMES_LOCK:
                _START_TIMES[run_id] = time.monotonic()

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        # F15: Responses-adapter results are accounted by the adapter itself
        # (its custom invoke bypasses the callback manager); never count a
        # call twice.
        for generation in (getattr(response, "generations", None) or []):
            message = getattr(generation, "message", None)
            if message is not None and getattr(
                getattr(message, "additional_kwargs", None), "get", lambda *_: None
            )("usage_accounted_by_adapter"):
                return
        run_id = str(kwargs.get("run_id") or "")
        started: Optional[float] = None
        if run_id:
            with _START_TIMES_LOCK:
                started = _START_TIMES.pop(run_id, None)
        try:
            usage, model_name = extract_langchain_usage(response)
            payload: Dict[str, Any] = {
                "purpose": "langchain_provider",
                "status": "success",
                "usage": usage,
            }
            if model_name:
                payload["model"] = model_name
            if started is not None:
                payload["latency_seconds"] = round(
                    max(0.0, time.monotonic() - started), 4
                )
            from tradingagents.run_logger import get_run_audit_logger

            get_run_audit_logger().log_event(event_type="llm_call", payload=payload)
        except Exception:
            # Accounting must never break model execution.
            pass

    def on_llm_error(self, error: Any, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id") or "")
        if run_id:
            with _START_TIMES_LOCK:
                _START_TIMES.pop(run_id, None)


def ensure_usage_callback(callbacks: Any) -> list:
    """Exactly one usage-accounting callback per constructed LLM.

    Caller/UI callbacks are preserved as-is; a callback already supplied by
    an inner construction is never duplicated.
    """
    merged = list(callbacks) if callbacks else []
    for callback in merged:
        if isinstance(callback, UsageAccountingCallback):
            return merged
    merged.append(UsageAccountingCallback())
    return merged


def record_direct_openai_usage(response: Any, *, model: str, purpose: str) -> None:
    """Account for one successful direct OpenAI SDK call (dataflow tools).

    Only provider-reported usage counts; a request that throws before a
    response exists never invents tokens.
    """
    usage = normalize_usage_map(getattr(response, "usage", None))
    payload = {
        "model": str(model or ""),
        "purpose": str(purpose or "direct_openai"),
        "status": "success",
        "usage": usage,
    }
    try:
        from tradingagents.run_logger import get_run_audit_logger

        get_run_audit_logger().log_event(event_type="llm_call", payload=payload)
    except Exception:
        pass
