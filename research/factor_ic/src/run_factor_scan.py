"""Scan a large candidate factor library, with an out-of-sample holdout.

The production screen is seven hand-picked factors. This asks a wider question:
does ANY structure in daily bars predict the 5-15 day forward return, and if so
which structures survive on data that had no chance to influence their
discovery?

The holdout is the whole point, and it is not optional. Searching N candidate
factors and reporting the best on the SAME data that chose them produces
"significant" findings by arithmetic alone: at a |t| > 2 bar roughly 4.6% of
purely random factors pass, so a 94-factor scan expects ~4 false positives
before any real effect is considered. The operator asked for as many factors as
possible; the honest way to honour that is to find them all, then keep only the
ones that repeat out of sample.

Three periods are reported, all from one pass over the data:

    discovery   2016-01-01 .. SPLIT   factors are searched here
    validation  SPLIT    .. end        never looked at during the search
    combined    2016-01-01 .. end      the headline number, with the caveat
                                       that it is contaminated by discovery

    python -m research.factor_ic.src.run_factor_scan \\
        --start 2016-01-01 --end 2026-09-01 --split 2021-01-01 \\
        --symbols-file research/factor_ic/out/trusted_symbols.txt
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import warnings
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from research.factor_ic.src import common, factor_lib
from research.factor_ic.src import run_ic_study as R
from research.factor_ic.src.common import log
from research.factor_ic.src.factor_lib import LIBRARY_WINDOW

#: Holding periods. 1 and 5 bracket the 5-15 day production window; 20/40/60
#: test whether a factor works at all, or only at a horizon the system does not
#: trade. A factor that is only significant at 60d is not usable here.
HORIZONS = (1, 5, 10, 20, 40, 60)

#: Cross-section floor, matching the production study.
MIN_CROSS = 20

#: Two-sided Bonferroni threshold for the family of library factors. Computed
#: from the actual factor count in main(); this is only the fallback default.
DEFAULT_FAMILY_ALPHA = 0.05


# --------------------------------------------------------------------------
# Dense cross-section grids
# --------------------------------------------------------------------------


def build_grids(store: Dict[str, dict], symbols: List[str], n_sessions: int):
    """Dense (n_symbols, n_sessions) float64 grids of o/h/l/c/v.

    The per-symbol arrays in ``store`` are ragged (each symbol has bars only
    where it traded), but 9,723 of 9,753 trusted symbols have gap-free coverage
    of their whole listed life, so a dense grid loses almost nothing and buys
    O(1) window slicing for every factor at once. Missing bars are NaN, and the
    caller uses the trailing contiguous run length to decide which factors a
    short-history name may contribute to.

    float64, not float32: the preflight compares ``fast_features`` against
    production's own float64 arithmetic, and a float32 grid put a ~5e-7
    relative error into every factor -- above the 1e-9 equivalence bar. Loosening
    the bar to accommodate the grid would have been the wrong direction;
    5 grids x 9,753 x 2,689 x 8 bytes is ~1.05 GB, which is affordable and
    removes the caveat entirely.
    """
    import numpy as np

    n = len(symbols)
    grids = {
        k: np.full((n, n_sessions), np.nan, dtype=np.float64)
        for k in ("o", "h", "l", "c", "v")
    }
    sidx = {s: i for i, s in enumerate(symbols)}
    for sym, entry in store.items():
        row = sidx.get(sym)
        if row is None:
            continue
        g = entry["gidx"]
        keep = g < n_sessions
        if not keep.any():
            continue
        g = g[keep]
        for k in ("o", "h", "l", "c", "v"):
            grids[k][row, g] = entry[k][keep].astype(np.float64)
    return grids


def market_returns(close: "object") -> "object":
    """Equal-weighted market log return per session, from the close grid.

    Computed over every symbol with a bar that session, not just the eligible
    cross-section, so the market series does not move when eligibility rules
    change. Used only by the market-relative factors (beta, idio vol, corr) and
    the market-timing probe.
    """
    import numpy as np

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(all="ignore"):
            lr = np.diff(np.log(close.astype(np.float64)), axis=1)
            return np.concatenate([[np.nan], np.nanmean(lr, axis=0)])


def trailing_run_lengths(grid: "object", i: int, width: int) -> "object":
    """How many consecutive non-NaN bars each symbol has ending at session i.

    Factor f with declared lookback L is valid for a symbol only when this is at
    least L+1, which is how a 252-day factor skips a stock that listed last
    month without that stock also being dropped from the 5-day factors on the
    same date.
    """
    import numpy as np

    window = grid[:, max(0, i - width + 1) : i + 1]
    present = ~np.isnan(window)
    # count trailing Trues by walking backwards; width <= 253 so this is cheap
    run = np.zeros(window.shape[0], dtype=np.int32)
    alive = np.ones(window.shape[0], dtype=bool)
    for j in range(window.shape[1] - 1, -1, -1):
        col = present[:, j]
        run += (col & alive).astype(np.int32)
        alive &= col
    return run


def fast_eligible(grids: dict, i: int, thresholds, required: int) -> "object":
    """Vectorised equivalent of production ``validate_and_clean_bars`` +
    the gate inside ``compute_features``, for the whole universe at one date.

    Production does this per symbol in pandas: ~9,753 DataFrame constructions
    and coercions per date, which over 500+ dates is hours. Every one of its
    conditions is a reduction over the same 61-bar window though, so all of them
    vectorise:

        stale_last_bar     a bar exists at session i
        missing_session    the trailing `required` sessions are all present
        bad_values         all 5 fields finite, close > 0, volume >= 0
        below_min_price    close >= min_price
        below_min_adv20    mean(close*volume, 20) >= min_adv20_usd
        zero_volume_baseline  mean(volume, 20) > 0

    This is an OPTIMISATION, not a redefinition. ``verify_fast_eligibility``
    asserts the boolean mask equals production's own per-symbol decision on a
    sample of dates spanning the full range; the scan refuses to use the fast
    path at all if that check is not run or fails. production's duplicate-
    session check is not replicated here because verify_data.py already proved
    the cache has no duplicate (symbol, session) rows.
    """
    import numpy as np

    lo = i - required + 1
    win = {k: grids[k][:, lo : i + 1] for k in ("o", "h", "l", "c", "v")}
    ok = np.ones(grids["c"].shape[0], dtype=bool)
    for k in ("o", "h", "l", "c", "v"):
        ok &= np.isfinite(win[k]).all(axis=1)
    ok &= (win["c"] > 0).all(axis=1)
    ok &= (win["v"] >= 0).all(axis=1)
    ok &= trailing_run_lengths(grids["c"], i, required) >= required

    c_last = win["c"][:, -1]
    ok &= c_last >= thresholds.min_price

    adv_win = thresholds.vol_mean_window
    with warnings.catch_warnings():
        # all-NaN slices are expected for short-history names; they become NaN
        # and are dropped, which is the whole point of the run-length gate.
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(all="ignore"):
            dollar = np.nanmean(win["c"][:, -adv_win:] * win["v"][:, -adv_win:], axis=1)
            ok &= np.isfinite(dollar) & (dollar >= thresholds.min_adv20_usd)
            baseline = np.nanmean(win["v"][:, -adv_win:], axis=1)
            ok &= np.isfinite(baseline) & (baseline > 0)
    return ok


def fast_features(grids: dict, rows: "object", i: int, thresholds) -> dict:
    """Vectorised ``compute_features`` + ``score_features`` for a whole date.

    Reproduces the production composite ``score`` exactly, from the same dense
    grids, so the factor scan has a like-for-like baseline without a per-symbol
    pandas call at every date. Index arithmetic is transcribed from
    ``metrics.compute_features`` with ``t = required_bars - 1 = 60``:

        r5          closes[60] / closes[55]      -> c[:, -1] / c[:, -6]
        r20         closes[60] / closes[40]      -> c[:, -1] / c[:, -21]
        r60         closes[60] / closes[0]       -> c[:, -1] / c[:,  0]
        vol20       sd(closes[i]/closes[i-1]-1 for i in 41..60) * sqrt(252)
        volume_ratio mean(v[-5:]) / mean(v[-20:])
        trend       closes[60] / mean(c[-20:]) - 1

    ``score`` reuses ``SCORE_WEIGHTS`` from the production module rather than
    restating the numbers, and its percentiles come from
    ``common.percentiles_fast``. The preflight asserts every field against
    production's own output on sampled dates.
    """
    import numpy as np
    from tradingagents.screening.metrics import SCORE_WEIGHTS

    required = thresholds.required_bars
    lo = i - required + 1
    c = grids["c"][rows, lo : i + 1].astype(np.float64)
    v = grids["v"][rows, lo : i + 1].astype(np.float64)
    w = thresholds.vol_mean_window
    recent = thresholds.ratio_recent_window

    price = c[:, -1]
    adv20 = np.mean(c[:, -w:] * v[:, -w:], axis=1)
    r5 = c[:, -1] / c[:, -6] - 1.0
    r20 = c[:, -1] / c[:, -21] - 1.0
    r60 = c[:, -1] / c[:, -61] - 1.0
    ret20 = c[:, -20:] / c[:, -21:-1] - 1.0
    vol20 = ret20.std(axis=1, ddof=1) * math.sqrt(252.0)
    volume_ratio = np.mean(v[:, -recent:], axis=1) / np.mean(v[:, -w:], axis=1)
    trend = price / np.mean(c[:, -w:], axis=1) - 1.0

    p_adv20 = common.percentiles_fast(adv20)
    p_r20 = common.percentiles_fast(r20)
    p_r60 = common.percentiles_fast(r60)
    p_vol20 = common.percentiles_fast(vol20)
    p_vr = common.percentiles_fast(volume_ratio)
    score = 100.0 * (
        SCORE_WEIGHTS["adv20"] * p_adv20
        + SCORE_WEIGHTS["r20"] * p_r20
        + SCORE_WEIGHTS["r60"] * p_r60
        + SCORE_WEIGHTS["vol20_inverse"] * (1.0 - p_vol20)
        + SCORE_WEIGHTS["volume_ratio"] * p_vr
    )
    return {
        "adv20": adv20,
        "r5": r5,
        "r20": r20,
        "r60": r60,
        "vol20": vol20,
        "volume_ratio": volume_ratio,
        "trend": trend,
        "score": score,
    }


def verify_fast_eligibility(grids, store, symbols, sessions, indices, thresholds, required) -> dict:
    """Assert the fast mask AND fast features match production exactly."""
    import numpy as np

    total = 0
    mismatches = []
    worst_feature = 0.0
    for i in indices:
        as_of = sessions[i]
        fast = fast_eligible(grids, i, thresholds, required)
        slow_feats, _excl = R.eligible_on(
            store, symbols, as_of, i, thresholds, None, required
        )
        slow = np.zeros(len(symbols), dtype=bool)
        sidx = {s: j for j, s in enumerate(symbols)}
        for f in slow_feats:
            slow[sidx[f.symbol]] = True
        diff = np.flatnonzero(fast != slow)
        total += 1

        # Feature agreement on the intersection, so this compares values and
        # not membership.
        both = np.flatnonzero(fast & slow)
        if both.size >= 2:
            ff = fast_features(grids, both, i, thresholds)
            shared = {symbols[j] for j in both}
            scored = {
                f.symbol: f
                for f in common.score_features([f for f in slow_feats if f.symbol in shared])
            }
            for pos, j in enumerate(both.tolist()):
                prod = scored.get(symbols[j])
                if prod is None:
                    continue
                for key, mine in ff.items():
                    theirs = getattr(prod, key)
                    if theirs is None:
                        continue
                    worst_feature = max(
                        worst_feature,
                        abs(float(mine[pos]) - float(theirs)) / max(1.0, abs(float(theirs))),
                    )
        if diff.size:
            mismatches.append(
                {
                    "session": as_of.isoformat(),
                    "n_diff": int(diff.size),
                    "examples": [
                        {
                            "symbol": symbols[j],
                            "fast": bool(fast[j]),
                            "production": bool(slow[j]),
                        }
                        for j in diff[:5]
                    ],
                }
            )
        log(
            f"  verify {as_of}  production={int(slow.sum())}  fast={int(fast.sum())}  "
            f"diff={int(diff.size)}  worst_rel_feature_err={worst_feature:.2e}"
        )
    return {
        "dates_checked": total,
        "dates_with_mismatch": len(mismatches),
        "mismatches": mismatches[:5],
        "worst_relative_feature_error": worst_feature,
        "exact": len(mismatches) == 0 and worst_feature < 1e-9,
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default=None)
    p.add_argument(
        "--split",
        default="2021-01-01",
        help="First session of the validation period. Everything before is "
        "discovery; everything after is out of sample.",
    )
    p.add_argument("--date-step", type=int, default=5)
    p.add_argument("--horizons", default="")
    p.add_argument("--symbols-file", default=None)
    p.add_argument("--max-symbols", type=int, default=0)
    p.add_argument(
        "--naive-t",
        type=float,
        default=2.0,
        help="The bar most factor screens use. Reported so it can be compared "
        "against the family-corrected threshold rather than used alone.",
    )
    p.add_argument(
        "--verify-fast-dates",
        type=int,
        default=8,
        help="Dates on which to assert the vectorised eligibility and feature "
        "paths equal production's per-symbol output exactly. 0 disables both "
        "the check and the fast path (the scan then uses production for every "
        "date, which is correct but hours slower).",
    )
    return p.parse_args()


def main() -> int:
    import numpy as np

    args = parse_args()
    horizons = (
        tuple(int(h) for h in args.horizons.split(",") if h.strip())
        if args.horizons.strip()
        else HORIZONS
    )
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    split = date.fromisoformat(args.split)

    raw = common.load_all_bars()
    if len(raw) == 0:
        print("no cached bars. Run: python -m research.factor_ic.src.collect_bars", flush=True)
        return 2
    log(f"cache: {common.bars_coverage(raw)}")

    calendar_rows = common.load_calendar_rows(start, end)
    sessions = common.calendar_sessions(calendar_rows)
    log(f"calendar proves {len(sessions)} sessions")

    store, symbols = R.prepare(raw, sessions, args.max_symbols)
    del raw

    if args.symbols_file:
        allowed = {
            line.strip().upper()
            for line in open(args.symbols_file, encoding="utf-8")
            if line.strip() and not line.strip().startswith("#")
        }
        before = len(symbols)
        symbols = [s for s in symbols if s.upper() in allowed]
        store = {s: v for s, v in store.items() if s.upper() in allowed}
        log(f"trusted filter: {before:,} -> {len(symbols):,} symbols")

    thresholds = common.get_thresholds()
    required = thresholds.required_bars
    n_sessions = len(sessions)

    max_h = max(horizons)
    first = LIBRARY_WINDOW - 1
    last = n_sessions - 1 - max_h
    if last < first:
        print(
            f"not enough sessions: need >= {LIBRARY_WINDOW + max_h + 1}, "
            f"calendar has {n_sessions}",
            flush=True,
        )
        return 2
    as_of_indices = list(range(first, last + 1, args.date_step))
    split_i = next(
        (i for i, d in enumerate(sessions) if d >= split), len(sessions)
    )
    log(
        f"scan: {len(as_of_indices)} sessions "
        f"({sessions[first]} .. {sessions[last]}), step={args.date_step}, "
        f"horizons={list(horizons)}"
    )
    log(
        f"  discovery  {sessions[first]} .. {sessions[min(split_i, last)]}  "
        f"({sum(1 for i in as_of_indices if i < split_i)} sessions)"
    )
    log(
        f"  validation {sessions[min(split_i, last + 1)]} .. {sessions[last]}  "
        f"({sum(1 for i in as_of_indices if i >= split_i)} sessions)"
    )

    log(f"building dense grids for {len(symbols):,} symbols x {n_sessions} sessions ...")
    grids = build_grids(store, symbols, n_sessions)
    mkt = market_returns(grids["c"])
    log("grids ready")

    # ------------------------------------------------------------------
    # Gate: the vectorised eligibility mask must equal production's own
    # per-symbol decision before the scan is allowed to use it. Checked on
    # dates spread across the whole range, including both sides of the split,
    # because a fast path that is right on average can still be wrong at the
    # edges -- and the edges are exactly where new listings and delistings
    # live.
    # ------------------------------------------------------------------
    if args.verify_fast_dates > 0:
        n_dates = len(as_of_indices)
        probes = sorted(
            {
                as_of_indices[int(round(p * (n_dates - 1)))]
                for p in np.linspace(0.0, 1.0, args.verify_fast_dates)
            }
        )
        log(f"verifying fast eligibility against production on {len(probes)} dates ...")
        verdict = verify_fast_eligibility(
            grids, store, symbols, sessions, probes, thresholds, required
        )
        log(
            f"  fast-path check: {verdict['dates_checked']} dates, "
            f"{verdict['dates_with_mismatch']} with any mismatch"
        )
        if not verdict["exact"]:
            for m in verdict["mismatches"]:
                log(f"  MISMATCH {m['session']}: {m['n_diff']} symbols, e.g. {m['examples']}")
            print(
                "fast eligibility path disagrees with production; refusing to "
                "scan with it. Re-run with --verify-fast-dates 0 to use the "
                "slow production path for every date.",
                flush=True,
            )
            return 3
    else:
        verdict = {"exact": None, "dates_checked": 0, "note": "verification skipped by flag"}
        log("WARNING: fast eligibility path NOT verified against production")

    names = factor_lib.factor_names() + [f"PROD_{s}" for s in R.STUDY_SERIES]
    # ic_rows[period][series][horizon] -> [(date, ic)]
    ic_rows: Dict[str, Dict[str, Dict[int, List[Tuple[str, float]]]]] = {
        p: {s: {h: [] for h in horizons} for s in names} for p in ("discovery", "validation", "combined")
    }
    cross_counts: Dict[str, List[int]] = {p: [] for p in ic_rows}
    factor_errors: Dict[str, str] = {}

    for n_done, i in enumerate(as_of_indices, start=1):
        as_of = sessions[i]
        mask_ok = fast_eligible(grids, i, thresholds, required)
        rows_all = np.flatnonzero(mask_ok)
        if rows_all.size < MIN_CROSS:
            continue
        elig = [symbols[j] for j in rows_all]

        lo = max(0, i - LIBRARY_WINDOW + 1)
        # Shape must be (n_eligible, LIBRARY_WINDOW), oldest bar first, which is
        # what factor_lib's Ctx documents. The fancy index already produces
        # that; an extra .T here silently handed the library
        # (LIBRARY_WINDOW, n_eligible) and every factor read the cross-section
        # as its time axis.
        cols = np.arange(lo, i + 1)
        win = {k: grids[k][rows_all[:, None], cols[None, :]] for k in ("o", "h", "l", "c", "v")}
        mkt_win = mkt[lo : i + 1]
        run = trailing_run_lengths(grids["c"][rows_all], i, LIBRARY_WINDOW)

        values, errs = factor_lib.compute_all(
            win["o"], win["h"], win["l"], win["c"], win["v"], mkt_win
        )
        for name, msg in errs:
            factor_errors.setdefault(name, msg)

        # The production seven plus its composite, on the SAME dates and the
        # SAME universe. Without this row the library has no baseline:
        # "significant" only means something relative to what the shipped screen
        # already achieves. fast_features is the preflight-verified vectorised
        # restatement of compute_features + score_features, so the baseline
        # costs nothing and cannot drift from production.
        prod = fast_features(grids, rows_all, i, thresholds)
        values.update({f"PROD_{k}": v for k, v in prod.items()})
        prod_lookback = {f"PROD_{s}": required for s in R.STUDY_SERIES}

        periods = ["combined"] + (["discovery"] if i < split_i else ["validation"])
        for horizon in horizons:
            if i + 1 >= n_sessions or i + horizon >= n_sessions:
                continue
            fwd = np.full(len(elig), np.nan)
            # Vectorised next-open forward return: entry is open[i+1], exit is
            # close[i+horizon], matching PLAN.md 4.1. A symbol missing either
            # leg gets NaN and is dropped by keep_fwd for this horizon only.
            entry_i = i + 1
            exit_i = i + horizon
            with np.errstate(all="ignore"):
                entry = grids["o"][rows_all, entry_i]
                exit_ = grids["c"][rows_all, exit_i]
                cand = np.isfinite(entry) & np.isfinite(exit_) & (entry > 0)
                fwd[cand] = exit_[cand] / entry[cand] - 1.0
            keep_fwd = cand
            if int(keep_fwd.sum()) < MIN_CROSS:
                continue
            for name, series in values.items():
                need = (
                    prod_lookback[name]
                    if name in prod_lookback
                    else factor_lib.lookback_of(name) + 1
                )
                mask = keep_fwd & (run >= need) & np.isfinite(series)
                ic = common.spearman_fast(series, fwd, mask=mask, min_n=MIN_CROSS)
                for p in periods:
                    ic_rows[p][name][horizon].append((as_of.isoformat(), ic))
            cross_counts["combined"].append(len(elig))

        if n_done % 25 == 0 or n_done == len(as_of_indices):
            log(f"  {n_done:4d}/{len(as_of_indices)} sessions  {as_of}  eligible={len(elig)}")

    if factor_errors:
        log("FACTORS THAT RAISED (reported, not silently turned into NaN):")
        for name, msg in sorted(factor_errors.items()):
            log(f"  {name}: {msg}")

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------

    def nw_lag(horizon: int) -> int:
        return max(1, math.ceil(horizon / max(1, args.date_step)))

    def summarise(series, lag):
        values = [v for _d, v in series]
        mean, t = common.newey_west(values, lag)
        finite = [v for v in values if math.isfinite(v)]
        pos = (sum(1 for v in finite if v > 0) / len(finite)) if finite else None
        st = common.describe(values)
        return {
            "ic_mean": None if not math.isfinite(mean) else round(mean, 6),
            "ic_t": None if not math.isfinite(t) else round(t, 3),
            "ic_positive_ratio": None if pos is None else round(pos, 4),
            "n": st["n"],
        }

    results: Dict[str, Dict[str, Dict[str, dict]]] = {}
    for p in ("discovery", "validation", "combined"):
        results[p] = {}
        for name in names:
            results[p][name] = {}
            for h in horizons:
                results[p][name][str(h)] = summarise(ic_rows[p][name][h], nw_lag(h))

    out_dir = common.OUT_DIR
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "library": {
            "n_factors": len(names),
            "window_bars": LIBRARY_WINDOW,
            "families": {
                fam: [f.name for f in factor_lib.REGISTRY if f.family == fam]
                for fam in factor_lib.families()
            },
            "factors_raising": factor_errors,
        },
        "meta": {
            "as_of_range": [sessions[first].isoformat(), sessions[last].isoformat()],
            "split": split.isoformat(),
            "sampled_sessions": len(as_of_indices),
            "date_step": args.date_step,
            "n_symbols_total": len(symbols),
            "horizons": list(horizons),
            "naive_t": args.naive_t,
            "t_stat_method": "newey_west",
            "nw_lag_rule": "max(1, ceil(horizon / date_step))",
            "fast_path_preflight": verdict,
            "median_cross_section": int(np.median(cross_counts["combined"])) if cross_counts["combined"] else 0,
        },
        "discovery": results["discovery"],
        "validation": results["validation"],
        "combined": results["combined"],
    }
    (out_dir / "factor_scan.json").write_text(
        json.dumps(payload, indent=2, default=float), encoding="utf-8"
    )
    log(f"wrote {out_dir / 'factor_scan.json'}")

    # Per-date IC series for every factor. Kept because the multiple-testing
    # accounting in the report needs the CORRELATION between factors' IC
    # series: 102 heavily overlapping factors are nowhere near 102 independent
    # tests, and the effective number of independent tests can only be
    # estimated from the joint behaviour, not from the factor count.
    ts_path = out_dir / "factor_ic_timeseries.csv.gz"
    with gzip.open(ts_path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["period", "factor", "horizon", "date", "ic", "cross_section"])
        for p in ("discovery", "validation", "combined"):
            for name in names:
                for h in horizons:
                    for d, v in ic_rows[p][name][h]:
                        writer.writerow([p, name, h, d, "" if v is None else f"{v:.6f}", ""])
    log(f"wrote {ts_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
