# F-20261004-11 — 財報品質因子 ×2：事先登記

2026-10-04 寫定，在建立任何財報面板、計算任何 IC 或組合之前。
plan.md v1.1、F-10 加上的條件 12／13 全部沿用。**只跑一次正式判定，不回頭改定義。**

## 1. 為什麼是這兩條

使用者在 F-10 之後選擇「加入財報資料」。只做多、挑個股、有最多外部證據的方向是
**獲利能力／品質**：

- Novy-Marx (2013) *The Other Side of Value*：毛利／總資產（GP/A）。
- Ball, Gerakos, Linnainmaa, Nikolaev (2016)：以現金為基礎的獲利能力（營業現金流／總資產）。
- Asness, Frazzini, Pedersen (2019) *Quality Minus Junk*：獲利、安全（低槓桿）等綜合。

Q2 的三成分來自 `out/20260929-screen-alternatives/REPORT.md` §3 在 2026-09-29 已寫下但
**從未測試**的提案；本輪只把「同產業百分位」改成全橫斷面百分位（無可靠歷史產業分類）。

| ID | 名稱 | 成分（符號） | 持有期 |
|---|---|---|---:|
| Q1 | `lit-gross-profitability` | GP/A（+1） | 10 |
| Q2 | `lit-quality-composite` | GP/A（+1）、CFO/A（+1）、Liabilities/A（−1），等權百分位 | 10 |

持有期 10 日的理由（寫在看結果前）：plan.md R2 的 walk-forward 每 fold 至少 8 期，
20 日持有期每 fold 只有 6 期，條件 7 必然 UNDECIDED（F-10 已實證）。財報排名變動慢，
10 日再平衡的換手成本低。**持有期以 5／10／20 掃描作為 R5 平台檢查**（R4 計入持有期為參數）。

## 2. 財報定義（point-in-time）

來源：SEC Financial Statement Data Sets（`sub.txt`、`num.txt`），2015Q1–2026Q2，as-filed。

- **申報**：只用 `form == "10-K"`（原始年報，不用 10-K/A：修正版常只含 Part III，缺值會誤判）。
- **可用時間**：`accepted` 的日期 **嚴格早於** 訊號日（當天接受的申報一律延後一天）。
- **取哪一份**：訊號日前已接受的**最新**一份 10-K；其會計期末距訊號日 ≤ 456 天（15 個月），
  否則視為過期、該股當日無值。最新一份缺必要欄位就是缺值，**不退回更舊的申報**。
- **數值**：同一 accession、`ddate == sub.period`、`uom == "USD"`、`version` 以 `us-gaap` 開頭、
  無 `coreg`、無 `segments`；流量 `qtrs == 4`，存量 `qtrs == 0`。同 tag 同期有矛盾值 → 缺值。
- **欄位**（依序取第一個存在的 tag，**不混用**）：
  - 營收 R：`Revenues`、`RevenueFromContractWithCustomerExcludingAssessedTax`、`SalesRevenueNet`、
    `RevenueFromContractWithCustomerIncludingAssessedTax`、`SalesRevenueGoodsNet`
  - 毛利 GP：`GrossProfit`；否則 R − 第一個存在的 `CostOfRevenue`、`CostOfGoodsAndServicesSold`、`CostOfGoodsSold`
  - 總資產 A：`Assets`（必須 > 0）
  - 營業現金流 CFO：`NetCashProvidedByUsedInOperatingActivities`
  - 負債 L：`Liabilities`（不用其他 tag 推算）
- **排除**：SIC 6000–6999（金融、保險、不動產；毛利與負債口徑不同）、SIC 未知。
- **身分**：SEC 現行 `company_tickers` 的 CIK→ticker，與股票宇宙（`stock_symbols.txt`）取交集；
  同一 CIK 多個 ticker 時只取字母序第一個。這是**現行**對照，不是歷史證券主檔（已知限制）。

## 3. 宇宙與基準

- 遮罩 = 生產資格（price ≥ 5、ADV20 ≥ $20M、完整 61 日）**∩ 當日該公式所有成分可算**；
  寬宇宙同理（條件 6）。
- 條件 12 的等權基準用**同一個遮罩**（同類配對：有財報、非金融的股票）。
  另揭露（不判定）相對完整生產資格宇宙等權的超額。
- 期間：探索 2016–2020；驗證 2021–2025；holdout 2026-01-02..2026-09-08（價格已在 F-10 讀過，
  但**本因子從未在任何期間計算過**）。

## 4. 條件與門檻

R10 條件 1–12 + F-10 的條件 13。`t_bonf` = 2 公式 Bonferroni，雙側 α=0.05/2 → **2.24**。
條件 12：驗證期超額 NW `t ≥ 2.24` 且 3 期區塊 bootstrap 95% CI 下界 > 0。
全部 13 條通過才稱 `VALIDATED`。

## 5. 已知限制（事先寫下）

- 現行 ticker 對照有存活者與身分偏差；下市公司幾乎不在股票宇宙內。
- XBRL tag 選擇是簡化映射，不等於 Compustat 逐欄口徑。
- 累計檢定數 ≥ 600 變體 + F-10 的 3 條 + 本輪 2 條。
- 若驗證期通過，仍需揭露：2021–2025 的價格已被多輪看過（但本因子未在其上計算過）。
