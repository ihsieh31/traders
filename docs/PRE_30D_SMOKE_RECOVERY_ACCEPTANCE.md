# Pre-30-Day Smoke / Recovery Acceptance

> **狀態更新（2026-09-22）**：本文件記錄的是收盤時執行的一次受控驗收。後續 P1/P2 修復與驗收已通過；它留下的唯一未完成項是交易時段內的真實 Paper recovery gate：經 production execution path 提交/取消後，模擬中斷並以原 `decision_id`、`client_order_id` 和 broker order 安全恢復，證明不會重複下單。這份文件的原始 `NOT ACCEPTED` 僅適用於當次收盤驗收，不表示目前仍有 P1/P2 blocker。

## Environment

- **HEAD:** `af4cec23eaf47b3e556a10361401ba289ca2e4ae`
- **Delta from supplied known HEAD `09fcbdd9d3d17a3d446871a73d0b00f1f5bedf61`:** `af4cec2 test: remove obsolete WebUI coverage` only; no production change.
- **Python:** 3.12.13 (`.venv-p2/bin/python`)
- **Test timestamp (UTC):** 2026-09-22T03:34:42.998858+00:00
- **Temporary root:** `/tmp/traders_pre30d_acceptance_20260922T033414Z/`
- **Alpaca market clock:** 2026-09-21 23:34:44--23:34:45 -04:00; `is_open=false`; next open 2026-09-22 09:30 -04:00.
- **Paper proof before any possible mutation:** repository factory constructed `TradingClient(..., paper=True)` and both live client base URLs were `https://paper-api.alpaca.markets`. No live endpoint was used.

## Routing snapshot

No secrets, tokens, keys, or Authorization values are recorded.

- Analysis provider/model: `openai` / `gemini-3.5-flash-lite` (22 successful calls in each arm)
- Decision/deep model: `agnes-3.0-flash` (Traders 3 successful calls; Berkshire 4)
- Fallback provider/model: disabled (`null` / `null`)
- Chat endpoint: `http://localhost:3000/v1`
- Embedding model/endpoint: `gemini-embedding-2-preview` / `https://generativelanguage.googleapis.com/v1beta/openai/`
- R12: **KNOWN / ACCEPTED / intentionally deferred**; no provider, model, endpoint, embedding, `.env`, or config change was made.

## Broker preflight

| Account | account_ref | Status | Equity / cash | Positions | Open orders |
| --- | --- | --- | --- | ---: | ---: |
| A | `92531ed73e59c83c` | Active | 100000 / 100000 | 0 | 0 |
| B | `792808c241c9742b` | Active | 100000 / 100000 | 0 | 0 |

The account references are different SHA-256-derived identifiers; full Alpaca account IDs were not retained.

## A/B Shadow Smoke

- Symbol/date: AAPL / 2026-09-21
- Pair status: `COMPLETED`
- Traders: `completed`, attempt 1, `decision_valid=true`, HOLD
- Berkshire: `completed`, attempt 1, `decision_valid=true`, HOLD
- Shared evidence SHA-256: `78446875a4e0cd65f10ec8c44132ea3815b7e9bfc97ffc835064ea7921f4098f`
- `pair_state.json`, `pair_summary.json`, `AB_CAMPAIGN.json`, and `evidence_packet.json` exist and parsed as JSON.
- Required state destinations are distinct across arms: `results_dir`, `memory_log_path`, `agent_memory_dir`, `data_cache_dir`, `execution_db_path`, `long_run_dir`, and `safety_state_path`.
- Alpaca A/B open orders after shadow: 0 / 0. Shadow broker mutations: **0**.
- All configured LLM models were accessed successfully: 47 Gemini calls and 7 Agnes calls total; neither arm recorded an LLM error.
- Gemini 3.5 Flash Lite tool-response: **NOT PROVEN.** The required A/B run is frozen-evidence mode, whose generated analyst prompt explicitly says “Do not call tools”; run telemetry confirms `tool_events=0` in both arms. No synthetic prompt or production-prompt modification was used to force a tool call.

**Shadow result: PASS.**

## Paper Account A Smoke

- account_ref: `92531ed73e59c83c`
- Test symbol/client order ID/initial broker status/local durable state/cancel status/post-reconcile: **NOT RUN**
- Reason: controlled stop at the execution-path blocker below, before any Paper order was submitted.
- PASS / FAIL: **NOT RUN**

## Paper Account B Smoke

- account_ref: `792808c241c9742b`
- Test symbol/client order ID/initial broker status/local durable state/cancel status/post-reconcile: **NOT RUN**
- Reason: controlled stop at the execution-path blocker below, before any Paper order was submitted.
- PASS / FAIL: **NOT RUN**

## Blocking evidence — closed-market execution path

The current `ExecutionService` cannot create the required safe closed-market test order:

1. `tradingagents/execution/requests.py::_build_market_request` constructs only `MarketOrderRequest`; its planner has no limit-price/order-type path for an opening `TradeIntent`.
2. `tradingagents/execution/dispatch.py::_validate_opening_dispatch` asks the broker clock immediately before an opening POST and returns a fail-closed error when `is_open` is not true.
3. This acceptance clock was closed. Altering the gate, replacing the broker clock, or permitting the service's market order would violate this acceptance's safety limits.

The requested B2 split permits a direct Paper connectivity/cancel smoke when `ExecutionService` has no safe limit-order path. It does **not** make C4 valid: C4 requires the original real Paper recovery order to be submitted through `ExecutionService`. That invariant cannot be exercised safely while the market is closed. Per the required blocker rule, testing stopped here; no production code was changed or bypassed.

## Offline Recovery

- analysis_completed resume / no repeated LLM / PARTIAL blocks next date / same-day resume: **NOT RUN** after the controlled stop.
- Existing repository tests contain the fake-broker/fault-injection coverage, but they were not rerun after the blocker because this is an acceptance run, not a substitute for the required real Paper path.
- PASS / FAIL: **NOT RUN**

## Real Paper Recovery

- decision_id / original client_order_id / original broker_order_id: **NOT CREATED**
- orders before restart / after restart: 0 / 0 for acceptance-prefixed orders
- duplicate broker order created: **NO** (no recovery order was submitted)
- cancel status / post-reconcile: **NOT RUN**
- PASS / FAIL: **NOT RUN**

## Final Broker Cleanup

- Account A `pre30d-smoke` orders remaining: 0
- Account B `pre30d-smoke` orders remaining: 0
- Account A `pre30d-recovery` orders remaining: 0
- Account B `pre30d-recovery` orders remaining: 0
- Unexpected test positions: none; both accounts remained flat at the last broker read.

## Tests

- `python -m pytest tests/ -q`: **NOT RUN** after controlled stop.
- `python -m pytest docs/review_2026_09_22/test_final_review_contracts.py -q --tb=short`: **NOT RUN** after controlled stop.
- R12: **KNOWN / ACCEPTED** and excluded from the rejection decision as instructed.

## Final decision for this acceptance attempt

**PENDING — real Paper recovery gate.**

Required real Paper submit/cancel and restart/replay invariants could not be performed through the production execution path under the mandated closed-market, non-marketable-limit safety constraints. No formal 30-day campaign was started by this acceptance attempt. P1/P2 remediation has since been accepted; the remaining task is to repeat this gate during an open regular session without weakening the market-clock or production execution safeguards.
