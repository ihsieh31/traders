"""Deterministic market-regime detection.

A cheap, fit-free regime filter over three observable dimensions:

- **Volatility**: realized vol (20-bar) ranked against its own trailing
  history (252 bars), with an absolute annualized floor so persistently
  wild assets — whose own percentile history looks unremarkable — still
  read as turbulent.
- **Trend**: price vs its SMA plus the SMA's recent slope (up/down/sideways).
- **Liquidity**: recent average share volume vs a longer baseline
  (thinning/normal/surging).

Threshold rules were chosen over an HMM deliberately: practitioners get
most of the regime-filter value from the realized-vol percentile plus a
trend filter, with no fitting, no initialization sensitivity, and no new
dependency. The output is used as a *filter*, not a signal generator —
hostile regimes shrink position sizes (multiplicative factors with a
floor), they never flip the agents' decisions. Missing data reads as
unknown with multiplier 1.0: absence of evidence never punishes.

Two integration points, both failure-isolated and config-gated:
- ``regime_report_block`` appends a deterministic markdown section to the
  market analyst's report, which every downstream agent already reads.
- ``regime_risk_multiplier`` scales the trade amount at execution time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import pandas as pd

_ANNUALIZATION = math.sqrt(252)


@dataclass
class RegimeConfig:
    enabled: bool = True
    vol_window: int = 20
    vol_percentile_window: int = 252
    calm_percentile: float = 40.0
    turbulent_percentile: float = 75.0
    turbulent_abs_annual_vol_pct: float = 40.0
    trend_window: int = 50
    trend_slope_bars: int = 5
    liquidity_window: int = 20
    liquidity_baseline_window: int = 60
    thin_liquidity_ratio: float = 0.7
    surging_liquidity_ratio: float = 1.5
    turbulent_size_factor: float = 0.5
    downtrend_size_factor: float = 0.75
    thin_liquidity_size_factor: float = 0.85
    min_risk_multiplier: float = 0.25

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "RegimeConfig":
        cfg = config or {}
        mapping = {
            "enabled": "regime_detection_enabled",
            "vol_window": "regime_vol_window",
            "turbulent_percentile": "regime_turbulent_percentile",
            "turbulent_abs_annual_vol_pct": "regime_turbulent_abs_annual_vol_pct",
            "trend_window": "regime_trend_window",
            "thin_liquidity_ratio": "regime_thin_liquidity_ratio",
            "turbulent_size_factor": "regime_turbulent_size_factor",
            "downtrend_size_factor": "regime_downtrend_size_factor",
            "thin_liquidity_size_factor": "regime_thin_liquidity_size_factor",
            "min_risk_multiplier": "regime_min_risk_multiplier",
        }
        kwargs = {}
        for field_name, key in mapping.items():
            if cfg.get(key) is not None:
                kwargs[field_name] = cfg[key]
        return cls(**kwargs)


@dataclass
class RegimeAssessment:
    symbol: str
    volatility_state: str  # calm | normal | turbulent | unknown
    trend_state: str  # up | down | sideways | unknown
    liquidity_state: str  # normal | thinning | surging | unknown
    label: str  # favorable | mixed | hostile | unknown
    risk_multiplier: float
    metrics: Dict[str, float] = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            "## Market Regime (deterministic indicator)",
            f"- **Regime**: {self.label}"
            + (f" — position sizing scaled to {self.risk_multiplier:.0%}" if self.risk_multiplier < 1.0 else ""),
            f"- **Volatility**: {self.volatility_state}"
            + (
                f" (realized {self.metrics['annualized_vol_pct']:.1f}% annualized, "
                f"p{self.metrics['vol_percentile']:.0f} of its own history)"
                if "annualized_vol_pct" in self.metrics
                else ""
            ),
            f"- **Trend**: {self.trend_state}"
            + (
                f" (price {self.metrics['price_vs_sma_pct']:+.1f}% vs SMA{int(self.metrics.get('trend_window', 0))})"
                if "price_vs_sma_pct" in self.metrics
                else ""
            ),
            f"- **Liquidity**: {self.liquidity_state}"
            + (
                f" (recent dollar volume {self.metrics['liquidity_ratio']:.2f}x its baseline)"
                if "liquidity_ratio" in self.metrics
                else ""
            ),
        ]
        if self.notes:
            lines.extend(f"- {note}" for note in self.notes)
        lines.append(
            "- This block is computed deterministically from price/volume history "
            "— treat it as context for risk posture, not as a trade signal."
        )
        return "\n".join(lines)


def _normalize(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        frame = frame.set_index("timestamp")
    if "close" not in frame.columns:
        raise ValueError("Price data needs a 'close' column.")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    return frame.sort_index()


def classify_regime(
    prices: pd.DataFrame,
    config: Optional[RegimeConfig] = None,
    symbol: str = "",
) -> RegimeAssessment:
    """Classify the current regime from an OHLCV history (newest last)."""
    config = config or RegimeConfig()
    unknown = RegimeAssessment(
        symbol=symbol,
        volatility_state="unknown",
        trend_state="unknown",
        liquidity_state="unknown",
        label="unknown",
        risk_multiplier=1.0,
        notes=["Insufficient price history for regime classification."],
    )
    try:
        frame = _normalize(prices)
    except (ValueError, AttributeError, TypeError):
        return unknown

    closes = frame["close"].astype(float)
    returns = closes.pct_change().dropna()
    if len(returns) < config.vol_window + config.trend_slope_bars:
        return unknown

    metrics: Dict[str, float] = {}
    notes = []

    # --- volatility ------------------------------------------------------------
    rolling_vol = returns.rolling(config.vol_window).std().dropna()
    current_vol = float(rolling_vol.iloc[-1])
    annualized_pct = current_vol * _ANNUALIZATION * 100.0
    history = rolling_vol.tail(config.vol_percentile_window)
    percentile = float((history <= current_vol).mean() * 100.0)
    metrics["annualized_vol_pct"] = annualized_pct
    metrics["vol_percentile"] = percentile

    if (
        percentile >= config.turbulent_percentile
        or annualized_pct >= config.turbulent_abs_annual_vol_pct
    ):
        vol_state = "turbulent"
        if annualized_pct >= config.turbulent_abs_annual_vol_pct:
            notes.append(
                f"Absolute volatility floor hit: {annualized_pct:.0f}% annualized "
                f"exceeds {config.turbulent_abs_annual_vol_pct:g}%."
            )
    elif percentile <= config.calm_percentile:
        vol_state = "calm"
    else:
        vol_state = "normal"

    # --- trend -------------------------------------------------------------------
    if len(closes) >= config.trend_window + config.trend_slope_bars:
        sma = closes.rolling(config.trend_window).mean().dropna()
        price = float(closes.iloc[-1])
        sma_now = float(sma.iloc[-1])
        sma_then = float(sma.iloc[-1 - config.trend_slope_bars])
        metrics["price_vs_sma_pct"] = (price / sma_now - 1.0) * 100.0
        metrics["trend_window"] = float(config.trend_window)
        if price > sma_now and sma_now > sma_then:
            trend_state = "up"
        elif price < sma_now and sma_now < sma_then:
            trend_state = "down"
        else:
            trend_state = "sideways"
    else:
        trend_state = "unknown"

    # --- liquidity -----------------------------------------------------------------
    # Share volume, not dollar volume: a price collapse would drag dollar
    # volume down by itself and double-count what the trend factor already
    # penalizes. Thinning here means participation drying up.
    volume = frame["volume"].astype(float).dropna()
    needed = config.liquidity_window + config.liquidity_baseline_window
    if float(volume.sum()) > 0 and len(volume) >= needed:
        recent = float(volume.tail(config.liquidity_window).mean())
        # Baseline strictly precedes the recent window, otherwise a slow
        # bleed would drag its own baseline down and hide the thinning.
        baseline = float(volume.iloc[-needed : -config.liquidity_window].mean())
        if baseline > 0:
            ratio = recent / baseline
            metrics["liquidity_ratio"] = ratio
            if ratio < config.thin_liquidity_ratio:
                liquidity_state = "thinning"
            elif ratio > config.surging_liquidity_ratio:
                liquidity_state = "surging"
            else:
                liquidity_state = "normal"
        else:
            liquidity_state = "unknown"
    else:
        liquidity_state = "unknown"

    # --- multiplier and label --------------------------------------------------------
    multiplier = 1.0
    if vol_state == "turbulent":
        multiplier *= config.turbulent_size_factor
    if trend_state == "down":
        multiplier *= config.downtrend_size_factor
    if liquidity_state == "thinning":
        multiplier *= config.thin_liquidity_size_factor
    multiplier = max(multiplier, config.min_risk_multiplier)

    if vol_state == "turbulent" or (trend_state == "down" and liquidity_state == "thinning"):
        label = "hostile"
    elif trend_state == "up" and vol_state in ("calm", "normal") and liquidity_state != "thinning":
        label = "favorable"
    else:
        label = "mixed"

    return RegimeAssessment(
        symbol=symbol,
        volatility_state=vol_state,
        trend_state=trend_state,
        liquidity_state=liquidity_state,
        label=label,
        risk_multiplier=multiplier if label != "favorable" else 1.0,
        metrics=metrics,
        notes=notes,
    )


def _default_price_loader():
    from tradingagents.dataflows.alpaca_utils import AlpacaUtils

    return AlpacaUtils.get_stock_data


def _load_assessment(
    symbol: str,
    price_loader: Optional[Callable] = None,
    config: Optional[RegimeConfig] = None,
    *,
    as_of: str | None = None,
) -> Optional[RegimeAssessment]:
    from datetime import date, timedelta

    from tradingagents.dataflows.interface_utils import analysis_date_mode, parse_analysis_date

    config = config or RegimeConfig()
    if not config.enabled:
        return None
    loader = price_loader or _default_price_loader()
    if as_of is not None:
        # Historical analysis: window is bounded exactly at the as-of date.
        # Invalid/future as_of raises rather than silently falling back to
        # today.
        analysis_date_mode(as_of)
        as_of_date = parse_analysis_date(as_of)
        start = (as_of_date - timedelta(days=550)).isoformat()
        end = as_of_date.isoformat()
    else:
        # Live execution/helper compatibility: bounded at today's Eastern
        # date; never pass end=None (which would pull data up to present).
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("America/New_York")).date()
        end = today.isoformat()
        start = (today - timedelta(days=550)).isoformat()
    prices = loader(symbol, start, end)
    return classify_regime(prices, config=config, symbol=symbol)


def regime_report_block(
    symbol: str,
    price_loader: Optional[Callable] = None,
    config: Optional[RegimeConfig] = None,
    *,
    as_of: str | None = None,
) -> str:
    """Markdown regime block for the market report; '' when unavailable."""
    try:
        assessment = _load_assessment(symbol, price_loader, config, as_of=as_of)
        if assessment is None or assessment.label == "unknown":
            return ""
        return assessment.to_markdown()
    except Exception as exc:
        print(f"[REGIME] Report block skipped for {symbol}: {exc}")
        return ""


def regime_risk_multiplier(
    symbol: str,
    price_loader: Optional[Callable] = None,
    config: Optional[RegimeConfig] = None,
) -> float:
    """Position-size multiplier in [min_risk_multiplier, 1.0]; 1.0 on failure."""
    try:
        assessment = _load_assessment(symbol, price_loader, config)
        if assessment is None:
            return 1.0
        if assessment.risk_multiplier < 1.0:
            print(
                f"[REGIME] {symbol}: {assessment.label} regime "
                f"(vol={assessment.volatility_state}, trend={assessment.trend_state}, "
                f"liquidity={assessment.liquidity_state}) -> "
                f"size x{assessment.risk_multiplier:.2f}"
            )
        return assessment.risk_multiplier
    except Exception as exc:
        print(f"[REGIME] Sizing skipped for {symbol}: {exc}")
        return 1.0
