"""Phase C third fixed LLM role: Screening.

Screening is a candidate *ranker*: it never forms a TradeIntent, never
receives a broker credential, and never sees holdings, cash or news.
Resolution follows the Phase B role pattern but is deliberately stricter:

- The role exists only when ``auto_screening_enabled`` is true; then
  ``screening_provider`` AND ``screening_model`` are required. There is
  no inheritance from ``llm_provider`` / ``deep_think_llm`` and no
  cross-role fallback — a missing value is a startup config error.
- The same vendor as Analysis is fine; provider/model/endpoint/key are
  resolved independently (role-specific env ``SCREENING_<PROVIDER>_API_KEY``
  first, then the provider's standard key).
- With screening disabled nothing here builds or calls a client.

Output contract (strict, validated before anything is stored): exactly
``select_n`` (20) entries with unique consecutive ranks 1..20, symbols
drawn from the presented input, finite 0..100 scores and non-empty
reasons ≤300 chars. A successful response that fails validation is a
screening failure (no repair request, no second LLM round); only
transport/permanent provider access failures go through the P2 retry
owner.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tradingagents.llm_clients.retry import RetryingLLM
from tradingagents.llm_clients.roles import (
    RoleConfigError,
    RoleSpec,
    _clean,
    _resolve_provider_key,
    _validate_provider,
)

SCREENING_ROLE = "screening"


class ScreeningConfigError(RoleConfigError):
    """Invalid screening role configuration; startup must fail closed."""


class ScreeningStop(RuntimeError):
    """A screening-stage failure that stops the whole round.

    ``reason`` is a stable machine-readable code (e.g.
    ``INSUFFICIENT_CANDIDATES``); ``detail`` is operator-facing text.
    No downstream analysis/decision/execution may run after this.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail or reason
        super().__init__(f"{reason}: {self.detail}" if detail else reason)


class ScreenedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, le=1_000)
    symbol: str = Field(min_length=1, max_length=16)
    screening_score: float
    short_reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def _check_fields(self):
        if not math.isfinite(self.screening_score) or not 0.0 <= self.screening_score <= 100.0:
            raise ValueError("screening_score must be a finite number in [0, 100]")
        if self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be uppercase with no surrounding whitespace")
        return self


class ScreeningOutput(BaseModel):
    """Structured Screening response. Structural checks that the schema can
    express live here (extra fields forbidden, finiteness, uniqueness); the
    exact count, rank consecutiveness (1..N) and input-membership checks run
    in :func:`invoke_screening_structured` so the failure surfaces as an
    explicit screening stop instead of a transport-shaped error."""

    model_config = ConfigDict(extra="forbid")

    candidates: List[ScreenedCandidate] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _check_uniqueness(self):
        ranks = [c.rank for c in self.candidates]
        if len(set(ranks)) != len(ranks):
            raise ValueError("duplicate ranks in screening output")
        symbols = [c.symbol for c in self.candidates]
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate symbols in screening output")
        return self

    def sorted_by_rank(self) -> List[ScreenedCandidate]:
        return sorted(self.candidates, key=lambda c: c.rank)


