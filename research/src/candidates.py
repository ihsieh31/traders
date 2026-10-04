"""Candidate formula definitions, past the baseline.

READ research/plan.md SECTIONS 3, 5 AND 6 FIRST.

``study.py`` owns the machinery. This file owns the formulas, and exists so
that adding a candidate is a ten-line declaration rather than a refactor.
Nothing here knows how to measure anything.

The two directions run in parallel and are deliberately kept apart:

  * **long** -- look for a 40-60 day edge, where the baseline's only
    non-zero t-statistic lives (F-20260928-04). Momentum-style effects are
    documented over months, not weeks, so this is where a new factor has the
    best prior.
  * **short** -- diagnose why the baseline has no 5-20 day edge. This is not
    a search for alpha. The baseline's ``r20`` carries a +0.25 weight and a
    *negative* 5-day IC of -0.0153 (t = -2.25), which is a horizon mismatch,
    not noise. The question is whether any variant fixes the mismatch or
    whether the direction itself is wrong at that horizon.

Registering a candidate is a declaration, and that is the point: the
pre-registration in plan.md 6.2 step 2 says the closed form is written down
BEFORE results are seen. A builder function is that written-down form.

Every candidate must declare:
  * its components with signs and lookbacks (plan.md R3)
  * its horizon
  * its free-parameter count, which is capped at 6 at construction (R4)
  * any parameters to sweep, with the grid fixed here and now (R5)
"""

from __future__ import annotations


import numpy as np

from research.src import factors as F
from research.src import panel as P
from research.src import study as S

#: Windows a factor may look back over (plan.md R4: integer, discrete,
#: exhaustively sweepable). A parameter swept over this grid costs
#: len(WINDOWS) tests in the multiple-testing budget (R9).
WINDOWS = (10, 20, 60, 120)


def _momentum(features: P.FeatureSet, lookback: int) -> np.ndarray:
    """Simple return over ``lookback`` sessions, as a fraction."""
    return features.__dict__[f"r{lookback}"]


# --------------------------------------------------------------------------
# Long direction: 40-60 day candidates
# --------------------------------------------------------------------------


