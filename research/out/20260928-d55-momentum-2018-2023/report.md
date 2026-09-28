# Round 20260928-d55-momentum-2018-2023 — d55-momentum

Generated 2026-09-27T17:08:32.981647+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.12361913401674125, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.06539799873528461, production 0.12361913401674125 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 2/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=9), bull_low_vol(n=10), bear_high_vol(n=6), bear_low_vol(n=2) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.010762242378356617, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0249% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 4.573161532235295** (n = 26 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r60` | +1 | -0.0040821085634899406 | -0.2805324248604874 | 1454 | 0.5055020632737276 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 26 | 0.014387559721762614 | 0.4960814597207497 | 0.06539799873528461 |
| wide | 1.5x | 26 | 0.013916405875608774 | 0.479837825597035 | 0.06325639034367625 |
| wide | 2.0x | 26 | 0.013445252029454919 | 0.4635940645647426 | 0.06111478195206782 |
| production | base | 26 | 0.02719620948368307 | 0.9888955909148497 | 0.12361913401674125 |
| production | 1.5x | 26 | 0.026735632560606147 | 0.972154469717631 | 0.12152560254820977 |
| production | 2.0x | 26 | 0.026275055637529222 | 0.9554130905286939 | 0.11943207107967829 |

## Walk-forward

2/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2018-11-23..2019-07-02 | 3 | 0.1149280576467724 |
| 1 | reselect | 2018-11-23..2019-07-02 | 3 | 0.1149280576467724 |
| 2 | frozen | 2020-05-27..2020-12-29 | 3 | 1.6841830996474334 |
| 2 | reselect | 2020-05-27..2020-12-29 | 3 | 0.6853178265726748 |
| 3 | frozen | 2021-11-22..2022-06-29 | 3 | -0.382873163670867 |
| 3 | reselect | 2021-11-22..2022-06-29 | 3 | -0.8461150175241844 |
| 4 | frozen | 2023-05-24..2023-12-29 | 3 | -0.21860343979671287 |
| 4 | reselect | 2023-05-24..2023-12-29 | 3 | 0.011330963973446575 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 60 | 0.12361913401674125 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 9 | 0.07060293402919968 | 0.3209224274054531 |
| bull_low_vol | 10 | -0.03185741536448642 | -0.1448064334749383 |
| bear_high_vol | 6 | 0.05954950494479836 | 0.27067956793090164 |
| bear_low_vol | 2 | 0.0005473744622751381 | 0.002488065737614264 |

## Diversification

- basis: daily log returns inside each 55-session window
- mean pairwise correlation among picks: **0.22358652263384918**
- median positions: 20.0
- **effective positions: 3.8108710939708583**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 28 (≈5/year at this holding period)
- periods with a usable return: 26
- mean picks with a forward return: 18.6 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **4.573161532235295**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 1,
  "free_parameters": 1,
  "n_eff": 1.0,
  "t_bonf": 1.9623390808264358,
  "variants_tried": 2,
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

