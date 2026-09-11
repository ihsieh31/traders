# Traders Paper-Production P1–P3 修復 — 實作提示詞

> 目標 Repository：`https://github.com/ihsieh31/traders`
>
> 審查基準 HEAD：`1e79e1947dd798280ccd5d8f8ce1a9576b71fa5e`
>
> 目的：在**不過度工程化、不改交易策略、不重寫 execution architecture** 的前提下，修掉目前仍可用程式直接改善的 P1–P3 問題，讓專案達到「單機、單 Alpaca Paper account、可長時間 unattended paper trading」的工程品質。

---

## 0. 你的角色與工作方式

你是本專案的實作者。請直接在目前 repository 上修改程式、補測試並執行驗證。

### 先做這些事

1. `git status --short`
2. `git rev-parse HEAD`
3. 確認目前分支與 HEAD。
4. 閱讀本提示詞列出的相關檔案及既有測試。
5. 如果 HEAD 已不是上述基準：
   - **不要 reset、不要 checkout 舊 commit、不要覆蓋使用者後續修改。**
   - 先看基準到目前 HEAD 的 diff。
   - 依照本提示詞要求的「安全語意」適配到目前最新架構。
6. 修改前記錄原始測試結果；修改後完整重跑。

### 禁止事項

- 不得切換成 live trading。
- 不得弱化 `paper=True` / paper endpoint 檢查。
- 不得改變交易策略、Top20 選股規則、LLM prompt、position sizing 演算法。
- 不得新增 PostgreSQL、Redis、Kafka、Celery、分散式鎖、額外服務。
- 不得新增第二套 order state database。
- 不得讓 WebUI / CLI / agent 直接繞過 `ExecutionService` 下單。
- 不得把 fail-closed 改成 warning-only。
- 不得為了測試通過刪掉或 skip 原本測試。
- 不得降低既有 timeout / retry / idempotency / reconciliation 保護。
- 不得做大規模 rename、格式化全專案或無關 refactor。
- 不要碰與本任務無關的檔案。
- 不要 push、不要 force push、不要改 Git history；只完成工作樹修改與測試。

---

# 1. 最終必須維持的系統不變量

本任務完成後，以下不變量必須仍成立：

1. 所有真正 broker mutation 仍集中在既有 execution layer。
2. Alpaca 仍為 paper-only。
3. `submit_order` timeout / UNKNOWN state 不得靠盲目 retry 解決。
4. idempotency identity 不得因本次修改改格式或失效。
5. preflight 在使用者正式授權前不得產生 broker mutation。
6. screening / analysis provider failure 仍必須 bounded retry 後 fail-closed。
7. kill switch 仍是最高優先級停止機制。
8. risk-reducing exit 的既有例外語意不得被本次修改破壞。
9. SQLite execution store、outbox、reconciliation 架構不重做。
10. 單機 / 單帳戶是本次 deployment scope；不要試圖支援 distributed executor。

---

# 2. 本次問題總表

| ID | 等級 | 問題 | 是否阻塞 unattended paper |
|---|---|---|---|
| P1-01 | P1 | `active.json` 損壞時被當成「沒有 active run」，可能建立新 `run_id` 並繞過原本 idempotency identity | 是 |
| P1-02 | P1 | Safety state 損壞時靜默重置 HWM / rejection / token state，形成 fail-open | 是 |
| P2-01 | P2 | unattended long-run 沒有硬性禁止 `safety_enabled=False` | 是，修完才允許 unattended |
| P2-02 | P2 | CryptoCompare/CoinDesk news HTTP request 沒有 timeout，外部服務可永久掛住分析 | Crypto 使用時是 |
| P2-03 | P2 | production dependency 安裝不可完全重現；`requirements.txt`、`setup.py`、Docker、CI 契約不一致 | 否，但商用品質要求修 |
| P3-01 | P3 | `QUICKSTART.md` clone 仍指向舊上游 repo | 否 |
| P3-02 | P3 | `setup.py` repository metadata 仍指向舊上游 | 否 |

**只修以上項目。**

---

# 3. P1-01 — Active long-run state corruption 必須 fail-closed

## 3.1 現況與風險

主要檔案：

