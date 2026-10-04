"""Round: is the score a RISK signal, even though it is not a RETURN signal?

The premise
-----------
Every previous round asked the score to predict returns. It cannot, and
CRSP says the underlying effects were ~zero in this window anyway. So this
round asks a different question that does not require alpha to exist:

    **Does the production score carry usable information about RISK?**

If it does, the score still has a job -- not "find the 20 stocks that go up",
but "tell us which names carry the drawdown". That is a question about
volatility and downside, both of which are measurable on this panel without
any new data and without any external truth source.

Order of work
-------------
Cheapest check first (S-52). The single decisive number is the
cross-sectional IC between the score and FORWARD REALISED VOLATILITY. If that
is indistinguishable from zero, the whole premise dies here and nothing
further gets written. Only if it survives do we build a portfolio on it.

Explicitly not done here
------------------------
No new factor, no new weight, no grid. Three pre-declared quantities and one
pre-declared benchmark. Portfolio construction is out of scope (plan.md
R14/R19): this round measures whether the information exists and hands it
over, it does not turn it into a strategy.

Read research/plan.md and research/process.md before changing anything here.
"""

from __future__ import annotations

import sys
from datetime import date

import numpy as np

from research.src import common as C
from research.src import factors as F
from research.src import measure as M
from research.src import panel as P

HORIZON = 20
TRUSTED = "research/out/trusted_symbols.txt"


def forward_realised_vol(panel: P.Panel, horizon: int) -> np.ndarray:
    """Annualised stdev of the DAILY log returns inside the holding window.

    Measured over days t+1 .. t+horizon, i.e. exactly the days a position
    entered at open[t+1] is actually held. Using the period return's own
    standard deviation is not an option -- a period return is one number, so
    its "volatility" would need a second dimension of data we do not have.

    This is forward-looking BY CONSTRUCTION and is a LABEL, not a feature.
    It is only ever used on the right-hand side of an IC and never fed back
    into a score (plan.md R7).
    """
    n_t = panel.close.shape[0]
    out = np.full(panel.close.shape, np.nan)
    if n_t < horizon + 2:
        return out
    with np.errstate(invalid="ignore", divide="ignore"):
        lr = np.diff(np.log(panel.close), axis=0)
    for t in range(0, n_t - horizon - 1):
        block = lr[t : t + horizon]  # days t+1 .. t+horizon
        out[t] = block.std(axis=0, ddof=1) * np.sqrt(252)
    return out


def forward_worst_day(panel: P.Panel, horizon: int) -> np.ndarray:
    """Worst single-day log return inside the holding window (a drawdown proxy)."""
    n_t = panel.close.shape[0]
    out = np.full(panel.close.shape, np.nan)
    if n_t < horizon + 2:
        return out
    with np.errstate(invalid="ignore", divide="ignore"):
        lr = np.diff(np.log(panel.close), axis=0)
    for t in range(0, n_t - horizon - 1):
        out[t] = np.nanmin(lr[t : t + horizon], axis=0)
    return out


def cross_sectional_ic(
    values: np.ndarray, labels: np.ndarray, mask: np.ndarray, grid: np.ndarray
) -> np.ndarray:
    """Spearman IC per session, ties share average rank (production rule)."""
    out = np.full(len(grid), np.nan)
    for i, t in enumerate(grid):
        cols = np.flatnonzero(mask[t])
        if cols.size < 30:
            continue
        x = values[t][cols]
        y = labels[t][cols]
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 30:
            continue
        out[i] = C.spearman_ic(x[ok], y[ok])
    return out


def ic_summary(series: np.ndarray, horizon: int) -> dict:
    clean = series[np.isfinite(series)]
    if clean.size < 10:
        return {"n": int(clean.size)}
    mean = float(clean.mean())
    sd = float(clean.std(ddof=1))
    lag = M.nw_lag(horizon, max(1, horizon))
    _m, t = M.newey_west(clean, lag)
    return {
        "n": int(clean.size),
        "mean": mean,
        "t": float(t),
        "sr": mean / sd if sd > 0 else float("nan"),
        "positive_ratio": float((clean > 0).mean()),
    }


