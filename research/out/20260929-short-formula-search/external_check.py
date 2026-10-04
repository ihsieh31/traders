"""Bounded historical quote checks for two loss cases and two hash-picked names."""
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def main():
    rows = [r for r in csv.DictReader((OUT / "selections.csv").open()) if r["horizon"] == "20"]
    chosen = ["W", "CVNA"]
    candidates = sorted((r for r in rows if r["model"] == "accrual_weak"),
                        key=lambda r: hashlib.sha256(f'20260929:{r["date"]}:{r["symbol"]}'.encode()).hexdigest())
    for r in candidates:
        if r["symbol"] not in chosen:
            chosen.append(r["symbol"])
        if len(chosen) == 4:
            break
    calendar = json.loads((ROOT / "research/data/calendar.json").read_text())["rows"]
    days = sorted(r["date"][:10] for r in calendar if "2016-01-04" <= r["date"][:10] <= "2020-12-31")
    indices = {d: i for i, d in enumerate(days)}
    sources, checked = [], []
    for symbol in chosen:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1=1451865600&period2=1609459200&interval=1d&events=div%2Csplits"
        p = OUT / f"yahoo_{symbol}.json"
        source = dict(symbol=symbol, url=url, use="2016-2020 quote OHLC only; current meta unused")
        try:
            if not p.exists():
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TradingBuffett historical-research/1.0"}), timeout=30) as response:
                    data = response.read(2*1024*1024+1)
                    assert len(data) <= 2*1024*1024
                    p.write_bytes(data)
            data = json.loads(p.read_text())["chart"]["result"][0]
            quote = data["indicators"]["quote"][0]
            bars = {}
            for i, stamp in enumerate(data["timestamp"]):
                day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
                assert "2016-01-04" <= day <= "2020-12-31"
                bars[day] = {k: quote[k][i] for k in ("open", "high", "low", "close")}
            source.update(status="received", sha256=hashlib.sha256(p.read_bytes()).hexdigest(), bars=len(bars))
            for r in rows:
                if r["symbol"] != symbol:
                    continue
                t = indices[r["date"]]
                entry_day, exit_day = days[t+1], days[t+20]
                entry, exit_ = bars.get(entry_day, {}).get("open"), bars.get(exit_day, {}).get("close")
                if entry is None or exit_ is None or entry <= 0:
                    checked.append(dict(date=r["date"], model=r["model"], symbol=symbol, status="missing_quote"))
                    continue
                local, external = float(r["single_gross"]), 1-exit_/entry
                checked.append(dict(date=r["date"], model=r["model"], symbol=symbol, status="checked",
                    entry_day=entry_day, exit_day=exit_day, external_entry=entry, external_exit=exit_,
                    local_gross=local, external_gross=external, difference=external-local,
                    direction_match=(local > 0) == (external > 0)))
        except Exception as exc:
            source.update(status="unavailable", error=str(exc))
        sources.append(source)
        print(symbol, source["status"], flush=True)
    (OUT / "external_sources.json").write_text(json.dumps(sources, indent=2)+"\n")
    (OUT / "external_prices.json").write_text(json.dumps(checked, indent=2)+"\n")
    ok = [r for r in checked if r["status"] == "checked"]
    result = dict(sample_symbols=chosen, source_status={s["symbol"]: s["status"] for s in sources},
        checked_observations=len(ok), distinct_pairs=len({(r["symbol"], r["date"]) for r in ok}),
        direction_matches=sum(r["direction_match"] for r in ok),
        max_return_difference=max((abs(r["difference"]) for r in ok), default=None),
        scope="Two observed loss cases plus two hash-picked main-method symbols; not whole-panel validation")
    (OUT / "external_summary.json").write_text(json.dumps(result, indent=2)+"\n")
    print(result, flush=True)


if __name__ == "__main__":
    main()
