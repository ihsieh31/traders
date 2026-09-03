# Phase A.1 實作提示詞：Paper 邊界與 Durable Order Foundation

把本文件完整貼到一個新的 Codex task。這是一個**實作 Gate**，不是驗收 Gate。

## 角色與目標

你是 `traders` 專案的 Phase A.1 實作者。工作目錄固定為：

```text
/Users/zongen/Downloads/codex/tradingAlpaca
```

目標是完成 Phase A 約 45% 的基礎工程：封死 Alpaca live trading、讓最終交易意圖 strict/fail-closed、建立唯一 execution entry，以及用 SQLite 建立 durable intent/order/fill ledger、兩層 idempotency、order state machine 與最小 `UNKNOWN` order recovery。

完成後停止，不開始 Phase A.2，不自行宣告 Phase A.1 `Accepted`。

## 必須使用 Ponytail

先讀並使用 `ponytail` skill，強度 `full`：

- 先追完整 execution call graph，再修改共同入口。
- 優先沿用現有 `TradeIntent`、`OrderIntent`、Alpaca helpers、guardrails 與 pytest。
- 優先 Python stdlib：`sqlite3`、`hashlib`、`json`、`datetime`。
- 不新增 ORM、migration framework、queue、DI framework、repository interface 或抽象 factory。
- 三張表和一個 execution service 已足夠；不要為 Phase A.2/B/C 預留 speculative scaffolding。
- 不能為了少寫 code 而省略交易 trust boundary、DB transaction、unique constraint 或錯誤處理。

## 開始前檢查

1. 讀完：

   - `PROJECT_GOALS_AND_STATUS.md`
   - `ARCHITECTURE.md`
   - `README.md`
   - `tradingagents/agents/schemas.py`
   - `tradingagents/agents/managers/risk_manager.py`
   - `tradingagents/agents/utils/structured.py`
   - `tradingagents/dataflows/alpaca_utils.py`
   - `tradingagents/dataflows/config.py`
   - `tradingagents/default_config.py`
   - WebUI/CLI/scheduler 所有 execution callers 與相關 tests

2. 記錄 `git status -sb`、HEAD SHA、`origin`、`upstream`。保留使用者既有變更；若有與本 Gate 重疊的 dirty changes，停止並列出衝突，不 reset、不覆蓋。
3. 用 `rg` 找出所有 `TradingClient`、`submit_order`、`close_position`、`execute_trade_intent`、`execute_trading_action`、`ALPACA_USE_PAPER` 與 free-text execution fallback。
4. 先跑現有相關測試與完整離線 suite，記錄 baseline。測試不得使用真實 broker/model/provider。

## 現有問題

### 1. Live trading 仍可被設定打開

目前 `get_alpaca_trading_client()` 會讀 `ALPACA_USE_PAPER`，並把結果傳給 `TradingClient(..., paper=use_paper)`。設定 false 即可能接到 real-money endpoint；README、sample env 與 UI 也仍宣稱支援 live。

### 2. Structured output failure 仍可能變成真實 action

Risk Manager structured output 失敗後會回到 free text，再從文字推導 recommendation 並建立 `TradeIntent`。WebUI 還有缺少 typed intent 時直接執行 legacy signal 的路徑。schema/provider/timeout failure 不應跨越交易邊界。

### 3. Broker call 發生在 durable record 之前

目前 order request 可直接 `submit_order()`，沒有先 commit 的 authoritative execution intent/order row。process 若在 broker 接單後、local write 前 crash，會留下孤兒委託。

### 4. 沒有兩層 idempotency

同一分析 callback 可能被執行兩次；送單 request 也沒有系統產生的 deterministic `client_order_id`。只靠 broker response 或 run log 不能防止重複下單。

### 5. 沒有 durable order lifecycle

目前送單回傳即被當成結果，缺少 `PENDING/SUBMITTING/ACCEPTED/PARTIAL/FILLED/CANCELED/REJECTED/EXPIRED/UNKNOWN` 的合法轉移與 partial-fill persistence。

## 必做解法

### A1-1 Paper-only hard lock

- 交易 client 的唯一 production factory 固定建立 `TradingClient(..., paper=True)`。
- 移除 `ALPACA_USE_PAPER` 對 execution 的控制；環境值為 false 也不能建立 live client。
- 若程式允許傳 base URL，只有明確 paper endpoint 才可啟動；未知或 live endpoint fail closed。
- README、`env.sample`、config、CLI、WebUI 移除 live 選項與 live-trading 宣稱。
- market data client 可照現況；本 Gate 封鎖的是 trading/account/order client。

### A1-2 Strict TradeIntent boundary

- Analyst、Research Manager、Trader 的文字 fallback 可保留，因它們不直接送單。
- Risk Manager 的 structured bind/invoke/validation 只要失敗，就產生明確 `INVALID/NO_TRADE` 結果；不能從 Markdown/regex/parser 猜 action。
- 缺少 schema-valid `TradeIntent` 時，execution entry 回傳 fail-closed result，broker order/close call 次數必須為 0。
- 移除 WebUI、CLI、scheduler 中 `trade_intent` 缺失時直接呼叫 legacy signal execution 的 fallback。
- 保留舊 analysis/report 顯示相容性，但相容性不得等於可交易性。

### A1-3 唯一 execution entry

