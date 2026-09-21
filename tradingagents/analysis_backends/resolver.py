"""Resolve the explicit analysis topology used by the graph."""

from __future__ import annotations

from collections.abc import Mapping


VALID_ANALYSIS_BACKENDS = frozenset({"traders", "berkshire"})


def resolve_analysis_backend(config: Mapping | None) -> str:
    """Return a validated analysis backend, defaulting to native Traders."""

    backend = (config or {}).get("analysis_backend", "traders")
    if not isinstance(backend, str) or backend not in VALID_ANALYSIS_BACKENDS:
        allowed = ", ".join(sorted(VALID_ANALYSIS_BACKENDS))
        raise ValueError(
            f"Invalid analysis_backend {backend!r}; expected one of: {allowed}"
        )
    return backend
