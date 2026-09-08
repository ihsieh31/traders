"""DMC-1 minimal-consistency behavior tests (offline, fake LLMs only).

Covers:
- B01-B05: the Trader keeps the original messages on a contextual completion
  and performs at most one such completion; empty first responses fail.
- C02: the standard action-to-intent mapping semantics are unchanged.
- D01: risk debators reject empty/invalid LLM content before touching the
  debate state.

Every test uses fake LLMs, fake memory and a temporary isolated environment;
no provider, broker or data client is created and a socket blocker guards
against accidental network access.
"""

import copy
import datetime
import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tradingagents.agents.utils.structured as structured_mod
import tradingagents.agents.trader.trader as trader_module
from tradingagents.agents.schemas import (
    ExecutableAction,
    PositionTransition,
    RiskDecision,
    TraderProposal,
    build_trade_intent_from_risk_decision,
)


def _past_date(days_ago=5):
    return (
        datetime.datetime.now() - datetime.timedelta(days=days_ago)
    ).strftime("%Y-%m-%d")


class _NoNetwork:
    """Belt-and-braces guard: any socket creation fails the test loudly."""

    def __enter__(self):
        self._orig = socket.socket

        def blocked(*args, **kwargs):
            raise AssertionError("network access attempted during offline test")

        socket.socket = blocked
        return self

    def __exit__(self, *exc):
        socket.socket = self._orig
        return False


