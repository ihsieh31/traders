"""Deterministic position sizing: fractional Kelly, ATR stops, exposure caps.

Everything in this module is pure math over explicit inputs — no broker or
LLM calls — so the sizing behavior is fully unit-testable and independent of
model output quality. The LLM decides direction; this layer decides size.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

# No empirical calibration is bundled. Kelly is opt-in and requires an
# explicitly supplied, versioned estimate from out-of-sample observations.
DEFAULT_CONFIDENCE_EDGE = {}

# Assumed stop distance as a fraction of price when no volatility data exists.
FALLBACK_STOP_PCT = 0.05


def compute_atr(bars, period: int = 14) -> Optional[float]:
    """Wilder-smoothed Average True Range from an OHLC DataFrame.

    Returns None whenever the data cannot support a meaningful ATR
    (missing columns, too few rows, NaNs, or a non-positive result) so
    callers can fall back to a conservative default instead of crashing.
    """
    try:
        if bars is None or len(bars) < period + 1:
            return None
        columns = {str(c).lower(): c for c in bars.columns}
        high = bars[columns["high"]].astype(float).reset_index(drop=True)
        low = bars[columns["low"]].astype(float).reset_index(drop=True)
        close = bars[columns["close"]].astype(float).reset_index(drop=True)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    true_range = true_range.iloc[1:]  # first row has no previous close

    if len(true_range) < period or true_range.isna().any():
        return None

    atr = float(true_range.iloc[:period].mean())
    for value in true_range.iloc[period:]:
        atr = (atr * (period - 1) + float(value)) / period

    if not math.isfinite(atr) or atr <= 0:
        return None
    return atr


def kelly_position_fraction(
    win_rate: float, win_loss_ratio: float, kelly_fraction: float = 0.5
) -> float:
    """Fractional Kelly allocation, clamped to [0, inf).

    Kelly % = W - (1 - W) / R, scaled down by `kelly_fraction`. Any invalid
    or edge-free input yields 0.0 (do not allocate) rather than an error.
    """
    try:
        win_rate = float(win_rate)
        win_loss_ratio = float(win_loss_ratio)
        kelly_fraction = float(kelly_fraction)
    except (TypeError, ValueError):
        return 0.0
    if not 0.0 < win_rate < 1.0 or win_loss_ratio <= 0.0 or kelly_fraction <= 0.0:
        return 0.0
    edge = win_rate - (1.0 - win_rate) / win_loss_ratio
    return max(0.0, edge * kelly_fraction)


@dataclass(frozen=True)
class RiskParameters:
    """Tunable, deterministic risk limits. Defaults are intentionally strict."""

    risk_per_trade_pct: float = 0.01  # fraction of equity risked per trade
    kelly_enabled: bool = False
    kelly_fraction: float = 0.5  # half-Kelly
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0
    max_position_pct: float = 0.20  # single-position notional / equity
    max_total_exposure_pct: float = 0.80  # gross portfolio exposure / equity
    min_notional: float = 10.0
    confidence_edge: dict = field(
        default_factory=lambda: dict(DEFAULT_CONFIDENCE_EDGE)
    )

    @classmethod
    def from_dict(cls, overrides: Optional[dict]) -> "RiskParameters":
        """Build parameters from a config dict, ignoring unknown keys."""
        if not overrides:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in overrides.items() if k in known})


@dataclass(frozen=True)
class SizingDecision:
    approved: bool
    notional: float
    stop_loss_price: Optional[float]
    risk_amount: float
    caps_applied: list
    reason: str

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "notional": self.notional,
            "stop_loss_price": self.stop_loss_price,
            "risk_amount": self.risk_amount,
            "caps_applied": list(self.caps_applied),
            "reason": self.reason,
        }


def _rejection(reason: str, notes: Optional[list] = None) -> SizingDecision:
    return SizingDecision(
        approved=False,
        notional=0.0,
        stop_loss_price=None,
        risk_amount=0.0,
        caps_applied=list(notes or []),
        reason=reason,
    )


class PositionSizer:
    """Deterministic sizing engine applied after the LLM's direction call."""

    def __init__(self, params: Optional[RiskParameters] = None):
        self.params = params or RiskParameters()

    def size_position(
        self,
        *,
        equity: float,
        price: float,
        atr: Optional[float],
        confidence: str,
        requested_notional: float,
        current_gross_exposure: float = 0.0,
        side: str = "buy",
        stop_loss_price: Optional[float] = None,
    ) -> SizingDecision:
        params = self.params
        try:
            equity = float(equity)
            price = float(price)
            requested_notional = float(requested_notional)
            current_gross_exposure = max(0.0, float(current_gross_exposure or 0.0))
        except (TypeError, ValueError):
            return _rejection("Invalid numeric inputs for position sizing.")

        if not math.isfinite(equity) or equity <= 0.0:
            return _rejection(f"Invalid account equity: {equity}.")
        if not math.isfinite(price) or price <= 0.0:
            return _rejection(f"Invalid price: {price}.")
        if not math.isfinite(requested_notional) or requested_notional <= 0.0:
            return _rejection(f"Invalid requested notional: {requested_notional}.")

        notes = []
        if stop_loss_price is not None:
            stop = float(stop_loss_price)
            stop_distance = stop - price if str(side).lower() in ("sell", "short") else price - stop
            if not math.isfinite(stop) or stop <= 0 or stop_distance <= 0:
                return _rejection("Invalid executable stop-loss price.")
        elif atr is not None and math.isfinite(atr) and atr > 0.0:
            stop_distance = float(atr) * params.atr_stop_multiplier
        else:
            stop_distance = price * FALLBACK_STOP_PCT
            notes.append("atr_unavailable_default_stop")

        exposure_room = equity * params.max_total_exposure_pct - current_gross_exposure
        if exposure_room < params.min_notional:
            return _rejection(
                "Portfolio exposure limit reached: "
                f"gross exposure {current_gross_exposure:.2f} leaves only "
                f"{max(exposure_room, 0.0):.2f} of the "
                f"{params.max_total_exposure_pct:.0%} cap.",
                notes,
            )

        risk_budget = equity * params.risk_per_trade_pct
        risk_notional = (risk_budget / stop_distance) * price

        kelly_notional = float("inf")
        if params.kelly_enabled:
            edge = params.confidence_edge.get(str(confidence).strip().lower())
            if not edge or len(edge) != 2:
                return _rejection("Kelly requires an explicit calibrated edge for this confidence.")
            kelly_f = kelly_position_fraction(*edge, params.kelly_fraction)
            if not math.isfinite(kelly_f) or kelly_f <= 0:
                return _rejection("No positive calibrated Kelly allocation.")
            kelly_notional = equity * kelly_f

        caps = {
            "requested_notional": requested_notional,
            "risk_per_trade": risk_notional,
            "kelly": kelly_notional,
            "max_position_pct": equity * params.max_position_pct,
            "max_total_exposure": exposure_room,
        }
        notional = min(caps.values())
        binding = [
            name
            for name, value in caps.items()
            if math.isclose(value, notional, rel_tol=1e-9, abs_tol=1e-9)
        ]

        if notional < params.min_notional:
            return _rejection(
                f"Sized notional {notional:.2f} is below the minimum order "
                f"size {params.min_notional:.2f} (binding cap: {binding[0]}).",
                notes,
            )

        notional = round(notional, 2)
        quantity = notional / price
        risk_amount = round(quantity * stop_distance, 2)
        if str(side).strip().lower() in ("sell", "short"):
            stop_loss_price = round(price + stop_distance, 6)
        else:
            stop_loss_price = round(price - stop_distance, 6)

        return SizingDecision(
            approved=True,
            notional=notional,
            stop_loss_price=stop_loss_price,
            risk_amount=risk_amount,
            caps_applied=binding + notes,
            reason=f"Sized {notional:.2f} bound by {', '.join(binding)}.",
        )
