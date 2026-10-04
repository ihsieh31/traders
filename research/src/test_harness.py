"""Correctness tests for the measurement path. NO CREDENTIALS, NO NETWORK.

    python -m research.src.test_harness

READ research/plan.md R7 AND research/process.md FIRST.

Two kinds of test live here, and the distinction is the point:

  * **ABSOLUTE** tests check a value against something written down --
    a hand computation, a published quantile, a literal expected series.
    These are the only ones that can catch an error in the TARGET VARIABLE.
  * **SYMMETRIC** tests check one implementation against another that shares
    an input. Useful for the data path, structurally incapable of catching a
    target-variable error: if the forward-return array is wrong, the error
    cancels in the subtraction and the difference is still zero
    (process.md S-14).

The synthetic-injection tests are the only ones that verify the harness can
detect a factor whose sign is known in advance. A harness that cannot
recover a planted sign cannot be trusted to report one.

Run this after ANY change to panel.py, factors.py or measure.py.
"""

from __future__ import annotations

import math
import traceback
from datetime import date, timedelta
from typing import Callable, List

import numpy as np

from research.src import factors as F
from research.src import measure as M
from research.src import panel as P

_FAILURES: List[str] = []


def _check(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
        print(f"  ok    {name}", flush=True)
    except AssertionError as exc:
        _FAILURES.append(f"{name}: {exc}")
        print(f"  FAIL  {name}: {exc}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _FAILURES.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()


# --------------------------------------------------------------------------
# Synthetic fixture
# --------------------------------------------------------------------------

N_SYMBOLS = 250
N_SESSIONS = 400
START_PRICE = 50.0
SHARES = 500_000  # ~$25M dollar volume at $50, clears the $20M ADV20 gate
GAP_SIGMA = 0.003
PULL = 0.03


def _sessions(n: int, end: date) -> List[date]:
    out, day = [], end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


def synthetic_panel(kappa: float = 0.0, seed: int = 11) -> P.Panel:
    """Bars in which a trailing 20-session move ``provably`` drives the next
    session's return, with a controllable sign.

    ``kappa < 0`` is a mean-reverting (reversal) process, so the r20 ->
    forward-return relationship must come out negative. That is the only
    end-to-end way to know the harness measures the sign it claims to.

    ``open != close`` is mandatory, not cosmetic: a fixture with ``open ==
    close`` makes the horizon-1 forward return identically zero, every rank
    correlation NaN, and the harness then correctly reports "no edge" for a
    reason that has nothing to do with the formula (process.md S-16).
    """
    rng = np.random.default_rng(seed)
    n = N_SESSIONS
    tilt = rng.normal(0.0, 1.0, size=N_SYMBOLS)
    tilt = (tilt - tilt.mean()) / tilt.std()
    noise = rng.normal(0.0, 0.02, size=(N_SYMBOLS, n))

    path = np.zeros((N_SYMBOLS, n))
    for t in range(1, n):
        past = path[:, max(0, t - 20) : t]
        if past.shape[1] == 20:
            z = (past[:, -1] - past[:, -1].mean()) / (past[:, -1].std() + 1e-12)
        else:
            z = np.zeros(N_SYMBOLS)
        path[:, t] = path[:, t - 1] + kappa * z + noise[:, t] - PULL * path[:, t - 1]
    # (symbol, time) while generating; the panel wants (time, symbol).
    close_t = (START_PRICE * np.exp(path)).T

    gaps = rng.normal(0.0, GAP_SIGMA, size=(N_SYMBOLS, n)).T
    gaps[0, :] = 0.0
    open_t = np.empty_like(close_t)
    open_t[0, :] = close_t[0, :]
    open_t[1:, :] = close_t[:-1, :] * np.exp(gaps[1:, :])

    sessions = _sessions(n, date(2026, 6, 30))
    return P.Panel(
        sessions=sessions,
        symbols=[f"SYN{i:04d}" for i in range(N_SYMBOLS)],
        open=open_t,
        high=np.maximum(open_t, close_t) * 1.005,
        low=np.minimum(open_t, close_t) * 0.995,
        close=close_t,
        volume=np.full((n, N_SYMBOLS), float(SHARES)),
        calendar_rows=[{"date": d.isoformat()} for d in sessions],
    )


# --------------------------------------------------------------------------
# Panel primitives
# --------------------------------------------------------------------------


def test_rolling_mean_matches_naive_loop() -> None:
    """ABSOLUTE-ish: a naive double loop is the definition, not a sibling."""
    rng = np.random.default_rng(3)
    x = rng.normal(size=(120, 7))
    for window in (1, 2, 5, 20, 61):
        got = P._rolling_mean(x, window)
        want = np.full_like(x, np.nan)
        for t in range(x.shape[0]):
            if t + 1 >= window:
                want[t] = x[t - window + 1 : t + 1].mean(axis=0)
        assert np.allclose(got, want, equal_nan=True, atol=1e-9), window


def test_rolling_var_matches_naive_loop() -> None:
    """Catches the (x[i]-m[i])**2 mistake that made vol20 15-20% wrong.

    The variance is a sum of deviations from the WINDOW mean, not from each
    point's own trailing mean. Those are different quantities and the second
    one looks like the first in code.
    """
    rng = np.random.default_rng(5)
    x = rng.normal(0.01, 0.02, size=(150, 9))
    for window in (2, 5, 20):
        got = P._rolling_var(x, window, ddof=1)
        want = np.full_like(x, np.nan)
        for t in range(x.shape[0]):
            if t + 1 >= window:
                w = x[t - window + 1 : t + 1]
                want[t] = w.var(axis=0, ddof=1)
        assert np.allclose(got, want, equal_nan=True, atol=1e-9), window

    # A constant window must read exactly 0, not NaN from cancellation noise.
    const = np.full((30, 3), 0.01)
    assert np.all(P._rolling_var(const, 20, ddof=1)[19:] == 0.0)


def test_trailing_complete_is_anchored_at_the_window() -> None:
    """Catches the cumulative-count mistake that anchored the test at row 0.

    A symbol whose ENTIRE history is complete must qualify at the far end of
    the array, not only at row window-1.
    """
    x = np.full((200, 4), 1.0)
    x[150:, 1] = np.nan   # symbol 1 incomplete from row 150 on
    x[:, 2] = np.nan      # symbol 2 never complete
    got = P._trailing_complete(x, 61)
    assert got[100, 0], "fully complete symbol should qualify mid-array"
    assert got[199, 0], "fully complete symbol should qualify at the last row"
    assert got[149, 1], "symbol 1 is still complete just before the hole"
    assert not got[199, 1], "symbol 1 is incomplete at the last row"
    assert not got[199, 2], "symbol 2 is never complete"
    assert not got[59, 0], "row 59 has no 61-session window"


def test_forward_returns_are_literal_expected_values() -> None:
    """ABSOLUTE. This is the test S-14 says you must have.

    The values are written out by hand from the definition
    ``fwd[t] = close[t+horizon] / open[t+1] - 1``. A symmetric comparison
    between two implementations that share the forward-return array cannot
    catch an error in that array -- the error cancels.
    """
    panel = synthetic_panel()
    horizon = 5
    fwd = P.forward_returns(panel, horizon)
    worst = 0.0
    for t in (100, 150, 200):
        for j in (0, 7, 42):
            want = panel.close[t + horizon, j] / panel.open[t + 1, j] - 1.0
            worst = max(worst, abs(fwd[t, j] - want))
    assert worst == 0.0, f"forward return differs from the definition by {worst}"

    # horizon 1 must reduce to the general form, not a special case.
    fwd1 = P.forward_returns(panel, 1)
    w1 = max(
        abs(fwd1[t, j] - (panel.close[t + 1, j] / panel.open[t + 1, j] - 1.0))
        for t in (100, 180)
        for j in (0, 11)
    )
    assert w1 == 0.0, f"horizon-1 is not the general next-open form: {w1}"

    # The last `horizon` rows must be NaN: a forward return may never leave
    # the panel (process.md S-20).
    assert np.all(np.isnan(fwd[-horizon:])), "forward return crossed the panel end"
    assert np.all(np.isfinite(fwd[: -horizon])), "valid rows should be finite"


def test_prefix_invariance() -> None:
    """Catches look-ahead. Truncating the panel must not change any factor
    value that was already reported.

    Any forward-looking window shows up here as a difference. This is the
    only automated look-ahead detector the harness has (plan.md R7).
    """
    full = synthetic_panel()
    features = P.compute_features(full)
    mask = P.eligible_mask(full, features)

    cut = 300
    truncated = P.Panel(
        sessions=full.sessions[:cut],
        symbols=full.symbols,
        open=full.open[:cut],
        high=full.high[:cut],
        low=full.low[:cut],
        close=full.close[:cut],
        volume=full.volume[:cut],
        calendar_rows=full.calendar_rows[:cut],
    )
    tfeat = P.compute_features(truncated)
    tmask = P.eligible_mask(truncated, tfeat)

    for name in ("adv20", "r5", "r20", "r60", "vol20", "volume_ratio", "trend"):
        a = getattr(features, name)[:cut]
        b = getattr(tfeat, name)
        assert np.allclose(a, b, equal_nan=True, atol=0.0, rtol=0.0), (
            f"{name} changed when the panel was truncated -- it reads the future"
        )
    assert np.array_equal(mask[:cut], tmask), "eligibility changed under truncation"


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def test_student_t_ppf_against_published() -> None:
    """ABSOLUTE: published two-sided 95% Student-t quantiles."""
    for df, want in (
        (1, 12.706), (2, 4.303), (5, 2.571), (10, 2.228),
        (30, 2.042), (100, 1.984), (1000, 1.962),
    ):
        got = M.student_t_ppf(0.975, df)
        assert abs(got - want) < 2e-3, f"df={df}: got {got}, want {want}"
    # The inverse must actually invert.
    for df in (5, 50):
        for p in (0.75, 0.9, 0.99):
            t = M.student_t_ppf(p, df)
            assert abs(M.student_t_sf(t, df) - (1 - p)) < 1e-9, (p, df)


def test_nw_lag_is_in_sampling_units() -> None:
    """The bug that inflated |t| by 0.3-1.5 (process.md S-1)."""
    assert M.nw_lag(60, 5) == 12, M.nw_lag(60, 5)
    assert M.nw_lag(20, 5) == 4
    assert M.nw_lag(1, 5) == 1
    # The trap: at date_step=1 the two conventions coincide, which is why the
    # wrong version survived so long.
    assert M.nw_lag(60, 1) == 60


def test_effective_tests_extremes() -> None:
    """N_eff must be n for independent series and 1 for identical ones."""
    rng = np.random.default_rng(0)
    independent = M.effective_tests(rng.normal(size=(10, 400)))
    assert 8.0 < independent["n_eff"] < 10.5, independent["n_eff"]
    one = rng.normal(size=(1, 400))
    identical = M.effective_tests(np.repeat(one, 10, axis=0))
    assert identical["n_eff"] < 1.05, identical["n_eff"]
    near = np.vstack(
        [one, one + rng.normal(scale=0.01, size=(5, 400))]
    )
    copies = M.effective_tests(near)
    assert copies["n_eff"] < 2.0, copies["n_eff"]


def test_newey_west_shrinks_autocorrelation() -> None:
    """The property the whole project rests on (S-1)."""
    from research.src.common import newey_west

    rng = np.random.default_rng(7)
    persistent = np.cumsum(rng.normal(0.0, 1.0, 500))
    _m, t_nw = newey_west(persistent, 20)
    t_ord = persistent.mean() / (persistent.std(ddof=1) / math.sqrt(persistent.size))
    assert abs(t_nw) < abs(t_ord) * 0.75, (t_nw, t_ord)


# --------------------------------------------------------------------------
# Portfolio mechanics
# --------------------------------------------------------------------------


def _flat_portfolio(n_periods: int = 8, top_k: int = 5, n_sym: int = 20) -> M.Portfolio:
    """A Portfolio with predictable contents, for mechanics tests."""
    names = [list(range(top_k)) for _ in range(n_periods)]
    gross = np.full(n_periods, 0.01)
    turnover = np.full(n_periods, 0.0)
    turnover[0] = 1.0
    return M.Portfolio(
        as_of=np.arange(n_periods) * 5 + 70,
        names=names,
        gross=gross,
        turnover=turnover,
        n_available=np.full(n_periods, top_k),
        per_name={j: 0.01 * n_periods / top_k for j in range(top_k)},
    )


def test_initial_turnover_is_one() -> None:
    """Funding 20 positions is a 100% turnover, not zero (S-21).

    Recording zero systematically understates cost by one full round-trip per
    strategy lifetime, which is most of the edge for a long holding period.
    """
    panel = synthetic_panel()
    features = P.compute_features(panel)
    mask = P.eligible_mask(panel, features)
    fwd = P.forward_returns(panel, 5)
    scores = np.full(panel.close.shape, np.nan)
    # a score that is constant within a row would tie-break on symbol index
    rng = np.random.default_rng(1)
    scores = rng.normal(size=panel.close.shape)
    scores[~mask] = np.nan
    pf = M.build_portfolio(scores, fwd, mask, horizon=5, top_k=20)
    assert len(pf) > 0, "no periods were produced at all"
    live = np.flatnonzero(np.isfinite(pf.turnover))
    assert live.size > 0, "no period produced a turnover figure"
    first = int(live[0])
    assert pf.turnover[first] == 1.0, (
        f"the first period with a selection has turnover {pf.turnover[first]}, "
        "must be 1.0 (funding 20 positions)"
    )
    assert np.all(pf.turnover[live] <= 1.0), "turnover cannot exceed 100%"
    assert np.all(pf.turnover[live] >= 0.0)


def test_turnover_matches_definition() -> None:
    """Turnover = 0.5 * sum|w_new - w_old|, checked against a hand calc.

    Period 1 funds three positions (turnover 1.0). Period 2 keeps two of the
    three names, so 0.5 * (2 * 1/3) = 1/3.
    """
    n_sym, top_k = 30, 3
    mask = np.ones((40, n_sym), dtype=bool)
    scores = np.tile(np.arange(n_sym, dtype=float), (40, 1))
    # At t=10 the top 3 by score are 5, 4, 3. At t=15, boost 2 and 1 above 3
    # and demote 5, so the new set is {3, 2, 1} -- one name retained.
    scores[15, 2] = 999.0
    scores[15, 1] = 998.0
    scores[15, 29] = -1.0
    fwd = np.full((40, n_sym), 0.01)
    pf = M.build_portfolio(scores, fwd, mask, horizon=5, top_k=top_k, as_of=[10, 15])
    assert pf.names[0] == [29, 28, 27], pf.names[0]
    assert pf.names[1] == [2, 1, 28], pf.names[1]
    assert pf.turnover[0] == 1.0, pf.turnover[0]
    # 1 of 3 retained -> 0.5 * (2 * 2/3) = 2/3
    assert abs(pf.turnover[1] - 2.0 / 3.0) < 1e-12, pf.turnover[1]


def test_nan_picks_are_dropped_not_zeroed() -> None:
    """A position you cannot sell does not earn zero (S-22, S-23)."""
    n_sym = 30
    mask = np.ones((40, n_sym), dtype=bool)
    scores = np.tile(np.arange(n_sym, dtype=float), (40, 1))
    fwd = np.full((40, n_sym), 0.01)
    fwd[10, 29] = np.nan   # the TOP pick has no forward return
    pf = M.build_portfolio(scores, fwd, mask, horizon=5, top_k=3, as_of=[10])
    assert pf.names[0] == [29, 28, 27], pf.names[0]
    assert pf.n_available[0] == 2, pf.n_available[0]
    assert abs(pf.gross[0] - 0.01) < 1e-12, (
        "must average the two available names, not divide by 3 and call the "
        f"missing one zero: {pf.gross[0]}"
    )


def test_per_name_contribution_stays_with_its_symbol() -> None:
    """A missing pick must not shift later returns onto the wrong symbol.

    Picks are [29, 28, 27]; 29 has no return, so 28 earned 0.02 and 27
    earned 0.04. Zipping the filtered returns against the unfiltered picks
    once credited 29 with 28's return and 28 with 27's.
    """
    n_sym = 30
    mask = np.ones((40, n_sym), dtype=bool)
    scores = np.tile(np.arange(n_sym, dtype=float), (40, 1))
    fwd = np.full((40, n_sym), 0.0)
    fwd[10, 29] = np.nan
    fwd[10, 28] = 0.02
    fwd[10, 27] = 0.04
    pf = M.build_portfolio(scores, fwd, mask, horizon=5, top_k=3, as_of=[10])
    assert 29 not in pf.per_name, f"missing pick was credited: {pf.per_name}"
    assert abs(pf.per_name[28] - 0.01) < 1e-12, pf.per_name
    assert abs(pf.per_name[27] - 0.02) < 1e-12, pf.per_name
    assert abs(sum(pf.per_name.values()) - pf.gross[0]) < 1e-12


def test_turnover_with_short_selection_and_flat_period() -> None:
    """Turnover uses the weights actually held, and a flat period resets.

    t=10 and t=15 each have only two eligible names {29, 28} (min_n=2),
    held at 1/2 each: the book did not change, so turnover is 0 (the old
    ``(top_k - overlap) / top_k`` reported 1/3). t=20 has too few eligible
    names, so the book is flat; t=25 is then a full rebuild (1.0), not a
    diff against the stale t=15 set.
    """
    n_sym = 30
    mask = np.ones((40, n_sym), dtype=bool)
    mask[10, :28] = False
    mask[15, :28] = False
    mask[20, :] = False
    mask[20, 29] = True
    scores = np.tile(np.arange(n_sym, dtype=float), (40, 1))
    fwd = np.full((40, n_sym), 0.01)
    pf = M.build_portfolio(scores, fwd, mask, horizon=5, top_k=3,
                           as_of=[10, 15, 20, 25], min_n=2)
    assert pf.names[1] == [29, 28], pf.names[1]
    assert pf.turnover[1] == 0.0, pf.turnover[1]
    assert pf.names[2] == [] and np.isnan(pf.turnover[2])
    assert pf.turnover[3] == 1.0, pf.turnover[3]


def test_delisted_exit_uses_last_trade_only_for_departures() -> None:
    """``delisted_exit`` fills a departed symbol, never a halted one.

    Symbol 0 stops trading after row 102 (leaves the panel). Symbol 1 is
    halted at row 105 only and trades again. Entry is open[101], exit row
    105 for t=100, horizon=5.
    """
    panel = synthetic_panel()
    panel.close[103:, 0] = np.nan
    panel.open[103:, 0] = np.nan
    panel.close[105, 1] = np.nan
    plain = P.forward_returns(panel, 5)
    filled = P.forward_returns(panel, 5, delisted_exit=True)
    assert np.isnan(plain[100, 0]), "the default must keep the hole visible"
    want = panel.close[102, 0] / panel.open[101, 0] - 1.0
    assert filled[100, 0] == want, (filled[100, 0], want)
    assert np.isnan(filled[100, 1]), "a halt is not a delisting"
    # Entered after the last trade: there is no position to close.
    assert np.isnan(filled[102, 0])
    others = np.ones(panel.close.shape[1], dtype=bool)
    others[[0, 1]] = False
    assert np.array_equal(plain[:, others], filled[:, others], equal_nan=True)


def test_study_grid_starts_at_the_study_start() -> None:
    """The as-of grid begins at ``start``, never inside the leading history.

    ``np.flatnonzero(valid_as_of(...))[::h]`` began at index 1 -- inside the
    66-session buffer that belongs to the previous period -- because
    flatnonzero treats the index array as a mask and drops index 0.
    """
    panel = synthetic_panel()
    start = panel.sessions[66]
    grid = P.study_as_of(panel, 20, start)
    assert grid[0] == 66, grid[:3]
    assert np.all(np.diff(grid) == 20)
    assert grid[-1] + 20 <= len(panel.sessions) - 1, "forward window left the panel"
    old = np.flatnonzero(P.valid_as_of(panel, 20))[::20]
    assert old[0] == 1, "the replaced idiom is documented as starting at index 1"
    # A start between sessions snaps forward to the next session.
    assert P.study_as_of(panel, 20, panel.sessions[65] + timedelta(days=1))[0] == 66


def test_equal_weight_excess_is_paired_and_charges_only_the_formula() -> None:
    """Excess = formula net - same-universe equal-weight gross, per period.

    Ten eligible names; the top 3 earn 0.04, the rest 0.01. EW gross is
    (3*0.04 + 7*0.01)/10 = 0.019. Period 1 funds the book (turnover 1.0):
    net 0.04 - 0.001 = 0.039, excess 0.020. Period 2 holds the same names
    (turnover 0): excess 0.021.
    """
    n_sym = 10
    mask = np.ones((40, n_sym), dtype=bool)
    scores = np.tile(np.arange(n_sym, dtype=float), (40, 1))
    fwd = np.full((40, n_sym), 0.01)
    fwd[:, 7:] = 0.04
    pf = M.build_portfolio(scores, fwd, mask, horizon=5, top_k=3, as_of=[10, 15], min_n=3)
    ex = M.equal_weight_excess(pf, fwd, mask, 5, cost=0.001, n_resamples=50)
    assert ex["n_periods"] == 2
    assert abs(ex["mean_excess_per_period"] - (0.020 + 0.021) / 2) < 1e-12, ex
    assert abs(ex["annualised_equal_weight"] - 0.019 * M.PERIODS_PER_YEAR / 5) < 1e-9


def test_fundamentals_are_point_in_time() -> None:
    """F-11 spec: newest 10-K accepted STRICTLY before the session, at most
    456 days after its period end, never replaced by an older filing, and
    financial SICs excluded."""
    import pandas as pd
    from research.src import fundamentals as FD

    assert FD.derive_fields({"Revenues": 100.0, "CostOfRevenue": 60.0}, {"Assets": 50.0})["gross_profit"] == 40.0
    # A contradictory (None) GrossProfit is missing; it does not fall back to R - COGS.
    assert FD.derive_fields({"GrossProfit": None, "Revenues": 100.0, "CostOfRevenue": 60.0}, {})["gross_profit"] is None
    assert FD.derive_fields({}, {"Assets": 0.0})["assets"] is None

    aapl = 320193  # AAPL in the cached SEC ticker map
    rows = [
        dict(adsh="a1", cik=aapl, sic="3571", period="20191231", accepted="2020-02-10",
             revenue=1.0, gross_profit=10.0, assets=100.0, cfo=5.0, liabilities=50.0),
        dict(adsh="a2", cik=aapl, sic="3571", period="20201231", accepted="2021-02-10",
             revenue=1.0, gross_profit=None, assets=100.0, cfo=5.0, liabilities=50.0),
        dict(adsh="b1", cik=1, sic="6022", period="20191231", accepted="2020-02-10",
             revenue=1.0, gross_profit=10.0, assets=100.0, cfo=5.0, liabilities=50.0),
    ]
    sessions = [date(2020, 2, 10), date(2020, 2, 11), date(2021, 2, 10), date(2021, 2, 11), date(2022, 6, 1)]
    fp = FD.fundamental_panel(sessions, ["AAPL"], pd.DataFrame(rows))
    gp = fp.values["gross_profit"][:, 0]
    assert np.isnan(gp[0]), "a filing accepted on the session day is not yet usable"
    assert gp[1] == 10.0 and gp[2] == 10.0
    assert np.isnan(gp[3]), "an incomplete newest filing must not fall back to the older one"
    assert fp.values["assets"][3, 0] == 100.0
    assert np.isnan(fp.values["assets"][4, 0]), "period end > 456 days before the session is stale"


def test_missing_values_are_never_ranked_first() -> None:
    """S-61: NaN used to get percentile 1.0 -- the best long, the first short."""
    from research.src.common import percentiles_fast

    p = percentiles_fast(np.array([3.0, np.nan, 1.0, 2.0]))
    assert np.isnan(p[1]) and list(p[[2, 3, 0]]) == [0.0, 0.5, 1.0], p
    n_sym = 30
    mask = np.ones((40, n_sym), dtype=bool)
    values = np.tile(np.arange(n_sym, dtype=float), (40, 1))
    values[:, 29] = np.nan  # would have been the top pick
    scores = F.compose_scores(F.Formula("x", [F.Component("v", values, 1)], 5, 3), mask)
    pf = M.build_portfolio(scores, np.full((40, n_sym), 0.01), mask, horizon=5, top_k=3, as_of=[10])
    assert pf.names[0] == [28, 27, 26], pf.names[0]


def test_nan_factor_does_not_shrink_the_cross_section() -> None:
    """One undefined component must not remove that name from every other
    component's IC on the same date."""
    n = 40
    mask = np.ones((n, 60), dtype=bool)
    values = np.tile(np.arange(60, dtype=float), (n, 1))
    values[20, 5] = np.nan
    values[20, 7] = np.nan  # both NaN in the same row
    fwd = np.tile(np.arange(60, dtype=float)[::-1], (n, 1))
    series = M.ic_series(values, fwd, mask, np.arange(n))
    ok = np.flatnonzero(np.isfinite(series))
    assert ok.size == n, f"{n - ok.size} dates produced no IC; only rows with <20 usable should fail"


def test_cost_scenarios_are_ordered() -> None:
    """A higher cost assumption must produce a strictly lower net return."""
    pf = _flat_portfolio()
    table = M.cost_table(pf, 5)
    assert (
        table["base"]["annualised_net"] > table["1.5x"]["annualised_net"]
        > table["2.0x"]["annualised_net"]
    ), table


def test_platform_versus_spike_classification() -> None:
    """The overfitting fingerprint (S-31)."""
    from research.src.study import classify_curve

    grid = [10, 20, 30, 40, 60]
    flat = classify_curve(grid, [0.10, 0.12, 0.11, 0.09, 0.10])
    assert flat["verdict"] == "PLATEAU", flat
    spiked = classify_curve(grid, [0.01, 0.02, 0.30, 0.01, 0.02])
    assert spiked["verdict"] == "SPIKE", spiked
    flipped = classify_curve(grid, [0.10, 0.11, -0.30, 0.10, 0.11])
    assert flipped["verdict"] == "SPIKE", flipped
    # A plateau whose best cell is in the middle or at the end is still a
    # plateau; the old rightward-only count called these SPIKE (S-60).
    assert classify_curve([3, 5, 10], [0.20, 0.23, 0.21])["verdict"] == "PLATEAU"
    assert classify_curve([5, 20, 60], [0.22, 0.23, 0.28])["verdict"] == "PLATEAU"
    assert classify_curve([60, 120, 180], [0.0845, 0.0746, 0.1499])["verdict"] == "SPIKE"


def test_parameter_budget_is_enforced() -> None:
    """plan.md R4: the cap must be structural, not a review comment."""
    values = np.zeros((5, 5))
    comps = [F.Component(f"c{i}", values, 1) for i in range(9)]
    try:
        F.Formula("too_big", comps, 5, 20, free_parameters=9)
    except ValueError as exc:
        assert "R4" in str(exc), exc
    else:
        raise AssertionError("a 9-parameter formula was accepted")
    F.Formula("ok", comps[:5], 5, 20, free_parameters=5)


# --------------------------------------------------------------------------
# End-to-end: can the harness recover a planted sign?
# --------------------------------------------------------------------------


def test_detects_a_harmful_factor() -> None:
    """A mean-reverting injection must produce a NEGATIVE r20 IC.

    If the harness cannot recover a sign it planted, it cannot be trusted to
    report one it did not.
    """
    panel = synthetic_panel(kappa=-0.03)
    features = P.compute_features(panel)
    mask = P.eligible_mask(panel, features)
    assert mask.sum() > 0, "synthetic fixture produced no eligible names"

    fwd = P.forward_returns(panel, 5)
    grid = np.arange(100, len(panel.sessions) - 5, 5)
    series = M.ic_series(features.r20, fwd, mask, grid)
    summary = M.summarise_ic(series, 5, 5)
    assert summary["n"] > 20, summary
    assert summary["ic_mean"] < -0.01, f"expected a negative IC, got {summary['ic_mean']}"
    assert summary["ic_t"] < -3, f"expected a strong t, got {summary['ic_t']}"


def test_sign_flips_with_the_injection() -> None:
    """Flipping the planted sign must flip the recovered IC, monotonically.

    A hidden absolute value, or a sign bug anywhere in the chain, would break
    this symmetry while leaving every individual test passing.
    """
    harmful = synthetic_panel(kappa=-0.03, seed=11)
    benign = synthetic_panel(kappa=+0.03, seed=11)

    def ic_of(panel):
        f = P.compute_features(panel)
        m = P.eligible_mask(panel, f)
        fwd = P.forward_returns(panel, 5)
        grid = np.arange(100, len(panel.sessions) - 5, 5)
        s = M.summarise_ic(M.ic_series(f.r20, fwd, m, grid), 5, 5)
        return s["ic_mean"], s["ic_t"]

    ic_h, t_h = ic_of(harmful)
    ic_b, t_b = ic_of(benign)
    assert ic_h < 0 < ic_b, f"sign did not flip: harmful {ic_h}, benign {ic_b}"
    assert ic_b > ic_h, "IC is not monotone in the injection"
    assert t_h < 0 < t_b, f"t did not flip: {t_h}, {t_b}"


def test_composition_uses_only_the_eligible_set() -> None:
    """The parent-population rule, isolated (S-12).

    Ranking over the whole panel instead of the eligible set is the single
    largest silent error this project has made. Here the effect is made
    unmistakable: a junk name outside the population has an extreme value,
    and if it is allowed into the ranking it drags every percentile.
    """
    panel = synthetic_panel()
    n_t, n_s = panel.shape
    mask = np.zeros((n_t, n_s), dtype=bool)
    mask[:, :50] = True
    rng = np.random.default_rng(2)
    values = rng.normal(size=(n_t, n_s))
    values[:, 50] = 1e9  # a junk name outside the population

    f = F.Formula("t", [F.Component("v", values, +1)], 5, 20)
    scores = F.compose_scores(f, mask)
    eligible_scores = scores[mask]
    assert np.isfinite(eligible_scores).all()
    # The junk name must be NaN, not ranked.
    assert np.all(np.isnan(scores[:, 50])), "a name outside the population was scored"
    # Percentiles of the 50 eligible names must span the full [0,1] range.
    rebuilt = eligible_scores / F.SCORE_SCALE * 1.0  # single component -> p
    assert rebuilt.min() < 0.02 and rebuilt.max() > 0.98, (
        rebuilt.min(),
        rebuilt.max(),
    )


def test_top_k_is_deterministic_under_ties() -> None:
    """All-equal scores must still produce a stable, size-k selection."""
    n_t, n_s = 30, 50
    mask = np.ones((n_t, n_s), dtype=bool)
    scores = np.zeros((n_t, n_s))
    picks = F.select_top_k(scores, mask, 20)
    first = picks[0]
    assert len(first) == 20
    for t in range(1, n_t):
        assert picks[t] == first, "tie-breaking is not deterministic"


# --------------------------------------------------------------------------


def main() -> int:
    print("=" * 72)
    print("Harness correctness tests (synthetic, no credentials, no network)")
    print("=" * 72)

    groups = [
        ("panel primitives", [
            ("rolling_mean vs naive loop", test_rolling_mean_matches_naive_loop),
            ("rolling_var vs naive loop", test_rolling_var_matches_naive_loop),
            ("trailing_complete anchoring", test_trailing_complete_is_anchored_at_the_window),
            ("forward returns vs written-out definition", test_forward_returns_are_literal_expected_values),
            ("prefix invariance (look-ahead)", test_prefix_invariance),
        ]),
        ("statistics", [
            ("student-t quantile vs published", test_student_t_ppf_against_published),
            ("NW lag in sampling units", test_nw_lag_is_in_sampling_units),
            ("N_eff extremes", test_effective_tests_extremes),
            ("NW shrinks autocorrelation", test_newey_west_shrinks_autocorrelation),
        ]),
        ("portfolio mechanics", [
            ("initial turnover is 1.0", test_initial_turnover_is_one),
            ("turnover matches definition", test_turnover_matches_definition),
            ("NaN picks dropped, not zeroed", test_nan_picks_are_dropped_not_zeroed),
            ("per-name contribution keeps its symbol", test_per_name_contribution_stays_with_its_symbol),
            ("turnover: short selection, flat period", test_turnover_with_short_selection_and_flat_period),
            ("delisted exit fills departures only", test_delisted_exit_uses_last_trade_only_for_departures),
            ("study grid starts at the study start", test_study_grid_starts_at_the_study_start),
            ("equal-weight excess is paired", test_equal_weight_excess_is_paired_and_charges_only_the_formula),
            ("fundamentals are point-in-time", test_fundamentals_are_point_in_time),
            ("missing values never ranked first", test_missing_values_are_never_ranked_first),
            ("NaN factor keeps the cross-section", test_nan_factor_does_not_shrink_the_cross_section),
            ("cost scenarios ordered", test_cost_scenarios_are_ordered),
            ("plateau vs spike", test_platform_versus_spike_classification),
            ("parameter budget enforced", test_parameter_budget_is_enforced),
        ]),
        ("end-to-end sign recovery", [
            ("detects a harmful factor", test_detects_a_harmful_factor),
            ("sign flips with the injection", test_sign_flips_with_the_injection),
            ("composition uses the eligible set only", test_composition_uses_only_the_eligible_set),
            ("top-k deterministic under ties", test_top_k_is_deterministic_under_ties),
        ]),
    ]

    for title, tests in groups:
        print(f"\n-- {title} --", flush=True)
        for name, fn in tests:
            _check(name, fn)

    print("=" * 72)
    if _FAILURES:
        print("HARNESS TESTS FAILED", flush=True)
        for line in _FAILURES:
            print(f"  - {line}", flush=True)
        print(
            "\nDo not run a study against a harness that fails these. Each "
            "one corresponds to a bug that shipped and produced valid-looking "
            "wrong output (see process.md).",
            flush=True,
        )
        return 1
    print("HARNESS TESTS PASSED", flush=True)
    print(
        "\nVerified: rolling mean and variance against a naive loop; the "
        "forward-return definition written out literally (S-14); prefix "
        "invariance, i.e. no factor reads the future; the Student-t "
        "quantile against published values; the Newey-West lag in sampling "
        "units; N_eff at both extremes; initial turnover; NaN handling; the "
        "eligible-set-only parent population; and recovery of a planted "
        "signal's sign in both directions.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
