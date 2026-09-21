# Traders

以 LangGraph 多代理分析、Alpaca Paper 執行與可稽核安全邊界組成的交易研究框架。

> **Paper-only**：程式固定使用 Alpaca Paper API。`TRADINGBUFFETT_ALPACA_USE_PAPER=False`、live endpoint 或無法證明為 paper 的設定都會 fail closed，零 broker mutation。
>
> 本專案僅供研究與教育，不構成投資建議。

## 目前用途

- CLI 單次多代理研究：Market、Social、News、Fundamentals、Macro。
- Bull／Bear 與 Risky／Safe／Neutral 雙層辯論。
- 結構化 `TradeIntent` 與單一 Paper execution service。
- 30 日無人值守 Paper observation。
- Traders × Berkshire 雙 30 日 A/B（shadow 或雙帳戶 Alpaca Paper 執行）。
- 回測、決策記憶、每日報告、成本與完整 run audit log。

WebUI 已移除。核心不再依賴 Dash、Flask、Plotly、Gradio 或任何 UI state。

## 安裝

需求：Python 3.10 以上；建議 3.11 或 3.12。

```bash
git clone https://github.com/ihsieh31/traders.git
cd traders
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.sample .env
```

Windows 啟用環境：

```powershell
.venv\Scripts\activate
```

完整鎖定安裝：

```bash
pip install --no-deps -r requirements.lock
pip check
```

## 最小設定

編輯 `.env`：

```env
TRADINGBUFFETT_ALPACA_API_KEY=your_paper_key
TRADINGBUFFETT_ALPACA_SECRET_KEY=your_paper_secret
TRADINGBUFFETT_ALPACA_USE_PAPER=True

TRADINGBUFFETT_LLM_PROVIDER=openai
TRADINGBUFFETT_OPENAI_API_KEY=your_openai_key
```

所有環境變數使用 `TRADINGBUFFETT_` namespace，不讀取其他 fork 的未加前綴設定。支援 OpenAI、local OpenAI-compatible、Google、Anthropic、xAI、MiniMax、DeepSeek、Qwen、GLM、OpenRouter、Ollama 與 Azure。

## 執行

查看 CLI：

```bash
python -m cli.main --help
```

互動式單次分析：

```bash
python -m cli.main analyze
```

30 日無人值守 Paper observation：

```bash
python -m cli.main long-run
```

第一次啟動會收集缺少的非機密設定、執行唯讀 preflight，並要求明確 Paper-test 授權。授權前不會送出 broker mutation。中斷後重跑同一指令會恢復原 observation；已停止的 observation 不會被偷偷續跑。

## 雙 30 日 Traders × Berkshire A/B

A/B runner 的唯一研究變因是 `analysis_backend`：Traders 使用原生五 analyst，Berkshire 使用 Berkshire Analysis Team；兩邊的 LLM 預算、凍結證據、Report Context、Bull/Bear、Trader、Risk 與所有下游節點相同。

```bash
python scripts/run_analysis_ab.py \
  --symbol NVDA \
  --date 2026-09-21 \
  --results-root ~/.tradingbuffett/results/ab
```

要實際送到兩個隔離的 Alpaca Paper 帳戶，必須另外設定 account A/B keys、令 `TRADINGBUFFETT_ALPACA_READ_ONLY=False`，並明確給定每臂上限：

```bash
python scripts/run_analysis_ab.py \
  --symbol NVDA \
  --date "$(TZ=America/New_York date +%F)" \
  --results-root ~/.tradingbuffett/results/ab-paper \
  --execute-paper \
  --paper-notional-usd 500
```

彙總：

```bash
python scripts/summarize_analysis_ab.py \
  --root ~/.tradingbuffett/results/ab
```

A/B 保護條件：

