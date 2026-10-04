"""Extract dated SEC annual facts; all values in one score share an accession."""
from __future__ import annotations
import collections
import csv
from datetime import date, timedelta
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pandas as pd

OUT = Path(__file__).resolve().parent
CACHE = ROOT / "research/data/sec-mscore"
SALES = ("SalesRevenueNet", "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueGoodsNet")
COGS = ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold")
AR = ("AccountsReceivableNetCurrent", "AccountsReceivableNet")
EXTRA = "ExtraordinaryItemNetOfTax"
TAGS = set(SALES+COGS+AR+("Assets", "AssetsCurrent", "PropertyPlantAndEquipmentNet", "Depreciation",
           "SellingGeneralAndAdministrativeExpense", "LiabilitiesCurrent", "LongTermDebtNoncurrent",
           "LongTermDebt", "LongTermDebtCurrent", "NetCashProvidedByUsedInOperatingActivities",
           "NetIncomeLoss", "GrossProfit", EXTRA))
WEIGHTS = {"DSRI": .920, "GMI": .528, "AQI": .404, "SGI": .892,
           "DEPI": .115, "SGAI": -.172, "TATA": 4.679, "LVGI": -.327}


def parse_day(s):
    return date.fromisoformat(f"{s[:4]}-{s[4:6]}-{s[6:8]}")


def mscore(ratios):
    return -4.84 + math.fsum(WEIGHTS[key]*ratios[key] for key in WEIGHTS)


def derive(meta, values):
    trace = {}
    period = meta["period"]
    current = parse_day(period)
    dates = {d for tag, d, q in values if tag in SALES and q == "4" and d < period}
    prior = sorted((d for d in dates if 350 <= (current-parse_day(d)).days <= 380),
                   key=lambda d: (abs((current-parse_day(d)).days-365), d))
    if not prior:
        raise ValueError("no_comparable_previous_year")
    previous = prior[0]

    def get(tag, day, q):
        value = values.get((tag, day, str(q)))
        if value is None or not math.isfinite(value):
            raise ValueError("missing_or_conflicting:"+tag)
        trace[f"{tag}|{day}|{q}"] = value
        return value

    def pair(tags, q):
        for tag in tags:
            if all(values.get((tag, d, str(q))) is not None for d in (period, previous)):
                return get(tag, period, q), get(tag, previous, q)
        raise ValueError("missing_pair:"+"/".join(tags))

    sales, sales0 = pair(SALES, 4)
    ar, ar0 = pair(AR, 0)
    assets, assets0 = pair(("Assets",), 0)
    ca, ca0 = pair(("AssetsCurrent",), 0)
    ppe, ppe0 = pair(("PropertyPlantAndEquipmentNet",), 0)
    dep, dep0 = pair(("Depreciation",), 4)
    sga, sga0 = pair(("SellingGeneralAndAdministrativeExpense",), 4)
    cl, cl0 = pair(("LiabilitiesCurrent",), 0)
    try:
        gp, gp0 = pair(("GrossProfit",), 4)
    except ValueError:
        cost, cost0 = pair(COGS, 4)
        gp, gp0 = sales-cost, sales0-cost0
    try:
        debt, debt0 = pair(("LongTermDebtNoncurrent",), 0)
    except ValueError:
        total, total0 = pair(("LongTermDebt",), 0)
        short, short0 = pair(("LongTermDebtCurrent",), 0)
        debt, debt0 = total-short, total0-short0
    income = get("NetIncomeLoss", period, 4)
    cfo = get("NetCashProvidedByUsedInOperatingActivities", period, 4)
    extra = values.get((EXTRA, period, "4"))
    fiscal_start = parse_day(previous)+timedelta(days=1)
    if (EXTRA, period, "4") in values and extra is None:
        raise ValueError("conflicting_extraordinary")
    if extra is not None:
        if fiscal_start > date(2015, 12, 15) and extra != 0:
            raise ValueError("extraordinary_after_elimination")
        income -= get(EXTRA, period, 4)
        earnings_method = "NI_minus_reported_extraordinary"
    elif fiscal_start > date(2015, 12, 15):
        earnings_method = "NI_after_ASU2015_01_effective_year"
    else:
        raise ValueError("extraordinary_status_unknown_before_effective_year")
    if min(sales, sales0, assets, assets0, dep, dep0, ar0, sga0, gp, gp0, cl0+debt0) <= 0:
        raise ValueError("nonpositive_ratio_denominator")
    if min(ar, sga, ca, ca0, ppe, ppe0, cl, cl0, debt, debt0) < 0:
        raise ValueError("negative_accounting_balance")
    aq, aq0 = 1-(ca+ppe)/assets, 1-(ca0+ppe0)/assets0
    if aq <= 0 or aq0 <= 0:
        raise ValueError("nonpositive_asset_quality_residual")
    ratios = dict(DSRI=(ar/sales)/(ar0/sales0), GMI=(gp0/sales0)/(gp/sales),
                  AQI=aq/aq0, SGI=sales/sales0, DEPI=(dep0/(dep0+ppe0))/(dep/(dep+ppe)),
                  SGAI=(sga/sales)/(sga0/sales0), TATA=(income-cfo)/assets,
                  LVGI=((cl+debt)/assets)/((cl0+debt0)/assets0))
    assert all(math.isfinite(v) for v in ratios.values())
    return dict(previous_period=previous, fiscal_start=str(fiscal_start),
                earnings_method=earnings_method, ratios=ratios, mscore=mscore(ratios), facts=trace)


