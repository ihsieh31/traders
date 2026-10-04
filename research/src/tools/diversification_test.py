"""Can low-correlation selection fix the 20-stock book without any new alpha?

The problem this targets
-----------------------
process.md S-39 measured the production Top-20 at average pairwise
correlation 0.265, i.e. a **participation ratio of 3.32** -- the book looks
like 20 positions and is statistically about 3. That is an engineering
defect, not a prediction problem, and it can be attacked without assuming any
factor works.

The test
--------
A/B against the existing production score on the same panel, same universe,
same forward returns, same cost table, same Newey-West, same participation
ratio definition. Only the *tie-break* changes:

  BASELINE   top 20 by production score (what runs today)
  VARIANT    greedy: take the highest score first, then repeatedly add the
             candidate maximising
                 pct_rank(score) - lam * mean_corr(candidate, selected)

The correlation is **trailing** -- the last 60 sessions of daily log returns
up to and including ``t`` -- never the forward returns being evaluated. Using
forward correlation would be pure look-ahead (plan.md R7-L1) and would make
this look better than it can possibly be in production.

Candidate pool
--------------
Diversifying across all ~400 eligible names is not the point; it would throw
the score away. So diversification happens *within the top-M by score*. M is
a pre-declared parameter and is swept as a whole curve, because a single
cell is not evidence (plan.md R5).

This is NOT a new formula and NOT a claim of alpha. It asks one engineering
question: does buying a less-correlated basket change the risk, and what
does the turnover cost? A "no" here is a complete and useful answer.

Read research/plan.md and research/process.md before changing anything here.
"""

from __future__ import annotations

import sys
from datetime import date
from typing import Callable, List, Optional, Sequence

import numpy as np

from research.src import factors as F
from research.src import measure as M
from research.src import panel as P

TOP_K = 20
TRAILING_CORR = 60

# Pre-declared grids. Reported as full surfaces, never as a best cell.
POOL_SIZES = (20, 50, 100, 200, 400)
LAMBDAS = (0.0, 0.25, 0.5, 1.0, 2.0)


def trailing_corr_block(
    close: np.ndarray, t: int, cols: np.ndarray, window: int
) -> Optional[np.ndarray]:
    """Correlation of daily log returns over ``t-window+1 .. t`` for ``cols``.

    Returns None if any name in the pool is missing inside the window: a
    correlation computed on ragged histories is not a correlation, and
    silently pairwise-dropping would make the pool depend on the data
    (process.md S-23).
    """
    if t - window + 1 < 0:
        return None
    block = close[t - window + 1 : t + 1][:, cols]
    if block.shape[0] < 3 or not np.isfinite(block).all():
        return None
    if np.any(block <= 0):
        return None
    rets = np.diff(np.log(block), axis=0)  # (window-1, n)
    sd = rets.std(axis=0, ddof=1)
    if not np.isfinite(sd).all() or np.any(sd == 0):
        return None
    corr = np.corrcoef(rets, rowvar=False)
    if not np.isfinite(corr).all():
        return None
    return corr


def diversify_select(
    close: np.ndarray,
    scores_row: np.ndarray,
    cols: np.ndarray,
    t: int,
    *,
    pool_size: int,
    lam: float,
    top_k: int = TOP_K,
    corr_window: int = TRAILING_CORR,
) -> Optional[List[int]]:
    """Greedy low-correlation selection within the top-``pool_size`` by score.

    ``lam == 0`` reduces exactly to production's top-k-by-score, so the
    baseline is reachable through the same code path -- which means any
    difference at lam=0 is a bug in this function, not an effect.
    """
    vals = scores_row[cols]
    order = np.lexsort((cols, -vals))
    pool = [int(j) for j in cols[order][:pool_size]]
    if len(pool) < top_k:
        return None
    if lam == 0.0:
        return pool[:top_k]

    pos = {j: i for i, j in enumerate(pool)}
    corr = trailing_corr_block(close, t, np.array(pool), corr_window)
    if corr is None:
        # Cannot measure the risk we are trying to reduce, so fall back to
        # the score ordering rather than inventing a diversification we did
        # not verify. Reporting this matters: a fallback that looks
        # identical to lam=0 is not evidence that diversification worked.
        return pool[:top_k]

    # Rank-normalise the score inside the pool so lam and score are on
    # comparable scales regardless of how the score is calibrated.
    pv = np.array([scores_row[j] for j in pool], dtype=float)
    if not np.isfinite(pv).all():
        return pool[:top_k]
    rank = np.argsort(np.argsort(pv)) / max(1, len(pv) - 1)

    selected = [0]  # highest score seeds the book
    remaining = set(range(1, len(pool)))
    while len(selected) < top_k and remaining:
        best_i, best_v = None, -np.inf
        for i in sorted(remaining):
            avg_corr = float(np.mean(corr[i, selected]))
            v = rank[i] - lam * avg_corr
            if v > best_v:
                best_v, best_i = v, i
        selected.append(best_i)
        remaining.discard(best_i)
    return [pool[i] for i in selected]


