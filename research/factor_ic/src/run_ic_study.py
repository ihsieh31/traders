"""Factor IC study: does the production screening score predict returns?

READ PLAN.md IN FULL BEFORE CHANGING ANYTHING HERE. The scope boundary
(PLAN.md section 1) is hard: this measures rank information coefficients. It
is NOT a backtest engine, NOT a portfolio simulator, and it never models
position sizing, turnover, or execution costs.

What it does, per authoritative session date:
  1. validate + compute factors with the PRODUCTION functions
     (validate_and_clean_bars -> compute_features -> score_features)
  2. build a next-open forward return for every eligible symbol
  3. Spearman rank IC per factor and per holding horizon, cross-sectionally
  4. aggregate over time with a Newey-West t-statistic (PLAN.md 3.3)

    python -m research.factor_ic.src.run_ic_study --start 2023-01-01
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from research.factor_ic.src import common
from research.factor_ic.src.common import (
    HORIZONS,
    MIN_CROSS_SECTION,
    STUDY_SERIES,
    WINDOW_SLACK_BARS,
    log,
)

#: PLAN.md section 4.5 robustness check: this many sessions between samples.
NON_OVERLAP_STEP = 21

#: Bars handed to validate_and_clean_bars. The function keeps only the
#: trailing ``required_bars``, so a small slack is enough and makes the
#: per-call cost independent of history depth.


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2023-01-01", help="First session date.")
    parser.add_argument("--end", default=None, help="Last session date. Default: today.")
    parser.add_argument(
        "--date-step",
        type=int,
        default=5,
        help=(
            "Sample every Nth session. Default 5 (weekly), which is well matched "
            "to a 5-15 day holding period and reduces overlap autocorrelation. "
            "Use 1 for the full daily series."
        ),
    )
    parser.add_argument(
        "--max-symbols", type=int, default=0, help="Cap symbols for a smoke test. 0 = all."
    )
    parser.add_argument(
        "--symbols-file",
        default=None,
        help=(
            "Newline-separated symbol allow-list. Point this at "
            "out/trusted_symbols.txt produced by verify_data.py so the study "
            "only scores instruments that passed the data-quality checks."
        ),
    )
    parser.add_argument(
        "--horizons",
        default="",
        help="Comma-separated holding periods. Default: all of "
        + ",".join(str(h) for h in HORIZONS),
    )
    return parser.parse_args()


# --------------------------------------------------------------------------
# Data preparation
# --------------------------------------------------------------------------


def prepare(raw, sessions: Sequence[date], max_symbols: int):
    """Build compact per-symbol arrays for O(1) window slicing and forward returns.

    Returns ``(store, symbols)`` where ``store`` maps a symbol to a dict of
    numpy arrays aligned to that symbol's own bar index:

        gidx    int32   global session position of each bar
        o/h/l/c float64 raw OHLC
        v       float64 volume

    numpy arrays rather than per-symbol DataFrames on purpose: the full US
    universe is ~13.5k symbols x ~927 sessions, and 13.5k pandas objects cost
    more memory than the data itself. The production validation still receives
    a real DataFrame per (symbol, as_of) -- it is built on demand from a
    71-row slice, so the pandas cost is bounded.
    """
    import numpy as np
    import pandas as pd

    sidx = {d: i for i, d in enumerate(sessions)}

    raw = raw.copy()
    # Defensive: a cache written by an older/edge-case collector can contain
    # null or non-string symbols (observed once as 918 unlabelled rows in a
    # single batch). Coerce and drop rather than crash on the sort -- and say
    # so, because silently losing a symbol would distort the cross-section.
    raw["symbol"] = raw["symbol"].astype("string")
    null_symbols = int(raw["symbol"].isna().sum() + (raw["symbol"].str.strip() == "").sum())
    if null_symbols:
        log(f"WARNING: dropping {null_symbols} rows with a null/empty symbol")
        raw = raw[raw["symbol"].notna() & (raw["symbol"].str.strip() != "")]
    raw["symbol"] = raw["symbol"].astype(str)
    dropped_symbols = sorted(set(raw["symbol"].unique()) - set(raw["symbol"].dropna()))

    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce")
    raw = raw[raw["timestamp"].notna()]
    raw = raw.drop_duplicates(subset=["symbol", "timestamp"], keep="last")
    # Session date in ET, matching validate_and_clean_bars (metrics.py:213).
    raw["session"] = raw["timestamp"].dt.tz_convert("US/Eastern").dt.date
    raw["gidx"] = raw["session"].map(sidx)
    raw = raw[raw["gidx"].notna()]
    raw["gidx"] = raw["gidx"].astype(np.int32)
    raw = raw.sort_values(["symbol", "gidx"], kind="mergesort")

    symbols = sorted(raw["symbol"].unique().tolist())
    if max_symbols > 0 and len(symbols) > max_symbols:
        step = len(symbols) / max_symbols
        symbols = sorted({symbols[int(i * step)] for i in range(max_symbols)})
    keep = set(symbols)
    raw = raw[raw["symbol"].isin(keep)]

    store: Dict[str, Dict[str, object]] = {}
    for sym, part in raw.groupby("symbol", sort=False):
        store[sym] = {
            "gidx": part["gidx"].to_numpy(dtype=np.int32),
            "timestamp": part["timestamp"].to_numpy(),
            "o": part["open"].to_numpy(dtype=float),
            "h": part["high"].to_numpy(dtype=float),
            "l": part["low"].to_numpy(dtype=float),
            "c": part["close"].to_numpy(dtype=float),
            "v": part["volume"].to_numpy(dtype=float),
        }
    log(f"prepared {len(store)} symbols x {len(sessions)} sessions")
    if dropped_symbols:
        log(f"  ({len(dropped_symbols)} symbol(s) had no usable bars and were excluded)")
    return store, symbols


def _slice_frame(entry: Dict[str, object], as_of_gidx: int, slack: int):
    """Build the small DataFrame ``validate_and_clean_bars`` expects.

    Passes the trailing ``slack`` bars at or before ``as_of_gidx``. The
    production function keeps only the trailing ``required_bars`` and performs
    the authoritative calendar comparison itself, so pre-slicing here changes
    no decision -- it only makes the per-call cost independent of how much
    history a symbol has.
    """
    import numpy as np
    import pandas as pd

    gidx = entry["gidx"]
    end = int(np.searchsorted(gidx, as_of_gidx, side="right"))
    if end == 0:
        return None
    start = max(0, end - slack)
    return pd.DataFrame(
        {
            "timestamp": entry["timestamp"][start:end],
            "open": entry["o"][start:end],
            "high": entry["h"][start:end],
            "low": entry["l"][start:end],
            "close": entry["c"][start:end],
            "volume": entry["v"][start:end],
        }
    )


def forward_next_open(entry: Dict[str, object], as_of_gidx: int, n_sessions: int, horizon: int):
    """close[as_of + horizon] / open[as_of + 1] - 1  (PLAN.md 4.1).

    Entry is the next session's open because the decision is made during or
    after the as_of session, so an as_of-close fill would be look-ahead. This
    matches the existing backtest convention (backtest/engine.py:136-137).
    Returns None when either leg is unproven or the entry is <= 0.
    """
    import numpy as np

    gidx = entry["gidx"]
    entry_i = as_of_gidx + 1
    exit_i = as_of_gidx + horizon
    if entry_i >= n_sessions or exit_i >= n_sessions:
        return None
    # Every bar's gidx is a proven calendar session and the arrays are sorted,
    # so an exact lookup is a binary search for a value we know exists.
    k_entry = int(np.searchsorted(gidx, entry_i, side="left"))
    k_exit = int(np.searchsorted(gidx, exit_i, side="left"))
    if k_entry >= gidx.size or gidx[k_entry] != entry_i:
        return None
    if k_exit >= gidx.size or gidx[k_exit] != exit_i:
        return None
    entry_price = float(entry["o"][k_entry])
    exit_price = float(entry["c"][k_exit])
    if not (math.isfinite(entry_price) and math.isfinite(exit_price)) or entry_price <= 0:
        return None
    return exit_price / entry_price - 1.0


def close_to_close(entry: Dict[str, object], as_of_gidx: int, n_sessions: int, horizon: int):
    """close[as_of + horizon] / close[as_of] - 1  (PLAN.md 4.1).

    LOOK-AHEAD BIASED. Reported only so the numbers can be lined up against
    academic momentum literature, which conventionally uses close-to-close.
    Must never be the basis of a conclusion.
    """
    import numpy as np

    gidx = entry["gidx"]
    exit_i = as_of_gidx + horizon
    if exit_i >= n_sessions:
        return None
    k_base = int(np.searchsorted(gidx, as_of_gidx, side="left"))
    k_exit = int(np.searchsorted(gidx, exit_i, side="left"))
    if k_base >= gidx.size or gidx[k_base] != as_of_gidx:
        return None
    if k_exit >= gidx.size or gidx[k_exit] != exit_i:
        return None
    base = float(entry["c"][k_base])
    exit_ = float(entry["c"][k_exit])
    if not (math.isfinite(base) and math.isfinite(exit_)) or base <= 0:
        return None
    return exit_ / base - 1.0


# --------------------------------------------------------------------------
# Main study
# --------------------------------------------------------------------------


def eligible_on(
    store: Dict[str, Dict[str, object]],
    symbols: Sequence[str],
    as_of: date,
    as_of_gidx: int,
    thresholds,
    calendar_rows,
    required: int,
):
    """Run the production validation + factor pipeline for one session date.

    Mirrors ``scan_universe`` (metrics.py:464-523) minus the market-cap and
    quarantine steps this study deliberately omits (PLAN.md 3.2). Every
    per-symbol decision is delegated to the production functions.
    """
    features = []
    exclusions: Dict[str, int] = {}
    slack = required + WINDOW_SLACK_BARS
    for sym in symbols:
        entry = store.get(sym)
        if entry is None:
            exclusions["no_frame"] = exclusions.get("no_frame", 0) + 1
            continue
        sub = _slice_frame(entry, as_of_gidx, slack)
        if sub is None or len(sub) < required:
            exclusions["insufficient_bars"] = exclusions.get("insufficient_bars", 0) + 1
            continue
        window, exclusion = common.validate_and_clean_bars(
            sym,
            sub,
            as_of=as_of,
            thresholds=thresholds,
            calendar_rows=calendar_rows,
        )
        if exclusion:
            exclusions[exclusion] = exclusions.get(exclusion, 0) + 1
            continue
        feats, factor_exclusion = common.compute_features(sym, window, thresholds=thresholds)
        if factor_exclusion:
            exclusions[factor_exclusion] = exclusions.get(factor_exclusion, 0) + 1
            continue
        features.append(feats)
    return features, exclusions


def main() -> int:
    args = parse_args()
    horizons = (
        tuple(int(h) for h in args.horizons.split(",") if h.strip())
        if args.horizons.strip()
        else HORIZONS
    )
    if args.date_step < 1:
        print("--date-step must be >= 1", flush=True)
        return 2

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()

    raw = common.load_all_bars()
    if len(raw) == 0:
        print(
            "no cached bars. Run: python -m research.factor_ic.src.collect_bars",
            flush=True,
        )
        return 2
    log(f"cache: {common.bars_coverage(raw)}")

    calendar_rows = common.load_calendar_rows(start, end)
    sessions = common.calendar_sessions(calendar_rows)
    log(f"calendar proves {len(sessions)} sessions")

    store, symbols = prepare(raw, sessions, args.max_symbols)
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
        log(
            f"symbols-file filter: {before:,} -> {len(symbols):,} "
            f"({len(symbols) / max(1, before):.1%} retained) from {args.symbols_file}"
        )
        if len(symbols) < MIN_CROSS_SECTION:
            print(
                f"only {len(symbols)} symbols survived the filter; "
                f"need at least {MIN_CROSS_SECTION}",
                flush=True,
            )
            return 2

    thresholds = common.get_thresholds()
    required = thresholds.required_bars
    max_h = max(horizons)
    n_sessions = len(sessions)

    # A usable as_of needs `required` sessions of history behind it and
    # `max_h` sessions of forward data after it.
    first = required - 1
    last = n_sessions - 1 - max_h
    if last < first:
        print(
            f"not enough sessions: need >= {required + max_h + 1}, calendar has {n_sessions}",
            flush=True,
        )
        return 2

    as_of_indices = list(range(first, last + 1, args.date_step))
    log(
        f"study: {len(as_of_indices)} sampled sessions "
        f"({sessions[first]} .. {sessions[last]}), step={args.date_step}, "
        f"horizons={list(horizons)}"
    )

    # ic_rows[series][horizon] -> list of (date, ic)
    ic_rows: Dict[str, Dict[int, List[Tuple[str, float]]]] = {
        s: {h: [] for h in horizons} for s in STUDY_SERIES
    }
    fwd_samples: Dict[int, List[float]] = {h: [] for h in horizons}
    c2c_samples: Dict[int, List[float]] = {h: [] for h in horizons}
    cross_section_sizes: List[int] = []
    exclusion_totals: Dict[str, int] = {}
    skipped_thin = 0

    import numpy as np

    for n_done, i in enumerate(as_of_indices, start=1):
        as_of = sessions[i]
        features, exclusions = eligible_on(
            store, symbols, as_of, i, thresholds, calendar_rows, required
        )
        for key, count in exclusions.items():
            exclusion_totals[key] = exclusion_totals.get(key, 0) + count
        cross_section_sizes.append(len(features))
        if len(features) < MIN_CROSS_SECTION:
            skipped_thin += 1
            continue

        scored = common.score_features(features)
        score_by_symbol = {f.symbol: f.score for f in scored}
        # Attach the composite score onto the feature objects once so every
        # per-horizon loop can read all series off the same row.
        for feats in features:
            if feats.score is None:
                feats.score = score_by_symbol.get(feats.symbol)

        for horizon in horizons:
            if i + 1 >= n_sessions or i + horizon >= n_sessions:
                continue
            pairs = []
            for feats in features:
                entry = store[feats.symbol]
                forward = forward_next_open(entry, i, n_sessions, horizon)
                if forward is None:
                    continue
                pairs.append((feats, forward))
            if len(pairs) < MIN_CROSS_SECTION:
                continue
            returns = [p[1] for p in pairs]
            fwd_samples[horizon].extend(returns)
            c2c = [
                value
                for value in (
                    close_to_close(store[feats.symbol], i, n_sessions, horizon)
                    for feats, _fr in pairs
                )
                if value is not None
            ]
            c2c_samples[horizon].extend(c2c)
            for series_name in STUDY_SERIES:
                xs = [getattr(feats, series_name) for feats, _fr in pairs]
                value = common.spearman_ic(xs, returns)
                ic_rows[series_name][horizon].append((as_of.isoformat(), value))

        if n_done % 25 == 0 or n_done == len(as_of_indices):
            log(
                f"  {n_done:4d}/{len(as_of_indices)} sessions  "
                f"{as_of}  cross-section={len(features)}"
            )

    # ----------------------------------------------------------------------
    # Aggregate
    # ----------------------------------------------------------------------

    def summarise(series: Sequence[Tuple[str, float]], lag: int) -> dict:
        values = [v for _d, v in series]
        mean, t_stat = common.newey_west(values, lag)
        finite = [v for v in values if math.isfinite(v)]
        positive = (
            sum(1 for v in finite if v > 0) / len(finite) if finite else None
        )
        stats = common.describe(values)
        return {
            "ic_mean": None if not math.isfinite(mean) else mean,
            "ic_t": None if not math.isfinite(t_stat) else t_stat,
            "ic_positive_ratio": positive,
            "n": stats["n"],
            "ic_median": stats["median"],
            "ic_std": stats["std"],
        }

    def nw_lag(horizon: int) -> int:
        """Newey-West lags in SAMPLE units, not sessions.

        The IC series is sampled every ``date_step`` sessions, so a factor
        window of H sessions overlaps across roughly ``H / date_step``
        consecutive observations. Passing H directly over-corrects (adds lags
        that carry no autocorrelation), which makes the t-statistic LESS
        conservative -- the wrong direction for a significance claim. An
        earlier version of this study had exactly that bug; with
        ``--date-step 5`` it inflated |t| by roughly 0.3-1.5 across the table.
        """
        return max(1, math.ceil(horizon / max(1, args.date_step)))

    main_results: Dict[str, Dict[str, dict]] = {}
    for series_name in STUDY_SERIES:
        main_results[series_name] = {}
        for horizon in horizons:
            main_results[series_name][str(horizon)] = summarise(
                ic_rows[series_name][horizon], nw_lag(horizon)
            )

    # Non-overlapping robustness check (PLAN.md 3.3 / 4.5).
    #
    # WARNING: with ``date_step=5`` over ~160 sampled sessions this thins to
    # only ~8 observations, which is far too few for the check to carry
    # information -- its standard error is roughly 5x the main table's. It is
    # reported with its own n and an explicit underpowered flag, and must NOT
    # be read as contradicting the main table when n is small.
    non_overlap: Dict[str, Dict[str, dict]] = {}
    keep = set(range(0, len(as_of_indices), NON_OVERLAP_STEP))
    for series_name in STUDY_SERIES:
        non_overlap[series_name] = {}
        for horizon in horizons:
            thinned = [
                ic_rows[series_name][horizon][k]
                for k in sorted(keep)
                if k < len(ic_rows[series_name][horizon])
            ]
            cell = summarise(thinned, 1)
            cell["underpowered"] = cell["n"] < 20
            non_overlap[series_name][str(horizon)] = cell
    non_overlap_n = max(
        (non_overlap[STUDY_SERIES[0]][str(h)]["n"] for h in horizons), default=0
    )

    # ----------------------------------------------------------------------
    # Outputs
    # ----------------------------------------------------------------------

    meta = {
        "as_of_range": [sessions[first].isoformat(), sessions[last].isoformat()],
        "sampled_sessions": len(as_of_indices),
        "date_step": args.date_step,
        "n_symbols_cached": len(symbols),
        "median_cross_section": (
            sorted(cross_section_sizes)[len(cross_section_sizes) // 2]
            if cross_section_sizes
            else 0
        ),
        "skipped_thin_cross_sections": skipped_thin,
        "horizons": list(horizons),
        "series": list(STUDY_SERIES),
        "thresholds": {
            "min_price": thresholds.min_price,
            "min_adv20_usd": thresholds.min_adv20_usd,
            "min_market_cap_usd": thresholds.min_market_cap_usd,
            "required_bars": required,
        },
        "market_cap_filter_applied": False,
        "entry_convention": "next_open",
        "t_stat_method": "newey_west",
        "newey_west_lag_rule": "max(1, ceil(horizon / date_step))",
        "non_overlap_step": NON_OVERLAP_STEP,
        "non_overlap_n": non_overlap_n,
        "non_overlap_underpowered": non_overlap_n < 20,
        "exclusion_totals": exclusion_totals,
        "limitations": [
            "survivorship_bias: universe is today's ACTIVE tradable US equities",
            "no_point_in_time_market_cap: screening_min_market_cap_usd not applied",
            "split_only_adjustment: dividends not reflected in forward returns",
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    payload = {
        "meta": meta,
        "main": main_results,
        "non_overlapping": non_overlap,
        "forward_return_distribution_next_open": {
            str(h): common.distribution(fwd_samples[h]) for h in horizons
        },
        "forward_return_distribution_close_to_close": {
            str(h): common.distribution(c2c_samples[h]) for h in horizons
        },
    }

    json_path = common.OUT_DIR / "ic_summary.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"wrote {json_path}")

    csv_path = common.OUT_DIR / "ic_timeseries.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["series", "horizon", "as_of", "ic"])
        for series_name in STUDY_SERIES:
            for horizon in horizons:
                for as_of_iso, value in ic_rows[series_name][horizon]:
                    writer.writerow([series_name, horizon, as_of_iso, value])
    log(f"wrote {csv_path}")

    md_path = common.OUT_DIR / "ic_report.md"
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    log(f"wrote {md_path}")

    _print_console_table(payload)
    log("=" * 72)
    log("Done. Read out/ic_report.md section 6 first: it lists what this study")
    log("does NOT answer, and the known biases, before you read any number.")
    return 0


def render_markdown(payload: dict) -> str:
    meta = payload["meta"]
    main = payload["main"]
    non_overlap = payload["non_overlapping"]
    horizons = [str(h) for h in meta["horizons"]]

    lines: List[str] = []
    add = lines.append
    add("# 因子 IC 研究報告")
    add("")
    add(f"產生時間：{meta['generated_at']}")
    add("")
    add("## 1. 樣本")
    add("")
    add(f"- 期間：`{meta['as_of_range'][0]}` .. `{meta['as_of_range'][1]}`")
    add(f"- 取樣 session 數：{meta['sampled_sessions']}（step={meta['date_step']}）")
    add(f"- 快取標的數：{meta['n_symbols_cached']}")
    add(f"- 橫斷面中位數：{meta['median_cross_section']}")
    add(f"- 因橫斷面過窄而跳過：{meta['skipped_thin_cross_sections']}")
    add(f"- 進場慣例：`{meta['entry_convention']}`")
    add(f"- t-stat 方法：`{meta['t_stat_method']}`")
    add(f"- 市值篩選：{'已套用' if meta['market_cap_filter_applied'] else '未套用（見 §6）'}")
    add("")
    add("## 2. IC 主表（Newey-West t-stat，lag = 持有期）")
    add("")
    add("| 因子 | " + " | ".join(f"{h}d" for h in horizons) + " |")
    add("|---|" + "---|" * len(horizons))
    for series_name in meta["series"]:
        cells = []
        for h in horizons:
            cell = main[series_name][h]
            if cell["ic_mean"] is None:
                cells.append("n/a")
            else:
                cells.append(
                    f"{cell['ic_mean']:+.4f}<br>t={cell['ic_t']:+.2f}<br>"
                    f"正號 {cell['ic_positive_ratio']:.0%}"
                )
        add(f"| `{series_name}` | " + " | ".join(cells) + " |")
    add("")
    add("## 3. non-overlapping 穩健性檢查")
    add("")
    if meta.get("non_overlap_underpowered"):
        add(
            f"> **此檢查統計效力不足，不可用。** 取樣 {meta['sampled_sessions']} 個 session、"
            f"每 {meta['non_overlap_step']} 個取一個，僅剩 **{meta.get('non_overlap_n')} 個觀測**，"
            "其標準誤約為主表的 5 倍。"
        )
        add("> 因子窗口為 61 個 session，在 3.7 年樣本內無法取得足夠的非重疊觀測。")
        add("> **請勿**把本節的數值當作與主表矛盾的證據。")
    else:
        add(f"每 {meta['non_overlap_step']} 個 session 取樣，n={meta.get('non_overlap_n')}")
    add("")
    add("| 因子 | " + " | ".join(f"{h}d" for h in horizons) + " |")
    add("|---|" + "---|" * len(horizons))
    for series_name in meta["series"]:
        cells = []
        for h in horizons:
            cell = non_overlap[series_name][h]
            cells.append("n/a" if cell["ic_mean"] is None else f"{cell['ic_mean']:+.4f}")
        add(f"| `{series_name}` | " + " | ".join(cells) + " |")
    add("")
    add("## 4. 前瞻報酬分布（next-open 進場）")
    add("")
    add("| 持有期 | n | p5 | p25 | median | p75 | p95 | median\\|abs\\| |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for h in horizons:
        d = payload["forward_return_distribution_next_open"].get(h) or {}
        if not d:
            continue
        add(
            f"| {h}d | {d['n']} | {d['p5']:+.2%} | {d['p25']:+.2%} | "
            f"{d['median']:+.2%} | {d['p75']:+.2%} | {d['p95']:+.2%} | {d['median_abs']:.2%} |"
        )
    add("")
    add("成本侵蝕估算請用 `median` 與 `median|abs|` 的比值；**IC 本身不含成本**。")
    add("")
    add("## 5. 排除統計")
    add("")
    add("| 原因 | 次數 |")
    add("|---|---:|")
    for key, count in sorted(meta["exclusion_totals"].items(), key=lambda kv: -kv[1]):
        add(f"| `{key}` | {count} |")
    add("")
    add("## 6. 本研究不回答什麼 / 已知偏差")
    add("")
    add("**不回答**：")
    add("")
    add("- 完整生產管線（含市值篩選與 LLM 覆蓋層）是否有 edge")
    add("- 任何 P&L、部位大小、換手或交易成本後的報酬")
    add("- 存活於歷史期間但今天已不在 universe 的標的")
    add("")
    add("**已知偏差**：")
    add("")
    for item in meta["limitations"]:
        add(f"- `{item}`")
    add("")
    add("**close-to-close 對照**（有 look-ahead bias，僅供與文獻對比，不得作為結論）：")
    add("")
    add("| 持有期 | n | median |")
    add("|---|---:|---:|")
    for h in horizons:
        d = payload["forward_return_distribution_close_to_close"].get(h) or {}
        if not d:
            continue
        add(f"| {h}d | {d['n']} | {d['median']:+.2%} |")
    add("")
    add("## 7. 判讀")
    add("")
    add("依 PLAN.md §10 的預先寫定規則判讀，**不得事後調整門檻或期間後只報有利結果**。")
    add("")
    return "\n".join(lines)


def _print_console_table(payload: dict) -> None:
    meta = payload["meta"]
    horizons = [str(h) for h in meta["horizons"]]
    log("")
    log("IC 主表（mean / Newey-West t）")
    log(f"{'factor':<16}" + "".join(f"{h + 'd':>16}" for h in horizons))
    for series_name in meta["series"]:
        cells = ""
        for h in horizons:
            c = payload["main"][series_name][h]
            cells += (
                f"{'n/a':>16}"
                if c["ic_mean"] is None
                else f"{c['ic_mean']:+.4f}/{c['ic_t']:+.1f}".rjust(16)
            )
        log(f"{series_name:<16}" + cells)


if __name__ == "__main__":
    raise SystemExit(main())
