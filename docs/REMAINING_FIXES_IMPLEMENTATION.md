# Remaining Fixes — Active State Fail-Closed + Reproducible Dependency Lock

## 目的

這一輪只修復目前驗收剩下的兩個問題，不做額外重構、不擴大 scope。

目前基準：

- Repository: `https://github.com/ihsieh31/traders`
- Baseline commit: `64b84aa0515af8d7e68b13f94a47cb01d5edffab`
- 現有 full test suite 已在 Python 3.11 / 3.12 通過：
  - `914 passed`
  - `240 subtests passed`

本輪修完後：

1. 直接跑完整測試與必要的 targeted tests。
2. 全部通過後直接 commit + push 到 `main`。
3. 不需要建立獨立「驗收 phase」或額外 acceptance prompt。
4. 不要修改與本次問題無關的交易策略、screening、risk、execution 邏輯。

---

# 修復範圍

只處理：

1. **P1 — existing `active.json` 不得因未知 / terminal status 被當成「沒有 active run」**
2. **P2 — `requirements.lock` 必須成為完整、封閉、可重現的 dependency set**

除此之外不要擴充功能。

---

# P1 — Active State Loader 必須對任何既有 state fail-closed

## 問題

目前 `load_active_state()` 已經正確處理：

- `active.json` 不存在 → `None`
- malformed / truncated JSON → hard stop
- root 不是 dict → hard stop
- 缺少 `run_id` → hard stop
- 空白 `run_id` → hard stop

但目前仍存在一個 fail-open：

```python
if data.get("status") in ("RUNNING", "INTERRUPTED"):
    return data
return None
```

這代表只要 `active.json` 本身存在、JSON 合法、`run_id` 合法，但 `status` 不是 `RUNNING` / `INTERRUPTED`，loader 就會回傳 `None`。

例如：

```json
{
  "run_id": "phase-d-old-run",
  "status": "RUNNIG"
}
```

或：

```json
{
  "run_id": "phase-d-old-run",
  "status": "COMPLETED"
}
```

CLI 會把 `None` 解讀成「目前沒有 active observation」，然後進入建立新 observation 的流程。

這違反本系統對 durable state 的 fail-closed 原則。

---

## 為什麼 terminal status 也不能直接忽略

`finalize_observation()` 的流程並不是先刪 active state。

它大致是：

1. 將 state 設為 `COMPLETED` / `STOPPED`
2. `save_active_state(state)`
3. 寫 manifest
4. 抓 final account snapshot
5. aggregate / write final reports
6. alerts / logs
7. 最後才 `clear_active_state()`

所以 process 完全可能在：

```text
save_active_state(COMPLETED)
        ↓
final report / alert 尚未完成
        ↓
process crash
```

此時磁碟上會合法存在：

```json
{
  "run_id": "...",
  "status": "COMPLETED"
}
```

這不能被當成 fresh install。

否則重啟後可能建立新的 observation，造成：

- 舊 observation finalization 未完成
- 新 run_id 被建立
- 舊 state / report / execution evidence 與新 observation 分裂
- idempotency boundary 被繞過

---

## 正確規則

### 唯一可以回傳 `None` 的情況

只有：

```text
active.json 根本不存在
```

### 任何 existing `active.json`

都必須：

- 若是合法 resumable state → return state
- 否則 → hard stop

不得 silently return `None`。

---

## 建議最小修改

不要重構 state machine。

只修改 `load_active_state()` 最後的 status handling。

建議：

```python
status = data.get("status")

if status in ("RUNNING", "INTERRUPTED"):
    return data

raise LongRunStop(
    "ACTIVE_STATE_CORRUPT",
    f"active state exists with unexpected/non-resumable status {status!r}",
)
```

若你認為 `ACTIVE_STATE_CONFLICT`、`ACTIVE_STATE_TERMINAL` 等錯誤碼語意更清楚，也可以新增，但：

- 不需要新增複雜 state recovery
- 不要自動刪檔
- 不要自動 finalize
- 不要自動建立新 observation

本輪只要求 fail-closed。

---

## 額外要求

### 不可：

- `unlink()` / 刪除未知 state
- 將 unknown status 自動改寫成 `RUNNING`
- 將 terminal state 自動視為不存在
- 自動建立新的 run_id
- 自動猜測 operator intent

### CLI 行為

`cli long-run` 遇到此類 existing but non-resumable state：

```text
打印明確錯誤
→ exit non-zero
→ 不做 preflight mutation
→ 不建立新 observation
→ 不產生新 run_id
→ 不做 broker submit/cancel
```

---

# P1 驗收方法

