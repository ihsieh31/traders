# Phase A.2 驗收提示詞：Broker Authority、Recovery 與 Unattended Paper Run

把本文件完整貼到一個**新的、獨立的 Codex task**。這是 Phase A 最終 fresh read-only acceptance，不是修復 task。

## 角色與唯一任務

你是 `traders` Phase A.2 的獨立驗收者。工作目錄固定為：

```text
/Users/zongen/Downloads/codex/tradingAlpaca
```

只驗收：BrokerSnapshot、固定 broker retry、startup/post-order/periodic reconciliation、startup recovery、freshness、account-scoped single execution lock、risk-reducing exit，以及真實 Alpaca Paper E2E。

你只能給出：

- `Accepted`
- `Not Accepted — code/test defect`
- `Not Accepted — Alpaca Paper evidence pending`
- `Not Accepted — prerequisite/independence failure`

不得修 code、tests 或文件，不得開始 Phase B/C。

## 必須使用 Ponytail

先讀並使用 `ponytail` skill，強度 `full`，但只作 read-only 驗收：

- 確認 A2 沿用 A1 execution service/store/state machine，而不是建立平行架構。
- 確認只使用必要 BrokerSnapshot、reconciler、retry helper 與 account lock。
- ORM、queue、event bus、second scheduler、distributed lease、generic policy framework 或未使用 abstraction 都是超範圍 finding。
- 不要求為美觀重構；broker authority、reconciliation、freshness、lock 與 fail-closed safety 不能被當成可刪複雜度。

## Read-only 與外部 call 規則

- 不使用 `apply_patch`，不修改任何 repo file。
- 不 commit、不 push、不 reset、不切 branch、不清理使用者變更。
- 可跑既有 tests，可用 OS temporary directory/DB 執行一次性 PoC；PoC 不寫入 repo。
- 不傳送 LLM/model/provider/webhook/Telegram call。
- 真實 Alpaca Paper call 只有在**本次驗收 task**獲得使用者明確授權、account 已確認為 disposable Paper、secrets 不會顯示或寫入 repo 時才可執行。
- 過去的 live/Paper 結果、implementation self-test 或 mock 不能自動代替本輪 A6 evidence。
- 未獲授權時不得呼叫 broker，最終不能給 `Accepted`；使用 `Not Accepted — Alpaca Paper evidence pending`。

## 強制前置條件

1. 讀完：

   - `PROJECT_GOALS_AND_STATUS.md`
   - 四份 `PHASE_A*_PROMPT.md`
   - Phase A.1 acceptance report 與 exact Accepted SHA
   - Phase A.2 implementation report/diff
   - 所有 execution/store/retry/reconciliation/scheduler/safety source 與 tests
   - `ARCHITECTURE.md`、README、env/config/UI operator status

2. 記錄 `git status -sb`、HEAD SHA、A1 Accepted SHA、A2 base SHA、完整 diff、remotes。
3. Phase A.1 沒有 fresh `Accepted`、A2 不是建立在該 revision 上、驗收 task 曾參與 A2 實作，或 worktree 有來源不明重疊 dirty changes：立即 `Not Accepted — prerequisite/independence failure`。
4. 不以文件 checkbox、實作者敘述、歷史 test totals 代替本輪 source/test/PoC/broker evidence。

## 問題與應有解法

Phase A.2 必須封死以下事故：

1. local ledger/memory 與 broker position/order/cash 不一致仍新增曝險。
2. startup 忽略 `PENDING/SUBMITTING/UNKNOWN/PARTIAL` order。
3. POST timeout 直接 retry 造成 duplicate order。
4. GET outage、malformed fields 或 stale snapshot 被當成可交易資料。
5. scheduler 第二個 process 同時 dispatch 同一 account。
6. `PAUSED` 狀態用 stale local position 執行錯誤 exit。
7. helper 雖存在，但 WebUI/scheduler/order callers 沒有實際使用。

## 驗收矩陣

逐項產出 Pass/Fail 與直接證據：

| 項目 | 必須證明 |
|---|---|
| A1 prerequisite | Phase A.1 exact revision 已 fresh Accepted，且 A1 regressions 保持綠 |
| BrokerSnapshot | UTC、account-bound，含 equity/cash/buying power/positions/orders/fills/gross exposure |
| Single snapshot | 同一次 sizing/safety/reconcile/pre-submit 使用同一 snapshot/version |
| Broker authority | broker facts 更新 local ledger；memory/checkpoint/local stale state 不可覆寫 |
| GET retry | 最多 3 次 bounded retry；之後 fail closed；無 mutation retry |
| POST timeout | 先 UNKNOWN；lookup 前零第二次 POST；所有 resubmit 沿用 client ID |
| Startup recovery | durable pending/nonterminal orders 先恢復/對帳，再允許 scheduler 新增風險 |
| Post-order reconcile | submit/close/cancel/protective action 後實際同步 order/fill/position/account |
| Periodic reconcile | scheduler 每輪 execution 前實際執行，不是 dead helper |
| CLEAN gate | 只有 `CLEAN` 可以新增曝險 |
| PAUSED matrix | mismatch/unknown/duplicate/unavailable/unresolved partial/timeout/stale 全部 PAUSED |
| Fill idempotency | broker replay 不重複入帳、不扭曲 filled quantity |
| Freshness | missing/future/stale/wrong-account/wrong-symbol facts 全部 fail closed |
| Single lock | 同 account 兩 process 只有一個進入 mutation critical section |
| Lock recovery | owner crash/exit 後可恢復，不需手改 DB |
| Lock scope | 不把長時間 LLM analysis 包在 execution lock 裡 |
| Risk-reducing exit | verified position/side/qty/open-order conditions 全通過才允許，且立即 reconcile |
| Operator visibility | 可看到 PAUSED 原因與 recovery 結果；文件與 runtime 一致 |
| Full regression | A1/A2 focused tests、chaos tests、完整離線 suite 全通過 |
| Paper-only | A2 沒有新增 live escape hatch |
| Ponytail | 無第二套 execution/DB/scheduler、無新 ORM/queue/distributed framework |
| Alpaca Paper E2E | 本輪或可驗證的 exact-revision獨立證據覆蓋安全 submit/recovery/reconcile |

