# 財報修復試驗、固定做空公式重測與 20 檔分析上限

2026-09-29。已完成前一步提出的 30 家資料試修、正式程式預算修正、固定公式重測與獨立核對。結果不支持採用 M-score 做空選股，沒有啟動封存區間測試。

**工程限制已落實；財報資料仍不足。** 30 家中 14 家補回 54 個數值，7 家越過原本第一個失敗原因，但完整可算 M-score 的公司仍為 **0/30**。同一附註檔套用到該季全部既有申報後，完整申報由 28 增至 94 筆；全期間完整率僅由 2.31% 增至 3.14%。主做空方法在 59 期只選出 6 次、涉及 3 家公司，按每期實際投入名目正規化的成本後平均為 **−5.11%**。

## 1. 固定範圍與原始來源

先固定 [METHOD.md](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/METHOD.md) 與 [sample.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/sample.json)，再取得附註數值及計算收益。樣本從 2018Q1 年度申報的五類首個失敗原因抽取，依 `SHA256(20260929:accession)` 排序、不同 CIK 去重，不依後續股價挑公司。樣本只代表這些失敗類別，不能外推全市場。

使用 SEC 官方 [Financial Statement and Notes Data Sets](https://www.sec.gov/data-research/sec-markets-data/financial-statement-notes-data-sets) 的 [2018Q1 原始 ZIP](https://www.sec.gov/files/dera/data/financial-statement-notes-data-sets/2018q1_notes.zip?download=1)，508,544,155 bytes。這個資料集涵蓋主表及附註，比原先只有主表的資料多；SEC 也提醒抽取資料不能取代完整申報。

只取同 accession、USD、標準 us-gaap tag、無 dimensions／coreg、日期在 2015 年至該申報期末的數值。期末存量取 qtrs=0，年度流量取 qtrs=4。依 [SEC 格式說明](https://www.sec.gov/files/aqfsn_1.pdf)，附註 ddate 被整理為月末，datp 是日期偏移比例，durp 是期間偏差比例；先恢復實際日期，再對齊兩年，不把 52/53 週財年一律改成 12/31。年度流量另要求 `abs(durp)<=0.04`。

缺值不補零、不以 D&A 代替純折舊、不推測 custom tag、不按收益新增 alias。相同 tag/date/qtrs 的數字不一致，照預先規格拒絕並保留所有來源。ZIP、表名、行號、原 tag、日期、期間、dimensions、數值及來源雜湊可在每個修復欄位的 `fact_sources` 查到；申報接受日及 accession 一併保留。

價格計算限 2016–2020，財報限 2015–2020；2015 僅作早期已知財報。**2021–2025 檔案未讀取。** 當前官方 ticker/CIK 對照仍不是歷史證券主檔，身分與存活偏差尚未解決。

## 2. 30 家實際修復結果

| 原本首個缺失 | 家數 | 有新增數值的家數 | 新增數值 | 第一個失敗原因前進 | 完整 M-score |
|---|---:|---:|---:|---:|---:|
| 純折舊 Depreciation | 10 | 7 | 24 | 5 | 0 |
| 應收帳款 | 5 | 4 | 16 | 1 | 0 |
| 可比前年度 | 5 | 2 | 11 | 1 | 0 |
| SG&A | 5 | 0 | 0 | 0 | 0 |
| 長期債務 | 5 | 1 | 3 | 0 | 0 |
| 合計 | 30 | 14 | 54 | 7 | 0 |

逐家公司、申報編號、新增數值及來源行號見 [pilot_audit.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/pilot_audit.json)。第一個原因前進只表示跨過一道檢查，後面的欄位仍可能缺失。

例如 MAS 有 `ReceivablesNetCurrent`，原口徑限定的 AccountsReceivable tag 仍缺；POWI／BOOM 等有分開的管理或銷售費用，不能僅憑名字把它們加總當作 SG&A；ECL／CPS 有包含 capital lease obligations 的概念，不能直接替代裸債務。這些是需要查原申報 context 的候選映射，**0/30 不等於公司完全沒有披露數字**。本輪證據表明，單純補下載標準 tag 附註並不足以完成這批缺失申報。

按預定規格，同一份下載另套用到 2018Q1 全部 964 筆既有非金融年度申報，其他季度不變：

| 範圍 | 原完整數 | 修復後完整數 | 完整率變化 |
|---|---:|---:|---:|
| 2018Q1 的 964 筆 | 28 | 94 | 2.90% → 9.75% |
| 全期間 7,889 筆 | 182 | 248 | 2.31% → 3.14% |

該季新完整 67 筆、失去完整資格 1 筆，淨增 66。失去資格的是 SHW：Assets 及 Depreciation 出現精確值與近似值衝突，例如 Assets 的 19,958,427,000 與 19,958,000,000。看起來涉及披露精度，但本輪未用原 XBRL context／精度規則裁決，仍依固定的衝突拒絕規則排除。所有衝突值與行號見 [quarter_audit.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/quarter_audit.json)。因此不能把重測變化全解釋成新增資料的好處。

## 3. 固定公式的重測證據

直接重用 [原固定 M-score 規格](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-mscore-validation/METHOD.md) 與原評估程式：八係數、`M>-1.78`、訊號前已接受申報、最新缺值不退回舊分數、500 日有效期、缺值及分母檢查均保持原樣。主方法加 `price<MA20` 且 `r20<0`，按原順位選最多 20 檔。未調係數或門檻。

59 個不重疊 20 日區間，次日開盤進場、第 20 日收盤退出，做空收益 `1-exit/entry`。以下成本情境為往返 10bp、年化借券費 5%，按實際曆日扣除；這是假設成本，實際歷史券源及費率未驗證。每檔原本金的 1/20，未用額度留現金；表中收益再除以各期實際投入比例，避免低曝險造成假改善。

| 固定方法 | 有選股期數 / 59 | 選股觀測次數 | 選滿 20 的期數 | 每期投入名目成本後均值 | 循環 block bootstrap 95% 區間 |
|---|---:|---:|---:|---:|---:|
| 同可算 M 公司池的價格基準 | 46 | 454 | 8 | −1.71% | [−3.58%, +0.13%] |
| M-score 單獨 | 11 | 20 | 0 | −2.36% | [−9.49%, +4.15%] |
| M-score＋價格弱勢（主方法） | 4 | 6 | 0 | **−5.11%** | [−13.58%, +3.37%] |
| 比率 1%/99% 縮尾敏感度 | 4 | 6 | 0 | −5.11% | [−13.58%, +3.37%] |

主方法 6 次觀測為 TUSK 一次、GPN 三次、FAST 兩次；55 期沒有選股，沒有一期湊滿 20。按全部 59 期及原本金計算的成本後均值為 −0.04145%，不能拿這個幾乎空倉的數字當成策略變好。6 次觀測中僅 1 次個股毛收益為正，4 次遭遇期間 high 高於入場價 10%，2 次超過 20%。排除 2020 的預定敏感度只剩 TUSK 一次觀測，無法支持跨年穩健性。

也有必須保留的正面結果：**在共同有選股的 4 期**，主方法相對同池價格基準的投入名目收益差平均 **+3.77 個百分點**，bootstrap 區間 [+0.78, +6.76]。這表示這 4 期虧損較少；不是主方法已獲利，也不是可靠的廣泛優勢。僅 3 家公司、4 期且同區間已反覆探索，block=3 的重抽區間不足以建立穩定推論。全額投入配對數為 0，不能宣稱 20 檔策略有效。

完整四方法、年度、排除 2020 與配對結果見 [summary.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/summary.json)；逐期與選股追蹤分別在 `periods.csv`、`selections.csv`、`coverage.csv`。修復前後主方法觀測由 9 減為 6，涉及 SHW 衝突排除及新增 TUSK，不能當作在相同成分下的乾淨前後比較。

## 4. 證據有重新算過

[verify.py](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/verify.py) 沒有只相信 summary：用獨立 CSV 逐行讀取原 ZIP，核對該季 94 筆完整申報所用欄位及試修 54 個新增數值，共核對 **4,032 個來源引用**。原始資料的單位、日期、期間、dimensions 及值均匹配；主表及附註兩份 ZIP 雜湊均與來源紀錄相符。另直接核對該季全部 **964 筆**申報的 CIK、期末、接受日、表單及 instance。見 [source_verification.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/source_verification.json)。

另重用獨立 Decimal／scalar oracle 核對 151 個選入申報的比率、236 個方法期、486 次選股觀測及 9,380 個不重複未來 OHLC bar，重新檢查申報可用日、價格確認、排序、20 名額及全部成本情境。最大比率差 5.33e−15，M-score 差 1.78e−15，損益差 5.55e−17。主方法中未修復的 FAST／GPN 又直接核對原 2020Q1 數值表的 44 個欄位。

主方法 6 次進出場另與 Yahoo 2016–2020 quote OHLC 核對，收益方向 **6/6 相同**，最大收益差約 **0.0000061 個百分點**。外部價格只核對 quote，不使用當前 meta；它證明這幾次價格計算一致，不能驗證券源、歷史公司宇宙或完整下市。見 [verification.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/verification.json)、`external_price_check.csv` 及 `external_sources.json`。

## 5. 正式程式的連帶修正

本輪正式程式修的是股票分析數量；M-score 保留研究用途。原做多 exclusion 公式沿用，auto 做空仍走原雙向價格候選及 Screening LLM。

- **每帳戶每輪**的 Screening LLM 候選、做多／做空候選及可安全審查持倉共用最多 20 檔。先讀持倉預留額度，legacy 送給 Screening LLM 的表也縮至剩餘額度。
- 持倉優先，新候選不足可少選，零名額跳過 Screening LLM；產業容量不足減少選取數，不降低產業上限填滿。持倉本身超額停止當輪，留下明確原因。
- 篩選後新增持倉若使本輪已看過的股票聯集超額，停止後續深度分析。重用每日快取的輪次不新增 Screening LLM 呼叫，仍重新查持倉並預留名額。
- 配置在所有模式拒絕大於 20 的 select_n／analysis_limit；cache schema 升至 6，所有模式指紋都包含分析上限。舊快取需重掃，零／部分選股快取可驗證。
- 長跑恢復固定同輪名單、核對指紋與名額；A/B 各帳戶核對自己的持倉及凍結名單，僅共同有分析名額的新候選配對，未配對的持倉仍單獨審查。

股票數限制不等於單一 LLM 呼叫數；同一股的多 agent／retry 仍受既有 token 預算管控。A/B 兩帳戶合併的不同股票聯集可能超過 20，這裡落實的是每帳戶每輪限制。價格排除門檻也不能證明企業品質。

實作在 `tradingagents/screening/policy.py`、`pipeline.py`、`selection_store.py` 與 `tradingagents/long_run.py`，設定與操作說明見 [EXCLUSION_SCREENING.md](/Users/zongen/Downloads/codex/tradingBuffett/docs/EXCLUSION_SCREENING.md)。快照變更也有語意核對：僅還原 schema-5 指紋即精確匹配上版完整快照，沒有靠重新接受雜湊掩蓋交易結果變化。

全套離線測試 **1,969 passed，335 subtests passed，86.61 秒**；唯一 warning 是既有 `websockets.legacy` 棄用提示。覆蓋雙向候選＋持倉共額、20／21 持倉、篩選後持倉變化、零／部分選股、cache、長跑及 A/B／復原路徑。見 [full_tests_final.log](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-repair-pilot/full_tests_final.log)。測試使用 fake broker／LLM，本輪沒有真實交易或模型請求。

## 6. 判定與剩餘工作

30 家試驗、預算連帶修正及固定方法重測已完成。試修未達完整財報可算條件；全期間覆蓋仍低，主方法絕對收益為負、觀測過少，故**不採用 M-score、不啟動封存資料測試**。已將失敗結果交付，沒有調公式追求通過。

繼續研究前，資料工作應先解決具體口徑：回到原申報 XBRL context／precision，核對 SG&A 分項、應收範圍、長債租賃及數值精度；每個映射先固定規則與來源，再報完整率及衝突率。取得足夠覆蓋後，仍須處理歷史證券主檔、併購／準則可比性、券源／召回／費率、股息補償及完整下市，才有條件評估可交易效果。本次沒有完成全市場財報資料清理，沒有樣本外策略證明。

## 7. 重現與封存身分

在專案根目錄執行；原始 ZIP 已快取於 ignored 的 `research/data/sec-mscore/`，既有輸出沒有覆寫：

```bash
.venv-p2/bin/python research/out/20260929-data-repair-pilot/fetch_notes.py
.venv-p2/bin/python research/out/20260929-data-repair-pilot/repair.py
.venv-p2/bin/python research/out/20260929-data-repair-pilot/evaluate.py
.venv-p2/bin/python research/out/20260929-data-repair-pilot/verify.py
TRADINGBUFFETT_ALPACA_READ_ONLY=false .venv-p2/bin/python -m pytest tests/ -q --tb=short
```

| 檔案 | SHA-256 |
|---|---|
| METHOD.md | `aafcc85846270917f5c709cea8755093769251c8cd6c52e73a59a556aee01a48` |
| sample.json | `0d26d1b7ca5085c4eb85b508bfe328240f93cc3f4b52ba951dd26f08b6b71020` |
| SEC 2018Q1 notes ZIP | `4b57ef7617913452e82e7a3e5b6ef0cf3f4909a131b129ee94902feebb964383` |
| 修復後 financials.json | `2d04210af8287e4f81be2189481eb8280be492e11ae6b7da6ff1defc13c87d28` |
