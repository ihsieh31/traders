"""Dense (time, symbol) panel, eligibility gate, and forward returns.

READ research/plan.md SECTIONS 4-5 AND research/process.md S-23/S-35 FIRST.

This module is the floor the formula harness stands on. Three things here
are load-bearing and have each broken a full research round:

1. **Shape is (time, symbol)**, always. A stray transpose makes every factor
   read the wrong axis and produces plausible wrong numbers. See S-10.
2. **Eligibility is the parent population.** The production screen computes
   cross-sectional percentiles over *only* the symbols that pass every
   threshold, not over the whole universe. Getting this wrong produced a
   90/100 discrepancy. See S-12.
3. **Forward returns are next-open in, same-day-out.** ``entry = open[t+1]``,
   ``exit = close[t+N]``. Using ``open[t]`` instead flips the sign of a
   reversal factor's 1-day IC from -0.017 to +0.493 at t = 56. See S-13.

The vectorised eligibility below is a restatement of the production
``validate_and_clean_bars`` + ``compute_features`` pair. That equivalence is
an ASSUMPTION until ``test_harness.py`` asserts it against the production
functions on real data. If you change anything here, run that test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

import numpy as np

from research.src.common import log, require_credentials

#: Production ``EligibilityThresholds.required_bars``. The factor window is
#: exactly this long, so no factor can look further back.
REQUIRED_BARS = 61

#: Production ``min_price``.
MIN_PRICE = 5.0

#: Production ``min_adv20_usd``. The $300M market-cap gate is NOT applied:
#: there is no point-in-time market cap in this dataset, and using today's
#: market cap to filter historical bars is look-ahead bias. This is a
#: deliberate, disclosed difference from production. See plan.md 4.4 item 2.
MIN_ADV20_USD = 20_000_000.0

#: A cross-section thinner than this is noise for both the percentile
#: transform and the rank correlation.
MIN_CROSS_SECTION = 20

FIELDS = ("open", "high", "low", "close", "volume")


@dataclass
class Panel:
    """Dense OHLCV panel. Every array is ``(n_sessions, n_symbols)`` float64.

    Missing bars are ``NaN`` and are never forward-filled.
    """

    sessions: List[date]
    symbols: List[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    calendar_rows: List[dict]

    def __post_init__(self) -> None:
        n_t, n_s = len(self.sessions), len(self.symbols)
        for name in FIELDS:
            arr = getattr(self, name)
            if arr.shape != (n_t, n_s):
                raise ValueError(
                    f"panel.{name} has shape {arr.shape}, expected "
                    f"{(n_t, n_s)} -- (time, symbol). See process.md S-10."
                )
            if arr.dtype != np.float64:
                raise ValueError(
                    f"panel.{name} is {arr.dtype}, expected float64. "
                    "float32 pushes factor error to ~5e-7, above the 1e-9 "
                    "equivalence threshold. See process.md S-11."
                )
        if len(set(self.sessions)) != n_t:
            raise ValueError("panel.sessions contains duplicates")
        if list(self.sessions) != sorted(self.sessions):
            raise ValueError("panel.sessions is not sorted ascending")

    @property
    def shape(self) -> Tuple[int, int]:
        """(n_sessions, n_symbols)."""
        return len(self.sessions), len(self.symbols)

    def symbol_index(self) -> dict:
        return {s: i for i, s in enumerate(self.symbols)}

    def session_index(self) -> dict:
        return {d: i for i, d in enumerate(self.sessions)}

    def describe(self) -> str:
        n_t, n_s = self.shape
        finite = np.isfinite(self.close).sum()
        return (
            f"{n_t} sessions x {n_s} symbols "
            f"({self.sessions[0]} .. {self.sessions[-1]}), "
            f"{finite:,} finite closes "
            f"({finite / max(1, n_t * n_s):.1%} density)"
        )


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def build_panel(
    *,
    start: date,
    end: date,
    symbols_file: Optional[str] = None,
    max_symbols: int = 0,
    buffer_sessions: int = REQUIRED_BARS + 5,
) -> Panel:
    """Scatter the bars cache into a dense panel.

    ``start`` is the first session the *study* cares about. The panel is
    extended backwards by ``buffer_sessions`` authoritative sessions so the
    first ``as_of`` still has a full 61-bar factor window -- without that
    buffer every factor is NaN at the start and the eligibility gate rejects
    the entire cross-section, which looks like "no data" rather than "no
    history".

    The calendar is clipped to exactly the panel window, so the panel's
    sessions ARE the authoritative sessions. Coverage checks therefore cannot
    be comparing against the wrong session set (process.md S-2/S-19).
    """
    from research.src import common

    require_credentials()
    # Fetch wide enough to cover the buffer, then clip to the buffer.
    fetch_start = start - timedelta(days=int(buffer_sessions * 1.6) + 14)
    calendar_rows = common.load_calendar_rows(fetch_start, end)
    all_sessions = sorted(
        date.fromisoformat(str(r["date"])[:10]) for r in calendar_rows
    )
    usable = [d for d in all_sessions if d <= end]
    if start not in usable:
        raise SystemExit(
            f"{start} is not an authoritative session; pick a date from "
            f"data/calendar.json ({usable[0]} .. {usable[-1]})"
        )
    first_usable = usable.index(start)
    lo_index = max(0, first_usable - buffer_sessions)
    sessions = usable[lo_index:]
    if len(sessions) < REQUIRED_BARS + 1:
        raise SystemExit(
            f"only {len(sessions)} sessions available for as_of={start} "
            f"(needed {REQUIRED_BARS + 1}); the bars cache may not reach back "
            f"far enough"
        )
    lo, hi = sessions[0].isoformat(), sessions[-1].isoformat()
    log(
        f"panel window {lo} .. {hi} ({len(sessions)} sessions; "
        f"{first_usable - lo_index} of them are leading history)"
    )

    allowed = None
    if symbols_file:
        allowed = {
            line.strip()
            for line in open(symbols_file, encoding="utf-8")
            if line.strip() and not line.startswith("#")
        }
        log(f"symbol filter: {len(allowed)} symbols from {symbols_file}")

    log("reading bars cache (this takes a couple of minutes for 275 MB)...")
    bars = common.load_all_bars()
    if len(bars) == 0:
        raise SystemExit("no cached bars -- run: python -m research.src.collect_bars")
    bars = bars.rename(columns={"timestamp": "_ts"})
    bars["session"] = bars["_ts"].astype(str).str.slice(0, 10)
    bars = bars[bars["session"].between(lo, hi)]
    if allowed is not None:
        bars = bars[bars["symbol"].isin(allowed)]
    if len(bars) == 0:
        raise SystemExit("bars cache has no rows in the requested window")

    symbols = sorted(bars["symbol"].unique())
    if max_symbols and len(symbols) > max_symbols:
        step = len(symbols) / max_symbols
        symbols = [symbols[int(i * step)] for i in range(max_symbols)]
        bars = bars[bars["symbol"].isin(set(symbols))]
    log(f"panel: {len(symbols)} symbols x {len(sessions)} sessions to scatter")

    # Keyed by ISO STRING, not date: the bars frame holds the first 10 chars
    # of the timestamp, and a date-keyed lookup misses on every row.
    t_ix = {d.isoformat(): i for i, d in enumerate(sessions)}
    s_ix = {s: i for i, s in enumerate(symbols)}
    shape = (len(sessions), len(symbols))
    arrays = {name: np.full(shape, np.nan, dtype=np.float64) for name in FIELDS}

    # ``Series.map`` returns float NaN for an unmapped key, which then fails
    # as an index. That is the right outcome -- a bar outside the panel means
    # the window logic above is wrong -- so it is made explicit here rather
    # than papered over with a fillna that would silently scatter bars into
    # the wrong cell.
    t_arr = bars["session"].map(t_ix)
    s_arr = bars["symbol"].map(s_ix)
    if t_arr.isna().any() or s_arr.isna().any():
        raise RuntimeError(
            f"{int(t_arr.isna().sum())} bars have a session outside "
            f"[{lo}, {hi}] and {int(s_arr.isna().sum())} have an unlisted "
            "symbol; the panel window and the bars filter disagree"
        )
    t_idx = t_arr.to_numpy(dtype=np.int64)
    s_idx = s_arr.to_numpy(dtype=np.int64)

    for name in FIELDS:
        arrays[name][t_idx, s_idx] = bars[name].to_numpy(dtype=np.float64)

    panel = Panel(
        sessions=sessions,
        symbols=symbols,
        calendar_rows=calendar_rows,
        **arrays,
    )
    log(panel.describe())
    return panel


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


def _rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    """Trailing mean over ``window`` rows, aligned so ``out[t]`` uses rows
    ``t-window+1 .. t``. Rows before the window are NaN.

    Implemented as a cumulative sum with a guard against inf contamination
    from the padding NaNs -- there is none here because the mask is applied
    afterwards, but the subtraction of a shifted cumsum is the part that
    silently produces garbage when window == 1.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    c = np.cumsum(np.nan_to_num(x, nan=0.0), axis=0)
    out = np.full(x.shape, np.nan, dtype=np.float64)
    if x.shape[0] >= window:
        out[window - 1 :] = c[window - 1 :] / window
        out[window:] -= c[:-window] / window
    return out


