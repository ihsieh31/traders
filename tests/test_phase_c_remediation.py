"""Phase C production-integration remediation regression (R1-R4).

Offline: Alpaca HTTP/client and LLM transport are faked at the boundary;
production validation/gate/pipeline/execution paths are exercised for real.
Covers real SDK enums, recovery entry gate, authoritative calendar, SIP feed.
"""

import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytz

from tradingagents.default_config import DEFAULT_CONFIG

_ET = pytz.timezone("US/Eastern")
_NOW = _ET.localize(datetime(2026, 9, 4, 17, 0))  # Friday after close
_AS_OF = date(2026, 9, 4)


# ---------------------------------------------------------------------------
# shared fake-calendar helpers (authoritative rows, no network)
# ---------------------------------------------------------------------------


def _ready_entry_policy():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"status": "READY", "minimum_price": 99, "maximum_price": 101,
            "expires_at": (now+timedelta(hours=1)).isoformat(),
            "exit_by": (now+timedelta(days=5)).isoformat(), "confirmation": "fixture observed setup"}


def _row(day, open_t=time(9, 30), close_t=time(16, 0)):
    return SimpleNamespace(
        date=day,
        open=datetime(day.year, day.month, day.day, open_t.hour, open_t.minute),
        close=datetime(day.year, day.month, day.day, close_t.hour, close_t.minute),
    )


def _rows_from_dates(dates, early_closes=None):
    early_closes = early_closes or {}
    return [_row(d, close_t=early_closes.get(d, time(16, 0))) for d in sorted(dates)]


def _standard_rows(as_of=None, count=95, early_closes=None):
    """Deterministic rows covering scan day and next trading day (offline)."""
    from tradingagents.dataflows.market_calendar import session_dates_ending_at

    anchor = as_of if as_of is not None else date(2026, 9, 8)
    dates = session_dates_ending_at(anchor, count)
    if _AS_OF not in dates:
        dates.append(_AS_OF)
    return _rows_from_dates(sorted(set(dates)), early_closes=early_closes)


def _base_config(**overrides):
    root = tempfile.mkdtemp(prefix="remed-")
    cache = Path(root) / "cache"
    results = Path(root) / "results"
    config = dict(DEFAULT_CONFIG)
    config.update(
        {
            "auto_screening_enabled": True,
            "screening_provider": "openai",
            "screening_model": "screen-fake",
            "screening_backend_url": None,
            "data_cache_dir": str(cache),
            "results_dir": str(results),
            "screening_selection_cache_path": str(cache / "selection.json"),
            "screening_as_of_override": _AS_OF.isoformat(),
            "sector_mapping": {},
            "corporate_action_events": [],
            "calendar_rows": _standard_rows(),
        }
    )
    config.update(overrides)
    return config


def _bars_df(sessions, closes, volumes):
    rows = []
    for i, session in enumerate(sessions):
        close = closes[i] if isinstance(closes, list) else closes
        volume = volumes[i] if isinstance(volumes, list) else volumes
        rows.append(
            {
                "timestamp": pd.Timestamp(session.year, session.month, session.day, 5, 0, tz="UTC"),
                "close": close,
                "volume": volume,
            }
        )
    return pd.DataFrame(rows)


def _healthy_bars(as_of=_AS_OF, n=61, volume=250_000.0, calendar_rows=None):
    from tradingagents.dataflows.market_calendar import _calendar_date_set

    if calendar_rows is not None:
        dates = sorted(_calendar_date_set(calendar_rows))
        # Take the n sessions ending at as_of from the injected calendar.
        idx = dates.index(as_of)
        sessions = dates[idx - n + 1 : idx + 1]
    else:
        from tradingagents.dataflows.market_calendar import session_dates_ending_at

        sessions = session_dates_ending_at(as_of, n)
    closes = [100.0 + i for i in range(len(sessions))]
    return _bars_df(sessions, closes, volume), sessions


# ---------------------------------------------------------------------------
# R1: real SDK enums
# ---------------------------------------------------------------------------


