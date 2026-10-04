# Round 20261004-literature-candidates/lit-reversal — lit-reversal

Generated 2026-10-04T08:14:57.874503+00:00

## Verdict

**REJECTED** — 3 of 13 decided checks failed (13/13 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | **FAIL** | validation NW t = 0.594, t_bonf = 2.398 |
| 2 | discovery and validation same sign | PASS | discovery net annualised 0.22824964283632718, validation 0.06073244166782281 |
| 3 | validation net annualised > 0 | PASS | 0.06073244166782281 |
| 4 | validation net annualised > 0 at 2.0x cost | PASS | 0.01329244166782289 |
| 5 | beats the production baseline | PASS | validation: formula 0.06073244166782281 vs production score 0.05483912945113947 (net annualised, same period, universe and holding period) |
| 6 | holds in both universes | PASS | wide 0.4326516739283128, production 0.22824964283632718 |
| 7 | walk-forward >= 3/4 folds same sign | PASS | frozen-variant agreement = 3/4 (threshold 3/4; K fixed at 4 before results, process.md S-40) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['lit_rev_window']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=57), bull_low_vol(n=126), bear_high_vol(n=47), bear_low_vol(n=17) |
| 10 | same sign after dropping best 5% of periods | **FAIL** | dropped 12 periods, mean -0.0018899364665796795, sign_flipped=True |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |
| 12 | validation excess over same-universe equal weight is significant | **FAIL** | annualised excess -0.032397471331644445, NW t -0.2569707168960752 (needs >= 2.398), 3-period block bootstrap 95% CI per period [-0.005496483397019555, 0.003969042308869863] (needs low > 0); same-universe equal weight, same as-of grid, zero cost |
| 13 | untouched holdout excess has the validation sign | PASS | holdout mean excess/period -0.007133233465012201 over 33 periods (sign check only; too few periods for a t) |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 2.5610574233242778** (n = 239 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `lit_rev` | -1 | -0.0162876670365983 | -2.747593424884542 | 1199 | 0.4854045037531276 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 239 | 0.008653033478566255 | 2.6414612736960583 | 0.4326516739283128 |
| wide | 1.5x | 239 | 0.008171966532959561 | 2.4948081039097465 | 0.40859832664797807 |
| wide | 2.0x | 239 | 0.007690899587352867 | 2.348131029661747 | 0.38454497936764337 |
| production | base | 239 | 0.004564992856726543 | 1.5472395330844915 | 0.22824964283632718 |
| production | 1.5x | 239 | 0.004092503316977591 | 1.3872936604799082 | 0.20462516584887955 |
| production | 2.0x | 239 | 0.0036200137772286365 | 1.227301512655628 | 0.18100068886143184 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 26 | 0.09749715172590434 |
| 1 | reselect | 2016-09-30..2017-03-31 | 26 | -0.06979583389331011 |
| 2 | frozen | 2018-01-02..2018-07-02 | 26 | 0.0020373775599234556 |
| 2 | reselect | 2018-01-02..2018-07-02 | 26 | 0.0020373775599234556 |
| 3 | frozen | 2019-04-04..2019-10-02 | 26 | -0.2627568658013888 |
| 3 | reselect | 2019-04-04..2019-10-02 | 26 | -0.2627568658013888 |
| 4 | frozen | 2020-07-06..2020-12-31 | 25 | 0.6998310345851101 |
| 4 | reselect | 2020-07-06..2020-12-31 | 25 | 0.6998310345851101 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `lit_rev_window` | **PLATEAU** | 3 | 5 | 0.22824964283632718 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 57 | 0.005498896405471944 | 0.2749448202735972 |
| bull_low_vol | 126 | -0.000984224166531679 | -0.04921120832658395 |
| bear_high_vol | 47 | 0.020029662912192256 | 1.0014831456096127 |
| bear_low_vol | 17 | 0.005375885649399792 | 0.2687942824699896 |

## Diversification

- basis: daily log returns inside each 5-session window
- mean pairwise correlation among picks: **0.2187026375568256**
- median positions: 20.0
- **effective positions: 3.879464936303372**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 251 (≈50/year at this holding period)
- periods with a usable return: 239
- mean picks with a forward return: 19.0 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **2.5610574233242778**
- block bootstrap crosses zero: **True**
- drop best 5% flips sign: **True**

## Search cost

```
{
  "components": 1,
  "free_parameters": 2,
  "n_eff": 1.0,
  "t_bonf": 2.398014817581415,
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

