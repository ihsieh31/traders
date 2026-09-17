# Boundary Document — `execution/service.py` and `long_run.py` Decomposition

Status: implemented ownership and boundary contract. The baseline descriptions
and original line numbers below intentionally refer to HEAD 69388cc; actual
final owners and line numbers are in `execution-long-run-inventory.json`.
`execution-long-run-audit.json` verifies all 156 definition/closure signatures
and bodies (the daily symbol block is re-expanded exactly), 27 module bindings
and one class flag. Runtime validation is in `execution-long-run-validation.md`.

Baseline of record:
`git` commit `69388cc36a793f4eb86a79d46eb7b622e6115c61` ("fix: stop-proof
protection coverage, recovery gap ordering, prohibited SHORT reversal").
Line numbers below are the HEAD 69388cc baseline line numbers.

Scope of this document: `tradingagents/execution/service.py` (3,779 lines) and
`tradingagents/long_run.py` (3,103 lines). Sibling modules that are NOT being
split — `tradingagents/execution/{__init__,store,authority,policy,context,lifecycle,auto_trade}.py` —
are treated as fixed dependencies; their public names appear here only as
imported symbols.

Companion artifact (same owner): `docs/refactoring/execution-long-run-inventory.json`
— exhaustive per-symbol inventory (old line/signature, planned new ownership,
inputs/outputs/exceptions/dependencies/callers/side effects/lock timing/tests)
derived from the two baseline source files; caller/test references are also
read from tracked Python files at the same baseline. Return expressions, call
sites and exception handlers are static evidence, not runtime type/effect or
coverage proofs. The inventory includes 184 records: 69 execution records
(including the class flag `stale_position_transition`) and 115 long-run records.

The scope is structural extraction with preserved behavior, schemas, IDs,
report format, authority and retry policy. Existing effects are inventoried,
not declared pure by moving files. The original planned owners remain in the
inventory as `planned_owner`; `new_owner` records the actual implementation.

---

## 1. Current architecture (as found at HEAD 69388cc)

### 1.1 `tradingagents/execution/service.py`

Single Phase-A execution entry. Trust boundary: validate `TradeIntent` ->
durable outbox COMMIT -> safety -> broker submit. DB commit failure => zero
broker calls. Missing/invalid intent => fail-closed with zero broker calls.
Header "ponytail ceiling" note: one service + one SQLite store + one authority
module; no facade/adapter/repository, scheduler, retry, or lease framework.

Module layout:

- Lines 1–58: docstring, imports, `__all__`, `_DEFAULT_DB`, `_TIMEOUT_MARKERS`.
- Lines 61–116: exception classification helpers (`_exception_http_status`,
  `_is_ambiguous_error`, `_definitive_rejection`) — R07/N11 semantics.
- Lines 119–153: `StaleRecoveryPositionError`, maintenance collector/summary
  (N15).
- Lines 156–173: DB path resolution (F13).
- Lines 176–356: intent validation and order planning
  (`validate_trade_intent`, `_planned_order_specs`, `_resolve_qty`,
  `_resolve_protective_prices`).
- Lines 358–505: request building and final-boundary authority
  (`RequestBuildError`, `_submit_authority_error`, `_build_protective_request`,
  `_build_market_request`, `_get_execution_config`, `_position_unchanged`).
- Lines 507–599: `_evaluate_opening_caps` (Phase B deterministic caps bridge).
- Lines 602–1098: `ExecutionService` protection/exit helpers
  (`_DeadlineGapOutcome` at 602; ownership verification, protection
  cancellation with race hardening, protection-gap invariant F04, persisted
  gap recovery, general coverage invariant R09/N07).
- Lines 1101–1288: `enforce_exit_deadlines` (scheduled exit due/kill path).
- Lines 1292–1617: `execute` (orchestrating entry: guards, lock, recovery,
  close-phase F04 sequencing, entry gates, submit, post-reconcile).
- Lines 1619–2243: `_execute_core` (outbox commit, idempotent replay, sizing,
  caps, protective legs, per-spec submit loop, result shaping).
- Lines 2246–2279: `_paused_result`, `_verified_reducing_exit`.
- Lines 2281–2803: recovery machinery (`_lookup_for_recovery`,
  `_adopt_recovery_order`, `_recovery_crossing_block`, `_resubmit_recovered`,
  `_reconcile_snapshot`, `_recover_locked`).
- Lines 2805–2913: `startup_recover`, `account_status`,
  `_attach_quarantine_status`.
- Lines 2915–3029: `_market_clock_closed`, `_validate_opening_dispatch` (R05/R13).
- Lines 3031–3278: `_submit_one` (single spec POST incl. bracket/OTO).
- Lines 3282–3698: liquidation (`liquidate`, `_prepare_liquidation_outbox`,
  `_liquidate_core`).
- Lines 3702–3730: `lookup_unknown` (bounded UNKNOWN adopt).
- Lines 3733–3779: module-level wrappers `_service`, `execute_trade_intent`,
  `liquidate_position`.

### 1.2 `tradingagents/long_run.py`

Phase D — 30-day unattended Alpaca Paper observation in one module. Every
broker mutation goes through `ExecutionService`; universe decisions through
Phase C `prepare_screening_round`; analysis through `TradingAgentsGraph`.

Module layout:

- Lines 1–54: docstring, imports, schema/duration/time/analyst constants,
  `PROVIDERS_REQUIRING_URL`, `PLACEHOLDER_MARKERS`, `LongRunStop`.
- Lines 60–84: path helpers (`base_dir`, `config_path`, `active_path`,
  `lock_path`, `run_dir`, `round_path`).
- Lines 91–106: secret scan (`_looks_placeholder`, `scan_files_for_secrets`).
- Lines 113–181: atomic JSON state, events, single-runner lock
  (`atomic_write_json`, `read_json`, `append_jsonl`, `utc_now_iso`,
  `RunnerLockBusy`, `runner_lock`).
- Lines 188–432: config (defaults, load/save with secret refusal, validation,
  runtime build, `git_baseline_commit`).
- Lines 439–570: scheduling (`eastern_now`, `effective_target_for_session`,
  `fetch_session_dates`, `next_due_session`).
- Lines 577–757: observation state (`session_close_et`,
  `mark_session_missed_after_close`, `new_observation_state`,
  `load_active_state`, `save_active_state`, `clear_active_state`, `log_event`,
  `completed_sessions`, `TERMINAL_ROUND_STATUSES`, `settled_sessions`).
- Lines 784–880: `LongRunDeps` injectable seams + default implementations
  (`_default_screening`, `_default_graph_factory`,
  `_default_execution_service`, `_default_broker_client`, `REDACTED_API_KEY`,
  `_default_llm_probe`).
- Lines 887–975: `SnapshotUnavailable`, `capture_account_snapshot`.
- Lines 982–1256: preflight and recovery gates (`run_preflight`,
  `_apply_runtime_config`, `_validate_long_run_execution_config`,
  `run_post_authorization_recovery`).
- Lines 1263–1414: round journal primitives and symbol-status constants
  (PENDING/ANALYZING/ANALYZED/EXECUTING/DONE/FAILED), maintenance summary
  shape, journal new/save/load, `_normalize_intent`,
  `_recover_intent_from_run_log`.
- Lines 1370–1389: F10 stop/window checkpoint reasons and `_control_stop_reason`.
- Lines 1392–1433: `summarize_execution_result`, `_build_graph_config`.
- Lines 1436–1955: `run_daily_round` (the daily loop: journal gate, R01 config
  apply, recovery + deadline maintenance N15, snapshots, F19 budget gating,
  screening with audit scope F15, per-symbol state machine cases A–E with F10
  checkpoints, post-round snapshot, daily report, COMPLETED finalize).
- Lines 1958–2104: screening audit scope, `_execute_intent`,
  `_record_execution`, `_check_execution_hard_stop` (N08 codes).
- Lines 2111–2172: stop flag (`_stop_requested`, `_install_signal_handlers`,
  `request_stop`), `sweep_missed_sessions`.
- Lines 2179–2375: scheduler bounded retry (F-03) and `run_observation_loop`.
- Lines 2382–2948: final reporting (`_load_snapshots`, `_load_rounds`,
  `_sum_unrealized`, `compute_drawdown`, `aggregate_final_report`, `_fmt_pct`,
  `_fmt_usd`, `render_final_markdown`, `write_final_report`).
- Lines 2951–2964: `send_long_run_alert`.
- Lines 2967–3074: `finalize_observation`.
- Lines 3077–3103: `setup_or_resume`.

---

## 2. Final package boundaries

### 2.1 `tradingagents/execution/` new modules

| Module | Responsibility |
|---|---|
| `service.py` | Public execution, recovery and liquidation coordination; account locks and compatibility wrappers. |
| `requests.py` | Request construction, sizing and error classification; SDK fallback. |
| `order_planning.py` | Intent validation, order specs and protective prices. |
| `dispatch.py` | Final submit authority and initial order dispatch. |
| `protection.py` | Protection ownership, coverage and cancellation races. |
| `recovery.py` | Bounded lookup, adoption, resubmission, reconciliation and caps. |
| `exits.py` | Prepared-row abandonment, liquidation cores and maintenance summaries. The deadline loop stays in the service. |
| `intent_execution.py` | Sizing, replay, outbox preparation and per-spec execution. |

Symbol ownership is generated from inventory `new_owner` in section 2.4.
These responsibility summaries do not maintain a separate symbol-owner list.

Cross-module call rules inside `execution/`:

- `service.py` orchestrators must call the moved helpers through direct module
  imports (eager, top-level) or the existing lazy-import pattern — never by
  duplicating logic.
- `store.py`, `authority.py`, `policy.py`, `context.py`, `auto_trade.py`,
  `lifecycle.py` are NOT touched by this refactor. Their exports
  (`ExecutionStore`, `canonical_decision_id`, `client_order_id_for`,
  `is_valid_order_transition`, `AccountExecutionLock`, `AccountLockBusy`,
  `BrokerAuthorityError`, `BrokerSnapshot`, `Reconciler`,
  `ReconciliationResult`, `broker_status_to_local`, `capture_broker_snapshot`,
  `capture_quote`, `SNAPSHOT_TTL_SECONDS`, `validate_freshness`,
  `validate_quote`, `utc_now`, `entry_check`) keep their current homes.
- `tradingagents/execution/__init__.py` re-exports must keep resolving exactly
  the same objects (it currently re-exports `ExecutionService`,
  `execute_trade_intent`, `liquidate_position`, `resolve_execution_db_path`
  from `service`). The package `__init__` may keep importing those names from
  `service.py` while `service.py` itself imports them from the new modules.

### 2.2 Seam policy for patch-sensitive module globals (mandatory)

Several tests bind module attributes of `tradingagents.execution.service` by
name. The refactor must keep these seams working at the same import path
without simply re-exporting the patched function into a second dead binding:

- `tradingagents.execution.service._get_execution_config` is monkeypatched by
  `tests/test_unattended_safety_regressions.py` (5 sites), `tests/test_phase_c_remediation.py`
  (6 sites), `tests/test_phase_c_screening.py` (1 site), `tests/test_phase_b_caps.py` (3 sites).
- `tradingagents.execution.service._build_market_request` is patched by
  `tests/test_phase_a2_broker_authority.py` (line 581) and exercised by the
  R10 tests in `tests/test_30d_minimal_repairs_20260912.py`.
- `tradingagents.execution.service.utc_now` is patched by
  `tests/test_strategy_consistency.py` (3 sites) for exit-deadline aging.
- `tradingagents.execution.service.capture_broker_snapshot` is patched by
  `tests/test_paper_readiness_20260912_regressions.py` (line 272) for
  generation/freshness races.

Required mechanics: retain explicit-signature wrappers in `service.py` for
moved functions and methods. Each wrapper delegates by module attribute and
passes the required collaborators by name, resolving them from the original
module at call time. The implementation invokes callbacks at the original
point in its unchanged body. Methods continue cross-responsibility calls
through the original `self` methods, preserving instance and class overrides.
This rule applies to private helpers as well as the four commonly patched
module names above; there is no whitelist that silently removes other seams.

Exceptions have one definition and explicit aliases at the original import
path. Immutable imported types may be shared; callable collaborators whose
lookup is observable are passed explicitly. Implementations must not import
`service.py` at runtime, copy a module's globals, use `exec`/`sys.modules`
tricks, or install methods dynamically. A simple re-export of a function is
not sufficient when its original callers expect patchable global lookup.
The detailed final symbol inventory distinguishes retained implementations,
compatibility wrappers, and exception aliases.

Lazy imports preserved verbatim (they are load-order and hermeticity seams, in
both files):

- service.py: `tradingagents.agents.schemas` (inside `validate_trade_intent`,
  `_resolve_protective_prices`), `tradingagents.dataflows.alpaca_utils`
  (`_resolve_qty`, `_execute_core` risk sizing, `_default_broker_factory`),
  `tradingagents.dataflows.config` (`_get_execution_config`,
  `_resolve_protective_prices`, `_execute_core` bracket config),
  `tradingagents.safety` (`get_safety_guard` — multiple sites),
  `tradingagents.risk.exposure` (`_evaluate_opening_caps`),
  `tradingagents.risk.corporate_actions` (`quarantine_gate`),
  `tradingagents.screening.gate` (`execute` Phase C gate, `_resubmit_recovered`),
  `alpaca.trading.requests`/`alpaca.trading.enums` (`_build_protective_request`,
  `_build_market_request` fallback, `enforce_exit_deadlines`,
  `_reconcile_snapshot`), `.policy entry_check` (three sites),
  `.lifecycle due_positions` (`enforce_exit_deadlines`).
- long_run.py: `tradingagents.screening.sessions.eastern_now`,
  `tradingagents.dataflows.market_calendar` (multiple),
  `tradingagents.default_config.DEFAULT_CONFIG`, `tradingagents.llm_clients.*`
  (retry/roles/factory), `tradingagents.screening.llm`,
  `tradingagents.screening.pipeline.prepare_screening_round`,
  `tradingagents.graph.trading_graph.TradingAgentsGraph`,
  `tradingagents.execution.ExecutionService`, `tradingagents.execution.service.validate_trade_intent`,
  `tradingagents.execution` (`ExecutionStore`, `resolve_execution_db_path`),
  `tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client`,
  `tradingagents.dataflows.config` (`get_config`/`set_config`/`get_alpaca_use_paper`),
  `tradingagents.safety` (`get_safety_guard`, `reset_safety_guard`),
  `tradingagents.safety.guardrails.OBSERVATION_HALT_CODES`,
  `tradingagents.run_logger` (`load_final_state_snapshot`, `get_run_audit_logger`),
  `tradingagents.daily_report.write_daily_report`, `tradingagents.llm_cost`,
  `tradingagents.alerts`, `tradingagents.agents.schemas`
  (`trade_intent_action`), `tradingagents.llm_clients.retry.ProviderFailure`.

These lazy imports must remain lazy in the new modules (same functions, same
exception swallowing) — hoisting them to module import time would change
offline-test behavior (the alpaca-SDK-optional path in `_build_market_request`
deliberately returns a dict payload when the SDK import fails).

### 2.3 `tradingagents/long_run_support/` new package

| Module | Responsibility |
|---|---|
| `long_run.py` | Round, scheduler, finalization and resume coordination; stop state and compatibility wrappers. |
| `state.py` | Paths, durable JSON state, best-effort telemetry, journals and runner lock. |
| `config.py` | Configuration, runtime installation and unattended validation. |
| `sessions.py` | Authoritative calendar scheduling, missed sessions and bounded retries. |
| `preflight.py` | Default factories, read-only probes and authorized recovery. |
| `round_support.py` | Intent recovery, graph setup, screening and execution bridge. |
| `symbols.py` | Daily per-symbol state machine with shared journal and graph lifecycle. |
| `reporting.py` | Evidence aggregation, report persistence and alerts. |

Symbol ownership is generated from inventory `new_owner` in section 2.4.

No contracts module is needed: shared exception identity is preserved through
explicit aliases (`RunnerLockBusy`, `SnapshotUnavailable`); `LongRunStop` is
supplied by name to helpers. Constants supplied by the coordinator do not acquire
second policy owners. Existing imported stdlib names remain compatible with the
old module surfaces; immutable annotation types are imports, not new state.

Rules inherited from the approved plan:

- `long_run.py` keeps the round observation loop, finalization, resume,
  stop flag, and `LongRunDeps` default seams. Tests patch
  `tradingagents.long_run._default_broker_client`,
  `tradingagents.long_run._default_execution_service`,
  `tradingagents.long_run.run_observation_loop`,
  `tradingagents.long_run.run_preflight`,
  `tradingagents.long_run.run_post_authorization_recovery`,
  `tradingagents.long_run.new_observation_state`, and
  `tradingagents.long_run._stop_requested` (module-global write via
  `monkeypatch.setattr(lr, "_stop_requested", False)`). All of these stay
  attributes of `tradingagents.long_run` with live semantics; where the
  implementation moves to `long_run_support`, the retained `long_run.py`
  binding must remain the one the caller paths resolve (same seam mechanism
  rule as 2.2 — default-seam indirection keeps resolving through the
  `long_run` module namespace at call time).
- Lazy imports in long_run.py (listed in 2.2) stay lazy at their new homes.
- The runner lock (`runner_lock`, `RunnerLockBusy`) stays with
  `setup_or_resume` ownership: implement it in `state.py` (file/lock
  primitives) and keep `long_run.runner_lock` as the live binding that
  `setup_or_resume` and the CLI tests resolve.

### 2.4 Public API stability contract

Names that must remain importable from their current modules after the
refactor (callers verified in source):

- `tradingagents.execution` package `__init__`: `ExecutionStore`,
  `ExecutionService`, `canonical_decision_id`, `client_order_id_for`,
  `intent_id_for_decision`, `is_valid_order_transition`, `order_id_for_client`,
  `execute_trade_intent`, `liquidate_position`, `AccountExecutionLock`,
  `AccountLockBusy`, `BrokerAuthorityError`, `BrokerFill`, `BrokerOrder`,
  `BrokerPosition`, `BrokerQuote`, `BrokerSnapshot`, `Reconciler`,
  `ReconciliationResult`, `capture_broker_snapshot`, `capture_quote`,
  `get_with_retry`, `validate_freshness`, `validate_quote`.
- `tradingagents.execution.service`: everything in its `__all__`
  (`ExecutionService`, `execute_trade_intent`, `liquidate_position`,
  `is_valid_order_transition`) plus the names other modules import today:
  `resolve_execution_db_path`, `validate_trade_intent` (long_run.py line 1864,
  tests), `_build_market_request` (patch seam), `_get_execution_config`
  (patch seam), `utc_now` (patch seam), `capture_broker_snapshot` (patch seam),
  `StaleRecoveryPositionError`, `RequestBuildError`.
- `tradingagents.long_run`: every public name used by `cli/main.py` and tests
  (`run_preflight`, `run_post_authorization_recovery`, `new_observation_state`,
  `run_observation_loop`, `setup_or_resume`, `runner_lock`, `RunnerLockBusy`,
  `LongRunStop`, `LongRunDeps`, `default_long_run_config`,
  `load_long_run_config`, `save_long_run_config`, `missing_config_fields`,
  `validate_long_run_config`, `parse_run_time_et`, `build_runtime_config`,
  `fetch_session_dates`, `mark_session_missed_after_close`,
  `sweep_missed_sessions`, `run_daily_round`, `new_round_journal`,
  `save_round_journal`, `load_round_journal`, `settled_sessions`,
  `completed_sessions`, `TERMINAL_ROUND_STATUSES`, `load_active_state`,
  `save_active_state`, `clear_active_state`, `active_path`, `round_path`,
  `base_dir`, `capture_account_snapshot`, `request_stop`, `PROVIDERS_REQUIRING_URL`,
  `atomic_write_json`, `read_json`, `log_event`, `finalize_observation`,
  `aggregate_final_report`, `write_final_report`, `render_final_markdown`,
  `compute_drawdown`, `send_long_run_alert`, `summarize_execution_result`,
  `SYMBOL_*` constants, `STOP_REASON_*` constants,
  `_default_broker_client`, `_default_execution_service`, `_default_llm_probe`,
  `_default_screening`, `_default_graph_factory`, `_validate_long_run_execution_config`,
  `_recover_intent_from_run_log`, `_stop_requested`, `request_stop`,
  `_looks_placeholder`, `_valid_backend_url`, `_unattended_safety_error`,
  `REDACTED_API_KEY`, `LONG_RUN_SCHEMA_VERSION`, `git_baseline_commit`,
  `scan_files_for_secrets`, `effective_target_for_session`, `session_close_et`,
  `eastern_now`, `utc_now_iso`, `append_jsonl`, `run_post_authorization_recovery`).

Function and method compatibility entries use explicit wrappers with named
call-time collaborators (see 2.2); class aliases preserve exception identity.
There is one implementation body per responsibility, not two independent
copies of a helper or mutable state.

---

### 2.4 Generated symbol ownership

`execution-long-run-inventory.json` is the machine-readable source of truth.
Regenerate this table with `python scripts/refactoring/sync_boundary_ownership.py`.
The regular CI pytest suite checks the entire table against every inventory
record; edit inventory ownership when moving symbols, then regenerate this view.

<!-- inventory-ownership:start -->
| Implementation owner | Baseline symbols |
|---|---|
| `tradingagents/execution/dispatch.py` | `ExecutionService._market_clock_closed`, `ExecutionService._submit_one`, `ExecutionService._validate_opening_dispatch`, `_get_execution_config`, `_submit_authority_error` |
| `tradingagents/execution/exits.py` | `ExecutionService._abandon_prepared_rows`, `ExecutionService._liquidate_core`, `ExecutionService._paused_result`, `ExecutionService._prepare_liquidation_outbox`, `_DeadlineGapOutcome`, `_DeadlineGapOutcome.__init__`, `_maintenance_summary`, `_new_maintenance_collector` |
| `tradingagents/execution/intent_execution.py` | `ExecutionService._execute_core` |
| `tradingagents/execution/order_planning.py` | `_planned_order_specs`, `_position_unchanged`, `_resolve_protective_prices`, `validate_trade_intent` |
| `tradingagents/execution/protection.py` | `ExecutionService._apply_protection_coverage`, `ExecutionService._broker_order_live`, `ExecutionService._cancel_owned_close_protections`, `ExecutionService._cancel_protection_with_race_check`, `ExecutionService._evaluate_protection_gap`, `ExecutionService._program_owned_live_reducing_qty`, `ExecutionService._protection_coverage_gaps`, `ExecutionService._recover_persisted_protection_gaps`, `ExecutionService._verified_reducing_exit`, `ExecutionService._verify_owned_close_protections`, `StaleRecoveryPositionError`, `StaleRecoveryPositionError.stale_position_transition` |
| `tradingagents/execution/recovery.py` | `ExecutionService._adopt_recovery_order`, `ExecutionService._lookup_for_recovery`, `ExecutionService._reconcile_snapshot`, `ExecutionService._recover_locked`, `ExecutionService._recover_locked._blocking_anomalies`, `ExecutionService._recover_locked._broker_clients`, `ExecutionService._recovery_crossing_block`, `ExecutionService._resubmit_recovered`, `_evaluate_opening_caps` |
| `tradingagents/execution/requests.py` | `RequestBuildError`, `_TIMEOUT_MARKERS`, `_build_market_request`, `_build_protective_request`, `_definitive_rejection`, `_exception_http_status`, `_is_ambiguous_error`, `_resolve_qty` |
| `tradingagents/execution/service.py` | `ExecutionService`, `ExecutionService.__init__`, `ExecutionService._attach_quarantine_status`, `ExecutionService._default_broker_factory`, `ExecutionService._quarantine_rejection`, `ExecutionService.account_status`, `ExecutionService.enforce_exit_deadlines`, `ExecutionService.enforce_exit_deadlines._deadline_maintenance`, `ExecutionService.enforce_exit_deadlines._fail_with_gap_check`, `ExecutionService.execute`, `ExecutionService.liquidate`, `ExecutionService.lookup_unknown`, `ExecutionService.quarantine_gate`, `ExecutionService.startup_recover`, `ExecutionService.store`, `_DEFAULT_DB`, `__all__`, `_default_db_path`, `_service`, `execute_trade_intent`, `liquidate_position`, `resolve_execution_db_path` |
| `tradingagents/long_run.py` | `DEFAULT_DURATION_CALENDAR_DAYS`, `DEFAULT_RUN_TIME_ET`, `LONG_RUN_SCHEMA_VERSION`, `LongRunDeps`, `LongRunStop`, `LongRunStop.__init__`, `PLACEHOLDER_MARKERS`, `PROVIDERS_REQUIRING_URL`, `REDACTED_API_KEY`, `SCHEDULER_RETRY_ATTEMPTS`, `SCHEDULER_RETRY_DELAY_SECONDS`, `SCREENING_AUDIT_SYMBOL`, `SESSION_CLOSE_ET`, `SESSION_OPEN_ET`, `STOP_REASON_NONE`, `STOP_REASON_STOP_REQUESTED`, `STOP_REASON_WINDOW_ENDED`, `SYMBOL_ANALYZED`, `SYMBOL_ANALYZING`, `SYMBOL_DONE`, `SYMBOL_EXECUTING`, `SYMBOL_FAILED`, `SYMBOL_PENDING`, `VALID_ANALYSTS`, `VALID_RESEARCH_DEPTHS`, `_control_stop_reason`, `_install_signal_handlers`, `_install_signal_handlers._handler`, `_stop_requested`, `finalize_observation`, `request_stop`, `run_daily_round`, `run_daily_round._record_maintenance`, `run_daily_round._recovery_can_submit`, `run_observation_loop`, `run_observation_loop._scheduling_operation`, `setup_or_resume` |
| `tradingagents/long_run_support/config.py` | `_apply_runtime_config`, `_unattended_safety_error`, `_valid_backend_url`, `_validate_long_run_execution_config`, `_validate_unattended_safety`, `build_runtime_config`, `default_long_run_config`, `git_baseline_commit`, `load_long_run_config`, `missing_config_fields`, `parse_run_time_et`, `save_long_run_config`, `validate_long_run_config` |
| `tradingagents/long_run_support/preflight.py` | `SnapshotUnavailable`, `_default_broker_client`, `_default_execution_service`, `_default_graph_factory`, `_default_llm_probe`, `_default_screening`, `capture_account_snapshot`, `capture_account_snapshot._get`, `capture_account_snapshot._num`, `run_post_authorization_recovery`, `run_preflight`, `run_preflight._check` |
| `tradingagents/long_run_support/reporting.py` | `_fmt_pct`, `_fmt_usd`, `_load_rounds`, `_load_snapshots`, `_sum_unrealized`, `aggregate_final_report`, `compute_drawdown`, `render_final_markdown`, `send_long_run_alert`, `write_final_report` |
| `tradingagents/long_run_support/round_support.py` | `_build_graph_config`, `_check_execution_hard_stop`, `_execute_intent`, `_normalize_intent`, `_record_execution`, `_recover_intent_from_run_log`, `_screening_with_audit_scope`, `summarize_execution_result` |
| `tradingagents/long_run_support/sessions.py` | `_run_scheduler_operation_with_retry`, `eastern_now`, `effective_target_for_session`, `fetch_session_dates`, `mark_session_missed_after_close`, `next_due_session`, `session_close_et`, `sweep_missed_sessions` |
| `tradingagents/long_run_support/state.py` | `RunnerLockBusy`, `TERMINAL_ROUND_STATUSES`, `_empty_maintenance_summary`, `_looks_placeholder`, `active_path`, `append_jsonl`, `atomic_write_json`, `base_dir`, `clear_active_state`, `completed_sessions`, `config_path`, `load_active_state`, `load_round_journal`, `lock_path`, `log_event`, `new_observation_state`, `new_round_journal`, `read_json`, `round_path`, `run_dir`, `runner_lock`, `save_active_state`, `save_round_journal`, `scan_files_for_secrets`, `settled_sessions`, `utc_now_iso` |
| `tradingagents/long_run_support/symbols.py` | `run_daily_round._execute_symbol` |
<!-- inventory-ownership:end -->


## 3. Temporal invariants (must hold before and after; tests listed are the
existing owners — the refactor must keep them passing, it does not invent new
proof of equivalence)

