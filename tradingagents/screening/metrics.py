"""Phase C deterministic eligibility, factors and Top40 ranking.

Pure functions over one scan's data: no LLM, no broker mutation, no
network. Every constant is a screening-configuration key validated once
at the pipeline boundary, so the whole computation is reproducible from
the recorded inputs and the cache fingerprint.

Data-quality contract (fail-closed per symbol, never forward-filled):
- Bars are daily, sorted by session date, unique per session, with
  price>0, volume>=0 and no NaN/Inf.
- Any bar dated after ``as_of`` (e.g. today's partially formed session)
  is dropped, NOT used; the remaining last bar must belong to ``as_of``.
- The required window is the last ``required_bars`` (61) sessions ending
  at ``as_of``; a missing session inside the window excludes the symbol.
- One explicit adjustment policy per scan (config ``screening_bar_adjustment``);
  a symbol whose quarantine ledger reports an unresolved split is excluded
  before ranking, so adjusted and raw prices are never mixed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from tradingagents.dataflows.market_calendar import session_dates_ending_at
from tradingagents.screening.sessions import most_recent_completed_session

FORMULA_VERSION = "phase-c-top40-1"

FACTOR_KEYS = ("adv20", "r5", "r20", "r60", "vol20", "volume_ratio", "trend")

# score = 100*(0.20*p_adv20 + 0.25*p_r20 + 0.25*p_r60 + 0.15*(1-p_vol20)
#              + 0.15*p_volume_ratio)
SCORE_WEIGHTS = {
    "adv20": 0.20,
    "r20": 0.25,
    "r60": 0.25,
    "vol20_inverse": 0.15,
    "volume_ratio": 0.15,
}


@dataclass(frozen=True)
class EligibilityThresholds:
    min_price: float = 5.0
    min_adv20_usd: float = 20_000_000.0
    required_bars: int = 61
    vol_window: int = 20
    vol_mean_window: int = 20
    ratio_recent_window: int = 5

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "EligibilityThresholds":
        cfg = config or {}
        return cls(
            min_price=float(cfg.get("screening_min_price", cls.min_price)),
            min_adv20_usd=float(cfg.get("screening_min_adv20_usd", cls.min_adv20_usd)),
            required_bars=int(cfg.get("screening_required_bars", cls.required_bars)),
        )


@dataclass
class SymbolFeatures:
    """One eligible symbol's compact factor row (units documented)."""

    symbol: str
    price: float          # USD close of the as_of session
    adv20: float          # USD, mean(close*volume) over the last 20 sessions
    r5: float             # 5-session simple return (fraction)
    r20: float            # 20-session simple return (fraction)
    r60: float            # 60-session simple return (fraction)
    vol20: float          # annualized sample-std of last 20 daily returns (fraction)
    volume_ratio: float   # mean(volume,last5)/mean(volume,last20)
    trend: float          # close/mean(close,last20) - 1 (fraction)
    score: Optional[float] = None
    sector: Optional[str] = None

    def factor_row(self) -> Dict[str, float]:
        return {
            "price": self.price,
            "adv20": self.adv20,
            "r5": self.r5,
            "r20": self.r20,
            "r60": self.r60,
            "vol20": self.vol20,
            "volume_ratio": self.volume_ratio,
            "trend": self.trend,
        }

    def to_cache_dict(self) -> dict:
        payload = {"symbol": self.symbol, **self.factor_row()}
        if self.score is not None:
            payload["score"] = self.score
        if self.sector is not None:
            payload["sector"] = self.sector
        return payload


