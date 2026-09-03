# Traders：專案目標與狀況

最後更新：2026-09-03

基準來源：[huygiatrng/AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent)

基準 commit：`8d9d770da9ecc108d70fd8a97caae032c53caad0`

## 1. 專案目標

把 AlpacaTradingAgent 改造成一套可長時間自動執行、**只允許 Alpaca Paper Trading**、在任何不確定狀態下停止新增風險的多人 Agent 交易系統。

本專案保留上游已完成的分析、辯論、風控、記憶、回測、WebUI、CLI 與告警能力，只補齊 P0 與 P1 的交易可靠性缺口。完成標準不是「能送出訂單」，而是重啟、逾時、重複執行、部分成交、資料過期與 broker/local 狀態不一致時，都不會猜測或重複下單。

### 成功條件

- 任何 production 路徑都無法連到 Alpaca live trading endpoint。
- 只有通過 strict schema 與 deterministic safety checks 的 `TradeIntent` 可以跨越交易邊界。
- 每個 logical order 有固定 `client_order_id`，重試不會建立第二筆訂單。
- SQLite ledger 能追蹤 intent、order、fill 與完整狀態轉移。
- 啟動及下單後 reconciliation 以 Alpaca 狀態為權威；不一致時暫停交易。
- 過期或無法取得的 account、position、quote 資料一律 `NO_TRADE`。
- 同一帳戶同一時間最多一個 execution worker。
- P1 corporate action、primary-source data 與 universe 功能保持小而可驗收。

## 2. 明確非目標

P0/P1 階段不做以下項目：

- Live trading、真實資金或切換 paper/live 的設定。
- PostgreSQL、DB role/ownership 架構或大量 migrations。
- 完整 immutable evidence ledger、source authority framework 或 security master graph。
- 搬入 Seven-Lens 全套 formal contracts；沿用現有 `TradeIntent`，只新增 execution 必要資料。
- 重寫 LangGraph、多 Agent、LLM provider、風控、回測、WebUI 或告警。
- 複雜分散式 lease；單機 SQLite/file lock 足以覆蓋目前部署模型。

需要多主機、多帳戶或實測發現 SQLite 吞吐不足時，才重新評估以上項目。

## 3. 現有架構與可沿用能力

### 執行流程

```text
WebUI / CLI
  -> 5 Analysts（Market、Social、News、Fundamentals、Macro）
  -> Bull / Bear debate -> Research Manager
  -> Trader -> Risky / Safe / Neutral debate -> Risk Manager
  -> typed TradeIntent
  -> deterministic sizing + safety guardrails
  -> Alpaca order API
```

### 主要模組

| 路徑 | 現有責任 | P0/P1 處理原則 |
|---|---|---|
| `tradingagents/graph/` | LangGraph 編排、checkpoint、signal processing | 沿用，不重寫 |
| `tradingagents/agents/` | 分析、研究、Trader、Risk Manager、schemas | 只收緊最終 structured-output 邊界 |
| `tradingagents/dataflows/alpaca_utils.py` | Alpaca data、account、position、order execution | execution reliability 的主要整合點 |
| `tradingagents/risk/` | Kelly、ATR、曝險上限與 deterministic sizing | 沿用 |
| `tradingagents/safety/` | notional/concentration、loss/drawdown/rejection breakers、kill switch | 沿用並維持 fail closed |
| `tradingagents/portfolio/` | correlation、volatility、gross exposure | 沿用 |
| `tradingagents/graph/checkpointer.py` | LangGraph SQLite resume | 與 execution ledger 分開；checkpoint 不是訂單帳本 |
| `webui/`、`cli/` | 操作介面與排程入口 | 接入同一 execution service，不各自實作交易規則 |
| `tests/` | 32 個離線 pytest 檔案 | 新增最小必要 regression tests |

### 已有且不重做

- typed `TradeIntent` / `OrderIntent`、broker-side bracket/OTO。
- deterministic position sizing、portfolio exposure、regime scaling。
- pre-trade guardrails、daily loss/drawdown/rejection circuit breakers、kill switch。
- broker position lookup 失敗時停止交易。
- SQLite graph checkpoint、decision log、Chroma memory、run audit log。
- backtest、chaos tests、CI、daily report、Telegram/webhook alerts。

## 4. 目前缺口