### 3.1 Execution service invariants

| # | Invariant | Enforced by (today) | Existing test owners (static evidence) |
|---|---|---|---|
| T-EX-1 | Durable-commit-before-POST: an intent row exists in SQLite before any broker call for that decision; commit failure means zero broker calls. | `execute`/`_execute_core` (outbox `create_outbox` at service.py:1961 before `_submit_one`), `_prepare_liquidation_outbox` (service.py:3458) | test_phase_a1_execution_foundation.py `DurableOutboxTests::test_db_commit_failure_makes_zero_broker_calls`, `OrderIdempotencyTests::test_commit_then_crash_before_submit_leaves_recoverable_pending` |
| T-EX-2 | One account lock around every mutating entry (execute/deadlines/liquidate/startup_recover); a second holder gets `AccountLockBusy`, never interleaved mutations. | `AccountExecutionLock` acquisition at service.py:1136, 1413, 2824, 3302 | test_phase_a1 `SingleEntryTests`, test_phase_a2 `FreshnessAndLockTests::test_same_account_lock_has_one_owner_and_recovers_after_exit`, `test_two_processes_compete_and_crashed_owner_releases_lock`; test_unattended F08 |
| T-EX-3 | Account binding (F06): `ensure_account_binding` before any recovery/execution mutation under the lock. | `_recover_locked` head (service.py:2706-2711) | test_unattended `F06AccountBindingTests` (t1–t6), `F06ConcurrentFirstBindTests` |
| T-EX-4 | Submit-boundary authority (R02/N03): `can_submit` re-checked after every blocking GET, immediately before the POST; refusal leaves the row at its pre-transition status (PENDING/UNKNOWN), never REJECTED/CANCELED fakes. | `_submit_authority_error` call sites (service.py:2570, 3126, 3605); row-status rules in `_resubmit_recovered` | test_30d_minimal_repairs `test_R01_*` (5 tests); test_final_paper_readiness_blockers `R02NoPostStateSemanticsTests` (4 tests); test_paper_readiness_20260912 `test_N03_*` |
| T-EX-5 | Final-POST dispatch revalidation (R05/R13): broker clock GET first, then snapshot/quote freshness, then entry policy — all before any opening POST; failure = CANCELED, zero POSTs. | `_validate_opening_dispatch` (service.py:2940) called from `_submit_one` (3110) and `_resubmit_recovered` (2551) | test_final_paper_readiness_blockers `R05ClockBeforeFreshnessTests` (t1–t3) |
| T-EX-6 | Kill switch checked after the fresh snapshot GET and before each DELETE/POST (N04); engaged kill switch means DELETE=0 / POST=0 and the protection stays live. | `_cancel_protection_with_race_check` (service.py:754-766), `_submit_authority_error` | test_paper_readiness_20260912 `test_N04_*` (3 tests) |
| T-EX-7 | Ambiguity classification (N11): only structured 4xx (never 408, never text) proves rejection; timeouts/resets/5xx/undecodable-200 stay UNKNOWN and reconcile by client_order_id; no auto-retry of a possibly-accepted POST. | `_exception_http_status`/`_definitive_rejection`/`_is_ambiguous_error` used in `_submit_one`, `_resubmit_recovered`, `_liquidate_core` | test_phase_a2 `test_post_timeout_is_unknown_without_retry_then_lookup_adopts`, `test_pre_submit_ambiguous_failure_marks_unknown_without_crash`; test_paper_readiness_20260912 `test_N11_*`; test_30d_minimal_repairs `test_R10_*`, `test_A6_*` |
| T-EX-8 | Recovery loop refresh (F02/F04): after each adoption/resubmit the snapshot is refreshed and re-reconciled; non-CLEAN facts stop the round before further mutations. | `_recover_locked` body (service.py:2754-2803) | test_full_review_phase1 `test_second_recovery_sees_first_resubmit`, `test_adoption_visible_to_next_recovery_item`, `test_refresh_failure_stops_before_second_submit`; test_unattended `F04RecoveryStopTests` |
| T-EX-9 | Unresolved SUBMITTING rows are never auto-resubmitted (F07); they may be adopted via bounded client-id lookup or keep the account paused. | `_recover_locked` recoverable_reasons (service.py:2715-2724), `break` at 2776 | test_unattended `F07SubmittingLookupTests` (t1–t3) |
| T-EX-10 | Recovery crossing block (N02): a recovery resubmit that would cross/reverse the fresh broker position is CANCELED pre-POST with `stale_position_transition`. | `_recovery_crossing_block` (service.py:2336), `StaleRecoveryPositionError` | test_paper_readiness_20260912 `test_N02_*` (3 tests) |
| T-EX-11 | Short policy re-derivation at recovery (N01): current config decides; close buys exempt. | `_resubmit_recovered` block (service.py:2424-2436) | test_paper_readiness_20260912 `test_N01_*` (3 tests) |
| T-EX-12 | F04 close sequencing: verify ownership -> durable close commit -> cancel proven protections -> refresh -> verified-exit check -> submit; durable commit failure leaves protections untouched; prepared rows are abandoned (CANCELED) when the position closed/changed during the race. | `liquidate` (service.py:3282), `execute` close phase (1433-1494), `enforce_exit_deadlines` (1184-1245) | test_full_review_phase1 `test_failed_close_persists_protection_gap_pause`, `test_durable_commit_failure_cancels_nothing`, `test_close_accepted_live_no_false_gap`, `test_persisted_gap_survives_restart_until_proven_safe`; test_phase_a2 `test_liquidation_requires_verified_position_and_no_conflicting_close` |
| T-EX-13 | Protection-gap invariant: after this program cancels protections, a still-live uncovered position PAUSEs with a stable `PROTECTION_GAP:` reason; persisted gaps survive restarts until broker facts prove safety; coverage (R09/N07) judged only for program-protected entries. | `_evaluate_protection_gap` (service.py:866), `_recover_persisted_protection_gaps` (921), `_protection_coverage_gaps` (975), `_apply_protection_coverage` (1074) | test_full_review_phase1 `test_failed_close_persists_protection_gap_pause`, `test_persisted_gap_survives_restart_until_proven_safe`; test_paper_readiness_20260912 `test_N07_*` (5 tests) |
| T-EX-14 | Deadline exits run before any new entry in `execute` (deadline result short-circuits with a hold/`reanalysis`-required response). | `execute` lines 1336-1343 | test_phase_a2 `test_scheduler_has_startup_gate_and_execution_has_periodic_gate`; test_unattended `F01DeadlineShortTests` |
| T-EX-15 | Reversal (R04/F05): a close-then-open intent executes ONLY its close phase; open leg durably CANCELED with `reversal_open_deferred`; success judged on close phase; `reanalysis_required` set. | `execute` lines 1345-1352, `_execute_core` lines 1830-1866, 2025-2063, 2194-2203 | test_unattended `F05ReversalCloseOnlyTests` (t1–t6) |
| T-EX-16 | Stale-position precondition (R14): opening specs with an intent `current_position` that no longer matches the fresh broker side fail closed with `stale_position_transition` (reversal close phases exempt). | `_execute_core` lines 1840-1866 | test_unattended `F02StaleRunTests` |
| T-EX-17 | Deadline close safety: `signed-qty` semantics (`_position_unchanged` NEW-R1) so unchanged SHORT closes are not misjudged; deadline close requires refreshed qty/side match and verified reducing exit; each symbol's cancellation count is isolated (F01). | `_position_unchanged` (493), `enforce_exit_deadlines` per-symbol body (1187-1270) | test_unattended `F01DeadlineShortTests` (t1–t4); test_full_review_phase1 `test_liquidate_short_*` (4 tests) |
| T-EX-18 | Caps/quarantine/entry gates re-run on recovery resubmits with fresh facts; blocked rows become CANCELED with reasons; headroom never reused (F02-generation of facts). | `_resubmit_recovered` lines 2437-2517 | test_full_review_phase1 `test_service_fails_closed_without_required_quote`, `test_service_values_outstanding_with_own_quote`; test_phase_c_remediation/quarantine suites |
| T-EX-19 | Idempotency: same `decision_id` dedupes via `canonical_decision_id` + `client_order_id_for`; repeat liquidation without explicit decision_id is its own decision (never silently deduped). | `_execute_core` replay block (1982-1997), `_prepare_liquidation_outbox` did policy (3473-3480) | test_phase_a1 `DecisionIdempotencyTests`, `OrderIdempotencyTests::test_client_order_id_stable_across_restart_and_roles_distinct`; test_phase_a2 `test_repeat_liquidation_without_decision_id_is_not_silently_deduped` |
| T-EX-20 | UNKNOWN adoption is bounded (3 attempts, explicit not-found proof) and never re-POSTs; `lookup_unknown` adopts observed state only. | `_lookup_for_recovery` (service.py:2281), `lookup_unknown` (3702) | test_phase_a1 `UnknownRecoveryTests::test_lookup_adopts_broker_order_without_second_logical_order`; test_phase_a2 `test_unknown_resubmits_once_only_after_three_explicit_not_found_lookups` |

