# Execution / long-run refactor validation — 2026-09-17

This records the initial completed refactor. The subsequent independent review,
expanded checks, baseline journal defect correction, and final **1,318 tests +
275 subtests** acceptance supersede it where noted; see
[independent review](execution-long-run-independent-review.md).

## Result

Completed structural decomposition with preserved strategy, authority, retry,
ID, persistence and report contracts. The final Python 3.12.13 full suite ran to
completion: **1,293 passed, 268 subtests passed, 2 unchanged dependency warnings,
zero skips/failures/errors, pytest exit code 0**, in 34.55 seconds.
All 1,250 original collected tests and all 1,267 input-worktree tests remain
collected. Forty-three tests were added relative to HEAD: 17 pre-existing
characterization/contracts cases plus 26 new ownership/seam cases in this turn.

## Scope and size

| Entry | HEAD 69388cc | Input worktree | Final | Reduction from HEAD |
|---|---:|---:|---:|---:|
| execution/service.py | 3,779 lines | 3,647 lines | 1,540 lines | 59.2% |
| long_run.py | 3,103 lines | 1,770 lines | 1,571 lines | 49.4% |

The largest service implementation is now `execute` (326 lines), followed by
`enforce_exit_deadlines` (188) and `liquidate` (175). Before decomposition it was
`_execute_core` (625), `execute` (326), `_resubmit_recovered` (257). The largest
long-run implementation is now `run_daily_round` (328), followed by
`run_observation_loop` (154) and `finalize_observation` (108); originally daily
round was 520 lines, aggregation 292 and Markdown rendering 173.

Internal execution owners: requests, order_planning, dispatch, protection,
recovery, exits, intent_execution. Internal long-run owners: state, config,
sessions, preflight, round_support, symbols, reporting. No new service class,
framework, state schema, dependency container or stop-state copy. Request and
planning extraction plus most long-run support files already existed in the input
worktree; this turn finished the remaining execution decomposition, removed four
shadowed planning/quantity definitions, consolidated configuration and alert
ownership, and extracted the symbol state machine.

The final sizes slightly exceed the plan's approximate ranges because original
explicit signatures, compatibility collaborators, public locks, deadline closure
counters and readable orchestration are retained. Cohesive `_execute_core`,
recovery resubmit and symbol loop bodies remain whole in their responsibility
modules. Reducing them by changing branch semantics was not part of this task.

Production changes are limited to the two entries and their new internal modules.
Existing store/authority/policy/lifecycle/auto_trade/context, CLI, WebUI, strategy,
config defaults, requirements and CI remain unchanged. Related architecture,
boundary/inventory/audit/validation/plan documents, isolated pytest tooling and
three new test files are included. Existing test edits are:

- `test_phase_d_long_run.py`: the input worktree already used runtime results_dir
  in its analysis-log recovery fixture, matching isolated result placement.
- `test_full_review_phase2_regressions.py`: the input already disabled wheel build
  isolation, avoiding dependency downloads inside the offline packaging test.
- `test_phase_a2_broker_authority.py`: replace fragile service source substrings
  with AST checks for periodic recovery in all four mutation entries and durable
  PAUSED persistence after a post-order authority failure. The test node remains.
- Refactor contracts import orders now include the new symbols module.
- Offline runner canonicalizes its own PYTHONPATH location, fixing macOS
  /tmp -> /private/tmp guard mismatches in an archived baseline checkout.

## Complete execution evidence

Artifacts are outside the repo. Each suite directory includes complete stdout.log,
stderr.log, actual returncode.txt, run.json (command/environment/start/end),
junit.xml and collected-nodeids.json. Guard events and fixed-input canonical
characterizations are retained where applicable.

| Run | Artifacts | Result |
|---|---|---|
| Input worktree full suite | `/private/tmp/tradingalpaca-refactor-start-20260917/` | 1,263 passed, 4 failed, 268 subtests, 2 warnings; code 1. The four helper import-order cases expected execution modules not yet present. |
| Original HEAD full suite, first archive attempt | `/private/tmp/tradingalpaca-refactor-original-suite-20260917/` | 1,243 passed, 7 guard failures; macOS /tmp alias mismatch, not production failures. |
| Original HEAD full suite, corrected runner | `/private/tmp/tradingalpaca-refactor-original-suite2-20260917/` | 1,250 passed, 268 subtests, 2 warnings; code 0; 32.43s. |
| Execution focused regression | `/private/tmp/tradingalpaca-refactor-execution-focused4-20260917/` | 238 passed, 2 warnings; code 0. |
| Long-run focused regression | `/private/tmp/tradingalpaca-refactor-long-focused-20260917/` | 171 passed, 4 subtests, 2 warnings; code 0. |
| Original HEAD characterization | `/private/tmp/tradingalpaca-refactor-original-characterization-20260917/` | 13 passed, 4 deliberately deselected new-module import cases, 2 warnings; code 0. This is a focused differential run, not full-suite acceptance. |
| Ownership and differential focused regression | `/private/tmp/tradingalpaca-refactor-boundary-focused-20260917/` | 43 passed, 2 warnings; code 0. |
| First post-decomposition full suite | `/private/tmp/tradingalpaca-refactor-full-first-20260917/` | 1,267 passed, 268 subtests, 2 warnings; code 0; 34.79s. |
| Final full suite, final production/test state | `/private/tmp/tradingalpaca-refactor-final-suite-20260917/` | **1,293 passed, 268 subtests, 2 warnings; code 0; 34.55s.** |