- 建立一個最小 execution service，讓 WebUI、CLI、scheduler、liquidation、position flip 與 protective order 進入相同 trust boundary。
- `AlpacaUtils` 可保留 data/query helpers；production caller 不得繞過 service 直接 submit/close。
- 不要同時建立多層 facade/adapter/repository。需要一個 service、一個 SQLite store，其他沿用既有型別即可。

### A1-4 三表 SQLite ledger 與 durable outbox

使用一個 `execution.db`、Python stdlib `sqlite3` 與 schema version `1`。最低三張表：

1. `execution_intents`
   - `intent_id` primary key。
   - `decision_id` unique。
   - `run_id`、`symbol`、`action`、`target_position`、validated `payload_json`。
   - `state`、UTC `created_at`、`updated_at`。
2. `orders`
   - `order_id` primary key、`intent_id` foreign key。
   - `client_order_id` unique、`broker_order_id` nullable unique。
   - `symbol`、`side`、`quantity`/`notional`、`status`、`filled_qty`、UTC timestamps。
3. `fills`
   - broker `execution_id` primary key。
   - `order_id`、`qty`、`price`、`filled_at`。

`execution_intents` + `PENDING` order row 就是 durable outbox：

```text
validate TradeIntent
-> BEGIN SQLite transaction
-> insert/get decision_id
-> insert/get deterministic logical order
-> COMMIT
-> only then allow broker submit
```

DB commit 失敗時 broker call 必須為 0。不要新增第四張 generic outbox table，除非直接證明三表無法維持上述原子性。

### A1-5 兩層 idempotency

- 第一層：stable `decision_id` unique；同一分析結果重複 callback 只回傳既有 intent。
- `intent_id` 在第一次 persistence 後固定；restart 不重新產生。
- 第二層：每個 logical order 有 deterministic、符合 Alpaca 格式/長度限制的 `client_order_id`。
- ID 只由 canonical stable fields 產生；不能使用當下時間或隨機值造成 retry 改 ID。
- 同一 intent 需要 close-then-open 或 protective children 時，每個 logical order 的 role/sequence 必須進入 canonical ID input。
- unique constraints 是最後防線；不能只靠 application-level `if`。

### A1-6 Order state machine

允許的最小狀態：

```text
PENDING -> SUBMITTING -> ACCEPTED -> PARTIAL -> FILLED
                         |           |
                         +-----------+-> CANCELED / REJECTED / EXPIRED
SUBMITTING -- ambiguous broker result --> UNKNOWN
UNKNOWN -- lookup by client_order_id --> broker-observed state
```

- 狀態轉移集中在一個地方驗證；非法轉移拒絕並保留原狀態。
- `PARTIAL` 更新同一 order 的 `filled_qty`；本 Gate 不自動補單。
- submit timeout/connection loss 標記 `UNKNOWN`，不得直接 retry。
- A1 只需完成 order-level lookup/adopt seam；完整 BrokerSnapshot、account-wide reconciliation 與 retry orchestration 留給 A2。

## 必要測試與驗收標準

新增最少量、可重現的 tests，至少證明：

1. `ALPACA_USE_PAPER=False`、未知/live base URL 都無法建立 live trading path。
2. Risk Manager bind/invoke/validation/timeout/provider error、空輸出、非法 action 全部 `NO_TRADE`，且 `submit_order`/`close_position` 為 0 calls。
3. schema-valid `TradeIntent` 仍能進入既有 deterministic safety checks。
4. 同一 `decision_id` 並行或重複呼叫只建立一個 intent。
5. 同一 logical order 跨 restart 產生相同 `client_order_id`；不同 order role 不碰撞。
6. DB commit failure 為 0 broker calls。
7. commit 後、submit 前中止可留下可恢復 `PENDING` row。
8. submit timeout 進入 `UNKNOWN`，lookup 前無第二次 POST。
9. broker lookup 找到既有 order 時 adopt `broker_order_id`，不建立第二筆 logical order。
10. partial fill 與所有 terminal states 正確 persistence；duplicate fill 不重複入帳。
11. 非法 state transition 被拒絕。
12. production code 中所有 order/close callers 都走唯一 execution entry。
13. 既有 focused tests 與完整 `python -m pytest tests/ -q` 通過。

測試可用 temporary SQLite 與 mocked Alpaca client；本 Gate 禁止真實 Alpaca/model/provider call。

## 文件與狀態

- 更新 `PROJECT_GOALS_AND_STATUS.md`：只標記 A1–A3「Implemented，pending fresh acceptance」，不要寫 `Accepted`。
- README/env/UI 的 Paper-only 說明必須與程式一致。
- 若實作需要 deliberate simplification，依 Ponytail 留一個短註解說明 ceiling 與何時升級。

## 明確排除

不要開始：

- Full BrokerSnapshot、startup/account-wide reconciliation。
- Periodic reconciliation、freshness TTL、account-scoped lock。
- Phase A.2 的 Paper E2E。
- SEC/IR、corporate action、sector constraints、memory/correlation/regime 強化。
- Universe/screener/ranking。
- PostgreSQL、ORM、queue、distributed lease。

## 完成報告格式

1. 結果：Implemented / Blocked；不得寫 Accepted。
2. 問題與實際解法：逐項對應 A1-1～A1-6。
3. 變更檔案與關鍵入口。
4. 測試命令、passed/failed/skipped、是否有任何外部 call。
5. 尚待 A2 或 fresh acceptance 的項目。
6. `git status -sb` 與 HEAD SHA。

完成 Phase A.1 後停止，等待新的 read-only acceptance task。
