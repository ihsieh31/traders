"""Shadow A/B runs must not require broker account credentials."""

from unittest.mock import Mock, patch

from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI

from tradingagents.agents.schemas import ResearchPlan
from tradingagents.agents.managers.risk_manager import create_risk_manager
from tradingagents.agents.trader.trader import create_trader
from tradingagents.llm_clients.openai_client import (
    NormalizedChatOpenAI,
    _append_json_schema_instructions,
)


def test_trader_shadow_mode_skips_broker_snapshot():
    state = {
        "company_of_interest": "NVDA",
        "trade_date": "2026-09-21",
        "investment_plan": "A sufficiently detailed investment plan " * 20,
        "investment_debate_state": {"history": ""},
        "report_context": {},
    }

    with patch("tradingagents.agents.trader.trader.capture_position_context") as capture:
        node = create_trader(Mock(), Mock(), {"auto_trade": False})
        # The LLM path is not reached: the assertion covers the broker gate.
        try:
            node(state)
        except Exception:
            pass
        capture.assert_not_called()


def test_risk_manager_shadow_mode_skips_broker_snapshot():
    state = {
        "company_of_interest": "NVDA",
        "trade_date": "2026-09-21",
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
            "latest_speaker": "Judge",
            "count": 0,
        },
        "trader_investment_plan": "plan",
        "current_position": "NEUTRAL",
        "investment_plan": "plan",
        "report_context": {},
    }

    with patch("tradingagents.agents.managers.risk_manager.capture_position_context") as capture:
        node = create_risk_manager(Mock(), Mock(), {"auto_trade": False})
        try:
            node(state)
        except Exception:
            pass
        capture.assert_not_called()


def test_openai_compatible_json_mode_can_override_function_calling():
    with patch.object(ChatOpenAI, "with_structured_output", return_value="sentinel") as bind:
        llm = NormalizedChatOpenAI(
            model="gemini-3.5-flash-lite",
            openai_api_key="test-key",
            structured_output_method="json_mode",
        )
        assert llm.with_structured_output(dict) == "sentinel"
        assert bind.call_args.kwargs["method"] == "json_mode"


def test_local_openai_endpoint_defaults_to_json_mode():
    with patch.object(ChatOpenAI, "with_structured_output", return_value="sentinel") as bind:
        llm = NormalizedChatOpenAI(
            model="gemini-3.5-flash-lite",
            openai_api_key="test-key",
            openai_api_base="http://localhost:3000/v1",
        )
        assert llm.with_structured_output(dict) == "sentinel"
        assert bind.call_args.kwargs["method"] == "json_mode"


def test_explicit_function_calling_still_overrides_local_default():
    with patch.object(ChatOpenAI, "with_structured_output", return_value="sentinel") as bind:
        llm = NormalizedChatOpenAI(
            model="gemini-3.5-flash-lite",
            openai_api_key="test-key",
            openai_api_base="http://localhost:3000/v1",
            structured_output_method="function_calling",
        )
        assert llm.with_structured_output(dict) == "sentinel"
        assert bind.call_args.kwargs["method"] == "function_calling"


def test_json_mode_injects_pydantic_schema_instructions():
    with patch.object(
        ChatOpenAI,
        "with_structured_output",
        return_value=RunnableLambda(lambda input_: input_),
    ):
        llm = NormalizedChatOpenAI(
            model="gemini-3.5-flash-lite",
            openai_api_key="test-key",
            openai_api_base="http://localhost:3000/v1",
        )
        structured = llm.with_structured_output(ResearchPlan)
        messages = structured.invoke([{"role": "user", "content": "analyze NVDA"}])

    assert messages[-1]["role"] == "system"
    assert "overrides any earlier output-format instruction" in messages[-1]["content"]
    assert "append a final transaction proposal" in messages[-1]["content"]
    assert '"recommendation"' in messages[-1]["content"]
    assert '"strategic_actions"' in messages[-1]["content"]


def test_json_schema_instruction_helper_preserves_string_prompts():
    result = _append_json_schema_instructions("analyze NVDA", '{"type":"object"}')
    assert result.startswith("analyze NVDA")
    assert "STRUCTURED JSON OUTPUT CONTRACT" in result
