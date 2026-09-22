# 連續多輪 30 日修復驗收（2026-09-23）

## 修復後驗收：離線通過

在同一工作樹加入 state-only 恢復與測試 fixture 修復後再次執行驗收。**C01–C04 的離線驗收通過；真實 Alpaca Paper recovery gate 仍待交易時段驗證。** 未使用 Codex Security，沒有呼叫真實券商或模型，也沒有啟動交易。

- C01：pair 只有 `pair_state.json`（`COMPLETED`）、尚無 `pair_summary.json` 時，隔天恢復會驗證 campaign fingerprint、pair 身分、frozen evidence 與兩臂結果；離線一日 campaign 回 `completed`、`completed_dates` 含原交易日、final report 計入 1 pair，且未重跑 pair。篡改 evidence hash 會被拒絕；未完成 pair 仍回 `previous_session_unfinished`。
- C02：含 `risk_invalid_reason` 的完成 run log 恢復為 `FAILED_TERMINAL`，`decision_valid=False`。
- C03：缺任一 execution DB 或帳戶綁定不符會拒絕結算；兩臂正確綁定通過唯讀檢查。
- C04：空首窗口延伸會把下一窗口首個 NYSE 交易日列入 `expected_sessions`。
- 已新增 C01 state-only 跨日恢復與 evidence 篡改拒絕的正式回歸測試，皆通過。使用專案隔離 runner（Python 3.12.13、pytest 9.1.1）重跑：聚焦 suite `84 passed`；完整 suite `1614 passed`（各 2 個既有依賴棄用警告，完整 suite 36.54 秒）；`git diff --check` 通過。
- 隔離 runner 將暫存執行狀態放在臨時 HOME 的 `.tradingbuffett` 命名空間下，符合應用程式路徑所有權檢查；測試資料和報告仍在 repository 外。
- 重跑命令：`.venv-p2/bin/python scripts/refactoring/run_tests.py -- tests/test_analysis_ab_campaign.py tests/test_launch_blockers.py tests/test_analysis_ab_runner.py tests/test_continuous_window_timing.py`；完整 suite 使用 `.venv-p2/bin/python scripts/refactoring/run_tests.py -- tests`。

以下保留首次驗收的未通過證據，以說明此次修復的起點。

## 首次驗收：未通過（歷史紀錄）

基準：`c36e079` 加工作樹中的修復；只做離線驗收，沒有呼叫真實券商或模型，沒有啟動交易，也沒有使用 Codex Security。審查依 [前次 C01–C04 報告](CONTINUOUS_30D_FINAL_AUDIT_2026-09-23.md)。

**結論：未通過。** C02–C04 的獨立故障重現已轉綠；C01 仍有一個正常的中斷點未恢復，完整測試套件也有 7 項失敗。

| 項目 | 驗收結果 | 證據 |
| --- | --- | --- |
| C01：pair 完成後遺失 campaign checkpoint | **部分通過** | pair state 與 summary 都存在時，隔天 `run_campaign --resume` 只補 `completed_dates`、回 `session_completed`，不重跑 pair。若程序恰在 pair state 寫成 `COMPLETED`、summary 尚未寫出時中斷，`_completed_pair_checkpoint` 拋出 `completed pair checkpoint is incomplete`；pair runner 亦因日期已過不能安全重跑。這是 production 寫入順序中可達的中斷點。 |
| C02：無效 Risk run log 恢復 | **通過獨立重現** | 注入 `risk_invalid_reason=structured_unavailable` 的 completed graph log 後，恢復結果為 `failed_terminal`、`decision_valid=False`，pair 為 `FAILED_TERMINAL`。 |
| C03：缺失 execution DB 的結算 | **通過獨立重現** | 任一 DB 缺失時結算檢查拋錯；兩臂 DB 存在且帳戶綁定雜湊符合起始帳戶時正常通過唯讀檢查。 |
| C04：空首窗口的邊界日 | **通過獨立重現** | 2026-06-21（日）開始的 1 日連續窗口，延伸查詢從 06-22（一）開始，`expected_sessions` 包含 06-22。 |

## 測試結果與原因

- 聚焦 suite：`83 passed, 7 failed, 8 subtests passed`。
- 完整 suite：`1607 passed, 7 failed, 313 subtests passed`。
- 7 項失敗都在既有 Paper 結算測試：3 項 `test_analysis_ab_campaign.py` fixture 沒有建立 execution DB；4 項 `test_launch_blockers.py` fixture 缺少 Berkshire DB，或以未雜湊的 `ref-A/ref-B` 與真實綁定值比較。新的 production gate 正確拒絕這些資料，測試 fixture 需改為兩臂皆有正確帳戶綁定，不能放寬 gate 讓測試變綠。
- 本次修復尚未新增 C01–C04 的正式回歸測試；獨立重現是在暫存目錄執行。

## 首次驗收當時所需的下一步（已於上方完成）

1. 讓 C01 在 pair state 已 `COMPLETED`、summary 缺失時安全恢復：驗證 pair 身分、兩臂結果、fingerprint 與 evidence 後，從 durable pair state 重建 derived summary 或讓 campaign 直接採納該已完成狀態；不得重新分析或下單。
2. 更新 7 項舊測試的 Paper fixture，建立兩臂 DB 與正確帳戶綁定；補一個 C01「state 已完成／summary 缺失」回歸及 C02–C04 的最小回歸。
3. 重跑聚焦與完整 suite。程式離線驗收全綠後，仍須在交易時段完成真實 Alpaca Paper submit/cancel 與原訂單身分的 crash/restart recovery gate；本次沒有驗證該 gate。
