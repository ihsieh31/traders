"""Pure-math performance metrics for backtest evaluation.

All functions operate on plain sequences / pandas objects and never touch
the network, the broker, or an LLM, so every number shown in the UI is
reproducible in a unit test. Metric names follow the TradingAgents paper
(arXiv:2412.20138): cumulative return, annualized return, Sharpe ratio,
and maximum drawdown, plus win rate over closed trades.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import pandas as pd

TRADING_DAYS_PER_YEAR = 252
CRYPTO_DAYS_PER_YEAR = 365


def _as_series(equity_curve) -> pd.Series:
    curve = (equity_curve.astype(float) if isinstance(equity_curve, pd.Series)
             else pd.Series(list(equity_curve), dtype=float))
    if not curve.map(math.isfinite).all() or (curve < 0).any():
        raise ValueError("Equity curve must contain finite, non-negative values.")
    return curve


def cumulative_return(equity_curve) -> Optional[float]:
    """Total return over the whole curve, e.g. 0.25 for +25%."""
    curve = _as_series(equity_curve)
    if len(curve) < 2 or curve.iloc[0] <= 0:
        return None
    return float(curve.iloc[-1] / curve.iloc[0] - 1.0)


def annualized_return(equity_curve, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> Optional[float]:
    """Geometric annualized return assuming one curve point per period."""
    curve = _as_series(equity_curve)
    total = cumulative_return(curve)
    if total is None:
        return None
    periods = len(curve) - 1
    growth = 1.0 + total
    if growth <= 0:
        return -1.0
    return float(growth ** (periods_per_year / periods) - 1.0)


def sharpe_ratio(
    equity_curve,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """Annualized Sharpe ratio from per-period equity values.

    `risk_free_rate` is annual; it is converted to a per-period rate.
    Returns None when the curve is too short or volatility is zero,
    rather than fabricating a number.
    """
    curve = _as_series(equity_curve)
    if len(curve) < 3:
        return None
    returns = curve.pct_change().dropna()
    if len(returns) < 2:
        return None
    per_period_rf = (1.0 + risk_free_rate) ** (1.0 / periods_per_year) - 1.0
    excess = returns - per_period_rf
    std = float(excess.std(ddof=1))
    if not math.isfinite(std) or std == 0.0:
        return None
    return float(excess.mean() / std * math.sqrt(periods_per_year))


def max_drawdown(equity_curve) -> Optional[float]:
    """Largest peak-to-trough decline as a positive fraction (0.2 = -20%)."""
    curve = _as_series(equity_curve)
    if len(curve) < 2:
        return None
    running_peak = curve.cummax()
    drawdowns = 1.0 - curve / running_peak
    return float(drawdowns.max())


def win_rate(trade_pnls: Sequence[float]) -> Optional[float]:
    """Fraction of closed trades with positive net PnL; None when no trades."""
    pnls = [float(p) for p in trade_pnls]
    if not all(math.isfinite(p) for p in pnls):
        raise ValueError("Trade P&L must contain finite values.")
    if not pnls:
        return None
    return sum(1 for p in pnls if p > 0) / len(pnls)


def summarize_performance(
    equity_curve,
    trade_pnls: Sequence[float] = (),
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> dict:
    """All headline metrics in one dict (values may be None when undefined)."""
    curve = _as_series(equity_curve)
    return {
        "cumulative_return": cumulative_return(curve),
        "annualized_return": annualized_return(curve, periods_per_year),
        "sharpe_ratio": sharpe_ratio(curve, risk_free_rate, periods_per_year),
        "max_drawdown": max_drawdown(curve),
        "win_rate": win_rate(trade_pnls),
        "num_trades": len(list(trade_pnls)),
        "num_periods": max(len(curve) - 1, 0),
        "final_equity": float(curve.iloc[-1]) if len(curve) else None,
    }
