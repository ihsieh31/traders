"""Is the Top-20's shallower drawdown real, or one lucky path?

The claim under test
--------------------
Previous round measured a max drawdown of -28.7% for the production Top-20
against -35.9% for the equal-weighted universe over 2016-2020, a 7.2pp
shallower book, at similar return. That is the most promising lead left, and
it has exactly one problem: **max drawdown is a single order statistic of a
single 66-period path, and a difference between two such paths is not a
test.**

Why the naive comparison fails
------------------------------
Two independent paths' drawdowns differ by chance all the time. The correct
comparison is *paired*: both books were exposed to the same market days, so
the difference must be measured on days both were held, and the null has to
preserve the shared exposure. Resampling the two books independently would
throw that pairing away and manufacture significance.

What this does
--------------
1. Builds the universe benchmark as an equal-weight book of every eligible
   name, rebalanced on the SAME 20-day schedule as the Top-20, so the two
   differ only in which names are held -- not in rebalancing cadence, cost
   convention, or window. (The previous round's "universe median" was the
   median *stock*, which is a different animal again.)
2. Paired moving-block bootstrap, block = 20 sessions per plan.md R16-Q2, on
   the joint day index, so the resampled market path is shared.
3. Reports the bootstrap distribution of the drawdown difference, plus
   secondary tail metrics that do not depend on a single extreme point.
4. Splits by regime (plan.md R15) with period counts, because a 7pp gap that
   exists in one regime is a beta story, not a risk-control story.

Pre-declared decision
---------------------
The lead is kept only if the paired bootstrap 95% interval for
(drawdown_book - drawdown_universe) lies entirely above zero, i.e. the
Top-20 is shallower with the market noise resampled out. Anything weaker is
reported as not established. No variant is tuned after seeing the interval.

Read research/plan.md and research/process.md before changing anything here.
"""

from __future__ import annotations

import sys
from datetime import date

import numpy as np

from research.src import factors as F
from research.src import measure as M
from research.src import panel as P

HORIZON = 20
TOP_K = 20
BLOCK = 20          # plan.md R16-Q2: block length >= holding period
N_BOOT = 2000       # plan.md R16-Q2: >= 1000
TRUSTED = "research/out/trusted_symbols.txt"


def held_daily_curve(panel, holds: dict[int, list[int]], horizon: int) -> np.ndarray:
    """Daily equal-weight log return of whatever is held on each session."""
    n_t = panel.close.shape[0]
    with np.errstate(invalid="ignore", divide="ignore"):
        lr = np.diff(np.log(panel.close), axis=0,
                     prepend=np.full((1, panel.close.shape[1]), np.nan))
    out = np.full(n_t, np.nan)
    for s in range(1, n_t):
        names = [j for j in holds.get(s, []) if j < panel.close.shape[1]]
        if not names:
            continue
        v = lr[s][names]
        v = v[np.isfinite(v)]
        if v.size:
            out[s] = float(np.mean(v))
    return out


def holds_from_picks(pf, horizon: int) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for i, t in enumerate(pf.as_of):
        for s in range(int(t) + 1, min(int(t) + horizon + 1, len(out) + horizon + 2)):
            out.setdefault(s, list(pf.names[i]))
    return out


def max_drawdown(r: np.ndarray) -> float:
    if r.size == 0:
        return float("nan")
    curve = np.exp(np.cumsum(r))
    return float((curve / np.maximum.accumulate(curve) - 1.0).min())


def paired_block_bootstrap(a: np.ndarray, b: np.ndarray, block: int, n_boot: int,
                            seed: int = 20260928) -> np.ndarray:
    """Bootstrap the drawdown difference with a SHARED resampled day index.

    Resampling the index jointly is the whole point: the two books are long
    the same market, so the null has to contain that. Independent resampling
    would let the market's own path differences leak into the statistic and
    inflate significance.
    """
    rng = np.random.default_rng(seed)
    n = a.size
    n_blocks = int(np.ceil(n / block))
    diffs = np.empty(n_boot)
    starts_max = n - block
    for k in range(n_boot):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
        diffs[k] = max_drawdown(a[idx]) - max_drawdown(b[idx])
    return diffs


def tail_metrics(r: np.ndarray) -> dict:
    if r.size < 30:
        return {}
    return {
        "worst_day": float(np.min(r)),
        "p05": float(np.percentile(r, 5)),
        "p01": float(np.percentile(r, 1)),
        "vol": float(r.std(ddof=1) * np.sqrt(252)),
    }