def main():
    mapping = json.loads((CACHE / "company_tickers_web.json").read_text())
    trusted = {s.strip() for s in (ROOT / "research/out/trusted_symbols.txt").read_text().splitlines() if s.strip()}
    symbols = {}
    for row in mapping:
        if row["ticker"] in trusted:
            cik = str(row["cik_str"])
            symbols.setdefault(cik, []).append(row["ticker"])
    symbols = {cik: sorted(set(names))[0] for cik, names in symbols.items()}
    all_meta, all_values, counts = {}, {}, collections.Counter()
    for year in range(2015, 2021):
        for quarter in range(1, 5):
            token = f"{year}q{quarter}"
            path = CACHE / f"{token}.zip"
            if not path.exists():
                raise RuntimeError("missing official quarter: "+token)
            with zipfile.ZipFile(path) as z:
                metas = {}
                with z.open("sub.txt") as fh:
                    for row in csv.DictReader(io.TextIOWrapper(fh), delimiter="\t"):
                        if row["cik"] not in symbols or row["form"] not in ("10-K", "10-K/A"):
                            continue
                        counts["matched_annual_filings"] += 1
                        try:
                            sic = int(row["sic"])
                            if 6000 <= sic <= 6999:
                                counts["financial_sector_filings"] += 1
                                continue
                            assert parse_day(row["period"]) <= date(2020, 12, 31)
                            accepted = date.fromisoformat(row["accepted"][:10])
                            assert accepted <= date(2020, 12, 31)
                        except (ValueError, AssertionError):
                            counts["invalid_metadata"] += 1
                            continue
                        keep = {key: row[key] for key in ("adsh", "cik", "name", "sic", "form", "period", "filed", "accepted", "instance")}
                        keep.update(symbol=symbols[row["cik"]], source_quarter=token)
                        metas[row["adsh"]] = keep
                data = {adsh: {} for adsh in metas}
                with z.open("num.txt") as fh:
                    chunks = pd.read_csv(fh, sep="\t", dtype=str, chunksize=250000,
                                         keep_default_na=False, usecols=["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value"])
                    for frame in chunks:
                        keep = frame["adsh"].isin(metas) & frame["tag"].isin(TAGS)
                        keep &= frame["version"].str.startswith("us-gaap/") & (frame["uom"] == "USD")
                        keep &= (frame["segments"] == "") & (frame["coreg"] == "") & frame["qtrs"].isin(("0", "4"))
                        frame = frame.loc[keep]
                        for r in frame.itertuples(index=False):
                            if not r.value:
                                continue
                            try:
                                value = float(r.value)
                            except ValueError:
                                continue
                            key = r.tag, r.ddate, r.qtrs
                            previous = data[r.adsh].get(key, "absent")
                            if previous != "absent" and previous != value:
                                value = None
                                counts["conflicting_numeric_keys"] += 1
                            data[r.adsh][key] = value
                all_meta.update(metas)
                all_values.update(data)
                print(token, "annual_filings", len(metas), "retained_facts", sum(map(len, data.values())), flush=True)
    records, reasons = [], collections.Counter()
    for adsh, meta in all_meta.items():
        item = dict(meta)
        try:
            item.update(status="complete", **derive(meta, all_values[adsh]))
        except ValueError as exc:
            item.update(status="unavailable", reason=str(exc))
            reasons[str(exc)] += 1
        records.append(item)
    records.sort(key=lambda r: (r["accepted"], r["adsh"]))
    (OUT / "financials.json").write_text(json.dumps(records, indent=2, allow_nan=False)+"\n")
    coverage = dict(counts, mapped_ciks=len(symbols), annual_records=len(records),
                    complete_records=sum(r["status"] == "complete" for r in records),
                    complete_ciks=len({r["cik"] for r in records if r["status"] == "complete"}),
                    rejected_reasons=dict(reasons),
                    mapping_sha256=hashlib.sha256((CACHE / "company_tickers_web.json").read_bytes()).hexdigest(),
                    method_sha256=hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest())
    (OUT / "financial_coverage.json").write_text(json.dumps(coverage, indent=2)+"\n")
    print(json.dumps(coverage, indent=2), flush=True)


if __name__ == "__main__":
    main()
