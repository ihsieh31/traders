"""A checkpoint may contain data, never an arbitrary constructor capability."""
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage

from tradingagents.graph.checkpointer import get_checkpointer


CONSTRUCTIONS = []


@dataclass
class UntrustedObject:
    value: str

    def __post_init__(self):
        CONSTRUCTIONS.append(self.value)


def test_checkpoint_does_not_reconstruct_untrusted_types(tmp_path):
    with get_checkpointer(tmp_path, "AUDIT") as saver:
        payload = saver.serde.dumps_typed(UntrustedObject("marker"))
        CONSTRUCTIONS.clear()
        restored = saver.serde.loads_typed(payload)
        assert CONSTRUCTIONS == []
        assert not isinstance(restored, UntrustedObject)


def test_strict_checkpoint_roundtrips_normal_trading_state(tmp_path):
    state = {"messages": [HumanMessage(content="test"), AIMessage(content="HOLD")],
             "final_trade_intent": {"symbol": "AAPL", "action": "HOLD"},
             "analysis_evidence": {"sha256": "a" * 64, "market": {"value": "data"}}}
    with get_checkpointer(tmp_path, "AUDIT") as saver:
        assert saver.serde.loads_typed(saver.serde.dumps_typed(state)) == state
