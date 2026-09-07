# Prompt 1 — PIT-1 實作：封住歷史分析的未來資料

## 你的角色

你是本階段的實作者。請在以下專案完成 **PIT-1：歷史分析 point-in-time 完整性最小修復**：

`/Users/zongen/Downloads/codex/tradingAlpaca`

這個工作樹已有其他人的未提交修改。不得執行 `git reset`、`git checkout --`、`git clean`，不得還原或重寫與本任務無關的內容。

完成後只能回報：

`Implemented — pending independent acceptance`

不得自行宣告 Accepted。

## 硬性限制

1. 不得呼叫真實 Alpaca、LLM、OpenAI Web Search、Google News、SEC、FRED、CryptoCompare、DeFiLlama 或其他網路服務。
2. 測試只能使用 mock、fake 或 injected dependency。
3. 不建立歷史新聞資料庫、snapshot service、事件總線、ORM、migration framework 或新 daemon。
4. 不改 screening 權重、交易方向邏輯、broker order state machine、SQLite schema 或回測報酬公式。
5. 本輪只保證「日級 cutoff」：任何交給 LLM 的歷史資料不得晚於 `curr_date/trade_date`。不要嘗試建立完整 intraday point-in-time 系統。

## 要修的既有問題

### P1：Alpaca 歷史窗口抓到現在

- `tradingagents/dataflows/interface.py::get_alpaca_data_window` 沒有把 `curr_date` 傳給 `end_date`。
- 同一函式會加入現在的 latest quote。
- 這個函式由 `AgentToolkit.get_alpaca_data_report` 與 `get_stock_data_table` 暴露給 Market Analyst。

### P2：Regime 永遠使用今天

- `tradingagents/regime.py::_load_assessment` 使用 `date.today()` 和 `end=None`。
- `market_analyst.py` 呼叫 `regime_report_block` 時沒有傳 `trade_date`。

### P3：Live-only 資料被用於歷史日期

- OpenAI hosted web search、目前的 Google News RSS、CryptoCompare/CoinDesk 與 DeFiLlama 沒有可靠的歷史 publication cutoff。
- 歷史日期只寫在 prompt/query 內，不足以阻止未來資料。

### P4：歷史判斷混入現在 broker state

- Trader 與 Risk Manager 會讀取現在的 position/account，與 `trade_date` 無關。

### P5：Macro 報告包含硬編碼 2024 FOMC schedule

- 2026 年執行仍會顯示 2024 行事曆。

## 唯一允許的日期規則

請在 `tradingagents/dataflows/interface_utils.py` 新增以下兩個小型 helper。不要另建日期 framework。

```python
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

AnalysisDateMode = Literal["live", "historical"]

def parse_analysis_date(value: str) -> date:
    """Strict YYYY-MM-DD parser. Raise ValueError on blank, malformed or future dates."""

def analysis_date_mode(value: str, *, now: datetime | None = None) -> AnalysisDateMode:
    """Compare value with today's America/New_York date."""
```

精確行為：

1. `parse_analysis_date` 只接受完整 `YYYY-MM-DD`，例如 `2026-09-07`。
2. 空字串、`None`、`2026/09/07`、包含時間的字串都丟 `ValueError`。
3. `analysis_date_mode` 使用 `ZoneInfo("America/New_York")`。
4. 若 injected `now` 沒有 timezone，丟 `ValueError`。
5. analysis date 小於美東今天：回傳 `historical`。
6. analysis date 等於美東今天：回傳 `live`。
7. analysis date 大於美東今天：丟 `ValueError("analysis date is in the future")`。

所有新分支都使用這兩個 helper，不要在不同檔案重新寫 `date.today()` 比較。

## 修改 A：Alpaca 歷史 bars 與 quote

### A1. 修改 `get_alpaca_data_window`

檔案：`tradingagents/dataflows/interface.py`

保留既有函式名稱和公開參數。函式進入後：

1. `curr_date` 缺失時可沿用現在日期，但先轉成明確 `YYYY-MM-DD`。
2. 呼叫 `analysis_date_mode(curr_date)`。
3. 呼叫 `AlpacaUtils.get_stock_data` 時固定傳：

