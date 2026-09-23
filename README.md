# Traders

> 以多代理研究、可稽核決策與 **Alpaca Paper-only** 執行組成的交易研究框架。它用來研究與驗證策略流程，**不是投資建議，也不支援實盤交易**。

## 這個專案在做什麼？

Traders 把「取得市場資訊 → 多角度研究 → 辯論 → 風險決策 → 模擬下單 → 稽核」放進同一個 Python 專案。它的目標不是讓 LLM 直接決定交易，而是讓 LLM 的研究結論必須通過結構化、可重播、具安全限制的執行邊界。

目前提供三種工作模式：

| 模式 | 用途 | 執行入口 |
| --- | --- | --- |
| 單次分析 | 互動式研究單一標的並產出決策報告 | `python -m cli.main analyze` |
| Paper observation（single） | 以每日排程執行單一策略的觀察流程（預設 30 日曆天，可任意天數或 `--continuous`），可中斷後恢復，可選 `--backend traders\|berkshire` | `python -m cli.main long-run` |
| Traders × Berkshire A/B | 用相同凍結證據比較兩種分析團隊：統一入口 unattended campaign，或單一 pair / 可續跑 campaign | `python -m cli.main long-run --mode ab`、`scripts/run_analysis_ab.py`、`scripts/run_analysis_ab_campaign.py` |

所有券商寫入都被限制為 Alpaca Paper API。偵測到 live endpoint、`TRADINGBUFFETT_ALPACA_USE_PAPER=False` 或無法證明為 Paper 的設定時，程式應直接停止，而不是退回到實盤。

## 它如何運作？

```text
市場資料、新聞、社群、財報、總經
              │
              ▼
Market / Social / News / Fundamentals / Macro analysts
              │  （平行研究）
              ▼
       Bull / Bear researchers → Research Manager
              │
              ▼
           Trader → Risky / Safe / Neutral debate
              │
              ▼
       Risk Manager → 結構化 TradeIntent
              │
              ▼
ExecutionService：持久化 outbox、券商快照、對帳與安全閘門
              │
              ▼
              Alpaca Paper
```

### 研究與決策層

- 五位分析師各自負責市場技術面、社群情緒、新聞、基本面與總經；再由 Bull/Bear 研究員辯論，Research Manager 彙整。
- Trader 提出行動方案；Risky、Safe、Neutral 三方再審視風險；Risk Manager 最後輸出 typed `TradeIntent`。
- 支援 OpenAI、OpenAI-compatible local endpoint、Google、Anthropic、xAI、MiniMax、DeepSeek、Qwen、GLM、OpenRouter、Ollama 與 Azure。可把 Analysis、Decision、Screening 拆成固定角色與不同模型。
- 記憶、反思與記憶維護可跨執行保存；prompt 可用 `TRADINGBUFFETT_PROMPT_DIR` 覆寫。

### 資料與選股層

- 資料來源包含 Alpaca、Finnhub、Google News、Reddit、FRED、SEC/IR，以及加密資產相關來源；部分情境可啟用 yfinance fallback。
- 手動模式以選定 watchlist 分析；啟用 `auto_screening_enabled` 時，會從可交易美股 universe 篩出當日 Top20。
- 篩選流程先以完整交易日、價格、成交額、報酬、波動、量能與市值作確定性過濾，再讓獨立 Screening role 對 Top40 排名選出嚴格驗證的 Top20。市值門檻預設 US$300M（`screening_min_market_cap_usd`）；缺少、無效或低於門檻的市值會 fail closed。
- Alpaca asset universe 不提供可靠市值欄位；在接上現有可靠 metadata provider 前，缺少市值的標的會被排除，可能使 auto-screening 無法湊足 Top20。不得關閉市值 gate 或以成交額代替市值。
- 當日選股快取會記錄日期、設定指紋與 SHA-256 完整性封印；過期、損毀或不合法時不沿用舊結果。

### 執行與安全層

`TradeIntent` 不是訂單。只有通過所有 deterministic 檢查後，才可能變成 Paper order：

