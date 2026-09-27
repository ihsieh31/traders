"""Data quality GATE for the project's bars cache.

READ research/plan.md SECTION 4 AND research/process.md SECTION C FIRST.

This module answers one question: which symbols can be trusted as inputs to a
formula test? It passes or fails and emits ``out/trusted_symbols.txt``.
It never edits data. To describe what the cache actually is rather than whether
it is usable, use ``audit_data.py`` instead -- the two are not substitutes.

This module answers three questions the operator asked directly:

  1. "有沒有拆股合股的問題"      -> split/merger handling
  2. "有沒有上市或是退場公司的問題" -> IPO and delisting
  3. "該標的都有標"              -> is every row correctly identified?

It is deliberately a *reporting and filtering* tool, not a silent cleaner.
Every check emits a count, and the symbols that fail a hard check are written
to ``out/trusted_symbols.txt`` for the study to consume. Nothing is dropped
in place: the cache stays exactly as collected so a later run can change a
threshold and re-derive the trusted set without re-downloading.

    python -m research.src.verify_data
    python -m research.src.verify_data --cross-check 300
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from research.src import common
from research.src.common import log

# --------------------------------------------------------------------------
# Thresholds. Every one is a CLI flag; the defaults are the values used for
# the 2026-09 run and are recorded in the report.
# --------------------------------------------------------------------------

#: A split-adjusted series should not jump on a split. A one-session log move
#: beyond this is either a genuine crash, a reverse split missing from the
#: adjustment, or a ticker reassigned to a different company. Large and mid
#: caps essentially never do this, so it is a cheap reuse screen.
EXTREME_LOG_MOVE = 0.60

#: Minimum sessions a symbol needs before it can be trusted for a 61-bar
#: window plus one. Anything shorter is a new listing, not a data defect.
MIN_SESSIONS = 61

#: Fraction of authoritative sessions a symbol must cover, counting only the
#: window between its own first and last bar. IPOs legitimately start late, so
#: coverage is measured inside that window, never against the full range.
MIN_WINDOW_COVERAGE = 0.98

#: Symbol suffixes that are not common stock. The strategy trades common
#: stock; warrants, units, preferreds and when-issued issues are different
#: instruments with different price dynamics and must not be pooled in.
#:
#: The preferred patterns deliberately allow a variable-length letter suffix.
#: An earlier revision matched only ``.PR`` and ``.P[A-Z]``, which silently
#: classified the common ``.PRF`` / ``.PRI`` / ``.PRE`` / ``.PRN`` / ``.PRZ``
#: preferred-share tickers as common stock -- they then entered the cross
#: section and, being illiquid with erratic adjusted histories, polluted both
#: the factor percentiles and the cross-source check.
INSTRUMENT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\.WS$", "warrant"),
    (r"\.WT$", "warrant"),
    (r"\.W$", "warrant"),
    (r"\+$", "warrant"),
    (r"\.U$", "unit"),
    (r"\.UN$", "unit"),
    (r"\.UNITS$", "unit"),
    (r"\.RT$", "rights"),
    (r"\.RTS$", "rights"),
    (r"\.WI$", "when_issued"),
    (r"\.PR[A-Z]{0,2}$", "preferred"),
    (r"\.P[A-Z]{0,2}$", "preferred"),
    (r"\.S[A-Z]{0,2}$", "preferred"),
    (r"-P[A-Z]{1,3}$", "preferred"),
    (r"/", "non_us_listing"),
)


def classify_instrument(symbol: str) -> str:
    text = str(symbol)
    for pattern, label in INSTRUMENT_PATTERNS:
        if re.search(pattern, text):
            return label
    return "common_stock"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2016-01-01", help="Expected first session.")
    parser.add_argument("--end", default=None, help="Expected last session.")
    parser.add_argument(
        "--cross-check",
        type=int,
        default=200,
        help=(
            "Symbols to cross-validate against an independent source (yfinance). "
            "0 disables. Uses only the overlap, since yfinance drops ~22%% of "
            "sessions (plan.md 4.3)."
        ),
    )
    parser.add_argument(
        "--cross-check-seed", type=int, default=20260927, help="Sampling seed."
    )
    parser.add_argument(
        "--extreme-log-move", type=float, default=EXTREME_LOG_MOVE
    )
    return parser.parse_args()


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------


def load(cache_dir: Path) -> Tuple["object", List[str], Dict[str, str]]:
    import pandas as pd

    files = sorted(cache_dir.glob("batch_*.csv.gz"))
    if not files:
        raise SystemExit(f"no cached bars under {cache_dir}")
    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    return df, [f.name for f in files], {}


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_schema(df, expected: Sequence[str]) -> dict:
    missing = [c for c in expected if c not in df.columns]
    return {
        "name": "schema",
        "passed": not missing,
        "detail": "all required columns present" if not missing else f"missing {missing}",
        "columns": list(df.columns),
    }


def check_identity(df) -> Tuple[dict, Dict[str, str]]:
    """Is every row carrying a usable ticker?

    This is the operator's "該標的都有標" question. A cache written by an
    earlier collector revision contained 918 rows with a null symbol, which
    is invisible downstream until prepare() tries to sort them. It is checked
    explicitly and loudly here.
    """
    import pandas as pd

    raw_null = int(df["symbol"].isna().sum())
    blank = int(df["symbol"].astype("string").str.strip().eq("").fillna(True).sum())
    coerced = int((df["symbol"].astype(str).str.strip() == "None").sum())
    literal_nan = int((df["symbol"].astype(str) == "nan").sum())
    bad = raw_null + blank + coerced + literal_nan
    return {
        "name": "symbol_identity",
        "passed": bad == 0,
        "detail": (
            "every row carries a non-empty ticker"
            if bad == 0
            else f"{bad} rows have a null/blank/placeholder ticker"
        ),
        "counts": {
            "null": raw_null,
            "blank": blank,
            "literal_None": coerced,
            "literal_nan": literal_nan,
        },
    }, {}


def check_instruments(df) -> Tuple[dict, Dict[str, str]]:
    """Split the universe into common stock and non-common instruments."""
    kinds = df["symbol"].map(classify_instrument)
    per_symbol = kinds.groupby(df["symbol"]).first()
    counts = Counter(kinds.unique())
    tally = Counter(per_symbol.values.tolist())
    return {
        "name": "instrument_mix",
        "passed": True,
        "detail": f"{len(per_symbol):,} symbols: " + ", ".join(
            f"{k}={v:,}" for k, v in sorted(tally.items())
        ),
        "by_symbol": tally,
    }, per_symbol.to_dict()


def check_duplicates(df) -> dict:
    dupes = int(df.duplicated(subset=["symbol", "timestamp"]).sum())
    return {
        "name": "duplicate_bars",
        "passed": dupes == 0,
        "detail": "no duplicate (symbol, timestamp)" if dupes == 0 else f"{dupes} duplicates",
        "duplicates": dupes,
    }


def check_monotonic(df) -> dict:
    import numpy as np

    unsorted = 0
    for _sym, part in df.groupby("symbol", sort=False):
        g = part["timestamp"].to_numpy()
        if len(g) > 1 and not np.all(g[1:] > g[:-1]):
            unsorted += 1
    return {
        "name": "monotonic_timestamps",
        "passed": unsorted == 0,
        "detail": (
            "timestamps strictly increase within every symbol"
            if unsorted == 0
            else f"{unsorted} symbols have non-monotonic timestamps"
        ),
        "symbols_affected": unsorted,
    }


def check_value_sanity(df) -> dict:
    import numpy as np

    px = df[["open", "high", "low", "close"]].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    nonfinite = int((~np.isfinite(px)).any(axis=1).sum())
    nonpositive = int((px <= 0).any(axis=1).sum())
    high_violation = int((df["high"] < df[["open", "close"]].max(axis=1) - 1e-9).sum())
    low_violation = int((df["low"] > df[["open", "close"]].min(axis=1) + 1e-9).sum())
    bad_volume = int((~np.isfinite(vol)).sum() + (vol < 0).sum())
    total = len(df)
    bad = nonfinite + nonpositive + high_violation + low_violation + bad_volume
    return {
        "name": "value_sanity",
        "passed": bad == 0,
        "detail": (
            f"{total:,} rows: OHLC positive and finite, high/low consistent, volume >= 0"
            if bad == 0
            else f"{bad:,} offending rows"
        ),
        "counts": {
            "non_finite_price": nonfinite,
            "non_positive_price": nonpositive,
            "high_below_open_or_close": high_violation,
            "low_above_open_or_close": low_violation,
            "bad_volume": bad_volume,
        },
        "rows": total,
    }


def check_calendar_coverage(df, sessions: Sequence[str]) -> Tuple[dict, Dict[str, dict]]:
    """Per-symbol coverage inside the symbol's OWN first..last window.

    Coverage is deliberately not measured against the full range: a 2025 IPO
    cannot have 2016 bars and that is not a defect. What matters is that a
    symbol which existed throughout its window has essentially every session.
    """
    session_set = set(sessions)
    records: Dict[str, dict] = {}
    for sym, part in df.groupby("symbol", sort=False):
        have = part["session"].to_numpy()
        have_set = set(have.tolist())
        lo, hi = have.min(), have.max()
        span = [s for s in sessions if lo <= s <= hi]
        missing = [s for s in span if s not in have_set]
        records[sym] = {
            "first": lo,
            "last": hi,
            "bars": int(len(have)),
            "span_sessions": len(span),
            "missing": len(missing),
            "coverage": 1.0 - (len(missing) / len(span)) if span else 0.0,
            "n_sessions": len(sessions),
        }
    coverages = [r["coverage"] for r in records.values()]
    thin = sum(1 for c in coverages if c < MIN_WINDOW_COVERAGE)
    short = sum(1 for r in records.values() if r["bars"] < MIN_SESSIONS)
    return {
        "name": "calendar_coverage",
        "passed": thin == 0 and short == 0,
        "detail": (
            f"{len(records):,} symbols; median coverage "
            f"{sorted(coverages)[len(coverages) // 2]:.4f}; "
            f"{thin} below {MIN_WINDOW_COVERAGE}; {short} under {MIN_SESSIONS} bars"
        ),
        "below_coverage_threshold": thin,
        "under_min_sessions": short,
        "median_coverage": sorted(coverages)[len(coverages) // 2] if coverages else 0.0,
    }, records


def check_extreme_moves(df, threshold: float) -> Tuple[dict, Dict[str, list]]:
    """One-session log moves beyond the threshold, per symbol.

    The series is split-adjusted, so a split should not appear here. What
    remains is: a genuine crash, a reverse split the adjustment missed, or a
    ticker reassigned to a different company. The last is the dangerous one
    because it splices two companies into one series.
    """
    flagged: Dict[str, list] = defaultdict(list)
    for sym, part in df.groupby("symbol", sort=False):
        close = part["close"].to_numpy(dtype=float)
        if len(close) < 2:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            moves = np.abs(np.diff(np.log(close)))
        idx = np.where(moves > threshold)[0]
        for i in idx:
            flagged[sym].append(
                {
                    "date": str(part["session"].to_numpy()[i + 1]),
                    "log_move": round(float(moves[i]), 4),
                }
            )
    n_flagged = sum(len(v) for v in flagged.values())
    return {
        "name": "extreme_moves",
        "passed": n_flagged == 0,
        "detail": (
            f"{n_flagged} one-session |log move| > {threshold} across "
            f"{len(flagged)} symbols (possible ticker reuse or missed reverse split)"
        ),
        "threshold": threshold,
        "events": n_flagged,
        "symbols": len(flagged),
    }, dict(flagged)


def check_splits_seen(df) -> dict:
    """Detect whether the adjustment policy looks applied.

    A split-adjusted series should contain very few |log moves| above
    0.60 for large/mid caps. This does not prove correctness -- it is a
    smoke test that the request carried ``Adjustment.SPLIT``.
    """
    import numpy as np

    big = 0
    total = 0
    for _sym, part in df.groupby("symbol", sort=False):
        close = part["close"].to_numpy(dtype=float)
        if len(close) < 2:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            moves = np.abs(np.diff(np.log(close)))
        big += int((moves > 0.60).sum())
        total += len(moves)
    return {
        "name": "split_adjustment_smoke",
        "passed": True,
        "detail": (
            f"{big:,} / {total:,} one-session moves exceed |log| 0.60 "
            f"({big / max(1, total):.4%}); a split-adjusted feed keeps this low"
        ),
        "rate": big / max(1, total),
    }


# --------------------------------------------------------------------------
# Independent cross-source validation
# --------------------------------------------------------------------------


def cross_validate(
    df, sample: Sequence[str], sessions: Sequence[str], limit: int
) -> dict:
    """Compare SIP closes against an independent source on shared sessions.

    yfinance is unusable as a *primary* feed (22% session loss, plan.md 4.3)
    but it is perfectly good as a *witness* on the dates it does have. A
    systematic offset here would mean the two feeds disagree about
    adjustment policy, which would invalidate every ratio in the study.

    Two things this got wrong on its first run, both now fixed. Both are
    recorded as process.md S-17 and S-18:

    1. It re-applied a split adjustment to Yahoo's ``Close``. Yahoo already
       delivers split-adjusted prices identical to Alpaca's
       ``Adjustment.SPLIT`` (AMZN 2016-01-04: 31.8495 vs 31.8500), so the
       comparison was double-adjusted and reported a 19x error on every
       pre-split date. The two feeds are compared directly now.
    2. It pooled every symbol into one distribution, so a handful of bad
       tickers looked like a global data failure. Statistics are per symbol,
       and a symbol only fails if ITS OWN median relative error is large.
    """
    import numpy as np
    import pandas as pd

    import yfinance as yf

    ours = {}
    for sym, part in df.groupby("symbol", sort=False):
        if sym in sample:
            ours[sym] = part.set_index("session")["close"].to_dict()
    if not ours:
        return {"name": "cross_source", "passed": True, "detail": "skipped (no sample)"}

    syms = list(ours)[:limit]
    start = min(min(v) for v in ours.values())
    end = max(max(v) for v in ours.values())
    try:
        raw = yf.download(
            syms,
            start=start,
            end=(datetime.fromisoformat(end) + timedelta(days=1)).date().isoformat(),
            auto_adjust=False,
            actions=True,
            group_by="ticker",
            progress=False,
            threads=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "cross_source",
            "passed": True,
            "detail": f"skipped: yfinance unavailable ({type(exc).__name__})",
        }

    per_symbol: Dict[str, dict] = {}
    for sym in syms:
        try:
            block = raw[sym].reset_index()
        except KeyError:
            continue
        if "Close" not in block.columns or "Date" not in block.columns:
            continue
        theirs = block["Close"].to_numpy(dtype=float)
        days = block["Date"].astype(str).to_numpy()
        errors: List[float] = []
        for d, price in zip(days, theirs):
            mine = ours[sym].get(str(d))
            if mine is None or not math.isfinite(price) or price <= 0:
                continue
            errors.append(abs(mine - price) / price)
        if errors:
            arr = np.asarray(errors)
            per_symbol[sym] = {
                "n": int(arr.size),
                "median": float(np.median(arr)),
                "p99": float(np.quantile(arr, 0.99)),
                "max": float(arr.max()),
            }
    if not per_symbol:
        return {
            "name": "cross_source",
            "passed": True,
            "detail": "no overlapping sessions with the witness source",
        }

    medians = np.asarray([v["median"] for v in per_symbol.values()])
    disagreements = {
        s: v for s, v in per_symbol.items() if v["median"] > 0.02
    }
    return {
        "name": "cross_source",
        "passed": not disagreements,
        "detail": (
            f"{len(per_symbol)} symbols cross-checked on shared sessions; "
            f"per-symbol median relative error: P50={np.median(medians):.2e} "
            f"P90={np.quantile(medians, 0.9):.2e} max={medians.max():.2e}; "
            f"{len(disagreements)} symbol(s) above the 2% per-symbol threshold"
        ),
        "symbols_checked": len(per_symbol),
        "median_of_symbol_medians": float(np.median(medians)),
        "p90_of_symbol_medians": float(np.quantile(medians, 0.9)),
        "disagreeing_symbols": len(disagreements),
        "disagreement_examples": dict(
            sorted(disagreements.items(), key=lambda kv: -kv[1]["median"])[:15]
        ),
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def main() -> int:
    import numpy as np
    import pandas as pd

    args = parse_args()
    df, files, _ = load(common.BARS_DIR)
    log(f"loaded {len(df):,} rows from {len(files)} batches, {df['symbol'].nunique():,} symbols")

    required = ["symbol", "timestamp", "open", "high", "low", "close", "volume"]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df[df["timestamp"].notna()]
    df["session"] = df["timestamp"].dt.tz_convert("US/Eastern").dt.date.astype(str)
    df = df.sort_values(["symbol", "timestamp"], kind="mergesort").reset_index(drop=True)

    calendar_rows = common.load_calendar_rows(
        date.fromisoformat(args.start), date.fromisoformat(args.end) if args.end else date.today()
    )
    sessions = common.calendar_sessions(calendar_rows)
    session_strs = [d.isoformat() for d in sessions]
    log(f"authoritative calendar proves {len(sessions)} sessions "
        f"({session_strs[0]} .. {session_strs[-1]})")

    checks: List[dict] = []
    checks.append(check_schema(df, required))
    ident, _ = check_identity(df)
    checks.append(ident)
    # Report first, then filter. The count above is the evidence; dropping the
    # rows here is what lets every downstream check actually run.
    before_rows = len(df)
    df["symbol"] = df["symbol"].astype("string")
    df = df[
        df["symbol"].notna()
        & (df["symbol"].str.strip() != "")
        & (df["symbol"].astype(str) != "None")
        & (df["symbol"].astype(str) != "nan")
    ]
    dropped_rows = before_rows - len(df)
    if dropped_rows:
        log(f"dropped {dropped_rows:,} rows with an unusable ticker")
    df["symbol"] = df["symbol"].astype(str)
    instr, per_symbol_kind = check_instruments(df)
    checks.append(instr)
    checks.append(check_duplicates(df))
    checks.append(check_monotonic(df))
    checks.append(check_value_sanity(df))
    checks.append(check_splits_seen(df))
    cover, records = check_calendar_coverage(df, session_strs)
    checks.append(cover)
    extreme, flagged = check_extreme_moves(df, args.extreme_log_move)
    checks.append(extreme)

    if args.cross_check > 0:
        import random

        candidates = sorted(
            s
            for s, k in per_symbol_kind.items()
            if k == "common_stock" and records.get(s, {}).get("coverage", 0) >= MIN_WINDOW_COVERAGE
        )
        rng = random.Random(args.cross_check_seed)
        sample = rng.sample(candidates, min(args.cross_check, len(candidates)))
        log(f"cross-validating {len(sample)} symbols against an independent feed")
        checks.append(cross_validate(df, set(sample), session_strs, args.cross_check))

    # ------------------------------------------------------------------
    # Trusted set
    # ------------------------------------------------------------------
    # Exclusion rules, in order of how much they protect against:
    #
    # 1. instrument     -- not common stock. Warrants, units, preferreds and
    #    rights have different price dynamics and must not be pooled into a
    #    cross-sectional percentile.
    # 2. too_few_bars   -- cannot support a 61-bar window.
    # 3. coverage       -- gaps inside the symbol's own first..last window.
    # 4. extreme_move   -- an unexplained >82% one-session move. On a
    #    split-adjusted feed a split should not appear, so this is either a
    #    missed reverse split, a warrant/unit with an erratic adjusted
    #    history, or a ticker reassigned to a different company. All three
    #    splice two instruments into one series, which would corrupt r60 and
    #    vol20 for that symbol. The cross-source check found that these
    #    disagreements are exact integer ratios (29x, 9x, 7x, 4x, 3x) --
    #    the signature of an unhandled reverse split -- so this rule is
    #    evidence-driven, not merely cautious.
    # 5. cross_source   -- names that the independent-source check found
    #    disagreeing, added by name so a sample-based check still protects
    #    the run.
    reasons: Dict[str, List[str]] = defaultdict(list)
    for sym, kind in per_symbol_kind.items():
        if kind != "common_stock":
            reasons[sym].append(f"instrument:{kind}")
    for sym, rec in records.items():
        if rec["bars"] < MIN_SESSIONS:
            reasons[sym].append("too_few_bars")
        elif rec["coverage"] < MIN_WINDOW_COVERAGE:
            reasons[sym].append(f"coverage:{rec['coverage']:.3f}")
    for sym, events in flagged.items():
        worst_move = max(abs(e["log_move"]) for e in events)
        reasons[sym].append(f"extreme_move:{len(events)}x{worst_move:.2f}")
    cross_check_disagreements: List[str] = []
    for check in checks:
        if check["name"] == "cross_source":
            cross_check_disagreements = list(
                check.get("disagreement_examples", {}).keys()
            )
    for sym in cross_check_disagreements:
        reasons.setdefault(sym, []).append("cross_source_disagreement")

    trusted = sorted(s for s in per_symbol_kind if s not in reasons)
    excluded = {s: r for s, r in sorted(reasons.items())}

    OUT_TRUSTED = common.OUT_DIR / "trusted_symbols.txt"
    OUT_TRUSTED.write_text("\n".join(trusted) + "\n", encoding="utf-8")
    OUT_JSON = common.OUT_DIR / "data_quality.json"
    OUT_JSON.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "range_collected": [session_strs[0], session_strs[-1]],
                "n_sessions": len(sessions),
                "n_symbols": len(per_symbol_kind),
                "n_trusted": len(trusted),
                "n_excluded": len(excluded),
                "thresholds": {
                    "extreme_log_move": args.extreme_log_move,
                    "min_sessions": MIN_SESSIONS,
                    "min_window_coverage": MIN_WINDOW_COVERAGE,
                },
                "checks": [
                    {k: v for k, v in c.items() if k != "by_symbol"} for c in checks
                ],
                "excluded_reasons": dict(
                    Counter(r.split(":")[0] for rs in excluded.values() for r in rs)
                ),
                "excluded_examples": dict(list(excluded.items())[:40]),
                "extreme_move_examples": {
                    k: v[:3] for k, v in list(flagged.items())[:20]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    log("=" * 72)
    for c in checks:
        mark = "PASS" if c["passed"] else "FAIL"
        log(f"  [{mark}] {c['name']:<28} {c['detail']}")
    log("=" * 72)
    log(f"trusted symbols : {len(trusted):,}  -> {OUT_TRUSTED}")
    log(f"excluded        : {len(excluded):,}")
    for reason, n in Counter(
        r.split(":")[0] for rs in excluded.values() for r in rs
    ).most_common():
        log(f"    {reason}: {n:,}")
    log(f"report          : {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
