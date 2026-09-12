# Architecture Guide

A concise map of how Traders works internally — for contributors
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
`LONG/NEUTRAL/SHORT` in trading mode — shorts are a per-run `allow_shorts`
opt-in enforced by a deterministic guard in the execution service, never
allowed on crypto), carried in a typed `TradeIntent`
schema. Numeric stop-loss and take-profit controls can be submitted as
broker-side bracket/OTO orders when enabled; qualitative controls remain
advisory.

There are two operating surfaces above the same pipeline: the interactive
WebUI/CLI for one-off analyses and scheduled rounds, and the Phase D
`long-run` orchestrator (`tradingagents/long_run.py`) that drives the whole
pipeline unattended for 30 calendar days with its own config, state,
scheduling, and reporting (see the Phase D lifecycle section below).

## Package map

| Path | Responsibility |
|---|---|
| `tradingagents/graph/` | LangGraph orchestration. `trading_graph.py` builds the graph and owns the LLM clients, memories, and reflection; `setup.py` wires nodes; `conditional_logic.py` controls debate rounds; `propagation.py` creates initial state; `signal_processing.py` extracts the final signal; `checkpointer.py` optional SQLite resume. |
| `tradingagents/agents/` | The agents themselves: `analysts/` (market, social, news, fundamentals, macro), `researchers/` (bull/bear), `managers/`, `trader/`, `risk_mgmt/`, plus `utils/` (agent states, memory, trading modes) and `schemas.py` (typed `TradeIntent`). |
| `tradingagents/dataflows/` | Every external data source behind one interface: `alpaca_utils.py` (bars, quotes, account, orders, execution), Finnhub, Google News, Reddit, FRED macro, crypto sources, with a yfinance fallback for supported failures. `config.py` holds runtime config + API keys. |
| `tradingagents/screening/` | Phase C full-market screening: session math (`sessions.py`), the ACTIVE US_EQUITY universe with pagination (`universe.py`), deterministic bars validation + eligibility + Top40 formula (`metrics.py`), the third Screening role with the strict Top20 schema (`llm.py`, `prompt.py`), the daily selection cache (`selection_store.py`), the round pipeline (`pipeline.py`) and the execution entry gate (`gate.py`). |
| `tradingagents/dataflows/market_calendar.py` | Single shared US-market holiday tables and session walks used by both the WebUI market-hours module and Phase C screening. |
| `tradingagents/llm_clients/` | Provider adapters (OpenAI, local OpenAI-compatible endpoints, Anthropic, Google, xAI, MiniMax, DeepSeek, Qwen, GLM, OpenRouter, Ollama, Azure — 12 names) behind `create_llm_client`. `roles.py` resolves the fixed Analysis/Decision/Screening roles plus the optional `analysis_fallback` route; `retry.py` is the single bounded retry owner (`RetryingLLM`/`RetryingRunnable` are real LangChain Runnables, `ProviderFailure`, exact request caps, transient-vs-permanent classification including all 5xx/Cloudflare codes) and hosts the shared-budget Primary→Fallback failover used by all three roles. |
| `tradingagents/execution/` | The execution core: `service.py` is the single durable entry (`execute`, `startup_recover`, `enforce_exit_deadlines`, `liquidate`, `account_status`, `lookup_unknown`); `store.py` is the stdlib-SQLite intent/order/fill ledger plus `protective_children` and the reserved `__ACCOUNT__` intent rows (account binding, frozen reconciliation baseline, explicit `rebase_account_state`); `authority.py` owns `capture_broker_snapshot`, TTL/freshness validation, the `Reconciler` (CLEAN/PAUSED reason vocabulary, gated operator `rebase_baseline`), and the account-keyed OS execution lock; `context.py` renders the shared Trader/Decision position context; `auto_trade.py` is the shared WebUI/Phase-D execution helper. |
| `tradingagents/safety/guardrails.py` | Deterministic `SafetyGuard`: kill switch (dominates everything), daily-loss halt from broker `last_equity`, drawdown-from-HWM breaker, consecutive-rejection breaker, per-order notional / symbol concentration caps, daily LLM token budget. State is flock-protected; a corrupt state file fails closed at startup. |
| `tradingagents/prompts/` | All agent prompts as editable text templates (`TRADINGAGENTS_PROMPT_DIR` overrides). |
| `tradingagents/run_logger.py` | Append-only audit trail: every prompt, tool call, LLM call (with token usage), state snapshot, and final state per run under `eval_results/<symbol>/TradingAgentsStrategy_logs/runs/`. Provider failures add a `provider_failure` event and a `stopped` run status. |
| `tradingagents/risk/exposure.py` | Phase B deterministic exposure evaluator: clips opening notionals to the canonical symbol cap, sector cap, gross cap and cash, counting outstanding increasing orders. |
| `tradingagents/risk/corporate_actions.py` | Persisted corporate-action quarantine (split/ticker change/delisting/non-tradable): fail-closed gate for new exposure, operator+CLEAN release, no TTL. |
| `tradingagents/dataflows/sec_ir.py` | Official SEC filings (submissions API) and configured company IR pages with honest published/retrieved metadata and per-type freshness. |
| `tradingagents/execution/context.py` | Shared Trader/Decision position context rendered from the strict `capture_broker_snapshot` path. |
| `tradingagents/default_config.py` | Single source of defaults; everything is overridable per run. |
| `tradingagents/long_run.py` | Phase D: the whole 30-day unattended observation lifecycle in one module — non-secret config (`~/.tradingagents/long_run/config.json`), atomic active state + single-runner lock, read-only preflight with per-role LLM probes, the daily-round pipeline, the scheduler loop with bounded retry, crash/resume, hard-stop vocabulary, and deterministic final reports. |
| `webui/` | Dash interface: `layout.py` composes panels from `components/`, `callbacks/` register interaction handlers, `utils/state.py` is the shared app state. Includes the allow-shorts/trading-mode switch, Phase C screening panel, safety guardrails panel, LLM cost panel, and backtest panel. Phase D has no WebUI surface — it is CLI-only. Entry: `python run_webui_dash.py`. |
| `cli/` | Terminal interface: `python -m cli.main` (interactive analysis with Step 5b role overrides and Step 5c screening settings) and `python -m cli.main long-run` (Phase D). |
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
   sizing, safety, reconciliation, and submit preflight; immediately before
   any exposure-adding POST the dispatcher re-validates the market clock,
   freshness, entry policy, and clipped size (failure ⇒ durable cancel with
   zero POSTs). Every broker action
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

