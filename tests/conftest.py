import pytest


_REMOVED_WEBUI_TEST_MODULES = {
    "test_audit_api_key_semantics.py",
}
_REMOVED_WEBUI_TEST_NODEIDS = {
    "tests/test_collection_skip_scope.py::MixedLegacyTests::test_removed_webui_behavior",
    "tests/test_audit_remaining_six_repairs.py::test_U01_parallel_coordinator_cannot_write_stale_same_symbol_report",
    "tests/test_audit_remaining_six_repairs.py::test_U01_parallel_status_cannot_follow_another_symbol",
    "tests/test_audit_remaining_six_repairs.py::test_owned_parallel_coordinator_discards_late_updates[same_symbol]",
    "tests/test_audit_remaining_six_repairs.py::test_owned_parallel_coordinator_discards_late_updates[other_symbol]",
    "tests/test_audit_remaining_six_repairs.py::test_owned_parallel_coordinator_discards_late_updates[stop]",
    "tests/test_audit_remaining_six_repairs.py::test_owned_parallel_coordinator_publishes_current_symbol",
    "tests/test_audit_remaining_six_repairs.py::test_owned_parallel_risk_coordinator_discards_stale_status",
    "tests/test_audit_remaining_six_repairs.py::test_final_result_during_slow_chart_is_discarded_after_restart",
    "tests/test_audit_remaining_six_repairs.py::test_stale_trade_dispatch_cannot_read_new_run_result",
    "tests/test_audit_repair_completion.py::test_load_env_never_returns_server_secret_and_clear_disables_fallback",
    "tests/test_audit_repair_completion.py::test_active_run_cannot_switch_credentials[analysis_running]",
    "tests/test_audit_repair_completion.py::test_active_run_cannot_switch_credentials[loop_enabled]",
    "tests/test_audit_repair_completion.py::test_active_run_cannot_switch_credentials[market_hour_enabled]",
    "tests/test_audit_repair_completion.py::test_settings_restore_screening_advanced_and_scheduling_controls",
    "tests/test_audit_repair_completion.py::test_webui_settings_and_account_callbacks_register_on_real_dash",
    "tests/test_audit_repair_completion.py::test_wheel_contains_resources_and_imports_from_outside_repo",
    "tests/test_chaos_resilience.py::PositionFetchOutageTests::test_trade_execution_skips_order_when_position_fetch_fails",
    "tests/test_decision_validation_integrity.py::HonestPresentationTests::test_teach_ui_uses_hypothetical_label",
    "tests/test_decision_validation_integrity.py::HonestPresentationTests::test_webui_source_has_no_walk_forward_copy",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIConfigBeforeRecoveryTests::test_r01_webui_applies_auto_screening_config_before_recovery",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIConfigBeforeRecoveryTests::test_r01_webui_call_order_config_applies_before_recovery",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIConfigBeforeRecoveryTests::test_r01_webui_config_failure_prevents_recovery",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIRuntimeCompletenessTests::test_r01_allow_shorts_none_fails_closed",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIRuntimeCompletenessTests::test_r01_allow_shorts_string_false_fails_closed",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIRuntimeCompletenessTests::test_r01_invalid_auto_screening_type_fails_closed",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIRuntimeCompletenessTests::test_r01_invalid_provider_settings_type_fails_closed",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIRuntimeCompletenessTests::test_r01_long_only_run_overrides_stale_short_enabled_global",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIRuntimeCompletenessTests::test_r01_provider_settings_cannot_override_explicit_allow_shorts",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIRuntimeCompletenessTests::test_r01_provider_settings_none_is_legal_and_recovery_runs",
    "tests/test_final_paper_readiness_blockers.py::R01WebUIRuntimeCompletenessTests::test_r01_short_enabled_run_overrides_stale_long_only_global",
    "tests/test_final_paper_readiness_blockers.py::R02WebUIGenerationRecoveryGuardTests::test_r02_webui_current_generation_can_recover",
    "tests/test_final_paper_readiness_blockers.py::R02WebUIGenerationRecoveryGuardTests::test_r02_webui_old_generation_cannot_resubmit_during_recovery",
    "tests/test_full_review_phase1_regressions.py::F05LiquidationIdentityTests::test_webui_callback_no_longer_passes_decision_id",
    "tests/test_full_review_phase1_regressions.py::F10StopCheckpointTests::test_request_stop_is_universal_across_modes",
    "tests/test_full_review_phase1_regressions.py::F10StopCheckpointTests::test_webui_state_stop_flag_gates_trade",
    "tests/test_full_review_phase2_regressions.py::F15UsageTests::test_gpt5_adapter_accounts_exactly_once",
    "tests/test_full_review_phase2_regressions.py::F17AnalysisDateTests::test_webui_uses_helper_not_local_now",
    "tests/test_full_review_phase2_regressions.py::F18EntryPointTests::test_entry_point_target_resolves",
    "tests/test_full_review_phase2_regressions.py::F18EntryPointTests::test_installed_wheel_entry_point_smoke",
    "tests/test_full_review_phase2_regressions.py::F18EntryPointTests::test_root_script_delegates_to_packaged_module",
    "tests/test_full_review_phase2_regressions.py::F18EntryPointTests::test_run_webui_dash_help_exits_without_server",
    "tests/test_full_review_phase2_regressions.py::F18EntryPointTests::test_webui_cli_imports_and_parses",
    "tests/test_llm_cost.py::CostWebUIWiringTests::test_callbacks_register_on_fresh_app",
    "tests/test_llm_cost.py::CostWebUIWiringTests::test_panel_component_builds",
    "tests/test_paper_readiness_20260912_regressions.py::test_N14_broker_outage_renders_error_states_without_leaking_details",
    "tests/test_paper_readiness_20260912_regressions.py::test_N14_legal_empty_account_still_renders_empty_and_zeros",
    "tests/test_phase_a1_execution_foundation.py::SingleEntryTests::test_production_callers_use_single_execution_entry",
    "tests/test_phase_a2_broker_authority.py::CallerAndExitPolicyTests::test_scheduler_has_startup_gate_and_execution_has_periodic_gate",
    "tests/test_phase_b_retry_stop.py::ParallelCoordinatorStopTests::test_parallel_risk_generic_failure_propagates_per_role",
    "tests/test_phase_b_retry_stop.py::SchedulerStopTests::test_operator_restart_clears_provider_stop",
    "tests/test_phase_b_retry_stop.py::SchedulerStopTests::test_provider_stop_clears_queue_and_halts_scheduling",
    "tests/test_phase_b_strategy_modules.py::CorrelationHookCallerTests::test_auto_trade_path_wires_the_hook",
    "tests/test_phase_b_strategy_modules.py::RegimeAndMemoryCallerEvidenceTests::test_regime_scaling_is_wired_into_auto_trade",
    "tests/test_phase_c_remediation.py::R3CalendarTests::test_webui_uses_authoritative_not_static",
    "tests/test_phase_c_screening.py::SchedulerIntegrationTests::test_screening_stop_halts_the_scheduler",
    "tests/test_phase_c_screening.py::SchedulerIntegrationTests::test_successful_plan_feeds_the_round_symbols",
    "tests/test_runtime_reliability_plan_b.py::WebuiAnalysisTest::test_r11_generation_captured_before_initial_chart",
    "tests/test_runtime_reliability_plan_b.py::WebuiAnalysisTest::test_r11_old_work_after_stop_start_never_trades",
    "tests/test_runtime_reliability_plan_b.py::WebuiAnalysisTest::test_r12_current_provider_failure_still_stops_current_queue",
    "tests/test_runtime_reliability_plan_b.py::WebuiAnalysisTest::test_r12_stale_provider_failure_does_not_clear_new_queue",
    "tests/test_unattended_safety_regressions.py::F02GenerationTests::test_f2_t3_stop_bumps_generation_and_start_never_restores_it",
    "tests/test_unattended_safety_regressions.py::F02GenerationTests::test_f2_t4_stop_without_restart_keeps_stop_requested",
    "tests/test_unattended_safety_regressions.py::F02StaleRunTests::test_f2_t1_and_t2_stale_run_never_trades_or_clears_new_run_flag",
    "tests/test_unattended_safety_regressions.py::F02StaleRunTests::test_f2_t1b_current_generation_run_trades_normally",
    "tests/test_unattended_safety_regressions.py::F02StaleSchedulerTests::test_f02_t1_stale_loop_scheduler_cannot_reset_new_run_state",
    "tests/test_unattended_safety_regressions.py::F02StaleSchedulerTests::test_f02_t2_stale_market_hour_scheduler_cannot_reset_screen_or_queue",
    "tests/test_unattended_safety_regressions.py::F02StaleSchedulerTests::test_f02_t3_current_generation_loop_scheduler_still_resets_and_dispatches",
    "tests/test_unattended_safety_regressions.py::F02StaleSchedulerTests::test_f02_t5_worker_never_started_before_stop_start_cannot_masquerade",
    "tests/test_unattended_safety_regressions.py::F02StaleSchedulerTests::test_f02_t6_stale_stopped_screening_cannot_halt_the_new_run",
    "tests/test_unattended_safety_regressions.py::F02StaleSchedulerTests::test_f02_t7_current_generation_screening_failure_still_halts_scheduler",
}


