# Round 20260928-d90-momentum — d90-momentum

Generated 2026-09-27T16:55:31.075960+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.14913468537427313, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.03862433398038386, production 0.14913468537427313 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 2 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r90_window']; spikes: ['r90_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=1), bull_low_vol(n=7), bear_high_vol(n=4), bear_low_vol(n=1) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.024445549318768203, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 3.409338267743712** (n = 12 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r90` | +1 | 0.01749229922952147 | 1.0263309824166509 | 1079 | 0.593141797961075 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 12 | 0.013904760232938192 | 0.7754971850139578 | 0.03862433398038386 |
| wide | 1.5x | 12 | 0.013404760232938191 | 0.7476111527479383 | 0.03723544509149498 |
| wide | 2.0x | 12 | 0.012904760232938193 | 0.7197251204819188 | 0.035846556202606086 |
| production | base | 12 | 0.05368848673473833 | 1.3907888604353627 | 0.14913468537427313 |
| production | 1.5x | 12 | 0.053217653401404996 | 1.3785581929242308 | 0.14782681500390277 |
| production | 2.0x | 12 | 0.052746820068071666 | 1.3663280861445932 | 0.1465189446335324 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 2 | 0.12323078425286287 |
| 1 | reselect | 2016-09-30..2017-03-31 | 2 | -0.07712375792875158 |
| 2 | frozen | 2018-01-02..2018-07-02 | 2 | 0.20271735777411476 |
| 2 | reselect | 2018-01-02..2018-07-02 | 2 | 0.20271735777411476 |
| 3 | frozen | 2019-04-04..2019-10-02 | 2 | -0.0694546394605161 |
| 3 | reselect | 2019-04-04..2019-10-02 | 2 | 0.09400404343729346 |
| 4 | frozen | 2020-07-06..2020-12-31 | 2 | 0.34293161290969576 |
| 4 | reselect | 2020-07-06..2020-12-31 | 2 | 0.34293161290969576 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r90_window` | **SPIKE** | 1 | 120 | 0.20608293885095313 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 1 | -0.017482263551903986 | -4.370565887975997 |
| bull_low_vol | 7 | 0.029657936672185064 | 0.08238315742273629 |
| bear_high_vol | 4 | 0.13179607641877022 | 0.36610021227436174 |
| bear_low_vol | 1 | 0.058750318407157796 | 14.68757960178945 |

## Diversification

- basis: daily log returns inside each 90-session window
- mean pairwise correlation among picks: **0.19706260179401103**
- median positions: 20.0
- **effective positions: 4.215683264311357**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 14 (≈3/year at this holding period)
- periods with a usable return: 12
- mean picks with a forward return: 17.1 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **3.409338267743712**
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

