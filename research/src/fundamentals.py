"""Point-in-time annual fundamentals from the SEC Financial Statement Data Sets.

Spec: out/20261004-quality-factor/PREREGISTRATION.md section 2. Two stages:

1. ``extract_filings`` reads every cached quarter zip once and writes one row
   per original 10-K: CIK, SIC, accepted date, fiscal period end and the five
   fields (revenue, gross profit, assets, operating cash flow, liabilities),
   each taken from the filing itself with a fixed tag order. A field with
   contradictory values inside one filing is missing, never averaged.
2. ``fundamental_panel`` scatters those rows onto a price panel: for each
   session, the newest 10-K accepted STRICTLY BEFORE that session, if its
   fiscal period ended within ``MAX_AGE_DAYS``. A stale or incomplete newest
   filing is missing -- an older filing is never used in its place.

No value is forward-filled across a missing field, and nothing reads a filing
before its acceptance date.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SEC_DIR = ROOT / "research/data/sec-mscore"
FILINGS_CSV = ROOT / "research/data/quality/filings_10k.csv"
TICKERS_JSON = SEC_DIR / "company_tickers_web.json"

MAX_AGE_DAYS = 456  # 15 months from fiscal period end

REVENUE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueGoodsNet",
)
COST_TAGS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")
FLOW_TAGS = set(REVENUE_TAGS) | set(COST_TAGS) | {"GrossProfit", "NetCashProvidedByUsedInOperatingActivities"}
STOCK_TAGS = {"Assets", "Liabilities"}
FIELDS = ("revenue", "gross_profit", "assets", "cfo", "liabilities")


def _quarters() -> List[Path]:
    return sorted(p for p in SEC_DIR.glob("20[0-9][0-9]q[1-4].zip"))


def _first(values: Dict[str, Optional[float]], tags: Sequence[str]) -> Optional[float]:
    """Value of the first tag PRESENT; a present-but-contradictory tag is missing."""
    for tag in tags:
        if tag in values:
            return values[tag]
    return None


def derive_fields(flows: Dict[str, Optional[float]], stocks: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """The five fields from one filing's tag values (None = absent or contradictory)."""
    revenue = _first(flows, REVENUE_TAGS)
    if "GrossProfit" in flows:
        gross = flows["GrossProfit"]
    else:
        cost = _first(flows, COST_TAGS)
        gross = revenue - cost if revenue is not None and cost is not None else None
    assets = stocks.get("Assets")
    if assets is not None and assets <= 0:
        assets = None
    return {
        "revenue": revenue,
        "gross_profit": gross,
        "assets": assets,
        "cfo": flows.get("NetCashProvidedByUsedInOperatingActivities"),
        "liabilities": stocks.get("Liabilities"),
    }


def _read_quarter(path: Path) -> List[dict]:
    with zipfile.ZipFile(path) as z:
        sub = pd.read_csv(z.open("sub.txt"), sep="\t", dtype=str, keep_default_na=False,
                          usecols=["adsh", "cik", "sic", "form", "period", "accepted"],
                          quoting=csv.QUOTE_NONE)
        sub = sub[sub["form"] == "10-K"]
        if sub.empty:
            return []
        meta = sub.set_index("adsh")
        wanted = set(meta.index)
        tags = FLOW_TAGS | STOCK_TAGS
        parts = []
        reader = pd.read_csv(
            z.open("num.txt"), sep="\t", dtype=str, keep_default_na=False, quoting=csv.QUOTE_NONE,
            usecols=["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value"],
            chunksize=1_000_000,
        )
        for chunk in reader:
            chunk = chunk[
                chunk["adsh"].isin(wanted) & chunk["tag"].isin(tags) & (chunk["uom"] == "USD")
                & (chunk["segments"] == "") & (chunk["coreg"] == "")
                & chunk["version"].str.startswith("us-gaap")
            ]
            if not chunk.empty:
                parts.append(chunk)
    if not parts:
        return []
    num = pd.concat(parts, ignore_index=True)
    num["value"] = pd.to_numeric(num["value"], errors="coerce")
    num = num[np.isfinite(num["value"])]
    num = num[num["ddate"] == num["adsh"].map(meta["period"])]
    is_flow = num["tag"].isin(FLOW_TAGS)
    num = num[(is_flow & (num["qtrs"] == "4")) | (~is_flow & (num["qtrs"] == "0"))]
    rows = []
    for adsh, group in num.groupby("adsh", sort=False):
        values: Dict[str, Optional[float]] = {}
        for tag, g in group.groupby("tag"):
            distinct = set(g["value"].round(4))
            values[tag] = float(next(iter(distinct))) if len(distinct) == 1 else None
        flows = {k: v for k, v in values.items() if k in FLOW_TAGS}
        stocks = {k: v for k, v in values.items() if k in STOCK_TAGS}
        m = meta.loc[adsh]
        rows.append({"adsh": adsh, "cik": int(m["cik"]), "sic": m["sic"], "period": m["period"],
                     "accepted": m["accepted"][:10], "quarter_file": path.stem,
                     **derive_fields(flows, stocks)})
    # Filings with no usable tag at all still count: their newest-filing
    # status must block an older filing from standing in for them.
    for adsh in wanted - set(num["adsh"]):
        m = meta.loc[adsh]
        rows.append({"adsh": adsh, "cik": int(m["cik"]), "sic": m["sic"], "period": m["period"],
                     "accepted": m["accepted"][:10], "quarter_file": path.stem,
                     **{f: None for f in FIELDS}})
    return rows


