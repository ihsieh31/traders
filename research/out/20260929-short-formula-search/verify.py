"""Independent scalar audit from raw SEC TSV and price CSV, no selector import."""
from collections import defaultdict
import csv
from datetime import date
from decimal import Decimal
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import socket
import statistics as S
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from research.src import common as C


def deny(*a, **k):
    raise RuntimeError("independent audit permits saved sources only")


socket.socket.connect = deny
socket.create_connection = deny


def number(s):
    return None if s == "" else float(s)


def close(a, b):
    assert (a is None and b is None) or (a is not None and b is not None and math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10)), (a, b)


def financial_audit(records, used):
    wanted = defaultdict(lambda: defaultdict(list))
    for adsh in used:
        r = records[adsh]
        for tag, traces in r["fact_sources"].items():
            for trace in traces:
                wanted[trace["archive"]][trace["line"]].append((r, tag, trace))
    checked = 0
    for archive, lines in sorted(wanted.items()):
        p = ROOT / "research/data/sec-mscore" / archive
        with p.open("rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
        meta_ids = {r["adsh"]: r for refs in lines.values() for r, _, _ in refs}
        checked_meta = set()
        with zipfile.ZipFile(p) as z:
            with z.open("sub.txt") as fh:
                for raw in csv.DictReader(io.TextIOWrapper(fh), delimiter="\t"):
                    if raw["adsh"] in meta_ids:
                        assert all(raw[k] == meta_ids[raw["adsh"]][k] for k in ("cik", "period", "accepted", "form", "instance"))
                        checked_meta.add(raw["adsh"])
            assert checked_meta == set(meta_ids)
            found = set()
            with z.open("num.txt") as fh:
                for line, raw in enumerate(csv.DictReader(io.TextIOWrapper(fh), delimiter="\t"), 2):
                    if line not in lines:
                        continue
                    for r, tag, trace in lines[line]:
                        assert digest == trace["sha256"] and raw["adsh"] == r["adsh"] and raw["tag"] == tag
                        assert raw["ddate"] == r["period"] == trace["date"] and "20150101" <= raw["ddate"] <= "20201231"
                        assert raw["qtrs"] == ("0" if tag == "Assets" else "4") == trace["qtrs"]
                        assert raw["version"].startswith("us-gaap/") and raw["uom"] == "USD" and not raw["segments"] and not raw["coreg"]
                        close(float(raw["value"]), r["facts"][tag])
                        checked += 1
                    found.add(line)
                    if found == set(lines):
                        break
            assert found == set(lines)
        print("SEC source verified", archive, len(meta_ids), "filings", flush=True)
    max_ratio = 0.
    for adsh in used:
        r = records[adsh]
        a, ni, cfo = (Decimal(str(r["facts"][k])) for k in ("Assets", "NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities"))
        assert a > 0
        for expected, actual in ((float((ni-cfo)/a), r["ac"]), (float(-cfo/a), r["cb"])):
            close(expected, actual)
            max_ratio = max(max_ratio, abs(expected-actual))
    return dict(filings=len(used), quarters=len(wanted), source_references=checked, max_ratio_error=max_ratio)


def percentile(values):
    ordered = sorted(values)
    answer = {}
    i = 0
    while i < len(ordered):
        j = i+1
        while j < len(ordered) and ordered[j] == ordered[i]:
            j += 1
        answer[ordered[i]] = .5 if len(ordered) == 1 else (i+j-1)/2/(len(ordered)-1)
        i = j
    return [answer[v] for v in values]


def main():
    financials = json.loads((OUT / "financials.json").read_text())
    records = {r["adsh"]: r for r in financials}
    universe = list(csv.DictReader((OUT / "universe.csv").open()))
    picks = list(csv.DictReader((OUT / "selections.csv").open()))
    periods = list(csv.DictReader((OUT / "periods.csv").open()))
    source = financial_audit(records, {r["adsh"] for r in universe})
    by_day, by_pick = defaultdict(list), defaultdict(list)
    for r in universe:
        by_day[r["date"]].append(r)
        actual = records[r["adsh"]]
        assert actual["status"] == "complete" and actual["accepted"][:10] < r["date"]
        period_day = date.fromisoformat(f'{actual["period"][:4]}-{actual["period"][4:6]}-{actual["period"][6:8]}')
        assert 0 <= (date.fromisoformat(r["date"])-period_day).days <= 500
        close(float(r["ac"]), actual["ac"])
        close(float(r["cb"]), actual["cb"])
    for day, rows in by_day.items():
        latest = {}
        for r in sorted((r for r in financials if r["accepted"][:10] < day), key=lambda r: (r["period"], r["accepted"], r["adsh"])):
            latest[r["cik"]] = r["adsh"]
        assert len({r["cik"] for r in rows}) == len(rows)
        assert all(latest[r["cik"]] == r["adsh"] for r in rows)
    for r in picks:
        by_pick[r["horizon"], r["model"], r["date"]].append(r)
    wanted = {r["symbol"] for r in picks} | {"SPY"}
    days = sorted(r["date"][:10] for r in json.loads(C.CALENDAR_PATH.read_text())["rows"] if "2016-01-04" <= r["date"][:10] <= "2020-12-31")
    day_ix = {d: i for i, d in enumerate(days)}
    raw = {}
    for path in sorted(C.BARS_DIR.glob("batch_*.csv.gz")):
        with gzip.open(path, "rt", newline="") as fh:
            for r in csv.DictReader(fh):
                day, symbol = r["timestamp"][:10], r["symbol"]
                if day not in day_ix or symbol not in wanted:
                    continue
                values = {k: float(r[k]) for k in ("open", "high", "low", "close", "volume")}
                if (symbol, day) in raw:
                    assert values == raw[symbol, day]
                raw[symbol, day] = values
    factors = {}
    factor_error = factor_relative_error = pnl_error = 0.
    path_bars = set()
    for p in picks:
        key = p["symbol"], p["date"]
        if key in factors:
            continue
        t = day_ix[p["date"]]
        bars = [raw[p["symbol"], day] for day in days[t-60:t+1]]
        market = [raw["SPY", day]["close"] for day in days[t-60:t+1]]
        assert all(b["low"] > 0 and b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"] and b["volume"] >= 0 for b in bars)
        c = [b["close"] for b in bars]
        ret = [b/a-1 for a, b in zip(c, c[1:])]
        mr = [b/a-1 for a, b in zip(market, market[1:])]
        mean_r, mean_m = S.mean(ret), S.mean(mr)
        beta = math.fsum((x-mean_r)*(y-mean_m) for x, y in zip(ret, mr))/math.fsum((y-mean_m)**2 for y in mr)
        r5 = c[-1]/c[-6]-1
        f = dict(adv20=S.mean(b["close"]*b["volume"] for b in bars[-20:]), r20=c[-1]/c[-21]-1, r60=c[-1]/c[0]-1,
            vol20=S.stdev(ret[-20:])*math.sqrt(252), volume_ratio=S.mean(b["volume"] for b in bars[-5:])/S.mean(b["volume"] for b in bars[-20:]),
            trend=c[-1]/S.mean(c[-20:])-1, r5=r5, beta=beta, e5=r5-beta*(market[-1]/market[-6]-1))
        assert c[-1] >= 5 and f["adv20"] >= 20e6
        u = next(u for u in by_day[p["date"]] if u["symbol"] == p["symbol"])
        assert u["adsh"] == p["adsh"]
        for k, value in f.items():
            close(value, float(u[k]))
            factor_error = max(factor_error, abs(value-float(u[k])))
            factor_relative_error = max(factor_relative_error, abs(value-float(u[k]))/max(abs(value), 1))
        factors[key] = f
    for period in periods:
        horizon, model, day = int(period["horizon"]), period["model"], period["date"]
        us = by_day[day]
        ps = by_pick[str(horizon), model, day]
        ranks = {k: percentile([float(u[k]) for u in us]) for k in ("adv20", "r20", "r60", "vol20", "volume_ratio")}
        expected = []
        for i, u in enumerate(us):
            ac, cb, r5, e5, trend, r20 = (float(u[k]) for k in ("ac", "cb", "r5", "e5", "trend", "r20"))
            weak = trend < 0 and r20 < 0
            if model == "price_weak":
                keep, score = weak, 100*(.20*ranks["adv20"][i]+.25*(1-ranks["r20"][i])+.25*(1-ranks["r60"][i])+.15*(1-ranks["vol20"][i])+.15*ranks["volume_ratio"][i])
            elif model == "accrual_weak": keep, score = weak and ac > 0, ac
            elif model == "cashburn_weak": keep, score = weak and cb > 0, cb
            elif model == "cashburn_rebound": keep, score = weak and cb > 0 and r5 > 0, r5
            else:
                assert model == "residual_rally"
                keep, score = r5 > 0 and e5 > 0 and math.isfinite(e5), e5
            if keep:
                expected.append((score, float(u["adv20"]), u["symbol"]))
        expected.sort(key=lambda x: (-x[0], -x[1], x[2]))
        assert len(expected) == int(period["pool_n"])
        assert [p["symbol"] for p in ps] == [x[2] for x in expected[:20]]
        assert len(ps) == int(period["n"]) <= 20 and len({p["symbol"] for p in ps}) == len(ps)
        single, adverse, unknown_end, unknown_path = [], [], 0, 0
        t = day_ix[day]
        for rank, p in enumerate(ps, 1):
            assert int(p["rank"]) == rank
            close(float(p["score"]), expected[rank-1][0])
            forward = [raw.get((p["symbol"], d), {}) for d in days[t+1:t+horizon+1]]
            entry, exit_ = forward[0].get("open", math.nan), forward[-1].get("close", math.nan)
            known = math.isfinite(entry) and entry > 0 and math.isfinite(exit_) and exit_ >= 0
            known_path = math.isfinite(entry) and entry > 0 and all(math.isfinite(b.get("high", math.nan)) and b["high"] > 0 for b in forward)
            value = (entry-exit_)/entry if known else None
            single.append(value)
            close(value, number(p["single_gross"]))
            if known:
                pnl_error = max(pnl_error, abs(value-float(p["single_gross"])))
            unknown_end += not known
            unknown_path += not known_path
            adverse.append(max(b["high"] for b in forward)/entry-1 if known_path else None)
            for d, b in zip(days[t+1:t+horizon+1], forward):
                if b:
                    assert b["low"] > 0 and b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
                    path_bars.add((p["symbol"], d))
        gross = math.fsum(single)/20 if all(v is not None for v in single) else None
        calendar_days = (date.fromisoformat(days[t+horizon])-date.fromisoformat(days[t+1])).days+1
        assert int(period["calendar_days"]) == calendar_days
        close(gross, number(period["gross"]))
        for rt in (.001, .002):
            for fee in (0, .05, .20):
                value = None if gross is None else gross-len(ps)/20*(rt+fee*calendar_days/365)
                close(value, number(period[f"net_rt{int(rt*10000)}bp_borrow{int(fee*100)}pct"]))
        for p, value in zip(ps, single):
            close(None if value is None else value-.001-.05*calendar_days/365, number(p["single_net5"]))
        for k, n in dict(unknown_end=unknown_end, unknown_path=unknown_path,
                high_up10=sum(v is not None and v >= .10 for v in adverse), high_up20=sum(v is not None and v >= .20 for v in adverse),
                end_down10=sum(v is not None and v >= .10 for v in single), winning_names=sum(v is not None and v > 0 for v in single)).items():
            assert int(period[k]) == n, (day, model, k, n, period[k])
    summary = json.loads((OUT / "summary.json").read_text())
    for horizon, item in summary["horizons"].items():
        for model, m in item["models"].items():
            rs = [r for r in periods if r["horizon"] == horizon and r["model"] == model]
            active = [r for r in rs if int(r["n"]) and number(r["gross"]) is not None]
            close(S.mean(float(r["net_rt10bp_borrow5pct"])*20/int(r["n"]) for r in active), m["per_invested_net5"]["mean"])
            assert sum(int(r["n"]) for r in rs) == m["total_name_observations"]
    assert summary["method_sha256"] == hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest()
    assert summary["financials_sha256"] == hashlib.sha256((OUT / "financials.json").read_bytes()).hexdigest()
    result = dict(financial_sources=source, universe_rows=len(universe), periods=len(periods), observations=len(picks),
        unique_signal_factors=len(factors), unique_forward_bars=len(path_bars), max_factor_absolute_error=factor_error,
        max_factor_scaled_error=factor_relative_error,
        max_single_pnl_error=pnl_error, filing_timing_and_latest_missing_rejection=True, independent_pool_ranking=True,
        all_cost_scenarios_and_high_events=True, summary_means_match=True, method_sha256=summary["method_sha256"])
    (OUT / "verification.json").write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