| 項目 | 2026-09-03 現況 | 判定 |
|---|---|---|
| Paper-only hard lock | `ALPACA_USE_PAPER` 仍可設為 false，`TradingClient(..., paper=use_paper)` | 缺少 |
| Strict execution schema | Risk Manager structured output 失敗後仍從 free text 推導 action；舊路徑也可直接用 signal 下單 | 缺少 |
| Deterministic idempotency | 系統會讀 broker 的 `client_order_id`，但送單時沒有自行產生固定 ID | 缺少 |
| Authoritative order ledger | 有 audit log、decision memory、checkpoint，但沒有 intent/order/fill ledger | 缺少 |
| Order state machine | 送單回傳即視為結果，沒有完整 partial/unknown/retry 流程 | 缺少 |
| Reconciliation | 啟動及送單後沒有 orders/fills/positions/cash 一致性檢查 | 缺少 |
| Broker snapshot authority | 執行前會查 position/account，但沒有版本化、freshness 與完整 snapshot | 部分完成 |
| Single execution lease | WebUI 有 process-local 執行狀態，沒有 account-scoped 跨 process lock | 缺少 |
| Corporate-action quarantine | 會讀 Alpaca asset/tradable 資訊，沒有 split/ticker/delisting quarantine 流程 | 缺少 |
| SEC / IR primary sources | 現有 fundamentals/news sources 未形成 SEC + IR 最小 primary-source 路徑 | 缺少 |
| Universe / screening | 可搜尋與輸入多 symbol，但沒有自動 eligibility/ranking pipeline | 缺少 |

## 5. P0：交易可靠性（必須先完成）

### P0-1 Paper-only hard lock

最小實作：移除 `ALPACA_USE_PAPER` 切換能力，交易 client 固定 `paper=True`；若 runtime/base URL 不是 paper endpoint，啟動失敗。

驗收：

- 設定 `ALPACA_USE_PAPER=False` 不能建立 live client，也不能觸發 broker call。
- CLI、WebUI、排程、close/liquidate 與 protective order 全部走同一 paper-only client factory。
- README、sample env 與 UI 不再宣稱或提供 live trading。

### P0-2 Strict schema / fail closed

最小實作：研究階段仍可使用 free text；Risk Manager 的 structured output 若 bind、invoke 或 validation 失敗，結果固定為不可執行的 `HOLD/NEUTRAL`，且不建立可送單 intent。移除 execution 層的 legacy free-text signal fallback。

驗收：

- schema failure、timeout、provider error、空輸出及非法 action 均為 `NO_TRADE`。
- 任一失敗案例中 `submit_order` 與 `close_position` 呼叫次數皆為 0。
- 正常且 schema-valid 的 `TradeIntent` 仍可通過既有 safety checks。

### P0-3 Deterministic ID + SQLite ledger

最小實作：使用 Python stdlib `sqlite3` 建立單一 `execution.db`，只存必要的 `trade_intents`、`orders`、`fills`。由穩定 logical intent fields 產生一個符合 Alpaca 長度限制的 `client_order_id`，並以 unique constraint 保證一筆 logical order 只有一個 ID。

最低必要欄位：`intent_id`、`run_id`、`symbol`、`side`、`quantity/notional`、`client_order_id`、`broker_order_id`、`status`、`filled_qty`、`created_at`、`updated_at`。

驗收：

- 同一 intent 執行兩次，只產生一個 `client_order_id` 和一筆 logical order。
- process 在 submit 前後中止，重啟後仍能判斷該查詢 broker 或安全送單。
- 不新增 ORM、migration framework、queue 或 PostgreSQL。

### P0-4 Order state machine + UNKNOWN recovery

最小狀態集：

```text
PENDING -> SUBMITTING -> ACCEPTED -> PARTIAL -> FILLED
                         |           |
                         +-> CANCELED/REJECTED
SUBMITTING -- ambiguous outcome --> UNKNOWN -- query client_order_id --> terminal/nonterminal state
```

驗收：

- 非法狀態轉移被拒絕並留在 ledger/audit 中。
- partial fill 更新 `filled_qty`，不建立新的 logical order 補單。
- submit timeout/connection loss 先依 `client_order_id` 查 broker；查清前禁止 retry。

### P0-5 Startup + post-order reconciliation

最小實作：啟動時讀取 account、positions、open/recent orders 與 fills；每次送單後再同步該 order、position、cash/equity。以 broker 為權威更新 local ledger。

驗收：

- `CLEAN` 才能交易；missing/contradictory/unknown 狀態標記 `DIRTY` 並暫停新增風險。
- restart 能恢復 accepted、partial 與 unknown order。
- broker 不可用時 fail closed；風險降低型動作是否允許必須有獨立且明確的測試。

### P0-6 Authoritative BrokerSnapshot

最小實作：建立一個 execution-facing `BrokerSnapshot`，包含 `observed_at`、account ID、equity、cash、buying power、positions、open orders、today fills 與 gross exposure；Agent memory 不得覆寫這些欄位。

驗收：

- 每次 sizing、safety 與 execution 使用同一份新鮮 snapshot。
- snapshot 不完整、account 不符或過期時不新增風險。
- audit log 可追溯本次決策使用的 snapshot timestamp/version。

