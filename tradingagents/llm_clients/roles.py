"""Phase B fixed LLM roles: Analysis vs Decision.

Two fixed roles, one resolver — no router, no runtime switching:

- **Analysis** serves every research node: the five analysts, bull/bear
  researchers, research manager, trader, and the risky/safe/neutral
  debators. Reflection and the legacy signal helper also stay on Analysis.
- **Decision** serves exactly one node: the Risk Manager, the only node
  whose structured output can become a TradeIntent.

Rules (implementation contract):
- With none of the six role keys set, the legacy quick/deep split and its
  provider/endpoint behavior are preserved untouched.
- Any single role key set switches to roles mode. Missing analysis
  provider/model default to ``llm_provider``/``deep_think_llm``; Decision
  defaults to the resolved Analysis when its own keys are absent.
- An explicit cross-provider Decision without a model is a startup config
  error — an incompatible model must never be silently inherited.
- Empty strings count as unset; explicitly invalid values are errors, never
  fallbacks. An explicitly set role provider is NOT rewritten by the global
  local-OpenAI switch.
- Credentials, endpoints, and provider kwargs are resolved per role; the
  Analysis key/base URL is never copied to a different-provider Decision.
- Role-specific env overrides (``ANALYSIS_OPENAI_API_KEY``,
  ``DECISION_ANTHROPIC_API_KEY``, ...) are checked before the provider's
  standard key so one vendor can be used with two accounts. Secrets are
  never persisted to UI config, logs, or sample values.
- Optional Analysis-only failover: when the ``analysis_fallback_provider``
  / ``analysis_fallback_model`` pair is set (endpoint optional), a second
  route is resolved for the same intended Analysis model and served by the
  failover wrapper in ``retry.py``. Any fallback key set without the
  required pair is a startup config error — failover fails closed. The
  fallback credential comes from ``ANALYSIS_FALLBACK_<PROVIDER>_API_KEY``
  before the provider's standard key, and a cross-provider fallback never
  inherits the Analysis endpoint. Decision and Screening never consume
  fallback configuration.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from tradingagents.dataflows.config import (
    get_llm_api_key,
    get_openai_base_url,
    is_local_openai_enabled,
)

ROLE_CONFIG_KEYS = (
    "analysis_provider",
    "analysis_model",
    "analysis_backend_url",
    "analysis_fallback_provider",
    "analysis_fallback_model",
    "analysis_fallback_backend_url",
    "decision_provider",
    "decision_model",
    "decision_backend_url",
)

_OPENAI_COMPATIBLE = (
    "openai",
    "local_openai",
    "xai",
    "minimax",
    "deepseek",
    "qwen",
    "glm",
    "ollama",
    "openrouter",
)
SUPPORTED_PROVIDERS = _OPENAI_COMPATIBLE + ("google", "anthropic", "azure")


class RoleConfigError(ValueError):
    """Invalid role configuration; startup must fail closed."""


@dataclass(frozen=True)
class RoleSpec:
    """One resolved role: provider/model/endpoint plus a credential."""

    role: str
    provider: str
    model: str
    backend_url: Optional[str]
    provider_explicit: bool

    def display_endpoint(self) -> str:
        """Endpoint safe for display: strips any query, fragment or userinfo secrets."""
        url = self.backend_url
        if not url:
            return ""
        # Keep the scheme+host+path only; query strings, fragments and
        # userinfo (user:password@host) can carry credentials and must never
        # reach logs or the UI. Redacted userinfo keeps the ***@ marker used
        # by retry.sanitize_error so operators can see credentials existed.
        text = str(url).split("?", 1)[0].split("#", 1)[0]
        return re.sub(r"(?<=://)[^/?#\s]*@", "***@", text)


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_provider_key(provider: str, role: str) -> str:
    """Role-specific env override first, then the provider's standard key."""
    provider_key = provider.upper().replace("/", "_")
    override_env = f"{role.upper()}_{provider_key}_API_KEY"
    override = os.getenv(override_env)
    if override and override.strip():
        return override.strip()
    return get_llm_api_key(provider) or ""