def build_portfolio_selector(
    close: np.ndarray,
    scores: np.ndarray,
    fwd: np.ndarray,
    mask: np.ndarray,
    *,
    horizon: int,
    as_of: Sequence[int],
    selector: Callable[[int, np.ndarray], Optional[List[int]]],
    top_k: int = TOP_K,
    min_n: int = 20,
) -> M.Portfolio:
    """``measure.build_portfolio`` with a pluggable per-period selector.

    The turnover, drop-unavailable and initial-build accounting is copied from
    build_portfolio verbatim so the two paths are measured identically. A
    divergence here would make the A/B meaningless.
    """
    picks: List[List[int]] = []
    gross = np.full(len(as_of), np.nan)
    turnover = np.full(len(as_of), np.nan)
    n_avail = np.zeros(len(as_of), dtype=int)
    per_name = {}
    previous: Optional[set] = None
    missing = 0
    fallbacks = 0

    for i, t in enumerate(as_of):
        cols = np.flatnonzero(mask[t]) if t < mask.shape[0] else np.empty(0, dtype=int)
        if cols.size < min_n:
            picks.append([])
            continue
        chosen = selector(int(t), cols)
        if chosen is None:
            missing += 1
            previous = None
            continue
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
            turnover[i] = 1.0  # initial build, S-21
        else:
            weight = 1.0 / top_k
            overlap = len(current & previous)
            turnover[i] = 0.5 * (2.0 * (top_k - overlap) * weight)
        previous = current

    pf = M.Portfolio(
        as_of=np.asarray(as_of, dtype=int),
        names=picks,
        gross=gross,
        turnover=turnover,
        n_available=n_avail,
        n_missing_periods=missing,
        per_name=per_name,
    )
    return pf


def daily_portfolio_returns(
    close: np.ndarray, pf: M.Portfolio, horizon: int = 20
) -> np.ndarray:
    """Daily equal-weight portfolio log return across the whole sample.

    The participation ratio is a *proxy* for diversification. The thing that
    actually matters to a risk layer is realised volatility and drawdown, and
    those cannot be read off a proxy. This builds the daily series the same
    way a real book would be marked: each name contributes 1/20 of the daily
    move on the days it is held.

    Daily returns are used rather than period returns because a 20-day book
    has only ~66 non-overlapping observations in five years, which is not
    enough to estimate a drawdown.
    """
    n_t = close.shape[0]
    ret = np.full(n_t, np.nan)
    holding = [[] for _ in range(n_t)]
    # A position entered at t is HELD for the next `horizon` sessions, i.e.
    # days t+1 .. t+horizon. Marking only day t (the as-of date) and leaving
    # the rest of the window flat compounds ~66 entry-day returns as if they
    # were the entire book, which inflates annualised return by roughly the
    # square root of the horizon. That produced a physically impossible
    # +157%/yr at Sharpe 9.4 before this was fixed.
    for i, t in enumerate(pf.as_of):
        names = list(pf.names[i])
        t = int(t)
        for s in range(t + 1, min(t + horizon + 1, n_t)):
            holding[s] = names
    with np.errstate(invalid="ignore", divide="ignore"):
        logret = np.diff(np.log(close), axis=0, prepend=np.full((1, close.shape[1]), np.nan))
    for s in range(1, n_t):
        names = sorted({j for j in holding[s] if j < close.shape[1]})
        if not names:
            continue
        vals = logret[s][names]
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            continue
        ret[s] = float(np.mean(vals))
    return ret


