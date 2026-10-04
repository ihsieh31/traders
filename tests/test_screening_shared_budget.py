"""The stock budget includes Screening, both price lanes, and held reviews."""
from datetime import timedelta
from unittest.mock import Mock

import pytest

from test_phase_c_screening import _base_config, _deps, _two_sided_screening_world, _fake_llm_invoke, _NOW
from tradingagents.screening.llm import resolve_screening_config, ScreeningConfigError
from tradingagents.screening.pipeline import prepare_screening_round, prepare_screening_round_from_selection
from tradingagents.screening.selection_store import SelectionStore


def setup(held=()):
    universe, bars = _two_sided_screening_world()
    config = _base_config(allow_shorts=True)
    seen = set()

    def invoke(candidates, sector_plan, **kw):
        seen.update(c.symbol for c in candidates)
        return _fake_llm_invoke(None)(candidates, sector_plan, **kw)

    positions = [{"symbol": s, "qty": (-1 if i % 2 else 1)} for i, s in enumerate(held)]
    return config, _deps(universe, bars, positions=positions, invoke=invoke), seen


def test_short_and_long_lanes_plus_duplicate_holdings_share_the_invocation_budget():
    config, deps, seen = setup(["HOLD1", "HOLD2", "HOLD2", "HOLD3"])
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert not plan.stopped, plan.detail
    assert len(seen) == 17 and len(plan.deep_analysis_set) == 20
    assert len(seen | set(plan.deep_analysis_set)) == 20
    assert {r["candidate_lane"] for r in plan.selection["top40"]} == {"positive_trend", "negative_trend"}
    assert {"HOLD1", "HOLD2", "HOLD3"}.issubset(plan.deep_analysis_set)
    assert len(plan.top20) == 17 and plan.scan_stats["selection_shortfall"] == 3


def test_full_held_budget_skips_screening_and_empty_selection_survives_cache_and_freeze():
    config, deps, seen = setup([f"H{i:02d}" for i in range(20)])
    deps.screening_invoke_fn = Mock(side_effect=AssertionError("no Screening slots remain"))
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert not plan.stopped and not plan.top20 and len(plan.deep_analysis_set) == 20
    assert not seen
    cached = prepare_screening_round(config, deps=deps, now=_NOW)
    assert cached.cached and cached.deep_analysis_set == plan.deep_analysis_set
    frozen = prepare_screening_round_from_selection(config, plan.selection, deps=deps)
    assert not frozen.stopped and frozen.deep_analysis_set == plan.deep_analysis_set
    deps.screening_invoke_fn.assert_not_called()


def test_too_many_holdings_stops_before_scan_or_llm_and_on_nontrading_days():
    config, deps, seen = setup([f"H{i:02d}" for i in range(21)])
    deps.universe_fn = Mock(side_effect=AssertionError("must reserve held slots first"))
    for now in (_NOW, _NOW+timedelta(days=1)):
        plan = prepare_screening_round(config, deps=deps, now=now)
        assert plan.reason == "HELD_REVIEW_BUDGET_EXCEEDED"
        assert not plan.deep_analysis_set and not plan.entry_allowed
    assert not seen
    deps.universe_fn.assert_not_called()


def test_holdings_change_after_screening_stops_before_analyzing_an_extra_symbol():
    config, deps, seen = setup()
    snapshots = iter([[], [{"symbol": "NEWHELD", "qty": -1}]])
    deps.positions_fn = lambda: next(snapshots)
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert len(seen) == 20
    assert plan.reason == "SCREENING_ANALYSIS_BUDGET_EXCEEDED"
    assert not plan.deep_analysis_set and not plan.entry_allowed


def test_cache_holdings_reserve_slots_and_legacy_budget_binds_the_fingerprint():
    config, deps, _ = setup()
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    deps.positions_fn = lambda: [{"symbol": "NEWHELD", "qty": -1}]
    cached = prepare_screening_round(config, deps=deps, now=_NOW)
    assert cached.cached and len(cached.deep_analysis_set) == 20
    assert cached.deferred_candidates == [plan.top20[-1]["symbol"]]
    assert "NEWHELD" in cached.deep_analysis_set
    store = SelectionStore(config["screening_selection_cache_path"])
    assert store.config_fingerprint(config, None) != store.config_fingerprint({**config, "screening_analysis_limit": 19}, None)
    old = dict(plan.selection, schema_version=5)
    store.save(old)
    assert store.load_valid(config, now=_NOW) is None


@pytest.mark.parametrize("key", ["screening_analysis_limit", "screening_select_n"])
def test_short_mode_cannot_configure_above_twenty(key):
    config, _, _ = setup()
    with pytest.raises(ScreeningConfigError, match="at most 20"):
        resolve_screening_config({**config, key: 21})