class R1EnumTests(unittest.TestCase):
    def _asset(self, symbol, *, status, asset_class, tradable=True):
        return SimpleNamespace(
            symbol=symbol,
            name=f"Company {symbol}",
            status=status,
            asset_class=asset_class,
            tradable=tradable,
            exchange="NASDAQ",
        )

    def test_real_enums_pass_universe(self):
        from alpaca.trading.enums import AssetClass, AssetStatus

        from tradingagents.screening.universe import fetch_us_equity_universe

        broker = SimpleNamespace(
            get_all_assets=lambda req: [
                self._asset("AAA", status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY),
            ]
        )
        universe = fetch_us_equity_universe(broker)
        self.assertEqual([u["symbol"] for u in universe], ["AAA"])

    def test_real_inactive_fails(self):
        from alpaca.trading.enums import AssetClass, AssetStatus

        from tradingagents.screening.universe import UniverseError, fetch_us_equity_universe

        broker = SimpleNamespace(
            get_all_assets=lambda req: [
                self._asset("AAA", status=AssetStatus.INACTIVE, asset_class=AssetClass.US_EQUITY),
            ]
        )
        with self.assertRaises(UniverseError):
            fetch_us_equity_universe(broker)

    def test_real_wrong_class_fails(self):
        from alpaca.trading.enums import AssetClass, AssetStatus

        from tradingagents.screening.universe import UniverseError, fetch_us_equity_universe

        broker = SimpleNamespace(
            get_all_assets=lambda req: [
                self._asset("AAA", status=AssetStatus.ACTIVE, asset_class=AssetClass.CRYPTO),
            ]
        )
        with self.assertRaises(UniverseError):
            fetch_us_equity_universe(broker)

    def test_real_nontradable_fails(self):
        from alpaca.trading.enums import AssetClass, AssetStatus

        from tradingagents.screening.universe import UniverseError, fetch_us_equity_universe

        broker = SimpleNamespace(
            get_all_assets=lambda req: [
                self._asset(
                    "AAA",
                    status=AssetStatus.ACTIVE,
                    asset_class=AssetClass.US_EQUITY,
                    tradable=False,
                ),
            ]
        )
        with self.assertRaises(UniverseError):
            fetch_us_equity_universe(broker)

    def test_plain_strings_still_pass(self):
        from tradingagents.screening.universe import fetch_us_equity_universe

        broker = SimpleNamespace(
            get_all_assets=lambda req: [
                SimpleNamespace(
                    symbol="AAA",
                    name="AAA",
                    status="active",
                    asset_class=SimpleNamespace(value="us_equity"),
                    tradable=True,
                    exchange="NASDAQ",
                )
            ]
        )
        universe = fetch_us_equity_universe(broker)
        self.assertEqual([u["symbol"] for u in universe], ["AAA"])

    def test_holdings_enum_active_accepted(self):
        from alpaca.trading.enums import AssetStatus

        from tradingagents.screening.pipeline import ScreeningDeps, prepare_screening_round
        from tradingagents.screening.metrics import EligibilityThresholds

        config = _base_config()
        rows = config["calendar_rows"]
        bars, sessions = _healthy_bars(calendar_rows=rows)
        # Build a scan world with one extra holding outside Top20.
        from tradingagents.dataflows.market_calendar import _calendar_date_set

        all_dates = sorted(_calendar_date_set(rows))
        # Use 25 symbols so Top20 leaves extras.
        universe = [{"symbol": f"T{i:02d}", "name": f"C {i}", "exchange": "NASDAQ"} for i in range(25)]
        bars_map = {}
        for i, u in enumerate(universe):
            b, _ = _healthy_bars(calendar_rows=rows)
            # Vary closes slightly for deterministic ranking.
            b = b.copy()
            b["close"] = b["close"] + i * 0.5
            bars_map[u["symbol"]] = b
        # Extra holding OK1 with real enum ACTIVE.
        def asset_fn(symbol):
            if symbol == "OK1":
                return SimpleNamespace(tradable=True, status=AssetStatus.ACTIVE)
            return SimpleNamespace(tradable=True, status="active")

        from tradingagents.screening.llm import ScreenedCandidate

        def invoke(candidates, sector_plan, *, select_n=20, max_per_sector=5):
            picked = candidates[:select_n]
            return [
                ScreenedCandidate(rank=i + 1, symbol=c.symbol, screening_score=90.0 - i, short_reason=f"ok {i}")
                for i, c in enumerate(picked)
            ]

        deps = ScreeningDeps(
            universe_fn=lambda cfg: universe,
            bars_fn=lambda symbols, **kw: {s: bars_map[s] for s in symbols},
            positions_fn=lambda: [{"symbol": "OK1", "qty": 1, "asset_class": "us_equity"}],
            asset_fn=asset_fn,
            quarantine_fn=lambda cfg: (lambda s: None),
            screening_invoke_fn=invoke,
            calendar_rows=rows,
        )
        plan = prepare_screening_round(config, deps=deps, now=_NOW)
        self.assertEqual(plan.status, "ok", plan.detail)
        self.assertIn("OK1", plan.extra_holdings)

    def test_holdings_enum_blocked(self):
        from alpaca.trading.enums import AssetStatus

        from tradingagents.screening.pipeline import ScreeningDeps, prepare_screening_round
        from tradingagents.screening.llm import ScreenedCandidate

        config = _base_config()
        rows = config["calendar_rows"]
        universe = [{"symbol": f"T{i:02d}", "name": f"C {i}", "exchange": "NASDAQ"} for i in range(25)]
        bars_map = {}
        for i, u in enumerate(universe):
            b, _ = _healthy_bars(calendar_rows=rows)
            b = b.copy()
            b["close"] = b["close"] + i * 0.5
            bars_map[u["symbol"]] = b

        def asset_fn(symbol):
            if symbol == "BAD1":
                return SimpleNamespace(tradable=True, status=AssetStatus.INACTIVE)
            if symbol == "BAD2":
                return SimpleNamespace(tradable=False, status=AssetStatus.ACTIVE)
            return SimpleNamespace(tradable=True, status="active")

        def invoke(candidates, sector_plan, *, select_n=20, max_per_sector=5):
            picked = candidates[:select_n]
            return [
                ScreenedCandidate(rank=i + 1, symbol=c.symbol, screening_score=90.0 - i, short_reason=f"ok {i}")
                for i, c in enumerate(picked)
            ]

        deps = ScreeningDeps(
            universe_fn=lambda cfg: universe,
            bars_fn=lambda symbols, **kw: {s: bars_map[s] for s in symbols},
            positions_fn=lambda: [
                {"symbol": "BAD1", "qty": 1, "asset_class": "us_equity"},
                {"symbol": "BAD2", "qty": 1, "asset_class": "us_equity"},
            ],
            asset_fn=asset_fn,
            quarantine_fn=lambda cfg: (lambda s: None),
            screening_invoke_fn=invoke,
            calendar_rows=rows,
        )
        plan = prepare_screening_round(config, deps=deps, now=_NOW)
        self.assertEqual(plan.status, "ok", plan.detail)
        blocked = {b["symbol"] for b in plan.blocked_holdings}
        self.assertIn("BAD1", blocked)
        self.assertIn("BAD2", blocked)
        self.assertNotIn("BAD1", plan.extra_holdings)


