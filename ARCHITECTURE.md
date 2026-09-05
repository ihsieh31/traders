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
                                       BrokerSnapshot + reconciliation + account lock
                                                       │
                                                       ▼
                                       Alpaca Paper execution (paper-only)
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
| `tradingagents/screening/` | Phase C full-market screening: session math (`sessions.py`), the ACTIVE US_EQUITY universe with pagination (`universe.py`), deterministic bars validation + eligibility + Top40 formula (`metrics.py`), the third Screening role with the strict Top20 schema (`llm.py`, `prompt.py`), the daily selection cache (`selection_store.py`), the round pipeline (`pipeline.py`) and the execution entry gate (`gate.py`). |
| `tradingagents/dataflows/market_calendar.py` | Single shared US-market holiday tables and session walks used by both the WebUI market-hours module and Phase C screening. |
| `tradingagents/llm_clients/` | Provider adapters (OpenAI, Anthropic, Google, xAI, MiniMax, DeepSeek, Qwen, GLM, OpenRouter, Ollama, Azure, local endpoints) behind `create_llm_client`. `roles.py` resolves the fixed Analysis/Decision roles; `retry.py` is the single bounded retry owner (`ProviderFailure` + exact request caps). |
| `tradingagents/prompts/` | All agent prompts as editable text templates (`TRADINGAGENTS_PROMPT_DIR` overrides). |
| `tradingagents/run_logger.py` | Append-only audit trail: every prompt, tool call, LLM call (with token usage), state snapshot, and final state per run under `eval_results/<symbol>/TradingAgentsStrategy_logs/runs/`. Provider failures add a `provider_failure` event and a `stopped` run status. |
| `tradingagents/risk/exposure.py` | Phase B deterministic exposure evaluator: clips opening notionals to the canonical symbol cap, sector cap, gross cap and cash, counting outstanding increasing orders. |
| `tradingagents/risk/corporate_actions.py` | Persisted corporate-action quarantine (split/ticker change/delisting/non-tradable): fail-closed gate for new exposure, operator+CLEAN release, no TTL. |
| `tradingagents/dataflows/sec_ir.py` | Official SEC filings (submissions API) and configured company IR pages with honest published/retrieved metadata and per-type freshness. |
| `tradingagents/execution/context.py` | Shared Trader/Decision position context rendered from the strict `capture_broker_snapshot` path. |
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
   `client_order_id`, UNKNOWN lookup/adopt). Before mutation it captures one
   immutable account-bound `BrokerSnapshot`, acquires a crash-safe account
   file lock, recovers durable nonterminal orders, requires reconciliation
   `CLEAN`, and validates a fresh symbol-bound quote. The same snapshot feeds
   sizing, safety, reconciliation, and submit preflight. Every broker action
   is followed immediately by another snapshot and reconciliation. Raw-signal execution
   (`AlpacaUtils.execute_trading_action`) is disabled fail-closed, and the
   direct helpers (`place_market_order` / `place_protected_market_order` /
   `close_position`) were removed; `AlpacaUtils` keeps data/query helpers only.
6. **Decision log** — the completed decision is appended to a markdown
   memory log as `pending`, and resolved later with realized returns and a
   reflection once the outcome is known.

## Phase C screening lifecycle (auto mode)

Off by default. With `auto_screening_enabled` on, the scheduler (WebUI loop/market-hour modes, CLI) calls
`tradingagents.screening.pipeline.prepare_screening_round` once per round:

1. **Scan-or-cache** — the first round of a US trading day fetches the full
   ACTIVE US_EQUITY universe, pulls daily bars in bounded 100-symbol batches
   under one explicit adjustment policy, validates the 61-session window per
   symbol (no unclosed bar, no holes, no NaN/Inf), applies the deterministic
   gates and percentile score, and sends the Top40 compact table to the
   Screening role, which must return exactly 20 strictly-validated entries.
   The result is stored in one JSON selection file; later rounds the same
   trading day reuse it after full revalidation (integrity seal, date,
   fingerprint, schema, membership) — `save()` stamps a SHA-256 self-hash
   over the payload and `load_valid()` verifies it first, so an edited or
   pre-seal file is treated as absent and the day rescans (tamper evidence,
   not provenance against a writer who recomputes the seal). Non-trading
   days never scan and never enter.
