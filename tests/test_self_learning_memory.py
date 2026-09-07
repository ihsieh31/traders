"""Tests for the self-learning memory loop: persistence, snapshot recovery,
and outcome-driven reflection into the per-agent ChromaDB memories."""

from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tradingagents.agents.utils.memory import FinancialSituationMemory
from tradingagents.graph.reflection import Reflector
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.run_logger import load_final_state_snapshot


def _fake_embedding(text):
    # Deterministic 8-dim embedding so similar texts produce similar vectors.
    seed = float(sum(ord(c) for c in text) % 97)
    return [seed / 97.0 + i * 0.01 for i in range(8)]


def _enable_fake_embeddings(memory):
    memory.embeddings_enabled = True
    memory.get_embedding = _fake_embedding


class MemoryPersistenceTests(unittest.TestCase):
    def test_default_memory_stays_in_process(self):
        memory = FinancialSituationMemory("ephemeral_test_memory")
        self.assertEqual(type(memory.chroma_client).__name__, "Client")

    def test_persistent_memory_survives_reopen(self):
        # ignore_cleanup_errors: chromadb keeps its store files open on
        # Windows, so temp-dir removal can race the process handle.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = {"agent_memory_dir": tmp, "memory_retrieval_enabled": True}

            first = FinancialSituationMemory("persist_test_memory", config)
            _enable_fake_embeddings(first)
            first.add_situations(
                [("High inflation with rising rates", "Prefer defensive sectors")]
            )
            self.assertEqual(first.situation_collection.count(), 1)

            second = FinancialSituationMemory("persist_test_memory", config)
            _enable_fake_embeddings(second)
            self.assertEqual(second.situation_collection.count(), 1)

            matches = second.get_memories("High inflation with rising rates", n_matches=1, as_of=(datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat())
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["recommendation"], "Prefer defensive sectors")

    def test_ids_do_not_collide_across_instances(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = {"agent_memory_dir": tmp, "memory_retrieval_enabled": True}
            for i in range(3):
                memory = FinancialSituationMemory("collision_test_memory", config)
                _enable_fake_embeddings(memory)
                memory.add_situations([(f"situation {i}", f"advice {i}")])
            final = FinancialSituationMemory("collision_test_memory", config)
            self.assertEqual(final.situation_collection.count(), 3)

    def test_empty_agent_memory_dir_means_ephemeral(self):
        memory = FinancialSituationMemory("cfg_test_memory", {"agent_memory_dir": ""})
        self.assertEqual(type(memory.chroma_client).__name__, "Client")


def _write_run(root, symbol, trade_date, status="completed", final_state=None, started_at=None):
    runs_dir = Path(root) / symbol / "TradingAgentsStrategy_logs" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    started = started_at or f"{trade_date}T10:00:00+00:00"
    payload = {
        "symbol": symbol,
        "trade_date": trade_date,
        "started_at": started,
        "status": status,
        "snapshots": {"final_state": final_state} if final_state is not None else {},
    }
    name = f"{trade_date}_{started.replace(':', '').replace('+', '')}.json"
    (runs_dir / name).write_text(json.dumps(payload), encoding="utf-8")


class LoadFinalStateSnapshotTests(unittest.TestCase):
    def test_returns_latest_completed_snapshot_for_date(self):
        with tempfile.TemporaryDirectory() as root:
            _write_run(
                root, "AAPL", "2026-01-05",
                final_state={"market_report": "old"},
                started_at="2026-01-05T09:00:00+00:00",
            )
            _write_run(
                root, "AAPL", "2026-01-05",
                final_state={"market_report": "new"},
                started_at="2026-01-05T15:00:00+00:00",
            )
            snapshot = load_final_state_snapshot("AAPL", "2026-01-05", eval_results_dir=root)
        self.assertEqual(snapshot, {"market_report": "new"})

    def test_ignores_failed_runs_other_dates_and_missing_snapshots(self):
        with tempfile.TemporaryDirectory() as root:
            _write_run(root, "AAPL", "2026-01-05", status="failed",
                       final_state={"market_report": "failed"})
            _write_run(root, "AAPL", "2026-01-06", final_state={"market_report": "other day"})
            _write_run(root, "AAPL", "2026-01-05", final_state=None)
            self.assertIsNone(
                load_final_state_snapshot("AAPL", "2026-01-05", eval_results_dir=root)
            )

    def test_missing_directory_returns_none(self):
        self.assertIsNone(
            load_final_state_snapshot("ZZZZ", "2026-01-05", eval_results_dir="no_such_dir")
        )

    def test_crypto_symbol_is_sanitized(self):
        with tempfile.TemporaryDirectory() as root:
            _write_run(root, "BTC_USD", "2026-01-05", final_state={"market_report": "btc"})
            snapshot = load_final_state_snapshot("BTC/USD", "2026-01-05", eval_results_dir=root)
        self.assertEqual(snapshot, {"market_report": "btc"})


def _full_state():
    return {
        "market_report": "market",
        "sentiment_report": "sentiment",
        "news_report": "news",
        "fundamentals_report": "fundamentals",
        "investment_debate_state": {
            "bull_history": "bull said",
            "bear_history": "bear said",
            "judge_decision": "judge decided",
        },
        "trader_investment_plan": "trader plan",
        "risk_debate_state": {"judge_decision": "risk decision"},
    }


class ReflectOnOutcomeTests(unittest.TestCase):
    def _reflector(self):
        llm = Mock()
        llm.invoke.return_value = Mock(content="lesson learned")
        return Reflector(llm)

    def test_all_components_reflect_and_store(self):
        reflector = self._reflector()
        memories = {
            name: Mock()
            for name in ("bull", "bear", "trader", "invest_judge", "risk_manager")
        }
        results = reflector.reflect_on_outcome(_full_state(), "+5.0% realized", memories)

        self.assertEqual(set(results), set(memories))
        self.assertTrue(all(results.values()))
        for memory in memories.values():
            memory.add_situations.assert_called_once()
            (pairs,), _ = memory.add_situations.call_args
            situation, lesson = pairs[0]
            self.assertIn("market", situation)
            self.assertEqual(lesson, "lesson learned")

    def test_one_failure_does_not_block_other_components(self):
        reflector = self._reflector()
        memories = {
            "bull": Mock(add_situations=Mock(side_effect=RuntimeError("chroma down"))),
            "trader": Mock(),
        }
        results = reflector.reflect_on_outcome(_full_state(), "-2.0% realized", memories)
        self.assertFalse(results["bull"])
        self.assertTrue(results["trader"])
        memories["trader"].add_situations.assert_called_once()

    def test_missing_memories_are_skipped(self):
        reflector = self._reflector()
        results = reflector.reflect_on_outcome(_full_state(), "+1.0%", {"bull": Mock()})
        self.assertEqual(set(results), {"bull"})


class OutcomeReflectionWiringTests(unittest.TestCase):
    """Exercise TradingAgentsGraph._reflect_agents_on_outcome via a stub self."""

    def _fake_graph(self, enabled=True):
        fake = Mock(spec=TradingAgentsGraph)
        fake.config = {"reflection_on_outcome_enabled": enabled}
        fake.reflector = Mock()
        for name in (
            "bull_memory",
            "bear_memory",
            "trader_memory",
            "invest_judge_memory",
            "risk_manager_memory",
        ):
            setattr(fake, name, Mock())
        return fake

    def test_resolved_outcome_triggers_reflection_with_recovered_state(self):
        fake = self._fake_graph()
        state = _full_state()
        with patch(
            "tradingagents.run_logger.load_final_state_snapshot", return_value=state
        ):
            TradingAgentsGraph._reflect_agents_on_outcome(
                fake, "AAPL", "2026-01-05", 0.05, 0.02, 5
            )
        fake.reflector.reflect_on_outcome.assert_called_once()
        args, _ = fake.reflector.reflect_on_outcome.call_args
        passed_state, returns_losses, memories = args
        self.assertIs(passed_state, state)
        self.assertIn("+5.0%", returns_losses)
        self.assertIn("+2.0%", returns_losses)
        self.assertEqual(
            set(memories),
            {"bull", "bear", "trader", "invest_judge", "risk_manager"},
        )

    def test_disabled_flag_skips_reflection(self):
        fake = self._fake_graph(enabled=False)
        with patch(
            "tradingagents.run_logger.load_final_state_snapshot"
        ) as loader:
            TradingAgentsGraph._reflect_agents_on_outcome(
                fake, "AAPL", "2026-01-05", 0.05, None, 5
            )
        loader.assert_not_called()
        fake.reflector.reflect_on_outcome.assert_not_called()

    def test_missing_snapshot_skips_reflection(self):
        fake = self._fake_graph()
        with patch(
            "tradingagents.run_logger.load_final_state_snapshot", return_value=None
        ):
            TradingAgentsGraph._reflect_agents_on_outcome(
                fake, "AAPL", "2026-01-05", 0.05, None, 5
            )
        fake.reflector.reflect_on_outcome.assert_not_called()

    def test_reflection_errors_never_propagate(self):
        fake = self._fake_graph()
        fake.reflector.reflect_on_outcome.side_effect = RuntimeError("llm down")
        with patch(
            "tradingagents.run_logger.load_final_state_snapshot",
            return_value=_full_state(),
        ):
            # Must not raise: outcome reflection is strictly best-effort.
            TradingAgentsGraph._reflect_agents_on_outcome(
                fake, "AAPL", "2026-01-05", 0.05, None, 5
            )


if __name__ == "__main__":
    unittest.main()