def _rolling_var(x: np.ndarray, window: int, ddof: int = 1) -> np.ndarray:
    """Trailing sample variance, ``ddof`` denominator, aligned like
    :func:`_rolling_mean`.

    ``sum (x - m)^2 = sum(x^2) - n * m^2`` where ``m`` is the mean of the
    SAME trailing window.

    That detail is the whole point. Computing ``(x[i] - m[i])**2`` and then
    summing over the window instead -- each point's deviation from its own
    trailing mean -- is a different quantity, and it silently produced a
    15-20% error in ``vol20``. It fails the same way as writing
    ``score * (x - m)`` instead of ``score(x - m)``: the algebra looks
    adjacent and the result is wrong.
    """
    if ddof >= window:
        raise ValueError("ddof must be smaller than the window")
    mean = _rolling_mean(x, window)
    sum_sq = _rolling_mean(x * x, window) * window
    with np.errstate(invalid="ignore"):
        ss = sum_sq - window * mean * mean
        var = ss / (window - ddof)
    # Near-zero variance makes the cancellation above return a tiny negative
    # number; clamp so a constant window reads 0, not NaN.
    return np.where(np.isfinite(var) & (var < 0.0), 0.0, var)


def _trailing_complete(x: np.ndarray, window: int) -> np.ndarray:
    """True where the trailing ``window`` rows are all finite.

    This is the vectorised form of production's rule that the trailing
    ``required_bars`` authoritative sessions ending at ``as_of`` must be
    present exactly -- a missing session is a hole, never forward-filled.

    The count is subtracted in int64, not float: a cumulative count is exact,
    so ``c[t] - c[t-window] == window`` is the whole test. Copying the
    difference pattern from :func:`_rolling_mean` without the subtraction
    silently leaves the comparison anchored at row 0, which makes the gate
    accept only symbols whose *entire* history is complete.
    """
    finite = np.isfinite(x)
    c = np.cumsum(finite, axis=0, dtype=np.int64)
    out = np.zeros(x.shape, dtype=bool)
    if x.shape[0] >= window:
        out[window - 1 :] = c[window - 1 :] == window
        if x.shape[0] > window:
            out[window:] = (c[window:] - c[:-window]) == window
    return out