- 唯一 `ExecutionService.execute` 入口與 strict schema；無效或模糊的模型輸出不會從文字猜測成訂單。
- SQLite durable outbox：先寫入 intent/order 並 commit，才可呼叫券商。
- `decision_id` 與 `client_order_id` 雙重冪等；timeout 或 5xx 會進入 `UNKNOWN`，先查詢/認領，不盲目重送。
- Alpaca `BrokerSnapshot` 是帳戶、持倉、訂單、成交與現金的權威；只有 reconciliation 為 `CLEAN` 才能增加曝險。
- 送單前重新檢查市場時鐘、帳戶、持倉、報價、entry policy 與部位大小；帳戶層 OS lock 防止兩個程序同時操作。
- kill switch、每日虧損、drawdown、連續拒單、單筆名目金額、集中度、部位/產業曝險與 LLM token budget 都由非 LLM 的 guardrail 管理。
- SHORT 必須逐次明確 opt-in，crypto 永不允許 SHORT；保護性的停損/停利可使用 broker-side bracket/OTO order。

## 快速開始

需求：Python 3.10+；建議 Python 3.11 或 3.12。以下範例以 macOS/Linux 為例。

1. 安裝專案與相依套件。

   ```bash
   git clone https://github.com/ihsieh31/traders.git
   cd traders
   python -m venv .venv
   source .venv/bin/activate
   pip install --no-deps -r requirements.lock
   pip check
   ```

   Windows 啟用虛擬環境：

   ```powershell
   .venv\Scripts\activate
   ```

   `requirements.lock` 是 CI/Docker 使用的完整鎖定依賴；若只進行開發，也可用 `pip install -r requirements.txt`。

2. 建立本機設定檔並填入最小必要金鑰。

   ```bash
   cp env.sample .env
   ```

   ```dotenv
   TRADINGBUFFETT_ALPACA_API_KEY=your_paper_key
   TRADINGBUFFETT_ALPACA_SECRET_KEY=your_paper_secret
   TRADINGBUFFETT_ALPACA_USE_PAPER=True
   TRADINGBUFFETT_ALPACA_READ_ONLY=True

   TRADINGBUFFETT_LLM_PROVIDER=openai
   TRADINGBUFFETT_OPENAI_API_KEY=your_openai_key
   ```

   `.env` 的金鑰一律使用 `TRADINGBUFFETT_` 前綴。保留 `ALPACA_READ_ONLY=True` 可做分析與唯讀 probe；只有在明確授權的 Paper observation 或 A/B Paper 執行前，才將它設為 `False`。

3. 確認可用指令，然後做互動式單次分析。

   ```bash
   python -m cli.main --help
   python -m cli.main analyze
   ```

   `analyze` 會在終端機詢問標的、模型、研究深度與策略設定，並輸出研究報告。第一次執行前，先閱讀 [Quick Start](QUICKSTART.md) 和 [本機 LLM 設定](LOCAL_LLM_GUIDE.md) 可減少設定時間。

## 常見操作

### 單策略 Paper observation（`long-run` single 模式）

```bash
python -m cli.main long-run                        # 互動設定 + 預設 30 日曆天
python -m cli.main long-run --duration-days 90     # 任意正整數日曆天，不再有 30 天上限
python -m cli.main long-run --continuous           # 每跑完一個 duration chunk 就延伸同一 observation，永不自動 finalize
python -m cli.main long-run --backend berkshire    # 單臂分析 backend：traders（預設）或 berkshire
```

第一次啟動會收集非機密的模型、分析師、每日 ET 執行時間、名目金額等設定，並把 secrets 留在 `.env`、把 observation 設定留在 `~/.tradingbuffett/long_run/config.json`。啟動前會進行唯讀 preflight；在取得明確 Paper 授權前不會執行 broker mutation。

同一指令會：

