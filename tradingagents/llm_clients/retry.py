"""Phase B single LLM retry owner.

Exactly one bounded retry layer exists for every LLM invocation path —
plain ``invoke``, structured-output runnables, and tool-bound runnables.
SDK-level retries are pinned to 0 by the adapters so nothing retries twice.

Policy (implementation contract):
- ``llm_max_retries`` must be an integer 0-3. First try + at most N retries
  = at most N+1 actual requests per logical LLM invocation.
- Transient failures (connect/timeouts, HTTP 429, 5xx) are retried with a
  capped exponential backoff. Permanent failures (401/403, invalid key,
  unknown model, malformed request) stop immediately — no retry.
- Every request runs under a finite per-request timeout owned by the
  adapter; this layer never issues an unbounded call.
- Exhaustion or a permanent failure raises :class:`ProviderFailure` carrying
  role/provider/model, the real attempt count, and a sanitized error
  category/message. The exception propagates: nodes must not swallow it
  into free-text fallbacks, empty reports, or a normal NO_TRADE.
- Non-retryable semantic failures (schema/bind/validation on a *successful*
  response) are NOT classified here: a successful response is returned
  as-is so strict callers can emit INVALID/NO_TRADE without another model
  request.

The Analysis-only failover siblings at the bottom of this module
(:class:`FailoverRetryingLLM` and friends) reuse the exact same policy over
one Primary/Fallback route pair that shares a single request budget. They
are additive: :class:`RetryingLLM` stays the retry owner for every
non-failover path.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Optional

from langchain_core.runnables import Runnable

_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
_TRANSIENT_TEXT_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "temporarily unavailable",
    "rate limit",
    "too many requests",
    "429",
    "overloaded",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "socket",
    "remote end closed",
)
_PERMANENT_TEXT_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "incorrect api key",
    "authentication",
    "invalid model",
    "model not found",
    "does not exist",
    "malformed",
    "invalid request",
    "unsupported",
    "permission denied",
)


class ProviderFailure(RuntimeError):
    """A provider access failure after its bounded retry policy ended.

    ``attempts`` is the real number of requests issued for the logical
    invocation (1 on an immediate permanent failure, up to 1+max_retries on
    transient exhaustion). ``category`` is "transient" or "permanent".
    The message is sanitized: no API keys or auth headers.
    """

    def __init__(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        attempts: int,
        category: str,
        detail: str,
    ):
        self.role = role
        self.provider = provider
        self.model = model
        self.attempts = int(attempts)
        self.category = category
        self.detail = sanitize_error(detail)
        super().__init__(
            f"LLM {category} failure after {self.attempts} request(s) "
            f"(role={role}, provider={provider}, model={model}): {self.detail}"
        )

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "attempts": self.attempts,
            "category": self.category,
            "error": self.detail,
        }


def sanitize_error(detail: Any) -> str:
    """Strip credentials from an error string before it leaves this layer."""
    import re

    text = str(detail or "")
    # Redact scheme credentials (user:pass@host) and query strings, which can
    # carry auth parameters, then anything key-like after key/token markers.
    text = re.sub(r"(?<=://)[^/?#\s]*@", "***@", text)
    text = re.sub(r"\?[^#\s]*", "?***", text)
    text = re.sub(r"(?i)(bearer\s+|api_key=|api-key=|key=|token=)([^\s\",;)]+)", r"\1***", text)
    # Common provider key shapes (sk-..., sk-proj-..., ghp_..., etc.).
    text = re.sub(r"\b(sk-[A-Za-z0-9_-]{8,}|sk-proj-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9]+)\b", "sk-***", text)
    return text[:600]


def classify_provider_error(exc: BaseException) -> str:
    """Classify a provider exception as "transient" or "permanent".

    Underlying families differ (openai, anthropic, google, azure), so the
    check combines the exception's status code attribute with marker text.
    An explicit permanent match wins over a transient match; unknown shapes
    are treated as permanent (fail fast, fail closed) rather than retried.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status in _RETRYABLE_STATUS_CODES:
            return "transient"
        if 400 <= status < 500 and status not in (408, 409, 429):
            return "permanent"
    if status in ("RESOURCE_EXHAUSTED", "UNAVAILABLE"):
        return "transient"
    for marker in _PERMANENT_TEXT_MARKERS:
        if marker in text:
            return "permanent"
    for marker in _TRANSIENT_TEXT_MARKERS:
        if marker in text:
            return "transient"
    return "permanent"


