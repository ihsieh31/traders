"""Structural diagnosis of the production exclusion formula. Spec: METHOD.md.

Offline: local bars cache and calendar only. Run from the repository root:
    .venv-p2/bin/python research/out/20261004-screen-diagnosis/diagnose.py
"""
from __future__ import annotations

import json
import socket
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np
from scipy.stats import rankdata

from research.src import panel as P


def _deny(*_a, **_k):
    raise RuntimeError("diagnosis is offline-only")


socket.socket.connect = _deny
socket.create_connection = _deny

OUT = Path(__file__).resolve().parent
TRUSTED = str(ROOT / "research/out/trusted_symbols.txt")
MAX_VOL20, MIN_R60, N = 0.28, -0.25, 20
H, CORR_WINDOW, VANISH_WINDOW, DRAWS = 20, 60, 60, 50


def pct(values):
    """Production ``ascending_percentiles``: (avg_rank-1)/(n-1); n==1 -> 0.5."""
    if values.size == 1:
        return np.array([0.5])
    return (rankdata(values, method="average") - 1.0) / (values.size - 1)


def select(feats, keep_row, t):
    cols = np.flatnonzero(keep_row)
    if cols.size == 0:
        return cols
    score = 100 * (0.5 * pct(feats.trend[t, cols]) + 0.3 * pct(feats.r60[t, cols])
                   + 0.2 * (1 - pct(feats.vol20[t, cols])))
    order = np.lexsort((cols, -score))  # symbols are sorted, so index == symbol order
    return cols[order][:N]


def eff_n(rets, cols):
    block = rets[:, cols]
    if np.isnan(block).any() or cols.size < 2:
        return np.nan, np.nan
    c = np.corrcoef(block, rowvar=False)
    rho = c[np.triu_indices(cols.size, 1)].mean()
    return rho, cols.size / (1 + (cols.size - 1) * rho)


