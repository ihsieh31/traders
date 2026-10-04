# Round 20261004-quality-factor/lit-gross-profitability — lit-gross-profitability

Generated 2026-10-04T08:15:36.330911+00:00

## Verdict

**REJECTED** — 3 of 13 decided checks failed (13/13 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | **FAIL** | validation NW t = 0.42, t_bonf = 2.245 |
| 2 | discovery and validation same sign | PASS | discovery net annualised 0.24018359599655356, validation 0.042316206303792306 |
| 3 | validation net annualised > 0 | PASS | 0.042316206303792306 |
| 4 | validation net annualised > 0 at 2.0x cost | PASS | 0.04003620630379231 |
| 5 | beats the production baseline | **FAIL** | validation: formula 0.042316206303792306 vs production score 0.10011091222105678 (net annualised, same period, universe and holding period) |
| 6 | holds in both universes | PASS | wide 0.20371477844692412, production 0.24018359599655356 |
| 7 | walk-forward >= 3/4 folds same sign | PASS | frozen-variant agreement = 3/4 (threshold 3/4; K fixed at 4 before results, process.md S-40) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['horizon']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 4 regimes agree, out of 4 with any data: bull_high_vol(n=27), bull_low_vol(n=64), bear_high_vol(n=23), bear_low_vol(n=9) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 6 periods, mean 0.004659561021592819, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |
| 12 | validation excess over same-universe equal weight is significant | **FAIL** | annualised excess -0.0467818505389509, NW t -1.0376997690564407 (needs >= 2.245), 3-period block bootstrap 95% CI per period [-0.005801866618315848, 0.001196672500314263] (needs low > 0); same-universe equal weight, same as-of grid, zero cost |
| 13 | untouched holdout excess has the validation sign | PASS | holdout mean excess/period -0.013820181552915686 over 16 periods (sign check only; too few periods for a t) |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.7720679161851576** (n = 119 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `gpa` | +1 | 0.01449655055294579 | 2.7344189332622144 | 1199 | 0.5404503753127606 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 119 | 0.008148591137876965 | 1.5970292735513227 | 0.20371477844692412 |
| wide | 1.5x | 119 | 0.008133044919389569 | 1.5937633987603133 | 0.2033261229847392 |
| wide | 2.0x | 119 | 0.008117498700902174 | 1.5904971128680268 | 0.20293746752255434 |
| production | base | 119 | 0.009607343839862142 | 2.0637508095505144 | 0.24018359599655356 |
| production | 1.5x | 119 | 0.009567007705408363 | 2.055066152111625 | 0.23917519263520906 |
| production | 2.0x | 119 | 0.009526671570954581 | 2.0463795819366095 | 0.23816678927386453 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 13 | 0.1419686575306524 |
| 1 | reselect | 2016-09-30..2017-03-31 | 13 | 0.3805558040191076 |
| 2 | frozen | 2018-01-02..2018-07-02 | 13 | 0.33546348957138417 |
| 2 | reselect | 2018-01-02..2018-07-02 | 13 | 0.33546348957138417 |
| 3 | frozen | 2019-04-04..2019-10-02 | 13 | -0.031872936068449605 |
| 3 | reselect | 2019-04-04..2019-10-02 | 13 | -0.031872936068449605 |
| 4 | frozen | 2020-07-06..2020-12-31 | 12 | 0.9357792844302586 |
| 4 | reselect | 2020-07-06..2020-12-31 | 12 | 0.9357792844302586 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `horizon` | **PLATEAU** | 3 | 5 | 0.24607850953691962 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 27 | 0.011847781745485156 | 0.2961945436371289 |
| bull_low_vol | 64 | 0.003384756989932859 | 0.08461892474832147 |
| bear_high_vol | 23 | 0.025799756070273926 | 0.6449939017568481 |
| bear_low_vol | 9 | 0.01140133586664439 | 0.2850333966661098 |

## Diversification

- basis: daily log returns inside each 10-session window
- mean pairwise correlation among picks: **0.23142278889833587**
- median positions: 20.0
- **effective positions: 3.705739809356314**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 125 (≈25/year at this holding period)
- periods with a usable return: 119
- mean picks with a forward return: 19.0 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.7720679161851576**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 1,
  "free_parameters": 2,
  "n_eff": 1.0,
  "t_bonf": 2.2447831150194224,
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

