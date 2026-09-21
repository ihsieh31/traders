# True Traders vs AI-Berkshire analysis A/B implementation

## Scope and control variable

The formal experiment compares `analysis_backend=traders` with
`analysis_backend=berkshire`.  Both arms keep `analysis_profile=traders` for
legacy compatibility; `analysis_profile=berkshire` is no longer a formal A/B
arm.  The native five-analyst backend remains the default, and the Berkshire
backend adds one `Berkshire Analysis Team` node before the shared
`Build Report Context` node.

## Graph topology

Traders:

```text
START -> Parallel Analysts -> Build Report Context -> Bull/Bear
       (or the existing sequential analyst chain)
```

Berkshire:

```text
START -> Berkshire Analysis Team -> Build Report Context -> Bull/Bear
```

The downstream nodes are the same in both compiled graphs:
`Build Report Context`, `Bull Researcher`, `Bear Researcher`, `Research
Manager`, `Trader`, `Risky Analyst`, `Safe Analyst`, `Neutral Analyst`, and
`Risk Judge`.  No Berkshire role can emit `TradeIntent`; the shared downstream
Trader and Risk Manager remain the only decision path.

## Evidence contract

The A/B runner acquires an advisory campaign lock, validates configuration,
creates or loads exactly one packet at:

```text
<results-root>/evidence/<trade-date>/<symbol>/evidence_packet.json
```

The packet contains `market`, `fundamentals`, `news`, `macro`, and `social`
sections, source/error metadata, identity (`symbol`, `trade_date`), capture
time, and a canonical SHA-256 over the packet without its `sha256` field.  Both
arms receive the same path and hash.  In `frozen_evidence` mode the five native
analysts and all Berkshire roles receive no analysis tools and can read only
that packet.  Live-only sources are marked unavailable for historical as-of
dates rather than being substituted with present-day data.

## Isolation and recovery

Memory, Chroma, results, data cache, execution state, and long-run state remain
backend-isolated.  The evidence packet and campaign manifest are deliberately
shared.  `auto_trade=False` and `checkpoint_enabled=False` are enforced for
the pair.

Each pair has a durable `pair_state.json` with `NEW`, `IN_PROGRESS`, `PARTIAL`,
`COMPLETED`, and `FAILED_TERMINAL` states.  A completed arm is reused on resume;
only retryable infrastructure failures (`ProviderFailure`, timeout, connection
failure, or process interruption) can consume one of three attempts.  A
`pair_summary.json` is written only after both arms are completed.  Configuration
fingerprint or evidence-hash drift fails closed.

Shadow safety is explicit: when `auto_trade=False`, the Trader and Risk Manager
do not query broker account/position state, and the graph exposes decision-only
context.  This keeps a broker credential outage from changing an analysis A/B
result and does not alter normal runs that do not set the shadow flag.  For
OpenAI-compatible gateways that reject function-calling JSON Schema, the
optional `structured_output_method=json_mode` setting selects JSON mode while
leaving the default function-calling behavior unchanged.

## Verification record

Baseline checkout confirmed:

```text
81123b6e7abe6526147bc79b469f68ee8a9c5e64
```

The repository's default Python 3.13 executable did not include pytest.  The
focused and full suites were run with the repository's `.venv-p2` Python 3.12
environment, with `PYTHONPATH=.`.  Python 3.11 is installed on this host but
does not have the test dependencies, so the GitHub Actions matrix remains the
authoritative 3.11 result.

Recorded local results:

```text
compileall: PASS
focused backend/evidence/recovery tests: 18 passed, 2 warnings
full suite: 1400 passed, 139 skipped, 2 warnings, 294 subtests
git diff --check: PASS
```

Real decision-only smoke confirmation:

```text
symbol/date: NVDA / 2026-09-21
pair status: COMPLETED
backends: traders=HOLD, berkshire=HOLD
signal agreement: true
evidence sources: 21; recorded source errors: 0
evidence SHA-256: 17860ad31ee1f64698c5d5ae288a743606dd9e2728ea75d24d4a23dc0af452ec
analysis_input_mode: frozen_evidence
auto_trade: false
checkpoint_enabled: false
shared downstream nodes: 9/9 present in both run logs
broker/order mutation events: 0
```

The supplied `agnes-3.0-flash` token was rejected by the local gateway with
HTTP 403.  The successful smoke used the gateway-authorized
`gemini-3.5-flash-lite` model and its configured OpenAI-compatible fallback.

Required commands:

```bash
.venv-p2/bin/python -m compileall -q tradingagents cli scripts
PYTHONPATH=. .venv-p2/bin/pytest tests/test_analysis_backend_resolution.py -q
PYTHONPATH=. .venv-p2/bin/pytest tests/test_berkshire_analysis_backend.py -q
PYTHONPATH=. .venv-p2/bin/pytest tests/test_ab_frozen_evidence.py -q
PYTHONPATH=. .venv-p2/bin/pytest tests/test_analysis_ab_runner.py -q
git diff --check
PYTHONPATH=. .venv-p2/bin/pytest -q
```

## Known limits

- The smoke used the gateway's available model rather than the supplied
  `agnes-3.0-flash`, which the token was not authorized to use.
- GitHub Actions Python 3.11/3.12 results require the CI workflow to execute.
- The old prompt-profile compatibility code remains for non-formal callers and
  is explicitly not the true Berkshire backend.
