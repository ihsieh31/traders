"""Independent scalar verification from raw cache, without the selector/panel."""
import csv
import gzip
import json
import math
import socket
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from research.src import common as C

import pandas as pd
from tradingagents.screening.metrics import (
    EligibilityThresholds, compute_features, validate_and_clean_bars,
)

OUT = Path(__file__).resolve().parent


def deny(*args, **kwargs):
    raise RuntimeError("raw-cache verification is offline")


socket.socket.connect = deny
socket.create_connection = deny


def main():
    picks = list(csv.DictReader((OUT / "selections.csv").open()))
    periods = list(csv.DictReader((OUT / "periods.csv").open()))
    wanted = {r["symbol"] for r in picks}
    calendar = [r for r in json.loads(C.CALENDAR_PATH.read_text())["rows"]
                if "2016-01-04" <= r["date"][:10] <= "2020-12-31"]
    calendar.sort(key=lambda r: r["date"])
    sessions = [r["date"][:10] for r in calendar]
    day_ix = {day: i for i, day in enumerate(sessions)}
    raw = {}
    for path in sorted(C.BARS_DIR.glob("batch_*.csv.gz")):
        with gzip.open(path, "rt", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                day, symbol = r["timestamp"][:10], r["symbol"]
                if day not in day_ix or symbol not in wanted:
                    continue
                raw[symbol, day] = {key: float(r[key]) for key in ("open", "high", "low", "close", "volume")}
                raw[symbol, day]["timestamp"] = r["timestamp"]
    grouped = {}
    for r in picks:
        grouped.setdefault((r["model"], r["date"]), []).append(r)
    max_pnl_error = max_factor_error = 0.0
    checked_paths, malformed = set(), set()
    spot_dates = {min(r["date"] for r in picks), "2020-03-20", max(r["date"] for r in picks)}
    factors_checked = 0
    for period in periods:
        selected = grouped.get((period["model"], period["date"]), [])
        ids = [r["symbol"] for r in selected]
        assert len(ids) == int(period["n"]) <= 20 and len(set(ids)) == len(ids)
        t = day_ix[period["date"]]
        gross, up10, up20, down10, win = [], 0, 0, 0, 0
        for p in selected:
            symbol = p["symbol"]
            bars = [raw[symbol, day] for day in sessions[t+1:t+21]]
            entry, exit_ = bars[0]["open"], bars[-1]["close"]
            assert entry > 0 and all(math.isfinite(b["high"]) and b["high"] > 0 for b in bars)
            short = (entry-exit_)/entry
            gross.append(short)
            peak = max(b["high"] for b in bars)
            up10 += peak >= 1.10*entry
            up20 += peak >= 1.20*entry
            down10 += short >= .10
            win += short > 0
            for day, b in zip(sessions[t+1:t+21], bars):
                checked_paths.add((symbol, day))
                if not (b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]):
                    malformed.add((symbol, day))
            if period["date"] in spot_dates:
                frame = pd.DataFrame([raw[symbol, day] for day in sessions[t-60:t+1]])
                clean, reason = validate_and_clean_bars(
                    symbol, frame, as_of=date.fromisoformat(period["date"]),
                    thresholds=EligibilityThresholds(), calendar_rows=calendar)
                assert reason is None, (symbol, period["date"], reason)
                features, reason = compute_features(symbol, clean, thresholds=EligibilityThresholds())
                assert reason is None, (symbol, period["date"], reason)
                for key in ("trend", "r20", "r60", "vol20"):
                    delta = abs(getattr(features, key)-float(p[key]))
                    max_factor_error = max(max_factor_error, delta)
                    assert delta < 1e-9, (symbol, key, delta)
                factors_checked += 1
        expected = math.fsum(gross)/20
        max_pnl_error = max(max_pnl_error, abs(expected-float(period["gross"])))
        assert abs(expected-float(period["gross"])) < 1e-12
        for key, value in (("high_up10", up10), ("high_up20", up20),
                           ("end_down10", down10), ("winning_names", win)):
            assert value == int(period[key]), (period["model"], period["date"], key)
        days = (date.fromisoformat(sessions[t+20])-date.fromisoformat(sessions[t+1])).days+1
        assert days == int(period["calendar_days"])
        for rt in (.001, .002):
            for fee in (0, .05, .20):
                key = f"net_rt{int(rt*10000)}bp_borrow{int(fee*100)}pct"
                net = expected - len(ids)/20*(rt+fee*days/365)
                assert abs(net-float(period[key])) < 1e-12
    result = dict(periods_verified=len(periods), selected_observations_verified=len(picks),
                  unique_forward_bars_checked=len(checked_paths),
                  malformed_ohlc_forward_bars=len(malformed),
                  malformed_samples=sorted(malformed)[:10],
                  production_factor_spots_checked=factors_checked,
                  factor_spot_dates=sorted(spot_dates),
                  maximum_factor_error=max_factor_error, maximum_pnl_error=max_pnl_error,
                  all_returns_costs_and_events_match=True, network="blocked")
    assert not malformed, result
    (OUT / "verification.json").write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
