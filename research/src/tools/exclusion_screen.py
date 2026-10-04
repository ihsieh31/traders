"""剔除式篩選器 — 濾掉垃圾股，剩下的取 20 個

規格（照需求）
--------------
1. **剔除垃圾股** — 硬規則，全部用 OHLCV，不需基本面、不需新資料
2. **找 20 個有潛力的** — 剩下的按「有向上動能但不是極端波動」排
3. **不做 alpha 排序** — 排序只是從存活者裡挑 20 個的填充規則，不是訊號

為什麼是剔除式而不是排序式
--------------------------
前面幾輪量到的核心事實：把波動當成「好」會主動殺掉離散度
（v1 離散度 3.91% vs 現行 score 8.57%），於是下游 LLM 拿到 20 個回報
幾乎一樣的名字，它的工作被浪費。剔除式避開這個陷阱 ——
它不對「好」下定義，只排除「壞」。

剔除規則（全部 point-in-time，只用 t 當日往前可得的資料）
----------------------------------------------------------
| # | 規則 | 欄位 | 理由 |
|---|---|---|---|
| E1 | `price < 5` | price | 生產資格閘門 |
| E2 | `ADV20 < $20M` | adv20 | 生產資格閘門 |
| E3 | `vol20 > 60%` | vol20 | 年化 60% ≈ 市場 3 倍。投機／困境／生技早期股都在這裡，不屬於「穩定且有潛力」 |
| E4 | `trend < 0` | trend | 跌破 20 日均線 = 落刀。剔除的股票裡如果有 20% 正在跌，LLM 看到的是恐慌不是潛力 |
| E5 | `r60 < -25%` | r60 | 三個月跌 25% = 已進入困境，不是「有潛力」 |

**刻意不做的事**：
- **不做「即將被併購」預測**。F-20260928-08 量到：會被 score 選中而後消失的
  16 檔退市股裡 **14 檔是併購型**（死前上漲、站在 60 日高點）。
  排除併購風險 = 排除自己的贏家。
- **不排序找 alpha**。排序只用來從存活者裡挑 20 個。

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

# Pre-declared thresholds. Not swept -- see the note above.
VOL_CEILING = 0.60      # E3
R60_FLOOR = -0.25       # E5
# Mirrors tradingagents.screening.metrics.TREND_EPSILON: a close equal to its
# 20-day mean is +/-1e-15 after rounding and must not pass E4 on noise.
TREND_EPSILON = 1e-12


def exclude_mask(panel, feats, prod, *, vol_ceiling=VOL_CEILING,
                 r60_floor=R60_FLOOR, require_trend=True):
    """Production eligibility MINUS the garbage rules."""
    keep = prod.copy()
    with np.errstate(invalid="ignore"):
        keep &= feats.vol20 <= vol_ceiling          # E3
        keep &= feats.r60 >= r60_floor              # E5
        if require_trend:
            keep &= feats.trend > TREND_EPSILON     # E4
    return keep


def survivor_score(feats, keep, *, min_n=20):
    """Filler ordering: how much upward movement, among survivors only.

    NOT an alpha signal. Its only job is to pick 20 deterministically out of
    however many survive, and to break ties by preferring names that are
    actually moving up rather than flat. Reported as a column so nobody
    mistakes it for the reason the names were chosen.
    """
    n_t, n_s = keep.shape
    out = np.full((n_t, n_s), np.nan)
    for t in range(n_t):
        cols = np.flatnonzero(keep[t])
        if cols.size < min_n:
            continue
        p_tr = np.asarray(F.percentiles_fast(feats.trend[t][cols]))
        p_r60 = np.asarray(F.percentiles_fast(feats.r60[t][cols]))
        p_vol = np.asarray(F.percentiles_fast(feats.vol20[t][cols]))
        out[t, cols] = 100.0 * (0.5 * p_tr + 0.3 * p_r60 + 0.2 * (1.0 - p_vol))
    return out


def main() -> int:
    horizon = 20
    for label, start, end in (
        ("驗證期 2021-2025", date(2021, 1, 4), date(2025, 12, 31)),
        ("探索期 2016-2020", date(2016, 1, 4), date(2020, 12, 31)),
    ):
        panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
        feats = P.compute_features(panel)
        prod = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
        keep = exclude_mask(panel, feats, prod)
        fwd = P.forward_returns(panel, horizon)
        fvol = forward_vol(panel, horizon)
        as_of = P.study_as_of(panel, horizon, start)

        surv = M.build_portfolio(survivor_score(feats, keep), fwd, keep,
                                 horizon=horizon, top_k=TOP_K, as_of=as_of)
        base = M.build_portfolio(F.production_score(feats, prod), fwd, prod,
                                 horizon=horizon, top_k=TOP_K, as_of=as_of)

        print("=" * 104)
        print(f"{label}｜非重疊期間 {len(as_of)} 個")
        print("=" * 104)
        print()
        print("【剔除規則各自移除多少】")
        print("-" * 104)
        steps = [("E1+E2 生產資格閘門", prod)]
        cur = prod
        for name, m in (("E3 波動 > 60%", feats.vol20 <= VOL_CEILING),
                        ("E4 跌破 20 日均線", feats.trend > 0),
                        ("E5 三個月跌 25%", feats.r60 >= R60_FLOOR)):
            cur = cur & np.isfinite(m) & m
            steps.append((f"再剔除 {name}", cur))
        for name, m in steps:
            med = float(np.median(m.sum(1)))
            print(f"  {name:<26} 存活中位數 {med:>7.0f} 檔"
                  f"（{med/np.median(prod.sum(1))*100:>5.1f}% of eligible）")
        print()
        med_k = float(np.median(keep.sum(1)))
        print(f"→ 存活 {med_k:.0f} 檔，要挑 20 檔。**存活數是 20 的 "
              f"{med_k/TOP_K:.0f} 倍 → LLM 有真選擇，不是被塞滿。**")
        print()

        print("【剔除式 vs 現行生產 score】")
        print("-" * 104)
        print(f"{'選法':<22}{'有效部位':>9}{'離散度':>9}{'前瞻波動':>10}"
              f"{'淨@1.0x':>9}{'@2.0x':>8}{'回撤':>9}{'最差單日':>10}")
        for name, pf, sc, mask in (("現行生產 score", base,
                                    F.production_score(feats, prod), prod),
                                   ("剔除式 v1", surv,
                                    survivor_score(feats, keep), keep)):
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
            print(f"{name:<22}{dv.get('effective_positions', float('nan')):>9.2f}"
                  f"{np.mean(disp)*100:>8.2f}%{np.median(sel)*100:>9.1f}%"
                  f"{ct['base']['annualised_net']*100:>8.1f}%"
                  f"{ct['2.0x']['annualised_net']*100:>7.1f}%"
                  f"{rp.get('mdd', float('nan'))*100:>8.1f}%"
                  f"{rp.get('worst', float('nan'))*100:>9.2f}%")
        print()

    print("=" * 104)
    print("【E3 波動上限的敏感度】60% 太鬆（只濾 8–11%）。門檻該設多緊？")
    print("=" * 104)
    print()
    for label, start, end in (
        ("驗證期 2021-2025", date(2021, 1, 4), date(2025, 12, 31)),
        ("探索期 2016-2020", date(2016, 1, 4), date(2020, 12, 31)),
    ):
        panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
        feats = P.compute_features(panel)
        prod = P.eligible_mask(panel, feats, min_adv20_usd=P.MIN_ADV20_USD)
        fwd = P.forward_returns(panel, horizon)
        fvol = forward_vol(panel, horizon)
        as_of = P.study_as_of(panel, horizon, start)
        print(f"{label}")
        print("-" * 104)
        print(f"{'vol 上限':>10}{'存活數':>9}{'/20':>7}{'離散度':>9}"
              f"{'前瞻波動':>10}{'淨@2.0x':>10}{'回撤':>9}{'有效部位':>10}")
        for cap in (1.00, 0.60, 0.45, 0.35, 0.28):
            keep = exclude_mask(panel, feats, prod, vol_ceiling=cap)
            n_surv = float(np.median(keep.sum(1)))
            if n_surv < TOP_K * 2:
                print(f"{cap:>10.2f}{n_surv:>9.0f}{n_surv/TOP_K:>6.0f}x"
                      f"{'存活不足，無法穩定取 20':>32}")
                continue
            sc = survivor_score(feats, keep)
            pf = M.build_portfolio(sc, fwd, keep, horizon=horizon,
                                   top_k=TOP_K, as_of=as_of)
            disp = []
            for i, t in enumerate(pf.as_of):
                rs = [fwd[t][j] for j in pf.names[i] if np.isfinite(fwd[t][j])]
                if len(rs) >= 5:
                    disp.append(float(np.std(rs)))
            sel = []
            for t in as_of:
                cols = np.flatnonzero(keep[t])
                order = np.lexsort((cols, -sc[t][cols]))
                sel += [fvol[t][j] for j in cols[order][:TOP_K]
                        if np.isfinite(fvol[t][j])]
            rp = risk(panel, pf, horizon)
            ct = M.cost_table(pf, horizon)
            dv = M.diversification(pf, panel, horizon)
            mark = "  ← 現用" if abs(cap - 0.60) < 1e-9 else ""
            print(f"{cap:>10.2f}{n_surv:>9.0f}{n_surv/TOP_K:>6.0f}x"
                  f"{np.mean(disp)*100:>8.2f}%{np.median(sel)*100:>9.1f}%"
                  f"{ct['2.0x']['annualised_net']*100:>9.1f}%"
                  f"{rp.get('mdd', float('nan'))*100:>8.1f}%"
                  f"{dv.get('effective_positions', float('nan')):>10.2f}{mark}")
        print()

    print("=" * 104)
    print("【公式】可以直接寫進 metrics.py")
    print("=" * 104)
    print("""
  # 1) 硬剔除（全部 point-in-time）
  keep = (price >= 5) & (ADV20 >= $20M)          # 生產資格閘門
       & (vol20 <= 0.60)                          # E3 年化波動 <= 60%
       & (trend > 0)                              # E4 站上 20 日均線
       & (r60  >= -0.25)                          # E5 三個月跌幅 < 25%

  # 2) 存活者取 20（填充排序，不是 alpha 訊號）
  order  = 0.50*p_cs(trend) + 0.30*p_cs(r60) + 0.20*(1 - p_cs(vol20))
  top20  = 存活者中依 order 取前 20

  # 3) 輸出給 risk_manager
  每檔附 vol20 / trend / r60 / adv20，供 sizing 使用
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
