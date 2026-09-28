# Round 20260928-d55-production-2016-2020 — d55-production

Generated 2026-09-27T17:10:07.825953+00:00

## Verdict

**REJECTED** — 1 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.486 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.178929105655517, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.16800462298797972, production 0.178929105655517 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 4/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | **FAIL** | swept ['r60_window']; spikes: ['r60_window'] |
| 9 | same sign in >= 2 regimes | PASS | 4 regimes agree, out of 4 with any data: bull_high_vol(n=7), bull_low_vol(n=10), bear_high_vol(n=3), bear_low_vol(n=2) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.027861877260825237, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0160% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.6581719699299373** (n = 21 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `adv20` | +1 | 0.020548925990192254 | 3.301401647783241 | 1144 | 0.6555944055944056 |
| `r20` | +1 | 0.005390584482755652 | 0.4075182178931118 | 1144 | 0.5725524475524476 |
| `r60` | +1 | 0.01024727530397959 | 0.6882476331947178 | 1144 | 0.6066433566433567 |
| `vol20` | -1 | 0.03051774497935923 | 1.8240222705459428 | 1144 | 0.5533216783216783 |
| `volume_ratio` | +1 | -0.003989744361781911 | -1.0638411184776821 | 1144 | 0.4763986013986014 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 21 | 0.036961017057355536 | 2.828703085610775 | 0.16800462298797972 |
| wide | 1.5x | 21 | 0.03647530277164125 | 2.7914938890222403 | 0.16579683078018753 |
| wide | 2.0x | 21 | 0.03598958848592697 | 2.7542852416885792 | 0.16358903857239532 |
| production | base | 21 | 0.03936440324421374 | 3.677967542753038 | 0.178929105655517 |
| production | 1.5x | 21 | 0.03890011752992803 | 3.634549027731661 | 0.17681871604512742 |
| production | 2.0x | 21 | 0.038435831815642314 | 3.5911298928210225 | 0.1747083264347378 |

## Walk-forward

4/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 3 | 0.19258596012792453 |
| 1 | reselect | 2016-09-30..2017-03-31 | 3 | 0.04564059823467355 |
| 2 | frozen | 2018-01-02..2018-07-02 | 3 | 0.17404955156946653 |
| 2 | reselect | 2018-01-02..2018-07-02 | 3 | 0.15131330526459327 |
| 3 | frozen | 2019-04-04..2019-10-02 | 3 | 0.13240007668331968 |
| 3 | reselect | 2019-04-04..2019-10-02 | 3 | 0.12877369491569898 |
| 4 | frozen | 2020-07-06..2020-12-31 | 3 | 0.44893325735469214 |
| 4 | reselect | 2020-07-06..2020-12-31 | 3 | 0.7604571213854388 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **SPIKE** | 2 | 90 | 0.1871712128745275 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.016396627273387636 | 0.07453012396994381 |
| bull_low_vol | 10 | 0.03697424470962314 | 0.1680647486801052 |
| bear_high_vol | 3 | 0.14802729230467115 | 0.6728513286575962 |
| bear_low_vol | 2 | 0.02303952275460073 | 0.10472510343000332 |

## Diversification

- basis: daily log returns inside each 55-session window
- mean pairwise correlation among picks: **0.31025904464248705**
- median positions: 20.0
- **effective positions: 2.900685524840313**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 24 (≈5/year at this holding period)
- periods with a usable return: 21
- mean picks with a forward return: 17.5 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.6581719699299373**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 5,
  "free_parameters": 5,
  "n_eff": 3.817507391313743,
  "t_bonf": 2.4855518893969917,
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

