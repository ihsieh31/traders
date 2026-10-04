"""Offline boundary checks: identity/price evidence, frozen reuse, and unknowns."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from test_exclusion_screening import world
from test_phase_c_screening import _AS_OF, _NOW
from tradingagents.screening.pipeline import prepare_screening_round, prepare_screening_round_from_selection
from tradingagents.screening.selection_store import SelectionStore, selection_rows_valid
from tradingagents.screening.quality import assess_financial_facts, historical_research_gate


@pytest.mark.parametrize("kind,category", [
    ("identity", "data_gap"), ("duplicate_id", "data_error"), ("ohlc", "data_error"),
    ("missing_ohlc", "data_error"), ("source", "data_gap"), ("feed", "data_error"),
    ("adjustment", "data_error"), ("market_cap", "data_gap"), ("timestamp", "data_error"),
])
def test_bad_inputs_are_excluded_without_zero_fill_or_extra_llm(kind, category):
    cfg,deps,llm = world(3)
    universe = deps.universe_fn(cfg)
    bad = universe[0]["symbol"]
    original = deps.bars_fn
    frames = original([r["symbol"] for r in universe])
    if kind=="identity":
        universe[0].pop("asset_id")
    elif kind=="duplicate_id":
        universe[1]["asset_id"] = universe[0]["asset_id"]
    elif kind=="market_cap":
        universe[0]["market_cap"] = None
    elif kind=="ohlc":
        frames[bad].loc[0,"high"] = 1
    elif kind=="missing_ohlc":
        frames[bad] = frames[bad].drop(columns="open")
    elif kind=="timestamp":
        frames[bad].loc[0,"timestamp"] = None
    else:
        frames[bad].attrs[{"source":"source", "feed":"feed", "adjustment":"adjustment"}[kind]] = {"source":None,"feed":"iex","adjustment":"raw"}[kind]
    deps.bars_fn = lambda symbols, **kwargs: {s:frames[s] for s in symbols}
    plan = prepare_screening_round(cfg,deps=deps,now=_NOW)
    assert not plan.stopped, plan.detail
    assert bad not in plan.deep_analysis_set
    quality = plan.selection["data_quality"]
    record = next(r for r in quality["records"] if r["symbol"]==bad)
    assert record["status"]=="excluded" and record["category"]==category
    assert quality["financials"]["status"]=="unknown" and quality["business_quality"]=="unknown"
    assert selection_rows_valid(plan.selection,cfg)
    llm.assert_not_called()


def test_ambiguous_identity_never_requests_bars_and_never_pads():
    cfg,deps,llm = world(2)
    rows = deps.universe_fn(cfg)
    rows[1]["asset_id"] = rows[0]["asset_id"]
    deps.bars_fn = Mock(side_effect=AssertionError("identity must be checked first"))
    plan = prepare_screening_round(cfg,deps=deps,now=_NOW)
    assert not plan.stopped and not plan.top20 and not plan.deep_analysis_set
    assert plan.selection["data_quality"]["counts"]=={"data_error":2}
    assert plan.scan_stats["selection_shortfall"]==20
    deps.bars_fn.assert_not_called()
    llm.assert_not_called()


@pytest.mark.parametrize("change", ["missing_report","identity","cap","bars","factors","derived_values","business_claim","old_schema"])
def test_resealed_bad_evidence_fails_daily_cache_and_frozen_ab(change):
    cfg,deps,_ = world(3)
    plan = prepare_screening_round(cfg,deps=deps,now=_NOW)
    store = SelectionStore(cfg["screening_selection_cache_path"])
    broken = copy.deepcopy(plan.selection)
    if change=="missing_report":
        broken.pop("data_quality")
    elif change=="old_schema":
        broken["schema_version"] = 6
    elif change=="business_claim":
        broken["data_quality"]["business_quality"] = "verified_good"
    else:
        record = next(r for r in broken["data_quality"]["records"] if r["symbol"]==broken["top20"][0]["symbol"])
        if change=="identity":record["asset_id"] = None
        if change=="cap":record["market_cap"] = None
        if change=="bars":record["bars"]["feed"] = "iex"
        if change=="factors":record["factors"]["price"] += 1
        if change=="derived_values":
            record["factors"]["r20"] += .01
            for row in broken["top40"]+broken["top20"]:
                if row["symbol"]==record["symbol"]:row["r20"] = record["factors"]["r20"]
    store.save(broken)
    assert store.load_valid(cfg,now=_NOW) is None
    frozen = prepare_screening_round_from_selection(cfg,store.load_raw(),deps=deps,session_date=str(_AS_OF))
    assert frozen.stopped and not frozen.deep_analysis_set


def test_completed_scan_snapshots_survive_daily_cache_replacement():
    cfg,deps,_ = world(3)
    first = prepare_screening_round(cfg,deps=deps,now=_NOW)
    store = SelectionStore(cfg["screening_selection_cache_path"])
    archived = store.snapshot_path(first.selection)
    before = archived.read_bytes()
    original = deps.bars_fn
    def changed(symbols, **kwargs):
        frames = original(symbols, **kwargs)
        for frame in frames.values():frame["high"] += 1
        return frames
    deps.bars_fn = changed
    second = prepare_screening_round(cfg,deps=deps,refresh=True,now=_NOW)
    assert not second.stopped and store.snapshot_path(second.selection)!=archived
    assert archived.read_bytes()==before
    assert selection_rows_valid(json.loads(before),cfg)
    assert len(list(archived.parent.glob("*.json")))==2


def test_snapshot_write_failure_stops_before_deep_analysis():
    cfg,deps,_ = world(3)
    with patch.object(SelectionStore,"_write_sealed",side_effect=OSError("disk full")):
        plan = prepare_screening_round(cfg,deps=deps,now=_NOW)
    assert plan.stopped and plan.reason=="SCREENING_SNAPSHOT_UNAVAILABLE"
    assert not plan.entry_allowed and not plan.deep_analysis_set


def financial_facts():
    common=dict(unit="USD", scope="consolidated", period_end="2019-12-31",
                available_at="2020-03-01T12:00:00",source="fixture_filing")
    return {"net_income":dict(common,value=10.,qtrs=4),
            "operating_cash_flow":dict(common,value=15.,qtrs=4), "assets":dict(common,value=100.,qtrs=0)}


@pytest.mark.parametrize("change,expected", [("none","usable"),("missing","unknown"),("scope_missing","unknown"),
    ("scope_mismatch","invalid"),("unit","invalid"),("period","invalid"),("future","invalid"),("zero_assets","invalid"),("nan","invalid")])
def test_financial_compatibility_and_availability_are_required(change, expected):
    facts=financial_facts()
    if change=="missing":facts.pop("operating_cash_flow")
    if change=="scope_missing":facts["net_income"].pop("scope")
    if change=="scope_mismatch":facts["net_income"]["scope"]="shareholder_attributable"
    if change=="unit":facts["assets"]["unit"]="USD_thousands"
    if change=="period":facts["net_income"]["period_end"]="2018-12-31"
    if change=="future":facts["net_income"]["available_at"]="2020-04-01"
    if change=="zero_assets":facts["assets"]["value"]=0
    if change=="nan":facts["net_income"]["value"]=float("nan")
    result=assess_financial_facts(facts,as_of="2020-03-31")
    assert result["status"]==expected
    assert assess_financial_facts(financial_facts())["status"]=="unknown"


def test_historical_gate_is_explicit_and_not_a_profit_claim():
    stopped=historical_research_gate({},financials=True,shorts=True)
    assert stopped["status"]=="STOPPED" and len(stopped["unverified_requirements"])==6
    proof={name:"verified" for name in stopped["unverified_requirements"]}
    assert historical_research_gate(proof,financials=True,shorts=True)["status"]=="READY"
    proof["delisting_outcomes"]="unknown"
    assert historical_research_gate(proof)["status"]=="STOPPED"