- `tradingagents/long_run.py`
- `cli/main.py`
- `tests/test_phase_d_long_run.py`
- 必要時補到既有 full-review regression test 檔

目前邏輯本質上是：

```python
def read_json(path):
    try:
        ...
    except (OSError, json.JSONDecodeError):
        return None

def load_active_state():
    data = read_json(active_path())
    if not isinstance(data, dict) or not data.get("run_id"):
        return None
```

而 CLI 看到 `load_active_state() is None` 時會進入「沒有 active observation」的新建流程。

問題：

- 檔案真的不存在。
- JSON 截斷。
- JSON 無法解析。
- root 不是 object。
- object 缺 `run_id`。

現在可能全部被折疊成 `None`。

但是 execution decision identity 會包含 `run_id`。若因 state corruption 產生新 run，同一 session / symbol 會得到新的 decision identity，原本對舊 run 的 idempotency 無法證明可以保護這次重新開始。

**商用安全規則：只有「檔案不存在」可以表示沒有 active run；檔案存在但不可可信解析時必須停止。**

---

## 3.2 必須採用的修法

### 決策已定：不要全域修改 `read_json()`

`read_json()` 目前還被 config 等非關鍵資料使用。不要把它整體改成 strict，避免擴大改動範圍。

### 在 critical active-state loader 內做 strict distinction

`load_active_state()` 必須具有以下語意：

#### Case A — `active.json` 根本不存在

```text
return None
```

這是唯一可以代表「沒有 active run」的情況。

#### Case B — 檔案存在且是有效 state

回傳 dict。

至少必須確認：

- root 是 dict
- `run_id` 是 non-empty string

若現有 state 有 schema/status 等既有必要欄位，維持原本兼容性，不要自行發明大 schema migration。

#### Case C — 檔案存在，但出現以下任一情況

- permission/read I/O error
- JSONDecodeError
- root 非 dict
- `run_id` missing
- `run_id` 不是 string
- `run_id.strip()` 為空

必須：

```text
raise LongRunStop(
    code="ACTIVE_STATE_CORRUPT",
    detail=<不包含 secrets 的可理解訊息>
)
```

可使用既有 `LongRunStop`，**不要另外設計大型 exception hierarchy**。

### 錯誤訊息規則

可以包含：

- state path
- corruption 類型

不得 dump 整個 state，不得把可能含 secret 的任意內容塞到 error。

---

## 3.3 CLI 行為要求

當 `load_active_state()` 因 corruption 丟出 `LongRunStop`：

- CLI 必須安全結束。
- 不得進 setup wizard 後建立新 run。
- 不得呼叫 `create_run_state` / 等價新建流程。
- 不得發生 broker mutation。
- 不得自動刪除、覆寫或修復 `active.json`。
- 操作者必須明確知道 state 需要人工處理。

如果現有 CLI 已有 `LongRunStop` catch，可最小化沿用；若 catch 位置不涵蓋 `load_active_state()`，只調整必要控制流。

---

## 3.4 P1-01 必補測試

至少新增/修改以下測試：

1. `active.json` 不存在：
   - `load_active_state() is None`

2. valid active state：
   - 正常回傳原 state
   - 不改檔案

3. truncated JSON：
   - `{"run_id":"x","status":`
   - 必須 raise `LongRunStop`
   - `code == "ACTIVE_STATE_CORRUPT"`

4. valid JSON 但 root 是 list：
   - raise

5. valid dict 但沒有 `run_id`：
   - raise

6. `run_id=""` / whitespace：
   - raise

7. CLI regression：
   - 預先建立 corrupt `active.json`
   - mock 所有可能 broker mutation
   - 執行 long-run 啟動入口
   - assert **沒有新 run 被建立**
   - assert **0 broker mutation**

### 必須修改既有錯誤測試

目前若有：

```python
self.assertIsNone(lr.load_active_state())
```

對 corrupt JSON 的測試，必須改成新的 fail-closed expectation。

不能保留舊語意。

---

# 4. P1-02 — Safety state corruption 不得靜默歸零

## 4.1 現況與風險

主要檔案：

- `tradingagents/safety/guardrails.py`
- `tests/test_safety_guardrails.py`
- `tests/test_chaos_resilience.py`
- 必要時相關 integration regression test

