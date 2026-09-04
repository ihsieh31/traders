# Traders：專案目標、執行計劃與目前狀況

版本：2.1

最後更新：2026-09-05

GitHub：[ihsieh31/traders](https://github.com/ihsieh31/traders)

本機：`/Users/zongen/Downloads/codex/tradingAlpaca`

上游：[huygiatrng/AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent) @ `8d9d770da9ecc108d70fd8a97caae032c53caad0`

## 1. 唯一目標

> **保留 AlpacaTradingAgent 的研究與策略系統，只重做最後「TradeIntent → Alpaca Paper → broker 真實狀態」這一段。**

Phase A 完成後，系統必須能在單機、單 Alpaca Paper 帳戶上長時間自動執行。程式遇到重啟、重複 callback、部分成交、broker timeout、資料過期或本機與 broker 狀態不一致時，必須停止新增風險，不能猜測或盲目重送。

## 2. 現階段範圍

### Active：Phase A — 安全可靠的 Paper execution

Phase A 是目前唯一實作範圍，包含原計劃中真正必要的 P0＋P1：

1. Paper-only hard lock。
2. Strict `TradeIntent`，交易邊界 fail closed。
3. 兩層 idempotency：analysis decision/intent，以及 broker `client_order_id`。
4. SQLite execution ledger，以及先 commit、後送單的 durable outbox。
5. Order state machine、partial fill 與 `UNKNOWN` recovery。
6. 固定的 broker GET/POST timeout 與 retry policy。
7. BrokerSnapshot 作為 account、position、order、fill、cash 的唯一權威來源。
8. Startup、送單後及週期性 reconciliation。
9. Account、position、order、quote freshness gate。
10. Account-scoped single execution lock。

Phase A 完成且取得真實 Alpaca Paper E2E 證據後，才可開始長期 Paper run。

### Deferred：Phase B — 策略品質

Phase B 不阻擋 Phase A 完成：

- SEC filing 與 company IR primary sources。
- Corporate-action quarantine：split、ticker change、delisting、non-tradable。
- Sector exposure constraints。
- 驗證並調整既有 correlation、regime、memory/reflection；不重寫。

### Deferred：Phase C — 自動選股

最後才做：

```text
Universe -> liquidity/eligibility filter -> deterministic ranking
         -> top-N candidates -> existing multi-agent analysis
```

Phase A、B 一律先用人工 watchlist。這能避免在 execution 尚未可靠前，同時引入整個市場的資料與資本競爭複雜度。

## 3. 不做的事情

- Live trading、真實資金或 paper/live 切換設定。
- PostgreSQL、DB roles、ORM、message queue 或 distributed lock。
- 完整 immutable evidence platform、security master、symbol-lineage graph。
- 20+ contracts、source authority hierarchy 或多輪 Risk rejection/resubmission。
- 重寫 LangGraph、Agent debate、LLM provider、WebUI、CLI、backtest 或 alerts。
- 強化 ChromaDB memory、Kelly sizing、correlation 或 regime intelligence。
- Phase A 期間建立 SEC/IR ingestion framework 或 automatic universe。

需要多機、多帳戶或量測證明 SQLite 不足時，才重新評估儲存與分散式架構。

## 4. 現有架構與保留項目

### 現有流程

```text
WebUI / CLI
  -> Market / Social / News / Fundamentals / Macro analysts
  -> Bull / Bear -> Research Manager
  -> Trader -> Risky / Safe / Neutral -> Risk Manager
  -> typed TradeIntent
  -> sizing + safety guardrails
  -> Alpaca order API
```

### 直接沿用

- typed `TradeIntent` / `OrderIntent`。
- broker-side bracket/OTO orders。
- ATR、單筆 notional、單股 concentration、gross exposure 限制。
- daily loss、drawdown、rejection breakers 與 kill switch。
- broker position lookup 失敗時停止交易。
- LangGraph SQLite checkpoint、decision log、Chroma memory、run audit log。
- portfolio correlation、volatility/regime scaling 的現有實作。
- backtest、chaos tests、CI、daily report、Telegram/webhook alerts。

Kelly 已存在但 `risk_sizing_enabled=False`；Phase A 保持關閉，不刪除也不強化。AI confidence 不是已校準勝率，不能把它直接當 Kelly edge。

## 5. Phase A 目標架構

```text
Risk Manager
    |
    v
strict TradeIntent -- invalid --> NO_TRADE
    |
    v
decision_id unique
    |
    v
SQLite transaction:
  persist execution_intent + order row (PENDING)
  COMMIT                         <- durable outbox boundary
    |
    v
account-scoped execution lock
    |
    v
fresh BrokerSnapshot + reconciliation == CLEAN
    |
    v
existing deterministic safety checks
    |
    v
SUBMITTING -> Alpaca Paper with deterministic client_order_id
    |
    +-- response --> ACCEPTED / PARTIAL / FILLED / terminal failure
    |
    +-- timeout --> UNKNOWN -> query client_order_id before any retry
    |
    v
post-order reconciliation -> authoritative local ledger
```

WebUI、CLI、scheduler、liquidation 與 protective orders 必須進入同一 execution service。其他模組不得直接呼叫 `submit_order()` 或 `close_position()`。

## 6. 最小 execution data model

只使用 Python stdlib `sqlite3` 和三張表。`execution_intents` 的 pending row 同時就是 durable outbox，不另外建立 queue 或 outbox framework。

### `execution_intents`

- `intent_id` primary key。
- `decision_id` unique：同一份分析結果只能建立一次 execution intent。
- `run_id`、`symbol`、`action`、`target_position`。
- `payload_json`：驗證完成的 `TradeIntent`。
- `state`、`created_at`、`updated_at`。

### `orders`

- `order_id` primary key。
- `intent_id` foreign key。
- `client_order_id` unique：同一 logical order 永遠使用相同 ID。
- `broker_order_id` nullable unique。
- `symbol`、`side`、`quantity` 或 `notional`。
- `status`、`filled_qty`、`created_at`、`updated_at`。

### `fills`

- `execution_id` primary key：重複同步同一 fill 不會重複入帳。
- `order_id`、`qty`、`price`、`filled_at`。

必要 index 與 unique constraints 直接寫在 SQLite schema；Phase A 不使用 ORM 或 migration framework。

## 7. 不可協商的規則

### 7.1 Paper-only

- 交易 client 固定 `paper=True`。
- production code、設定、sample env、CLI 與 WebUI 移除 live 選項與宣稱。
- 偵測到非 paper endpoint 或無法確認 endpoint 時 startup failed，broker call 為 0。

### 7.2 Strict execution boundary

- Analyst、Research Manager 與 Trader 仍可 free-text fallback。
- Risk Manager structured bind、invoke、validation、timeout、429 或 provider failure，一律 `INVALID/NO_TRADE`。
- 不從 free text、Markdown 或 legacy signal 猜 `BUY/SELL/LONG/SHORT`。
- 只有 schema-valid `TradeIntent` 能建立 `execution_intent`。

### 7.3 兩層 idempotency

```text
analysis decision_id (unique)
        -> execution intent_id
        -> one or more logical orders
        -> deterministic client_order_id (unique)
```

- callback、scheduler 或 process 重複處理同一 `decision_id` 時，只回傳既有 intent。
- 重啟後沿用原 `intent_id` 與 `client_order_id`，不能重新產生。
- protective child order 也必須可關聯至原 intent 與 broker parent order。

### 7.4 Durable outbox

- 必須先在一個 SQLite transaction 內寫入 intent 與 `PENDING` order，commit 成功後 executor 才能呼叫 Alpaca。
- DB commit 失敗：零 broker call。
- commit 後、submit 前 crash：startup recovery 重新取得該 row 並安全處理。
- submit 成功、回寫 DB 前 crash：依既有 `client_order_id` 查 broker 並 adopt，不建立新 logical order。

### 7.5 Order state machine

```text
PENDING -> SUBMITTING -> ACCEPTED -> PARTIAL -> FILLED
                         |           |
                         +-----------+-> CANCELED / REJECTED / EXPIRED

SUBMITTING -- ambiguous outcome --> UNKNOWN
UNKNOWN -- broker lookup --> ACCEPTED / PARTIAL / FILLED / terminal state
```

- 非法狀態轉移拒絕並寫 audit。
- `PARTIAL` 更新 `filled_qty`，不自動建立補單。
- `UNKNOWN` 未解決前，該帳戶保持 `PAUSED` 且禁止新增風險。

### 7.6 固定 retry policy

- Idempotent GET failure：最多 3 次、短暫 exponential backoff；之後 fail closed。
- POST validation/rejection：不 retry，直接記錄 terminal state。
- POST submit timeout/connection loss：不直接 retry，先標記 `UNKNOWN`。
- `UNKNOWN` 以同一 `client_order_id` 做 bounded lookup；找到即 adopt。
- broker 明確回覆不存在後，才可用**同一個** `client_order_id` 再 submit；仍不確定則保持 `PAUSED`。

### 7.7 Broker authority 與 reconciliation

`BrokerSnapshot` 至少包含：

- `observed_at`、account ID、equity、cash、buying power。
- positions、open/recent orders、today fills、gross exposure。

執行前 sizing、safety 與送單使用同一份 snapshot。Agent memory、checkpoint、audit log 與 local ledger 都不能覆寫 broker facts。

只有 reconciliation=`CLEAN` 才能新增風險。以下全部轉為 `PAUSED`：

- position mismatch。
- unknown order 或 duplicate `client_order_id`。
- cash/equity/account unavailable。
- unresolved partial fill。
- broker timeout 尚未解決。
- snapshot 不完整、account ID 不符或過期。

Startup、每次 order action 後及 scheduler 每輪開始前都要 reconciliation。風險降低型 exit 只有在 broker position 已即時確認，且沒有矛盾 open order 時才能執行；不能根據 stale local state 猜測。

### 7.8 Freshness

Phase A 只 gate execution 必要資料：account、positions、orders、fills 與 quote。TTL 使用少量明確預設值並集中設定；缺時戳、未來時間或超過 TTL 都是 `NO_TRADE`。News/SEC/IR freshness 留到 Phase B。

### 7.9 Single execution lock

- 使用 SQLite transaction 或 stdlib file lock，以 Alpaca account ID 為 key。
- 第二個 process 取不到 lock 時立即退出 execution，不送單。
- crash 後可恢復；Phase A 不做 distributed lease。

## 8. 實作順序與完成 Gate

| Gate | 工作 | 必須通過的證據 | 狀態 |
|---|---|---|---|
| A0 基準 | clone、架構盤點、計劃、離線 suite | upstream SHA；`298 passed, 158 subtests passed` | 完成 |
| A1 封死邊界 | Paper-only、strict TradeIntent、集中 execution entry | live config 無法送單；所有 structured failure 零 broker call | Accepted |
| A2 Durable intent | 三表 SQLite、兩層 ID、commit-before-submit | duplicate callback 與三個 crash points 都不重複送單 | Accepted |
| A3 Order recovery | state machine、partial fill、UNKNOWN、固定 retry | timeout/restart/partial/terminal transition tests | Accepted |
| A4 Broker authority | BrokerSnapshot、startup/post-order reconciliation | mismatch/unavailable/duplicate/unknown 全部 `PAUSED` | Accepted |
| A5 無人值守 | freshness、single lock、periodic reconciliation | stale snapshot 與雙 process 都是零新增曝險 | Accepted |
| A6 Paper E2E | 真實 Alpaca Paper sandbox | submit、partial/cancel、timeout recovery、restart、reconcile 證據 | 完成（submit→fill→adopt→reconcile→verified close→flat 已在真實 Paper 驗證；partial/timeout branch 由 deterministic mock/chaos 證據覆蓋） |

規則：一次只做一個 Gate。每個 Gate 要有 focused regression、完整離線 suite 與乾淨工作樹；mock evidence 與真實 Alpaca Paper evidence 分開報告。A6 已於 fresh acceptance 通過；長期無人值守前仍需一段穩定 Paper observation。

## 9. Phase A 提示詞執行順序

工程量按依賴切成兩組，不允許跳步或由執行者自行選 Gate：

1. [Phase A.1 實作](PHASE_A1_IMPLEMENTATION_PROMPT.md)：A1–A3，約 45%。
2. [Phase A.1 fresh read-only 驗收](PHASE_A1_ACCEPTANCE_PROMPT.md)。
3. [Phase A.2 實作](PHASE_A2_IMPLEMENTATION_PROMPT.md)：A4–A6，約 55%；前提是 A.1 已 Accepted。
4. [Phase A.2 fresh read-only 驗收](PHASE_A2_ACCEPTANCE_PROMPT.md)：Phase A 最終 Gate。

Implementation 只能回報 Implemented/pending acceptance；只有獨立 acceptance task 能給 Accepted。任何驗收中發生的修復都必須另開 remediation task，修復後再做 fresh acceptance。

## 10. Phase A 驗收矩陣

至少覆蓋以下情境：

| 情境 | 預期結果 |
|---|---|
| `ALPACA_USE_PAPER=False` 或非 paper endpoint | startup failed；零 broker call |
| Risk Manager schema/timeout/429/provider failure | `NO_TRADE`；零 order/close call |
| 同一 `decision_id` 觸發兩次 | 回傳同一 intent；只產生一組 logical orders |
| DB commit 前 crash | 無 broker order |
| DB commit 後、submit 前 crash | restart 從 `PENDING` 恢復 |
| submit 後、ACK 寫 DB 前 crash | 以 `client_order_id` adopt broker order |
| POST timeout | `UNKNOWN`；lookup 前零 retry |
| partial fill | 更新同一 order；不自動補單 |
| broker/local position mismatch | `PAUSED`；禁止新增風險 |
| account/cash/equity unavailable | `PAUSED` |
| stale/invalid timestamp | `NO_TRADE` |
| 兩個 executor 同時啟動 | 只有 lock owner 可執行 |
| verified risk-reducing exit | 依明確 policy 執行並立即 reconcile |

## 11. Phase B 與 C 的進入條件

### Phase B

只有 A6 通過並完成一段穩定 Paper observation 後開始。每項功能各自證明能改善資料品質或風險控制；沒有證據就不擴建。

Corporate action 只做：偵測 split/ticker change/delisting/non-tradable → quarantine → 禁止新增曝險 → reconcile → alert。SEC/IR 只保留 `source`、`url`、`published_at`、`retrieved_at`，不建立 evidence platform。

### Phase C

只有人工 watchlist 已穩定運作且使用者確認需要全市場自動選股時開始。第一版只用 Alpaca tradability、最低流動性與少量 deterministic 指標產生 top-N；不做 ML ranking、portfolio optimizer 或 point-in-time research platform。

## 12. 目前狀況

### 已完成

- [x] 完整 clone 上游 Git history；`upstream` 保留原 repo。
- [x] 公開 GitHub repo `ihsieh31/traders`；`origin/main` 已同步。
- [x] 現有 execution、structured output、risk、safety、WebUI/CLI 路徑盤點。
- [x] Python `compileall` 通過。
- [x] Python 3.12 隔離環境：`298 passed, 158 subtests passed`（21.38 秒）；4 warnings 均來自第三方套件。
- [x] tracked secrets 與大於 50 MB 檔案檢查未發現候選項目。
- [x] 計劃重整為 Phase A/B/C；durable outbox、fail-closed matrix、兩層 idempotency 與 retry policy 已明文化。
- [x] Phase A 已拆成兩份 implementation 與兩份 fresh read-only acceptance prompts。
- [x] Phase A.1 實作（A1–A3，約 45%）：paper-only hard lock、strict TradeIntent、唯一 execution entry、三表 SQLite durable outbox、兩層 idempotency、order state machine 與最小 UNKNOWN lookup/adopt — Accepted（fresh read-only acceptance 通過：`321 passed, 158 subtests passed`，對抗 PoC 9/9，mock only，無外部 call）。
- [x] Phase A.2 A4/A5 實作：immutable `BrokerSnapshot`、固定 GET/POST policy、startup/post-order/periodic reconciliation、durable `CLEAN`/`PAUSED` reasons、UTC freshness gate、account-scoped OS lock 與 verified risk-reducing exit — Accepted（fresh read-only acceptance 2026-09-04）。
- [x] Phase A.2 驗收後 remediation（A4/A5 code review findings）：P2 liquidate 預設 decision_id 改為 per-call（重複平倉不再被靜默 dedup；重複/併發安全仍由 `_verified_reducing_exit` 的 broker 驗證把關）、P3 統一 broker status mapper 至 authority（刪除 service 重複實作，未知狀態維持 fail-safe ACCEPTED）、P3 `_submit_one` 外層 except 的 `broker_calls` 提前初始化並如實回報 POST 計數。補 4 項 regression tests；完整離線 suite `347 passed, 158 subtests passed`。修復內容已納入 2026-09-04 fresh acceptance 範圍。
- [x] Phase A.2 fresh read-only 驗收（2026-09-04）：驗收矩陣全數 Pass、對抗 PoC 12/12、focused A1/A2 `49 passed`、完整離線 suite `347 passed, 158 subtests passed`、`compileall` 與 `git diff --check` 全綠；source review 無 mutation bypass 或 stale authority path — **Accepted**。
- [x] Phase A.2 A6 真實 Alpaca Paper E2E（2026-09-04， disposable paper account、經明確授權）：paper endpoint 驗證（`BaseURL.TRADING_PAPER`）、durable outbox commit→submit 時序驗證、真實 submit `F` notional $10 → broker fill 0.687164671 @ 14.538 → 本地 adopt 同一 broker_order_id → reconcile `CLEAN` → verified close（sell 0.687164671 @ 14.562）→ 帳戶 flat、零 open orders、最終狀態 `CLEAN`。無 LLM/provider call、無 secret 洩漏。

### 尚未完成

- [ ] 長期 Paper observation（小 notional、人工 watchlist）尚未開始。
- [ ] Phase B、Phase C；目前明確延後。

## 13. 下一個具體行動

Phase A（A0–A6）已全部通過 fresh acceptance 並完成真實 Alpaca Paper E2E。下一步：把 A2 revision 提交至 `main`，然後開始長期 Paper observation——先以小 notional、人工 watchlist 觀察 startup recovery、`PAUSED` 行為與 reconciliation 紀律；Phase B 依第 11 節條件（A6 通過＋一段穩定 observation）另行啟動。
