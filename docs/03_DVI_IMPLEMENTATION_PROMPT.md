# Prompt 3 — DVI-1 實作：決策完整性、Daily Loss 與誠實績效呈現

## 你的角色

你是第二階段實作者。專案位置：

`/Users/zongen/Downloads/codex/tradingAlpaca`

任務：`DVI-1：決策完整性、daily-loss 接線與誠實績效呈現最小修復`

前置條件：PIT-1 已經由另一個 task 判定 Accepted。若看不到 PIT-1 修復，停止並回報前置未成立，不要在本任務重做 PIT-1。

工作樹已有其他人的修改。不得 reset、checkout、clean 或還原無關變更。

完成後只能回報：

`Implemented — pending independent acceptance`

## Ponytail ceiling

本輪只修四個行為：

1. Analyst 不再提前給 executable action。
2. Selected analyst 失敗或缺報告時，停止下游決策。
3. `last_equity` 真正接入 initial submit 與 recovery daily-loss gate。
4. Backtest/Teach Memory/Long-run 不再暗示已證明收益。

不得建立：

- 完整 portfolio simulator；
- 新資料庫或 migration framework；
- historical security master；
- citation graph；
- 新 workflow engine；
- Kelly calibration；
- benchmark downloader。

不得改 screening 因子權重、broker order state machine、idempotency、bracket/OTO 語義或報酬計算公式。

## 修改 A：Analyst 只產生研究報告，不產生 action

### A1. 修改五個 analyst prompt

檔案：

- `tradingagents/prompts/templates/analysts/market_system.md`
- `tradingagents/prompts/templates/analysts/news_system.md`
- `tradingagents/prompts/templates/analysts/social_system.md`
- `tradingagents/prompts/templates/analysts/fundamentals_system.md`
- `tradingagents/prompts/templates/analysts/macro_system.md`
- `tradingagents/prompts/templates/shared/analyst_tool_system.md`

每個 analyst 的固定輸出契約改為：

```text
1. As-of and sources used
2. Verified observations / supplied evidence
3. Bullish implications
4. Bearish implications
5. Missing or conflicting evidence
6. Horizon relevance

Do not output BUY, HOLD, SELL, LONG, SHORT or NEUTRAL as a final recommendation.
Do not output FINAL TRANSACTION PROPOSAL.
The Research Manager, Trader and Risk Manager own executable decisions.
```

可以在事實敘述中出現一般英文 `buyback`、`short interest` 等詞；禁止的是 analyst-level final action。

### A2. 移除五個 analyst 的補 action LLM call

檔案：

- `tradingagents/agents/analysts/market_analyst.py`
- `news_analyst.py`
- `social_media_analyst.py`
- `fundamentals_analyst.py`
- `macro_analyst.py`

每個檔案目前都有等效邏輯：

```python
if "FINAL TRANSACTION PROPOSAL:" not in analysis_content:
    final_prompt = ...
    final_result = llm.invoke(final_prompt)
    analysis_content += final_result.content
```

完整移除這段補 action 呼叫。第一次完成工具迴圈後的 `analysis_content` 就是 analyst report。

空內容不可自行補 BUY/HOLD/SELL；交給下方 coverage failure 處理。

`*_final_recommendation.md` 可保留為未使用歷史檔案以減少刪檔風險，但 runtime 不得再引用。若保留，在檔案首行加註 `Legacy unused template`，不要讓 loader/runtime 使用。

### A3. 不動最終決策層

以下仍必須產生 structured action：

- Research Manager → `ResearchPlan`
- Trader → `TraderProposal`
- Risk Manager → `RiskDecision` / `TradeIntent`

不要更改 `ExecutableAction` enum。

## 修改 B：Selected analyst coverage fail closed

### B1. 新增最小狀態欄位

在既有 `AgentState` TypedDict 加入：

```python
analysis_status: dict[str, str]
analysis_errors: dict[str, str]
```

固定 status 只使用：

- `completed`
- `failed`

不要新增 class hierarchy。

### B2. 每個 analyst node 的成功／失敗

五個 analyst node：

- 成功產出 non-empty report 時，同時回傳該 analyst 的 `analysis_status[name] = "completed"`。
- broad exception 不得再回傳警告文字假裝 report completed。
- exception 時記錄 sanitized error，然後重新 raise。
- `ProviderFailure` 保留既有原始型別並向上傳播。