必須新增 targeted regression tests。

至少包含：

## Test 1 — typo status

建立：

```json
{
  "run_id": "existing-run",
  "status": "RUNNIG"
}
```

預期：

```text
load_active_state()
→ raises LongRunStop
```

不得：

```text
return None
```

---

## Test 2 — terminal COMPLETED state still exists

建立：

```json
{
  "run_id": "existing-run",
  "status": "COMPLETED"
}
```

預期：

```text
hard stop
```

不能當成 fresh observation。

---

## Test 3 — terminal STOPPED state still exists

建立：

```json
{
  "run_id": "existing-run",
  "status": "STOPPED"
}
```

預期：

```text
hard stop
```

---

## Test 4 — CLI-level no new run / no broker mutation

針對至少一種 existing invalid / terminal state 跑 CLI path。

必須證明：

```text
new_observation_state calls == 0
broker submit calls == 0
broker cancel calls == 0
post-authorization recovery calls == 0
```

若測試架構不方便直接觀察 `new_observation_state`，至少要 assert：

- 沒建立新的 active state
- 沒建立新的 observation directory / manifest
- 沒進入 preflight / recovery mutation path

---

## Test 5 — missing file behavior unchanged

`active.json` 不存在：

```text
load_active_state() is None
```

這是唯一合法 fresh-start path。

---

# P2 — requirements.lock 必須真正封閉

## 問題

目前已有 `requirements.lock`，大部分 package 都使用 exact pin：

```text
package==x.y.z
```

Docker 與 GitHub Actions 也已開始使用它。

但目前 CI 的實際 install log 顯示：

```text
pip install -r requirements.lock
```

仍會額外解析 lock 裡沒有的 transitive dependencies。

已確認至少有：

```text
py-mini-racer>=0.6.0
→ py-mini-racer==0.6.0

akracer>=0.0.13
→ akracer==0.0.14

greenlet>=1
→ greenlet==3.5.5
```

這些不是目前 `requirements.lock` 裡的 explicit exact pins。

因此現在的 lock 仍不是 closed dependency set。

未來 PyPI 上游更新後，同一份 `requirements.lock` 仍可能裝出不同環境。

---

# P2 正確目標

`requirements.lock` 必須滿足：

```text
所有 runtime dependency
+
所有 transitive dependency
=
完整 exact pins
```

而 CI / Docker 安裝時不得再讓 pip 自行補 dependency。

理想驗證方式：

```bash
pip install --no-deps -r requirements.lock
pip check
```

含意：

### `--no-deps`

只安裝 lock 中明確列出的 package。

如果 lock 漏 dependency，pip 不會偷偷修正。

### `pip check`

驗證已安裝套件的 dependency requirement 是否全部被滿足。

如果漏：

```text
akracer
greenlet
py-mini-racer
...
```

CI 就必須 fail。

---

# P2 建議修法

## Step 1 — 從 clean Python 3.11 environment 重新生成完整 lock

不要只手工補目前看到的 3 個 package。

在乾淨環境執行 equivalent 流程：

```bash
python3.11 -m venv /tmp/traders-lock
source /tmp/traders-lock/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt

pip freeze --all > requirements.lock
```

需確認 output：

- 所有行皆為 deterministic exact pin
- 沒有 local file path
- 沒有 machine-specific editable package
- 沒有 `@ file:///...`
- 沒有 temp path
- 沒有 credentials
- 沒有 platform-specific垃圾值

若 clean freeze 產生不應納入 production runtime 的 package，請確認其來源，不要直接亂刪。

---

## Step 2 — CI 改為封閉安裝

目前：

```bash
pip install -r requirements.lock pytest
```

這裡有兩個問題：

1. lock 仍允許 dependency resolution
2. `pytest` 本身未 lock

本輪建議做法有兩種。

### 推薦方案 A

將 test-only dependencies 也固定版本。

例如新增：

```text
requirements-test.lock
```

或直接將 pytest / pytest transitive dependencies納入現有 lock。

CI：

```bash
pip install --no-deps -r requirements.lock
pip check
python -m pytest tests/ -v --tb=short
```

如果 pytest 也在 `requirements.lock`：

最乾淨。

### 可接受方案 B

runtime lock 與 test deps 分離：

```bash
pip install --no-deps -r requirements.lock
pip check

pip install "pytest==<exact-version>"
```

但若採方案 B，至少 pytest 版本必須 exact pin。

不要保留：

```bash
pip install ... pytest
```

因為這會讓 CI test tool 本身漂移。

---

## Step 3 — Docker 使用同一 closed lock

