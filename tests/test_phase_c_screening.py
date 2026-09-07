"""Phase C tests: full-market screening, the third Screening role, the
deterministic Top40 formula, the strict Top20 contract, the daily selection
cache, the Top20 ∪ holdings round plan, and the fail-closed entry gate.

Everything runs offline: universes/bars/positions/assets are fakes, the
Screening LLM is a fake behind the real bounded-retry owner, and execution
uses the mock broker + temporary SQLite from the Phase B tests.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytz

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dataflows.market_calendar import (
    is_us_trading_day,
    session_dates_ending_at,
)
from tradingagents.llm_clients.retry import ProviderFailure, RetryingLLM
from tradingagents.screening.gate import check_entry_allowed
from tradingagents.screening.llm import (
    ScreeningConfigError,
    ScreeningOutput,
    ScreeningStop,
    ScreenedCandidate,
    invoke_screening_structured,
    resolve_screening_config,
)
from tradingagents.screening.metrics import (
    EligibilityThresholds,
    ascending_percentiles,
    compute_features,
    scan_universe,
    select_top_k,
    validate_and_clean_bars,
)
from tradingagents.screening.pipeline import (
    RoundPlan,
    ScreeningDeps,
    prepare_screening_round,
)
from tradingagents.screening.selection_store import SelectionStore
from tradingagents.screening.sessions import (
    current_trading_date,
    most_recent_completed_session,
)
from tradingagents.screening.universe import UniverseError, fetch_us_equity_universe

_ET = pytz.timezone("US/Eastern")
_NOW = _ET.localize(datetime(2026, 9, 4, 17, 0))  # Friday after the close (EDT)
_AS_OF = date(2026, 9, 4)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _ready_entry_policy():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"status": "READY", "minimum_price": 99, "maximum_price": 101,
            "expires_at": (now+timedelta(hours=1)).isoformat(),
            "exit_by": (now+timedelta(days=5)).isoformat(), "confirmation": "fixture observed setup"}


def _tmp_dirs():
    root = tempfile.mkdtemp(prefix="phase-c-")
    return root, Path(root) / "cache", Path(root) / "results"


def _std_rows(as_of=None, count=95):
    """Deterministic authoritative rows matching the legacy window (offline).

    Covers both the scan ``_AS_OF`` (2026-09-04) and the next trading day
    (2026-09-08, after the Labor Day holiday) so next-day cache invalidation
    can be proven without network.
    """
    from datetime import datetime as _dt

    anchor = as_of if as_of is not None else date(2026, 9, 8)
    dates = session_dates_ending_at(anchor, count)
    return [
        SimpleNamespace(
            date=d,
            open=_dt(d.year, d.month, d.day, 9, 30),
            close=_dt(d.year, d.month, d.day, 16, 0),
        )
        for d in dates
    ]


def _base_config(**overrides):
    root, cache, results = _tmp_dirs()
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
            # Offline authoritative calendar (no network in tests).
            "calendar_rows": _std_rows(),
        }
    )
    config.update(overrides)
    return config


def _bars_df(sessions, closes, volumes):
    """Daily-bar frame with Alpaca-style UTC date-anchored timestamps."""
    rows = []
    for i, session in enumerate(sessions):
        close = closes[i] if isinstance(closes, list) else closes
        volume = volumes[i] if isinstance(volumes, list) else volumes
        rows.append(
            {
                "timestamp": pd.Timestamp(
                    session.year, session.month, session.day, 5, 0, tz="UTC"
                ),
                "close": close,
                "volume": volume,
            }
        )
    return pd.DataFrame(rows)


def _session_closes(sessions, start=100.0, step=1.0):
    return [start + step * i for i in range(len(sessions))]


def _healthy_bars(as_of=_AS_OF, n=61, start=100.0, step=1.0, volume=250_000.0):
    sessions = session_dates_ending_at(as_of, n)
    return _bars_df(sessions, _session_closes(sessions, start, step), volume)


def _universe(symbols):
    return [
        {"symbol": s, "name": f"Company {s}", "exchange": "NASDAQ"} for s in symbols
    ]


def _candidates(n, *, prefix="T", base=100.0):
    """n eligible SymbolFeatures rows with distinct, monotone factors."""
    sessions = session_dates_ending_at(_AS_OF, 61)
    rows = _std_rows()
    features = []
    for i in range(n):
        bars = _bars_df(
            sessions,
            _session_closes(sessions, start=base + i * 10, step=1.0 + i * 0.1),
            250_000.0,
        )
        window, err = validate_and_clean_bars(
            f"{prefix}{i:02d}", bars, as_of=_AS_OF, thresholds=EligibilityThresholds(),
            calendar_rows=rows,
        )
        assert err is None, err
        feature, ferr = compute_features(
            f"{prefix}{i:02d}", window, thresholds=EligibilityThresholds()
        )
        assert ferr is None, ferr
        features.append(feature)
    return features


def _fake_llm_invoke(candidates_out, *, counter=None, select_n=20):
    def invoke(candidates, sector_plan, *, select_n=select_n, max_per_sector=5):
        if counter is not None:
            counter["screening"] += 1
        picked = candidates[:select_n]
        return [
            ScreenedCandidate(
                rank=i + 1,
                symbol=c.symbol,
                screening_score=90.0 - i,
                short_reason=f"r60 {c.r60:+.2f}, adv20 {c.adv20:,.0f}",
            )
            for i, c in enumerate(picked)
        ]

    return invoke


def _deps(universe, bars, *, positions=None, assets=None, invoke=None,
          quarantine=None, calendar_rows=None):
    class _Asset:
        def __init__(self, tradable=True, status="active"):
            self.tradable = tradable
            self.status = status

    if positions is None:
        positions = []
    # assets: dict symbol -> tradable flag; None = every queried asset is
    # tradable/active (the common fixture case).
    if assets is None:
        asset_map = None
    else:
        asset_map = {symbol: _Asset(tradable=flag) for symbol, flag in assets.items()}

    def asset_fn(symbol):
        if asset_map is None:
            return _Asset()
        if symbol in asset_map:
            return asset_map[symbol]
        raise RuntimeError(f"no asset fixture for {symbol}")

    return ScreeningDeps(
        universe_fn=lambda cfg: universe,
        bars_fn=lambda symbols, **kw: {s: bars[s] for s in symbols},
        positions_fn=lambda: positions,
        asset_fn=asset_fn,
        quarantine_fn=lambda cfg: quarantine if callable(quarantine) else (lambda sym: None),
        screening_invoke_fn=invoke or _fake_llm_invoke(None),
        calendar_rows=calendar_rows if calendar_rows is not None else _std_rows(),
    )


# ---------------------------------------------------------------------------
# C05: sessions, DST, holidays
# ---------------------------------------------------------------------------


class SessionCalendarTests(unittest.TestCase):
    def test_as_of_intraday_is_previous_session(self):
        intraday = _ET.localize(datetime(2026, 9, 4, 15, 59))
        self.assertEqual(most_recent_completed_session(intraday), date(2026, 9, 3))
        after_close = _ET.localize(datetime(2026, 9, 4, 16, 0))
        self.assertEqual(most_recent_completed_session(after_close), date(2026, 9, 4))

    def test_weekend_falls_back_to_friday(self):
        saturday = _ET.localize(datetime(2026, 9, 5, 12, 0))
        self.assertEqual(most_recent_completed_session(saturday), date(2026, 9, 4))
        self.assertEqual(current_trading_date(saturday), date(2026, 9, 4))

    def test_holiday_is_not_a_session(self):
        self.assertFalse(is_us_trading_day(date(2026, 4, 3)))  # Good Friday 2026

    def test_61_session_window_spans_dst_change_and_skips_holidays(self):
        as_of = date(2026, 3, 9)  # first session after the 2026-03-08 DST switch
        sessions = session_dates_ending_at(as_of, 61)
        self.assertEqual(len(sessions), 61)
        self.assertEqual(sessions[-1], as_of)
        self.assertNotIn(date(2026, 3, 8), sessions)  # Sunday
        for session in sessions:
            self.assertLessEqual(session, as_of)
        # window strictly increasing and gap-free per the calendar
        from tradingagents.dataflows.market_calendar import previous_trading_day

        for earlier, later in zip(sessions, sessions[1:]):
            self.assertEqual(previous_trading_day(later), earlier)
        # bars built on these session dates validate across the DST boundary
        bars = _bars_df(sessions, _session_closes(sessions), 250_000.0)
        window, err = validate_and_clean_bars(
            "DST", bars, as_of=as_of, thresholds=EligibilityThresholds(),
            calendar_rows=_std_rows(as_of, 90),
        )
        self.assertIsNone(err)
        self.assertEqual(len(window), 61)

    def test_good_friday_excluded_from_window(self):
        as_of = date(2026, 4, 6)  # Monday after Good Friday 2026-04-03
        sessions = session_dates_ending_at(as_of, 61)
        self.assertNotIn(date(2026, 4, 3), sessions)


# ---------------------------------------------------------------------------
# C01/C02: Screening role configuration
# ---------------------------------------------------------------------------


class ScreeningRoleConfigTests(unittest.TestCase):
    def test_disabled_resolves_to_no_client(self):
        resolved = resolve_screening_config(
            {"auto_screening_enabled": False, "screening_provider": "openai"}
        )
        self.assertFalse(resolved["enabled"])

    def test_enabled_requires_provider_and_model_without_fallback(self):
        for missing in (
            {"screening_model": "m"},
            {"screening_provider": "openai"},
            {"screening_provider": "", "screening_model": "m"},
        ):
            config = {"auto_screening_enabled": True, **missing}
            with self.assertRaises(ScreeningConfigError):
                resolve_screening_config(config)

    def test_unsupported_provider_fails_closed(self):
        with self.assertRaises(ScreeningConfigError):
            resolve_screening_config(
                {
                    "auto_screening_enabled": True,
                    "screening_provider": "not-a-vendor",
                    "screening_model": "m",
                }
            )

    def test_three_roles_resolve_independently(self):
        config = {
            "llm_provider": "openai",
            "analysis_provider": "openai",
            "analysis_model": "analysis-model",
            "analysis_backend_url": "https://analysis.example/v1",
            "decision_provider": "anthropic",
            "decision_model": "decision-model",
            "decision_backend_url": "https://decision.example/v1",
            "auto_screening_enabled": True,
            "screening_provider": "google",
            "screening_model": "screening-model",
            "screening_backend_url": None,
        }
        roles = resolve_screening_config(config)
        self.assertTrue(roles["enabled"])
        spec = roles["spec"]
        self.assertEqual((spec.provider, spec.model), ("google", "screening-model"))
        # the Screening role never inherits another role's endpoint
        self.assertIsNone(spec.backend_url)
        self.assertNotEqual(spec.model, config["analysis_model"])
        self.assertNotEqual(spec.model, config["decision_model"])

    def test_role_specific_api_key_env_wins(self):
        with patch.dict(os.environ, {"SCREENING_GOOGLE_API_KEY": "screen-key"}):
            resolved = resolve_screening_config(
                {
                    "auto_screening_enabled": True,
                    "screening_provider": "google",
                    "screening_model": "m",
                }
            )
        self.assertEqual(resolved["api_key"], "screen-key")

    def test_missing_api_key_fails_at_client_build(self):
        from tradingagents.screening.llm import build_screening_llm

        with patch("tradingagents.llm_clients.roles.get_llm_api_key", return_value=""):
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                resolved = resolve_screening_config(
                    {
                        "auto_screening_enabled": True,
                        "screening_provider": "openai",
                        "screening_model": "m",
                    }
                )
                with self.assertRaises(ScreeningConfigError):
                    build_screening_llm(resolved, DEFAULT_CONFIG)

    def test_pipeline_refuses_to_run_when_disabled(self):
        config = _base_config(auto_screening_enabled=False)
        with self.assertRaises(ScreeningConfigError):
            prepare_screening_round(config, deps=_deps([], {}), now=_NOW)


# ---------------------------------------------------------------------------
# C03: universe
# ---------------------------------------------------------------------------


class UniverseTests(unittest.TestCase):
    def _asset(self, symbol, *, status="active", asset_class="us_equity", tradable=True):
        return SimpleNamespace(
            symbol=symbol,
            name=f"Company {symbol}",
            status=status,
            asset_class=SimpleNamespace(value=asset_class),
            tradable=tradable,
            exchange="NASDAQ",
        )

    def test_full_paged_universe_with_filters(self):
        assets = [
            self._asset("AAA"),
            self._asset("BBB", tradable=False),      # non-tradable dropped
            self._asset("CCC", status="inactive"),   # inactive dropped
            self._asset("XXX", asset_class="crypto"),  # wrong class dropped
            self._asset("AAA"),                      # duplicate dropped
        ]
        broker = SimpleNamespace(get_all_assets=lambda request: iter(assets))
        universe = fetch_us_equity_universe(broker)
        self.assertEqual([u["symbol"] for u in universe], ["AAA"])

    def test_paged_iterator_is_consumed_fully(self):
        class Paged:
            def __init__(self):
                self._pages = [
                    [self._asset("AAA"), self._asset("BBB")],
                    [self._asset("CCC")],
                ]

            def _asset(self, symbol):
                return SimpleNamespace(
                    symbol=symbol, name=symbol, status="active",
                    asset_class=SimpleNamespace(value="us_equity"),
                    tradable=True, exchange="NASDAQ",
                )

            def __iter__(self):
                for page in self._pages:
                    yield from page

        broker = SimpleNamespace(get_all_assets=lambda request: Paged())
        universe = fetch_us_equity_universe(broker)
        self.assertEqual([u["symbol"] for u in universe], ["AAA", "BBB", "CCC"])

    def test_failure_raises_no_static_fallback(self):
        def boom(request):
            raise RuntimeError("alpaca down")

        broker = SimpleNamespace(get_all_assets=boom)
        with self.assertRaises(UniverseError):
            fetch_us_equity_universe(broker)


# ---------------------------------------------------------------------------
# C04/C05: eligibility thresholds and bar data quality
# ---------------------------------------------------------------------------


class EligibilityTests(unittest.TestCase):
    def _window(self, bars, **kw):
        return validate_and_clean_bars(
            "SYM", bars, as_of=_AS_OF, thresholds=EligibilityThresholds(**kw),
            calendar_rows=_std_rows(),
        )

    def test_exactly_61_bars_qualify_60_do_not(self):
        _, err = self._window(_healthy_bars(n=61))
        self.assertIsNone(err)
        _, err = self._window(_healthy_bars(n=60))
        self.assertEqual(err, "insufficient_bars")

    def test_price_threshold_is_inclusive_at_five_dollars(self):
        sessions = session_dates_ending_at(_AS_OF, 61)
        at_threshold = _bars_df(sessions, [5.0] * 61, 400_000.0)
        _, err = self._window(at_threshold)
        self.assertIsNone(err)
        below = _bars_df(sessions, [4.99] * 61, 400_000.0)
        window, err = self._window(below)
        self.assertIsNone(err)  # bars themselves are clean
        feature, ferr = compute_features(
            "SYM", window, thresholds=EligibilityThresholds()
        )
        self.assertIsNone(feature)
        self.assertEqual(ferr, "below_min_price")

    def test_adv20_threshold_is_inclusive_at_twenty_million(self):
        sessions = session_dates_ending_at(_AS_OF, 61)
        exactly = _bars_df(sessions, [100.0] * 61, 200_000.0)  # 100*200000 = 20M
        _, err = self._window(exactly)
        self.assertIsNone(err)
        under = _bars_df(sessions, [100.0] * 61, 199_999.99)
        feature, ferr = compute_features(
            "SYM",
            self._window(under)[0],
            thresholds=EligibilityThresholds(),
        )
        self.assertIsNone(feature)
        self.assertEqual(ferr, "below_min_adv20")

    def test_unclosed_same_day_bar_is_dropped(self):
        sessions = session_dates_ending_at(_AS_OF, 61)
        rows = []
        for session in sessions:
            rows.append(
                {
                    "timestamp": pd.Timestamp(session.year, session.month, session.day, 5, 0, tz="UTC"),
                    "close": 100.0,
                    "volume": 250_000.0,
                }
            )
        # today's partially formed daily bar (session incomplete)
        rows.append(
            {
                "timestamp": pd.Timestamp(2026, 9, 5, 5, 0, tz="UTC"),
                "close": 101.0,
                "volume": 10.0,
            }
        )
        window, err = validate_and_clean_bars(
            "SYM", pd.DataFrame(rows), as_of=_AS_OF, thresholds=EligibilityThresholds(),
            calendar_rows=_std_rows(),
        )
        self.assertIsNone(err)
        self.assertEqual(len(window), 61)
        self.assertNotIn(
            pd.Timestamp(2026, 9, 5, 5, 0, tz="UTC"), list(window["timestamp"])
        )

    def test_stale_last_bar_excluded(self):
        sessions = session_dates_ending_at(_AS_OF, 62)[:-1]  # end at as_of-1
        bars = _bars_df(sessions, _session_closes(sessions), 250_000.0)
        _, err = self._window(bars)
        self.assertEqual(err, "stale_last_bar")

    def test_duplicate_session_excluded(self):
        sessions = session_dates_ending_at(_AS_OF, 61)
        bars = _bars_df(sessions, _session_closes(sessions), 250_000.0)
        dup = pd.concat([bars, bars.tail(1)], ignore_index=True)
        _, err = self._window(dup)
        self.assertEqual(err, "duplicate_session")

    def test_missing_session_excluded_not_forward_filled(self):
        sessions = session_dates_ending_at(_AS_OF, 62)
        # drop one in-window session but keep the row count >=61 via an
        # extra earlier session, so tail(61) is a shifted (wrong) window
        broken = [s for s in sessions if s != sessions[10]]
        broken.insert(0, sessions[0] - pd.Timedelta(days=1).to_pytimedelta())
        broken_dates = [
            d.date() if hasattr(d, "date") else d for d in broken
        ]
        from tradingagents.dataflows.market_calendar import previous_trading_day

        broken_dates[0] = previous_trading_day(broken_dates[1])
        bars = _bars_df(broken_dates, _session_closes(broken_dates), 250_000.0)
        _, err = self._window(bars)
        self.assertIn(err, ("missing_session", "stale_last_bar"))

    def test_nan_inf_and_negative_values_excluded(self):
        sessions = session_dates_ending_at(_AS_OF, 61)
        closes = _session_closes(sessions)
        bad = list(closes)
        bad[30] = float("nan")
        _, err = self._window(_bars_df(sessions, bad, 250_000.0))
        self.assertEqual(err, "bad_values")

        bad = list(closes)
        bad[30] = float("inf")
        _, err = self._window(_bars_df(sessions, bad, 250_000.0))
        self.assertEqual(err, "bad_values")

        _, err = self._window(_bars_df(sessions, closes, -1.0))
        self.assertEqual(err, "bad_values")

        bad = list(closes)
        bad[30] = 0.0
        _, err = self._window(_bars_df(sessions, bad, 250_000.0))
        self.assertEqual(err, "bad_values")

    def test_zero_volume_baseline_excluded(self):
        sessions = session_dates_ending_at(_AS_OF, 61)
        bars = _bars_df(sessions, _session_closes(sessions), 0.0)
        window, err = self._window(bars)
        self.assertIsNone(err)  # bars valid; eligibility fails
        feature, ferr = compute_features(
            "SYM", window, thresholds=EligibilityThresholds()
        )
        self.assertIsNone(feature)
        self.assertEqual(ferr, "below_min_adv20")


# ---------------------------------------------------------------------------
# C06/C07: deterministic formula, ties, shuffle, Top40
# ---------------------------------------------------------------------------


class RankingFormulaTests(unittest.TestCase):
    def test_percentile_edges_and_average_rank_ties(self):
        self.assertEqual(ascending_percentiles([7.0]), [0.5])
        self.assertEqual(ascending_percentiles([3.0, 1.0, 2.0]), [1.0, 0.0, 0.5])
        self.assertEqual(
            ascending_percentiles([1.0, 2.0, 2.0, 3.0]), [0.0, 0.5, 0.5, 1.0]
        )

    def test_factors_match_independent_recomputation(self):
        import math
        import statistics

        sessions = session_dates_ending_at(_AS_OF, 61)
        closes = [100.0 + i for i in range(61)]
        volumes = [1_000_000.0] * 61
        window, err = validate_and_clean_bars(
            "SYM", _bars_df(sessions, closes, volumes),
            as_of=_AS_OF, thresholds=EligibilityThresholds(),
            calendar_rows=_std_rows(),
        )
        self.assertIsNone(err)
        feature, ferr = compute_features(
            "SYM", window, thresholds=EligibilityThresholds()
        )
        self.assertIsNone(ferr)
        t = 60
        self.assertEqual(feature.price, 160.0)
        self.assertAlmostEqual(feature.r5, closes[t] / closes[t - 5] - 1)
        self.assertAlmostEqual(feature.r20, closes[t] / closes[t - 20] - 1)
        self.assertAlmostEqual(feature.r60, closes[t] / closes[t - 60] - 1)
        self.assertAlmostEqual(feature.adv20, statistics.fmean(
            c * v for c, v in zip(closes[-20:], volumes[-20:])
        ))
        returns = [closes[i] / closes[i - 1] - 1 for i in range(t - 19, t + 1)]
        expected_vol = statistics.stdev(returns) * math.sqrt(252)
        self.assertAlmostEqual(feature.vol20, expected_vol)
        self.assertAlmostEqual(feature.volume_ratio, 1.0)
        self.assertAlmostEqual(
            feature.trend, closes[t] / statistics.fmean(closes[-20:]) - 1
        )

    def test_dominant_symbol_scores_exact_100_and_other_0(self):
        # A: smooth uptrend, steady volume. B: flat choppy price, shrinking
        # volume. On a two-name universe every percentile is 0 or 1, so the
        # weighted score is exactly 100 / 0.
        sessions = session_dates_ending_at(_AS_OF, 61)
        up = [100.0 + i for i in range(61)]
        choppy = [100.0 + (2.0 if i % 2 else -2.0) for i in range(61)]
        bars = {
            "AAA": _bars_df(sessions, up, [1_000_000.0] * 61),
            "BBB": _bars_df(sessions, choppy, [400_000.0 - i * 1000 for i in range(61)]),
        }
        universe = _universe(["AAA", "BBB"])
        scored, stats = scan_universe(
            universe, bars, as_of=_AS_OF,
            thresholds=EligibilityThresholds(), quarantine_checker=lambda s: None,
            calendar_rows=_std_rows(),
        )
        self.assertEqual(stats.eligible, 2)
        self.assertAlmostEqual(scored[0].score, 100.0)
        self.assertAlmostEqual(scored[1].score, 0.0)
        self.assertEqual(scored[0].symbol, "AAA")

    def test_full_tie_breaks_by_symbol_and_shuffle_is_inert(self):
        sessions = session_dates_ending_at(_AS_OF, 61)
        identical = _bars_df(sessions, [50.0] * 61, 500_000.0)
        bars = {s: identical for s in ("MMM", "AAA", "ZZZ")}
        universe = _universe(["MMM", "AAA", "ZZZ"])
        scored1, _ = scan_universe(
            universe, bars, as_of=_AS_OF,
            thresholds=EligibilityThresholds(), quarantine_checker=lambda s: None,
            calendar_rows=_std_rows(),
        )
        self.assertEqual([f.symbol for f in scored1], ["AAA", "MMM", "ZZZ"])
        self.assertEqual(len({f.score for f in scored1}), 1)

        shuffled = _universe(["ZZZ", "AAA", "MMM"])
        scored2, _ = scan_universe(
            shuffled, bars, as_of=_AS_OF,
            thresholds=EligibilityThresholds(), quarantine_checker=lambda s: None,
            calendar_rows=_std_rows(),
        )
        self.assertEqual(
            [(f.symbol, round(f.score, 12)) for f in scored1],
            [(f.symbol, round(f.score, 12)) for f in scored2],
        )

    def test_single_eligible_symbol_gets_neutral_score_50(self):
        bars = {"ONE": _healthy_bars()}
        scored, _ = scan_universe(
            _universe(["ONE"]), bars, as_of=_AS_OF,
            thresholds=EligibilityThresholds(), quarantine_checker=lambda s: None,
            calendar_rows=_std_rows(),
        )
        self.assertEqual(len(scored), 1)
        self.assertAlmostEqual(scored[0].score, 50.0)

    def test_top_k_selection(self):
        features = _candidates(45)
        scored = select_top_k(sorted(features, key=lambda f: f.symbol), 40)
        self.assertEqual(len(scored), 40)


# ---------------------------------------------------------------------------
# C08/C09/C11: screening invocation contract
# ---------------------------------------------------------------------------


class _FakeInner:
    """Fake provider inner LLM counting real requests."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def with_structured_output(self, schema):
        inner = self

        class _Structured:
            def invoke(self, prompt, **kwargs):
                inner.calls += 1
                outcome = inner.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return _Structured()


