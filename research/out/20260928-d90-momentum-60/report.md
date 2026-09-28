# Round 20260928-d90-momentum-60 — d90-momentum60

Generated 2026-09-27T16:55:45.083434+00:00

## Verdict

**REJECTED** — 2 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.08280916622681543, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.11347083070392994, production 0.08280916622681543 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 2 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=1), bull_low_vol(n=7), bear_high_vol(n=4), bear_low_vol(n=1) |
| 10 | same sign after dropping best 5% of periods | **FAIL** | dropped 1 periods, mean -0.003093446529275812, sign_flipped=True |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 7.6207942771206705** (n = 12 periods)
- **THIS SAMPLE CANNOT DISTINGUISH THE EFFECT FROM ZERO. Any point estimate here must not be cited as a conclusion, including the passing conditions.**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r60` | +1 | 0.01770974267788808 | 0.9540825792040716 | 1109 | 0.5996393146979261 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 12 | 0.04084949905341478 | 0.7406810751899934 | 0.11347083070392994 |
| wide | 1.5x | 12 | 0.04035991572008144 | 0.7318137291029698 | 0.11211087700022622 |
| wide | 2.0x | 12 | 0.0398703323867481 | 0.7229461440060712 | 0.1107509232965225 |
| production | base | 12 | 0.029811299841653555 | 0.5870131668750993 | 0.08280916622681543 |
| production | 1.5x | 12 | 0.02933004984165356 | 0.5775198772148692 | 0.08147236067125989 |
| production | 2.0x | 12 | 0.028848799841653547 | 0.5680271331000326 | 0.08013555511570429 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 2 | 0.11424417572781347 |
| 1 | reselect | 2016-09-30..2017-03-31 | 2 | -0.12011513244262637 |
| 2 | frozen | 2018-01-02..2018-07-02 | 2 | 0.2603233168016793 |
| 2 | reselect | 2018-01-02..2018-07-02 | 2 | 0.2603233168016793 |
| 3 | frozen | 2019-04-04..2019-10-02 | 2 | 0.05060927121035363 |
| 3 | reselect | 2019-04-04..2019-10-02 | 2 | 0.1472723568878488 |
| 4 | frozen | 2020-07-06..2020-12-31 | 2 | 0.1668993680953702 |
| 4 | reselect | 2020-07-06..2020-12-31 | 2 | -0.461930418573542 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 120 | 0.20608293885095313 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 1 | -0.021994291614162477 | -5.4985729035406194 |
| bull_low_vol | 7 | 0.012565411541611797 | 0.03490392094892166 |
| bear_high_vol | 4 | 0.07665798723935437 | 0.212938853442651 |
| bear_low_vol | 1 | 0.061798047204659465 | 15.449511801164865 |

## Diversification

- basis: daily log returns inside each 90-session window
- mean pairwise correlation among picks: **0.24663672610283327**
- median positions: 20.0
- **effective positions: 3.517350688943794**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 14 (≈3/year at this holding period)
- periods with a usable return: 12
- mean picks with a forward return: 17.1 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **7.6207942771206705**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **True**

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

