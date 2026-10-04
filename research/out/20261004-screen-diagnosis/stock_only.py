"""Part 2 of METHOD.md: rerun the evidence on a stock-only universe.

Offline. Run from the repository root:
    .venv-p2/bin/python research/out/20261004-screen-diagnosis/stock_only.py
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "research/out/20260929-screen-review"))

import numpy as np

import diagnose as D  # offline socket guard
import audit as A     # screen-review definitions, reused verbatim
from research.src import factors as F, panel as P
from research.src.tools.exclusion_screen import exclude_mask, survivor_score

MASTER = ROOT / "research/out/20260928-external-anchor/tiingo_symbol_master.csv"
US_EXCHANGES = {"NYSE", "NASDAQ", "NYSE ARCA", "AMEX", "NYSE MKT", "BATS", "NYSE NAT"}
H = 20


def asset_types():
    types = {}
    with MASTER.open(newline="") as f:
        for row in csv.DictReader(f):
            if row["exchange"] in US_EXCHANGES and row["priceCurrency"] == "USD":
                types.setdefault(row["ticker"].upper(), set()).add(row["assetType"])
    return {t: next(iter(v)) for t, v in types.items() if len(v) == 1}


def write_universes():
    types = asset_types()
    trusted = [s.strip() for s in open(D.TRUSTED) if s.strip() and not s.startswith("#")]
    kind = {s: types.get(s.replace(".", "-"), "unknown") for s in trusted}
    stocks = sorted(s for s, k in kind.items() if k == "Stock")
    (HERE / "stock_symbols.txt").write_text("\n".join(stocks) + "\n")
    counts = {}
    for k in kind.values():
        counts[k] = counts.get(k, 0) + 1
    return kind, counts


def etf_share(start, end, kind):
    """E1: ETF share of the exclusion picks in the mixed (trusted) universe."""
    panel = P.build_panel(start=start, end=end, symbols_file=D.TRUSTED)
    feats = P.compute_features(panel)
    elig = P.eligible_mask(panel, feats)
    with np.errstate(invalid="ignore"):
        keep = elig & (feats.vol20 <= D.MAX_VOL20) & (feats.trend > 0) & (feats.r60 >= D.MIN_R60)
    first = panel.sessions.index(start)
    shares, worst = [], None
    for t in range(first, len(panel.sessions) - H, H):
        picks = D.select(feats, keep[t], t)
        if picks.size == 0:
            continue
        n_etf = sum(kind.get(panel.symbols[j]) == "ETF" for j in picks)
        shares.append(n_etf / picks.size)
        if worst is None or n_etf > worst["etf"]:
            worst = {"date": panel.sessions[t].isoformat(), "etf": int(n_etf), "picks": int(picks.size),
                     "symbols": [panel.symbols[j] for j in picks]}
    pool_etf = []
    for t in range(first, len(panel.sessions)):
        cols = np.flatnonzero(elig[t])
        if cols.size:
            pool_etf.append(np.mean([kind.get(panel.symbols[j]) == "ETF" for j in cols]))
    return {"periods": len(shares), "mean_etf_share_of_picks": float(np.mean(shares)),
            "periods_with_any_etf": int(sum(s > 0 for s in shares)),
            "mean_etf_share_of_eligible_pool": float(np.mean(pool_etf)), "worst_period": worst}


def stock_evidence(start, end, rng):
    panel = P.build_panel(start=start, end=end, symbols_file=str(HERE / "stock_symbols.txt"))
    feats = P.compute_features(panel)
    prod = P.eligible_mask(panel, feats)
    keep = exclude_mask(panel, feats, prod, vol_ceiling=.28)
    first = panel.sessions.index(start)
    # E2 / D1: every session, production semantics (select even when < 20).
    pool = keep[first:].sum(axis=1)
    ready = prod[first:].sum(axis=1) >= 20
    short_days = [{"date": panel.sessions[first + i].isoformat(), "survivors": int(pool[i]),
                   "eligible": int(prod[first + i].sum())}
                  for i in np.flatnonzero(ready & (pool < 20))]
    # E2 / D3: effective positions on non-overlapping samples.
    with np.errstate(invalid="ignore", divide="ignore"):
        daily = np.vstack([np.full((1, panel.close.shape[1]), np.nan), panel.close[1:] / panel.close[:-1] - 1])
    effn = {"picked": [], "survivor_random": [], "eligible_random": []}
    for t in range(first, len(panel.sessions) - H, H):
        picks = D.select(feats, keep[t], t)
        if picks.size < 20:
            continue
        window = daily[t - D.CORR_WINDOW + 1:t + 1]
        effn["picked"].append(D.eff_n(window, picks)[1])
        for key, pool_cols in (("survivor_random", np.flatnonzero(keep[t])), ("eligible_random", np.flatnonzero(prod[t]))):
            effn[key].append(np.nanmean([D.eff_n(window, rng.choice(pool_cols, 20, replace=False))[1]
                                         for _ in range(D.DRAWS)]))
    # E3: the screen-review evaluation, same functions, same sampling.
    scores = {"current20": F.production_score(feats, prod), "report28": survivor_score(feats, keep),
              "reuse28": F.production_score(feats, keep)}
    ts = np.array([t for t in P.valid_as_of(panel, H) if start <= panel.sessions[t] <= end][::H])
    with np.errstate(invalid="ignore", divide="ignore"):
        totals, paired, matched, _periods, _examples = A.evaluate(panel, prod, keep, scores, ts)
    keys = ("n", "bad_path10_bounds", "up_end10_rate")
    return {
        "symbols": len(panel.symbols), "sessions": len(panel.sessions) - first,
        "pool": {"min": int(pool[ready].min()), "median": float(np.median(pool[ready])),
                 "days_below_20_after_ready": len(short_days), "days_not_ready": int((~ready).sum())},
        "short_days": short_days,
        "effective_positions": {k: float(np.nanmean(v)) for k, v in effn.items()},
        "cohorts": {k: {kk: totals[k][kk] for kk in keys} for k in ("eligible", "keep", "removed", *scores)},
        "matched_selection_rates": matched,
        "paired_bad_path_delta_block3": {k: v[1] for k, v in paired.items()},
    }


def main():
    kind, counts = write_universes()
    print("trusted universe by Tiingo assetType:", counts, flush=True)
    rng = np.random.default_rng(20261004)
    out = {"universe_counts": counts, "splits": {}}
    for start, end in ((date(2016, 1, 4), date(2020, 12, 31)), (date(2021, 1, 4), date(2025, 12, 31))):
        label = f"{start.year}-{end.year}"
        out["splits"][label] = {"E1_mixed_universe": etf_share(start, end, kind),
                                "stock_only": stock_evidence(start, end, rng)}
        print(label, json.dumps({k: v for k, v in out["splits"][label]["stock_only"].items() if k != "short_days"},
                                ensure_ascii=False), flush=True)
        print(label, "E1", json.dumps(out["splits"][label]["E1_mixed_universe"], ensure_ascii=False), flush=True)
    (HERE / "stock_only_summary.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print("STOCK-ONLY COMPLETE")


if __name__ == "__main__":
    main()
