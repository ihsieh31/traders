import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.agents.schemas import RiskDecision, build_trade_intent_from_risk_decision
from scripts.run_analysis_ab import (
    FORMAL_ANALYSTS,
    _paper_account_preflight,
    _recover_completed_analysis,
    _ensure_campaign_manifest,
    assert_ab_invariants,
    build_ab_configs,
    run_analysis_ab,
)
from scripts.summarize_analysis_ab import summarize
from tradingagents.experiments.evidence_snapshot import evidence_packet_sha256
from tradingagents.long_run_support.state import atomic_write_json


class FakeGraph:
    configs = []

    def __init__(self, selected_analysts, debug, config):
        self.selected_analysts = selected_analysts
        self.debug = debug
        self.config = config
        type(self).configs.append(config)

    def propagate(self, symbol, trade_date):
        backend = self.config["analysis_backend"]
        signal = "BUY" if backend == "traders" else "HOLD"
        intent = build_trade_intent_from_risk_decision(
            symbol=symbol, trading_mode="investment", current_position="NEUTRAL",
            decision=RiskDecision(
                action=signal, confidence="high", risk_rationale="fixture",
                required_controls="fixture",
            ), trade_date=trade_date,
        ).model_dump(mode="json")
        return (
            {"company_of_interest": symbol, "trade_date": trade_date,
             "report_context": {}, "final_trade_intent": intent},
            signal,
        )


def fake_evidence(path, *, symbol, trade_date, **_kwargs):
    available = lambda value: {"source": {"status": "available", "value": value}}
    packet = {
        "schema_version": 1, "symbol": symbol, "trade_date": trade_date,
        "captured_at": "2026-09-21T00:00:00+00:00",
        "market": available("market"),
        "fundamentals": available("fundamentals"),
        "news": available("news"),
        "macro": available("macro"),
        "social": available("social"),
        "sources": [], "errors": [],
    }
    packet["sha256"] = evidence_packet_sha256(packet)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet), encoding="utf-8")
    return packet


