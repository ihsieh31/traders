# Round 20260928-long-baseline — long-baseline

Generated 2026-09-27T16:56:38.823314+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.479 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.11310870435448053, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.1621458432071192, production 0.11310870435448053 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 3 with any data: bull_high_vol(n=7), bull_low_vol(n=10), bear_high_vol(n=3) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.01728654998608916, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0164% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 2.5611968433012695** (n = 19 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `adv20` | +1 | -0.0029413266988064066 | -0.37117003141785926 | 1139 | 0.5399473222124671 |
| `r20` | +1 | 0.006997252014693596 | 0.49579525139398206 | 1139 | 0.5618964003511853 |
| `r60` | +1 | 0.015401785883403418 | 0.9225892435085284 | 1139 | 0.6084284460052678 |
| `vol20` | -1 | 0.047480733074317306 | 2.4718837213382576 | 1139 | 0.5856014047410009 |
| `volume_ratio` | +1 | -0.0050909629452239305 | -1.060496683151254 | 1139 | 0.47585601404741 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 19 | 0.0389150023697086 | 2.6667884411325513 | 0.1621458432071192 |
| wide | 1.5x | 19 | 0.038445265527603335 | 2.634618403303675 | 0.16018860636501392 |
| wide | 2.0x | 19 | 0.037975528685498076 | 2.602447025577828 | 0.15823136952290867 |
| production | base | 19 | 0.027146089045075327 | 2.2580455491350606 | 0.11310870435448053 |
| production | 1.5x | 19 | 0.02670266799244374 | 2.220983459408754 | 0.11126111663518226 |
| production | 2.0x | 19 | 0.026259246939812162 | 2.1839247917960947 | 0.10941352891588402 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 3 | 0.07526201936164403 |
| 1 | reselect | 2016-09-30..2017-03-31 | 3 | 0.10106763965049562 |
| 2 | frozen | 2018-01-02..2018-07-02 | 3 | 0.1262239841762136 |
| 2 | reselect | 2018-01-02..2018-07-02 | 3 | 0.22692865191228445 |
| 3 | frozen | 2019-04-04..2019-10-02 | 3 | 0.0911477564856383 |
| 3 | reselect | 2019-04-04..2019-10-02 | 3 | 0.10868179405064941 |
| 4 | frozen | 2020-07-06..2020-12-31 | 3 | 0.33975391052945064 |
| 4 | reselect | 2020-07-06..2020-12-31 | 3 | 1.0842717226539895 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 120 | 0.12794075091419385 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.038406557992786285 | 0.16002732496994287 |
| bull_low_vol | 10 | 0.007078019222306167 | 0.02949174675960903 |
| bear_high_vol | 3 | 0.07151871722555057 | 0.2979946551064607 |
| bear_low_vol | 0 | None | None |

## Diversification

- basis: daily log returns inside each 60-session window
- mean pairwise correlation among picks: **0.3228533694794338**
- median positions: 20.0
- **effective positions: 2.803392208815983**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 22 (≈4/year at this holding period)
- periods with a usable return: 19
- mean picks with a forward return: 17.3 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **2.5611968433012695**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 5,
  "free_parameters": 5,
  "n_eff": 3.744594954560696,
  "t_bonf": 2.478637087023328,
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

