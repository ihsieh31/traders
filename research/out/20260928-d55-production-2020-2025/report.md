# Round 20260928-d55-production-2020-2025 — d55-production

Generated 2026-09-27T17:09:07.085627+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.498 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.13671872094408413, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.15058584573352488, production 0.13671872094408413 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=7), bull_low_vol(n=9), bear_high_vol(n=8), bear_low_vol(n=3) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.02342627186659284, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0136% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.8962923019321205** (n = 26 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `adv20` | +1 | 0.01933186481093868 | 3.5227225119168857 | 1453 | 0.6366139022711631 |
| `r20` | +1 | -0.005231095600818852 | -0.4060632845902375 | 1453 | 0.503785271851342 |
| `r60` | +1 | -0.0031674206913780943 | -0.2083693026197624 | 1453 | 0.5216792842395045 |
| `vol20` | -1 | -0.014445097547125375 | -0.7536929013865169 | 1453 | 0.45492085340674465 |
| `volume_ratio` | +1 | 0.003759925131794609 | 1.061842020560529 | 1453 | 0.5464556090846524 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 26 | 0.03312888606137547 | 2.7904599230130938 | 0.15058584573352488 |
| wide | 1.5x | 26 | 0.032638501445990856 | 2.749182622907903 | 0.1483568247545039 |
| wide | 2.0x | 26 | 0.03214811683060624 | 2.7079043372849334 | 0.14612780377548293 |
| production | base | 26 | 0.030078118607698507 | 2.310760651614163 | 0.13671872094408413 |
| production | 1.5x | 26 | 0.029598310915390814 | 2.273729087676946 | 0.1345377768881401 |
| production | 2.0x | 26 | 0.029118503223083122 | 2.2367027079217094 | 0.13235683283219601 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2020-11-23..2021-06-30 | 3 | 0.2679842485529278 |
| 1 | reselect | 2020-11-23..2021-06-30 | 3 | -0.0029476798182653686 |
| 2 | frozen | 2022-05-24..2022-12-28 | 3 | -0.2321061910169645 |
| 2 | reselect | 2022-05-24..2022-12-28 | 3 | -0.2321061910169645 |
| 3 | frozen | 2023-11-22..2024-07-01 | 3 | 0.2219806403892089 |
| 3 | reselect | 2023-11-22..2024-07-01 | 3 | 0.10209978662696385 |
| 4 | frozen | 2025-05-28..2025-12-31 | 3 | 0.2573333445722629 |
| 4 | reselect | 2025-05-28..2025-12-31 | 3 | -0.01756199714831294 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 120 | 0.16864900051555048 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.034856873190855925 | 0.15844033268570876 |
| bull_low_vol | 9 | 0.03335728566653905 | 0.15162402575699568 |
| bear_high_vol | 8 | 0.04918441016399013 | 0.2235655007454097 |
| bear_low_vol | 3 | -0.040766865060021286 | -0.18530393209100587 |

## Diversification

- basis: daily log returns inside each 55-session window
- mean pairwise correlation among picks: **0.32387152100310745**
- median positions: 20.0
- **effective positions: 2.7958111874399667**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 28 (≈5/year at this holding period)
- periods with a usable return: 26
- mean picks with a forward return: 18.6 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.8962923019321205**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 5,
  "free_parameters": 5,
  "n_eff": 3.9580288974968814,
  "t_bonf": 2.4984727925113956,
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