class AnalysisABRunnerTests(unittest.TestCase):
    def setUp(self):
        FakeGraph.configs.clear()

    def test_missing_trade_intent_fails_arm(self):
        class MissingIntentGraph(FakeGraph):
            def propagate(self, symbol, trade_date):
                return {"company_of_interest": symbol}, "HOLD"

        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                summary = run_analysis_ab(
                    symbol="AAPL", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=Path(tmp) / "ab", selected_analysts=FORMAL_ANALYSTS,
                    graph_cls=MissingIntentGraph,
                )
        self.assertEqual(summary["status"], "FAILED_TERMINAL")
        failed = next(arm for arm in summary["arms"].values() if arm["status"] == "failed_terminal")
        self.assertFalse(failed["result"]["decision_valid"])
        self.assertEqual(failed["result"]["error_type"], "InvalidTradeIntent")

    def test_recovered_log_without_trade_intent_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"results_dir": tmp}
            runs = Path(tmp) / "AAPL" / "TradingAgentsStrategy_logs" / "runs"
            runs.mkdir(parents=True)
            (runs / "one.json").write_text(json.dumps({
                "status": "completed", "trade_date": "2026-09-21",
                "metadata": {"analysis_backend": "traders", "analysis_source": "ab_traders",
                             "evidence_packet_sha256": "a" * 64},
                "summary": {"final_signal": "HOLD"},
                "snapshots": {"final_state": {"final_trade_intent": None}},
            }), encoding="utf-8")
            result = _recover_completed_analysis(
                config, backend="traders", symbol="AAPL", trade_date="2026-09-21",
                evidence_sha256="a" * 64,
            )
        self.assertEqual(result["status"], "failed_terminal")
        self.assertFalse(result["decision_valid"])

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
            self.assertEqual(configs["traders"]["reflection_on_outcome_enabled"], False)
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

    def test_reflection_setting_is_inherited_from_base_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            for enabled in (False, True):
                with self.subTest(enabled=enabled):
                    base = dict(DEFAULT_CONFIG)
                    base["reflection_on_outcome_enabled"] = enabled
                    configs = build_ab_configs(
                        base,
                        symbol="NVDA",
                        trade_date="2026-09-21",
                        pair_dir=Path(tmp) / str(enabled),
                    )
                    self.assertIs(
                        configs["traders"]["reflection_on_outcome_enabled"], enabled
                    )
                    self.assertIs(
                        configs["berkshire"]["reflection_on_outcome_enabled"], enabled
                    )

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
                    results_root=Path(tmp) / "ab", selected_analysts=FORMAL_ANALYSTS, graph_cls=FakeGraph,
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
            manifest = json.loads((Path(tmp) / "ab" / "AB_CAMPAIGN.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["shared"]["checkpoint_enabled"], False)
            self.assertEqual(persisted["shared"]["memory_retrieval_enabled"], True)
            self.assertEqual(persisted["shared"]["reflection_on_outcome_enabled"], False)
            self.assertEqual(persisted["shared"]["auto_trade"], False)
            self.assertEqual(len(persisted["campaign_fingerprint"]), 64)
            self.assertEqual(set(manifest["resolved_llm_routes"]), {"traders", "berkshire"})
            self.assertIn("embedding", manifest["resolved_llm_routes"]["traders"])

    def test_runner_restores_process_global_config_after_profiles(self):
        from tradingagents.dataflows.config import get_config, replace_config, set_config

        class MutatingGraph(FakeGraph):
            def __init__(self, selected_analysts, debug, config):
                super().__init__(selected_analysts, debug, config)
                set_config(config)

        before = get_config()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with patch(
                    "scripts.run_analysis_ab.build_or_load_evidence_packet",
                    side_effect=fake_evidence,
                ):
                    run_analysis_ab(
                        symbol="NVDA",
                        trade_date="2026-09-21",
                        base_config=DEFAULT_CONFIG,
                        results_root=Path(tmp) / "ab",
                        selected_analysts=FORMAL_ANALYSTS,
                        graph_cls=MutatingGraph,
                    )
            self.assertEqual(get_config(), before)
        finally:
            replace_config(before)

    def test_campaign_rejects_midstream_config_drift_and_pair_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ab"
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                run_analysis_ab(
                    symbol="NVDA", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FakeGraph,
                )
            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                    run_analysis_ab(
                        symbol="NVDA", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                        results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FakeGraph,
                    )
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                second_pair = run_analysis_ab(
                    symbol="AAPL", trade_date="2026-09-22", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FakeGraph,
                )
            self.assertEqual(second_pair["traders"]["status"], "completed")
            self.assertEqual(second_pair["berkshire"]["status"], "completed")
            changed = dict(DEFAULT_CONFIG, quick_think_llm="different-model")
            with self.assertRaisesRegex(RuntimeError, "campaign invariant violation"):
                with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                    run_analysis_ab(
                        symbol="MSFT", trade_date="2026-09-23", base_config=changed,
                        results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FakeGraph,
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
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FlakyGraph,
                )
                self.assertEqual(first["status"], "PARTIAL")
                self.assertFalse((root / "2026-09-21" / "NVDA" / "pair_summary.json").exists())
                second = run_analysis_ab(
                    symbol="NVDA", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FlakyGraph,
                )
            self.assertEqual(second["schema_version"], 2)
            self.assertEqual(FlakyGraph.attempts["traders"], 1)
            self.assertEqual(FlakyGraph.attempts["berkshire"], 2)
            self.assertTrue((root / "2026-09-21" / "NVDA" / "pair_summary.json").exists())

    def test_campaign_blocks_next_date_when_prior_pair_partial(self):
        calls = []

        class FlakyGraph(FakeGraph):
            def propagate(self, symbol, trade_date):
                calls.append((self.config["analysis_backend"], trade_date))
                if self.config["analysis_backend"] == "berkshire":
                    raise TimeoutError("provider outage")
                return super().propagate(symbol, trade_date)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ab"
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence), patch(
                "scripts.run_analysis_ab._execution_order", return_value=["traders", "berkshire"]
            ):
                first = run_analysis_ab(
                    symbol="AAPL", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FlakyGraph,
                )
                self.assertEqual(first["status"], "PARTIAL")
                with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence) as evidence:
                    with self.assertRaisesRegex(RuntimeError, "continuity"):
                        run_analysis_ab(
                            symbol="AAPL", trade_date="2026-09-22", base_config=DEFAULT_CONFIG,
                            results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FlakyGraph,
                        )
                    evidence.assert_not_called()
            self.assertEqual(calls, [("traders", "2026-09-21"), ("berkshire", "2026-09-21")])

    def test_campaign_allows_resuming_same_partial_pair(self):
        attempts = {"traders": 0, "berkshire": 0}

        class ResumingGraph(FakeGraph):
            def propagate(self, symbol, trade_date):
                backend = self.config["analysis_backend"]
                attempts[backend] += 1
                if backend == "berkshire" and attempts[backend] == 1:
                    raise TimeoutError("provider outage")
                return super().propagate(symbol, trade_date)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ab"
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                first = run_analysis_ab(
                    symbol="AAPL", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=ResumingGraph,
                )
                second = run_analysis_ab(
                    symbol="AAPL", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=ResumingGraph,
                )
            self.assertEqual(first["status"], "PARTIAL")
            self.assertEqual(second["status"], "COMPLETED")
            self.assertEqual(attempts, {"traders": 1, "berkshire": 2})

    def test_campaign_allows_next_date_after_prior_pair_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ab"
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence):
                first = run_analysis_ab(
                    symbol="AAPL", trade_date="2026-09-21", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FakeGraph,
                )
                second = run_analysis_ab(
                    symbol="AAPL", trade_date="2026-09-22", base_config=DEFAULT_CONFIG,
                    results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FakeGraph,
                )
            self.assertEqual(first["status"], "COMPLETED")
            self.assertEqual(second["status"], "COMPLETED")

    def test_campaign_fails_closed_on_malformed_prior_pair_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ab"
            state_path = root / "2026-09-21" / "AAPL" / "pair_state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not-json", encoding="utf-8")
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence) as evidence:
                with self.assertRaisesRegex(RuntimeError, "unreadable"):
                    run_analysis_ab(
                        symbol="AAPL", trade_date="2026-09-22", base_config=DEFAULT_CONFIG,
                        results_root=root, selected_analysts=FORMAL_ANALYSTS, graph_cls=FakeGraph,
                    )
                evidence.assert_not_called()

    def test_campaign_manifest_uses_durable_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ab"
            with patch("scripts.run_analysis_ab.atomic_write_json", wraps=atomic_write_json) as durable:
                _ensure_campaign_manifest(root, DEFAULT_CONFIG, FORMAL_ANALYSTS)
            durable.assert_called_once()
            self.assertEqual(durable.call_args.args[0], root / "AB_CAMPAIGN.json")
            self.assertTrue((root / "AB_CAMPAIGN.json").exists())

    def test_paper_mode_routes_both_arms_through_isolated_accounts(self):
        class PaperGraph(FakeGraph):
            def propagate(self, symbol, trade_date):
                return super().propagate(symbol, trade_date)

        executed = []

        def fake_execute(backend, config, result, **kwargs):
            executed.append((backend, config["_alpaca_account_profile"], kwargs["paper_notional_usd"]))
            return {
                "success": True,
                "broker_attempted": backend == "traders",
                "broker_calls": int(backend == "traders"),
                "hold": backend == "berkshire",
                "orders": [],
                "account": config["_alpaca_account_profile"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.run_analysis_ab.build_or_load_evidence_packet", side_effect=fake_evidence), patch(
                "scripts.run_analysis_ab._paper_account_preflight",
                return_value={
                    "traders": {"account": "A", "account_ref": "ref-a"},
                    "berkshire": {"account": "B", "account_ref": "ref-b"},
                },
            ), patch("scripts.run_analysis_ab._execute_paper_arm", side_effect=fake_execute):
                summary = run_analysis_ab(
                    symbol="NVDA",
                    trade_date="2026-09-21",
                    base_config=DEFAULT_CONFIG,
                    results_root=Path(tmp) / "ab",
                    selected_analysts=FORMAL_ANALYSTS,
                    graph_cls=PaperGraph,
                    execute_paper=True,
                    paper_notional_usd=500.0,
                )
        self.assertEqual(set(executed), {("traders", "A", 500.0), ("berkshire", "B", 500.0)})
        self.assertTrue(summary["shared"]["auto_trade"])
        self.assertEqual(summary["traders"]["paper_execution"]["account"], "A")
        self.assertEqual(summary["berkshire"]["paper_execution"]["account"], "B")

    def test_paper_preflight_proves_distinct_flat_open_accounts(self):
        class Client:
            def __init__(self, account_id):
                self.account_id = account_id

            def get_account(self):
                return SimpleNamespace(id=self.account_id, equity="100000")

            def get_all_positions(self):
                return []

            def get_orders(self, _request):
                return []

            def get_clock(self):
                return SimpleNamespace(
                    is_open=True,
                    timestamp=datetime(
                        2026, 9, 21, 11, 0, tzinfo=ZoneInfo("America/New_York")
                    ),
                )

        clients = {"A": Client("account-a"), "B": Client("account-b")}
        with patch(
            "tradingagents.dataflows.alpaca_utils.alpaca_read_only_enabled",
            return_value=False,
        ), patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
            side_effect=lambda *, account, read_only: clients[account],
        ):
            snapshot = _paper_account_preflight(
                trade_date="2026-09-21", require_matched_flat_start=True
            )
        self.assertEqual(snapshot["traders"]["account"], "A")
        self.assertEqual(snapshot["berkshire"]["account"], "B")
        self.assertNotEqual(
            snapshot["traders"]["account_ref"], snapshot["berkshire"]["account_ref"]
        )

    def test_summary_aggregates_pair_signals_without_report_duplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fingerprint = "f" * 64
            (root / "AB_CAMPAIGN.json").write_text(
                json.dumps({"schema_version": 2, "config_fingerprint": fingerprint}),
                encoding="utf-8",
            )
            evidence_path = root / "evidence" / "2026-09-21" / "AAPL" / "evidence_packet.json"
            evidence = fake_evidence(
                evidence_path, symbol="AAPL", trade_date="2026-09-21"
            )
            pair_dir = root / "2026-09-21" / "AAPL"
            pair_dir.mkdir(parents=True)
            (pair_dir / "pair_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "pair_id": "pair-1",
                        "campaign_fingerprint": fingerprint,
                        "symbol": "AAPL",
                        "trade_date": "2026-09-21",
                        "evidence_sha256": evidence["sha256"],
                        "shared": {
                            "evidence_packet_path": str(evidence_path),
                            "evidence_packet_sha256": evidence["sha256"],
                        },
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
            result = summarize(root)
            self.assertEqual(result["pair_count"], 1)
            self.assertEqual(result["signal_agreement_pairs"], 1)
            self.assertEqual(result["profiles"]["traders"]["signals"], {"BUY": 1})
            self.assertEqual(result["profiles"]["berkshire"]["report_context_runs"], 1)

    def test_paper_arm_uses_shared_session_cutoff_at_execution_and_recovery(self):
        import scripts.run_analysis_ab as ab

        class Service:
            instances = []

            def __init__(self, **_kwargs):
                self.instances.append(self)

            def startup_recover(self, can_submit=None):
                self.recovery_allowed = bool(can_submit and can_submit())
                return {"success": True, "account_execution_state": "CLEAN"}

        def invoke_execution(**kwargs):
            allowed = bool(kwargs["can_submit"]())
            return {
                "success": allowed,
                "broker_attempted": allowed,
                "broker_calls": int(allowed),
                "orders": [],
            }

        cases = (
            ("11:00", datetime(2026, 9, 8, 15, 29, tzinfo=timezone.utc), True),
            ("11:00", datetime(2026, 9, 8, 15, 30, tzinfo=timezone.utc), True),
            ("11:00", datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc), False),
            # Simulated authoritative early-close target; the executor must
            # use the calendar's value instead of replacing it with 11:00.
            ("12:30", datetime(2026, 9, 8, 16, 45, tzinfo=timezone.utc), True),
        )
        for target, now, expected in cases:
            with self.subTest(target=target, now=now):
                Service.instances.clear()
                with patch("tradingagents.dataflows.config.set_config"), patch(
                    "tradingagents.safety.reset_safety_guard"
                ), patch("tradingagents.execution.ExecutionService", Service), patch(
                    "tradingagents.execution.auto_trade.execute_auto_trade",
                    side_effect=invoke_execution,
                ), patch(
                    "tradingagents.long_run.effective_target_for_session",
                    return_value={"effective_target": target},
                ) as effective_target:
                    result = ab._execute_paper_arm_inner(
                        "traders",
                        {"execution_db_path": "/tmp/fake-execution.sqlite3"},
                        {"trade_intent": {"symbol": "AAPL", "action": "BUY"}},
                        symbol="AAPL",
                        trade_date="2026-09-08",
                        pair_id="pair-test",
                        paper_notional_usd=1.0,
                        now_fn=lambda: now,
                    )
                self.assertEqual(Service.instances[0].recovery_allowed, expected)
                self.assertEqual(result["broker_calls"], int(expected))
                effective_target.assert_called_once()
                self.assertEqual(effective_target.call_args.args[1], "11:00")


if __name__ == "__main__":
    unittest.main()
