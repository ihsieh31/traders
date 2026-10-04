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
- A finite positive market cap is required and must be at least the configured
  ``screening_min_market_cap_usd``; missing or unusable metadata is excluded.
"""

from __future__ import annotations

from tradingagents.redaction import sanitize_for_log

import math
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from tradingagents.dataflows.market_calendar import session_dates_ending_at_auth
from tradingagents.screening.sessions import most_recent_completed_session_production
from .quality import identity_issues, quality_record, reason_category, bar_evidence

FORMULA_VERSION = "exclusion-20261004-1"
# ``trend > 0`` means "close above its 20-session mean". Decimal prices are
# not exact in binary, so a close EQUAL to that mean computes as +/-1e-16 and
# the gate used to pass or fail on rounding noise (research parity check,
# research/out/20261004-screen-diagnosis). Cent-priced data cannot produce a
# real |trend| below ~7e-10 even at $700k a share, so anything within this
# tolerance is "not above the mean".
TREND_EPSILON = 1e-12
# Authoritative consolidated feed for Phase C US-equity screening liquidity.
# Hardcoded SIP: the $20M ADV20 threshold is defined on consolidated volume.
SCREENING_DATA_FEED = "sip"

FACTOR_KEYS = ("adv20", "r5", "r20", "r60", "vol20", "volume_ratio", "trend")
# The longest factor lag (r60) plus the as_of session itself.
MIN_REQUIRED_BARS = 61

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
    min_market_cap_usd: float = 300_000_000.0
    required_bars: int = 61
    vol_window: int = 20
    vol_mean_window: int = 20
    ratio_recent_window: int = 5

    def __post_init__(self) -> None:
        try:
            threshold = float(self.min_market_cap_usd)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "screening_min_market_cap_usd must be positive and finite"
            ) from exc
        if isinstance(self.min_market_cap_usd, bool) or not math.isfinite(threshold) or threshold <= 0:
            raise ValueError("screening_min_market_cap_usd must be positive and finite")
        object.__setattr__(self, "min_market_cap_usd", threshold)
        # A NaN floor compares False against every value and admits everything.
        for name, key in (("min_price", "screening_min_price"),
                          ("min_adv20_usd", "screening_min_adv20_usd")):
            raw = getattr(self, name)
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be finite and non-negative") from exc
            if isinstance(raw, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be finite and non-negative")
            object.__setattr__(self, name, value)
        # r60 needs closes[t-60]; a shorter window wraps to a negative index
        # and silently measures a different return.
        bars = self.required_bars
        if isinstance(bars, bool) or not isinstance(bars, int) or bars < MIN_REQUIRED_BARS:
            raise ValueError(f"screening_required_bars must be an integer >= {MIN_REQUIRED_BARS}")

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "EligibilityThresholds":
        cfg = config or {}
        raw_market_cap_threshold = cfg.get(
            "screening_min_market_cap_usd", cls.min_market_cap_usd
        )
        if isinstance(raw_market_cap_threshold, bool):
            raise ValueError("screening_min_market_cap_usd must be positive and finite")
        raw_bars = cfg.get("screening_required_bars", cls.required_bars)
        if isinstance(raw_bars, float) and raw_bars.is_integer():
            raw_bars = int(raw_bars)
        return cls(
            min_price=cfg.get("screening_min_price", cls.min_price),
            min_adv20_usd=cfg.get("screening_min_adv20_usd", cls.min_adv20_usd),
            min_market_cap_usd=raw_market_cap_threshold,
            required_bars=raw_bars,
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
    positive_score: Optional[float] = None
    negative_score: Optional[float] = None
    candidate_lane: Optional[str] = None
    exclusion_score: Optional[float] = None

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
        if self.positive_score is not None:
            payload["positive_score"] = self.positive_score
        if self.negative_score is not None:
            payload["negative_score"] = self.negative_score
        if self.candidate_lane is not None:
            payload["candidate_lane"] = self.candidate_lane
        if self.exclusion_score is not None:
            payload["exclusion_score"] = self.exclusion_score
        return payload


@dataclass
class ScanStats:
    """Exclusion bookkeeping for one scan (no evidence platform, just counts)."""

    universe_total: int = 0
    excluded: Dict[str, int] = field(default_factory=dict)
    eligible: int = 0
    records: List[dict] = field(default_factory=list)
    # symbol -> its records, extended lazily: a reverse scan of the whole
    # universe per exclusion was quadratic in the universe size.
    _by_symbol: Dict[Any, List[dict]] = field(default_factory=dict, init=False, repr=False, compare=False)
    _indexed: int = field(default=0, init=False, repr=False, compare=False)

    def record(self, reason: str, symbol=None) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1
        if symbol is not None:
            for row in self.records[self._indexed:]:
                self._by_symbol.setdefault(row["symbol"], []).append(row)
            self._indexed = len(self.records)
            for row in reversed(self._by_symbol.get(symbol, ())):
                if row["reason"] is None:
                    row.update(status="excluded", reason=reason, category=reason_category(reason))
                    break


def resolve_as_of(
    config: Optional[dict] = None,
    now=None,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
) -> date:
    """Most recent completed authoritative session (bars data date).

    Raises :class:`CalendarError` when the Alpaca calendar cannot prove the
    session — callers must fail closed (no silent ``as_of`` rollback).
    """
    override = (config or {}).get("screening_as_of_override")
    injected = calendar_rows
    if injected is None and (config or {}).get("calendar_rows") is not None:
        injected = (config or {}).get("calendar_rows")
    client = calendar_client
    if client is None and (config or {}).get("calendar_client") is not None:
        client = (config or {}).get("calendar_client")
    resolved = most_recent_completed_session_production(now, client=client, calendar_rows=injected)
    if override and str(override) != str(resolved):
        raise ValueError("screening_as_of_override must match the authoritative completed session")
    return resolved


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
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
    expected_sessions: Any = None,
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return (clean_window_df, None) or (None, exclusion_reason).

    The clean frame holds exactly the ``required_bars`` authoritative sessions
    ending at ``as_of`` with timestamp/close/volume columns, one row per
    session. The expected window comes from the Alpaca calendar — a missing
    session is never forward-filled, and ``as_of`` is never silently moved
    backward when the final daily bar has not landed.

    ``expected_sessions`` lets a caller validating many symbols against the
    same ``as_of`` supply that window once: either the list of dates or a
    zero-argument callable returning it (called only when a symbol reaches
    the session check, so calendar errors surface exactly as before).
    """
    required = thresholds.required_bars
    if frame is None:
        return None, "missing_bars"
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        return None, "malformed_bars"
    if frame.empty:
        return None, "missing_bars"

    if any(c not in frame.columns for c in ("timestamp", "open", "high", "low", "close", "volume")):
        return None, "malformed_bars"

    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if timestamps.isna().any():
        return None, "malformed_bars"

    # Session dates in ET (daily bars are date-anchored); drop anything
    # after as_of — an unclosed same-day bar is never used.
    days = timestamps.dt.tz_convert("US/Eastern").dt.tz_localize(None).to_numpy().astype("datetime64[D]")
    kept = np.flatnonzero(days <= np.datetime64(as_of, "D"))
    if kept.size == 0:
        return None, "stale_last_bar"

    order = kept[np.argsort(days[kept], kind="stable")]
    sessions = days[order]
    if np.unique(sessions).size != sessions.size:
        return None, "duplicate_session"
    if sessions[-1] != np.datetime64(as_of, "D"):
        return None, "stale_last_bar"
    if sessions.size < required:
        return None, "insufficient_bars"

    rows = order[-required:]
    columns = {"timestamp": timestamps.iloc[rows].reset_index(drop=True)}
    for column in ("open", "high", "low", "close", "volume"):
        values = np.asarray(pd.to_numeric(frame[column].to_numpy()[rows], errors="coerce"))
        try:
            finite = np.isfinite(values.astype(np.float64))
        except (TypeError, ValueError):
            return None, "bad_values"
        if not finite.all():
            return None, "bad_values"
        columns[column] = values
    o, h, l, c = (columns[k].astype(np.float64) for k in ("open", "high", "low", "close"))
    if (o <= 0).any() or (h <= 0).any() or (l <= 0).any() or (c <= 0).any():
        return None, "bad_values"
    if (columns["volume"].astype(np.float64) < 0).any():
        return None, "bad_values"
    if (l > np.minimum(o, c)).any() or (h < np.maximum(o, c)).any():
        return None, "invalid_ohlc"

    # The required window must contain every authoritative trading session
    # ending at as_of — a missing day is a hole, not something to
    # forward-fill. Calendar failures propagate (fail-closed, no silent
    # as_of rollback).
    if expected_sessions is None:
        expected = session_dates_ending_at_auth(
            as_of, required, client=calendar_client, calendar_rows=calendar_rows
        )
    else:
        expected = expected_sessions() if callable(expected_sessions) else expected_sessions
    if len(expected) != required or not np.array_equal(
        sessions[-required:], np.array(expected, dtype="datetime64[D]")
    ):
        return None, "missing_session"

    return pd.DataFrame(columns), None


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


