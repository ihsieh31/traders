# Round 20260928-d70-production — d70-production

Generated 2026-09-27T16:59:48.207385+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.487 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.04933615747759679, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.05049406925286875, production 0.04933615747759679 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 2 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=5), bull_low_vol(n=7), bear_high_vol(n=2), bear_low_vol(n=2) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.004862037454871014, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0158% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 6.199107787819718** (n = 16 periods)
- **THIS SAMPLE CANNOT DISTINGUISH THE EFFECT FROM ZERO. Any point estimate here must not be cited as a conclusion, including the passing conditions.**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `adv20` | +1 | -0.002357820700713694 | -0.2798859792226359 | 1129 | 0.5217006200177148 |
| `r20` | +1 | 0.009252044949965706 | 0.6313969888508069 | 1129 | 0.5713020372010629 |
| `r60` | +1 | 0.018561518300119265 | 1.0443474633385885 | 1129 | 0.6191319751992914 |
| `vol20` | -1 | 0.05110730402185943 | 2.5488854382249224 | 1129 | 0.6164747564216121 |
| `volume_ratio` | +1 | -0.007684793734671555 | -1.6704172745934884 | 1129 | 0.47032772364924713 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 16 | 0.014138339390803249 | 0.6804239384900928 | 0.05049406925286875 |
| wide | 1.5x | 16 | 0.013677401890803253 | 0.658234303080632 | 0.04884786389572591 |
| wide | 2.0x | 16 | 0.013216464390803253 | 0.6360449631338003 | 0.04720165853858305 |
| production | base | 16 | 0.0138141240937271 | 0.7509276499160065 | 0.04933615747759679 |
| production | 1.5x | 16 | 0.013354749093727102 | 0.7259545308385731 | 0.047695532477596794 |
| production | 2.0x | 16 | 0.0128953740937271 | 0.7009813821738976 | 0.04605490747759679 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 2 | 0.1308299988026654 |
| 1 | reselect | 2016-09-30..2017-03-31 | 2 | 0.26939095814306757 |
| 2 | frozen | 2018-01-02..2018-07-02 | 2 | 0.022854688502292424 |
| 2 | reselect | 2018-01-02..2018-07-02 | 2 | -0.016400845483082564 |
| 3 | frozen | 2019-04-04..2019-10-02 | 2 | 0.12234086712339264 |
| 3 | reselect | 2019-04-04..2019-10-02 | 2 | 0.043191408141559494 |
| 4 | frozen | 2020-07-06..2020-12-31 | 2 | 0.5493912904906577 |
| 4 | reselect | 2020-07-06..2020-12-31 | 2 | 0.060598367024721655 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 2 | 90 | 0.07194127183716775 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 5 | 0.020545961851794768 | 0.07337843518498131 |
| bull_low_vol | 7 | -0.006842285471052724 | -0.024436733825188302 |
| bear_high_vol | 2 | 0.03228150533976806 | 0.11529109049917166 |
| bear_low_vol | 2 | 0.05081458192924635 | 0.1814806497473084 |

## Diversification

- basis: daily log returns inside each 70-session window
- mean pairwise correlation among picks: **0.295423496757423**
- median positions: 20.0
- **effective positions: 3.0243247475010966**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 18 (≈4/year at this holding period)
- periods with a usable return: 16
- mean picks with a forward return: 17.8 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **6.199107787819718**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 5,
  "free_parameters": 5,
  "n_eff": 3.8356146166907052,
  "t_bonf": 2.4872463226568975,
  "variants_tried": 6,
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