def run(label, start, end, rng):
    panel = P.build_panel(start=start, end=end, symbols_file=TRUSTED)
    feats = P.compute_features(panel)
    elig = P.eligible_mask(panel, feats)
    first = panel.sessions.index(start)
    with np.errstate(invalid="ignore"):
        g_vol = feats.vol20 <= MAX_VOL20
        g_trend = feats.trend > 1e-12  # production TREND_EPSILON
        g_r60 = feats.r60 >= MIN_R60
    keep = elig & g_vol & g_trend & g_r60
    n_t = len(panel.sessions)
    with np.errstate(invalid="ignore", divide="ignore"):
        daily = panel.close[1:] / panel.close[:-1] - 1.0
    daily = np.vstack([np.full((1, daily.shape[1]), np.nan), daily])
    last_seen = np.maximum.accumulate(np.where(np.isfinite(panel.close), np.arange(n_t)[:, None], -1), axis=0)[-1]

    picks, shortfall, overlaps = {}, [], []
    prev = None
    for t in range(first, n_t):
        chosen = select(feats, keep[t], t)
        picks[t] = chosen
        if chosen.size < N:
            shortfall.append({
                "date": panel.sessions[t].isoformat(), "eligible": int(elig[t].sum()),
                "survivors": int(keep[t].sum()),
                "pass_vol_only": int((elig[t] & g_vol[t]).sum()),
                "pass_trend_only": int((elig[t] & g_trend[t]).sum()),
                "pass_r60_only": int((elig[t] & g_r60[t]).sum()),
            })
        if prev is not None and prev.size == N and chosen.size == N:
            overlaps.append(len(set(prev) & set(chosen)) / N)
        prev = chosen

    # D3-D6 on non-overlapping samples starting at the study start.
    fwd_drop = P.forward_returns(panel, H)
    fwd_fill = P.forward_returns(panel, H, delisted_exit=True)
    samples = [t for t in range(first, n_t - H, H) if picks[t].size == N]
    d3 = {"picked": [], "survivor_random": [], "eligible_random": []}
    vanish = {"picked": [0, 0], "eligible": [0, 0]}
    vanished_rows, all_rows = [], []
    d6 = {"periods": 0, "missing_dropped": 0, "missing_filled": 0, "mean_dropped": [], "mean_filled": []}
    for t in samples:
        window = daily[t - CORR_WINDOW + 1:t + 1]
        chosen = picks[t]
        d3["picked"].append(eff_n(window, chosen)[1])
        for key, pool in (("survivor_random", np.flatnonzero(keep[t])), ("eligible_random", np.flatnonzero(elig[t]))):
            d3[key].append(np.nanmean([eff_n(window, rng.choice(pool, N, replace=False))[1] for _ in range(DRAWS)]))
        if t + VANISH_WINDOW < n_t:
            gone = last_seen < t + VANISH_WINDOW
            vanish["picked"][0] += int(gone[chosen].sum())
            vanish["picked"][1] += N
            e = np.flatnonzero(elig[t])
            vanish["eligible"][0] += int(gone[e].sum())
            vanish["eligible"][1] += e.size
            for j in chosen:
                row = {"date": panel.sessions[t].isoformat(), "symbol": panel.symbols[j],
                       "vol20": float(feats.vol20[t, j]), "r60": float(feats.r60[t, j]),
                       "trend": float(feats.trend[t, j]), "vanished": bool(gone[j])}
                all_rows.append(row)
                if gone[j]:
                    vanished_rows.append(row)
        a, b = fwd_drop[t, chosen], fwd_fill[t, chosen]
        d6["periods"] += 1
        d6["missing_dropped"] += int(np.isnan(a).sum())
        d6["missing_filled"] += int(np.isnan(b).sum())
        d6["mean_dropped"].append(float(np.nanmean(a)))
        d6["mean_filled"].append(float(np.nanmean(b)))

    def q(values):
        v = np.asarray([x for x in values if np.isfinite(x)])
        return {"n": int(v.size), "mean": float(v.mean()), "p10": float(np.percentile(v, 10)),
                "median": float(np.median(v)), "p90": float(np.percentile(v, 90))} if v.size else None

    sizes = [int(keep[t].sum()) for t in range(first, n_t)]
    return {
        "label": label, "sessions": n_t - first, "symbols": len(panel.symbols),
        "D1": {"survivor_pool": q(sizes), "days_below_20": len(shortfall), "shortfall_days": shortfall},
        "D2": {"daily_overlap": q(overlaps)},
        "D3": {k: q(v) for k, v in d3.items()},
        "D4": {"picked_vanish_rate": vanish["picked"][0] / max(1, vanish["picked"][1]),
               "eligible_vanish_rate": vanish["eligible"][0] / max(1, vanish["eligible"][1]),
               "picked_counts": vanish["picked"], "eligible_counts": vanish["eligible"]},
        "D5": {"vanished": {k: q([r[k] for r in vanished_rows]) for k in ("vol20", "r60", "trend")},
               "all_picks": {k: q([r[k] for r in all_rows]) for k in ("vol20", "r60", "trend")},
               "vanished_rows": vanished_rows},
        "D6": {"periods": d6["periods"], "missing_dropped": d6["missing_dropped"],
               "missing_filled": d6["missing_filled"],
               "mean_period_return_dropped": float(np.mean(d6["mean_dropped"])),
               "mean_period_return_filled": float(np.mean(d6["mean_filled"]))},
    }


def main():
    rng = np.random.default_rng(20261004)
    results = [run("2016-2020", date(2016, 1, 4), date(2020, 12, 31), rng),
               run("2021-2025", date(2021, 1, 4), date(2025, 12, 31), rng)]
    (OUT / "summary.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    for r in results:
        print("=" * 72, "\n", r["label"], f"sessions={r['sessions']} symbols={r['symbols']}")
        print("D1 pool", r["D1"]["survivor_pool"], "days<20:", r["D1"]["days_below_20"])
        print("D2 overlap", r["D2"]["daily_overlap"])
        print("D3", json.dumps(r["D3"]))
        print("D4", {k: v for k, v in r["D4"].items()})
        print("D5 vanished", json.dumps(r["D5"]["vanished"]), "\n   all", json.dumps(r["D5"]["all_picks"]))
        print("D6", r["D6"])
    print("DIAGNOSIS COMPLETE")


if __name__ == "__main__":
    main()
