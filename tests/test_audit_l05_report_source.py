"""L05 regression: the parallel coordinator must prefer the explicit
report field over the trailing message content.

The market analyst appends the deterministic regime block to the
market_report field AFTER creating the trailing AIMessage, so the
message content is the stale, regime-less variant. The merger reads the
message first, so the stale text wins — downstream agents never see the
regime block.
"""

import tradingagents.agents  # noqa: F401
from langchain_core.messages import AIMessage


class _DeleteNodes:
    def __getitem__(self, key):
        return lambda state: {"messages": state["messages"]}


def test_l05_report_field_wins_over_message(monkeypatch):
    from tradingagents.graph.setup import GraphSetup

    owner = GraphSetup.__new__(GraphSetup)
    owner.config = {"analyst_call_delay": 0, "analyst_start_delay": 0,
                    "tool_result_delay": 0}
    monkeypatch.setattr("tradingagents.graph.setup.time.sleep", lambda s: None)

    def fake_market_node(state):
        return {
            "messages": [AIMessage(content="stale message WITHOUT regime block")],
            "market_report": "report WITH regime block",
            "analysis_status": {"market": "completed"},
            "analysis_errors": {},
        }

    coordinator = owner._create_parallel_analysts_coordinator(
        ["market"],
        {"market": fake_market_node},
        {"market": None},
        {"market": lambda state: {"messages": state["messages"]}},
    )
    out = coordinator({"company_of_interest": "AAPL", "messages": []})
    assert "WITH regime block" in out["market_report"], (
        "the explicit report field must win over the stale message content"
    )
    assert "WITHOUT regime block" not in out["market_report"]
