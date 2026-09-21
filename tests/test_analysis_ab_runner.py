import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tradingagents.default_config import DEFAULT_CONFIG
from scripts.run_analysis_ab import (
    assert_ab_invariants,
    build_ab_configs,
    run_analysis_ab,
)
from scripts.summarize_analysis_ab import summarize
from tradingagents.experiments.evidence_snapshot import evidence_packet_sha256


class FakeGraph:
    configs = []

    def __init__(self, selected_analysts, debug, config):
        self.selected_analysts = selected_analysts
        self.debug = debug
        self.config = config
        type(self).configs.append(config)

    def propagate(self, symbol, trade_date):
        backend = self.config["analysis_backend"]
        return (
            {"company_of_interest": symbol, "trade_date": trade_date, "report_context": {}},
            "BUY" if backend == "traders" else "HOLD",
        )


def fake_evidence(path, *, symbol, trade_date, **_kwargs):
    packet = {
        "schema_version": 1, "symbol": symbol, "trade_date": trade_date,
        "captured_at": "2026-09-21T00:00:00+00:00",
        "market": {}, "fundamentals": {}, "news": {}, "macro": {}, "social": {},
        "sources": [], "errors": [],
    }
    packet["sha256"] = evidence_packet_sha256(packet)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet), encoding="utf-8")
    return packet


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
            self.assertNotEqual(configs["traders"]["analysis_backend"], configs["berkshire"]["analysis_backend"])
            self.assertEqual(configs["traders"]["analysis_profile"], configs["berkshire"]["analysis_profile"])
            self.assertEqual(configs["traders"]["analysis_input_mode"], "frozen_evidence")
            self.assertEqual(configs["traders"]["evidence_packet_path"], configs["berkshire"]["evidence_packet_path"])
            self.assertEqual(configs["traders"]["evidence_packet_sha256"], configs["berkshire"]["evidence_packet_sha256"])
            self.assertNotEqual(configs["traders"]["results_dir"], configs["berkshire"]["results_dir"])
            self.assertNotEqual(configs["traders"]["_analysis_source"], configs["berkshire"]["_analysis_source"])
            self.assertNotEqual(configs["traders"]["data_cache_dir"], configs["berkshire"]["data_cache_dir"])
            self.assertNotEqual(configs["traders"]["screening_selection_cache_path"], configs["berkshire"]["screening_selection_cache_path"])
            self.assertIn("_profiles/traders", configs["traders"]["agent_memory_dir"])
            self.assertIn("_profiles/berkshire", configs["berkshire"]["agent_memory_dir"])

    def test_shared_memory_path_is_rejected(self):
        a = {
            "analysis_backend": "traders", "analysis_profile": "traders",
            "analysis_input_mode": "frozen_evidence", "evidence_packet_path": "/tmp/evidence.json",
            "evidence_packet_sha256": "a" * 64, "checkpoint_enabled": False, "auto_trade": False,
            "memory_retrieval_enabled": True, "reflection_on_outcome_enabled": True,
            "memory_maintenance_enabled": True,
        }
        b = dict(a, analysis_backend="berkshire")
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
        b = {"analysis_profile": "traders", "analysis_backend": "berkshire", "model": "b", "results_dir": "b"}
        with self.assertRaisesRegex(RuntimeError, "A/B invariant violation.*model"):
            assert_ab_invariants(a, b)

    def test_runner_serializes_profiles_and_writes_pair_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                summary = run_analysis_ab(
                    symbol="NVDA", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=Path(tmp) / "ab", selected_analysts=("market", "news"), graph_cls=FakeGraph,
                )
            self.assertEqual(summary["traders"]["status"], "completed")
            self.assertEqual(summary["berkshire"]["status"], "completed")
            self.assertFalse(summary["signal_agreement"])
            self.assertEqual(
                [config["analysis_backend"] for config in FakeGraph.configs],
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
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                run_analysis_ab(
                    symbol="NVDA", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=("market", "news"), graph_cls=FakeGraph,
                )
            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                    run_analysis_ab(
                        symbol="NVDA", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                        results_root=root, selected_analysts=("market", "news"), graph_cls=FakeGraph,
                    )
            changed = dict(DEFAULT_CONFIG, quick_think_llm="different-model")
            with self.assertRaisesRegex(RuntimeError, "campaign invariant violation"):
                with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                    run_analysis_ab(
                        symbol="AAPL", trade_date="2026-09-22", base_config=changed,
                        results_root=root, selected_analysts=("market", "news"), graph_cls=FakeGraph,
                    )

    def test_partial_pair_reuses_completed_arm_and_writes_summary_only_after_resume(self):
        class FlakyGraph(FakeGraph):
            attempts = {}

            def propagate(self, symbol, trade_date):
                backend = self.config["analysis_backend"]
                type(self).attempts[backend] = type(self).attempts.get(backend, 0) + 1
                if backend == "berkshire" and type(self).attempts[backend] == 1:
                    error = type("ProviderFailure", (Exception,), {})
                    raise error("transient provider timeout")
                return super().propagate(symbol, trade_date)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ab"
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                first = run_analysis_ab(
                    symbol="NVDA", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=("market", "news"), graph_cls=FlakyGraph,
                )
                self.assertEqual(first["status"], "PARTIAL")
                self.assertFalse((root / "2026-09-21" / "NVDA" / "pair_summary.json").exists())
                second = run_analysis_ab(
                    symbol="NVDA", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=("market", "news"), graph_cls=FlakyGraph,
                )
            self.assertEqual(second["schema_version"], 2)
            self.assertEqual(FlakyGraph.attempts["traders"], 1)
            self.assertEqual(FlakyGraph.attempts["berkshire"], 2)
            self.assertTrue((root / "2026-09-21" / "NVDA" / "pair_summary.json").exists())
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