### 3.2 Long-run invariants

| # | Invariant | Enforced by (today) | Existing test owners (static evidence) |
|---|---|---|---|
| T-LR-1 | Single runner: one Phase-D process per machine/user via `fcntl` lock; busy lock mutates nothing. | `runner_lock` (long_run.py:158), `setup_or_resume` (3077) | test_phase_d_long_run `LockAndAtomicTest::test_second_runner_blocked_while_first_holds`, `test_no_active_observation_refuses_resume`, `test_second_runner_refused`; test_paper_readiness_20260912 `test_N12_*` |
| T-LR-2 | Active-state corruption is a hard stop, never a fresh run (run_id identity protects decision idempotency). | `load_active_state` (long_run.py:683) | test_phase_d_long_run `test_truncated_json_fails_closed`, `test_missing_run_id_fails_closed`, `test_blank_run_id_fails_closed`, `test_terminal_completed_state_fails_closed`, `test_terminal_stopped_state_fails_closed`; test_phase_d CLI tests |
| T-LR-3 | Settled sessions (`TERMINAL_ROUND_STATUSES` = COMPLETED/MISSED/STOPPED) are never re-picked or re-run; corrupt journals are STATE_CORRUPT, never overwritten. | `next_due_session` settled filter (498), `run_daily_round` journal gate (1474-1496), `sweep_missed_sessions` (2132), `mark_session_missed_after_close` (591) | test_phase_d `SchedulingTest::test_missed_session_is_never_rescheduled`, `TerminalJournalGateTest` (4 tests), `LateResumeTest::test_late_resume_never_backfills_a_missed_session`; test_30d_minimal_repairs `test_R04_*` |
| T-LR-4 | Cooperative stop/window (F10/R02): `_control_stop_reason` gates every new analysis and every broker mutation; stop/window refusals inside recovery leave rows durably unresolved; STOP_REQUESTED keeps the journal resumable (symbols PENDING, not FAILED); WINDOW_ENDED records `WINDOW_ENDED_DURING_ROUND` and never trades the analysis. | `_control_stop_reason` (1375), checkpoints 1–6 inside `run_daily_round` (1502-1727, 1838-1861) | test_full_review_phase1 `test_signal_stop_during_analysis_blocks_execution`, `test_window_end_during_analysis_blocks_execution`, `test_analyzed_resume_checks_stop_before_execution`, `test_request_stop_is_universal_across_modes`; test_30d_minimal_repairs `test_R01_*` |
| T-LR-5 | Round journal is the resume authority; EXECUTING re-entry reuses the identical decision id (durable dedupe, no second POST); ANALYZING re-enters only with a provably matching run log of THIS observation (F12/H-09 metadata keys `analysis_source` + `long_run_observation_id`). | `run_daily_round` cases A–E (1714-1901), `_recover_intent_from_run_log` (1332) | test_phase_d `DailyRoundTest::test_crash_at_executing_resumes_without_duplicate_post`, `test_analyzing_with_completed_run_log_recovers_intent`; test_full_review_phase1 `test_recovery_selects_matching_observation_not_newer_manual`, `test_newer_other_observation_run_ignored`, `test_no_metadata_match_means_no_recovery` |
| T-LR-6 | Budget gating (F19): new LLM work (screening or PENDING/ANALYZING symbols) is gated by `check_llm_budget`; execution-only resumes skip screening and never re-enter the pipeline; budget 0 is normalized to the 20M cap and persisted (B-02) with an audit event. | `run_daily_round` F19 blocks (1589-1635, 1811-1827), `_validate_long_run_execution_config` (1118) | test_full_review_phase2 `test_exhausted_budget_before_round_stops_with_zero_llm_work`, `test_analyzed_only_resume_skips_screening_pipeline`, `test_executing_only_resume_skips_screening_and_recovers`, `test_execution_only_resume_with_exhausted_budget_and_no_cache`, `test_journal_without_screening_summary_never_skips`; test_30d_minimal_repairs `test_R03_*` |
| T-LR-7 | Scheduler bounded retry (F-03): exactly 3 attempts / 5s delay for calendar-backed operations; LongRunStop is never retried; persistent failure finalizes STOPPED with CALENDAR_UNAVAILABLE and a full report. | `_run_scheduler_operation_with_retry` (2183), loop wiring (2276-2336) | test_pre_30d_reliability_repairs `SchedulerBoundedRetryTest` (5 tests) |
| T-LR-8 | Round-exception fence (F-04): an ordinary exception inside a round finalizes STOPPED with journal evidence (`UNEXPECTED_ROUND_ERROR`); BaseException (KeyboardInterrupt/SystemExit) propagates. | `run_observation_loop` except path (2359-2375) | test_pre_30d_reliability_repairs `RoundExceptionFenceTest` (4 tests) |
| T-LR-9 | R13 late-session settlement: a never-started session whose authoritative close has passed is settled MISSED, never run as an overdue round; H-04 unreadable journal = STATE_CORRUPT not overwrite. | `mark_session_missed_after_close` (591-658) | test_pre_30d_reliability_repairs `test_t03_5_mark_session_missed_after_close_retries_transient`; test_30d_minimal_repairs `test_R04_*` |
| T-LR-10 | R01 config-before-recovery: the round's runtime is applied to the global config (merge, not replace) and re-validated BEFORE `startup_recover` in both `run_daily_round` and `run_post_authorization_recovery`; paper mode + safety + auto-screening are re-proven after apply. | `_apply_runtime_config` (1094), `_validate_long_run_execution_config` (1118), call order in `run_daily_round` (1508-1515) and `run_post_authorization_recovery` (1229-1231) | test_final_paper_readiness_blockers `R01WebUIConfigBeforeRecoveryTests`; test_30d_minimal_repairs `test_R03_budget_out_of_range_refuses_startup`; test_full_review_phase1 `test_run_preflight_never_mutates`, `test_post_authorization_recovery_runs_and_creates_nothing_on_failure` |
| T-LR-11 | Generation/freshness of scheduler actors (F02): a stale-generation loop/scheduler cannot reset the new run's state or dispatch work (WebUI path); `_stop_requested`/generation semantics per test F02 t1–t7. | stop-flag handling + `request_stop` (2111-2129), loop (2222) | test_unattended `F02GenerationTests`, `F02StaleRunTests`, `F02StaleSchedulerTests`, `F04SubmittingBlocksQueueTests` (WebUI `_RecordingApp` fixture) |
| T-LR-12 | Safety policy: unattended runs require `safety_enabled=True` (P2-01) before any probe; kill switch and circuit breakers hard-stop the observation with stable codes (N08: `EXECUTION_AMBIGUOUS`, `ACCOUNT_PAUSED`, `SAFETY_CIRCUIT_BREAKER`, `KILL_SWITCH`, `PROVIDER_FAILURE`); single-order refusals do not stop. | `_unattended_safety_error` (274), `run_preflight` gate (996-999), `_check_execution_hard_stop` (2055) | test_phase_d `ConfigValidationTest::test_safety_enabled_true_is_the_only_allowed_value`, `test_preflight_refuses_disabled_safety_before_any_probe`, `test_engaged_kill_switch_stops_the_round`; test_paper_readiness_20260912 `test_N08_*` (6 tests) |
| T-LR-13 | Preflight is read-only (no `startup_recover`, no test orders); recovery runs only after authorization; a failed post-auth recovery creates no observation state. | `run_preflight` (982), `run_post_authorization_recovery` (1210) | test_full_review_phase1 `test_run_preflight_never_mutates`, `test_post_authorization_recovery_runs_and_creates_nothing_on_failure` |
| T-LR-14 | Finalization: finalize records unrun sessions as MISSED (COMPLETED path) or STOPPED (stopped path) preserving partial evidence and unreadable journals verbatim; one fresh `phase="final"` snapshot; ending equity only from that snapshot (F14); reports + manifest persisted; active state cleared last; alerts failure-isolated. | `finalize_observation` (2967-3074), `aggregate_final_report` (2451) | test_phase_d `SchedulerStopFinalizationTest` (3 tests), `FinalReportMathTest`; test_full_review_phase2 `test_final_report_uses_fresh_final_snapshot`, `test_final_snapshot_failure_reports_unknown` |
| T-LR-15 | N15 maintenance accounting: recovery/deadline mutation summaries are persisted immediately after each call (`_record_maintenance`) and counted once at the broker boundary; the final report sums per-symbol execution + maintenance tallies. | `_record_maintenance` (1536), `aggregate_final_report` exec_tally (2534-2584) | test_paper_readiness_20260912 `test_N15_recovery_bracket_post_counts_once_in_the_final_report`, `test_N15_adoption_only_recovery_counts_zero_mutations`, `test_N15_deadline_close_cancel_and_post_counts_match_the_report` |
| T-LR-16 | Window semantics: observation end date is excluded; overdue sessions resume before advancing; nothing is scheduled after `ends_at`. | `next_due_session` (498-570), loop (2222-2375) | test_phase_d `SchedulingTest::test_observation_end_date_is_excluded`, `test_next_due_prefers_overdue_session`, `test_completed_session_skipped`, `FakeClockSimulationTest::test_full_window_simulation_finalizes_with_reports`, `test_resume_continues_original_window` |

