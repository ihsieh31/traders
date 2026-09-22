# 連續多輪 30 日交易最終深度審查（2026-09-23）

審查基準：`c36e079`，工作樹原本乾淨。範圍包括單策略 `long-run --continuous`、30 個 NYSE session 的 A/B campaign、auto launcher、pair crash/resume、最後結算與相關執行狀態。使用 ponytail 原則，優先找能在既有邊界修復的根因；未使用 Codex Security。沒有呼叫真實券商、模型或啟動交易。

**結論：不能宣稱所有邏輯正確，也不應啟動正式 30 日 Paper A/B。** 舊文件稱程式面只剩真實 Paper recovery gate；本次在該 gate 之外確認三個 A/B 缺陷和一個短連續窗口缺陷。以下重現均以暫存目錄、假日曆與假 broker/模型邊界執行，沒有讀取金鑰。

| ID | 等級 | 已確認行為與影響 | 最小修正方向 |
| --- | --- | --- | --- |
| C01 | P1 | pair 已持久化為 `COMPLETED`，但 campaign 尚未寫入 `completed_dates` 時中斷；隔天 `--resume` 固定回 `previous_session_unfinished`，無法繼續其餘 29 日。即使同日重試，pair runner 看見既有 `pair_summary.json` 也會拒絕重跑。 | campaign 恢復時先核對該日 pair state、summary、fingerprint 和 evidence，對已完成的 pair 補寫日期 checkpoint；不得再次分析或下單。 |
| C02 | P1 | 分析 graph 已寫完 run log、但 `_run_one` 尚未處理 `risk_invalid_reason` 時中斷，`_recover_completed_analysis` 會直接回傳 `status=completed`，且沒有 `decision_valid`／錯誤原因。shadow pair 可將無效 Risk 決策當成有效完成樣本。 | 恢復時沿用 `_run_one` 的 Risk 有效性判斷；無效狀態保持 terminal/invalid，不讓完成的 graph log 覆蓋決策契約。 |
| C03 | P1 | Day-30 `_unsettled_primary_orders` 對不存在的 backend execution DB 直接 `continue`；兩個 DB 都缺失時，Paper campaign 仍可 pin ending equity、寫報告、標為 `COMPLETED`。此時沒有 ledger 可證明 primary orders 已結算。 | Paper finalization 要求兩臂 DB 都存在且帳戶綁定可驗證；缺失時 fail closed，保留 campaign 供人工恢復，不建立空 DB 充數。 |
| C04 | P2 | 單策略 `--continuous --duration-days 1` 從無交易日的初始窗口延伸時，空 `expected_sessions` 使查詢從舊結束日的隔天開始，漏掉舊結束日這個下一窗口的首個交易日。 | 空清單時以舊窗口的結束日作為查詢起點；保留半開窗口末日過濾與去重。預設 30 日窗口通常含交易日，本項主要影響已支援的短窗口。 |

## 可追查的證據

- C01：[campaign 日期判斷](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab_campaign.py:822) 先拒絕昨天；[完成日期寫入](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab_campaign.py:876) 在 pair runner 返回後；[pair replay 拒絕](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:1109)。故障注入在 pair 寫出 `pair_state.json`、`pair_summary.json` 後拋出 `KeyboardInterrupt`：`campaign_completed_dates=[]`，隔天 outcome=`previous_session_unfinished`。
- C02：[run log 恢復](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:931) 只比對 completed/日期/backend/evidence；[正式分析判斷](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:604) 檢查 `risk_invalid_reason`。注入包含 `risk_invalid_reason=structured_unavailable` 的 completed run log，恢復結果仍為 `completed`，未帶 `decision_valid`。
- C03：[缺 DB 即略過](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab_campaign.py:445)；[結案閘門](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab_campaign.py:601)。離線一日 Paper campaign 中，兩個 DB 都不存在，結果仍為 `outcome=completed`、`status=COMPLETED`、`ending_equity` 已固定。該案例只證明結案閘門缺口，沒有模擬真實成交。
- C04：[空窗口起點](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/long_run.py:1309)。以 2026-06-21（日）至 06-22（一）11:00 ET 的空首窗口重現：延伸查詢為 06-23..06-23，新的 `expected_sessions=[]`，06-22 交易日永遠沒有被凍結。

## 驗證與界線

- 聚焦測試：`90 passed`，8 個 subtests；完整 suite：`1614 passed`，313 個 subtests；`compileall` 與 `git diff --check` 通過。四項缺陷都是現有測試未覆蓋的交錯／遺失狀態，綠燈不能取代故障注入。
- 既有 Paper preflight、雙帳戶配置隔離、frozen evidence、30 session 日曆與 terminal replay 防護在本次閱讀路徑中沒有發現新的確定性反例；這不表示它們已經過真實券商驗證。
- 真實 Alpaca Paper submit/cancel、原 `decision_id` 與 `client_order_id` 的 crash/restart recovery gate 仍未完成。修復 C01–C03 並加入小型離線回歸後，才應執行該 gate；在此之前不能把 30 日任務視為 ready。
