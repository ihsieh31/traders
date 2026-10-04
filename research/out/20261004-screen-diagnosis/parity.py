"""Production exclusion path vs research restatement on real cached bars.

Feeds real bars for sampled dates through tradingagents.screening.metrics
(scan_universe -> score_exclusion_candidates -> select_exclusion_candidates)
and compares the 20 names with diagnose.select(). Offline only.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import diagnose as D  # also installs the offline socket guard
from research.src import panel as P
from tradingagents.screening import metrics as M
from tradingagents.screening.policy import ExclusionThresholds
from tradingagents.screening.prompt import build_sector_plan


def production_pick(panel, t):
    as_of = panel.sessions[t]
    lo = t - 61 - 5
    ts = pd.to_datetime([f"{d.isoformat()} 12:00" for d in panel.sessions[lo:t + 1]], utc=True)
    universe, bars = [], {}
    for j, sym in enumerate(panel.symbols):
        c = panel.close[lo:t + 1, j]
        if not np.isfinite(c[-1]):
            continue
        frame = pd.DataFrame({"timestamp": ts, "open": panel.open[lo:t + 1, j], "high": panel.high[lo:t + 1, j],
                              "low": panel.low[lo:t + 1, j], "close": c, "volume": panel.volume[lo:t + 1, j]})
        frame = frame[np.isfinite(frame["close"].to_numpy())].reset_index(drop=True)
        frame.attrs.update(source="research_cache", feed="sip", adjustment="split")
        bars[sym] = frame
        universe.append({"symbol": sym, "asset_id": f"id-{sym}", "identity_source": "research",
                         "asset_class": "us_equity", "asset_status": "active", "tradable": True,
                         "market_cap": 1e12, "market_cap_source": "not-modelled"})
    rows = [{"date": d.isoformat()} for d in panel.sessions[:t + 1]]
    scored, stats = M.scan_universe(universe, bars, as_of=as_of, thresholds=M.EligibilityThresholds(),
                                    calendar_rows=rows)
    ranked = M.score_exclusion_candidates(scored, ExclusionThresholds(), stats)
    plan = build_sector_plan(ranked, max_per_sector=5, select_n=min(20, len(ranked)))
    return [f.symbol for f in M.select_exclusion_candidates(ranked, select_n=20, sector_plan=plan)], len(ranked)


def main():
    panel = P.build_panel(start=date(2022, 1, 3), end=date(2022, 12, 30), symbols_file=D.TRUSTED)
    feats = P.compute_features(panel)
    elig = P.eligible_mask(panel, feats)
    with np.errstate(invalid="ignore"):
        keep = elig & (feats.vol20 <= D.MAX_VOL20) & (feats.trend > 1e-12) & (feats.r60 >= D.MIN_R60)
    first = panel.sessions.index(date(2022, 1, 3))
    mismatches = 0
    for t in range(first, len(panel.sessions), 9):
        research = [panel.symbols[j] for j in D.select(feats, keep[t], t)]
        prod, survivors = production_pick(panel, t)
        same = research == prod
        mismatches += not same
        print(panel.sessions[t], "survivors", survivors, int(keep[t].sum()), "picks", len(prod),
              "MATCH" if same else f"DIFF research={research} prod={prod}")
    print("PARITY", "OK" if mismatches == 0 else f"{mismatches} MISMATCHES")


if __name__ == "__main__":
    main()