def _retrying_llm(outcomes, max_retries=3):
    llm = RetryingLLM(
        _FakeInner(outcomes), role="screening", provider="openai",
        model="screen-fake", max_retries=max_retries,
    )
    llm._controller.sleep = lambda seconds: None
    return llm


def _valid_output(n=20, symbols=None):
    symbols = symbols or [f"T{i:02d}" for i in range(n)]
    return ScreeningOutput(
        candidates=[
            ScreenedCandidate(
                rank=i + 1, symbol=s, screening_score=80.0 - i,
                short_reason=f"r60 +{i}% with adv20 25m",
            )
            for i, s in enumerate(symbols)
        ]
    )


class ScreeningInvocationTests(unittest.TestCase):
    def _invoke(self, llm, n_input=40):
        input_symbols = {f"T{i:02d}" for i in range(n_input)}
        return invoke_screening_structured(
            llm, [{"role": "user", "content": "table"}],
            expected_count=20, input_symbols=input_symbols,
        )

    def test_transient_then_success_is_four_requests(self):
        outcomes = [
            TimeoutError("request timed out"),
            TimeoutError("connection reset"),
            TimeoutError("service unavailable"),
            _valid_output(),
        ]
        llm = _retrying_llm(outcomes)
        result = self._invoke(llm)
        self.assertEqual(len(result), 20)
        self.assertEqual(llm.inner.calls, 4)
        self.assertEqual([c.rank for c in result], list(range(1, 21)))

    def test_four_transient_failures_have_no_fifth_request(self):
        outcomes = [TimeoutError("timed out")] * 4
        llm = _retrying_llm(outcomes)
        with self.assertRaises(ProviderFailure) as ctx:
            self._invoke(llm)
        self.assertEqual(llm.inner.calls, 4)
        self.assertEqual(ctx.exception.category, "transient")

    def test_permanent_401_stops_after_one_request(self):
        llm = _retrying_llm([RuntimeError("401 unauthorized")])
        with self.assertRaises(ProviderFailure):
            self._invoke(llm)
        self.assertEqual(llm.inner.calls, 1)

    def test_schema_invalid_output_is_screening_failure_not_repair(self):
        # A successful HTTP response whose payload fails the schema check:
        # exactly one request, no repair round, ScreeningStop raised.
        llm = _retrying_llm([_valid_output(n=19)])
        with self.assertRaises(ScreeningStop) as ctx:
            self._invoke(llm)
        self.assertEqual(ctx.exception.reason, "SCREENING_INVALID_OUTPUT")
        self.assertEqual(llm.inner.calls, 1)

    def test_validation_error_inside_structured_invoke_is_reclassified(self):
        import pydantic

        try:
            ScreeningOutput.model_validate({"candidates": []})
        except pydantic.ValidationError as exc:
            validation_error = exc
        llm = _retrying_llm([validation_error])
        with self.assertRaises(ScreeningStop) as ctx:
            self._invoke(llm)
        self.assertEqual(ctx.exception.reason, "SCREENING_INVALID_OUTPUT")
        self.assertEqual(llm.inner.calls, 1)

    def test_out_of_input_symbol_rejected(self):
        llm = _retrying_llm([_valid_output(symbols=[f"T{i:02d}" for i in range(19)] + ["NOPE"])])
        with self.assertRaises(ScreeningStop) as ctx:
            self._invoke(llm)
        self.assertEqual(ctx.exception.reason, "SCREENING_INVALID_OUTPUT")

    def test_rank_gap_rejected(self):
        # 20 well-formed rows but ranks 1..19 + 25 (a gap): the exact
        # F1 adversarial case. One request, no repair, zero downstream.
        llm = _retrying_llm([
            ScreeningOutput(
                candidates=[ScreenedCandidate(rank=i + 1, symbol=f"T{i:02d}", screening_score=80.0 - i, short_reason="ok") for i in range(19)]
                + [ScreenedCandidate(rank=25, symbol="T19", screening_score=60.0, short_reason="ok")]
            )
        ])
        with self.assertRaises(ScreeningStop) as ctx:
            self._invoke(llm)
        self.assertEqual(ctx.exception.reason, "SCREENING_INVALID_OUTPUT")
        self.assertIn("consecutive", ctx.exception.detail)
        self.assertEqual(llm.inner.calls, 1)

    def test_rank_set_permutation_is_valid(self):
        # Ranks 1..20 assigned to the 20 symbols in any order are legitimate
        # LLM priority choices: sorted_by_rank() restores 1..20 consecutively.
        symbols = [f"T{i:02d}" for i in range(20)]
        ranks = [3, 1, 2] + list(range(4, 21))
        llm = _retrying_llm([
            ScreeningOutput(
                candidates=[
                    ScreenedCandidate(rank=r, symbol=s, screening_score=80.0, short_reason="ok")
                    for r, s in zip(ranks, symbols)
                ]
            )
        ])
        result = self._invoke(llm)
        self.assertEqual([c.rank for c in result], list(range(1, 21)))

    def test_extra_field_rejected(self):
        payload = {
            "candidates": [
                {
                    "rank": i + 1,
                    "symbol": f"T{i:02d}",
                    "screening_score": 50.0,
                    "short_reason": "ok",
                    "bonus_field": "not allowed",
                }
                for i in range(20)
            ]
        }
        with self.assertRaises(Exception):
            ScreeningOutput.model_validate(payload)


