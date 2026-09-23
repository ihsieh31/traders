"""Final dispatch regressions for Alpaca US-equity short eligibility."""

from datetime import timedelta
from enum import Enum
from types import SimpleNamespace as NS

import pytest

from test_execution_safety_plan_a import env, opening
from tradingagents.dataflows.alpaca_utils import ExecutionTradingClient


def _short_result(env, *, equity=100000, notional=1000, allow_shorts=True):
    env[1].account_equity = equity
    return env[0].execute(
        trade_intent=opening("SHORT"), dollar_amount=notional,
        allow_shorts=allow_shorts,
    )


def _rejection_text(result):
    return str(result)


def test_etb_shortable_us_equity_reaches_the_opening_post(env):
    result = _short_result(env)

    assert result["success"], result
    assert len(env[1].submits) == 1
    assert env[1].asset_calls == ["AAPL"]


def test_asset_lookup_precedes_the_final_freshness_proof(env, monkeypatch):
    import tradingagents.execution.service as service_module

    events = []
    broker = env[1]
    get_clock = broker.get_clock
    get_asset = broker.get_asset
    validate_freshness = service_module.validate_freshness

    def tracked_clock():
        events.append("clock")
        return get_clock()

    def tracked_asset(symbol):
        events.append("asset")
        return get_asset(symbol)

    def tracked_freshness(*args, **kwargs):
        events.append("freshness")
        return validate_freshness(*args, **kwargs)

    broker.get_clock = tracked_clock
    broker.get_asset = tracked_asset
    monkeypatch.setattr(service_module, "validate_freshness", tracked_freshness)

    result = _short_result(env)

    assert result["success"], result
    clock_index = max(i for i, event in enumerate(events) if event == "clock")
    asset_index = next(i for i, event in enumerate(events) if i > clock_index and event == "asset")
    freshness_index = next(
        i for i, event in enumerate(events) if i > asset_index and event == "freshness"
    )
    assert clock_index < asset_index < freshness_index


def test_slow_asset_lookup_makes_old_snapshot_fail_freshness(env, monkeypatch):
    import tradingagents.execution.authority as authority

    broker = env[1]
    get_asset = broker.get_asset
    captured_at = authority.utc_now()

    def slow_asset(symbol):
        asset = get_asset(symbol)
        monkeypatch.setattr(
            authority, "utc_now",
            lambda: captured_at + timedelta(seconds=120),
        )
        return asset

    broker.get_asset = slow_asset

    result = _short_result(env)

    assert len(broker.submits) == 0
    assert broker.asset_calls == ["AAPL"]
    assert "stale" in str(result).lower()


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"shortable": False}, "not marked shortable"),
        ({"borrow_status": "hard_to_borrow"}, "hard_to_borrow"),
        ({"borrow_status": None}, "borrow_status is unavailable"),
        ({"_missing_borrow_status": True}, "borrow_status is unavailable"),
        ({"borrow_status": "new_status"}, "borrow_status is unavailable"),
        ({"symbol": "MSFT"}, "symbol does not match"),
        ({"status": "inactive"}, "not active"),
        ({"tradable": False}, "not tradable"),
        ({"asset_class": "crypto"}, "not a US equity"),
    ],
)
def test_unavailable_or_ineligible_asset_never_posts(env, changes, reason):
    asset_fields = {
        "symbol": "AAPL", "asset_class": "us_equity", "status": "active",
        "tradable": True, "shortable": True, "borrow_status": "easy_to_borrow",
    }
    asset_fields.update(changes)
    if asset_fields.pop("_missing_borrow_status", False):
        asset_fields.pop("borrow_status")
    env[1].asset = NS(**asset_fields)

    result = _short_result(env)

    assert len(env[1].submits) == 0
    assert env[1].asset_calls == ["AAPL"]
    assert reason in _rejection_text(result)


def test_asset_lookup_timeout_fails_closed(env):
    env[1].asset_error = TimeoutError("paper API timed out")

    result = _short_result(env)

    assert len(env[1].submits) == 0
    assert env[1].asset_calls == ["AAPL"]
    assert "broker asset lookup failed" in _rejection_text(result)


@pytest.mark.parametrize("equity", [1999.99, 2000.0])
def test_short_minimum_equity_boundary(env, equity):
    # The smaller request isolates the equity eligibility threshold from the
    # existing entry policy's independent risk-based notional cap.
    result = _short_result(env, equity=equity, notional=150)

    if equity < 2000:
        assert len(env[1].submits) == 0
        assert "below $2,000" in _rejection_text(result)
    else:
        assert result["success"], result
        assert len(env[1].submits) == 1
    assert env[1].asset_calls == ["AAPL"]


