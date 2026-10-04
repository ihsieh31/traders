"""穩定度篩選器 — 找 20 檔「穩定且有潛力」的股票

目標（照需求定義，不要求賺錢）
----------------------------
- **穩定**：前瞻實現波動顯著低於宇宙
- **有一定潛力**：站上均線、有趨勢，不是廢股
- **可交易**：過生產資格閘門

這不是一個賺錢的公式，也不宣稱是。它是一個**風險特徵篩選器**，
而風險特徵正是這個專案裡唯一被量到 t > 30 的訊號
（score 預測前瞻波動 IC −0.22, t = −34.71，見 F-20260928-08）。

公式
----
    Score = 0.50·(1 − p_cs(vol20))     低波動 → 穩定
          + 0.30·p_cs(trend)            close/MA20 − 1 → 不是廢股
          + 0.20·p_cs(adv20)            流動性 → 大戶可參與

三個成分，全部是生產 `compute_features` 已在算的欄位，
不新增任何資料需求、不新增任何 look-ahead。
橫斷面母體 = 生產資格宇宙（與生產一致，見 process.md S-12）。

權重是事前寫死的（plan.md R4 允許的第三類：事前寫死的權重集合），
本輪不掃權重、不挑組態 —— 掃了就是在雜訊裡挑尖峰。

Read research/plan.md and research/process.md before changing anything here.
"""

from __future__ import annotations

import sys
from datetime import date

import numpy as np

from research.src import factors as F
from research.src import measure as M
from research.src import panel as P

TRUSTED = "research/out/trusted_symbols.txt"
TOP_K = 20
WEIGHTS = {"low_vol": 0.50, "trend": 0.30, "adv20": 0.20}


def stability_score(features: P.FeatureSet, mask: np.ndarray,
                    *, min_n: int = 20) -> np.ndarray:
    """Stable + some-potential score, ranked inside the eligible set only."""
    n_t, n_s = mask.shape
    out = np.full((n_t, n_s), np.nan)
    for t in range(n_t):
        cols = np.flatnonzero(mask[t])
        if cols.size < min_n:
            continue
        p_vol = np.asarray(F.percentiles_fast(features.vol20[t][cols]))
        p_trend = np.asarray(F.percentiles_fast(features.trend[t][cols]))
        p_adv = np.asarray(F.percentiles_fast(features.adv20[t][cols]))
        out[t, cols] = 100.0 * (
            WEIGHTS["low_vol"] * (1.0 - p_vol)
            + WEIGHTS["trend"] * p_trend
            + WEIGHTS["adv20"] * p_adv
        )
    return out


def forward_vol(panel: P.Panel, horizon: int) -> np.ndarray:
    """Annualised stdev of daily log returns over days t+1..t+horizon."""
    n_t = panel.close.shape[0]
    out = np.full(panel.close.shape, np.nan)
    if n_t < horizon + 2:
        return out
    with np.errstate(invalid="ignore", divide="ignore"):
        lr = np.diff(np.log(panel.close), axis=0)
    for t in range(0, n_t - horizon - 1):
        out[t] = lr[t: t + horizon].std(axis=0, ddof=1) * np.sqrt(252)
    return out


def daily_curve(panel, holds: dict[int, list[int]]) -> np.ndarray:
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


def risk(panel, pf, horizon):
    holds: dict[int, list[int]] = {}
    for i, t in enumerate(pf.as_of):
        for s in range(int(t) + 1, min(int(t) + horizon + 1, panel.close.shape[0])):
            holds.setdefault(s, list(pf.names[i]))
    r = daily_curve(panel, holds)
    r = r[np.isfinite(r)]
    if r.size < 30:
        return {}
    curve = np.exp(np.cumsum(r))
    vol = float(r.std(ddof=1) * np.sqrt(252))
    ann = float(curve[-1] ** (252.0 / r.size) - 1.0)
    mdd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    return {"ann": ann, "vol": vol, "sr": ann / vol if vol > 0 else np.nan,
            "mdd": mdd, "worst": float(r.min())}


def run_period(start, end, horizon=20) -> dict:
    """Everything the two demands are judged on, for one period."""
    panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
    feats = P.compute_features(panel)
    prod = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
    fwd = P.forward_returns(panel, horizon)
    fvol = forward_vol(panel, horizon)
    as_of = P.study_as_of(panel, horizon, start)

    stable = stability_score(feats, prod)
    production = F.production_score(feats, prod)
    out: dict = {"as_of": as_of, "n_periods": len(as_of)}

    for key, sc in (("stable", stable), ("production", production)):
        pf = M.build_portfolio(sc, fwd, prod, horizon=horizon, top_k=TOP_K, as_of=as_of)
        ct = M.cost_table(pf, horizon)
        rp = risk(panel, pf, horizon)
        dv = M.diversification(pf, panel, horizon)
        sel, uni = [], []
        for t in as_of:
            cols = np.flatnonzero(prod[t])
            order = np.lexsort((cols, -sc[t][cols]))
            sel += [fvol[t][j] for j in cols[order][:TOP_K] if np.isfinite(fvol[t][j])]
            uni += [fvol[t][j] for j in cols if np.isfinite(fvol[t][j])]
        out[key] = {
            "fvol_med": float(np.median(sel)),
            "fvol_q01": float(np.percentile(sel, 1)),
            "uni_fvol_med": float(np.median(uni)),
            "net_1x": ct["base"]["annualised_net"],
            "net_2x": ct["2.0x"]["annualised_net"],
            "nw_t": ct["base"]["nw_t_net"],
            "sharpe": rp.get("sr", float("nan")),
            "vol": rp.get("vol", float("nan")),
            "mdd": rp.get("mdd", float("nan")),
            "worst": rp.get("worst", float("nan")),
            "turnover": float(np.nanmean(pf.turnover)),
            "eff_n": dv.get("effective_positions", float("nan")),
            "corr": dv.get("mean_pairwise_corr", float("nan")),
        }
    return out


