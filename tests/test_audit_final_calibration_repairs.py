"""Final calibration repairs for the pre-30-day adversarial review (2026-09-18).

Covers: filled_qty monotonicity (H1), outbox replay created-flag + spec
mismatch surfacing (H2), durable-boundary numeric validation (H3), signal
extraction membership whitelist (H5), Finnhub historical look-ahead (C2).
"""
import json
import logging

import pytest

from tradingagents.dataflows import interface
from tradingagents.dataflows.interface_utils import (
    HISTORICAL_SOURCE_UNAVAILABLE,
    current_analysis_date,
)
from tradingagents.execution.store import ExecutionStore
from tradingagents.graph.signal_processing import (
    ALLOWED_SIGNALS,
    SignalProcessor,
)


# ---------------------------------------------------------------------------
# H1 — durable filled_qty monotonicity
# ---------------------------------------------------------------------------


def make_store(tmp_path):
    return ExecutionStore(tmp_path / "exec.db")


def seed_sell_order(store, did="dec-fixture"):
    _, rows, created = store.create_outbox(
        decision_id=did,
        run_id=None,
        symbol="AAPL",
        action="SELL",
        target_position="NEUTRAL",
        payload_json=json.dumps({"kind": "fixture"}),
        orders=[
            {
                "client_order_id": "ta-" + did,
                "symbol": "AAPL",
                "side": "sell",
                "quantity": 9.0,
                "notional": None,
            }
        ],
    )
    assert created
    return rows[0]


def test_transition_order_refuses_filled_qty_regression(tmp_path):
    store = make_store(tmp_path)
    row = seed_sell_order(store)
    assert store.transition_order(row["order_id"], "SUBMITTING")[0]
    assert store.transition_order(row["order_id"], "PARTIAL", filled_qty=5.0)[0]
    # Same-state idempotent transition with a stale lower payload.
    ok, _ = store.transition_order(row["order_id"], "PARTIAL", filled_qty=2.0)
    assert ok
    assert store.get_order(row["order_id"])["filled_qty"] == 5.0
    # Genuine growth still applies; same-state idempotent FILLED keeps max.
    assert store.transition_order(row["order_id"], "FILLED", filled_qty=9.0)[0]
    ok, _ = store.transition_order(row["order_id"], "FILLED", filled_qty=1.0)
    assert ok
    assert store.get_order(row["order_id"])["filled_qty"] == 9.0


def test_sync_order_from_broker_refuses_regression_and_non_finite(tmp_path):
    store = make_store(tmp_path)
    row = seed_sell_order(store)
    store.sync_order_from_broker(
        row["order_id"], "ACCEPTED", broker_order_id="b1", filled_qty=9.0
    )
    store.sync_order_from_broker(
        row["order_id"], "PARTIAL", broker_order_id="b1", filled_qty=0.5
    )
    assert store.get_order(row["order_id"])["filled_qty"] == 9.0
    store.sync_order_from_broker(
        row["order_id"], "FILLED", broker_order_id="b1", filled_qty=float("nan")
    )
    final = store.get_order(row["order_id"])
    assert final["filled_qty"] == 9.0 and final["status"] == "FILLED"


def test_record_fill_never_rewrites_terminal_order_accounting_down(tmp_path):
    store = make_store(tmp_path)
    row = seed_sell_order(store)
    store.transition_order(row["order_id"], "SUBMITTING")
    assert store.transition_order(row["order_id"], "FILLED", filled_qty=9.0)[0]
    # A late fill event must not lower a terminal order's accounting.
    store.record_fill(execution_id="late-1", order_id=row["order_id"], qty=3.0, price=100)
    assert store.get_order(row["order_id"])["filled_qty"] == 9.0


def test_transition_order_ignores_non_finite_filled_qty(tmp_path):
    store = make_store(tmp_path)
    row = seed_sell_order(store)
    store.transition_order(row["order_id"], "SUBMITTING")
    assert store.transition_order(row["order_id"], "PARTIAL", filled_qty=4.0)[0]
    ok, updated = store.transition_order(
        row["order_id"], "PARTIAL", filled_qty=float("inf")
    )
    assert ok and updated["filled_qty"] == 4.0
    ok, updated = store.transition_order(
        row["order_id"], "PARTIAL", filled_qty=float("nan")
    )
    assert ok and updated["filled_qty"] == 4.0