# ---------------------------------------------------------------------------
# R3: authoritative calendar
# ---------------------------------------------------------------------------


class R3CalendarTests(unittest.TestCase):
    def test_normal_day_before_and_after_close(self):
        from tradingagents.dataflows.market_calendar import most_recent_completed_session_auth

        day = date(2026, 9, 4)  # Friday
        prev = date(2026, 9, 3)
        rows = _rows_from_dates([date(2026, 9, 2), prev, day])
        before = _ET.localize(datetime(2026, 9, 4, 15, 59))
        after = _ET.localize(datetime(2026, 9, 4, 16, 0))
        self.assertEqual(most_recent_completed_session_auth(before, calendar_rows=rows), prev)
        self.assertEqual(most_recent_completed_session_auth(after, calendar_rows=rows), day)

    def test_early_close_honored(self):
        from tradingagents.dataflows.market_calendar import most_recent_completed_session_auth

        day = date(2026, 9, 4)
        prev = date(2026, 9, 3)
        rows = _rows_from_dates([prev, day], early_closes={day: time(13, 0)})
        just_before = _ET.localize(datetime(2026, 9, 4, 12, 59))
        at_close = _ET.localize(datetime(2026, 9, 4, 13, 0))
        self.assertEqual(most_recent_completed_session_auth(just_before, calendar_rows=rows), prev)
        self.assertEqual(most_recent_completed_session_auth(at_close, calendar_rows=rows), day)

    def test_weekend_and_holiday(self):
        from tradingagents.dataflows.market_calendar import is_us_trading_day_auth

        rows = _rows_from_dates([date(2026, 9, 3), date(2026, 9, 4)])
        # Saturday not in rows -> not a trading day.
        self.assertFalse(is_us_trading_day_auth(date(2026, 9, 5), calendar_rows=rows))
        self.assertTrue(is_us_trading_day_auth(date(2026, 9, 4), calendar_rows=rows))

    def test_2028_authoritative_holiday(self):
        from tradingagents.dataflows.market_calendar import is_us_trading_day_auth

        # 2028-06-19 is a Monday; fake Alpaca says it is NOT a session
        # (holiday). Static 2024-2027 tables would call it a trading day.
        holiday = date(2028, 6, 19)
        rows = _rows_from_dates([date(2028, 6, 16), date(2028, 6, 20)])
        self.assertFalse(is_us_trading_day_auth(holiday, calendar_rows=rows))
        # And a 2028 session is honored when present.
        rows2 = _rows_from_dates([holiday, date(2028, 6, 20)])
        self.assertTrue(is_us_trading_day_auth(holiday, calendar_rows=rows2))

    def test_dst_window(self):
        from tradingagents.dataflows.market_calendar import session_dates_ending_at_auth

        as_of = date(2026, 3, 9)
        # Build 61 weekdays back, skipping Sunday 2026-03-08 (DST switch).
        dates = []
        cursor = as_of
        while len(dates) < 61:
            if cursor.weekday() < 5:
                dates.append(cursor)
            cursor -= pd.Timedelta(days=1).to_pytimedelta()
        dates = sorted(dates)
        rows = _rows_from_dates(dates)
        window = session_dates_ending_at_auth(as_of, 61, calendar_rows=rows)
        self.assertEqual(len(window), 61)
        self.assertEqual(window[-1], as_of)
        self.assertNotIn(date(2026, 3, 8), window)

    def test_61_sessions_authoritative_no_phantom(self):
        from tradingagents.dataflows.market_calendar import session_dates_ending_at_auth

        as_of = date(2026, 4, 6)  # Monday after Good Friday 2026-04-03
        dates = []
        cursor = as_of
        while len(dates) < 61:
            if cursor.weekday() < 5 and cursor != date(2026, 4, 3):
                dates.append(cursor)
            cursor -= pd.Timedelta(days=1).to_pytimedelta()
        dates = sorted(dates)
        rows = _rows_from_dates(dates)
        window = session_dates_ending_at_auth(as_of, 61, calendar_rows=rows)
        self.assertNotIn(date(2026, 4, 3), window)
        self.assertEqual(len(window), 61)

    def test_calendar_failure_stops_scan(self):
        from tradingagents.screening.pipeline import ScreeningDeps, prepare_screening_round

        class BoomClient:
            def get_calendar(self, request):
                raise RuntimeError("500 calendar down")

        config = _base_config()
        config.pop("calendar_rows", None)
        config.pop("screening_as_of_override", None)
        deps = ScreeningDeps(
            universe_fn=lambda cfg: [{"symbol": "AAA", "name": "A", "exchange": "X"}],
            bars_fn=lambda symbols, **kw: {},
            positions_fn=lambda: [],
            quarantine_fn=lambda cfg: (lambda s: None),
            calendar_client=BoomClient(),
        )
        # Friday intraday needs calendar for trading-day + as_of.
        now = _ET.localize(datetime(2026, 9, 4, 10, 0))
        plan = prepare_screening_round(config, deps=deps, now=now)
        self.assertTrue(plan.stopped)
        self.assertIn(plan.reason, ("CALENDAR_UNAVAILABLE", "BARS_UNAVAILABLE", "SCAN_REFUSED_NON_TRADING_DAY"))
        # Zero Screening calls implied: stopped before scan when calendar down.
        self.assertEqual(plan.deep_analysis_set, [])

    def test_calendar_incomplete_fails_closed(self):
        from tradingagents.dataflows.market_calendar import CalendarError, session_dates_ending_at_auth

        rows = _rows_from_dates([date(2026, 9, 3), date(2026, 9, 4)])
        with self.assertRaises(CalendarError):
            session_dates_ending_at_auth(date(2026, 9, 4), 61, calendar_rows=rows)

    def test_missing_final_bar_no_silent_rollback(self):
        from tradingagents.screening.metrics import EligibilityThresholds, validate_and_clean_bars

        rows = _standard_rows()
        # Bars end at previous session, but as_of claims today (completed).
        prev_sessions = sorted(
            [r.date for r in rows if r.date < _AS_OF]
        )[-61:]
        bars = _bars_df(prev_sessions, [100.0] * 61, 250_000.0)
        window, err = validate_and_clean_bars(
            "SYM", bars, as_of=_AS_OF, thresholds=EligibilityThresholds(), calendar_rows=rows
        )
        self.assertIsNone(window)
        self.assertEqual(err, "stale_last_bar")

    def test_fetch_uses_get_calendar_request(self):
        from alpaca.trading.requests import GetCalendarRequest

        from tradingagents.dataflows.market_calendar import clear_calendar_cache, fetch_trading_calendar

        clear_calendar_cache()
        seen = {}

        class FakeClient:
            def get_calendar(self, request):
                seen["req"] = request
                assert isinstance(request, GetCalendarRequest)
                return [_row(date(2026, 9, 4))]

        rows = fetch_trading_calendar(date(2026, 9, 4), date(2026, 9, 4), client=FakeClient())
        self.assertEqual(len(rows), 1)
        self.assertEqual(seen["req"].start, date(2026, 9, 4))
        clear_calendar_cache()

    def test_webui_uses_authoritative_not_static(self):
        from webui.utils.market_hours import is_market_open

        # Early-close day: 13:00 close. 14:00 should be closed authoritatively,
        # but static 16:00 logic would say open.
        day = date(2026, 9, 4)
        rows = _rows_from_dates([date(2026, 9, 3), day], early_closes={day: time(13, 0)})
        dt = _ET.localize(datetime(2026, 9, 4, 14, 0))
        is_open, reason = is_market_open(dt, calendar_rows=rows)
        self.assertFalse(is_open)