def validate() -> int:
    """Holdout check: 2021-2025 was never read while the weights were fixed."""
    print("=" * 92)
    print("驗證期檢查 — 權重 0.50/0.30/0.20 事前寫死，2021–2025 未參與任何選擇")
    print("=" * 92)
    print()
    disc = run_period(date(2016, 1, 4), date(2020, 12, 31))
    hold = run_period(date(2021, 1, 4), date(2025, 12, 31))
    print(f"探索期 66 個期間 / 驗證期 {hold['n_periods']} 個期間")
    print()
    print("【需求 1：穩定】前瞻實現波動中位數")
    print("-" * 92)
    print(f"{'':<26}{'探索期':>12}{'驗證期':>12}{'驗證期/宇宙':>14}")
    for key, label in (("production", "現行生產 score"), ("stable", "穩定度篩選器 v1")):
        d, h = disc[key], hold[key]
        print(f"{label:<26}{d['fvol_med']*100:>11.1f}%{h['fvol_med']*100:>11.1f}%"
              f"{h['fvol_med']/h['uni_fvol_med']*100:>13.0f}%")
    print(f"{'（全宇宙基準）':<26}{disc['stable']['uni_fvol_med']*100:>11.1f}%"
          f"{hold['stable']['uni_fvol_med']*100:>11.1f}%")
    print()
    print("【需求 2：有一定潛力】報酬只回報，不作為通過條件")
    print("-" * 92)
    print(f"{'':<26}{'淨@1.0x':>11}{'淨@2.0x':>10}{'NW t':>8}{'Sharpe':>9}")
    for key, label, r in (("production", "現行 score（探索期）", disc["production"]),
                          ("stable", "穩定度篩選器（探索期）", disc["stable"]),
                          ("production", "現行 score（驗證期）", hold["production"]),
                          ("stable", "穩定度篩選器（驗證期）", hold["stable"])):
        print(f"{label:<26}{r['net_1x']*100:>10.1f}%{r['net_2x']*100:>9.1f}%"
              f"{r['nw_t']:>8.2f}{r['sharpe']:>9.2f}")
    print()
    print("【交付給 risk_manager】")
    print("-" * 92)
    print(f"{'':<26}{'波動':>9}{'回撤':>9}{'最差單日':>10}{'有效部位':>10}{'相關':>8}{'換手':>7}")
    for key, label, r in (("production", "現行 score（驗證期）", hold["production"]),
                          ("stable", "穩定度篩選器（驗證期）", hold["stable"])):
        print(f"{label:<26}{r['vol']*100:>8.1f}%{r['mdd']*100:>8.1f}%"
              f"{r['worst']*100:>9.2f}%{r['eff_n']:>10.2f}{r['corr']:>8.3f}"
              f"{r['turnover']:>7.2f}")
    print()
    h_st, h_pr = hold["stable"], hold["production"]
    print("【驗證期結論】")
    print("-" * 92)
    checks = [
        ("穩定性在驗證期維持（篩選器波動 < 生產 score 波動）",
         h_st["fvol_med"] < h_pr["fvol_med"]),
        ("組合波動低於生產 score", h_st["vol"] < h_pr["vol"]),
        ("最大回撤不比生產 score 差", h_st["mdd"] >= h_pr["mdd"]),
        ("2.0x 成本下淨年化 > 0（可交易）", h_st["net_2x"] > 0),
        ("Sharpe 不低於生產 score", h_st["sharpe"] >= h_pr["sharpe"]),
    ]
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print()
    print("注意：這 5 條是**風險與可交易性**的條件，不是 alpha 條件（plan.md R10 不適用）。")
    print("報酬數字照報，但依需求不作為通過依據。")
    return 0


