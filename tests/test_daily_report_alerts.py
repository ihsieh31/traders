"""Tests for the daily report generator and the alert dispatcher.

Alerts are stdlib-only (Telegram Bot API + generic webhook), deduplicated
with a cooldown so a tripped circuit breaker cannot flood the channel, and
fully failure-isolated: a dead webhook must never affect order flow. The
daily report is assembled purely from data the system already persists.
"""

import json
import os
import io
import tempfile
import unittest
from contextvars import Context
from contextlib import redirect_stdout
from pathlib import Path

from tradingagents.alerts import (
    AlertConfig,
    notify_safety_block,
    reset_alert_dedupe,
    send_alert,
)
from tradingagents.daily_report import generate_daily_report, write_daily_report
from tradingagents.safety.guardrails import SafetyGuard


class _FakeTransport:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, url, payload):
        if self.fail:
            raise ConnectionError("endpoint down")
        self.calls.append((url, payload))


def _alert_config(**overrides):
    kwargs = {
        "enabled": True,
        "telegram_bot_token": "TOKEN",
        "telegram_chat_id": "42",
        "webhook_url": "https://hooks.example/x",
        "cooldown_seconds": 900,
    }
    kwargs.update(overrides)
    return AlertConfig(**kwargs)


class AlertDispatchTests(unittest.TestCase):
    def setUp(self):
        reset_alert_dedupe()

    def test_sends_to_telegram_and_webhook(self):
        transport = _FakeTransport()
        result = send_alert(
            "Circuit breaker", "details", config=_alert_config(), transport=transport
        )
        self.assertTrue(result["sent"])
        urls = [u for u, _ in transport.calls]
        self.assertTrue(any("api.telegram.org/botTOKEN/sendMessage" in u for u in urls))
        self.assertIn("https://hooks.example/x", urls)
        # Telegram payload carries chat id and both subject and body.
        telegram_payload = next(p for u, p in transport.calls if "telegram" in u)
        self.assertEqual(telegram_payload["chat_id"], "42")
        self.assertIn("Circuit breaker", telegram_payload["text"])

    def test_duplicate_alert_is_suppressed_within_cooldown(self):
        transport = _FakeTransport()
        config = _alert_config()
        first = send_alert("halt", "body", config=config, transport=transport)
        second = send_alert("halt", "body", config=config, transport=transport)
        self.assertTrue(first["sent"])
        self.assertTrue(second["deduped"])
        self.assertEqual(len(transport.calls), 2)  # telegram + webhook, once

    def test_different_keys_are_not_deduped(self):
        transport = _FakeTransport()
        config = _alert_config()
        send_alert("halt", "body", key="a", config=config, transport=transport)
        send_alert("halt", "body", key="b", config=config, transport=transport)
        self.assertEqual(len(transport.calls), 4)

    def test_disabled_or_unconfigured_sends_nothing(self):
        transport = _FakeTransport()
        result = send_alert(
            "halt", "body", config=_alert_config(enabled=False), transport=transport
        )
        self.assertFalse(result["sent"])
        result = send_alert(
            "halt",
            "body",
            config=AlertConfig(enabled=True),  # no channels configured
            transport=transport,
        )
        self.assertFalse(result["sent"])
        self.assertEqual(transport.calls, [])

    def test_transport_failure_never_raises(self):
        result = send_alert(
            "halt",
            "body",
            config=_alert_config(),
            transport=_FakeTransport(fail=True),
        )
        self.assertFalse(result["sent"])

    def test_transport_failure_log_does_not_echo_credentials_or_urls(self):
        def leaking_transport(url, payload):
            raise ConnectionError(f"failed request to {url}")

        output = io.StringIO()
        with redirect_stdout(output):
            send_alert(
                "halt",
                "body",
                config=_alert_config(),
                transport=leaking_transport,
            )

        logged = output.getvalue()
        self.assertNotIn("TOKEN", logged)
        self.assertNotIn("hooks.example", logged)

    def test_notify_safety_block_dedupes_on_reasons(self):
        transport = _FakeTransport()
        config = _alert_config()
        notify_safety_block("AAPL", ["Kill switch is engaged"], config=config, transport=transport)
        notify_safety_block("AAPL", ["Kill switch is engaged"], config=config, transport=transport)
        self.assertEqual(len(transport.calls), 2)  # one alert, two channels


