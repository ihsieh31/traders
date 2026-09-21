import json
import tempfile
import unittest
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG
from scripts.run_analysis_ab import (
    assert_ab_invariants,
    build_ab_configs,
    run_analysis_ab,
)
from scripts.summarize_analysis_ab import summarize


class FakeGraph:
    configs = []

    def __init__(self, selected_analysts, debug, config):
        self.selected_analysts = selected_analysts
        self.debug = debug
        self.config = config
        type(self).configs.append(config)

    def propagate(self, symbol, trade_date):
        profile = self.config["analysis_profile"]
        return (
            {"company_of_interest": symbol, "trade_date": trade_date, "report_context": {}},
            "BUY" if profile == "traders" else "HOLD",
        )


class AnalysisABRunnerTests(unittest.TestCase):
    def setUp(self):
        FakeGraph.configs.clear()

    def test_configs_share_experiment_inputs_and_isolate_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = dict(DEFAULT_CONFIG)
            base.update({"quick_think_llm": "same-quick", "deep_think_llm": "same-deep"})
            configs = build_ab_configs(
                base,
                symbol="NVDA",
                trade_date="2026-09-21",
                pair_dir=Path(tmp) / "pair",
            )
            self.assertEqual(configs["traders"]["quick_think_llm"], configs["berkshire"]["quick_think_llm"])
            self.assertEqual(configs["traders"]["checkpoint_enabled"], False)
            self.assertEqual(configs["berkshire"]["memory_retrieval_enabled"], True)
            self.assertEqual(configs["traders"]["reflection_on_outcome_enabled"], True)
            self.assertEqual(configs["berkshire"]["memory_maintenance_enabled"], True)
            self.assertNotEqual(configs["traders"]["results_dir"], configs["berkshire"]["results_dir"])
            self.assertNotEqual(configs["traders"]["_analysis_source"], configs["berkshire"]["_analysis_source"])
            self.assertNotEqual(configs["traders"]["data_cache_dir"], configs["berkshire"]["data_cache_dir"])
            self.assertNotEqual(configs["traders"]["screening_selection_cache_path"], configs["berkshire"]["screening_selection_cache_path"])
            self.assertIn("_profiles/traders", configs["traders"]["agent_memory_dir"])
            self.assertIn("_profiles/berkshire", configs["berkshire"]["agent_memory_dir"])

    def test_shared_memory_path_is_rejected(self):
        a = {
            "analysis_profile": "traders", "auto_trade": False,
            "memory_retrieval_enabled": True, "reflection_on_outcome_enabled": True,
            "memory_maintenance_enabled": True,
        }
        b = dict(a, analysis_profile="berkshire")
        for key in (
            "results_dir", "memory_log_path", "agent_memory_dir", "data_cache_dir",
            "screening_selection_cache_path", "execution_db_path",
            "execution_lock_dir", "long_run_dir",
        ):
            a[key] = b[key] = f"/tmp/shared/{key}"
        with self.assertRaisesRegex(RuntimeError, "results_dir is shared"):
            assert_ab_invariants(a, b)

    def test_different_shared_model_is_rejected(self):
        a = {"analysis_profile": "traders", "model": "a", "results_dir": "a"}
        b = {"analysis_profile": "berkshire", "model": "b", "results_dir": "b"}
        with self.assertRaisesRegex(RuntimeError, "A/B invariant violation.*model"):
            assert_ab_invariants(a, b)

    def test_runner_serializes_profiles_and_writes_pair_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_analysis_ab(
                symbol="NVDA",
                trade_date="2026-09-21",
                base_config=DEFAULT_CONFIG,
                results_root=Path(tmp) / "ab",
                selected_analysts=("market", "news"),
                graph_cls=FakeGraph,
            )
            self.assertEqual(summary["traders"]["status"], "completed")
            self.assertEqual(summary["berkshire"]["status"], "completed")
            self.assertFalse(summary["signal_agreement"])
            self.assertEqual(
                [config["analysis_profile"] for config in FakeGraph.configs],
                summary["execution_order"],
            )
            pair_summary = Path(tmp) / "ab" / "2026-09-21" / "NVDA" / "pair_summary.json"
            self.assertTrue(pair_summary.exists())
            persisted = json.loads(pair_summary.read_text(encoding="utf-8"))
            self.assertEqual(persisted["shared"]["checkpoint_enabled"], False)
            self.assertEqual(persisted["shared"]["memory_retrieval_enabled"], True)
            self.assertEqual(persisted["shared"]["auto_trade"], False)
            self.assertEqual(len(persisted["campaign_fingerprint"]), 64)

    def test_campaign_rejects_midstream_config_drift_and_pair_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ab"
            run_analysis_ab(
                symbol="NVDA", trade_date="2026-09-21",
                base_config=DEFAULT_CONFIG, results_root=root,
                selected_analysts=("market", "news"), graph_cls=FakeGraph,
            )
            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                run_analysis_ab(
                    symbol="NVDA", trade_date="2026-09-21",
                    base_config=DEFAULT_CONFIG, results_root=root,
                    selected_analysts=("market", "news"), graph_cls=FakeGraph,
                )
            changed = dict(DEFAULT_CONFIG, quick_think_llm="different-model")
            with self.assertRaisesRegex(RuntimeError, "campaign invariant violation"):
                run_analysis_ab(
                    symbol="AAPL", trade_date="2026-09-22",
                    base_config=changed, results_root=root,
                    selected_analysts=("market", "news"), graph_cls=FakeGraph,
                )

    def test_summary_aggregates_pair_signals_without_report_duplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair_dir = Path(tmp) / "2026-09-21" / "AAPL"
            pair_dir.mkdir(parents=True)
            (pair_dir / "pair_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pair_id": "pair-1",
                        "signal_agreement": True,
                        "traders": {
                            "status": "completed",
                            "signal": "BUY",
                            "final_state_keys": ["report_context"],
                        },
                        "berkshire": {
                            "status": "completed",
                            "signal": "BUY",
                            "final_state_keys": ["report_context"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = summarize(tmp)
            self.assertEqual(result["pair_count"], 1)
            self.assertEqual(result["signal_agreement_pairs"], 1)
            self.assertEqual(result["profiles"]["traders"]["signals"], {"BUY": 1})
            self.assertEqual(result["profiles"]["berkshire"]["report_context_runs"], 1)


if __name__ == "__main__":
    unittest.main()
