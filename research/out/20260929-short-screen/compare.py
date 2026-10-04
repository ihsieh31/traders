"""Offline, fixed 2016-2020 short-price diagnostics; read METHOD.md first."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import socket
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import numpy as np
from research.src import common as C, panel as P

OUT = Path(__file__).resolve().parent
NAMES = ("legacy_short_proxy", "mirror28", "bounded45", "efficient60")
H, BUDGET = 20, 20
START, END = date(2016, 1, 4), date(2020, 12, 31)


def deny_network(*args, **kwargs):
    raise RuntimeError("short-screen research permits local cache only")


socket.socket.connect = deny_network
socket.create_connection = deny_network


def build_panel():
    """Scan date/symbol first; only parse numerical OHLCV inside 2016-2020."""
    rows = [r for r in json.loads(C.CALENDAR_PATH.read_text())["rows"]
            if str(START) <= r["date"][:10] <= str(END)]
    rows.sort(key=lambda r: r["date"])
    sessions = [date.fromisoformat(r["date"][:10]) for r in rows]
    assert sessions[0] == START and sessions[-1] == END
    trusted_path = ROOT / "research/out/trusted_symbols.txt"
    symbols = sorted({s.strip() for s in trusted_path.read_text().splitlines()
                      if s.strip() and not s.startswith("#")})
    t_ix = {str(d): i for i, d in enumerate(sessions)}
    s_ix = {s: i for i, s in enumerate(symbols)}
    arrays = {name: np.full((len(sessions), len(symbols)), np.nan)
              for name in P.FIELDS}
    seen = np.zeros((len(sessions), len(symbols)), dtype=bool)
    count = duplicates = 0
    files = sorted(C.BARS_DIR.glob("batch_*.csv.gz"))
    for k, path in enumerate(files):
        with gzip.open(path, "rt", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            col = {name: i for i, name in enumerate(header)}
            for raw in reader:
                stamp = raw[col["timestamp"]][:10]
                symbol = raw[col["symbol"]]
                if stamp not in t_ix or symbol not in s_ix:
                    continue
                i, j = t_ix[stamp], s_ix[symbol]
                values = [float(raw[col[name]]) for name in P.FIELDS]
                if seen[i, j]:
                    duplicates += 1
                    assert all(np.isclose(arrays[name][i, j], value,
                                          rtol=0, atol=0, equal_nan=True)
                               for name, value in zip(P.FIELDS, values)), (stamp, symbol)
                for name, value in zip(P.FIELDS, values):
                    arrays[name][i, j] = value
                seen[i, j] = True
                count += 1
        if (k + 1) % 20 == 0:
            C.log(f"read {k+1}/{len(files)} cache batches; {count:,} in-window bars")
    digest = hashlib.sha256()
    digest.update("\n".join(map(str, sessions)).encode())
    digest.update("\n".join(symbols).encode())
    for name in P.FIELDS:
        digest.update(arrays[name].tobytes())
    meta = dict(n_sessions=len(sessions), n_symbols=len(symbols),
                in_window_bar_rows=count, identical_duplicate_rows=duplicates,
                panel_sha256=digest.hexdigest(),
                trusted_symbols_sha256=hashlib.sha256(trusted_path.read_bytes()).hexdigest(),
                method_sha256=hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest())
    C.log("in-window panel ready")
    return P.Panel(sessions=sessions, symbols=symbols, calendar_rows=rows, **arrays), meta


def extra_at(close, t):
    window = close[t-60:t+1]
    logret = np.log(window[1:] / window[:-1])
    total = np.sum(np.abs(logret), axis=0)
    er = np.divide(np.log(window[-1] / window[0]), total,
                   out=np.zeros_like(total), where=total > 0)
    er[~np.isfinite(total)] = np.nan
    ret = window[-20:] / window[-21:-1] - 1
    up = np.sqrt(252 * np.mean(np.maximum(ret, 0)**2, axis=0))
    return er, up


def models_at(feats, eligible, close, t):
    u = eligible[t].copy()
    # Explicitly require finite inputs to percentile ranks.
    for key in ("adv20", "r20", "r60", "vol20", "volume_ratio", "trend"):
        u &= np.isfinite(getattr(feats, key)[t])
    trend, r20, r60, vol = (getattr(feats, key)[t]
                            for key in ("trend", "r20", "r60", "vol20"))
    er, up = extra_at(close, t)
    masks = {"legacy_short_proxy": u & (trend < 0) & ((r20 < 0) | (r60 < 0)),
             "mirror28": u & (vol <= .28) & (trend < 0) & (r60 >= -.25) & (r60 < 0),
             "bounded45": u & (vol <= .45) & (trend < 0) & (r20 < 0) & (r60 >= -.25) & (r60 < 0)}
    masks["efficient60"] = masks["bounded45"] & np.isfinite(er) & np.isfinite(up)
    full_score = np.full(len(u), np.nan)
    ix = np.flatnonzero(u)
    if len(ix):
        p = {key: C.percentiles_fast(getattr(feats, key)[t, ix])
             for key in ("adv20", "r20", "r60", "vol20", "volume_ratio")}
        full_score[ix] = 100*(.20*p["adv20"] + .25*(1-p["r20"])
                             + .25*(1-p["r60"]) + .15*(1-p["vol20"])
                             + .15*p["volume_ratio"])
    out = {}
    for name in NAMES:
        cols = np.flatnonzero(masks[name])
        if len(cols) == 0:
            out[name] = (cols, np.array([], dtype=float))
            continue
        if name == "legacy_short_proxy":
            score = full_score[cols]
        elif name in ("mirror28", "bounded45"):
            score = 100*(.50*C.percentiles_fast(-trend[cols])
                         + .30*C.percentiles_fast(-r60[cols])
                         + .20*(1-C.percentiles_fast(vol[cols])))
        else:
            score = 100/3*(C.percentiles_fast(-er[cols])
                          + C.percentiles_fast(feats.adv20[t, cols])
                          + 1-C.percentiles_fast(up[cols]))
        order = np.lexsort((cols, -score))
        out[name] = (cols[order], score[order])
    return out, np.flatnonzero(u)


def measure(panel, t, ids):
    n = len(ids)
    entry, exit_ = panel.open[t+1, ids], panel.close[t+H, ids]
    highs = panel.high[t+1:t+H+1, ids]
    valid_entry = np.isfinite(entry) & (entry > 0)
    known_end = valid_entry & np.isfinite(exit_) & (exit_ >= 0)
    known_path = valid_entry & np.isfinite(highs).all(axis=0) & (highs > 0).all(axis=0)
    short = 1-exit_/entry
    adverse = np.max(highs, axis=0)/entry-1 if n else np.array([])
    gross = float(short.sum()/BUDGET) if known_end.all() else None
    calendar_days = (panel.sessions[t+H] - panel.sessions[t+1]).days + 1
    row = dict(n=n, unknown_end=int((~known_end).sum()),
               unknown_path=int((~known_path).sum()),
               high_up10=int((known_path & (adverse >= .10)).sum()),
               high_up20=int((known_path & (adverse >= .20)).sum()),
               end_down10=int((known_end & (short >= .10)).sum()),
               winning_names=int((known_end & (short > 0)).sum()),
               calendar_days=calendar_days, gross=gross)
    for rt in (.001, .002):
        for fee in (0, .05, .20):
            key = f"net_rt{int(rt*10000)}bp_borrow{int(fee*100)}pct"
            row[key] = None if gross is None else gross - n/BUDGET*(rt+fee*calendar_days/365)
    return row


def bootstrap(x):
    x = np.asarray(x, dtype=float)
    if not len(x):
        return dict(mean=None, ci95=None, n=0)
    rng = np.random.default_rng(20260929)
    starts = rng.integers(0, len(x), size=(2000, int(np.ceil(len(x)/3))))
    ix = ((starts[:, :, None] + np.arange(3)) % len(x)).reshape(2000, -1)[:, :len(x)]
    ci = np.quantile(x[ix].mean(axis=1), [.025, .975])
    return dict(mean=float(x.mean()), ci95=[float(v) for v in ci], n=len(x))


def aggregate(rows):
    n = sum(r["n"] for r in rows)
    out = dict(periods=len(rows), total_name_observations=n,
               full20_periods=sum(r["n"] == BUDGET for r in rows),
               partial_periods=sum(0 < r["n"] < BUDGET for r in rows),
               zero_periods=sum(r["n"] == 0 for r in rows))
    for key in ("unknown_end", "unknown_path", "high_up10", "high_up20", "end_down10", "winning_names"):
        out[key] = sum(r[key] for r in rows)
        out[key+"_rate"] = out[key]/n if n else None
    for key in ("high_up10", "high_up20"):
        out[key+"_rate_upper"] = (out[key]+out["unknown_path"])/n if n else None
    keys = ["gross"]+[key for key in rows[0] if key.startswith("net_")]
    out["returns"] = {}
    for key in keys:
        values = [r[key] for r in rows if r[key] is not None]
        mean, tstat = C.newey_west(values, lag=1)
        out["returns"][key] = dict(**bootstrap(values), unknown_periods=len(rows)-len(values),
                                    nw_lag1_t=float(tstat) if np.isfinite(tstat) else None)
    return out


def checks():
    assert np.allclose(1-np.array([90, 110, 250])/100, [.10, -.10, -1.50])
    prices = np.tile((100*.996**np.arange(90))[:, None], (1, 30))
    er, up = extra_at(prices, 89)
    assert np.allclose(er, -1) and np.allclose(up, 0)
    assert all(np.allclose(a, b, equal_nan=True)
               for a, b in zip(extra_at(prices, 74), extra_at(prices[:75], 74)))
    dates = [date(2016, 1, 4)+timedelta(days=i) for i in range(90)]
    symbols = [f"S{i:02}" for i in range(30)]
    panel = P.Panel(dates, symbols, prices.copy(), prices*1.01, prices*.99,
                    prices.copy(), np.full_like(prices, 1e7), [])
    feats = P.compute_features(panel)
    prefix = P.compute_features(P.Panel(dates[:75], symbols, panel.open[:75], panel.high[:75],
                                       panel.low[:75], panel.close[:75], panel.volume[:75], []))
    for key in vars(feats):
        assert np.allclose(getattr(feats, key)[:75], getattr(prefix, key), equal_nan=True)
    eligible = P.eligible_mask(panel, feats)
    models, _ = models_at(feats, eligible, prices, 74)
    for ids, scores in models.values():
        assert len(set(ids[:BUDGET])) == min(BUDGET, len(ids))
        assert ids[:BUDGET].tolist() == list(range(BUDGET))
        assert np.allclose(scores, scores[0])
    from tradingagents.screening.metrics import SymbolFeatures, select_research_candidates
    # Nonconstant cross-section checks legacy against actual production lane.
    for j in range(30):
        feats.adv20[74, j] *= 1+j/100
        feats.r20[74, j] -= j/1000
        feats.r60[74, j] -= j/1000
        feats.vol20[74, j] += j/1000
    models, universe = models_at(feats, eligible, prices, 74)
    prod = select_research_candidates([
        SymbolFeatures(symbol=symbols[j], **{key: float(getattr(feats, key)[74, j]) for key in vars(feats)})
        for j in universe], top_k=60, allow_shorts=True)
    ids, scores = models["legacy_short_proxy"]
    assert [r.symbol for r in prod] == [symbols[j] for j in ids]
    assert np.allclose([r.negative_score for r in prod], scores)
    # Open100, end90, intraday high125 => +10% short return but >20% squeeze.
    panel.open[61, 0] = 100
    panel.close[80, 0] = 90
    panel.high[61:81, 0] = 105
    panel.high[65, 0] = 125
    row = measure(panel, 60, np.array([0]))
    assert np.isclose(row["gross"], .10/BUDGET) and row["high_up20"] == 1
    assert np.isclose(row["net_rt10bp_borrow5pct"],
                      .10/BUDGET-(.001+.05*20/365)/BUDGET)
    panel.close[80, 0] = np.nan
    assert measure(panel, 60, np.array([0]))["gross"] is None
    assert measure(panel, 60, np.array([], dtype=int))["gross"] == 0
    return dict(short_pnl=True, unbounded_loss=True, daily_high_risk=True,
                missing_exit_unknown=True, prefix_invariance=True,
                legacy_production_equivalence=True, budget_and_ties=True, network="blocked")


def main():
    result = dict(status="exploratory; previously researched 2016-2020 sample",
                  variants=list(NAMES), horizon=H, date_step=20, self_check=checks())
    panel, result["data"] = build_panel()
    C.log("computing production trailing features")
    feats = P.compute_features(panel)
    eligible = P.eligible_mask(panel, feats)
    ts = np.array([t for t in P.valid_as_of(panel, H) if t >= 60][::20])
    rows, picks, market = [], [], []
    for t in ts:
        models, universe = models_at(feats, eligible, panel.close, t)
        market_row = measure(panel, t, universe)
        market_row["gross_equal_weight"] = (None if market_row["gross"] is None else
                                              market_row["gross"]*BUDGET/len(universe))
        market.append(dict(date=str(panel.sessions[t]), **market_row))
        for name, (cols, scores) in models.items():
            ids = cols[:BUDGET]
            assert len(ids) <= BUDGET and len(set(ids)) == len(ids)
            rows.append(dict(date=str(panel.sessions[t]), year=panel.sessions[t].year,
                             model=name, pool_n=len(cols), **measure(panel, t, ids)))
            for rank, (j, score) in enumerate(zip(ids, scores[:BUDGET]), start=1):
                picks.append(dict(date=str(panel.sessions[t]), model=name, rank=rank,
                                  symbol=panel.symbols[j], score=float(score),
                                  trend=float(feats.trend[t, j]), r20=float(feats.r20[t, j]),
                                  r60=float(feats.r60[t, j]), vol20=float(feats.vol20[t, j])))
    result["scheduled_periods"] = len(ts)
    result["models"] = {name: aggregate([r for r in rows if r["model"] == name]) for name in NAMES}
    result["annual"] = {str(year): {name: aggregate([r for r in rows if r["model"] == name and r["year"] == year])
                                  for name in NAMES} for year in range(2016, 2021)}
    result["paired_full20"] = {}
    base = {r["date"]: r for r in rows if r["model"] == NAMES[0]}
    for name in NAMES[1:]:
        pairs = [(r, base[r["date"]]) for r in rows if r["model"] == name and
                 r["n"] == BUDGET and base[r["date"]]["n"] == BUDGET and
                 r["gross"] is not None and base[r["date"]]["gross"] is not None]
        result["paired_full20"][name] = {
            "gross_delta": bootstrap([a["gross"]-b["gross"] for a, b in pairs]),
            "high_up10_delta": bootstrap([(a["high_up10"]+a["unknown_path"]
                                           -b["high_up10"]-b["unknown_path"])/BUDGET for a, b in pairs])}
    values = [r["gross_equal_weight"] for r in market if r["gross_equal_weight"] is not None]
    result["eligible_universe_short_proxy"] = dict(**bootstrap(values), unknown_periods=len(market)-len(values))
    for filename, data in (("periods.csv", rows), ("selections.csv", picks), ("market.csv", market)):
        with (OUT / filename).open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)
    (OUT / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    C.log("all four preregistered variants complete")
    print(json.dumps({name: {"gross": v["returns"]["gross"],
                           "net5pct": v["returns"]["net_rt10bp_borrow5pct"],
                           "high_up10_rate": v["high_up10_rate"],
                           "full20_periods": v["full20_periods"]}
                      for name, v in result["models"].items()}, indent=2), flush=True)


if __name__ == "__main__":
    with np.errstate(invalid="ignore", divide="ignore"):
        if "--checks-only" in sys.argv:
            print(json.dumps(checks()))
        else:
            main()