1. 建立指定日曆天數（預設 30）的 observation（只在美股交易日執行）；`--continuous` 時，每完成一個 chunk 就由權威 Alpaca 日曆再凍結延伸相同天數的 session，run identity 與已完成前綴不變。延伸與初始 window 採相同半開區間語義 `[start, end)`：正好等於新 `ends_at` 的交易日屬於下一個 chunk，本 chunk 不會取得一個永遠排不到的 session 而被誤標 MISSED。
2. 為每一日保存 round journal、帳戶快照、事件與報告。
3. 遭遇 crash 時使用同一 observation state 安全恢復，而不是建立新 run。
4. 遇到 provider、安全或對帳無法證明正確的情況時停止，不會靜默繼續。
5. 結束後輸出 `final_report.md` 與 `final_report.json`。

此模式不是背景服務，不會安裝 OS autostart；需要保留程序運行，或在中斷後重新執行同一指令來恢復。最後報告是觀察報告，不代表自動清倉或獲利證明。

若既有 `active.json` 的狀態是 `COMPLETED` 或 `STOPPED`，runner 會拒絕開始新 run。這可能表示 finalization 中斷；先依 `run_id` 檢查 state 與 final report，確認後才由 operator 手動清理 `active.json`。程式不會自動刪除 terminal state。

`--backend berkshire` 時，所有會被寫入的執行期路徑都隔離到 `~/.tradingbuffett/single/berkshire/`（`results/`、`memory/trading_memory.md`、`memory/agent_memory/`、`data_cache/`、`screening_selection.json`、`execution/execution.sqlite3`、`execution/recovery_ledger.sqlite3`、`safety/state.json`、`safety/KILL_SWITCH`）；不會 copy、symlink 或 fallback 到 Traders 的 memory / safety 狀態。Traders 則沿用既有共享路徑完全不變。

既有 active observation 的 config 是權威：resume 時可以重述相同值，但只要 `--backend`、`--continuous`、`--duration-days` 與 persisted state 不同，就會 fail closed（exit 2，不寫 state、不 bump restart_count、不做 recovery）。backend 與生命週期（finite/continuous、duration 天數）只能在新建 observation 時決定。

### Traders × Berkshire A/B

```bash
# shadow：不送出 broker order
python scripts/run_analysis_ab.py \
  --symbol NVDA \
  --date 2026-09-21 \
  --results-root ~/.tradingbuffett/results/ab

# 匯總既有結果
python scripts/summarize_analysis_ab.py \
  --root ~/.tradingbuffett/results/ab
```

此實驗只應改變 `analysis_backend`：Traders 使用原生五 analyst；Berkshire 使用 Berkshire Analysis Team。兩臂應共享同一份 frozen `EvidencePacket`，而下游的 Report Context、Bull/Bear、Trader、Risk 與安全執行邊界維持相同；記憶、cache、audit、execution DB、Safety state、broker lock 與（Paper 模式下）Alpaca 帳戶則應各自隔離。

A/B performance includes execution timing/latency differences; it is not a pure isolated analysis-quality measurement. 兩臂依序分析與執行，市場價格和分析延遲可能使成交時間不同。

若要嘗試兩個隔離 Alpaca Paper 帳戶的執行，需要 A/B 專用 credentials、`TRADINGBUFFETT_ALPACA_READ_ONLY=False`、明確的 `--execute-paper` 與每臂上限，例如：

```bash
python scripts/run_analysis_ab.py \
  --symbol NVDA \
  --date "$(TZ=America/New_York date +%F)" \
  --results-root ~/.tradingbuffett/results/ab-paper \
  --execute-paper \
  --paper-notional-usd 500
```

**請先閱讀下方「目前進度與限制」。** [2026-09-23 修復驗收](docs/CONTINUOUS_30D_ACCEPTANCE_2026-09-23.md) 記錄 C01–C04 的離線驗收。本次啟動前修復後完整 suite 為 `1632 passed, 334 subtests passed`。正式 30 日雙帳戶實驗仍須在交易時段完成真實 Paper recovery gate；目前應限於 shadow 或受控驗收方式。

#### 正式 30-Day A/B campaign

A/B campaign 是 Traders 與 Berkshire 的同一標的、`--days` 指定數量（預設 30，可任意正整數）的 **NYSE 有效交易日** 雙帳戶 Paper 實驗。它重用單一 pair runner：兩臂保有隔離的 analysis、memory、execution DB、Safety state 與 Paper account，並共享每一日 frozen evidence。它不是常駐服務；在每個交易日市場開盤時由既有 cron/scheduler 呼叫一次即可。

