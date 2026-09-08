# Traders 最小修復實作規劃（2026-09-08 最終複審後）

## 0. 目的與範圍

本輪只修復 2026-09-08 最終複審仍確認存在的 **3 個問題**：

1. **F15：LangChain usage callback 對真實 `LLMResult.generations` 結構解析錯誤，可能漏計 token。**
2. **F19：Long-run resume 在只剩 `ANALYZED / EXECUTING` 工作時仍可能重新執行 Screening，形成 token budget bypass。**
3. **NEW-R1：F04 修復引入 protected SHORT 關倉的 qty 符號比較錯誤，正常 SHORT close 可能被誤判為 position changed。**

本輪目標是：

- 封閉上述 3 個具體缺口。
- 保持現有架構。
- 不重做 Phase 1 / Phase 2。
- 不改已通過的交易安全語義。
- 不引入新的 persistence、state machine、provider abstraction、screening framework。
- 不「順便清理」無關程式碼。
- 用最小 production diff + 精準 regression tests 完成。

---

# 1. 修復原則

## 1.1 最小修改

優先修改既有函式，不新增大型 abstraction。

允許新增：

- 1 個極小的 internal helper，用於正規化 LangChain generation 結構。
- 針對既有 long-run journal resume 流程的窄條件分支。
- SHORT close regression fixture / test。
- 必要的 focused regression tests。

不允許：

- 重寫 `RunAuditLogger`。
- 重寫 `SafetyGuard`。
- 重寫整個 `run_daily_round()`。
- 新增另一套 screening cache / journal。
- 新增新的 execution state machine。
- 改變 broker authority / reconciliation 的整體模型。
- 為了測試方便而 bypass production gate。
- 修改既有安全限制來「讓測試過」。

---

# 2. 問題一：F15 — 真實 LangChain callback 可能漏計 token

## 2.1 問題描述

目前 `tradingagents/llm_clients/usage.py` 的 usage extraction 大致假設：

```python
for generation in response.generations:
    message = getattr(generation, "message", None)
```

但是 LangChain 真正進入 `on_llm_end()` 的常見資料結構是：

```python
LLMResult(
    generations=[
        [ChatGeneration(...)]
    ]
)
```

也就是：

```text
LLMResult
└── generations
    └── list
        └── ChatGeneration
            └── message
                └── usage_metadata
```

因此目前程式第一層拿到的是 `list`，不是 `ChatGeneration`。

結果：

```python
getattr(generation, "message", None)
```

會得到 `None`。

### 可能後果

對部分 provider：

- provider 明明回報 token usage；
- `UsageAccountingCallback` 卻解析不到；
- `RunAuditLogger.log_event("llm_call")` 沒有得到正確 token；
- `SafetyGuard.record_llm_tokens()` 不會增加；
- daily token budget 可能失真；
- final cost attribution 可能漏計。

尤其 Google / Gemini 路徑不能假設一定能從 `llm_output` fallback 取得 token，因此這不是純測試結構問題，而是真實 production accounting 缺口。

---

## 2.2 修復方法

### A. 新增一個非常小的 generation iterator

建議放在：

```text
tradingagents/llm_clients/usage.py
```

例如：

```python
def _iter_generations(response):
    for item in getattr(response, "generations", None) or []:
        if isinstance(item, (list, tuple)):
            for generation in item:
                yield generation
        else:
            yield item
```

目的只有一個：

> 同時支援平面 `ChatResult.generations=[ChatGeneration]` 與真實 `LLMResult.generations=[[ChatGeneration]]`。

不要做 recursive arbitrary traversal，不要建立通用 schema normalization framework。

---

### B. `extract_langchain_usage()` 統一使用 `_iter_generations()`

原本：

```python
for generation in response.generations:
```

改成：

```python
for generation in _iter_generations(response):
```

保持原本 usage extraction 優先序：

1. `message.usage_metadata`
2. `message.response_metadata`
3. `response.llm_output`

不要改變既有 provider normalization 規則，除非測試證明必要。

---

### C. `UsageAccountingCallback.on_llm_end()` 的 adapter duplicate marker 也必須走同一 helper

目前 GPT-5 Responses adapter 用：

```python
additional_kwargs["usage_accounted_by_adapter"]
```

避免 callback 再計一次。

這個檢查也不能繼續假設 flat generations。

改成：

```python
for generation in _iter_generations(response):
    message = getattr(generation, "message", None)
    ...
```

確保真實 LangChain callback 結構下仍能避免 duplicate accounting。

---

### D. Model attribution 必須保持可用

