import tempfile
import unittest
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from tradingagents.graph.checkpointer import (
    clear_checkpoint,
    get_checkpointer,
    has_checkpoint,
    thread_id,
)


class CounterState(TypedDict):
    value: int


class CheckpointerTests(unittest.TestCase):
    def test_checkpoint_is_ticker_date_isolated_and_clearable(self):
        workflow = StateGraph(CounterState)
        workflow.add_node("increment", lambda state: {"value": state["value"] + 1})
        workflow.add_edge(START, "increment")
        workflow.add_edge("increment", END)

        with tempfile.TemporaryDirectory() as tmp:
            config = {"configurable": {"thread_id": thread_id("BTC/USD", "2026-01-02")}}
            with get_checkpointer(tmp, "BTC/USD") as checkpointer:
                graph = workflow.compile(checkpointer=checkpointer)
                self.assertEqual(graph.invoke({"value": 1}, config=config)["value"], 2)

            self.assertTrue(has_checkpoint(tmp, "BTC/USD", "2026-01-02"))
            self.assertFalse(has_checkpoint(tmp, "ETH/USD", "2026-01-02"))

            clear_checkpoint(tmp, "BTC/USD", "2026-01-02")
            self.assertFalse(has_checkpoint(tmp, "BTC/USD", "2026-01-02"))

    def test_thread_id_is_scoped_by_source_and_observation(self):
        direct = thread_id("AAPL", "2026-01-02")
        cli = thread_id("AAPL", "2026-01-02", source="cli_stream")
        webui = thread_id("AAPL", "2026-01-02", source="webui_stream")
        observation_a = thread_id(
            "AAPL", "2026-01-02", source="long_run", observation_id="obs-a"
        )
        observation_b = thread_id(
            "AAPL", "2026-01-02", source="long_run", observation_id="obs-b"
        )

        self.assertEqual(len({direct, cli, webui, observation_a, observation_b}), 5)


if __name__ == "__main__":
    unittest.main()
