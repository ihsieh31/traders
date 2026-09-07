# Prompt 4 — DVI-1 獨立驗收：決策、Daily Loss 與績效語義

## 你的角色

你是 fresh read-only independent acceptance reviewer。

專案：`/Users/zongen/Downloads/codex/tradingAlpaca`

驗收：`DVI-1：決策完整性、daily-loss 接線與誠實績效呈現`

## 硬性限制

1. 不修改、修補、格式化、建立或刪除 repository 內任何檔案。
2. 不得 reset、checkout、clean。
3. 發現缺陷不得修，只能判 Not Accepted 並提供最小 remediation。
4. 禁止真實 LLM、Alpaca、broker POST、網路、新聞、SEC、FRED、webhook call。
5. 可使用 injected fake 及 `/tmp` 臨時 PoC。

## 前置驗證

先確認 PIT-1 仍成立。至少重跑 PIT focused tests，並靜態確認沒有重新引入：

- historical data 抓到 present；
- historical latest quote；
- historical today-regime；
- historical live-only web/crypto source；
- historical current broker context。

任何 PIT regression 直接判：

`Not Accepted — PIT regression`

## 驗收前後 source 完整性

驗收前執行：

```bash
git status --short
git diff --stat
git diff --check
```

保存所有 modified/untracked source hashes。驗收後重算，必須一致。

## 驗收矩陣

### DVI01 — 五個 Analyst 不再輸出 action

逐一檢查 Market、News、Social、Fundamentals、Macro 的 runtime prompt 與 node：

- report 契約包含 sources/as-of、observations、bullish implications、bearish implications、missing/conflicting evidence。
- runtime 不要求 BUY/HOLD/SELL/LONG/SHORT/NEUTRAL final action。
- runtime 不要求 `FINAL TRANSACTION PROPOSAL`。
- node 不再進行補 action 的第二次 LLM call。

使用 fake LLM 對五個 node 各跑至少一個 production-path PoC，記錄 invoke count 和 report text。

### DVI02 — 最終決策層沒有被誤刪

驗證：

- Research Manager 仍產生 `ResearchPlan`。
- Trader 仍產生 `TraderProposal`。
- Risk Manager 仍產生 `RiskDecision`/`TradeIntent`。
- `ExecutableAction` enum 保留。
- 只有這些決策層可以產生 executable final action。

### DVI03 — Parallel failure 必須停止

從真實 graph/setup public path 建立 fake round：

- 選擇至少 market、news。
- market 正常。
- news 丟一般 RuntimeError，不是 ProviderFailure。

記錄並要求：

- round STOPPED/exception/NO_TRADE，不能 completed。
- coordinator 不得回傳原 state 假裝 news 完成。
- Bull/Bear/Research Manager/Trader/Risk Manager invoke count=0。
- execution intent rows=0。
- broker POST/mutation=0。

### DVI04 — Sequential failure 與 Parallel 一致

用相同 fake，設定 `parallel_analysts=False`。

所有 DVI03 結果必須相同。若只修 parallel path，Fail。

### DVI05 — Coverage gate 精確性

驗證以下四例：

1. selected=`[market, news]`，兩份 completed/non-empty → Pass。
2. selected=`[market, news]`，news status failed → 拒絕。
3. selected=`[market, news]`，news report 空白 → 拒絕。
4. selected=`[market, news]`，fundamentals/macro/social 缺失 → 不得拒絕。

另外靜態確認 gate 依 structured status + report presence，不是大量 prose keyword 猜測。

### DVI06 — Heuristic score 語義

檢查所有會看到 matrix 的 Research Manager、Trader、Risk Manager prompt 及 rendered context：

- 名稱為 `Heuristic claim priority matrix` 或完全等義。
- 明示不是 source verification。
- 明示不是 confidence、probability、win rate、independent vote。
- 明示 numeric-looking text 仍可能錯。
- 明示不能單靠高 score 給 high confidence。

不要要求 scorer 能查證事實；本 Gate 驗的是不誤導。

### DVI07 — BrokerSnapshot 取得 last_equity

從 `capture_broker_snapshot` public function 注入 fake account：

健康案例：

```text
equity=95000
last_equity=100000
```

要求 snapshot 精確保存兩值。

分別測：missing、None、`"bad"`、NaN、inf、0、負值。全部必須 fail closed，不得以 equity 補值。

### DVI08 — Initial submission Daily Loss

使用 `ExecutionService.execute` 正式入口與完整 fake snapshot/quote/order environment。

案例 A：

- equity=95,000
- last_equity=100,000
- daily_loss_halt_pct=10

要求 daily-loss check 為 pass，之後是否下單由其他 guard 決定。

案例 B：

- equity=89,000
- last_equity=100,000
- daily_loss_halt_pct=10

要求：

