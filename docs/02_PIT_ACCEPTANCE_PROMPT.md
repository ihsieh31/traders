# Prompt 2 — PIT-1 獨立驗收：歷史資料不得越過 cutoff

## 你的角色

你是 fresh read-only 獨立驗收者，驗收專案：

`/Users/zongen/Downloads/codex/tradingAlpaca`

驗收對象：`PIT-1：歷史分析 point-in-time 完整性`

## 禁止事項

1. 不得修改、格式化、修補、建立或刪除任何專案檔案。
2. 不得執行 `git reset`、`git checkout --`、`git clean`。
3. 發現問題時不得順手修復，只能判定 Not Accepted。
4. 不得呼叫真實 LLM、Alpaca、新聞、SEC、FRED、DeFiLlama、CryptoCompare 或其他外部服務。
5. 可在 `/tmp` 建立臨時 PoC，但驗收結束前自行清理；不得寫入 repository。

## 驗收前完整性記錄

執行並保存輸出：

```bash
git status --short
git diff --stat
git diff --check
```

再計算所有 tracked modified files 與 untracked source files 的 hash。驗收後重算，必須一致。不要因工作樹原本不乾淨而判失敗；本項只驗證驗收者沒有造成新 mutation。

## 驗收方法

不能只看實作者新增的 unit test。每個 Critical Gate 至少需要：

- source 靜態檢查；以及
- 從 public/production wrapper 進入的 injected fake PoC。

不得把 private helper 綠燈當成完整接線證據。

## 驗收矩陣

### PIT01 — 共用日期規則

檢查 `interface_utils.py`：

- 只有一份共用的 strict date parser/mode helper。
- 使用 `America/New_York`。
- 支援 timezone-aware `now` 注入。
- past=`historical`、ET today=`live`。
- future、blank、錯誤格式、naive `now` 均 fail closed。

若各模組各自寫 `date.today()` 判斷，PIT01 Fail。

### PIT02 — Alpaca public wrapper cutoff

從 `AgentToolkit.get_alpaca_data_report` 或它實際暴露的 public tool 進入，注入 fake `AlpacaUtils.get_stock_data`。

固定案例：

- curr_date=`2025-01-10`
- fake rows：`2025-01-09`、`2025-01-10`、`2025-01-13`

必要結果：

- fake loader 收到 `end_date="2025-01-10"`。
- report 包含 01-09、01-10。
- report 不含 01-13。
- report header 不得寫 `to present`。

### PIT03 — Historical quote 為零呼叫

在 PIT02 同一 production path 注入 `get_latest_quote` mock。

- historical：call count=0，報告無 Bid/Ask/latest timestamp。
- ET today：call count=1，可顯示 injected quote。

若 historical 先取 quote 再丟棄，仍為 Fail。

### PIT04 — Technical brief 無回歸

檢查 1h/4h/1d `compute_indicators`：

- loader end date 等於 curr_date。
- 回傳資料不得晚於 curr_date。
- SMA/RSI/ATR 等既有計算沒有被換掉。

本 Gate 不要求 intraday decision timestamp；不要把未實作完整 intraday 視為本輪失敗。

### PIT05 — Historical regime

注入記錄參數的 price loader：

- `regime_report_block(..., as_of="2025-01-10")` 的 end 必須是 `2025-01-10`。
- start 必須是該日期往前 550 calendar days，不是驗收日往前 550 日。
- Market Analyst 必須把 `state["trade_date"]` 傳入 `regime_report_block`。
- historical report path 不得出現 `date.today()` 或 `end=None` 等效旁路。
- execution-time `regime_risk_multiplier` 原有 live 使用仍可執行。

### PIT06 — Live-only Web/News/Crypto

逐一從公開函式或 AgentToolkit wrapper 驗證：

- OpenAI stock news web search
- OpenAI global/macro web search
- OpenAI fundamentals web search
- Google News
- CryptoCompare/CoinDesk
- DeFiLlama

對 historical date：

- transport/client constructor/API call count=0。
- 回傳以 `UNAVAILABLE_FOR_HISTORICAL_AS_OF` 開頭或完全等義的固定錯誤。
- 不得 fallback 到另一個 live source。

對 ET today：

- injected fake transport 仍可被呼叫。
- 不需要也不得進行真實網路請求。

### PIT07 — Historical broker context

分別驗證 Trader node 與 Risk Manager node：

案例 A：historical state 沒有 position/account snapshot。

- current broker position/account helper call count=0。
- prompt/context 必須標示 unavailable。
- 不得猜 NEUTRAL、0 cash、0 exposure。
- 不得產生 opening READY TradeIntent。

案例 B：historical state 含 injected position/account。

- 只使用 injected state。
- broker call count 仍為 0。

案例 C：live state。

- 原本 fresh broker context 接線仍存在。

### PIT08 — 正確的歷史資料源仍可用

執行既有或臨時 PoC，證明：

- FRED `realtime_start`、`realtime_end` 等於 analysis date。
- SEC filing acceptance time 晚於 as_of 時被排除。
- SimFin Publish Date 晚於 curr_date 時被排除。
- Alpaca historical bars/technical brief 沒有被全面禁用。

若實作者為求簡單而全面關閉 historical analysis，PIT08 Fail。

### PIT09 — FOMC 靜態日期

檢查 source 及 fake output：

- 不含 `2024 FOMC Meeting Schedule`。
- 不含另一年度的替代硬編碼會議表。
- 無權威來源時明示 calendar unavailable。
- FRED rate history 保留。

### PIT10 — 全專案旁路搜尋

對 `tradingagents/dataflows`、`tradingagents/agents`、`tradingagents/regime.py` 搜尋：

```text
date.today
datetime.now
end=None
get_latest_quote
web_search
requests.get
current_position
get_account
```

逐項分類。只接受：

1. 明確 live-only；
2. 有 strict as-of cutoff；
3. 與分析資料無關。

任何 analyst-exposed historical path 可越過 cutoff，PIT10 Fail。

### PIT11 — 離線回歸

執行：

```bash
python -m pytest tests/test_historical_asof_boundary.py -q
python -m pytest tests/test_strategy_consistency.py tests/test_regime_detection.py tests/test_phase_b_sec_ir.py tests/test_phase_b_position_context.py -q
python -m pytest tests/ -q
python -m compileall tradingagents
git diff --check
```

如果測試會嘗試真實網路或 broker mutation，立即停止並判 Not Accepted；不得批准外部連線。

## 判定規則

以下任一項失敗，直接：

`Not Accepted — point-in-time defect`

- PIT02：價格越過 cutoff。
- PIT03：historical latest quote 被呼叫。
- PIT05：historical regime 使用 today。
- PIT06：historical live-only source 有 transport call。
- PIT07：historical node 讀現在 broker 或猜測 NEUTRAL。
- PIT10：發現 public bypass。
- PIT11：完整離線 suite 失敗或發生外部 call。

只有 PIT01–PIT11 全部通過、無 source mutation、無 public bypass，才可判定：

`Accepted — PIT-1`

## 最終報告格式

1. Verdict。
2. PIT01–PIT11 表格：Pass/Fail、證據、檔案與行號。
3. 所有測試命令與 passed/failed 數量。
4. fake transport、broker GET/POST/mutation 計數。
5. 驗收前後 source hash 是否一致。
6. 限制：僅日級 cutoff；尚未驗證真實 vendor 品質、完整 intraday point-in-time 或策略收益。
7. 若失敗，只列最小 remediation，不得修改檔案。

