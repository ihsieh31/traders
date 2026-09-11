# Traders Paper-Production P1–P3 修復 — 獨立驗收提示詞

> 驗收 Repository：`https://github.com/ihsieh31/traders`
>
> 原始審查基準：`1e79e1947dd798280ccd5d8f8ce1a9576b71fa5e`
>
> 你的角色：**獨立、read-only 驗收者，不是實作者。**
>
> 目標：驗證 P1–P3 修復是否真正成立，特別防止「測試看起來綠，但 corruption/restart 時仍 fail-open」或「為了修問題破壞既有交易安全不變量」。

---

# 0. 最重要規則：不得修改 repository

你不得：

- 修改任何 tracked file。
- 新增 repo 內測試檔。
- 自動格式化。
- 修 bug。
- commit。
- push。
- reset。
- checkout 覆蓋現況。
- `git clean`。
- 修改 lock file。
- 幫實作者補漏。

若需要臨時 Python script / fixture：

- 只能放 `/tmp/...`
- 執行完不得影響 repository。

開始前：

```bash
git status --short
git rev-parse HEAD
git branch --show-current
```

記錄初始 working tree。

驗收結束再執行：

```bash
git status --short
```

驗收者不得造成新的差異。

若一開始 working tree 已有修改，記錄它們；不要清除。

---

# 1. 驗收結論只能是以下之一

## ACCEPTED

只有全部 P1、P2、P3 需求與 full regression 都通過。

## NOT ACCEPTED

任一必要條件失敗。

不要使用「大致通過」「應該沒事」「看起來可以」作為最終 verdict。

可以在 NOT ACCEPTED 下區分 blocker severity，但整體仍是 NOT ACCEPTED。

---

# 2. 驗收優先級

按順序：

1. 確認 scope / diff。
2. 驗 P1-01。
3. 驗 P1-02。
4. 驗 P2-01。
5. 驗 P2-02。
6. 驗 P2-03。
7. 驗 P3。
8. 跑 targeted tests。
9. 跑 full suite。
10. 重新掃 broker mutation authority、paper-only、HTTP timeout。
11. 確認驗收過程未改 repo。

---

# 3. 先審 diff，不要先相信實作報告

若可取得修復前 commit，執行類似：

```bash
git diff <base>..HEAD --stat
git diff <base>..HEAD -- tradingagents/long_run.py
git diff <base>..HEAD -- tradingagents/safety/guardrails.py
git diff <base>..HEAD -- tradingagents/dataflows/coindesk_utils.py
git diff <base>..HEAD -- requirements.txt
git diff <base>..HEAD -- requirements.lock
git diff <base>..HEAD -- setup.py
git diff <base>..HEAD -- Dockerfile
git diff <base>..HEAD -- .github/workflows/tests.yml
git diff <base>..HEAD -- QUICKSTART.md
git diff <base>..HEAD -- tests
```

若 repository HEAD 已經超過原始基準很多：

- 找實際修復 commit 或使用 merge-base / recent history 確認改動。
- 不要因 base 不完全一致就放棄。
- 驗的是**最新程式目前語意**。

### Scope fail 條件

發現以下任一項直接至少記 P2/P1 finding：

- 改交易策略。
- 改 Top20 ranking。
- 改 prompt 以規避測試。
- 新增 broker mutation 旁路。
- 開 live trading。
- 移除/弱化 paper endpoint guard。
- 移除 timeout/reconciliation/idempotency 保護。
- 大規模無關 refactor 使安全 review 無法可靠完成。

---

# 4. P1-01 驗收 — active.json corruption 必須 fail-closed

## 4.1 靜態檢查

檢查：

- `tradingagents/long_run.py`
- `cli/main.py`
- relevant tests

確認 `load_active_state()` 能分辨：

```text
file does not exist
```

和：

```text
file exists but invalid
```

**不得再透過 generic `read_json()` 把兩者都轉成 None。**

允許：

```text
missing -> None
```

禁止：

```text
corrupt -> None
invalid root -> None
missing run_id -> None
```

corrupt existing state 必須 raise 可識別的 hard-stop exception，例如：

```text
LongRunStop(code="ACTIVE_STATE_CORRUPT")
```

名稱若略有差異可接受，但語意必須相同。