def test_enum_borrow_status_is_normalized(env):
    class BorrowStatus(Enum):
        ETB = " EASY_TO_BORROW "

    env[1].asset.borrow_status = BorrowStatus.ETB
    result = _short_result(env)

    assert result["success"], result
    assert len(env[1].submits) == 1


def test_raw_asset_response_and_alpaca_class_alias_reach_the_gate(env):
    env[1].asset = {
        "symbol": "AAPL", "class": "us_equity", "status": "active",
        "tradable": True, "shortable": True, "borrow_status": "easy_to_borrow",
    }
    result = _short_result(env)

    assert result["success"], result
    assert len(env[1].submits) == 1


def test_execution_client_preserves_raw_borrow_status():
    class SDKClient:
        def __init__(self):
            self.paths = []

        def get(self, path):
            self.paths.append(path)
            return {"symbol": "AAPL", "borrow_status": "easy_to_borrow"}

        def submit_order(self, request):
            return request

    sdk_client = SDKClient()
    broker = ExecutionTradingClient(sdk_client)

    asset = broker.get_asset("AAPL")

    assert sdk_client.paths == ["/assets/AAPL"]
    assert asset["borrow_status"] == "easy_to_borrow"
    assert broker.submit_order("request") == "request"


def test_execution_client_factory_preserves_account_and_read_only_settings(monkeypatch):
    import tradingagents.dataflows.alpaca_utils as alpaca_utils

    calls = []

    class SDKClient:
        def get(self, path):
            return {"borrow_status": "easy_to_borrow"}

    sdk_client = SDKClient()

    def trading_client_factory(base_url=None, *, account=None, read_only=None):
        calls.append((base_url, account, read_only))
        return sdk_client

    monkeypatch.setattr(alpaca_utils, "get_alpaca_trading_client", trading_client_factory)

    broker = alpaca_utils.get_alpaca_execution_client(
        account="B", read_only=False
    )

    assert isinstance(broker, ExecutionTradingClient)
    assert calls == [(None, "B", False)]
    assert broker.get_asset("AAPL")["borrow_status"] == "easy_to_borrow"


def test_long_open_does_not_request_borrow_status(env):
    result = env[0].execute(trade_intent=opening("BUY"), dollar_amount=1000)

    assert result["success"], result
    assert len(env[1].submits) == 1
    assert env[1].asset_calls == []


def test_buy_cover_for_existing_short_does_not_request_borrow_status(env):
    env[1].qty = -5

    result = env[0].execute(
        trade_intent=opening("LONG", "SHORT"), dollar_amount=1000,
        allow_shorts=True,
    )

    assert result["success"], result
    assert env[1].submits
    assert env[1].asset_calls == []


def test_reducing_sell_does_not_request_borrow_status(env):
    from tradingagents.agents.schemas import RiskDecision, build_trade_intent_from_risk_decision

    env[1].qty = 5
    intent = build_trade_intent_from_risk_decision(
        symbol="AAPL", trading_mode="investment", current_position="LONG",
        allow_shorts=True,
        decision=RiskDecision(
            action="SELL", confidence="high", risk_rationale="close long",
            required_controls="close",
        ),
    ).model_dump(mode="json")

    result = env[0].execute(
        trade_intent=intent, dollar_amount=1000,
        allow_shorts=True,
    )

    assert result["success"], result
    assert env[1].submits
    assert env[1].asset_calls == []


def test_allow_shorts_false_blocks_before_asset_lookup(env):
    result = _short_result(env, allow_shorts=False)

    assert len(env[1].submits) == 0
    assert env[1].asset_calls == []
    assert "Short exposure is disabled" in _rejection_text(result)


def test_crypto_short_behavior_stays_before_asset_lookup(env):
    intent = opening("SHORT")
    intent["symbol"] = "BTC/USD"
    intent["execution_constraints"]["asset_class"] = "crypto"

    result = env[0].execute(
        trade_intent=intent, dollar_amount=1000, allow_shorts=True,
    )

    assert len(env[1].submits) == 0
    assert env[1].asset_calls == []
    assert "Crypto short exposure is not supported" in _rejection_text(result)


def test_long_to_short_reversal_only_submits_the_reducing_close(env):
    env[1].qty = 5

    result = env[0].execute(
        trade_intent=opening("SHORT", "LONG"), dollar_amount=1000,
        allow_shorts=True,
    )

    assert result["success"], result
    assert len(env[1].submits) == 1
    assert env[1].asset_calls == []
