# 超長 A/B 測試最後審查 — 2026-09-26

本次針對正式入口 `python -m cli.main long-run --mode ab` 與 continuous observation 做程式碼追蹤、故障重現及修補。使用 ponytail 原則：重用既有狀態、schema 驗證、有限重試及成本彙總；沒有加入依賴、背景服務或第二套狀態機。未使用 Codex Security。

## 結論

確認並修復下列 8 類問題。這是程式與離線回歸驗證結果，並非已完成數月連續運行的證明，也不是投資績效驗證。本次沒有啟動正式 A/B observation 或下達 Paper 訂單。

| 優先級 | 問題與觸發條件 | 修補後行為 |
|---|---|---|
| P1 | 第一側分析完成，第二側因提交期限已過而直接 `DONE`；原本只攔截 `FAILED`，第一側仍會進入執行流程。原有 broker gate 仍限制新增曝險，但配對前置條件已失效。 | 任一側缺少交易意圖或分析失敗，該配對不進入新的執行流程；另一側已分析意圖記為 `DONE`、`no_trade`、`AB_PAIR_INCOMPLETE`。若已有 `EXECUTING`，停止並保留不確定執行狀態。 |
| P1 | 在完成 graph 日誌後、交易意圖驗證前中斷，重啟時 `ANALYZING` 的 run-log 恢復路徑原本直接接受非空意圖。 | A/B 恢復意圖使用與正常分析相同的 `validate_trade_intent`；不合格的紀錄不能被當成完成的配對意圖。 |
| P2 | 當天尚未產生 shared selection，但 runner 在提交期限之後、收盤之前啟動；回合保持 `PENDING`，scheduler 可不斷重試並寫入。 | 確認期限已過時，立即持久化 `MISSED / SESSION_SUBMISSION_DEADLINE`，同一天不再被排程。無法確認交易時窗仍沿用原本的保守處理。 |
| P2 | A/B 協調器未像單側回合一樣檢查 schema/status；已完成 arm 也會跳過 schema 驗證。 | 不支援的 schema、未知 coordinator status 直接 `STATE_CORRUPT`；已完成 arm 仍須通過 schema 檢查。 |
| P2 | continuous window 延伸只查詢交易日曆一次，一次暫時錯誤即結束整段長期 observation。 | 重用既有三次、每次間隔五秒的有限重試；只重試日曆讀取。重試耗盡仍停止，且不改動原視窗；不重播會修改狀態的整段延伸操作。 |
| P2 | 提前停止時只有 `shared_screening.top20` 字串列表；報表只接受完成回合的字典列表，導致當天 Top20 與時間差母數遺失。 | 同時讀取兩種現有格式，保留已凍結但未完成回合的比較母數。 |
| P2 | 兩帳戶都持有、但不在共享 Top20 的標的，被計入共同訊號／分歧統計。 | 正式配對訊號統計限定為當天共享 Top20；額外持倉管理仍保留在各 arm 報告。 |
| P2 | shared screening 日誌使用父 observation ID，而兩側成本掃描只匹配 arm ID，因此共享選股用量漏計。 | 在 `shared_screening.llm_operations` 獨立列示共享 tokens、已定價成本與未定價 tokens，Markdown 同步顯示；不重複歸入任何 arm。 |

## 檢查範圍與依據

- **入口與恢復**：追蹤 CLI 的 global/runner locks、明確 Paper 授權、兩帳戶識別、experiment fingerprint、active state 與恢復參數比對。正式入口以全市場共享選股運行；停用的舊單標的 campaign 不當作正式入口。
- **共享輸入與帳戶隔離**：檢查 shared selection 的日期、設定 fingerprint、integrity seal、Top20 membership；確認兩側 evidence hash 配對與缺失／變更檢查。兩側執行 DB、recovery ledger、記憶與 safety 狀態各有路徑，切換時重套 runtime。
- **執行與中斷**：檢查兩側先分析後執行的順序、單側完成後恢復、`ANALYZING / ANALYZED / EXECUTING` 分支、固定 decision identity、未知訂單狀態與 settlement gate。新增測試特別涵蓋兩側分析之間逾時、無效意圖及不支援 schema。
- **排程與長期運行**：檢查凍結交易日集合、收盤與每日提交期限、continuous 半開區間延伸、週末／假日等待、有限 calendar retry。沿用既有 fake-clock 測試，未加入新的排程框架。
- **結果可信度**：檢查未完成回合母數、Top20 訊號比較、execution timing、最終帳戶 snapshot、無法可靠取得的績效欄位及各 observation 的 LLM 成本歸屬。
- **資源與持久化**：檢視 JSON 原子取代與 fsync、SQLite 連線管理及 bounded calendar cache；本次修復了可確認的 scheduler 重試熱迴圈。歷史結果與日誌持續保存是現有設計，沒有為節省容量刪除審計資料。

## 驗證

原始基線：`1800 passed, 335 subtests passed`。新增／擴充案例先重現原問題，再驗證修補後結果，集中在 `tests/test_long_run_ab.py`：

- 已逾時且未開始的回合必須進入 settled sessions。
- 無效 coordinator schema/status 在進入 screening 前被拒絕，日誌內容保持原樣。
- 即使 arm 已完成，未知 schema 仍被拒絕。
- run-log 恢復的無效意圖不會成為 `ANALYZED` 可配對意圖。
- 兩側分析之間過期時，沒有任何一側進入 execution；無效意圖的另一側明確結案不交易。
- 日曆短暫失敗後只延伸一次；持續失敗恰好三次嘗試且不改動視窗。
- 完成及提前停止兩種報表保留相同 Top20 與時間差母數，排除共同額外持倉。
- 共享成本、Traders 成本與其他 observation 的數字分別為 123、7、999 tokens 時，只在對應欄位計入 123 與 7，排除 999。

跨模組回歸：`214 passed, 13 subtests passed`，涵蓋 A/B、continuous timing、long-run、runtime reliability、resume drift 與模組邊界。

最終完整套件：`1809 passed, 335 subtests passed, 2 warnings`（55.73 秒）；`git diff --check` 通過。執行命令：

```sh
.venv-p2/bin/python -m pytest -q
git diff --check
```

## 實際運行邊界

- 這次沒有驗證真實帳戶、API 配額、真實 LLM 延遲或多月磁碟成長；離線成功不能推論整個 Top20 能在每日 30 分鐘提交寬限內完成。逾時現在會明確阻止不完整配對並留下紀錄。
- 既有兩個第三方 deprecation warnings 來自 `websockets.legacy` 與 LangGraph serializer 預設值；本次未為消除警告升級依賴。
- 修改 production source 會改變正式 A/B 的 experiment fingerprint。如果已有 active observation，既有 `CONFIG_DRIFT` 保護可能拒絕用新版續跑。這次沒有更改 fingerprint、刪除 active state 或改寫舊結果來繞過保護。
- 終止狀態已寫入但最終報表尚未完成的斷電情境，現行設計仍要求人工檢查，而非自動刪除 active state；這是既有防止重複建立 observation 的保守行為。
