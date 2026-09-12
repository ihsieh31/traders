# Quick Start

From zero to a first multi-agent analysis in about five minutes.

> **Python ≥ 3.10 required** (3.11/3.12 recommended — this is what CI and
> the Docker image use; Phase D `long-run` refuses older interpreters).

## 1. Install

This quickstart is for the **Traders** fork
([ihsieh31/traders](https://github.com/ihsieh31/traders)) — not the
upstream [AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent)
it was forked from.

```bash
git clone https://github.com/ihsieh31/traders.git
cd traders
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure keys

```bash
cp env.sample .env   # Windows: copy env.sample .env
```

Edit `.env` — the minimum to run:

| Key | Where to get it | Required |
|---|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | free paper account at [alpaca.markets](https://alpaca.markets) | ✅ |
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) (or set `LLM_PROVIDER` to another provider) | ✅ |
| `FINNHUB_API_KEY` | [finnhub.io](https://finnhub.io) — richer news | optional |
| `FRED_API_KEY` | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) — macro analyst | optional |
| `COINDESK_API_KEY` | crypto news | optional |

> **Paper-only: keep `ALPACA_USE_PAPER=True`.** Everything works against the
> Alpaca Paper API. Live trading is disabled; `False` or a live endpoint
> fails closed with zero broker calls.

## 3. Run

```bash
python run_webui_dash.py
```

Open the printed URL (default `http://127.0.0.1:7860`; if the port is taken
the app scans for a free one), then:

1. Enter symbols — stocks (`NVDA, AAPL`), crypto (`BTC/USD`), or a mix.
2. Pick your LLM provider/models and research depth.
3. Press **Analyze** and watch the five analysts, the bull/bear debate,
   and the risk team stream their reports live.
4. Execute the recommendation manually, or enable auto-execution and
   recurring scheduled analysis.

Prefer a terminal? `python -m cli.main` runs the same pipeline
interactively.

## 3b. 30-day Paper observation (Phase D, CLI-only)

```bash
python -m cli.main long-run
```

The first run asks for any missing settings (Analysis/Decision/Screening
role provider+model, optional Analysis fallback, daily run time, trade
notional, `allow_shorts` short-exposure opt-in, missing API keys — secrets
are written to the local `.env`, never into the config file), runs a
read-only preflight with one LLM probe per role route, then asks for one
explicit Paper-test authorization before entering `RUNNING`. Only after
authorization does it run one `CLEAN`-required execution recovery and
create the observation.

- Paper only (`ALPACA_USE_PAPER=True`); 30 calendar days, US trading days
  only (authoritative Alpaca calendar; early closes run at close−30min).
- The terminal process must stay running. After a crash/reboot (or Ctrl-C),
  rerunning the same command resumes the original window (no duplicate
  orders, one session executes at most once). A session missed while the
  process was down is recorded as `MISSED_PROCESS_DOWN` and is never
  backfilled with a late (stale) analysis or order.
- Transient calendar/scheduling errors retry up to 3 times (5 s apart);
  exhausted retries fail closed. An ordinary bug inside a daily round
  finalizes the observation `STOPPED/UNEXPECTED_ROUND_ERROR` with evidence
  instead of silently killing the process.
- Hard safety/provider failures (unsafe recovery, paused account, kill
  switch, screening stop, LLM budget exhausted, ambiguous broker outcome)
  stop the observation with a partial report instead of silently
  continuing. A stopped observation is final: rerunning `long-run` starts a
  new 30-day window, it does not resume the stopped one.
- Final reports: `~/.tradingagents/long_run/runs/<run_id>/final_report.md`
  and `final_report.json` — an honest account-equity observation with its
  limitations listed, not a profitability proof.

## 4. Verify your setup

```bash
python -m pytest tests/
```

The suite is deterministic (no network, no live keys) — it should pass on
a fresh clone.

## 5. Where results live

- **Reports & audit trail**: `eval_results/<symbol>/TradingAgentsStrategy_logs/runs/`
  — every prompt, tool call, LLM call (with token usage), and the final state.
- **Decision log**: `~/.tradingagents/memory/trading_memory.md` — every
  final decision, later resolved with realized returns.
- **Phase D observation**: `~/.tradingagents/long_run/` — saved settings
  (`config.json`), the active-run state, and per-run evidence plus final
  reports under `runs/<run_id>/`.
- **Durable execution ledger**: `eval_results/execution.db` — intents,
  orders, fills, protection links, and the account's last `CLEAN`/`PAUSED`
  reconciliation state. Never delete rows to force `CLEAN`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Alpaca API key or secret not found` | `.env` not loaded or keys empty — recheck step 2. |
| `unauthorized` from Alpaca | Paper keys expired — regenerate paper keys (live keys are unsupported). |
| Analysis stalls at an analyst | Usually a rate limit; lower research depth or increase the start delays in settings. |
| Crypto symbol not found | Use the slash format: `BTC/USD`, not `BTCUSD`. |

Next: read [ARCHITECTURE.md](ARCHITECTURE.md) for how the pipeline works
inside.