```python
data = AlpacaUtils.get_stock_data(
    symbol=symbol,
    start_date=start_date,
    end_date=curr_date,
    timeframe=timeframe,
)
```

4. 即使 provider 回傳錯誤資料，也要在函式內做第二層 cutoff：

```python
timestamps = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
data = data[timestamps.notna()].copy()
data["timestamp"] = timestamps[timestamps.notna()]
data = data[data["timestamp"].dt.date <= parsed_analysis_date]
```

5. 若沒有 `timestamp` 欄、timestamp 無法解析或過濾後為空，回傳明確 unavailable/error；不得回傳未驗證原資料。
6. report header 必須寫實際的 `start_date to curr_date`，不得再寫 `to present`。
7. 只有 `mode == "live"` 才能呼叫 `AlpacaUtils.get_latest_quote`。
8. historical 模式不得呼叫 latest quote，報告不得出現 Bid、Ask 或現在 timestamp。

### A2. 不要修改 `AlpacaUtils.get_stock_data` 的 inclusive end-date 契約

`AlpacaUtils.get_stock_data` 現在會把 date-only end 加一天傳給 vendor，以取得指定日期資料。保留此行為；真正的防線是 `get_alpaca_data_window` 回傳前再次過濾 `timestamp.date <= curr_date`。

### A3. Toolkit wrapper

確認以下 wrapper 仍走修正後的同一函式，不得另建旁路：

- `AgentToolkit.get_alpaca_data_report`
- `AgentToolkit.get_stock_data_table`

## 修改 B：Regime report 使用 as-of

檔案：

- `tradingagents/regime.py`
- `tradingagents/agents/analysts/market_analyst.py`

### B1. 精確修改 signature

把 `_load_assessment` 改成：

```python
def _load_assessment(
    symbol: str,
    price_loader: Optional[Callable] = None,
    config: Optional[RegimeConfig] = None,
    *,
    as_of: str | None = None,
) -> Optional[RegimeAssessment]:
```

把 `regime_report_block` 加上同樣 keyword-only `as_of`。

行為：

- `as_of` 有值：使用 `parse_analysis_date(as_of)`；start=`as_of - 550 calendar days`；loader 呼叫為 `loader(symbol, start, as_of)`。
- `as_of` 無值：只保留給 live execution/helper 相容使用；以美東今天作 end，不得再傳 `end=None`。
- invalid/future as_of 不得悄悄退回 today。

### B2. Market Analyst 接線

把：

```python
regime_report_block(ticker, config=...)
```

改成：

```python
regime_report_block(
    ticker,
    config=RegimeConfig.from_config(get_config() or {}),
    as_of=current_date,
)
```

`regime_risk_multiplier` 的 execution-time 呼叫維持現在語義，本輪不要重構 execution sizing。

## 修改 C：歷史模式禁用 live-only 資料

### C1. 新增固定錯誤文字

在 `interface_utils.py` 定義一個常數：

```python
HISTORICAL_SOURCE_UNAVAILABLE = (
    "UNAVAILABLE_FOR_HISTORICAL_AS_OF: source has no verified point-in-time cutoff"
)
```

所有歷史拒絕使用此 prefix，方便 coverage gate 與測試判斷。

### C2. 必須加 guard 的公開函式

在進行任何 client 建構、API key lookup 或 HTTP call 之前檢查日期：

- `get_stock_news_openai(ticker, curr_date)`
- 所有接收 `curr_date` 的 OpenAI global/macro web-search 函式
- `get_fundamentals_openai(ticker, curr_date)`
- `get_google_news(query, curr_date, ...)`

若 mode 為 historical，立即回傳：

```text
UNAVAILABLE_FOR_HISTORICAL_AS_OF: source has no verified point-in-time cutoff
```

不得建 client、讀 live endpoint 或執行 fallback 到另一個 live source。

### C3. Crypto 函式補 `curr_date`

對以下 wrapper 增加 optional `curr_date`，並一路由 analyst/toolkit 傳入 `state["trade_date"]`：

- `interface.get_coindesk_news`
- `coindesk_utils.get_news`
- `interface.get_defillama_fundamentals`
- 對應 AgentToolkit methods

精確規則：