def resolve_role_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve Analysis/Decision roles from a config dict.

    Returns ``{"mode": "legacy"}`` or ``{"mode": "roles", "analysis":
    RoleSpec, "decision": RoleSpec, "analysis_api_key", "decision_api_key",
    "analysis_fallback": RoleSpec | None, "analysis_fallback_api_key"}``.
    Raises RoleConfigError for invalid combinations so startup fails closed.
    """
    values = {key: _clean(config.get(key)) for key in ROLE_CONFIG_KEYS}
    if not any(values.values()):
        return {"mode": "legacy"}

    global_provider = _clean(config.get("llm_provider")) or "openai"
    global_backend = _clean(config.get("backend_url"))
    # Mirror the legacy global resolution: the local-OpenAI switch rewrites
    # the global provider (never an explicitly configured role provider).
    effective_global_provider = global_provider
    if global_provider == "openai" and is_local_openai_enabled():
        effective_global_provider = "local_openai"

    # --- Analysis role ---
    analysis_provider = values["analysis_provider"]
    analysis_explicit = analysis_provider is not None
    if analysis_provider is None:
        analysis_provider = effective_global_provider
    _validate_provider(analysis_provider, "analysis_provider")

    analysis_model = values["analysis_model"]
    if analysis_model is None:
        if analysis_provider == effective_global_provider:
            analysis_model = _clean(config.get("deep_think_llm"))
        else:
            raise RoleConfigError(
                "analysis_model is required when analysis_provider "
                f"({analysis_provider!r}) differs from llm_provider "
                f"({effective_global_provider!r}): refusing to inherit the "
                "incompatible deep_think_llm model"
            )
    if analysis_model is None:
        raise RoleConfigError("analysis_model is required in roles mode")

    # Same-provider inheritance only; a cross-provider role never inherits
    # the global endpoint.
    analysis_backend_url = values["analysis_backend_url"]
    if analysis_backend_url is None and analysis_provider == effective_global_provider:
        analysis_backend_url = global_backend
    if analysis_provider == "local_openai":
        analysis_backend_url = analysis_backend_url or get_openai_base_url()

    # --- Decision role ---
    decision_provider = values["decision_provider"]
    decision_explicit_provider = decision_provider is not None
    if decision_provider is None:
        decision_provider = analysis_provider
    _validate_provider(decision_provider, "decision_provider")

    decision_model = values["decision_model"]
    if decision_model is None:
        if decision_provider == analysis_provider:
            decision_model = analysis_model
        else:
            raise RoleConfigError(
                "decision_model is required when decision_provider differs "
                f"from the analysis provider ('{decision_provider}' vs "
                f"'{analysis_provider}'): refusing to inherit an incompatible "
                "model"
            )

    decision_backend_url = values["decision_backend_url"]
    if decision_backend_url is None and decision_provider == analysis_provider:
        decision_backend_url = analysis_backend_url
    if decision_provider == "local_openai":
        decision_backend_url = decision_backend_url or get_openai_base_url()

    # --- Optional Analysis Fallback (Analysis role only) ---
    # Same intended model, second provider route. None of the three keys
    # set = failover disabled. Any key set = failover is intentional and
    # the provider/model pair is required; endpoint stays optional. A
    # cross-provider fallback never inherits the Analysis endpoint.
    fb_provider = values["analysis_fallback_provider"]
    fb_model = values["analysis_fallback_model"]
    fb_backend = values["analysis_fallback_backend_url"]
    analysis_fallback: Optional[RoleSpec] = None
    analysis_fallback_api_key = ""
    if fb_provider is not None or fb_model is not None or fb_backend is not None:
        if fb_provider is None or fb_model is None:
            raise RoleConfigError(
                "analysis_fallback_provider and analysis_fallback_model are "
                "both required when any analysis_fallback_* key is set: "
                "refusing a partially configured failover route"
            )
        _validate_provider(fb_provider, "analysis_fallback_provider")
        fb_backend_url = fb_backend
        if fb_backend_url is None and fb_provider == analysis_provider:
            fb_backend_url = analysis_backend_url
        if fb_provider == "local_openai":
            fb_backend_url = fb_backend_url or get_openai_base_url()
        analysis_fallback = RoleSpec(
            role="analysis_fallback",
            provider=fb_provider,
            model=fb_model,
            backend_url=fb_backend_url,
            provider_explicit=True,
        )
        analysis_fallback_api_key = _resolve_provider_key(
            fb_provider, "analysis_fallback"
        )

    analysis = RoleSpec(
        role="analysis",
        provider=analysis_provider,
        model=analysis_model,
        backend_url=analysis_backend_url,
        provider_explicit=analysis_explicit,
    )
    decision = RoleSpec(
        role="decision",
        provider=decision_provider,
        model=decision_model,
        backend_url=decision_backend_url,
        provider_explicit=decision_explicit_provider,
    )
    return {
        "mode": "roles",
        "analysis": analysis,
        "decision": decision,
        "analysis_api_key": _resolve_provider_key(analysis.provider, "analysis"),
        "decision_api_key": _resolve_provider_key(decision.provider, "decision"),
        "analysis_fallback": analysis_fallback,
        "analysis_fallback_api_key": analysis_fallback_api_key,
    }


def _validate_provider(provider: str, label: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise RoleConfigError(
            f"unsupported {label}: {provider!r} (supported: "
            f"{', '.join(SUPPORTED_PROVIDERS)})"
        )


def describe_roles(resolved: Dict[str, Any]) -> str:
    """Human-readable, secret-free role summary for logs and the UI."""
    if resolved.get("mode") != "roles":
        return "legacy quick/deep providers (no role overrides)"
    analysis: RoleSpec = resolved["analysis"]
    decision: RoleSpec = resolved["decision"]
    summary = (
        f"Analysis={analysis.provider}/{analysis.model}"
        f" endpoint={analysis.display_endpoint() or 'provider default'}; "
        f"Decision={decision.provider}/{decision.model}"
        f" endpoint={decision.display_endpoint() or 'provider default'}"
    )
    fallback: Optional[RoleSpec] = resolved.get("analysis_fallback")
    if fallback is not None:
        summary += (
            f"; AnalysisFallback={fallback.provider}/{fallback.model}"
            f" endpoint={fallback.display_endpoint() or 'provider default'}"
        )
    return summary