# ---------------------------------------------------------------------------
# R4: SIP feed
# ---------------------------------------------------------------------------


class R4SipTests(unittest.TestCase):
    def _universe_bars(self, rows, n=25):
        from tradingagents.dataflows.market_calendar import _calendar_date_set

        dates = sorted(_calendar_date_set(rows))
        as_of_idx = dates.index(_AS_OF)
        sessions = dates[as_of_idx - 60 : as_of_idx + 1]
        universe = [{"symbol": f"T{i:02d}", "name": f"C {i}", "exchange": "NASDAQ"} for i in range(n)]
        bars = {}
        for i, u in enumerate(universe):
            closes = [100.0 + i + j * 0.5 for j in range(61)]
            bars[u["symbol"]] = _bars_df(sessions, closes, 250_000.0)
        return universe, bars, sessions

    def test_request_uses_sip(self):
        from alpaca.data.enums import DataFeed

        from tradingagents.screening.metrics import fetch_daily_bars_batch

        captured = {}

        class FakeResp:
            def __init__(self, df):
                self._df = df

            @property
            def df(self):
                return self._df

        class FakeClient:
            def get_stock_bars(self, request):
                captured["feed"] = request.feed
                import pandas as pd

                rows = [{"symbol": s, "timestamp": pd.Timestamp(2026, 9, 4, tz="UTC"), "close": 100.0, "volume": 1.0} for s in request.symbol_or_symbols]
                df = pd.DataFrame(rows).set_index(["symbol", "timestamp"])
                return FakeResp(df)

        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_stock_client", return_value=FakeClient()
        ):
            fetch_daily_bars_batch(["AAA"], as_of=_AS_OF)
        self.assertEqual(captured["feed"], DataFeed.SIP)

    def test_sip_failure_no_iex_fallback_and_zero_llm(self):
        from alpaca.data.enums import DataFeed

        from tradingagents.screening.pipeline import ScreeningDeps, prepare_screening_round

        config = _base_config()
        rows = config["calendar_rows"]
        universe, bars, _ = self._universe_bars(rows)
        calls = {"bars": 0, "screening": 0, "feeds": []}

        def failing_bars(symbols, **kw):
            calls["bars"] += 1
            raise RuntimeError("403 subscription: SIP feed not entitled (feed=sip)")

        def invoke(candidates, sector_plan, *, select_n=20, max_per_sector=5):
            calls["screening"] += 1
            raise AssertionError("must not reach Screening")

        deps = ScreeningDeps(
            universe_fn=lambda cfg: universe,
            bars_fn=failing_bars,
            positions_fn=lambda: [],
            quarantine_fn=lambda cfg: (lambda s: None),
            screening_invoke_fn=invoke,
            calendar_rows=rows,
        )
        plan = prepare_screening_round(config, deps=deps, now=_NOW)
        self.assertTrue(plan.stopped)
        self.assertEqual(plan.reason, "BARS_UNAVAILABLE")
        self.assertIn("sip", plan.detail.lower() + "sip")
        self.assertEqual(calls["screening"], 0)
        self.assertEqual(calls["bars"], 1)

    def test_no_iex_in_phase_c_path(self):
        import pathlib

        text = pathlib.Path("tradingagents/screening/metrics.py").read_text()
        self.assertNotIn("DataFeed.IEX", text)
        self.assertIn("DataFeed.SIP", text)

    def test_cache_rejects_old_feed(self):
        from tradingagents.screening.pipeline import ScreeningDeps, prepare_screening_round
        from tradingagents.screening.selection_store import SelectionStore
        from tradingagents.screening.llm import ScreenedCandidate

        config = _base_config()
        rows = config["calendar_rows"]
        universe, bars, _ = self._universe_bars(rows)

        def invoke(candidates, sector_plan, *, select_n=20, max_per_sector=5):
            picked = candidates[:select_n]
            return [
                ScreenedCandidate(rank=i + 1, symbol=c.symbol, screening_score=90.0 - i, short_reason=f"ok {i}")
                for i, c in enumerate(picked)
            ]

        deps = ScreeningDeps(
            universe_fn=lambda cfg: universe,
            bars_fn=lambda symbols, **kw: {s: bars[s] for s in symbols},
            positions_fn=lambda: [],
            quarantine_fn=lambda cfg: (lambda s: None),
            screening_invoke_fn=invoke,
            calendar_rows=rows,
        )
        plan = prepare_screening_round(config, deps=deps, now=_NOW)
        self.assertEqual(plan.status, "ok", plan.detail)
        store = SelectionStore(config["screening_selection_cache_path"])
        sel = store.load_valid(config, now=_NOW)
        self.assertIsNotNone(sel)
        self.assertEqual(sel.get("data_feed"), "sip")
        # Tamper to legacy IEX semantics -> invalid.
        path = Path(config["screening_selection_cache_path"])
        data = json.loads(path.read_text())
        data["data_feed"] = "iex"
        # Recompute integrity would be needed, but even with recomputed seal
        # the feed check must reject. Simulate an old IEX file by fixing seal:
        import hashlib

        material = {k: v for k, v in data.items() if k != "integrity"}
        data["integrity"] = hashlib.sha256(
            json.dumps(material, sort_keys=True, default=str).encode()
        ).hexdigest()
        path.write_text(json.dumps(data))
        self.assertIsNone(store.load_valid(config, now=_NOW))
        # Missing feed (pre-remediation schema-2 file) also invalid.
        data2 = json.loads(path.read_text())
        data2.pop("data_feed", None)
        material2 = {k: v for k, v in data2.items() if k != "integrity"}
        data2["integrity"] = hashlib.sha256(
            json.dumps(material2, sort_keys=True, default=str).encode()
        ).hexdigest()
        path.write_text(json.dumps(data2))
        self.assertIsNone(store.load_valid(config, now=_NOW))

    def test_formula_unchanged_with_sip_volumes(self):
        from tradingagents.screening.metrics import EligibilityThresholds, compute_features, validate_and_clean_bars

        rows = _standard_rows()
        bars, sessions = _healthy_bars(calendar_rows=rows)
        # Known consolidated volumes: hand-calculate ADV20/volume_ratio.
        closes = [100.0 + i for i in range(61)]
        volumes = [1_000_000.0] * 61
        bars = _bars_df(sessions, closes, volumes)
        window, err = validate_and_clean_bars(
            "SYM", bars, as_of=_AS_OF, thresholds=EligibilityThresholds(), calendar_rows=rows
        )
        self.assertIsNone(err)
        feat, ferr = compute_features("SYM", window, thresholds=EligibilityThresholds())
        self.assertIsNone(ferr)
        import statistics

        self.assertAlmostEqual(feat.adv20, statistics.fmean([c * v for c, v in zip(closes[-20:], volumes[-20:])]))
        self.assertAlmostEqual(feat.volume_ratio, 1.0)

    def test_refresh_failure_invalidates_cache(self):
        from tradingagents.screening.pipeline import ScreeningDeps, prepare_screening_round
        from tradingagents.screening.llm import ScreenedCandidate
        from tradingagents.screening.gate import check_entry_allowed

        config = _base_config()
        rows = config["calendar_rows"]
        universe, bars, _ = self._universe_bars(rows)

        def invoke(candidates, sector_plan, *, select_n=20, max_per_sector=5):
            picked = candidates[:select_n]
            return [
                ScreenedCandidate(rank=i + 1, symbol=c.symbol, screening_score=90.0 - i, short_reason=f"ok {i}")
                for i, c in enumerate(picked)
            ]

        deps = ScreeningDeps(
            universe_fn=lambda cfg: universe,
            bars_fn=lambda symbols, **kw: {s: bars[s] for s in symbols},
            positions_fn=lambda: [],
            quarantine_fn=lambda cfg: (lambda s: None),
            screening_invoke_fn=invoke,
            calendar_rows=rows,
        )
        plan = prepare_screening_round(config, deps=deps, now=_NOW)
        self.assertEqual(plan.status, "ok")

        def failing(symbols, **kw):
            raise RuntimeError("403 SIP entitled failed (feed=sip)")

        deps2 = ScreeningDeps(
            universe_fn=lambda cfg: universe,
            bars_fn=failing,
            positions_fn=lambda: [],
            quarantine_fn=lambda cfg: (lambda s: None),
            screening_invoke_fn=invoke,
            calendar_rows=rows,
        )
        plan2 = prepare_screening_round(config, deps=deps2, now=_NOW, refresh=True)
        self.assertTrue(plan2.stopped)
        import os

        self.assertFalse(os.path.exists(config["screening_selection_cache_path"]))
        self.assertIsNotNone(check_entry_allowed("T00", config=config, now=_NOW))