Final command:

```sh
.venv-p2/bin/python scripts/refactoring/run_tests.py \
  --artifacts /tmp/tradingalpaca-refactor-final-suite-20260917 \
  -- tests/ -v --tb=short
```

The runner executes actual `python -m pytest`, disables ambient plugin loading
and inherited credentials/dotenv, isolates HOME/state/DB/locks/results/cache/tmp,
and blocks INET/DNS and external non-Python children. It does not use early exit,
maxfail, warning suppression, fail-only reruns or a selected subset as final
acceptance. `pip check` reports **No broken requirements found**. Intermediate
extraction-tool syntax/body assertion failures were repaired before acceptance;
their incomplete focused logs remain under execution-focused{,2,3} directories.

Original source archive is `/private/tmp/tradingalpaca-refactor-original-20260917/`.
It uses exact HEAD production source and the same isolated runner plus the two
input hermetic fixture adjustments listed above. Preserved input source is under
`/private/tmp/tradingalpaca-refactor-input-20260917/`.

## Equivalence and boundary audit

`execution-long-run-inventory.json` accounts for all **184 baseline records**:
156 definitions (including 12 nested closures), 27 module assignments and one
class flag. Each retains baseline inputs/outputs/exceptions/handlers/dependencies,
state writes/callers/tests/lock timing and now records its actual unique final
owner/location. Planned owners are retained as history, not presented as actual.

`execution-long-run-audit.json` reports **zero structural issues**. It checks
original signatures, one exact implementation owner, no duplicate definitions,
exception aliases and same-domain reverse imports. 155 of 156 definition bodies
are preserved: 154 exact AST bodies plus daily-round's exact re-expanded
contiguous symbol block. ExecutionService itself keeps class/state/public identity
with delegated methods; its whole class body necessarily changes. All 27 original
module initializer values and the exception class flag are preserved with
original bindings or explicit aliases. `_stop_requested` has only its original
owner. The mutable journal and existing service instance are passed by reference.

For every extracted service method, a runtime sentinel verifies lookup of the
new module attribute at call time, original self identity, staticmethod behavior,
argument forwarding and original global clock/snapshot/config replacements.
Additional sentinels cover old classifier/timeout-marker/request-exception seams.
Existing original-entry tests cover package exports, signatures, exception
identity, config/factory defaults and instance/class overrides. Six fresh-process
import orders include all new helpers and prove no import-time external calls.
The original lazy cross-subsystem symbols -> execution.service import is retained
for missing-intent diagnostics; neither helper tree imports its own coordinator.

All **eight** canonical characterization outputs were independently reproduced
from original HEAD without a fixture, after verifying pinned original source
hashes. Final outputs match the original bytes and fixture SHA256s exactly:

- `contracts`: old signatures, exports, status/constants and store schema.
- `protected_close`: opened/prohibited reversal/closed results, ordered GET /
  durable COMMIT / DELETE / refresh / POST trace and logical SQLite rows.
- `adopt_unknown`: existing broker order adoption without duplicate POST.
- `block_submitting`: absent broker fact remains unresolved, zero blind replay.
- `gap_between_items`: coverage loss blocks subsequent recovery mutation.
- `long_run_fresh`: runtime/journal-before-maintenance/screening/analysis/execution,
  final report and all persisted observation file bytes.
- `long_run_resume`: execution-only resume does not screen/analyze again,
  identical IDs/results/journal/report/artifacts and terminal replay behavior.
- `final_report_failure`: corrupt journal bytes preserved and terminal active
  state retained when report writing fails; no alert/clear before report success.

