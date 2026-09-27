"""Shared paths, env isolation, calendar and cache for the factor IC study.

READ PLAN.md BEFORE CHANGING ANYTHING HERE.

Two environment constraints shape this module:

1. ``tradingagents/default_config.py`` calls ``load_namespaced_dotenv()`` at
   *module import time*. Every ``TRADINGBUFFETT_*`` key in the operator's
   ``.env`` therefore lands in ``os.environ`` the moment anything under
   ``tradingagents`` is imported. The isolation overrides below MUST be applied
   before the first such import, which is why they run at module top level and
   why nothing in this file imports ``tradingagents`` above them.

2. There is no ``pyarrow`` in this environment, so the bars cache is
   compressed CSV rather than parquet. There is also no ``scipy`` or
   ``statsmodels``: Spearman and Newey-West are implemented locally.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

RESEARCH_ROOT = Path(__file__).resolve().parents[1]

#: Test/redirect hook. The smoke test points this at a temporary directory so
#: it can never clobber real cached bars or a real authoritative calendar.
#: Unset during normal operation.
_DATA_OVERRIDE = os.environ.get("TRADINGBUFFETT_RESEARCH_DATA_DIR", "").strip()

DATA_DIR = Path(_DATA_OVERRIDE) if _DATA_OVERRIDE else RESEARCH_ROOT / "data"
OUT_DIR = (Path(_DATA_OVERRIDE) / "out") if _DATA_OVERRIDE else (RESEARCH_ROOT / "out")
BARS_DIR = DATA_DIR / "bars"
CALENDAR_PATH = DATA_DIR / "calendar.json"

# --------------------------------------------------------------------------
# Environment isolation -- MUST run before any tradingagents import.
# --------------------------------------------------------------------------

ISOLATED_ENV = {
    # Every one of these is a real override read by production code:
    #   EXECUTION_DB   -> execution/service.py:113  (get_env("EXECUTION_DB"))
    #   LONG_RUN_DIR   -> long_run_support/state.py:31,156
    # The rest are read by DEFAULT_CONFIG at tradingagents/default_config.py.
    "TRADINGBUFFETT_EXECUTION_DB": str(DATA_DIR / "execution.sqlite3"),
    "TRADINGBUFFETT_LONG_RUN_DIR": str(DATA_DIR / "long_run"),
    "TRADINGBUFFETT_EXECUTION_LOCK_DIR": str(DATA_DIR / "execution-locks"),
    "TRADINGBUFFETT_RESULTS_DIR": str(DATA_DIR / "results"),
    "TRADINGBUFFETT_CACHE_DIR": str(DATA_DIR / "cache"),
    "TRADINGBUFFETT_MEMORY_LOG_PATH": str(DATA_DIR / "memory.md"),
    "TRADINGBUFFETT_AGENT_MEMORY_DIR": str(DATA_DIR / "agent_memory"),
    "TRADINGBUFFETT_SAFETY_STATE_PATH": str(DATA_DIR / "safety" / "state.json"),
    "TRADINGBUFFETT_SAFETY_KILL_SWITCH_PATH": str(DATA_DIR / "safety" / "KILL_SWITCH"),
    "TRADINGBUFFETT_SCREENING_SELECTION_CACHE_PATH": str(
        DATA_DIR / "screening_selection.json"
    ),
}


def apply_env_isolation() -> None:
    """Point every durable-state path at this research directory.

    Idempotent. Refuses to run if ``~/.tradingbuffett`` is somehow already a
    target, which would mean we are about to write into operator state.
    """
    for key, value in ISOLATED_ENV.items():
        os.environ[key] = value
    for key, value in ISOLATED_ENV.items():
        if os.environ[key] != value:
            raise RuntimeError(f"env isolation failed for {key}")


apply_env_isolation()

for _d in (DATA_DIR, OUT_DIR, BARS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

_T0 = datetime.now(timezone.utc)


def log(message: str) -> None:
    elapsed = (datetime.now(timezone.utc) - _T0).total_seconds()
    print(f"[{elapsed:8.1f}s] {message}", flush=True)


# --------------------------------------------------------------------------
# Production code reuse (imported only after isolation is applied)
# --------------------------------------------------------------------------

from tradingagents.dataflows.market_calendar import (  # noqa: E402
    fetch_trading_calendar,
)
from tradingagents.screening.metrics import (  # noqa: E402
    FACTOR_KEYS,
    SCORE_WEIGHTS,
    EligibilityThresholds,
    SymbolFeatures,
    ascending_percentiles,
    compute_features,
    score_features,
    validate_and_clean_bars,
)

#: The seven factors plus the composite score produced by ``score_features``.
STUDY_SERIES: tuple[str, ...] = FACTOR_KEYS + ("score",)

#: Holding periods measured, in trading days. 1 and 3 bracket the short end,
#: 20/40/60 test whether the r20/r60 horizons carry the edge instead.
HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20, 40, 60)

#: A cross-section thinner than this is skipped: below this the percentile
#: transform in ``score_features`` and the rank correlation are both noise.
MIN_CROSS_SECTION = 20

#: Maximum bars handed to ``validate_and_clean_bars`` per (symbol, as_of).
#: The function only ever keeps the trailing ``required_bars`` window, so a
#: slightly larger slice is sufficient and keeps the per-call cost O(1)
#: instead of O(full history). See PLAN.md section 6.3.
WINDOW_SLACK_BARS = 10


def get_thresholds() -> EligibilityThresholds:
    """Production eligibility thresholds, unmodified.

    ``from_config({})`` yields exactly the production defaults. The
    ``min_market_cap_usd`` field is present and required (it is a frozen
    dataclass that rejects non-positive values) but is NEVER read by
    ``compute_features`` -- market-cap filtering lives only in
    ``scan_universe``, which this study deliberately does not call. See
    PLAN.md section 8.
    """
    return EligibilityThresholds.from_config({})


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------


def load_calendar_rows(start: date, end: date, *, refresh: bool = False) -> List[dict]:
    """Authoritative US session rows covering ``[start - 7d, end + 7d]``.

    One network call, cached to JSON as ``[{"date": "YYYY-MM-DD"}, ...]``.
    ``market_calendar._row_date`` accepts that dict shape, so the cache is
    directly injectable as ``calendar_rows=`` and no per-date call is ever
    made.

    The cache is re-fetched when it does not cover the requested range. A
    stale cache is not a harmless optimisation here: the first 10.7-year
    collection reused a 927-session calendar left behind by an earlier
    3.7-year run, which would have silently compared every coverage check
    against the wrong session set.

    The acceptance test is the UNPADDED ``[start, end]``. The +/-7d padding
    exists only to make the fetch a little wider than the request; requiring
    the cache to cover the padding too is wrong, and was a real regression
    once: ``smoke_test`` seeds a cache covering exactly its synthetic range,
    failed the padded test, silently fell through to the network, and overwrote
    the fixture's calendar with the real one -- which shifted the sampled
    as-of grid and made the study disagree with its own numpy reference.
    """
    want_lo = start.isoformat()
    want_hi = end.isoformat()
    if CALENDAR_PATH.exists() and not refresh:
        try:
            payload = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
            rows = payload.get("rows")
        except (OSError, ValueError):
            rows = None
        if isinstance(rows, list) and rows:
            lo = min(str(r["date"])[:10] for r in rows)
            hi = max(str(r["date"])[:10] for r in rows)
            if lo <= want_lo and hi >= want_hi:
                return rows
            log(
                f"cached calendar covers {lo}..{hi}, need {want_lo}..{want_hi}; "
                "refetching"
            )

    log(f"fetching trading calendar {start}..{end} (single call)")
    from tradingagents.dataflows.market_calendar import _calendar_date_set

    raw = fetch_trading_calendar(start - timedelta(days=7), end + timedelta(days=7))
    proven = sorted(_calendar_date_set(raw))
    if not proven:
        raise RuntimeError("authoritative calendar returned zero sessions")
    rows = [{"date": d.isoformat()} for d in proven]
    CALENDAR_PATH.write_text(
        json.dumps(
            {"fetched_at": datetime.now(timezone.utc).isoformat(), "rows": rows},
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"calendar cached: {len(rows)} sessions -> {CALENDAR_PATH}")
    return rows


def calendar_sessions(calendar_rows: Sequence[dict]) -> List[date]:
    """ISO date strings -> sorted ``date`` objects."""
    return sorted(date.fromisoformat(str(r["date"])[:10]) for r in calendar_rows)


# --------------------------------------------------------------------------
# Bars cache (compressed CSV per batch; no pyarrow in this environment)
# --------------------------------------------------------------------------

BAR_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def batch_cache_path(batch_index: int) -> Path:
    return BARS_DIR / f"batch_{batch_index:05d}.csv.gz"


def save_batch(batch_index: int, frame) -> int:
    """Persist one symbol batch. Returns the row count written."""
    path = batch_cache_path(batch_index)
    frame.to_csv(path, index=False, compression="gzip")
    return len(frame)


def load_all_bars() -> "object":
    """Concatenate every cached batch into one DataFrame.

    Returns an empty DataFrame (not None) when nothing is cached so callers
    can report a clear "run collect_bars first" message.
    """
    import pandas as pd

    files = sorted(BARS_DIR.glob("batch_*.csv.gz"))
    if not files:
        return pd.DataFrame(columns=list(BAR_COLUMNS))
    frames = [pd.read_csv(f) for f in files]
    out = pd.concat(frames, ignore_index=True)
    return out


def bars_coverage(frame) -> str:
    if frame is None or len(frame) == 0:
        return "no cached bars"
    stamps = frame["timestamp"]
    return (
        f"{frame['symbol'].nunique()} symbols, {len(frame)} rows, "
        f"{stamps.min()} .. {stamps.max()}"
    )


# --------------------------------------------------------------------------
# Statistics (no scipy / statsmodels in this environment)
# --------------------------------------------------------------------------


def spearman_ic(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation, reusing production tie handling.

    Pearson correlation of average-rank percentiles *is* Spearman, and
    ``ascending_percentiles`` already implements the average-rank tie rule the
    production screen uses. Reusing it keeps the study's tie behaviour
    identical to the screen's.

    Returns NaN when either side is constant (zero variance) or n < 2.
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return float("nan")
    px = ascending_percentiles([float(v) for v in xs])
    py = ascending_percentiles([float(v) for v in ys])
    mx = sum(px) / n
    my = sum(py) / n
    num = sum((a - mx) * (b - my) for a, b in zip(px, py))
    dx = math.sqrt(sum((a - mx) ** 2 for a in px))
    dy = math.sqrt(sum((b - my) ** 2 for b in py))
    if dx == 0.0 or dy == 0.0:
        return float("nan")
    return num / (dx * dy)


def newey_west(series: Iterable[float], lag: int) -> tuple[float, float]:
    """Newey-West adjusted t-statistic for the sample mean.

    Overlapping 61-bar factor windows make the daily IC series strongly
    autocorrelated, so an ordinary t-statistic is badly inflated. This is the
    Bartlett-kernel long-run variance estimator applied to the mean:

        lrv = g0 + 2 * sum_{j=1..L} (1 - j/(L+1)) * gj
        t   = mean / sqrt(lrv / n)

    Returns ``(mean, t_stat)``; ``t_stat`` is NaN when the long-run variance
    is not positive or n is too small.

    Normalisation note: autocovariances use ``1/n``, the Newey-West (1987) /
    Andrews (1991) convention that makes the estimator consistent. The
    textbook t-test uses ``1/(n-1)``. The two differ by a factor of
    ``sqrt(n/(n-1))``, so at lag=0 this returns
    ``t_textbook * sqrt(n/(n-1))`` rather than ``t_textbook``. That is
    expected, not a bug.

    PLAN.md section 3.3: a study that reports only ordinary t-statistics is
    invalid.
    """
    import numpy as np

    x = np.asarray(
        [v for v in series if v is not None and math.isfinite(float(v))], dtype=float
    )
    n = x.size
    if n < 3:
        return (float(x.mean()) if n else float("nan")), float("nan")
    dev = x - x.mean()
    lrv = float(np.dot(dev, dev)) / n
    upper = min(int(lag), n - 1)
    for j in range(1, upper + 1):
        weight = 1.0 - j / (lag + 1.0)
        lrv += 2.0 * weight * float(np.dot(dev[j:], dev[:-j])) / n
    if not math.isfinite(lrv) or lrv <= 0.0:
        return float(x.mean()), float("nan")
    return float(x.mean()), float(x.mean() / math.sqrt(lrv / n))


def describe(values: Sequence[float]) -> dict:
    """Summary block used everywhere in the outputs."""
    import numpy as np

    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return {"n": 0, "mean": None, "median": None, "std": None, "min": None, "max": None}
    arr = np.asarray(clean, dtype=float)
    return {
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "std": float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


def distribution(values: Sequence[float]) -> dict:
    """Percentile block for forward-return distributions (PLAN.md 7.1)."""
    import numpy as np

    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return {}
    arr = np.asarray(clean, dtype=float)
    return {
        "n": int(arr.size),
        "p5": float(np.percentile(arr, 5)),
        "p25": float(np.percentile(arr, 25)),
        "median": float(np.median(arr)),
        "p75": float(np.percentile(arr, 75)),
        "p95": float(np.percentile(arr, 95)),
        "median_abs": float(np.median(np.abs(arr))),
    }


def require_credentials() -> None:
    """Fail loudly and early rather than mid-run with a confusing error."""
    missing = [
        name
        for name in ("TRADINGBUFFETT_ALPACA_API_KEY", "TRADINGBUFFETT_ALPACA_SECRET_KEY")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        print(
            "missing Alpaca credentials in the environment: "
            + ", ".join(missing)
            + "\nSet them in the project .env (paper keys are sufficient; this "
            "study only reads historical bars).",
            file=sys.stderr,
        )
        raise SystemExit(2)
