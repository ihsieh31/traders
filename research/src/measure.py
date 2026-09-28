"""Measurement: IC, portfolio returns, costs, regimes, resampling.

READ research/plan.md SECTIONS 5 AND 6 FIRST.

Everything here is formula-agnostic. Nothing in this module knows what a
factor is; it takes arrays and returns numbers. That is what makes it
possible to test it independently of any particular formula, and it is the
reason process.md can record measurement pitfalls without referencing one.

The two things most easily got wrong in this project's history, both
preserved below as load-bearing code rather than as comments:

  * the Newey-West lag is in SAMPLING units (``ceil(horizon/date_step)``),
    not bars (process.md S-1);
  * a period return series is sampled NON-OVERLAPPING, because overlapping
    periods double-charge costs and inflate the annualisation (S-7).

There is no scipy or statsmodels in this environment, so the Student-t
quantile needed for the family-wise threshold is implemented here and
checked against known values in ``test_harness``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from research.src.common import newey_west, spearman_fast

#: Round-trip cost scenarios. The base is plan.md R12; 2.0x is a HARD pass
#: condition (R10-4), not a sensitivity to be reported and forgotten.
COST_SCENARIOS = {"base": 0.0010, "1.5x": 0.0015, "2.0x": 0.0020}

#: Slippage multiplier by ADV20 tier (plan.md R13-L3).
TIER_SLIPPAGE = {1: 1.0, 2: 1.5, 3: 2.5}

#: Periods per year used for annualisation.
PERIODS_PER_YEAR = 250

#: Single-side cost components before the tier multiplier (plan.md R12).
COST_COMPONENTS = {
    "commission": 0.0000,
    "tax": 0.0000,
    "spread": 0.0002,
    "slippage": 0.0003,
}


# --------------------------------------------------------------------------
# IC
# --------------------------------------------------------------------------


def nw_lag(horizon: int, date_step: int) -> int:
    """Newey-West lag in SAMPLING units. See process.md S-1.

    ``date_step=1`` makes this identical to ``horizon``, which is exactly why
    the wrong version survived so long: it only diverges once the sampling
    is thinned.
    """
    return max(1, math.ceil(horizon / date_step))


def ic_series(
    values: np.ndarray,
    fwd: np.ndarray,
    mask: np.ndarray,
    as_of: Sequence[int],
    *,
    min_n: int = 20,
) -> np.ndarray:
    """Per-as-of cross-sectional Spearman rank correlation.

    ``values`` may contain NaN inside the eligible set; those symbols are
    dropped for THIS series only, so one undefined component does not shrink
    the cross-section for every other component on the same date.
    """
    out = np.full(len(as_of), np.nan)
    for i, t in enumerate(as_of):
        cols = np.flatnonzero(mask[t])
        if cols.size < min_n:
            continue
        x = values[t][cols]
        y = fwd[t][cols]
        keep = np.isfinite(x) & np.isfinite(y)
        if int(keep.sum()) < min_n:
            continue
        out[i] = spearman_fast(x, y, mask=keep, min_n=min_n)
    return out


def summarise_ic(series: np.ndarray, horizon: int, date_step: int) -> dict:
    """Mean IC, Newey-West t, sign consistency, coverage."""
    clean = series[np.isfinite(series)]
    if clean.size < 3:
        return {
            "ic_mean": None,
            "ic_median": None,
            "ic_t": None,
            "n": int(clean.size),
            "positive_ratio": None,
            "n_positive": 0,
            "nw_lag": nw_lag(horizon, date_step),
        }
    mean, t = newey_west(clean, nw_lag(horizon, date_step))
    return {
        "ic_mean": float(mean),
        "ic_median": float(np.median(clean)),
        "ic_t": float(t),
        "n": int(clean.size),
        "positive_ratio": float((clean > 0).mean()),
        "n_positive": int((clean > 0).sum()),
        "nw_lag": nw_lag(horizon, date_step),
    }


# --------------------------------------------------------------------------
# Multiple testing
# --------------------------------------------------------------------------


def effective_tests(ic_matrix: np.ndarray) -> dict:
    """``N_eff = n^2 / sum(lambda^2)`` from the IC correlation eigenvalues.

    ``ic_matrix`` is ``(n_series, n_dates)``. Independent series give
    ``N_eff = n``; identical series give ``N_eff = 1``. Using the raw series
    count instead is the classic error -- two factors correlated at 0.99 are
    not two tests (process.md S-4).
    """
    x = np.asarray(ic_matrix, dtype=float)
    x = x[np.all(np.isfinite(x), axis=1)] if x.size else x
    n = x.shape[0]
    if n < 2:
        return {"n_series": int(n), "n_eff": float(max(1, n)), "eigenvalues": []}
    corr = np.corrcoef(x)
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    eigenvalues = np.linalg.eigvalsh(corr)
    eigenvalues = np.clip(eigenvalues, 0.0, None)
    n_eff = float(n * n / max(eigenvalues @ eigenvalues, 1e-12))
    return {
        "n_series": int(n),
        "n_eff": n_eff,
        "eigenvalues": [float(v) for v in eigenvalues[::-1]],
    }


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Numerical Recipes)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_sf(t: float, df: float) -> float:
    """Upper-tail probability of Student's t."""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    return 0.5 * betainc(df / 2.0, 0.5, x)


