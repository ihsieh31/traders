"""You only get 20 LLM analyses a day. Make those 20 count.

The time budget settles the architecture: the screen cannot widen, because
per-symbol LLM research is the binding cost. So the 20 the screen emits are
the only equities the fundamental layer will ever see. That makes two numbers
the ones that matter, and neither of them is return:

  1. **effective positions** -- how many independent bets are in the 20.
     At 2.4-2.9 the LLM is choosing among three things wearing twenty hats.
  2. **cross-sectional dispersion of forward returns** -- if all 20 are going
     to return the same thing, the LLM's analysis is wasted no matter how
     good it is. This is the opportunity set, and it is the number that says
     whether the downstream research has anything to discriminate.

Diversification was rejected earlier for damaging portfolio Sharpe. That
verdict stands for a portfolio and does NOT transfer here: the portfolio is
the LLM's output, not the screener's. For a funnel, a wider spread of
outcomes is free upside -- it costs no LLM budget, because the count is
capped at 20 either way.

Method: greedy selection under a hard cap on average correlation to the
already-chosen names, keeping the stability screen's low-vol preference.
The whole curve over the cap is reported; no single cell is presented as
the answer (plan.md R5).

Read research/plan.md and research/process.md before changing anything here.
"""

from __future__ import annotations

import sys
from datetime import date

import numpy as np

from research.src import factors as F
from research.src import measure as M
from research.src import panel as P
from research.src.tools.stability_screen import forward_vol, stability_score, risk

TRUSTED = "research/out/trusted_symbols.txt"
TOP_K = 20
CORR_WINDOW = 60

# Pre-declared grid on the one new parameter: the correlation cap.
CORR_CAPS = (0.99, 0.50, 0.40, 0.35, 0.30, 0.25, 0.20)


def select(close, score_row, cols, t, *, cap, pool=200, top_k=TOP_K):
    """Greedy: highest score first, then reject anything too correlated.

    A hard cap is used rather than a penalty weight so the parameter has a
    direct meaning ("no two picks may average more than `cap` correlation")
    and the output is a clean, auditable constraint rather than a blend.
    """
    order = np.lexsort((cols, -score_row[cols]))
    pool_idx = [int(j) for j in cols[order][:pool]]
    if len(pool_idx) < top_k:
        return None
    if cap >= 0.99 or t - CORR_WINDOW + 1 < 0:
        return pool_idx[:top_k]
    block = close[t - CORR_WINDOW + 1: t + 1][:, np.array(pool_idx)]
    if not np.isfinite(block).all() or np.any(block <= 0):
        return pool_idx[:top_k]
    rets = np.diff(np.log(block), axis=0)
    sd = rets.std(axis=0, ddof=1)
    if not np.isfinite(sd).all() or np.any(sd == 0):
        return pool_idx[:top_k]
    corr = np.corrcoef(rets, rowvar=False)
    if not np.isfinite(corr).all():
        return pool_idx[:top_k]

    chosen = [0]
    remaining = sorted(range(1, len(pool_idx)),
                       key=lambda i: -score_row[pool_idx[i]])
    for i in remaining:
        if len(chosen) >= top_k:
            break
        if np.mean(corr[i, chosen]) <= cap:
            chosen.append(i)
    if len(chosen) < top_k:      # cap too tight: relax for the remainder
        for i in remaining:
            if len(chosen) >= top_k:
                break
            if i not in chosen:
                chosen.append(i)
    return [pool_idx[i] for i in chosen[:top_k]]


def build(close, scores, fwd, mask, *, horizon, as_of, cap):
    picks, gross, turn, nav, per_name = [], [], [], [], {}
    prev, missing = None, 0
    for t in as_of:
        cols = np.flatnonzero(mask[t])
        if cols.size < 100:
            picks.append([])
            continue
        chosen = select(close, scores[t], cols, int(t), cap=cap)
        if chosen is None:
            missing += 1
            continue
        picks.append(chosen)
        rs = [fwd[t][j] for j in chosen if np.isfinite(fwd[t][j])]
        nav.append(len(rs))
        if not rs:
            missing += 1
            prev = set(chosen)
            continue
        gross.append(float(np.mean(rs)))
        for j, r in zip(chosen, rs):
            per_name[j] = per_name.get(j, 0.0) + float(r) / len(rs)
        cur = set(chosen)
        turn.append(1.0 if prev is None else 0.5 * (2 * (TOP_K - len(cur & prev)) / TOP_K))
        prev = cur
    return M.Portfolio(as_of=np.asarray(as_of, dtype=int), names=picks,
                       gross=np.array(gross), turnover=np.array(turn),
                       n_available=np.array(nav), n_missing_periods=missing,
                       per_name=per_name)


def main() -> int:
    horizon = 20
    for label, start, end in (
        ("驗證期 2021-2025", date(2021, 1, 4), date(2025, 12, 31)),
        ("探索期 2016-2020", date(2016, 1, 4), date(2020, 12, 31)),
    ):
        panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
        feats = P.compute_features(panel)
        mask = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
        fwd = P.forward_returns(panel, horizon)
        fvol = forward_vol(panel, horizon)
        as_of = P.study_as_of(panel, horizon, start)
        stable = stability_score(feats, mask)
        production = F.production_score(feats, mask)

        print("=" * 100)
        print(f"{label}｜非重疊期間 {len(as_of)} 個｜LLM 預算固定 20 檔")
        print("=" * 100)
        print()
        print("「離散度」= 同一期 20 檔前瞻報酬的橫斷面標準差。LLM 只能分辨這個量。")
        print()
        print(f"{'選法':<26}{'有效部位':>9}{'平均相關':>10}{'離散度':>9}"
              f"{'前瞻波動':>10}{'淨@2.0x':>10}{'回撤':>9}")
        print("-" * 100)

        rows = []
        pf_p = M.build_portfolio(production, fwd, mask, horizon=horizon,
                                 top_k=TOP_K, as_of=as_of)
        rows.append(("現行生產 score", pf_p, production, 0.99))
        pf_s = M.build_portfolio(stable, fwd, mask, horizon=horizon,
                                 top_k=TOP_K, as_of=as_of)
        rows.append(("穩定度 v1 (w=0.5)", pf_s, stable, 0.99))
        for cap in CORR_CAPS[1:]:
            pf_c = build(panel.close, stable, fwd, mask,
                         horizon=horizon, as_of=as_of, cap=cap)
            rows.append((f"v1 + 相關上限 {cap:.2f}", pf_c, stable, cap))

        for name, pf, sc, cap in rows:
            dv = M.diversification(pf, panel, horizon)
            disp = []
            for i, t in enumerate(pf.as_of):
                rs = [fwd[t][j] for j in pf.names[i] if np.isfinite(fwd[t][j])]
                if len(rs) >= 5:
                    disp.append(float(np.std(rs)))
            sel = []
            for t in as_of:
                cols = np.flatnonzero(mask[t])
                order = np.lexsort((cols, -sc[t][cols]))
                sel += [fvol[t][j] for j in cols[order][:TOP_K]
                        if np.isfinite(fvol[t][j])]
            rp = risk(panel, pf, horizon)
            ct = M.cost_table(pf, horizon)
            print(f"{name:<26}{dv.get('effective_positions', float('nan')):>9.2f}"
                  f"{dv.get('mean_pairwise_corr', float('nan')):>10.3f}"
                  f"{np.mean(disp)*100:>8.2f}%{np.median(sel)*100:>9.1f}%"
                  f"{ct['2.0x']['annualised_net']*100:>9.1f}%"
                  f"{rp.get('mdd', float('nan'))*100:>8.1f}%")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