def score_exclusion_candidates(features, thresholds, stats=None):
    """Report formula: gate first, rank percentiles ONLY among survivors.

    Keep the legacy score for comparison. Returns a separate exclusion_score;
    ties use average ranks, then symbol order. No quota padding or relaxed gates.
    """
    survivors = []
    for item in features:
        reason = None
        if not all(math.isfinite(v) for v in item.factor_row().values()):
            reason = "exclusion_nonfinite"
        elif item.vol20 < 0 or item.vol20 > thresholds.max_vol20:
            reason = "exclusion_vol20"
        elif item.trend <= TREND_EPSILON:
            reason = "exclusion_trend"
        elif item.r60 < thresholds.min_r60:
            reason = "exclusion_r60"
        if reason:
            if stats is not None:
                stats.record(reason, symbol=item.symbol)
        else:
            survivors.append(item)
    if not survivors:
        return []
    percentiles = {key: ascending_percentiles([getattr(f, key) for f in survivors])
                   for key in ("trend", "r60", "vol20")}
    ranked = [replace(item, exclusion_score=100 * (
        .5 * percentiles["trend"][i] + .3 * percentiles["r60"][i]
        + .2 * (1 - percentiles["vol20"][i]))) for i, item in enumerate(survivors)]
    return sorted(ranked, key=lambda f: (-f.exclusion_score, f.symbol))


