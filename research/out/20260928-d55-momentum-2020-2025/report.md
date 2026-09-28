# Round 20260928-d55-momentum-2020-2025 — d55-momentum

Generated 2026-09-27T17:09:25.208400+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.20605563107925165, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.14850404520799207, production 0.20605563107925165 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 2 regimes agree, out of 4 with any data: bull_high_vol(n=7), bull_low_vol(n=9), bear_high_vol(n=8), bear_low_vol(n=3) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.01898148136509303, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 3.945951578376513** (n = 26 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r60` | +1 | -0.0031674206913780943 | -0.2083693026197624 | 1453 | 0.5216792842395045 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 26 | 0.03267088994575825 | 0.8761167396031574 | 0.14850404520799207 |
| wide | 1.5x | 26 | 0.03219781302268134 | 0.8634126610952555 | 0.14635369555764247 |
| wide | 2.0x | 26 | 0.0317247360996044 | 0.8507090958208875 | 0.14420334590729272 |
| production | base | 26 | 0.04533223883743536 | 1.05097877567145 | 0.20605563107925165 |
| production | 1.5x | 26 | 0.04487743114512767 | 1.0404363030467165 | 0.20398832338694398 |
| production | 2.0x | 26 | 0.044422623452819984 | 1.0298937618832007 | 0.2019210156946363 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2020-11-23..2021-06-30 | 3 | 0.16748597686395483 |
| 1 | reselect | 2020-11-23..2021-06-30 | 3 | 0.16748597686395483 |
| 2 | frozen | 2022-05-24..2022-12-28 | 3 | -0.40015772141902634 |
| 2 | reselect | 2022-05-24..2022-12-28 | 3 | -0.40015772141902634 |
| 3 | frozen | 2023-11-22..2024-07-01 | 3 | 0.28611931219856024 |
| 3 | reselect | 2023-11-22..2024-07-01 | 3 | 0.16307099774858455 |
| 4 | frozen | 2025-05-28..2025-12-31 | 3 | 1.0490285469737435 |
| 4 | reselect | 2025-05-28..2025-12-31 | 3 | -0.3478112862508771 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 120 | 0.25805664053795346 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.06843952018436363 | 0.3110887281107438 |
| bull_low_vol | 9 | -0.03852904759216723 | -0.17513203450985104 |
| bear_high_vol | 8 | 0.19256356016191492 | 0.8752889098268861 |
| bear_low_vol | 3 | -0.17757151069173593 | -0.8071432304169815 |

## Diversification

- basis: daily log returns inside each 55-session window
- mean pairwise correlation among picks: **0.21910959443816155**
- median positions: 20.0
- **effective positions: 3.8736550881597847**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 28 (≈5/year at this holding period)
- periods with a usable return: 26
- mean picks with a forward return: 18.6 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **3.945951578376513**
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

