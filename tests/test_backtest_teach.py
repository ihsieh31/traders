"""Tests for the backtest -> self-learning-memory bridge (intensive teaching).

The bridge replays decisions this deployment already recorded under
eval_results/ against historical prices, computes each decision's realized
outcome with the same next-open execution discipline the backtest engine
uses, and injects one dated lesson per decision into the persistent
per-agent ChromaDB memories — idempotently, so re-teaching never duplicates.
"""

from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import pandas as pd

from tradingagents.agents.utils.memory import FinancialSituationMemory
from tradingagents.backtest.teach import (
    compute_decision_outcomes,
    teach_memories_from_history,
)


def _fake_embedding(text):
    seed = float(sum(ord(c) for c in text) % 97)
    return [seed / 97.0 + i * 0.01 for i in range(8)]


def _enable_fake_embeddings(memory):
    memory.retrieval_enabled = True
    memory.embeddings_enabled = True
    memory.get_embedding = _fake_embedding


def _make_memories():
    # Unique collection names per call: chroma's ephemeral client is shared
    # process-wide, so a fixed name would leak lessons between tests.
    import uuid

    suffix = uuid.uuid4().hex[:8]
    names = ["bull", "bear", "trader", "invest_judge", "risk_manager"]
    memories = {}
    for name in names:
        memory = FinancialSituationMemory(f"teach_test_{name}_{suffix}")
        _enable_fake_embeddings(memory)
        memories[name] = memory
    return memories


def _price_frame(start="2025-01-06", days=15, opens=None):
    dates = pd.bdate_range(start=start, periods=days)
    if opens is None:
        opens = [100.0 + i for i in range(days)]
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": opens,
            "high": [o + 1.0 for o in opens],
            "low": [o - 1.0 for o in opens],
            "close": [o + 0.5 for o in opens],
            "volume": [1_000.0] * days,
        }
    )


