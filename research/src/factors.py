"""The formula contract: components, composition, selection, parameter sweeps.

READ research/plan.md SECTION 5 (R3, R4, R5) FIRST.

A *formula* is a small declarative object: a list of components with signs, a
composition rule, a holding period, and a selection size. Nothing here knows
how to compute a component -- a component is handed in as a finished 2-D
array. That separation is deliberate: the measurement path must be testable
without inventing any factor.

The one rule that is not negotiable, because getting it wrong is silent:

    Cross-sectional percentiles are computed over the ELIGIBLE SET ONLY.

Computing them over the whole panel once produced a 90/100 discrepancy
against the production score and invalidated a whole round of results
without raising anything. ``process.md`` S-12.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

import numpy as np

from research.src.common import percentiles_fast

#: A composite is scaled to this. Purely cosmetic -- selection is by rank --
#: but it keeps the numbers comparable with the production score.
SCORE_SCALE = 100.0


@dataclass
class Component:
    """One ingredient of a composite.

    ``values`` is a 2-D array shaped like the panel. Cells outside the
    eligible set are ignored, not zero-filled (process.md S-23).
    """

    name: str
    values: np.ndarray
    sign: int = 1
    lookback: int = 0

    def __post_init__(self) -> None:
        if self.sign not in (1, -1):
            raise ValueError(f"{self.name}: sign must be +1 or -1")
        if self.values.ndim != 2:
            raise ValueError(
                f"{self.name}: values must be 2-D (time, symbol), got "
                f"{self.values.ndim}-D"
            )

    @property
    def oriented(self) -> np.ndarray:
        """Values with the sign already applied.

        Sign is applied to the VALUE, not to the percentile rank. Flipping
        the value and re-ranking is equivalent for a monotone transform, but
        applying the sign after ranking is what plan.md R3 specifies, and the
        two differ when a component is later residualised.
        """
        return self.values if self.sign > 0 else -self.values


@dataclass
class Formula:
    """A pre-registered selection rule.

    The score is the EQUAL-WEIGHT mean of sign-oriented cross-sectional
    percentiles. Equal weight is not a simplification -- IC-weighting lets
    the few components most likely overfit dominate, which is the opposite
    of what this project wants (plan.md R3).
    """

    name: str
    components: List[Component]
    horizon: int
    top_k: int = 20
    #: Free parameters, hard-capped by plan.md R4.
    free_parameters: int = 0
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError(f"{self.name}: no components")
        if self.horizon < 1:
            raise ValueError(f"{self.name}: horizon must be >= 1")
        if self.free_parameters > MAX_FREE_PARAMETERS:
            raise ValueError(
                f"{self.name}: {self.free_parameters} free parameters exceeds "
                f"the plan.md R4 budget of {MAX_FREE_PARAMETERS}"
            )
        shape = self.components[0].values.shape
        for c in self.components:
            if c.values.shape != shape:
                raise ValueError(
                    f"{c.name} has shape {c.values.shape}, expected {shape}"
                )

    @property
    def n_parameters(self) -> int:
        return len(self.components) + 1  # + the holding period


#: plan.md R4. A formula with more free parameters than this is rejected at
#: construction time, so the budget cannot be quietly exceeded.
MAX_FREE_PARAMETERS = 6


@dataclass
class Parameter:
    """One sweepable parameter and the grid it is swept over.

    The grid MUST be fixed before any result is seen. Expanding it after
    seeing a curve is how a plateau becomes a spike after the fact
    (process.md S-31). ``values`` is therefore part of the contract, not a
    default.
    """

    name: str
    values: List
    build: Callable[[object], List[Component]]

    def __post_init__(self) -> None:
        if len(self.values) < 3:
            raise ValueError(
                f"parameter {self.name}: a sweep needs at least 3 values to "
                "tell a plateau from a spike; got "
                f"{len(self.values)}"
            )
        if len(set(map(str, self.values))) != len(self.values):
            raise ValueError(f"parameter {self.name}: duplicate grid values")


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


def compose_scores(
    formula: Formula, mask: np.ndarray, *, min_n: int = 20
) -> np.ndarray:
    """Composite score per session, ranked within the eligible set.

    Returns a full-panel array with NaN outside the eligible set. Sessions
    whose cross-section is thinner than ``min_n`` are left NaN rather than
    ranked on a handful of names (process.md S-23).
    """
    n_t, n_s = mask.shape
    out = np.full((n_t, n_s), np.nan, dtype=np.float64)
    for t in range(n_t):
        cols = np.flatnonzero(mask[t])
        if cols.size < min_n:
            continue
        acc = np.zeros(cols.size, dtype=np.float64)
        for component in formula.components:
            pct = percentiles_fast(component.values[t][cols])
            acc += component.sign * np.asarray(pct, dtype=np.float64)
        out[t, cols] = SCORE_SCALE * acc / len(formula.components)
    return out


def production_score(
    features, mask: np.ndarray, *, min_n: int = 20
) -> np.ndarray:
    """The main project's existing selection score, rebuilt.

    This is the BASELINE every new formula has to beat (plan.md R10
    condition 5). It is also the calibration anchor for the measurement
    path: if this does not match production's ``score_features`` to 0.0,
    the parent population is wrong and nothing else in the round can be
    trusted.

    ``vol20`` enters inverted, exactly as production does.
    """
    n_t, n_s = mask.shape
    out = np.full((n_t, n_s), np.nan, dtype=np.float64)
    for t in range(n_t):
        cols = np.flatnonzero(mask[t])
        if cols.size < min_n:
            continue
        p_adv = percentiles_fast(features.adv20[t][cols])
        p_r20 = percentiles_fast(features.r20[t][cols])
        p_r60 = percentiles_fast(features.r60[t][cols])
        p_vol = percentiles_fast(features.vol20[t][cols])
        p_vr = percentiles_fast(features.volume_ratio[t][cols])
        out[t, cols] = SCORE_SCALE * (
            0.20 * p_adv
            + 0.25 * p_r20
            + 0.25 * p_r60
            + 0.15 * (1.0 - p_vol)
            + 0.15 * p_vr
        )
    return out


def select_top_k(
    scores: np.ndarray, mask: np.ndarray, top_k: int, *, min_n: int = 20
) -> Dict[int, List[int]]:
    """Top-``k`` symbol indices per session, by score descending.

    Ties break on symbol index so the selection is deterministic under
    input shuffles, matching production's ``(-score, symbol)`` ordering.
    """
    picks: Dict[int, List[int]] = {}
    for t in range(scores.shape[0]):
        cols = np.flatnonzero(mask[t])
        if cols.size < min_n:
            continue
        vals = scores[t][cols]
        order = np.lexsort((cols, -vals))  # primary -vals, tiebreak cols
        picks[t] = [int(j) for j in cols[order][:top_k]]
    return picks


def parameter_report(formula: Formula) -> Dict:
    """The parameter inventory plan.md R4 requires be declared up front."""
    return {
        "n_components": len(formula.components),
        "horizon": formula.horizon,
        "declared_free_parameters": formula.free_parameters,
        "within_budget": formula.free_parameters <= MAX_FREE_PARAMETERS,
        "components": [
            {
                "name": c.name,
                "sign": c.sign,
                "lookback": c.lookback,
            }
            for c in formula.components
        ],
    }