不要用報告 prose 內是否含 `error` 作主要成功判斷。

### B3. Parallel coordinator

檔案：`tradingagents/graph/setup.py`

修改 `_create_parallel_analysts_coordinator`：

- 一般 exception 不得 `return analyst_type, analyst_state`。
- 取消尚未開始的 futures，然後重新 raise。
- 不得把失敗 analyst 標成 UI completed；應標 error/failed（使用既有 UI status 能接受的最接近值）。
- 任一 selected analyst 失敗，整個 Parallel Analysts node 失敗，不能 merge partial reports 後繼續。

### B4. Coverage gate 的固定位置

不要新增 LangGraph node。把小型 validation function 放在 `tradingagents/agents/utils/report_context.py`，並在 `create_report_context_node` 一開始執行。

function signature 固定為：

```python
def validate_selected_analyst_coverage(
    state: dict,
    selected_analysts: list[str],
) -> None:
```

規則：

| analyst 名稱 | 必要 report key |
|---|---|
| market | market_report |
| social | sentiment_report |
| news | news_report |
| fundamentals | fundamentals_report |
| macro | macro_report |

對每個 selected analyst：

1. `analysis_status[name]` 必須為 `completed`。
2. 對應 report 必須是 non-empty string。
3. 缺失時丟出單一明確例外，例如 `AnalysisCoverageError`。

不要要求未被選擇的 analyst。

在 `setup.py` 呼叫 `create_report_context_node` 時，把該次實際的 `selected_analysts` 傳入 factory。不要從 global config 猜測。

例外向上傳播後，沿用現有 run logger/long-run stop 機制，結果應為 STOPPED/NO_TRADE。不要在本輪新增另一套停止狀態機。

## 修改 C：Evidence score 明確降級為閱讀排序

不要重寫 scorer，也不要更改 JSON key，以免造成不必要相容性破壞。

只修改 LLM-visible 文字：

- `tradingagents/prompts/templates/managers/research_manager.md`
- `tradingagents/prompts/templates/managers/risk_manager.md`
- `tradingagents/prompts/templates/trader/*`
- `report_context.py` 的 rendered heading/guidance

統一使用：

`Heuristic claim priority matrix`

在每個會看到 matrix 的 prompt 加入以下等義規則：

```text
The priority score is only a reading-order heuristic. It is not source verification,
model confidence, probability, win rate or an independent vote. Numeric-looking text
may still be wrong. Inspect the supplied excerpt, source label and as-of date before
using a claim. Never assign high confidence solely because this score is high.
```

Research Manager 必須先處理缺失來源與矛盾，再做 action。不要改現有權重公式。

## 修改 D：接通 Daily Loss baseline

### D1. BrokerSnapshot

檔案：`tradingagents/execution/authority.py`

在 `BrokerSnapshot` 的 `equity` 後加入：

```python
last_equity: float
```

在 `capture_broker_snapshot`：

```python
last_equity = _number(
    _value(account, "last_equity"),
    field="account last equity",
    minimum=0,
)
if last_equity <= 0:
    raise BrokerAuthorityError("account last equity must be positive")
```

建立 snapshot 時必須帶入 `last_equity=last_equity`。

不可使用 `equity`、cash、buying_power 或 HWM 補缺失值。

### D2. Initial submission 與 recovery

搜尋所有：

```python
guard.check_order(... account={...})
```

凡是使用 BrokerSnapshot 的 execution path，都固定傳：

```python
account={
    "equity": snapshot.equity,
    "last_equity": snapshot.last_equity,
}
```

至少涵蓋：

- initial submission；
- pending/submitting/unknown recovery 或 resubmission；
- 其他會增加 exposure 的正式入口。

不得只修 WebUI safety status。

### D3. 缺 baseline 的語義

- production snapshot 缺/NaN/inf/0/負 last_equity：snapshot capture fail closed。
- exposure-increasing order 不得送 broker POST。
- verified risk-reducing exit 保留既有 bypass daily-loss 語義，不能被困住。
- kill switch 仍高於 reducing-exit 例外。

更新所有直接建立 `BrokerSnapshot(...)` 的 tests/fixtures，通常使用與 equity 相同的健康 `last_equity`。

在註解及文件加一行限制：broker last_equity 仍可能受入出金影響，本輪沒有實作 cash-flow adjusted TWR。

## 修改 E：誠實呈現績效