@dataclass
class FeatureSet:
    """Per (session, symbol) factor values, aligned to the panel.

    Every field is a 2-D array shaped like the panel. Values at ineligible
    cells are still computed where the window allows it; use
    :func:`eligible_mask` to know which cells are actually in the population.
    """

    price: np.ndarray
    adv20: np.ndarray
    r5: np.ndarray
    r20: np.ndarray
    r60: np.ndarray
    vol20: np.ndarray
    volume_ratio: np.ndarray
    trend: np.ndarray


def compute_features(panel: Panel) -> FeatureSet:
    """Vectorised restatement of production ``compute_features``.

    Uses only the trailing ``REQUIRED_BARS`` sessions, matching production's
    window exactly. No factor looks further back, so nothing here can be
    contaminated by future bars -- but see ``test_harness`` for the prefix
    invariance assertion that proves it.

    ``vol20`` is the sample standard deviation (ddof=1) of the 20 daily
    returns ending at ``t``, annualised by ``sqrt(252)`` -- identical to
    production. Note the production window is ``range(t-19, t+1)``, i.e. 20
    returns needing 21 closes.
    """
    close, volume = panel.close, panel.volume

    with np.errstate(invalid="ignore", divide="ignore"):
        price = close
        dollar = close * volume

        adv20 = _rolling_mean(dollar, 20)
        base5 = np.full(close.shape, np.nan)
        base20 = np.full(close.shape, np.nan)
        base60 = np.full(close.shape, np.nan)
        if close.shape[0] > 5:
            base5[5:] = close[:-5]
        if close.shape[0] > 20:
            base20[20:] = close[:-20]
        if close.shape[0] > 60:
            base60[60:] = close[:-60]
        r5 = close / base5 - 1.0
        r20 = close / base20 - 1.0
        r60 = close / base60 - 1.0

        # 20 daily returns ending at t: close[i]/close[i-1]-1 for i in
        # [t-19, t]. That needs close[t-20] through close[t].
        ret = np.full(close.shape, np.nan)
        if close.shape[0] > 1:
            ret[1:] = close[1:] / close[:-1] - 1.0
        vol20 = np.sqrt(_rolling_var(ret, 20, ddof=1)) * np.sqrt(252.0)

        recent_vol = _rolling_mean(volume, 5)
        baseline_vol = _rolling_mean(volume, 20)
        volume_ratio = recent_vol / baseline_vol

        mean_close20 = _rolling_mean(close, 20)
        trend = close / mean_close20 - 1.0

    return FeatureSet(
        price=price,
        adv20=adv20,
        r5=r5,
        r20=r20,
        r60=r60,
        vol20=vol20,
        volume_ratio=volume_ratio,
        trend=trend,
    )


