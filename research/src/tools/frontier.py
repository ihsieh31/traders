"""The stability/upside frontier: what does buying the quietest stocks cost you?

The v1 screener delivers 51% of the universe's forward volatility and 3.4%/yr
in the holdout. Those two facts are the same fact. Low-volatility equities
systematically lag high-volatility equities -- that is the low-volatility
anomaly, and it is not an artefact of this dataset.

This script maps the frontier so the trade-off is a number rather than an
opinion. It sweeps ONE pre-declared parameter -- the weight on the low-vol
term -- and reports the whole curve. No cell is "the answer"; the point on
the curve is a product decision, not a statistical one.

Reporting the entire curve is also the only honest form here (plan.md R5):
a single cell that looked best would be a spike, and picking it would be
exactly the overfitting the project spent seven rounds documenting.

Read research/plan.md and research/process.md before changing anything here.
"""

from __future__ import annotations

import sys
from datetime import date

import numpy as np

from research.src import factors as F
from research.src import measure as M
from research.src import panel as P
from research.src.tools.stability_screen import forward_vol, risk

TRUSTED = "research/out/trusted_symbols.txt"
TOP_K = 20

# Pre-declared grid on the one free parameter. w=0.5 is the shipped v1.
W_LOWVOL = (0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0)
W_TREND = 0.15          # fixed across the sweep
W_ADV = 0.10            # fixed across the sweep


def score_at(feats, mask, w_lowvol, *, min_n=20):
    n_t, n_s = mask.shape
    out = np.full((n_t, n_s), np.nan)
    w_rest = 1.0 - w_lowvol
    for t in range(n_t):
        cols = np.flatnonzero(mask[t])
        if cols.size < min_n:
            continue
        p_vol = np.asarray(F.percentiles_fast(feats.vol20[t][cols]))
        p_tr = np.asarray(F.percentiles_fast(feats.trend[t][cols]))
        p_adv = np.asarray(F.percentiles_fast(feats.adv20[t][cols]))
        if w_rest <= 1e-12:
            out[t, cols] = 100.0 * (1.0 - p_vol)
        else:
            scale_tr, scale_adv = w_rest * (1 - W_TREND), w_rest * W_ADV
            out[t, cols] = 100.0 * (
                w_lowvol * (1.0 - p_vol)
                + scale_tr * p_tr
                + scale_adv * p_adv
            )
    return out


def main() -> int:
    horizon = 20
    print("=" * 96)
    print("穩定 / 上升空間的取捨曲線")
    print("w = 低波動項的權重。w=1 買最安靜的股票，w=0 買最吵的。")
    print("整條曲線都報，不挑任何一格作為「最佳」（plan.md R5）")
    print("=" * 96)
    print()

    for label, start, end in (
        ("探索期 2016-2020", date(2016, 1, 4), date(2020, 12, 31)),
        ("驗證期 2021-2025", date(2021, 1, 4), date(2025, 12, 31)),
    ):
        panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
        feats = P.compute_features(panel)
        mask = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
        fwd = P.forward_returns(panel, horizon)
        fvol = forward_vol(panel, horizon)
        as_of = P.study_as_of(panel, horizon, start)

        uni = []
        for t in as_of:
            cols = np.flatnonzero(mask[t])
            uni += [fvol[t][j] for j in cols if np.isfinite(fvol[t][j])]
        uni_med = float(np.median(uni))

        print("-" * 96)
        print(f"{label}｜非重疊期間 {len(as_of)} 個｜全宇宙前瞻波動中位 {uni_med*100:.1f}%")
        print("-" * 96)
        print(f"{'w(低波動)':>9}{'前瞻波動':>10}{'/宇宙':>8}{'淨@1.0x':>10}"
              f"{'@2.0x':>8}{'NW t':>7}{'Sharpe':>8}{'回撤':>9}{'有效部位':>10}")
        for w in W_LOWVOL:
            sc = score_at(feats, mask, w)
            pf = M.build_portfolio(sc, fwd, mask, horizon=horizon,
                                   top_k=TOP_K, as_of=as_of)
            ct = M.cost_table(pf, horizon)
            rp = risk(panel, pf, horizon)
            dv = M.diversification(pf, panel, horizon)
            sel = []
            for t in as_of:
                cols = np.flatnonzero(mask[t])
                order = np.lexsort((cols, -sc[t][cols]))
                sel += [fvol[t][j] for j in cols[order][:TOP_K]
                        if np.isfinite(fvol[t][j])]
            med = float(np.median(sel))
            mark = "  ← v1" if abs(w - 0.5) < 1e-9 else ""
            print(f"{w:>9.2f}{med*100:>9.1f}%{med/uni_med*100:>7.0f}%"
                  f"{ct['base']['annualised_net']*100:>9.1f}%"
                  f"{ct['2.0x']['annualised_net']*100:>7.1f}%"
                  f"{ct['base']['nw_t_net']:>7.2f}{rp.get('sr', float('nan')):>8.2f}"
                  f"{rp.get('mdd', float('nan'))*100:>8.1f}%"
                  f"{dv.get('effective_positions', float('nan')):>10.2f}{mark}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
