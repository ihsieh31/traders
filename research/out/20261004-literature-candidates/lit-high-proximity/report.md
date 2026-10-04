# Round 20261004-literature-candidates/lit-high-proximity — lit-high-proximity

Generated 2026-10-04T08:14:25.672459+00:00

## Verdict

**REJECTED** — 4 of 12 decided checks failed (12/13 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | **FAIL** | validation NW t = 0.428, t_bonf = 2.398 |
| 2 | discovery and validation same sign | PASS | discovery net annualised 0.11931195899563664, validation 0.023343444691437773 |
| 3 | validation net annualised > 0 | PASS | 0.023343444691437773 |
| 4 | validation net annualised > 0 at 2.0x cost | PASS | 0.011982557594663578 |
| 5 | beats the production baseline | **FAIL** | validation: formula 0.023343444691437773 vs production score 0.02776130657779443 (net annualised, same period, universe and holding period) |
| 6 | holds in both universes | PASS | wide 0.12901409468726255, production 0.11931195899563664 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 6 non-overlapping periods, below the floor of 8. A 20-day horizon over 4 folds of 5 years leaves too few test periods to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['lit_hi_window']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=12), bull_low_vol(n=32), bear_high_vol(n=13), bear_low_vol(n=4) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 3 periods, mean 0.004532182211479405, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |
| 12 | validation excess over same-universe equal weight is significant | **FAIL** | annualised excess -0.06434774311176447, NW t -0.9063186439567549 (needs >= 2.398), 3-period block bootstrap 95% CI per period [-0.01727422897843341, 0.005118652759714976] (needs low > 0); same-universe equal weight, same as-of grid, zero cost |
| 13 | untouched holdout excess has the validation sign | **FAIL** | holdout mean excess/period 0.0036360905164880726 over 8 periods (sign check only; too few periods for a t) |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 3.402337179603973** (n = 56 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `lit_hi` | +1 | -0.01459355132837661 | -1.2337354813728107 | 1140 | 0.49473684210526314 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 56 | 0.010321127574981003 | 1.1881056869959838 | 0.12901409468726255 |
| wide | 1.5x | 56 | 0.009865324003552433 | 1.135617129254563 | 0.1233165500444054 |
| wide | 2.0x | 56 | 0.009409520432123859 | 1.0831298779935974 | 0.11761900540154824 |
| production | base | 56 | 0.009544956719650931 | 1.2827613051894171 | 0.11931195899563664 |
| production | 1.5x | 56 | 0.00908870671965093 | 1.221495833960616 | 0.11360883399563662 |
| production | 2.0x | 56 | 0.008632456719650931 | 1.1602244387220304 | 0.10790570899563663 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 7 | 0.1513760672986487 |
| 1 | reselect | 2016-09-30..2017-03-31 | 7 | 0.5481893289892763 |
| 2 | frozen | 2018-01-02..2018-07-02 | 7 | 0.15963830548312877 |
| 2 | reselect | 2018-01-02..2018-07-02 | 7 | 0.15963830548312877 |
| 3 | frozen | 2019-04-04..2019-10-02 | 7 | -0.1537875260763472 |
| 3 | reselect | 2019-04-04..2019-10-02 | 7 | -0.16678564633473708 |
| 4 | frozen | 2020-07-06..2020-12-31 | 6 | 0.1942525255573258 |
| 4 | reselect | 2020-07-06..2020-12-31 | 6 | 1.6813306264357644 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `lit_hi_window` | **PLATEAU** | 3 | 120 | 0.11931195899563664 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 12 | 0.00924984240431174 | 0.11562303005389675 |
| bull_low_vol | 32 | -0.0011755755971945558 | -0.014694694964931948 |
| bear_high_vol | 13 | 0.04010394647889005 | 0.5012993309861256 |
| bear_low_vol | 4 | 0.009329646125643857 | 0.11662057657054821 |

## Diversification

- basis: daily log returns inside each 20-session window
- mean pairwise correlation among picks: **0.2829903162370214**
- median positions: 20.0
- **effective positions: 3.136361465240685**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 62 (≈13/year at this holding period)
- periods with a usable return: 56
- mean picks with a forward return: 18.1 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **3.402337179603973**
- block bootstrap crosses zero: **True**
- drop best 5% flips sign: **False**

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

