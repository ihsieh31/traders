# Round 20260928-d50-momentum — d50-momentum

Generated 2026-09-27T16:59:09.113306+00:00

## Verdict

**UNDECIDED** — 0 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.28995730640102824, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.24285609327142826, production 0.28995730640102824 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['r60_window']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 3 with any data: bull_high_vol(n=7), bull_low_vol(n=13), bear_high_vol(n=4) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.05236324778922778, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0249% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.137951796421219** (n = 23 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r60` | +1 | 0.014161113402653322 | 0.911084062957272 | 1149 | 0.6161879895561357 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 23 | 0.04857121865428565 | 2.03362086300778 | 0.24285609327142826 |
| wide | 1.5x | 23 | 0.048136436045589995 | 2.0153472328045154 | 0.24068218022794996 |
| wide | 2.0x | 23 | 0.047701653436894344 | 1.997074581008253 | 0.2385082671844717 |
| production | base | 23 | 0.057991461280205645 | 3.588049540435561 | 0.28995730640102824 |
| production | 1.5x | 23 | 0.057578417801944774 | 3.5628498095556878 | 0.28789208900972385 |
| production | 2.0x | 23 | 0.05716537432368391 | 3.53764400609512 | 0.2858268716184196 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 3 | 0.3373656092592635 |
| 1 | reselect | 2016-09-30..2017-03-31 | 3 | 0.25072804511458097 |
| 2 | frozen | 2018-01-02..2018-07-02 | 3 | 0.2546159393393681 |
| 2 | reselect | 2018-01-02..2018-07-02 | 3 | 0.2546159393393681 |
| 3 | frozen | 2019-04-04..2019-10-02 | 3 | -0.004994829927524163 |
| 3 | reselect | 2019-04-04..2019-10-02 | 3 | -0.21978109924526948 |
| 4 | frozen | 2020-07-06..2020-12-31 | 3 | 0.682253511953985 |
| 4 | reselect | 2020-07-06..2020-12-31 | 3 | 0.4711777146431811 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **PLATEAU** | 3 | 60 | 0.28995730640102824 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.08608445322832871 | 0.43042226614164353 |
| bull_low_vol | 13 | 0.03570056742491383 | 0.17850283712456916 |
| bear_high_vol | 4 | 0.08903502010751636 | 0.4451751005375818 |
| bear_low_vol | 0 | None | None |

## Diversification

- basis: daily log returns inside each 50-session window
- mean pairwise correlation among picks: **0.2312799896080554**
- median positions: 20.0
- **effective positions: 3.707603689075737**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 26 (≈5/year at this holding period)
- periods with a usable return: 23
- mean picks with a forward return: 17.7 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.137951796421219**
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

