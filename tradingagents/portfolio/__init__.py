"""Deterministic portfolio-level intelligence.

The agent pipeline analyzes each symbol in isolation; nothing ever looks at
the book as a whole. This layer sits above the per-symbol decisions and
adjusts the size of NEW long exposure with three plain-arithmetic guards —
zero LLM involvement, so it cannot be argued out of a limit:

- **Correlation penalty**: a candidate whose returns correlate above a
  threshold (default 0.6 — the level practitioners treat as duplicated
  risk) with any existing position is scaled down. Only positive
  correlation is penalized; a strong negative correlation is a hedge.
- **Inverse-volatility sizing** (simplified risk parity): realized daily
  volatility above the target scales the size by target/realized. Chosen
  over optimization because it needs no return forecasts and stays stable.
- **Gross exposure cap**: total book value is clipped to a percentage of
  equity; this is the only guard allowed to zero a trade, and it says why.

Sizing rules: factors never size a trade UP (the agents' requested amount
is the ceiling), missing data never punishes (factor 1.0 plus a warning),
and combined penalties are floored so trades cannot silently vanish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

_MIN_OVERLAP_BARS = 20  # fewer shared bars than this makes correlation noise


@dataclass
class PortfolioLimitsConfig:
    enabled: bool = True
    lookback_bars: int = 60
    high_correlation: float = 0.6
    correlated_size_factor: float = 0.5
    vol_sizing_enabled: bool = True
    target_daily_vol_pct: float = 2.0
    max_gross_exposure_pct: float = 100.0
    min_size_factor: float = 0.25

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "PortfolioLimitsConfig":
        cfg = config or {}
        mapping = {
            "enabled": "portfolio_intelligence_enabled",
            "lookback_bars": "portfolio_lookback_bars",
            "high_correlation": "portfolio_high_correlation",
            "correlated_size_factor": "portfolio_correlated_size_factor",
            "vol_sizing_enabled": "portfolio_vol_sizing_enabled",
            "target_daily_vol_pct": "portfolio_target_daily_vol_pct",
            "max_gross_exposure_pct": "portfolio_max_gross_exposure_pct",
            "min_size_factor": "portfolio_min_size_factor",
        }
        kwargs = {}
        for field_name, key in mapping.items():
            if cfg.get(key) is not None:
                kwargs[field_name] = cfg[key]
        return cls(**kwargs)


@dataclass
class PortfolioVerdict:
    symbol: str
    requested_notional: float
    adjusted_notional: float
    allowed: bool
    config: PortfolioLimitsConfig
    correlations: Dict[str, float] = field(default_factory=dict)
    factors: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


def daily_returns(prices) -> pd.Series:
    """Close-to-close daily returns from an OHLCV frame or a close series."""
    if isinstance(prices, pd.Series):
        closes = prices.astype(float)
    else:
        frame = prices.copy()
        frame.columns = [str(c).lower() for c in frame.columns]
        if "timestamp" in frame.columns:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"])
            frame = frame.set_index("timestamp")
        if "close" not in frame.columns:
            raise ValueError("Price data needs a 'close' column.")
        closes = frame["close"].astype(float)
    return closes.pct_change().dropna()


def realized_daily_vol(returns: pd.Series) -> float:
    """Standard deviation of daily returns; 0.0 when undefined."""
    if returns is None or len(returns) < 2:
        return 0.0
    vol = float(returns.std())
    return vol if pd.notna(vol) else 0.0


def _candidate_returns(
    symbol: str, price_history: Dict[str, pd.DataFrame], lookback: int
) -> Optional[pd.Series]:
    frame = (price_history or {}).get(symbol)
    if frame is None or len(frame) < 3:
        return None
    try:
        return daily_returns(frame).tail(lookback)
    except (ValueError, KeyError, TypeError):
        return None


def assess_new_position(
    symbol: str,
    requested_notional: float,
    equity: Optional[float],
    open_positions: Dict[str, float],
    price_history: Dict[str, pd.DataFrame],
    config: Optional[PortfolioLimitsConfig] = None,
) -> PortfolioVerdict:
    """Size a proposed NEW position against the whole book.

    `open_positions` maps symbol -> absolute market value; `price_history`
    maps symbol -> OHLCV frame (the candidate's own history included).
    """
    config = config or PortfolioLimitsConfig()
    requested = max(float(requested_notional or 0.0), 0.0)
    verdict = PortfolioVerdict(
        symbol=symbol,
        requested_notional=requested,
        adjusted_notional=requested,
        allowed=True,
        config=config,
    )

    candidate = _candidate_returns(symbol, price_history, config.lookback_bars)
    factor = 1.0

    # --- correlation against every open position -----------------------------
    if candidate is not None and open_positions:
        max_positive = None
        for other_symbol in open_positions:
            if other_symbol == symbol:
                continue
            other = _candidate_returns(other_symbol, price_history, config.lookback_bars)
            if other is None:
                continue
            aligned = pd.concat([candidate, other], axis=1, join="inner").dropna()
            if len(aligned) < _MIN_OVERLAP_BARS:
                continue
            corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
            if pd.isna(corr):
                continue
            verdict.correlations[other_symbol] = corr
            if max_positive is None or corr > max_positive:
                max_positive = corr
        if max_positive is not None and max_positive > config.high_correlation:
            factor *= config.correlated_size_factor
            verdict.factors["correlation"] = config.correlated_size_factor
            worst = max(verdict.correlations, key=verdict.correlations.get)
            verdict.reasons.append(
                f"High correlation with open position {worst} "
                f"({max_positive:+.2f} > {config.high_correlation:g}): "
                f"size scaled by {config.correlated_size_factor:g}."
            )

    # --- inverse-volatility sizing -------------------------------------------
    if config.vol_sizing_enabled and candidate is not None:
        vol_pct = realized_daily_vol(candidate) * 100.0
        if vol_pct > config.target_daily_vol_pct > 0:
            vol_factor = config.target_daily_vol_pct / vol_pct
            factor *= vol_factor
            verdict.factors["volatility"] = vol_factor
            verdict.reasons.append(
                f"Realized daily volatility {vol_pct:.2f}% exceeds the "
                f"{config.target_daily_vol_pct:g}% target: size scaled by {vol_factor:.2f}."
            )

    if candidate is None:
        verdict.reasons.append(
            f"Price history unavailable for {symbol} — correlation and "
            "volatility sizing skipped, size unchanged."
        )

    # Penalties are floored so a trade the agents wanted cannot silently
    # shrink to dust; only the exposure cap below may zero it.
    # D08: the floor must stay within (0, 1] — a configured floor above 1
    # (or non-finite) can only ever ENLARGE a trade, so it is clamped to 1.0
    # (no scaling, no silent size-up).
    min_size_factor = config.min_size_factor
    if not (isinstance(min_size_factor, (int, float))
            and isfinite(min_size_factor) and 0 < min_size_factor <= 1):
        min_size_factor = 1.0
    if factor < min_size_factor:
        factor = min_size_factor
        verdict.factors["floor"] = min_size_factor
    verdict.adjusted_notional = requested * factor

    # --- gross exposure cap ----------------------------------------------------
    cap_pct = float(config.max_gross_exposure_pct or 0)
    equity_value = float(equity) if equity else None
    if cap_pct > 0 and equity_value and equity_value > 0:
        gross = sum(abs(float(v or 0.0)) for v in (open_positions or {}).values())
        headroom = equity_value * cap_pct / 100.0 - gross
        if verdict.adjusted_notional > headroom:
            clipped = max(headroom, 0.0)
            verdict.reasons.append(
                f"Gross exposure ${gross:,.0f} against a "
                f"{cap_pct:g}% cap of ${equity_value:,.0f} equity leaves "
                f"${max(headroom, 0.0):,.0f} headroom: size clipped."
            )
            verdict.adjusted_notional = clipped
    elif cap_pct > 0:
        verdict.reasons.append(
            "Account equity unavailable — gross exposure cap skipped."
        )

    verdict.allowed = verdict.adjusted_notional > 0
    return verdict


def adjust_new_position_notional(
    symbol: str,
    action: str,
    requested_notional: float,
    gather_state: Callable[[], Tuple[Optional[float], Dict[str, float], Dict[str, pd.DataFrame]]],
    config: Optional[PortfolioLimitsConfig] = None,
) -> float:
    """Execution-time hook: portfolio-aware size for NEW long exposure only.

    SELL/HOLD/NEUTRAL (closing or keeping) pass through untouched — the
    layer limits what gets added to the book, never what leaves it. Any
    failure in `gather_state` (broker down, bad data) returns the original
    amount: the portfolio layer must never block a trade by breaking.
    """
    config = config or PortfolioLimitsConfig()
    if not config.enabled or str(action or "").upper() not in ("BUY", "LONG"):
        return requested_notional
    try:
        equity, open_positions, price_history = gather_state()
        verdict = assess_new_position(
            symbol,
            requested_notional,
            equity,
            open_positions or {},
            price_history or {},
            config=config,
        )
        for reason in verdict.reasons:
            print(f"[PORTFOLIO] {symbol}: {reason}")
        if verdict.adjusted_notional != requested_notional:
            print(
                f"[PORTFOLIO] {symbol}: requested ${requested_notional:,.0f} -> "
                f"adjusted ${verdict.adjusted_notional:,.0f}"
            )
        return verdict.adjusted_notional
    except Exception as exc:
        print(f"[PORTFOLIO] Sizing skipped for {symbol}: {exc}")
        return requested_notional


def gather_portfolio_state_via_alpaca(
    symbol: str,
    lookback_days: int = 120,
) -> Tuple[Optional[float], Dict[str, float], Dict[str, pd.DataFrame]]:
    """Collect (equity, open positions, price history) from Alpaca.

    Imported lazily so this package stays importable without the dataflows
    stack; the caller wraps failures via adjust_new_position_notional.
    """
    from datetime import date, timedelta

    from tradingagents.dataflows.alpaca_utils import (
        AlpacaUtils,
        get_alpaca_trading_client,
    )

    client = get_alpaca_trading_client()
    account = client.get_account()
    try:
        equity = float(account.equity)
    except (TypeError, ValueError):
        equity = None

    open_positions: Dict[str, float] = {}
    for pos in client.get_all_positions():
        try:
            open_positions[str(pos.symbol).upper()] = abs(float(pos.market_value))
        except (TypeError, ValueError):
            continue

    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    price_history: Dict[str, pd.DataFrame] = {}
    for wanted in {symbol.upper().replace("/", ""), *open_positions}:
        try:
            price_history[wanted] = AlpacaUtils.get_stock_data(wanted, start, None)
        except Exception:
            continue
    # The candidate may have been requested with its display symbol.
    key = symbol.upper().replace("/", "")
    if key in price_history and symbol not in price_history:
        price_history[symbol] = price_history[key]
    return equity, open_positions, price_history


__all__ = [
    "PortfolioLimitsConfig",
    "PortfolioVerdict",
    "adjust_new_position_notional",
    "assess_new_position",
    "daily_returns",
    "gather_portfolio_state_via_alpaca",
    "realized_daily_vol",
]