### 3.3 Cross-cutting temporal ordering (both files)

1. Order inside a single mutating entry (must remain identical after the
   split): identity/binding proof -> recovery -> gates -> durable commit ->
   cancel/protect work -> POST -> adopt/transition -> post-reconcile ->
   protection-gap decision.
2. Every "blocking GET" (snapshot, quote, clock, order lookup) happens BEFORE
   the final freshness/authority proof, never after (R05 ordering in
   `_validate_opening_dispatch`; N03/N04 in `_resubmit_recovered` and
   `_cancel_protection_with_race_check`).
3. Journal/state writes are atomic and durable (`atomic_write_json` tmp+file fsync+replace+directory fsync) and
   precede the risky work they give evidence about (journal created before
   recovery in `run_daily_round`; `analysis_run_ref` written before
   `propagate`).
4. Stop checks are cooperative checkpoints only: they never abort an in-flight
   SDK call; they guarantee no NEW analysis or broker order begins after the
   stop/window is observed.
5. Reports retain persisted evidence plus the existing runtime DB/safety/time reference reads (journals, snapshots,
   events, run logs, execution.db reference); reporting never mutates broker
   state.

---

## 4. Side effects, lock timing, and global state (boundary-relevant)

### 4.1 `ExecutionService` mutation surfaces

