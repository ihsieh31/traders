# Round 20260928-long-momentum-only — long-momentum-only

Generated 2026-09-27T16:56:50.157525+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.31451951758500113, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.2705491231142049, production 0.31451951758500113 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 3 with any data: bull_high_vol(n=7), bull_low_vol(n=10), bear_high_vol(n=3) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.05703387558933856, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.66375033168176** (n = 19 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r60` | +1 | 0.015401785883403418 | 0.9225892435085284 | 1139 | 0.6084284460052678 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 19 | 0.06493178954740918 | 1.638474195521431 | 0.2705491231142049 |
| wide | 1.5x | 19 | 0.06447521060004076 | 1.6268420705304754 | 0.26864671083350317 |
| wide | 2.0x | 19 | 0.06401863165267233 | 1.6152114974571454 | 0.26674429855280135 |
| production | base | 19 | 0.07548468422040026 | 2.6419672379786197 | 0.31451951758500113 |
| production | 1.5x | 19 | 0.07505573685197921 | 2.6268003258899064 | 0.3127322368832467 |
| production | 2.0x | 19 | 0.07462678948355816 | 2.6116349033271082 | 0.31094495618149237 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 3 | 0.3092619438898382 |
| 1 | reselect | 2016-09-30..2017-03-31 | 3 | 0.12929954347613806 |
| 2 | frozen | 2018-01-02..2018-07-02 | 3 | 0.29099745003385213 |
| 2 | reselect | 2018-01-02..2018-07-02 | 3 | 0.29099745003385213 |
| 3 | frozen | 2019-04-04..2019-10-02 | 3 | 0.0839351022947995 |
| 3 | reselect | 2019-04-04..2019-10-02 | 3 | -0.10811628359005392 |
| 4 | frozen | 2020-07-06..2020-12-31 | 3 | 1.2813663553536296 |
| 4 | reselect | 2020-07-06..2020-12-31 | 3 | 0.42099779479925986 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 120 | 0.32146817418170753 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.1513564838411927 | 0.6306520160049697 |
| bull_low_vol | 10 | 0.020183276544566527 | 0.08409698560236054 |
| bear_high_vol | 3 | 0.1080791105649278 | 0.4503296273538659 |
| bear_low_vol | 0 | None | None |

## Diversification

- basis: daily log returns inside each 60-session window
- mean pairwise correlation among picks: **0.21563594435624403**
- median positions: 20.0
- **effective positions: 3.9238129386092324**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 22 (≈4/year at this holding period)
- periods with a usable return: 19
- mean picks with a forward return: 17.3 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.66375033168176**
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