def eligible_mask(
    panel: Panel,
    features: FeatureSet,
    *,
    min_price: float = MIN_PRICE,
    min_adv20_usd: Optional[float] = MIN_ADV20_USD,
) -> np.ndarray:
    """The production eligibility gate, vectorised.

    Pass ``min_adv20_usd=None`` for the WIDE universe (plan.md R11). The
    wide universe still requires the full 61-session window and the price
    floor -- it only drops the liquidity gate.

    Every clause below corresponds to a production exclusion reason. A
    clause that silently disappears is a universe that silently changes.
    """
    close, volume = panel.close, panel.volume
    # missing_session / insufficient_bars: the full trailing 61-session
    # window must be present. Out-of-window rows fall out here too, so
    # every clause below can rely on finiteness inside the window.
    mask = _trailing_complete(close, REQUIRED_BARS) & _trailing_complete(
        volume, REQUIRED_BARS
    )

    with np.errstate(invalid="ignore"):
        # bad_values: production rejects close <= 0 and volume < 0. NaN
        # compares False on both sides, so it is rejected rather than
        # silently allowed -- the window test above already excluded it.
        mask &= ~(close <= 0.0)
        mask &= ~(volume < 0.0)

        # below_min_price
        mask &= ~(features.price < min_price)

        # below_min_adv20
        if min_adv20_usd is not None:
            mask &= ~(features.adv20 < min_adv20_usd)

        # zero_volume_baseline
        baseline = _rolling_mean(volume, 20)
        mask &= ~(baseline <= 0.0)

        # insufficient_bars for the lagged bases: production raises on a
        # non-positive base close rather than skipping the symbol
        for lag in (5, 20, 60):
            base = np.full(close.shape, np.nan)
            if close.shape[0] > lag:
                base[lag:] = close[:-lag]
            mask &= ~(base <= 0.0)

    return mask


