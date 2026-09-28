# Round 20260928-d55-production-2018-2023 — d55-production

Generated 2026-09-27T17:08:16.471984+00:00

## Verdict

**UNDECIDED** — 0 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.492 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.08996613901763117, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.044486187511509366, production 0.08996613901763117 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['r60_window']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=9), bull_low_vol(n=10), bear_high_vol(n=6), bear_low_vol(n=2) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.012596919921602221, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0148% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 2.781458380404316** (n = 26 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `adv20` | +1 | 0.017007715475925583 | 2.8618589622118735 | 1454 | 0.6375515818431912 |
| `r20` | +1 | -0.010995197669837855 | -0.8792588369874541 | 1454 | 0.5055020632737276 |
| `r60` | +1 | -0.0040821085634899406 | -0.2805324248604874 | 1454 | 0.5055020632737276 |
| `vol20` | -1 | -0.02116139308299789 | -1.1374052331492754 | 1454 | 0.4325997248968363 |
| `volume_ratio` | +1 | 0.004083800496152094 | 1.1656496798243912 | 1454 | 0.5392022008253095 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 26 | 0.00978696125253206 | 0.7839245760917521 | 0.044486187511509366 |
| wide | 1.5x | 26 | 0.009302345867916676 | 0.7451886964798318 | 0.04228339030871217 |
| wide | 2.0x | 26 | 0.008817730483301294 | 0.7064443079204462 | 0.040080593105914976 |
| production | base | 26 | 0.019792550583878857 | 1.3971109675594486 | 0.08996613901763117 |
| production | 1.5x | 26 | 0.019326204430032707 | 1.3642045221590238 | 0.08784638377287594 |
| production | 2.0x | 26 | 0.018859858276186556 | 1.3312972817914854 | 0.08572662852812071 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2018-11-23..2019-07-02 | 3 | 0.26451185343564565 |
| 1 | reselect | 2018-11-23..2019-07-02 | 3 | 0.28158953347827786 |
| 2 | frozen | 2020-05-27..2020-12-29 | 3 | 0.45501614272892843 |
| 2 | reselect | 2020-05-27..2020-12-29 | 3 | 0.3664226963501527 |
| 3 | frozen | 2021-11-22..2022-06-29 | 3 | -0.2632278635033738 |
| 3 | reselect | 2021-11-22..2022-06-29 | 3 | -0.3448423016846348 |
| 4 | frozen | 2023-05-24..2023-12-29 | 3 | 0.030426292056259657 |
| 4 | reselect | 2023-05-24..2023-12-29 | 3 | -0.3200150981565947 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **PLATEAU** | 3 | 60 | 0.08996613901763117 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 9 | 0.03480299065609402 | 0.15819541207315466 |
| bull_low_vol | 10 | 0.013255435456850215 | 0.06025197934931917 |
| bear_high_vol | 6 | 0.024708001496416346 | 0.1123090977109834 |
| bear_low_vol | 2 | -0.033083764407072884 | -0.15038074730487677 |

## Diversification

- basis: daily log returns inside each 55-session window
- mean pairwise correlation among picks: **0.34174603476745047**
- median positions: 20.0
- **effective positions: 2.6690956645134123**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 28 (≈5/year at this holding period)
- periods with a usable return: 26
- mean picks with a forward return: 18.6 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **2.781458380404316**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 5,
  "free_parameters": 5,
  "n_eff": 3.884143732663933,
  "t_bonf": 2.4917439756029776,
  "variants_tried": 6,
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

