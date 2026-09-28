# Round 20260928-short-combined — short-combined

Generated 2026-09-27T16:45:14.900088+00:00

## Verdict

**UNDECIDED** — 0 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.064 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.16644311390807404, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.11213261712665165, production 0.16644311390807404 |
| 7 | walk-forward >= 3/4 folds same sign | PASS | frozen-variant agreement = 4/4 (threshold 3/4; K fixed at 4 before results, process.md S-40) |
| 8 | every parameter +/- 1 cell same sign | not measured | no parameter sweep run |
| 9 | same sign in >= 2 regimes | PASS | 4 regimes agree, out of 4 with any data: bull_high_vol(n=36), bull_low_vol(n=58), bear_high_vol(n=25), bear_low_vol(n=4) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 6 periods, mean 0.0013805765996466656, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 2.9012790536977677** (n = 119 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r5_flipped` | -1 | -0.015303030614221318 | -2.0026636716788477 | 1189 | 0.471825063078217 |
| `trend_flipped` | -1 | -0.017595576247070425 | -2.2306660431447316 | 1189 | 0.4819175777964676 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 119 | 0.004485304685066066 | 0.8477163516611568 | 0.11213261712665165 |
| wide | 1.5x | 119 | 0.004045598802713124 | 0.7647746081507677 | 0.10113997006782811 |
| wide | 2.0x | 119 | 0.0036058929203601838 | 0.6817971771601992 | 0.09014732300900459 |
| production | base | 119 | 0.006657724556322961 | 1.3689092491281092 | 0.16644311390807404 |
| production | 1.5x | 119 | 0.006222640522709515 | 1.2795682709482796 | 0.15556601306773787 |
| production | 2.0x | 119 | 0.00578755648909607 | 1.1902099306218132 | 0.14468891222740177 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 13 | 0.21309305410906135 |
| 1 | reselect | 2016-09-30..2017-03-31 | 13 | 0.21309305410906135 |
| 2 | frozen | 2018-01-02..2018-07-02 | 13 | 0.24151174279891346 |
| 2 | reselect | 2018-01-02..2018-07-02 | 13 | 0.24151174279891346 |
| 3 | frozen | 2019-04-04..2019-10-02 | 13 | 0.07007658104881878 |
| 3 | reselect | 2019-04-04..2019-10-02 | 13 | 0.07007658104881878 |
| 4 | frozen | 2020-07-06..2020-12-31 | 13 | 0.7132475241728078 |
| 4 | reselect | 2020-07-06..2020-12-31 | 13 | 0.6968448613296325 |

## Parameter sweeps

None run. Condition 8 stays unmeasured.

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 36 | 0.005745131604625663 | 0.14362829011564157 |
| bull_low_vol | 58 | 0.0035232822651829095 | 0.08808205662957273 |
| bear_high_vol | 25 | 0.010107275851803562 | 0.2526818962950891 |
| bear_low_vol | 4 | 0.04111979398006174 | 1.0279948495015436 |

## Diversification

- basis: daily log returns inside each 10-session window
- mean pairwise correlation among picks: **0.20473948875838485**
- median positions: 20.0
- **effective positions: 4.089937491151177**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 132 (≈25/year at this holding period)
- periods with a usable return: 119
- mean picks with a forward return: 18.0 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **2.9012790536977677**
- block bootstrap crosses zero: **True**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 2,
  "free_parameters": 2,
  "n_eff": 1.2737377501332756,
  "t_bonf": 2.0642176568424144,
  "variants_tried": 3,
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