目前 `_load_state()` 在讀取/解析失敗後會回傳 fresh state，大致：

```python
{
    "high_water_mark": None,
    "consecutive_rejections": 0,
    "llm_tokens": {}
}
```

這會把：

- equity high-water mark
- consecutive broker rejection streak
- daily LLM token usage

全部重置。

結果：

- drawdown breaker 可能失去過去高點；
- rejection circuit breaker 重新從 0 開始；
- token budget 歸零。

這不是 acceptable fail-closed behavior。

---

## 4.2 必須採用的修法

### 新增小型、局部 exception

在 `guardrails.py` 內新增：

```python
class SafetyStateError(RuntimeError):
    pass
```

不要建立額外 package/module。

### 新增 fresh-state helper

例如：

```python
def _fresh_state():
    return {
        "high_water_mark": None,
        "consecutive_rejections": 0,
        "llm_tokens": {},
    }
```

避免 default state 在多處複製。

### `_load_state()` 的新語意

#### Case A — state file 不存在

這是全新安裝的正常情況：

```text
return fresh state
```

#### Case B — state file 存在且可解析

必須做最小必要 validation。

已知欄位規則：

- `high_water_mark`
  - `None` 或 positive finite number
- `consecutive_rejections`
  - non-negative integer
  - `bool` 不算合法 integer
- `llm_tokens`
  - dict
  - key 必須可視為日期字串；不必做昂貴 schema
  - value 必須是 non-negative integer
  - `bool` 不合法

### 向後相容決策

如果 state 是 dict，但**缺少某個已知 key**：

- 可以用 fresh default 補上缺少 key；
- 不視為 corruption。

這是刻意決策，避免舊版 state 因新增欄位無法升級。

如果欄位**存在但型別/值非法**：

- raise `SafetyStateError`
- 不得偷偷 normalize 成 0

未知額外 key：

- 保留，不需要刪掉；
- 不要因 unknown key fail。

### I/O / JSON error

只要檔案存在卻：

- read permission failure
- I/O error
- JSON parse failure

必須 raise `SafetyStateError`。

---

## 4.3 Safety state write durability 一起補強

因為已經修改同一 persistence 模組，順便把 `_save_state()` 做成更可靠的 atomic durable write。

### 要求

- temp file 必須建立在 `state_path.parent`
- 寫 JSON
- `flush()`
- `os.fsync(file.fileno())`
- `os.replace(temp, state_path)`
- 失敗時盡力清 temp
- 不得先 truncate 真正 state file
- 不需要 backup rotation
- 不需要 journal
- 不需要 database
- 不要求 parent-directory fsync；不要過度工程化

可參考 `long_run.atomic_write_json()` 的風格，但避免 safety layer 為此依賴 `long_run.py`。

---

## 4.4 Startup fail-closed

當 `SafetyGuard(...)` 因 existing corrupt safety state raise：

- execution / long-run 啟動必須直接失敗；
- 不得改用 disabled guard；
- 不得自動建立 fresh state 覆蓋 corrupt state；
- 不得發 broker order。

不需要自動 engage kill switch，因為初始化本身失敗就已 fail-closed。

---

## 4.5 P1-02 必補測試

至少：

1. state 不存在 → fresh state 正常初始化。
2. valid full state → 原值保存。
3. valid legacy dict 缺 key → 只補缺少 default。
4. malformed JSON → `SafetyStateError`。
5. JSON root list → `SafetyStateError`。
6. `high_water_mark="abc"` → error。
7. `high_water_mark=NaN/inf` → error。
8. negative rejection → error。
9. `consecutive_rejections=True` → error。
10. `llm_tokens=[]` → error。
11. token count negative / bool → error。
12. corrupt state 啟動 execution path → 0 broker mutation。
13. `_save_state()` 寫出的檔案可重新 load。
14. 模擬 replace 前失敗時，既有正式 state 不應被 truncate。

不要寫依賴真實 Alpaca / 真實 LLM 的測試。

---

# 5. P2-01 — unattended long-run 禁止 `safety_enabled=False`

## 5.1 問題

`SafetyGuard` 本身刻意允許：

```python
safety_enabled=False
```

