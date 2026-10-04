# F-20261004-09 — 生產剔除式 20 檔公式的結構診斷與修正

2026-10-04。診斷輪（plan.md §6.1a），**不提出新公式**。規格在執行前寫入
[`METHOD.md`](./METHOD.md)，共三部分，第二、三部分是看到前一部分結果後追加的，
追加原因照實記在 METHOD.md。

## 一句話

剔除式公式在生產宇宙仍能降低 20 日大跌率；但先前所有研究數字都是在**一半是 ETF**
的宇宙上量的，「不足 20 檔」的頻率被低估約 6 倍；硬補到 20 檔會引入較差的候選，
所以維持「最多 20 檔」。另修了 1 個生產門檻 bug、4 個研究量測 bug，並把掃描加速約 3.8 倍。

## 1. 發現與處置

| # | 發現 | 證據 | 處置 |
|---|---|---|---|
| 1 | **研究可信集約一半是 ETF**（4,842／9,753）；生產宇宙只有股票（Nasdaq 股票 screener 的市值副作用） | `stock_only_summary.json` `universe_counts` | 新增股票宇宙 `stock_symbols.txt`（4,804 檔）；process.md S-59 |
| 2 | 混 ETF 時剔除式名單平均 **11.7%／15.2%** 是 ETF；2020-04-20 **20／20** 是債券 ETF；2024-07-05 有 13 檔 ETF（含 3 倍槓桿 SPXL、UPRO） | 同上 `E1_mixed_universe` | 研究證據改在股票宇宙重算 |
| 3 | **股票宇宙下存活不足 20 檔：68 天（2016–2020，最少 0 檔）＋ 22 天（2021–2025，最少 3 檔）**，不是先前報告的 9＋5 天 | 同上 `pool` | 更正 `docs/EXCLUSION_SCREENING.md` |
| 4 | `trend > 0` 由浮點捨入決定（收盤＝均價時算出 ±1e-16）；生產對研究 26 天有 3 天差一檔 | `parity.log`（修正前輸出見本報告 §3） | 生產 `TREND_EPSILON = 1e-12`，公式版本 `exclusion-20261004-1`；修正後 **26／26 天逐檔一致**；S-58 |
| 5 | `build_portfolio` 先過濾再 zip，個股貢獻錯位；換手率假設每期滿 `top_k`；空倉期間不清空部位 | `research/src/test_harness.py` 三個新測試，舊程式上失敗 | 已修；S-56 |
| 6 | 8 支工具用 `np.flatnonzero(valid_as_of(...))[::h]`：網格位移一格且從前置歷史開始（2021–2025 首期 2020-12-24） | 同上 `study grid` 測試 | 新增 `panel.study_as_of`；S-57 |
| 7 | 「退市 P&L 空洞」：harness 原本丟棄持有中消失的部位；但可信集資格池 108,994 個股票期間只有 **2** 個在 60 日內消失，入選檔受影響 **0** | `summary.json` D4／D6 | `forward_returns(..., delisted_exit=True)` 可選；既有組合數字不受影響 |
| 8 | `build_panel` 只讀本地快取卻要求 Alpaca 金鑰 | — | 金鑰檢查移到真正需要下載日曆的分支 |

## 2. 在生產（股票）宇宙下，公式的風險證據仍成立

20 日內任一收盤較次日開盤跌 ≥10% 的比例，按日期配對、3 期區塊 bootstrap：

| | 2016–2020 | 2021–2025 |
|---|---:|---:|
| 原分數取 20（`current20`） | 14.07% | 19.68% |
| 剔除式（`report28`） | 10.53% | 11.83% |
| 差，95% CI（百分點） | **−3.51 [−5.88, −0.44]** | **−7.75 [−11.42, −2.92]** |
| 存活池 vs 被剔除池 | 9.53% vs 18.71% | 11.71% vs 25.31% |

依 METHOD.md 事先寫定的規則（兩期 CI 上界都 < 0），**證據在生產宇宙仍成立**。
代價不變：期末漲 ≥10% 的比例也從 11.9%／12.5% 降到 7.9%／9.1%。
有效部位數：入選 3.35／3.27，存活池隨機 20 檔 3.73／3.34——集中是市場本身的性質，
不是排序造成的（比值 0.90／0.98，未達事先寫定的 < 0.8）。
相鄰交易日名單平均重疊 66%（每天約 7 檔新名字）。

## 3. 為什麼不補到 20 檔

唯一受測、事先寫定的補位規則 F1（從未過 vol20／trend 但過 r60 的股票中，用同一公式補）：

| 不足日的 20 日大跌率（兩期合併） | 比例 | 股票期間數 |
|---|---:|---:|
| 當日存活者 | **11.1%** | 577 |
| 原分數取 20 | 22.5% | 1,800 |
| **F1 補位者** | **26.2%** | 1,223 |
| 整個資格池 | 29.6% | 110,467 |

補位者比原公式還差，依規則**不實作**。門檻在崩跌日正是最有效的時候。
有效樣本只有 13 個不重疊 20 日區塊，這是方向性證據。逐日資料見 `fill_test_summary.json`。

修正前的 parity 輸出（保留為證據）：2022-01-14 生產多選 SJNK（trend `+2.2e-16`），
2022-04-29／05-25／10-17 研究多選 STZ／PTLC／VTEB（trend `3.1e-15`／`3.8e-15`／`1.1e-15`，
生產為 `0.0`）。

## 4. 生產程式的變更

- `tradingagents/screening/metrics.py`：`TREND_EPSILON`、`FORMULA_VERSION = "exclusion-20261004-1"`；
  `validate_and_clean_bars` 改 numpy 實作、交易日窗口每次掃描只算一次。
- `tradingagents/screening/quality.py`：`bar_evidence` 以 numpy 組列。
- 合成 3,000 檔掃描 **15.3 秒 → 4.1 秒**；新舊實作 20,000 組隨機／異常行情差分測試全同
  （排除理由、輸出窗口與 dtype、證據 hash、因子值）。

## 5. 限制

- Tiingo `assetType` 是**現在**的分類，不是 point-in-time；107 檔無法分類者排除。
- 研究宇宙仍無 $300M 市值門檻，生產的不足日只會更多。
- 兩段資料都已被反覆研究，本輪不構成樣本外證據。
- 大跌率是價格風險，不是公司品質。

## 6. 重現

```bash
.venv-p2/bin/python research/out/20261004-screen-diagnosis/diagnose.py    # 第一部分 → summary.json
.venv-p2/bin/python research/out/20261004-screen-diagnosis/parity.py      # 生產 vs 研究 → parity.log
.venv-p2/bin/python research/out/20261004-screen-diagnosis/stock_only.py  # 第二部分 → stock_only_summary.json
.venv-p2/bin/python research/out/20261004-screen-diagnosis/fill_test.py   # 第三部分 → fill_test_summary.json
```

全部離線（socket 被封鎖），只讀 `research/data` 與日曆快取。