def pytest_collection_modifyitems(items):
    """Skip obsolete assertions whose subject was the removed WebUI layer."""
    marker = pytest.mark.skip(reason="WebUI was intentionally removed; test is obsolete")
    for item in items:
        path = getattr(item, "path", None)
        if path is not None and path.name in _REMOVED_WEBUI_TEST_MODULES:
            item.add_marker(marker)
            continue
        if item.nodeid in _REMOVED_WEBUI_TEST_NODEIDS:
            item.add_marker(marker)


@pytest.fixture(autouse=True)
def isolate_long_run_state(monkeypatch, tmp_path):
    """Never let tests write the operator's real durable state."""
    monkeypatch.setenv(
        "TRADINGBUFFETT_LONG_RUN_DIR", str(tmp_path / "long_run_state")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_EXECUTION_LOCK_DIR", str(tmp_path / "execution_locks")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_RESULTS_DIR", str(tmp_path / "results")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_CACHE_DIR", str(tmp_path / "cache")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_MEMORY_LOG_PATH", str(tmp_path / "memory.md")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_AGENT_MEMORY_DIR", str(tmp_path / "agent_memory")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_EXECUTION_DB", str(tmp_path / "execution.sqlite3")
    )
    import tradingagents.safety.guardrails as guardrails

    monkeypatch.setattr(guardrails, "_SAFETY_HOME", tmp_path / "safety")
    guardrails.reset_safety_guard()


@pytest.fixture(autouse=True)
def isolate_operator_llm_endpoint(monkeypatch):
    """Keep the operator's local .env routing from changing unit-test defaults."""
    monkeypatch.delenv("TRADINGBUFFETT_OPENAI_USE_LOCAL", raising=False)
    monkeypatch.delenv("TRADINGBUFFETT_OPENAI_BASE_URL", raising=False)