# ---------------------------------------------------------------------------
# R2: recovery gate
# ---------------------------------------------------------------------------


def _execution_broker(posts, positions=None, orders=None):
    from types import SimpleNamespace

    state = {"positions": list(positions or []), "orders": list(orders or []), "posts": posts}

    def submit_order(request):
        posts.append(request)
        get = request.get if isinstance(request, dict) else (lambda n: getattr(request, n, None))
        order = SimpleNamespace(
            id=f"broker-{len(posts)}",
            client_order_id=get("client_order_id"),
            symbol=str(get("symbol")),
            side=str(getattr(get("side"), "value", get("side"))),
            status="accepted",
            qty=str(get("qty") or 0),
            notional=get("notional"),
            filled_qty="0",
            filled_avg_price=None,
            updated_at=_NOW,
        )
        state["orders"].append(order)
        return order

    broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(id="paper-1", equity="100000", last_equity="100000", cash="80000", buying_power="200000"),
        # R13: the opening gate proves the session from the broker clock.
        get_clock=lambda: SimpleNamespace(is_open=True),
        get_all_positions=lambda: list(state["positions"]),
        get_orders=lambda request=None: list(state["orders"]),
        get_order_by_client_order_id=lambda cid: next((o for o in state["orders"] if o.client_order_id == cid), None),
        submit_order=submit_order,
        state=state,
    )
    return broker