```bash
# 首次建立 campaign；在當日市場開盤時會執行第一個 due pair。
python -m scripts.run_analysis_ab_campaign \
  --symbol AAPL \
  --start-date 2026-09-23 \
  --days 30 \
  --execute \
  --paper-notional-usd 500 \
  --results-root ~/.tradingbuffett/results/ab-paper-aapl

# 之後每天用同一個 campaign root 恢復；不會重跑 completed session。
python -m scripts.run_analysis_ab_campaign \
  --resume ~/.tradingbuffett/results/ab-paper-aapl

# 統一入口：啟動 unattended auto launcher 子行程，逐日自動 resume 同一 campaign。
python -m cli.main long-run --mode ab \
  --symbol AAPL --start-date 2026-09-23 \
  --execute --paper-notional-usd 500

# continuous：永不 finalize；每跑完 --days 個 session 就由權威日曆凍結延伸下一個 chunk，
# campaign_id、baseline equity、fingerprint 與已完成前綴全部保留。
python -m cli.main long-run --mode ab \
  --symbol AAPL --start-date 2026-09-23 --days 30 --continuous
```

campaign 會以 Alpaca calendar 固定 30 個 NYSE sessions，未完成前一日 pair 時停止而不會跳到下一日；市場關閉時保留 state 供下次呼叫。它會固定 A/B config fingerprint 與解析後的非秘密 LLM/embedding provider、model、endpoint identity；變更即 fail closed。`campaign_state.json`、`AB_CAMPAIGN.json` 與 pair state 都不會寫入 API key、token、password 或 authorization header。

**全域 runner lock**：single long-run、legacy A/B pair runner、A/B campaign coordinator 與 auto launcher 都會先取得 application-wide flock（`~/.tradingbuffett/runner.global.lock`，永不刪除檔案）；同一時間全機只允許一個 Paper runner 進程，競爭者立即以 exit code 2 fail closed。鎖順序固定為 global runner lock（最外層）→ 各模式既有鎖（Phase-D runner lock / campaign lock / pair `.ab.lock`）。

首次使用 `--config-json` 時，resume 也必須提供相同檔案；否則 coordinator 會將它視為 config drift 而拒絕繼續。

完成第 30 個 pair 後，`campaign_summary.json` 與 `campaign_summary.md` 會比較兩個 Paper 帳戶。起始與結束 equity 都直接讀取 Alpaca broker，報告包含 starting equity、ending equity、absolute P&L、return%，以及既有 analysis/execution telemetry；它不會宣告「贏家」。結束 broker snapshot 會在 completion 時 durable pin，之後 resume 不會因帳戶後續變動而刷新它。若 final report 寫入中斷，重新執行 `--resume` 只會重新產生報告，不會再次交易。

### 30 日 A/B campaign 的 launch-blocker 防護（已 code-complete）

