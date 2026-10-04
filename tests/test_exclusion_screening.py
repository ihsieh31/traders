"""Offline production-path evidence for the report formula and its boundaries."""
from dataclasses import replace
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from test_phase_c_screening import _base_config, _deps, _healthy_bars, _universe, _NOW, _AS_OF
from tradingagents.screening.metrics import SymbolFeatures, ScanStats, score_exclusion_candidates
from tradingagents.screening.policy import ExclusionThresholds, screening_method
from tradingagents.screening.llm import resolve_screening_config, ScreeningConfigError, build_screening_llm
from tradingagents.screening.pipeline import prepare_screening_round, prepare_screening_round_from_selection
from tradingagents.screening.selection_store import SelectionStore, selection_rows_valid
from tradingagents.screening.gate import check_entry_allowed
from tradingagents.screening.context import screening_context_from_selection, render_screening_context


def feature(symbol, trend=.01, r60=.1, vol20=.1):
    return SymbolFeatures(symbol=symbol, price=100, adv20=25_000_000, r5=.01, r20=.03,
                          r60=r60, vol20=vol20, volume_ratio=1, trend=trend, score=77)


def world(n=25, **config_updates):
    config = _base_config(screening_method='auto', screening_provider=None, screening_model=None,
                          **config_updates)
    names = [f'T{i:02d}' for i in range(n)]
    bars = {name: _healthy_bars(start=100 + i, step=1 + .1 * i) for i, name in enumerate(names)}
    invoke = Mock(side_effect=AssertionError('deterministic screening must never invoke an LLM'))
    deps = _deps(_universe(names), bars, invoke=invoke)
    return config, deps, invoke


def test_formula_hand_calculation_after_gating_and_stable_ties():
    # Survivor percentiles are A=(0,0,0), B=(.5,1,.5), C=(1,.5,1).
    rows = [feature('A'), feature('B', .02, .3, .2), feature('C', .03, .2, .28),
            feature('BAD', 9, 9, .280001)]
    stats = ScanStats()
    ranked = score_exclusion_candidates(rows, ExclusionThresholds(), stats)
    assert [(r.symbol, r.exclusion_score) for r in ranked] == [('B', 65), ('C', 65), ('A', 20)]
    assert all(r.score == 77 for r in ranked)  # old score survives for comparisons
    assert stats.excluded == {'exclusion_vol20': 1}
    assert ranked == score_exclusion_candidates(rows[::-1], ExclusionThresholds())
    assert score_exclusion_candidates([feature('ONE')], ExclusionThresholds())[0].exclusion_score == 50


@pytest.mark.parametrize('change,passes', [
    ({'vol20': .28}, True), ({'vol20': .280001}, False),
    ({'trend': 0}, False), ({'trend': -.001}, False),
    # A close equal to its 20-day mean computes as +/-1e-16 from decimal
    # prices; rounding noise must not decide the gate. The smallest real
    # cent-priced trend is ~7e-10.
    ({'trend': 2.220446049250313e-16}, False), ({'trend': 7e-10}, True),
    ({'r60': -.25}, True), ({'r60': -.250001}, False),
    ({'vol20': float('nan')}, False), ({'r60': float('inf')}, False),
])
def test_exact_gate_boundaries(change, passes):
    assert bool(score_exclusion_candidates([replace(feature('A'), **change)], ExclusionThresholds())) == passes


def test_default_long_screen_needs_no_llm_role_but_short_and_legacy_stay_explicit():
    resolved = resolve_screening_config({'auto_screening_enabled': True})
    assert resolved['method'] == 'exclusion' and resolved['spec'] is None
    with pytest.raises(ScreeningConfigError, match='does not use an LLM'):
        build_screening_llm(resolved, {})
    assert screening_method({'allow_shorts': True}) == 'legacy'
    with pytest.raises(ScreeningConfigError):
        resolve_screening_config({'auto_screening_enabled': True, 'allow_shorts': True})
    with pytest.raises(ScreeningConfigError, match='long-only'):
        resolve_screening_config({'auto_screening_enabled': True, 'allow_shorts': True, 'screening_method': 'exclusion'})


@pytest.mark.parametrize('config_update', [
    {'screening_max_vol20': float('nan')}, {'screening_max_vol20': True},
    {'screening_max_vol20': 0}, {'screening_min_r60': -.5 - 1},
    {'screening_analysis_limit': 21}, {'screening_select_n': 21},
    {'screening_analysis_limit': 0}, {'screening_method': 'typo'},
])
def test_invalid_policies_fail_before_data_or_llm(config_update):
    with pytest.raises(ScreeningConfigError):
        resolve_screening_config({'auto_screening_enabled': True, **config_update})