def main() -> int:
    start, end = date(2016, 1, 4), date(2020, 12, 31)
    horizon = HORIZON

    print("=" * 88)
    print(f"輪次：score 當作「風險訊號」來看（不是報酬訊號）")
    print(f"探索期 {start} .. {end}，持有 {horizon} 日，生產資格宇宙")
    print("=" * 88)
    print()

    panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
    feats = P.compute_features(panel)
    mask = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
    fwd = P.forward_returns(panel, horizon)
    scores = F.production_score(feats, mask)

    fwd_vol = forward_realised_vol(panel, horizon)
    fwd_worst = forward_worst_day(panel, horizon)

    # Overlapping daily grid for the IC (the IC is a factor-level statistic and
    # plan.md R8 puts factor IC on the overlapping sampling).
    first = int(P.study_as_of(panel, horizon, start)[0])
    grid = np.arange(first, len(panel.sessions) - horizon, dtype=int)
    print(f"IC 取樣日 {len(grid)}（重疊取樣，date_step=1，因子層用途）")
    print()

    # ---------------- TEST 1: the decisive number ----------------
    print("=" * 88)
    print("TEST 1（決定性）：score 能不能預測前瞻波動？")
    print("=" * 88)
    s_vol = ic_summary(cross_sectional_ic(scores, fwd_vol, mask, grid), horizon)
    s_worst = ic_summary(cross_sectional_ic(scores, fwd_worst, mask, grid), horizon)
    s_ret = ic_summary(cross_sectional_ic(scores, fwd, mask, grid), horizon)

    print(f"{'標的':<26}{'IC 均值':>10}{'NW t':>9}{'IC SR':>8}{'同號比例':>10}{'n':>7}")
    print("-" * 88)
    for label, s in [
        ("前瞻實現波動 fwd_vol", s_vol),
        ("前瞻最差單日 fwd_worst", s_worst),
        ("前瞻報酬 fwd_ret（對照）", s_ret),
    ]:
        print(
            f"{label:<26}{s['mean']:>+10.4f}{s['t']:>+9.2f}"
            f"{s['sr']:>8.2f}{s['positive_ratio']*100:>9.1f}%{s['n']:>7}"
        )
    print()

    if abs(s_vol["t"]) < 2.0:
        print("→ score 對前瞻波動沒有可用的 IC。風險訊號的前提在這裡就死了，")
        print("  後面的投資組合不必寫。")
        return 0

    # ---------------- TEST 2: score quintiles ----------------
    print("=" * 88)
    print("TEST 2：依 score 分五檔，看風險與報酬的剖面")
    print("=" * 88)
    as_of = grid[::horizon]
    buckets = {q: {"vol": [], "worst": [], "ret": []} for q in range(1, 6)}
    for t in as_of:
        cols = np.flatnonzero(mask[t])
        if cols.size < 100:
            continue
        vals = scores[t][cols]
        order = np.lexsort((cols, -vals))
        n = len(order)
        edges = [int(n * k / 5) for k in range(6)]
        for q in range(1, 6):
            sel = [int(cols[i]) for i in order[edges[q - 1] : edges[q]]]
            v = [fwd_vol[t][j] for j in sel if np.isfinite(fwd_vol[t][j])]
            w = [fwd_worst[t][j] for j in sel if np.isfinite(fwd_worst[t][j])]
            rr = [fwd[t][j] for j in sel if np.isfinite(fwd[t][j])]
            if v:
                buckets[q]["vol"].append(float(np.mean(v)))
            if w:
                buckets[q]["worst"].append(float(np.mean(w)))
            if rr:
                buckets[q]["ret"].append(float(np.mean(rr)))

    print(f"{'檔':<8}{'平均前瞻波動':>14}{'最差單日':>12}{'平均報酬/期':>13}")
    print("-" * 88)
    for q in range(1, 6):
        b = buckets[q]
        if not b["vol"]:
            continue
        vol = float(np.mean(b["vol"]))
        worst = float(np.mean(b["worst"])) if b["worst"] else float("nan")
        ret = float(np.mean(b["ret"])) if b["ret"] else float("nan")
        ann = ret * (M.PERIODS_PER_YEAR / horizon) if np.isfinite(ret) else float("nan")
        print(f"Q{q} (高→低){'':<2}{vol*100:>12.1f}%{worst*100:>11.2f}%{ann*100:>12.1f}%")
    print()

    # ---------------- TEST 3: the benchmark nobody has used ----------------
    print("=" * 88)
    print("TEST 3：市場基準（橫斷面中位數報酬 = 全宇宙等權）")
    print("=" * 88)
    mkt = P.cross_sectional_median_log_return(panel)
    mkt = mkt[np.isfinite(mkt)]
    curve = np.exp(np.cumsum(mkt))
    peak = np.maximum.accumulate(curve)
    vol = float(mkt.std(ddof=1) * np.sqrt(252))
    ann = float(curve[-1] ** (252.0 / mkt.size) - 1.0)
    mdd = float((curve / peak - 1.0).min())
    print(f"  年化報酬 {ann*100:>7.1f}%   年化波動 {vol*100:>6.1f}%   "
          f"Sharpe {ann/vol if vol>0 else float('nan'):>5.2f}   最大回撤 {mdd*100:>6.1f}%")
    print(f"  對照我們的 Top-20（先前一輪量到）: 6.7% / 20.0% / 0.34 / −28.7%")
    print()
    print("→ 判讀：把『買指數』當基準，而不是把 0 當基準。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
