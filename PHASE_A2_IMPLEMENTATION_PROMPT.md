# Phase A.2 實作提示詞：Broker Authority、Recovery 與 Unattended Paper Run

把本文件完整貼到一個新的 Codex task。這是一個**實作 Gate**，不是驗收 Gate。

## 角色與目標

你是 `traders` 專案的 Phase A.2 實作者。工作目錄固定為：

```text
/Users/zongen/Downloads/codex/tradingAlpaca
```

目標是完成 Phase A 剩餘約 55%：在已驗收的 durable order foundation 上，建立 BrokerSnapshot、固定 timeout/retry、startup/post-order/periodic reconciliation、startup recovery、freshness gate、account-scoped single execution lock，以及可驗收的 Alpaca Paper E2E。

完成後停止，不開始 Phase B/C，不自行宣告 Phase A.2 或整個 Phase A `Accepted`。

## 必須使用 Ponytail

先讀並使用 `ponytail` skill，強度 `full`：

- 沿用 Phase A.1 的 execution service、SQLite store、state machine 與 ID，不重建第二套。
- 優先 Python stdlib 與既有 Alpaca client；不新增 scheduler、retry、lock、event-bus framework。
- 一個 BrokerSnapshot、一個 reconciler、一個 account lock 足夠。
- 不預做多機、多帳戶 orchestration、WebSocket event platform、PostgreSQL 或 distributed lease。
- 不能簡化掉 broker authority、freshness、reconciliation、UNKNOWN recovery 或風險降低型 exit 的安全條件。

## 強制前置條件

1. 讀完：

   - `PROJECT_GOALS_AND_STATUS.md`
   - `PHASE_A1_IMPLEMENTATION_PROMPT.md`
   - `PHASE_A1_ACCEPTANCE_PROMPT.md`
   - Phase A.1 最新 acceptance report/evidence
   - Phase A.1 實作的 execution service/store/state machine/tests
   - `ARCHITECTURE.md`、WebUI/CLI/scheduler 與 safety/risk modules

2. 記錄 `git status -sb`、HEAD SHA、Phase A.1 Accepted SHA、remotes。
3. 沒有 fresh Phase A.1 `Accepted`，或目前 HEAD 不是該 accepted revision/其可追溯後繼，立即停止並報告 prerequisite 不成立。
4. 若有與本 Gate 重疊的 dirty changes，停止並列出；不 reset、不覆蓋。
5. 跑 Phase A.1 focused tests 與完整離線 suite；baseline 不綠就停止，不把 A1 defect 混進 A2。

## 現有問題

### 1. Local state 不是 broker authority

Agent memory、checkpoint、run log 與 SQLite ledger 都可能過期。下次決策若沒有同一份新鮮 account/position/order/fill/cash snapshot，sizing、safety 與 execution 可能基於互相矛盾的狀態。

### 2. Order-level UNKNOWN 不等於 account 已恢復一致

即使可以依 `client_order_id` 查單，仍需把 broker orders/fills/positions/cash 與 local ledger 對帳。position mismatch、duplicate ID、unresolved partial fill 或 cash/equity unavailable 時不能新增風險。

### 3. Startup 與 scheduler 缺少 recovery gate

process restart、排程重入或上一次 run 中止後，若直接分析並送單，可能忽略 `PENDING/SUBMITTING/UNKNOWN/PARTIAL` orders。

### 4. Retry policy、freshness 與 execution lock 尚未完整落地

GET 可有限 retry，但 POST timeout 絕不能盲重送。過期 quote/account snapshot 不可使用。同一 account 的第二個 process 不可同時 dispatch。

## 必做解法

### A2-1 Authoritative BrokerSnapshot

建立一個小型 immutable/typed `BrokerSnapshot`，至少包含：

- UTC `observed_at`。
- Alpaca account ID、equity、cash、buying power。
- positions。
- open/recent orders。
- today fills 或可證明等價的 broker execution facts。
- gross exposure。

規則：

