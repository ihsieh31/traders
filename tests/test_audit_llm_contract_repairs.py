"""L01/L03/L04 offline contract regressions for the custom LLM adapter."""

import pytest
import tradingagents.agents  # noqa: F401
from langchain_core.messages import AIMessage
from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel


class _ChatResultFixture:
    def __init__(self, message):
        self.generations = [type("G", (), {"message": message})()]


def test_l03_chat_branch_receives_request_timeout():
    """L03: get_chat_model pops `timeout` and only forwarded it to the
    Responses branch; the Chat Completions branch built its model without
    any timeout, so llm_request_timeout_seconds was silently dropped for
    non-Responses models."""
    from tradingagents.agents.utils.gpt5_llm import get_chat_model

    model = get_chat_model("gpt-4.1", api_key="k", timeout=42.0)
    assert getattr(model, "request_timeout", None) == 42.0


def test_l03_responses_branch_keeps_timeout():
    from tradingagents.agents.utils.gpt5_llm import get_chat_model

    model = get_chat_model("gpt-5-mini", api_key="k", timeout=7.5)
    assert isinstance(model, GPT5ChatModel)
    assert model.timeout == 7.5


def test_l04_chat_prompt_value_keeps_roles_and_tool_messages():
    """L04: invoke(ChatPromptValue) previously str()-collapsed a
    system+human prompt into one HumanMessage and turned ToolMessages into
    "[]". Message roles must survive the invoke boundary so bound-tool
    agents keep their tool call/output protocol."""
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.messages import ToolMessage

    captured = []

    class _RecordingGPT5(GPT5ChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured.append(list(messages))
            return _ChatResultFixture(AIMessage(content="ok"))

    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are {name}."),
        ("placeholder", "{history}"),
        ("human", "{question}"),
    ])
    tool_msg = ToolMessage(content="tool says hi", tool_call_id="call-1")
    value = prompt.invoke({"name": "T", "history": [tool_msg], "question": "why?"})
    model = _RecordingGPT5(model="gpt-5-mini", api_key="k")
    model.invoke(value)
    roles = [type(m).__name__ for m in captured[0]]
    assert roles == ["SystemMessage", "ToolMessage", "HumanMessage"]
    assert captured[0][0].content == "You are T."


class _TraderProposalFixture:
    content = "FINAL TRANSACTION PROPOSAL: **BUY**"
    action = "BUY"
    confidence = "medium"
    risk_rationale = "r"
    required_controls = "c"
    entry_timing = "market"
    position_sizing = "fixed"


class _RiskDecisionFixture:
    content = "FINAL TRANSACTION PROPOSAL: **HOLD**"
    action = "HOLD"
    confidence = "medium"
    risk_rationale = "r"
    required_controls = "stop"


class _FakeManagerLLM:
    def with_structured_output(self, schema, **kw):
        from types import SimpleNamespace
        name = getattr(schema, "__name__", "")
        if name == "RiskDecision":
            # Structured path returns a pydantic-valid dict: the strict risk
            # boundary validates it with the real RiskDecision schema.
            payload = {
                "action": "HOLD", "confidence": "medium",
                "risk_rationale": "r", "required_controls": "c",
            }
        else:
            payload = "FINAL TRANSACTION PROPOSAL: **BUY**"
        return SimpleNamespace(invoke=lambda p, _p=payload: _p)

    def invoke(self, p):
        return AIMessage(content="FINAL TRANSACTION PROPOSAL: **BUY**")


def test_l01_risk_manager_verifies_same_broker_account_as_trader(monkeypatch):
    """L01: the Trader observes account paper-1 and persists it into state;
    the Risk Manager must pass that account as expected_account_id so an
    account switch between the two prompts fails closed. On HEAD
    broker_account_id has no state channel and the expected-account check
    silently degrades to None (any account accepted)."""
    from types import SimpleNamespace
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from unittest.mock import patch

    from tradingagents.agents.managers.risk_manager import create_risk_manager
    from tradingagents.agents.trader import trader as trader_module
    from tradingagents.execution.context import build_position_context
    from tradingagents.execution.authority import BrokerPosition, BrokerSnapshot

    def _snapshot(account_id):
        from datetime import datetime, timezone as tz
        return BrokerSnapshot(
            observed_at=datetime.now(tz.utc),
            version="v",
            account_id=account_id,
            equity=100000.0,
            last_equity=100000.0,
            cash=80000.0,
            buying_power=160000.0,
            positions=(BrokerPosition("NVDA", 100.0, 20000.0),),
            orders=(),
            fills=(),
            gross_exposure=20000.0,
        )

    trader_ctx = build_position_context("NVDA", _snapshot("paper-1"))

    def _state():
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        return {
            "company_of_interest": "NVDA",
            "trade_date": today,
            "investment_plan": "FINAL TRANSACTION PROPOSAL: **BUY** — " + "x" * 200,
            "investment_debate_state": {"count": 2, "history": "h",
                                        "current_response": "", "judge_decision": "plan"},
            "risk_debate_state": {"count": 0, "history": "", "risky_history": "",
                                  "safe_history": "", "neutral_history": "",
                                  "judge_decision": "", "current_risky_response": "",
                                  "current_safe_response": "", "current_neutral_response": ""},
            "market_report": "m", "sentiment_report": "s", "news_report": "n",
            "fundamentals_report": "f", "macro_report": "mac",
            "messages": [],
        }

    memory = SimpleNamespace(get_memories=lambda *a, **k: [])
    expected_calls = []

    def fake_risk_capture(symbol, broker=None, *, broker_factory=None,
                          expected_account_id=None):
        expected_calls.append(expected_account_id)
        if expected_account_id != "paper-1":
            from tradingagents.execution.authority import BrokerAuthorityError
            raise BrokerAuthorityError(
                f"broker account mismatch: expected {expected_account_id!r}"
            )
        return build_position_context(symbol, _snapshot("paper-1"))

    with patch(
        "tradingagents.agents.trader.trader.capture_position_context",
        return_value=trader_ctx,
    ), patch(
        "tradingagents.agents.trader.trader.capture_agent_prompt",
        side_effect=lambda *a, **k: None,
    ), patch(
        "tradingagents.agents.managers.risk_manager.capture_position_context",
        side_effect=fake_risk_capture,
    ), patch(
        "tradingagents.agents.managers.risk_manager.capture_agent_prompt",
        side_effect=lambda *a, **k: None,
    ), patch(
        "tradingagents.agents.trader.trader.TradingMemoryLog"
    ), patch(
        "tradingagents.agents.managers.risk_manager.TradingMemoryLog"
    ) as risk_log:
        risk_log.return_value.get_past_context.return_value = ""
        trader_node = trader_module.create_trader(_FakeManagerLLM(), memory, config={})
        trader_out = trader_node(_state())
        assert trader_out.get("broker_account_id") == "paper-1", (
            "the Trader must persist the account it observed into state"
        )
        risk_node = create_risk_manager(_FakeManagerLLM(), memory, config={})
        risk_state = dict(_state())
        risk_state.update(trader_out)
        risk_out = risk_node(risk_state)

    assert expected_calls == ["paper-1"], expected_calls
    assert risk_out["broker_account_id"] == "paper-1"
