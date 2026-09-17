"""U18 regression: risk-debate parser must strip full speaker prefixes.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.4 U18: "Risky Analyst:"
is 14 characters but the parser slices [:13], leaving a stray ':' at the
start of every risky message. The same off-by-one applies to "Safe
Analyst:" (13 chars — actually correct) and the parser must use
len(prefix)/removeprefix instead of magic numbers at all sites.
"""

import unittest


class U18DebateParserTests(unittest.TestCase):
    def _parse(self, history):
        from webui.components.ui import _parse_risk_debate_history

        return _parse_risk_debate_history(history)

    def test_risky_prefix_fully_stripped(self):
        messages = self._parse("Risky Analyst: Downside is severe here.")
        self.assertEqual(messages, [("risky", "Downside is severe here.")])

    def test_safe_and_neutral_prefixes_stripped(self):
        messages = self._parse(
            "Safe Analyst: Capital preservation first.\n"
            "Neutral Analyst: Split the difference."
        )
        self.assertEqual(
            messages,
            [
                ("safe", "Capital preservation first."),
                ("neutral", "Split the difference."),
            ],
        )

    def test_full_debate_round_trip(self):
        history = (
            "Risky Analyst: Upside is real.\n"
            "Safe Analyst: Downside is real.\n"
            "Neutral Analyst: Both are real."
        )
        messages = self._parse(history)
        self.assertEqual(
            messages,
            [
                ("risky", "Upside is real."),
                ("safe", "Downside is real."),
                ("neutral", "Both are real."),
            ],
        )


if __name__ == "__main__":
    unittest.main()