- Broker POST/DELETE/GET: only via `broker.submit_order`
  (`_submit_one` service.py:3031, `_resubmit_recovered` 2610,
  `_liquidate_core` 3625), `broker.cancel_order_by_id`
  (`_cancel_protection_with_race_check` 768), and read paths
  (`capture_broker_snapshot`, `capture_quote`, `get_order_by_id`,
  `get_order_by_client_order_id`/`get_order_by_client_id`, `get_clock`).
- SQLite writes: `create_outbox`, `transition_order`, `update_intent_state`,
  `sync_order_from_broker`, `register_protective_child`, `save_account_state`,
  `ensure_account_binding` — all through `self._store` under the
  `AccountExecutionLock` for mutating entries, including UNKNOWN adoption;
  `account_status` is the read-side exception.
- Filesystem: `AccountExecutionLock` (flock under the lock dir),
  quarantine ledger (via `build_quarantine_gate`), execution DB file.
- Env reads at call time: `TRADINGAGENTS_EXECUTION_DB`
  (`resolve_execution_db_path`), `TRADINGAGENTS_SNAPSHOT_TTL_SECONDS`
  (`_validate_opening_dispatch`).

### 4.2 Long-run mutation surfaces

- Files under `base_dir()`: `config.json`, `active.json`, `runner.lock`,
  `runs/<run_id>/{events.jsonl,manifest.json,account_snapshots.jsonl,rounds/*.json,daily_reports/*,final_report.{json,md}}`,
  base `events.jsonl` (budget normalization event).