## Phase D observation lifecycle (`long-run`)

`python -m cli.main long-run` owns the 30-day unattended Paper observation.
One module (`tradingagents/long_run.py`) owns the lifecycle; every broker
mutation still goes through `ExecutionService`, every universe decision
through Phase C screening, every analysis through the normal graph.

1. **Setup + preflight** — non-secret config lives in
   `~/.tradingagents/long_run/config.json` (a write of suspected secret
   material is refused); credentials go to the local `.env` via hidden
   prompts (role-specific key envs are honored first). Preflight is
   strictly read-only: config schema validation, the unattended safety gate
   (`safety_enabled` must be on; auto-screening and paper mode are
   re-verified after the runtime config is applied), one tiny LLM transport
   probe per role route (analysis, decision, screening, and the fallback
   route when configured — credentials can differ per role), an Alpaca
   read-only account/positions proof, and an authoritative calendar proof.
   A corrupted `active.json` is a hard stop (`ACTIVE_STATE_CORRUPT`); no
   new observation is minted on top of unreadable state.
2. **Authorization** — an explicit confirmation gate. Before it, zero
   broker mutations; after it and before the observation is created, one
   post-authorization `startup_recover()` runs (it may resubmit a missing
   PENDING/UNKNOWN order) and must report `CLEAN`. The run's runtime config
   is installed as the global execution config *before* any recovery so the
   entry gate reads this run's settings, and the installed config is
   re-validated (auto-screening on, safety on, paper-only).
3. **Daily round** (`run_daily_round`) — the journal gate first
   (a `COMPLETED` session is idempotent; `MISSED`/`STOPPED` refuse re-run
   via `SESSION_SETTLED`; corrupt/unreadable journals raise
   `STATE_CORRUPT`), then stop/window prechecks, recovery with a
   `can_submit` authority re-checked inside recovery right before any
   resubmit POST, exit-deadline enforcement, sanitized pre-round account
   snapshot (NaN/missing facts raise `SNAPSHOT_UNAVAILABLE` rather than
   becoming zeros), the daily LLM budget gate, Phase C screening (an
   execution-only resume with a proven selection skips screening instead of
   spending tokens), then serial per-symbol analysis (crash-safe
   ANALYZING markers; a provably completed run log can restore the intent)
   and per-symbol execution through the shared auto-trade path. Ambiguous
   broker outcomes (`EXECUTION_AMBIGUOUS`), a paused account
   (`ACCOUNT_PAUSED`), or an engaged kill switch stop the whole
   observation.
