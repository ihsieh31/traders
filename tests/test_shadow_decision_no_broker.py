"""Shadow A/B runs must not require broker account credentials."""

from unittest.mock import Mock, patch

from langchain_openai import ChatOpenAI

from tradingagents.agents.managers.risk_manager import create_risk_manager
from tradingagents.agents.trader.trader import create_trader
from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI


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