def resolve_screening_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the Screening role; strict — no fallback, no inheritance.

    Returns ``{"enabled": False}`` when auto screening is off (callers must
    not build any client), or ``{"enabled": True, "spec": RoleSpec,
    "api_key": str}``. Raises :class:`ScreeningConfigError` on any missing
    or unsupported value while enabled.
    """
    enabled = bool(config.get("auto_screening_enabled", False))
    if not enabled:
        return {"enabled": False}

    provider = _clean(config.get("screening_provider"))
    if provider is None:
        raise ScreeningConfigError(
            "auto_screening_enabled requires screening_provider: refusing to "
            "inherit the Analysis/global provider for the Screening role"
        )
    model = _clean(config.get("screening_model"))
    if model is None:
        raise ScreeningConfigError(
            "auto_screening_enabled requires screening_model: refusing to "
            "inherit the Analysis/global model for the Screening role"
        )
    try:
        _validate_provider(provider, "screening_provider")
    except RoleConfigError as exc:
        raise ScreeningConfigError(str(exc)) from exc
    backend_url = _clean(config.get("screening_backend_url"))
    if provider == "local_openai":
        from tradingagents.dataflows.config import get_openai_base_url

        backend_url = backend_url or get_openai_base_url()

    spec = RoleSpec(
        role=SCREENING_ROLE,
        provider=provider,
        model=model,
        backend_url=backend_url,
        provider_explicit=True,
    )
    return {
        "enabled": True,
        "spec": spec,
        "api_key": _resolve_provider_key(provider, SCREENING_ROLE),
    }


def build_screening_llm(resolved: Dict[str, Any], config: Dict[str, Any]) -> RetryingLLM:
    """Build the bounded-retry Screening client (low-randomness profile).

    Reuses the P2 single retry owner (``llm_max_retries``) and the same
    provider adapters; the low-randomness preference maps to the existing
    quick profile's model params (no temperature is injected for models
    that do not declare one).
    """
    if not resolved.get("enabled"):
        raise ScreeningConfigError("screening is disabled; no client may be built")
    spec: RoleSpec = resolved["spec"]
    api_key = resolved.get("api_key") or ""
    if not api_key:
        raise ScreeningConfigError(
            f"no API key resolved for the Screening role "
            f"(set SCREENING_{spec.provider.upper()}_API_KEY or the provider's standard key)"
        )

    from tradingagents.llm_clients import create_llm_client
    from tradingagents.openai_model_registry import normalize_model_params

    params = normalize_model_params(
        spec.model,
        (config or {}).get("quick_llm_params"),
        role="quick",
    )
    client = create_llm_client(
        provider=spec.provider,
        model=spec.model,
        base_url=spec.backend_url,
        api_key=api_key,
        model_role="quick",
        **params,
    )
    from tradingagents.default_config import DEFAULT_CONFIG

    max_retries = (config or {}).get("llm_max_retries", DEFAULT_CONFIG.get("llm_max_retries", 3))
    return RetryingLLM(
        client.get_llm(),
        role=SCREENING_ROLE,
        provider=spec.provider,
        model=spec.model,
        max_retries=int(max_retries),
    )


def describe_screening_role(resolved: Dict[str, Any]) -> str:
    if not resolved.get("enabled"):
        return "screening disabled (manual watchlist mode)"
    spec: RoleSpec = resolved["spec"]
    return (
        f"Screening={spec.provider}/{spec.model}"
        f" endpoint={spec.display_endpoint() or 'provider default'}"
    )


def invoke_screening_structured(
    screening_llm: Any,
    prompt: List[dict],
    *,
    expected_count: int,
    input_symbols: set,
) -> List[ScreenedCandidate]:
    """One logical Screening invocation with strict output validation.

    Transport/permanent provider failures propagate as
    :class:`ProviderFailure` (the P2 retry owner already capped the real
    request count). A *successful* response whose payload fails the schema
    or the membership/count checks raises :class:`ScreeningStop` — no
    repair request, no second LLM round, no fallback model.
    """
    from tradingagents.llm_clients.retry import ProviderFailure

    try:
        structured = screening_llm.with_structured_output(ScreeningOutput)
    except Exception as exc:
        # Binding makes no HTTP request; a bind failure must fail the
        # screening stage closed instead of falling back to free text.
        raise ScreeningStop(
            "SCREENING_STRUCTURED_UNAVAILABLE",
            f"structured output binding failed for the Screening role ({type(exc).__name__})",
        ) from exc
    if structured is None:
        raise ScreeningStop(
            "SCREENING_STRUCTURED_UNAVAILABLE",
            "structured output binding failed for the Screening role",
        )
    try:
        raw = structured.invoke(prompt)
    except ProviderFailure as exc:
        cause = exc.__cause__
        cause_name = type(cause).__name__ if cause is not None else ""
        if cause is not None and (
            isinstance(cause, ValidationError)
            or cause_name in ("OutputParserException", "ValidationError")
        ):
            # Successful HTTP response, unusable payload: a screening
            # failure, not a provider outage. Zero repair requests.
            raise ScreeningStop(
                "SCREENING_INVALID_OUTPUT",
                f"model returned a schema-invalid screening payload ({cause_name})",
            ) from exc
        raise
    except ScreeningStop:
        raise
    except ValidationError as exc:
        raise ScreeningStop(
            "SCREENING_INVALID_OUTPUT",
            "model returned a schema-invalid screening payload",
        ) from exc

    if isinstance(raw, ScreeningOutput):
        output = raw
    else:
        try:
            output = ScreeningOutput.model_validate(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            raise ScreeningStop(
                "SCREENING_INVALID_OUTPUT",
                "model returned a schema-invalid screening payload",
            ) from exc

    candidates = output.sorted_by_rank()
    if len(candidates) != expected_count:
        raise ScreeningStop(
            "SCREENING_INVALID_OUTPUT",
            f"expected exactly {expected_count} candidates, got {len(candidates)}",
        )
    ranks = [candidate.rank for candidate in candidates]
    if ranks != list(range(1, expected_count + 1)):
        raise ScreeningStop(
            "SCREENING_INVALID_OUTPUT",
            f"ranks must be exactly 1..{expected_count} consecutive, got {ranks}",
        )
    for candidate in candidates:
        if candidate.symbol not in input_symbols:
            raise ScreeningStop(
                "SCREENING_INVALID_OUTPUT",
                f"candidate {candidate.symbol!r} is not a member of the presented input",
            )
    return candidates