- **Terminal replay 語意**：同一 `decision_id` 重放時，若 primary 訂單處於 REJECTED/CANCELED/EXPIRED（保留失敗語意）或 SUBMITTING/UNKNOWN/ACCEPTED/PARTIAL（交由 recovery/reconciliation 處理），一律 fail closed、零新 POST、不會被誤報為成功；只有 FILLED（已驗證 identity）與 PENDING（沿用原 deterministic client_order_id 重送）走既有路徑。
- **Day-30 final settlement gate**：Paper campaign 每次 finalization 前都要求兩個 backend 執行 DB 存在且帳戶綁定符合 campaign 起始帳戶，並無條件呼叫兩邊既有 `ExecutionService.startup_recover`；兩邊 `success is True` 才繼續檢查 campaign symbol 的 primary 訂單是否 terminal。Recovery 不乾淨或仍有未結算訂單時不 pin ending equity、不產生正式 final report，並回報 `awaiting_final_settlement`（已完成 state 偵測到新 anomaly 時回報 `recovery_not_clean`）。Recovery 只採納既有狀態，不重新分析或產生新 decision_id。
- **一鍵 auto launcher（unattended 30 sessions）**：`scripts/run_analysis_ab_campaign_auto.py` 逐日 resume 同一 campaign root：`session_completed` 後等待下一個 frozen session 的 effective target 再繼續（沿用 `state["sessions"]`，不自算交易日）；`not_due` 以 ≤300 秒 bounded polling 跨日等待；`market_closed` 是 schedule wait（直接等到當日 effective target，早開盤前啟動不會耗用 retry 預算，early close 由 calendar authority 自動調整）；calendar 讀取使用 campaign Account A read-only credentials；`pair_unfinished` 才使用 `MAX_SAME_DAY_RETRIES` 次有界同日重試；`awaiting_final_settlement` 走有界 settlement 重試，超過預算後交還人工；未指定 `--results-root` 時，create 與後續 resume 都與 coordinator 一致使用 `~/.tradingbuffett/results/ab`（不會落到 CWD），CLI 要求 create 必須同時提供 `--symbol` 與 `--start-date`，且 `--resume` 不得與兩者混用。
- **Final report 完整性 gate**：finalization 會逐一驗證每個 frozen session 的 on-disk `pair_state.json`（COMPLETED、symbol/date 相符），且最終報告只計入 campaign 自己的 session+symbol pairs（`expected_pair_dirs` 過濾）；pair 數不符時 fail closed，不產出報告。

- 2026-09-23 審查缺陷修復驗收：**離線通過**（詳見 [驗收報告](docs/CONTINUOUS_30D_ACCEPTANCE_2026-09-23.md)）。
- Real Alpaca Paper recovery gate: **PENDING**（需 market-hours 對真實 Paper API 驗證 recovery/reconciliation 行為）。
- Gate 的正式人工步驟與證據欄位見 [Real Alpaca Paper recovery gate](docs/REAL_ALPACA_PAPER_RECOVERY_GATE.md)。在該 gate 實際通過且有可靠市值資料來源前，不可宣稱已完成 30 日 campaign 啟動驗收。

## 產物在哪裡？

| 產物 | 預設位置 | 用途 |
| --- | --- | --- |
| 單次 run audit | `~/.tradingbuffett/results/<symbol>/TradingAgentsStrategy_logs/runs/` | prompts、tool calls、LLM usage、state、錯誤與最終狀態 |
| 決策記憶 | `~/.tradingbuffett/memory/trading_memory.md` | 歷史決策紀錄 |
| agent memory | `~/.tradingbuffett/memory/agent_memory/` | 反思與檢索記憶 |
| execution ledger | `~/.tradingbuffett/execution/execution.sqlite3` | intent、order、fill、帳戶對帳狀態 |
| long-run state | `~/.tradingbuffett/long_run/` | observation 設定、active state、journals、最終報告 |
| A/B 實驗結果 | `~/.tradingbuffett/results/ab/` | campaign、frozen evidence、pair state、兩臂記錄與 summary |

不要刪除 execution ledger 或 active state 來「恢復」帳戶。這會破壞冪等與對帳證據；若狀態異常，應依 audit 與明確的維護流程處理。

## 驗證與開發

```bash
python -m compileall -q tradingagents cli scripts
python -m pytest -q
```

完整測試數量請以最新 GitHub Actions run 為準。測試通過表示既有回歸測試通過，**不等於**正式 A/B 或真實 Paper recovery 已完全驗證。

主要程式區域如下：

| 路徑 | 職責 |
| --- | --- |
| `cli/` | Typer CLI 與終端機互動流程 |
| `tradingagents/graph/` | LangGraph 編排、節點連線、signal extraction、reflection |
| `tradingagents/agents/` | analysts、researchers、managers、trader、risk roles 與 schemas |
| `tradingagents/dataflows/` | 市場/新聞/總經/SEC 資料介面與交易日曆 |
| `tradingagents/screening/` | 美股 universe、確定性排名、Top20 與 entry gate |
| `tradingagents/execution/` | durable execution、broker authority、recovery、保護單與 exit |
| `tradingagents/long_run*.py` | 30 日 observation 的設定、排程、state、恢復與報告 |
| `tradingagents/analysis_backends/berkshire/` | Berkshire Analysis Team backend |
| `scripts/run_analysis_ab.py` | frozen-evidence A/B pair runner |
| `scripts/run_analysis_ab_campaign.py` | 30 NYSE-session A/B campaign coordinator 與 broker-equity finalizer |
| `tests/` | 離線 mock、故障注入與回歸測試 |