- Global process state: `tradingagents.dataflows.config.set_config` (R01,
  `_apply_runtime_config`, `_build_graph_config`), safety-guard reset on budget
  normalization, SIGTERM handler installing `_stop_requested`.
- Broker: none directly — all via `ExecutionService`
  (`startup_recover`, `enforce_exit_deadlines`, `execute_auto_trade`) and
  read-only `capture_account_snapshot` through `deps.broker_client_factory`.
- Lock timing: `runner_lock` is held for the whole
  `run_observation_loop` (setup_or_resume line 3102-3103); the account lock is
  taken inside `ExecutionService` per mutation; `_apply_runtime_config` must
  complete before any recovery/execution path (R01 ordering).

### 4.3 Module-level mutable state

- `tradingagents.long_run._stop_requested` (bool, set by the SIGTERM handler
  closure, read by `_control_stop_reason`, `request_stop`,
  `run_observation_loop`); monkeypatched by tests
  (`test_paper_readiness_20260912_regressions.py:55`) and central to F02
  generation semantics. It MUST remain the `tradingagents.long_run` module
  attribute that the handler and `_control_stop_reason` read (a moved copy
  would break `monkeypatch.setattr(lr, "_stop_requested", ...)` and the
  F02 stale-run tests).
- `ExecutionService` instance state: `_store`, `_broker_factory`,
  `_quote_factory`, `_quarantine_gate` (lazily built, cached). `__all__` is a mutable export list; `_DEFAULT_DB` and `_TIMEOUT_MARKERS`
  are immutable str/tuple constants. Long-run policy/status set constants are
  mutable Python sets: share their objects rather than copying them across
  modules. The inventory includes all module assignments, not only flags.

