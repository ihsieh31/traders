"""Follow-up: benchmark the score against correctly-matched alternatives.

Two things the previous round left open, both of which change the conclusion:

1. **The benchmark was mismatched.** CRSP ``Mkt-RF`` is a cap-weighted total
   market return, net of the risk-free rate, with no trading costs. Our Top-20
   is an equal-weighted 20-stock book, gross of the risk-free rate, net of
   costs, on a liquidity-filtered universe. Subtracting one from the other
   produced a confident "we trail the index by 9.3pp/yr" that was largely an
   artefact of comparing a cap-weighted excess return against an
   equal-weighted median. The honest comparison is against like-for-like
   equal-weighted alternatives on the SAME universe.

2. **The quintile spread was reported without a test.** Q1 10.8% -> Q5 21.6%
   is a 10.8pp/yr spread on 66 periods. At this project's own sample sizes
   that is exactly the shape of a number that is indistinguishable from zero
   (plan.md R16-Q1). It needs a t before it can be believed.

Neither of these is a new idea. Both are the same discipline the project
already wrote down, applied to the result that was about to be believed.

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
TOP_K = 20


def portfolio_from_sorts(
    panel, mask, fwd, as_of, order_fn
) -> M.Portfolio:
    """Equal-weight book built from a per-session ordering function."""
    picks, gross, turn, nav, per_name = [], [], [], [], {}
    previous = None
    missing = 0
    for t in as_of:
        cols = np.flatnonzero(mask[t])
        if cols.size < 100:
            picks.append([])
            continue
        chosen = [int(j) for j in order_fn(t, cols)]
        picks.append(chosen)
        rets = [fwd[t][j] for j in chosen if np.isfinite(fwd[t][j])]
        nav.append(len(rets))
        if not rets:
            missing += 1
            previous = set(chosen)
            continue
        gross.append(float(np.mean(rets)))
        for j, r in zip(chosen, rets):
            per_name[j] = per_name.get(j, 0.0) + float(r) / len(rets)
        cur = set(chosen)
        turn.append(1.0 if previous is None else 0.5 * (2.0 * (TOP_K - len(cur & previous)) / TOP_K))
        previous = cur
    return M.Portfolio(
        as_of=np.asarray(as_of, dtype=int), names=picks,
        gross=np.array(gross), turnover=np.array(turn),
        n_available=np.array(nav), n_missing_periods=missing, per_name=per_name,
    )


def daily_curve(panel, pf: M.Panel, horizon: int = 20) -> np.ndarray:
    n_t = panel.close.shape[0]
    holding = [[] for _ in range(n_t)]
    for i, t in enumerate(pf.as_of):
        for s in range(int(t) + 1, min(int(t) + horizon + 1, n_t)):
            holding[s] = list(pf.names[i])
    with np.errstate(invalid="ignore", divide="ignore"):
        lr = np.diff(np.log(panel.close), axis=0,
                     prepend=np.full((1, panel.close.shape[1]), np.nan))
    out = np.full(n_t, np.nan)
    for s in range(1, n_t):
        names = sorted({j for j in holding[s] if j < panel.close.shape[1]})
        if not names:
            continue
        v = lr[s][names]
        v = v[np.isfinite(v)]
        if v.size:
            out[s] = float(np.mean(v))
    return out


def risk(panel, pf, horizon=20):
    r = daily_curve(panel, pf, horizon)
    r = r[np.isfinite(r)]
    if r.size < 30:
        return {}
    curve = np.exp(np.cumsum(r))
    vol = float(r.std(ddof=1) * np.sqrt(252))
    ann = float(curve[-1] ** (252.0 / r.size) - 1.0)
    mdd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    return {"ann": ann, "vol": vol, "sr": ann / vol if vol > 0 else np.nan, "mdd": mdd}


def main() -> int:
    start, end = date(2016, 1, 4), date(2020, 12, 31)
    horizon = HORIZON
    panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
    feats = P.compute_features(panel)
    mask = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
    fwd = P.forward_returns(panel, horizon)
    scores = F.production_score(feats, mask)
    as_of = P.study_as_of(panel, horizon, start)
    print(f"非重疊期間 {len(as_of)} 個；eligible 中位 {np.median(mask.sum(1)):.0f} 檔")
    print()

    # ---------------- 1. like-for-like benchmarks ----------------
    print("=" * 88)
    print("1. 基準線（同一宇宙、同一資格閘門、同一 20 日窗口）")
    print("=" * 88)
    uni = []
    for t in as_of:
        cols = np.flatnonzero(mask[t])
        r = [fwd[t][j] for j in cols if np.isfinite(fwd[t][j])]
        if r:
            uni.append(float(np.mean(r)))  # equal-weighted MEAN of all eligible
    uni = np.array(uni)
    mkt_med = P.cross_sectional_median_log_return(panel)
    mkt_med = mkt_med[np.isfinite(mkt_med)]
    curve = np.exp(np.cumsum(mkt_med))
    med_ann = float(curve[-1] ** (252.0 / mkt_med.size) - 1.0)
    med_vol = float(mkt_med.std(ddof=1) * np.sqrt(252))
    med_mdd = float((curve / np.maximum.accumulate(curve) - 1.0).min())

    print(f"{'基準':<34}{'年化':>9}{'波動':>8}{'Sharpe':>9}{'最大回撤':>10}")
    print("-" * 88)
    print(f"{'全宇宙等權平均（扣換手成本前）':<34}"
          f"{np.nanmean(uni)*(250/horizon)*100:>8.1f}%{'':>8}{'':>9}{'':>10}")
    print(f"{'全宇宙中位數（日報酬複合）':<34}"
          f"{med_ann*100:>8.1f}%{med_vol*100:>7.1f}%{med_ann/med_vol:>9.2f}{med_mdd*100:>9.1f}%")
    print(f"{'CRSP Mkt-RF（市值加權、超額）':<34}"
          f"{16.0:>8.1f}%{'':>8}{0.82:>9.2f}{'':>10}")
    print()
    print("注意：三者不是同一個東西。市值加權 vs 等權中位 在大盤牛市裡差距很大，")
    print("      而我們的量測路徑不含交易成本、也不扣無風險利率。不可直接相減。")
    print()

    # ---------------- 2. does the quintile spread survive a t? ----------------
    print("=" * 88)
    print("2. 五檔報酬差的顯著性（66 個期間，這是能不能引用的關鍵）")
    print("=" * 88)
    per_q = {q: [] for q in range(1, 6)}
    for t in as_of:
        cols = np.flatnonzero(mask[t])
        if cols.size < 100:
            continue
        order = np.lexsort((cols, -scores[t][cols]))
        n = len(order)
        edges = [int(n * k / 5) for k in range(6)]
        for q in range(1, 6):
            sel = [int(cols[i]) for i in order[edges[q - 1]:edges[q]]]
            r = [fwd[t][j] for j in sel if np.isfinite(fwd[t][j])]
            if r:
                per_q[q].append(float(np.mean(r)))
    for q in range(1, 6):
        per_q[q] = np.array(per_q[q])
    q1, q5 = per_q[1], per_q[5]
    spread = q5 - q1
    m1, ms = M.newey_west(spread, M.nw_lag(horizon, horizon))
    ci = M.confidence_interval(spread)
    # confidence_interval returns "width" and a precomputed ratio; R16-Q1
    # wants width/|effect| and calls it the thing that decides whether any
    # downstream statistic means anything.
    ratio = ci.get("width_over_abs_mean")
    print(f"  Q1(最高分) 平均/期 {q1.mean()*100:+.3f}%   n={q1.size}")
    print(f"  Q5(最低分) 平均/期 {q5.mean()*100:+.3f}%   n={q5.size}")
    print(f"  價差 Q5−Q1      {m1*100:+.3f}%/期  → 年化 {m1*(250/horizon)*100:+.1f}%")
    print(f"  Newey-West t    {ms:+.2f}")
    print(f"  95% CI          [{ci['ci_low']*100:+.3f}, {ci['ci_high']*100:+.3f}]%/期  "
          f"寬度/效應 = {ratio:.2f}")
    print()
    if abs(ms) < 2.0 or (ratio is not None and ratio > 2.0):
        print("  → 價差方向是『低分檔賺更多』，但 t 與 CI 都不支持。")
        print("    依 R16-Q1 讀法，這份資料不能分辨它與 0。**不可引用。**")
    print()

    # ---------------- 3. risk profile of the book vs the universe ----------------
    print("=" * 88)
    print("3. 生產 Top-20 的風險 vs 全宇宙")
    print("=" * 88)
    pf = M.build_portfolio(scores, fwd, mask, horizon=horizon, top_k=TOP_K, as_of=as_of)
    rp = risk(panel, pf, horizon)
    ct = M.cost_table(pf, horizon)
    print(f"  Top-20        年化(成本後) {ct['base']['annualised_net']*100:>6.1f}%  "
          f"日序列年化 {rp['ann']*100:>5.1f}%  波動 {rp['vol']*100:>5.1f}%  "
          f"Sharpe {rp['sr']:>5.2f}  回撤 {rp['mdd']*100:>6.1f}%")
    print(f"  全宇宙中位數   年化 {med_ann*100:>5.1f}%  "
          f"{'':>13}  波動 {med_vol*100:>5.1f}%  "
          f"Sharpe {med_ann/med_vol:>5.2f}  回撤 {med_mdd*100:>6.1f}%")
    print()
    print("  換手率（每 20 日）:", f"{np.nanmean(pf.turnover):.3f}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
