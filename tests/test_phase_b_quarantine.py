"""Phase B4 tests: corporate-action quarantine — persistence, fail-closed
execution gating, release semantics, and honest coverage limits."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.risk.corporate_actions import (
    QuarantineGate,
    QuarantineStore,
    utc_now,
)

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


class QuarantineStoreTests(unittest.TestCase):
    def _store(self, tmp, **kwargs):
        return QuarantineStore(Path(tmp) / "quarantine.json", **kwargs)

    def test_all_reasons_block_and_persist_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for reason in ("split", "ticker_change", "delisting", "non_tradable"):
                store.quarantine(symbol=f"S{reason[:3]}", reason=reason,
                                 source="unit-test")
            # A new instance (simulated restart) sees the same records.
            reopened = self._store(tmp)
            for reason in ("split", "ticker_change", "delisting", "non_tradable"):
                symbol = f"S{reason[:3]}"
                self.assertTrue(reopened.is_quarantined(symbol, now=_NOW), reason)
            active = reopened.all_active(now=_NOW)
            self.assertEqual(len(active), 4)

    def test_invalid_reason_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ValueError):
                store.quarantine(symbol="AAPL", reason="price_dropped_a_lot")

    def test_future_event_activates_only_at_effective_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            future = _NOW + timedelta(days=7)
            store.quarantine(symbol="TSLA", reason="split", effective_at=future)
            self.assertFalse(store.is_quarantined("TSLA", now=_NOW))
            self.assertTrue(store.is_quarantined("TSLA", now=_NOW + timedelta(days=8)))

    def test_unknown_effective_time_quarantines_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.quarantine(symbol="FOO", reason="delisting", effective_at=None)
            self.assertTrue(store.is_quarantined("FOO", now=_NOW))
            # An unparseable timestamp is equally fail-closed.
            store.quarantine(symbol="BAR", reason="delisting", effective_at="not-a-date")
            self.assertTrue(store.is_quarantined("BAR", now=_NOW))

    def test_release_requires_operator_and_clean_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.quarantine(symbol="AAPL", reason="split")
            with self.assertRaises(ValueError):
                store.release("AAPL", operator="")
            with self.assertRaises(ValueError):
                store.release("AAPL", operator="op", reconciliation_is_clean=False)
            store.release("AAPL", operator="op", reconciliation_is_clean=True)
            self.assertFalse(store.is_quarantined("AAPL", now=_NOW))

    def test_no_ttl_auto_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.quarantine(symbol="AAPL", reason="delisting")
            # A year later the record is still active: nothing ages out.
            self.assertTrue(store.is_quarantined("AAPL", now=_NOW + timedelta(days=365)))


class QuarantineGateTests(unittest.TestCase):
    def test_gate_reports_reason_and_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = QuarantineStore(Path(tmp) / "q.json")
            store.quarantine(symbol="AAPL", reason="ticker_change", source="op-note")
            gate = QuarantineGate(store)
            reason = gate.check("AAPL")
            self.assertIn("quarantined", reason)
            self.assertIn("ticker_change", reason)
            self.assertIn("op-note", reason)
            self.assertIsNone(gate.check("MSFT"))


class QuarantineConfigInputTests(unittest.TestCase):
    def test_configured_events_load_into_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "results_dir": tmp,
                "corporate_action_events": [
                    {"symbol": "TSLA", "reason": "split", "source": "operator",
                     "effective_at": "2026-01-01T00:00:00+00:00"},
                ],
            }
            from tradingagents.risk.corporate_actions import build_quarantine_gate

            gate = build_quarantine_gate(config)
            self.assertTrue(gate.check("TSLA"))
            self.assertIsNone(gate.check("AAPL"))

    def test_unbuildable_gate_returns_none_for_fail_closed_callers(self):
        from tradingagents.risk.corporate_actions import build_quarantine_gate

        self.assertIsNone(build_quarantine_gate(None))
        self.assertIsNone(build_quarantine_gate({}))


class QuarantineExecutionGateTests(unittest.TestCase):
    """The execution entry refuses new exposure for quarantined symbols and
    surfaces them on the operator status path."""

    def test_account_status_lists_active_quarantines(self):
        broker = SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity="100000", cash="80000", buying_power="160000"
            ),
            get_all_positions=lambda: [],
            get_orders=lambda request=None: [],
        )
        with tempfile.TemporaryDirectory() as tmp:
            from tradingagents.execution.service import ExecutionService

            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=lambda s: None,
            )
            store = QuarantineStore(Path(tmp) / "q.json")
            store.quarantine(symbol="NVDA", reason="split")
            svc._quarantine_gate = QuarantineGate(store)
            svc.startup_recover()
            status = svc.account_status()
            self.assertEqual(status["state"], "CLEAN")
            self.assertEqual(status["quarantined_symbols"], {"NVDA": "split"})

    def test_gate_sees_quarantine_and_release_from_other_instances(self):
        # Acceptance F3: a gate must judge from the persisted file, not a
        # stale in-memory copy, so releases and quarantines made through
        # another instance (or process) are honored immediately.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quarantine.json"
            gate = QuarantineGate(QuarantineStore(path))
            self.assertIsNone(gate.check("AAPL"))
            # Another store instance quarantines the symbol.
            other = QuarantineStore(path)
            other.quarantine(symbol="AAPL", reason="split", source="other-instance")
            self.assertIsNotNone(gate.check("AAPL"))
            # Another store instance releases it (operator+CLEAN implied here).
            other.release("AAPL", operator="op", reconciliation_is_clean=True)
            self.assertIsNone(gate.check("AAPL"))

    def test_account_status_surfaces_quarantine_before_first_reconcile(self):
        # Acceptance F4: even with no startup reconciliation on record the
        # operator path must list active quarantines.
        broker = SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity="100000", cash="80000", buying_power="160000"
            ),
            get_all_positions=lambda: [],
            get_orders=lambda request=None: [],
        )
        with tempfile.TemporaryDirectory() as tmp:
            from tradingagents.execution.service import ExecutionService

            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=lambda s: None,
            )
            store = QuarantineStore(Path(tmp) / "q.json")
            store.quarantine(symbol="TSLA", reason="ticker_change")
            svc._quarantine_gate = QuarantineGate(store)
            status = svc.account_status()
            self.assertEqual(status["state"], "PAUSED")
            self.assertEqual(
                status["reasons"], ["startup reconciliation has not run"]
            )
            self.assertEqual(status["quarantined_symbols"], {"TSLA": "ticker_change"})


if __name__ == "__main__":
    unittest.main()
