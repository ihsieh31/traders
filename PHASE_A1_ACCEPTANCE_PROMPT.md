# Phase A.1 驗收提示詞：Paper 邊界與 Durable Order Foundation

把本文件完整貼到一個**新的、獨立的 Codex task**。這是 fresh read-only acceptance，不是修復 task。

## 角色與唯一任務

你是 `traders` Phase A.1 的獨立驗收者。工作目錄固定為：

```text
/Users/zongen/Downloads/codex/tradingAlpaca
```

只驗收 `PHASE_A1_IMPLEMENTATION_PROMPT.md` 的範圍：Paper-only、strict `TradeIntent`、唯一 execution entry、三表 SQLite durable outbox、兩層 idempotency、order state machine 與最小 `UNKNOWN` lookup/adopt。

你只能給出：

- `Accepted`
- `Not Accepted — <具體原因>`

不得修 code、測試或文件，不得開始 Phase A.2。

## 必須使用 Ponytail

先讀並使用 `ponytail` skill，強度 `full`，但只作 read-only 判斷：

- 確認實作是否沿用現有型別/helpers/guardrails 與 stdlib。
- 找出不必要的 ORM、queue、factory、repository interface、重複 execution paths 或 speculative A2/B/C scaffolding。
- 不因為可以更漂亮而提出重構；只有會增加維護面、繞過 boundary 或不在需求內的複雜度才是 blocker/finding。
- 安全 trust boundary、DB transaction、unique constraint、state validation 與 fail-closed handling 不是可刪的「過度工程化」。

## Read-only 規則

- 不使用 `apply_patch`，不修改任何 tracked/untracked file。
- 不 commit、不 push、不切 branch、不 reset、不清理使用者變更。
- 不呼叫真實 Alpaca、LLM、provider、webhook、Telegram 或其他外部服務。
- 可讀 source/diff/history，可跑現有 tests，可在 OS temporary directory 執行一次性 PoC；不得把 PoC 寫進 repo。
- 發現問題只報告，不修復。若需要修復，結論必須 `Not Accepted`，另開實作 task 後再做 fresh acceptance。

## 前置條件

1. 讀完：

   - `PROJECT_GOALS_AND_STATUS.md`
   - `PHASE_A1_IMPLEMENTATION_PROMPT.md`
   - `ARCHITECTURE.md`
   - implementation report/commit diff
   - 所有被改動的 source、tests、README/env/UI 文件

2. 記錄 `git status -sb`、HEAD SHA、base SHA、完整 diff 與 remotes。
3. Phase A.1 必須由另一個 task 實作完成。若此 task 同時做過實作或 worktree 有來源不明且重疊的 dirty changes，結論 `Not Accepted — acceptance is not independent/clean`。
4. 不以實作者自述、文件 checkbox 或歷史 test totals 代替本輪證據。

## 問題與應有解法

驗收的核心不是「有新模組」，而是以下事故已被封死：

1. 設定 false 或傳錯 endpoint 不能接到 Alpaca live。
2. Risk Manager structured failure 不能由 free text 猜成可交易 action。
3. WebUI/CLI/scheduler/liquidation/protective order 不能繞過唯一 execution entry。
4. broker call 前必須先 durable commit intent/order。
5. 同一分析與同一 logical order 重複執行不能產生重複委託。
6. partial fill、terminal state 與 ambiguous submit 必須有 durable lifecycle。
7. POST timeout 不能直接 retry；先 `UNKNOWN`，再依同一 `client_order_id` lookup/adopt。

## 驗收矩陣

逐項產出 Pass/Fail 與直接證據：