# ---------------------------------------------------------------------------
# H2 — outbox replay: created flag + spec mismatch surfacing
# ---------------------------------------------------------------------------


def test_created_flag_is_insert_rowcount_not_wall_clock(tmp_path):
    store = make_store(tmp_path)
    spec = {
        "client_order_id": "ta-created-flag",
        "symbol": "AAPL",
        "side": "sell",
        "quantity": 9.0,
        "notional": None,
    }
    _, _, created_first = store.create_outbox(
        decision_id="dec-created-flag",
        run_id=None,
        symbol="AAPL",
        action="SELL",
        target_position="NEUTRAL",
        payload_json=json.dumps({"k": 1}),
        orders=[dict(spec)],
    )
    _, _, created_second = store.create_outbox(
        decision_id="dec-created-flag",
        run_id=None,
        symbol="AAPL",
        action="SELL",
        target_position="NEUTRAL",
        payload_json=json.dumps({"k": 1}),
        orders=[dict(spec, client_order_id="ta-created-flag")],
    )
    assert created_first is True
    assert created_second is False


def test_outbox_replay_with_mutated_spec_keeps_stored_rows_and_warns(
    tmp_path, caplog
):
    store = make_store(tmp_path)
    did = "dec-spec-mismatch"
    seed_sell_order(store, did=did)
    with caplog.at_level(logging.WARNING, logger="tradingagents.execution.store"):
        intent, orders, created = store.create_outbox(
            decision_id=did,
            run_id=None,
            symbol="AAPL",
            action="SELL",
            target_position="NEUTRAL",
            payload_json=json.dumps({"kind": "mutated"}),
            orders=[
                {
                    "client_order_id": "ta-" + did,
                    "symbol": "AAPL",
                    "side": "sell",
                    "quantity": 9999.0,
                    "notional": None,
                }
            ],
        )
    assert created is False
    assert float(orders[0]["quantity"]) == 9.0
    assert any("replay spec mismatch" in rec.getMessage() for rec in caplog.records)


def test_outbox_replay_with_identical_spec_stays_silent(tmp_path, caplog):
    store = make_store(tmp_path)
    did = "dec-identical-replay"
    seed_sell_order(store, did=did)
    with caplog.at_level(logging.WARNING, logger="tradingagents.execution.store"):
        _, _, created = store.create_outbox(
            decision_id=did,
            run_id=None,
            symbol="AAPL",
            action="SELL",
            target_position="NEUTRAL",
            payload_json=json.dumps({"kind": "fixture"}),
            orders=[
                {
                    "client_order_id": "ta-" + did,
                    "symbol": "AAPL",
                    "side": "sell",
                    "quantity": 9.0,
                    "notional": None,
                }
            ],
        )
    assert created is False
    assert not [r for r in caplog.records if "replay spec mismatch" in r.getMessage()]


# ---------------------------------------------------------------------------
# H3 — durable-boundary numeric validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-5.0, 0.0, float("nan"), float("inf")])
def test_create_outbox_rejects_non_positive_quantity(tmp_path, bad):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="finite and positive"):
        store.create_outbox(
            decision_id="dec-bad-qty",
            run_id=None,
            symbol="AAPL",
            action="BUY",
            target_position="LONG",
            payload_json="{}",
            orders=[
                {
                    "client_order_id": "ta-bad-qty",
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": bad,
                    "notional": None,
                }
            ],
        )
    assert store.list_all_orders() == []


def test_create_outbox_rejects_non_numeric_and_bad_notional(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="must be numeric"):
        store.create_outbox(
            decision_id="dec-str-qty",
            run_id=None,
            symbol="AAPL",
            action="BUY",
            target_position="LONG",
            payload_json="{}",
            orders=[
                {
                    "client_order_id": "ta-str",
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "abc",
                    "notional": None,
                }
            ],
        )
    with pytest.raises(ValueError, match="notional"):
        store.create_outbox(
            decision_id="dec-neg-notional",
            run_id=None,
            symbol="AAPL",
            action="BUY",
            target_position="LONG",
            payload_json="{}",
            orders=[
                {
                    "client_order_id": "ta-neg",
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": None,
                    "notional": -1000.0,
                }
            ],
        )
    assert store.list_all_orders() == []