若 usage 有 token，但 `model_name` 無法從 `message.response_metadata` 或 `llm_output` 得到，不應因此丟掉 token。

優先目標：

- **token budget 一定要記帳**
- model attribution best-effort

若目前 callback 可從 start event / serialized metadata 安全保存 model，可以做極小補強；但不得為此重構 callback lifecycle。

若無法可靠取得 provider model：

```text
usage 有值、model 不明
```

可以：

- token 照計
- model attribution 留空 / unknown
- final cost 將該 token 視為 unpriced / unattributed

**禁止為了補 model 名稱而估猜 provider/model。**

---

## 2.3 修復邊界

本項不得修改：

- `SafetyGuard.record_llm_tokens()` 的語義。
- daily budget threshold。
- GPT-5 adapter 自己的 direct usage accounting ownership。
- `RunAuditLogger.log_event()` 的 budget-before-attribution 原則。
- provider retry 次數。
- LLM request timeout。
- pricing table。
- screening behavior。

本輪只修：

```text
provider-reported usage
→ callback extraction
→ exactly-once audit/budget accounting
```

---

## 2.4 必加驗收測試

至少新增以下 regression tests。

### F15-A：真實 `LLMResult` nested generations

使用：

```python
LLMResult(
    generations=[[
        ChatGeneration(
            message=AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 30,
                    "output_tokens": 50,
                    "total_tokens": 80,
                },
            )
        )
    ]],
    llm_output={},
)
```

驗證：

```text
SafetyGuard token delta == 80
```

---

### F15-B：exactly once

在 active audit run 下觸發一次 nested `LLMResult` callback。

驗證：

- `SafetyGuard` 只增加一次。
- run log 只有一筆對應 `llm_call` usage event。
- summary `total_llm_tokens == 80`。
- 不得是 `160`。

---

### F15-C：GPT-5 adapter marker 在 nested result 下仍避免 double count

建立：

```python
AIMessage(
    ...,
    additional_kwargs={
        "usage_accounted_by_adapter": True
    },
    usage_metadata={...}
)
```

包成：

```python
LLMResult(generations=[[ChatGeneration(...)]])
```

驗證：

```text
callback 額外增加 token == 0
```

GPT-5 adapter 自己的 accounting 測試仍應維持原本 exactly-once 行為。

---

### F15-D：Google-shaped usage

模擬 Google / Gemini 真實 message usage：

```python
usage_metadata={
    "input_tokens": 10,
    "output_tokens": 15,
    "total_tokens": 25,
}
```

且：

```python
llm_output={"prompt_feedback": {}}
```

驗證：

- 仍能取到 25 tokens。
- 不依賴 `llm_output["token_usage"]`。
- budget +25。

---

# 3. 問題二：F19 — resume path 仍可能重新做 Screening

## 3.1 問題描述

`run_daily_round()` 現在已有 daily budget gate，大致邏輯：

```python
needs_new_llm_work = journal is None or any(
    status in (PENDING, ANALYZING)
)

if needs_new_llm_work:
    check_llm_budget()
```

這個方向是正確的：

- `ANALYZED`
- `EXECUTING`

代表既有決策已經產生，resume 應優先完成 deterministic execution/recovery，而不是重新問 LLM。

但是之後目前仍會無條件：

```python
plan = _screening_with_audit_scope(...)
```

### 問題情境

1. 系統已完成 Screening。
2. 已完成部分或全部 symbol analysis。
3. journal 裡剩下的工作只有：

```text
ANALYZED
EXECUTING
DONE
FAILED
```

4. process crash / restart。
5. token budget 已用完。
6. 因為沒有 `PENDING / ANALYZING`，budget gate 不阻擋 resume。
7. 但程式仍重新進入 screening pipeline。
8. 如果 selection cache 正常，可能只是 cache hit。
9. 如果 selection cache 遺失、損毀、過期或無法重用，就可能再次發送 Screening LLM request。

這違反本來的 resume 原則：

> 已持久化的 ANALYZED/EXECUTING 工作，不能因 resume 再產生新的 LLM work。

---

## 3.2 修復方法

### 核心原則

若 round journal 已存在，而且 journal 已包含可恢復的 screening / symbol state，同時剩餘工作不需要新的分析：

> **直接沿用 journal，跳過 Screening pipeline。**

---

### 建議最小條件

在呼叫 `_screening_with_audit_scope()` 前判斷：

```python
resume_without_new_llm = (
    journal is not None
    and journal.get("screening")
    and not any(
        (entry or {}).get("status")
        in (SYMBOL_PENDING, SYMBOL_ANALYZING)
        for entry in (journal.get("symbols") or {}).values()
    )
)
```