Docker 目前已使用 `requirements.lock`。

改成：

```dockerfile
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-deps -r requirements.lock \
    && python -m pip check
```

注意：

若 build tool 的 `pip/setuptools/wheel` 更新本身不影響 runtime dependency determinism，可以保留。

但 runtime dependency 必須全部由 closed lock 決定。

---

## Step 4 — 保持 requirements.txt 為 human-maintained top-level manifest

現有結構可以保留：

```text
requirements.txt
    = 人工維護的 top-level dependency constraints

requirements.lock
    = clean environment resolve 後的完整 exact dependency closure
```

不要把所有 transitive dependency 再塞回 `requirements.txt`。

---

# P2 驗收方法

## Test / CI 1 — closed install

在全新環境：

```bash
pip install --no-deps -r requirements.lock
pip check
```

必須 exit 0。

這是最重要的驗收。

---

## Test / CI 2 — 不得出現額外 dependency resolution

CI log 裡不應再看到：

```text
Collecting <package> (from some-package==...->-r requirements.lock)
```

對於 lock 內不存在的 runtime dependency。

所有 runtime dependency 都必須可以在 `requirements.lock` 找到 exact pin。

---

## Test / CI 3 — Python 3.11 full suite

```bash
python -m pytest tests/ -v --tb=short
```

必須全部通過。

---

## Test / CI 4 — Python 3.12 full suite

同樣全部通過。

如果同一份 lock 無法支援 3.11 / 3.12，請不要自行拆分大架構。

先確認是否是：

- package wheel compatibility
- Python marker
- freeze environment 問題

在保持現有架構下最小處理。

---

## Test / CI 5 — Docker build

至少確認：

```bash
docker build .
```

dependency install 階段可以成功：

```text
--no-deps install
pip check
```

若 CI 現在沒有 Docker build job，不強制新增大型 pipeline。

但本地或現有可用流程至少要驗一次 Dockerfile 安裝邏輯。

---

# 不要修改的範圍

本輪嚴禁順手重構：

- screening pipeline
- Top20 selection
- LLM role routing
- failover
- execution service
- broker reconciliation
- safety breaker arithmetic
- portfolio sizing
- protective orders
- long-run scheduler
- report aggregation
- historical data boundary
- Alpaca paper-only lock

除非修這兩個問題必須碰到，否則不改。

---

# 必須保留的既有安全性

修復後必須保持：

## Paper-only

```text
TradingClient(..., paper=True)
ALPACA_USE_PAPER=False → fail closed
live / unknown endpoint → fail closed
```

## Broker mutation authority

正式 production code 中 broker mutation 仍只能經：

```text
tradingagents/execution/service.py
```

不要新增直接：

```python
broker.submit_order(...)
broker.cancel_order_by_id(...)
broker.close_position(...)
```

到其他 production module。

## Preflight

仍必須：

```text
read-only
```

且 user authorization 之前不得執行 recovery mutation。

## Safety

unattended long-run：

```text
safety_enabled must be exactly True
```

且 preflight 在任何 network probe 前就應 fail closed。

---

# 完成條件

以下全部成立才可以直接 push。

## P1

- [ ] existing typo status hard-stops
- [ ] existing `COMPLETED` active state hard-stops
- [ ] existing `STOPPED` active state hard-stops
- [ ] missing active file still returns `None`
- [ ] invalid existing state cannot create new run
- [ ] invalid existing state causes 0 broker mutations

## P2

- [ ] regenerated full `requirements.lock`
- [ ] all runtime transitive dependencies exact-pinned
- [ ] `pip install --no-deps -r requirements.lock` succeeds
- [ ] `pip check` succeeds
- [ ] CI no longer installs unpinned `pytest`
- [ ] Docker uses closed lock install + `pip check`

## Regression

- [ ] Python 3.11 full test suite PASS
- [ ] Python 3.12 full test suite PASS
- [ ] paper-only tests PASS
- [ ] broker authority tests PASS
- [ ] active-state tests PASS
- [ ] safety persistence tests PASS
- [ ] preflight read-only tests PASS

---

# Commit / Push 要求

這次不需要建立額外 acceptance phase。

修復完成並確認上述測試全部通過後：

1. review `git diff`
2. 確認沒有 unrelated files
3. commit
4. push 到 `main`

建議 commit message：

```text
fix: close remaining long-run state and dependency lock gaps
```

push 後回報：

```text
commit SHA:
files changed:
targeted tests:
full CI result:
```

不要自行宣稱「production ready」或「正式驗收通過」。

只需要如實回報修復與測試結果，之後由外部 review 最新 `main`。