def _long_baseline(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """The production factor set re-measured at 60 days.

    Not a new formula. This is the control for the long direction: if a
    candidate cannot beat this at its own horizon, the candidate is the
    problem, not the horizon.

    F-20260928-04 measured the same components at 5 days and got
    ``+2.81%`` net annualised at ``t = 0.43``; at 60 days it got
    ``+11.31%`` at ``t = 2.26``. The components did not change. The
    horizon did.
    """
    return F.Formula(
        name="long-baseline",
        components=[
            F.Component("adv20", features.adv20, +1, 20),
            F.Component("r20", features.r20, +1, 20),
            F.Component("r60", features.r60, +1, 60),
            F.Component("vol20", features.vol20, -1, 20),
            F.Component("volume_ratio", features.volume_ratio, +1, 5),
        ],
        horizon=60,
        top_k=20,
        free_parameters=5,
        notes=(
            "Production's five components at a 60-day holding period. The "
            "control for every long-horizon candidate."
        ),
    )


def _long_momentum_only(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Momentum alone, 60 days.

    The prior: 1-3 month cross-sectional momentum is one of the best
    replicated effects in the literature. The baseline dilutes it with four
    other components, two of which had near-zero IC at every horizon
    measured. This asks whether dilution is the problem.
    """
    return F.Formula(
        name="long-momentum-only",
        components=[F.Component("r60", features.r60, +1, 60)],
        horizon=60,
        top_k=20,
        free_parameters=1,
        notes="Single component. Tests whether the baseline dilutes a real signal.",
    )


def _long_momentum_vol(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Momentum, conditioned on low realised volatility.

    The prior is that momentum is stronger among names that are not already
    volatile, where the move is more likely to be a drift than a reaction.
    ``vol20`` is the baseline's only component with a significant 5-day IC
    (+0.0179, t = +2.40 in the production direction), so it is the one
    component with direct evidence behind it.
    """
    return F.Formula(
        name="long-momentum-vol",
        components=[
            F.Component("r60", features.r60, +1, 60),
            F.Component("vol20", features.vol20, -1, 20),
        ],
        horizon=60,
        top_k=20,
        free_parameters=2,
        notes="Momentum plus a low-volatility tilt.",
    )


# --------------------------------------------------------------------------
# Short direction: diagnosing the 5-20 day null
# --------------------------------------------------------------------------


def _short_r20_flip(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Production's r20 with its sign FLIPPED, at 10 days.

    This is a diagnostic, not a candidate. F-20260928-04 found ``r20`` has a
    5-day IC of **-0.0153 with t = -2.25** while production gives it a
    **+0.25 weight**. That is a sign disagreement, and the literature
    explains it: monthly momentum works over 3-12 months, while the
    strongest 1-3 week effect is short-term *reversal*.

    If flipping the sign recovers a 10-day edge, the mismatch is confirmed
    and the finding is a horizon problem. If it does not, ``r20`` carries no
    short-horizon information at all and the production weight is simply
    unjustified rather than backwards.
    """
    return F.Formula(
        name="short-r20-flip",
        components=[F.Component("r20_flipped", features.r20, -1, 20)],
        horizon=10,
        top_k=20,
        free_parameters=1,
        notes=(
            "DIAGNOSTIC. Production gives r20 a +0.25 weight; its measured "
            "5-day IC is negative and significant. This tests the reversal "
            "reading of that sign."
        ),
    )


def _short_reversal(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Short-term reversal, 5-day and 10-day legs.

    F-20260927-03 already recorded that ``mom_1``/``mom_2``/``mom_3``/``mom_5``
    were negative in BOTH periods of an earlier split. That is four
    independent holding periods agreeing on a sign, which is worth more than
    any single |t|, and it is the strongest directional finding this project
    has. This candidate is that finding, in production's own factor set:
    ``r5`` is computed in production and carries a **zero weight**.

    Prior claim, therefore: do not re-report as new.
    """
    return F.Formula(
        name="short-reversal",
        components=[F.Component("r5_flipped", features.r5, -1, 5)],
        horizon=10,
        top_k=20,
        free_parameters=1,
        notes=(
            "r5 with a negative sign. Production computes r5 and weights it "
            "at 0.00. The sign prior comes from F-20260927-03, not from "
            "this round."
        ),
    )


def _short_trend(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """``trend`` with a negative sign, 10 days.

    Production also computes ``trend`` (close vs 20-day mean) and weights it
    at **0.00**. It is a textbook 1-month reversal signal, and the second
    zero-weight component in the production set. Same diagnostic purpose as
    ``short-reversal``, on the other dead component.
    """
    return F.Formula(
        name="short-trend",
        components=[F.Component("trend_flipped", features.trend, -1, 20)],
        horizon=10,
        top_k=20,
        free_parameters=1,
        notes="trend with a negative sign. Production weights it at 0.00.",
    )


def _short_combined(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Both zero-weight components, flipped, at 10 days.

    If reversal really is the 1-3 week effect, the two zero-weight
    components should carry it jointly. If they are near-duplicates of each
    other, the composite will show it: a high correlation between r5 and
    trend will show up as a much smaller ``N_eff`` than 2.
    """
    return F.Formula(
        name="short-combined",
        components=[
            F.Component("r5_flipped", features.r5, -1, 5),
            F.Component("trend_flipped", features.trend, -1, 20),
        ],
        horizon=10,
        top_k=20,
        free_parameters=2,
        notes="Both zero-weight production components, sign-flipped.",
    )


# --------------------------------------------------------------------------
# 90-day candidates (the operator's chosen holding period)
# --------------------------------------------------------------------------


def _m90(panel: P.Panel, features: P.FeatureSet) -> np.ndarray:
    """90-session simple return, recomputed here rather than reused.

    ``FeatureSet`` only carries r5/r20/r60, so a 90-day momentum leg has to
    be built from the panel directly. Same definition production uses:
    ``close[t] / close[t-90] - 1``.
    """
    close = panel.close
    out = np.full(close.shape, np.nan, dtype=np.float64)
    if close.shape[0] > 90:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[90:] = close[90:] / close[:-90] - 1.0
    return out


def _d90_momentum(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """90-session momentum, 90-day hold. The operator's target.

    Both the lookback and the holding period are 90, so a position is exited
    on the same horizon by which it was selected. The alternative -- 60-day
    momentum held 90 days -- is deliberately NOT tried first; one leg, one
    test. The 60 vs 90 comparison is between holding periods, not between
    momentum definitions.
    """
    return F.Formula(
        name="d90-momentum",
        components=[F.Component("r90", _m90(panel, features), +1, 90)],
        horizon=90,
        top_k=20,
        free_parameters=1,
        notes="r90 momentum, 90-day hold. The operator's chosen configuration.",
    )


def _d90_momentum_60(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """60-session momentum held 90 days.

    The prior: 12-1 momentum uses a long lookback and skips the most recent
    month, so 60-day momentum carried 90 days is closer to the classic
    construction than 90-day momentum carried 90 days. One parameter
    differs from ``d90-momentum``, which is what makes the pair a
    controlled comparison rather than two shots in the dark.
    """
    return F.Formula(
        name="d90-momentum60",
        components=[F.Component("r60", features.r60, +1, 60)],
        horizon=90,
        top_k=20,
        free_parameters=1,
        notes="r60 momentum held 90 days. Classic-style long lookback.",
    )


def _d90_production(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Production's five components at a 90-day hold.

    The control. A candidate that cannot beat this at 90 days is the
    candidate's problem, not the horizon's.
    """
    return F.Formula(
        name="d90-production",
        components=[
            F.Component("adv20", features.adv20, +1, 20),
            F.Component("r20", features.r20, +1, 20),
            F.Component("r60", features.r60, +1, 60),
            F.Component("vol20", features.vol20, -1, 20),
            F.Component("volume_ratio", features.volume_ratio, +1, 5),
        ],
        horizon=90,
        top_k=20,
        free_parameters=5,
        notes="Production components at a 90-day holding period. The control.",
    )


def _d90_momentum_vol(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """90-day momentum plus a low-volatility tilt, 90-day hold.

    ``vol20`` is the one production component that was individually
    significant in F-04 (5-day IC +0.0179, t = +2.40 in the production
    direction), so it is the one with direct evidence behind it rather than
    a prior borrowed from the literature.
    """
    return F.Formula(
        name="d90-momentum-vol",
        components=[
            F.Component("r90", _m90(panel, features), +1, 90),
            F.Component("vol20", features.vol20, -1, 20),
        ],
        horizon=90,
        top_k=20,
        free_parameters=2,
        notes="r90 momentum plus a low-volatility tilt, 90-day hold.",
    )


# --------------------------------------------------------------------------
# Horizon probes: 50 and 70 days
# --------------------------------------------------------------------------


def _d50_momentum(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """r60 momentum, 50-day hold.

    Probes whether the 60-day result is a plateau or a spike in the HOLDING
    PERIOD. ``r60_window`` was already a plateau (plan.md R5 condition 8), but
    that sweeps the momentum window at a fixed hold. This sweeps the hold with
    the window fixed, which is a different and independent question.

    R5's plateau rule applies here too: 50/60/70 must agree, or the 60-day
    choice is a point estimate rather than a region.
    """
    return F.Formula(
        name="d50-momentum",
        components=[F.Component("r60", features.r60, +1, 60)],
        horizon=50,
        top_k=20,
        free_parameters=1,
        notes="r60 momentum, 50-day hold. Holding-period probe below 60.",
    )


def _d70_momentum(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """r60 momentum, 70-day hold. The mirror probe above 60."""
    return F.Formula(
        name="d70-momentum",
        components=[F.Component("r60", features.r60, +1, 60)],
        horizon=70,
        top_k=20,
        free_parameters=1,
        notes="r60 momentum, 70-day hold. Holding-period probe above 60.",
    )


def _d50_production(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Production's five components at a 50-day hold. The control at 50."""
    return F.Formula(
        name="d50-production",
        components=[
            F.Component("adv20", features.adv20, +1, 20),
            F.Component("r20", features.r20, +1, 20),
            F.Component("r60", features.r60, +1, 60),
            F.Component("vol20", features.vol20, -1, 20),
            F.Component("volume_ratio", features.volume_ratio, +1, 5),
        ],
        horizon=50,
        top_k=20,
        free_parameters=5,
        notes="Production components at a 50-day holding period.",
    )


def _d70_production(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Production's five components at a 70-day hold. The control at 70."""
    return F.Formula(
        name="d70-production",
        components=[
            F.Component("adv20", features.adv20, +1, 20),
            F.Component("r20", features.r20, +1, 20),
            F.Component("r60", features.r60, +1, 60),
            F.Component("vol20", features.vol20, -1, 20),
            F.Component("volume_ratio", features.volume_ratio, +1, 5),
        ],
        horizon=70,
        top_k=20,
        free_parameters=5,
        notes="Production components at a 70-day holding period.",
    )


def _d55_production(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Production's five components at a 55-day hold.

    55 was the peak of the 35..70 discovery sweep (NW t = 3.76, the highest of
    the eight), but the sweep's own shape says that is exactly what an
    overfit peak looks like: t went 1.35 -> 3.35 -> 3.76 -> 2.72 -> 0.93
    across 45/50/55/60/70. This formula exists to be re-measured on windows
    the sweep never touched, which is the only way to tell a peak from a
    region.
    """
    return F.Formula(
        name="d55-production",
        components=[
            F.Component("adv20", features.adv20, +1, 20),
            F.Component("r20", features.r20, +1, 20),
            F.Component("r60", features.r60, +1, 60),
            F.Component("vol20", features.vol20, -1, 20),
            F.Component("volume_ratio", features.volume_ratio, +1, 5),
        ],
        horizon=55,
        top_k=20,
        free_parameters=5,
        notes=(
            "Peak of the 35..70 discovery sweep. Being re-measured on "
            "2018-2023 and 2020-2025, neither of which the sweep used."
        ),
    )


def _d55_momentum(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """r60 momentum at a 55-day hold. The single-component counterpart."""
    return F.Formula(
        name="d55-momentum",
        components=[F.Component("r60", features.r60, +1, 60)],
        horizon=55,
        top_k=20,
        free_parameters=1,
        notes="r60 momentum, 55-day hold. Single-component control.",
    )


# --------------------------------------------------------------------------
# F-20261004-10: literature-anchored candidates, pre-registered in
# out/20261004-literature-candidates/PREREGISTRATION.md before any run.
# --------------------------------------------------------------------------


def _reversal_values(panel: P.Panel, window: int) -> np.ndarray:
    """``close[t] / close[t-window] - 1`` (production's r-definition)."""
    return S._momentum(panel, window)


def _high_proximity_values(panel: P.Panel, window: int) -> np.ndarray:
    """``close / max(close over the trailing window)``; NaN unless complete.

    George & Hwang (2004) use 52 weeks. The bars start on 2016-01-04, so a
    252-session window is undefined for all of 2016; 120 was declared
    instead, before any result.
    """
    from numpy.lib.stride_tricks import sliding_window_view

    close = panel.close
    out = np.full(close.shape, np.nan, dtype=np.float64)
    if close.shape[0] >= window:
        # max over a window containing a NaN is NaN: an incomplete window
        # is undefined, never a max over fewer sessions.
        peak = sliding_window_view(close, window, axis=0).max(axis=2)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[window - 1:] = close[window - 1:] / peak
    return out


def _dollar_volume_values(panel: P.Panel, window: int) -> np.ndarray:
    """Mean of close*volume over the trailing window; NaN unless complete."""
    from numpy.lib.stride_tricks import sliding_window_view

    dollar = panel.close * panel.volume
    out = np.full(dollar.shape, np.nan, dtype=np.float64)
    if dollar.shape[0] >= window:
        out[window - 1:] = sliding_window_view(dollar, window, axis=0).mean(axis=2)
    return out


S.EXTRA_SWEEPS.update({
    "lit_rev": (_reversal_values, (3, 5, 10)),
    "lit_hi": (_high_proximity_values, (60, 120, 180)),
    "lit_adv": (_dollar_volume_values, (5, 20, 60)),
})


def _lit_reversal(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """K1: buy the 20 largest 5-session losers, hold 5 sessions."""
    return F.Formula(
        name="lit-reversal",
        components=[F.Component("lit_rev", _reversal_values(panel, 5), -1, 5)],
        horizon=5, top_k=20, free_parameters=2,
        notes="-r5. Direction from F-20260927-03 (read 2021+); Jegadeesh 1990, Lehmann 1990.",
    )


def _lit_high_proximity(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """K2: buy the 20 names closest to their trailing 120-session high, hold 20."""
    return F.Formula(
        name="lit-high-proximity",
        components=[F.Component("lit_hi", _high_proximity_values(panel, 120), +1, 120)],
        horizon=20, top_k=20, free_parameters=2,
        notes="close / max(close, 120). George & Hwang 2004 (52-week high), window shortened to fit the data.",
    )


def _lit_liquidity(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """K3: buy the 20 highest 20-session dollar volumes, hold 20."""
    return F.Formula(
        name="lit-liquidity",
        components=[F.Component("lit_adv", _dollar_volume_values(panel, 20), +1, 20)],
        horizon=20, top_k=20, free_parameters=2,
        notes="ADV20. Direction from F-20260927-03 (read 2021+); a large-cap tilt, disclosed as such.",
    )


# --------------------------------------------------------------------------
# F-20261004-11: fundamental quality, pre-registered in
# out/20261004-quality-factor/PREREGISTRATION.md before any panel was built.
# --------------------------------------------------------------------------

_RATIO_CACHE: dict = {}


def _quality_ratios(panel: P.Panel) -> dict:
    key = (id(panel), len(panel.sessions), len(panel.symbols))
    if key not in _RATIO_CACHE:
        from research.src import fundamentals as FD

        fp = FD.fundamental_panel(panel.sessions, panel.symbols)
        print(f"fundamental coverage (any session): {fp.coverage}", flush=True)
        _RATIO_CACHE.clear()
        _RATIO_CACHE[key] = FD.ratios(fp)
    return _RATIO_CACHE[key]


def _lit_gross_profitability(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Q1: Novy-Marx gross profits / total assets, latest 10-K, 10-day hold."""
    r = _quality_ratios(panel)
    return F.Formula(
        name="lit-gross-profitability",
        components=[F.Component("gpa", r["gpa"], +1, 0)],
        horizon=10, top_k=20, free_parameters=2,
        notes="GP/A from the newest 10-K accepted before the session (Novy-Marx 2013).",
    )


def _lit_quality_composite(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """Q2: equal-weight GP/A, CFO/A and low Liabilities/A, 10-day hold."""
    r = _quality_ratios(panel)
    return F.Formula(
        name="lit-quality-composite",
        components=[
            F.Component("gpa", r["gpa"], +1, 0),
            F.Component("cfoa", r["cfoa"], +1, 0),
            F.Component("lev", r["lev"], -1, 0),
        ],
        horizon=10, top_k=20, free_parameters=4,
        notes="screen-alternatives 2026-09-29 proposal, cross-sectional instead of sector percentiles.",
    )


for _name in ("lit-gross-profitability", "lit-quality-composite"):
    S.COMPLETE_CASE.add(_name)
    S.HORIZON_SWEEPS[_name] = (5, 10, 20)


def register_all() -> None:
    for builder in (
        _long_baseline,
        _long_momentum_only,
        _long_momentum_vol,
        _short_r20_flip,
        _short_reversal,
        _short_trend,
        _short_combined,
        _d90_momentum,
        _d90_momentum_60,
        _d90_production,
        _d90_momentum_vol,
        _d50_momentum,
        _d70_momentum,
        _d50_production,
        _d70_production,
        _d55_production,
        _d55_momentum,
        _lit_reversal,
        _lit_high_proximity,
        _lit_liquidity,
        _lit_gross_profitability,
        _lit_quality_composite,
    ):
        S.register(builder.__name__.lstrip("_").replace("_", "-"), builder)


register_all()
