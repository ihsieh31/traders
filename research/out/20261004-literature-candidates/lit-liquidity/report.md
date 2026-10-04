# Round 20261004-literature-candidates/lit-liquidity — lit-liquidity

Generated 2026-10-04T08:15:17.911426+00:00

## Verdict

**REJECTED** — 2 of 12 decided checks failed (12/13 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | **FAIL** | validation NW t = 1.741, t_bonf = 2.398 |
| 2 | discovery and validation same sign | PASS | discovery net annualised 0.2339263566315837, validation 0.17153940403580503 |
| 3 | validation net annualised > 0 | PASS | 0.17153940403580503 |
| 4 | validation net annualised > 0 at 2.0x cost | PASS | 0.16881762984225662 |
| 5 | beats the production baseline | PASS | validation: formula 0.17153940403580503 vs production score 0.02776130657779443 (net annualised, same period, universe and holding period) |
| 6 | holds in both universes | PASS | wide 0.2339263566315837, production 0.2339263566315837 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 6 non-overlapping periods, below the floor of 8. A 20-day horizon over 4 folds of 5 years leaves too few test periods to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['lit_adv_window']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=12), bull_low_vol(n=32), bear_high_vol(n=13), bear_low_vol(n=4) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 3 periods, mean 0.011568524127010102, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0006% of ADV20 |
| 12 | validation excess over same-universe equal weight is significant | **FAIL** | annualised excess 0.08384821623260284, NW t 1.2268738494472886 (needs >= 2.398), 3-period block bootstrap 95% CI per period [-0.002273150234219158, 0.01785484747595279] (needs low > 0); same-universe equal weight, same as-of grid, zero cost |
| 13 | untouched holdout excess has the validation sign | PASS | holdout mean excess/period 0.011743449010020347 over 8 periods (sign check only; too few periods for a t) |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.8154955332264369** (n = 59 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `lit_adv` | +1 | 0.003889230094362773 | 0.753076223820461 | 1199 | 0.5170975813177648 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 59 | 0.018714108530526696 | 2.521563572490048 | 0.2339263566315837 |
| wide | 1.5x | 59 | 0.01862004073391653 | 2.5090689509169897 | 0.23275050917395662 |
| wide | 2.0x | 59 | 0.018525972937306356 | 2.4965692156971055 | 0.23157466171632946 |
| production | base | 59 | 0.018714108530526696 | 2.521563572490048 | 0.2339263566315837 |
| production | 1.5x | 59 | 0.01862004073391653 | 2.5090689509169897 | 0.23275050917395662 |
| production | 2.0x | 59 | 0.018525972937306356 | 2.4965692156971055 | 0.23157466171632946 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 7 | 0.20828317539148267 |
| 1 | reselect | 2016-09-30..2017-03-31 | 7 | 0.29138507752268433 |
| 2 | frozen | 2018-01-02..2018-07-02 | 7 | 0.28248555596882285 |
| 2 | reselect | 2018-01-02..2018-07-02 | 7 | 0.28248555596882285 |
| 3 | frozen | 2019-04-04..2019-10-02 | 7 | 0.057843441882987026 |
| 3 | reselect | 2019-04-04..2019-10-02 | 7 | 0.057843441882987026 |
| 4 | frozen | 2020-07-06..2020-12-31 | 6 | 0.4589227327041462 |
| 4 | reselect | 2020-07-06..2020-12-31 | 6 | 0.4589227327041462 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `lit_adv_window` | **PLATEAU** | 3 | 60 | 0.2763176435216715 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 12 | 0.023951678539316054 | 0.29939598174145066 |
| bull_low_vol | 32 | 0.003027066535247049 | 0.03783833169058811 |
| bear_high_vol | 13 | 0.07145160620293813 | 0.8931450775367266 |
| bear_low_vol | 4 | -0.01653038413273565 | -0.20662980165919564 |

## Diversification

- basis: daily log returns inside each 20-session window
- mean pairwise correlation among picks: **0.3649833013567479**
- median positions: 20.0
- **effective positions: 2.5205796742223816**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 62 (≈13/year at this holding period)
- periods with a usable return: 59
- mean picks with a forward return: 19.0 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.8154955332264369**
- block bootstrap crosses zero: **False**
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