@dataclass
class ScanStats:
    """Exclusion bookkeeping for one scan (no evidence platform, just counts)."""

    universe_total: int = 0
    excluded: Dict[str, int] = field(default_factory=dict)
    eligible: int = 0

    def record(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1


def resolve_as_of(config: Optional[dict] = None, now=None) -> date:
    """Most recent completed US regular session (bars data date)."""
    override = (config or {}).get("screening_as_of_override")
    if override:
        return date.fromisoformat(str(override))
    return most_recent_completed_session(now)


def bars_for_symbol(bars_df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Extract one symbol's rows from a multi-symbol batch response."""
    frame = bars_df
    if "symbol" in frame.columns:
        frame = frame[frame["symbol"] == symbol]
    return frame


def validate_and_clean_bars(
    symbol: str,
    frame: pd.DataFrame,
    *,
    as_of: date,
    thresholds: EligibilityThresholds,
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return (clean_window_df, None) or (None, exclusion_reason).

    The clean frame holds exactly the ``required_bars`` sessions ending at
    ``as_of`` with timestamp/close/volume columns, one row per session.
    """
    required = thresholds.required_bars
    if frame is None or len(frame) == 0:
        return None, "missing_bars"

    if "timestamp" not in frame.columns or "close" not in frame.columns:
        return None, "malformed_bars"

    working = frame.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=True, errors="coerce")
    working = working[working["timestamp"].notna()]
    if working.empty:
        return None, "malformed_bars"

    # Session dates in ET (daily bars are date-anchored); drop anything
    # after as_of — an unclosed same-day bar is never used.
    working["session"] = working["timestamp"].dt.tz_convert("US/Eastern").dt.date
    working = working[working["session"] <= as_of]
    if working.empty:
        return None, "stale_last_bar"

    working = working.sort_values("session", kind="mergesort")
    sessions = list(working["session"])
    if len(set(sessions)) != len(sessions):
        return None, "duplicate_session"
    if sessions[-1] != as_of:
        return None, "stale_last_bar"
    if len(sessions) < required:
        return None, "insufficient_bars"

    window = working.tail(required)
    numeric_cols = [c for c in ("open", "high", "low", "close", "volume") if c in window.columns]
    for column in numeric_cols:
        values = pd.to_numeric(window[column], errors="coerce")
        if values.isna().any():
            return None, "bad_values"
        if not values.map(math.isfinite).all():
            return None, "bad_values"
    if "close" not in window.columns or "volume" not in window.columns:
        return None, "malformed_bars"
    if (window["close"] <= 0).any():
        return None, "bad_values"
    if (window["volume"] < 0).any():
        return None, "bad_values"

    # The required window must contain every trading session ending at
    # as_of — a missing day is a hole, not something to forward-fill.
    expected = session_dates_ending_at(as_of, required)
    actual = list(window["session"])
    if actual != expected:
        return None, "missing_session"

    return window[["timestamp", "close", "volume"]].reset_index(drop=True), None


def compute_features(
    symbol: str,
    window: pd.DataFrame,
    *,
    thresholds: EligibilityThresholds,
) -> Tuple[Optional[SymbolFeatures], Optional[str]]:
    """Eligibility gate + factor computation from a validated bar window."""
    closes = [float(v) for v in window["close"]]
    volumes = [float(v) for v in window["volume"]]
    t = len(closes) - 1

    price = closes[t]
    if price < thresholds.min_price:
        return None, "below_min_price"
    if price == thresholds.min_price:
        pass  # exactly at the threshold qualifies

    vol_mean_window = thresholds.vol_mean_window
    adv20 = sum(c * v for c, v in zip(closes[-vol_mean_window:], volumes[-vol_mean_window:])) / vol_mean_window
    if adv20 < thresholds.min_adv20_usd:
        return None, "below_min_adv20"

    def simple_return(lag: int) -> float:
        base = closes[t - lag]
        if base <= 0:
            raise ValueError("non-positive base close")
        return closes[t] / base - 1.0

    try:
        r5 = simple_return(5)
        r20 = simple_return(20)
        r60 = simple_return(60)
    except (ValueError, IndexError):
        return None, "insufficient_bars"

    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(t - 19, t + 1)]
    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
    vol20 = math.sqrt(variance) * math.sqrt(252.0)

    recent_volume = sum(volumes[-thresholds.ratio_recent_window:]) / thresholds.ratio_recent_window
    baseline_volume = sum(volumes[-vol_mean_window:]) / vol_mean_window
    if baseline_volume <= 0:
        return None, "zero_volume_baseline"
    volume_ratio = recent_volume / baseline_volume

    mean_close20 = sum(closes[-vol_mean_window:]) / vol_mean_window
    trend = closes[t] / mean_close20 - 1.0

    return (
        SymbolFeatures(
            symbol=symbol,
            price=price,
            adv20=adv20,
            r5=r5,
            r20=r20,
            r60=r60,
            vol20=vol20,
            volume_ratio=volume_ratio,
            trend=trend,
        ),
        None,
    )


def ascending_percentiles(values: Sequence[float]) -> List[float]:
    """Ascending percentile p=(average_rank-1)/(n-1); ties share the
    average rank; n==1 maps to 0.5. Deterministic under input shuffles."""
    n = len(values)
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0 + 1.0  # 1-based average rank
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return [(rank - 1.0) / (n - 1) for rank in ranks]


def score_features(features: List[SymbolFeatures]) -> List[SymbolFeatures]:
    """Assign deterministic scores across the full eligible universe."""
    if not features:
        return features
    n = len(features)
    percentile_columns: Dict[str, List[float]] = {}
    for key in ("adv20", "r20", "r60", "vol20", "volume_ratio"):
        values = [getattr(f, key) for f in features]
        percentile_columns[key] = (
            [0.5] * n if n == 1 else ascending_percentiles(values)
        )

    scored: List[SymbolFeatures] = []
    for index, item in enumerate(features):
        score = 100.0 * (
            SCORE_WEIGHTS["adv20"] * percentile_columns["adv20"][index]
            + SCORE_WEIGHTS["r20"] * percentile_columns["r20"][index]
            + SCORE_WEIGHTS["r60"] * percentile_columns["r60"][index]
            + SCORE_WEIGHTS["vol20_inverse"] * (1.0 - percentile_columns["vol20"][index])
            + SCORE_WEIGHTS["volume_ratio"] * percentile_columns["volume_ratio"][index]
        )
        scored.append(
            SymbolFeatures(
                symbol=item.symbol,
                **item.factor_row(),
                score=score,
                sector=item.sector,
            )
        )
    scored.sort(key=lambda f: (-f.score, f.symbol))
    return scored


def select_top_k(scored: List[SymbolFeatures], top_k: int) -> List[SymbolFeatures]:
    return scored[:top_k]


def scan_universe(
    universe: List[dict],
    bars_by_symbol: Dict[str, pd.DataFrame],
    *,
    as_of: date,
    thresholds: EligibilityThresholds,
    sector_mapping: Optional[Dict[str, str]] = None,
    quarantine_checker=None,
) -> Tuple[List[SymbolFeatures], ScanStats]:
    """Run eligibility + factors over the whole universe.

    ``quarantine_checker`` (P2 corporate-action gate) excludes quarantined
    symbols before ranking; an unavailable checker is a caller error —
    the pipeline refuses to scan without it (fail-closed).
    """
    stats = ScanStats(universe_total=len(universe))
    mapping = sector_mapping or {}
    eligible: List[SymbolFeatures] = []

    for entry in universe:
        symbol = entry["symbol"]
        if quarantine_checker is not None:
            reason = quarantine_checker(symbol)
            if reason:
                stats.record("quarantined")
                continue
        window, exclusion = validate_and_clean_bars(
            symbol, bars_by_symbol.get(symbol), as_of=as_of, thresholds=thresholds
        )
        if exclusion:
            stats.record(exclusion)
            continue
        features, factor_exclusion = compute_features(symbol, window, thresholds=thresholds)
        if factor_exclusion:
            stats.record(factor_exclusion)
            continue
        features.sector = mapping.get(symbol)
        eligible.append(features)

    stats.eligible = len(eligible)
    scored = score_features(eligible)
    return scored, stats


def fetch_daily_bars_batch(
    symbols: List[str],
    *,
    as_of: date,
    adjustment: str = "split",
    batch_size: int = 100,
    lookback_calendar_days: int = 130,
) -> Dict[str, pd.DataFrame]:
    """Fetch daily bars for all symbols in bounded chunks (default 100).

    Uses the existing Alpaca stock data client. Any transport-level
    failure raises so the pipeline can stop the run — per-symbol data
    problems surface later as exclusions, not transport errors.
    """
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from tradingagents.dataflows.alpaca_utils import get_alpaca_stock_client

    client: StockHistoricalDataClient = get_alpaca_stock_client()
    start = pd.Timestamp(as_of - timedelta(days=lookback_calendar_days), tz="UTC")
    end = pd.Timestamp(as_of, tz="UTC") + pd.Timedelta(days=1)
    adjustment_map = {
        "raw": Adjustment.RAW,
        "split": Adjustment.SPLIT,
        "dividend": Adjustment.DIVIDEND,
        "all": Adjustment.ALL,
    }
    adjustment_value = adjustment_map.get(str(adjustment or "split").lower())
    if adjustment_value is None:
        raise ValueError(f"unsupported screening_bar_adjustment: {adjustment!r}")

    effective_batch = max(1, min(int(batch_size or 100), 100))
    frames: Dict[str, pd.DataFrame] = {}
    for chunk_start in range(0, len(symbols), effective_batch):
        chunk = symbols[chunk_start : chunk_start + effective_batch]
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Day,
            start=start.to_pydatetime(),
            end=end.to_pydatetime(),
            adjustment=adjustment_value,
            feed=DataFeed.IEX,
        )
        response = client.get_stock_bars(request)
        batch_df = response.df.reset_index()
        for symbol in chunk:
            frames[symbol] = bars_for_symbol(batch_df, symbol)
    return frames
