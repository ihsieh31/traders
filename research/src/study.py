"""The round driver: run every check plan.md section 6.2 requires.

    python -m research.src.study --formula production --period discovery

READ research/plan.md SECTIONS 5 AND 6 FIRST, AND research/process.md.

This module is deliberately linear and explicit rather than clever. It runs
the checks in the order they can invalidate each other, writes every
intermediate to ``out/<round-id>/``, and refuses to print a verdict that
does not correspond to a file on disk.

Ordering matters: a wide confidence interval means the sample is too small,
and running bootstrap and drop-best on top of that only produces more noise
from the same noise. So the CI check gates the rest (plan.md R16).
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from research.src import factors as F
from research.src import measure as M
from research.src import panel as P
from research.src.common import OUT_DIR, log

#: plan.md R1. The validation period is a consumable -- it is read once.
DISCOVERY = (date(2016, 1, 4), date(2020, 12, 31))
VALIDATION = (date(2021, 1, 4), date(2025, 12, 31))
#: Sessions after the validation period. Never read by any round before
#: F-20261004-10; used only for a sign check (condition 13).
HOLDOUT_START = date(2026, 1, 2)

#: plan.md R2. Fixed up front so it cannot be tuned after the fact (S-40).
WALKFORWARD_FOLDS = 4
WALKFORWARD_TRAIN_FRACTION = 0.60

#: A fold with fewer non-overlapping periods than this cannot decide a sign
#: question, and its "agreement" must not be counted. At a 60-day horizon a
#: 126-session fold yields ~3 periods, where 4/4 agreement is close to
#: meaningless. Set once, alongside K, not after seeing any result.
MIN_WALKFORWARD_PERIODS_PER_FOLD = 8

#: plan.md R8. IC uses the thinned grid; the portfolio uses non-overlapping.
IC_DATE_STEP = 5

TRADING_DAYS_PER_YEAR = 252


# --------------------------------------------------------------------------
# Formula registry
# --------------------------------------------------------------------------

Builder = Callable[[P.Panel, P.FeatureSet], F.Formula]
_REGISTRY: Dict[str, Builder] = {}

#: Components whose window can be rebuilt by a candidate-supplied function,
#: keyed by component name: (rebuild(panel, window) -> values, fixed grid).
#: The grid is declared with the candidate, before any result (R5, S-31).
EXTRA_SWEEPS: Dict[str, Tuple[Callable[[P.Panel, int], np.ndarray], Tuple[int, ...]]] = {}

#: Formulas whose universe is "eligible AND every component defined", and
#: whose equal-weight benchmark is that same universe (F-20261004-11). Other
#: formulas keep the plain eligibility mask, so earlier rounds are unchanged.
COMPLETE_CASE: set = set()

#: Holding-period grids swept as the R5 plateau check, keyed by formula name.
HORIZON_SWEEPS: Dict[str, Tuple[int, ...]] = {}


def formula_mask(panel: P.Panel, features: P.FeatureSet, formula: F.Formula, *, wide: bool = False) -> np.ndarray:
    mask = P.eligible_mask(panel, features, min_adv20_usd=None if wide else P.MIN_ADV20_USD)
    if formula.name in COMPLETE_CASE:
        for c in formula.components:
            mask &= np.isfinite(c.values)
    return mask


def register(name: str, builder: Builder) -> None:
    _REGISTRY[name] = builder


def available() -> List[str]:
    return sorted(_REGISTRY)


def _production_builder(panel: P.Panel, features: P.FeatureSet) -> F.Formula:
    """The main project's current selection rule, as a pre-registered formula.

    It is the baseline every candidate must beat (plan.md R10-5), and it is
    the harness's own calibration: ``test_harness`` proves the same
    composition reproduces production's ``score_features`` to 0.0.
    """
    return F.Formula(
        name="production",
        components=[
            F.Component("adv20", features.adv20, +1, 20),
            F.Component("r20", features.r20, +1, 20),
            F.Component("r60", features.r60, +1, 60),
            F.Component("vol20", features.vol20, -1, 20),
            F.Component("volume_ratio", features.volume_ratio, +1, 5),
        ],
        horizon=5,
        top_k=20,
        free_parameters=5,
        notes="score = 100*(0.20*p_adv20 + 0.25*p_r20 + 0.25*p_r60 "
        "+ 0.15*(1-p_vol20) + 0.15*p_volume_ratio)",
    )


register("production", _production_builder)


def _load_candidates() -> None:
    """Register the candidate formulas, once, whatever the entry path.

    Running this module as a script puts it in ``sys.modules`` under
    ``__main__``, so ``candidates.py`` importing ``research.src.study`` would
    otherwise create a SECOND copy of this module with its own empty
    registry. The result is that every candidate is "not found" when run from
    the command line but visible when imported as a library.

    Aliasing ``__main__`` to its canonical name first makes both paths reach
    the same registry.
    """
    import sys

    alias = "research.src.study"
    if __name__ == "__main__" and alias not in sys.modules:
        sys.modules[alias] = sys.modules["__main__"]
    from research.src import candidates  # noqa: F401  (registers on import)


_load_candidates()


# --------------------------------------------------------------------------
# Core analysis
# --------------------------------------------------------------------------


def _sessions_in(panel: P.Panel, span: Tuple[date, date]) -> np.ndarray:
    lo, hi = span
    return np.array(
        [i for i, d in enumerate(panel.sessions) if lo <= d <= hi], dtype=int
    )


def _span_periods(panel: P.Panel, span: Tuple[date, date], horizon: int) -> np.ndarray:
    """Non-overlapping portfolio grid INSIDE ``span``.

    Starts at the span's first session, steps by ``horizon``, and keeps only
    periods whose exit session is still inside the span. Letting
    ``build_portfolio`` fall back to its whole-panel grid sampled the leading
    history (the previous period's last months) and, on a panel covering two
    periods, mixed them (process.md S-57).
    """
    idx = _sessions_in(panel, span)[::horizon]
    hi = span[1]
    return np.array(
        [t for t in idx if t + horizon < len(panel.sessions) and panel.sessions[t + horizon] <= hi],
        dtype=int,
    )


def _excess(panel, features, formula, span, mask, *, top_k: int) -> dict:
    """Condition 12/13 measurement: paired excess over equal weight in ``span``."""
    f = F.Formula(formula.name, formula.components, formula.horizon, top_k)
    fwd = P.forward_returns(panel, formula.horizon)
    pf = M.build_portfolio(F.compose_scores(f, mask), fwd, mask, horizon=formula.horizon,
                           top_k=top_k, as_of=_span_periods(panel, span, formula.horizon))
    out = M.equal_weight_excess(pf, fwd, mask, formula.horizon)
    out["period"] = [span[0].isoformat(), span[1].isoformat()]
    return out


def _ic_grid(panel: P.Panel, span: Tuple[date, date], horizon: int) -> np.ndarray:
    """Thinned as-of grid, dropping any that would run past the panel end."""
    idx = _sessions_in(panel, span)
    return idx[(idx + horizon) < len(panel.sessions)]


def analyse(
    panel: P.Panel,
    features: P.FeatureSet,
    formula: F.Formula,
    span: Tuple[date, date],
    *,
    top_k: Optional[int] = None,
    horizons: Optional[Sequence[int]] = None,
) -> dict:
    """Everything measurable for one formula over one period."""
    horizons = list(horizons or [formula.horizon])
    top_k = top_k or formula.top_k
    wide = formula_mask(panel, features, formula, wide=True)
    prod = formula_mask(panel, features, formula)

    out: dict = {
        "period": [span[0].isoformat(), span[1].isoformat()],
        "formula": F.parameter_report(formula),
        "universes": {
            # Bookkeeping, not a universe. Calling it "n_sessions_in_period"
            # alone made `for u in universes` walk into an int and crash.
            "_meta": {"n_sessions_in_period": int(len(_sessions_in(panel, span)))},
        },
    }
    for universe, mask in (("wide", wide), ("production", prod)):
        block: dict = {}
        market = P.cross_sectional_median_log_return(panel)
        regimes = M.label_regimes(market)
        for horizon in horizons:
            grid = _ic_grid(panel, span, horizon)
            fwd = P.forward_returns(panel, horizon)

            ic_rows: List[dict] = []
            for c in formula.components:
                series = M.ic_series(c.values, fwd, mask, grid)
                row = M.summarise_ic(series, horizon, IC_DATE_STEP)
                row.update({"component": c.name, "sign": c.sign, "horizon": horizon})
                ic_rows.append(row)

            # Portfolio: NON-OVERLAPPING periods, date_step = horizon (S-7)
            scores = F.compose_scores(
                F.Formula(formula.name, formula.components, horizon, top_k), mask
            )
            pf = M.build_portfolio(scores, fwd, mask, horizon=horizon, top_k=top_k,
                                   as_of=_span_periods(panel, span, horizon))
            net_base = pf.net(M.COST_SCENARIOS["base"])

            # Independent periods available in a year at this holding period.
            periods_per_year = TRADING_DAYS_PER_YEAR / horizon
            block[f"h{horizon}"] = {
                "ic": ic_rows,
                "portfolio": M.cost_table(pf, horizon),
                "n_periods": len(pf),
                "periods_per_year": periods_per_year,
                "mean_n_available": float(pf.n_available.mean()) if len(pf) else 0.0,
                "n_missing_periods": pf.n_missing_periods,
                "confidence_interval": M.confidence_interval(net_base),
                "bootstrap": M.block_bootstrap(net_base, horizon, n_resamples=1000),
                "drop_best": M.drop_best(net_base, horizon),
                "contribution": M.contribution_decomposition(pf),
                "diversification": M.diversification(pf, panel, horizon),
                "regimes": M.split_by_regime(pf, regimes),
                "return_distribution": M.describe(panel, net_base),
            }
        out["universes"][universe] = block
    return out


# --------------------------------------------------------------------------
# Walk-forward (plan.md R2)
# --------------------------------------------------------------------------


def walk_forward(
    panel: P.Panel,
    features: P.FeatureSet,
    formula: F.Formula,
    span: Tuple[date, date],
    *,
    universe: str = "production",
    folds: int = WALKFORWARD_FOLDS,
) -> dict:
    """Rolling forward validation inside the discovery period.

    Two variants per fold, because they answer different questions:

    * ``frozen``  -- the pre-registered formula on the fold's test segment.
      Answers "does the rule hold on unseen dates?"
    * ``reselect`` -- component signs chosen on the fold's TRAIN segment, then
      applied to the test segment. Answers "does the SELECTION PROCEDURE
      hold?", which is the thing that actually overfits.

    Reporting only ``frozen`` would flatter the search: the signs were picked
    once using all of discovery, so every fold's train segment is already
    contaminated.
    """
    mask = formula_mask(panel, features, formula, wide=universe == "wide")
    idx = _sessions_in(panel, span)
    bounds = np.linspace(0, idx.size, folds + 1).astype(int)
    horizon = formula.horizon
    fwd = P.forward_returns(panel, horizon)
    M.label_regimes(P.cross_sectional_median_log_return(panel))

    rows = []
    for k in range(folds):
        seg = idx[bounds[k] : bounds[k + 1]]
        cut = bounds[k] + int((bounds[k + 1] - bounds[k]) * WALKFORWARD_TRAIN_FRACTION)
        train, test = seg[: cut - bounds[k]], seg[cut - bounds[k] :]
        if test.size == 0 or train.size == 0:
            continue
        for variant in ("frozen", "reselect"):
            comps = formula.components
            if variant == "reselect":
                comps = [
                    F.Component(
                        c.name,
                        c.values,
                        _sign_on(train, c.values, fwd, mask, horizon),
                        c.lookback,
                    )
                    for c in formula.components
                ]
            f = F.Formula(formula.name, comps, horizon, formula.top_k)
            scores = F.compose_scores(f, mask)
            # Thin the test segment to non-overlapping periods. Passing every
            # session would make 126 overlapping periods out of 66 sessions,
            # which double-charges cost and inflates the annualisation
            # (process.md S-7).
            test_periods = np.array(
                [t for t in test[::horizon] if t + horizon < len(panel.sessions)
                 and panel.sessions[t + horizon] <= span[1]], dtype=int)
            pf = M.build_portfolio(
                scores, fwd, mask, horizon=horizon, top_k=formula.top_k,
                as_of=test_periods,
            )
            net = pf.net(M.COST_SCENARIOS["base"])
            rows.append(
                {
                    "fold": k + 1,
                    "variant": variant,
                    "train_start": panel.sessions[train[0]].isoformat(),
                    "train_end": panel.sessions[train[-1]].isoformat(),
                    "test_start": panel.sessions[test[0]].isoformat(),
                    "test_end": panel.sessions[test[-1]].isoformat(),
                    "n_test_sessions": int(test.size),
                    "n_periods": len(pf),
                    "mean_net_per_period": float(np.nanmean(net)) if np.isfinite(net).any() else None,
                    "annualised_net": float(
                        np.nanmean(net) * (M.PERIODS_PER_YEAR / horizon)
                    )
                    if np.isfinite(net).any()
                    else None,
                    "n_below_min": int(np.sum(np.isfinite(net) & (net < 0))),
                }
            )
    signed = [r for r in rows if r["variant"] == "frozen" and r["annualised_net"] is not None]
    agree = None
    if signed:
        signs = np.sign([r["annualised_net"] for r in signed])
        nonzero = signs[signs != 0]
        agree = int(max((nonzero > 0).sum(), (nonzero < 0).sum())) if nonzero.size else 0

    # A sign agreement computed on three points per fold is not evidence.
    # At a 60-day holding period each 126-session fold yields ~3 periods, and
    # "4/4 folds agree" then means almost nothing while reading as a pass.
    # Below the floor the result is reported but the check is left undecided.
    min_periods = MIN_WALKFORWARD_PERIODS_PER_FOLD
    too_thin = bool(signed) and min(r["n_periods"] for r in signed) < min_periods
    return {
        "folds": folds,
        "train_fraction": WALKFORWARD_TRAIN_FRACTION,
        "universe": universe,
        "horizon": horizon,
        "rows": rows,
        "frozen_sign_agreement": f"{agree}/{len(signed)}" if signed else None,
        "min_periods_per_fold": min_periods,
        "too_thin_to_decide": too_thin,
        "note": "K and the train fraction are fixed before results "
        "(process.md S-40); do not tune them after seeing this",
    }


def _sign_on(
    train: np.ndarray,
    values: np.ndarray,
    fwd: np.ndarray,
    mask: np.ndarray,
    horizon: int,
) -> int:
    """Sign of the mean IC on the train segment."""
    usable = train[(train + horizon) < fwd.shape[0]]
    if usable.size == 0:
        return 1
    series = M.ic_series(values, fwd, mask, usable)
    clean = series[np.isfinite(series)]
    if clean.size == 0:
        return 1
    return 1 if float(np.mean(clean)) >= 0 else -1


# --------------------------------------------------------------------------
# Parameter sweep (plan.md R5)
# --------------------------------------------------------------------------


def classify_curve(values: Sequence, metrics: Sequence[float]) -> dict:
    """Plateau or spike?

    A plateau is three consecutive grid values that agree in sign and vary by
    less than 50% between neighbours. A single-significant-cell result is
    the signature of overfitting, not of an edge (process.md S-31).
    """
    finite = [(v, m) for v, m in zip(values, metrics) if m is not None and math.isfinite(m)]
    if len(finite) < 3:
        return {"verdict": "INSUFFICIENT", "run_length": 0, "curve": list(finite)}
    curve = [m for _v, m in finite]
    best_idx = int(np.argmax([abs(m) for m in curve]))
    # Longest run of adjacent cells that share a sign and whose magnitudes
    # are within a factor of two of each other, ANYWHERE on the curve. The
    # earlier version only counted rightward from the best cell, so a curve
    # peaking in its middle or last cell could never be a plateau -- every
    # 3-cell sweep whose best cell was not the first read as SPIKE
    # (process.md S-60).
    run = longest = 1
    for i in range(1, len(curve)):
        a, b = curve[i - 1], curve[i]
        same = a != 0 and b != 0 and math.copysign(1, a) == math.copysign(1, b)
        close = min(abs(a), abs(b)) >= 0.5 * max(abs(a), abs(b))
        run = run + 1 if same and close else 1
        longest = max(longest, run)
    run = longest
    verdict = "PLATEAU" if run >= 3 else "SPIKE"
    return {
        "verdict": verdict,
        "run_length": run,
        "best_value": finite[best_idx][0],
        "best_metric": finite[best_idx][1],
        "curve": [{"value": v, "metric": m} for v, m in finite],
        "rule": "three consecutive same-sign cells, adjacent magnitudes within "
        "a factor of two, anywhere on the curve (plan.md R5)",
    }


def parameter_sweep(
    panel: P.Panel,
    features: P.FeatureSet,
    parameter: F.Parameter,
    span: Tuple[date, date],
    *,
    metric_horizon: int,
    metric_universe: str = "production",
    top_k: int = 20,
    mask: Optional[np.ndarray] = None,
) -> dict:
    """Sweep one parameter over its fixed grid and classify the curve.

    Two series are produced for every cell, because they answer different
    questions and can disagree:

    * ``annualised_net`` -- the portfolio after costs, i.e. the R5 plateau
      test. This is the one that decides condition 8.
    * ``ic_mean`` -- the component's own cross-sectional IC, i.e. the
      evidence that the component works at that window regardless of the
      portfolio around it.

    A window can look fine on IC and fail in the portfolio, or the reverse.
    Reporting only one hides which is happening.
    """
    if mask is None:
        mask = P.eligible_mask(
            panel,
            features,
            min_adv20_usd=None if metric_universe == "wide" else P.MIN_ADV20_USD,
        )
    fwd = P.forward_returns(panel, metric_horizon)
    grid = _ic_grid(panel, span, metric_horizon)
    rows = []
    for value in parameter.values:
        comps = parameter.build(value)
        f = F.Formula("sweep", comps, metric_horizon, top_k)
        scores = F.compose_scores(f, mask)
        pf = M.build_portfolio(scores, fwd, mask, horizon=metric_horizon, top_k=top_k,
                               as_of=_span_periods(panel, span, metric_horizon))
        net = pf.net(M.COST_SCENARIOS["base"])
        series = M.ic_series(comps[0].values, fwd, mask, grid)
        rows.append(
            {
                "value": value,
                "annualised_net": float(np.nanmean(net) * (M.PERIODS_PER_YEAR / metric_horizon))
                if np.isfinite(net).any()
                else None,
                "ic_mean": float(np.nanmean(series)) if np.isfinite(series).any() else None,
                "n_periods": len(pf),
            }
        )
    curve = classify_curve(parameter.values, [r["annualised_net"] for r in rows])
    curve["parameter"] = parameter.name
    curve["grid"] = list(parameter.values)
    curve["rows"] = rows
    # The plateau test is only meaningful if the cells genuinely differ.
    # A constant parameter would pass it while measuring nothing.
    distinct = len({r["value"] for r in rows}) == len(rows)
    curve["genuine"] = distinct
    if not distinct:
        curve["verdict"] = "NOT-SWEPTABLE"
    return curve


def horizon_sweep(
    panel: P.Panel,
    formula: F.Formula,
    span: Tuple[date, date],
    grid: Sequence[int],
    mask: np.ndarray,
    *,
    top_k: int = 20,
) -> dict:
    """R5 plateau check on the holding period (plan.md R4 counts it as a
    parameter). The score does not depend on the horizon; only the
    rebalance spacing and the forward return do."""
    scores = F.compose_scores(formula, mask)
    rows = []
    for h in grid:
        fwd = P.forward_returns(panel, h)
        pf = M.build_portfolio(scores, fwd, mask, horizon=h, top_k=top_k,
                               as_of=_span_periods(panel, span, h))
        net = pf.net(M.COST_SCENARIOS["base"])
        rows.append({
            "value": h,
            "annualised_net": float(np.nanmean(net) * (M.PERIODS_PER_YEAR / h)) if np.isfinite(net).any() else None,
            "ic_mean": None,
            "n_periods": len(pf),
        })
    curve = classify_curve(list(grid), [r["annualised_net"] for r in rows])
    curve.update(parameter="horizon", grid=list(grid), rows=rows, genuine=True)
    return curve


# --------------------------------------------------------------------------
# Acceptance (plan.md R10)
# --------------------------------------------------------------------------


def _universe(blob: dict, which: str, horizon: int) -> dict:
    """The ``h<horizon>`` block for one universe.

    Resolves the horizon explicitly rather than trusting ``formula.horizon``:
    ``--horizons`` can request a horizon the formula was not declared with,
    and the two then disagree.
    """
    blob = blob.get("universes", blob)
    if which not in blob:
        raise KeyError(f"universe {which!r} missing; have {sorted(blob)}")
    return blob[which][f"h{horizon}"]


def judge(
    discovery: dict,
    validation: Optional[dict],
    *,
    t_bonf: float,
    walk_forward_result: Optional[dict] = None,
    sweeps: Optional[List[dict]] = None,
    baseline: Optional[float] = None,
    participation_max: Optional[float] = None,
    excess: Optional[dict] = None,
    holdout_excess: Optional[dict] = None,
) -> dict:
    """The eleven pass conditions of plan.md R10.

    A condition whose evidence was not measured is recorded as ``None`` and
    the verdict is UNDECIDED -- never as a pass. "We didn't check" and "it
    passed" are different facts and collapsing them is how a weak result gets
    promoted (plan.md R17).
    """
    horizon = int(discovery["formula"]["horizon"])
    dblock = _universe(discovery, "production", horizon)
    wblock = _universe(discovery, "wide", horizon)
    dport = dblock["portfolio"]["base"]
    dwide = wblock["portfolio"]["base"]
    drobust = dblock["confidence_interval"]
    ddrop = dblock["drop_best"]["by_fraction"].get("5%", {})

    vblock = vport = None
    if validation:
        vblock = _universe(validation, "production", horizon)
        vport = vblock["portfolio"]["base"]

    checks: List[dict] = []

    def add(num: int, name: str, passed: Optional[bool], detail: str) -> None:
        checks.append({"n": num, "name": name, "passed": passed, "detail": detail})

    add(
        1,
        "validation |t_NW| >= t_bonf",
        None if vport is None else abs(vport["nw_t_gross"] or 0.0) >= t_bonf,
        f"validation NW t = "
        f"{None if vport is None else round(vport['nw_t_gross'], 3)}, "
        f"t_bonf = {t_bonf:.3f}",
    )
    add(
        2,
        "discovery and validation same sign",
        None
        if vport is None
        else (dport["annualised_net"] or 0.0) * (vport["annualised_net"] or 0.0) > 0,
        f"discovery net annualised {dport['annualised_net']}, "
        f"validation {None if vport is None else vport['annualised_net']}",
    )
    add(
        3,
        "validation net annualised > 0",
        None if vport is None else (vport["annualised_net"] or -1.0) > 0,
        f"{None if vport is None else vport['annualised_net']}",
    )
    if vblock is not None:
        v2x = vblock["portfolio"]["2.0x"]["annualised_net"]
        add(4, "validation net annualised > 0 at 2.0x cost", (v2x or -1.0) > 0, f"{v2x}")
    else:
        add(4, "validation net annualised > 0 at 2.0x cost", None, "no validation run yet")
    if baseline is None or vport is None:
        add(5, "beats the production baseline", None, "no validation baseline run supplied")
    else:
        # plan.md R10-5 says "in the same VALIDATION period". The earlier
        # version compared the discovery number and was never given a baseline.
        add(
            5,
            "beats the production baseline",
            (vport["annualised_net"] or -1e9) > baseline,
            f"validation: formula {vport['annualised_net']} vs production score {baseline} "
            "(net annualised, same period, universe and holding period)",
        )
    add(
        6,
        "holds in both universes",
        (dwide["annualised_net"] or 0.0) * (dport["annualised_net"] or 0.0) > 0,
        f"wide {dwide['annualised_net']}, production {dport['annualised_net']}",
    )
    agree = (walk_forward_result or {}).get("frozen_sign_agreement")
    thin = (walk_forward_result or {}).get("too_thin_to_decide")
    if agree is None:
        add(7, "walk-forward >= 3/4 folds same sign", None, "no walk-forward run")
    elif thin:
        add(
            7,
            "walk-forward >= 3/4 folds same sign",
            None,
            f"UNDECIDABLE: agreement = {agree} but a fold holds only "
            f"{min(r['n_periods'] for r in walk_forward_result['rows'] if r['variant'] == 'frozen')} "
            f"non-overlapping periods, below the floor of "
            f"{MIN_WALKFORWARD_PERIODS_PER_FOLD}. A {horizon}-day horizon over 4 folds "
            "of 5 years leaves too few test periods to measure sign agreement (process.md S-6)",
        )
    else:
        got_n, got_tot = (int(v) for v in str(agree).split("/"))
        # R10-7 requires 3 of 4, i.e. at least 75% -- not "at least half".
        # The half-threshold version passed 2/4, which is how a coin flip
        # passes a sign test.
        add(
            7,
            "walk-forward >= 3/4 folds same sign",
            got_n * 4 >= got_tot * 3,
            f"frozen-variant agreement = {agree} (threshold 3/4; K fixed at "
            f"{WALKFORWARD_FOLDS} before results, process.md S-40)",
        )
    if not sweeps:
        add(8, "every parameter +/- 1 cell same sign", None, "no parameter sweep run")
    else:
        spikes = [s["parameter"] for s in sweeps if s["verdict"] != "PLATEAU"]
        add(
            8,
            "every parameter +/- 1 cell same sign",
            not spikes,
            f"swept {[s['parameter'] for s in sweeps]}; spikes: {spikes or 'none'}",
        )
    cells = {k: v for k, v in dblock["regimes"].items() if v.get("n_periods", 0) > 0}
    signs = [math.copysign(1, v["mean_net"]) for v in cells.values() if v.get("mean_net")]
    agreeing = max((sum(1 for s in signs if s > 0), sum(1 for s in signs if s < 0)), default=0)
    add(
        9,
        "same sign in >= 2 regimes",
        agreeing >= 2,
        f"{agreeing} regimes agree, out of {len(cells)} with any data: "
        + ", ".join(f"{k}(n={v['n_periods']})" for k, v in cells.items()),
    )
    add(
        10,
        "same sign after dropping best 5% of periods",
        not ddrop.get("sign_flipped", False),
        f"dropped {ddrop.get('dropped')} periods, mean {ddrop.get('mean')}, "
        f"sign_flipped={ddrop.get('sign_flipped')}",
    )
    if participation_max is None:
        add(
            11,
            "order size <= 0.1% of ADV20",
            None,
            "participation not supplied; pass --notional to measure it",
        )
    else:
        add(
            11,
            "order size <= 0.1% of ADV20",
            participation_max <= 0.001,
            f"worst-case participation = {participation_max:.4%} of ADV20",
        )
    if excess is None:
        add(12, "validation excess over same-universe equal weight is significant", None,
            "no equal-weight benchmark run (needs a validation run)")
    else:
        boot = excess.get("bootstrap") or {}
        t_ex = excess.get("nw_t")
        add(
            12,
            "validation excess over same-universe equal weight is significant",
            t_ex is not None and t_ex >= t_bonf and boot.get("ci_low") is not None and boot["ci_low"] > 0,
            f"annualised excess {excess.get('annualised_excess')}, NW t {t_ex} (needs >= {t_bonf:.3f}), "
            f"{boot.get('block')}-period block bootstrap 95% CI per period "
            f"[{boot.get('ci_low')}, {boot.get('ci_high')}] (needs low > 0); {excess.get('benchmark')}",
        )
    if holdout_excess is not None:
        same = (
            excess is not None
            and (excess.get("mean_excess_per_period") or 0.0) * (holdout_excess.get("mean_excess_per_period") or 0.0) > 0
        )
        add(
            13,
            "untouched holdout excess has the validation sign",
            same if holdout_excess.get("n_periods") else None,
            f"holdout mean excess/period {holdout_excess.get('mean_excess_per_period')} over "
            f"{holdout_excess.get('n_periods')} periods (sign check only; too few periods for a t)",
        )
    decided = [c for c in checks if c["passed"] is not None]
    ratio = drobust.get("width_over_abs_mean")
    disclosure = {
        "name": "sample sufficiency (DISCLOSURE, not a pass condition)",
        "metric": "CI width / |mean period return|",
        "value": ratio,
        "n_periods": drobust.get("n"),
        "reading": _sufficiency_reading(ratio),
        "why_not_a_condition": (
            "The original threshold was CI width < effect. It is "
            "mathematically unreachable on this dataset at any holding "
            "period -- 404 years of data at 5 days, 30 years at 60. A rule "
            "that can only ever say FAIL trains people to ignore the rules, "
            "so it was demoted to a mandatory disclosure. See plan.md R16-Q1 "
            "and process.md S-45."
        ),
    }
    return {
        "t_bonf": t_bonf,
        "excess_validation": excess,
        "excess_holdout": holdout_excess,
        "baseline_validation_net_annualised": baseline,
        "checks": checks,
        "disclosures": [disclosure],
        "n_checks": len(checks),
        "n_decided": len(decided),
        "n_failed": sum(1 for c in decided if not c["passed"]),
        "verdict": "REJECTED"
        if any(not c["passed"] for c in decided)
        else ("UNDECIDED" if len(decided) < len(checks) else "PASSED"),
        "note": "passed=None means NOT MEASURED, which is not a pass "
        "(plan.md R17). Conditions 1-5 require a validation run; without one "
        "the verdict is UNDECIDED, never PASSED.",
    }


def _sufficiency_reading(ratio: Optional[float]) -> str:
    """Turn the CI ratio into a sentence a reader can act on."""
    if ratio is None:
        return "not computable"
    if ratio < 2.0:
        return "the direction of the effect is broadly trustworthy"
    if ratio < 5.0:
        return (
            "the direction may be right but the magnitude is not "
            "trustworthy; do not quote the point estimate"
        )
    return (
        "THIS SAMPLE CANNOT DISTINGUISH THE EFFECT FROM ZERO. Any point "
        "estimate here must not be cited as a conclusion, including the "
        "passing conditions."
    )


# --------------------------------------------------------------------------
# Outputs (plan.md 6.3)
# --------------------------------------------------------------------------


def _round_dir(round_id: str) -> Path:
    d = OUT_DIR / round_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dump(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, indent=2, default=_json_default), encoding="utf-8"
    )
    log(f"wrote {path.relative_to(OUT_DIR.parent)}")


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def _csv(path: Path, rows: List[dict], columns: List[str]) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log(f"wrote {path.relative_to(OUT_DIR.parent)}")


def write_outputs(
    round_id: str,
    *,
    formula: F.Formula,
    discovery: dict,
    validation: Optional[dict],
    walk_forward_result: dict,
    sweeps: List[dict],
    acceptance: dict,
    participation_max: Optional[float],
    baseline_net: Optional[float],
    search_cost: dict,
) -> Path:
    """Every artefact plan.md 6.3 lists. A round missing any of them is not
    finished and must not be recorded as VALIDATED."""
    d = _round_dir(round_id)
    horizon = int(discovery["formula"]["horizon"])
    dblock = discovery["universes"]["production"][f"h{horizon}"]

    _dump(
        d / "portfolio.json",
        {
            "meta": {
                "round": round_id,
                "formula": formula.name,
                "horizon": horizon,
                "top_k": formula.top_k,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "cost_components": M.COST_COMPONENTS,
                "cost_scenarios": M.COST_SCENARIOS,
                "sampling": "non-overlapping (date_step = horizon)",
                "ic_sampling": f"date_step = {IC_DATE_STEP}",
            },
            "discovery": {
                u: {k: v for k, v in b.items() if k.startswith("h")}
                for u, b in discovery["universes"].items()
                if u != "_meta"
            },
            "validation": (
                {
                    u: {k: v for k, v in b.items() if k.startswith("h")}
                    for u, b in validation["universes"].items()
                    if u != "_meta"
                }
                if validation
                else None
            ),
            "baseline_net_annualised": baseline_net,
            "max_participation_of_adv20": participation_max,
        },
    )

    ic_rows = []
    for period, blob in (("discovery", discovery), ("validation", validation)):
        if not blob:
            continue
        for universe, block in blob["universes"].items():
            for key, v in block.items():
                if not key.startswith("h"):
                    continue
                for row in v["ic"]:
                    ic_rows.append(
                        {"period": period, "universe": universe, **row}
                    )
    _csv(
        d / "ic.csv",
        ic_rows,
        ["period", "universe", "horizon", "component", "sign", "ic_mean",
         "ic_median", "ic_t", "n", "n_positive", "positive_ratio", "nw_lag"],
    )

    _csv(
        d / "walkforward.csv",
        walk_forward_result["rows"],
        ["fold", "variant", "train_start", "train_end", "test_start",
         "test_end", "n_test_sessions", "n_periods", "mean_net_per_period",
         "annualised_net", "n_below_min"],
    )

    sweep_rows = [
        {
            "parameter": s["parameter"],
            "value": r["value"],
            "annualised_net": r["annualised_net"],
            "n_periods": r["n_periods"],
            "verdict": s["verdict"],
            "run_length": s["run_length"],
        }
        for s in sweeps
        for r in s["rows"]
    ]
    _csv(
        d / "param_sweep.csv",
        sweep_rows,
        ["parameter", "value", "annualised_net", "n_periods", "verdict",
         "run_length"],
    )

    regime_rows = [
        {"universe": "production", "cell": k, **v}
        for k, v in dblock["regimes"].items()
    ]
    _csv(
        d / "regimes.csv",
        regime_rows,
        ["universe", "cell", "n_periods", "mean_gross", "mean_net",
         "annualised_net", "positive_ratio"],
    )

    _dump(
        d / "robustness.json",
        {
            "confidence_interval": dblock["confidence_interval"],
            "block_bootstrap": dblock["bootstrap"],
            "drop_best": dblock["drop_best"],
            "contribution": dblock["contribution"],
            "n_periods": dblock["n_periods"],
            "periods_per_year": dblock["periods_per_year"],
            "note": "resampling unit is the PERIOD, never the trade "
            "(process.md S-33); block length is the holding period (S-16)",
        },
    )
    _dump(
        d / "correlation.json",
        {
            "diversification": dblock["diversification"],
            "note": "20 names is not 20 independent bets (process.md S-39)",
        },
    )
    _dump(d / "acceptance.json", acceptance)
    (d / "formula.md").write_text(_formula_spec(formula, search_cost), encoding="utf-8")
    log(f"wrote {(d / 'formula.md').relative_to(OUT_DIR.parent)}")
    (d / "report.md").write_text(
        _report(round_id, formula, discovery, validation, walk_forward_result,
                sweeps, acceptance, search_cost),
        encoding="utf-8",
    )
    log(f"wrote {(d / 'report.md').relative_to(OUT_DIR.parent)}")
    return d


def _formula_spec(formula: F.Formula, search_cost: dict) -> str:
    lines = [
        f"# {formula.name}",
        "",
        "> Pre-registered specification. Written BEFORE results were seen;",
        "> if you are reading it after the fact, the round is not a valid test.",
        "",
        "## Parameters",
        "",
        f"- free parameters: **{formula.free_parameters}** (budget 6, plan.md R4)",
        f"- holding period: **{formula.horizon}** sessions",
        f"- selection: top **{formula.top_k}** by score, equal weight",
        f"- variants tried this round: {search_cost.get('variants_tried', 'n/a')}",
        "",
        "## Components",
        "",
        "| # | name | sign | lookback |",
        "|---:|---|---:|---:|",
    ]
    for i, c in enumerate(formula.components, 1):
        lines.append(f"| {i} | `{c.name}` | {c.sign:+d} | {c.lookback} |")
    lines += [
        "",
        "## Composition",
        "",
        "```",
        "score = 100 * mean_over_components( sign * percentile_cs(component) )",
        "        percentile computed over the ELIGIBLE SET ONLY (process.md S-12)",
        "```",
        "",
        "## Entry and exit",
        "",
        "```",
        "entry = open[as_of + 1]",
        "exit  = close[as_of + horizon]",
        "```",
        "",
        f"{formula.notes}" if formula.notes else "",
        "",
    ]
    return "\n".join(lines) + "\n"


def _report(
    round_id: str,
    formula: F.Formula,
    discovery: dict,
    validation: Optional[dict],
    walk_forward_result: dict,
    sweeps: List[dict],
    acceptance: dict,
    search_cost: dict,
) -> str:
    horizon = int(discovery["formula"]["horizon"])
    d = discovery["universes"]["production"][f"h{horizon}"]
    w = discovery["universes"]["wide"][f"h{horizon}"]
    out = [
        f"# Round {round_id} — {formula.name}",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Verdict",
        "",
        f"**{acceptance['verdict']}** — "
        f"{acceptance['n_failed']} of {acceptance['n_decided']} decided checks failed "
        f"({acceptance['n_decided']}/{acceptance['n_checks']} measured).",
        "",
        "| # | condition | result | detail |",
        "|---:|---|---|---|",
    ]
    for c in acceptance["checks"]:
        mark = {True: "PASS", False: "**FAIL**", None: "not measured"}[c["passed"]]
        # A raw pipe inside a cell ends the cell and silently corrupts the
        # whole table, so the name and detail are escaped before printing.
        name = c["name"].replace("|", "\\|")
        detail = str(c["detail"]).replace("|", "\\|")
        out.append(f"| {c['n']} | {name} | {mark} | {detail} |")

    for note in acceptance.get("disclosures", []):
        out += [
            "",
            "## Sample sufficiency (disclosure, not a pass condition)",
            "",
            f"- **{note['metric']} = {note['value']}** "
            f"(n = {note.get('n_periods')} periods)",
            f"- **{note['reading']}**",
            "",
            f"> {note['why_not_a_condition']}",
        ]

    out += [
        "",
        "## Component IC (production universe, discovery)",
        "",
        "| component | sign | IC | NW t | n | positive |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in d["ic"]:
        out.append(
            f"| `{row['component']}` | {row['sign']:+d} | {row['ic_mean']} | "
            f"{row['ic_t']} | {row['n']} | {row['positive_ratio']} |"
        )

    out += [
        "",
        "## Portfolio (non-overlapping periods, net of cost)",
        "",
        "| universe | scenario | periods | mean/period | NW t | annualised |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for label, block in (("wide", w), ("production", d)):
        for scenario, s in block["portfolio"].items():
            out.append(
                f"| {label} | {scenario} | {s['n_periods']} | "
                f"{s['mean_net_per_period']} | {s['nw_t_net']} | "
                f"{s['annualised_net']} |"
            )

    out += [
        "",
        "## Walk-forward",
        "",
        f"{walk_forward_result.get('frozen_sign_agreement')} folds agree "
        f"(K={walk_forward_result['folds']}, train="
        f"{walk_forward_result['train_fraction']}, fixed before results).",
        "",
        "| fold | variant | test window | periods | annualised net |",
        "|---:|---|---|---:|---:|",
    ]
    for r in walk_forward_result["rows"]:
        out.append(
            f"| {r['fold']} | {r['variant']} | {r['test_start']}..{r['test_end']} | "
            f"{r['n_periods']} | {r['annualised_net']} |"
        )

    out += ["", "## Parameter sweeps", ""]
    if sweeps:
        out += ["| parameter | verdict | run length | best value | best metric |",
                "|---|---|---:|---:|---:|"]
        for s in sweeps:
            out.append(
                f"| `{s['parameter']}` | **{s['verdict']}** | {s['run_length']} | "
                f"{s.get('best_value')} | {s.get('best_metric')} |"
            )
    else:
        out.append("None run. Condition 8 stays unmeasured.")

    out += [
        "",
        "## Regimes",
        "",
        "| cell | periods | mean net | annualised net |",
        "|---|---:|---:|---:|",
    ]
    for cell, v in d["regimes"].items():
        out.append(
            f"| {cell} | {v.get('n_periods', 0)} | {v.get('mean_net')} | "
            f"{v.get('annualised_net')} |"
        )

    div = d["diversification"]
    out += [
        "",
        "## Diversification",
        "",
        f"- basis: {div.get('basis', 'n/a')}",
        f"- mean pairwise correlation among picks: **{div.get('mean_pairwise_corr')}**",
        f"- median positions: {div.get('median_positions_per_period')}",
        f"- **effective positions: {div.get('effective_positions')}**",
        f"- {div.get('note', '')}",
        "",
        "## Robustness",
        "",
        f"- non-overlapping periods constructed: {d['n_periods']} "
        f"(≈{d['periods_per_year']:.0f}/year at this holding period)",
        f"- periods with a usable return: "
        f"{d['portfolio']['base']['n_periods']}",
        f"- mean picks with a forward return: {d['mean_n_available']:.1f} of {formula.top_k}",
        f"- periods where nothing could be exited: {d['n_missing_periods']}",
        f"- CI width / |mean| = **{d['confidence_interval'].get('width_over_abs_mean')}**",
        f"- block bootstrap crosses zero: **{d['bootstrap'].get('crosses_zero')}**",
        f"- drop best 5% flips sign: **{d['drop_best']['by_fraction'].get('5%', {}).get('sign_flipped')}**",
        "",
        "## Search cost",
        "",
        "```",
        json.dumps(search_cost, indent=2, default=_json_default),
        "```",
        "",
        "## Disclosures (plan.md 4.4)",
        "",
        "1. Survivorship bias: the universe is today's still-traded US equities. "
        "Delisted and acquired names are absent entirely. Direction is optimistic "
        "and the share is NOT measurable from this dataset.",
        "2. No point-in-time market cap, so this universe is not the production "
        "universe. Results do not extrapolate to the full production pipeline.",
        "3. Split-adjusted only, no dividends. A real bias for 20/40/60-day windows, "
        "direction is an overestimate.",
        "4. Costs are estimates, not measured fills. The 2.0x scenario is a bound, "
        "not a bid/ask measurement.",
        "5. Capacity is not evaluated. The 0.1% participation cap is an early "
        "warning, not a capacity study.",
        "6. This project does no position-level risk management. Passing all "
        "eleven conditions does NOT make a formula deployable.",
        "",
    ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Participation rate (plan.md R13-L2, R10-11)
# --------------------------------------------------------------------------


def max_participation(
    panel: P.Panel,
    features: P.FeatureSet,
    scores: np.ndarray,
    mask: np.ndarray,
    *,
    horizon: int,
    top_k: int,
    notional: float,
) -> Optional[float]:
    """Worst-case order size as a fraction of that name's ADV20.

    ``notional`` is the per-name order value, NOT the portfolio value: a
    20-name book puts 1/20 of the portfolio into each name, and the relevant
    comparison is that slice against the name's own daily dollar volume.

    Returns ``None`` when ADV20 is unavailable rather than guessing -- a
    participation number invented from a missing denominator is worse than
    no number.
    """
    if notional <= 0:
        return None
    worst = 0.0
    seen = 0
    for t in np.arange(0, len(panel.sessions) - horizon, horizon):
        cols = np.flatnonzero(mask[t])
        if cols.size < 20:
            continue
        vals = scores[t][cols]
        order = np.lexsort((cols, -vals))
        for j in cols[order][:top_k]:
            adv = features.adv20[t][j]
            if not np.isfinite(adv) or adv <= 0:
                continue
            worst = max(worst, notional / float(adv))
            seen += 1
    return worst if seen else None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _sweepable(panel: P.Panel, name: str) -> bool:
    """Which components can be rebuilt at a different window.

    Only the simple-return family has an alternative definition available
    from the panel alone. Sweeping a component that cannot be rebuilt would
    produce a flat curve, which reads as a plateau and would pass R5 while
    measuring nothing -- a worse outcome than not sweeping at all.
    """
    return name in ("r5", "r20", "r60", "r90")


#: Momentum legs that are legal to sweep. 90 is included so the operator's
#: chosen configuration is not the one component that escapes R5.
SWEEP_WINDOWS = (5, 20, 60, 90, 120)


def _momentum(panel: P.Panel, lookback: int) -> np.ndarray:
    """Simple return over ``lookback`` sessions, from the panel closes.

    Uses the same definition production uses for r5/r20/r60:
    ``close[t] / close[t-lookback] - 1``.
    """
    close = panel.close
    out = np.full(close.shape, np.nan, dtype=np.float64)
    if close.shape[0] > lookback:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[lookback:] = close[lookback:] / close[:-lookback] - 1.0
    return out


def _default_sweep(formula: F.Formula, panel: P.Panel) -> List[F.Parameter]:
    """Sweep each momentum component's window over a grid fixed in advance.

    The grid lives in code, written before any result exists. Expanding it
    after seeing a curve is how a plateau becomes a spike retroactively
    (process.md S-31). The number of cells swept is reported in the search
    cost so it can be added to ``N_eff`` (R9).
    """
    sweeps: List[F.Parameter] = []
    for c in formula.components:
        if c.name in EXTRA_SWEEPS:
            rebuild, grid = EXTRA_SWEEPS[c.name]

            def build_extra(value, target=c, rebuild=rebuild):
                return [
                    F.Component(other.name, rebuild(panel, value) if other is target else other.values,
                                other.sign, value if other is target else other.lookback)
                    for other in formula.components
                ]

            sweeps.append(F.Parameter(name=f"{c.name}_window", values=list(grid), build=build_extra))
            continue
        if not _sweepable(panel, c.name):
            continue

        def build(value, target=c):
            """The formula with `target`'s window replaced, others held."""
            comps = [
                F.Component(
                    other.name,
                    _momentum(panel, value) if other is target else other.values,
                    other.sign,
                    value if other is target else other.lookback,
                )
                for other in formula.components
            ]
            return comps

        # Neighbourhood of the declared window, clipped to the legal set.
        # 90 needs 60/90/120, not 45/90/180: a 180-day lookback needs 253
        # sessions of history, which the 5-year discovery window cannot
        # supply for most of the sample.
        grid = sorted(
            {w for w in SWEEP_WINDOWS if 0.4 * c.lookback <= w <= 2.5 * c.lookback}
        )
        if c.lookback not in grid:
            grid.append(c.lookback)
        grid = sorted(set(grid))
        if len(grid) < 3:
            continue
        sweeps.append(F.Parameter(name=f"{c.name}_window", values=grid, build=build))
    return sweeps


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--formula", default="production", help=f"one of {available()}")
    p.add_argument("--start", default=DISCOVERY[0].isoformat())
    p.add_argument("--end", default=DISCOVERY[1].isoformat())
    p.add_argument(
        "--period",
        choices=("discovery", "validation", "both"),
        default="discovery",
        help="validation reads the consumable holdout (plan.md R1)",
    )
    p.add_argument("--symbols-file", default=str(OUT_DIR / "trusted_symbols.txt"))
    p.add_argument("--round-id", default=None)
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--horizons", default=None, help="comma separated, default: formula's own")
    p.add_argument("--max-symbols", type=int, default=0)
    p.add_argument(
        "--notional",
        type=float,
        default=5_000.0,
        help="per-name order value, for the participation cap (plan.md R13-L2)",
    )
    p.add_argument("--walk-forward", type=int, default=WALKFORWARD_FOLDS)
    p.add_argument(
        "--family-size",
        type=int,
        default=0,
        help="pre-registered number of formulas in this round; when set, t_bonf is "
        "Bonferroni over the round's formulas instead of one formula's components",
    )
    p.add_argument("--bootstrap", type=int, default=1000)
    p.add_argument("--dry-run", action="store_true", help="build the panel, print, write nothing")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.formula not in _REGISTRY:
        raise SystemExit(f"unknown formula {args.formula!r}; have {available()}")

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    log(f"building panel for {start} .. {end} (needs credentials)")
    panel = P.build_panel(
        start=start,
        end=end,
        symbols_file=args.symbols_file,
        max_symbols=args.max_symbols,
    )
    features = P.compute_features(panel)
    wide = P.eligible_mask(panel, features, min_adv20_usd=None)
    prod = P.eligible_mask(panel, features, min_adv20_usd=P.MIN_ADV20_USD)
    log(f"eligible per session: wide median {np.median(wide.sum(1)):.0f}, "
        f"production median {np.median(prod.sum(1)):.0f}")

    formula = _REGISTRY[args.formula](panel, features)
    if formula.name in COMPLETE_CASE:
        wide = formula_mask(panel, features, formula, wide=True)
        prod = formula_mask(panel, features, formula)
        log(f"complete-case universe: wide median {np.median(wide.sum(1)):.0f}, "
            f"production median {np.median(prod.sum(1)):.0f}")
    horizons = (
        [int(h) for h in args.horizons.split(",")]
        if args.horizons
        else [formula.horizon]
    )
    log(f"formula {formula.name}: {len(formula.components)} components, "
        f"horizon {formula.horizon}, {formula.free_parameters} free parameters")

    # Discovery never extends into the holdout, whatever --end says: with
    # --period both the panel covers 2016-2025, and using (start, end) here
    # handed the walk-forward, the sweeps and t_bonf the validation years.
    span = (start, min(end, DISCOVERY[1]))
    discovery = analyse(panel, features, formula, span, top_k=args.top_k, horizons=horizons)
    wf = walk_forward(panel, features, formula, span, folds=args.walk_forward)
    sweeps: List[dict] = []
    for parameter in _default_sweep(formula, panel):
        sweeps.append(
            parameter_sweep(
                panel, features, parameter, span,
                metric_horizon=formula.horizon, top_k=args.top_k, mask=prod,
            )
        )
    if formula.name in HORIZON_SWEEPS:
        sweeps.append(horizon_sweep(panel, formula, span, HORIZON_SWEEPS[formula.name], prod,
                                    top_k=args.top_k))

    scores = F.compose_scores(formula, prod)
    part = max_participation(
        panel, features, scores, prod,
        horizon=formula.horizon, top_k=args.top_k, notional=args.notional,
    )

    # Family-wise threshold from the DISCOVERY component IC only. Deriving it
    # from the validation period would spend the holdout on setting the bar.
    h = formula.horizon
    grid = _ic_grid(panel, span, h)
    fwd = P.forward_returns(panel, h)

    # Every component is scored on the SAME as-of grid, then rows are dropped
    # where ANY component is undefined. Without the shared mask the rows have
    # different lengths -- a 90-day lookback is undefined for the first 90
    # sessions of the panel, a 20-day one is not -- and numpy refuses to
    # stack them. Correlating differently-sampled rows would also understate
    # N_eff, i.e. quietly make the threshold too lax.
    ic_matrix = np.array(
        [M.ic_series(c.values, fwd, prod, grid) for c in formula.components],
        dtype=float,
    )
    if ic_matrix.size:
        ic_matrix = ic_matrix[:, np.all(np.isfinite(ic_matrix), axis=0)]
    n_eff_info = (
        M.effective_tests(ic_matrix)
        if ic_matrix.ndim == 2 and ic_matrix.shape[0] >= 2 and ic_matrix.shape[1] >= 3
        else {"n_series": int(ic_matrix.shape[0]) if ic_matrix.ndim == 2 else 0,
              "n_eff": float(max(1, ic_matrix.shape[0] if ic_matrix.ndim == 2 else 1)),
              "eigenvalues": [],
              "note": "too few complete series to estimate N_eff from the "
              "correlation matrix; falling back to the component count"}
    )
    t_bonf = M.bonferroni_t(max(n_eff_info["n_eff"], args.family_size or 0), df=1000)
    log(f"N_eff = {n_eff_info['n_eff']:.2f} from {n_eff_info['n_series']} components "
        f"-> t_bonf = {t_bonf:.3f}")

    validation = None
    if args.period in ("validation", "both"):
        v_start, v_end = VALIDATION
        if start <= v_start and end >= v_end:
            validation = analyse(
                panel, features, formula, (v_start, v_end),
                top_k=args.top_k, horizons=horizons,
            )
        else:
            log(
                f"validation window {v_start}..{v_end} is outside the built "
                f"panel {start}..{end}; rebuild with --start/--end covering it. "
                "Skipping -- the holdout is read once, on purpose (plan.md R1)."
            )

    baseline_net = excess = holdout_excess = None
    if validation is not None:
        v_span = VALIDATION
        excess = _excess(panel, features, formula, v_span, prod, top_k=args.top_k)
        base_formula = _REGISTRY["production"](panel, features)
        base_formula = F.Formula(base_formula.name, base_formula.components, formula.horizon, args.top_k)
        base_scores = F.compose_scores(base_formula, prod)
        base_pf = M.build_portfolio(
            base_scores, P.forward_returns(panel, formula.horizon), prod,
            horizon=formula.horizon, top_k=args.top_k,
            as_of=_span_periods(panel, v_span, formula.horizon),
        )
        baseline_net = base_pf.annualised(base_pf.net(M.COST_SCENARIOS["base"]), formula.horizon)
        if end >= HOLDOUT_START:
            holdout_excess = _excess(panel, features, formula, (HOLDOUT_START, end), prod, top_k=args.top_k)

    acceptance = judge(
        discovery, validation,
        t_bonf=t_bonf,
        walk_forward_result=wf,
        sweeps=sweeps,
        baseline=baseline_net,
        participation_max=part,
        excess=excess,
        holdout_excess=holdout_excess,
    )
    if excess is not None and formula.name in COMPLETE_CASE:
        # Disclosure only: the same picks against the FULL eligible universe.
        full = P.eligible_mask(panel, features, min_adv20_usd=P.MIN_ADV20_USD)
        fwd_h = P.forward_returns(panel, formula.horizon)
        pf_v = M.build_portfolio(
            F.compose_scores(F.Formula(formula.name, formula.components, formula.horizon, args.top_k), prod),
            fwd_h, prod, horizon=formula.horizon, top_k=args.top_k,
            as_of=_span_periods(panel, VALIDATION, formula.horizon))
        acceptance["disclosure_excess_vs_full_eligible"] = M.equal_weight_excess(pf_v, fwd_h, full, formula.horizon)
    search_cost = {
        "components": len(formula.components),
        "free_parameters": formula.free_parameters,
        "n_eff": n_eff_info["n_eff"],
        "t_bonf": t_bonf,
        "variants_tried": len(formula.components) + 1,
        "note": "plan.md R9 requires the TRUE count including self-rejected "
        "variants and swept parameter cells. If you tried more, say so here.",
    }

    print()
    print("=" * 72)
    print(f"ROUND VERDICT: {acceptance['verdict']}")
    print(
        f"  {acceptance['n_failed']} failed / {acceptance['n_decided']} decided "
        f"of {acceptance['n_checks']} checks"
    )
    print("=" * 72)
    for c in acceptance["checks"]:
        mark = {True: "PASS", False: "FAIL", None: "????"}[c["passed"]]
        print(f"  [{mark}] {c['n']:>2}. {c['name']}")
        print(f"          {c['detail']}")
    for note in acceptance.get("disclosures", []):
        print()
        print(f"  [NOTE] {note['name']}")
        print(f"          {note['metric']} = {note['value']} (n={note.get('n_periods')})")
        print(f"          -> {note['reading']}")
    print()

    if args.dry_run:
        log("--dry-run: nothing written")
        return 0

    round_id = args.round_id or (
        f"{datetime.now(timezone.utc):%Y%m%d}-{formula.name}"
    )
    out = write_outputs(
        round_id,
        formula=formula,
        discovery=discovery,
        validation=validation,
        walk_forward_result=wf,
        sweeps=sweeps,
        acceptance=acceptance,
        participation_max=part,
        baseline_net=baseline_net,
        search_cost=search_cost,
    )
    print()
    log(f"artefacts in {out}")
    log("now add one concise entry to history.md pointing at this directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