## 必做 source/call-graph review

不要只看 tests：

- 追 WebUI、CLI、scheduler、liquidation、position flip、protective order 到唯一 execution service。
- 追 startup 與 scheduler loop，確認 recovery/reconciliation gate 在 mutation 前。
- 追 BrokerSnapshot 建立、timestamp/account binding、sizing/safety consumers。
- 追 GET/POST retry，確認 generic retry 不會套到 mutation。
- 追 `UNKNOWN` lookup/adopt/resubmit 與 `client_order_id` 保持不變。
- 追 `CLEAN/PAUSED` 決策及所有列出的 mismatch branches。
- 追 account lock acquisition/release 與 critical section。
- 追 risk-reducing exit quantity/side/open-order verification。

若存在 production broker mutation bypass、dead reconciliation helper 或 stale local-state path，即使 tests green 也必須 `Not Accepted — code/test defect`。

## 必做對抗 PoC

使用 mock broker、temporary SQLite，至少直接執行：

1. Account/position/order/fill 任一 GET 連續 outage。
2. GET 前兩次失敗第三次成功，以及第三次後停止。
3. POST timeout + lookup found、lookup definitively absent、lookup still ambiguous 三條路徑。
4. malformed/NaN/negative broker numeric fields。
5. missing/future/stale/wrong-account/wrong-symbol snapshot。
6. startup 帶 `PENDING/SUBMITTING/UNKNOWN/PARTIAL` rows。
7. broker/local position mismatch、duplicate ID、unknown broker order、unresolved partial。
8. duplicate fill replay。
9. scheduler round 在 `PAUSED` 狀態。
10. 兩個 process/SQLite connections 同 account 競爭 lock。
11. lock owner abnormal exit 後重新取得。
12. risk-reducing exit 的正向案例，以及超量/錯方向/stale/矛盾 order 反向案例。

已有永久 tests 直接覆蓋時可以重跑引用；不能修改或新增 test 來修 acceptance。

## 必跑測試

1. Phase A.1 regressions。
2. Phase A.2 focused tests。
3. 既有 safety、chaos、structured decision、bracket order、position sizing tests。
4. 完整：

   ```bash
   python -m pytest tests/ -q
   ```

5. `python -m compileall` 或現有 import check。
6. `git diff --check`。

報告 exact command、passed/failed/skipped/subtests/warnings、duration 與外部 call accounting。任何未解釋 skip、missing dependency、timeout 或中止都不是通過。

## 真實 Alpaca Paper E2E

只有當本次獲得明確授權時執行。最低證據：

1. 證明 account/base URL 是 Paper，且 production client 沒有 live 切換。
2. 使用最小安全 order 建立 deterministic `client_order_id`，local durable row 先存在。
3. broker ACK/order facts 被 adopt 到同一 local order。
4. 查詢 order/fill/position/account 後 reconciliation=`CLEAN`；若市場條件只允許 cancel/無 fill，誠實標記未覆蓋項目。
5. 執行安全 cancel/close 或確認無剩餘意外 exposure/open orders。
6. secrets 不出現在輸出、git diff、logs 或 DB payload。

部分成交與真正 network timeout 很難安全製造時，可由 deterministic broker mock/chaos test證明 branch correctness，但仍需要至少一條真實 Paper submit→broker state→reconcile 路徑。不要聲稱模擬了實際發生的 broker timeout。

## Accepted 標準

只有以下全部成立才能 `Accepted`：

- 前置 Gate、independence、revision 與 worktree 邊界成立。
- 驗收矩陣全部 Pass。
- source review 無 mutation bypass 或 stale authority path。
- adversarial PoC、focused/chaos/full suite 全通過，無未解釋 skip。
- 真實 Alpaca Paper E2E 在 exact revision 上完成，且離場/cleanup 狀態清楚。
- 無 live call、model/provider call 或 secret disclosure。
- 無超出 A2 的大型依賴/架構。

若 code/tests 全通過但缺本輪可接受的真實 Paper evidence，結論必須 `Not Accepted — Alpaca Paper evidence pending`。若驗收中修了任何檔案，該 session 失去獨立性，不能給 Accepted；修復後要再開 fresh acceptance。

## 輸出格式

1. Verdict、exact HEAD SHA、A1 Accepted SHA。
2. Findings：依嚴重度列 `file:line`、問題、PoC、影響；沒有則明確寫無。
3. 驗收矩陣：逐項 Pass/Fail + source/test/PoC/Paper evidence。
4. 測試結果與外部 call accounting。
5. Alpaca Paper E2E：authorization、Paper 身分、order/reconcile/cleanup 非秘密證據。
6. Ponytail 範圍檢查。
7. `git status -sb`。
8. 下一步：Accepted 才可開始長期 Paper observation；否則另開 remediation 或補 evidence task。

完成 verdict 後停止，不修改 repo，不開始 Phase B/C。