- `memory_retrieval_enabled`、outcome reflection、memory maintenance 強制開啟。
- Traders 與 Berkshire 各自使用持久且互斥的 memory、cache、audit、execution DB、Safety state 與 kill switch；broker lock 固定在同一主機目錄，並以已驗證的不同 account ID 分鍵。
- 兩邊序列執行；順序依 `symbol + trade_date` 交錯，避免固定先跑同一邊。
- 每組先建立同一份 frozen EvidencePacket；五個 section 都必須至少有一筆可用資料，且使用 campaign pin 的 SHA-256 驗證。
- `AB_CAMPAIGN.json` 固定整段測試的 config、完整 analyst set、程式與 prompt fingerprint；中途漂移會停止。
- 同一 symbol/date 不可覆寫重跑。
- Checkpoint 強制關閉；未給 `--execute-paper` 時為 shadow，明確開啟時 Traders 固定 account A、Berkshire 固定 account B。
- Paper 執行要求兩個不同帳戶、第一組開始前持倉與 open orders 為空且 equity 誤差在 0.1%／US$1 內，並要求 Alpaca 當日且 regular session 開市。
- endpoint 必須是 `paper-api.alpaca.markets` 且 SDK 永遠使用 `paper=True`；live 或未知 endpoint 直接拒絕。
- `pair_summary.json` 只在兩邊完成後寫入，任一 profile 看不到對方結果。

兩臂分析讀取同一份 point-in-time EvidencePacket，不在各自執行時重新查外部資料。

## 執行安全

所有訂單只能經過 `ExecutionService.execute`：

- Durable SQLite outbox：先 commit intent／order，再允許 broker POST。
- Deterministic `decision_id` 與 `client_order_id`，避免重啟重複送單。
- POST timeout／5xx 視為模糊結果，進入 `UNKNOWN` 後 lookup/adopt，不直接重送。
- `BrokerSnapshot` 是 account、positions、orders、fills 與 cash 的唯一權威。
- Reconciliation 只有 `CLEAN` 才能增加曝險；任何不一致維持 `PAUSED`。
- 最後 POST 前重新驗證 market clock、snapshot、quote、entry policy 與 size。
- 帳戶層 OS lock 阻止雙 process 同時執行。
- Kill switch、daily loss、drawdown、consecutive rejection、notional、concentration 與 token budget 均為 deterministic guardrail。
- SHORT 需每次 run 明確 opt-in；crypto 永遠不允許 SHORT。

## 全市場 Screening

啟用 `auto_screening_enabled` 後：

1. 從 Alpaca 取得完整 ACTIVE tradable US-equity universe。
2. 以權威交易日曆驗證 61 根完整日 K。
3. 套用價格、流動性、報酬、波動與 volume ratio 確定性門檻。
4. Top40 compact 因子表交給獨立 Screening role。
5. 嚴格驗證恰 20 個排名後形成 Top20。
6. 每輪重新取得 holdings；只有當日 Top20 可增加曝險，其他持股僅可檢視或減風險。

Selection cache 使用 atomic replace、flock、日期／設定指紋與 SHA-256 完整性封印；損毀、過期或人工 refresh 失敗不會沿用舊名單。

## 記憶與稽核

- Decision log：`~/.tradingbuffett/memory/trading_memory.md`
- Agent memory：`~/.tradingbuffett/memory/agent_memory/`
- Run audit：`~/.tradingbuffett/results/<symbol>/TradingAgentsStrategy_logs/runs/`
- Execution ledger：`~/.tradingbuffett/execution/execution.sqlite3`
- Long-run state：`~/.tradingbuffett/long_run/`
- A/B state：`~/.tradingbuffett/results/ab/_profiles/<profile>/`

Run audit 記錄 prompts、tool calls、LLM usage、state snapshots、errors 與 final state。不得刪除 execution ledger 來強迫帳戶恢復 `CLEAN`。

## Docker

Docker image 現在是 CLI image：

```bash
docker build -t traders .
docker run --rm --env-file .env traders --help
docker run --rm -it --env-file .env \
  -v "$HOME/.tradingbuffett:/app/.tradingbuffett" \
  traders long-run
```

## 驗證

```bash
python -m compileall -q tradingagents cli scripts
python -m pytest -q
```

2026-09-22 最終基準：`1414 passed, 139 skipped, 294 subtests passed`。Skipped cases 是已移除 WebUI 的歷史回歸斷言。

## 文件

- [Quick Start](QUICKSTART.md)
- [Architecture](ARCHITECTURE.md)
- [Local LLM Guide](LOCAL_LLM_GUIDE.md)
- [Project Goals and Status](PROJECT_GOALS_AND_STATUS.md)
- [雙 30 日最終審查](docs/DUAL_30D_FINAL_REVIEW_2026-09-21.md)

## 上游

本專案由 [AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent) 與 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 衍生，執行、安全、長期 observation、screening、memory 與 A/B 邊界已在本 fork 重構。

License: Apache-2.0