# ---------------------------------------------------------------------------
# C08/C10: prompt contract and sector diversity
# ---------------------------------------------------------------------------


class PromptAndSectorTests(unittest.TestCase):
    def test_prompt_carries_factor_units_and_forbidden_outputs(self):
        from tradingagents.screening.prompt import build_screening_messages

        candidates = _candidates(3)
        for c in candidates:
            c.sector = "Tech"
        messages = build_screening_messages(
            candidates, {"applied": True, "max_per_sector": 5}, select_n=20
        )
        system = messages[0]["content"]
        user = messages[1]["content"]
        for token in ("adv20", "vol20", "volume_ratio", "sqrt(252)", "USD", "0..100"):
            self.assertIn(token, system)
        self.assertIn("at most 5", system)
        self.assertIn("Do NOT output BUY/SELL", system)
        self.assertIn("never see holdings, cash", system)
        self.assertIn("T00", user)
        self.assertNotIn("position", user.lower())

    def test_sector_plan_and_capacity(self):
        from tradingagents.screening.prompt import build_sector_plan

        candidates = _candidates(25)
        for i, c in enumerate(candidates):
            c.sector = "Tech" if i < 22 else f"S{i}"
        plan = build_sector_plan(candidates, max_per_sector=5, select_n=20)
        self.assertTrue(plan["applied"])
        self.assertTrue(plan["insufficient_capacity"])  # 5 + 3 = 8 < 20

        for c in candidates:
            c.sector = None
        plan = build_sector_plan(candidates, max_per_sector=5, select_n=20)
        self.assertFalse(plan["applied"])
        self.assertFalse(plan["insufficient_capacity"])
        self.assertEqual(len(plan["missing_sectors"]), 25)

    def test_sector_diversity_violation_fails_closed(self):
        from tradingagents.screening.prompt import run_screening_invocation

        candidates = _candidates(40)
        for c in candidates:
            c.sector = "Tech"
        llm = _retrying_llm([_valid_output(symbols=[f"T{i:02d}" for i in range(20)])])
        with self.assertRaises(ScreeningStop) as ctx:
            run_screening_invocation(
                llm, candidates, {"applied": True, "max_per_sector": 5, "missing_sectors": []},
                select_n=20, max_per_sector=5,
            )
        self.assertEqual(ctx.exception.reason, "SCREENING_INVALID_OUTPUT")
        self.assertIn("sector diversity violated", ctx.exception.detail)

    def test_insufficient_sector_capacity_stops_before_llm(self):
        from tradingagents.screening.prompt import run_screening_invocation

        candidates = _candidates(40)
        for i, c in enumerate(candidates):
            c.sector = "Tech" if i < 30 else f"S{i}"
        llm = _retrying_llm([])
        with self.assertRaises(ScreeningStop) as ctx:
            run_screening_invocation(
                llm, candidates,
                {"applied": True, "max_per_sector": 5, "missing_sectors": [],
                 "insufficient_capacity": True},
                select_n=20, max_per_sector=5,
            )
        self.assertEqual(ctx.exception.reason, "INSUFFICIENT_SECTOR_CAPACITY")
        self.assertEqual(llm.inner.calls, 0)


