# Round 20260928-d90-momentum-vol — d90-momentum-vol

Generated 2026-09-27T16:56:24.014955+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.228 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.037715270652253725, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.057557191853137686, production 0.037715270652253725 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 2 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r90_window']; spikes: ['r90_window'] |
| 9 | same sign in >= 2 regimes | PASS | 2 regimes agree, out of 4 with any data: bull_high_vol(n=1), bull_low_vol(n=7), bear_high_vol(n=4), bear_low_vol(n=1) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.0029215172474943343, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0248% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 9.56436902759437** (n = 12 periods)
- **THIS SAMPLE CANNOT DISTINGUISH THE EFFECT FROM ZERO. Any point estimate here must not be cited as a conclusion, including the passing conditions.**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r90` | +1 | 0.01749229922952147 | 1.0263309824166509 | 1079 | 0.593141797961075 |
| `vol20` | -1 | 0.03240562845055103 | 1.6447022627615508 | 1109 | 0.5770964833183048 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 12 | 0.020720589067129567 | 1.931899338409628 | 0.057557191853137686 |
| wide | 1.5x | 12 | 0.0202226724004629 | 1.8853527252745594 | 0.05617409000128583 |
| wide | 2.0x | 12 | 0.019724755733796232 | 1.83881213459484 | 0.05479098814943398 |
| production | base | 12 | 0.013577497434811342 | 0.5067764156162781 | 0.037715270652253725 |
| production | 1.5x | 12 | 0.013087914101478007 | 0.48847586335256155 | 0.03635531694855002 |
| production | 2.0x | 12 | 0.012598330768144678 | 0.47017732116266586 | 0.034995363244846325 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 2 | 0.10483042272712785 |
| 1 | reselect | 2016-09-30..2017-03-31 | 2 | -0.1683914423809565 |
| 2 | frozen | 2018-01-02..2018-07-02 | 2 | -0.031222644572444225 |
| 2 | reselect | 2018-01-02..2018-07-02 | 2 | 0.3072479702901829 |
| 3 | frozen | 2019-04-04..2019-10-02 | 2 | 0.07199579683216958 |
| 3 | reselect | 2019-04-04..2019-10-02 | 2 | 0.17597526835221805 |
| 4 | frozen | 2020-07-06..2020-12-31 | 2 | 0.05017402916982198 |
| 4 | reselect | 2020-07-06..2020-12-31 | 2 | -0.13444442442246649 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r90_window` | **SPIKE** | 1 | 60 | 0.10064478300513281 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 1 | -0.026764706402210393 | -6.691176600552598 |
| bull_low_vol | 7 | -0.018413460696471467 | -0.05114850193464296 |
| bear_high_vol | 4 | 0.0742106037728259 | 0.2061405660356275 |
| bear_low_vol | 1 | 0.09595708917676911 | 23.989272294192276 |

## Diversification

- basis: daily log returns inside each 90-session window
- mean pairwise correlation among picks: **0.3471996289624318**
- median positions: 20.0
- **effective positions: 2.63268989044206**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 14 (≈3/year at this holding period)
- periods with a usable return: 12
- mean picks with a forward return: 17.1 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **9.56436902759437**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 2,
  "free_parameters": 2,
  "n_eff": 1.913700312950999,
  "t_bonf": 2.227625054249076,
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

