"""Part 3 of METHOD.md: the single pre-declared shortfall fill rule F1.

Offline. Run from the repository root:
    .venv-p2/bin/python research/out/20261004-screen-diagnosis/fill_test.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import numpy as np

import diagnose as D  # offline socket guard + production restatement
from research.src import factors as F, panel as P

H = 20


def ranked(feats, cols, t):
    if cols.size == 0:
        return cols
    score = 100 * (0.5 * D.pct(feats.trend[t, cols]) + 0.3 * D.pct(feats.r60[t, cols])
                   + 0.2 * (1 - D.pct(feats.vol20[t, cols])))
    return cols[np.lexsort((cols, -score))]


def outcomes(panel, t, cols):
    entry = panel.open[t + 1, cols]
    path = panel.close[t + 1:t + H + 1, cols]
    known = np.isfinite(entry) & (entry > 0) & np.isfinite(path).all(axis=0)
    bad = known & (path.min(axis=0) / entry - 1 <= -0.10)
    up = known & (path[-1] / entry - 1 >= 0.10)
    return {"n": int(cols.size), "unknown": int((~known).sum()), "bad": int(bad.sum()), "up": int(up.sum())}


def add(total, part):
    for k, v in part.items():
        total[k] = total.get(k, 0) + v


def run(start, end):
    panel = P.build_panel(start=start, end=end, symbols_file=str(HERE / "stock_symbols.txt"))
    feats = P.compute_features(panel)
    elig = P.eligible_mask(panel, feats)
    with np.errstate(invalid="ignore"):
        g_vt = (feats.vol20 <= D.MAX_VOL20) & (feats.trend > 1e-12)
        g_r60 = feats.r60 >= D.MIN_R60
    keep = elig & g_vt & g_r60
    fill_pool = elig & ~g_vt & g_r60
    current = F.production_score(feats, elig)
    first = panel.sessions.index(start)
    groups = {k: {} for k in ("fill", "survivors", "current20", "eligible")}
    days, blocks, fill_counts = [], set(), []
    for t in range(first, len(panel.sessions) - H):
        k = int(keep[t].sum())
        if elig[t].sum() < 20 or k >= 20:
            continue
        survivors = ranked(feats, np.flatnonzero(keep[t]), t)
        fill = ranked(feats, np.flatnonzero(fill_pool[t]), t)[:20 - k]
        cols = np.flatnonzero(elig[t])
        cur = cols[np.lexsort((cols, -current[t, cols]))][:20]
        add(groups["fill"], outcomes(panel, t, fill))
        add(groups["survivors"], outcomes(panel, t, survivors))
        add(groups["current20"], outcomes(panel, t, cur))
        add(groups["eligible"], outcomes(panel, t, cols))
        days.append(panel.sessions[t].isoformat())
        blocks.add((t - first) // H)
        fill_counts.append(int(fill.size))
    return groups, days, len(blocks), fill_counts


def main():
    total = {k: {} for k in ("fill", "survivors", "current20", "eligible")}
    report = {"splits": {}}
    for start, end in ((date(2016, 1, 4), date(2020, 12, 31)), (date(2021, 1, 4), date(2025, 12, 31))):
        groups, days, blocks, fills = run(start, end)
        report["splits"][f"{start.year}-{end.year}"] = {"days": len(days), "distinct_20d_blocks": blocks,
                                                        "fill_names_total": sum(fills), "groups": groups,
                                                        "dates": days}
        for k, v in groups.items():
            add(total[k], v)
    rates = {k: {"n": v["n"], "unknown": v["unknown"], "bad_rate": v["bad"] / v["n"] if v["n"] else None,
                 "up_rate": v["up"] / v["n"] if v["n"] else None} for k, v in total.items()}
    report["combined"] = rates
    verdict = rates["fill"]["bad_rate"] <= rates["current20"]["bad_rate"]
    report["verdict"] = ("F1 fill bad-rate <= current20: implement as default-off option" if verdict
                         else "F1 fill bad-rate > current20: do not implement; keep at-most-20")
    (HERE / "fill_test_summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"combined": rates, "verdict": report["verdict"],
                      **{k: {kk: vv for kk, vv in v.items() if kk != "dates"} for k, v in report["splits"].items()}},
                     indent=1, ensure_ascii=False))
    print("FILL TEST COMPLETE")


if __name__ == "__main__":
    with np.errstate(invalid="ignore", divide="ignore"):
        main()
