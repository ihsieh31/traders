import unittest

from tradingagents.graph.signal_processing import SignalProcessor


class FailingLLM:
    def invoke(self, _messages):
        raise AssertionError("LLM should not be called for deterministic final proposals")


class SignalProcessorTests(unittest.TestCase):
    def test_extracts_executable_action_without_llm(self):
        processor = SignalProcessor(FailingLLM())

        self.assertEqual(
            processor.process_signal("Advisory Rating: Overweight\nFINAL TRANSACTION PROPOSAL: **BUY**"),
            "BUY",
        )
        self.assertEqual(
            processor.process_signal("FINAL TRANSACTION PROPOSAL: **SHORT**"),
            "SHORT",
        )

    def test_the_last_final_proposal_wins_over_a_quoted_earlier_one(self):
        # A risk manager quoting the trader's proposal before overruling it
        # used to be read by a fixed LONG > SHORT > ... priority instead.
        processor = SignalProcessor(FailingLLM())
        text = (
            "Trader proposed: FINAL TRANSACTION PROPOSAL: **LONG**\n"
            "After the risk debate we overrule it.\n"
            "FINAL TRANSACTION PROPOSAL: **NEUTRAL**"
        )
        self.assertEqual(processor.process_signal(text), "NEUTRAL")
        self.assertEqual(
            processor.process_signal("FINAL TRANSACTION PROPOSAL: **BUY** ... FINAL TRANSACTION PROPOSAL: **SELL**"),
            "SELL",
        )

    def test_conflicting_tail_keywords_are_not_resolved_by_priority(self):
        class StaticLLM:
            def invoke(self, _messages):
                return type("R", (), {"content": "SHORT"})()

        # "LONG" used to win only because it is checked first.
        text = "Momentum faded; rather than stay LONG we now prefer to go SHORT"
        self.assertEqual(SignalProcessor(StaticLLM()).process_signal(text), "SHORT")
        self.assertEqual(
            SignalProcessor(FailingLLM()).process_signal("conclusion: stay LONG"), "LONG")


if __name__ == "__main__":
    unittest.main()
