"""Old entry-point contracts and fresh-process offline import orders.

These supplement, not replace, the original-source differential broker/artifact
cases in test_execution_long_run_refactor.py. No assertions on __module__.
"""
import inspect
import json
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from test_execution_long_run_refactor import evidence, fixed, STAMP, ROOT
from test_execution_safety_plan_a import env, opening
from tradingagents import long_run as lr
from tradingagents.execution import service as service_module


SERVICE_FUNCTIONS = (
    "execute_trade_intent", "liquidate_position", "resolve_execution_db_path",
    "validate_trade_intent", "_build_market_request", "_build_protective_request",
    "_get_execution_config", "_evaluate_opening_caps", "_submit_authority_error",
)
SERVICE_METHODS = (
    "__init__", "execute", "liquidate", "startup_recover", "enforce_exit_deadlines",
    "_execute_core", "_prepare_liquidation_outbox", "_reconcile_snapshot",
    "_protection_coverage_gaps", "_program_owned_live_reducing_qty",
)
LONG_RUN_FUNCTIONS = (
    "LongRunDeps", "LongRunStop", "default_long_run_config", "build_runtime_config",
    "validate_long_run_config", "run_preflight", "run_post_authorization_recovery",
    "run_daily_round", "run_observation_loop", "finalize_observation", "setup_or_resume",
    "runner_lock", "atomic_write_json", "load_active_state", "save_active_state",
    "new_round_journal", "save_round_journal", "load_round_journal", "next_due_session",
    "effective_target_for_session", "capture_account_snapshot", "aggregate_final_report",
    "render_final_markdown", "write_final_report", "_execute_intent", "request_stop",
)


def test_original_signatures_exports_and_constants(fixed):
    import tradingagents.execution as execution
    from tradingagents.execution import authority, store
    signatures = {}
    for module, names in ((service_module, SERVICE_FUNCTIONS), (lr, LONG_RUN_FUNCTIONS)):
        for name in names:
            signatures[("service." if module is service_module else "long_run.") + name] = str(inspect.signature(getattr(module, name)))
    for name in SERVICE_METHODS:
        signatures["ExecutionService." + name] = str(inspect.signature(getattr(service_module.ExecutionService, name)))
    assert execution.ExecutionService is service_module.ExecutionService
    assert execution.execute_trade_intent is service_module.execute_trade_intent
    assert execution.liquidate_position is service_module.liquidate_position
    assert service_module.BrokerAuthorityError is authority.BrokerAuthorityError
    assert service_module.AccountLockBusy is authority.AccountLockBusy
    for name in execution.__all__:
        assert hasattr(execution, name)
    evidence("contracts", {"signatures": signatures, "execution_exports": execution.__all__,
        "constants": {name: getattr(lr, name) for name in
            ("LONG_RUN_SCHEMA_VERSION", "TERMINAL_ROUND_STATUSES", "SYMBOL_PENDING", "SYMBOL_ANALYZING", "SYMBOL_ANALYZED", "SYMBOL_EXECUTING", "SYMBOL_DONE")},
        "schema_version": store.SCHEMA_VERSION, "order_statuses": sorted(store.ORDER_STATUSES)}, fixed)


def test_exception_identity_at_old_boundaries(fixed, monkeypatch):
    import alpaca.trading.requests as requests
    def broken(*a, **kw):
        raise ValueError("construction sentinel")
    monkeypatch.setattr(requests, "MarketOrderRequest", broken)
    with pytest.raises(service_module.RequestBuildError) as exc:
        service_module._build_protective_request("AAPL", "buy", 1, 90, 120, "sentinel")
    assert type(exc.value) is service_module.RequestBuildError
    assert isinstance(exc.value.__cause__, ValueError)
    with pytest.raises(lr.LongRunStop) as stop:
        lr._validate_unattended_safety({"safety_enabled": False})
    assert type(stop.value) is lr.LongRunStop
    assert stop.value.code == "SAFETY_DISABLED"
    with pytest.raises(lr.SnapshotUnavailable) as snapshot:
        lr.capture_account_snapshot(NS(get_account=lambda: NS(equity=float("nan"), cash=1)))
    assert type(snapshot.value) is lr.SnapshotUnavailable
    with lr.runner_lock():
        with pytest.raises(lr.RunnerLockBusy) as lock:
            with lr.runner_lock():
                pytest.fail("second runner acquired lock")
        assert type(lock.value) is lr.RunnerLockBusy