---

## 4.2 動態 adversarial tests

使用 temporary `TRADINGAGENTS_LONG_RUN_DIR`。

### Test A — truly absent

- active path 不存在
- `load_active_state()` 應返回 `None`

### Test B — valid

內容至少有有效 `run_id`。

- 正常回傳
- 不修改檔案

### Test C — truncated JSON

```json
{"run_id":"abc","status":
```

預期：

- raise hard stop
- 不 return None

### Test D — root list

```json
[]
```

預期 hard stop。

### Test E — missing run_id

```json
{"status":"RUNNING"}
```

預期 hard stop。

### Test F — blank run_id

```json
{"run_id":"   ","status":"RUNNING"}
```

預期 hard stop。

---

## 4.3 CLI-level 最重要 regression

建立 corrupt `active.json`。

將可能 broker mutation 全部 mock / instrument：

- `submit_order`
- cancel
- recovery mutation
- 新 run state create/save 路徑

呼叫實際 long-run CLI/control entry。

驗收條件：

- 啟動失敗。
- 沒有新 `run_id`。
- corrupt file 不被覆寫成 fresh state。
- 0 broker mutation。

若只有 unit loader test，沒有 CLI/control-flow 證據：

**P1-01 不得 ACCEPT。**

---

# 5. P1-02 驗收 — safety persistence corruption 必須 fail-closed

## 5.1 靜態檢查

`tradingagents/safety/guardrails.py`

必須存在明確區別：

### state file 不存在

允許 fresh default：

```python
{
    "high_water_mark": None,
    "consecutive_rejections": 0,
    "llm_tokens": {},
}
```

### state file 已存在但：

- unreadable
- malformed JSON
- non-dict
- known field wrong type/value

必須 raise。

禁止：

```python
except (...):
    pass
return fresh_state
```

這種 fail-open pattern。

---

## 5.2 State validation 驗收

至少實際測：

### high_water_mark

ACCEPT：

- `None`
- positive finite numeric

REJECT：

- `"abc"`
- `NaN`
- `Infinity`
- negative / zero（若實作定義 HWM 必須 positive）

### consecutive_rejections

ACCEPT：

- 0
- positive integer

REJECT：

- -1
- `True`
- float
- string

### llm_tokens

ACCEPT：

```json
{"2026-09-09": 123}
```

REJECT：

```json
[]
```

以及：

```json
{"2026-09-09": -1}
```

```json
{"2026-09-09": true}
```

---

## 5.3 Legacy compatibility

測 valid dict 缺少其中一個 known key：

例如：

```json
{"high_water_mark": 100000}
```

應：

- 不因缺 key 拒絕；
- 補 fresh default；
- 已有有效值不得被重置。

這是規格要求。

---

## 5.4 Corruption 不得被覆蓋

建立 corrupt safety state 後 instantiate `SafetyGuard`：

- 應 raise。
- 原 corrupt file 不得被自動覆蓋成 fresh JSON。

這非常重要。

---

## 5.5 Atomic save 驗收

檢查 `_save_state()`：

- temp 在同 directory
- write
- flush
- `os.fsync`
- `os.replace`

建立既有 valid state。

模擬 save 在 `os.replace` 前失敗：

- 原 state file 內容仍存在；
- 不得先被 truncate。

不要求 directory fsync / journaling / backups。

---

## 5.6 Integration fail-closed

至少一個 execution/long-run construction test：

- existing corrupt safety state
- 啟動 auto execution path
- 初始化應停止
- 0 broker mutation

若程式只是 unit test `_load_state`，但上層捕捉 error 後偷偷 fallback disabled/fresh guard：

**P1-02 FAIL。**

---

# 6. P2-01 驗收 — unattended safety 不能關

## 6.1 General SafetyGuard semantics 不應被刪掉

`SafetyGuard(config={"safety_enabled": False})` 一般 unit API 可以仍代表 disabled。

驗收者不要要求整個 feature 被刪。

---

## 6.2 Long-run production gate

驗：

```python
runtime["safety_enabled"] = False
```

### `validate_long_run_config`

必須返回 error。

### direct `run_preflight`

即使 caller 跳過 validation：

- 也必須 hard stop；
- 不能進入可交易狀態；
- 0 broker mutation。