---

## 5. Monkeypatch seams and lazy imports (conservation requirements)

The refactor MUST preserve, with same-name module attributes:

1. `tradingagents.long_run` function attributes patched by tests: `_default_broker_client`,
   `_default_execution_service`, `run_observation_loop`, `run_preflight`,
   `run_post_authorization_recovery`, `new_observation_state`, `runner_lock`,
   `fetch_session_dates`, `_stop_requested` (see section 2.3 mapping; tests in
   test_phase_d_long_run.py lines 407-504 and test_paper_readiness_20260912_regressions.py).
2. `tradingagents.execution.service` module attributes patched by tests:
   `_get_execution_config`, `_build_market_request`, `utc_now`,
   `capture_broker_snapshot` (see section 2.2). For these, keep a live
   same-name binding on `service` so patching affects the tested code path;
   do not leave a second orphan binding for any other name.
3. `LongRunDeps` remains the primary injection surface (screening_fn,
   graph_factory, execution_service_factory, broker_client_factory,
   llm_probe_fn, calendar_client, calendar_rows, now_fn, sleep_fn, alert_fn);
   the `_default_*` functions remain the seams used when deps fields are None.
4. All lazy imports listed in 2.2 stay lazy at their new locations, including
   the SDK-optional fallback in `_build_market_request` (alpaca ImportError ->
   dict payload) and `validate_trade_intent`'s schema-import failure path.
5. `docs/paper_readiness_*_repros.py` scripts import from
   `tradingagents.execution.service` directly; they are evidence artifacts, not
   production callers, but the import surface above keeps them loadable.

---

## 6. Baseline caveats / observations (hardening amendments noted below)

1. `setup_or_resume` (long_run.py:3088-3103) probes the runner lock, bumps
   and persists `restart_count` BEFORE acquiring the lock that guards the
   loop. The N12 tests (`test_second_runner_never_overwrites_the_active_state`,
   `test_race_inside_the_lock_refuses_without_recovery_or_writes`) pin the
   current sequencing; any ownership change must keep this exact order or the
   regression semantics break.
2. Baseline `lookup_unknown` adopted without the account lock. The hardening
   amendment now verifies broker identity, acquires the shared account lock,
   re-reads the row, verifies a fresh snapshot and DB account binding, then
   performs bounded lookup/adoption. Busy or unprovable authority pauses
   without adoption; this entry never POSTs.
3. `enforce_exit_deadlines` returns `broker_calls` as
   `cancellation_calls + submit POSTs` while some early `BrokerAuthorityError`
   paths inside the loop have already mutated the ledger (prepared rows
   abandoned) — the maintenance summary counts DELETEs and POSTs only, per
   N15's "counted at the mutation boundary" definition.
4. `_resubmit_recovered` (service.py:2580-2595) rebuilds the protective
   request AFTER the `SUBMITTING` transition and after the final authority
   check; a `RequestBuildError` there becomes REJECTED (documented R10/A6
   semantics, pinned by `test_R10_*`/`test_A6_*`).
5. `_execute_core` guard verdict bookkeeping assigns `safety_error` in both
   the `except` and `else` branches before reading it. The earlier draft's
   claim of an unbound-variable race was incorrect; this is not a known
   defect. Preserve both branches and their existing result classification.
6. `_service`, `execute_trade_intent`, `liquidate_position` are thin
   compatibility wrappers (service.py:3733-3779) kept for the legacy call
   shape (`test_wrapper_delegates_to_durable_service`,
   `test_production_callers_use_single_execution_entry`).