def student_t_ppf(p: float, df: float) -> float:
    """Quantile of Student's t by bisection on the survival function.

    Slow but unconditionally correct, and this is called a handful of times
    per round, not in a loop.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    target = 1.0 - p
    lo, hi = -400.0, 400.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if student_t_sf(mid, df) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bonferroni_t(n_eff: float, alpha: float = 0.05, df: int | None = None) -> float:
    """Two-sided family-wise threshold for ``n_eff`` simultaneous tests."""
    n_eff = max(1.0, float(n_eff))
    if df is None:
        df = 1000
    return student_t_ppf(1.0 - alpha / (2.0 * n_eff), df)


# --------------------------------------------------------------------------
# Portfolio
# --------------------------------------------------------------------------


@dataclass
class Portfolio:
    """Top-k equal-weight portfolio over non-overlapping periods.

    Periods are spaced ``horizon`` sessions apart, so each one is an
    independent ``horizon``-day holding with its own cost. Overlapping
    sampling would charge the entry cost once per overlapping rebalance and
    annualise 252 overlapping holdings as if they were 252 independent bets
    (process.md S-7).
    """

    as_of: np.ndarray           # (n_periods,) session indices
    names: List[List[int]]      # per period, symbol indices
    gross: np.ndarray           # (n_periods,) mean forward return
    turnover: np.ndarray        # (n_periods,) 0..1
    n_available: np.ndarray     # per period, how many picks had a forward return
    n_missing_periods: int = 0
    per_name: Dict[int, float] = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.as_of.size)

    def net(self, round_trip: float) -> np.ndarray:
        return self.gross - self.turnover * round_trip

    def annualised(self, returns: np.ndarray, horizon: int) -> float:
        if returns.size == 0:
            return float("nan")
        return float(np.nanmean(returns) * (PERIODS_PER_YEAR / horizon))


def build_portfolio(
    scores: np.ndarray,
    fwd: np.ndarray,
    mask: np.ndarray,
    *,
    horizon: int,
    top_k: int = 20,
    as_of: Optional[Sequence[int]] = None,
    min_n: int = 20,
) -> Portfolio:
    """Equal-weight top-k portfolio on non-overlapping periods.

    A pick with no forward return (halted, delisted inside the window) is
    DROPPED from that period's average, not filled with zero -- a position
    you cannot sell does not earn zero, it just does not exist. The count of
    drops is reported, because a period averaging three names is not the
    same measurement as one averaging twenty (process.md S-22).
    """
    n_t = scores.shape[0]
    if as_of is None:
        # 0, horizon, 2*horizon, ... while t + horizon <= n_t - 1, so a
        # period never runs past the panel end (process.md S-20).
        #
        # Note np.arange(n_t - horizon, horizon - 1, horizon) is EMPTY for
        # any realistic panel, because numpy returns nothing when stop < start
        # for a positive step. That bug made every default portfolio run
        # silently produce zero periods.
        as_of = np.arange(0, max(0, n_t - horizon), horizon, dtype=int)
    as_of = np.asarray(sorted(int(t) for t in as_of), dtype=int)

    picks: List[List[int]] = []
    gross = np.full(as_of.size, np.nan)
    turnover = np.full(as_of.size, np.nan)
    n_avail = np.zeros(as_of.size, dtype=int)
    per_name: Dict[int, float] = {}
    previous: Optional[set] = None
    missing = 0

    for i, t in enumerate(as_of):
        cols = np.flatnonzero(mask[t]) if t < n_t else np.empty(0, dtype=int)
        if cols.size < min_n:
            picks.append([])
            continue
        vals = scores[t][cols]
        order = np.lexsort((cols, -vals))
        chosen = [int(j) for j in cols[order][:top_k]]
        picks.append(chosen)

        rets = [fwd[t][j] for j in chosen if np.isfinite(fwd[t][j])]
        n_avail[i] = len(rets)
        if not rets:
            missing += 1
            previous = set(chosen)
            continue
        gross[i] = float(np.mean(rets))
        for j, r in zip(chosen, rets):
            per_name[j] = per_name.get(j, 0.0) + float(r) / len(rets)

        current = set(chosen)
        if previous is None:
            turnover[i] = 1.0  # initial build funds 20 positions (S-21)
        else:
            weight = 1.0 / top_k
            overlap = len(current & previous)
            turnover[i] = 0.5 * (2.0 * (top_k - overlap) * weight)
        previous = current

    return Portfolio(
        as_of=as_of,
        names=picks,
        gross=gross,
        turnover=turnover,
        n_available=n_avail,
        n_missing_periods=missing,
        per_name=per_name,
    )


def cost_table(portfolio: Portfolio, horizon: int) -> Dict[str, dict]:
    """Net annualised return under each cost scenario."""
    out = {}
    for label, cost in COST_SCENARIOS.items():
        net = portfolio.net(cost)
        gross = portfolio.gross
        out[label] = {
            "round_trip": cost,
            "n_periods": int(np.isfinite(net).sum()),
            "mean_gross_per_period": float(np.nanmean(gross)),
            "mean_net_per_period": float(np.nanmean(net)),
            "annualised_gross": portfolio.annualised(gross, horizon),
            "annualised_net": portfolio.annualised(net, horizon),
            "nw_t_gross": _period_t(gross, horizon),
            "nw_t_net": _period_t(net, horizon),
            "positive_ratio": float(np.nanmean(net > 0)) if np.isfinite(net).any() else None,
        }
    return out


def _period_t(returns: np.ndarray, horizon: int) -> Optional[float]:
    clean = returns[np.isfinite(returns)]
    if clean.size < 3:
        return None
    _mean, t = newey_west(clean, nw_lag(horizon, max(1, horizon)))
    return float(t)


# --------------------------------------------------------------------------
# Regime labelling (plan.md R15)
# --------------------------------------------------------------------------


@dataclass
class Regimes:
    trend: np.ndarray       # +1 bull / -1 bear, from trailing 60 sessions only
    vol: np.ndarray         # 1 high vol / -1 low vol
    sessions: np.ndarray


def label_regimes(
    market: np.ndarray, *, lookback: int = 60
) -> Regimes:
    """Bull/bear and high/low vol labels from TRAILING data only.

    The definition itself must be free of look-ahead: the label at ``t`` may
    only use market returns up to ``t``. A label that peeks forward turns the
    regime split into a second, invisible selection over the sample.
    """
    market = np.asarray(market, dtype=float)
    n = market.size
    trend = np.zeros(n, dtype=int)
    for t in range(n):
        lo = max(0, t - lookback + 1)
        window = market[lo : t + 1]
        window = window[np.isfinite(window)]
        if window.size >= max(10, lookback // 3):
            trend[t] = 1 if float(window.sum()) > 0 else -1
    realised = np.full(n, np.nan)
    for t in range(1, n):
        lo = max(0, t - lookback + 1)
        w = market[lo : t + 1]
        w = w[np.isfinite(w)]
        if w.size >= 10:
            realised[t] = float(np.std(w, ddof=1) * math.sqrt(252.0))
    med = float(np.nanmedian(realised)) if np.isfinite(realised).any() else float("nan")
    vol = np.where(realised > med, 1, -1)
    return Regimes(trend=trend, vol=vol, sessions=np.arange(n))


def split_by_regime(
    portfolio: Portfolio, regimes: Regimes
) -> Dict[str, dict]:
    """Portfolio statistics per regime cell, each with its own period count."""
    out: Dict[str, dict] = {}
    trend = regimes.trend
    vol = regimes.vol
    for t_label, t_name in ((1, "bull"), (-1, "bear")):
        for v_label, v_name in ((1, "high_vol"), (-1, "low_vol")):
            sel = [
                i
                for i, t in enumerate(portfolio.as_of)
                if t < trend.size and trend[t] == t_label and vol[t] == v_label
            ]
            if not sel:
                out[f"{t_name}_{v_name}"] = {"n_periods": 0}
                continue
            idx = np.asarray(sel)
            gross = portfolio.gross[idx]
            net = portfolio.net(COST_SCENARIOS["base"])[idx]
            horizon = max(1, int(np.median(np.diff(portfolio.as_of)))) if len(idx) > 1 else 1
            out[f"{t_name}_{v_name}"] = {
                "n_periods": int(idx.size),
                "mean_gross": float(np.nanmean(gross)),
                "mean_net": float(np.nanmean(net)),
                "annualised_net": float(np.nanmean(net) * (PERIODS_PER_YEAR / horizon))
                if np.isfinite(net).any()
                else None,
                "positive_ratio": float(np.nanmean(net > 0)) if np.isfinite(net).any() else None,
            }
    return out


# --------------------------------------------------------------------------
# Robustness (plan.md R16)
# --------------------------------------------------------------------------


def confidence_interval(returns: np.ndarray, *, alpha: float = 0.05) -> dict:
    """Normal-approximation CI for the mean period return.

    A wide CI relative to the effect is the FIRST thing to check: if the
    interval is wider than the edge, every downstream statistic is noise and
    there is no point running bootstrap or drop-best (process.md S-33).
    """
    clean = returns[np.isfinite(returns)]
    n = clean.size
    if n < 3:
        return {"n": int(n), "mean": None, "ci_low": None, "ci_high": None, "width": None}
    mean = float(clean.mean())
    se = float(clean.std(ddof=1) / math.sqrt(n))
    z = student_t_ppf(1.0 - alpha / 2.0, max(1, n - 1))
    return {
        "n": int(n),
        "mean": mean,
        "stderr": se,
        "ci_low": mean - z * se,
        "ci_high": mean + z * se,
        "width": 2.0 * z * se,
        "width_over_abs_mean": (2.0 * z * se / abs(mean)) if mean != 0 else None,
    }


def block_bootstrap(
    returns: np.ndarray,
    horizon: int,
    *,
    n_resamples: int = 2000,
    block: Optional[int] = None,
    alpha: float = 0.05,
    seed: int = 20260927,
) -> dict:
    """Moving-block bootstrap on the PERIOD series.

    Block length defaults to the holding period, because adjacent period
    returns overlap in time and an i.i.d. bootstrap destroys that structure
    and UNDERSTATES the variance -- producing an over-confident interval.

    The resampling unit is the period, never the individual trade. Resampling
    trades treats 20 correlated positions in one period as 20 independent
    observations and grossly overstates the sample size (S-33).
    """
    clean = returns[np.isfinite(returns)]
    n = clean.size
    if n < 6:
        return {"n": int(n), "insufficient": True}
    block = int(block or max(1, math.ceil(horizon)))
    block = min(block, n)
    n_blocks = int(math.ceil(n / block))
    rng = np.random.default_rng(seed)
    means = np.empty(n_resamples, dtype=float)
    for r in range(n_resamples):
        starts = rng.integers(0, max(1, n - block + 1), size=n_blocks)
        sample = np.concatenate(
            [clean[s : s + block] for s in starts]
        )[:n]
        means[r] = sample.mean()
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {
        "n": int(n),
        "block": block,
        "n_resamples": int(n_resamples),
        "mean": float(clean.mean()),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "crosses_zero": bool(lo <= 0.0 <= hi),
        "p_positive": float((means > 0).mean()),
    }


def drop_best(returns: np.ndarray, horizon: int, fractions=(0.01, 0.03, 0.05)) -> dict:
    """Recompute the mean after removing the best ``fraction`` of periods.

    A strategy whose edge lives in a handful of periods will flip sign here.
    That is a legitimate negative result, not a bug (S-32).
    """
    clean = returns[np.isfinite(returns)]
    n = clean.size
    if n < 10:
        return {"n": int(n), "insufficient": True}
    order = np.argsort(clean)[::-1]
    out = {"n": int(n), "full_mean": float(clean.mean()), "by_fraction": {}}
    for f in fractions:
        k = max(1, int(round(n * f)))
        kept = np.delete(clean, order[:k])
        _m, t = newey_west(kept, nw_lag(horizon, max(1, horizon)))
        out["by_fraction"][f"{f:.0%}"] = {
            "dropped": k,
            "mean": float(kept.mean()),
            "nw_t": float(t),
            "sign_flipped": bool(kept.mean() * clean.mean() < 0),
        }
    return out


def contribution_decomposition(portfolio: Portfolio) -> dict:
    """Where the return came from: by period and by symbol.

    A single symbol or a single period contributing more than ~30% of the
    total means the edge is an event, not a rule (plan.md R16-Q4).
    """
    gross = portfolio.gross
    finite = gross[np.isfinite(gross)]
    total = float(finite.sum()) if finite.size else 0.0
    by_period = {}
    if finite.size and total != 0.0:
        shares = np.sort(np.abs(finite / total))[::-1]
        for q in (0.01, 0.05, 0.10):
            k = max(1, int(round(finite.size * q)))
            by_period[f"top_{int(q*100)}%"] = float(shares[:k].sum())
    by_symbol = {}
    if portfolio.per_name and total != 0.0:
        shares = sorted(
            (abs(v / total) for v in portfolio.per_name.values()), reverse=True
        )
        arr = np.asarray(shares)
        for q in (0.01, 0.05, 0.10):
            k = max(1, int(round(arr.size * q)))
            by_symbol[f"top_{int(q*100)}%"] = float(arr[:k].sum())
    return {
        "total_gross": total,
        "n_periods": int(finite.size),
        "n_symbols_contributing": len(portfolio.per_name),
        "period_share": by_period,
        "symbol_share": by_symbol,
    }


def describe(panel, returns: np.ndarray) -> dict:
    """Percentile block for a return series.

    Reported next to every headline number so a reader can see how much of a
    gross edge costs would eat, without running a backtest.
    """
    clean = np.asarray(returns)[np.isfinite(returns)]
    if clean.size == 0:
        return {"n": 0}
    return {
        "n": int(clean.size),
        "p5": float(np.percentile(clean, 5)),
        "p25": float(np.percentile(clean, 25)),
        "median": float(np.median(clean)),
        "p75": float(np.percentile(clean, 75)),
        "p95": float(np.percentile(clean, 95)),
        "median_abs": float(np.median(np.abs(clean))),
    }


def diversification(
    portfolio: Portfolio, panel, horizon: int
) -> dict:
    """Are the 20 picks 20 independent bets?

    Twenty names at average pairwise correlation 0.85 is closer to one bet
    than to twenty (plan.md R14-H2, process.md S-39). The participation
    ratio ``N / (1 + (N-1) * rho)`` is the standard one-number summary.

    The correlation is computed over the DAILY returns inside each holding
    window, not over the period returns. Period returns cannot answer the
    question: a period gives each name exactly one number, and a fast book
    holds each name only three or four times in the whole sample, so there
    is nothing to correlate.

    Using the window's daily returns is also the more faithful reading of the
    risk -- it is the correlation of the things actually held, over exactly
    the time they are held.
    """
    if horizon < 2:
        return {
            "mean_pairwise_corr": None,
            "note": "horizon 1 gives one observation per pick; pairwise "
            "correlation is undefined. Report the holding period instead.",
        }
    per_period: List[float] = []
    n_t = len(panel.sessions)
    picks_seen = 0
    for i, t in enumerate(portfolio.as_of):
        names = [j for j in portfolio.names[i] if np.isfinite(panel.close[t, j])]
        if len(names) < 4 or t + horizon >= n_t:
            continue
        # `horizon` daily returns need `horizon + 1` closes: the entry day's
        # open is not in this table, so the window is measured on closes only,
        # rows t+1 .. t+horizon against rows t .. t+horizon-1.
        block = np.log(
            panel.close[t + 1 : t + horizon + 1][:, names]
            / panel.close[t : t + horizon][:, names]
        )
        if not np.isfinite(block).all():
            keep = np.isfinite(block).all(axis=0)
            block = block[:, keep]
        if block.shape[0] < 2 or block.shape[1] < 4:
            continue
        if block.std(axis=0).min() <= 0:
            continue
        corr = np.corrcoef(block, rowvar=False)
        iu = np.triu_indices(corr.shape[0], 1)
        off = corr[iu]
        off = off[np.isfinite(off)]
        if off.size:
            per_period.append(float(off.mean()))
            picks_seen += block.shape[1]
    if not per_period:
        return {
            "n_periods_measured": 0,
            "mean_pairwise_corr": None,
            "note": "no period had enough constant-variance picks to correlate",
        }
    rho = float(np.mean(per_period))
    n = float(np.median([len(p) for p in portfolio.names if p]))
    pr = n / (1.0 + (n - 1.0) * rho) if rho > -1.0 / max(1.0, n - 1) else float(n)
    return {
        "n_periods_measured": len(per_period),
        "basis": f"daily log returns inside each {horizon}-session window",
        "mean_pairwise_corr": rho,
        "median_positions_per_period": n,
        "effective_positions": float(pr),
        "note": "20 names is not 20 independent bets; see process.md S-39",
    }
