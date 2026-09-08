# Traders Full Review Remediation — Phase 2 Implementation Report

## 1. Phase 1 acceptance precondition

The working tree contains the Phase 1 remediation (uncommitted at the time
Phase 2 was implemented; baseline commit `2dccbc0`). The Phase 1 acceptance
document `docs/PHASE_1_SAFETY_ACCEPTANCE.md` is present as the reviewer
template; **no completed "PHASE 1 VERDICT: ACCEPTED" line was found in the
repository** when Phase 2 work began. Per the operator instruction
（「去實作」）Phase 2 was implemented on top of that working tree. The
independent reviewer must confirm the Phase 1 verdict before running the
Phase 2 acceptance (`docs/PHASE_2_OPERATIONS_ACCEPTANCE.md`).

## 2. Production files changed (Phase 2 scope)

| File | Change |
|---|---|
| `tradingagents/graph/trading_graph.py` | F08: normalized `self.llm_request_timeout_seconds`; role/fallback clients now pass the same timeout via `create_llm_client` |
| `tradingagents/screening/llm.py` | F08: `build_screening_llm` passes the configured timeout |
| `tradingagents/agents/utils/gpt5_llm.py` | F08: `get_chat_model` forwards `timeout`+`callbacks` into `GPT5ChatModel`; `bind_tools()` clone preserves both. F15: adapter accounts usage exactly once (`write_audit=False` UI path + single token-bearing audit event), populates `AIMessage.usage_metadata`, marks results `usage_accounted_by_adapter` |
| `tradingagents/llm_clients/usage.py` | **New, small**: `normalize_usage_map`, `extract_langchain_usage`, `UsageAccountingCallback`, `ensure_usage_callback`, `record_direct_openai_usage` |
| `tradingagents/llm_clients/factory.py` | F15: `create_llm_client` attaches exactly one usage callback to every constructed LLM |
| `tradingagents/run_logger.py` | F15: `log_event("llm_call")` feeds `SafetyGuard.record_llm_tokens` BEFORE run attribution; exactly one increment; no-run case returns after safety accounting |
| `tradingagents/dataflows/interface.py` | F15: the three direct OpenAI web-search tools (stock news / global news / fundamentals) record provider usage after each successful response (both Responses and chat-completions branches) |
| `webui/utils/state.py` | F15: `register_llm_call(..., write_audit=False)` lets the GPT-5 adapter update UI counters without re-emitting the audit event |
| `tradingagents/llm_cost.py` | F13: `scan_run_costs(..., metadata_match=...)` exact-metadata filter (identity = metadata, never filename/time) |
| `tradingagents/long_run.py` | F13: final report calls `scan_run_costs` with `metadata_match={"long_run_observation_id": run_id}` and resolves the execution DB via the shared resolver. F14: `finalize_observation` takes one read-only fresh snapshot (`phase:"final"`); on failure logs `final_snapshot_unavailable`. F15.6: `_screening_with_audit_scope` wraps screening in a normal audit run (`__SCREENING__`, `source="long_run_screening"`, observation id). F19: budget gates before screening and before every fresh PENDING analysis (`LLM_BUDGET_EXHAUSTED`); ANALYZED/EXECUTING resume unaffected; budget 0 stays unlimited |
| `tradingagents/execution/service.py` | F13: `resolve_execution_db_path(explicit)` — explicit > current `TRADINGAGENTS_EXECUTION_DB` (read at call time) > literal default; import-time freezing removed |
| `tradingagents/execution/__init__.py` | F13: re-export `resolve_execution_db_path` |
| `tradingagents/dataflows/macro_utils.py` | F16: `spread_bps = (10Y − 2Y) × 100.0` for display and classification; thresholds unchanged |
| `tradingagents/dataflows/interface_utils.py` | F17: `current_analysis_date(now=None)` — America/New_York calendar date; naive injected `now` rejected |
| `webui/components/analysis.py` | F17: real-time path uses `current_analysis_date()` |
| `cli/main.py` | F17: ordinary CLI uses `current_analysis_date()` |
| `webui/cli.py` | **New**: packaged Dash CLI entry (`main()`); app import deferred so `--help` never builds the app |
| `run_webui_dash.py` | F18: thin compatibility wrapper importing `webui.cli.main` |
| `setup.py` | F18: `tradingagents-web=webui.cli:main` |
| `ARCHITECTURE.md`, `env.sample` | F08: removed the "roles mode ignores the timeout" statement |