- 同一次 execution 的 sizing、safety、reconciliation 與 submit preflight 使用同一 snapshot/version。
- Agent memory、checkpoint、run log、local order row 不可覆寫 broker facts。
- account ID 缺失/不符、snapshot 欄位不完整或取值無法解析時 fail closed。
- 不建立通用 event sourcing 或 snapshot history platform；保留本次 audit 所需 timestamp/version 即可。

### A2-2 固定 broker retry policy

集中實作，所有 broker callers 共用：

- Idempotent GET failure：最多 3 次，短暫 exponential backoff，之後 fail closed。
- POST validation/rejection：不 retry，記錄 terminal state。
- POST submit timeout/connection loss：不直接 retry，order 轉 `UNKNOWN`。
- 用相同 `client_order_id` 做 bounded lookup；找到即 adopt。
- broker 明確回覆不存在後，才可用相同 `client_order_id` resubmit；仍不確定則維持 `UNKNOWN/PAUSED`。
- 不對非 idempotent close/liquidate 或 protective child mutation 使用 generic retry decorator。

若 Alpaca eventual consistency 讓一次 not-found 不足以證明不存在，使用少量 bounded lookup window；不要建立 background retry framework。

### A2-3 Reconciliation 與 fail-closed state

建立 `CLEAN` / `PAUSED` 兩個 account execution states；只有 `CLEAN` 能新增風險。

至少下列任一條件為 `PAUSED`：

- broker/local position mismatch。
- unknown broker/local order。
- duplicate `client_order_id` 或 broker order identity 衝突。
- cash、equity、account ID unavailable/invalid。
- unresolved partial fill。
- `PENDING/SUBMITTING/UNKNOWN` 超過允許 recovery window。
- snapshot 不完整或過期。
- broker timeout 尚未被 authoritative lookup 解決。

Reconciler 必須：

- 以 broker 為權威更新 local order/fill facts。
- 對同一 fill 重播保持 idempotent。
- 不偷偷刪除 local row 來「變乾淨」。
- 把 mismatch 原因寫入可讀 audit/status，供 WebUI/報告顯示。

### A2-4 Startup、post-order 與 periodic recovery

- Startup 在 scheduler/auto-trade 啟動前取得 lock、讀取 durable pending orders、查 broker、reconcile；未 `CLEAN` 不開始新增風險。
- 每次 submit/close/cancel/protective action 後立即更新該 order/fills/position/account 並 reconcile。
- scheduler 每輪 execution 前重新取得新鮮 snapshot 並 reconcile。
- 恢復 `PENDING/SUBMITTING/UNKNOWN/PARTIAL` 時沿用既有 IDs；不建立新 intent/order。
- 使用現有 scheduler loop 的最小 hook；不另建排程系統或 daemon。

### A2-5 Freshness gate

Phase A 只 gate execution 必要資料：account、positions、orders、fills、quote。

- 使用少量、集中且有安全預設的 TTL；允許既有 config override，但不做 policy DSL。
- missing timestamp、future timestamp、超過 TTL、不同 account/symbol 的 snapshot 都 `NO_TRADE/PAUSED`。
- 時間比較一律 timezone-aware UTC。
- News、SEC/IR freshness 不在本 Gate。

### A2-6 Account-scoped single execution lock

- 使用 SQLite transaction 或 OS stdlib file lock；key 是 verified Alpaca account ID。
- 同一 account 只有一個 process 可執行 recovery/reconcile/dispatch critical section。
- 第二個 process 取不到 lock 時立即回傳明確 busy/paused，不送單。
- crash 後 OS/transaction 機制可釋放；不要自造需要 stale-heartbeat cleanup 的 lease table，除非平台限制直接證明必要。
- lock 只保護 execution，不鎖住長時間 LLM analysis。

### A2-7 風險降低型 exit policy

當 account 是 `PAUSED` 時，新增曝險永遠禁止。只有同時滿足以下條件才能做 risk-reducing exit：

