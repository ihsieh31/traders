# Round 20260928-d90-production — d90-production

Generated 2026-09-27T16:56:07.571598+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.499 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.12242074882778965, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.11478025479043323, production 0.12242074882778965 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 2 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 4 regimes agree, out of 4 with any data: bull_high_vol(n=1), bull_low_vol(n=7), bear_high_vol(n=4), bear_low_vol(n=1) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.03096622775929162, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0084% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 2.19111630729875** (n = 12 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `adv20` | +1 | 0.02833962240558935 | 3.5650606961280573 | 1109 | 0.6825969341749324 |
| `r20` | +1 | 0.0016730264362342447 | 0.11111924476371911 | 1109 | 0.5653742110009017 |
| `r60` | +1 | 0.01770974267788808 | 0.9540825792040716 | 1109 | 0.5996393146979261 |
| `vol20` | -1 | 0.03240562845055103 | 1.6447022627615508 | 1109 | 0.5770964833183048 |
| `volume_ratio` | +1 | -0.006237031412491051 | -1.5243945655956253 | 1109 | 0.44634806131650134 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 12 | 0.04132089172455596 | 1.8074620014093685 | 0.11478025479043323 |
| wide | 1.5x | 12 | 0.04083130839122263 | 1.7859298133572574 | 0.11342030108672951 |
| wide | 2.0x | 12 | 0.0403417250578893 | 1.7644004036313035 | 0.11206034738302582 |
| production | base | 12 | 0.04407146957800428 | 2.320021797344724 | 0.12242074882778965 |
| production | 1.5x | 12 | 0.04358813624467095 | 2.29400748760472 | 0.12107815623519708 |
| production | 2.0x | 12 | 0.043104802911337614 | 2.268005873299608 | 0.11973556364260447 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 2 | 0.20370723862174045 |
| 1 | reselect | 2016-09-30..2017-03-31 | 2 | 0.004263110561266755 |
| 2 | frozen | 2018-01-02..2018-07-02 | 2 | 0.04696678461048909 |
| 2 | reselect | 2018-01-02..2018-07-02 | 2 | 0.08500235151084985 |
| 3 | frozen | 2019-04-04..2019-10-02 | 2 | 0.09781643602375815 |
| 3 | reselect | 2019-04-04..2019-10-02 | 2 | 0.17751448136967968 |
| 4 | frozen | 2020-07-06..2020-12-31 | 2 | 0.34295677412318837 |
| 4 | reselect | 2020-07-06..2020-12-31 | 2 | -0.5732283279661045 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 1 | 120 | 0.1285030203539105 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 1 | 0.030927503591636152 | 7.731875897909038 |
| bull_low_vol | 7 | 0.010453204239022334 | 0.029036678441728705 |
| bear_high_vol | 4 | 0.08633040929172275 | 0.23980669247700764 |
| bear_low_vol | 1 | 0.16576647379609052 | 41.441618449022634 |

## Diversification

- basis: daily log returns inside each 90-session window
- mean pairwise correlation among picks: **0.35468330624087135**
- median positions: 20.0
- **effective positions: 2.584318956231852**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 14 (≈3/year at this holding period)
- periods with a usable return: 12
- mean picks with a forward return: 17.1 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **2.19111630729875**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 5,
  "free_parameters": 5,
  "n_eff": 3.9645144121576124,
  "t_bonf": 2.499056748063448,
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

