# Remaining execution / long-run refactor — 2026-09-17

## Scope and expected result

Continue the existing `refactor/execution-long-run-modules` worktree at HEAD
`69388cc36a793f4eb86a79d46eb7b622e6115c61`. The input already contains partial
request/planning and long-run support extraction; preserve those edits.
Production changes are limited to `execution/service.py`, its internal extracted
modules, `long_run.py`, and `long_run_support/`. Related tests, test tooling and
architecture/boundary/validation documents are included. No strategy, schema,
ID, broker policy, retry or dependency changes. No broker/LLM calls, deployment,
Git commit or push are part of this task.

The original 184-symbol inventory remains the baseline contract. Every original
entry/signature must remain resolvable; every implementation must have one
owner. Ordinary functions and explicit wrappers preserve call-time collaborators
and instance/class overrides. No mixins, dynamic method installation, module
proxy, copied globals or second service/dependency/stop-state owner.

Expected entry sizes: service approximately 1,200–1,500 lines including explicit
compatibility methods; long_run approximately 1,400–1,500 lines including its
existing compatibility functions. These reflect keeping locks and readable
orchestration in the entry files, not a mandatory size target. Large cohesive
execution bodies are moved intact; simplifying branch semantics is out of scope.

## Logic understood before production edits

Execution validates intent and rejects prohibited SHORT before maintenance.
Deadline exits can invalidate the analysis, requiring reanalysis before entry.
Reversals run the close phase only, retaining deterministic outbox identity while
canceling the opposite opening row. Opening-only policy, quarantine, screening,
quotes and caps do not obstruct proven reducing closes. Broker snapshot is the
sole account/position authority. All mutation paths establish account binding
under the account lock. Outbox commit precedes POST and protection DELETE.
Protective cancellation is rechecked with fresh snapshots before/after each
DELETE, tolerates sibling cascade by broker facts, and checks kill switch after
GET. Side/quantity changes abandon prepared rows; protection gaps persist PAUSED
until fresh facts prove flat/protected/closing. Stops alone count as protection;
target siblings do not, siblings cover only their largest remaining quantity.
Recovery adopts broker-existing rows, resubmits only provably absent PENDING or
UNKNOWN rows, never blindly replays SUBMITTING/PARTIAL/protective children.
Each adoption/resubmit refreshes, reconciles and checks coverage before the next
item. Resubmit uses current short policy, screening, quarantine, caps, original
entry policy/stop, market clock, freshness and final authority. Refused final
recovery authority preserves the row's original durable state. Structured 4xx
except 408 proves rejection; unprovable POST outcomes remain UNKNOWN.

Long-run retains one stop flag, dependency dataclass and lifecycle coordinator.
Journal validation precedes runtime installation and recovery. Empty fresh
journal precedes maintenance; maintenance evidence is saved before failure.
Preflight is read-only; recovery follows explicit external observation
permission. Execution-only resume uses persisted selection/intent without new
LLM work; fresh screening and each new analysis check budget. Symbol states:
PENDING -> ANALYZING -> ANALYZED -> EXECUTING -> DONE; ANALYZING reuses only a
completed log bound to this observation; EXECUTING recovers with identical ID;
DONE/FAILED do not repeat. Stop/window checkpoints remain at their exact places.
Calendar failures retry a bounded three attempts with injected sleep. Closed or
past sessions settle MISSED while unresolved execution stays recovery-owned.
Hard stops settle STOPPED; signal interruption keeps original resumable window.
Finalization preserves corrupt journal bytes, writes terminal state/manifest,
attempts fresh final snapshot, renders reports, logs/alerts, then clears active.
Report failure before clear keeps terminal active state. Ending equity is from
final snapshot only; evidence, drawdown, journal maintenance, scoped LLM costs,
DB reference and safety status retain the original calculation/limitations.

## Implementation ownership