若只有 UI warning：

FAIL。

若只在 config validator 擋，但 direct preflight 可以繞：

FAIL。

---

# 7. P2-02 驗收 — HTTP timeout

檔案：

`tradingagents/dataflows/coindesk_utils.py`

確認所有該函式內 outbound `requests.get` 使用：

```text
timeout=<finite positive>
```

建議 15 秒；不同但合理 bounded value 可接受。

動態 mock：

1. inspect kwargs 有 timeout。
2. raise `requests.exceptions.Timeout`。
3. 函式返回現有 error-contract，不永久掛住。
4. historical as-of path 不應發 HTTP。

不得因本修復新增無上限 retry。

---

# 8. P2-03 驗收 — dependency reproducibility

## 8.1 Manifest architecture

應形成：

### `requirements.txt`

人類維護 top-level runtime dependencies。

至少確認原本 setup 宣告但 requirements 缺少的 direct dependency 已處理：

- langchain
- numpy
- typer

以及既有 lower bounds 沒有被無理由放鬆。

### `setup.py`

不得繼續維護一份與 requirements 重複且易漂移的完整 hard-coded runtime list。

可用小 helper 讀 `requirements.txt`。

必須排除 `setuptools` 作為 runtime install requirement。

### `MANIFEST.in`

若 setup runtime dependency source 依賴 `requirements.txt`，source distribution 必須 include 該檔。

---

## 8.2 Lock file 檢查

預期：

`requirements.lock`

檢查：

- 不含 `-e .`
- 不含本機 absolute path
- 不含 `/tmp`
- 不含 `file://`
- production package 應為 exact resolved version
- 不得只是複製原 `requirements.txt`

如果使用雙 lock，必須有實際 3.11/3.12 incompatibility 證據；沒有證據而無故雙 lock，記 P3 overengineering finding，但不一定單獨阻塞。

---

## 8.3 Fresh environment install

### Python 3.11

建立 temporary venv：

```bash
python3.11 -m venv /tmp/traders-accept-py311
...
pip install -r requirements.lock
```

必須成功。

### Python 3.12

若 executable 可用：

同樣 fresh install。

若本機沒有 3.12：

- 檢查 GitHub Actions matrix 仍包含 3.12；
- 若可讀 CI result，必須確認修復 commit 的 3.12 job success；
- 若完全無法取得 3.12 證據，標示 `UNVERIFIED`，整體不得宣稱 dependency acceptance 完全通過。

---

## 8.4 Docker / CI

`Dockerfile` production dependencies 必須安裝 lock，而不是 range-based `requirements.txt`。

`.github/workflows/tests.yml` Python 3.11 + 3.12 也必須安裝 lock。

不能把 matrix 降成只測 3.11 來讓 lock 過關。

---

## 8.5 Packaging test

至少：

```bash
python -m pip wheel --no-deps . -w /tmp/traders-wheel-accept
```

必須成功。

若 Docker 可用：

```bash
docker build -t traders-accept .
```

必須成功。

若 Docker runtime 不可用，可標示未執行，但需靜態確認 Dockerfile syntax/lock path。

---

# 9. P3 驗收

## P3-01 QUICKSTART

`QUICKSTART.md` 安裝主路徑必須是：

```bash
git clone https://github.com/ihsieh31/traders.git
cd traders
```

不得仍把使用者導到 direct upstream clone。

README 可以保留 upstream attribution link。

---

## P3-02 setup metadata

`setup.py` repository URL 必須指：

```text
https://github.com/ihsieh31/traders
```

可以保留 upstream project URLs / credit。

不得藉此刪除原作者 attribution。

---

# 10. 必跑既有測試

至少：

```bash
python -m pytest tests/test_phase_d_long_run.py -q
python -m pytest tests/test_safety_guardrails.py -q
python -m pytest tests/test_chaos_resilience.py -q
```

再找本次新增/修改的 regression tests 執行。

最後：

```bash
python -m pytest tests/ -q
python -m compileall tradingagents cli webui
git diff --check
python -m pip wheel --no-deps . -w /tmp/traders-wheel-accept
```

### 規則