而現有測試甚至驗證 safety disabled 時可略過 kill switch / notional 等檢查。

這個 feature 可以保留給研究/特殊測試環境。

**但是 unattended long-run / production paper execution 不可以允許它。**

---

## 5.2 修復邊界

不要改 `SafetyGuard.check_order()` 的一般語意。

也不要刪除 `safety_enabled` feature。

只在 unattended long-run production path 加硬性 gate。

### 建議實作

在 `tradingagents/long_run.py` 增加小 helper，例如：

```python
def _validate_unattended_safety(runtime):
    ...
```

語意：

```python
runtime.get("safety_enabled", True) is True
```

才允許。

下列都視為 invalid：

- `False`
- `0`
- `"false"`
- `None`（若 key 明確存在為 None）

如果 key 完全不存在，可按既有 default behavior 視為 True；但 `build_runtime_config()` 正常應帶入 default。

### 必須在兩層防守

1. `validate_long_run_config(..., runtime=runtime)`
   - 加入清楚 validation error

2. `run_preflight(...)`
   - 也必須自行 fail-closed
   - 不可只相信 caller 一定先跑 validation

若 `run_preflight` 收到 disabled safety：

```text
raise LongRunStop("SAFETY_DISABLED", ...)
```

並且必須在任何 Alpaca/broker side effect 前發生。

---

## 5.3 測試

1. runtime safety true → 原本正常。
2. safety false → config validation error。
3. direct `run_preflight` + false → `LongRunStop`.
4. broker/account/calendar/LLM probe mocks assert 未被不必要呼叫；至少 broker mutation 為 0。
5. `SafetyGuard` 的一般 unit test `safety_enabled=False` 可以保留，不要破壞 general-purpose semantics。

---

# 6. P2-02 — CryptoCompare news HTTP request 加 bounded timeout

## 6.1 問題

檔案：

`tradingagents/dataflows/coindesk_utils.py`

目前：

```python
requests.get(url, headers=headers)
```

沒有 timeout。

如果外部 socket 卡住，分析 thread/process 可能無限等待。

---

## 6.2 必須採用的修法

在該 module 定義局部常數：

```python
HTTP_TIMEOUT_SECONDS = 15
```

或沿用專案已有同類 dataflow timeout constant；若已有清楚統一 convention，優先沿用，不新增第二套。

請改成：

```python
requests.get(
    url,
    headers=headers,
    timeout=HTTP_TIMEOUT_SECONDS,
)
```

### Exception semantics

現有：

```python
except requests.exceptions.RequestException as e:
```

已能涵蓋 Timeout。

保持既有 caller 契約：回傳 error string，而不是在這次任務改成新的 exception architecture。

不要加入 retry；bounded timeout 已足夠，避免擴大行為。

---

## 6.3 測試

1. mock `requests.get`，確認 `timeout` 是 finite positive number。
2. 模擬 `requests.exceptions.Timeout`：
   - 函式必須有限時間返回既有格式的 error result；
   - 不 crash。
3. historical as-of 原本應在 HTTP 前拒絕的路徑仍不得呼叫 `requests.get`。
4. 不發任何真實 HTTP。

---

# 7. P2-03 — 可重現 dependency/install contract

## 7.1 問題

目前 production install 契約分散：

- `requirements.txt`
- `setup.py install_requires`
- `Dockerfile`
- `.github/workflows/tests.yml`

且很多 dependency 沒 exact pin。

結果同一 commit 在未來重新 build 可能解析到不同 transitive dependency。

另外 `setup.py` 與 `requirements.txt` direct dependencies 目前也不一致。

---

## 7.2 這次採用的設計

### `requirements.txt`

定位：

> 人類維護的 top-level runtime dependency specification。

保留合理 version ranges，不要求每個 transitive package 都手動寫進這個檔案。

### `requirements.lock`

新增：

> production/CI 的 exact resolved lock。

生成方式：

1. 使用**乾淨的 Python 3.11 virtualenv**。
2. 升級 pip/setuptools/wheel。
3. 安裝最終版 `requirements.txt`。
4. 用該乾淨 venv 的 resolved environment 生成 exact lock。
5. lock 不得包含：
   - editable local repo
   - `/tmp/...`
   - `file://...`
   - developer machine absolute path