def test_production_scan_cache_and_gate_use_exact_report_formula_without_llm():
    config, deps, invoke = world()
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert not plan.stopped and len(plan.top20) == len(plan.deep_analysis_set) == 20
    assert len(plan.selection['top40']) == 25  # full survivors, no legacy Top40 truncation
    assert selection_rows_valid(plan.selection, config)
    assert all('vol20' in r and 'adv20' in r for r in plan.top20)
    cached = prepare_screening_round(config, deps=deps, now=_NOW)
    assert cached.cached and cached.top20 == plan.top20
    invoke.assert_not_called()
    assert check_entry_allowed(plan.top20[0]['symbol'], config=config, now=_NOW) is None
    assert check_entry_allowed('OUTSIDE', config=config, now=_NOW) is not None
    frozen = prepare_screening_round_from_selection(config, plan.selection, deps=deps, session_date=str(_AS_OF))
    assert not frozen.stopped and frozen.deep_analysis_set == plan.deep_analysis_set


def test_partial_selection_never_pads_and_frozen_ab_accepts_it(tmp_path):
    config, deps, invoke = world(9)
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert not plan.stopped and len(plan.top20) == 9
    assert plan.scan_stats['selection_shortfall'] == 11
    assert prepare_screening_round(config, deps=deps, now=_NOW).cached
    assert not prepare_screening_round_from_selection(config, plan.selection, deps=deps).stopped
    from tradingagents.long_run import _read_ab_selection_artifact, _ab_selection_hash, LongRunStop
    artifact = tmp_path / 'selection.json'
    artifact.write_text(json.dumps({'status': 'SCREENING_COMPLETE', 'session_date': str(_AS_OF),
                                   'as_of': str(_AS_OF), 'selection': plan.selection,
                                   'selection_hash': _ab_selection_hash(plan.selection)}))
    assert _read_ab_selection_artifact(artifact, session_date=str(_AS_OF), screening_config=config)
    # legacy exact-N contract is preserved
    with pytest.raises(LongRunStop):
        _read_ab_selection_artifact(artifact, session_date=str(_AS_OF), screening_config={**config, 'screening_method': 'legacy'})
    invoke.assert_not_called()


def test_zero_survivors_keeps_empty_selection_without_relaxing_gates():
    config, deps, invoke = world(25)
    good = prepare_screening_round(config, deps=deps, now=_NOW)
    assert not good.stopped
    deps.bars_fn = lambda symbols, **kwargs: {s: _healthy_bars(start=200, step=-1) for s in symbols}
    plan = prepare_screening_round(config, refresh=True, deps=deps, now=_NOW)
    assert not plan.stopped and not plan.top20 and not plan.deep_analysis_set
    assert SelectionStore(config['screening_selection_cache_path']).load_raw()['top20'] == []
    assert selection_rows_valid(plan.selection, config)
    invoke.assert_not_called()


def test_sector_cap_scans_full_survivor_pool_and_can_return_under_twenty():
    sectors = {f'T{i:02d}': 'ONE' if i < 21 else 'TWO' for i in range(30)}
    config, deps, invoke = world(30, sector_mapping=sectors)
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert not plan.stopped and len(plan.top20) == 10
    counts = {}
    for row in plan.top20:
        sector = sectors[row['symbol']]
        counts[sector] = counts.get(sector, 0) + 1
    assert counts == {'ONE': 5, 'TWO': 5}
    assert selection_rows_valid(plan.selection, config)
    invoke.assert_not_called()


def test_holdings_reserve_slots_and_fresh_holdings_on_cache_hits():
    config, deps, invoke = world()
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    overlap = plan.top20[-1]['symbol']
    deps.positions_fn = lambda: [{'symbol': overlap, 'qty': 1}, {'symbol': 'HELD', 'qty': 1}, {'symbol': 'HELD', 'qty': 1}]
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    assert plan.cached and len(plan.deep_analysis_set) == 20
    assert overlap in plan.deep_analysis_set and 'HELD' in plan.deep_analysis_set
    assert plan.extra_holdings == ['HELD'] and len(plan.deferred_candidates) == 1
    assert len(set(plan.deep_analysis_set)) == 20
    deps.positions_fn = lambda: [{'symbol': f'H{i:02d}', 'qty': 1} for i in range(21)]
    stopped = prepare_screening_round(config, deps=deps, now=_NOW)
    assert stopped.reason == 'HELD_REVIEW_BUDGET_EXCEEDED' and not stopped.entry_allowed
    assert not stopped.deep_analysis_set
    invoke.assert_not_called()