def main() -> int:
    if "--validate" in sys.argv:
        return validate()
    start, end = date(2016, 1, 4), date(2020, 12, 31)
    horizon = 20
    panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
    feats = P.compute_features(panel)
    prod = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
    wide = P.eligible_mask(panel, feats, min_adv20_usd=None)
    fwd = P.forward_returns(panel, horizon)
    fvol = forward_vol(panel, horizon)
    as_of = P.study_as_of(panel, horizon, start)

    stable = stability_score(feats, prod)
    production = F.production_score(feats, prod)

    print("=" * 92)
    print("穩定度篩選器 v1  vs  現行生產 score")
    print(f"探索期 {start} .. {end}｜持有 {horizon} 日｜非重疊期間 {len(as_of)} 個")
    print("=" * 92)
    print()

    # ---- 需求 1：穩定（前瞻實現波動）----
    print("【需求 1】穩定 —— 選出來的股票，前瞻實現波動有沒有比較低？")
    print("-" * 92)
    print(f"{'選法':<30}{'前瞻波動中位':>14}{'Q1（最穩的一檔）':>20}{'全宇宙中位':>12}")
    rows = []
    for label, sc, mask in (("現行生產 score", production, prod),
                            ("穩定度篩選器 v1", stable, prod),
                            ("穩定度篩選器（寬宇宙）", stability_score(feats, wide), wide)):
        pf = M.build_portfolio(sc, fwd, mask, horizon=horizon, top_k=TOP_K, as_of=as_of)
        sel, uni = [], []
        for t in as_of:
            cols = np.flatnonzero(mask[t])
            order = np.lexsort((cols, -sc[t][cols]))
            chosen = [int(j) for j in cols[order][:TOP_K]]
            sel += [fvol[t][j] for j in chosen if np.isfinite(fvol[t][j])]
            uni += [fvol[t][j] for j in cols if np.isfinite(fvol[t][j])]
        sel, uni = np.array(sel), np.array(uni)
        rows.append((label, sel, uni))
        print(f"{label:<30}{np.median(sel)*100:>13.1f}%{np.percentile(sel,1)*100:>19.1f}%"
              f"{np.median(uni)*100:>11.1f}%")
    _, sel_v1, uni_all = rows[1]   # rows[1] = (label, selected, universe)
    print()
    print(f"→ 穩定度篩選器的前瞻波動中位數是全宇宙的 "
          f"{np.median(sel_v1)/np.median(uni_all)*100:.0f}%；"
          f"最穩那一檔（Q1）低 {(1-np.percentile(sel_v1,1)/np.percentile(uni_all,1))*100:.0f}%")
    print()

    # ---- 需求 2：有一定潛力（不是廢股，也不要求賺）----
    print("【需求 2】有一定潛力 —— 報酬只是回報，不作為通過條件")
    print("-" * 92)
    print(f"{'選法':<30}{'淨年化@1.0x':>13}{'@2.0x':>9}{'NW t':>8}{'Sharpe':>9}")
    for label, sc, mask in (("現行生產 score", production, prod),
                            ("穩定度篩選器 v1", stable, prod)):
        pf = M.build_portfolio(sc, fwd, mask, horizon=horizon, top_k=TOP_K, as_of=as_of)
        ct = M.cost_table(pf, horizon)
        rp = risk(panel, pf, horizon)
        print(f"{label:<30}{ct['base']['annualised_net']*100:>12.1f}%"
              f"{ct['2.0x']['annualised_net']*100:>8.1f}%"
              f"{ct['base']['nw_t_net']:>8.2f}{rp.get('sr', float('nan')):>9.2f}")
    print()

    # ---- 交付給下游的東西 ----
    print("【交付】risk_manager 需要的輸入")
    print("-" * 92)
    pf_v1 = M.build_portfolio(stable, fwd, prod, horizon=horizon, top_k=TOP_K, as_of=as_of)
    pf_prod = M.build_portfolio(production, fwd, prod, horizon=horizon, top_k=TOP_K, as_of=as_of)
    print(f"{'選法':<30}{'年化波動':>10}{'最大回撤':>10}{'最差單日':>10}{'有效部位':>10}{'換手':>8}")
    for label, pf in (("現行生產 score", pf_prod), ("穩定度篩選器 v1", pf_v1)):
        rp = risk(panel, pf, horizon)
        dv = M.diversification(pf, panel, horizon)
        print(f"{label:<30}{rp['vol']*100:>9.1f}%{rp['mdd']*100:>9.1f}%"
              f"{rp['worst']*100:>9.2f}%{dv.get('effective_positions', float('nan')):>10.2f}"
              f"{np.nanmean(pf.turnover):>8.2f}")
    print()

    # ---- 這個公式現在怎麼算 ----
    print("【公式】可以直接寫進 metrics.py")
    print("-" * 92)
    print("""
  score = 100 * ( 0.50*(1 - p_cs(vol20))     # 低波動 → 穩定
                + 0.30* p_cs(trend)          # close/MA20-1 > 0 → 不是廢股
                + 0.20* p_cs(adv20) )         # 流動性

  母體 = 生產資格宇宙（price>=5 且 ADV20>=$20M），與生產一致
  輸出 = 前 20 檔等權，每檔附 vol20 / trend / adv20 給 risk_manager sizing
""")
    print("三個成分全部是生產已在算的欄位，無新資料需求、無 look-ahead。")
    print("權重事前寫死、不掃描 —— 掃了就是在雜訊裡挑尖峰（R5）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
