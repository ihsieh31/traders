# 篩選資料可信度與研究停止條件

2026-09-29。目標是最多20檔供LLM分析、輸入可追溯、未知不變成零。價格／身分通過不表示公司品質好或策略能獲利。現有公式與門檻保持原樣。

## 正式篩選

掃描在請求價格前檢查股票代號、broker asset_id、來源、active/tradable US_EQUITY及重複身分。衝突的所有列都停用。Alpaca資產ID只證明當前商品快照，沒有證明歷史發行公司／CIK或普通股類型。

價格要求可識別來源、SIP量、同一調整政策；61個完整權威交易日；OHLCV有限、價格正、成交量非負、low<=open/close<=high。缺日、缺欄、不合理值不補零、不向前填、不改as_of。市值必須有來源及有限正值，沿用既有最低市值／價格／ADV20門檻。

每個原始宇宙列記錄理由及分類：`data_gap`（資料缺口）、`data_error`（矛盾／不合理）、`eligibility`（資格不足）、`risk_policy`（原風險規則）、`usable`。這些分類不是好／爛公司的標籤。財報及企業品質明確為unknown，當前價格篩選不使用財報公式。

每日SelectionStore帶有`data_quality`。schema 7使舊快取失效；資料品質報告、來源、身分、市值與因子不一致的快取／凍結A/B名單不可用。入選最多20檔另保留完整61日OHLCV；讀取時核對hash、價格一致性並重算因子。未入選列保留排除理由或量測摘要／窗口hash。

已完成掃描在每日快取旁的`screening_snapshots/<交易日>-<hash>.json`留存完整選股與品質快照。每日快取替換不刪舊快照；快照寫入失敗停止該輪。這是可重播的本地紀錄，來源標籤及self-hash不是第三方數據正確性的保證。

持倉風險覆核、做多、做空共用每帳戶每輪最多20個LLM股票名額，不足不補。持倉不會自動被當成通過新股價格門檻；其未取得的量測明確不可推定。長跑journal保存品質摘要、因子context及快照路徑。舊policy／fingerprint不允許續跑新的分析，已授權execution-only recovery維持既有保護。

## 財報與歷史研究

`assess_financial_facts`接收已正規化的`net_income`、`operating_cash_flow`、`assets`，保留原始tag與來源。必須有值、單位、期間、qtrs、可取得日期及合併範圍；缺證據回傳unknown，明確矛盾回傳invalid。函式不猜custom tag，不把不同歸屬範圍湊成比率。聲明相同scope只是合同的一項；原文審查與來源查核仍需要另外完成。

歷史收益證明需要歷史商品身分、下市結果、價格調整／股息、當時可取得時間；使用財報再需期間／單位／合併範圍，做空另需歷史券源及成本。`historical_research_gate`對適用且未驗證項回傳STOPPED。READY僅表示輸入準備好，不表示alpha成立。

本次 [缺口報告](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-data-trust/REPORT.md) 分開保存真實SEC資料盤點與合成的正式流程示範，沒有新回測或參數搜尋。只讀既有2015–2020財報及已允許的2016–2020價格輸出。2021–2025研究價格保持封存。

```bash
.venv-p2/bin/python research/out/20260929-data-trust/audit.py
# 歷史研究准入檢查：目前預期exit 2（STOPPED），不是成功驗證策略。
.venv-p2/bin/python research/out/20260929-data-trust/audit.py --validate-for-research
.venv-p2/bin/python -m pytest tests/test_screening_data_quality.py -q
```

舊研究檔保留供審查／重現；本次沒有把停止條件改成收益門檻，也沒有禁止手動重現舊實驗。新的歷史收益結論必須先列出適用資料證據，通過准入後另外使用未反覆研究的樣本。