def _write_run(root, symbol, trade_date, final_signal, tokens=1000, status="completed"):
    runs_dir = Path(root) / symbol / "TradingAgentsStrategy_logs" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    started = f"{trade_date}T10:00:00+00:00"
    payload = {
        "symbol": symbol,
        "trade_date": trade_date,
        "started_at": started,
        "status": status,
        "events": [
            {
                "type": "llm_call",
                "payload": {
                    "model": "fake-deep",
                    "usage": {"input_tokens": tokens, "output_tokens": tokens // 10},
                },
            }
        ],
        "summary": {"final_signal": final_signal, "total_llm_tokens": tokens},
    }
    (runs_dir / f"{trade_date}_run.json").write_text(json.dumps(payload), encoding="utf-8")


class DailyReportTests(unittest.TestCase):
    def _report(self, root, tmp, day="2026-07-11"):
        guard = SafetyGuard(
            config={"safety_enabled": True},
            state_path=Path(tmp) / "state.json",
            kill_switch_path=Path(tmp) / "KILL_SWITCH",
        )
        config = {
            "results_dir": root,
            "memory_log_path": str(Path(tmp) / "trading_memory.md"),
            "llm_pricing_per_million": {"fake-deep": {"input": 10.0, "output": 30.0}},
        }
        return generate_daily_report(day=day, config=config, guard=guard)

    def test_report_contains_decisions_safety_cost_and_performance(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as tmp:
            _write_run(root, "AAPL", "2026-07-11", "BUY", tokens=100_000)
            _write_run(root, "NVDA", "2026-07-11", "HOLD", tokens=50_000)
            _write_run(root, "EXCL", "2026-07-01", "SELL")  # different day: excluded
            report = self._report(root, tmp)

        self.assertIn("Daily Trading Report", report)
        self.assertIn("AAPL", report)
        self.assertIn("BUY", report)
        self.assertNotIn("EXCL", report)
        for section in ("Decisions", "Safety", "LLM cost", "Realized performance"):
            self.assertIn(section, report)
        self.assertIn("$", report)  # a priced cost figure appears

    def test_kill_switch_state_is_reported(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as tmp:
            guard = SafetyGuard(
                config={"safety_enabled": True},
                state_path=Path(tmp) / "state.json",
                kill_switch_path=Path(tmp) / "KILL_SWITCH",
            )
            guard.engage_kill_switch("drill")
            report = generate_daily_report(
                day="2026-07-11", config={"results_dir": root}, guard=guard
            )
        self.assertIn("KILL SWITCH", report.upper())

    def test_write_daily_report_creates_md_and_html(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as tmp:
            _write_run(root, "AAPL", "2026-07-11", "BUY")
            guard = SafetyGuard(
                config={"safety_enabled": True},
                state_path=Path(tmp) / "state.json",
                kill_switch_path=Path(tmp) / "KILL_SWITCH",
            )
            md_path, html_path = write_daily_report(
                day="2026-07-11",
                output_dir=str(Path(tmp) / "reports"),
                config={"results_dir": root},
                guard=guard,
            )
            self.assertTrue(Path(md_path).exists())
            self.assertTrue(Path(html_path).exists())
            html = Path(html_path).read_text(encoding="utf-8")
            self.assertIn("AAPL", html)

    def test_empty_day_still_produces_a_report(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as tmp:
            report = self._report(root, tmp, day="2026-01-01")
        self.assertIn("No completed analyses", report)


class ConfigTests(unittest.TestCase):
    def test_default_config_exposes_alert_keys(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        self.assertIn("alerts_enabled", DEFAULT_CONFIG)
        self.assertIn("alert_webhook_url", DEFAULT_CONFIG)
        self.assertIn("alert_telegram_bot_token", DEFAULT_CONFIG)

    def test_alert_config_from_project_config(self):
        config = AlertConfig.from_config(
            {
                "alerts_enabled": True,
                "alert_telegram_bot_token": "T",
                "alert_telegram_chat_id": "C",
                "alert_webhook_url": "https://x",
                "alert_cooldown_seconds": 60,
            }
        )
        self.assertEqual(config.telegram_bot_token, "T")
        self.assertEqual(config.cooldown_seconds, 60)


class RunLoggerSecretTests(unittest.TestCase):
    def test_run_config_redacts_provider_and_alert_credentials(self):
        from tradingagents.run_logger import RunAuditLogger

        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                logger = RunAuditLogger()
                run_id = logger.start_run(
                    symbol="AAPL",
                    trade_date="2026-07-11",
                    config={
                        "minimax_api_key": "minimax-secret",
                        "alert_telegram_bot_token": "telegram-secret",
                        "alert_telegram_chat_id": "private-chat",
                        "alert_webhook_url": "https://example.test/private-hook",
                        "daily_llm_token_budget": 12345,
                    },
                )
                logger.finish_run(run_id=run_id)
                path = next(Path("eval_results").glob("**/runs/*.json"))
                stored = json.loads(path.read_text(encoding="utf-8"))["config"]
            finally:
                os.chdir(cwd)

        self.assertEqual(stored["minimax_api_key"], "[REDACTED]")
        self.assertEqual(stored["alert_telegram_bot_token"], "[REDACTED]")
        self.assertEqual(stored["alert_telegram_chat_id"], "[REDACTED]")
        self.assertEqual(stored["alert_webhook_url"], "[REDACTED]")
        self.assertEqual(stored["daily_llm_token_budget"], 12345)

    def test_concurrent_contexts_keep_same_symbol_events_on_their_own_run(self):
        from tradingagents.run_logger import RunAuditLogger

        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                logger = RunAuditLogger()
                first = Context()
                second = Context()
                run_a = first.run(logger.start_run, "AAPL", "2026-09-13")
                run_b = second.run(logger.start_run, "AAPL", "2026-09-13")
                first.run(logger.log_event, "node", "AAPL", None, {"owner": "a"})
                second.run(logger.log_event, "node", "AAPL", None, {"owner": "b"})

                self.assertEqual(logger._active_runs[run_a]["events"][-1]["payload"]["owner"], "a")
                self.assertEqual(logger._active_runs[run_b]["events"][-1]["payload"]["owner"], "b")
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