def cross_sections(
    mask: np.ndarray, *, min_n: int = MIN_CROSS_SECTION
) -> np.ndarray:
    """Session indices whose cross-section is at least ``min_n`` wide."""
    return np.flatnonzero(mask.sum(axis=1) >= min_n)


# --------------------------------------------------------------------------
# Forward returns
# --------------------------------------------------------------------------


def forward_returns(panel: Panel, horizon: int) -> np.ndarray:
    """Next-open entry, same-day-out exit, the plan.md R6 convention.

    ``fwd[t, j] = close[t + horizon] / open[t + 1] - 1``

    The last ``horizon`` rows are NaN because the window leaves the panel:
    a forward return may never cross the period boundary (process.md S-20).
    Those losses are symmetric across periods by construction, which is what
    keeps two sides comparable.

    There is deliberately no ``horizon == 1`` special case. Such a branch is
    where the off-by-one that flipped a sign once lived; at ``horizon == 1``
    this general form already reduces to ``close[t+1] / open[t+1] - 1``.
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    n_t = len(panel.sessions)
    out = np.full(panel.close.shape, np.nan, dtype=np.float64)
    if n_t < horizon + 1:
        return out
    with np.errstate(invalid="ignore", divide="ignore"):
        entry = panel.open[1 : n_t - horizon + 1]
        exit_ = panel.close[horizon:n_t]
        out[: n_t - horizon] = exit_ / entry - 1.0
    return out


def valid_as_of(panel: Panel, horizon: int) -> np.ndarray:
    """Session indices whose forward return does not leave the panel."""
    n_t = len(panel.sessions)
    if n_t < horizon + 1:
        return np.empty(0, dtype=int)
    return np.arange(n_t - horizon, dtype=int)


# --------------------------------------------------------------------------
# Market context (for regime labelling, plan.md R15)
# --------------------------------------------------------------------------


def cross_sectional_median_log_return(panel: Panel) -> np.ndarray:
    """Equal-weight market return proxy: per-session cross-sectional median
    of the daily log return.

    ``nanmedian`` over symbols, not a mean: the universe is full of
    micro-caps whose outliers would otherwise dominate.
    """
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = np.full(panel.close.shape, np.nan)
        if len(panel.sessions) > 1:
            ret[1:] = np.log(panel.close[1:] / panel.close[:-1])
    out = np.full(len(panel.sessions), np.nan)
    for t in range(len(panel.sessions)):
        row = ret[t]
        row = row[np.isfinite(row)]
        if row.size:
            out[t] = float(np.median(row))
    return out


def liquidity_tier(adv20: np.ndarray) -> np.ndarray:
    """Slippage multiplier by ADV20 band (plan.md R13-L3). Values 1..3."""
    tier = np.full(adv20.shape, 4, dtype=np.int64)
    with np.errstate(invalid="ignore"):
        tier[adv20 >= 100e6] = 1
        tier[(adv20 >= 20e6) & (adv20 < 100e6)] = 2
        tier[(adv20 >= 5e6) & (adv20 < 20e6)] = 3
    return tier


TIER_SLIPPAGE_MULTIPLIER = {1: 1.0, 2: 1.5, 3: 2.5, 4: 2.5}