4. **Scheduling loop** — `sweep_missed_sessions` settles sessions missed
   while the process was down as `MISSED_PROCESS_DOWN` (never backfilled
   with stale analysis or late orders); the authoritative close proof
   settles a never-started past session as MISSED instead of running it
   overdue. Calendar/scheduling transient exceptions retry up to 3 times,
   5.0 s apart, through the injected `deps.sleep_fn`; `LongRunStop` is
   never retried; exhausted retries fail closed as `CALENDAR_UNAVAILABLE`.
   An ordinary (non-`LongRunStop`) exception escaping a round is fenced:
   the observation finalizes `STOPPED/UNEXPECTED_ROUND_ERROR` with journal
   evidence instead of killing the process silently; `KeyboardInterrupt`/
   `SystemExit` deliberately propagate.
5. **Terminal semantics** — SIGTERM/Ctrl-C yield `INTERRUPTED`; rerunning
   the command resumes the original window under the single runner lock
   (at most one execution per session). Window end finalizes `COMPLETED`
   (unrun sessions settle MISSED). Any hard stop is final `STOPPED` with a
   stop code (`RECOVERY_UNSAFE`, `PROVIDER_FAILURE`, `SCREENING_STOPPED`,
   `LLM_BUDGET_EXHAUSTED`, `SAFETY_DISABLED`, `DEADLINE_EXIT_UNSAFE`,
   `SNAPSHOT_UNAVAILABLE`, `EXECUTION_AMBIGUOUS`, `ACCOUNT_PAUSED`,
   `KILL_SWITCH`, `STATE_CORRUPT`, `CALENDAR_UNAVAILABLE`,
   `UNEXPECTED_ROUND_ERROR`, …); rerunning starts a brand-new window.
6. **Final report** — deterministic Markdown+JSON assembled only from
   persisted evidence: coverage (completed/missed/stopped/restarts), the
   daily equity series (ending values come only from a fresh
   `phase=final` snapshot captured at observation end — a failed capture
   is reported as unavailable, never backfilled with a stale round value),
   decisions/signals, screening scans and Top20 turnover, safety events,
   execution tallies, observation-scoped LLM costs, and an explicit
   `return_kind=unadjusted_account_equity_change` with its limitations.
   Reports land in `~/.tradingagents/long_run/runs/<run_id>/`.

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
| `eval_results/execution.db` | Durable intent/order/fill ledger (`execution_intents`, `orders`, `fills`, `protective_children`; schema v3). Reserved `__ACCOUNT__` intent rows store the DB↔account binding and the latest `CLEAN`/`PAUSED` reasons plus the reconciliation baseline — no second persistence system. |
| `eval_results/.execution-locks/` | Per-account stdlib OS locks. File descriptors are released by the OS after process exit/crash; no stale lease cleanup exists. |
| `~/.tradingagents/long_run/config.json` | Non-secret Phase-D observation settings (roles, notional, run time, analysts, `allow_shorts`). Secret-bearing writes are refused; credentials live in the local `.env`. |
| `~/.tradingagents/long_run/active.json` + `runner.lock` | The single active observation state (corrupt state is a hard stop) and the cross-process single-runner lock. |
| `~/.tradingagents/long_run/runs/<run_id>/` | Per-observation evidence: `manifest.json`, append-only `events.jsonl`, `account_snapshots.jsonl` (`startup`/`pre_round`/`post_round`/`final` phases), `rounds/<YYYY-MM-DD>.json` journals, `daily_reports/`, and `final_report.{md,json}`. |

## Execution recovery and authority

Scheduler/auto-trade startup calls `ExecutionService.startup_recover()` before
analysis can dispatch orders. It performs bounded `client_order_id` lookup for
`PENDING`, `SUBMITTING`, `UNKNOWN`, and `PARTIAL` rows. A definitively absent
idempotent market order may be resubmitted once with the same ID; ambiguous
lookups and non-idempotent closes stay `PAUSED`. Rows that are `SUBMITTING` or
`PARTIAL` with no provable broker fact are never replayed — recovery reports
them unsafe and the unattended run stops. Unattended callers pass a
`can_submit` authority (stop flag + observation window) that recovery
re-checks immediately before every resubmit POST, so a stop that arrives
while broker GETs are in flight can still prevent the order; a refusal
provably made no POST and leaves the row in its pre-transition status.
Each scheduler execution calls the same recovery/reconciliation gate again.