**禁止手工猜 transitive versions。**

### 跨 Python 版本規則

現有 CI 是 Python 3.11 + 3.12。

優先使用單一 `requirements.lock`。

只有當同一 lock 經實際證明無法同時安裝在 3.11 / 3.12 時，才允許：

- `requirements-py311.lock`
- `requirements-py312.lock`

不要一開始就做雙 lock。

---

## 7.3 先修 top-level manifest 一致性，再產 lock

目前 `setup.py` 宣告、但 `requirements.txt` 沒明確列出的 direct runtime dependencies，要加入 `requirements.txt`：

```text
langchain>=0.3.27,<0.4.0
numpy>=1.24.0
typer>=0.9.0
```

同時把以下已存在但 setup 有較明確 lower bound 的條目同步為至少：

```text
pandas>=2.0.0
praw>=7.7.0
stockstats>=0.5.4
rich>=13.0.0
questionary>=2.0.1
gradio>=4.0.0
plotly>=5.18.0
```

已有更嚴格/相容 constraint 時保留更嚴格者。

不要移除目前 `requirements.txt` 其他依賴。

### `setup.py install_requires` 的最小一致性方案

不要再維護另一份完整 hard-coded dependency list。

在 `setup.py` 用一個很小 helper 讀取 repository root 的 `requirements.txt`：

- 忽略 blank line
- 忽略 `#` comment
- 忽略 `setuptools` 本身（build dependency，不放 runtime install_requires）
- 若未來看到 `-r` / index directive，應明確拒絕或忽略，不要把無效 directive 傳給 setuptools

`install_requires` 使用此 helper 的結果。

新增 `MANIFEST.in`：

```text
include requirements.txt
```

以確保 source distribution 仍能讀到它。

不要遷移到 Poetry/PDM/uv/全新 pyproject dependency system；本次不要重構 packaging。

---

## 7.4 `requirements.lock` 使用位置

### Dockerfile

builder stage：

- `COPY requirements.txt requirements.lock ./`
- production dependency 安裝改用 `requirements.lock`

例如：

```bash
python -m pip install -r requirements.lock
```

仍保留：

```bash
python -m pip install --no-deps -e .
```

因 dependency 已先由 lock 安裝。

### GitHub Actions

`.github/workflows/tests.yml`

CI 改為：

```bash
pip install -r requirements.lock pytest
```

Python 3.11 / 3.12 matrix 不變。

不要移除 offline/hermetic test 原則。

---

## 7.5 Dependency 驗收

至少完成：

1. `requirements.lock` 每個正常 package 都是 exact resolved version。
2. fresh Python 3.11 venv：
   - `pip install -r requirements.lock`
   - 成功。
3. Python 3.12：
   - 若本機可用，實際安裝；
   - 若本機不可用，CI matrix 必須負責驗證，實作報告需說明未在本機跑。
4. `python -m pip wheel --no-deps . -w <tmpdir>` 成功。
5. `setup.py` 不再有與 `requirements.txt` 長期漂移的第二份 hard-coded dependency list。
6. Dockerfile 使用 lock。
7. CI 使用 lock。
8. 不因 lock 破壞 Python 3.11 / 3.12 測試。

---

# 8. P3-01 — QUICKSTART clone 指向錯誤 repo

檔案：

`QUICKSTART.md`

目前還指向：

```bash
git clone https://github.com/huygiatrng/AlpacaTradingAgent.git
cd AlpacaTradingAgent
```

改成：

```bash
git clone https://github.com/ihsieh31/traders.git
cd traders
```

只修與目前 repository 不一致的安裝指令。

不要重寫整份 QUICKSTART。

如果其他 instruction 因 repo name 明顯失效，可一併做最小必要修正。

---

# 9. P3-02 — setup.py repository metadata

目前 `setup.py` 的 `url` 仍是上游。

改成：

```text
https://github.com/ihsieh31/traders
```

可新增 `project_urls`：

- Source / Repository → `https://github.com/ihsieh31/traders`
- Upstream TradingAgents → 原官方來源
- Upstream AlpacaTradingAgent → fork 上游來源

但不要改作者/授權歸屬來抹掉 upstream credit。