2. **Holdings union** — fresh broker positions are fetched every round
   (never cached with the selection). US-equity holdings outside the Top20
   are analyzed for held-risk review only; quarantined or non-tradable ones
   are listed with an explicit blocked-review reason; crypto positions are
   listed separately under their existing management path.
3. **Entry gate** — inside the single `ExecutionService` entry, any
   exposure-adding order in auto mode requires the symbol to be in today's
   validated Top20 (holdings outside it may only HOLD or reduce risk through
   the verified Phase A path). The gate reads the on-disk selection, so
   restarts and direct callers get the same answer; P2 exposure caps and the
   quarantine gate still run after it.
4. **Stop semantics** — any screening-stage failure (universe/bars/
   quarantine unavailable, <20 candidates, sector capacity, invalid LLM
   output, provider exhaustion) or a holdings-fetch failure returns a
   stopped plan; the scheduler halts the round and the schedule with zero
   downstream analysis and zero new mutations. A manual refresh removes the
   cached selection before scanning, so a failed refresh cannot fall back.

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
| `dataflows/data_cache/screening_selection.json` | Phase C daily Top20 selection (trading date, as_of, role/model, config fingerprint, Top40 features, validated Top20, sector mode, `integrity` self-hash seal). No credentials; `.lock` sibling serializes concurrent first scans. |
| `eval_results/execution.db` | Three-table durable intent/order/fill ledger. A reserved account-status intent stores the latest `CLEAN`/`PAUSED` reasons and reconciliation baseline without a second persistence system. |
| `eval_results/.execution-locks/` | Per-account stdlib OS locks. File descriptors are released by the OS after process exit/crash; no stale lease cleanup exists. |

## Execution recovery and authority

Scheduler/auto-trade startup calls `ExecutionService.startup_recover()` before
analysis can dispatch orders. It performs bounded `client_order_id` lookup for
`PENDING`, `SUBMITTING`, `UNKNOWN`, and `PARTIAL` rows. A definitively absent
idempotent market order may be resubmitted once with the same ID; ambiguous
lookups and non-idempotent closes stay `PAUSED`. Each scheduler execution calls
the same recovery/reconciliation gate again.

Only `CLEAN` accounts may add exposure. Missing/malformed account facts, stale
snapshot/quote, identity conflicts, unknown orders, position mismatch, and
unresolved partial fills persist a readable `PAUSED` reason. A paused account
may only reduce risk when a fresh broker position proves the side and exact
maximum quantity, no conflicting close order exists, and the paper/safety/lock
gates pass. Explicit close market orders carry the durable `client_order_id`;
POST validation/rejection is terminal and POST timeout becomes `UNKNOWN`.

Phase B adds three deterministic gates for exposure-adding orders (verified
reducing exits keep the Phase A path): the corporate-action quarantine, the
exposure-cap evaluator (symbol/sector/gross/cash headroom including
outstanding increasing orders), and — on the recovery path — the same
quarantine and cap checks recomputed from the fresh snapshot, so a recovered
resubmit is clipped to currently provable headroom instead of reusing the
original size. An opening order on an already-held symbol is an exposure
increase governed by these caps (the Phase A implicit-HOLD shortcut was
removed per the Phase B contract).

## Configuration

`tradingagents/default_config.py` is the single source of truth; the WebUI
and CLI pass overrides per run, and API keys come from `.env` /
environment (see `env.sample`). This build is paper-only: the trading client
is hard-locked to `paper=True`, `ALPACA_USE_PAPER=False` fails closed, and
only the explicit paper endpoint is allowed.

Phase B additions: `analysis_provider/model/backend_url` and
`decision_provider/model/backend_url` (all empty = legacy quick/deep),
`llm_max_retries` (0-3, validated at startup), `llm_request_timeout_seconds`
(applied to the legacy quick/deep clients; in roles mode the Analysis/Decision
clients use their provider SDK's default per-request timeout — retry count and
backoff caps still bound every request in both modes),
`sec_ir_*` and `company_ir_pages` for primary sources,
`corporate_action_events` for the manual quarantine feed,
`auto_screening_enabled` + `screening_provider/model/backend_url` for the
Phase C Screening role (required, never inherited, off by default) with the
`screening_*` deterministic thresholds/constants, and
`max_sector_exposure_pct` + `sector_mapping` (the sector cap is active once a
mapping exists; unknown sectors then refuse new risk).

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