# ---------------------------------------------------------------------------
# C12/C13/C14: Top20 ∪ holdings round plan
# ---------------------------------------------------------------------------


class UnionAndHoldingsTests(unittest.TestCase):
    def _config(self, **kw):
        return _base_config(**kw)

    def _scan_world(self, n=25):
        sessions = session_dates_ending_at(_AS_OF, 61)
        universe = _universe([f"T{i:02d}" for i in range(n)])
        bars = {
            u["symbol"]: _bars_df(
                sessions,
                _session_closes(sessions, start=100.0 + i, step=1.0 + i * 0.05),
                250_000.0,
            )
            for i, u in enumerate(universe)
        }
        return universe, bars

    def test_union_is_top20_rank_order_plus_sorted_extras(self):
        universe, bars = self._scan_world()
        invoke_counter = {"screening": 0}
        deps = _deps(
            universe, bars,
            positions=[
                {"symbol": "T19", "qty": 5, "asset_class": "us_equity"},   # in Top20
                {"symbol": "T00", "qty": 3, "asset_class": "us_equity"},   # extra (outside Top20)
                {"symbol": "T04", "qty": 2, "asset_class": "us_equity"},   # extra (just outside)
                {"symbol": "BTC/USD", "qty": 0.5, "asset_class": "crypto"},
            ],
            invoke=_fake_llm_invoke(None, counter=invoke_counter),
        )
        plan = prepare_screening_round(self._config(), deps=deps, now=_NOW)
        self.assertEqual(plan.status, "ok")
        top20 = [e["symbol"] for e in plan.top20]
        self.assertEqual(len(top20), 20)
        # Top20 by rank first, extra holdings sorted by symbol, deduped
        self.assertEqual(plan.deep_analysis_set[:20], top20)
        self.assertEqual(plan.deep_analysis_set[20:], ["T00", "T04"])
        self.assertEqual(len(set(plan.deep_analysis_set)), len(plan.deep_analysis_set))
        self.assertEqual(plan.overlap_holdings, ["T19"])
        self.assertEqual(
            plan.other_asset_holdings, [{"symbol": "BTC/USD", "qty": 0.5}]
        )

    def test_holdings_failure_stops_round_without_analysis(self):
        universe, bars = self._scan_world()

        def broken_positions():
            raise RuntimeError("broker positions unavailable")

        deps = _deps(universe, bars, positions=None)
        deps.positions_fn = broken_positions
        plan = prepare_screening_round(self._config(), deps=deps, now=_NOW)
        self.assertTrue(plan.stopped)
        self.assertEqual(plan.reason, "HOLDINGS_UNAVAILABLE")
        self.assertEqual(plan.deep_analysis_set, [])

    def test_quarantined_and_nontradable_holdings_are_blocked_review(self):
        universe, bars = self._scan_world()
        deps = _deps(
            universe, bars,
            positions=[
                {"symbol": "Q1", "qty": 1, "asset_class": "us_equity"},
                {"symbol": "NT1", "qty": 1, "asset_class": "us_equity"},
                {"symbol": "OK1", "qty": 1, "asset_class": "us_equity"},
            ],
            quarantine=lambda s: "split pending" if s == "Q1" else None,
            assets={"Q1": True, "NT1": False, "OK1": True},
        )
        plan = prepare_screening_round(self._config(), deps=deps, now=_NOW)
        self.assertEqual(plan.status, "ok")
        blocked = {b["symbol"]: b["reason"] for b in plan.blocked_holdings}
        self.assertIn("quarantined", blocked["Q1"])
        self.assertIn("not safely analyzable", blocked["NT1"])
        self.assertEqual(plan.extra_holdings, ["OK1"])
        self.assertEqual(plan.deep_analysis_set[-1], "OK1")

    def test_non_trading_day_is_held_review_only(self):
        saturday = _ET.localize(datetime(2026, 9, 5, 12, 0))
        universe, bars = self._scan_world()
        invoke_counter = {"screening": 0}
        deps = _deps(
            universe, bars,
            positions=[{"symbol": "HLD", "qty": 1, "asset_class": "us_equity"}],
            invoke=_fake_llm_invoke(None, counter=invoke_counter),
        )
        config = self._config()
        plan = prepare_screening_round(config, deps=deps, now=saturday)
        self.assertEqual(plan.status, "ok")
        self.assertEqual(plan.mode, "held_review")
        self.assertFalse(plan.entry_allowed)
        self.assertEqual(plan.deep_analysis_set, ["HLD"])
        self.assertEqual(invoke_counter["screening"], 0)  # no Screening on weekends
        # ... and a manual refresh on a non-trading day is refused
        plan2 = prepare_screening_round(config, deps=deps, now=saturday, refresh=True)
        self.assertTrue(plan2.stopped)
        self.assertEqual(plan2.reason, "SCAN_REFUSED_NON_TRADING_DAY")


