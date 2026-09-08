"""Phase 1 remediation regressions (full review 2026-09-08).

One focused offline reproduction per finding: F01, F02, F03, F04, F05,
F06, F07, F09, F10, F11, F12. No real Alpaca mutation and no paid LLM call
— every transport is faked in-process.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tradingagents.agents  # noqa: F401  (production-safe import order)
from tradingagents.execution.authority import (
    BrokerAuthorityError,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    BrokerSnapshot,
)


def _now():
    return datetime.now(timezone.utc)


def _fresh_quote(symbol, price=100.0):
    return BrokerQuote(
        symbol.replace("/", ""), price - 0.1, price + 0.1, _now()
    )


def _snapshot(positions=None, orders=None, *, equity=100000.0, cash=80000.0,
              account_id="paper-1"):
    positions = tuple(positions or ())
    orders = tuple(orders or ())
    return BrokerSnapshot(
        observed_at=_now(),
        version="v-p1",
        account_id=account_id,
        equity=equity,
        last_equity=equity,
        cash=cash,
        buying_power=equity * 2,
        positions=positions,
        orders=orders,
        fills=(),
        gross_exposure=sum(abs(p.market_value) for p in positions),
    )


def _pos(symbol, qty, mv):
    return BrokerPosition(symbol, qty, mv)


def _order(symbol, side, *, qty=10, notional=None, status="new",
           client_id="ta-x", filled_qty=0.0):
    return BrokerOrder(
        broker_order_id=f"b-{client_id}",
        client_order_id=client_id,
        symbol=symbol,
        side=side,
        status=status,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=None,
        updated_at=_now(),
        notional=notional,
    )


def _ready_entry_policy():
    now = datetime.now(timezone.utc)
    return {
        "status": "READY", "minimum_price": 99, "maximum_price": 101,
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "exit_by": (now + timedelta(days=5)).isoformat(),
        "confirmation": "fixture observed setup",
    }


def _buy_intent(symbol="AAPL", action="BUY", current="NEUTRAL"):
    from tradingagents.agents.schemas import (
        ExecutableAction,
        RiskDecision,
        build_trade_intent_from_risk_decision,
    )

    return build_trade_intent_from_risk_decision(
        symbol=symbol,
        trading_mode="investment",
        current_position=current,
        allow_shorts=False,
        trade_date="2026-09-05",
        decision=RiskDecision(
            action=ExecutableAction(action),
            confidence="medium",
            risk_rationale="phase1 regression",
            required_controls="strict",
            entry_policy=_ready_entry_policy(),
            stop_loss_price=95.0,
        ),
    ).model_dump(mode="json")


def _state_broker(*, positions=None, orders=None, equity=100000.0,
                  cash=80000.0, submit_calls=None, cancel_calls=None,
                  reject_close=False, account_id="paper-1"):
    """In-memory broker fake with live list state."""
    state = {
        "positions": list(positions or []),
        "orders": list(orders or []),
    }
    submit_calls = submit_calls if submit_calls is not None else []
    cancel_calls = cancel_calls if cancel_calls is not None else []

    def submit_order(request):
        submit_calls.append(request)
        get = request.get if isinstance(request, dict) else (
            lambda n: getattr(request, n, None)
        )
        side = get("side")
        order = SimpleNamespace(
            id=f"broker-{len(submit_calls)}",
            client_order_id=get("client_order_id"),
            symbol=str(get("symbol")),
            side=str(getattr(side, "value", side)),
            status="rejected" if reject_close else "accepted",
            qty=str(get("qty") or 0),
            notional=get("notional"),
            filled_qty="0",
            filled_avg_price=None,
            updated_at=_now(),
        )
        state["orders"].append(order)
        if reject_close:
            from alpaca.trading.enums import OrderStatus
            raise RuntimeError("422 unprocessable: insufficient buying power")
        return order

    def get_orders(request=None):
        return list(state["orders"])

    def get_order_by_client_order_id(cid):
        return next(
            (o for o in state["orders"] if o.client_order_id == cid), None
        )

    def get_order_by_id(order_id, filter=None):
        raise BrokerAuthorityError("order lookup remains uncertain: no such order")

    broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(
            id=account_id, equity=str(equity), last_equity=str(equity),
            cash=str(cash), buying_power=str(equity * 2),
        ),
        get_all_positions=lambda: list(state["positions"]),
        get_orders=get_orders,
        get_order_by_client_order_id=get_order_by_client_order_id,
        get_order_by_id=get_order_by_id,
        submit_order=submit_order,
        cancel_order_by_id=lambda oid: cancel_calls.append(oid),
        state=state,
    )
    return broker


def _service(tmp, broker, quote_factory=None):
    from tradingagents.execution.service import ExecutionService

    return ExecutionService(
        db_path=str(Path(tmp) / "execution.db"),
        broker_factory=lambda: broker,
        quote_factory=quote_factory or (lambda s: _fresh_quote(s)),
    )


def _caps_config(**overrides):
    config = {"max_symbol_concentration_pct": 20.0, "sector_mapping": {}}
    config.update(overrides)
    return config


def _patch_caps(config):
    return patch(
        "tradingagents.execution.service._get_execution_config",
        return_value=config,
    )


class _GuardIsolated:
    """Isolate process-global state for service-level tests.

    This file sorts alphabetically BEFORE the phase test files, so any
    global state it leaves behind would poison them:

    - the real safety-guard singleton persists state under
      ~/.tradingagents/safety (kill switch, rejection streaks);
    - run_daily_round/set_config swap the ambient tradingagents config.

    Both are snapshotted and restored around every test, and the long-run
    stop flag is reset.
    """

    def setUp(self):
        import tradingagents.dataflows.config as _cfgmod
        import tradingagents.long_run as _lr

        self._cfgmod = _cfgmod
        self._lr = _lr
        self._saved_config = _cfgmod.get_config()
        guard = MagicMock()
        guard.enabled = False
        self._guard_patch = patch(
            "tradingagents.safety.get_safety_guard", return_value=guard
        )
        self._guard_patch.start()
        super().setUp()

    def tearDown(self):
        self._guard_patch.stop()
        self._lr._stop_requested = False
        self._cfgmod._config = dict(self._saved_config)
        super().tearDown()


# ---------------------------------------------------------------------------
# F01 — GPT-5 structured output adapter
# ---------------------------------------------------------------------------


class _FakeResponses:
    """Captures requests, replays a canned function_call response."""

    def __init__(self, calls, response_factory):
        self._calls = calls
        self._factory = response_factory

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return self._factory(kwargs)


class F01StructuredOutputTests(_GuardIsolated, unittest.TestCase):
    def _structured(self, schema, arguments):
        from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel

        calls = []
        factory = _FakeResponses(calls, lambda kw: SimpleNamespace(
            output=[SimpleNamespace(
                id="call-1", type="function_call", name=schema.__name__,
                arguments=arguments,
            )],
            output_text="", usage=None,
        ))
        model = GPT5ChatModel(
            model="gpt-5-mini", api_key="test-key", timeout=42.0,
        )
        structured = model.with_structured_output(schema)
        # bind_tools clones the model (with its own OpenAI client); inject
        # the fake transport into the bound clone.
        bound = structured.steps[0]
        object.__setattr__(bound, "_client", SimpleNamespace(responses=factory))
        return structured, bound, calls

    def test_screening_output_binds_as_one_function_tool(self):
        from tradingagents.screening.llm import ScreeningOutput

        structured, bound, calls = self._structured(
            ScreeningOutput,
            json.dumps({"candidates": [
                {"symbol": "AAA", "rank": 1, "screening_score": 90.0,
                 "short_reason": "ok"},
            ]}),
        )
        # A fake function-call response parses to a real ScreeningOutput.
        parsed = structured.invoke("screen")
        self.assertIsInstance(parsed, ScreeningOutput)
        # Exactly one tool, non-empty JSON schema, correct function name.
        self.assertEqual(len(bound._bound_tools), 1)
        tools = calls[0]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["name"], "ScreeningOutput")
        self.assertTrue(tools[0].get("parameters", {}).get("properties"))
        # Forced tool choice survives the bind.
        self.assertEqual(calls[0]["tool_choice"], "required")

    def test_risk_decision_binds_and_parses(self):
        from tradingagents.agents.schemas import ExecutableAction, RiskDecision

        structured, bound, calls = self._structured(
            RiskDecision,
            json.dumps({"action": "HOLD", "confidence": "low",
                        "risk_rationale": "r", "required_controls": "c"}),
        )
        parsed = structured.invoke("decide")
        self.assertIsInstance(parsed, RiskDecision)
        self.assertEqual(parsed.action, ExecutableAction.HOLD)
        self.assertEqual(calls[0]["tools"][0]["name"], "RiskDecision")
        self.assertEqual(calls[0]["tool_choice"], "required")

    def test_bound_model_preserves_timeout(self):
        from tradingagents.screening.llm import ScreeningOutput

        _, bound, _ = self._structured(
            ScreeningOutput,
            json.dumps({"candidates": [
                {"symbol": "AAA", "rank": 1, "screening_score": 90.0,
                 "short_reason": "ok"},
            ]}),
        )
        self.assertEqual(bound.timeout, 42.0)

    def test_no_second_repair_request_on_schema_failure(self):
        from tradingagents.screening.llm import ScreeningOutput

        structured, bound, calls = self._structured(
            ScreeningOutput, json.dumps({"garbage": True}),
        )
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            structured.invoke("screen")
        # No repair request, no second round: exactly one transport call.
        self.assertEqual(len(calls), 1)

    def test_unconvertible_tool_fails_at_bind_time(self):
        from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel, ToolBindingError

        model = GPT5ChatModel(model="gpt-5-mini", api_key="k")
        with self.assertRaises(ToolBindingError):
            model.bind_tools([object()])

    def test_named_tool_choice_normalizes_to_function_shape(self):
        from tradingagents.agents.utils.gpt5_llm import (
            _normalize_responses_tool_choice,
        )

        normalized = _normalize_responses_tool_choice({"type": "function",
                                                       "function": {"name": "X"}})
        self.assertEqual(normalized, {"type": "function", "name": "X"})
        self.assertEqual(_normalize_responses_tool_choice("any"), "required")


# ---------------------------------------------------------------------------
# F07 — finite timeouts on historical data + FRED
# ---------------------------------------------------------------------------


class F07TimeoutTests(_GuardIsolated, unittest.TestCase):
    def test_stock_historical_client_wraps_session_with_timeout(self):
        from tradingagents.dataflows import alpaca_utils as au

        class FakeSession:
            def request(self, *a, **kw):
                return "ok"

        class FakeClient:
            def __init__(self, *a, **kw):
                self._session = FakeSession()

        with patch.object(au, "get_api_key", return_value="k"), \
             patch.object(au, "StockHistoricalDataClient", FakeClient):
            client = au.get_alpaca_stock_client()
        # The session request is wrapped with a functools.partial carrying
        # the fixed connect/read timeout.
        wrapped = client._session.request
        self.assertTrue(hasattr(wrapped, "keywords"), "session.request must be partial-wrapped")
        self.assertIsNotNone(wrapped.keywords.get("timeout"))

    def test_crypto_historical_client_wraps_session_with_timeout(self):
        from tradingagents.dataflows import alpaca_utils as au

        class FakeSession:
            def request(self, *a, **kw):
                return "ok"

        class FakeCryptoClient:
            def __init__(self, *a, **kw):
                self._session = FakeSession()

        with patch.object(au, "get_api_key", return_value="k"), \
             patch.object(au, "CryptoHistoricalDataClient", FakeCryptoClient):
            client = au.get_alpaca_crypto_client()
        wrapped = client._session.request
        self.assertTrue(hasattr(wrapped, "keywords"), "session.request must be partial-wrapped")
        self.assertIsNotNone(wrapped.keywords.get("timeout"))

    def test_stock_historical_client_without_session_fails_closed(self):
        from tradingagents.dataflows import alpaca_utils as au

        class BareClient:
            def __init__(self, *a, **kw):
                pass

        with patch.object(au, "get_api_key", return_value="k"), \
             patch.object(au, "StockHistoricalDataClient", BareClient):
            with self.assertRaises(RuntimeError):
                au.get_alpaca_stock_client()

    def test_fred_request_carries_timeout(self):
        from tradingagents.dataflows import macro_utils

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {}

        def fake_get(url, params=None, timeout=None):
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.object(macro_utils.requests, "get", fake_get), \
             patch.object(macro_utils, "get_fred_api_key", return_value="k"):
            macro_utils.get_fred_data("FEDFUNDS", "2026-01-01", "2026-02-01")
        self.assertIsNotNone(captured.get("timeout"))


# ---------------------------------------------------------------------------
# F03 — outstanding orders valued with their own symbol's quote
# ---------------------------------------------------------------------------


class F03PerSymbolQuoteTests(_GuardIsolated, unittest.TestCase):
    def test_pure_evaluator_uses_per_symbol_prices(self):
        from tradingagents.risk.exposure import outstanding_increasing_notional

        snapshot = _snapshot(orders=[
            _order("XYZ", "buy", qty=10, client_id="ta-xyz"),
        ])
        # $10 for XYZ (its own quote) — never the $10 candidate price.
        total, fully = outstanding_increasing_notional(
            snapshot, reference_prices={"XYZ": 1000.0}
        )
        self.assertEqual(total, 10000.0)
        self.assertTrue(fully)

    def test_candidate_quote_never_valued_across_symbols(self):
        # The adversarial case: XYZ buy 10 with quote $1,000; candidate ABC
        # at $10. The old scalar behavior valued XYZ at $100.
        snapshot = _snapshot(orders=[
            _order("XYZ", "buy", qty=10, client_id="ta-xyz"),
        ])
        from tradingagents.risk.exposure import evaluate_opening_exposure

        decision = evaluate_opening_exposure(
            symbol="ABC",
            proposed_notional=5000.0,
            snapshot=snapshot,
            quote_price=10.0,  # candidate quote only
            symbol_cap_pct=25.0,
            gross_cap_pct=3.0,  # gross cap 3000: 10000 outstanding blocks
            sector_mapping={},
        )
        self.assertFalse(decision.approved)
        self.assertIn("estimate", decision.reason.lower())

    def test_per_symbol_quote_blocks_or_allows_correctly(self):
        from tradingagents.risk.exposure import evaluate_opening_exposure

        snapshot = _snapshot(orders=[
            _order("XYZ", "buy", qty=10, client_id="ta-xyz"),
        ])
        # With the correct $1,000 XYZ quote, gross headroom (3000) minus
        # 10000 outstanding is negative => reject any new exposure.
        decision = evaluate_opening_exposure(
            symbol="ABC",
            proposed_notional=500.0,
            snapshot=snapshot,
            quote_price=10.0,
            reference_prices={"XYZ": 1000.0},
            symbol_cap_pct=25.0,
            gross_cap_pct=3.0,
            sector_mapping={},
        )
        self.assertFalse(decision.approved)

    def test_service_fails_closed_without_required_quote(self):
        quotes = {}

        def quote_factory(symbol):
            if symbol in quotes:
                return quotes[symbol]
            raise BrokerAuthorityError(f"no quote for {symbol}")

        broker = _state_broker(orders=[
            _order("XYZ", "buy", qty=10, client_id="manual-xyz"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker, quote_factory=quote_factory)
            with _patch_caps(_caps_config()), \
                 patch("tradingagents.screening.gate.check_entry_allowed",
                       return_value=None):
                result = svc.execute(
                    trade_intent=_buy_intent("ABC"),
                    dollar_amount=500.0,
                )
            self.assertFalse(result.get("success"))
            self.assertEqual(result.get("broker_calls", 0), 0)
            self.assertEqual(len(broker.state["orders"]), 1)  # no submit

    def test_service_values_outstanding_with_own_quote(self):
        # Caps low enough that the XYZ outstanding order must block ABC.
        # The candidate's quote stays inside its authorized entry range
        # (99-101); XYZ's own quote is $1,000.
        def quote_factory(symbol):
            return _fresh_quote(symbol, price=1000.0 if symbol == "XYZ" else 100.0)

        broker = _state_broker(orders=[
            _order("XYZ", "buy", qty=10, client_id="manual-xyz"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker, quote_factory=quote_factory)
            with _patch_caps(_caps_config(
                max_symbol_concentration_pct=25.0,
                portfolio_max_gross_exposure_pct=3.0,
            )), patch("tradingagents.screening.gate.check_entry_allowed",
                      return_value=None):
                result = svc.execute(
                    trade_intent=_buy_intent("ABC"),
                    dollar_amount=500.0,
                )
            self.assertFalse(result.get("success"))
            self.assertIn("Exposure cap rejected", result.get("error", ""))
            # Zero broker submits for the candidate.
            self.assertEqual(result.get("broker_calls", 0), 0)


# ---------------------------------------------------------------------------
# F02 — recovery refreshes authority between mutations
# ---------------------------------------------------------------------------


class F02RecoveryRefreshTests(_GuardIsolated, unittest.TestCase):
    def _seed_pending(self, svc, symbol, notional, tag):
        from tradingagents.execution.store import client_order_id_for

        did = f"dec-seed-{tag}"
        coid = client_order_id_for(did, symbol, "buy", role="open", seq=0)
        intent_row, orders, _ = svc.store.create_outbox(
            decision_id=did,
            run_id=None,
            symbol=symbol,
            action="BUY",
            target_position="LONG",
            payload_json=json.dumps(_buy_intent(symbol)),
            orders=[{"client_order_id": coid, "symbol": symbol, "side": "buy",
                     "quantity": None, "notional": notional}],
        )
        return orders[0]

    def test_second_recovery_sees_first_resubmit(self):
        # Two missing PENDING buys of ~2500 each, gross cap 3% (3000):
        # after the first resubmit the second must see it live and be
        # clipped/refused; aggregate live exposure stays <= 3000.
        quotes = {}
        def quote_factory(symbol):
            if symbol not in quotes:
                quotes[symbol] = _fresh_quote(symbol, price=100.0)
            return quotes[symbol]

        submit_calls = []
        broker = _state_broker(submit_calls=submit_calls)
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker, quote_factory=quote_factory)
            first = self._seed_pending(svc, "AAA", 2500.0, "a")
            second = self._seed_pending(svc, "BBB", 2500.0, "b")
            # The recovery resubmit runs the Phase C entry gate; keep it
            # open (the gate itself is covered by its own suite).
            with _patch_caps(_caps_config(
                max_symbol_concentration_pct=25.0,
                portfolio_max_gross_exposure_pct=3.0,
            )), patch("tradingagents.screening.gate.check_entry_allowed",
                      return_value=None):
                result = svc.startup_recover()
            self.assertTrue(result.get("success"))
            # Both resubmitted, but the second one clipped to the remaining
            # 500 headroom (3000 - 2500 live after the first submit).
            # Protective brackets carry whole-share qty; value each request
            # with the worst authorized price (101).
            def _request_notional(request):
                qty = float(getattr(request, "qty", 0) or 0)
                notional = getattr(request, "notional", None)
                if notional:
                    return float(notional)
                return qty * 101.0

            total = sum(_request_notional(r) for r in submit_calls)
            self.assertLessEqual(total, 3000.0 + 1e-6)
            self.assertGreaterEqual(len(submit_calls), 2)
            # The second request's notional must reflect the first one's
            # presence: ~500 left (whole-share bracket qty may round up by
            # less than one share's price), not another full 2500.
            second_requests = [r for r in submit_calls
                               if getattr(r, "client_order_id", None)
                               == second["client_order_id"]]
            self.assertTrue(second_requests)
            second_notional = _request_notional(second_requests[0])
            self.assertLessEqual(second_notional, 500.0 + 101.0)
            self.assertLess(second_notional, 2500.0)

    def test_adoption_visible_to_next_recovery_item(self):
        quotes = {}
        def quote_factory(symbol):
            if symbol not in quotes:
                quotes[symbol] = _fresh_quote(symbol, price=100.0)
            return quotes[symbol]

        submit_calls = []
        broker = _state_broker(submit_calls=submit_calls)
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker, quote_factory=quote_factory)
            first = self._seed_pending(svc, "AAA", 1000.0, "a")
            second = self._seed_pending(svc, "BBB", 1000.0, "b")
            # The broker already has AAA's order: adoption, not resubmit.
            broker.state["orders"].append(SimpleNamespace(
                id="broker-adopt", client_order_id=first["client_order_id"],
                symbol="AAA", side="buy", status="accepted", qty="0",
                notional="1000", filled_qty="0", filled_avg_price=None,
                updated_at=_now(),
            ))
            with _patch_caps(_caps_config(
                max_symbol_concentration_pct=25.0,
                portfolio_max_gross_exposure_pct=5.0,
            )), patch("tradingagents.screening.gate.check_entry_allowed",
                      return_value=None):
                result = svc.startup_recover()
            row = svc.store.get_order(first["order_id"])
            self.assertEqual(row["broker_order_id"], "broker-adopt")
            self.assertTrue(result.get("success"))

    def test_refresh_failure_stops_before_second_submit(self):
        quotes = {}
        def quote_factory(symbol):
            if symbol not in quotes:
                quotes[symbol] = _fresh_quote(symbol, price=100.0)
            return quotes[symbol]

        submit_calls = []
        broker = _state_broker(submit_calls=submit_calls)
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker, quote_factory=quote_factory)
            self._seed_pending(svc, "AAA", 1000.0, "a")
            self._seed_pending(svc, "BBB", 1000.0, "b")
            with _patch_caps(_caps_config(
                max_symbol_concentration_pct=25.0,
                portfolio_max_gross_exposure_pct=100.0,
            )), patch("tradingagents.screening.gate.check_entry_allowed",
                      return_value=None), patch(
                "tradingagents.execution.service.capture_broker_snapshot",
                side_effect=[
                    # 1: initial identity capture, 2: locked-scope snapshot,
                    # 3: the after-first-mutation refresh — it explodes, so
                    # recovery must stop BEFORE the second resubmit.
                    _snapshot(account_id="paper-1"),
                    _snapshot(account_id="paper-1"),
                    BrokerAuthorityError("refresh exploded"),
                ],
            ):
                result = svc.startup_recover()
            self.assertFalse(result.get("success"))
            # Only the FIRST resubmit happened; the second never reached POST.
            self.assertLessEqual(len(submit_calls), 1)


# ---------------------------------------------------------------------------
# F09 — snapshot includes authoritative OPEN orders
# ---------------------------------------------------------------------------


class F09OpenOrderSnapshotTests(_GuardIsolated, unittest.TestCase):
    def _fake_broker(self, recent, open_orders):
        return SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity="100000", last_equity="100000",
                cash="80000", buying_power="160000",
            ),
            get_all_positions=lambda: [],
            get_orders=lambda request=None: (
                list(open_orders)
                if getattr(getattr(request, "status", None), "value", None) == "open"
                else list(recent)
            ),
        )

    def test_open_order_missing_from_recent_history_is_included(self):
        from tradingagents.execution.authority import capture_broker_snapshot

        def _raw(cid, status):
            return SimpleNamespace(
                id=f"b-{cid}", client_order_id=cid, symbol="AAPL",
                side="sell", status=status, qty="1", notional=None,
                filled_qty="0", filled_avg_price=None, updated_at=_now(),
            )

        recent = [_raw(f"ta-c{i:03d}", "canceled") for i in range(500)]
        # The older still-live stop is ABSENT from the recent ALL list but
        # present in the authoritative OPEN listing.
        live_stop = _raw("ta-live-stop", "new")
        open_orders = [live_stop]
        broker = self._fake_broker(recent, open_orders)
        snapshot = capture_broker_snapshot(broker, sleep=lambda _: None)
        symbols = [o.client_order_id for o in snapshot.orders]
        self.assertIn("ta-live-stop", symbols)

    def test_conflicting_duplicate_identity_fails_closed(self):
        from tradingagents.execution.authority import capture_broker_snapshot

        def _raw(cid, symbol, side):
            return SimpleNamespace(
                id="b-dup", client_order_id=cid, symbol=symbol, side=side,
                status="new", qty="1", notional=None, filled_qty="0",
                filled_avg_price=None, updated_at=_now(),
            )

        broker = self._fake_broker(
            [_raw("ta-a", "AAPL", "sell")],
            [_raw("ta-a", "MSFT", "sell")],
        )
        with self.assertRaises(BrokerAuthorityError):
            capture_broker_snapshot(broker, sleep=lambda _: None)

    def test_open_list_at_api_limit_fails_closed(self):
        from tradingagents.execution.authority import API_ORDER_LIMIT, capture_broker_snapshot

        def _raw(cid):
            return SimpleNamespace(
                id=f"b-{cid}", client_order_id=cid, symbol="AAPL",
                side="sell", status="new", qty="1", notional=None,
                filled_qty="0", filled_avg_price=None, updated_at=_now(),
            )

        open_orders = [_raw(f"ta-o{i:04d}") for i in range(API_ORDER_LIMIT)]
        broker = self._fake_broker([], open_orders)
        with self.assertRaises(BrokerAuthorityError) as ctx:
            capture_broker_snapshot(broker, sleep=lambda _: None)
        self.assertIn("API limit", str(ctx.exception))


# ---------------------------------------------------------------------------
# F11 — corrupt quarantine state fails closed
# ---------------------------------------------------------------------------


class F11QuarantineCorruptionTests(_GuardIsolated, unittest.TestCase):
    def test_missing_file_is_empty_ledger(self):
        from tradingagents.risk.corporate_actions import QuarantineStore

        with tempfile.TemporaryDirectory() as tmp:
            store = QuarantineStore(Path(tmp) / "q.json")
            self.assertEqual(store.all_active(), [])

    def test_corrupt_file_raises_and_blocks_opening(self):
        from tradingagents.risk.corporate_actions import (
            QuarantineStateError,
            QuarantineStore,
        )

        with tempfile.TemporaryDirectory() as tmp:
            # build_quarantine_gate reads <results_dir>/quarantine.json.
            path = Path(tmp) / "quarantine.json"
            store = QuarantineStore(path)
            store.quarantine(symbol="NVDA", reason="split")
            # Corrupt the same file.
            path.write_text("{not json at all", encoding="utf-8")
            with self.assertRaises(QuarantineStateError):
                QuarantineStore(path)
            # The execution gate refuses new risk (fail-closed, zero posts).
            posts = []
            broker = _state_broker(submit_calls=posts)
            svc = _service(tmp, broker)
            svc._quarantine_gate = None  # force rebuild from the corrupt file
            from tradingagents.risk.corporate_actions import (
                build_quarantine_gate,
            )
            with _patch_caps(_caps_config(
                results_dir=str(tmp),
                corporate_action_events=[],
            )):
                gate = build_quarantine_gate({"results_dir": str(tmp)})
                # build_quarantine_gate swallows the error into None...
                self.assertIsNone(gate)
                rejection = svc._quarantine_rejection("NVDA")
            self.assertIsNotNone(rejection)
            self.assertIn("unavailable", rejection["error"])

    def test_malformed_top_level_fails_closed(self):
        from tradingagents.risk.corporate_actions import (
            QuarantineStateError,
            QuarantineStore,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text('["not", "a", "dict"]', encoding="utf-8")
            with self.assertRaises(QuarantineStateError):
                QuarantineStore(path)

    def test_malformed_symbol_shape_fails_closed(self):
        from tradingagents.risk.corporate_actions import (
            QuarantineStateError,
            QuarantineStore,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps({"NVDA": "oops"}), encoding="utf-8")
            with self.assertRaises(QuarantineStateError):
                QuarantineStore(path)

    def test_active_quarantine_blocks_opening_but_not_exit(self):
        from tradingagents.risk.corporate_actions import QuarantineGate, QuarantineStore

        with tempfile.TemporaryDirectory() as tmp:
            store = QuarantineStore(Path(tmp) / "q.json")
            store.quarantine(symbol="NVDA", reason="split")
            gate = QuarantineGate(store)
            self.assertIsNotNone(gate.check("NVDA"))
            # Verified risk-reducing exits bypass the opening gate entirely
            # (service routes closes through _verified_reducing_exit).
            snapshot = _snapshot(positions=[_pos("NVDA", 10, 1000.0)])
            broker = _state_broker(positions=snapshot.positions)
            with tempfile.TemporaryDirectory() as tmp2:
                svc = _service(tmp2, broker)
                svc._quarantine_gate = gate
                with _patch_caps(_caps_config()):
                    result = svc.execute(
                        trade_intent=_buy_intent("NVDA", action="SELL"),
                        current_position="LONG",
                        trade_intent_action_note=None,
                    ) if False else svc.execute(
                        trade_intent={
                            **_buy_intent("NVDA", action="SELL"),
                        },
                        current_position="LONG",
                    )
                # The quarantine rejection must NOT be the blocker for a
                # close; whatever the outcome, it is not a quarantined
                # rejection (closes keep the Phase A path).
                self.assertFalse(result.get("quarantined"))


# ---------------------------------------------------------------------------
# F04 — protection gap after a failed close
# ---------------------------------------------------------------------------


class F04ProtectionGapTests(_GuardIsolated, unittest.TestCase):
    def _positioned_broker(self, *, reject_close):
        stop = SimpleNamespace(
            id="b-stop", client_order_id="ta-stop-1", symbol="AAPL",
            side="sell", status="new", qty="9", notional=None, filled_qty="0",
            filled_avg_price=None, updated_at=_now(),
        )
        submit_calls, cancel_calls = [], []
        state = {"positions": [], "orders": [stop]}

        def submit_order(request):
            submit_calls.append(request)
            get = request.get if isinstance(request, dict) else (
                lambda n: getattr(request, n, None)
            )
            if reject_close:
                raise RuntimeError("422 rejected: close failed")
            order = SimpleNamespace(
                id="b-close", client_order_id=get("client_order_id"),
                symbol=str(get("symbol")),
                side=str(getattr(get("side"), "value", get("side"))),
                status="accepted", qty=str(get("qty") or 0), notional=None,
                filled_qty="0", filled_avg_price=None, updated_at=_now(),
            )
            state["orders"].append(order)
            return order

        def cancel_order_by_id(oid):
            cancel_calls.append(oid)
            # Real brokers keep canceled orders visible in history.
            for o in state["orders"]:
                if getattr(o, "id", None) == oid:
                    o.status = "canceled"

        def get_order_by_id(oid, filter=None):
            if oid == "b-parent":
                return SimpleNamespace(id="b-parent", legs=[SimpleNamespace(
                    id="b-stop", client_order_id="ta-stop-1",
                )])
            raise BrokerAuthorityError("order lookup remains uncertain")

        broker = SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity="100000", last_equity="100000",
                cash="80000", buying_power="160000",
            ),
            get_all_positions=lambda: list(state["positions"]),
            get_orders=lambda request=None: list(state["orders"]),
            get_order_by_client_order_id=lambda cid: next(
                (o for o in state["orders"] if o.client_order_id == cid), None),
            get_order_by_id=get_order_by_id,
            submit_order=submit_order,
            cancel_order_by_id=cancel_order_by_id,
            state=state,
            _submit_calls=submit_calls,
            _cancel_calls=cancel_calls,
        )
        return broker

    def _seed_protected_position(self, svc, broker):
        """A durable 9-share AAPL lot with a registered protective stop."""
        from tradingagents.execution.store import client_order_id_for

        did = "dec-parent"
        coid = client_order_id_for(did, "AAPL", "buy", role="open", seq=0)
        intent_row, orders, _ = svc.store.create_outbox(
            decision_id=did,
            run_id=None,
            symbol="AAPL",
            action="BUY",
            target_position="LONG",
            payload_json=json.dumps(_buy_intent("AAPL")),
            orders=[{"client_order_id": coid, "symbol": "AAPL", "side": "buy",
                     "quantity": 9, "notional": None}],
        )
        parent = orders[0]
        svc.store.sync_order_from_broker(
            parent["order_id"], "FILLED", broker_order_id="b-parent",
            filled_qty=9,
        )
        # Register the broker-visible protective child (proven by the
        # nested parent lookup in _reconcile_snapshot).
        broker.state["orders"].append(SimpleNamespace(
            id="b-stop", client_order_id="ta-stop-1", symbol="AAPL",
            side="sell", status="new", qty="9", notional=None, filled_qty="0",
            filled_avg_price=None, updated_at=_now(),
        ))
        child_view = SimpleNamespace(
            id="b-stop", client_order_id="ta-stop-1", symbol="AAPL",
            side="sell", qty=9, filled_qty=0, broker_order_id="b-stop",
        )
        svc.store.register_protective_child(
            svc.store.get_order(parent["order_id"]), child_view,
        )
        return parent

    def test_failed_close_persists_protection_gap_pause(self):
        broker = self._positioned_broker(reject_close=True)
        broker.state["positions"] = [
            SimpleNamespace(symbol="AAPL", qty="9", market_value="900",
                            avg_entry_price="100", unrealized_pl="0",
                            current_price="100"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            self._seed_protected_position(svc, broker)
            with _patch_caps(_caps_config()):
                result = svc.liquidate("AAPL")
            # Position remains, stop is gone, and the result is NOT clean.
            self.assertFalse(result.get("success"))
            self.assertTrue(result.get("paused"))
            self.assertTrue(result.get("protection_gap"))
            state = svc.store.get_account_state("paper-1")
            self.assertEqual(state["state"], "PAUSED")
            reasons = json.loads(state["reasons_json"])
            self.assertTrue(
                any(r.startswith("PROTECTION_GAP:") for r in reasons),
                reasons,
            )
            # A subsequent BUY/open attempt makes zero submit calls.
            with _patch_caps(_caps_config()), \
                 patch("tradingagents.screening.gate.check_entry_allowed",
                       return_value=None):
                blocked = svc.execute(
                    trade_intent=_buy_intent("AAPL"),
                    dollar_amount=1000.0,
                )
            self.assertFalse(blocked.get("success"))
            self.assertEqual(blocked.get("broker_calls", 0), 0)

    def test_durable_commit_failure_cancels_nothing(self):
        broker = self._positioned_broker(reject_close=False)
        broker.state["positions"] = [
            SimpleNamespace(symbol="AAPL", qty="9", market_value="900",
                            avg_entry_price="100", unrealized_pl="0",
                            current_price="100"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            self._seed_protected_position(svc, broker)
            with patch.object(
                svc.store, "create_outbox",
                side_effect=RuntimeError("disk exploded"),
            ):
                result = svc.liquidate("AAPL")
            self.assertFalse(result.get("success"))
            # Protections untouched: zero cancel calls.
            self.assertEqual(len(broker._cancel_calls), 0)

    def test_close_accepted_live_no_false_gap(self):
        broker = self._positioned_broker(reject_close=False)
        broker.state["positions"] = [
            SimpleNamespace(symbol="AAPL", qty="9", market_value="900",
                            avg_entry_price="100", unrealized_pl="0",
                            current_price="100"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            self._seed_protected_position(svc, broker)
            with _patch_caps(_caps_config()):
                result = svc.liquidate("AAPL")
            self.assertFalse(result.get("protection_gap", False))
            state = svc.store.get_account_state("paper-1")
            if state is not None:
                reasons = json.loads(state["reasons_json"])
                self.assertFalse(
                    any(r.startswith("PROTECTION_GAP:") for r in reasons)
                )

    def test_persisted_gap_survives_restart_until_proven_safe(self):
        broker = self._positioned_broker(reject_close=True)
        broker.state["positions"] = [
            SimpleNamespace(symbol="AAPL", qty="9", market_value="900",
                            avg_entry_price="100", unrealized_pl="0",
                            current_price="100"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            self._seed_protected_position(svc, broker)
            with _patch_caps(_caps_config()):
                svc.liquidate("AAPL")
            # Restart: a fresh service over the same store stays PAUSED.
            svc2 = _service(tmp, broker)
            with _patch_caps(_caps_config()):
                recovered = svc2.startup_recover()
            self.assertFalse(recovered.get("success"))
            self.assertIn(
                "PROTECTION_GAP:",
                str(recovered.get("reconciliation_reasons")),
            )


# ---------------------------------------------------------------------------
# F05 — WebUI liquidation identity
# ---------------------------------------------------------------------------


class F05LiquidationIdentityTests(_GuardIsolated, unittest.TestCase):
    def test_service_generates_distinct_ids_per_call(self):
        # The WebUI must not pass a decision_id; the service's per-call
        # identity guarantees a re-entered position's liquidation is never
        # deduped against an old lifecycle.
        broker = _state_broker()
        broker.state["positions"] = [
            SimpleNamespace(symbol="AAPL", qty="5", market_value="500",
                            avg_entry_price="100", unrealized_pl="0",
                            current_price="100"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            with _patch_caps(_caps_config()):
                first = svc.liquidate("AAPL")
            self.assertTrue(first.get("success"))
            # The first close fills (that is how the position was exited and
            # re-entered): the prior close is terminal history now.
            for order in broker.state["orders"]:
                if order.client_order_id == first["orders"][0]["client_order_id"]:
                    order.status = "filled"
                    order.filled_qty = "5"
                    order.filled_avg_price = "100"
            # Re-enter the same position, reload the page (counter reset),
            # liquidate again — a NEW durable intent must be created.
            broker.state["positions"] = [
                SimpleNamespace(symbol="AAPL", qty="5", market_value="500",
                                avg_entry_price="100", unrealized_pl="0",
                                current_price="100"),
            ]
            with _patch_caps(_caps_config()):
                second = svc.liquidate("AAPL")
            self.assertTrue(second.get("success"))
            self.assertNotEqual(first["decision_id"], second["decision_id"])
            self.assertNotEqual(first["intent_id"], second["intent_id"])

    def test_webui_callback_no_longer_passes_decision_id(self):
        source = Path(
            "webui/callbacks/trading_callbacks.py"
        ).read_text(encoding="utf-8") if Path(
            "webui/callbacks/trading_callbacks.py"
        ).exists() else (Path(__file__).resolve().parents[1]
                         / "webui/callbacks/trading_callbacks.py").read_text(
            encoding="utf-8")
        self.assertNotIn("ui-liquidate", source)


# ---------------------------------------------------------------------------
# F06 — preflight is read-only; recovery moves behind authorization
# ---------------------------------------------------------------------------


class _F06Deps:
    pass


class F06PreflightReadOnlyTests(_GuardIsolated, unittest.TestCase):
    def _run(self, *, authorize):
        from tradingagents import long_run as lr

        submit_calls, cancel_calls = [], []
        broker = _state_broker(submit_calls=submit_calls,
                               cancel_calls=cancel_calls)
        recover_calls = {"n": 0}

        class RecoveryService:
            def startup_recover(self):
                recover_calls["n"] += 1
                # A resubmit would happen here: exercise it on the fake.
                return {"success": True, "account_execution_state": "CLEAN",
                        "reconciliation_reasons": []}

        deps = lr.LongRunDeps(
            broker_client_factory=lambda: broker,
            execution_service_factory=lambda: RecoveryService(),
            calendar_rows=[],
        )
        return lr, deps, broker, submit_calls, cancel_calls, recover_calls

    def test_run_preflight_never_mutates(self):
        from tradingagents import long_run as lr

        lr_local, deps, broker, submit_calls, cancel_calls, recover_calls = (
            self._run(authorize=False))
        cfg = self._valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        with patch("tradingagents.llm_clients.roles._resolve_provider_key",
                   return_value="k"), \
             patch.object(lr, "_default_llm_probe",
                          return_value={"ok": True}):
            result = lr.run_preflight(cfg, runtime, deps)
        self.assertTrue(result["ok"])
        self.assertEqual(len(submit_calls), 0)
        self.assertEqual(len(cancel_calls), 0)
        self.assertEqual(recover_calls["n"], 0)

    def test_post_authorization_recovery_runs_and_creates_nothing_on_failure(self):
        from tradingagents import long_run as lr

        _, deps, broker, submit_calls, cancel_calls, recover_calls = (
            self._run(authorize=True))
        # Recovery failure must raise, leaving no observation state.
        class FailingService:
            def startup_recover(self):
                return {"success": False,
                        "reconciliation_reasons": ["unresolved UNKNOWN"]}

        deps.execution_service_factory = lambda: FailingService()
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_post_authorization_recovery(deps)
        self.assertEqual(ctx.exception.code, "PREFLIGHT_FAILED")
        from tradingagents.long_run import active_path

        self.assertFalse(active_path().exists())

    def _valid_cfg(self):
        from tradingagents import long_run as lr

        cfg = lr.default_long_run_config()
        cfg.update({
            "duration_calendar_days": 30,
            "run_time_et": "11:00",
            "base_trade_notional_usd": 1000.0,
            "analysts": ["market"],
            "research_depth": 3,
            "output_language": "English",
            "analysis_provider": "openai",
            "analysis_model": "gpt-fake-analysis",
            "decision_provider": "openai",
            "decision_model": "gpt-fake-decision",
            "screening_provider": "openai",
            "screening_model": "gpt-fake-screening",
        })
        return cfg


# ---------------------------------------------------------------------------
# F12 — run-log recovery bound to the exact observation
# ---------------------------------------------------------------------------


class F12ObservationBindingTests(unittest.TestCase):
    def _write_run(self, runs_dir, name, *, observation_id, symbol_intent,
                   started_at, status="completed", trade_date="2026-09-08"):
        runs_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": name, "symbol": "AAA", "trade_date": trade_date,
            "status": status, "started_at": started_at,
            "metadata": {
                "source": "long_run",
                "long_run_observation_id": observation_id,
            } if observation_id is not None else {"source": "webui_stream"},
            "snapshots": {"final_state": {
                "final_trade_intent": symbol_intent,
            }},
            "summary": {},
        }
        (runs_dir / f"{name}.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def test_recovery_selects_matching_observation_not_newer_manual(self):
        from tradingagents.long_run import _recover_intent_from_run_log

        hold = {**_buy_intent("AAA"), "action": "HOLD"}
        buy = _buy_intent("AAA")
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "AAA" / "TradingAgentsStrategy_logs" / "runs"
            self._write_run(runs, "r-old", observation_id="obs-1",
                            symbol_intent=hold,
                            started_at="2026-09-08T14:00:00+00:00")
            # A NEWER run from a different (manual) source: must be ignored.
            self._write_run(runs, "r-new-manual", observation_id=None,
                            symbol_intent=buy,
                            started_at="2026-09-08T15:00:00+00:00")
            recovered = _recover_intent_from_run_log(
                "AAA", "2026-09-08",
                observation_id="obs-1", results_dir=str(tmp),
            )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.get("action"), "HOLD")

    def test_newer_other_observation_run_ignored(self):
        from tradingagents.long_run import _recover_intent_from_run_log

        hold = {**_buy_intent("AAA"), "action": "HOLD"}
        buy = _buy_intent("AAA")
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "AAA" / "TradingAgentsStrategy_logs" / "runs"
            self._write_run(runs, "r-old", observation_id="obs-1",
                            symbol_intent=hold,
                            started_at="2026-09-08T14:00:00+00:00")
            self._write_run(runs, "r-new-obs2", observation_id="obs-2",
                            symbol_intent=buy,
                            started_at="2026-09-08T15:00:00+00:00")
            recovered = _recover_intent_from_run_log(
                "AAA", "2026-09-08",
                observation_id="obs-1", results_dir=str(tmp),
            )
        self.assertEqual(recovered.get("action"), "HOLD")

    def test_custom_results_dir_wins(self):
        from tradingagents.long_run import _recover_intent_from_run_log

        hold = {**_buy_intent("AAA"), "action": "HOLD"}
        buy = _buy_intent("AAA")
        with tempfile.TemporaryDirectory() as tmp:
            # Default directory holds a conflicting manual BUY...
            default_runs = (Path(tmp) / "AAA" / "TradingAgentsStrategy_logs"
                            / "runs")
            self._write_run(default_runs, "r-default", observation_id=None,
                            symbol_intent=buy,
                            started_at="2026-09-08T16:00:00+00:00")
            # ...while the custom results_dir holds the matching HOLD.
            custom_root = Path(tmp) / "custom"
            custom_runs = (custom_root / "AAA" / "TradingAgentsStrategy_logs"
                           / "runs")
            self._write_run(custom_runs, "r-custom", observation_id="obs-1",
                            symbol_intent=hold,
                            started_at="2026-09-08T14:00:00+00:00")
            recovered = _recover_intent_from_run_log(
                "AAA", "2026-09-08",
                observation_id="obs-1", results_dir=str(custom_root),
            )
        self.assertEqual(recovered.get("action"), "HOLD")

    def test_no_metadata_match_means_no_recovery(self):
        from tradingagents.long_run import _recover_intent_from_run_log

        buy = _buy_intent("AAA")
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "AAA" / "TradingAgentsStrategy_logs" / "runs"
            self._write_run(runs, "r-manual", observation_id=None,
                            symbol_intent=buy,
                            started_at="2026-09-08T15:00:00+00:00")
            recovered = _recover_intent_from_run_log(
                "AAA", "2026-09-08",
                observation_id="obs-1", results_dir=str(tmp),
            )
        self.assertIsNone(recovered)

    def test_load_final_state_snapshot_filter_is_exact(self):
        from tradingagents.run_logger import load_final_state_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "AAA" / "TradingAgentsStrategy_logs" / "runs"
            self._write_run(runs, "r1", observation_id="obs-1",
                            symbol_intent=_buy_intent("AAA"),
                            started_at="2026-09-08T14:00:00+00:00")
            hit = load_final_state_snapshot(
                "AAA", "2026-09-08", eval_results_dir=str(tmp),
                metadata_match={"source": "long_run",
                                "long_run_observation_id": "obs-1"},
            )
            self.assertIsNotNone(hit)
            miss = load_final_state_snapshot(
                "AAA", "2026-09-08", eval_results_dir=str(tmp),
                metadata_match={"source": "long_run",
                                "long_run_observation_id": "other"},
            )
            self.assertIsNone(miss)


# ---------------------------------------------------------------------------
# F10 — cooperative stop/window checkpoints
# ---------------------------------------------------------------------------


class F10StopCheckpointTests(_GuardIsolated, unittest.TestCase):
    def setUp(self):
        import tradingagents.long_run as lr

        self.lr = lr
        super().setUp()

    def tearDown(self):
        self.lr._stop_requested = False
        super().tearDown()

    def _cfg(self):
        lr = self.lr
        cfg = lr.default_long_run_config()
        cfg.update({
            "duration_calendar_days": 30,
            "run_time_et": "11:00",
            "base_trade_notional_usd": 1000.0,
            "analysts": ["market"],
            "research_depth": 3,
            "output_language": "English",
            "analysis_provider": "openai",
            "analysis_model": "gpt-fake-analysis",
            "decision_provider": "openai",
            "decision_model": "gpt-fake-decision",
            "screening_provider": "openai",
            "screening_model": "gpt-fake-screening",
        })
        return cfg

    def _plan(self, symbols):
        from tradingagents.long_run import new_round_journal

        return SimpleNamespace(
            stopped=False,
            deep_analysis_set=list(symbols),
            top20=[{"symbol": s, "rank": i + 1,
                    "screening_score": 90.0 - i, "short_reason": "x"}
                   for i, s in enumerate(symbols)],
            selection_date="2026-09-08", as_of="2026-09-08", cached=False,
            overlap_holdings=[], extra_holdings=[], blocked_holdings=[],
            screening_description="Screening=fake",
        )

    def test_signal_stop_during_analysis_blocks_execution(self):
        lr = self.lr
        journal = lr.new_round_journal("2026-09-08", ["AAA", "BBB"])
        journal["status"] = "RUNNING"
        lr.save_round_journal("run-stop", journal)

        class StopDuringFirstGraph:
            calls = []

            def propagate(self, symbol, trade_date):
                self.calls.append(symbol)
                lr._stop_requested = True  # stop becomes observable now
                return ({"final_trade_intent": _buy_intent(symbol)},
                        "BUY")

        graph = StopDuringFirstGraph()

        class Service:
            recover_calls = 0
            execute_calls = []

            def startup_recover(self):
                self.recover_calls += 1
                return {"success": True, "account_execution_state": "CLEAN",
                        "reconciliation_reasons": []}

            def enforce_exit_deadlines(self):
                return {"success": True, "deadline_exits": [],
                        "broker_calls": 0}

            def execute(self, **kwargs):
                self.execute_calls.append(kwargs)
                return {"success": True, "broker_calls": 1}

        service = Service()
        deps = lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: self._plan(("AAA", "BBB")),
            graph_factory=lambda config: graph,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _state_broker(),
        )
        out = lr.run_daily_round(
            run_id="run-stop", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()),
            deps=deps,
        )
        # No broker execution at all; the second symbol was never analyzed.
        self.assertEqual(service.execute_calls, [])
        self.assertEqual(graph.calls, ["AAA"])
        # The remaining symbols were NOT marked FAILED.
        self.assertNotEqual(
            out["symbols"]["BBB"]["status"], "FAILED")

    def test_window_end_during_analysis_blocks_execution(self):
        lr = self.lr
        journal = lr.new_round_journal("2026-09-08", ["AAA"])
        journal["status"] = "RUNNING"
        lr.save_round_journal("run-window", journal)

        class Graph:
            calls = []

            def propagate(self, symbol, trade_date):
                self.calls.append(symbol)
                return ({"final_trade_intent": _buy_intent(symbol)}, "BUY")

        graph = Graph()
        service = F10StopCheckpointTests._ExecCountingService()

        # Fake clock: inside the window during setup, past ends_at right
        # after the analysis returns.
        base = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
        ends_at = datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc)
        times = [base, base, base,
                 ends_at + timedelta(seconds=1)]
        calls = {"n": 0}

        def now_fn():
            idx = min(calls["n"], len(times) - 1)
            calls["n"] += 1
            return times[idx]

        deps = lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: self._plan(("AAA",)),
            graph_factory=lambda config: graph,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _state_broker(),
            now_fn=now_fn,
        )
        out = lr.run_daily_round(
            run_id="run-window", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()),
            deps=deps, ends_at=ends_at,
        )
        self.assertEqual(service.execute_calls, [])
        self.assertEqual(
            out["stop_reason"], "WINDOW_ENDED_DURING_ROUND")

    def test_analyzed_resume_checks_stop_before_execution(self):
        lr = self.lr
        journal = lr.new_round_journal("2026-09-08", ["AAA"])
        journal["status"] = "RUNNING"
        journal["symbols"]["AAA"] = {
            "status": "ANALYZED", "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": _buy_intent("AAA"),
            "execution_result_summary": None,
        }
        lr.save_round_journal("run-resume", journal)
        lr._stop_requested = True  # stop requested before this resume

        class NoAnalysisGraph:
            def propagate(self, symbol, trade_date):
                raise AssertionError("must not re-analyze on ANALYZED resume")

        service = F10StopCheckpointTests._ExecCountingService()
        deps = lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: self._plan(("AAA",)),
            graph_factory=lambda config: NoAnalysisGraph(),
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _state_broker(),
        )
        out = lr.run_daily_round(
            run_id="run-resume", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()),
            deps=deps,
        )
        self.assertEqual(service.execute_calls, [])
        self.assertEqual(
            out["symbols"]["AAA"]["status"], "ANALYZED")  # untouched for resume

    class _ExecCountingService:
        def __init__(self):
            self.execute_calls = []

        def startup_recover(self):
            return {"success": True, "account_execution_state": "CLEAN",
                    "reconciliation_reasons": []}

        def enforce_exit_deadlines(self):
            return {"success": True, "deadline_exits": [], "broker_calls": 0}

        def execute(self, **kwargs):
            self.execute_calls.append(kwargs)
            return {"success": True, "broker_attempted": True,
                    "broker_calls": 1}

    def test_webui_state_stop_flag_gates_trade(self):
        from webui.utils.state import AppState

        state = AppState()
        self.assertFalse(state.is_stop_requested())
        state.stop_loop_mode()
        self.assertTrue(state.is_stop_requested())
        # Only an explicit Start clears it; reset_for_loop never does.
        state.start_loop(["AAA"], {})
        self.assertFalse(state.is_stop_requested())
        state.stop_market_hour_mode()
        self.assertTrue(state.is_stop_requested())
        state.reset_for_loop()
        self.assertTrue(state.is_stop_requested())

    def test_request_stop_is_universal_across_modes(self):
        from webui.utils.state import AppState

        state = AppState()
        state.start_loop(["AAA"], {})
        state.request_stop()
        self.assertTrue(state.stop_loop)
        self.assertTrue(state.stop_market_hour)
        self.assertTrue(state.is_stop_requested())


if __name__ == "__main__":
    unittest.main()
