"""Pick a short formula on a selection period, backtest it once afterwards.

Rounds (each spec written before its run):
  f12  out/20261004-short-search/PREREGISTRATION.md         2016-2020 -> 2021-2025
  f13  out/20261004-short-three-periods/PREREGISTRATION.md  2016-2019 -> 2022-2025,
       with 2020-2021 (the pandemic) excluded from selection and verdict

    python -m research.src.tools.short_round --round f13 --stage select
    python -m research.src.tools.short_round --round f13 --stage backtest

``select`` builds a panel that ENDS on the selection period's last day, so
nothing later can be read while choosing (plan.md R1, process.md S-25).
``backtest`` reads only the frozen ``selection.json`` and refuses to run a
second time.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import numpy as np

from research.src import factors as F
from research.src import measure as M
from research.src import panel as P
from research.src import study as S
from research.src.common import OUT_DIR, log

SYMBOLS = OUT_DIR / "20261004-screen-diagnosis/stock_symbols.txt"
HOLDOUT = (date(2026, 1, 2), date(2026, 9, 8))
ROUNDS = {
    "f12": {"dir": OUT_DIR / "20261004-short-search",
            "select": (date(2016, 1, 4), date(2020, 12, 31)),
            "backtest": (date(2021, 1, 4), date(2025, 12, 31)), "stress": None},
    "f13": {"dir": OUT_DIR / "20261004-short-three-periods",
            "select": (date(2016, 1, 4), date(2019, 12, 31)),
            "backtest": (date(2022, 1, 3), date(2025, 12, 31)),
            "stress": (date(2020, 1, 2), date(2021, 12, 31))},
}
# Set by main() from --round; module-level so the measurement helpers stay simple.
ROUND = ROUNDS["f12"]["dir"]
SELECT = ROUNDS["f12"]["select"]
BACKTEST = ROUNDS["f12"]["backtest"]
STRESS = None
HORIZON = 10
HORIZON_GRID = (5, 10, 20)
TOP_K = 20
COSTS = {"base": (0.0010, 0.05), "2.0x": (0.0020, 0.10)}  # (round trip, borrow per year)
T_THRESHOLD = 2.576  # Bonferroni over 5 candidates, two-sided 0.05
NOTIONAL = 5_000.0


# --------------------------------------------------------------------------
# Candidate scores: higher = shorted first
# --------------------------------------------------------------------------


def _ret(panel: P.Panel, w: int) -> np.ndarray:
    return S._momentum(panel, w)


def _max_daily(panel: P.Panel, w: int) -> np.ndarray:
    from numpy.lib.stride_tricks import sliding_window_view

    close = panel.close
    daily = np.full(close.shape, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        daily[1:] = close[1:] / close[:-1] - 1.0
    out = np.full(close.shape, np.nan)
    if close.shape[0] >= w:
        out[w - 1:] = sliding_window_view(daily, w, axis=0).max(axis=2)  # NaN if incomplete
    return out


def _vol(panel: P.Panel, w: int) -> np.ndarray:
    close = panel.close
    daily = np.full(close.shape, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        daily[1:] = close[1:] / close[:-1] - 1.0
        return np.sqrt(P._rolling_var(daily, w, ddof=1)) * math.sqrt(252.0)


def _far_from_high(panel: P.Panel, w: int) -> np.ndarray:
    from research.src.candidates import _high_proximity_values

    return 1.0 - _high_proximity_values(panel, w)


def _low_gpa(panel: P.Panel, _w: int) -> np.ndarray:
    from research.src.candidates import _quality_ratios

    return -_quality_ratios(panel)["gpa"]


#: name -> (score(panel, window), declared window, window grid or None, complete-case universe)
CANDIDATES: Dict[str, Tuple[Callable[[P.Panel, int], np.ndarray], int, Optional[Tuple[int, ...]], bool]] = {
    "short-winners": (_ret, 5, (3, 5, 10), False),
    "short-max": (_max_daily, 20, (10, 20, 60), False),
    "short-vol": (_vol, 20, (10, 20, 60), False),
    "short-far-from-high": (_far_from_high, 120, (60, 120, 180), False),
    "short-low-gpa": (_low_gpa, 0, None, True),
}


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


def masks(panel, features, values, complete):
    prod = P.eligible_mask(panel, features, min_adv20_usd=P.MIN_ADV20_USD)
    wide = P.eligible_mask(panel, features, min_adv20_usd=None)
    if complete:
        ok = np.isfinite(values)
        prod, wide = prod & ok, wide & ok
    return prod, wide


def scores_for(values, mask):
    return F.compose_scores(F.Formula("short", [F.Component("s", values, +1, 0)], HORIZON, TOP_K), mask)


def short_series(panel, scores, mask, span, horizon, cost="base"):
    """Per-period absolute and relative (vs equal-weight long) short net."""
    rt, borrow = COSTS[cost]
    fwd = P.forward_returns(panel, horizon)
    pf = M.build_portfolio(scores, fwd, mask, horizon=horizon, top_k=TOP_K,
                           as_of=S._span_periods(panel, span, horizon))
    carry = borrow * horizon / 252.0
    short_cost = pf.turnover * rt + carry
    absolute = -pf.gross - short_cost
    ew = np.full(len(pf), np.nan)
    for i, t in enumerate(pf.as_of):
        v = fwd[t][mask[t]]
        v = v[np.isfinite(v)]
        if v.size:
            ew[i] = v.mean()
    relative = ew - pf.gross - short_cost
    return pf, absolute, relative


def summary(series, horizon):
    clean = series[np.isfinite(series)]
    boot = M.block_bootstrap(series, horizon, block=3, n_resamples=2000)
    return {
        "n_periods": int(clean.size),
        "mean_per_period": float(clean.mean()) if clean.size else None,
        "annualised": float(clean.mean() * M.PERIODS_PER_YEAR / horizon) if clean.size else None,
        "nw_t": M._period_t(series, horizon),
        "ci_low": boot.get("ci_low"),
        "ci_high": boot.get("ci_high"),
    }


def walk_forward(panel, scores, mask, span, horizon):
    idx = S._sessions_in(panel, span)
    bounds = np.linspace(0, idx.size, S.WALKFORWARD_FOLDS + 1).astype(int)
    rows = []
    for k in range(S.WALKFORWARD_FOLDS):
        seg = idx[bounds[k]:bounds[k + 1]]
        test = seg[int(seg.size * S.WALKFORWARD_TRAIN_FRACTION):]
        lo, hi = panel.sessions[test[0]], panel.sessions[test[-1]]
        _pf, _a, rel = short_series(panel, scores, mask, (lo, hi), horizon)
        rows.append({"fold": k + 1, "test": [lo.isoformat(), hi.isoformat()],
                     "n_periods": int(np.isfinite(rel).sum()),
                     "mean_relative": float(np.nanmean(rel)) if np.isfinite(rel).any() else None})
    return rows


def regimes(panel, pf, rel):
    reg = M.label_regimes(P.cross_sectional_median_log_return(panel))
    out = {}
    for tl, tn in ((1, "bull"), (-1, "bear")):
        for vl, vn in ((1, "high_vol"), (-1, "low_vol")):
            sel = [i for i, t in enumerate(pf.as_of) if reg.trend[t] == tl and reg.vol[t] == vl and np.isfinite(rel[i])]
            out[f"{tn}_{vn}"] = {"n_periods": len(sel),
                                 "mean_relative": float(np.mean(rel[sel])) if sel else None}
    return out


def evaluate_selection(panel, features, name):
    fn, window, grid, complete = CANDIDATES[name]
    values = fn(panel, window)
    prod, wide = masks(panel, features, values, complete)
    sc = scores_for(values, prod)
    pf, absolute, rel = short_series(panel, sc, prod, SELECT, HORIZON)
    _pfw, _aw, rel_wide = short_series(panel, scores_for(values, wide), wide, SELECT, HORIZON)
    wf = walk_forward(panel, sc, prod, SELECT, HORIZON)
    sweeps = []
    if grid:
        curve = [summary(short_series(panel, scores_for(fn(panel, w), prod), prod, SELECT, HORIZON)[2], HORIZON)["annualised"]
                 for w in grid]
        sweeps.append({"parameter": "window", "grid": list(grid), "curve": curve,
                       **{k: v for k, v in S.classify_curve(list(grid), curve).items() if k in ("verdict", "run_length")}})
    curve = [summary(short_series(panel, sc, prod, SELECT, h)[2], h)["annualised"] for h in HORIZON_GRID]
    sweeps.append({"parameter": "horizon", "grid": list(HORIZON_GRID), "curve": curve,
                   **{k: v for k, v in S.classify_curve(list(HORIZON_GRID), curve).items() if k in ("verdict", "run_length")}})
    cells = regimes(panel, pf, rel)
    drop = M.drop_best(rel, HORIZON)
    s_rel, s_wide = summary(rel, HORIZON), summary(rel_wide, HORIZON)
    checks = {
        6: (s_rel["mean_per_period"] or -1) > 0 and (s_wide["mean_per_period"] or -1) > 0,
        7: all(r["n_periods"] >= S.MIN_WALKFORWARD_PERIODS_PER_FOLD for r in wf)
        and sum((r["mean_relative"] or -1) > 0 for r in wf) * 4 >= 3 * len(wf),
        8: all(s["verdict"] == "PLATEAU" and all((c or -1) > 0 for c in s["curve"]) for s in sweeps),
        9: sum((c["mean_relative"] or -1) > 0 for c in cells.values() if c["n_periods"]) >= 2,
        10: not drop.get("insufficient") and drop["by_fraction"]["5%"]["mean"] > 0,
    }
    return {
        "name": name, "window": window, "complete_case": complete,
        "median_universe": float(np.median(prod[S._sessions_in(panel, SELECT)].sum(1))),
        "relative": s_rel, "relative_wide": s_wide, "absolute": summary(absolute, HORIZON),
        "walk_forward": wf, "sweeps": sweeps, "regimes": cells,
        "drop_best_5pct": drop.get("by_fraction", {}).get("5%"),
        "checks": {str(k): bool(v) for k, v in checks.items()},
        "qualified": all(checks.values()),
    }


def stage_select() -> int:
    out = ROUND / "selection.json"
    if out.exists():
        raise SystemExit(f"{out} exists: the selection is frozen")
    panel = P.build_panel(start=SELECT[0], end=SELECT[1], symbols_file=str(SYMBOLS))
    assert panel.sessions[-1] <= SELECT[1], "selection panel must not reach the backtest period"
    features = P.compute_features(panel)
    results = [evaluate_selection(panel, features, name) for name in CANDIDATES]
    qualified = [r for r in results if r["qualified"] and (r["relative"]["nw_t"] or 0) > 0]
    chosen = max(qualified, key=lambda r: r["relative"]["nw_t"])["name"] if qualified else None
    payload = {"rule": "PREREGISTRATION.md section 4", "candidates": results, "selected": chosen}
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    for r in results:
        log(f"{r['name']:<22} qualified={r['qualified']} checks={r['checks']} "
            f"relative ann={r['relative']['annualised']:.4f} t={r['relative']['nw_t']:.2f} "
            f"absolute ann={r['absolute']['annualised']:.4f}")
    log(f"SELECTED: {chosen}")
    return 0


def stage_backtest() -> int:
    selection = json.loads((ROUND / "selection.json").read_text())
    name = selection["selected"]
    out = ROUND / "backtest.json"
    if name is None:
        raise SystemExit("no candidate qualified in 2016-2020; the backtest period stays unread")
    if out.exists():
        raise SystemExit(f"{out} exists: the backtest is run once")
    panel = P.build_panel(start=SELECT[0], end=HOLDOUT[1], symbols_file=str(SYMBOLS))
    features = P.compute_features(panel)
    fn, window, _grid, complete = CANDIDATES[name]
    values = fn(panel, window)
    prod, _wide = masks(panel, features, values, complete)
    sc = scores_for(values, prod)
    _pf, absolute, rel = short_series(panel, sc, prod, BACKTEST, HORIZON)
    _pf2, absolute2, _rel2 = short_series(panel, sc, prod, BACKTEST, HORIZON, cost="2.0x")
    base_sc = scores_for(-F.production_score(features, prod), prod)
    _pfb, _ab, rel_base = short_series(panel, base_sc, prod, BACKTEST, HORIZON)
    _pfh, _ah, rel_hold = short_series(panel, sc, prod, HOLDOUT, HORIZON)
    stress = None
    if STRESS is not None:
        _pfs, abs_stress, rel_stress = short_series(panel, sc, prod, STRESS, HORIZON)
        stress = {"period": [STRESS[0].isoformat(), STRESS[1].isoformat()],
                  "relative": summary(rel_stress, HORIZON), "absolute": summary(abs_stress, HORIZON),
                  "note": "disclosure only: not used for selection or the verdict"}
    adv = features.adv20
    picks_adv = [adv[t][j] for t, names in zip(_pf.as_of, _pf.names) for j in names]
    participation = NOTIONAL / min(picks_adv) if picks_adv else None
    s_rel, s_abs, s_abs2 = summary(rel, HORIZON), summary(absolute, HORIZON), summary(absolute2, HORIZON)
    s_base, s_hold = summary(rel_base, HORIZON), summary(rel_hold, HORIZON)
    selected_rel = next(r for r in selection["candidates"] if r["name"] == name)["relative"]
    checks = {
        "1+12": (s_rel["nw_t"] or 0) >= T_THRESHOLD and (s_rel["ci_low"] or -1) > 0,
        "2": (s_rel["mean_per_period"] or 0) * (selected_rel["mean_per_period"] or 0) > 0,
        "3": (s_abs["mean_per_period"] or -1) > 0,
        "4": (s_abs2["mean_per_period"] or -1) > 0,
        "5": (s_rel["mean_per_period"] or -1) > (s_base["mean_per_period"] or 1e9),
        "11": participation is not None and participation <= 0.001,
        "13": (s_rel["mean_per_period"] or 0) * (s_hold["mean_per_period"] or 0) > 0,
    }
    payload = {"selected": name, "t_threshold": T_THRESHOLD, "relative": s_rel, "absolute": s_abs,
               "absolute_2x": s_abs2, "production_short_relative": s_base, "holdout_relative": s_hold,
               "max_participation": participation, "checks": checks, "stress_disclosure": stress,
               "verdict": "PASSED" if all(checks.values()) else "REJECTED"}
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    log(json.dumps(payload, indent=1, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", choices=("select", "backtest"), required=True)
    p.add_argument("--round", choices=sorted(ROUNDS), default="f12")
    args = p.parse_args(argv)
    global ROUND, SELECT, BACKTEST, STRESS
    cfg = ROUNDS[args.round]
    ROUND, SELECT, BACKTEST, STRESS = cfg["dir"], cfg["select"], cfg["backtest"], cfg["stress"]
    ROUND.mkdir(parents=True, exist_ok=True)
    return stage_select() if args.stage == "select" else stage_backtest()


if __name__ == "__main__":
    raise SystemExit(main())
