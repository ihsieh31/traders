# Round 20260928-short-r20-flip — short-r20-flip

Generated 2026-09-27T16:44:52.080191+00:00

## Verdict

**UNDECIDED** — 0 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.16118884279854886, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.14074457266858165, production 0.16118884279854886 |
| 7 | walk-forward >= 3/4 folds same sign | PASS | frozen-variant agreement = 3/4 (threshold 3/4; K fixed at 4 before results, process.md S-40) |
| 8 | every parameter +/- 1 cell same sign | not measured | no parameter sweep run |
| 9 | same sign in >= 2 regimes | PASS | 4 regimes agree, out of 4 with any data: bull_high_vol(n=36), bull_low_vol(n=58), bear_high_vol(n=25), bear_low_vol(n=4) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 6 periods, mean 0.0006395613919953922, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 2.6910264636565744** (n = 119 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r20_flipped` | -1 | -0.022460359033518357 | -2.878757827437251 | 1189 | 0.46761984861227923 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 119 | 0.005629782906743265 | 1.03742452981817 | 0.14074457266858165 |
| wide | 1.5x | 119 | 0.005282934167247468 | 0.973628407017183 | 0.1320733541811867 |
| wide | 2.0x | 119 | 0.004936085427751669 | 0.909816007279267 | 0.12340213569379173 |
| production | base | 119 | 0.006447553711941954 | 1.486286916381912 | 0.16118884279854886 |
| production | 1.5x | 119 | 0.006116881443034394 | 1.4100324995924896 | 0.15292203607585986 |
| production | 2.0x | 119 | 0.005786209174126829 | 1.3337793686843253 | 0.1446552293531707 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 13 | 0.4750610452184249 |
| 1 | reselect | 2016-09-30..2017-03-31 | 13 | 0.4750610452184249 |
| 2 | frozen | 2018-01-02..2018-07-02 | 13 | 0.2337938921263986 |
| 2 | reselect | 2018-01-02..2018-07-02 | 13 | 0.2337938921263986 |
| 3 | frozen | 2019-04-04..2019-10-02 | 13 | -0.01208293012003252 |
| 3 | reselect | 2019-04-04..2019-10-02 | 13 | -0.01208293012003252 |
| 4 | frozen | 2020-07-06..2020-12-31 | 13 | 0.4044821570946311 |
| 4 | reselect | 2020-07-06..2020-12-31 | 13 | 0.8926816615073397 |

## Parameter sweeps

None run. Condition 8 stays unmeasured.

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 36 | 0.005034303944686448 | 0.1258575986171612 |
| bull_low_vol | 58 | 0.0030088686394818495 | 0.07522171598704623 |
| bear_high_vol | 25 | 0.0107391914949363 | 0.2684797873734075 |
| bear_low_vol | 4 | 0.045070414919630285 | 1.1267603729907572 |

## Diversification

- basis: daily log returns inside each 10-session window
- mean pairwise correlation among picks: **0.1867645600520439**
- median positions: 20.0
- **effective positions: 4.39702822003304**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 132 (≈25/year at this holding period)
- periods with a usable return: 119
- mean picks with a forward return: 18.0 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **2.6910264636565744**
- block bootstrap crosses zero: **True**
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