## 3. Tests added / updated

- **New** `tests/test_full_review_phase2_regressions.py` — 36 behavioral
  tests covering F08, F13, F14, F15, F16, F17, F18, F19 (see §4).
- **Updated** `tests/test_phase_d_long_run.py` — the equity-math fixture's
  last snapshot now carries `phase: "final"` (F14 semantics).

## 4. Finding status

| Finding | Status | Regression evidence |
|---|---|---|
| F08 | **Fixed** | Role Analysis/Decision (OpenAI + Anthropic) receive timeout 7; Analysis fallback timeout 9; screening timeout 11; GPT-5 factory + `bind_tools()` clone keep timeout 13 with exactly one usage callback |
| F13 | **Fixed** | 3-observation fixture: OBS-A report totals 150 (100 analysis + 50 screening), old/manual 1,000,000 and OBS-B 200 excluded; unfiltered scan unchanged; `TRADINGAGENTS_EXECUTION_DB` read at call time, `ExecutionService` and report resolve the same custom DB, default DB never created |
| F14 | **Fixed** | Last post-round equity 100,000 + fresh final broker 101,500 → report ending 101,500, `phase:"final"` appended, positions from final GET; final GET failure → `ending_snapshot_available=false`, ending equity/positions `None`, `last_observed_equity=100000` diagnostic-only, `final_snapshot_unavailable` event logged |
| F15 | **Fixed** | 100-token call with no active run → +100 exactly; 80-token generic callback call → +80; Google/Anthropic-shaped usage normalized; direct OpenAI tool (real `get_stock_news_openai`, fake transport) → +77 exactly once; missing usage never invented; GPT-5 adapter usage 123 → +123, one audit event, `write_audit=False` UI path; screening audit run carries `long_run_observation_id` + `source="long_run_screening"` |
| F16 | **Fixed** | 4.00−3.00 → `100.00 basis points` NORMAL; 3.25−3.00 → `25.00 bps` FLAT; 2.90−3.00 → `-10.00 bps` INVERTED; `1.00 basis points` never appears |
| F17 | **Fixed** | 2026-09-09 00:30 Taipei → `2026-09-08`; naive injected `now` raises; WebUI/CLI sources use the helper (no `datetime.now().strftime`); `parse_analysis_date` accepts the value; long-run ET scheduling untouched |
| F18 | **Fixed** | `import webui.cli` OK; setup.py entry target updated; wheel contains `webui/cli.py` + correct entry_points; isolated venv (Python 3.12) `pip install --no-deps` then `tradingagents-web --help` exits 0 without a server; `python run_webui_dash.py --help` still works |
| F19 | **Fixed** | Budget 1 / used 2 → `LLM_BUDGET_EXHAUSTED`, 0 screening + 0 analysis requests; screening consuming the remainder blocks the first fresh analysis; pre-existing ANALYZED intent executes with budget exhausted and zero new LLM calls; budget 0 (10M used) still unlimited |

## 5. Exactly-once token-accounting evidence