Only temporary root paths are normalized. No business ID, time, status, monetary
value, reason, event ordering or DB row is dropped. Broader original regressions
cover cancel cascade/kill races, signed position/quantity changes, stale/closed
zero-POST gates, current short policy, caps/quarantine/screening, partial fills,
account binding/locks, stop/window races, scoped LLM budgets/costs, journal resume,
calendar retries, MISSED/EXECUTING distinction, interruption, hard-stop
finalization and report mathematics.

## Collection, warning and guard comparison

Original 1,250 node IDs are a subset of final 1,293; input 1,267 are also a subset.
No original test was removed, renamed, skipped or deselected in the final suite.
Original/final warning categories, messages and dependency locations are the same:
websockets legacy DeprecationWarning and langgraph allowed_objects
LangChainPendingDeprecationWarning. Neither is hidden or silenced.

Original, input and final full runs each recorded five blocked socket.bind
attempts and 21 blocked non-Python subprocess attempts (for example git/uname).
These are caught optional capability probes under the inherited guard; counts
and event classes did not change. No recorded INET connect/DNS/send attempt was
allowed. This Python audit hook is not a claim of OS sandboxing native extensions.
No broker/LLM/notification action or live observation was performed by this task.

## Existing behavior retained and limits

The boundary document lists existing oddities separately: setup lock probe before
restart-count persistence and later loop lock; lookup_unknown ledger adoption
without the account lock; differing initial/recovery/close classification and
state-transition positions; report label POST calls including maintenance
DELETEs; best-effort aggregation opening the existing ExecutionStore (possible
local schema initialization) and reading live safety status/time. Unreachable
legacy protective-fallback reporting and defensive planner branches are retained.
They are not cleanup regressions, and changing them requires a separate behavior
change with its own contract/tests.

Only Python **3.12.13** was fully executed with dependencies ready. CI targets
3.11 and 3.12; local 3.11.15 lacks Alpaca and Dash dependencies, so 3.11 remains
unverified. No dependency installation, CI dispatch, push or deployment was used
to imply additional-version validation. Source equivalence and offline tests do
not prove every possible broker/provider path or replace a real 30-day observation.
Moved functions/exceptions have new implementation modules and traceback frames;
their original import aliases, signatures and exception object identity remain.

`git diff --check` passes. Source audit, full collection comparison and eight
byte-identical characterizations were checked after final production/test edits.
Final documentation edits only record these results. No Git commit or push was made.


## Review hardening validation — 2026-09-17

This amendment supersedes the baseline caveat that UNKNOWN adoption runs without
an account lock. The combined repair adds generated inventory ownership plus
its CI consistency test, file/directory durability barriers and durable unlink,
verified-account serialization and binding proof for UNKNOWN adoption, and the
scheduler always-true branch cleanup. Inventory implementation line references
are refreshed; the four changed records explicitly distinguish hardening from
baseline AST equivalence. Earlier refactor audit/differential artifacts remain
historical evidence of the extraction, not claims of hardening AST equivalence.

The full offline suite ran on Python 3.12.13 after all production and test edits:
**1363 passed, 275 subtests passed, 2 existing dependency warnings, exit code 0**
in 36.06 seconds. Command:
`python scripts/refactoring/run_tests.py -- tests -q --tb=short`.
Artifacts (runner metadata, collected IDs, JUnit, stdout/stderr and return code):
`/private/var/folders/46/3vjy25x94rvgpv7dj_1h3snm0000gn/T/tradingalpaca-refactor-jxgezb7n/`.

The 44 cases in `tests/test_review_hardening.py` cover file/rename/directory sync
ordering, newly created ancestor entries, pre/post-replace faults and process
interruption, partial serialization preserving the valid target, directory FD
cleanup, durable unlink and propagated permission/I/O/sync errors, telemetry
without fsync, real concurrent account-lock contention between two services
(manual/manual and manual/startup), lock-time ledger re-read after competing
adoption, identity/account/binding/lock/lookup failures, and scheduler default
client/injected client/rows/calendar failure across overdue/future/settled/end
exclusion/early close cases. Concurrent recovery checks require one persisted
order, one adoption and zero submit/cancel calls. A further ownership test checks
all 184 inventory symbols in the generated Markdown table and runs in existing
CI without a new workflow. The legacy lookup fixture now supplies verified
broker identity instead of an identity-less MagicMock.

Fault injection checks software ordering and failure semantics; it does not
simulate physical power loss or prove storage hardware guarantees. JSONL remains
best-effort evidence, never authority. A post-replace/unlink sync failure is
reported although the namespace change may already be visible. Account locks
remain non-blocking and same-host: a busy UNKNOWN lookup pauses for later retry.
No live broker or paid provider calls were made.