- `curr_date` 為 historical：在 HTTP 前回傳固定 unavailable prefix。
- `curr_date` 為 live：保留現有行為。
- `curr_date` 缺失：為相容既有直接 live 使用，可視為 live；在 docstring 明寫「缺失只允許 live direct call，historical caller 必須傳值」。

不要嘗試倒推 CryptoCompare/DeFiLlama 的歷史快照。

### C4. Analyst 工具選擇

在 News、Social、Fundamentals、Macro analyst 取得 `current_date` 後先計算 mode。

- historical 模式不要把 live-only tool 加入 `tools`。
- 保留具 point-in-time 能力的來源，例如 SEC、SimFin、FRED、Alpaca historical bars、technical brief、已按日期讀取的 Finnhub cache。
- 若某 analyst 因此沒有可用來源，報告必須明示 source unavailable；不得自行填入現在資料。

本輪不要全面關閉 historical analysis。

## 修改 D：歷史模式不得讀現在 broker

檔案：

- `tradingagents/agents/trader/trader.py`
- `tradingagents/agents/managers/risk_manager.py`

不要建立 historical portfolio store。使用以下固定規則：

1. live mode：保留現有 fresh broker context。
2. historical mode：不得呼叫任何 Alpaca position/account helper。
3. historical mode 只可讀 state 已提供的：
   - `current_position`
   - `position_stats`
   - `account_status`
4. 缺少時使用字串 `UNAVAILABLE_FOR_HISTORICAL_AS_OF`，不得使用 NEUTRAL、0 position、0 cash 或現在帳戶代替。
5. Risk Manager 若缺少可信 historical position/account，不得產生 opening READY intent。輸出應為 NO_TRADE 或 WAIT/HOLD，並說明 historical portfolio context unavailable。

若現有 state 欄位名稱不同，沿用現有欄位；不要新增資料庫或大型 schema。測試 fixture 應透過正式 node state 注入歷史值。

## 修改 E：移除 2024 FOMC schedule

檔案：`tradingagents/dataflows/macro_utils.py`

在 `get_fed_calendar_and_minutes`：

- 刪除八筆硬編碼的 2024 meeting dates。
- 不替換成 2025/2026/2027 硬編碼日期。
- 保留 FRED policy-rate history。
- 插入以下等義文字：

```text
FOMC event calendar unavailable: no authoritative point-in-time calendar source is configured.
```

## 測試實作

集中新增：`tests/test_historical_asof_boundary.py`。

不要建立多個新測試檔。必要時更新既有 fixture。

至少實作以下測試：

1. `analysis_date_mode` 的 past/today/future/malformed/naive-now。
2. historical Alpaca wrapper 傳入 `end_date=curr_date`。
3. fake bars 含 cutoff 後 row，LLM-visible report 排除該 row。
4. historical latest quote call count 為 0。
5. live latest quote call count 為 1。
6. historical regime loader 收到 `start=as_of-550d`、`end=as_of`。
7. Market Analyst 傳 trade_date 給 regime。
8. historical OpenAI/Google/CryptoCompare/DeFiLlama transport call count 全為 0。
9. live crypto/news path 仍會走 injected transport。
10. historical Trader/Risk Manager broker call count 為 0。
11. 無 historical portfolio context 時不得產生 opening READY。
12. injected historical portfolio context 會出現在 prompt/context。
13. output/source 不再含 `2024 FOMC Meeting Schedule`。
14. 既有 FRED vintage、SEC acceptance-time、SimFin publish-date 測試仍通過。

## 執行驗證

依序執行：

```bash
python -m pytest tests/test_historical_asof_boundary.py -q
python -m pytest tests/test_strategy_consistency.py tests/test_regime_detection.py tests/test_phase_b_sec_ir.py tests/test_phase_b_position_context.py -q
python -m compileall tradingagents
git diff --check
```

若時間足夠再執行：

```bash
python -m pytest tests/ -q
```

所有測試必須 offline。若任何測試意外準備呼叫外部服務，先修正 mock，不得放行網路。

## 完成回報

回報必須包含：

1. `Implemented — pending independent acceptance`
2. 修改檔案清單。
3. A–E 每項實際行為差異。
4. 測試命令、passed/failed 數量。
5. 外部 call=0、broker mutation=0。
6. 明確限制：只有日級 cutoff、live-only 歷史來源被拒絕、尚未證明策略收益。