- opening exposure 被拒。
- broker POST=0。
- safety result 顯示 daily_loss fail，不是 skipped。

### DVI09 — Recovery 不能繞過 Daily Loss

在 temporary SQLite 建立 pending/submitting opening intent，走 production recovery method。

使用 89k/100k fake snapshot：

- 不得補送 order。
- broker POST=0。
- local result 必須清楚記錄 safety block。

若 initial path 正確但 recovery 仍送單，Fail。

### DVI10 — Reducing exit 保留

同樣 89k/100k：

- 已由 broker snapshot 核實的減倉/平倉不被 daily-loss breaker 困住。
- kill switch 仍可以阻擋所有 order。
- opening order 仍不可送。

### DVI11 — 所有 Snapshot constructor 已更新

搜尋全專案 `BrokerSnapshot(`：

- production constructor 都帶真實 last_equity。
- test fixtures 都明確提供 last_equity。
- 不得存在使用預設值掩蓋漏接線的 dataclass default。

### DVI12 — Backtest UI 誠實

檢查 source 與 component render：

- 顯示 `Recorded Signal Diagnostic`。
- 不含 `Out-of-sample windows`。
- 顯示 `Segmented diagnostic windows`。
- 顯著說明不是 OOS training、不是完整 portfolio backtest、不證明收益。
- 顯示未重播 screening、live sizing、protective orders、完整成本。

同時確認 engine 的 next-open、commission、slippage 公式沒有被改動。

### DVI13 — Teach Memory 語義

- UI 不含 `realized next-open return`。
- 顯示 `fixed-horizon hypothetical position return` 或完全等義。
- `teach.py` metadata 仍為 `hypothetical_position_return`。
- incomplete horizon 仍被跳過。
- 不得稱為 broker realized P&L。

### DVI14 — Long-run attribution

用既有 fake round/report builder 產生 JSON 與 Markdown：

- JSON 保留原有 return 欄位。
- `return_kind=unadjusted_account_equity_change` 或完全等義。
- limitations 包含 deposits/withdrawals、pre-existing positions、not pure attribution、not net profitability。
- Markdown 在報酬附近顯示同樣限制。
- 不要求本輪有 TWR ledger。

### DVI15 — 文件沒有收益誤導

檢查 README、STRATEGY_REPAIR_PLAN、backtest、long-run、WebUI：

- Top40 權重被標為 research baseline，不是 validated alpha。
- 30 天 observation 是 operational observation，不是收益證明。
- execution E2E 不得被描述為 profitability evidence。
- 不得刪除或篡改既有歷史驗收紀錄。

### DVI16 — 完整離線回歸

執行：

```bash
python -m pytest tests/test_decision_validation_integrity.py tests/test_phase_b_retry_stop.py tests/test_trading_graph_mocked.py tests/test_report_context_scoring.py -q
python -m pytest tests/test_safety_guardrails.py tests/test_phase_a2_broker_authority.py tests/test_phase_a1_execution_foundation.py tests/test_chaos_resilience.py -q
python -m pytest tests/test_backtest_engine.py tests/test_backtest_teach.py tests/test_backtest_webui.py tests/test_phase_d_long_run.py -q
python -m pytest tests/test_historical_asof_boundary.py -q
python -m pytest tests/ -q
python -m compileall tradingagents webui
git diff --check
```

所有 external transport 必須為 0。驗收不得要求網路權限。

## 判定規則

以下任一情況直接 Not Accepted：

- 任一 analyst 仍輸出 executable final proposal。
- analyst exception 後仍呼叫下游決策層。
- coverage gate 誤要求未選 analyst，或漏擋 selected missing report。
- initial/recovery 任何 opening path 未使用 last_equity。
- -11% daily loss 仍送出 opening broker POST。
- snapshot 猜測 last_equity。
- reducing exit 被 daily-loss breaker 困住。
- UI 仍稱 `Out-of-sample windows`。
- long-run equity change 被稱為純策略淨收益。
- PIT-1 regression。
- 真實外部 call 或驗收造成 source mutation。
- 完整離線 suite、compileall 或 diff check 失敗。

只有 DVI01–DVI16 全部 Pass、無 public bypass，才可判定：

`Accepted — DVI-1`

## 最終報告格式

1. Verdict。
2. DVI01–DVI16 表格：Pass/Fail、直接證據、檔案與行號。
3. 每個 PoC 的 LLM node、execution intent、broker GET/POST/mutation 計數。
4. focused/full suite/compile/diff 結果。
5. 驗收前後 source hash 是否一致。
6. 未涵蓋限制：完整 portfolio simulator、historical delisted universe、benchmark/alpha/CI、cash-flow adjusted TWR、長期前瞻收益。
7. 若失敗，只列最小 remediation；不得修改檔案。