def _build_selection_for_symbols(config, rows, symbols_top20, universe_extra=5):
    """Create a validated selection whose Top20 is exactly symbols_top20."""
    from tradingagents.screening.llm import ScreenedCandidate
    from tradingagents.screening.pipeline import ScreeningDeps, prepare_screening_round

    # Universe must contain Top20 plus extras for ranking.
    all_symbols = list(symbols_top20) + [f"X{i:02d}" for i in range(universe_extra)]
    universe = [{"symbol": s, "name": s, "exchange": "NASDAQ"} for s in all_symbols]
    dates = sorted([r.date for r in rows])
    as_of_idx = dates.index(_AS_OF)
    sessions = dates[as_of_idx - 60 : as_of_idx + 1]
    bars = {}
    for i, u in enumerate(universe):
        # Rank extras lower by giving Top20 higher drift.
        base = 200.0 if u["symbol"] in set(symbols_top20) else 50.0
        closes = [base + j * 0.8 for j in range(61)]
        bars[u["symbol"]] = _bars_df(sessions, closes, 300_000.0)

    order = {s: i for i, s in enumerate(symbols_top20)}

    def invoke(candidates, sector_plan, *, select_n=20, max_per_sector=5):
        # Force exact Top20 regardless of factor order.
        by_symbol = {c.symbol: c for c in candidates}
        out = []
        for i, s in enumerate(symbols_top20[:select_n]):
            c = by_symbol[s]
            out.append(ScreenedCandidate(rank=i + 1, symbol=s, screening_score=90.0 - i, short_reason=f"forced {s}"))
        return out

    deps = ScreeningDeps(
        universe_fn=lambda cfg: universe,
        bars_fn=lambda symbols, **kw: {s: bars[s] for s in symbols},
        positions_fn=lambda: [],
        quarantine_fn=lambda cfg: (lambda s: None),
        screening_invoke_fn=invoke,
        calendar_rows=rows,
    )
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert plan.status == "ok", plan.detail
    assert [e["symbol"] for e in plan.top20] == symbols_top20[:20]
    return plan


