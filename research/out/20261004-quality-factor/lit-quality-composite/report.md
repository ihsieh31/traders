# Round 20261004-quality-factor/lit-quality-composite — lit-quality-composite

Generated 2026-10-04T08:15:57.023097+00:00

## Verdict

**REJECTED** — 4 of 13 decided checks failed (13/13 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | **FAIL** | validation NW t = 0.429, t_bonf = 2.328 |
| 2 | discovery and validation same sign | PASS | discovery net annualised 0.19818422671246588, validation 0.03969549201092555 |
| 3 | validation net annualised > 0 | PASS | 0.03969549201092555 |
| 4 | validation net annualised > 0 at 2.0x cost | PASS | 0.037775492010925574 |
| 5 | beats the production baseline | **FAIL** | validation: formula 0.03969549201092555 vs production score 0.07973373549155952 (net annualised, same period, universe and holding period) |
| 6 | holds in both universes | PASS | wide 0.24750355275406677, production 0.19818422671246588 |
| 7 | walk-forward >= 3/4 folds same sign | PASS | frozen-variant agreement = 3/4 (threshold 3/4; K fixed at 4 before results, process.md S-40) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['horizon']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 4 regimes agree, out of 4 with any data: bull_high_vol(n=27), bull_low_vol(n=64), bear_high_vol(n=23), bear_low_vol(n=9) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 6 periods, mean 0.0037000366896756316, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |
| 12 | validation excess over same-universe equal weight is significant | **FAIL** | annualised excess -0.051743423136289726, NW t -1.1866736301166945 (needs >= 2.328), 3-period block bootstrap 95% CI per period [-0.005394403784682557, 0.001182925002102864] (needs low > 0); same-universe equal weight, same as-of grid, zero cost |
| 13 | untouched holdout excess has the validation sign | **FAIL** | holdout mean excess/period 0.0035664587058285672 over 16 periods (sign check only; too few periods for a t) |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.9278235813433808** (n = 119 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `gpa` | +1 | 0.01076830159544356 | 1.8960409797082587 | 1199 | 0.53628023352794 |
| `cfoa` | +1 | 0.0009386704728508546 | 0.18554722385982325 | 1199 | 0.5162635529608006 |
| `lev` | -1 | -0.005308507239520619 | -1.1748192008895557 | 1199 | 0.4920767306088407 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 119 | 0.009900142110162671 | 2.224937019987076 | 0.24750355275406677 |
| wide | 1.5x | 119 | 0.009875982446297128 | 2.219295313917731 | 0.2468995611574282 |
| wide | 2.0x | 119 | 0.009851822782431581 | 2.213651716482908 | 0.24629556956078952 |
| production | base | 119 | 0.007927369068498636 | 1.9408048311780892 | 0.19818422671246588 |
| production | 1.5x | 119 | 0.007886612765977628 | 1.9305859617910661 | 0.1971653191494407 |
| production | 2.0x | 119 | 0.007845856463456617 | 1.9203668725850567 | 0.1961464115864154 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 13 | 0.14576038020875356 |
| 1 | reselect | 2016-09-30..2017-03-31 | 13 | 0.07215573498186799 |
| 2 | frozen | 2018-01-02..2018-07-02 | 13 | 0.2531294740238258 |
| 2 | reselect | 2018-01-02..2018-07-02 | 13 | 0.2531294740238258 |
| 3 | frozen | 2019-04-04..2019-10-02 | 13 | -0.2045608452261222 |
| 3 | reselect | 2019-04-04..2019-10-02 | 13 | 0.2389807596989128 |
| 4 | frozen | 2020-07-06..2020-12-31 | 12 | 0.4061129758343955 |
| 4 | reselect | 2020-07-06..2020-12-31 | 12 | 1.2924435822231672 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `horizon` | **PLATEAU** | 3 | 20 | 0.21568935628865593 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 27 | 0.005242718507196329 | 0.13106796267990822 |
| bull_low_vol | 64 | 0.0035668295073665907 | 0.08917073768416477 |
| bear_high_vol | 23 | 0.023209429134229687 | 0.5802357283557422 |
| bear_low_vol | 9 | 0.012731174089797515 | 0.31827935224493786 |

## Diversification

- basis: daily log returns inside each 10-session window
- mean pairwise correlation among picks: **0.2817307274877629**
- median positions: 20.0
- **effective positions: 3.1481765698119637**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 125 (≈25/year at this holding period)
- periods with a usable return: 119
- mean picks with a forward return: 19.0 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.9278235813433808**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 3,
  "free_parameters": 4,
  "n_eff": 2.488050639588938,
  "t_bonf": 2.3282766849489525,
  "variants_tried": 4,
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