- broker 即時確認該 symbol 的真實 position 與方向。
- exit 數量不超過 verified position，且確實降低 absolute exposure。
- 沒有矛盾的 open close order 或 unresolved identity。
- Paper-only、strict intent、single lock 與現有 safety/kill-switch policy 仍通過明確規則。
- action 後立即 reconciliation；失敗保持 `PAUSED`。

不能從 stale local state、LLM memory 或 free text 決定 exit 數量。

### A2-8 真實 Alpaca Paper E2E

本 prompt **不是**自行使用 credentials 或呼叫 broker 的授權。

- 先完成全 mock/temporary DB 的 deterministic tests。
- 只有本次 task 收到使用者明確授權、確認是 disposable Alpaca Paper account 且 secrets 不會輸出/寫入 repo，才可執行真實 E2E。
- 沒有授權時停止在「Implemented，Paper E2E pending」，不得假造或用 mock 代替。
- 真實 E2E 只做最小 notional/quantity，並優先在可安全取消/平倉的標的與市場條件下執行。
- 不測 live endpoint，不使用真實資金。

## 必要測試與驗收標準

新增最少量 tests，至少證明：

1. BrokerSnapshot 欄位完整、UTC、account-bound，且 execution 內共用同一 version。
2. GET transient failure 最多 3 次後停止；成功重試不造成 mutation。
3. POST timeout 不直接 retry；lookup/adopt/resubmit 使用同一 `client_order_id`。
4. startup 對 `PENDING/SUBMITTING/UNKNOWN/PARTIAL` 可恢復，無 duplicate order/fill。
5. startup 未 `CLEAN` 時 scheduler/auto-trade 不啟動新增曝險。
6. position mismatch、unknown order、duplicate ID、cash/equity unavailable、unresolved partial、stale snapshot 都 `PAUSED`。
7. post-order 與 scheduler-round reconciliation 被實際 caller 觸發，不只是存在未使用的 helper。
8. stale、future、missing、wrong-account/wrong-symbol timestamp/facts 都 fail closed。
9. 兩個 process/connection 競爭同一 account lock，只有一個可進入 mutation critical section。
10. lock owner crash/exit 後可恢復，不需人工改 DB。
11. risk-reducing exit 僅在 verified conditions 執行；超量、錯方向、矛盾 open order、stale position 都零 broker mutation。
12. Phase A.1 tests 與完整 `python -m pytest tests/ -q` 保持通過。

Chaos tests 至少覆蓋：GET outage、POST timeout、malformed broker numeric/time fields、partial-fill replay、restart、兩 executor race。

## 文件與狀態

- 更新 `ARCHITECTURE.md` 與 operator-facing README，只描述實際完成的 startup/recovery/paused 行為。
- 更新 `PROJECT_GOALS_AND_STATUS.md`：標記 A4/A5 `Implemented，pending fresh acceptance`；A6 依真實證據標記 pending 或 implemented，不得寫 Accepted。
- 明確說明如何查看 `PAUSED` 原因與如何安全恢復；不要新增獨立 runbook，若 README/現有 UI 足夠就直接補在原處。

## 明確排除

不要開始：

- SEC/IR、corporate-action framework、sector constraints。
- Kelly/memory/correlation/regime 強化。
- Universe/screener/ranking。
- PostgreSQL、ORM、message queue、WebSocket event platform、distributed lock。
- Live trading 或任何可切換到 live 的 escape hatch。

## 完成報告格式

1. 結果：Implemented / Blocked / Implemented, Paper E2E pending；不得寫 Accepted。
2. 問題與實際解法：逐項對應 A2-1～A2-8。
3. 變更檔案與 runtime call flow。
4. 驗收矩陣自測、命令、passed/failed/skipped/warnings。
5. 外部 call accounting：Alpaca/model/provider 各幾次；若有 Paper call，列目的與非秘密結果。
6. 未完成項目與 fresh acceptance 前提。
7. `git status -sb`、HEAD SHA、Phase A.1 Accepted SHA。

完成 Phase A.2 後停止，等待新的 read-only acceptance task。不要開始 Phase B/C。
