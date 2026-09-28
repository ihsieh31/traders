# Round 20260928-short-trend — short-trend

Generated 2026-09-27T16:45:03.298661+00:00

## Verdict

**UNDECIDED** — 0 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.18790841015129217, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.15236909962450582, production 0.18790841015129217 |
| 7 | walk-forward >= 3/4 folds same sign | PASS | frozen-variant agreement = 3/4 (threshold 3/4; K fixed at 4 before results, process.md S-40) |
| 8 | every parameter +/- 1 cell same sign | not measured | no parameter sweep run |
| 9 | same sign in >= 2 regimes | PASS | 4 regimes agree, out of 4 with any data: bull_high_vol(n=36), bull_low_vol(n=58), bear_high_vol(n=25), bear_low_vol(n=4) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 6 periods, mean 0.0014618021754296462, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0250% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 2.5506909119915093** (n = 119 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `trend_flipped` | -1 | -0.017595576247070425 | -2.2306660431447316 | 1189 | 0.4819175777964676 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 119 | 0.006094763984980232 | 1.1232430515165948 | 0.15236909962450582 |
| wide | 1.5x | 119 | 0.005695184153047457 | 1.0497540505875402 | 0.14237960382618645 |
| wide | 2.0x | 119 | 0.005295604321114684 | 0.9762429728927999 | 0.1323901080278671 |
| production | base | 119 | 0.007516336406051687 | 1.5909280441479747 | 0.18790841015129217 |
| production | 1.5x | 119 | 0.007124529683362612 | 1.5078918821755074 | 0.17811324208406532 |
| production | 2.0x | 119 | 0.0067327229606735355 | 1.4248658602419038 | 0.16831807401683838 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 13 | 0.46140817150452557 |
| 1 | reselect | 2016-09-30..2017-03-31 | 13 | 0.46140817150452557 |
| 2 | frozen | 2018-01-02..2018-07-02 | 13 | 0.113163525320998 |
| 2 | reselect | 2018-01-02..2018-07-02 | 13 | 0.113163525320998 |
| 3 | frozen | 2019-04-04..2019-10-02 | 13 | -0.01955768827462677 |
| 3 | reselect | 2019-04-04..2019-10-02 | 13 | -0.01955768827462677 |
| 4 | frozen | 2020-07-06..2020-12-31 | 13 | 0.6837791127127677 |
| 4 | reselect | 2020-07-06..2020-12-31 | 13 | 0.9536941968450589 |

## Parameter sweeps

None run. Condition 8 stays unmeasured.

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 36 | 0.006752812821270743 | 0.16882032053176857 |
| bull_low_vol | 58 | 0.0043783813148995 | 0.10945953287248751 |
| bear_high_vol | 25 | 0.010952635805989163 | 0.2738158951497291 |
| bear_low_vol | 4 | 0.04079786989493556 | 1.019946747373389 |

## Diversification

- basis: daily log returns inside each 10-session window
- mean pairwise correlation among picks: **0.186707777537875**
- median positions: 20.0
- **effective positions: 4.398071401233457**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 132 (≈25/year at this holding period)
- periods with a usable return: 119
- mean picks with a forward return: 18.0 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **2.5506909119915093**
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