def validate_llm_max_retries(value: Any) -> int:
    """Validate llm_max_retries: integer 0-3 only, anything else is an error."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"llm_max_retries must be an integer 0-3, got {value!r}"
        )
    if not 0 <= value <= 3:
        raise ValueError(
            f"llm_max_retries must be an integer 0-3, got {value!r}"
        )
    return value


class _RetryController:
    def __init__(self, *, role: str, provider: str, model: str, max_retries: int):
        self.role = role
        self.provider = provider
        self.model = model
        self.max_retries = validate_llm_max_retries(max_retries)
        self.backoff_cap = float(
            os.getenv("TRADINGAGENTS_LLM_RETRY_BACKOFF_MAX_SECONDS", "4.0")
        )
        self.sleep = time.sleep

    def run(self, call) -> Any:
        total = 1 + self.max_retries
        last_exc: Optional[BaseException] = None
        for attempt in range(1, total + 1):
            try:
                return call()
            except ProviderFailure:
                raise
            except Exception as exc:
                last_exc = exc
                category = classify_provider_error(exc)
                if category == "permanent" or attempt >= total:
                    raise ProviderFailure(
                        role=self.role,
                        provider=self.provider,
                        model=self.model,
                        attempts=attempt,
                        category=category,
                        detail=f"{type(exc).__name__}: {exc}",
                    ) from exc
                backoff = min(self.backoff_cap, 0.5 * (2 ** (attempt - 1)))
                if not math.isnan(backoff) and backoff > 0:
                    self.sleep(backoff)
        raise ProviderFailure(
            role=self.role,
            provider=self.provider,
            model=self.model,
            attempts=total,
            category="transient",
            detail=f"{type(last_exc).__name__}: {last_exc}",
        )


class RetryingRunnable(Runnable):
    """Wraps a structured-output or tool-bound runnable with the same policy.

    Extends :class:`~langchain_core.runnables.Runnable` so the wrapper can
    compose in real agent chains (``prompt | llm.bind_tools(tools)``); the
    acceptance found that a plain wrapper breaks every analyst's LCEL chain
    construction with TypeError before any request is issued.
    """

    def __init__(self, inner: Any, controller: _RetryController):
        self._inner = inner
        self._controller = controller

    @property
    def inner(self) -> Any:
        return self._inner

    def invoke(self, input: Any, config: Optional[Any] = None, **kwargs) -> Any:
        return self._controller.run(
            lambda: (
                self._inner.invoke(input, config, **kwargs)
                if config is not None
                else self._inner.invoke(input, **kwargs)
            )
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RetryingLLM(Runnable):
    """Single retry owner around one resolved role client.

    Extends :class:`~langchain_core.runnables.Runnable` so it composes with
    LangChain prompt chains exactly like the unwrapped client it replaces.
    Delegates every attribute to the wrapped LLM except ``invoke``,
    ``with_structured_output`` and ``bind_tools``, which run under the
    bounded retry policy so structured and tool paths obey the same limit.
    """

    def __init__(
        self,
        inner: Any,
        *,
        role: str,
        provider: str,
        model: str,
        max_retries: int,
    ):
        self._inner = inner
        self._controller = _RetryController(
            role=role, provider=provider, model=model, max_retries=max_retries
        )

    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def retry_policy(self) -> dict:
        return {
            "role": self._controller.role,
            "provider": self._controller.provider,
            "model": self._controller.model,
            "max_retries": self._controller.max_retries,
            "max_requests_per_invocation": 1 + self._controller.max_retries,
        }

    def invoke(self, input: Any, config: Optional[Any] = None, **kwargs) -> Any:
        return self._controller.run(
            lambda: (
                self._inner.invoke(input, config, **kwargs)
                if config is not None
                else self._inner.invoke(input, **kwargs)
            )
        )

    def with_structured_output(self, schema: Any, **kwargs) -> RetryingRunnable:
        return RetryingRunnable(
            self._inner.with_structured_output(schema, **kwargs), self._controller
        )

    def bind_tools(self, tools: Any, **kwargs) -> RetryingRunnable:
        return RetryingRunnable(self._inner.bind_tools(tools, **kwargs), self._controller)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _FailoverRetryController:
    """Shared-budget Primary→Fallback owner for the Analysis role.

    One logical invocation gets exactly ``1 + max_retries`` provider
    requests, shared between Primary and Fallback — they do NOT each
    receive the budget. Attempt 1 is always Primary. A transient Primary
    failure switches every remaining attempt to Fallback (no Primary retry
    before failover, and no bouncing back: once switched, the invocation
    stays on Fallback). Permanent failures stop immediately — failover
    must never hide a misconfiguration or an incompatible request.
    Selection state is per ``run`` call (per logical invocation), never
    global, so concurrent invocations cannot affect each other.
    """

    def __init__(
        self,
        *,
        role: str,
        primary_provider: str,
        primary_model: str,
        fallback_provider: str,
        fallback_model: str,
        max_retries: int,
        on_switch=None,
    ):
        self.role = role
        self.primary_provider = primary_provider
        self.primary_model = primary_model
        self.fallback_provider = fallback_provider
        self.fallback_model = fallback_model
        self.max_retries = validate_llm_max_retries(max_retries)
        self.backoff_cap = float(
            os.getenv("TRADINGAGENTS_LLM_RETRY_BACKOFF_MAX_SECONDS", "4.0")
        )
        self.sleep = time.sleep
        self.on_switch = on_switch  # best-effort audit hook: payload dict -> None

    @property
    def max_requests_per_invocation(self) -> int:
        return 1 + self.max_retries

    def _emit_switch(self, trigger_category: str, next_attempt: int) -> None:
        if self.on_switch is None:
            return
        try:
            self.on_switch(
                {
                    "role": self.role,
                    "from_provider": self.primary_provider,
                    "from_model": self.primary_model,
                    "to_provider": self.fallback_provider,
                    "to_model": self.fallback_model,
                    "trigger_category": trigger_category,
                    "attempt": next_attempt,
                    "max_requests_per_invocation": self.max_requests_per_invocation,
                }
            )
        except Exception:
            pass  # audit logging must never affect LLM execution

    def run(self, primary_call, fallback_call) -> Any:
        total = self.max_requests_per_invocation
        attempt = 0
        on_fallback = False
        while attempt < total:
            attempt += 1
            provider = self.fallback_provider if on_fallback else self.primary_provider
            model = self.fallback_model if on_fallback else self.primary_model
            call = fallback_call if on_fallback else primary_call
            try:
                return call()
            except ProviderFailure:
                raise
            except Exception as exc:
                category = classify_provider_error(exc)
                if category == "permanent" or attempt >= total:
                    raise ProviderFailure(
                        role=self.role,
                        provider=provider,
                        model=model,
                        attempts=attempt,
                        category=category,
                        detail=f"{type(exc).__name__}: {exc}",
                    ) from exc
                if not on_fallback:
                    on_fallback = True
                    self._emit_switch(category, attempt + 1)
                backoff = min(self.backoff_cap, 0.5 * (2 ** (attempt - 1)))
                if not math.isnan(backoff) and backoff > 0:
                    self.sleep(backoff)
        raise ProviderFailure(
            role=self.role,
            provider=self.fallback_provider,
            model=self.fallback_model,
            attempts=total,
            category="transient",
            detail="retry budget exhausted",
        )


class FailoverRetryingRunnable(Runnable):
    """Failover-aware structured-output / tool-bound runnable.

    Both route runnables are built from the same schema or tools and share
    the one request budget owned by :class:`_FailoverRetryController`.
    Never wrap each side in its own ``RetryingLLM`` — that would double
    the cap.
    """

    def __init__(
        self,
        primary: Any,
        fallback: Any,
        controller: _FailoverRetryController,
    ):
        self._primary = primary
        self._fallback = fallback
        self._controller = controller

    @property
    def inner(self) -> Any:
        return self._primary

    @property
    def fallback(self) -> Any:
        return self._fallback

    def invoke(self, input: Any, config: Optional[Any] = None, **kwargs) -> Any:
        if config is not None:
            return self._controller.run(
                lambda: self._primary.invoke(input, config, **kwargs),
                lambda: self._fallback.invoke(input, config, **kwargs),
            )
        return self._controller.run(
            lambda: self._primary.invoke(input, **kwargs),
            lambda: self._fallback.invoke(input, **kwargs),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


class FailoverRetryingLLM(Runnable):
    """Analysis-only Primary→Fallback wrapper with one shared retry budget.

    Sibling of :class:`RetryingLLM`, which stays unchanged for every
    non-failover path. Attempt 1 always goes to Primary; after a transient
    Primary failure the remaining budget stays on Fallback; permanent
    failures stop immediately; exhaustion raises :class:`ProviderFailure`.
    ``with_structured_output`` and ``bind_tools`` build both route runnables
    with the same schema/tools and share the controller, so every Analysis
    invocation surface obeys identical rules. Unknown attributes delegate
    to the Primary inner LLM. Extends
    :class:`~langchain_core.runnables.Runnable` so existing LCEL chains
    keep composing.
    """

    def __init__(
        self,
        primary_inner: Any,
        fallback_inner: Any,
        *,
        role: str = "analysis",
        primary_provider: str,
        primary_model: str,
        fallback_provider: str,
        fallback_model: str,
        max_retries: int,
        on_switch=None,
    ):
        self._primary = primary_inner
        self._fallback = fallback_inner
        self._controller = _FailoverRetryController(
            role=role,
            primary_provider=primary_provider,
            primary_model=primary_model,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
            max_retries=max_retries,
            on_switch=(
                on_switch if on_switch is not None else self._audit_failover_event
            ),
        )

    @property
    def inner(self) -> Any:
        return self._primary

    @property
    def fallback_inner(self) -> Any:
        return self._fallback

    @staticmethod
    def _audit_failover_event(payload: dict) -> None:
        """Best-effort secret-free failover event via the run audit logger.

        Reuses ``RunAuditLogger.log_event`` (arbitrary event types are
        already persisted); logging failure never affects LLM execution.
        """
        try:
            from tradingagents.run_logger import get_run_audit_logger

            get_run_audit_logger().log_event(
                event_type="llm_provider_failover", payload=payload
            )
        except Exception:
            pass

    @property
    def retry_policy(self) -> dict:
        return {
            "role": self._controller.role,
            "primary_provider": self._controller.primary_provider,
            "primary_model": self._controller.primary_model,
            "fallback_provider": self._controller.fallback_provider,
            "fallback_model": self._controller.fallback_model,
            "max_retries": self._controller.max_retries,
            "max_requests_per_invocation": self._controller.max_requests_per_invocation,
            "failover_enabled": True,
        }

    def invoke(self, input: Any, config: Optional[Any] = None, **kwargs) -> Any:
        if config is not None:
            return self._controller.run(
                lambda: self._primary.invoke(input, config, **kwargs),
                lambda: self._fallback.invoke(input, config, **kwargs),
            )
        return self._controller.run(
            lambda: self._primary.invoke(input, **kwargs),
            lambda: self._fallback.invoke(input, **kwargs),
        )

    def with_structured_output(self, schema: Any, **kwargs) -> FailoverRetryingRunnable:
        return FailoverRetryingRunnable(
            self._primary.with_structured_output(schema, **kwargs),
            self._fallback.with_structured_output(schema, **kwargs),
            self._controller,
        )

    def bind_tools(self, tools: Any, **kwargs) -> FailoverRetryingRunnable:
        return FailoverRetryingRunnable(
            self._primary.bind_tools(tools, **kwargs),
            self._fallback.bind_tools(tools, **kwargs),
            self._controller,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)