若：

```python
resume_without_new_llm is True
```

則：

- 不呼叫 `_screening_with_audit_scope()`
- 不新增 Screening audit LLM scope
- 不重新建立 symbol universe
- 使用 journal 已有：

```text
screening
symbols
trade_intent
analysis_run_ref
execution state
```

直接繼續後面的 ANALYZED / EXECUTING resume。

---

### 不要重新建造假的 plan

若目前後續程式碼需要 `plan`，優先最小調整：

- 將「把 plan 寫入 journal」那一段只放在真的做 Screening 時執行。
- journal 已存在的 execution-only resume 不重新寫 screening summary。

不要為此新增一個 `RecoveredScreeningPlan` class。

---

### journal 不完整時必須 fail closed

只有在 journal 本身已能證明 round selection/state 時才允許 skip Screening。

例如以下情況：

```text
journal 存在
但 journal["screening"] 缺失 / 明顯無效
```

不要猜測。

若這種 round 還要新做 Screening：

- 必須重新進入正常 budget gate。
- budget exhausted → `LLM_BUDGET_EXHAUSTED`
- 不得偷偷 bypass。

---

## 3.3 Execution Phase C gate 不得移除

即使 resume skip Screening，真正 execution 仍必須經過現有：

```text
ExecutionService
→ Phase C entry gate
→ current validated Top20 authority
```

如果 external/current selection authority 已經無法證明該 order 可開新 exposure：

```text
fail closed
```

這是正確行為。

不要因為 journal 有舊 screening 結果就繞過 production Phase C execution gate。

---

## 3.4 修復邊界

本項不得：

- 改變每日 Screening 的正常 fresh-run 行為。
- 允許舊 Top20 直接取代 current execution gate。
- 新增第二套 selection cache。
- 修改 Screening LLM prompt。
- 修改 screening ranking。
- 修改 Top20 規則。
- 修改 deep-analysis universe。
- 修改 Phase C entry gate。
- 修改 execution idempotency。

只修：

```text
resume 時「沒有新 LLM 工作」
→ 不應重新進入可能產生 LLM request 的 Screening pipeline
```

---

## 3.5 必加驗收測試

### F19-A：ANALYZED-only resume 不得呼叫 screening_fn

建立 persisted journal：

```text
AAA = ANALYZED
trade_intent 已存在
```

設定：

```python
screening_fn = function_that_raises_if_called
graph = function_that_raises_if_called
```

驗證：

- `screening_fn` call count = 0
- graph / LLM analysis call count = 0
- existing intent 仍可進 execution
- execution call count = 1

---

### F19-B：EXECUTING-only resume 不得呼叫 Screening

建立：

```text
AAA = EXECUTING
trade_intent 已存在
```

驗證：

- screening 0 call
- analysis 0 call
- startup recovery + deterministic execution resume 正常進行

---

### F19-C：budget exhausted + selection cache 不存在，仍不得產生 Screening request

這個測試必須刻意模擬「cache 不存在」。

不要讓測試因 fake screening 本來就不使用 LLM 而失去意義。

做法：

- journal 已完整，只有 ANALYZED/EXECUTING。
- budget 已 exhausted。
- screening_fn 設為：

```python
raise AssertionError("screening must not run")
```

驗證 round resume 能直接走 execution path，且 Screening 完全未呼叫。

---

### F19-D：journal 需要新 PENDING work 時仍必須受 budget gate

建立：

```text
AAA = PENDING
budget exhausted
```

驗證：

- `LLM_BUDGET_EXHAUSTED`
- screening 0 request
- graph 0 request
- broker 0 mutation

確保本次 skip 邏輯不會誤放行真正需要新 LLM work 的 resume。

---

# 4. 問題三：NEW-R1 — protected SHORT close 的符號比較錯誤

## 4.1 問題描述

F04 修復新增了 protection cancellation 後重新確認 position 是否變動的邏輯。

目前類似：

```python
if abs(after_cancel.qty - abs(close_position.qty)) > 1e-8:
```

對 LONG：

```text
before = +5
after  = +5

abs(5 - abs(5)) = 0
```

正常。

但對 SHORT：

```text
before = -5
after  = -5

abs(-5 - abs(-5))
= abs(-5 - 5)
= 10
```

因此：

> 倉位實際完全沒有改變，程式卻判定 position changed。

可能後果：

- prepared liquidation outbox 被 abandon。
- close order 不送出。
- protections 已經取消。
- 系統進入 protection-gap / PAUSED。
- 正常 protected SHORT 無法順利 liquidation。