Only `CLEAN` accounts may add exposure. Missing/malformed account facts, stale
snapshot/quote, identity conflicts, unknown orders, position mismatch, and
unresolved partial fills persist a readable `PAUSED` reason. A paused account
may only reduce risk when a fresh broker position proves the side and exact
maximum quantity, no conflicting close order exists, and the paper/safety/lock
gates pass. Explicit close market orders carry the durable `client_order_id`;
POST validation/rejection is terminal, POST timeout (or any 5xx response) is
an ambiguous outcome and becomes `UNKNOWN`. Immediately before any
exposure-adding POST, the dispatcher re-validates the broker market clock
(an unprovable clock counts as closed), snapshot/quote freshness, the entry
policy, and the clipped size; a failure here cancels the durable row with
zero POSTs rather than refreshing facts and forcing the order through.

Phase B adds three deterministic gates for exposure-adding orders (verified
reducing exits keep the Phase A path): the corporate-action quarantine, the
exposure-cap evaluator (symbol/sector/gross/cash headroom including
outstanding increasing orders), and — on the recovery path — the same
quarantine and cap checks recomputed from the fresh snapshot, so a recovered
resubmit is clipped to currently provable headroom instead of reusing the
original size. An opening order on an already-held symbol is an exposure
increase governed by these caps (the Phase A implicit-HOLD shortcut was
removed per the Phase B contract).

Reconciliation baselines are frozen: normal `save_account_state()` never
moves `baseline_positions`/`baseline_at`, so unexplained broker drift keeps
pausing the account. The only way to replace a baseline is the explicit
operator maintenance API `Reconciler.rebase_baseline(snapshot, reason=...)`
(backed by `ExecutionStore.rebase_account_state`), which demands a non-empty
reason, a fresh snapshot, an existing account state, a normal reconcile that
pauses with *exactly* the `broker/local position mismatch` reason, no
non-terminal local orders, and no live broker orders of any namespace; it
then verifies a clean re-reconcile and leaves the stored baseline untouched
on any failure. Nothing in recovery, the observation loop, or the normal
execution path ever calls it automatically.

## Configuration

`tradingagents/default_config.py` is the single source of truth; the WebUI
and CLI pass overrides per run, and API keys come from `.env` /
environment (see `env.sample`). This build is paper-only: the trading client
is hard-locked to `paper=True`, `ALPACA_USE_PAPER=False` fails closed, and
only the explicit paper endpoint is allowed.

Phase B additions: `analysis_provider/model/backend_url` and
`decision_provider/model/backend_url` (all empty = legacy quick/deep),
`analysis_fallback_provider/model/backend_url` (optional failover route,
shared by the Analysis, Decision, and Screening roles within one retry
budget),
`llm_max_retries` (0-3, validated at startup), `llm_request_timeout_seconds`
(applied to every production LLM client path: legacy quick/deep, the
Analysis/Decision role clients and their Analysis fallback, Screening, memory
embeddings, and the GPT-5 Responses adapter including its bound clones; retry
count and backoff caps still bound every request in both modes),
`sec_ir_*` and `company_ir_pages` for primary sources,
`corporate_action_events` for the manual quarantine feed,
`auto_screening_enabled` + `screening_provider/model/backend_url` for the
Phase C Screening role (required, never inherited, off by default) with the
`screening_*` deterministic thresholds/constants,
`allow_shorts` (short exposure opt-in; derives the `trading_mode`
investment/trading distinction used in decision ids and prompts), and
`max_sector_exposure_pct` + `sector_mapping` (the sector cap is active once a
mapping exists; unknown sectors then refuse new risk).
Embeddings route through an OpenAI-compatible client separately overridable
with `OPENAI_EMBEDDING_MODEL` / `OPENAI_EMBEDDING_BASE_URL` /
`OPENAI_EMBEDDING_API_KEY` (e.g. a Gemini endpoint) and self-disable after
their first failure.

Phase D does not add keys to `DEFAULT_CONFIG`; its settings live in
`~/.tradingagents/long_run/config.json` (`duration_calendar_days`,
`run_time_et`, `base_trade_notional_usd`, the three role triples, the
optional fallback triple, `allow_shorts`, analysts, depth, language) and
force full-system mode at runtime: `auto_screening_enabled=True`,
safety on, paper-only, with `llm_max_retries` validated.

Dependency management: `requirements.txt` is the human-maintained top-level
spec; `requirements.lock` (generated, 222 exact pins) is the closed closure
used by CI and Docker (`pip install --no-deps -r requirements.lock && pip
check`). Do not hand-edit the lock.

## Testing conventions

- `python -m pytest tests/` — the suite is deterministic: no network, no
  live keys; external boundaries are mocked and embeddings are faked
  (58 files, ~1060 tests at the time of writing).
- `tests/test_import_no_network.py`-style guards assert that importing the
  package performs no network calls.
- CI (GitHub Actions) installs from `requirements.lock` with `pip check`
  and runs the suite offline on Python 3.11 and 3.12.
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
