# Round 20260928-long-momentum-vol — long-momentum-vol

Generated 2026-09-27T16:57:02.675201+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.202 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.11377849490095945, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.10276911136092454, production 0.11377849490095945 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 3 with any data: bull_high_vol(n=7), bull_low_vol(n=10), bear_high_vol(n=3) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.019989514451746005, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0241% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 2.4329269410317305** (n = 19 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r60` | +1 | 0.015401785883403418 | 0.9225892435085284 | 1139 | 0.6084284460052678 |
| `vol20` | -1 | 0.047480733074317306 | 2.4718837213382576 | 1139 | 0.5856014047410009 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 19 | 0.024664586726621886 | 1.5996257125915347 | 0.10276911136092454 |
| wide | 1.5x | 19 | 0.024172481463463985 | 1.567689230398627 | 0.10071867276443328 |
| wide | 2.0x | 19 | 0.023680376200306098 | 1.5357535547034733 | 0.09866823416794208 |
| production | base | 19 | 0.027306838776230268 | 1.8505733992502715 | 0.11377849490095945 |
| production | 1.5x | 19 | 0.026837101934125 | 1.8190345486258033 | 0.11182125805885418 |
| production | 2.0x | 19 | 0.026367365092019746 | 1.7874849018843155 | 0.10986402121674894 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 3 | 0.2168342274636787 |
| 1 | reselect | 2016-09-30..2017-03-31 | 3 | 0.03262365307933898 |
| 2 | frozen | 2018-01-02..2018-07-02 | 3 | 0.167613934979305 |
| 2 | reselect | 2018-01-02..2018-07-02 | 3 | 0.17291943112883804 |
| 3 | frozen | 2019-04-04..2019-10-02 | 3 | 0.11772055430394944 |
| 3 | reselect | 2019-04-04..2019-10-02 | 3 | 0.08763418159133646 |
| 4 | frozen | 2020-07-06..2020-12-31 | 3 | 0.28581569766121023 |
| 4 | reselect | 2020-07-06..2020-12-31 | 3 | 0.9129377966245451 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 120 | 0.12564367740779894 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.05153007955093438 | 0.21470866479555994 |
| bull_low_vol | 10 | 0.008800393366020395 | 0.03666830569175165 |
| bear_high_vol | 3 | 0.04054850859418827 | 0.16895211914245115 |
| bear_low_vol | 0 | None | None |

## Diversification

- basis: daily log returns inside each 60-session window
- mean pairwise correlation among picks: **0.3317740234757879**
- median positions: 20.0
- **effective positions: 2.738335685827556**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 22 (≈4/year at this holding period)
- periods with a usable return: 19
- mean picks with a forward return: 17.3 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **2.4329269410317305**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 2,
  "free_parameters": 2,
  "n_eff": 1.791548153810757,
  "t_bonf": 2.2017765923857597,
  "variants_tried": 3,
  "note": "plan.md R9 requires the TRUE count including self-rejected variants and swept parameter cells. If you tried more, say so here."
}
```

## Disclosures (plan.md 4.4)

1. Survivorship bias: the universe is today's still-traded US equities. Delisted and acquired names are absent entirely. Direction is optimistic and the share is NOT measurable from this dataset.
2. No point-in-time market cap, so this universe is not the production universe. Results do not extrapolate to the full production pipeline.
3. Split-adjusted only, no dividends. A real bias for 20/40/60-day windows, direction is an overestimate.
4. Costs are estimates, not measured fills. The 2.0x scenario is a bound, not a bid/ask measurement.
5. Capacity is not evaluated. The 0.1% participation cap is an early warning, not a capacity study.
6. This project does no position-level risk management. Passing all eleven conditions does NOT make a formula deployable.