這是一個 fail-closed 功能回歸，不是超額曝險漏洞，但必須修正。

---

## 4.2 修復方法

比較應分成：

1. position side 是否一致。
2. absolute quantity 是否一致。

例如：

```python
same_side = (
    (after_cancel.qty > 0) == (close_position.qty > 0)
)

same_qty = (
    abs(abs(after_cancel.qty) - abs(close_position.qty)) <= 1e-8
)

if not same_side or not same_qty:
    ...
```

或等價、可讀性相同的最小判斷。

---

## 4.3 必須保留的 safety behavior

以下情況仍必須被判定為 position changed：

### A. 數量真的變了

```text
before +5 → after +3
before -5 → after -3
```

### B. side 翻轉

```text
before +5 → after -5
before -5 → after +5
```

### C. position 消失

目前已有：

```python
if after_cancel is None:
```

應保留原本「position 在 race 中已關閉，因此 no new close needed」邏輯。

不要把這個 case 併到 qty comparison 裡。

---

## 4.4 修復邊界

不得改變：

- commit-before-cancel 原則。
- protection ownership verification。
- durable close outbox。
- `PROTECTION_GAP` persistent PAUSED invariant。
- failed close 的 fail-closed 行為。
- LONG close 已通過的流程。
- broker snapshot / reconciliation 流程。
- close order side 決策。

本輪只修：

```text
取消 protection 後
對 SHORT position 的「是否真的變動」判斷
```

---

## 4.5 必加驗收測試

### R1-A：protected SHORT unchanged → close 必須送出

建立：

```text
SHORT AAPL qty = -5
protective BUY stop 已存在
```

流程：

1. durable close prepare 成功。
2. cancel protective order。
3. fresh snapshot 仍是 `qty=-5`。
4. close BUY order 被送出。

驗證：

- 不得出現 `close position changed during protection cancellation`
- 不得因 qty sign 誤判進 `PROTECTION_GAP`
- close broker POST count = 1
- close side = BUY
- close quantity = 5

---

### R1-B：SHORT 數量真的改變仍要 fail closed

例如：

```text
before = -5
after  = -3
```

驗證：

- prepared close 不得盲目送原始 5 股。
- 應走既有 position-changed / protection-gap 安全路徑。
- 不得新增 exposure。

---

### R1-C：side flip 必須 fail closed

例如：

```text
before = -5
after  = +5
```

驗證：

- 不得視為 same qty。
- 不得送舊的 close request。
- account 進安全處理。

---

### R1-D：既有 LONG F04 tests 全部繼續 PASS

必須證明修 SHORT 沒有破壞：

- LONG failed close → persistent protection gap。
- durable commit failure → zero cancellation。
- accepted close → no false protection gap。
- restart 後 persisted gap 仍維持。

---

# 5. 建議修改檔案

預期 production diff 應集中在：

```text
tradingagents/llm_clients/usage.py
tradingagents/long_run.py
tradingagents/execution/service.py
```

測試主要集中：

```text
tests/test_full_review_phase2_regressions.py
tests/test_full_review_phase1_regressions.py
```

若需要新 test file，可以新增一個很小的 regression file；但優先放入現有 full-review regression suites。

理想狀況：

```text
production files changed: 3
test files changed: 2
```

若 production 修改明顯擴散到 6、7 個以上檔案，應先重新檢查是否過度工程化。

---

# 6. 禁止修改區域

除非新測試證明這些地方直接阻礙上述 3 個修復，否則不要動：

```text
tradingagents/safety.py
tradingagents/execution/authority.py
tradingagents/execution/store.py
tradingagents/risk/exposure.py
tradingagents/risk/corporate_actions.py
tradingagents/screening/gate.py
tradingagents/screening/pipeline.py
tradingagents/screening/llm.py
tradingagents/llm_cost.py
webui/*
cli/*
setup.py
```

尤其不要：

- 改 daily token budget 定義。
- 放寬 safety gate。
- 取消 Phase C entry gate。
- 修改 paper-only restriction。
- 修改 retry=3 ownership。
- 修改 30-day scheduler。
- 修改 Top20 selection strategy。
- 修改 provider fallback policy。
- 修改現有 broker recovery policy。

---

# 7. 實作順序

建議按以下順序：

## Step 1 — F15

先修 `usage.py`：

```text
nested generation iterator
→ extraction
→ duplicate marker
→ tests
```

因為 F19 的 token budget 正確性依賴 F15 accounting。

---

## Step 2 — F19

修：

```text
existing journal
+ no PENDING/ANALYZING
→ skip screening
→ directly resume deterministic work
```

