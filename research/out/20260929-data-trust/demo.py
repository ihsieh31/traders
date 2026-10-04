"""Synthetic production scan/cache/frozen-path example; no LLM or broker calls."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/"tests")]
from test_phase_c_screening import _base_config,_universe,_healthy_bars,_bars_df,_deps,_AS_OF,_NOW
from tradingagents.screening.pipeline import prepare_screening_round,prepare_screening_round_from_selection
from tradingagents.screening.selection_store import SelectionStore,selection_rows_valid


def main():
    names=[f"OK{i:02d}" for i in range(9)]+["MISSCAP","BADOHLC","NOID","RISK","DUPA","DUPB"]
    universe=_universe(names)
    by={r["symbol"]:r for r in universe}
    by["MISSCAP"]["market_cap"]=None
    by["NOID"].pop("asset_id")
    by["DUPB"]["asset_id"]=by["DUPA"]["asset_id"]
    frames={s:_healthy_bars(start=100+i*.5) for i,s in enumerate(names)}
    frames["BADOHLC"].loc[0,"low"]=10000
    from tradingagents.dataflows.market_calendar import session_dates_ending_at
    frames["RISK"]=_bars_df(session_dates_ending_at(_AS_OF,61),[100*(.9 if i%2==0 else 1.1) for i in range(61)],1_000_000.)
    cfg=_base_config(screening_method="auto",screening_provider=None,screening_model=None,
        screening_selection_cache_path=str(OUT/"demo_cache/selection.json"))
    positions=[dict(symbol=f"HELD{i}",qty=1,asset_class="us_equity") for i in range(3)]
    llm_calls=[]
    def forbidden(*args,**kwargs):
        llm_calls.append(1)
        raise AssertionError("this deterministic demo cannot spend any LLM request")
    deps=_deps(universe,frames,positions=positions,invoke=forbidden)
    plan=prepare_screening_round(cfg,refresh=True,deps=deps,now=_NOW)
    assert not plan.stopped,plan.detail
    assert len(plan.top20)==9 and len(plan.deep_analysis_set)==12 and not llm_calls
    assert selection_rows_valid(plan.selection,cfg)
    cached=prepare_screening_round(cfg,deps=deps,now=_NOW)
    assert cached.cached and cached.deep_analysis_set==plan.deep_analysis_set
    frozen=prepare_screening_round_from_selection(cfg,plan.selection,deps=deps,session_date=str(_AS_OF))
    assert not frozen.stopped and frozen.deep_analysis_set==plan.deep_analysis_set
    assert plan.selection["data_quality"]["counts"]=={"usable":9,"data_gap":2,"data_error":3,"risk_policy":1}
    store=SelectionStore(cfg["screening_selection_cache_path"])
    result=dict(kind="synthetic; not a live-market scan",input_symbols=len(names),selected=len(plan.top20),
        held_review_symbols=3,deep_analysis_symbols=len(plan.deep_analysis_set),llm_requests=len(llm_calls),
        counts=plan.selection["data_quality"]["counts"],shortfall=plan.scan_stats["selection_shortfall"],
        cache_revalidated=True,frozen_ab_revalidated=True,
        selected_raw_price_windows=sum("window" in r.get("bars",{}) for r in plan.selection["data_quality"]["records"]),
        snapshot=str(store.snapshot_path(plan.selection)))
    (OUT/"production_path_demo.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))


if __name__=="__main__":main()
