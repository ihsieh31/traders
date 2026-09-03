# Architecture Guide

A concise map of how AlpacaTradingAgent works internally — for contributors
and operators who want to know where things happen and why.

## The big picture

```
                        ┌──────────────────────── WebUI (Dash) / CLI ───────────────────────┐
                        │  symbols, LLM provider, depth, auto-trade, scheduling             │
                        └──────────────────────────────┬────────────────────────────────────┘
                                                       ▼
┌───────────────────────────── TradingAgentsGraph (LangGraph) ─────────────────────────────┐
│                                                                                          │
│  5 parallel analysts                research debate                execution chain       │
│  ┌────────────────────┐      ┌───────────────────────────┐   ┌───────────────────────┐   │
│  │ Market             │      │ Bull researcher           │   │ Trader                │   │
│  │ Social sentiment   │  ──► │ Bear researcher           │──►│ Risky/Safe/Neutral    │   │
│  │ News               │      │ (N debate rounds)         │   │ risk debate           │   │
│  │ Fundamentals       │      │ Research manager (judge)  │   │ Risk manager (judge)  │   │
│  │ Macro (FRED)       │      └───────────────────────────┘   └──────────┬────────────┘   │
│  └────────────────────┘                                                 │                │
│        ▲ tools: Alpaca data, Finnhub, Google News, Reddit,              ▼                │
│          CoinDesk/DeFiLlama (crypto), OpenAI web search       final decision +           │
│                                                               typed TradeIntent          │
└──────────────────────────────────────────────────────────────────┬───────────────────────┘
                                                                   ▼
                                       Alpaca Paper execution (paper-only) — market/close orders
```

Final decisions are executable actions (`BUY/HOLD/SELL` in investment mode,
`LONG/NEUTRAL/SHORT` in trading mode), carried in a typed `TradeIntent`
schema. Numeric stop-loss and take-profit controls can be submitted as
broker-side bracket/OTO orders when enabled; qualitative controls remain
advisory.

## Package map

| Path | Responsibility |
|---|---|
| `tradingagents/graph/` | LangGraph orchestration. `trading_graph.py` builds the graph and owns the LLM clients, memories, and reflection; `setup.py` wires nodes; `conditional_logic.py` controls debate rounds; `propagation.py` creates initial state; `signal_processing.py` extracts the final signal; `checkpointer.py` optional SQLite resume. |
| `tradingagents/agents/` | The agents themselves: `analysts/` (market, social, news, fundamentals, macro), `researchers/` (bull/bear), `managers/`, `trader/`, `risk_mgmt/`, plus `utils/` (agent states, memory, trading modes) and `schemas.py` (typed `TradeIntent`). |
| `tradingagents/dataflows/` | Every external data source behind one interface: `alpaca_utils.py` (bars, quotes, account, orders, execution), Finnhub, Google News, Reddit, FRED macro, crypto sources, with a yfinance fallback for supported failures. `config.py` holds runtime config + API keys. |
| `tradingagents/llm_clients/` | Provider adapters (OpenAI, Anthropic, Google, xAI, MiniMax, DeepSeek, Qwen, GLM, OpenRouter, Ollama, Azure, local endpoints) behind `create_llm_client`. |
| `tradingagents/prompts/` | All agent prompts as editable text templates (`TRADINGAGENTS_PROMPT_DIR` overrides). |
| `tradingagents/run_logger.py` | Append-only audit trail: every prompt, tool call, LLM call (with token usage), state snapshot, and final state per run under `eval_results/<symbol>/TradingAgentsStrategy_logs/runs/`. |
| `tradingagents/default_config.py` | Single source of defaults; everything is overridable per run. |
| `webui/` | Dash interface: `layout.py` composes panels from `components/`, `callbacks/` register interaction handlers, `utils/state.py` is the shared app state. Entry: `python run_webui_dash.py`. |
| `cli/` | Terminal interface: `python -m cli.main`. |
| `tests/` | Pytest suite; deterministic, no network, no live keys. |

## The analysis lifecycle

1. **Kickoff** — WebUI/CLI builds a config (provider, models, depth, mode)
   and calls `TradingAgentsGraph.propagate(symbol, date)`. A run log is
   opened immediately; everything that follows is recorded incrementally.
2. **Analysts** — five analysts run (parallel by default) with tool access;
   each produces a markdown report. Context managers keep downstream
   prompts within budget by chunking and scoring report evidence.
3. **Research debate** — bull and bear researchers argue over the reports
   for N rounds; the research manager judges and writes an investment plan.
4. **Execution chain** — the trader turns the plan into a proposal; the
   risky/safe/neutral risk debate stress-tests it; the risk manager issues
   the final decision plus a typed `TradeIntent`.