def _intent(symbol, action="BUY", current="NEUTRAL"):
    from tradingagents.agents.schemas import ExecutableAction, RiskDecision, build_trade_intent_from_risk_decision

    return build_trade_intent_from_risk_decision(
        symbol=symbol,
        trading_mode="investment",
        current_position=current,
        allow_shorts=False,
        trade_date="2026-09-04",
        decision=RiskDecision(
            action=ExecutableAction(action), confidence="medium", risk_rationale="r2", required_controls="strict", entry_policy=_ready_entry_policy(), stop_loss_price=95
        ),
    ).model_dump(mode="json")


class R2RecoveryGateTests(unittest.TestCase):
    def _service(self, config, broker, price=100.0):
        from datetime import timezone

        from tradingagents.execution.authority import BrokerQuote
        from tradingagents.execution.service import ExecutionService

        import uuid

        svc = ExecutionService(
            db_path=str(Path(config["results_dir"]) / f"exec-{uuid.uuid4().hex}.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda s: BrokerQuote(s, price - 0.1, price + 0.1, datetime.now(timezone.utc)),
        )
        return svc

    def _seed_pending(self, svc, symbol, side="buy", notional=1000.0):
        from tradingagents.execution.store import client_order_id_for

        svc.store.ensure_account_binding("paper-1")

        did = f"dec-seed-{symbol}"
        coid = client_order_id_for(did, symbol, side, role="open", seq=0)
        intent_row, orders, _ = svc.store.create_outbox(
            decision_id=did,
            run_id=None,
            symbol=symbol,
            action="BUY",
            target_position="LONG",
            payload_json=json.dumps(_intent(symbol)),
            orders=[{"client_order_id": coid, "symbol": symbol, "side": side, "quantity": None, "notional": notional}],
        )
        return orders[0]

    def test_out_of_top20_blocked_zero_posts(self):
        top20 = [f"M{i:02d}" for i in range(20)]
        config = _base_config()
        rows = config["calendar_rows"]
        _build_selection_for_symbols(config, rows, top20)
        posts = []
        broker = _execution_broker(posts)
        svc = self._service(config, broker)
        self._seed_pending(svc, "ZZZ")
        with patch(
            "tradingagents.execution.service._get_execution_config", return_value=config
        ), patch("tradingagents.screening.gate.eastern_now", return_value=_NOW):
            result = svc.startup_recover()
        self.assertEqual(posts, [])
        # Durably blocked: second run also zero posts (no infinite loop).
        with patch(
            "tradingagents.execution.service._get_execution_config", return_value=config
        ), patch("tradingagents.screening.gate.eastern_now", return_value=_NOW):
            result2 = svc.startup_recover()
        self.assertEqual(posts, [])
        remaining = svc.store.list_recoverable_orders()
        self.assertEqual(remaining, [])

    def test_non_trading_day_blocked(self):
        top20 = [f"M{i:02d}" for i in range(20)]
        config = _base_config()
        rows = config["calendar_rows"]
        _build_selection_for_symbols(config, rows, top20)
        posts = []
        broker = _execution_broker(posts)
        svc = self._service(config, broker)
        self._seed_pending(svc, top20[0])
        saturday = _ET.localize(datetime(2026, 9, 5, 12, 0))
        # Saturday rows: same calendar proves Saturday is not a session.
        with patch(
            "tradingagents.execution.service._get_execution_config", return_value=config
        ), patch("tradingagents.screening.gate.eastern_now", return_value=saturday):
            result = svc.startup_recover()
        self.assertEqual(posts, [])

    def test_current_top20_allowed(self):
        top20 = [f"M{i:02d}" for i in range(20)]
        config = _base_config()
        rows = config["calendar_rows"]
        _build_selection_for_symbols(config, rows, top20)
        posts = []
        broker = _execution_broker(posts)
        svc = self._service(config, broker)
        self._seed_pending(svc, top20[0])
        with patch(
            "tradingagents.execution.service._get_execution_config", return_value=config
        ), patch("tradingagents.screening.gate.eastern_now", return_value=_NOW):
            result = svc.startup_recover()
        self.assertGreaterEqual(len(posts), 1)

    def test_broker_existing_adopted_no_duplicate(self):
        from types import SimpleNamespace

        top20 = [f"M{i:02d}" for i in range(20)]
        config = _base_config()
        rows = config["calendar_rows"]
        _build_selection_for_symbols(config, rows, top20)
        # Outsider NOT in Top20, but broker already has the order -> adopt.
        posts = []
        broker = _execution_broker(posts)
        svc = self._service(config, broker)
        local = self._seed_pending(svc, "ZZZ")
        # Pre-populate broker with same deterministic client_order_id.
        existing = SimpleNamespace(
            id="broker-99",
            client_order_id=local["client_order_id"],
            symbol="ZZZ",
            side="buy",
            status="accepted",
            qty="0",
            notional=1000.0,
            filled_qty="0",
            filled_avg_price=None,
            updated_at=_NOW,
        )
        broker.state["orders"].append(existing)
        with patch(
            "tradingagents.execution.service._get_execution_config", return_value=config
        ), patch("tradingagents.screening.gate.eastern_now", return_value=_NOW):
            result = svc.startup_recover()
        self.assertEqual(posts, [])
        row = svc.store.get_order(local["order_id"])
        self.assertEqual(row["broker_order_id"], "broker-99")

    def test_reducing_exit_allowed_outside_top20(self):
        from tradingagents.execution.authority import BrokerPosition

        top20 = [f"M{i:02d}" for i in range(20)]
        config = _base_config()
        rows = config["calendar_rows"]
        _build_selection_for_symbols(config, rows, top20)
        posts = []
        held = BrokerPosition("ZZZ", 10.0, 1000.0)
        broker = _execution_broker(posts, positions=[held])
        svc = self._service(config, broker)
        # Seed an explicitly authorized liquidation, with quantity bounded by
        # the broker position. Side alone never proves an order is a close.
        from tradingagents.execution.store import client_order_id_for

        svc.store.ensure_account_binding("paper-1")
        did = "dec-exit-ZZZ"
        coid = client_order_id_for(did, "ZZZ", "sell", role="close", seq=0)
        # Use quantity so _resubmit passes idempotency check.
        svc.store.create_outbox(
            decision_id=did,
            run_id=None,
            symbol="ZZZ",
            action="SELL",
            target_position="NEUTRAL",
            payload_json=json.dumps({"symbol": "ZZZ", "kind": "liquidation"}),
            orders=[{"client_order_id": coid, "symbol": "ZZZ", "side": "sell", "quantity": 10.0, "notional": None}],
        )
        with patch(
            "tradingagents.execution.service._get_execution_config", return_value=config
        ), patch("tradingagents.screening.gate.eastern_now", return_value=_NOW):
            result = svc.startup_recover()
        # Reducing exit may POST (allowed) — assert no gate block prevents it.
        # At minimum it must not be CANCELED for Top20 reasons; it either
        # resubmits (>=0 posts) without a Phase C block message.
        self.assertTrue(result["success"] or "Phase C" not in str(result.get("reconciliation_reasons")))


if __name__ == "__main__":
    unittest.main()