class _FakePlainLLM:
    """No structured binding; scripted invoke outcomes; records prompts."""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        raise RuntimeError("structured output unavailable in fake")

    def invoke(self, prompt):
        self.calls.append(copy.deepcopy(prompt))
        outcome = self.contents.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeScriptedLLM:
    """Structured binding that shares one scripted invoke outcome queue."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        return self

    def invoke(self, prompt):
        self.calls.append(copy.deepcopy(prompt))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _resp(content):
    return SimpleNamespace(content=content)


def _trader_state(**extra):
    state = {
        "company_of_interest": "NVDA",
        "trade_date": _past_date(5),
        "investment_plan": (
            "Buy on confirmation above $950 with stop $920 and target $1,020. "
            "Plan body padded so the trader uses the standard plan prompt. "
        )
        + "x" * 200,
        "investment_debate_state": {
            "count": 2,
            "history": "debate history",
            "current_response": "manager plan",
            "judge_decision": "plan",
        },
        "current_position": "NEUTRAL",
        "position_stats": "ACCOUNT_SENTINEL position stats (as of past date)",
        "account_status": "ACCOUNT_SENTINEL account status (as of past date)",
        "fundamentals_report": (
            "## Overview\nEVIDENCE_SENTINEL revenue growth accelerated 45% in the "
            "latest quarter with expanding gross margin."
        ),
        "market_report": (
            "## Setup\nPrice broke above $950 resistance on rising volume with "
            "bullish momentum."
        ),
        "messages": [],
    }
    state.update(extra)
    return state


class PromptOverrideCleanupMixin(unittest.TestCase):
    """Use the built-in templates: drop any external prompt override."""

    def setUp(self):
        self._old_prompt_dir = os.environ.pop("TRADINGAGENTS_PROMPT_DIR", None)

    def tearDown(self):
        if self._old_prompt_dir is not None:
            os.environ["TRADINGAGENTS_PROMPT_DIR"] = self._old_prompt_dir


class TraderFallbackContextTests(PromptOverrideCleanupMixin):
    """B01-B04: real Trader node with fake LLMs and counting logical calls."""

    def _run_trader(self, fake_llm, state):
        memory = SimpleNamespace(get_memories=lambda *a, **k: [])
        logical_calls = []
        real_helper = structured_mod.invoke_structured_or_freetext

        def counting(structured_llm, plain_llm, prompt, render, agent_name):
            logical_calls.append(copy.deepcopy(prompt) if isinstance(prompt, list) else prompt)
            return real_helper(structured_llm, plain_llm, prompt, render, agent_name)

        with patch.object(
            trader_module, "invoke_structured_or_freetext", counting
        ), patch(
            "tradingagents.agents.trader.trader.capture_position_context",
            side_effect=AssertionError("broker must not be called in offline test"),
        ), patch(
            "tradingagents.agents.trader.trader.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ), _NoNetwork():
            node = trader_module.create_trader(fake_llm, memory, config={})
            out = node(state)
        return out, logical_calls

    def test_b01_complete_answer_keeps_original_text_with_single_call(self):
        fake = _FakePlainLLM(
            [_resp("Wait.\nFINAL TRANSACTION PROPOSAL: **HOLD**")]
        )
        out, logical_calls = self._run_trader(fake, _trader_state())

        self.assertEqual(len(logical_calls), 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("Wait.", out["trader_investment_plan"])
        self.assertIn(
            "FINAL TRANSACTION PROPOSAL: **HOLD**", out["trader_investment_plan"]
        )
        self.assertEqual(out["recommended_action"], "HOLD")
        self.assertEqual(out["messages"][0].content, out["trader_investment_plan"])

    def test_b02_missing_action_gets_one_context_preserving_completion(self):
        fake = _FakePlainLLM(
            [
                _resp("Evidence incomplete."),
                _resp(
                    "Hold off until evidence arrives.\n"
                    "FINAL TRANSACTION PROPOSAL: **HOLD**"
                ),
            ]
        )
        out, logical_calls = self._run_trader(fake, _trader_state())

        self.assertEqual(len(logical_calls), 2)
        self.assertEqual(len(fake.calls), 2)

        # The completion prompt starts with the exact original messages.
        first, second = fake.calls[0], fake.calls[1]
        self.assertEqual(second[: len(first)], first)

        system_message = second[0]["content"]
        self.assertIn("EVIDENCE_SENTINEL", system_message)
        self.assertIn("ACCOUNT_SENTINEL", system_message)
        self.assertIn("SWING TRADING INVESTMENT MODE", system_message)

        assistant_turn = second[len(first)]
        self.assertEqual(assistant_turn["role"], "assistant")
        self.assertEqual(assistant_turn["content"], "Evidence incomplete.")

        user_turn = second[len(first) + 1]
        self.assertEqual(user_turn["role"], "user")
        self.assertIn("Complete the final action format", user_turn["content"])
        self.assertIn("Evidence incomplete.", user_turn["content"])

        # Original analysis is preserved and the final line is present.
        self.assertIn("Evidence incomplete.", out["trader_investment_plan"])
        self.assertIn(
            "FINAL TRANSACTION PROPOSAL: **HOLD**", out["trader_investment_plan"]
        )
        self.assertEqual(out["recommended_action"], "HOLD")

    def test_b03_invalid_first_response_raises_without_completion(self):
        cases = [
            ("none", _resp(None)),
            ("blank", _resp("   ")),
            ("non-string", _resp(123)),
        ]
        for label, first in cases:
            with self.subTest(case=label):
                fake = _FakePlainLLM([first, _resp("never used")])
                with self.assertRaisesRegex(
                    ValueError, "Trader returned empty or invalid analysis"
                ):
                    self._run_trader(fake, _trader_state())
                self.assertEqual(len(fake.calls), 1)

    def test_b03_completion_still_missing_action_raises_without_third_call(self):
        cases = [
            ("empty completion", _resp("")),
            ("no action again", _resp("Still cannot decide.")),
        ]
        for label, second in cases:
            with self.subTest(case=label):
                fake = _FakePlainLLM([_resp("Evidence incomplete."), second])
                with self.assertRaisesRegex(
                    ValueError,
                    "Trader final action unavailable after one contextual completion",
                ):
                    self._run_trader(fake, _trader_state())
                self.assertEqual(len(fake.calls), 2)

    def test_b04_structured_success_is_one_logical_call(self):
        proposal = TraderProposal(
            action=ExecutableAction.HOLD,
            confidence="medium",
            reasoning="Evidence is mixed; stay flat.",
        )
        fake = _FakeScriptedLLM([proposal])
        out, logical_calls = self._run_trader(fake, _trader_state())

        self.assertEqual(len(logical_calls), 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn(
            "FINAL TRANSACTION PROPOSAL: **HOLD**", out["trader_investment_plan"]
        )
        self.assertEqual(out["recommended_action"], "HOLD")

    def test_b04_structured_failure_falls_back_to_free_text_on_same_prompt(self):
        fake = _FakeScriptedLLM(
            [
                ValueError("schema violation"),
                _resp("Hold.\nFINAL TRANSACTION PROPOSAL: **HOLD**"),
            ]
        )
        out, logical_calls = self._run_trader(fake, _trader_state())

        self.assertEqual(len(logical_calls), 1)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0], fake.calls[1])
        self.assertEqual(out["recommended_action"], "HOLD")

    def test_b04_provider_failure_propagates_without_completion(self):
        from tradingagents.llm_clients.retry import ProviderFailure

        failure = ProviderFailure(
            role="analysis", provider="openai", model="m", attempts=2,
            category="transient", detail="timeout",
        )
        fake = _FakeScriptedLLM([failure])
        with self.assertRaises(ProviderFailure) as ctx:
            self._run_trader(fake, _trader_state())
        self.assertIs(ctx.exception, failure)
        self.assertEqual(len(fake.calls), 1)

    def test_b05_real_trader_failure_stops_all_downstream_nodes(self):
        from langgraph.graph import END, START, StateGraph

        from tradingagents.agents.managers.risk_manager import create_risk_manager
        from tradingagents.agents.risk_mgmt.aggresive_debator import (
            create_risky_debator,
        )
        from tradingagents.agents.risk_mgmt.conservative_debator import (
            create_safe_debator,
        )
        from tradingagents.agents.risk_mgmt.neutral_debator import (
            create_neutral_debator,
        )
        from tradingagents.agents.utils.agent_states import AgentState
        from tradingagents.graph.conditional_logic import ConditionalLogic

        counts = {"Risky": 0, "Safe": 0, "Neutral": 0, "Risk Judge": 0, "exec": 0}

        class _MustNotRunLLM:
            def with_structured_output(self, schema, **kwargs):
                raise RuntimeError("no structured binding in fake")

            def invoke(self, prompt):
                raise AssertionError("downstream node must not run after Trader failure")

        def counted(name, node):
            def wrapped(state):
                counts[name] += 1
                return node(state)

            return wrapped

        def execution_spy(state):
            counts["exec"] += 1
            return {}

        memory = SimpleNamespace(get_memories=lambda *a, **k: [])
        trading_llm = _FakePlainLLM([_resp("")])
        with patch(
            "tradingagents.agents.trader.trader.capture_position_context",
            side_effect=AssertionError("broker must not be called in offline test"),
        ), patch(
            "tradingagents.agents.trader.trader.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ), patch(
            "tradingagents.agents.risk_mgmt.aggresive_debator.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ), patch(
            "tradingagents.agents.risk_mgmt.conservative_debator.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ), patch(
            "tradingagents.agents.risk_mgmt.neutral_debator.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ), _NoNetwork():
            # Real production Trader node (raises on empty analysis).
            trader_node = trader_module.create_trader(trading_llm, memory, config={})

            cond = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
            workflow = StateGraph(AgentState)
            workflow.add_node("Trader", trader_node)
            workflow.add_node(
                "Risky Analyst",
                counted("Risky", create_risky_debator(_MustNotRunLLM(), config={})),
            )
            workflow.add_node(
                "Safe Analyst",
                counted("Safe", create_safe_debator(_MustNotRunLLM(), config={})),
            )
            workflow.add_node(
                "Neutral Analyst",
                counted("Neutral", create_neutral_debator(_MustNotRunLLM(), config={})),
            )
            workflow.add_node(
                "Risk Judge",
                counted(
                    "Risk Judge",
                    create_risk_manager(_MustNotRunLLM(), memory, config={}),
                ),
            )
            workflow.add_node("Execution Spy", execution_spy)
            workflow.add_edge(START, "Trader")
            workflow.add_conditional_edges(
                "Trader",
                cond.should_continue_risk_analysis,
                {
                    "Risky Analyst": "Risky Analyst",
                    "Safe Analyst": "Safe Analyst",
                    "Neutral Analyst": "Neutral Analyst",
                    "Risk Judge": "Risk Judge",
                },
            )
            workflow.add_conditional_edges(
                "Risky Analyst",
                cond.should_continue_risk_analysis,
                {"Safe Analyst": "Safe Analyst", "Risk Judge": "Risk Judge"},
            )
            workflow.add_conditional_edges(
                "Safe Analyst",
                cond.should_continue_risk_analysis,
                {"Neutral Analyst": "Neutral Analyst", "Risk Judge": "Risk Judge"},
            )
            workflow.add_conditional_edges(
                "Neutral Analyst",
                cond.should_continue_risk_analysis,
                {"Risky Analyst": "Risky Analyst", "Risk Judge": "Risk Judge"},
            )
            workflow.add_edge("Risk Judge", "Execution Spy")
            workflow.add_edge("Execution Spy", END)
            graph = workflow.compile()

            state = _trader_state()
            state["risk_debate_state"] = {"count": 0, "history": ""}
            with self.assertRaises(ValueError):
                graph.invoke(state, {"recursion_limit": 25})

        for name, count in counts.items():
            self.assertEqual(count, 0, f"{name} must not run after Trader failure")


class ActionMappingSemanticsTests(unittest.TestCase):
    """C02: the existing intent builder semantics are unchanged."""

    def _intent(self, action, current_position, trading_mode="trading"):
        decision = RiskDecision(
            action=action,
            confidence="medium",
            risk_rationale="rationale",
            required_controls="stop below entry",
        )
        return build_trade_intent_from_risk_decision(
            symbol="NVDA",
            trading_mode=trading_mode,
            current_position=current_position,
            decision=decision,
            allow_shorts=True,
        )

    def test_maintain_paths_produce_no_order(self):
        cases = [
            (ExecutableAction.LONG, "LONG", PositionTransition.HOLD_LONG),
            (ExecutableAction.SHORT, "SHORT", PositionTransition.HOLD_SHORT),
            (ExecutableAction.NEUTRAL, "NEUTRAL", PositionTransition.STAY_NEUTRAL),
        ]
        for action, current, expected in cases:
            with self.subTest(action=action, current=current):
                intent = self._intent(action, current)
                self.assertEqual(intent.position_transition, expected)
                self.assertEqual(intent.order_intent.order_type, "none")
                self.assertEqual(intent.order_intent.sizing_basis, "no_order")

    def test_long_to_neutral_requests_full_close(self):
        intent = self._intent(ExecutableAction.NEUTRAL, "LONG")
        self.assertEqual(intent.position_transition, PositionTransition.CLOSE_LONG)
        self.assertEqual(intent.order_intent.order_type, "close_position")
        self.assertEqual(intent.order_intent.side, "sell")
        self.assertEqual(intent.order_intent.sizing_basis, "current_position")

    def test_investment_mode_hold_maintains_position(self):
        decision = RiskDecision(
            action=ExecutableAction.HOLD,
            confidence="medium",
            risk_rationale="rationale",
            required_controls="stop below entry",
        )
        intent = build_trade_intent_from_risk_decision(
            symbol="NVDA",
            trading_mode="investment",
            current_position="LONG",
            decision=decision,
            allow_shorts=False,
        )
        self.assertEqual(intent.position_transition, PositionTransition.HOLD_LONG)
        self.assertEqual(intent.order_intent.order_type, "none")


class RiskDebatorContentValidationTests(PromptOverrideCleanupMixin):
    """D01: empty/invalid LLM content fails before any debate-state mutation."""

    def _factories(self):
        from tradingagents.agents.risk_mgmt.aggresive_debator import (
            create_risky_debator,
        )
        from tradingagents.agents.risk_mgmt.conservative_debator import (
            create_safe_debator,
        )
        from tradingagents.agents.risk_mgmt.neutral_debator import (
            create_neutral_debator,
        )

        return (
            ("Risky", create_risky_debator),
            ("Safe", create_safe_debator),
            ("Neutral", create_neutral_debator),
        )

    def _state(self):
        return {
            "company_of_interest": "NVDA",
            "trader_investment_plan": "plan\nFINAL TRANSACTION PROPOSAL: **HOLD**",
            "current_position": "NEUTRAL",
            "risk_debate_state": {
                "history": "",
                "risky_history": "",
                "safe_history": "",
                "neutral_history": "",
                "risky_messages": [],
                "safe_messages": [],
                "neutral_messages": [],
                "current_risky_response": "",
                "current_safe_response": "",
                "current_neutral_response": "",
                "count": 0,
            },
        }

    def _patch_capture(self, factory_module):
        return patch(
            f"tradingagents.agents.risk_mgmt.{factory_module}.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        )

    def test_empty_content_raises_without_state_mutation(self):
        cases = [
            ("none", _resp(None)),
            ("empty", _resp("")),
            ("blank", _resp("   ")),
            ("non-string", _resp(123)),
        ]
        module_names = {"Risky": "aggresive_debator", "Safe": "conservative_debator", "Neutral": "neutral_debator"}
        for role, factory in self._factories():
            for label, response in cases:
                with self.subTest(role=role, case=label), self._patch_capture(
                    module_names[role]
                ), _NoNetwork():
                    snapshot = copy.deepcopy(self._state()["risk_debate_state"])
                    state = self._state()

                    class _BrokenLLM:
                        def invoke(self, prompt):
                            return response

                    node = factory(_BrokenLLM(), config={})
                    with self.assertRaisesRegex(
                        ValueError,
                        f"{role} risk analyst returned empty or invalid content",
                    ):
                        node(state)
                    # count/history/messages must be untouched; report_context
                    # caching is pre-existing production behavior.
                    self.assertEqual(state["risk_debate_state"], snapshot)

    def test_short_nonempty_statement_is_valid_and_counted(self):
        module_names = {"Risky": "aggresive_debator", "Safe": "conservative_debator", "Neutral": "neutral_debator"}
        for role, factory in self._factories():
            with self.subTest(role=role), self._patch_capture(
                module_names[role]
            ), _NoNetwork():
                state = self._state()

                class _ShortLLM:
                    def invoke(self, prompt):
                        return _resp("Insufficient evidence.")

                node = factory(_ShortLLM(), config={})
                out = node(state)
                debate = out["risk_debate_state"]
                self.assertEqual(debate["count"], 1)
                self.assertEqual(debate["latest_speaker"], role)
                self.assertEqual(
                    debate[f"{role.lower()}_messages"],
                    [f"{role} Analyst: Insufficient evidence."],
                )


if __name__ == "__main__":
    unittest.main()
