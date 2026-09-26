"""Fixed-input characterization against git archive 69388cc (never a live broker).

The compact fixture holds SHA256s, not a large golden/source copy. Full canonical
trace, logical SQLite rows and artifact bytes are emitted under REFACTOR_ARTIFACTS
on every run. A missing fixture is allowed ONLY in the original source archive,
verified by its two source hashes; it is not a bless-current-code switch.

Sequence coverage reused without duplication: test_execution_safety_plan_a R03
(cancel cascade/refusal), R04 (close-only reversal), R05 (expired/stale zero POST);
test_execution_stop_coverage_regressions (stop types, initial/between-item gap,
prohibited reversal/duplicate); test_phase_d_long_run (settled sessions, MISSED,
interrupts, corrupt journals, finalization/report math); full_review_phase2 F19
(execution-only exhausted-budget resume), F14 (fresh final snapshot);
test_runtime_reliability_plan_b R01/R02/R13 (runtime installation, stop races,
closed-session MISSED versus EXECUTING ownership, clock before opening POST);
test_pre_30d_reliability_repairs F03 (bounded scheduler retry).
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS

import pytest

from test_execution_safety_plan_a import env, opening
import test_execution_safety_plan_a as execution_fakes
import test_phase_d_long_run as round_fakes
from tradingagents import long_run as lr
from tradingagents.execution import service as service_module
from tradingagents.execution import authority, store, policy

STAMP = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/execution_long_run_refactor_69388cc.json"
ORIGINAL_HASHES = {'tradingagents/long_run.py': 'fa49cdfe3ea1d8f6aab54e1dfb3654aaff57dcd496c9f973fd99205f97db68be', 'tradingagents/execution/service.py': 'c9151d84f24b9b0a1d4a424a638bbbfff96e2c98d3c863abeaa83bf3d59a6e5a'}
UPDATED_CANONICAL_HASHES = {
    # These complete snapshots reflect the deterministic R/R and portfolio
    # stop-risk gates. No trace, result, or ledger fields are normalized away.
    "protected_close": "e489968434e1c4fbfeb0fda922f78dc3f64137b9c5f0f9f25161dda51c8b046b",
    "adopt_unknown": "661f53c4d727bb4a8647cce70d59f35525ca68b07066241f4ea6110b3265ed3e",
    "block_submitting": "3cbf0f6772631c053d6a093bc7a461c09fb96f58429c1710c790b3b05211436a",
    # Recovery cap evaluation now retains conservative exposure from the
    # account's live protective OCO children.
    "gap_between_items": "0b89641b87c59a768a445521413976093cc48f0eb7ff26c415e21a7669873b4b",
    "contracts": "af1ef28c11a81f72175753f881a9f3a16639d608eaaafafb747bd31a691f61a5",
    # Missing execution DB is now reported as unavailable without creating
    # a new empty ledger; these are the resulting complete report snapshots.
    "long_run_fresh": "3437abc6b80e5965ddd94c97925adc16a4863f1b341810edd185357bc3c74b47",
    "long_run_resume": "5420793cdcd243437ee3b096cbda23cd351b1c2cd498dbebc44e07b9550ff734",
}


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return STAMP.astimezone(tz) if tz else STAMP.replace(tzinfo=None)


@pytest.fixture
def fixed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from datetime import timedelta
    ticks = iter(range(10000))
    def clock():
        return STAMP + timedelta(microseconds=next(ticks))
    monkeypatch.setattr(execution_fakes, "now", clock)
    # Keep the broker clock at the fixed session timestamp; reading it must
    # not consume a fake wall-clock tick and shift unrelated audit evidence.
    monkeypatch.setattr(execution_fakes.Broker, "get_clock",
                        lambda self: NS(is_open=True, timestamp=STAMP))
    original_capture = authority.capture_broker_snapshot
    monkeypatch.setattr(service_module, "capture_broker_snapshot",
                        lambda client, **kwargs: original_capture(client, **{**kwargs, "now": clock}))
    monkeypatch.setattr(authority, "utc_now", clock)
    monkeypatch.setattr(service_module, "utc_now", clock)
    monkeypatch.setattr(store, "utcnow_iso", lambda: STAMP.isoformat())
    monkeypatch.setattr(policy, "datetime", FixedDateTime)
    monkeypatch.setattr(lr, "datetime", FixedDateTime)
    monkeypatch.setattr(lr, "utc_now_iso", lambda: STAMP.isoformat())
    monkeypatch.setattr(lr, "_stop_requested", False)
    monkeypatch.setenv("TRADINGBUFFETT_EXECUTION_DB", str(tmp_path / "report.sqlite3"))
    from tradingagents.dataflows import config
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.run_logger as logger
    import tradingagents.agents.schemas as schemas
    monkeypatch.setattr(schemas, "datetime", FixedDateTime)
    import test_full_review_phase1_regressions as recovery_fakes
    monkeypatch.setattr(recovery_fakes, "datetime", FixedDateTime)
    monkeypatch.setattr(logger, "datetime", FixedDateTime)
    monkeypatch.setattr(logger, "uuid", NS(uuid4=lambda: NS(hex="0123456789abcdef")))
    monkeypatch.setattr(logger, "_RUN_AUDIT_LOGGER", logger.RunAuditLogger())
    monkeypatch.setattr(config, "_config", {**DEFAULT_CONFIG, "auto_screening_enabled": False, "alerts_enabled": False,
                                          "results_dir": str(tmp_path / "results"),
                                          "data_cache_dir": str(tmp_path / "cache")})
    return tmp_path


def canonical(payload, tmp_path):
    # Only the explicitly non-business temporary root is normalized. No time,
    # monetary value, reason, ID, status, or event ordering is dropped.
    return json.dumps(payload, sort_keys=True, indent=2, default=str).replace(str(tmp_path), "<TMP>") + "\n"


def evidence(name, payload, tmp_path):
    text = canonical(payload, tmp_path)
    output = Path(os.environ.get("REFACTOR_ARTIFACTS", str(tmp_path))) / "characterization"
    output.mkdir(parents=True, exist_ok=True)
    (output / (name + ".json")).write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text())
        expected = UPDATED_CANONICAL_HASHES.get(name, baseline["sha256"][name])
        assert digest == expected, f"original/final difference: {output / (name + '.json')}"
    else:
        assert ORIGINAL_HASHES, "Original source hashes must be pinned before capture"
        for relative, expected in ORIGINAL_HASHES.items():
            assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, "Missing original baseline fixture; refusing to bless refactored code"
    return payload


def logical_rows(service):
    with sqlite3.connect(service.store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return {table: [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
                for table in ("schema_version", "execution_intents", "orders", "fills", "protective_children")}


def trace_service(monkeypatch, service, broker):
    trace = []
    for name in ("get_account", "get_clock", "get_all_positions", "get_orders",
                 "get_order_by_id", "get_order_by_client_id", "submit_order", "cancel_order_by_id"):
        original = getattr(broker, name)
        def call(*args, _name=name, _original=original, **kwargs):
            if _name == "submit_order":
                request = args[0]
                detail = {k: getattr(request, k, None) for k in
                          ("symbol", "side", "type", "qty", "notional", "client_order_id")}
            elif _name == "get_orders":
                detail = str(getattr(args[0].status, "value", args[0].status))
            else:
                detail = list(args)
            trace.append([_name, detail])
            return _original(*args, **kwargs)
        monkeypatch.setattr(broker, name, call)
    original = service.store.create_outbox
    def commit(**kwargs):
        result = original(**kwargs)
        # A distinct connection proves durability, rather than observing an
        # uncommitted object in the service's connection.
        with sqlite3.connect(service.store.db_path) as conn:
            rows = conn.execute("SELECT decision_id FROM execution_intents WHERE decision_id=?", (kwargs["decision_id"],)).fetchall()
        assert rows == [(kwargs["decision_id"],)]
        trace.append(["outbox_committed", kwargs["decision_id"]])
        return result
    monkeypatch.setattr(service.store, "create_outbox", commit)
    return trace


def test_characterize_protected_close_and_prohibited_reversal(env, fixed, monkeypatch):
    service, broker = env
    trace = trace_service(monkeypatch, service, broker)
    opened = service.execute(trade_intent=opening(stamp=STAMP), dollar_amount=1000)
    assert opened["success"] and broker.qty == 9, json.dumps(opened, indent=2)
    before = logical_rows(service)
    boundary = len(trace)
    with monkeypatch.context() as patch:
        patch.setattr(service, "enforce_exit_deadlines", lambda **kw: pytest.fail("prohibited reversal reached maintenance"))
        refused = service.execute(trade_intent=opening("SHORT", "LONG", stamp=STAMP), dollar_amount=1000)
    assert refused["broker_calls"] == 0 and len(trace) == boundary
    assert logical_rows(service) == before
    closed = service.liquidate("AAPL")
    assert closed["success"] and closed["broker_calls"] == 2 and broker.qty == 0
    close_trace = trace[boundary:]
    commit = next(i for i, event in enumerate(close_trace) if event[0] == "outbox_committed")
    cancel = next(i for i, event in enumerate(close_trace) if event[0] == "cancel_order_by_id")
    refresh = next(i for i, event in enumerate(close_trace) if i > cancel and event[0] == "get_all_positions")
    submit = next(i for i, event in enumerate(close_trace) if event[0] == "submit_order")
    assert commit < cancel < refresh < submit
    evidence("protected_close", {"trace": trace, "opened": opened, "refused": refused,
                                "closed": closed, "db": logical_rows(service)}, fixed)


@pytest.mark.parametrize("mode", ["adopt_unknown", "block_submitting", "gap_between_items"])
def test_characterize_recovery(env, fixed, monkeypatch, mode):
    from test_execution_stop_coverage_regressions import seed_pending
    service, broker = env
    if mode == "gap_between_items":
        assert service.execute(trade_intent=opening(stamp=STAMP), dollar_amount=1000)["success"]
    service.store.ensure_account_binding("audit-fixture")
    first = seed_pending(service, "AAA")
    second = seed_pending(service, "BBB")
    if mode != "gap_between_items":
        service.store.transition_order(first["order_id"], "SUBMITTING")
        if mode == "adopt_unknown":
            service.store.transition_order(first["order_id"], "UNKNOWN")
            broker.orders.append(NS(id="adopted-AAA", client_order_id=first["client_order_id"],
                symbol="AAA", side="buy", type="limit", status="accepted", qty=9,
                filled_qty=0, filled_avg_price=None, updated_at=STAMP, legs=[], notional=None))
        # Second item must not cause an unrelated opening in adoption evidence.
        service.store.transition_order(second["order_id"], "CANCELED")
    else:
        def lose_protection(request):
            for leg in broker.orders[0].legs:
                leg.status = "canceled"
            order = NS(id="recovered-AAA", client_order_id=request.client_order_id,
                symbol="AAA", side="buy", type="limit", status="accepted", qty=request.qty,
                filled_qty=0, filled_avg_price=None, updated_at=STAMP, legs=[], notional=None)
            broker.orders.append(order)
            return order
        monkeypatch.setattr(broker, "submit_order", lose_protection)
    trace = trace_service(monkeypatch, service, broker)
    result = service.startup_recover()
    posts = [event for event in trace if event[0] == "submit_order"]
    if mode == "gap_between_items":
        assert len(posts) == 1 and not result["success"]
        assert service.store.get_order(second["order_id"]) == second
        assert any("PROTECTION_GAP:" in reason for reason in result["reconciliation_reasons"])
    else:
        assert not posts
        if mode == "adopt_unknown":
            assert service.store.get_order(first["order_id"])["status"] == "ACCEPTED"
        else:
            assert not result["success"]
    evidence(mode, {"trace": trace, "result": result, "db": logical_rows(service)}, fixed)


@pytest.mark.parametrize("resume", [False, True])
def test_characterize_long_run_journal_report(fixed, monkeypatch, resume):
    from tradingagents.dataflows import config
    from tradingagents.safety import SafetyGuard
    monkeypatch.setattr("tradingagents.regime.regime_risk_multiplier", lambda *a, **kw: 1.0)
    monkeypatch.setattr("tradingagents.portfolio.adjust_new_position_notional", lambda s, a, amount, **kw: amount)
    guard = SafetyGuard(state_path=fixed / "safety/state.json", kill_switch_path=fixed / "safety/KILL_SWITCH")
    monkeypatch.setattr("tradingagents.safety.get_safety_guard", lambda: guard)
    cfg = round_fakes._valid_cfg(daily_llm_token_budget=1000)
    runtime = lr.build_runtime_config(cfg)
    runtime.update(results_dir=str(fixed / "results"), data_cache_dir=str(fixed / "cache"), daily_llm_token_budget=1000)
    trace = []
    run_id = "refactor-resume" if resume else "refactor-fresh"
    session = round_fakes.SESSION_A
    original_save = lr.save_round_journal
    def save(run_id, journal):
        original_save(run_id, journal)
        trace.append(["journal", journal["status"], {s: e["status"] for s, e in journal["symbols"].items()}])
    monkeypatch.setattr(lr, "save_round_journal", save)
    original_apply = lr._apply_runtime_config
    def apply(runtime):
        original_apply(runtime)
        trace.append(["runtime", config.get_config()["auto_screening_enabled"]])
    monkeypatch.setattr(lr, "_apply_runtime_config", apply)
    class Service(round_fakes.FakeService):
        def startup_recover(self, can_submit=None):
            assert config.get_config()["auto_screening_enabled"] is True
            assert lr.round_path(run_id, session).is_file()
            trace.append(["recover", can_submit() if can_submit else None])
            return super().startup_recover(can_submit)
        def enforce_exit_deadlines(self, can_submit=None):
            assert lr.load_round_journal(run_id, session)["maintenance_execution"]["recovery"] is not None
            trace.append(["deadlines", can_submit() if can_submit else None])
            return super().enforce_exit_deadlines(can_submit)
        def execute(self, **kwargs):
            assert lr.load_round_journal(run_id, session)["symbols"]["AAA"]["status"] == "EXECUTING"
            trace.append(["execute", kwargs["decision_id"], kwargs["dollar_amount"]])
            return super().execute(**kwargs)
    service = Service()
    graph = round_fakes.FakeGraph()
    def screening(config, refresh=False):
        assert not resume, "execution-only resume screened"
        trace.append(["screening", refresh])
        return round_fakes._fake_plan(("AAA",))
    def graph_factory(config):
        assert not resume, "execution-only resume analyzed"
        trace.append(["graph"])
        return graph
    if resume:
        journal = lr.new_round_journal(session, ["AAA"])
        journal["status"] = "RUNNING"
        journal["screening"] = {"selection_date": session, "as_of": session, "cached": False,
            "top20": [{"symbol": "AAA", "rank": 1}], "deep_analysis_set": ["AAA"],
            "overlap_holdings": [], "extra_holdings": [], "blocked_holdings": [], "description": "saved"}
        journal["symbols"]["AAA"].update(status="EXECUTING", signal="BUY", trade_intent=round_fakes._buy_intent("AAA"))
        lr.save_round_journal(run_id, journal)
        trace.clear()
    deps, _, _ = round_fakes._deps(service=service, graph=graph, symbols=("AAA",),
        screening_fn=screening, graph_factory=graph_factory, now_fn=lambda: STAMP)
    out = lr.run_daily_round(run_id=run_id, session_date=session, long_cfg=cfg, runtime=runtime, deps=deps)
    assert out["status"] == "COMPLETED" and len(service.execute_calls) == 1
    if resume:
        assert graph.calls == []
    # Terminal replay returns exactly the journal without recovery or execution.
    recovery_count = service.recover_calls
    assert lr.run_daily_round(run_id=run_id, session_date=session, long_cfg=cfg, runtime=runtime, deps=deps) == out
    assert service.recover_calls == recovery_count
    state = {"run_id": run_id, "started_at": "2026-09-08T14:00:00+00:00",
             "ends_at": "2026-10-08T14:00:00+00:00", "status": "COMPLETED",
             "expected_sessions": [session, round_fakes.SESSION_B], "finished_at": STAMP.isoformat()}
    lr.sweep_missed_sessions(run_id=run_id, expected=state["expected_sessions"], today="2026-09-10")
    final = lr.capture_account_snapshot(round_fakes.FakeBroker(equity=102000, cash=52000))
    final["phase"] = "final"
    lr.append_jsonl(lr.run_dir(run_id) / "account_snapshots.jsonl", final)
    report = lr.aggregate_final_report(state, cfg, runtime)
    assert report["coverage"]["completion_rate"] == 0.5
    paths = lr.write_final_report(report)
    files = {str(path.relative_to(lr.run_dir(run_id))): path.read_text()
             for path in sorted(lr.run_dir(run_id).rglob("*")) if path.is_file()}
    evidence("long_run_resume" if resume else "long_run_fresh",
             {"trace": trace, "journal": out, "report": report, "paths": paths,
              "files": files, "graph_calls": graph.calls}, fixed)


def test_characterize_final_report_failure_preserves_active_state(fixed, monkeypatch):
    state = {"run_id": "report-failure", "started_at": "2026-09-08T14:00:00+00:00",
             "ends_at": "2026-10-08T14:00:00+00:00", "status": "RUNNING",
             "expected_sessions": ["2026-09-08"]}
    cfg = round_fakes._valid_cfg()
    runtime = lr.build_runtime_config(cfg)
    runtime.update(results_dir=str(fixed / "results"))
    lr.save_active_state(state)
    original_bytes = b"{broken journal"
    path = lr.round_path(state["run_id"], "2026-09-08")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original_bytes)
    trace = []
    def fail_report(report):
        trace.append(["report", report["run_id"], lr.read_json(lr.active_path())["status"]])
        raise OSError("fixed report storage failure")
    monkeypatch.setattr(lr, "write_final_report", fail_report)
    deps, _, _ = round_fakes._deps(alert_fn=lambda *a: trace.append(["alert"]))
    with pytest.raises(OSError, match="fixed report storage failure"):
        lr.finalize_observation(state, cfg, runtime, deps, final_status="COMPLETED")
    assert trace == [["report", "report-failure", "COMPLETED"]]
    assert path.read_bytes() == original_bytes
    active = lr.read_json(lr.active_path())
    assert active["status"] == "COMPLETED"
    with pytest.raises(lr.LongRunStop) as exc:
        lr.load_active_state()
    assert exc.value.code == "ACTIVE_STATE_CORRUPT"
    files = {str(p.relative_to(lr.run_dir(state["run_id"]))): p.read_text()
             for p in sorted(lr.run_dir(state["run_id"]).rglob("*")) if p.is_file()}
    evidence("final_report_failure", {"trace": trace, "active": active, "files": files,
                                      "resume_error": str(exc.value)}, fixed)