def risk_profile(close: np.ndarray, pf: M.Portfolio, horizon: int = 20) -> dict:
    r = daily_portfolio_returns(close, pf, horizon)
    r = r[np.isfinite(r)]
    if r.size < 30:
        return {}
    vol = float(r.std(ddof=1) * np.sqrt(252))
    curve = np.exp(np.cumsum(r))
    peak = np.maximum.accumulate(curve)
    mdd = float((curve / peak - 1.0).min())
    ann = float((curve[-1] ** (252.0 / r.size) - 1.0))
    return {
        "ann_ret": ann,
        "ann_vol": vol,
        "sharpe": ann / vol if vol > 0 else float("nan"),
        "max_drawdown": mdd,
        "n_days": int(r.size),
    }


def report(pf: M.Portfolio, panel, horizon: int, label: str) -> dict:
    ct = M.cost_table(pf, horizon)
    div = M.diversification(pf, panel, horizon)
    base, c15, c20 = ct["base"], ct["1.5x"], ct["2.0x"]
    eff = div.get("effective_positions")
    ci = M.confidence_interval(pf.net(0.0010))
    ratio = None
    if ci and ci.get("ci_width") is not None and abs(ci.get("mean", 0.0)) > 0:
        ratio = ci["ci_width"] / abs(ci["mean"])
    return {
        "label": label,
        "mean_pairwise_corr": div.get("mean_pairwise_corr"),
        "effective_positions": eff,
        "turnover": float(np.nanmean(pf.turnover)),
        "ann_base": base["annualised_net"],
        "ann_1.5x": c15["annualised_net"],
        "ann_2.0x": c20["annualised_net"],
        "nw_t_base": base["nw_t_net"],
        "n_periods": base["n_periods"],
        "ci_ratio": ratio,
    }


