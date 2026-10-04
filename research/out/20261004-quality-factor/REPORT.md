# F-20261004-11 — 財報品質因子 ×2：結果

2026-10-04。規格見 [`PREREGISTRATION.md`](./PREREGISTRATION.md)，在建立財報面板前寫定；
每條公式只跑一次正式判定。**兩條都 `REJECTED`，仍沒有找到符合標準一的公式。**

## 1. 判定

| 條件 | Q1 毛利／資產 | Q2 品質綜合（毛利、現金流、低負債） |
|---|:-:|:-:|
| 1 驗證期 \|t\| ≥ t_bonf | ✗ 0.42（需 2.245） | ✗ 0.43（需 2.328） |
| 2 兩期同號 | ✓ | ✓ |
| 3 驗證期淨年化 > 0 | ✓ 4.2% | ✓ 4.0% |
| 4 成本 2x 仍 > 0 | ✓ | ✓ |
| 5 勝過生產 score（同宇宙、驗證期） | ✗ 4.2% vs 10.0% | ✗ 4.0% vs 8.0% |
| 6 兩個宇宙都成立 | ✓ | ✓ |
| 7 walk-forward ≥ 3/4 | ✓ 3/4 | ✓ 3/4 |
| 8 持有期 5／10／20 平台 | ✓ | ✓ |
| 9 ≥ 2 個市場狀態同號 | ✓ 4/4 | ✓ 4/4 |
| 10 剔除最佳 5% 仍同號 | ✓ | ✓ |
| 11 下單量 ≤ 0.1% ADV20 | ✓ | ✓ |
| **12 顯著勝過同宇宙等權** | ✗ **−4.7%/年，t −1.04** | ✗ **−5.2%/年，t −1.19** |
| 13 2026 holdout 與驗證同號 | ✓（負） | ✗（正，16 期） |
| **判定** | **REJECTED** | **REJECTED** |

揭露（不判定）：相對**完整**生產資格宇宙的等權，超額為 −3.7%／−3.9%/年（t −0.76）。

## 2. 讀法

- **探索期很好、驗證期消失**：探索期（2016–2020）淨年化 24.0%／19.8%，驗證期只剩 4.2%／4.0%，
  而且**輸**同宇宙等權約 5 個百分點。這是典型的「樣本內有效、樣本外沒有」。
- 穩健性條件（7–11）都過，代表探索期內的結果不是單一參數或少數期間造成的；
  但那只說明探索期內穩定，不代表會延續到之後。
- 外部文獻顯示品質因子在 2010 年後的美股大型股中長期偏弱，與這個結果一致。
  本輪不據此宣稱任何因果。

## 3. 資料與限制

| 項目 | 內容 |
|---|---|
| 財報 | SEC Financial Statement Data Sets 2015Q1–2026Q2，70,924 份原始 10-K（`fetch_sec.py`、`research/src/fundamentals.py`） |
| 可用宇宙 | 非金融、能對到 CIK、成分齊全：生產資格中位 **584 檔**（Q1）／**408 檔**（Q2），完整生產資格是 1,267 檔 |
| 覆蓋落差 | 營業現金流 tag 的覆蓋率 2015–2017 年 56–61%，2019 年後 93–98%；Q2 探索期前段樣本較小。依事先登記不改 tag |
| `t_bonf` | 事先登記寫 2.24（2 公式）；harness 取 `max(成分 N_eff, 公式數)`，Q2 成分 N_eff 2.49 → 2.328，**比登記更嚴**，不影響判定 |
| 身分 | SEC 現行 ticker 對照，非歷史主檔；存活者偏差照舊 |

## 4. 程式變更

- `research/src/fundamentals.py`：point-in-time 10-K 解析（只用訊號日前已接受的最新一份、
  過期 > 456 日即缺值、不退回舊申報、同申報內矛盾值即缺值）；`test_harness` 新增對應測試。
  Walmart FY2019 等抽查與公開數字一致。
- `research/src/study.py`：`COMPLETE_CASE`（宇宙與等權基準＝所有成分可算的股票）、
  `HORIZON_SWEEPS`（持有期平台檢查）、相對完整資格宇宙的揭露。
- `fetch_sec.py` 的 User-Agent 從環境變數讀入，聯絡 email 不寫入任何檔案。

## 5. 重現

```bash
SEC_USER_AGENT="Name email" .venv-p2/bin/python research/out/20261004-quality-factor/fetch_sec.py
.venv-p2/bin/python -m research.src.fundamentals
for f in lit-gross-profitability lit-quality-composite; do
  .venv-p2/bin/python -m research.src.study --formula $f --period both \
    --start 2016-01-04 --end 2026-09-08 \
    --symbols-file research/out/20261004-screen-diagnosis/stock_symbols.txt \
    --family-size 2 --round-id 20261004-quality-factor/$f
done
```
