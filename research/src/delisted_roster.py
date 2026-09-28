"""Build a delisting roster from SEC EDGAR and measure real price-recovery rates.

READ research/plan.md SECTIONS 4.1-4.3 FIRST.

Why this exists
---------------
The project universe is "tickers that trade today", so every name that was
acquired, taken private, or failed is simply absent, and the resulting bias is
stated but not measurable. Two things are needed to fix it, and they come from
different places:

1. **The event list** -- who stopped trading, when, on which exchange, and under
   which symbol. ``Form 25`` / ``Form 25-NSE`` is the notice of removal from
   listing and is filed *at the moment of removal*, so the filing date is a
   usable delisting date. This module pulls it straight from EDGAR full-text
   search. No entitlement, no vendor.

2. **The price history for those names** -- which no free source has complete.
   ``--check`` measures, per provider, how much of the roster is actually
   recoverable, so the shortfall is a number rather than an assumption.

Both halves are needed. The roster without prices is a fact; prices without a
roster is a guess. Neither alone removes the bias.

    # roster only
    python -m research.src.delisted_roster --start 2015-01-01 --end 2026-09-01

    # roster + per-provider recovery rate
    python -m research.src.delisted_roster --start 2024-01-01 --end 2024-12-31 --check
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from research.src.common import DATA_DIR, log

# SEC requires a descriptive UA with contact info. Be a good citizen: <=10 rps.
EDGAR_UA = "tradingBuffett research (contact: zongen@example.com)"
EFT_ENDPOINT = "https://efts.sec.gov/LATEST/search-index"

#: ``Form 25`` is the delisting notice; ``Form 25-NSE`` is the Nasdaq-specific
#: variant of the same notice. Both mean "removal from listing". ``Form
#: 15-12G`` is deregistration: it fires for many entities that were never
#: listed, so it is a much noisier signal and is opt-in.
DELISTING_FORMS = ("25", "25-NSE")
DEREG_FORMS = ("15-12G", "15-12B")

#: EDGAR caps a full-text-search result set; walk it in pages, politely.
PAGE_LIMIT = 1000
SLEEP_S = 0.34

#: Providers that are actually reachable without a paid entitlement.
FREE_PROVIDERS = ("yfinance", "stockanalysis")

_STOCKANALYSIS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_SYMBOL_RE = re.compile(r"\(([^()]*)\)\s*\(CIK")


@dataclass
class DelistingEvent:
    """One removal-from-listing record.

    ``tickers`` can hold more than one symbol: a single Form 25 can remove a
    whole class of warrants, preferreds, and units, and treating those as if
    they were the common stock is a survivorship bug of a different kind.
    """

    filing_date: str
    form: str
    cik: str
    company: str
    tickers: List[str] = field(default_factory=list)
    sic: str = ""
    exchange_state: str = ""
    accession: str = ""


def _get_json(url: str, headers: Dict[str, str], retries: int = 4) -> dict:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            last = exc
            time.sleep(1.2 * (attempt + 1))
    raise last  # type: ignore[misc]


def _extract_tickers(display_names: Iterable[str]) -> List[str]:
    """Pull symbols out of EDGAR's ``Name (TICK, TICK2) (CIK ...)`` format.

    EDGAR does not expose a symbol field on search hits, but ``display_names``
    renders the registrant's EDGAR name, which for a listed company carries the
    symbol list in parentheses. Taking the *last* parenthesised group before
    the CIK avoids swallowing the CIK itself.
    """
    tickers: List[str] = []
    for name in display_names:
        if "CIK" not in name:
            continue
        head = name.split("(CIK")[0]
        for group in re.findall(r"\(([^()]+)\)", head):
            for part in re.split(r"[,\s]+", group):
                part = part.strip()
                if part and re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,9}", part):
                    if part not in tickers:
                        tickers.append(part)
    return tickers


def _pick_company_cik(ciks: List[str], display_names: List[str]) -> str:
    """EDGAR lists the registrant first and the exchange second in ``ciks``.

    Taking the last element returns NYSE/Nasdaq's own CIK and collapses the whole
    roster onto a handful of exchange identifiers. Match by name instead.
    """
    for name in display_names:
        if "CIK" not in name:
            continue
        if any(w in name.upper() for w in ("EXCHANGE", "CORP.", "INC.", "LLC", "TRUST")) and len(
            display_names
        ) > 1:
            m = re.search(r"\(CIK (\d+)", name)
            if m and name is display_names[0]:
                return m.group(1)
    return (ciks or [""])[0]


def fetch_form_events(form: str, start: str, end: str) -> List[DelistingEvent]:
    """All hits for one form type in a date window, paged."""
    events: List[DelistingEvent] = []
    offset = 0
    seen_accessions: Set[str] = set()
    while offset < PAGE_LIMIT:
        query = urllib.parse.urlencode(
            {"forms": form, "startdt": start, "enddt": end, "from": offset}
        )
        payload = _get_json(f"{EFT_ENDPOINT}?{query}", {"User-Agent": EDGAR_UA})
        hits = payload.get("hits", {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            src = hit.get("_source", {})
            names = src.get("display_names") or []
            adsh = src.get("adsh") or ""
            # One trust can file many near-identical 25-NSEs on one day; keep one.
            key = f"{src.get('file_date')}|{names[0] if names else ''}|{adsh}"
            if key in seen_accessions:
                continue
            seen_accessions.add(key)
            ciks = [str(c) for c in (src.get("ciks") or [])]
            company = ""
            for n in names:
                if "CIK" in n and not re.search(
                    r"(NEW YORK STOCK EXCHANGE|NASDAQ|NYSE AMERICAN|CBOE|EXCHANGE LLC)",
                    n.upper(),
                ):
                    company = n.split("(CIK")[0].strip()
                    break
            if not company:
                company = (names[0].split("(CIK")[0].strip() if names else "")
            sics = [s for s in (src.get("sics") or []) if s and s != "0000"]
            events.append(
                DelistingEvent(
                    filing_date=src.get("file_date", ""),
                    form=src.get("form", form),
                    cik=_pick_company_cik(ciks, names),
                    company=company,
                    tickers=_extract_tickers(names),
                    sic=sics[0] if sics else "",
                    exchange_state=",".join(src.get("biz_states") or []),
                    accession=adsh,
                )
            )
        offset += len(hits)
        time.sleep(SLEEP_S)
    return events


def build_roster(start: str, end: str, include_dereg: bool = False) -> List[DelistingEvent]:
    forms = list(DELISTING_FORMS) + (list(DEREG_FORMS) if include_dereg else [])
    events: List[DelistingEvent] = []
    for form in forms:
        log(f"EDGAR full-text search: form={form} {start}..{end}")
        events.extend(fetch_form_events(form, start, end))
    # Dedupe on (cik, filing_date); keep the form-25 record in preference to 15-12G.
    events.sort(key=lambda e: (e.cik, e.filing_date, 0 if e.form in DELISTING_FORMS else 1))
    out: List[DelistingEvent] = []
    for ev in events:
        if out and out[-1].cik == ev.cik and out[-1].filing_date == ev.filing_date:
            merged = out[-1]
            merged.tickers = sorted(set(merged.tickers) | set(ev.tickers))
            continue
        out.append(ev)
    return out


# --------------------------------------------------------------------------
# Provider recovery probes
# --------------------------------------------------------------------------


def probe_stockanalysis(ticker: str) -> Optional[dict]:
    """stockanalysis.com's undocumented JSON history endpoint.

    ``range=10Y`` is relative to the symbol's *last* bar, so for a dead ticker
    the window still ends at its final session. Returns ``None`` when the symbol
    is unknown, and raises the recovery-fidelity concern: a recycled ticker
    answers 200 with the *new* company's history, which is worse than a 404.
    """
    url = (
        f"https://stockanalysis.com/api/symbol/s/{ticker}/history"
        "?range=10Y&period=Daily"
    )
    try:
        payload = _get_json(url, _STOCKANALYSIS_HEADERS, retries=2)
    except Exception:  # noqa: BLE001
        return None
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    return {"rows": len(rows), "first": rows[-1].get("t"), "last": rows[0].get("t")}


def probe_yfinance(ticker: str) -> Optional[dict]:
    """Yahoo purges delisted history entirely; this confirms it per symbol."""
    try:
        import yfinance  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        hist = yfinance.Ticker(ticker).history(period="max", auto_adjust=True)
    except Exception:  # noqa: BLE001
        return None
    if hist is None or len(hist) == 0:
        return None
    return {
        "rows": int(len(hist)),
        "first": str(hist.index[0].date()),
        "last": str(hist.index[-1].date()),
    }


def run_recovery_check(
    events: List[DelistingEvent], limit: int = 60
) -> Dict[str, object]:
    """Sample the roster and ask each free provider for history."""
    with_tickers = [e for e in events if e.tickers]
    sample = with_tickers[:: max(1, len(with_tickers) // limit)][:limit]
    results: Dict[str, List[Tuple[str, object]]] = {
        "stockanalysis": [],
        "yfinance": [],
    }
    for i, ev in enumerate(sample, 1):
        for ticker in ev.tickers[:1]:
            if i % 5 == 0:
                log(f"  probing {i}/{len(sample)} ({ticker})")
            results["stockanalysis"].append((ticker, probe_stockanalysis(ticker)))
            results["yfinance"].append((ticker, probe_yfinance(ticker)))
            time.sleep(0.2)
    summary = {}
    for name, pairs in results.items():
        hits = [(t, r) for t, r in pairs if r]
        # A hit whose last bar is far after the delisting date is a recycled
        # ticker, not a recovery. Count it separately instead of crediting it.
        by_ticker = {e.tickers[0]: e for e in with_tickers if e.tickers}
        genuine = 0
        recycled = 0
        for t, r in hits:
            assert isinstance(r, dict)
            delisted = by_ticker[t].filing_date
            if (r.get("last") or "") > delisted[:4] + "-12-31":
                recycled += 1
            else:
                genuine += 1
        summary[name] = {
            "probed": len(pairs),
            "any_response": len(hits),
            "plausible": genuine,
            "recycled_ticker_suspects": recycled,
            "recovered_symbols": [t for t, _ in hits],
        }
    return summary


def write_roster_csv(events: List[DelistingEvent], path) -> int:
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["filing_date", "form", "cik", "company", "tickers", "sic", "state", "accession"]
        )
        for ev in events:
            writer.writerow(
                [
                    ev.filing_date,
                    ev.form,
                    ev.cik,
                    ev.company,
                    "|".join(ev.tickers),
                    ev.sic,
                    ev.exchange_state,
                    ev.accession,
                ]
            )
            rows += 1
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2015-01-01", help="First filing date (inclusive).")
    parser.add_argument("--end", default=None, help="Last filing date. Default: today.")
    parser.add_argument(
        "--include-dereg",
        action="store_true",
        help="Also pull Form 15-12G/15-12B (deregistration). Much noisier.",
    )
    parser.add_argument("--check", action="store_true", help="Measure price recovery per provider.")
    parser.add_argument("--limit", type=int, default=60, help="Symbols to probe with --check.")
    parser.add_argument("--out", default=None, help="CSV path. Default: data/delisted/roster.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end = args.end or __import__("datetime").date.today().isoformat()
    out_path = args.out or str(DATA_DIR / "delisted" / "roster.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    events = build_roster(args.start, end, include_dereg=args.include_dereg)
    n_written = write_roster_csv(events, out_path)
    n_ciks = len({e.cik for e in events})
    n_tickers = len({t for e in events for t in e.tickers})
    log(
        f"roster: {n_written} events, {n_ciks} distinct CIKs, "
        f"{n_tickers} distinct symbols -> {out_path}"
    )

    if args.check:
        summary = run_recovery_check(events, limit=args.limit)
        log("price-recovery probe:")
        for name, s in summary.items():
            if isinstance(s, dict):
                log(
                    f"  {name}: {s['plausible']}/{s['probed']} plausible "
                    f"({s['recycled_ticker_suspects']} recycled-ticker suspects)"
                )


if __name__ == "__main__":
    main()
