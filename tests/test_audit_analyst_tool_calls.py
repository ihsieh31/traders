"""L02 regression: analysts must execute standard LangChain tool_calls.

Anthropic/Google provider adapters return AIMessage.tool_calls (the
standard LangChain field) and leave additional_kwargs without
`tool_calls`; the analyst loops only inspected the raw
additional_kwargs, so tool calls were silently ignored (zero tool
executions, an immediate empty "report" with no evidence).
"""

from types import SimpleNamespace as NS

import tradingagents.agents  # noqa: F401
from langchain_core.messages import AIMessage

EXECUTED = []


class _ToolFn:
    def __init__(self, name):
        self.name = name

    def invoke(self, args):
        EXECUTED.append((self.name, dict(args or {})))
        return f"{self.name} evidence"


class _Toolkit:
    """Offline toolkit: no Alpaca credentials, one offline indicator tool."""

    config = {"online_tools": False,
              "max_tool_iterations_per_agent": 8,
              "max_same_tool_call_repeats": 1}

    def __init__(self):
        self.get_stockstats_indicators_report = _ToolFn(
            "get_stockstats_indicators_report")

    def has_alpaca_credentials(self):
        return False


class _Chain:
    """Scripted chain: first a standard-field tool call, then the report."""

    def __init__(self, responses):
        self._responses = responses
        self._n = 0

    def __or__(self, other):
        return self

    def partial(self, **kw):
        return self

    def format_messages(self, messages):
        raise RuntimeError("fake prompt has no template")

    def invoke(self, msgs):
        response = self._responses[min(self._n, len(self._responses) - 1)]
        self._n += 1
        return response


class _FakeLLM:
    def bind_tools(self, tools):
        return self


def test_l02_standard_tool_calls_drive_market_analyst_loop(monkeypatch):
    from tradingagents.agents.analysts import market_analyst as mod

    EXECUTED.clear()
    standard_call = AIMessage(
        content="",
        tool_calls=[{"name": "get_stockstats_indicators_report",
                     "args": {"ticker": "AAPL", "curr_date": "2026-09-17"},
                     "id": "call-1", "type": "tool_call"}],
    )
    final = AIMessage(content="## Market report\nbased on tool evidence")
    monkeypatch.setattr(
        mod, "ChatPromptTemplate",
        NS(from_messages=lambda *a, **k: _Chain([standard_call, final])),
    )
    monkeypatch.setattr(mod, "capture_agent_prompt", lambda *a, **k: None)
    monkeypatch.setattr(
        "tradingagents.regime.regime_report_block", lambda *a, **k: ""
    )

    state = {
        "messages": [NS(content="analyze AAPL")],
        "company_of_interest": "AAPL",
        "trade_date": "2026-09-17",
    }
    node = mod.create_market_analyst(_FakeLLM(), _Toolkit())
    out = node(state)

    assert EXECUTED, "standard .tool_calls must drive the tool loop"
    assert any("get_stockstats_indicators_report" == name for name, _ in EXECUTED)
    assert "Market report" in out["market_report"]
    assert out["analysis_status"]["market"] == "completed"


def test_l02_raw_additional_kwargs_tool_calls_still_work(monkeypatch):
    """Backward compatibility: the legacy additional_kwargs shape must keep
    executing tools after the standard-field path is added."""
    from tradingagents.agents.analysts import market_analyst as mod

    EXECUTED.clear()
    raw_call = AIMessage(content="")
    raw_call.additional_kwargs = {"tool_calls": [{
        "name": "get_stockstats_indicators_report",
        "args": {"ticker": "MSFT", "curr_date": "2026-09-17"},
        "id": "call-raw", "type": "tool_call",
    }]}
    final = AIMessage(content="## Market report\nraw path")
    monkeypatch.setattr(
        mod, "ChatPromptTemplate",
        NS(from_messages=lambda *a, **k: _Chain([raw_call, final])),
    )
    monkeypatch.setattr(mod, "capture_agent_prompt", lambda *a, **k: None)
    monkeypatch.setattr(
        "tradingagents.regime.regime_report_block", lambda *a, **k: ""
    )

    state = {
        "messages": [NS(content="analyze MSFT")],
        "company_of_interest": "MSFT",
        "trade_date": "2026-09-17",
    }
    node = mod.create_market_analyst(_FakeLLM(), _Toolkit())
    out = node(state)

    assert EXECUTED, "raw additional_kwargs tool calls must still execute"
    assert "Market report" in out["market_report"]
    assert out["analysis_status"]["market"] == "completed"