| 項目 | 必須證明 |
|---|---|
| Paper-only factory | `TradingClient(..., paper=True)` 為唯一 production trading client；不存在 live execution switch |
| Endpoint fail closed | false/unknown/live 設定導致 startup/execution refusal，零 broker call |
| 文件/UI 一致 | README、env sample、CLI、WebUI 不再提供或宣稱 live trading |
| Strict Risk boundary | bind/invoke/validation/timeout/429/provider/empty/illegal action 均 `NO_TRADE` |
| No legacy execution | 缺少 valid `TradeIntent` 時沒有 signal/regex/Markdown fallback 下單 |
| Single execution entry | 所有 production order/close paths 都通過同一 service |
| Minimal data model | 只有必要 SQLite store；三表與 constraints 能支援本 Gate |
| Durable outbox | intent + `PENDING` order commit 後才允許 POST；commit failure 零 POST |
| Decision idempotency | 同一 `decision_id` 只建立一個 intent |
| Order idempotency | 同一 logical order 跨 retry/restart 使用同一 `client_order_id` |
| Constraints | `decision_id`、`client_order_id`、broker/fill IDs 有 DB-level uniqueness |
| State machine | 合法轉移可行；非法轉移拒絕；terminal state 不回到 nonterminal |
| Partial fill | 更新同一 order，不自動補單；duplicate fill 不重複入帳 |
| UNKNOWN | ambiguous POST 先 UNKNOWN；lookup 找到即 adopt；lookup 前無第二次 POST |
| Crash points | commit 前、commit 後/submit 前、submit 後/local ACK 前都有可重現安全結果 |
| Regression | focused tests 與完整離線 suite 通過 |
| Ponytail | 無新 ORM/queue/framework、重複 abstraction 或 A2/B/C speculative code |

## 必做 source review

用 `rg` 和 call-graph 閱讀確認，而不是只看測試名稱：

- 所有 `TradingClient` constructors。
- 所有 `submit_order`、`close_position` 及 execution wrapper callers。
- 所有 `ALPACA_USE_PAPER`、paper/live UI/config/README 文字。
- Risk Manager structured fallback 與 `TradeIntent` 建立路徑。
- WebUI/CLI/scheduler 的 typed-intent 缺失分支。
- SQLite transaction boundary、schema/constraints、ID canonicalization。
- state transition function、partial fill、UNKNOWN lookup/adopt。

若 production code 仍有可達的直接 broker mutation bypass，即使 tests green 也必須 `Not Accepted`。

## 必做對抗 PoC

使用 mocked client 與 temporary SQLite，至少直接執行：

1. `ALPACA_USE_PAPER=False` / live URL bypass attempt。
2. Risk Manager structured exception + free text 包含 `BUY NVDA`。
3. 同一 `decision_id` 連續及並行兩次。
4. DB commit exception。
5. commit 後/submit 前 simulated crash + restart。
6. submit 回 broker order 後/local write 前 simulated crash。
7. POST timeout 後確認沒有直接第二次 POST。
8. partial fill 重播與 duplicate execution ID。
9. terminal → nonterminal illegal transition。

PoC 若現有永久測試已直接覆蓋，可以重跑並引用；不能為了驗收新增或修改 test file。

## 測試

1. 跑 Phase A.1 focused tests，逐條記錄命令與結果。
2. 跑完整：

   ```bash
   python -m pytest tests/ -q
   ```

3. 執行 `git diff --check` 及必要的 import/compile check。
4. 報告 passed/failed/skipped/subtests/warnings；skip 或 missing dependency 不能被描述成通過。

本 Gate 不需要真實 Alpaca Paper call；若測試嘗試連線，停止並判定測試隔離失敗。

## Accepted 標準

只有以下全部成立才能 `Accepted`：

- 驗收 session fresh、read-only、revision 明確。
- 驗收矩陣全部 Pass。
- 沒有可達 live trading path、free-text execution 或 broker mutation bypass。
- DB constraints、crash PoC、UNKNOWN/partial lifecycle 直接通過。
- focused 與完整離線 suite 無失敗/意外 skip。
- 沒有不必要新依賴或超出 Phase A.1 的架構。

任一直接證據缺失、命令未完成、測試失敗、範圍污染或安全 bypass 都是 `Not Accepted`。不要以「大致完成」或 remediation 建議代替 verdict。

## 輸出格式

1. Verdict 與 exact HEAD SHA。
2. Findings：依嚴重度列出；每項包含 `file:line`、問題、可重現方式、影響。沒有 finding 時明確寫無。
3. 驗收矩陣：每項 Pass/Fail + source/test/PoC 證據。
4. 測試與外部 call accounting。
5. Ponytail 範圍檢查：保留/可刪項目；只列本 Gate。
6. `git status -sb`。
7. 下一步：Accepted 才可進 Phase A.2；Not Accepted 則另開 remediation task。

完成 verdict 後停止，不修改 repo，不開始 Phase A.2。