| Module | Responsibility / side effects |
|---|---|
| service | Public execute/deadline/startup/liquidation/status/lookup orchestration; account locks, existing instance state, explicit compatibility seams |
| order_planning | Intent schema validation, logical specs, protective price interpretation, signed position comparison; no broker mutations |
| requests | SDK requests, SDK-unavailable fallback, exception HTTP classification; no POST |
| dispatch | Market-clock/freshness/entry/final-authority checks and initial submit; original durable transitions / POST classification |
| protection | Ownership, cancel-race snapshots/DELETE, stop coverage and persistent protection gaps; same store and original self methods |
| recovery | Lookup/adopt/resubmit/reconcile/recovery queue and exposure cap bridge; original store, GET/POST order and maintenance counts |
| exits | Prepared close abandonment, close outbox/core and maintenance summary; original durable commit and close POST |
| intent_execution | Entire _execute_core: risk sizing, policy/caps, outbox, replay, per-spec dispatch and result shaping |
| long_run_support/config | Config construction/validation plus runtime installation and unattended global execution validation |
| long_run_support/state | Atomic JSON, JSONL, paths, active state, journals, runner lock; no new persistence owner |
| long_run_support/sessions | Authoritative calendar, missed-session settlement, bounded retry |
| long_run_support/preflight | Lazy default factories, role probes, sanitized snapshots, post-authorization recovery |
| long_run_support/round_support | Intent/log recovery, screening audit, auto-trade bridge, execution summary/hard-stop |
| long_run_support/symbols | Cohesive per-symbol resume/analysis/execution state machine; journal/log callbacks and stop authority supplied by coordinator |
| long_run_support/reporting | Existing evidence aggregation/render/write and failure-isolated alert adapter |

Exceptions retain one definition and explicit original aliases. All extracted
functions receive original patch-sensitive globals by named keyword arguments;
implementation modules never import the coordinator back. Cross-method calls
continue through the original self. Deadline loop and its closures remain in
service to keep the account lock, counters and exceptions in one readable scope.

## Execution and acceptance sequence

1. Preserve input outside repo; finish full isolated input suite. Also execute
   original tracked baseline suite in a temporary archive with the same offline
   runner and the two already-existing hermetic test adjustments. Keep stdout,
   stderr, actual pytest exit code, JUnit, collected IDs, guard events and Python
   version. Expected incomplete-input import failures are classified separately.
2. Extract execution by responsibility with unchanged function bodies, exact
   original signatures and call-time collaborators. Remove shadowed original
   planning copies. Check each body, lazy imports and every old method seam.
3. Consolidate long-run runtime validation under config, alerts under reporting;
   extract the per-symbol loop with its closures intact, passing explicit
   collaborators. Keep stop state, round/scheduler/finalize/resume in main.
4. Run focused characterization, old-entry signatures/exception identity,
   patch sentinels, imports and safety/order regressions. Audit all symbols,
   unique owners, callback timing, state ownership and boundaries. Extend tests
   for the new symbols module and static extraction/body equivalence.
5. Run the final full suite to completion without early exit or suppression.
   Fix regressions and rerun the entire suite after final changes. Compare
   collection, warnings/skips, baseline trace/SQLite/artifacts and final syntax
   audit. Publish final ownership inventory, boundary contracts and validation
   report with paths and limits. Python versions not actually run remain marked
   unverified; offline simulations are not a real 30-day broker observation.

## Risks and controls

The main risk is altered global lookup after moving methods. Explicit named
collaborators from the old wrapper plus runtime sentinels address it. The second
risk is changing cancellation/recovery/checkpoint order; identical bodies and
fixed-input broker traces/persisted rows complement the full regression suite.
The symbol block adds a call frame but keeps original closures and branches,
passing the mutable journal by reference and returning only its stop reason.
No lock acquisition/release moves. Existing behavior oddities (unreachable
protective fallback reporting, narrow recovery rejection semantics, setup lock
probe, signal state semantics) are documented rather than silently repaired.
