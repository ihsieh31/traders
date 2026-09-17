"""Guard module ownership, all original signatures and call-time method seams."""
import ast
import importlib
import inspect
from pathlib import Path

import pytest

from tradingagents.execution import service as entry

ROOT = Path(__file__).resolve().parents[1]
OWNERS = {
    'protection': ('_verify_owned_close_protections', '_broker_order_live',
        '_cancel_protection_with_race_check', '_cancel_owned_close_protections',
        '_program_owned_live_reducing_qty', '_evaluate_protection_gap',
        '_recover_persisted_protection_gaps', '_protection_coverage_gaps',
        '_apply_protection_coverage', '_verified_reducing_exit'),
    'recovery': ('_lookup_for_recovery', '_adopt_recovery_order',
        '_recovery_crossing_block', '_resubmit_recovered', '_reconcile_snapshot',
        '_recover_locked'),
    'dispatch': ('_market_clock_closed', '_validate_opening_dispatch', '_submit_one'),
    'exits': ('_abandon_prepared_rows', '_prepare_liquidation_outbox',
        '_liquidate_core', '_paused_result'),
    'intent_execution': ('_execute_core',),
}


@pytest.mark.parametrize('owner,name', [(owner, name) for owner, names in OWNERS.items() for name in names])
@pytest.mark.parametrize('include_optional', [False, True])
def test_old_method_delegates_to_single_owner_at_call_time(tmp_path, monkeypatch, owner, name, include_optional):
    service = entry.ExecutionService(db_path=tmp_path / 'execution.db')
    module = importlib.import_module('tradingagents.execution.' + owner)
    marker = object()
    clock = object()
    snapshot = object()
    config = object()
    monkeypatch.setattr(entry, 'utc_now', clock)
    monkeypatch.setattr(entry, 'capture_broker_snapshot', snapshot)
    monkeypatch.setattr(entry, '_get_execution_config', config)
    calls = []

    def replacement(*args, **kwargs):
        calls.append((args, kwargs))
        return marker

    monkeypatch.setattr(module, name, replacement)
    method = getattr(service, name)
    required = {key: object() for key, param in inspect.signature(method).parameters.items()
                if include_optional or param.default is inspect.Parameter.empty}
    assert method(**required) is marker
    assert len(calls) == 1
    args, kwargs = calls[0]
    descriptor = inspect.getattr_static(entry.ExecutionService, name)
    assert args == (() if isinstance(descriptor, staticmethod) else (service,))
    for key, value in required.items():
        assert kwargs[key] is value
    for key, value in (('utc_now', clock), ('capture_broker_snapshot', snapshot),
                       ('_get_execution_config', config)):
        if key in kwargs:
            assert kwargs[key] is value


def test_old_request_classifier_and_exception_seams(monkeypatch):
    monkeypatch.setattr(entry, '_exception_http_status', lambda exc: 422)
    assert entry._definitive_rejection(RuntimeError('no HTTP attributes')) is True
    monkeypatch.setattr(entry, '_TIMEOUT_MARKERS', ('local-marker',))
    assert entry._is_ambiguous_error(RuntimeError('local-marker')) is True
    assert entry._is_ambiguous_error(RuntimeError('timeout')) is False
    import alpaca.trading.requests as requests

    class ConstructionError(ValueError):
        pass

    def broken(*args, **kwargs):
        raise ValueError('construction sentinel')

    monkeypatch.setattr(entry, 'RequestBuildError', ConstructionError)
    monkeypatch.setattr(requests, 'MarketOrderRequest', broken)
    with pytest.raises(ConstructionError):
        entry._build_protective_request('AAPL', 'buy', 1, 90, 120, 'client')


def test_current_ownership_has_no_duplicates_or_second_stop_state():
    # Tool is loaded from a file, does not import production or run a subprocess.
    import importlib.util
    spec = importlib.util.spec_from_file_location('boundary_audit', ROOT / 'scripts/refactoring/audit_boundaries.py')
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    # Full original-source comparison runs outside pytest because git child
    # processes are deliberately forbidden by the inherited offline guard.
    paths = [ROOT / 'tradingagents/execution/service.py', ROOT / 'tradingagents/long_run.py']
    paths += list((ROOT / 'tradingagents/long_run_support').glob('*.py'))
    paths += [ROOT / f'tradingagents/execution/{name}.py' for name in (*OWNERS, 'requests', 'order_planning')]
    for path in paths:
        tree = ast.parse(path.read_text())
        scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        for scope in scopes:
            names = [n.name for n in scope.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
            assert len(names) == len(set(names)), path
        if path.parent.name == 'long_run_support':
            assert not any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                           and n.id == '_stop_requested' for n in ast.walk(tree)), path
        if path.name == 'symbols.py':
            helper = audit.definitions(tree)['run_symbol_work']
            assert ast.unparse(helper.body[-1]) == 'return stop_reason'
