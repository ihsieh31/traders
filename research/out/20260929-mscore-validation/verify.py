"""Independent scalar audit; no imports from the preparation or selection scripts."""
import csv
import gzip
import hashlib
import io
import json
import math
import socket
import sys
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from research.src import common as C


def deny(*args, **kwargs):
    raise RuntimeError("verification uses only saved sources")


socket.socket.connect = deny
socket.create_connection = deny


def ratios_from_trace(r):
    d, p, f = r["period"], r["previous_period"], r["facts"]

    def pair(tags, q):
        for tag in tags:
            keys = [f"{tag}|{day}|{q}" for day in (d, p)]
            if all(k in f for k in keys):
                return tuple(Decimal(str(f[k])) for k in keys)
        raise AssertionError((r["adsh"], tags))

    sales, sales0 = pair(("SalesRevenueNet", "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueGoodsNet"), 4)
    ar, ar0 = pair(("AccountsReceivableNetCurrent", "AccountsReceivableNet"), 0)
    assets, assets0 = pair(("Assets",), 0)
    ca, ca0 = pair(("AssetsCurrent",), 0)
    ppe, ppe0 = pair(("PropertyPlantAndEquipmentNet",), 0)
    dep, dep0 = pair(("Depreciation",), 4)
    sga, sga0 = pair(("SellingGeneralAndAdministrativeExpense",), 4)
    cl, cl0 = pair(("LiabilitiesCurrent",), 0)
    try:
        gp, gp0 = pair(("GrossProfit",), 4)
    except AssertionError:
        cost, cost0 = pair(("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"), 4)
        gp, gp0 = sales-cost, sales0-cost0
    try:
        debt, debt0 = pair(("LongTermDebtNoncurrent",), 0)
    except AssertionError:
        total, total0 = pair(("LongTermDebt",), 0)
        short, short0 = pair(("LongTermDebtCurrent",), 0)
        debt, debt0 = total-short, total0-short0
    income = Decimal(str(f[f"NetIncomeLoss|{d}|4"]))
    cfo = Decimal(str(f[f"NetCashProvidedByUsedInOperatingActivities|{d}|4"]))
    extra_key = f"ExtraordinaryItemNetOfTax|{d}|4"
    if extra_key in f:
        income -= Decimal(str(f[extra_key]))
    else:
        prior_date = date.fromisoformat(f"{p[:4]}-{p[4:6]}-{p[6:]}")
        assert prior_date >= date(2015, 12, 15)
        assert r["earnings_method"] == "NI_after_ASU2015_01_effective_year"
    values = dict(DSRI=ar*sales0/(sales*ar0), GMI=gp0*sales/(sales0*gp),
                  AQI=((assets-ca-ppe)/assets)/((assets0-ca0-ppe0)/assets0),
                  SGI=sales/sales0, DEPI=(dep0/(dep0+ppe0))/(dep/(dep+ppe)),
                  SGAI=sga*sales0/(sales*sga0), TATA=(income-cfo)/assets,
                  LVGI=((cl+debt)/assets)/((cl0+debt0)/assets0))
    weights = dict(DSRI=".920", GMI=".528", AQI=".404", SGI=".892",
                   DEPI=".115", SGAI="-.172", TATA="4.679", LVGI="-.327")
    score = Decimal("-4.84") + sum(Decimal(weights[k])*values[k] for k in weights)
    return values, score


def verify_raw_financials(records):
    """Main strategy's three accessions: direct TSV reader against retained traces."""
    found = {r["adsh"]: {} for r in records}
    quarters = sorted({r["source_quarter"] for r in records})
    for quarter in quarters:
        wanted = {r["adsh"]: r for r in records if r["source_quarter"] == quarter}
        with zipfile.ZipFile(ROOT / "research/data/sec-mscore" / f"{quarter}.zip") as z:
            with z.open("sub.txt") as fh:
                for row in csv.DictReader(io.TextIOWrapper(fh), delimiter="\t"):
                    if row["adsh"] in wanted:
                        for key in ("cik", "period", "accepted", "form", "instance"):
                            assert row[key] == wanted[row["adsh"]][key]
            with z.open("num.txt") as fh:
                reader = csv.reader(io.TextIOWrapper(fh), delimiter="\t")
                cols = {k: i for i, k in enumerate(next(reader))}
                for row in reader:
                    adsh = row[cols["adsh"]]
                    if adsh not in wanted:
                        continue
                    key = "|".join(row[cols[k]] for k in ("tag", "ddate", "qtrs"))
                    if key not in wanted[adsh]["facts"]:
                        continue
                    if (row[cols["uom"]] != "USD" or row[cols["segments"]] or row[cols["coreg"]]
                            or not row[cols["version"]].startswith("us-gaap/") or not row[cols["value"]]):
                        continue
                    value = float(row[cols["value"]])
                    assert value == wanted[adsh]["facts"][key], (adsh, key, value)
                    found[adsh][key] = value
        print("raw SEC verified", quarter, flush=True)
    assert all(found[r["adsh"]] == r["facts"] for r in records)
    return dict(accessions=len(records), facts=sum(len(r["facts"]) for r in records),
                quarters=quarters, values_and_metadata_match=True)


