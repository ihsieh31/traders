# Quick Start

From zero to a first multi-agent analysis in about five minutes.

## 1. Install

```bash
git clone https://github.com/huygiatrng/AlpacaTradingAgent.git
cd AlpacaTradingAgent
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

Open the printed URL (usually `http://127.0.0.1:8050`), then:

1. Enter symbols — stocks (`NVDA, AAPL`), crypto (`BTC/USD`), or a mix.
2. Pick your LLM provider/models and research depth.
3. Press **Analyze** and watch the five analysts, the bull/bear debate,
   and the risk team stream their reports live.
4. Execute the recommendation manually, or enable auto-execution and
   recurring scheduled analysis.

Prefer a terminal? `python -m cli.main` runs the same pipeline
interactively.

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

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Alpaca API key or secret not found` | `.env` not loaded or keys empty — recheck step 2. |
| `unauthorized` from Alpaca | Paper keys expired — regenerate paper keys (live keys are unsupported). |
| Analysis stalls at an analyst | Usually a rate limit; lower research depth or increase the start delays in settings. |
| Crypto symbol not found | Use the slash format: `BTC/USD`, not `BTCUSD`. |

Next: read [ARCHITECTURE.md](ARCHITECTURE.md) for how the pipeline works
inside.
