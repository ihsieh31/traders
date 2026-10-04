# 資料可信度閘門與缺口盤點

2026-09-29。已把資料檢查、每檔排除理由、選股快照與讀取驗證寫入正式流程。現在可以限制LLM每帳戶每輪最多分析20檔，排除不合格或資料有問題的候選；財報缺口仍不足以證明能排除所有爛公司，歷史資料也不足以確認做多／做空公式能獲利。

## 這次完成的行為

- 股票身分先檢查：代號、broker asset_id、來源、active/tradable US_EQUITY；相同代號或資產ID有歧義時全部隔離，不請求這些股票的價格。這是當前商品身分，尚未證明歷史發行公司／CIK對應。
- 價格要求61個完整權威交易日、OHLCV有效、來源與SIP／調整政策一致；市值要求來源及有效正值。缺資料不補零、不向前填、不退回較早日期。
- 每列保留來源、輸入列序號、結果及理由；資料缺口、資料矛盾、資格不足、風險規則分開記錄。未取得的財報及企業品質明確是unknown。
- 入選最多20檔保存完整61日OHLCV。讀取每日快取或凍結A/B名單時核對價格窗口hash、OHLC一致性並重算因子；缺證據或不一致不能續用。schema升到7，舊快取重掃。
- 每次完成掃描另存`screening_snapshots/<交易日>-<hash>.json`，每日快取覆寫不刪快照；快照寫入失敗停止該輪。長跑journal記錄品質摘要與快照路徑；舊品質epoch的續跑不能新增LLM分析。
- 持倉覆核、做多、做空沿用共用20檔名額，不足不補。持倉覆核不代表該股通過新股資料門檻；沒有篩選量測時，風險prompt明確告知不可推定。

正式流程目前仍以價格／流動性與既有風險規則篩選，沒有把未驗證財報或新的做空財報公式接入交易。來源標籤、hash及自述scope是可追溯性與一致性檢查，不能代替第三方原始資料查核。

程式契約與重現方式見[資料品質說明](/Users/zongen/Downloads/codex/tradingBuffett/docs/DATA_QUALITY_GATE.md)。

## 真實財報證據

只盤點既有2015–2020財報輸出，沒有讀取2021–2025研究價格，也沒有重新搜尋公式或計算收益。原財報輸出先以SHA-256鎖定：`3a13d4e340f90ec503e62d04fe6d7c58592c59b71d708c26f480a34d53a0f986`。

| 項目 | 結果 | 能說明什麼 |
| --- | ---: | --- |
| 財報紀錄 | 7,889 | 本次盤點範圍，並非完整市場 |
| 三個必要數字完整 | 4,973，約63.04% | 數字齊全，不代表經濟口徑正確 |
| 缺／衝突必要數字 | 2,915 | 無法計算所需比率 |
| 原流程因Assets非正而未完成 | 1 | 本輪未另查原文，仍列unknown |
| 數字完整但scope等證據不足 | 4,972 | 不可當作已通過語義驗證 |
| 已知scope矛盾 | 1 | CVNA 2019；本次另存核對後修正 |

原始紀錄語義狀態：7,888筆unknown、1筆invalid。另存的單筆修正通過資料契約，沒有把其餘unknown改為可用。完整數量與理由見[coverage.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-trust/coverage.json)；逐筆清單見[financial_quality.csv](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-trust/financial_quality.csv)。

### 已核對的CVNA修正

2019年報`0001690820-20-000052`中，歸屬Carvana Co.的淨損是−114,659,000 USD，合併淨損是−364,639,000 USD；合併CFO是−757,134,000 USD，Assets是2,057,748,000 USD。把前者與合併CFO／Assets配對會產生scope矛盾。原始年報見[SEC CVNA 2019 10-K](https://www.sec.gov/Archives/edgar/data/1690820/000169082020000052/cvna-20191231.htm)。

本次重新核對本地`2020q1.zip`的4筆原始num.txt紀錄、行號、adsh、期間、單位、qtrs、無segment/coreg及SHA-256。canonical淨損改用該申報已核對的`ProfitLoss`，保留原始tag與來源，資料契約由`invalid / scope_mismatch`轉成`usable`。

修正只適用這一份申報，不能推廣成所有公司的tag互換。成果另存[canonical_cvna_example.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-trust/canonical_cvna_example.json)，原公式、名單、收益輸出未因此更改。這證明能定位並修復特定口徑錯誤，不證明策略收益。

## 歷史研究停止條件

| 必要證據 | 現況 |
| --- | --- |
| 歷史商品／發行公司身分 | unknown |
| 下市與消失股票的結果 | unknown |
| 價格調整及股息的一致性 | unknown |
| 資料在訊號當時已可取得 | partial |
| 財報期間、單位、合併範圍（使用財報時） | partial |
| 歷史券源及借券成本（做空時） | unknown |

`historical_research_gate`要求適用項都verified，才回傳READY；READY也只表示輸入準備好，之後仍要做獨立策略驗證。本次執行`audit.py --validate-for-research`實際回傳exit 2與STOPPED，證據在[research_gate.log](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-trust/research_gate.log)。

舊研究腳本與成果保留供審查／重現；本次沒有聲稱已攔截所有手動研究入口。新的歷史收益結論應先通過這項准入檢查，再使用未被反覆調參的資料。之前反覆研究的樣本維持探索性定位。

## 正式流程的離線示範

使用合成股票與合成價格走真實`prepare_screening_round`、每日快取及凍結A/B讀取，沒有呼叫LLM或真實broker。

| 項目 | 實際結果 |
| --- | ---: |
| 輸入列 | 15 |
| 通過 | 9 |
| 缺資料 | 2 |
| 資料矛盾 | 3 |
| 被既有風險規則排除 | 1 |
| 新股候選 | 9，缺額11，不補滿 |
| 另需覆核的持倉 | 3 |
| 本輪待深度分析 | 12，低於20 |
| LLM請求 | 0 |
| 入選原始價格窗口 | 9 |
| 每日快取／凍結A/B重新驗證 | 均通過 |

可重現結果見[production_path_demo.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-trust/production_path_demo.json)。這是流程證據，沒有使用真實行情或證明選股收益。後續正式執行才會累積真實的前瞻快照。

## 驗證與執行方式

新增測試涵蓋錯誤身分、重複資產ID、OHLC、缺欄、來源／feed／調整政策、市值、時間戳；重新seal後的壞快取、改動因子、舊schema；快照保留及寫入失敗；財報scope／單位／期間／可取得日期與STOPPED／READY界線。

新增品質測試共30項；連同受影響的續跑回歸測試，78項通過。最終全套測試為**1,999 passed、335 subtests passed**，耗時92.96秒；只有既有websockets棄用警告。原始結果見[full_tests.log](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-trust/full_tests.log)。Python編譯與`git diff --check`也通過。這些是程式驗證，沒有證明真實行情來源完整或策略獲利。

```bash
.venv-p2/bin/python research/out/20260929-data-trust/audit.py
.venv-p2/bin/python research/out/20260929-data-trust/demo.py
# 目前預期exit 2，表示缺資料而停止准入。
.venv-p2/bin/python research/out/20260929-data-trust/audit.py --validate-for-research
.venv-p2/bin/python -m pytest tests/ -q --tb=short
```

現在優先累積固定篩選規則下的前瞻紀錄，並按缺口補資料。做空研究先補券源／借券成本與下市處理；財報先逐申報確認canonical口徑。資料尚未具備時，保留unknown及候選公式，不為了得到正收益繼續換公式。