def select_exclusion_candidates(ranked, *, select_n, sector_plan):
    """Greedy score order under the existing optional sector cap; may return <N."""
    selected, counts = [], {}
    for item in ranked:
        if sector_plan["applied"]:
            count = counts.get(item.sector, 0)
            if count >= sector_plan["max_per_sector"]:
                continue
            counts[item.sector] = count + 1
        selected.append(item)
        if len(selected) == select_n:
            break
    return selected


def select_research_candidates(
    features: List[SymbolFeatures], *, top_k: int, allow_shorts: bool
) -> List[SymbolFeatures]:
    """Select the Top-K research pool, optionally balancing trend directions.

    ``features`` must contain the full eligible universe with the existing
    positive ``score`` assigned by :func:`score_features`. Lane labels are
    candidate-generation audit metadata only; they do not imply a trade side.
    """
    if not allow_shorts:
        return select_top_k(features, top_k)
    if top_k <= 0 or not features:
        return []

    # Duplicate symbols should not consume multiple Top-K slots. Resolve any
    # malformed duplicate deterministically before assigning lane membership.
    ordered = sorted(
        features,
        key=lambda f: (
            f.symbol, f.price, f.adv20, f.r5, f.r20, f.r60,
            f.vol20, f.volume_ratio, f.trend, f.score or 0.0,
        ),
    )
    unique = {}
    for feature in ordered:
        unique.setdefault(feature.symbol, feature)
    features = list(unique.values())

    percentiles = {
        key: ascending_percentiles([getattr(f, key) for f in features])
        for key in ("adv20", "r20", "r60", "vol20", "volume_ratio")
    }
    positive_lane: List[SymbolFeatures] = []
    negative_lane: List[SymbolFeatures] = []
    for i, feature in enumerate(features):
        negative_score = 100.0 * (
            0.20 * percentiles["adv20"][i]
            + 0.25 * (1.0 - percentiles["r20"][i])
            + 0.25 * (1.0 - percentiles["r60"][i])
            + 0.15 * (1.0 - percentiles["vol20"][i])
            + 0.15 * percentiles["volume_ratio"][i]
        )
        lane = None
        research_score = feature.score
        if feature.trend > 0 and (feature.r20 > 0 or feature.r60 > 0):
            lane = "positive_trend"
        elif feature.trend < 0 and (feature.r20 < 0 or feature.r60 < 0):
            lane = "negative_trend"
            research_score = negative_score
        if lane is None:
            continue

        candidate = SymbolFeatures(
            symbol=feature.symbol,
            **feature.factor_row(),
            score=research_score,
            sector=feature.sector,
            positive_score=feature.score,
            negative_score=negative_score,
            candidate_lane=lane,
        )
        lane_rows = positive_lane if lane == "positive_trend" else negative_lane
        lane_rows.append(candidate)

    positive_lane.sort(key=lambda f: (-float(f.score or 0.0), f.symbol))
    negative_lane.sort(key=lambda f: (-float(f.score or 0.0), f.symbol))
    positive_target = (top_k + 1) // 2
    negative_target = top_k // 2
    positive = positive_lane[:positive_target]
    negative = negative_lane[:negative_target]
    selected = [*positive, *negative]

    if len(selected) < top_k:
        if len(positive) < positive_target:
            selected.extend(negative_lane[negative_target:])
        elif len(negative) < negative_target:
            selected.extend(positive_lane[positive_target:])
    # Keep the lane quotas and shortage fill above, then remove the lane
    # grouping order before candidates reach Screening. ``score`` is already
    # the directional research score for each selected candidate.
    return sorted(
        selected[:top_k],
        key=lambda f: (-float(f.score or 0.0), f.symbol),
    )