README 既有 upstream attribution 不得刪。

---

# 10. 建議實作順序

請依以下順序做，避免 dependency lock 因 requirements 後續變動而失效：

1. P1-01 active state fail-closed。
2. P1-02 safety state fail-closed + durable save。
3. P2-01 unattended safety hard gate。
4. P2-02 HTTP timeout。
5. P3-01 QUICKSTART。
6. P3-02 setup metadata。
7. P2-03 先同步 `requirements.txt` / `setup.py`，最後才生成 lock。
8. 更新 Docker / CI 使用 lock。
9. 跑 targeted tests。
10. 跑 full suite。
11. 做 scope / mutation authority regression scan。

---

# 11. 必跑測試與靜態驗證

至少執行：

```bash
python -m pytest tests/test_phase_d_long_run.py -q
python -m pytest tests/test_safety_guardrails.py -q
python -m pytest tests/test_chaos_resilience.py -q
```

再搜尋與本次改動直接相關的 integration/full-review tests 一併跑。

最後必須：

```bash
python -m pytest tests/ -q
python -m compileall tradingagents cli webui
git diff --check
```

若 repository 本身有既定 wheel/build test，也要跑。

Dependency：

```bash
python -m pip wheel --no-deps . -w /tmp/traders-wheel-test
```

若 Docker 可用：

```bash
docker build -t traders-repair-test .
```

若 Docker 不可用，明確報告「未執行」，不能宣稱已通過。

---

# 12. 必做 regression scan

修改完成後搜尋：

```text
submit_order(
close_position(
cancel_order
requests.get(
requests.post(
safety_enabled
load_active_state
SafetyGuard(
```

檢查：

- 沒新增 broker mutation bypass。
- 沒新增無 timeout 的 HTTP call。
- 沒有為了修 P1 把一般 config JSON 讀取全部變 strict。
- 沒有讓 safety corruption fallback fresh state。
- 沒有 unattended path 可透過 falsey safety config 繼續。

---

# 13. 驗收標準

## P1-01 ACCEPT

只有當：

- missing active file → no active run
- existing invalid active file → hard stop
- CLI 不建立新 run
- 0 broker mutation
- valid state resume 不破壞

才通過。

## P1-02 ACCEPT

只有當：

- missing safety file → fresh init
- existing corrupt/invalid safety file → hard fail
- legacy missing keys 可安全補 default
- durable atomic write
- 0 broker mutation

才通過。

## P2-01 ACCEPT

只有當 unattended long-run 無法用 `safety_enabled=False` 啟動，且 general SafetyGuard feature 沒被粗暴刪除。

## P2-02 ACCEPT

只有當 CryptoCompare HTTP 一定有 finite timeout 且 Timeout 被正常處理。

## P2-03 ACCEPT

只有當：

- deployment/CI 使用 exact lock
- setup / requirements 不再維護互相漂移的兩份 runtime dependency lists
- wheel 可 build
- 3.11/3.12 CI 契約不被破壞

## P3 ACCEPT

QUICKSTART / repository metadata 指向正確 project，同時保留 upstream attribution。

---

# 14. 最終回報格式

完成後只用以下格式回報，不要寫長篇敘事：

```markdown
# Implementation Result

## Baseline
- Starting HEAD:
- Ending working tree / commit:
- Branch:

## Files Changed
- path — purpose

## P1
### P1-01
- Status: FIXED / NOT FIXED
- Implementation:
- Tests:

### P1-02
- Status:
- Implementation:
- Tests:

## P2
### P2-01
...
### P2-02
...
### P2-03
...

## P3
### P3-01
...
### P3-02
...

## Full Validation
- targeted tests:
- full pytest:
- compileall:
- git diff --check:
- wheel build:
- Docker build:
- Python 3.11 lock install:
- Python 3.12 lock install / CI:

## Remaining Risks
只列本任務完成後仍真實存在、且不是本提示詞明確排除的問題。

## Scope Check
- trading strategy changed: NO
- broker mutation authority changed: NO（若 YES 必須解釋，通常視為失敗）
- live trading enabled: NO
- new infrastructure introduced: NO
```

若任何 P1 無法完整修復或 full regression 失敗，不得宣稱 production-ready。