def main() -> int:
    start, end = date(2016, 1, 4), date(2020, 12, 31)
    horizon = HORIZON
    panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
    feats = P.compute_features(panel)
    mask = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
    fwd = P.forward_returns(panel, horizon)
    scores = F.production_score(feats, mask)
    as_of = P.study_as_of(panel, horizon, start)
    print(f"非重疊期間 {len(as_of)}；eligible 中位 {np.median(mask.sum(1)):.0f} 檔")
    print()

    # ---- book 1: production Top-20 ----
    pf = M.build_portfolio(scores, fwd, mask, horizon=horizon, top_k=TOP_K, as_of=as_of)
    book = held_daily_curve(panel, holds_from_picks(pf, horizon), horizon)

    # ---- book 2: equal-weight ALL eligible names, same 20-day schedule ----
    holds_uni: dict[int, list[int]] = {}
    for t in as_of:
        cols = [int(j) for j in np.flatnonzero(mask[t])]
        if len(cols) < 100:
            continue
        for s in range(int(t) + 1, min(int(t) + horizon + 1, panel.close.shape[0])):
            holds_uni.setdefault(s, cols)
    uni = held_daily_curve(panel, holds_uni, horizon)

    idx = np.isfinite(book) & np.isfinite(uni)
    book, uni = book[idx], uni[idx]
    print(f"共同持有日 {book.size} 天（兩條路徑完全對齊）")
    print()

    print("=" * 84)
    print("1. 兩條路徑的風險剖面（同一 20 日再平衡、同一資格閘門）")
    print("=" * 84)
    tb, tu = tail_metrics(book), tail_metrics(uni)
    print(f"{'路徑':<26}{'年化':>8}{'波動':>8}{'Sharpe':>8}{'最大回撤':>10}"
          f"{'最差單日':>10}{'1%尾':>9}")
    print("-" * 84)
    for label, r, t in (("生產 Top-20", book, tb), ("全宇宙等權 20 日再平衡", uni, tu)):
        curve = np.exp(np.cumsum(r))
        ann = float(curve[-1] ** (252.0 / r.size) - 1.0)
        print(f"{label:<26}{ann*100:>7.1f}%{t['vol']*100:>7.1f}%{ann/t['vol']:>8.2f}"
              f"{max_drawdown(r)*100:>9.1f}%{t['worst_day']*100:>9.2f}%{t['p01']*100:>8.2f}%")
    print()

    obs = max_drawdown(book) - max_drawdown(uni)
    print(f"觀察到的回撤差 (Top20 − 宇宙) = {obs*100:+.1f} pp  "
          f"（正值 = Top-20 比較淺）")
    print()

    print("=" * 84)
    print(f"2. 配對 moving-block bootstrap（block={BLOCK}, n={N_BOOT}，共用重抽樣日索引）")
    print("=" * 84)
    diffs = paired_block_bootstrap(book, uni, BLOCK, N_BOOT)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    p_not_better = float((diffs <= 0).mean())
    print(f"  bootstrap 95% 區間: [{lo*100:+.1f}, {hi*100:+.1f}] pp")
    print(f"  P(差值 <= 0) = {p_not_better:.3f}")
    print(f"  中位數差值 {np.median(diffs)*100:+.1f} pp")
    print()
    if lo > 0:
        print("  → 區間entirely 在 0 以上：回撤變淺在重抽樣市場路徑下成立。")
        verdict = "SUPPORTED"
    else:
        print("  → 區間跨過 0：回撤差異與市場噪音無法區分。")
        verdict = "NOT ESTABLISHED"
    print()

    print("=" * 84)
    print("3. 分 regime（R15）— 7.2pp 差如果只存在於單一 regime，那是 beta 不是風控")
    print("=" * 84)
    mkt = P.cross_sectional_median_log_return(panel)
    reg = M.label_regimes(mkt)
    trend, volr = reg.trend, reg.vol
    print(f"{'regime':<22}{'天數':>7}{'Top-20回撤':>12}{'宇宙回撤':>11}{'差':>9}")
    print("-" * 84)
    for tv, tl in ((+1, "牛市"), (-1, "熊市")):
        for vv, vl in ((+1, "高波動"), (-1, "低波動")):
            # trend/vol are full-panel length while book/uni have been
            # compressed by `idx`; align them before combining, or the
            # boolean mask silently mismatches (this threw once).
            tr, vo = trend[idx], volr[idx]
            m = (tr == tv) & (vo == vv)
            if m.sum() < 20:
                print(f"{tl + '/' + vl:<22}{int(m.sum()):>7}{'樣本不足':>12}{'':>11}{'':>9}")
                continue
            db, du = max_drawdown(book[m]), max_drawdown(uni[m])
            print(f"{tl + '/' + vl:<22}{int(m.sum()):>7}{db*100:>11.1f}%"
                  f"{du*100:>10.1f}%{(db-du)*100:>8.1f}%")
    print()
    print(f"判定: {verdict}")
    print(f"本輪變體數: 1（單一預先宣告的比較，無參數掃描）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