def test_old_service_globals_and_instance_class_patch_sentinels(env, fixed, monkeypatch):
    service, broker = env
    captures = []
    original_capture = service_module.capture_broker_snapshot
    def capture(*args, **kwargs):
        captures.append("capture")
        return original_capture(*args, **kwargs)
    monkeypatch.setattr(service_module, "capture_broker_snapshot", capture)
    assert service.execute(trade_intent=opening(stamp=STAMP), dollar_amount=1000)["success"]
    assert captures
    builds = []
    original_build = service_module._build_market_request
    def build(*args, **kwargs):
        builds.append(args)
        return original_build(*args, **kwargs)
    monkeypatch.setattr(service_module, "_build_market_request", build)
    assert service.liquidate("AAPL")["success"]
    assert len(builds) == 1 and builds[0][0] == "AAPL"
    marker = {"sentinel": True}
    monkeypatch.setattr(service, "_execute_core", lambda **kw: marker)
    assert service.execute(trade_intent=None) is marker
    monkeypatch.delattr(service, "_execute_core")
    monkeypatch.setattr(service_module.ExecutionService, "_execute_core", lambda self, **kw: marker)
    assert service.execute(trade_intent=None) is marker


def test_call_time_config_and_long_run_default_factories(env, fixed, monkeypatch):
    service, _ = env
    seen = []
    monkeypatch.setattr(service_module, "_get_execution_config", lambda: {"marker": "call-time"})
    # Lazy quarantine initialization must consult the old config global.
    import tradingagents.risk.corporate_actions as quarantine
    monkeypatch.setattr(quarantine, "build_quarantine_gate", lambda cfg: seen.append(cfg) or NS())
    other = service_module.ExecutionService(db_path=fixed / "other.db", broker_factory=lambda: None)
    assert seen == []
    other.quarantine_gate()
    other.quarantine_gate()
    assert seen == [{"marker": "call-time"}]
    class ConfigSentinel(Exception):
        pass
    def config_sentinel():
        raise ConfigSentinel("old config global reached by planning")
    monkeypatch.setattr(service_module, "_get_execution_config", config_sentinel)
    with pytest.raises(ConfigSentinel):
        service_module._evaluate_opening_caps(symbol="AAPL", specs=[], snapshot=None,
                                              quote=None, intent_dict={})
    marker = object()
    deps = lr.LongRunDeps()
    monkeypatch.setattr(lr, "_default_execution_service", lambda: marker)
    # Patch the lifecycle boundary, not a support implementation module.
    called = []
    monkeypatch.setattr(lr, "_control_stop_reason", lambda *a: called.append(marker) or "stop")
    result = lr.run_daily_round(run_id="sentinel", session_date="2026-09-08", long_cfg={}, runtime={}, deps=deps)
    assert result is None and called == [marker]
    assert deps.now_fn() == STAMP
    monkeypatch.setattr(lr, "_stop_requested", True)
    assert lr.request_stop() is True


# New support modules are tested only after production lands. The old archive
# test run selects the four tests above, behavioral suite, and old-entry orders.
EXECUTION_MODULES = ["tradingagents.execution." + name for name in
    ("service", "order_planning", "requests", "dispatch", "protection", "recovery", "exits", "intent_execution")]
LONG_RUN_MODULES = ["tradingagents.long_run"] + ["tradingagents.long_run_support." + name for name in
    ("config", "state", "sessions", "preflight", "round_support", "reporting", "symbols")]


@pytest.mark.parametrize("order", ["old_execution_first", "old_long_run_first", "execution_first", "long_run_first", "helpers_first", "reverse_helpers"])
def test_fresh_process_import_orders_no_external_calls(tmp_path, order):
    helpers = EXECUTION_MODULES[1:] + LONG_RUN_MODULES[1:]
    orders = {"old_execution_first": [EXECUTION_MODULES[0], LONG_RUN_MODULES[0]],
              "old_long_run_first": [LONG_RUN_MODULES[0], EXECUTION_MODULES[0]],
              "execution_first": EXECUTION_MODULES + LONG_RUN_MODULES,
              "long_run_first": LONG_RUN_MODULES + EXECUTION_MODULES,
              "helpers_first": helpers + [EXECUTION_MODULES[0], LONG_RUN_MODULES[0]],
              "reverse_helpers": list(reversed(helpers)) + [LONG_RUN_MODULES[0], EXECUTION_MODULES[0]]}
    code = r'''
import importlib, json, socket, sys
attempts = []
def audit(event, args):
    if event.startswith("socket.") and event not in ("socket.__new__",):
        attempts.append(event)
        raise RuntimeError("import external call: " + event)
    if event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
        attempts.append(event)
        raise RuntimeError("import external process: " + event)
sys.addaudithook(audit)
for name in json.loads(sys.argv[1]):
    importlib.import_module(name)
assert not attempts, attempts
print("IMPORTS_OK_NO_EXTERNAL_CALLS")
'''
    result = subprocess.run([sys.executable, "-c", code, json.dumps(orders[order])],
                            cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "IMPORTS_OK_NO_EXTERNAL_CALLS" in result.stdout
