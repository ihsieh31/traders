# Traders：安全可靠執行的多代理 Alpaca Paper 交易框架

> **Traders**（本機目錄 `tradingAlpaca`，公開 repo [ihsieh31/traders](https://github.com/ihsieh31/traders)）是一個以 LLM 多代理進行市場分析、並以「安全可靠的自動執行」為核心目標的交易框架。本專案保留 [AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent) 的研究與策略系統，重做了最後一哩路：`TradeIntent → Alpaca Paper → broker 真實狀態`。
>
> **Paper-only**：交易客户端被寫死鎖定在 Alpaca Paper API（`paper=True`），整個程式不存在任何 live 下單路徑；設定 `ALPACA_USE_PAPER=False` 或指到非 paper endpoint 一律啟動失敗、零 broker 呼叫。
>
> **免責聲明**：本專案僅供教育與研究用途，不構成任何金融、投資或交易建議。交易有風險，任何交易決策請自行審慎評估。

快速上手請看 [QUICKSTART.md](QUICKSTART.md)；管線內部細節請看 [ARCHITECTURE.md](ARCHITECTURE.md)；目標、安全規則與完整驗收紀錄請看 [PROJECT_GOALS_AND_STATUS.md](PROJECT_GOALS_AND_STATUS.md)。

---

## 一、專案內容

### 1.1 它是什麼

一支由 LangGraph 驅動的多代理交易系統：五個分析師 + 多空辯論 + 交易員 + 風險團隊共同產出結構化交易決策，再經過一道嚴格的執行層，把決策變成 Alpaca Paper 帳戶上的真實訂單。設計原則是 **fail-closed（出錯即停止，絕不猜測、絕不盲目重送）**——遇到重啟、重複 callback、部分成交、broker timeout、資料過期或本機與 broker 狀態不一致時，系統停止新增風險，而不是繼續交易。

### 1.2 多代理分析流程

```
WebUI / CLI
  → Market / Social / News / Fundamentals / Macro 五分析師（可平行執行）
  → Bull / Bear 研究員辯論 → Research Manager
  → Trader → Risky / Safe / Neutral 風險辯論 → Risk Manager
  → 結構化 TradeIntent（BUY/HOLD/SELL 或 LONG/NEUTRAL/SHORT）
  → 倉位 sizing + 安全護欄
  → 單一執行入口 → Alpaca Paper 下單
```

- **資產支援**：美股與加密貨幣（加密貨幣使用 `BTC/USD`、`ETH/USD` 斜線格式），可混合輸入如 `NVDA, ETH/USD, AAPL`。
- **LLM 多提供者**：OpenAI、本地 OpenAI 相容端點（LM Studio / Ollama / vLLM）、Google Gemini、Anthropic Claude、xAI、MiniMax、DeepSeek、Qwen、GLM、OpenRouter、Azure OpenAI；保留 GPT reasoning 控制、Gemini thinking level、Claude effort 等提供者專屬參數。
- **記憶與反思**：完成的決策寫入 markdown 記憶檔，之後以已實現報酬回結；失敗的 LangGraph run 可用 SQLite checkpoint 斷點續跑。
- **周邊完整**：WebUI 儀表板（多標的進度表、互動圖表、聊天式辯論、持倉管理）、CLI、回測、每日報告、Telegram/webhook 告警、chaos 測試與 CI。

### 1.3 執行與風控架構（本專案的核心重做）

所有下單都必須經過**單一執行入口** `ExecutionService.execute`，其他模組不得直接呼叫 broker API。執行層包含：

- **兩層冪等**：分析層 `decision_id` 唯一（同一份分析只會建立一次執行 intent）＋ broker 層 deterministic `client_order_id` 唯一（重啟後沿用原 ID，不產生新邏輯訂單）。
- **Durable outbox**：stdlib SQLite 三張表（`execution_intents` / `orders` / `fills`），先在同一個 transaction 內 commit intent 與 PENDING 訂單、commit 成功後才送單。三個 crash 點（commit 前／commit 後送單前／送單後回寫前）都有明確恢復語義，最後一種以 `client_order_id` 向 broker 查找後 adopt。
- **訂單狀態機**：`PENDING → SUBMITTING → ACCEPTED → PARTIAL → FILLED`，含 `UNKNOWN`（timeout 下不明結果）的 bounded lookup 恢復；`UNKNOWN` 未解決前帳戶保持 `PAUSED`。
- **固定 broker retry 政策**：唯讀 GET 最多 3 次短退避後 fail-closed；POST 被拒不重試；POST timeout 先標 `UNKNOWN` 再查詢，不直接重送。
- **BrokerSnapshot 唯一權威**：account、positions、orders、fills、cash 一律以即時快照為準，本機 ledger、memory、checkpoint 都不能覆寫 broker 事實。只有 reconciliation `CLEAN` 才能新增風險；持倉不符、重複 ID、未解決部分成交等一律 `PAUSED`。
- **新鮮度 gate 與單一執行鎖**：account/position/order/quote 有 TTL；以 Alpaca account ID 為 key 的 OS 層執行鎖，第二個 process 拿不到鎖立即退出。
- **降風險平倉例外**：已驗證的減持 exit 在 broker 即時確認持倉後仍可執行。

### 1.4 LLM 固定角色與有界重試

- **Analysis / Decision 兩角色分離**：設定任一 `analysis_*` / `decision_*` key 後，Analysis provider/model 服務所有研究節點（五分析師、多空、research manager、trader、風險辯論），Decision 只服務 Risk Manager。每個角色獨立解析 provider/model/endpoint/credential（如 `DECISION_OPENAI_API_KEY`），跨 provider 缺 model 為啟動期錯誤，機密不進 UI store 與 log。
- **有界重試**：`llm_max_retries`（0–3）＝每個邏輯呼叫最多 N+1 次請求；暫時性錯誤（timeout/連線/429/5xx）封頂退避重試，永久性錯誤（401/403）立即停。provider 存取失敗整輪標記 `STOPPED` 並停止自動排程，絕不偽裝成正常 `NO_TRADE`。
- **選配 Analysis failover**：設定 `analysis_fallback_provider/model` 後，Primary 暫時性失敗可在共享重試預算內轉試 Fallback（不回彈、永久錯誤不 failover），切換記錄無機密 audit 事件。

### 1.5 全市場自動選股（Screening → Top20）

開啟 `auto_screening_enabled` 後，每個美股交易日：

```
Alpaca 全量 ACTIVE tradable US_EQUITY（分頁，無 fallback 名單）
  → 61 根完整日 K 驗證（NY 交易日曆、無前補、單一 adjustment 政策）
  → 確定性門檻：close ≥ $5、20 日均額 ≥ $20M
  → 跨截面百分位公式（adv20/r20/r60/反波動/volume_ratio）→ Top40
  → 獨立 Screening LLM 角色 → 嚴格 schema 驗證的 Top20
  → Top20 ∪ 現有持股（每輪重取）→ 深度分析
  → 只有當日已驗證 Top20 成員可開新倉（entry gate 內嵌於執行入口）
```

- Screening 角色只看 compact 因子表（看不到持倉、現金與新聞），必須回傳恰 20 筆、rank 1–20 連續唯一、輸出成員、有限 0–100 分數、基於因子的理由；任何不合法輸出整輪停止，零修補請求、零下游。
- 每日選股結果存於小型 JSON cache（原子寫入、flock 防雙執行者、SHA-256 自雜湊完整性密封）；隔日/損毀/未來時間戳/設定變更的 cache 視同不存在。人工 refresh 失敗先刪 cache、絕不回退舊名單。
- 交易日曆以 Alpaca 官方 `get_calendar` 為權威；bars 固定 SIP feed、無 IEX fallback。

### 1.6 30 天無人值守 Paper 觀察（Phase D）

單一指令：

```bash
python -m cli.main long-run
```

- 30 個日曆天、僅美股交易日（權威 Alpaca 日曆；early close 於收盤前 30 分鐘執行）；首次執行互動補齊設定、唯讀 preflight，並要求一次明確的 Paper-test 授權才進入 `RUNNING`。
- Crash/重啟後重跑同一指令即恢復原觀察窗口，不重複下單、每個 session 至多執行一次；process 離線期間錯過的 session 記為 `MISSED_PROCESS_DOWN`，絕不以過期分析或補單回填。
- 硬性安全/provider 失敗會停止觀察並產出部分報告（最終報告：`~/.tradingagents/long_run/runs/<run_id>/final_report.{md,json}`）。

### 1.7 目錄導覽

```
tradingagents/
  agents/            分析師、研究員、trader、risk 節點與結構化 schema
  dataflows/         Alpaca/Finnhub/FRED/Reddit/SEC-IR/加密貨幣等資料源、市場日曆
  graph/             LangGraph trading graph
  llm_clients/       多提供者客戶端、固定角色（roles）、有界重試（retry）
  execution/         單一執行入口、durable outbox、broker authority、auto-trade 準備
  screening/         全市場 universe、指標公式、Top20 LLM 契約、cache、entry gate
  risk/              exposure headroom、corporate-action quarantine
  portfolio/         相關性、regime、倉位上限
  long_run.py        Phase D 30 天觀察編排
  prompts/templates/ 模型提示詞（可外部覆寫 TRADINGAGENTS_PROMPT_DIR）
webui/               Dash WebUI；cli/ 互動式 CLI
tests/               離線確定性測試套件（無網路、無真實金鑰）
```

---

## 二、當前成果

開發採 **Gate 制 + 每次 fresh read-only 獨立驗收**（驗收不得修檔；修復後重新 fresh acceptance），每階段都有離線確定性測試、對抗 PoC 與真實 Paper E2E 證據分開報告。

| 階段 | 內容 | 狀態 |
|---|---|---|
| Phase A（P1） | 安全可靠的 Paper 執行：paper-only hard lock、strict TradeIntent、兩層冪等、durable outbox、訂單狀態機與 UNKNOWN 恢復、BrokerSnapshot 權威、三段 reconciliation、新鮮度 gate、單一執行鎖 | **Accepted**（2026-09-04） |
| Phase B（P2） | 策略與資料品質：SEC filing/公司 IR 第一手來源、corporate-action 隔離（quarantine）、sector 曝險上限、Analysis/Decision 雙角色、LLM 有界重試與 run-stop、Trader/Risk Manager 的 fresh broker 持股 context、exposure headroom 裁切 | **Accepted**（2026-09-05，B01–B24 全 Pass） |
| Phase C（P3） | 全市場自動選股：ACTIVE US_EQUITY 全量 universe、確定性 Top40、獨立 Screening 角色與嚴格 Top20 契約、Top20 ∪ 持股深度分析、每日 selection cache（含完整性密封）、fail-closed entry gate、權威交易日曆與 SIP feed | **Accepted**（2026-09-05，C01–C23 全 Pass；F1、M1 修復後複驗通過） |
| Phase D | 30 天無人值守 Paper 觀察編排（`long-run`） | 已實作、離線測試全綠（尚未列入已驗收紀錄） |

**執行驗證證據**：Phase A.2 曾於一次性真實 Alpaca Paper 帳戶完成完整 E2E——paper endpoint 驗證 → durable commit → 真實送單 → broker 成交 → 本地 adopt 同一 broker_order_id → reconcile `CLEAN` → 已驗證平倉 → 帳戶 flat、零 open orders。

**離線測試現況**（2026-09-06，含工作樹中 Phase D 變更）：

```bash
python -m pytest tests/
# 645 passed, 177 subtests passed
```

套件完全離線確定性（無網路、無真實金鑰），乾淨 clone 即可通過。

**尚未完成 / 已知限制**：

- 長期 Paper observation（小 notional）尚未開始；啟用長期無人值守 Paper 自動交易前必須完成，且需使用者另行明確授權——已驗收的 build 不會自行啟用交易。
- 離線驗收只證明 mock 鏈路與 fail-closed 語義；真實 Alpaca universe/bars 資料品質、真實 Screening vendor 輸出品質尚未驗證。
- 少量非阻擋維修項（死設定鍵 `llm_retry_backoff_max_seconds`、`ScreeningDeps.llm_factory` 死欄位等）留待一般維修。
- Top40 權重是 research baseline，不是 validated alpha；尚未有任何統計驗證的前瞻收益證據。
- 30 天無人值守 observation 屬於 operational observation（帳戶權益變化），不是 profitability proof：未調整入出金、可能含觀察期前既有部位、非純策略歸因，且未計入全部研究與交易成本。
- Broker `last_equity` 作為 daily-loss baseline 仍可能受入出金影響；本版本未實作 cash-flow adjusted TWR。

---

## 三、安裝與配置

### 3.1 環境需求

- Python **≥ 3.10**（建議 3.12；本專案以 3.12 venv 驗證）
- 一組免費的 [Alpaca](https://alpaca.markets) Paper API 金鑰
- 一個 LLM 提供者的 API 金鑰

### 3.2 安裝

```bash
git clone https://github.com/ihsieh31/traders.git
cd traders
python -m venv .venv
# Windows: .venv\Scripts\activate   macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

### 3.3 配置 API 金鑰

```bash
cp env.sample .env   # Windows: copy env.sample .env
```

編輯 `.env`。**最低可執行組合**只有兩項：

| 金鑰 | 取得處 | 必要 |
|---|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | [alpaca.markets](https://alpaca.markets) 免費 Paper 帳戶（用 Paper key，不是 live key） | ✅ |
| `OPENAI_API_KEY`（或改 `LLM_PROVIDER` 用其他提供者） | [platform.openai.com](https://platform.openai.com) | ✅ |
| `FINNHUB_API_KEY` | [finnhub.io](https://finnhub.io) — 股票新聞、內部人交易、財報日曆 | 選配 |
| `FRED_API_KEY` | [FRED](https://fred.stlouisfed.org/docs/api/api_key.html) — 巨觀分析師 | 選配 |
| `COINDESK_API_KEY` | [CryptoCompare](https://www.cryptocompare.com/cryptopian/api-keys) — 加密貨幣新聞 | 選配 |
| `ALPHA_VANTAGE_API_KEY` | [Alpha Vantage](https://www.alphavantage.co/support/#api-key) — 選配 fallback 行情源 | 選配 |
| `SEC_IR_USER_AGENT` | 填入姓名與聯絡方式 — SEC 第一手資料來源的存取政策要求 | 啟用基本面 SEC 來源時填 |

> **Paper-only**：保持 `ALPACA_USE_PAPER=True`。設為 `False` 或指到 live endpoint 會 fail-closed、零 broker 呼叫，系統不存在 live 下單路徑。

**LLM 提供者**：在 `.env` 設 `LLM_PROVIDER`（支援 `openai`、`local_openai`、`google`、`anthropic`、`xai`、`minimax`、`deepseek`、`qwen`、`glm`、`openrouter`、`ollama`、`azure`），並填對應金鑰（`GOOGLE_API_KEY`、`ANTHROPIC_API_KEY`、`DEEPSEEK_API_KEY`、`ZHIPU_API_KEY` 等，完整清單見 `env.sample`）。本地端點用 `OPENAI_USE_LOCAL=true` + `OPENAI_BASE_URL`（如 LM Studio `http://localhost:1234/v1`）。

**角色與自動選股**：`analysis_*` / `decision_*` 與 screening 開關（`screening_provider` / `screening_model` / `screening_backend_url`）是 **config key，在 WebUI 或 CLI 設定**，環境變數只放角色專屬金鑰覆寫（如 `ANALYSIS_OPENAI_API_KEY`、`DECISION_OPENAI_API_KEY`、`SCREENING_<PROVIDER>_API_KEY`）。LLM 重試次數用 `LLM_MAX_RETRIES`（0–3）。

**執行緒與路徑**（皆選配）：`TRADINGAGENTS_RESULTS_DIR`（報告輸出，預設 `eval_results/`）、`TRADINGAGENTS_CACHE_DIR`、`TRADINGAGENTS_MEMORY_LOG_PATH`（決策記憶檔）、`TRADINGAGENTS_PROMPT_DIR`（外部提示詞覆寫）。

### 3.4 執行

**WebUI**（預設 `http://127.0.0.1:7860`，埠被占用會自動往後找）：

```bash
python run_webui_dash.py
# 常用選項：--port / --share / --server-name / --debug / --max-threads
```

開啟頁面後：輸入標的（`NVDA, AAPL`、`BTC/USD` 或混合）→ 選擇 LLM 提供者/模型與研究深度 → 按 **Analyze** 觀看五分析師、多空辯論與風險團隊即時串流報告 → 手動執行建議，或開啟自動執行與定期排程分析。

**CLI**：

```bash
python -m cli.main              # 互動式單次分析
python -m cli.main long-run     # Phase D：30 天無人值守 Paper 觀察
```

**Docker**：

```bash
cp env.sample .env   # 先填好提供者、行情與 Alpaca 憑證
docker compose up -d --build   # 指定埠：HOST_PORT=7861 docker compose up -d --build
```

### 3.5 驗證安裝與結果位置

```bash
python -m pytest tests/   # 645 passed, 177 subtests passed（離線、無網路）
```

| 產物 | 位置 |
|---|---|
| 報告與完整 audit trail（每個 prompt、tool call、LLM token 用量、最終狀態） | `eval_results/<symbol>/TradingAgentsStrategy_logs/runs/` |
| 決策記憶檔（每筆最終決策，之後以已實現報酬回結） | `~/.tradingagents/memory/trading_memory.md` |
| 30 天觀察設定與最終報告 | `~/.tradingagents/long_run/`（`final_report.{md,json}`） |

### 3.6 Python API

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# 單一股票
_, decision = ta.propagate("NVDA", "2024-05-10")
print(decision)

# 加密貨幣與混合標的
for symbol in ["NVDA", "ETH/USD", "AAPL"]:
    _, decision = ta.propagate(symbol, "2024-05-10")
    print(f"{symbol}: {decision}")
```

常用 config：`deep_think_llm` / `quick_think_llm`（模型）、`max_debate_rounds`（辯論輪數）、`online_tools`（即時資料）、`allow_shorts`（做空模式）、`parallel_analyst` 系列延遲（防 API 超載）、`checkpoint_enabled`（失敗 run 斷點續跑）。

### 3.7 常見問題

| 症狀 | 解法 |
|---|---|
| `Alpaca API key or secret not found` | `.env` 未載入或金鑰為空——重查 3.3 步驟。 |
| Alpaca 回 `unauthorized` | Paper 金鑰過期——重新產生 Paper 金鑰（不支援 live 金鑰）。 |
| 分析卡在某個分析師 | 通常是 rate limit；降低研究深度或加大 analyst 啟動延遲。 |
| 加密貨幣標的找不到 | 使用斜線格式 `BTC/USD`，不是 `BTCUSD`。 |

### 3.8 Paper 執行狀態與安全恢復

WebUI 的 Alpaca 帳戶狀態顯示最近一次持久化執行狀態：`CLEAN` 才允許新增曝險；`PAUSED` 表示不會送出新單，常見原因包括 broker 快照過期/損毀、持倉不符、未知或重複的訂單識別、未解決的部分成交，或另一個 process 持有執行鎖。

恢復方式：停止重複的 app process → 到 Alpaca 確認 Paper 帳戶與訂單 → 重新啟動自動交易。啟動時會沿用持久化的 `client_order_id` 重新 reconcile；**不要**刪除 SQLite 資料列或手改狀態來強迫 `CLEAN`。快照 TTL 預設 30 秒、報價 TTL 15 秒，可用 `TRADINGAGENTS_SNAPSHOT_TTL_SECONDS` / `TRADINGAGENTS_QUOTE_TTL_SECONDS` 覆寫。

---

## 上游專案

本專案為獨立強化版本，源於以下兩個上游專案，感謝原作者的開創性工作：

- **[TradingAgents](https://github.com/TauricResearch/TradingAgents)**（Tauric Research）——多代理 LLM 金融交易框架的原始出處，本專案的代理架構（分析師 / 研究員 / 交易員 / 風險管理）承襲自此。
- **[AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent)**（huygiatrng，本機目錄名 `tradingAlpaca` 的由來）——本專案的直接 fork 上游（fork 點 `8d9d770`），在其 Alpaca 整合、多資產支援與 WebUI 基礎上重做執行層。

若需引用原始 TradingAgents 研究：

```bibtex
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework},
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138},
}
```