def _write_run(root, symbol, trade_date, final_signal, final_state=None, status="completed"):
    runs_dir = Path(root) / symbol / "TradingAgentsStrategy_logs" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    started = f"{trade_date}T10:00:00+00:00"
    payload = {
        "symbol": symbol,
        "trade_date": trade_date,
        "started_at": started,
        "ended_at": started,
        "status": status,
        "summary": {"final_signal": final_signal, "llm_call_events": 1, "tool_events": 1},
        "snapshots": {"final_state": final_state} if final_state is not None else {},
    }
    name = f"{trade_date}_run.json"
    (runs_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def _final_state(text):
    return {
        "market_report": f"market: {text}",
        "sentiment_report": f"sentiment: {text}",
        "news_report": f"news: {text}",
        "fundamentals_report": f"fundamentals: {text}",
        "final_trade_decision": f"decision text for {text}",
    }


class ComputeDecisionOutcomesTests(unittest.TestCase):
    def test_buy_enters_next_open_and_exits_horizon_open(self):
        prices = _price_frame(days=10)  # opens 100..109
        outcomes = compute_decision_outcomes(
            prices, {"2025-01-06": "BUY"}, horizon_bars=3
        )
        self.assertEqual(len(outcomes), 1)
        out = outcomes[0]
        # Decision on the first bar's date -> entry at second bar's open (101),
        # exit 3 bars later at open 104.
        self.assertEqual(out["entry_price"], 101.0)
        self.assertEqual(out["exit_price"], 104.0)
        self.assertAlmostEqual(out["asset_return"], 104.0 / 101.0 - 1.0)
        self.assertAlmostEqual(out["decision_return"], out["asset_return"])
        self.assertFalse(out["partial"])

    def test_sell_closes_exposure(self):
        prices = _price_frame(days=10)
        outcomes = compute_decision_outcomes(
            prices, {"2025-01-06": "SELL"}, horizon_bars=3
        )
        out = outcomes[0]
        self.assertEqual(out["decision_return"], 0.0)

    def test_hold_without_known_position_has_unknown_return(self):
        prices = _price_frame(days=10)
        outcomes = compute_decision_outcomes(
            prices, {"2025-01-06": "HOLD"}, horizon_bars=3
        )
        out = outcomes[0]
        self.assertIsNone(out["decision_return"])
        self.assertGreater(out["asset_return"], 0.0)

    def test_truncated_horizon_is_not_taught(self):
        prices = _price_frame(days=5)
        # Entry at bar 1, horizon 10 runs past the data -> exit at last bar.
        outcomes = compute_decision_outcomes(
            prices, {"2025-01-06": "BUY"}, horizon_bars=10
        )
        self.assertEqual(outcomes, [])

    def test_signal_on_or_after_last_bar_is_skipped(self):
        prices = _price_frame(days=5)  # last bar 2025-01-10
        outcomes = compute_decision_outcomes(
            prices, {"2025-01-10": "BUY", "2025-02-01": "BUY"}, horizon_bars=3
        )
        self.assertEqual(outcomes, [])

    def test_weekend_signal_applies_to_next_bar(self):
        prices = _price_frame(days=10)
        # Saturday decision -> entry at Monday+1 bar open.
        outcomes = compute_decision_outcomes(
            prices, {"2025-01-11": "BUY"}, horizon_bars=2
        )
        self.assertEqual(len(outcomes), 1)
        # First bar strictly after 2025-01-11 is Mon 2025-01-13 (open 105).
        self.assertEqual(outcomes[0]["entry_price"], 105.0)


class TeachMemoriesTests(unittest.TestCase):
    def _teach(self, root, memories, **kwargs):
        prices = _price_frame(days=15)
        return teach_memories_from_history(
            "AAPL",
            memories,
            price_loader=lambda symbol, start, end: prices,
            eval_results_dir=root,
            horizon_bars=3,
            **kwargs,
        )

    def test_deterministic_lessons_written_to_all_memories(self):
        memories = _make_memories()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            _write_run(root, "AAPL", "2025-01-06", "BUY", _final_state("day one"))
            _write_run(root, "AAPL", "2025-01-08", "SELL", _final_state("day three"))
            summary = self._teach(root, memories)

        self.assertEqual(summary["decisions_taught"], 2)
        for memory in memories.values():
            self.assertEqual(memory.situation_collection.count(), 2)

        # The lesson stored against the day-one situation carries the
        # realized outcome. (The fake embedding is not semantic, so query
        # with the exact situation text instead of a fragment.)
        day_one = _final_state("day one")
        situation = "\n\n".join(
            day_one[k]
            for k in (
                "market_report",
                "sentiment_report",
                "news_report",
                "fundamentals_report",
            )
        )
        matches = memories["trader"].get_memories(situation, n_matches=1, as_of=(datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat())
        self.assertEqual(len(matches), 1)
        self.assertIn("%", matches[0]["recommendation"])
        self.assertIn("BUY", matches[0]["recommendation"])

    def test_teaching_is_idempotent(self):
        memories = _make_memories()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            _write_run(root, "AAPL", "2025-01-06", "BUY", _final_state("day one"))
            first = self._teach(root, memories)
            second = self._teach(root, memories)

        self.assertEqual(first["decisions_taught"], 1)
        self.assertEqual(second["decisions_taught"], 0)
        self.assertEqual(second["decisions_skipped_duplicate"], 1)
        for memory in memories.values():
            self.assertEqual(memory.situation_collection.count(), 1)

    def test_lessons_are_tagged_for_later_maintenance(self):
        memories = _make_memories()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            _write_run(root, "AAPL", "2025-01-06", "BUY", _final_state("day one"))
            self._teach(root, memories)

        stored = memories["bull"].situation_collection.get(include=["metadatas"])
        meta = stored["metadatas"][0]
        self.assertEqual(meta["source"], "backtest_teach")
        self.assertEqual(meta["symbol"], "AAPL")
        self.assertEqual(meta["trade_date"], "2025-01-06")
        self.assertEqual(meta["action"], "BUY")

    def test_decision_without_final_state_is_skipped(self):
        memories = _make_memories()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            _write_run(root, "AAPL", "2025-01-06", "BUY", final_state=None)
            summary = self._teach(root, memories)

        self.assertEqual(summary["decisions_taught"], 0)
        self.assertEqual(summary["decisions_skipped_no_state"], 1)
        for memory in memories.values():
            self.assertEqual(memory.situation_collection.count(), 0)

    def test_single_outcome_does_not_generate_causal_llm_lesson(self):
        memories = _make_memories()
        reflector = Mock()
        reflector.reflect_on_final_decision.return_value = "LLM lesson about the trade"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            _write_run(root, "AAPL", "2025-01-06", "BUY", _final_state("day one"))
            summary = self._teach(root, memories, reflector=reflector)

        self.assertEqual(summary["decisions_taught"], 1)
        reflector.reflect_on_final_decision.assert_not_called()
        matches = memories["trader"].get_memories("market: day one", n_matches=1, as_of=(datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat())
        self.assertIn("not broker realized P&L", matches[0]["recommendation"])

    def test_no_recorded_signals_raises(self):
        memories = _make_memories()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            with self.assertRaises(ValueError):
                self._teach(root, memories)


class DefaultAgentMemoriesTests(unittest.TestCase):
    def test_builds_the_five_reflection_memories(self):
        from tradingagents.backtest.teach import default_agent_memories

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            memories = default_agent_memories({"agent_memory_dir": tmp})
            self.assertEqual(
                sorted(memories),
                ["bear", "bull", "invest_judge", "risk_manager", "trader"],
            )
            # Collection names must match the ones TradingAgentsGraph uses,
            # so taught lessons are retrieved by the live agents.
            self.assertEqual(
                memories["bull"].situation_collection.name, "bull_memory"
            )


class MemoryMetadataTests(unittest.TestCase):
    def test_add_situations_accepts_extra_metadata(self):
        memory = FinancialSituationMemory("teach_test_meta_memory")
        _enable_fake_embeddings(memory)
        memory.add_situations(
            [("situation text", "advice text")],
            extra_metadata={"source": "backtest_teach", "teach_key": "AAPL|2025-01-06"},
        )
        stored = memory.situation_collection.get(include=["metadatas"])
        meta = stored["metadatas"][0]
        self.assertEqual(meta["recommendation"], "advice text")
        self.assertEqual(meta["teach_key"], "AAPL|2025-01-06")

    def test_has_metadata_value(self):
        memory = FinancialSituationMemory("teach_test_haskey_memory")
        _enable_fake_embeddings(memory)
        self.assertFalse(memory.has_metadata_value("teach_key", "AAPL|2025-01-06"))
        memory.add_situations(
            [("situation text", "advice text")],
            extra_metadata={"teach_key": "AAPL|2025-01-06"},
        )
        self.assertTrue(memory.has_metadata_value("teach_key", "AAPL|2025-01-06"))
        self.assertFalse(memory.has_metadata_value("teach_key", "AAPL|2025-01-07"))


if __name__ == "__main__":
    unittest.main()
