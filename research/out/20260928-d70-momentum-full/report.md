# Round 20260928-d70-momentum-full — d70-momentum

Generated 2026-09-27T17:00:22.213142+00:00

## Verdict

**REJECTED** — 2 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.08344370576513004, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | **FAIL** | wide -0.053609779881346256, production 0.08344370576513004 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 2 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 2 regimes agree, out of 4 with any data: bull_high_vol(n=5), bull_low_vol(n=7), bear_high_vol(n=2), bear_low_vol(n=2) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.006303213774679659, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0244% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 7.305724529912033** (n = 16 periods)
- **THIS SAMPLE CANNOT DISTINGUISH THE EFFECT FROM ZERO. Any point estimate here must not be cited as a conclusion, including the passing conditions.**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r60` | +1 | 0.012614717604635495 | 0.7750572533692752 | 1129 | 0.6102745792736936 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 16 | -0.01501073836677695 | -0.36965403541571246 | -0.053609779881346256 |
| wide | 1.5x | 16 | -0.015491988366776949 | -0.3815055515523808 | -0.05532852988134625 |
| wide | 2.0x | 16 | -0.01597323836677695 | -0.39335708109293366 | -0.05704727988134625 |
| production | base | 16 | 0.023364237614236408 | 0.6426534339090474 | 0.08344370576513004 |
| production | 1.5x | 16 | 0.022909550114236413 | 0.6301162980787862 | 0.08181982183655861 |
| production | 2.0x | 16 | 0.02245486261423641 | 0.6175803461274963 | 0.08019593790798718 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 2 | 0.008390201613936396 |
| 1 | reselect | 2016-09-30..2017-03-31 | 2 | -0.05133310267881246 |
| 2 | frozen | 2018-01-02..2018-07-02 | 2 | 0.10508297267086755 |
| 2 | reselect | 2018-01-02..2018-07-02 | 2 | 0.10508297267086755 |
| 3 | frozen | 2019-04-04..2019-10-02 | 2 | -0.1395206187395884 |
| 3 | reselect | 2019-04-04..2019-10-02 | 2 | -0.19607777056548248 |
| 4 | frozen | 2020-07-06..2020-12-31 | 2 | 0.9378189188154387 |
| 4 | reselect | 2020-07-06..2020-12-31 | 2 | -0.5334216355916626 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 120 | 0.1581255344538234 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 5 | 0.0533041160888458 | 0.19037184317444927 |
| bull_low_vol | 7 | -0.01311103012629399 | -0.04682510759390711 |
| bear_high_vol | 2 | -0.004024357590807784 | -0.014372705681456372 |
| bear_low_vol | 2 | 0.10356657372461349 | 0.3698806204450482 |

## Diversification

- basis: daily log returns inside each 70-session window
- mean pairwise correlation among picks: **0.22000390435187142**
- median positions: 20.0
- **effective positions: 3.8609485684298064**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 18 (≈4/year at this holding period)
- periods with a usable return: 16
- mean picks with a forward return: 17.8 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **7.305724529912033**
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