補 execution-only resume tests。

---

## Step 3 — SHORT close regression

只改 qty/side comparison。

先加 failing SHORT regression，再改 production condition。

---

## Step 4 — focused tests

至少跑：

```bash
pytest -q tests/test_full_review_phase1_regressions.py
pytest -q tests/test_full_review_phase2_regressions.py
```

---

## Step 5 — 相關既有 suites

至少再跑：

```bash
pytest -q tests/test_phase_d_long_run.py
pytest -q tests/test_phase_a2_broker_authority.py
pytest -q tests/test_phase_c_screening.py
pytest -q tests/test_phase_c_remediation.py
```

如果檔名在目前 repo 有差異，以現有最接近的 execution / long-run / screening suite 為準。

---

## Step 6 — full suite

最後：

```bash
pytest -q
```

---

# 8. 驗收標準

我之後會直接依以下標準做獨立驗收。

---

## Gate A — F15

必須全部成立：

- 真實 nested `LLMResult.generations=[[...]]` 可解析。
- Google/Gemini-style `message.usage_metadata` 可記帳。
- provider-reported token 不因沒有 active run 而遺失 budget accounting。
- active run 下只記一次。
- GPT-5 adapter 不 double count。
- 缺少 usage 時不估算 token。
- token 有值但 model unknown 時，不得丟掉 token。
- final audit summary 與 SafetyGuard 數量一致。

任一不成立：

```text
NOT ACCEPTED
```

---

## Gate B — F19

必須全部成立：

- fresh round 仍正常 Screening。
- 有 `PENDING/ANALYZING` 的 resume 仍受 budget gate。
- 只有 `ANALYZED/EXECUTING` 的 resume：
  - Screening call = 0
  - new LLM analysis call = 0
  - deterministic execution/recovery 可繼續
- selection cache 遺失不能造成 execution-only resume 偷跑 Screening LLM。
- 不可 bypass Phase C execution entry gate。

任一不成立：

```text
NOT ACCEPTED
```

---

## Gate C — SHORT close

必須全部成立：

- protected SHORT unchanged qty 可正常 close。
- close side 正確為 BUY。
- close quantity 使用 absolute qty。
- quantity 真變動仍 fail closed。
- side flip 仍 fail closed。
- existing LONG F04 regression 全過。
- commit-before-cancel invariant 不變。
- failed close 的 protection-gap invariant 不變。

任一不成立：

```text
NOT ACCEPTED
```

---

# 9. Full-suite 驗收規則

### 最佳結果

```text
pytest -q
→ 全綠
```

直接通過此 gate。

---

### 若仍只有已知 baseline date-sensitive 失敗

目前已知曾存在 3 個 date-sensitive Phase C fixtures。

若 full suite 仍失敗：

不得只寫：

```text
pre-existing
```

必須提供：

1. 失敗 test 完整名稱。
2. 最新 HEAD 的 failure output。
3. baseline `2dccbc0e65d9bc3acb8e252ea49747a1da27ef06` 同一測試 failure output。
4. 證明 failure signature 相同。
5. 證明本輪 production diff 沒有碰造成該 failure 的邏輯。

只有確認「baseline 同樣失敗，而且 failure 原因完全一致」時，我才會把它視為非本輪 blocker。

任何新的 failure：

```text
NOT ACCEPTED
```

---

# 10. 最終交付要求

實作者完成後應：

1. Commit 所有本輪 production + regression tests。
2. Push 到目前 `main`。
3. 提供最新 commit SHA。
4. 提供：
   - focused test 結果
   - related suites 結果
   - full `pytest -q` 結果
5. 不要啟動真正的 30-day observation。
6. 不要做任何 real broker mutation。
7. 不要做 paid/external LLM call 作為測試依賴。
8. 不需要另寫 acceptance prompt。
9. 不需要自稱「independently accepted」；最後 acceptance 由外部複審決定。

---

# 11. 完成定義

本輪只有在以下全部成立才算完成：

```text
F15 nested LangChain usage accounting：PASS
F19 execution-only resume zero new LLM work：PASS
protected SHORT close regression：PASS
Phase 1 existing regressions：PASS
Phase 2 existing regressions：PASS
無新 full-suite regression：PASS
```

預期 production 修改量應很小。

這不是新 phase，也不是架構升級。

這一輪應視為：

> **Final targeted remediation：修正 2 個剩餘 acceptance 缺口 + 1 個修復時引入的 fail-closed SHORT close regression。**

完成並 push 後，直接交由外部 reviewer 重新抓最新 HEAD 驗收。
