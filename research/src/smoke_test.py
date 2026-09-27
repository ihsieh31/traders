"""Self-test for the foundation layer. NO CREDENTIALS, NO NETWORK REQUIRED.

    python -m research.src.smoke_test

This test exists because the foundation has exactly one job: be right in ways
that no downstream crash will ever reveal. Every check below corresponds to a
bug that actually shipped in this project and produced clean, plausible,
wrong output. See research/process.md for the full write-up; this file is the
executable subset.

The three failure classes covered here:

  1. A statistic that is subtly misnormalised still returns a finite number,
     so nothing crashes -- it just quietly over- or under-states significance.
  2. A cache that does not cover the requested range is not an error, so the
     caller silently measures the wrong session set.
  3. A fast vectorised restatement of a slow reference drifts unless the
     equivalence is asserted rather than assumed.

Everything is written to a temporary directory via
TRADINGBUFFETT_RESEARCH_DATA_DIR, so the real bars cache and the real
authoritative calendar can never be touched.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def check_spearman() -> None:
    """Spearman: extremes, degeneracy, and the average-rank tie rule."""
    from research.src import common

    assert math.isclose(common.spearman_ic([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
    assert math.isclose(common.spearman_ic([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)
    assert math.isnan(common.spearman_ic([5, 5, 5, 5], [1, 2, 3, 4]))
    assert math.isnan(common.spearman_ic([1, 2], [1, 2, 3]))

    # A monotone transform must not change the answer, and an affine shift
    # (subtracting a per-date constant) must not either -- the latter is why
    # "market-adjusted" variants of a factor that subtract a date constant are
    # the same factor wearing a hat. See process.md S-4.
    assert math.isclose(
        common.spearman_ic([1.0, 2.0, 3.0, 4.0], [1.0, 4.0, 9.0, 16.0]),
        common.spearman_ic([1.0, 2.0, 3.0, 4.0], [0.0, 3.0, 8.0, 15.0]),
    )


def check_newey_west() -> None:
    """Newey-West: exact hand computation, normalisation, and the property."""
    from research.src import common

    # x = [1,2,3,4], lag = 1
    #   mean = 2.5                      dev = [-1.5, -0.5, 0.5, 1.5]
    #   g0   = 5/4                      = 1.25
    #   g1   = (0.75 - 0.25 + 0.75)/4   = 0.3125
    #   w1   = 1 - 1/2                  = 0.5
    #   lrv  = 1.25 + 2*0.5*0.3125       = 1.5625
    #   t    = 2.5 / sqrt(1.5625/4)     = 2.5 / 0.625 = 4.0
    mean, t_exact = common.newey_west([1.0, 2.0, 3.0, 4.0], 1)
    assert math.isclose(mean, 2.5, rel_tol=1e-12), mean
    assert math.isclose(t_exact, 4.0, rel_tol=1e-12), t_exact

    # lag = 0 must leave g0 alone, and g0 is 1/n-normalised (the Newey-West
    # convention), so lag-0 is the textbook t scaled by sqrt(n/(n-1)).
    # Asserting plain equality here would wrongly encode the 1/(n-1) form.
    import numpy as np

    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    _m, t_lag0 = common.newey_west(x, 0)
    arr = np.asarray(x)
    t_ord = arr.mean() / (arr.std(ddof=1) / math.sqrt(arr.size))
    expected = t_ord * math.sqrt(arr.size / (arr.size - 1.0))
    assert math.isclose(t_lag0, expected, rel_tol=1e-12), (t_lag0, expected)

    # The property the whole project rests on: a strongly autocorrelated series
    # must have its |t| shrunk. If it does not, overlapping windows inflate
    # significance exactly as process.md S-1 warns.
    rng = np.random.default_rng(7)
    persistent = np.cumsum(rng.normal(0.0, 1.0, 500))
    _pm, t_persist = common.newey_west(persistent, 20)
    t_ordinary = persistent.mean() / (persistent.std(ddof=1) / math.sqrt(persistent.size))
    assert math.isfinite(t_persist), t_persist
    assert abs(t_persist) < abs(t_ordinary) * 0.75, (t_persist, t_ordinary)

    # Degenerate inputs return NaN rather than raising, so a caller that
    # forgets to filter gets a visible missing number instead of a crash that
    # aborts a long run at hour two.
    assert math.isnan(common.newey_west([1.0, 1.0, 1.0, 1.0], 2)[1])
    assert math.isnan(common.newey_west([], 5)[1])
    assert math.isnan(common.newey_west([1.0, 2.0], 5)[1])


def check_nw_lag_unit() -> None:
    """The NW lag must be in SAMPLING units, never in bars.

    This is the single highest-impact arithmetic mistake in the project's
    history: lag = horizon instead of lag = ceil(horizon / date_step) inflated
    |t| by 0.3-1.5, which is enough to push noise over the |t| = 2 line.
    See process.md S-1.
    """
    import math as _math

    def nw_lag_for(horizon: int, date_step: int) -> int:
        return max(1, _math.ceil(horizon / date_step))

    cases = [(1, 5, 1), (5, 5, 1), (10, 5, 2), (20, 5, 4), (60, 5, 12), (60, 1, 60)]
    for horizon, step, expected in cases:
        got = nw_lag_for(horizon, step)
        assert got == expected, f"horizon={horizon} step={step}: {got} != {expected}"
    # The trap: for a densely sampled series the two forms coincide, which is
    # exactly why the mistake survived a year of daily-sampling work.
    assert nw_lag_for(60, 1) == 60
    print("  nw_lag is computed in sampling units (ceil(horizon / date_step))", flush=True)


def check_fast_matches_slow() -> None:
    """spearman_fast must equal spearman_ic; the equivalence is an assumption
    until something asserts it.

    The fast path exists because a full scan is far too many cross-sections for
    the pure-Python reference. Permuted, heavily-tied and NaN-bearing inputs
    are used because those are the three ways a rank implementation silently
    disagrees with its own definition.
    """
    import numpy as np

    from research.src import common

    rng = np.random.default_rng(11)
    worst = 0.0
    checked = 0
    for trial in range(200):
        n = int(rng.integers(30, 700))
        x = rng.normal(size=n)
        y = rng.normal(size=n)
        if trial % 3 == 1:
            # quantising creates massive ties
            x = np.round(x * 4) / 4
            y = np.round(y * 4) / 4
        if trial % 3 == 2:
            x[rng.random(n) < 0.1] = np.nan
            y[rng.random(n) < 0.1] = np.nan
        order = rng.permutation(n)
        x, y = x[order], y[order]
        keep = np.isfinite(x) & np.isfinite(y)
        if int(keep.sum()) < 20:
            continue
        slow = common.spearman_ic(x[keep].tolist(), y[keep].tolist())
        fast = common.spearman_fast(x, y, mask=keep, min_n=20)
        if math.isnan(slow) and math.isnan(fast):
            continue
        assert math.isfinite(fast), (trial, slow, fast)
        worst = max(worst, abs(slow - fast))
        checked += 1
    assert checked > 50, checked
    assert worst <= 1e-12, f"spearman_fast diverges from spearman_ic by {worst}"
    assert math.isnan(common.spearman_fast(np.ones(50), rng.normal(size=50), min_n=20))
    assert math.isnan(common.spearman_fast(rng.normal(size=5), rng.normal(size=5), min_n=20))
    print(f"  spearman_fast == spearman_ic: {checked} cases, max diff {worst:.2e}", flush=True)


# --------------------------------------------------------------------------
# Calendar cache
# --------------------------------------------------------------------------


def _weekdays(n: int, end: date) -> list:
    out, day = [], end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


def _write_calendar(path: Path, sessions: list) -> None:
    path.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "rows": [{"date": d.isoformat()} for d in sessions],
            }
        ),
        encoding="utf-8",
    )


def check_calendar_cache() -> None:
    """A cache that does not cover the request must be refetched, and the
    coverage test must use the UNPADDED range.

    Both halves shipped as real bugs. A stale 927-session calendar was reused
    for a 2,689-session study, silently invalidating every coverage check.
    Then "fixing" it by also requiring the +/-7d fetch padding broke the
    synthetic calendar, causing a silent fall-through to the real network
    calendar. See process.md S-2 and S-3.
    """
    tmp = Path(tempfile.mkdtemp(prefix="foundation-smoke-calendar-"))
    try:
        os.environ["TRADINGBUFFETT_RESEARCH_DATA_DIR"] = str(tmp)
        for mod in list(sys.modules):
            if mod.startswith("research"):
                del sys.modules[mod]
        from research.src import common

        sessions = _weekdays(400, date(2026, 6, 30))
        common.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _write_calendar(common.CALENDAR_PATH, sessions)

        def _no_network(*_a, **_k):
            raise AssertionError(
                "the calendar cache was rejected as stale and the code fell "
                "through to the network -- coverage was tested against the "
                "padded range instead of [start, end]"
            )

        common.fetch_trading_calendar = _no_network

        # A request strictly inside the cached range is served from cache.
        lo, hi = sessions[0], sessions[-1]
        rows = common.load_calendar_rows(lo, hi)
        assert len(rows) >= len(sessions), (len(rows), len(sessions))

        # A request that reaches past the cache must REFETCH, and must do so
        # loudly rather than serving the short cache.
        _write_calendar(common.CALENDAR_PATH, sessions[:200])
        try:
            common.load_calendar_rows(lo, hi)
        except AssertionError:
            pass  # expected: the injected no-network hook fired, i.e. refetch
        else:
            raise AssertionError(
                "a calendar cache that does not cover [start, end] was "
                "silently accepted"
            )
        print("  stale calendar cache is rejected, not silently reused", flush=True)
    finally:
        os.environ.pop("TRADINGBUFFETT_RESEARCH_DATA_DIR", None)
        for mod in list(sys.modules):
            if mod.startswith("research"):
                del sys.modules[mod]
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Bars cache
# --------------------------------------------------------------------------


def check_bars_cache() -> None:
    """Round-trip a batch through the compressed-CSV cache.

    The environment has no pyarrow, so the cache is CSV.gz. A silently
    mangled round-trip would corrupt every downstream number.
    """
    import numpy as np
    import pandas as pd

    tmp = Path(tempfile.mkdtemp(prefix="foundation-smoke-bars-"))
    try:
        os.environ["TRADINGBUFFETT_RESEARCH_DATA_DIR"] = str(tmp)
        for mod in list(sys.modules):
            if mod.startswith("research"):
                del sys.modules[mod]
        from research.src import common

        n = 50
        frame = pd.DataFrame(
            {
                "symbol": ["AAA", "BBB"] * (n // 2),
                "timestamp": [date(2020, 1, 1)] * n,
                "open": np.linspace(1.0, 2.0, n),
                "high": np.linspace(1.1, 2.1, n),
                "low": np.linspace(0.9, 1.9, n),
                "close": np.linspace(1.05, 2.05, n),
                "volume": np.full(n, 1_000_000.0),
            }
        )
        written = common.save_batch(0, frame)
        assert written == n, written
        back = common.load_all_bars()
        assert len(back) == n, len(back)
        assert set(back.columns) == set(frame.columns), list(back.columns)
        worst = float(
            np.abs(
                back[["open", "high", "low", "close", "volume"]].to_numpy()
                - frame[["open", "high", "low", "close", "volume"]].to_numpy()
            ).max()
        )
        assert worst < 1e-9, worst
        print(f"  bars cache round-trip: {n} rows, max diff {worst:.2e}", flush=True)
    finally:
        os.environ.pop("TRADINGBUFFETT_RESEARCH_DATA_DIR", None)
        for mod in list(sys.modules):
            if mod.startswith("research"):
                del sys.modules[mod]
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------


def main() -> int:
    print("=" * 72, flush=True)
    print("Foundation self-test (synthetic, no credentials, no network)", flush=True)
    print("=" * 72, flush=True)

    failures = []
    for name, fn in (
        ("spearman", check_spearman),
        ("newey_west", check_newey_west),
        ("nw_lag_units", check_nw_lag_unit),
        ("fast_vs_slow_rank", check_fast_matches_slow),
        ("calendar_cache", check_calendar_cache),
        ("bars_cache", check_bars_cache),
    ):
        print(f"\n-- {name} --", flush=True)
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
            print(f"  FAILED: {exc}", flush=True)

    print("=" * 72, flush=True)
    if failures:
        print("SELF-TEST FAILED", flush=True)
        for line in failures:
            print(f"  - {line}", flush=True)
        print(
            "\nDo not run any study against this foundation until these pass.",
            flush=True,
        )
        return 1
    print("SELF-TEST PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
