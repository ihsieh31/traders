# 雙 30 日 A/B 測試前最終審查

日期：2026-09-21

## 結論

系統已達到「可開始 shadow-decision A/B」的程式面條件。兩邊共用相同 graph、tool set、model、analyst set、交易日與下游決策節點，唯一實驗變數是四個非技術 analyst 的 prompt profile。本 runner 強制 `auto_trade=False`，因此這是雙邊決策品質比較，不會從 A/B runner 送出 broker order。

## 已驗證的隔離邊界

1. 記憶全開：`memory_retrieval_enabled`、`reflection_on_outcome_enabled`、`memory_maintenance_enabled` 兩邊都強制為 `True`。
2. 記憶不共享：Traders 與 Berkshire 各自擁有 `memory.md`、Chroma `agent_memory`、results、data cache、screening cache、execution DB/lock 與 long-run state；任一路徑相同即 fail closed。
3. 記憶可跨日累積：狀態位於 `<results-root>/_profiles/<profile>/`，不再每個 symbol/date 重建空記憶。
4. 執行不互擾：兩 profile 序列執行，整個 campaign 有 advisory file lock，避免 process-global config 與 audit context 重疊。
5. 順序偏誤控制：執行順序由 `SHA-256(symbol|trade_date)` 確定並在配對間交錯，不固定讓同一邊先跑。
6. 不可偷看：第一邊的結果不會進入第二邊 config、prompt、memory 或 graph state；`pair_summary.json` 在兩邊結束後才寫入。四個 profile prompt 都明確禁止輸出最終交易指令，下游 Trader/Risk 節點共用。
7. 防止中途改規則：`AB_CAMPAIGN.json` 儲存 config + analyst set 指紋；30 日期間任一共用條件改變即拒絕執行。
8. 防止挑結果：同一 symbol/date 的已完成 pair 不可覆寫重跑；未解決的 `.pair_in_progress` 也會 fail closed，必須先人工審查。
9. 不借用舊答案：LangGraph checkpoint 強制關閉。它是 crash-resume 狀態，不是學習記憶；開啟會有同 symbol/date 重用已有答案的嫌疑。

## 公平性邊界

程式可保證設定、狀態、記憶、執行順序與結果可追溯，但不能保證兩次即時網路請求的回應逐 byte 相同。新聞、搜尋與 provider 回應可在序列執行的數分鐘內改變。目前以執行順序交錯、相同 as-of gate 與 30 日多樣本降低此偏誤。若要做「輸入逐 byte 相同」的實驗，仍需另外建立 point-in-time evidence record/replay 層；現在不應對這點過度聲稱。

## WebUI 移除

WebUI 原本不只是呈現層：核心 tool wrapper、prompt capture、GPT-5 usage 與 graph update 含有對 `webui.app_state` 的執行期依賴。若直接刪目錄，tool call 會因 import error 失敗。審查已將 prompt audit 移到 `tradingagents/prompt_capture.py`，tool timeout/retry/audit 改為核心獨立路徑，並移除純 UI progress/counter 寫入。

已刪除 `webui/`、兩個 root WebUI launcher、Dash service compose、WebUI console entry point、UI-only dependencies 與 UI-only tests。Docker image 改為 CLI entry point。wheel 已重建並確認不含 `webui` 或 `run_webui` 檔案。

## 驗證證據

- `python -m compileall -q tradingagents cli scripts`：pass。
- A/B focused suite：10 passed。
- 標準 pytest suite：1384 passed、139 skipped（皆為已移除 WebUI 的過期斷言）、284 subtests passed。
- 純核心測試集：875 passed、232 subtests passed。
- Wheel build：pass，184 files，無 WebUI artifact。
- `git diff --check`：pass。

## 啟動前單一下一步

用固定的 config 啟動第一個 pair，確認根目錄產生 `AB_CAMPAIGN.json`；後續 30 日都使用同一個 `--results-root`。