def test_outbox_allows_deferred_size_resolution(tmp_path):
    """The F04 liquidation pre-commit persists quantity=None, notional=None."""
    store = make_store(tmp_path)
    _, orders, created = store.create_outbox(
        decision_id="dec-deferred-size",
        run_id=None,
        symbol="AAPL",
        action="SELL",
        target_position="NEUTRAL",
        payload_json="{}",
        orders=[
            {
                "client_order_id": "ta-defer",
                "symbol": "AAPL",
                "side": "sell",
                "quantity": None,
                "notional": None,
            }
        ],
    )
    assert created and orders[0]["quantity"] is None and orders[0]["notional"] is None


# ---------------------------------------------------------------------------
# H5 — signal extraction membership whitelist
# ---------------------------------------------------------------------------


class StaticLLM:
    def __init__(self, content):
        self._content = content

    def invoke(self, _messages):
        return type("Resp", (), {"content": self._content})()


def test_deterministic_patterns_still_win_without_llm():
    processor = SignalProcessor(StaticLLM("should never be reached"))
    assert processor.process_signal("FINAL TRANSACTION PROPOSAL: **BUY**") == "BUY"
    assert (
        processor.process_signal("FINAL TRANSACTION PROPOSAL: **NEUTRAL**")
        == "NEUTRAL"
    )


def test_tail_fallback_requires_word_boundaries():
    # "BUYBACK" must no longer extract BUY via substring matching; with no
    # FINAL TRANSACTION PROPOSAL pattern the (static) LLM path decides.
    processor = SignalProcessor(StaticLLM("HOLD"))
    assert processor.process_signal("we plan to BUYBACK shares next quarter") == "HOLD"


def test_llm_signal_is_whitelisted_and_fails_safe():
    assert SignalProcessor(StaticLLM("  BUY  ")).process_signal("x") == "BUY"
    processor = SignalProcessor(StaticLLM("ACCUMULATE"))
    assert processor.process_signal("rationale only") == "HOLD"
    for bad in ("BUY BUY BUY", "NO_TRADE", "", "SELL NOW!", "the answer is SHORT now"):
        assert (
            SignalProcessor(StaticLLM(bad)).process_signal("nothing deterministic")
            in ALLOWED_SIGNALS
        )


def test_every_allowed_signal_is_extractable_from_llm_response():
    for action in sorted(ALLOWED_SIGNALS):
        processor = SignalProcessor(StaticLLM(f"the decision is {action}."))
        assert processor.process_signal("nothing deterministic here") == action


# ---------------------------------------------------------------------------
# C2 — Finnhub live fallback must refuse a historical as-of
# ---------------------------------------------------------------------------


def _refuse_live(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("live Finnhub fetch must not run for a historical as-of")

    monkeypatch.setattr(interface, "fetch_company_news_live", _boom)
    monkeypatch.setattr(interface, "fetch_insider_sentiment_live", _boom)
    monkeypatch.setattr(interface, "fetch_insider_transactions_live", _boom)


def _force_cache_miss(monkeypatch):
    def miss(*_args, **_kwargs):
        raise FileNotFoundError("no cache in fixture")

    monkeypatch.setattr(interface, "get_data_in_range", miss)


def test_finnhub_news_historical_refuses_live_fallback(monkeypatch):
    _refuse_live(monkeypatch)
    _force_cache_miss(monkeypatch)
    assert (
        interface.get_finnhub_news("AAPL", "2025-01-10", 5)
        == HISTORICAL_SOURCE_UNAVAILABLE
    )


@pytest.mark.parametrize(
    "fn",
    [
        interface.get_finnhub_company_insider_sentiment,
        interface.get_finnhub_company_insider_transactions,
    ],
)
def test_finnhub_insider_historical_refuses_live_fallback(monkeypatch, fn):
    _refuse_live(monkeypatch)
    _force_cache_miss(monkeypatch)
    assert fn("AAPL", "2025-01-10", 15) == HISTORICAL_SOURCE_UNAVAILABLE


def test_finnhub_news_live_mode_still_uses_live_fallback(monkeypatch):
    _force_cache_miss(monkeypatch)
    called = {}
    monkeypatch.setattr(
        interface,
        "fetch_company_news_live",
        lambda *a, **k: (called.setdefault("hit", True), [])[1],
    )
    result = interface.get_finnhub_news("AAPL", current_analysis_date(), 5)
    assert called.get("hit") is True
    assert "No Finnhub news items found" in result

