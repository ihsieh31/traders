"""Schema-validation failures must not be wrapped as provider-access failures.

Regression for the 2026-09-14 first-session stop (run
lr-20260914T115852Z-dde155): agnes-3.0-flash answered the Research Manager's
structured call with ``{'recommendation': 'NEUTRAL'}`` — a successful
provider response missing the other required ``ResearchPlan`` fields. The
pydantic ValidationError fell into the retry owner's catch-all, was
misclassified "permanent" (unknown shape), and was raised as
ProviderFailure, which halts a long-run round by design. The retry module's
own contract says schema/bind/validation on a *successful* response are NOT
classified as provider failures; these tests pin that contract for the
transport-adjacent paths where validation happens INSIDE the retried call
(``with_structured_output`` runnables).
"""
import unittest
from types import SimpleNamespace

from langchain_core.exceptions import OutputParserException
from pydantic import ValidationError

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.structured import (
    invoke_risk_structured_strict,
    invoke_structured_object_or_freetext,
)
from tradingagents.llm_clients.retry import (
    _FailoverRetryController,
    ProviderFailure,
    RetryingLLM,
)

NO_SLEEP = lambda seconds: None


def _research_plan_validation_error():
    """The exact incident: NEUTRAL with the three required fields missing."""
    try:
        ResearchPlan.model_validate({"recommendation": "NEUTRAL"})
    except ValidationError as exc:
        return exc
    raise AssertionError("fixture must produce a ValidationError")


def _owner(max_retries=3):
    llm = RetryingLLM(
        SimpleNamespace(), role="analysis", provider="openai",
        model="agnes-3.0-flash", max_retries=max_retries,
    )
    llm._controller.sleep = NO_SLEEP
    return llm._controller


def _failover_controller(**kwargs):
    switches = []
    controller = _FailoverRetryController(
        role="analysis", primary_provider="openai", primary_model="agnes-3.0-flash",
        fallback_provider="openai", fallback_model="gemini-3.8-flash",
        max_retries=kwargs.get("max_retries", 3), on_switch=switches.append,
    )
    controller.sleep = NO_SLEEP
    return controller, switches


class RetryOwnerSchemaPassthroughTests(unittest.TestCase):
    def test_validation_error_propagates_raw_with_single_request(self):
        calls = []

        def call():
            calls.append(1)
            raise _research_plan_validation_error()

        with self.assertRaises(ValidationError):
            _owner().run(call)
        # Not wrapped, no retry burn, remaining budget untouched: the call
        # ran exactly once and the original type survived.
        self.assertEqual(len(calls), 1)

    def test_output_parser_exception_propagates_raw(self):
        calls = []

        def call():
            calls.append(1)
            raise OutputParserException("malformed structured payload")

        with self.assertRaises(OutputParserException):
            _owner().run(call)
        self.assertEqual(len(calls), 1)

    def test_real_transport_failures_still_raise_provider_failure(self):
        # Non-regression: the new passthrough must not weaken the transport
        # policy — 429/5xx/connectivity still consume the budget and wrap.
        calls = []

        def call():
            calls.append(1)
            raise ConnectionError("connection reset")

        with self.assertRaises(ProviderFailure) as ctx:
            _owner(max_retries=1).run(call)
        self.assertEqual(len(calls), 2)
        self.assertEqual(ctx.exception.category, "transient")

    def test_permanent_status_marker_still_wraps_immediately(self):
        calls = []

        def call():
            calls.append(1)
            raise RuntimeError("401 unauthorized")

        with self.assertRaises(ProviderFailure) as ctx:
            _owner().run(call)
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.category, "permanent")


class FailoverSchemaPassthroughTests(unittest.TestCase):
    def test_validation_error_never_switches_route_or_retries(self):
        calls = {"primary": 0, "fallback": 0}
        controller, switches = _failover_controller()

        def primary():
            calls["primary"] += 1
            raise _research_plan_validation_error()

        def fallback():
            calls["fallback"] += 1
            return "should never run"

        with self.assertRaises(ValidationError):
            controller.run(primary, fallback)
        self.assertEqual(calls, {"primary": 1, "fallback": 0})
        self.assertEqual(switches, [])

    def test_transient_primary_failure_still_fails_over(self):
        # Non-regression of the shared-budget failover around the new clause.
        calls = {"primary": 0, "fallback": 0}
        controller, switches = _failover_controller(max_retries=1)

        def primary():
            calls["primary"] += 1
            raise ConnectionError("connection reset")

        def fallback():
            calls["fallback"] += 1
            return "ok"

        self.assertEqual(controller.run(primary, fallback), "ok")
        self.assertEqual(calls, {"primary": 1, "fallback": 1})
        self.assertEqual(len(switches), 1)


class NodeBoundaryRegressionTests(unittest.TestCase):
    """The handlers that the retry owner must let do their documented job."""

    def test_research_manager_shape_falls_back_to_free_text(self):
        def _raising_invoke(prompt):
            raise _research_plan_validation_error()

        structured_llm = SimpleNamespace(invoke=_raising_invoke)
        plain_llm = SimpleNamespace(invoke=lambda prompt: SimpleNamespace(
            content="FREE-TEXT PLAN: recommendation NEUTRAL after debate"))
        text, obj = invoke_structured_object_or_freetext(
            structured_llm, plain_llm, "prompt", render_research_plan,
            "Research Manager",
        )
        self.assertIsNone(obj)
        self.assertIn("FREE-TEXT PLAN", text)

    def test_risk_manager_strict_still_emits_no_trade(self):
        def _raising_invoke(prompt):
            raise _research_plan_validation_error()

        structured_llm = SimpleNamespace(invoke=_raising_invoke)
        content, decision, reason = invoke_risk_structured_strict(
            structured_llm, "prompt", render_research_plan, "Risk Manager",
            schema=ResearchPlan,
        )
        self.assertIsNone(content)
        self.assertIsNone(decision)
        self.assertIsNotNone(reason)
        self.assertIn("structured_invoke_failed", reason)


if __name__ == "__main__":
    unittest.main()
