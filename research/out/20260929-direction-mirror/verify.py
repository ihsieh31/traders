"""Independent stdlib checks of saved quotes, ranks, portfolio arithmetic."""
import csv
from collections import defaultdict
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import statistics

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
PREV = OUT.parent / "20260929-short-formula-search"


def read(path):
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def main():
    summary = json.loads((OUT / "summary.json").read_text())
    for name, digest in summary["output_sha256"].items():
        assert hashlib.sha256((OUT/name).read_bytes()).hexdigest() == digest
    for name, digest in summary["inputs"].items():
        assert hashlib.sha256((PREV/name).read_bytes()).hexdigest() == digest
    u = defaultdict(list)
    for row in read(PREV / "universe.csv"):
        u[row["date"]].append(row)
    selected = defaultdict(list)
    for row in read(OUT / "long_selections.csv"):
        selected[row["date"]].append(row)
    for day, universe in u.items():
        survivors = [r for r in universe if 0<=float(r["vol20"])<=.28 and float(r["trend"])>0 and float(r["r60"])>=-.25]
        def percentile(key, value):
            values = [float(r[key]) for r in survivors]
            return .5 if len(values)==1 else (sum(v<value for v in values)+(sum(v==value for v in values)-1)/2)/(len(values)-1)
        scores = {r["symbol"]: 100*(.5*percentile("trend",float(r["trend"]))+.3*percentile("r60",float(r["r60"]))+.2*(1-percentile("vol20",float(r["vol20"])))) for r in survivors}
        expected = sorted(scores, key=lambda s:(-scores[s],s))[:20]
        actual = sorted(selected[day],key=lambda r:int(r["rank"]))
        assert [r["symbol"] for r in actual] == expected
        for row in actual:
            assert abs(float(row["exclusion_score"])-scores[row["symbol"]])<1e-12
    prices = {}
    for row in read(OUT / "used_prices.csv"):
        assert "2016-01-04"<=row["date"]<="2020-12-31"
        key = row["date"], row["symbol"]
        assert key not in prices
        prices[key] = float(row["open"]), float(row["close"])
    calendar = json.loads((ROOT / "research/data/calendar.json").read_text())["rows"]
    sessions = sorted(r["date"][:10] for r in calendar if "2016-01-04"<=r["date"][:10]<="2020-12-31")
    picks = defaultdict(list)
    for r in read(PREV / "selections.csv"):
        picks[r["date"],int(r["horizon"]),r["model"]].append(r)
    rows = read(OUT / "periods.csv")
    error = 0
    for row in rows:
        day,h,model = row["date"],int(row["horizon"]),row["model"]
        t = sessions.index(day)
        entry,exit_ = sessions[t+1],sessions[t+h]
        days = (date.fromisoformat(exit_)-date.fromisoformat(entry)).days+1
        assert int(row["calendar_days"]) == days
        if model=="SPY":
            symbols=["SPY"]
        elif model=="equal_weight_U":
            symbols=[r["symbol"] for r in u[day]]
        elif model=="exclusion_long_same_U":
            symbols=[r["symbol"] for r in selected[day]]
        else:
            symbols=[r["symbol"] for r in picks[day,h,model]]
        assert len(symbols)==int(row["n"])
        returns=[prices[exit_,s][1]/prices[entry,s][0]-1 for s in symbols]
        gross=statistics.mean(returns)
        long,short=gross-.001,-gross-.001-.05*days/365
        for key,value in (("long_gross",gross),("short_gross",-gross),("long_net10bp",long),("short_net10bp_borrow5pct",short)):
            e=abs(float(row[key])-value)
            error=max(error,e)
            assert e<1e-12,(row,key,value)
    joint = read(OUT / "joint_periods.csv")
    for r in joint:
        day,h=r["date"],int(r["horizon"])
        t=sessions.index(day)
        entry,exit_=sessions[t+1],sessions[t+h]
        days=(date.fromisoformat(exit_)-date.fromisoformat(entry)).days+1
        target={p["symbol"] for p in picks[day,h,r["model"]] if int(p["rank"])<=10}
        old_long={p["symbol"] for p in selected[day] if int(p["rank"])<=10}
        overlap=target&old_long
        target-=overlap
        old_long-=overlap
        assert len(target)+len(old_long)<=20 and not target&old_long
        assert (len(target),len(old_long),len(overlap))==(int(r["long_after_swap"]),int(r["short_after_swap"]),int(r["cancelled_overlap"]))
        def pnl(symbol,direction):
            return direction*(prices[exit_,symbol][1]/prices[entry,symbol][0]-1)-.001-(.05*days/365 if direction<0 else 0)
        swap=(sum(pnl(s,1) for s in target)+sum(pnl(s,-1) for s in old_long))/20
        original=(sum(pnl(s,-1) for s in target)+sum(pnl(s,1) for s in old_long))/20
        assert abs(swap-float(r["swapped_net"]))<1e-12
        assert abs(original-float(r["original_net"]))<1e-12
    for h,models in summary["horizons"].items():
        for model,stats in models.items():
            rr=[r for r in rows if r["horizon"]==h and r["model"]==model]
            for key,value in stats.items():
                if isinstance(value,dict) and "mean" in value:
                    assert math.isclose(statistics.mean(float(r[key]) for r in rr),value["mean"],abs_tol=1e-12)
    for h,models in summary["joint_10_plus_10"].items():
        for model,stats in models.items():
            rr=[r for r in joint if r["horizon"]==h and r["model"]==model]
            for key in ("original_net","swapped_net","swapped_minus_original"):
                assert math.isclose(statistics.mean(float(r[key]) for r in rr),stats[key]["mean"],abs_tol=1e-12)
    evidence=dict(source_and_output_hashes_match=True, authorized_quote_rows=len(prices),
        independent_production_rank_dates=len(u), period_rows=len(rows), joint_rows=len(joint),
        max_period_absolute_pnl_error=error, all_summary_means_match=True, joint_unique_budget_20=True,
        production_code_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
          for name in ("tradingagents/screening/metrics.py","tradingagents/screening/policy.py")})
    (OUT / "verification.json").write_text(json.dumps(evidence,indent=2)+"\n")
    print(json.dumps(evidence,indent=2))


if __name__=="__main__":
    main()