def scan_universe(
    universe: List[dict],
    bars_by_symbol: Dict[str, pd.DataFrame],
    *,
    as_of: date,
    thresholds: EligibilityThresholds,
    sector_mapping: Optional[Dict[str, str]] = None,
    quarantine_checker=None,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
    adjustment_policy: str = "split",
) -> Tuple[List[SymbolFeatures], ScanStats]:
    """Run eligibility + factors over the whole universe.

    ``quarantine_checker`` (P2 corporate-action gate) excludes quarantined
    symbols before ranking; an unavailable checker is a caller error —
    the pipeline refuses to scan without it (fail-closed). The expected
    61-session window is authoritative (Alpaca calendar); calendar failures
    propagate so the scan stops instead of using a different ``as_of``.
    """
    stats = ScanStats(universe_total=len(universe))
    mapping = sector_mapping or {}
    eligible: List[SymbolFeatures] = []
    window_cache: List[List[date]] = []

    def expected_window() -> List[date]:
        # One calendar lookup per scan instead of one per symbol; still
        # deferred until a symbol needs it, so failures surface unchanged.
        if not window_cache:
            window_cache.append(session_dates_ending_at_auth(
                as_of, thresholds.required_bars, client=calendar_client, calendar_rows=calendar_rows
            ))
        return window_cache[0]

    for entry, identity_reason in zip(universe, identity_issues(universe)):
        record = quality_record(entry)
        record["input_index"] = len(stats.records)
        stats.records.append(record)
        symbol = record["symbol"]
        def reject(reason):
            record.update(status="excluded", reason=reason, category=reason_category(reason))
            stats.record(reason)
        if identity_reason:
            reject(identity_reason)
            continue
        raw_market_cap = entry.get("market_cap")
        try:
            if isinstance(raw_market_cap, bool):
                raise ValueError("boolean is not a market cap")
            market_cap = float(raw_market_cap)
        except (TypeError, ValueError):
            market_cap = float("nan")
        if not math.isfinite(market_cap) or market_cap <= 0:
            reject("missing_market_cap")
            continue
        record["market_cap"] = market_cap
        if not isinstance(entry.get("market_cap_source"), str) or not entry["market_cap_source"].strip():
            reject("missing_market_cap_source")
            continue
        if market_cap < thresholds.min_market_cap_usd:
            reject("below_min_market_cap")
            continue
        if quarantine_checker is not None:
            reason = quarantine_checker(symbol)
            if reason:
                reject("quarantined")
                continue
        frame = bars_by_symbol.get(symbol)
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            if not isinstance(frame.attrs.get("source"), str) or not frame.attrs["source"].strip():
                reject("missing_bar_source")
                continue
            if frame.attrs.get("feed") != SCREENING_DATA_FEED:
                reject("bar_feed_mismatch")
                continue
            if frame.attrs.get("adjustment") != adjustment_policy:
                reject("bar_adjustment_mismatch")
                continue
        window, exclusion = validate_and_clean_bars(
            symbol,
            bars_by_symbol.get(symbol),
            as_of=as_of,
            thresholds=thresholds,
            calendar_client=calendar_client,
            calendar_rows=calendar_rows,
            expected_sessions=expected_window,
        )
        if exclusion:
            reject(exclusion)
            continue
        features, factor_exclusion = compute_features(symbol, window, thresholds=thresholds)
        if factor_exclusion:
            reject(factor_exclusion)
            continue
        features.sector = mapping.get(symbol)
        record["bars"] = bar_evidence(window, frame, as_of=as_of, adjustment=adjustment_policy)
        record["factors"] = features.factor_row()
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
    sleep_fn=None,
) -> Dict[str, pd.DataFrame]:
    """Fetch daily bars for all symbols in bounded chunks (default 100).

    Phase C screening liquidity (ADV20, volume_ratio) is defined on
    consolidated US-market volume, so this path always requests
    ``DataFeed.SIP``. Any transport/entitlement failure raises with SIP
    context so the pipeline stops fail-closed — there is no IEX fallback
    and the $20M threshold is never reinterpreted as an IEX threshold.
    Per-symbol data problems surface later as exclusions, not transport
    errors.
    """
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from tradingagents.dataflows.alpaca_utils import (
        _apply_read_timeout,
        fetch_with_bounded_retry,
        get_alpaca_stock_client,
    )

    client: StockHistoricalDataClient = get_alpaca_stock_client()
    # Bulk 100-symbol/130-day reads are the largest market-data payloads in
    # the system; give this path more headroom than the default 10s bound.
    _apply_read_timeout(client, timeout=(3.05, 20.0))
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
            feed=DataFeed.SIP,
        )
        try:
            response = fetch_with_bounded_retry(
                lambda: client.get_stock_bars(request), sleep=sleep_fn,
            )
        except Exception as exc:
            # Transient hiccups retried bounded above; still no silent IEX
            # retry: surface SIP/entitlement context and stop fail-closed.
            raise RuntimeError(f"SIP consolidated bars unavailable (feed=sip): {sanitize_for_log(str(exc))}") from exc
        batch_df = response.df.reset_index()
        for symbol in chunk:
            frames[symbol] = bars_for_symbol(batch_df, symbol)
            frames[symbol].attrs.update(source="alpaca_stock_bars", feed=SCREENING_DATA_FEED,
                                        adjustment=str(adjustment or "split").lower())
    return frames