def main():
    picks = list(csv.DictReader((OUT / "selections.csv").open()))
    periods = list(csv.DictReader((OUT / "periods.csv").open()))
    financials = {r["adsh"]: r for r in json.loads((OUT / "financials.json").read_text())}
    max_ratio_error = max_score_error = 0.0
    selected_accessions = {p["adsh"] for p in picks}
    for adsh in selected_accessions:
        r = financials[adsh]
        values, score = ratios_from_trace(r)
        for k, value in values.items():
            error = abs(float(value)-r["ratios"][k])
            max_ratio_error = max(max_ratio_error, error)
            assert math.isclose(float(value), r["ratios"][k], rel_tol=1e-10, abs_tol=1e-10)
        error = abs(float(score)-r["mscore"])
        max_score_error = max(max_score_error, error)
        assert error < 1e-10
    main_accessions = {p["adsh"] for p in picks if p["model"] == "m_weak_raw"}
    raw_financial_check = verify_raw_financials([financials[k] for k in sorted(main_accessions)])

    sessions = sorted(r["date"][:10] for r in json.loads(C.CALENDAR_PATH.read_text())["rows"]
                      if "2016-01-04" <= r["date"][:10] <= "2020-12-31")
    day_ix = {d: i for i, d in enumerate(sessions)}
    wanted = {p["symbol"] for p in picks}
    raw = {}
    for path in sorted(C.BARS_DIR.glob("batch_*.csv.gz")):
        with gzip.open(path, "rt", newline="") as fh:
            reader = csv.reader(fh)
            cols = {k: i for i, k in enumerate(next(reader))}
            for row in reader:
                symbol, day = row[cols["symbol"]], row[cols["timestamp"]][:10]
                if symbol not in wanted or day not in day_ix:
                    continue
                values = {k: float(row[cols[k]]) for k in ("open", "high", "low", "close", "volume")}
                if (symbol, day) in raw:
                    assert raw[symbol, day] == values
                raw[symbol, day] = values
    grouped = {}
    for p in picks:
        assert p["accepted"][:10] < p["date"]
        grouped.setdefault((p["model"], p["date"]), []).append(p)
    external = {}
    for symbol in {p["symbol"] for p in picks if p["model"] == "m_weak_raw"}:
        r = json.loads((OUT / f"yahoo_{symbol}.json").read_text())
        q = r["indicators"]["quote"][0]
        for i, stamp in enumerate(r["timestamp"]):
            day = datetime.fromtimestamp(stamp, ZoneInfo("America/New_York")).date().isoformat()
            if day not in day_ix:
                continue
            external[symbol, day] = {k: q[k][i] for k in ("open", "high", "low", "close")}
    max_pnl_error = 0.0
    external_rows, checked_paths = [], set()
    for period in periods:
        selected = grouped.get((period["model"], period["date"]), [])
        assert len(selected) == int(period["n"]) <= 20
        assert len({p["cik"] for p in selected}) == len(selected)
        t = day_ix[period["date"]]
        dates = sessions[t+1:t+21]
        returns, up10, up20, down10, win = [], 0, 0, 0, 0
        for p in selected:
            current = financials[p["adsh"]]
            eligible_filings = [r for r in financials.values() if r["cik"] == p["cik"]
                                and r["accepted"][:10] < p["date"]]
            latest = max(eligible_filings, key=lambda r: (r["period"], r["accepted"], r["adsh"]))
            assert latest["adsh"] == p["adsh"] and latest["status"] == "complete"
            period_end = date.fromisoformat(f'{current["period"][:4]}-{current["period"][4:6]}-{current["period"][6:]}')
            assert 0 <= (date.fromisoformat(p["date"])-period_end).days <= 500
            history = [raw[p["symbol"], d] for d in sessions[t-60:t+1]]
            assert len(history) == 61
            for b in history:
                assert all(math.isfinite(v) and v > 0 for v in b.values())
                assert b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
            close = history[-1]["close"]
            assert close >= 5 and sum(b["close"]*b["volume"] for b in history[-20:])/20 >= 20_000_000
            if p["model"] != "m_only_raw":
                assert close < sum(b["close"] for b in history[-20:])/20
                assert close < history[-21]["close"]
            if p["model"] in ("m_only_raw", "m_weak_raw"):
                assert float(p["raw_mscore"]) > -1.78
                assert float(p["raw_mscore"]) == current["mscore"]
            if p["model"] == "m_weak_winsor":
                assert float(p["score"]) > -1.78
            bars = [raw[p["symbol"], d] for d in dates]
            for d, b in zip(dates, bars):
                assert all(math.isfinite(v) and v > 0 for v in b.values())
                assert b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
                checked_paths.add((p["symbol"], d))
            entry, exit_ = bars[0]["open"], bars[-1]["close"]
            short = (entry-exit_)/entry
            returns.append(short)
            peak = max(b["high"] for b in bars)
            up10 += peak >= entry*1.1
            up20 += peak >= entry*1.2
            down10 += short >= .1
            win += short > 0
            if period["model"] == "m_weak_raw":
                xb = [external[p["symbol"], d] for d in dates]
                assert all(b[k] is not None and b[k] > 0 for b in xb for k in b)
                xe, xx = xb[0]["open"], xb[-1]["close"]
                xs = 1-xx/xe
                external_rows.append(dict(signal=period["date"], symbol=p["symbol"],
                    entry_date=dates[0], exit_date=dates[-1], entry_local=entry, exit_local=exit_,
                    entry_external=xe, exit_external=xx, short_local=short, short_external=xs,
                    return_absolute_difference=abs(xs-short),
                    direction_match=(xs > 0) == (short > 0),
                    entry_source_scale=xe/entry, exit_source_scale=xx/exit_,
                    external_up10=max(b["high"] for b in xb) >= 1.1*xe,
                    external_up20=max(b["high"] for b in xb) >= 1.2*xe))
        expected = math.fsum(returns)/20
        max_pnl_error = max(max_pnl_error, abs(expected-float(period["gross"])))
        assert abs(expected-float(period["gross"])) < 1e-12
        assert int(period["unknown_end"]) == int(period["unknown_path"]) == 0
        for key, value in (("high_up10", up10), ("high_up20", up20), ("end_down10", down10), ("winning_names", win)):
            assert value == int(period[key])
        days = (date.fromisoformat(dates[-1])-date.fromisoformat(dates[0])).days+1
        assert days == int(period["calendar_days"])
        for rt in (.001, .002):
            for fee in (0, .05, .20):
                key = f"net_rt{int(rt*10000)}bp_borrow{int(fee*100)}pct"
                assert abs(expected-len(selected)/20*(rt+fee*days/365)-float(period[key])) < 1e-12
    with (OUT / "external_price_check.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(external_rows[0]))
        w.writeheader()
        w.writerows(external_rows)
    result = dict(periods_verified=len(periods), observations_verified=len(picks),
        unique_forward_bars=len(checked_paths), independent_trace_accessions=len(selected_accessions),
        maximum_ratio_error=max_ratio_error, maximum_score_error=max_score_error,
        maximum_pnl_error=max_pnl_error, raw_main_financials=raw_financial_check,
        main_external_observations=len(external_rows),
        external_max_return_difference=max(r["return_absolute_difference"] for r in external_rows),
        external_direction_matches=sum(r["direction_match"] for r in external_rows),
        external_gross_mean=sum(r["short_external"] for r in external_rows)/len(external_rows),
        local_gross_mean=sum(r["short_local"] for r in external_rows)/len(external_rows),
        method_sha256=hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest(),
        checks="All arithmetic, cost scenarios, filing timing and OHLC assertions passed. External differences reported, not forced to match.")
    (OUT / "verification.json").write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
