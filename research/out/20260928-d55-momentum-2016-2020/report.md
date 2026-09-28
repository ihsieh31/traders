# Round 20260928-d55-momentum-2016-2020 — d55-momentum

Generated 2026-09-27T17:10:29.365753+00:00

## Verdict

**UNDECIDED** — 0 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 1.962 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.358511779100498, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.32566669010110055, production 0.358511779100498 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 3 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['r60_window']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=7), bull_low_vol(n=10), bear_high_vol(n=3), bear_low_vol(n=2) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.06242828749893092, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0248% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 1.6894622872389995** (n = 21 periods)
- **the direction of the effect is broadly trustworthy**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `r60` | +1 | 0.01024727530397959 | 0.6882476331947178 | 1144 | 0.6066433566433567 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 21 | 0.07164667182224212 | 2.5071793195011156 | 0.32566669010110055 |
| wide | 1.5x | 21 | 0.07118595753652783 | 2.4911177809893 | 0.3235725342569447 |
| wide | 2.0x | 21 | 0.07072524325081353 | 2.475055310590904 | 0.3214783784127888 |
| production | base | 21 | 0.07887259140210955 | 2.8188755209802454 | 0.358511779100498 |
| production | 1.5x | 21 | 0.0784249723544905 | 2.802989015168062 | 0.356477147065866 |
| production | 2.0x | 21 | 0.07797735330687143 | 2.7871009364586756 | 0.3544425150312338 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 3 | 0.18446195749380134 |
| 1 | reselect | 2016-09-30..2017-03-31 | 3 | 0.04813196984922151 |
| 2 | frozen | 2018-01-02..2018-07-02 | 3 | 0.46272309590975386 |
| 2 | reselect | 2018-01-02..2018-07-02 | 3 | 0.46272309590975386 |
| 3 | frozen | 2019-04-04..2019-10-02 | 3 | -0.023909714926265768 |
| 3 | reselect | 2019-04-04..2019-10-02 | 3 | 0.030266504777173114 |
| 4 | frozen | 2020-07-06..2020-12-31 | 3 | 0.8706660533483869 |
| 4 | reselect | 2020-07-06..2020-12-31 | 3 | -0.3842554904877581 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **PLATEAU** | 3 | 60 | 0.358511779100498 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 7 | 0.07353296479018964 | 0.33424074904631657 |
| bull_low_vol | 10 | 0.060981603246130056 | 0.27718910566422755 |
| bear_high_vol | 3 | 0.27616417801827703 | 1.2552917182648957 |
| bear_low_vol | 2 | -0.01027536129244088 | -0.0467061876929131 |

## Diversification

- basis: daily log returns inside each 55-session window
- mean pairwise correlation among picks: **0.20845299978634477**
- median positions: 20.0
- **effective positions: 4.03176466435796**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 24 (≈5/year at this holding period)
- periods with a usable return: 21
- mean picks with a forward return: 17.5 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **1.6894622872389995**
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

