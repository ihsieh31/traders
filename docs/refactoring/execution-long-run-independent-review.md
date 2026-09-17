# Independent original-source review — 2026-09-17

## Result

Compared the current worktree directly against original commit
`69388cc36a793f4eb86a79d46eb7b622e6115c61`, rather than accepting the previous
model's report. No original definition, module-level constant, entry signature,
decorator, service class state, collected baseline test, or characterized normal
workflow was found missing. One original journal-validation defect was found,
reproduced against the archived original, and fixed.

Final complete Python 3.12.13 run: **1,318 tests passed, 275 subtests passed,
0 failures/errors/skips, 2 existing dependency deprecation warnings, exit 0**,
34.85 seconds. JUnit records 1,593 cases, all successful. All 1,250 original
collected test IDs remain. All eight original fixed-input characterization
artifacts are byte-identical to this final run.

## Independent structural checks

`audit_boundaries.py` now additionally checks:

- Every original module-level initializer remains at its old entry or resolves
  through an explicit alias to the same initializer; the stop flag has one owner.
- Every moved non-nested function/method wrapper calls its unique matched
  implementation owner and forwards every original argument unchanged, including
  optional arguments. Additional collaborators resolve through the same named
  entry globals at call time.
- Original decorators and service class non-method statements/state remain.

The audit inventories 156 definitions (including closures). There are 154 exact
implementation-body matches and one daily-round baseline-flow match after
recomposing the extracted symbol block and excluding the exact, explicitly
reported new validation call. The remaining ExecutionService class has delegated
methods, so its complete class AST intentionally differs. Its non-method state,
individual methods, signatures, and runtime identity are checked separately.
There are zero structural issues. The report does **not** classify the new
daily-round validation as an identical behavior-preserving extraction.

Runtime wrapper tests exercise both required-only calls and calls with all
optional parameters supplied as identity sentinels across all 24 extracted
service methods. Original mutable instance state, static methods, module patch
seams, exception identity, six clean-process import orders, and unchanged public
contracts remain covered by the suite.

Review also checked the preserved ordering of durable outbox writes, account
locking/binding, broker lookup/adoption, fresh reconciliation between recovery
items, cancellation race checks, protection coverage, final submission gates,
per-symbol resume checkpoints, cooperative stop handling, and terminal report
persistence. Exact source comparison and the eight independent original-source
behavior traces support these checks; neither is a proof of all possible inputs.

## Concrete defect and correction

The original resumable-journal gate checked only schema and round status.
An unknown symbol status such as `TYPO` then fell through the symbol loop's
`continue`, allowing unfinished work to be skipped and the round subsequently
marked complete. Malformed symbol maps/entries also reached maintenance before
failing or being skipped.

The coordinator now calls `state.validate_resumable_symbols` for PENDING/RUNNING
existing journals before config installation, startup recovery, screening,
analysis, broker execution, or journal writes. It requires a dictionary of
nonblank symbol names and dictionary entries whose status is one of PENDING,
ANALYZING, ANALYZED, EXECUTING, DONE, FAILED. Invalid input raises the existing
`LongRunStop("STATE_CORRUPT", ...)` and preserves the original journal bytes.
Empty maps remain valid for pre-screening journals. Existing terminal-session
idempotency/refusal behavior is retained. Strategy, sizing, decision IDs,
database schema, order policy, retry rules, and valid resume logic are unchanged.

The new integration regression checks seven corrupt shapes/states, zero recovery
calls, zero execution calls, and unchanged persisted bytes. Copied into the
archived original with production source untouched, all seven subcases fail.
They all pass in the final complete worktree run.

## Evidence

| Evidence | Location | Result |
|---|---|---|
| Expanded original-source audit | `/private/tmp/tradingalpaca-independent-audit4-20260917.json` | 156 definitions, 154 exact bodies, 155 baseline-flow matches, 0 issues |
| Original defect reproduction | `/private/tmp/tradingalpaca-original-corrupt-repro-20260917/` | 7 failed subcases, expected exit 1; original source retained |
| First independent full run | `/private/tmp/tradingalpaca-independent-full-20260917/` | 1,318 passed, exit 0 |
| Final verbose full run | `/private/tmp/tradingalpaca-independent-final-20260917/` | 1,318 passed + 275 subtests, 2 warnings, exit 0, 34.85s |
| Original full-suite comparison | `/private/tmp/tradingalpaca-refactor-original-suite2-20260917/` | 1,250 baseline IDs; 0 lost |
| Original characterization comparison | `/private/tmp/tradingalpaca-refactor-original-characterization-20260917/characterization/` | All 8 final artifacts byte-identical |

Full runs contain stdout/stderr, actual return code, command/environment metadata,
JUnit, collected IDs, offline guard events, and characterization artifacts.
Final command:

```sh
.venv-p2/bin/python scripts/refactoring/run_tests.py \
  --artifacts /private/tmp/tradingalpaca-independent-final-20260917 \
  -- tests -v --tb=short
```

No early exit, warning suppression, test deletion, or fixture re-blessing was
used. `git diff --check` passes. Production connections to Alpaca/LLM providers
were not made; the suite uses isolated state and offline fakes. These results
establish tested structural and behavioral compatibility on Python 3.12, not
live-provider availability or validation on other Python versions.

## Follow-up comparison before commit — 2026-09-17

Reviewed the current worktree against `69388cc36a793f4eb86a79d46eb7b622e6115c61`
again. The source audit reports 156 definitions, 154 exact implementation bodies,
155 baseline-flow matches, and zero structural issues. All seven execution
implementation modules and all seven long-run support modules have no unresolved
global references. Checked coordinator delegation, instance/journal sharing,
account-lock scope, durable-close-before-cancel ordering, recovery authority,
symbol resume checkpoints, stop handling, and final report completion ordering.
No additional refactor defect was found; only extra trailing blank lines in
seven implementation files were removed after staging exposed them to the
whitespace check. No executable statements were changed in this follow-up.
The corrupt resumable-journal rejection described above remains an
intentional correction to original behavior, so equivalence does not extend to
those malformed inputs.

Verified the archived original entry sources against `git show` at the baseline.
All eight original characterization artifacts match both the baseline fixture
hashes and this follow-up's current artifacts byte for byte. Ran only the three
refactor test files, `TerminalJournalGateTest`, and `CallerAndExitPolicyTests`:
**77 tests and 9 subtests passed, exit 0, 2 existing dependency warnings**, in
2.67 seconds. The full suite was not rerun, as requested. Artifacts are at
`/private/tmp/tradingalpaca-user-review-focused-20260917/`; the new source audit is
`/tmp/tradingalpaca-current-review-audit-20260917.json`. `git diff --check` passes.