7. `completed_sessions` (long_run.py:747) is retained but superseded by
   `settled_sessions` for scheduling; the final report uses `TERMINAL_ROUND_STATUSES`.
   It remains exported for resume-state introspection.
8. `aggregate_final_report`'s renderer labels `exe['broker_calls']` as
   "broker POST calls" while it also includes recovery/deadline cancel calls
   via `maintenance_broker_calls`; report format must not change in this
   refactor (test_N15_* pins the numbers).
9. `webui/callbacks/*` and `tradingagents/dataflows/alpaca_utils.py` import
   `ExecutionService` lazily at call sites; `tradingagents/screening/gate.py`
   is imported by service (Phase C gate) — the dependency direction
   `service -> screening.gate -> config` must not be inverted by the split.
10. `tests/test_phase_a1_execution_foundation.py::test_direct_mutation_helpers_are_removed`
    asserts certain legacy helper names do not exist on the service module;
    preserve that absence list. Compatibility wrappers preserve names that
    already exist at the baseline; they must not resurrect removed legacy
    helpers. This is not a whitelist limiting preservation to four seams.

---

## 7. Final collaborator, result, exception and side-effect contracts

The exhaustive 184-entry inventory retains each original input/output/raise,
handler, direct dependency, state write, caller/test reference and lock scope.
Final ownership/signature/AST evidence is added to every record; it is not a
claim of exhaustive runtime path coverage. The following summarizes inter-module
boundaries rather than requiring readers to reverse-engineer the wrappers.

| Boundary | Inputs / result | Failure / effects / lock contract |
|---|---|---|
| service -> planning | Original intent, dollar amount, signal/asset and warning list; validated dict/error tuple, specs list, optional price dict or position bool | Schema/config stay lazy; invalid intent returns error with zero POST; warning list may mutate; no lock acquisition |
| service -> requests | Original symbol/side/size/client ID and named exception/classifier/marker collaborators; SDK request, dict fallback, None or classification bool/int | SDK imports stay lazy; protective construction raises the old aliased RequestBuildError; no broker request is sent |
| service -> dispatch | Original self, durable row/spec/intent, snapshot/quote/broker, can_submit; named builders/classifier/authority/freshness/config types | Original self methods link market-clock and final proof; returns reason or exact submission dict; store transitions and at most one original submit; inherits caller lock |
| service -> protection | Original self/snapshot/broker/owned order, account ID, symbol/reducing side/cancel count; named snapshot/status/result/error collaborators | Ownership rejects with original authority error; cancel returns freshest snapshot and actual DELETE count; gaps return pause dict or original reconciliation result; same store and held lock |
| service -> recovery | Original self/broker/local row/snapshot/can_submit/optional maintenance collector; named builders, policy/config/caps/status/snapshot/exception collaborators | Lookup returns found/None or uncertain authority error; adoption updates same ledger; resubmit retains current policy and exact mutation counting; queue returns snapshot/result; never acquires another lock |
| service -> exits | Original self/symbol/decision/run/quantity/side/prepared outbox; named clock/ID/build/classifier/authority/status collaborators | Prepare returns exact outbox or error dict; close returns exact existing success/dedupe/unknown/rejected shape; prepared abandonment and summaries preserve ledger/state/count behavior; no new lock |
| service -> intent_execution | Original self and complete _execute_core arguments; named validation/spec/price/qty/ID/cap/json collaborators | Complete original sizing/outbox/replay/dispatch/result body; original self cross-method calls; broker mutation only at delegated submit boundary; inherits execute lock |
| long_run -> config | Original cfg/runtime plus policy constants and named paths/writes/errors/time | Config load/save/validation shapes unchanged; runtime install and budget normalization still mutate global config/guard/local event at original time; no broker mutation |
| long_run -> state | Original paths/records/journals/run ID plus named path/clock/redaction/atomic-write/error collaborators | File and directory fsync for authoritative JSON; durable active unlink; best-effort JSONL; unchanged journal schema; runner generator holds/relinquishes original flock; corrupt active state raises same LongRunStop; no broker mutation |
| long_run -> sessions | Same date/window/settled/calendar rows/client plus named journal/state/clock/retry callbacks/constants | Authoritative GET can raise calendar error; existing retry converts exhausted ordinary errors to CALENDAR_UNAVAILABLE; journal settlement and log effects unchanged; inherits runner scope |
| long_run -> preflight | Original cfg/runtime/deps, named LongRunDeps/LongRunStop/default factories/probe/snapshot/calendar callbacks | Redacted probe boundary, same checked optional sources and snapshot results; read-only probes/GET before external observation authorization; later authorized recovery delegates to the sole service |
| long_run -> round_support | Same runtime/journal/symbol/intent/result/observation/notional plus named summary/journal/log/halt callbacks/constants | Same global graph config, scoped audit logger, deterministic IDs and auto-trade bridge; unknown/paused/breaker/kill flags convert to exact old LongRunStop; no direct broker mutation |
| long_run -> symbols | Mutable journal, run/session/cfg/runtime/deps/service/window/graph factory; original recovery-authority and trade-action/error types; all named old entry callbacks/status constants | Shares journal by reference; returns None, STOP_REQUESTED or WINDOW_ENDED. Original closure and cases are retained as an exact contiguous block. Provider failure/ambiguous execution raises old errors; normal analysis failure records FAILED and continues. One graph at most; no stop/deps/container/lock owner |
| long_run -> reporting | Same run/state/cfg/runtime/report/deps, named evidence/arithmetic/path/time/redaction/renderer collaborators | Same JSON/Markdown and return path order; writes local reports. Cost/DB/safety reference errors remain best-effort. Opening ExecutionStore can initialize local schema as before (not a pure read). Alert callback/default adapter runs only at original finalize point and swallows ordinary failures; no broker mutation |

All private method wrappers preserve signature, staticmethod descriptors and
original self identity. Global callables used in moved bodies are passed as named
arguments at call time; callbacks execute at their original point inside bodies.
The original lazy cross-subsystem import `symbols -> execution.service` is
retained for missing-intent diagnostics; it is not an import back to long_run.
There are no runtime reverse imports within either decomposition.

The deadline loop remains whole in service to preserve closure counters, lock
and exception scope. `_execute_core` remains whole in intent_execution and the
recovery submit remains whole in recovery to preserve their distinct semantics.
The symbol block is extracted as one function because it has one journal/graph
lifecycle. These are intentional cohesive responsibilities, not unfinished
copies of the primary entry files.

Validation, versions, complete-suite artifacts, collection/warning comparison,
known existing behavior and remaining verification limits are documented in
`execution-long-run-validation.md`. Schemas/IDs/strategy/limits/retry/report
format are unchanged; no live broker observation or deployment was performed.
# Independent review amendment — 2026-09-17

Existing PENDING/RUNNING round journals now pass
`state.validate_resumable_symbols(journal, LongRunStop=LongRunStop)` before
recovery/config installation. Input: a journal with a dictionary symbol map and
recognized symbol statuses. Output: None; raises existing STATE_CORRUPT for
malformed maps/entries/statuses. No I/O, state writes, locks, broker calls or
new state owner. Empty maps remain valid. This corrects an original silent-skip
defect and is the only added production behavior in the independent review.
See [review and full evidence](execution-long-run-independent-review.md).

# Review hardening amendment — 2026-09-17

Authoritative JSON writes now fsync directory metadata after replacement and
persist newly created ancestor directory entries. Active-state unlink fsyncs
its parent and suppresses only FileNotFoundError. A failure after replace or
unlink is reported even though the namespace operation may already have taken
place; callers must not infer rollback. JSONL events and account-snapshot
telemetry stay best effort and are not resume authority. UNKNOWN adoption now
shares the verified-account lock and binding proof with startup recovery;
a busy lock requires a later retry. Scheduler cleanup removes an always-true
branch without changing calendar authority or output. Refactor AST/differential
evidence above describes the baseline extraction; these deliberate hardening
changes are covered by the new regression tests and combined suite validation.