### E1. Backtest UI 固定文案

修改：

- `webui/components/backtest_panel.py`
- `webui/callbacks/backtest_callbacks.py`

精確替換：

- `Walk-Forward Backtest` → `Recorded Signal Diagnostic`
- `Out-of-sample windows` → `Segmented diagnostic windows`
- `Run Backtest` 按鈕可保留，避免不必要 UI 相容修改。

在面板標題下顯示：

```text
Replays recorded single-symbol signals. This is not out-of-sample training or a
full portfolio backtest and does not prove profitability. Screening, live sizing,
protective-order behavior and complete research/trading costs are not replayed.
```

不要改 equity curve、metrics cards、next-open fill、commission 或 slippage。

### E2. Teach Memory 文案

將 UI 的：

`realized next-open return`

改成：

`fixed-horizon hypothetical position return`

保留 `backtest/teach.py` 目前 `hypothetical_position_return` metadata 與 incomplete horizon skip。

### E3. Long-run report

在 `tradingagents/long_run.py` 產生 final JSON 的位置保留既有 return 欄位，另新增：

```python
"return_kind": "unadjusted_account_equity_change",
"return_limitations": [
    "Not adjusted for deposits or withdrawals",
    "May include positions that existed before the observation",
    "Not pure strategy attribution",
    "Not net profitability unless all research and trading costs are included",
],
```

Markdown final report 在總報酬附近顯示同樣限制。不要實作 TWR ledger。

### E4. 文件

在 README 當前限制區與 `docs/STRATEGY_REPAIR_PLAN.md` 當前狀態補充或確認：

- Top40 權重是 research baseline，不是 validated alpha。
- 30 天 observation 是 operational observation，不是 profitability proof。
- 不刪除或重寫既有歷史驗收紀錄。

## 測試實作

優先擴充既有測試，不要新增超過一個新測試檔。若需要，新檔固定為：

`tests/test_decision_validation_integrity.py`

至少覆蓋：

1. 五個 analyst 正常報告不含 `FINAL TRANSACTION PROPOSAL`。
2. 每個 analyst 只發生原本必要的 LLM/tool loop，不再多一次補 action call。
3. Research Manager、Trader、Risk Manager 仍能輸出 structured action。
4. parallel 任一 selected analyst exception 時，下游 call count=0。
5. sequential 任一 selected analyst exception 時，下游 call count=0。
6. execution intent rows=0、broker mutation=0。
7. 只選 market/news 時不要求其他三份 report。
8. selected report 缺失或 status failed 時 coverage gate 拒絕。
9. matrix prompt 明示 heuristic，不是 confidence/probability/win rate。
10. BrokerSnapshot 保存 last_equity。
11. last_equity 缺/NaN/inf/0/負數時 capture fail closed。
12. initial submit：95k/100k 在 10% threshold 不因 daily loss 被擋；89k/100k 被擋且 POST=0。
13. recovery path：89k/100k 不補送且 POST=0。
14. risk-reducing exit 在同樣虧損下仍可走原有流程。
15. WebUI source/output 不含 `Out-of-sample windows`。
16. Teach UI 不含 `realized next-open return`。
17. Long-run JSON 與 Markdown 含 return limitations。
18. Backtest next-open、commission、slippage 原測試仍通過。

## 執行驗證

```bash
python -m pytest tests/test_decision_validation_integrity.py tests/test_phase_b_retry_stop.py tests/test_trading_graph_mocked.py tests/test_report_context_scoring.py -q
python -m pytest tests/test_safety_guardrails.py tests/test_phase_a2_broker_authority.py tests/test_phase_a1_execution_foundation.py tests/test_chaos_resilience.py -q
python -m pytest tests/test_backtest_engine.py tests/test_backtest_teach.py tests/test_backtest_webui.py tests/test_phase_d_long_run.py -q
python -m pytest tests/test_historical_asof_boundary.py -q
python -m pytest tests/ -q
python -m compileall tradingagents webui
git diff --check
```

所有測試必須完全離線。

## 完成回報

1. `Implemented — pending independent acceptance`
2. 修改檔案清單。
3. A–E 每項修改前後行為。
4. focused/full suite/compile/diff 結果。
5. external call=0、broker POST/mutation=0。
6. 明確未解決：完整 portfolio simulation、benchmark/alpha 統計、cash-flow adjusted TWR、長期前瞻收益證明。

