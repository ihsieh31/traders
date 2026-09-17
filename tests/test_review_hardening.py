"""Review repairs: durability faults, shared recovery lock and calendar behavior.

All broker/calendar calls use fixtures. Fault injection verifies ordering and
failure semantics, not physical power-loss behavior of a filesystem/device.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import stat
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from test_execution_safety_plan_a import env, now
from tradingagents import long_run as lr
from tradingagents.execution import service as entry
from tradingagents.execution.authority import AccountExecutionLock, BrokerAuthorityError
from tradingagents.long_run_support import state


def test_authoritative_write_syncs_file_then_replace_then_directory(tmp_path, monkeypatch):
    path = tmp_path / 'active.json'
    events = []
    fsync, replace = os.fsync, os.replace

    def sync(fd):
        events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        fsync(fd)

    def rename(source, target):
        assert Path(source).parent == path.parent
        assert json.loads(Path(source).read_text()) == {'run_id': 'new'}
        events.append('replace')
        replace(source, target)

    monkeypatch.setattr(state.os, 'fsync', sync)
    monkeypatch.setattr(state.os, 'replace', rename)
    lr.atomic_write_json(path, {'run_id': 'new'})
    assert events == ['file', 'replace', 'directory']
    assert json.loads(path.read_text()) == {'run_id': 'new'}
    assert not list(tmp_path.glob('*.tmp'))


def test_new_authoritative_directory_entries_are_synced(tmp_path, monkeypatch):
    parents = []
    original = state._fsync_directory
    monkeypatch.setattr(state, '_fsync_directory', lambda p: (parents.append(p), original(p)))
    path = tmp_path / 'runs' / 'new' / 'rounds' / 'session.json'
    lr.atomic_write_json(path, {'status': 'PENDING'})
    assert parents == [tmp_path, tmp_path / 'runs', tmp_path / 'runs/new', path.parent]


@pytest.mark.parametrize('failure', ['serialization', 'file_sync', 'replace'])
def test_pre_replace_failure_preserves_valid_target(tmp_path, monkeypatch, failure):
    path = tmp_path / 'active.json'
    path.write_text('{"run_id": "old", "status": "RUNNING"}')
    original = path.read_bytes()

    def broken(*args, **kwargs):
        if failure == 'serialization':
            args[1].write('{"corrupt":')
        raise OSError('injected before replace')

    if failure == 'serialization':
        monkeypatch.setattr(state.json, 'dump', broken)
    elif failure == 'file_sync':
        monkeypatch.setattr(state.os, 'fsync', broken)
    else:
        monkeypatch.setattr(state.os, 'replace', broken)
    with pytest.raises(OSError, match='injected before replace'):
        lr.atomic_write_json(path, {'run_id': 'new'})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))


def test_post_replace_failure_reports_error_and_retains_complete_new_target(tmp_path, monkeypatch):
    path = tmp_path / 'active.json'
    path.write_text('{"run_id": "old"}')
    monkeypatch.setattr(state, '_fsync_directory', Mock(side_effect=OSError('directory sync failed')))
    with pytest.raises(OSError, match='directory sync failed'):
        lr.atomic_write_json(path, {'run_id': 'new'})
    assert json.loads(path.read_text()) == {'run_id': 'new'}
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('after_replace', [False, True])
def test_interruption_around_replace_leaves_only_complete_authoritative_json(tmp_path, monkeypatch, after_replace):
    path = tmp_path / 'active.json'
    path.write_text('{"run_id": "old"}')
    replace = os.replace

    def interrupt(source, target):
        if after_replace:
            replace(source, target)
        raise SystemExit('injected process interruption')

    monkeypatch.setattr(state.os, 'replace', interrupt)
    with pytest.raises(SystemExit, match='process interruption'):
        lr.atomic_write_json(path, {'run_id': 'new'})
    assert json.loads(path.read_text()) == {'run_id': 'new' if after_replace else 'old'}
    assert not list(tmp_path.glob('*.tmp'))


def test_directory_sync_closes_descriptor_on_failure(tmp_path, monkeypatch):
    close = os.close
    closed = []
    monkeypatch.setattr(state.os, 'fsync', Mock(side_effect=OSError('sync failed')))
    monkeypatch.setattr(state.os, 'close', lambda fd: (closed.append(fd), close(fd)))
    with pytest.raises(OSError, match='sync failed'):
        state._fsync_directory(tmp_path)
    assert len(closed) == 1
    with pytest.raises(OSError):
        os.fstat(closed[0])


def test_clear_active_state_unlinks_then_syncs_and_missing_is_noop(monkeypatch):
    path = lr.active_path()
    lr.atomic_write_json(path, {'run_id': 'old'})
    synced = []

    def sync(parent):
        assert not path.exists()
        synced.append(parent)

    monkeypatch.setattr(state, '_fsync_directory', sync)
    lr.clear_active_state()
    lr.clear_active_state()
    assert synced == [path.parent]


@pytest.mark.parametrize('error', [PermissionError('denied'), OSError('I/O failed')])
def test_clear_active_state_propagates_unlink_failure(monkeypatch, error):
    path = lr.active_path()
    lr.atomic_write_json(path, {'run_id': 'old'})
    monkeypatch.setattr(state.os, 'unlink', Mock(side_effect=error))
    with pytest.raises(type(error), match=str(error)):
        lr.clear_active_state()
    assert path.exists()


def test_clear_active_state_propagates_post_unlink_sync_failure(monkeypatch):
    path = lr.active_path()
    lr.atomic_write_json(path, {'run_id': 'old'})
    monkeypatch.setattr(state, '_fsync_directory', Mock(side_effect=OSError('sync failed')))
    with pytest.raises(OSError, match='sync failed'):
        lr.clear_active_state()
    assert not path.exists()


def test_jsonl_telemetry_has_no_fsync_barrier(tmp_path, monkeypatch):
    monkeypatch.setattr(state.os, 'fsync', Mock(side_effect=AssertionError('telemetry fsync')))
    path = tmp_path / 'events.jsonl'
    state.append_jsonl(path, {'type': 'one'}, sanitize_for_log=lambda x: x)
    state.append_jsonl(path, {'type': 'two'}, sanitize_for_log=lambda x: x)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{'type': 'one'}, {'type': 'two'}]


def unknown_order(service, broker):
    service.store.ensure_account_binding(broker.get_account().id)
    _, rows, _ = service.store.create_outbox(
        decision_id='unknown-review', run_id=None, symbol='AAPL', action='BUY',
        target_position='LONG', payload_json='{}',
        orders=[{'client_order_id': 'unknown-review-client', 'symbol': 'AAPL', 'side': 'buy', 'quantity': 1}],
    )
    row = rows[0]
    service.store.transition_order(row['order_id'], 'SUBMITTING')
    service.store.transition_order(row['order_id'], 'UNKNOWN')
    found = NS(id='observed-review', client_order_id=row['client_order_id'], symbol='AAPL',
               side='buy', qty=1, filled_qty=0, filled_avg_price=None, status='accepted',
               updated_at=now(), legs=[], notional=None, type='market')
    return row, found


@pytest.mark.parametrize('competitor', ['lookup_unknown', 'startup_recover'])
def test_unknown_lookup_and_recovery_share_real_account_lock(env, monkeypatch, competitor):
    service, broker = env
    row, found = unknown_order(service, broker)
    other = entry.ExecutionService(db_path=service.db_path, broker_factory=lambda: broker)
    entered, release = Event(), Event()
    adoptions = []
    adopt = service._adopt_recovery_order

    def blocked_lookup(*args):
        entered.set()
        assert release.wait(10), 'test did not release lookup'
        return found

    monkeypatch.setattr(service, '_lookup_for_recovery', blocked_lookup)
    monkeypatch.setattr(service, '_adopt_recovery_order', lambda *args: (adoptions.append(args), adopt(*args)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.lookup_unknown, row['client_order_id'])
        try:
            assert entered.wait(10), 'lookup did not reach the held-lock section'
            second = pool.submit(getattr(other, competitor), *([row['client_order_id']] if competitor == 'lookup_unknown' else []))
            result = second.result(timeout=10)
            if competitor == 'lookup_unknown':
                assert result['busy'] and result['uncertain'] and result['paused']
            else:
                assert not result['success'] and result['account_execution_state'] == 'PAUSED'
                assert 'busy' in result['error']
            assert service.store.get_order(row['order_id'])['status'] == 'UNKNOWN'
        finally:
            release.set()
        result = first.result(timeout=10)
    assert result['adopted'] and result['status'] == 'ACCEPTED'
    assert len(adoptions) == 1
    broker.orders.append(found)
    assert not other.lookup_unknown(row['client_order_id'])['adopted']
    assert other.startup_recover()['success']
    assert broker.submits == [] and broker.cancels == []
    assert len(service.store.list_all_orders()) == 1


def test_unknown_lookup_rechecks_row_after_competing_adoption(env, monkeypatch):
    service, broker = env
    row, found = unknown_order(service, broker)
    other = entry.ExecutionService(db_path=service.db_path, broker_factory=lambda: broker)
    capture = entry.capture_broker_snapshot
    captures = []

    def recover_before_lock(*args, **kwargs):
        snapshot = capture(*args, **kwargs)
        captures.append(snapshot)
        with AccountExecutionLock(other.db_path, snapshot.account_id):
            other._adopt_recovery_order(other.store.get_order(row['order_id']), found)
        return snapshot

    monkeypatch.setattr(entry, 'capture_broker_snapshot', recover_before_lock)
    monkeypatch.setattr(service, '_lookup_for_recovery', Mock(side_effect=AssertionError('stale lookup')))
    result = service.lookup_unknown(row['client_order_id'])
    assert result['found'] and not result['adopted']
    assert result['order']['status'] == 'ACCEPTED'
    assert len(captures) == 1
    assert broker.submits == []


def test_unknown_lookup_fast_paths_do_not_construct_broker(env, monkeypatch):
    service, broker = env
    row, _ = unknown_order(service, broker)
    service.store.transition_order(row['order_id'], 'REJECTED')
    monkeypatch.setattr(service, '_broker_factory', Mock(side_effect=AssertionError('broker constructed')))
    assert not service.lookup_unknown('missing')['found']
    assert not service.lookup_unknown(row['client_order_id'])['adopted']


@pytest.mark.parametrize('failure', ['identity', 'account_changed', 'binding', 'lock_io', 'lookup'])
def test_unknown_lookup_fails_closed_without_adoption(env, monkeypatch, failure):
    service, broker = env
    row, found = unknown_order(service, broker)
    lookup = Mock(return_value=found)
    monkeypatch.setattr(service, '_lookup_for_recovery', lookup)
    if failure == 'identity':
        monkeypatch.setattr(entry, 'capture_broker_snapshot', Mock(side_effect=BrokerAuthorityError('unprovable identity')))
    elif failure == 'account_changed':
        account = broker.get_account()
        other = NS(**{**vars(account), 'id': 'different-account'})
        monkeypatch.setattr(broker, 'get_account', Mock(side_effect=[account, other]))
    elif failure == 'binding':
        account = broker.get_account()
        monkeypatch.setattr(broker, 'get_account', lambda: NS(**{**vars(account), 'id': 'different-account'}))
    elif failure == 'lock_io':
        monkeypatch.setattr(entry, 'AccountExecutionLock', Mock(side_effect=OSError('cannot open lock')))
    else:
        lookup.side_effect = BrokerAuthorityError('lookup uncertain')
    result = service.lookup_unknown(row['client_order_id'])
    assert not result['found'] and result['uncertain'] and result['paused']
    assert service.store.get_order(row['order_id'])['status'] == 'UNKNOWN'
    assert broker.submits == []
    if failure != 'lookup':
        lookup.assert_not_called()


def test_unknown_lookup_explicit_not_found_keeps_unknown(env, monkeypatch):
    service, broker = env
    row, _ = unknown_order(service, broker)
    monkeypatch.setattr(service, '_lookup_for_recovery', lambda *a: None)
    result = service.lookup_unknown(row['client_order_id'])
    assert result['not_found'] and not result['found']
    assert service.store.get_order(row['order_id'])['status'] == 'UNKNOWN'
    assert broker.submits == []


@pytest.mark.parametrize('source', ['default_client', 'injected_client', 'calendar_rows', 'failure'])
@pytest.mark.parametrize('case', ['overdue', 'future', 'settled', 'end_excluded', 'early_close'])
def test_scheduler_calendar_paths_keep_characterized_behavior(monkeypatch, source, case):
    from tradingagents.dataflows import market_calendar as calendar
    import tradingagents.dataflows.alpaca_utils as alpaca

    calendar.clear_calendar_cache()
    rows = [
        {'date': '2026-09-08', 'close': '16:00'},
        {'date': '2026-09-09', 'close': '13:00' if case == 'early_close' else '16:00'},
    ]

    def get_calendar(request):
        if source == 'failure':
            raise OSError('calendar unavailable')
        return [row for row in rows if str(request.start)[:10] <= row['date'] <= str(request.end)[:10]]

    client = NS(get_calendar=Mock(side_effect=get_calendar))
    monkeypatch.setattr(alpaca, 'get_alpaca_trading_client', lambda: client)
    options = {'calendar_client': client} if source == 'injected_client' else {}
    if source == 'calendar_rows':
        options = {'calendar_rows': rows}
    clock = datetime(2026, 9, 9, 14 if case == 'early_close' else 10)
    settled = [] if case == 'overdue' else ['2026-09-08']
    if case == 'settled':
        settled.append('2026-09-09')
    arguments = dict(now=clock, run_time_et='15:00', started_at=lr.eastern_now(datetime(2026, 9, 8)),
                     ends_at=lr.eastern_now(datetime(2026, 9, 9 if case == 'end_excluded' else 10)),
                     settled=settled, **options)
    try:
        if source == 'failure':
            with pytest.raises(calendar.CalendarError, match='calendar unavailable'):
                lr.next_due_session(**arguments)
        else:
            result = lr.next_due_session(**arguments)
            if case in ('settled', 'end_excluded'):
                assert result is None
            else:
                day = '2026-09-08' if case == 'overdue' else '2026-09-09'
                early = case == 'early_close'
                target = '12:30' if early else '15:00'
                assert result == {
                    'session_date': day, 'configured_target': '15:00', 'effective_target': target,
                    'authoritative_close': '13:00' if early else '16:00',
                    'schedule_adjustment': 'EARLY_CLOSE' if early else 'NONE',
                    'due': case != 'future', 'effective_at': f'{day}T{target}:00-04:00',
                }
            if source == 'calendar_rows':
                client.get_calendar.assert_not_called()
            else:
                assert client.get_calendar.called
    finally:
        calendar.clear_calendar_cache()