- The SafetyGuard increment lives in exactly one place:
  `RunAuditLogger.log_event` (`llm_call` branch, budget-before-attribution).
  The old in-branch increment was removed (single increment, A-anti-cheat #4).
- `create_llm_client` dedupes via `ensure_usage_callback` (one callback per
  constructed LLM, caller callbacks preserved); the GPT-5 adapter marks its
  results `usage_accounted_by_adapter` so the common callback never
  re-counts them; `webui register_llm_call(write_audit=False)` updates UI
  counters only.
- Tests: `test_budget_increments_without_active_run` (+100 with no run),
  `test_gpt5_adapter_accounts_exactly_once` (+123, 1 event),
  `test_direct_openai_tool_usage_recorded_once` (+77 per call),
  `test_interface_web_search_tool_records_usage_via_fake_transport`
  (real tool layer, +77, zero chat.completions calls).

## 6. Timeout propagation evidence

`test_role_clients_receive_configured_timeout` (Analysis + Decision),
`test_analysis_fallback_receives_configured_timeout`,
`test_screening_llm_receives_configured_timeout`,
`test_gpt5_factory_and_bound_clone_preserve_timeout_and_single_callback`,
plus the Phase 1 `test_bound_model_preserves_timeout` (42.0 through
`with_structured_output`). Retry ownership unchanged: `RetryingLLM` /
`FailoverRetryingLLM` remain the single retry owner; SDK retries stay
pinned to 0.

## 7. Observation-scoped cost evidence

`test_scan_run_costs_metadata_filter_scopes_to_observation`: identity is
the exact `long_run_observation_id` metadata (not filename, not time
window); both the Phase-1-tagged per-symbol run and the Phase-2-tagged
`__SCREENING__` run are included; old/manual and concurrent-observation
runs are excluded; the unfiltered scan keeps global behavior.

## 8. Final-snapshot evidence

`test_final_report_uses_fresh_final_snapshot` (fresh success: ending =
101,500 from `phase:"final"`, no broker mutation — fake broker only
implements `get_account`/`get_all_positions` reads) and
`test_final_snapshot_failure_reports_unknown` (ending unknown, honest
markdown line, `final_snapshot_unavailable` event).

## 9. Packaging entry-point smoke result

- `python -m pip wheel --no-deps . -w /tmp/traders-wheel-test` → OK.
- Isolated Python 3.12 venv: `pip install --no-deps <wheel>` →
  `tradingagents-web --help` prints usage and **exits 0** without starting
  a server or touching the network.
- `python run_webui_dash.py --help` remains functional (exit 0).
- Wheel contents include `webui/cli.py` and
  `entry_points.txt: tradingagents-web = webui.cli:main`.

## 10. Completion commands

```
python -m compileall -q tradingagents cli webui   # PASS
pytest -q tests/test_full_review_phase2_regressions.py   # 36 passed
pytest -q    # 868 passed, 240 subtests, 3 FAILED (pre-existing)
git diff --check   # PASS (trailing whitespace fixed)
python -m pip wheel --no-deps . -w /tmp/traders-wheel-test   # PASS
```

The 3 full-suite failures
(`test_phase_c_remediation::R2RecoveryGateTests::test_current_top20_allowed`,
`test_phase_c_screening::ExecutionEntryGateTests::test_buy_inside_top20_reaches_the_broker`,
`test_phase_c_screening::SchedulerIntegrationTests::test_full_chain_assets_bars_screening_union_execution_gate`)
were verified **pre-existing at baseline commit `2dccbc0`** via an isolated
`git worktree` checkout — they are date-sensitive screening-gate fixtures,
not Phase 2 regressions.

## 11. Safety confirmation

Zero real broker mutations (all broker interactions in tests are
`SimpleNamespace`/`MagicMock` fakes exposing only read methods) and zero
paid/external LLM calls (every OpenAI/LangChain transport is faked; the
wheel smoke test contacts nothing).

## 12. Deviations from the plan

1. **Phase 1 verdict precondition**: implemented per operator instruction
   without finding a written `PHASE 1 VERDICT: ACCEPTED`; reviewers must
   confirm the Phase 1 result first (see §1).
2. **Pre-existing full-suite failures**: 3 date-sensitive screening tests
   fail at the baseline commit itself; documented here and left untouched
   (fixing them is outside Phase 2 scope).
3. `register_llm_call`'s new `write_audit` flag matches the plan's
   "narrowly named flag" allowance (§5.4).
4. `webui/cli.py` defers `from webui.app_dash import run_app` until after
   argument parsing so the installed `--help` smoke test never builds the
   Dash app — required to satisfy A12's "help exits without starting
   external network work".

No automatic 30-day observation was started.