5. **Signal + execution** — `SignalProcessor` extracts the executable
   action. If auto-trading is on, the WebUI executes the typed `TradeIntent`
   via the single durable entry `tradingagents.execution.ExecutionService`
   (SQLite outbox commit before any broker POST, deterministic
   `client_order_id`, UNKNOWN lookup/adopt). Raw-signal execution
   (`AlpacaUtils.execute_trading_action`) is disabled fail-closed, and the
   direct helpers (`place_market_order` / `place_protected_market_order` /
   `close_position`) were removed; `AlpacaUtils` keeps data/query helpers only.
6. **Decision log** — the completed decision is appended to a markdown
   memory log as `pending`, and resolved later with realized returns and a
   reflection once the outcome is known.

## Memory and learning

Two complementary memories:

- **Decision log** (`TradingMemoryLog`): append-only markdown of every
  final decision, later updated with realized return / alpha / holding
  days and a reflection. Recent same-ticker and cross-ticker entries are
  injected into future prompts as past context.
- **Per-agent situation memories** (`FinancialSituationMemory`): five
  ChromaDB collections (bull, bear, trader, invest judge, risk manager)
  storing (situation embedding → lesson) pairs, queried by similarity at
  decision time.

## Persistence map

| Location | Contents |
|---|---|
| `eval_results/<symbol>/TradingAgentsStrategy_logs/runs/*.json` | Full audit trail per run: config, events (prompts, tool calls, LLM calls with token usage), snapshots, final state, final signal. |
| `~/.tradingagents/memory/trading_memory.md` | The decision log (path configurable). |
| `~/.tradingagents/memory/agent_memory/` | Persistent per-agent ChromaDB reflection memories. |
| `~/.tradingagents/safety/` | Safety high-water mark, rejection/token counters, and the optional `KILL_SWITCH` flag. |
| `reports/YYYY-MM-DD.{md,html}` | Optional daily operations reports. |
| `tradingagents/dataflows/data_cache/` | Cached market data. |
| `eval_results/.../checkpoints` | Optional SQLite LangGraph checkpoints for resume. |

## Configuration

`tradingagents/default_config.py` is the single source of truth; the WebUI
and CLI pass overrides per run, and API keys come from `.env` /
environment (see `env.sample`). This build is paper-only: the trading client
is hard-locked to `paper=True`, `ALPACA_USE_PAPER=False` fails closed, and
only the explicit paper endpoint is allowed.

## Testing conventions

- `python -m pytest tests/` — the suite is deterministic: no network, no
  live keys; external boundaries are mocked and embeddings are faked.
- `tests/test_import_no_network.py`-style guards assert that importing the
  package performs no network calls.
- On Windows, ChromaDB keeps store files open: use
  `TemporaryDirectory(ignore_cleanup_errors=True)` in tests.

## Integrated contribution set

These reviewed contributions were integrated together because several are
stacked and share execution, configuration, and WebUI paths:

| PR | Adds |
|---|---|
| [#25](https://github.com/huygiatrng/AlpacaTradingAgent/pull/25) | Deterministic risk sizing: fractional Kelly, ATR stops, exposure caps |
| [#26](https://github.com/huygiatrng/AlpacaTradingAgent/pull/26) | Walk-forward backtesting engine over recorded decisions + WebUI panel |
| [#27](https://github.com/huygiatrng/AlpacaTradingAgent/pull/27) | Self-learning memory: persistent agent memories fed by realized outcomes |
| [#29](https://github.com/huygiatrng/AlpacaTradingAgent/pull/29) | No network calls / circular imports at package import time |
| [#30](https://github.com/huygiatrng/AlpacaTradingAgent/pull/30) | Real bracket/OTO protective orders |
| [#31](https://github.com/huygiatrng/AlpacaTradingAgent/pull/31) | Pydantic 3-ready model config |
| [#32](https://github.com/huygiatrng/AlpacaTradingAgent/pull/32) | Production safety layer: pre-trade checks, circuit breakers, kill switch |
| [#33](https://github.com/huygiatrng/AlpacaTradingAgent/pull/33) | GitHub Actions CI |
| [#34](https://github.com/huygiatrng/AlpacaTradingAgent/pull/34) | Intensive teaching: seed agent memories from recorded backtest history |
| [#35](https://github.com/huygiatrng/AlpacaTradingAgent/pull/35) | FinMem-style memory maintenance: decay, dedup, size caps |
| [#36](https://github.com/huygiatrng/AlpacaTradingAgent/pull/36) | Chaos test suite + NaN/HTML broker-data hardening |
| [#37](https://github.com/huygiatrng/AlpacaTradingAgent/pull/37) | LLM cost monitor: attributed spend vs realized returns |
| [#38](https://github.com/huygiatrng/AlpacaTradingAgent/pull/38) | Portfolio-level intelligence: correlation, vol sizing, exposure cap |
| [#39](https://github.com/huygiatrng/AlpacaTradingAgent/pull/39) | Deterministic market-regime detection |
| [#40](https://github.com/huygiatrng/AlpacaTradingAgent/pull/40) | Daily operations report + Telegram/webhook alerts |
