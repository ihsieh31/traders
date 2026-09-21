"""Analysis-method profile resolution for controlled analyst experiments.

Profiles deliberately select prompt templates only.  Analyst topology,
tooling, tool-loop limits, and every downstream decision node remain shared.
"""

from __future__ import annotations

from collections.abc import Mapping


VALID_ANALYSIS_PROFILES = frozenset({"traders", "berkshire"})

_TRADERS_PROMPTS = {
    "fundamentals": "analysts/fundamentals_system",
    "news": "analysts/news_system",
    "macro": "analysts/macro_system",
    "social": "analysts/social_system",
}

_BERKSHIRE_PROMPTS = {
    "fundamentals": "berkshire/fundamentals_system",
    "news": "berkshire/news_system",
    "macro": "berkshire/macro_system",
    "social": "berkshire/social_system",
}

PROFILE_PROMPTS = {
    "traders": _TRADERS_PROMPTS,
    "berkshire": _BERKSHIRE_PROMPTS,
}


def resolve_analysis_profile(config: Mapping | None) -> str:
    """Return a validated analysis profile, defaulting to ``traders``."""

    profile = (config or {}).get("analysis_profile", "traders")
    if not isinstance(profile, str) or profile not in VALID_ANALYSIS_PROFILES:
        allowed = ", ".join(sorted(VALID_ANALYSIS_PROFILES))
        raise ValueError(
            f"Invalid analysis_profile {profile!r}; expected one of: {allowed}"
        )
    return profile


def analyst_prompt(config: Mapping | None, analyst: str) -> str:
    """Resolve the template name for one analyst.

    Market is intentionally absent: the technical analyst is shared by both
    profiles so the first A/B experiment changes only research methodology.
    """

    # Technical analysis is deliberately invariant across the experiment.
    if analyst == "market":
        resolve_analysis_profile(config)
        return "analysts/market_system"

    profile = resolve_analysis_profile(config)
    try:
        return PROFILE_PROMPTS[profile][analyst]
    except KeyError as exc:
        supported = ", ".join(sorted(PROFILE_PROMPTS[profile]))
        raise ValueError(
            f"Analyst {analyst!r} has no profile-specific prompt; "
            f"supported analysts: {supported}"
        ) from exc
