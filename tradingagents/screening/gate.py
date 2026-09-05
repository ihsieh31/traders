"""Phase C entry gate: only today's validated Top20 may open exposure.

Program-derived (never LLM-asserted) and re-verified at the actual
execution dispatch — the single :class:`~tradingagents.execution.service.
ExecutionService` entry consults this gate for every exposure-adding leg,
so direct callers and checkpoint resumes cannot bypass it. The manual
watchlist mode is untouched: the gate is active only when the ambient run
config enables ``auto_screening_enabled``.

Fail-closed rules when the gate is active:
- New entries are refused on non-trading days entirely.
- Entries require a cache-valid selection for the current trading date;
  a stale, corrupted, or configuration-changed selection blocks every
  opening order (it never falls back to an old list).
- Symbols outside the validated Top20 (including extra holdings pulled in
  only for held-risk review) may HOLD, SELL, or take a verified
  risk-reducing exit — never a new BUY/LONG, a flip's opening leg, or a
  short. Overlapping Top20 members may add, still subject to the P2
  exposure caps and Phase A gates, which run after this gate.
- Crypto and other asset classes keep their existing management path and
  are not part of the US-equity entry ranking.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from tradingagents.screening.selection_store import (
    SelectionStore,
    default_selection_cache_path,
)
from tradingagents.screening.sessions import (
    eastern_now,
    is_us_trading_day,
)


def check_entry_allowed(
    symbol: str,
    *,
    config: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    store: Optional[SelectionStore] = None,
    resolved_spec: Any = None,
) -> Optional[str]:
    """Return ``None`` when a new entry is allowed, else a block reason.

    Reads only the on-disk validated selection — never in-memory round
    state — so a restarted process re-derives the same answer.
    """
    if config is None:
        from tradingagents.dataflows.config import get_config

        config = get_config() or {}
    if not config.get("auto_screening_enabled", False):
        return None  # manual watchlist mode: unchanged behavior

    normalized = str(symbol or "").upper().strip()
    if not normalized or "/" in normalized:
        return None  # other asset classes keep their existing path

    eastern = eastern_now(now)
    if not is_us_trading_day(eastern.date()):
        return (
            "auto screening: today is not a US equity trading day; "
            "new entries are blocked (held risk management only)"
        )

    if store is None:
        store = SelectionStore(default_selection_cache_path(config))
    selection = store.load_valid(config, spec=resolved_spec, now=now)
    if selection is None:
        return (
            "auto screening: no validated Top20 selection for the current "
            "trading date; new entries are blocked fail-closed"
        )
    top20 = {entry["symbol"] for entry in selection.get("top20", [])}
    if normalized not in top20:
        return (
            f"auto screening: {normalized} is not in today's validated Top20; "
            "holdings outside the selection may only HOLD or reduce risk"
        )
    return None
