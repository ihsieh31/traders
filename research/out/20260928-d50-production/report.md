# Round 20260928-d50-production — d50-production

Generated 2026-09-27T16:59:34.172801+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.471 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.1458490082561215, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.18671552551751947, production 0.1458490082561215 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 3 with any data: bull_high_vol(n=7), bull_low_vol(n=13), bear_high_vol(n=4) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.023397232834161966, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0142% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.4029133925885253** (n = 23 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `adv20` | +1 | -0.003607825133321713 | -0.4952892705544453 | 1149 | 0.5230635335073978 |
| `r20` | +1 | 0.0030847635399197577 | 0.2357255539457403 | 1149 | 0.5831157528285466 |
| `r60` | +1 | 0.014161113402653322 | 0.911084062957272 | 1149 | 0.6161879895561357 |
| `vol20` | -1 | 0.04472099930721463 | 2.537863223995266 | 1149 | 0.5909486510008704 |
| `volume_ratio` | +1 | -0.004343486580073101 | -0.9970081858584442 | 1149 | 0.4760661444734552 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 23 | 0.037343105103503894 | 3.7697452652522925 | 0.18671552551751947 |
| wide | 1.5x | 23 | 0.03688006162524302 | 3.7222974454980853 | 0.1844003081262151 |
| wide | 2.0x | 23 | 0.03641701814698216 | 3.6748656766929035 | 0.1820850907349108 |
| production | base | 23 | 0.029169801651224295 | 3.8279013503258006 | 0.1458490082561215 |
| production | 1.5x | 23 | 0.028745888607746045 | 3.7721369506270834 | 0.14372944303873023 |
| production | 2.0x | 23 | 0.02832197556426778 | 3.7163712002362748 | 0.1416098778213389 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 3 | 0.04534741515959981 |
| 1 | reselect | 2016-09-30..2017-03-31 | 3 | 0.19974419288141157 |
| 2 | frozen | 2018-01-02..2018-07-02 | 3 | 0.1581491035145381 |
| 2 | reselect | 2018-01-02..2018-07-02 | 3 | 0.22987500473057296 |
| 3 | frozen | 2019-04-04..2019-10-02 | 3 | 0.13150336917286906 |
| 3 | reselect | 2019-04-04..2019-10-02 | 3 | 0.13376895547162354 |
| 4 | frozen | 2020-07-06..2020-12-31 | 3 | 0.2963883647229839 |
| 4 | reselect | 2020-07-06..2020-12-31 | 3 | 0.7953392380516534 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 2 | 90 | 0.14730239669576778 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.037711956642078805 | 0.18855978321039402 |
| bull_low_vol | 13 | 0.019665414534337847 | 0.09832707267168923 |
| bear_high_vol | 4 | 0.05042378417907175 | 0.25211892089535876 |
| bear_low_vol | 0 | None | None |

## Diversification

- basis: daily log returns inside each 50-session window
- mean pairwise correlation among picks: **0.34911931772963295**
- median positions: 20.0
- **effective positions: 2.6201100922337464**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 26 (≈5/year at this holding period)
- periods with a usable return: 23
- mean picks with a forward return: 17.7 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.4029133925885253**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 5,
  "free_parameters": 5,
  "n_eff": 3.6608539907814297,
  "t_bonf": 2.470507713091359,
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

