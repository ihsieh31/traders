# Quick Start

## 1. Install

```bash
git clone https://github.com/ihsieh31/traders.git
cd traders
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+ is required; 3.11 or 3.12 is recommended.

## 2. Configure

```bash
cp env.sample .env
```

Minimum `.env` values:

```env
TRADINGBUFFETT_ALPACA_API_KEY=your_paper_key
TRADINGBUFFETT_ALPACA_SECRET_KEY=your_paper_secret
TRADINGBUFFETT_ALPACA_USE_PAPER=True
TRADINGBUFFETT_LLM_PROVIDER=openai
TRADINGBUFFETT_OPENAI_API_KEY=your_openai_key
```

This is a paper-only build. Live credentials or `ALPACA_USE_PAPER=False` fail closed.

## 3. Choose a command

```bash
python -m cli.main --help
```

Interactive analysis:

```bash
python -m cli.main analyze
```

30-day unattended Paper observation:

```bash
python -m cli.main long-run
```

Traders × Berkshire shadow A/B pair:

```bash
python scripts/run_analysis_ab.py \
  --symbol NVDA \
  --date 2026-09-21 \
  --results-root ~/.tradingbuffett/results/ab
```

Use the same A/B results root for all 30 days. The first pair creates `AB_CAMPAIGN.json`; later pairs fail closed if shared settings or the analyst set change.

## 4. Verify

```bash
python -m compileall -q tradingagents cli scripts
python -m pytest -q
```

The test suite is offline and must not use real keys or broker mutation.

## 5. Output locations

| Artifact | Default path |
|---|---|
| Run audit logs | `~/.tradingbuffett/results/<symbol>/TradingAgentsStrategy_logs/runs/` |
| Decision memory | `~/.tradingbuffett/memory/trading_memory.md` |
| Agent vector memory | `~/.tradingbuffett/memory/agent_memory/` |
| Execution ledger | `~/.tradingbuffett/execution/execution.sqlite3` |
| Long-run state | `~/.tradingbuffett/long_run/` |
| A/B results | `~/.tradingbuffett/results/ab/` |

## Troubleshooting

| Symptom | Action |
|---|---|
| Alpaca key missing | Confirm the two namespaced Paper keys are present in `.env`. |
| Alpaca unauthorized | Regenerate Paper keys; live keys are unsupported. |
| Provider stops a run | Check the run audit event, credentials, timeout and bounded retry settings. |
| Memory is empty | Confirm an embedding-capable OpenAI-compatible endpoint is configured. |
| A/B campaign invariant violation | Restore the original config/analyst set or start a new results root. |
| `.pair_in_progress` exists | Audit the interrupted pair before removing the marker; do not blindly rerun it. |

Continue with [README.md](README.md) and [ARCHITECTURE.md](ARCHITECTURE.md).