# ---------------------------------------------------------------------------
# C17/C18: selection cache
# ---------------------------------------------------------------------------


class SelectionCacheTests(unittest.TestCase):
    def _payload(self, config):
        # Build a realistic payload through the public scan path.
        universe, bars = _universe([f"T{i:02d}" for i in range(25)]), None
        sessions = session_dates_ending_at(_AS_OF, 61)
        bars = {
            u["symbol"]: _bars_df(
                sessions,
                _session_closes(sessions, start=100.0 + i, step=1.0 + i * 0.05),
                250_000.0,
            )
            for i, u in enumerate(universe)
        }
        deps = _deps(universe, bars, positions=[])
        plan = prepare_screening_round(config, deps=deps, now=_NOW)
        self.assertEqual(plan.status, "ok", plan.detail)
        return plan

    def test_same_day_cache_reused_across_restart(self):
        config = _base_config()
        plan = self._payload(config)
        self.assertFalse(plan.cached)
        # A brand-new store instance (fresh process) must read the same file.
        fresh_store = SelectionStore(config["screening_selection_cache_path"])
        selection = fresh_store.load_valid(config, now=_NOW)
        self.assertIsNotNone(selection)
        plan2 = prepare_screening_round(config, deps=_deps([], {}), now=_NOW)
        self.assertTrue(plan2.cached)
        self.assertEqual(plan2.top20, plan.top20)

    def test_next_day_cache_is_invalid(self):
        config = _base_config()
        self._payload(config)
        # Tuesday after the long weekend (Mon 2026-09-07 is Labor Day)
        tuesday = _ET.localize(datetime(2026, 9, 8, 17, 0))
        store = SelectionStore(config["screening_selection_cache_path"])
        self.assertIsNone(store.load_valid(config, now=tuesday))

    def test_config_or_model_change_invalidates(self):
        config = _base_config()
        self._payload(config)
        store = SelectionStore(config["screening_selection_cache_path"])
        changed = _base_config(screening_model="other-model")
        self.assertIsNone(store.load_valid(changed, now=_NOW))
        changed = _base_config(sector_mapping={"T00": "Tech"})
        self.assertIsNone(store.load_valid(changed, now=_NOW))

    def test_corrupted_and_future_dated_cache_invalid(self):
        config = _base_config()
        self._payload(config)
        path = Path(config["screening_selection_cache_path"])
        path.write_text("{not json", encoding="utf-8")
        store = SelectionStore(config["screening_selection_cache_path"])
        self.assertIsNone(store.load_valid(config, now=_NOW))

        # regenerate and tamper with generated_at (future timestamp)
        self._payload(config)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["generated_at"] = "2099-01-01T00:00:00+00:00"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(store.load_valid(config, now=_NOW))

    def test_tampered_top20_membership_invalid(self):
        config = _base_config()
        self._payload(config)
        path = Path(config["screening_selection_cache_path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["top20"][7]["symbol"] = "FAKE"
        path.write_text(json.dumps(data), encoding="utf-8")
        store = SelectionStore(config["screening_selection_cache_path"])
        self.assertIsNone(store.load_valid(config, now=_NOW))

    def test_tampered_rank_gap_invalid(self):
        # F1 defense-in-depth: a stored payload whose ranks are not 1..20
        # (e.g. the historical rank-gap defect's output) never validates.
        config = _base_config()
        self._payload(config)
        path = Path(config["screening_selection_cache_path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["top20"][-1]["rank"] = 25
        path.write_text(json.dumps(data), encoding="utf-8")
        store = SelectionStore(config["screening_selection_cache_path"])
        self.assertIsNone(store.load_valid(config, now=_NOW))

    def test_top40_member_swap_breaks_integrity_seal(self):
        # M1: swapping a top20 slot for another *top40 member* used to stay
        # schema-valid (membership is checked against top40, so the swap was
        # internally consistent). save() now stamps an integrity seal and
        # load_valid() rejects any edit that does not recompute it.
        config = _base_config()
        self._payload(config)
        path = Path(config["screening_selection_cache_path"])
        store = SelectionStore(config["screening_selection_cache_path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        top20_symbols = {entry["symbol"] for entry in data["top20"]}
        top40_extras = [
            row["symbol"] for row in data["top40"] if row["symbol"] not in top20_symbols
        ]
        self.assertTrue(top40_extras)  # 25 eligible > 20 selected
        data["top20"][3]["symbol"] = top40_extras[0]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(store.load_valid(config, now=_NOW))

    def test_missing_or_blank_seal_is_invalid(self):
        # A pre-seal build's output (no integrity field) is never usable.
        config = _base_config()
        path = Path(config["screening_selection_cache_path"])
        store = SelectionStore(config["screening_selection_cache_path"])

        self._payload(config)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("integrity")
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(store.load_valid(config, now=_NOW))

        self._payload(config)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["integrity"] = ""
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(store.load_valid(config, now=_NOW))

    def test_edits_with_stale_seal_are_invalid(self):
        # Any field edit without a fully recomputed seal is rejected — e.g. a
        # backdated as_of (which the date checks alone did not catch) or a
        # tampered top40 factor row. The seal is consistency evidence, not
        # provenance: a local file writer could recompute it, which is why
        # the gate also re-derives everything else on read.
        config = _base_config()
        self._payload(config)
        path = Path(config["screening_selection_cache_path"])
        store = SelectionStore(config["screening_selection_cache_path"])
        self.assertIsNotNone(store.load_valid(config, now=_NOW))

        data = json.loads(path.read_text(encoding="utf-8"))
        data["as_of"] = "2026-09-03"
        data["top40"][0]["score"] = 99.99
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(store.load_valid(config, now=_NOW))

    def test_seal_roundtrip_keeps_same_day_reuse(self):
        # Positive: the seal is computed inside save(), so the normal
        # save -> load path validates unchanged (restart re-reads the file).
        config = _base_config()
        self._payload(config)
        fresh_store = SelectionStore(config["screening_selection_cache_path"])
        self.assertIsNotNone(fresh_store.load_valid(config, now=_NOW))

    def test_invalidate_removes_file(self):
        config = _base_config()
        self._payload(config)
        store = SelectionStore(config["screening_selection_cache_path"])
        self.assertTrue(store.invalidate())
        self.assertFalse(os.path.exists(config["screening_selection_cache_path"]))

    def test_manual_refresh_failure_never_falls_back(self):
        config = _base_config()
        self._payload(config)
        # refresh with a failing screening stage: cache removed first, the
        # round stops, and the gate must not see the old selection.
        def failing(*args, **kwargs):
            raise RuntimeError("LLM down")

        universe, bars = self._payload_universe_bars()
        deps = _deps(universe, bars, positions=[], invoke=failing)
        plan = prepare_screening_round(config, deps=deps, now=_NOW, refresh=True)
        self.assertTrue(plan.stopped)
        self.assertFalse(os.path.exists(config["screening_selection_cache_path"]))
        self.assertIsNotNone(
            check_entry_allowed("T00", config=config, now=_NOW)
        )

    def _payload_universe_bars(self, n=25):
        sessions = session_dates_ending_at(_AS_OF, 61)
        universe = _universe([f"T{i:02d}" for i in range(n)])
        bars = {
            u["symbol"]: _bars_df(
                sessions,
                _session_closes(sessions, start=100.0 + i, step=1.0 + i * 0.05),
                250_000.0,
            )
            for i, u in enumerate(universe)
        }
        return universe, bars


# ---------------------------------------------------------------------------
# C15: entry gate (function + real execution entry)
# ---------------------------------------------------------------------------


def _selection_config():
    """Config + a valid cached selection built through the real pipeline."""
    config = _base_config()
    universe, bars = SelectionCacheTests._payload_universe_bars(None)
    deps = _deps(universe, bars, positions=[])
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert plan.status == "ok", plan.detail
    return config, plan


class EntryGateTests(unittest.TestCase):

    def test_manual_mode_is_untouched(self):
        config = _base_config(auto_screening_enabled=False)
        self.assertIsNone(check_entry_allowed("ANY", config=config, now=_NOW))

    def test_auto_mode_without_selection_blocks(self):
        config = _base_config()
        reason = check_entry_allowed("AAPL", config=config, now=_NOW)
        self.assertIsNotNone(reason)
        self.assertIn("no validated Top20", reason)

    def test_crypto_symbols_keep_their_existing_path(self):
        config = _base_config()
        self.assertIsNone(check_entry_allowed("BTC/USD", config=config, now=_NOW))

    def test_top20_member_allowed_and_holdings_only_blocked(self):
        config, plan = _selection_config()
        member = plan.top20[0]["symbol"]
        outsider = next(
            u["symbol"] for u in SelectionCacheTests._payload_universe_bars(None)[0]
            if u["symbol"] not in {e["symbol"] for e in plan.top20}
        )
        self.assertIsNone(check_entry_allowed(member, config=config, now=_NOW))
        reason = check_entry_allowed(outsider, config=config, now=_NOW)
        self.assertIsNotNone(reason)
        self.assertIn("not in today's validated Top20", reason)

    def test_non_trading_day_blocks_even_with_valid_cache(self):
        config, plan = _selection_config()
        saturday = _ET.localize(datetime(2026, 9, 5, 12, 0))
        reason = check_entry_allowed(plan.top20[0]["symbol"], config=config, now=saturday)
        self.assertIsNotNone(reason)
        self.assertIn("not a US equity trading day", reason)


class ExecutionEntryGateTests(unittest.TestCase):
    """The gate runs inside the real single execution entry."""

    def _intent(self, action="BUY", symbol="AAPL", current="NEUTRAL"):
        from tradingagents.agents.schemas import (
            ExecutableAction,
            RiskDecision,
            build_trade_intent_from_risk_decision,
        )

        return build_trade_intent_from_risk_decision(
            symbol=symbol,
            trading_mode="investment",
            current_position=current,
            allow_shorts=False,
            trade_date="2026-09-04",
            decision=RiskDecision(
                action=ExecutableAction(action),
                confidence="medium",
                risk_rationale="gate test",
                required_controls="strict", entry_policy=_ready_entry_policy(), stop_loss_price=95,
            ),
        ).model_dump(mode="json")

    def _broker(self, *, positions=None):
        submit_calls = []
        orders = []
        state = {"positions": list(positions or []), "orders": orders, "submit_calls": submit_calls}

        def submit_order(request):
            submit_calls.append(1)
            get = request.get if isinstance(request, dict) else (
                lambda name: getattr(request, name, None)
            )
            order = SimpleNamespace(
                id=f"broker-{len(submit_calls)}",
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
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity="100000", last_equity="100000", cash="80000", buying_power="200000"
            ),
            get_all_positions=lambda: list(state["positions"]),
            get_orders=lambda request=None: list(state["orders"]),
            get_order_by_client_order_id=lambda cid: next(
                (o for o in state["orders"] if o.client_order_id == cid), None
            ),
            submit_order=submit_order,
            state=state,
        )
        return broker

    def _quote(self, symbol, price=100.0):
        from datetime import timezone

        from tradingagents.execution.authority import BrokerQuote

        return BrokerQuote(
            symbol, price - 0.1, price + 0.1, datetime.now(timezone.utc)
        )

    def _run(self, config, intent, *, positions=None):
        from tradingagents.execution.service import ExecutionService

        import uuid

        broker = self._broker(positions=positions)
        service = ExecutionService(
            # a fresh ledger per run: recovery of a previous test's orders
            # must not mask the gate being tested
            db_path=str(Path(config["results_dir"]) / f"execution-{uuid.uuid4().hex}.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda symbol: self._quote(symbol),
        )
        with patch(
            "tradingagents.execution.service._get_execution_config",
            return_value=config,
        ), patch(
            # hermetic wall clock: the gate must see the selection's Friday
            "tradingagents.screening.gate.eastern_now",
            return_value=_NOW,
        ):
            result = service.execute(
                trade_intent=intent, dollar_amount=1000.0, allow_shorts=False
            )
        return result, broker

    def test_buy_outside_top20_makes_zero_broker_calls(self):
        config, plan = _selection_config()
        outsider = next(
            u["symbol"] for u in SelectionCacheTests._payload_universe_bars(None)[0]
            if u["symbol"] not in {e["symbol"] for e in plan.top20}
        )
        result, broker = self._run(
            config, self._intent(symbol=outsider)
        )
        self.assertFalse(result["success"])
        self.assertTrue(result.get("entry_gate_blocked"))
        self.assertEqual(result.get("broker_calls", 0), 0)
        self.assertEqual(broker.state["submit_calls"], [])

    def test_buy_inside_top20_reaches_the_broker(self):
        config, plan = _selection_config()
        member = plan.top20[0]["symbol"]
        result, broker = self._run(config, self._intent(symbol=member))
        self.assertTrue(result.get("success"), result)
        self.assertGreaterEqual(len(broker.state["submit_calls"]), 1)
        self.assertFalse(result.get("entry_gate_blocked", False))

    def test_holdings_only_sell_is_not_gated(self):
        config, plan = _selection_config()
        outsider = next(
            u["symbol"] for u in SelectionCacheTests._payload_universe_bars(None)[0]
            if u["symbol"] not in {e["symbol"] for e in plan.top20}
        )
        from tradingagents.execution.authority import BrokerPosition

        held = BrokerPosition(outsider, 10.0, 1000.0)
        result, broker = self._run(
            config,
            self._intent(action="SELL", symbol=outsider, current="LONG"),
            positions=[held],
        )
        self.assertTrue(result.get("success"), result)
        self.assertGreaterEqual(len(broker.state["submit_calls"]), 1)


# ---------------------------------------------------------------------------
# C19/C20/C21: scheduler integration (webui halt + double-runner + chain)
# ---------------------------------------------------------------------------


class SchedulerIntegrationTests(unittest.TestCase):
    def test_screening_stop_halts_the_scheduler(self):
        from webui.utils.state import app_state
        from webui.callbacks.control_callbacks import _prepare_auto_round_symbols

        app_state.reset()
        stopped = RoundPlan(
            status="stopped", reason="INSUFFICIENT_CANDIDATES", detail="only 3"
        )
        with patch(
            "tradingagents.screening.pipeline.prepare_screening_round",
            return_value=stopped,
        ):
            result = _prepare_auto_round_symbols(
                {"auto_screening_enabled": True}
            )
        self.assertIsNone(result)
        self.assertIsNotNone(app_state.screening_stop_reason)
        self.assertIn("INSUFFICIENT_CANDIDATES", app_state.screening_stop_reason)
        self.assertTrue(app_state.stop_loop)
        self.assertTrue(app_state.stop_market_hour)
        self.assertEqual(app_state.analysis_queue, [])
        app_state.reset()

    def test_successful_plan_feeds_the_round_symbols(self):
        from webui.utils.state import app_state
        from webui.callbacks.control_callbacks import _prepare_auto_round_symbols

        app_state.reset()
        universe, bars = SelectionCacheTests._payload_universe_bars(None)
        deps = _deps(
            universe, bars,
            positions=[{"symbol": "T00", "qty": 1, "asset_class": "us_equity"}],
        )
        config = _base_config()
        with patch(
            "tradingagents.screening.pipeline.prepare_screening_round",
            return_value=prepare_screening_round(config, deps=deps, now=_NOW),
        ):
            symbols = _prepare_auto_round_symbols(
                {"auto_screening_enabled": True,
                 "screening_selection_cache_path": config["screening_selection_cache_path"],
                 "screening_as_of_override": config["screening_as_of_override"],
                 "data_cache_dir": config["data_cache_dir"],
                 "results_dir": config["results_dir"]}
            )
        self.assertIsNotNone(symbols)
        self.assertEqual(len(symbols), 21)  # Top20 + T00 extra
        self.assertEqual(
            [entry["symbol"] for entry in app_state.screening_top20], symbols[:20]
        )
        app_state.reset()

    def test_two_concurrent_first_scans_produce_one_screening_call(self):
        config = _base_config()
        universe, bars = SelectionCacheTests._payload_universe_bars(None)
        counter = {"screening": 0}

        def invoke(candidates, sector_plan, *, select_n=20, max_per_sector=5):
            counter["screening"] += 1
            time.sleep(0.3)  # keep the first runner inside the scan critical section
            return _fake_llm_invoke(None)(
                candidates, sector_plan, select_n=select_n,
                max_per_sector=max_per_sector,
            )

        deps = _deps(universe, bars, positions=[], invoke=invoke)
        results = {}

        def runner():
            results[threading.get_ident()] = prepare_screening_round(
                config, deps=deps, now=_NOW
            )

        threads = [threading.Thread(target=runner) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        plans = list(results.values())
        self.assertTrue(all(p.status == "ok" for p in plans), plans)
        self.assertEqual(counter["screening"], 1)
        self.assertEqual(
            plans[0].top20[0]["symbol"], plans[1].top20[0]["symbol"]
        )

    def test_full_chain_assets_bars_screening_union_execution_gate(self):
        """C21: assets → bars → Top40 → Screening → 20∪holdings → gate."""
        config = _base_config()
        universe, bars = SelectionCacheTests._payload_universe_bars(25)
        deps = _deps(
            universe, bars,
            positions=[
                {"symbol": "T04", "qty": 2, "asset_class": "us_equity"},  # outside Top20
                {"symbol": "BTC/USD", "qty": 1.0, "asset_class": "crypto"},
            ],
        )
        plan = prepare_screening_round(config, deps=deps, now=_NOW)
        self.assertEqual(plan.status, "ok", plan.detail)
        self.assertEqual(len(plan.selection["top40"]), 25)
        self.assertEqual(len(plan.top20), 20)
        self.assertEqual(plan.extra_holdings, ["T04"])
        self.assertEqual(plan.deep_analysis_set, [e["symbol"] for e in plan.top20] + ["T04"])

        # The Top20's best member may enter through the real execution entry;
        # the holdings-only extra may not.
        member = plan.top20[0]["symbol"]
        entry_tests = ExecutionEntryGateTests()
        result, broker = entry_tests._run(config, entry_tests._intent(symbol=member))
        self.assertTrue(result.get("success"), result)
        outsider_result, outsider_broker = entry_tests._run(
            config, entry_tests._intent(symbol="T04")
        )
        self.assertTrue(outsider_result.get("entry_gate_blocked"))
        self.assertEqual(outsider_broker.state["submit_calls"], [])


if __name__ == "__main__":
    unittest.main()
