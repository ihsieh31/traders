"""Post-hoc direction check. Numeric prices strictly limited to 2016-2020."""
import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PREV = OUT.parent / "20260929-short-formula-search"
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("previous_short", OUT.parent / "20260929-short-screen/compare.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
from tradingagents.screening.metrics import SymbolFeatures, score_exclusion_candidates
from tradingagents.screening.policy import ExclusionThresholds


def read_csv(path):
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, rows):
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def returns(gross_short, days, rt=.001, fee=.05):
    return -gross_short-rt, gross_short-rt-fee*days/365


def checks():
    for price in (90, 110, 250):
        short = 1-price/100
        long, net_short = returns(short, 28)
        assert abs((long+net_short)-(-.002-.05*28/365)) < 1e-12
        assert abs(long-(-short-.001)) < 1e-12
    assert returns(-1.5, 28)[0] == 1.499
    return {"gross_inverse": True, "costs_do_not_reverse": True, "unbounded_short_loss": True}


def main():
    checked = checks()
    periods = read_csv(PREV / "periods.csv")
    picks = read_csv(PREV / "selections.csv")
    universe = read_csv(PREV / "universe.csv")
    by_date = defaultdict(list)
    for row in universe:
        assert "2016-01-04" <= row["date"] <= "2020-12-31"
        by_date[row["date"]].append(row)
    dates = sorted(by_date)
    calendar = json.loads(A.C.CALENDAR_PATH.read_text())["rows"]
    sessions = sorted(r["date"][:10] for r in calendar if "2016-01-04" <= r["date"][:10] <= "2020-12-31")
    timing = {}
    needed = set()
    for day in dates:
        t = sessions.index(day)
        for horizon in (5, 20):
            entry, exit_ = sessions[t+1], sessions[t+horizon]
            timing[day, horizon] = (entry, exit_, (date.fromisoformat(exit_)-date.fromisoformat(entry)).days+1)
            for symbol in ["SPY"]+[r["symbol"] for r in by_date[day]]:
                needed.update(((day, symbol), (entry, symbol), (exit_, symbol)))
    prices = {}
    files = sorted(A.C.BARS_DIR.glob("batch_*.csv.gz"))
    for k, path in enumerate(files):
        with gzip.open(path, "rt", newline="") as fh:
            reader = csv.reader(fh)
            columns = {name: i for i, name in enumerate(next(reader))}
            for raw in reader:
                stamp = raw[columns["timestamp"]][:10]
                if not "2016-01-04" <= stamp <= "2020-12-31":
                    continue
                key = stamp, raw[columns["symbol"]]
                if key not in needed:
                    continue
                # Only requested, authorized-date price numbers are parsed.
                values = float(raw[columns["open"]]), float(raw[columns["close"]])
                if key in prices:
                    assert prices[key] == values
                prices[key] = values
        if (k+1) % 40 == 0:
            print("cache batches", k+1, "/", len(files), "requested quotes", len(prices), flush=True)
    quote_rows = [dict(date=d, symbol=s, open=v[0], close=v[1]) for (d, s), v in sorted(prices.items())]
    write_csv(OUT / "used_prices.csv", quote_rows)
    lookup = {(r["date"], r["horizon"], r["model"]): r for r in periods}
    groups = defaultdict(list)
    selected_long = {}
    for day, values in by_date.items():
        features = [SymbolFeatures(symbol=r["symbol"], price=prices[day, r["symbol"]][1],
                    **{key: float(r[key]) for key in ("adv20", "r5", "r20", "r60", "vol20", "volume_ratio", "trend")}) for r in values]
        selected_long[day] = score_exclusion_candidates(features, ExclusionThresholds())[:20]
    price_checks = 0
    for p in picks:
        day, horizon, symbol = p["date"], int(p["horizon"]), p["symbol"]
        entry, exit_, _ = timing[day, horizon]
        s = Decimal(1)-Decimal(str(prices[exit_, symbol][1]))/Decimal(str(prices[entry, symbol][0]))
        assert abs(float(s)-float(p["single_gross"])) < 1e-12
        groups[day, horizon, p["model"]].append((symbol, float(s)))
        price_checks += 1
    for day in dates:
        for horizon in (5, 20):
            entry, exit_, _ = timing[day, horizon]
            for model, symbols in (("exclusion_long_same_U", [f.symbol for f in selected_long[day]]),
                                   ("equal_weight_U", [r["symbol"] for r in by_date[day]]), ("SPY", ["SPY"])):
                for symbol in symbols:
                    e, x = prices.get((entry, symbol)), prices.get((exit_, symbol))
                    assert e and x and e[0] > 0 and x[1] >= 0, (day, horizon, model, symbol, "unknown quote")
                    groups[day, horizon, model].append((symbol, 1-x[1]/e[0]))
    rows = []
    all_models = sorted({key[2] for key in groups})
    for day in dates:
        for horizon in (5, 20):
            _, _, days = timing[day, horizon]
            for model in all_models:
                pairs = groups[day, horizon, model]
                n = len(pairs)
                if n == 0:
                    continue
                short_gross = sum(v for _, v in pairs)/n
                if (day, str(horizon), model) in lookup:
                    old = lookup[day, str(horizon), model]
                    assert int(old["n"]) == n
                    assert abs(short_gross-float(old["gross"])*20/n) < 1e-12
                    assert days == int(old["calendar_days"])
                long, short = returns(short_gross, days)
                spy = groups[day, horizon, "SPY"][0][1]
                same_U = sum(v for _, v in groups[day, horizon, "equal_weight_U"])/len(groups[day, horizon, "equal_weight_U"])
                fraction = 1 if model in ("SPY", "equal_weight_U") else n/20
                rows.append(dict(date=day, year=int(day[:4]), horizon=horizon, model=model, n=n, calendar_days=days,
                    long_gross=-short_gross, short_gross=short_gross, long_net10bp=long, long_net20bp=-short_gross-.002,
                    short_net10bp_borrow5pct=short, long_vs_SPY_gross=-short_gross+spy,
                    long_vs_equal_U_gross=-short_gross+same_U,
                    long_cash_net10bp=long*fraction, short_cash_net10bp_borrow5pct=short*fraction))
    write_csv(OUT / "periods.csv", rows)
    write_csv(OUT / "long_selections.csv", [dict(date=d, rank=i, symbol=f.symbol, exclusion_score=f.exclusion_score)
              for d in dates for i, f in enumerate(selected_long[d], 1)])
    result = dict(status="post-hoc exploratory direction inversion; no independent holdout", checks=checked,
        independently_repriced_previous_observations=price_checks, signal_dates=len(dates), requested_quotes=len(needed), found_quotes=len(prices),
        inputs={name: hashlib.sha256((PREV/name).read_bytes()).hexdigest() for name in ("periods.csv", "selections.csv", "universe.csv", "summary.json")}, horizons={})
    # Formula-only paired portfolios: at most ten names on each side, 1/20
    # capital per name. Opposite positions in the same symbol cancel before costs.
    joint_rows = []
    for day in dates:
        for horizon in (20, 5):
            _, _, days = timing[day, horizon]
            borrow = .05*days/365
            for model in ("price_weak", "accrual_weak", "cashburn_weak", "cashburn_rebound", "residual_rally"):
                target = {r["symbol"]: -float(r["single_gross"]) for r in picks
                          if r["date"] == day and int(r["horizon"]) == horizon and r["model"] == model and int(r["rank"]) <= 10}
                entry, exit_, _ = timing[day, horizon]
                old_long = {f.symbol: prices[exit_, f.symbol][1]/prices[entry, f.symbol][0]-1 for f in selected_long[day][:10]}
                overlap = set(target)&set(old_long)
                for symbol in overlap:
                    target.pop(symbol)
                    old_long.pop(symbol)
                assert len(target)+len(old_long) <= 20 and not set(target)&set(old_long)
                swapped = (sum(r-.001 for r in target.values())+sum(-r-.001-borrow for r in old_long.values()))/20
                original = (sum(-r-.001-borrow for r in target.values())+sum(r-.001 for r in old_long.values()))/20
                assert abs(swapped+original+(len(target)+len(old_long))*(.002+borrow)/20) < 1e-12
                joint_rows.append(dict(date=day, year=int(day[:4]), horizon=horizon, model=model,
                    long_after_swap=len(target), short_after_swap=len(old_long), cancelled_overlap=len(overlap),
                    original_net=original, swapped_net=swapped, swapped_minus_original=swapped-original))
    write_csv(OUT / "joint_periods.csv", joint_rows)
    result["joint_10_plus_10"] = {}
    for horizon in (20, 5):
        result["joint_10_plus_10"][str(horizon)] = {}
        for model in ("price_weak", "accrual_weak", "cashburn_weak", "cashburn_rebound", "residual_rally"):
            rr = [r for r in joint_rows if r["horizon"] == horizon and r["model"] == model]
            result["joint_10_plus_10"][str(horizon)][model] = {
                **{key: A.bootstrap([r[key] for r in rr]) for key in ("original_net", "swapped_net", "swapped_minus_original")},
                "groups": {label: A.bootstrap([r["swapped_net"] for r in rr if pred(r["year"])])
                           for label, pred in (("2016_2018", lambda y:y<=2018), ("2019_2020", lambda y:y>=2019), ("exclude2020", lambda y:y<2020))}}
    result["robustness"] = {}
    for horizon in (20, 5):
        result["robustness"][str(horizon)] = {}
        for model in ("accrual_weak", "cashburn_weak", "cashburn_rebound", "residual_rally"):
            pp = [r for r in picks if r["model"] == model and int(r["horizon"]) == horizon]
            totals = defaultdict(float)
            for p in pp:
                totals[p["symbol"]] += -float(p["single_gross"])-.001
            best = max(totals, key=totals.get)
            item = {"best_sum_observation_contributor": best}
            for label, exclusions, excluded_date in (("without_best_symbol", {best}, None),
                 ("without_W_CVNA", {"W", "CVNA"}, None), ("without_March2020_signal", set(), "2020-03-20")):
                by_day = defaultdict(list)
                for p in pp:
                    if p["symbol"] not in exclusions and p["date"] != excluded_date:
                        by_day[p["date"]].append(-float(p["single_gross"])-.001)
                item[label] = A.bootstrap([sum(v)/len(v) for v in by_day.values()])
            result["robustness"][str(horizon)][model] = item
    keys = ["long_gross", "short_gross", "long_net10bp", "long_net20bp", "short_net10bp_borrow5pct", "long_vs_SPY_gross", "long_vs_equal_U_gross", "long_cash_net10bp", "short_cash_net10bp_borrow5pct"]
    for h in (20, 5):
        result["horizons"][str(h)] = {}
        for model in all_models:
            rr = [r for r in rows if r["horizon"] == h and r["model"] == model]
            stat = {key: A.bootstrap([r[key] for r in rr]) for key in keys}
            stat["groups"] = {label: {key: A.bootstrap([r[key] for r in rr if pred(r["year"])]) for key in keys}
                              for label, pred in (("2016_2018", lambda y: y<=2018), ("2019_2020", lambda y:y>=2019), ("exclude2020", lambda y:y<2020))}
            stat["active_periods"] = len(rr)
            stat["full20_periods"] = sum(r["n"] == 20 for r in rr)
            result["horizons"][str(h)][model] = stat
            print(h, model, "long net", stat["long_net10bp"], "long-SPY", stat["long_vs_SPY_gross"], flush=True)
    for key in ("METHOD.md", "check.py", "used_prices.csv", "periods.csv", "long_selections.csv", "joint_periods.csv"):
        result.setdefault("output_sha256", {})[key] = hashlib.sha256((OUT/key).read_bytes()).hexdigest()
    (OUT / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")


if __name__ == "__main__":
    main()