def extract_filings(out: Path = FILINGS_CSV) -> pd.DataFrame:
    rows: List[dict] = []
    for path in _quarters():
        got = _read_quarter(path)
        print(f"{path.stem}: {len(got)} 10-K filings", flush=True)
        rows.extend(got)
    frame = pd.DataFrame(rows).drop_duplicates("adsh").sort_values(["cik", "accepted", "adsh"])
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, index=False)
    return frame


def ticker_to_cik(symbols: Iterable[str]) -> Dict[str, int]:
    """Current SEC ticker map onto panel symbols; one ticker per CIK (alphabetical first)."""
    rows = json.loads(TICKERS_JSON.read_text())
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(str(r["ticker"]).upper(), int(r["cik_str"]))
    mapped = {}
    for s in sorted(symbols):
        cik = by_ticker.get(s.upper().replace(".", "-"))
        if cik is not None:
            mapped[s] = cik
    first: Dict[int, str] = {}
    for s, cik in mapped.items():
        first.setdefault(cik, s)
    return {s: cik for s, cik in mapped.items() if first[cik] == s}


@dataclass
class FundamentalPanel:
    values: Dict[str, np.ndarray]   # field -> (time, symbol), NaN when unknown
    coverage: Dict[str, float]


def fundamental_panel(sessions: Sequence[date], symbols: Sequence[str],
                      filings: Optional[pd.DataFrame] = None) -> FundamentalPanel:
    if filings is None:
        filings = pd.read_csv(FILINGS_CSV, dtype={"sic": str, "period": str, "accepted": str})
    sic = pd.to_numeric(filings["sic"], errors="coerce")
    filings = filings[sic.notna() & ~sic.between(6000, 6999)]
    cik_of = ticker_to_cik(symbols)
    col = {s: j for j, s in enumerate(symbols)}
    session_days = np.array([np.datetime64(d, "D") for d in sessions])
    out = {f: np.full((len(sessions), len(symbols)), np.nan) for f in FIELDS}
    by_cik = {cik: g for cik, g in filings.groupby("cik")}
    for symbol, cik in cik_of.items():
        g = by_cik.get(cik)
        if g is None:
            continue
        g = g.sort_values(["accepted", "adsh"])
        accepted = g["accepted"].to_numpy(dtype="datetime64[D]")
        period = pd.to_datetime(g["period"], format="%Y%m%d").to_numpy(dtype="datetime64[D]")
        # Newest filing accepted strictly before each session.
        k = np.searchsorted(accepted, session_days, side="left") - 1
        ok = k >= 0
        age = np.where(ok, (session_days - period[np.clip(k, 0, None)]).astype(int), 10**6)
        ok &= age <= MAX_AGE_DAYS
        j = col[symbol]
        for f in FIELDS:
            vals = g[f].to_numpy(dtype=float)
            out[f][ok, j] = vals[k[ok]]
    coverage = {f: float(np.isfinite(v).any(axis=0).mean()) for f, v in out.items()}
    return FundamentalPanel(values=out, coverage=coverage)


def ratios(fp: FundamentalPanel) -> Dict[str, np.ndarray]:
    a = fp.values["assets"]
    with np.errstate(invalid="ignore", divide="ignore"):
        return {
            "gpa": fp.values["gross_profit"] / a,
            "cfoa": fp.values["cfo"] / a,
            "lev": fp.values["liabilities"] / a,
        }


if __name__ == "__main__":
    started = datetime.now()
    frame = extract_filings()
    print(f"{len(frame)} filings -> {FILINGS_CSV} in {datetime.now() - started}")