## 目前進度與限制（2026-09-23）

### 已完成且可供開發/研究使用

- Paper-only hard lock、strict `TradeIntent`、durable SQLite outbox、券商快照與 reconciliation、UNKNOWN lookup/adopt、帳戶 lock、風險與安全 guardrail。
- Phase B 的角色化 LLM、重試/停止語意、持倉/曝險限制、SEC/IR、corporate-action quarantine。
- Phase C 的全市場 Top20 screening 與執行 entry gate。
- Phase D 的 30 日單策略 observation：設定、preflight、journal、crash/resume、排程與最終報告。
- Traders/Berkshire frozen-evidence A/B runner、雙帳戶設定、30 NYSE-session campaign coordinator 與 shadow smoke；2026-09-22 shadow A/B smoke 成功，兩臂皆產出有效 HOLD，且零 broker mutation。30 日 final equity comparison 以 broker account 為權威，不以 local DB、fills 或 signals 推算；2026-09-23 C01–C04 離線修復驗收已通過。

### 剩餘驗證 gate

- **2026-09-23 修復驗收：離線通過。** C01 的 state-only 中斷恢復與 C02–C04 獨立重現均通過；詳見 [驗收報告](docs/CONTINUOUS_30D_ACCEPTANCE_2026-09-23.md)。本次啟動前修復後完整 suite：`1632 passed, 334 subtests passed`。
- **真實 Paper recovery 驗證：尚未完成。** 必須在交易時段內，透過未修改的 production execution path 完成安全的 Paper submit/cancel，以及中斷後以原本的 `decision_id`、`client_order_id` 與 broker order 恢復/認領，並證明不會重複下單。
- 2026-09-22 的 pre-30-day acceptance 當時因市場收盤，無法安全建立 production path 所需的測試訂單；驗收正確地沒有繞過市場時鐘或送出不受控訂單。

完成並記錄真實 Paper recovery gate 後，才可將 30 日雙帳戶 A/B 視為已完成啟動前驗證。訊號一致率與 telemetry 仍不等同報酬率或投資建議。

## 延伸文件

- [Quick Start](QUICKSTART.md)：更精簡的安裝與疑難排解
- [Architecture Guide](ARCHITECTURE.md)：模組、資料流與執行邊界細節
- [Local LLM Guide](LOCAL_LLM_GUIDE.md)：local/OpenAI-compatible 模型設定
- [Project Goals and Status](PROJECT_GOALS_AND_STATUS.md)：完整階段歷史與驗收紀錄（含歷史狀態）
- [2026-09-22 最終深度審查](docs/FINAL_30D_DEEP_REVIEW_2026-09-22.md)：修復前的審查與重現基準
- [2026-09-23 連續 30 日審查](docs/CONTINUOUS_30D_FINAL_AUDIT_2026-09-23.md)：修復前缺陷證據與離線重現結果
- [2026-09-23 修復驗收](docs/CONTINUOUS_30D_ACCEPTANCE_2026-09-23.md)：C01–C04 離線再次驗收與前次失敗紀錄
- [2026-09-22 Pre-30-Day Acceptance](docs/PRE_30D_SMOKE_RECOVERY_ACCEPTANCE.md)：最近一次 shadow、broker preflight 與待補的 Paper recovery 驗收
- [Real Alpaca Paper recovery gate](docs/REAL_ALPACA_PAPER_RECOVERY_GATE.md)：正式 Paper crash/restart 人工驗收程序與證據清單

## 沿革與授權

本 fork 源自 [AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent) 與 [TradingAgents](https://github.com/TauricResearch/TradingAgents)，並在 Paper execution、安全、長期 observation、screening、memory 與 A/B 邊界上重構。

License: [Apache-2.0](LICENSE)