## 6. P1：無人值守能力

執行順序以風險降低與工作量排序，不代表要一次做完。

### P1-1 Data/account freshness gate

- 為 quote、account、portfolio snapshot、news/primary source 定義少量明確 TTL。
- 時戳缺失、未來時間、超過 TTL 或 broker unavailable 一律 `NO_TRADE`。
- TTL 保留設定旋鈕；不建立通用 policy framework。

### P1-2 Single execution lease

- 使用 stdlib file lock 或 SQLite transaction，key 為 Alpaca account ID。
- 第二個 worker 無法取得 lease 時直接退出且不送單。
- 支援 crash 後可恢復；單機模型足夠前不做 distributed lock。

### P1-3 Corporate-action quarantine

- 只處理 split、ticker change、delisting、non-tradable。
- 偵測到異常後禁止新開倉、執行 reconciliation 並告警。
- 不建立完整 security master 或 symbol-lineage graph。

### P1-4 SEC + company IR primary sources

- 只新增 SEC filing 與公司 IR 兩條 primary-source 路徑。
- 每筆保留 `source/url/published_at/retrieved_at`；來源或時間無法驗證時不可宣稱為 primary evidence。
- 不擴充到 BEA、BLS、EIA、Treasury、GDELT 或 source-role framework。

### P1-5 Universe + screening

- 第一版產出可交易、具最低流動性的候選清單，再用少量 deterministic 指標排序。
- 每日只把前 N 名送入既有分析 pipeline，並保留人工 watchlist 模式。
- 不先做 portfolio optimizer、point-in-time research platform 或 ML ranking。

## 7. 建議里程碑與 Gate

| 里程碑 | 範圍 | 完成 Gate | 狀態 |
|---|---|---|---|
| M0 基準 | clone、架構盤點、現有測試基準、目標文件 | upstream SHA、測試結果與缺口可重現 | 完成 |
| M1 邊界封鎖 | P0-1、P0-2 | 無 live client；structured failure 零 broker call | 未開始 |
| M2 安全送單 | P0-3、P0-4 | duplicate/timeout/partial-fill tests 全通過 | 未開始 |
| M3 權威恢復 | P0-5、P0-6 | restart/reconciliation matrix 全通過 | 未開始 |
| M4 執行互斥 | P1-1、P1-2 | stale data 與雙 worker 均 fail closed | 未開始 |
| M5 長期資產安全 | P1-3 | corporate-action fixtures 觸發 quarantine | 未開始 |
| M6 研究來源 | P1-4 | SEC/IR timestamp 與 fallback 行為可驗收 | 未開始 |
| M7 候選產生 | P1-5 | 固定輸入得到 deterministic top-N | 未開始 |
| M8 Paper E2E | 全部 P0/P1 | Alpaca paper sandbox 完整走過送單、部分/取消/重啟/對帳 | 未開始 |

每個里程碑的最低發布規則：focused regression tests、完整離線 suite、無 secrets、工作樹乾淨。涉及 broker 的 Gate 需另外標示 mock evidence 與真實 Alpaca Paper evidence，不得互相替代。

## 8. 目前狀況

### 已完成

- [x] 完整 clone 上游 Git history 與工作樹。
- [x] 確認基準 commit 與主要模組責任。
- [x] 對照 P0/P1 與目前程式碼，排除上游已具備功能。
- [x] Python `compileall` 通過（`cli`、`tradingagents`、`webui`）。
- [x] Python 3.12 隔離環境完整離線 suite：`298 passed, 158 subtests passed`（21.38 秒）；4 個 warnings 均來自第三方套件。
- [x] 檢查 tracked secrets 與大於 50 MB 的工作樹檔案；未發現候選項目。

### 尚未完成／限制

- [ ] GitHub `ihsieh31/traders` 建立與 push。
- [ ] P0、P1 程式實作。
- [ ] 真實 Alpaca Paper credentials 與 broker E2E 驗證。

## 9. 已決定事項

- 保留上游 Git history；新的 `origin` 指向 `ihsieh31/traders`，原始 repo 保留為 `upstream`。
- 先完成全部 P0，再做 P1；P1 中 freshness 與 execution lease 優先。
- execution path 只有一個入口，WebUI、CLI、排程與 liquidation 不各自維護可靠性邏輯。
- SQLite 是 P0/P1 唯一新增的持久化技術；優先使用 stdlib 與既有依賴。
- 任一不確定狀態採 fail closed，不用 LLM 或字串 parser 猜測 broker action。

## 10. 下一個具體行動

建立可重現的隔離測試環境，記錄完整 baseline；接著只做 M1：paper-only client factory 與 strict execution schema boundary。M1 驗收通過前不開始 ledger 或 reconciliation。