- 不得用 `-x` 的結果代替 full suite。
- 不得把 failing tests 稱為 pre-existing，除非能用修復前 commit 在相同環境重現並提供證據。
- 不得 skip network-related safety test 來換綠燈。
- 測試應保持 hermetic，不使用真實 Alpaca/LLM credentials。

---

# 11. Broker mutation authority regression

全 repo 搜尋：

```text
submit_order(
close_position(
cancel_order
replace_order
```

確認本次修改沒有：

- WebUI 直接 broker submit
- analyst/graph 直接 submit
- new recovery bypass
- 新 second execution authority

若正式 production code 新增一個可繞過 `ExecutionService` 的下單點：

**直接 NOT ACCEPTED，P1。**

---

# 12. Paper-only regression

檢查：

- `TradingClient(... paper=True)` 或等價 hard paper path 仍存在。
- `ALPACA_USE_PAPER=False` 仍 fail-closed。
- 非 paper endpoint 仍拒絕。
- 本任務沒有新增 live mode。

任何 live enablement：

**直接 NOT ACCEPTED，P1。**

---

# 13. HTTP boundedness regression

搜尋正式碼：

```text
requests.get(
requests.post(
httpx.
urllib.request
```

本次至少確認修改涉及的 CryptoCompare path 已 bounded。

若本次 patch 新增新的 network I/O 卻沒有 timeout，記 P2。

不要擴大到要求一次修完整 repo 所有歷史 HTTP，除非新 diff 明顯引入。

---

# 14. 驗收 Gate

## Gate A — P1

以下全部通過：

- P1-01 active corruption fail-closed
- P1-02 safety corruption fail-closed
- no broker mutation bypass
- paper-only intact

否則直接 `NOT ACCEPTED`。

## Gate B — P2

以下全部通過：

- unattended safety cannot be disabled
- CoinDesk/CryptoCompare timeout bounded
- dependency lock/install contract成立
- full regression green

任一失敗：`NOT ACCEPTED`。

## Gate C — P3

QUICKSTART / metadata 修正完成。

任一指定 P3 沒完成：

`NOT ACCEPTED`，但標示為 non-safety blocker。

---

# 15. 最終報告格式

請嚴格使用：

```markdown
# Acceptance Result

## Verdict
ACCEPTED / NOT ACCEPTED

## Repository State
- HEAD:
- Branch:
- Initial git status:
- Final git status:
- Reviewer modified repo: NO

## Gate A — P1
### P1-01 Active state corruption
- Result: PASS / FAIL
- Evidence:
- Adversarial test:
- Broker mutations observed:

### P1-02 Safety state corruption
- Result:
- Evidence:
- Corruption behavior:
- Legacy behavior:
- Atomic write behavior:
- Broker mutations observed:

### Core invariants
- broker mutation authority:
- paper-only:
- idempotency/reconciliation regression:

## Gate B — P2
### P2-01 Unattended safety gate
- Result:
- Validation bypass test:

### P2-02 HTTP timeout
- Result:
- Timeout value:
- Timeout exception behavior:

### P2-03 Dependency reproducibility
- Result:
- lock:
- Python 3.11 fresh install:
- Python 3.12 fresh install / CI:
- Docker:
- wheel:

## Gate C — P3
### P3-01 QUICKSTART
- Result:

### P3-02 setup metadata
- Result:

## Test Results
- targeted:
- full pytest:
- compileall:
- git diff --check:
- wheel:
- Docker:

## Findings
按嚴重度：
- P1:
- P2:
- P3:

如果沒有就寫 None，不要硬找問題。

## Final Paper-Production Decision
- attended Alpaca Paper: GO / NO-GO
- unattended single-host single-account Alpaca Paper: GO / NO-GO

## Required Follow-up
只列真正阻塞項；若 ACCEPTED 寫 None。
```

---

# 16. 最終判定原則

不要因為：

- 測試數量很多；
- commit message 寫 FIXED；
- 實作者報告寫 production-ready；
- unit tests 綠；

就直接接受。

這次最核心的驗收問題是：

> **當持久化 state 已存在但損壞時，系統是否「停止」而不是「假裝第一次啟動」。**

以及：

> **unattended trading 是否永遠無法在 safety disabled 的狀態下繼續。**

如果這兩件事沒有用實際 adversarial control-flow test 證明：

**NOT ACCEPTED。**