def main() -> int:
    start, end = date(2016, 1, 4), date(2020, 12, 31)
    horizon = 20
    trusted = "research/out/trusted_symbols.txt"

    print("=" * 92)
    print("低相關選股 A/B 測試 — 生產資格宇宙，2016-01-04 .. 2020-12-31，持有 20 日")
    print("=" * 92)
    print()
    print("基準 = 現行生產 score 的 Top-20。變體 = 同一橫斷面內的貪婪低相關挑選。")
    print("相關性只用 t 當日往前 60 個 session 的日報酬，不用前瞻報酬。")
    print()

    panel = P.build_panel(start=start, end=end, symbols_file=trusted)
    feats = P.compute_features(panel)
    mask = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
    fwd = P.forward_returns(panel, horizon)
    # Thin to NON-OVERLAPPING periods. Passing every valid session gives
    # 1304 periods that each overlap the previous 19, which makes the
    # Newey-West lag of 1 far too small for the actual dependence and
    # inflates every t-value (process.md S-7). The project's own study.py
    # does this correctly -- walk_forward thins with test[::horizon] and the
    # main/sweep paths rely on build_portfolio's spaced default. An earlier
    # version of this script passed every session and reported t=5.30, which
    # was this bug, not a result.
    as_of = P.study_as_of(panel, horizon, start)
    print(f"eligible/期 中位數 {np.median(mask.sum(1)):.0f}；"
          f"非重疊期間 {len(as_of)} 個（每 {horizon} 個 session 取一次）")
    print()

    scores = F.production_score(feats, mask)
    rows: List[dict] = []

    base_pf = M.build_portfolio(
        scores, fwd, mask, horizon=horizon, top_k=TOP_K, as_of=as_of
    )
    b = report(base_pf, panel, horizon, "BASELINE 生產 score Top-20")
    rows.append(b)

    for pool in POOL_SIZES:
        for lam in LAMBDAS:
            sel = lambda t, cols, p=pool, l=lam: diversify_select(  # noqa: E731
                panel.close, scores[t], cols, t, pool_size=p, lam=l
            )
            pf = build_portfolio_selector(
                panel.close, scores, fwd, mask,
                horizon=horizon, as_of=as_of, selector=sel,
            )
            rows.append(
                report(pf, panel, horizon, f"pool={pool:<4} lam={lam:<5}")
            )

    def show(r: dict) -> None:
        print(
            f"{r['label']:<26}"
            f"{r['mean_pairwise_corr']:>7.3f}"
            f"{r['effective_positions']:>8.2f}"
            f"{r['turnover']:>9.3f}"
            f"{r['ann_base']*100:>9.1f}%"
            f"{r['ann_1.5x']*100:>9.1f}%"
            f"{r['ann_2.0x']*100:>9.1f}%"
            f"{r['nw_t_base']:>8.2f}"
        )

    print(f"{'變體':<26}{'平均相關':>7}{'有效部位':>8}{'換手':>9}"
          f"{'淨@1.0x':>10}{'淨@1.5x':>10}{'淨@2.0x':>10}{'NW t':>8}")
    print("-" * 92)
    for r in rows:
        show(r)

    # --- the actual objective: realised risk, not a diversification proxy ---
    print()
    print("=" * 92)
    print("真正的目標：實現風險（日報酬序列，非期間序列）")
    print("=" * 92)
    print(f"{'變體':<26}{'年化報酬':>10}{'年化波動':>10}{'Sharpe':>9}{'最大回撤':>10}")
    print("-" * 92)
    key_variants = [
        ("BASELINE 生產 score", base_pf),
    ]
    for pool, lam in ((200, 0.5), (200, 1.0), (200, 2.0), (400, 1.0), (400, 2.0)):
        sel = lambda t, cols, p=pool, l=lam: diversify_select(  # noqa: E731
            panel.close, scores[t], cols, t, pool_size=p, lam=l
        )
        pf = build_portfolio_selector(
            panel.close, scores, fwd, mask,
            horizon=horizon, as_of=as_of, selector=sel,
        )
        key_variants.append((f"pool={pool} lam={lam}", pf))

    for label, pf in key_variants:
        rp = risk_profile(panel.close, pf, horizon)
        if not rp:
            continue
        print(
            f"{label:<26}{rp['ann_ret']*100:>9.1f}%{rp['ann_vol']*100:>9.1f}%"
            f"{rp['sharpe']:>9.2f}{rp['max_drawdown']*100:>9.1f}%"
        )

    print()
    print("=" * 92)
    print("對照基準（CRSP Mkt-RF，同期間）: +16.0%/年, t=1.84, SR 0.82")
    print("我們的 Top-20 是純多頭，無空頭腿，所以它先拿到市場 beta。")
    print("=" * 92)

    best_div = max(rows[1:], key=lambda r: (r["effective_positions"] or 0))
    print()
    print(f"有效部位數最高: {best_div['label']}  → {best_div['effective_positions']:.2f} "
          f"(基準 {b['effective_positions']:.2f})")
    print(f"  2.0x 成本淨年化 {best_div['ann_2.0x']*100:+.1f}%  "
          f"(基準 {b['ann_2.0x']*100:+.1f}%)")
    print(f"  換手 {best_div['turnover']:.3f} (基準 {b['turnover']:.3f})")

    print()
    print("R5 平台檢查（整條曲線，不是最好的一格）— pool=200:")
    for r in rows:
        if r["label"].startswith("pool=200"):
            print(f"  lam={r['label'].split('lam=')[1]:<6}"
                  f"有效部位 {r['effective_positions']:>5.2f}"
                  f"  2.0x 淨 {r['ann_2.0x']*100:>6.1f}%"
                  f"  t {r['nw_t_base']:>5.2f}")
    print()
    print(f"本輪變體數（計入多重檢定）: {len(rows) - 1} + 1 基準 = {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