@pytest.mark.parametrize('mutation', ['gate', 'order', 'score', 'factor', 'duplicate', 'method'])
def test_resealed_invalid_caches_and_frozen_selections_fail_closed(mutation):
    config, deps, _ = world()
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    bad = copy.deepcopy(plan.selection)
    if mutation == 'gate': bad['top40'][0]['vol20'] = .29
    elif mutation == 'order': bad['top20'][0], bad['top20'][1] = bad['top20'][1], bad['top20'][0]
    elif mutation == 'score': bad['top40'][0]['exclusion_score'] += 1
    elif mutation == 'factor': bad['top20'][0]['vol20'] += .01
    elif mutation == 'duplicate': bad['top40'].append(copy.deepcopy(bad['top40'][0]))
    elif mutation == 'method': bad['method'] = 'legacy'
    store = SelectionStore(config['screening_selection_cache_path'])
    store.save(bad)  # even a fresh self-hash cannot make invalid formulas usable
    bad = store.load_raw()
    assert store.load_valid(config, now=_NOW) is None
    assert prepare_screening_round_from_selection(config, bad, deps=deps).stopped
    assert check_entry_allowed(plan.top20[0]['symbol'], config=config, now=_NOW) is not None


@pytest.mark.parametrize('update', [{'screening_max_vol20': .3}, {'screening_min_r60': -.2},
                                    {'screening_method': 'legacy', 'screening_provider': 'openai', 'screening_model': 'fake'},
                                    {'screening_analysis_limit': 19}])
def test_formula_settings_invalidate_daily_and_frozen_cache(update):
    config, deps, _ = world()
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    changed = {**config, **update}
    assert SelectionStore(config['screening_selection_cache_path']).load_valid(changed, now=_NOW) is None
    assert prepare_screening_round_from_selection(changed, plan.selection, deps=deps).stopped


def test_frozen_risk_context_has_units_and_does_not_claim_unknown_holdings_passed():
    config, deps, _ = world(9)
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    config['_screening_context'] = screening_context_from_selection(plan.selection)
    symbol = plan.top20[0]['symbol']
    text = render_screening_context(config, symbol, str(_AS_OF))
    assert 'vol20' in text and 'annualized' in text and str(_AS_OF) in text
    assert 'not a return forecast' in text
    assert 'unavailable' in render_screening_context(config, 'HELD', str(_AS_OF))
    assert 'unavailable' in render_screening_context(config, symbol, '2020-01-01')


def test_screening_measurements_reach_actual_risk_manager_prompt():
    from unittest.mock import MagicMock
    from test_phase_a1_execution_foundation import StrictRiskBoundaryTests
    config = {'allow_shorts': False, '_screening_context': {
        'as_of': '2026-01-02', 'method': 'exclusion', 'rows': {'AAPL': feature('AAPL').factor_row()},
    }}
    with patch('tradingagents.agents.managers.risk_manager.analysis_date_mode', return_value='current'), \
         patch('tradingagents.agents.managers.risk_manager.capture_agent_prompt') as capture:
        StrictRiskBoundaryTests()._run_node(MagicMock(), None, config)
    prompt = capture.call_args.args[1]
    assert 'Frozen screening measurements' in prompt and 'vol20' in prompt and 'adv20' in prompt
    assert 'as_of=2026-01-02' in prompt and 'not a return forecast' in prompt


def test_schema_four_cache_is_not_reused_even_after_resealing():
    config, deps, _ = world()
    plan = prepare_screening_round(config, deps=deps, now=_NOW)
    store = SelectionStore(config['screening_selection_cache_path'])
    old = copy.deepcopy(plan.selection)
    old['schema_version'] = 4
    store.save(old)
    assert store.load_valid(config, now=_NOW) is None
    assert prepare_screening_round_from_selection(config, store.load_raw(), deps=deps).stopped


def test_non_trading_day_held_review_obeys_stock_budget_and_never_scans():
    from datetime import timedelta
    config, deps, invoke = world()
    deps.universe_fn = Mock(side_effect=AssertionError('no non-trading-day scan'))
    deps.positions_fn = lambda: [{'symbol': f'H{i:02d}', 'qty': 1} for i in range(20)]
    plan = prepare_screening_round(config, deps=deps, now=_NOW + timedelta(days=1))
    assert not plan.stopped and plan.mode == 'held_review' and not plan.entry_allowed
    assert len(plan.deep_analysis_set) == 20
    deps.positions_fn = lambda: [{'symbol': f'H{i:02d}', 'qty': 1} for i in range(21)]
    assert prepare_screening_round(config, deps=deps, now=_NOW + timedelta(days=1)).reason == 'HELD_REVIEW_BUDGET_EXCEEDED'
    deps.universe_fn.assert_not_called()
    invoke.assert_not_called()
