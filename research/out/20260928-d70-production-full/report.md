# Round 20260928-d70-production-full — d70-production

Generated 2026-09-27T17:01:07.306963+00:00

## Verdict

**UNDECIDED** — 0 of 5 decided checks failed (5/11 measured).

| # | condition | result | detail |
|---:|---|---|---|
| 1 | validation \|t_NW\| >= t_bonf | not measured | validation NW t = None, t_bonf = 2.501 |
| 2 | discovery and validation same sign | not measured | discovery net annualised 0.06345931289828854, validation None |
| 3 | validation net annualised > 0 | not measured | None |
| 4 | validation net annualised > 0 at 2.0x cost | not measured | no validation run yet |
| 5 | beats the production baseline | not measured | no baseline run supplied |
| 6 | holds in both universes | PASS | wide 0.03989768807432092, production 0.06345931289828854 |
| 7 | walk-forward >= 3/4 folds same sign | not measured | UNDECIDABLE: agreement = 3/4 but a fold holds only 2 non-overlapping periods, below the floor of 8. A 60-day horizon over 5 years leaves too few folds to measure sign agreement (process.md S-6) |
| 8 | every parameter +/- 1 cell same sign | PASS | swept ['r60_window']; spikes: none |
| 9 | same sign in >= 2 regimes | PASS | 3 regimes agree, out of 4 with any data: bull_high_vol(n=5), bull_low_vol(n=7), bear_high_vol(n=2), bear_low_vol(n=2) |
| 10 | same sign after dropping best 5% of periods | PASS | dropped 1 periods, mean 0.008774082712462845, sign_flipped=False |
| 11 | order size <= 0.1% of ADV20 | PASS | worst-case participation = 0.0107% of ADV20 |

## Sample sufficiency (disclosure, not a pass condition)

- **CI width / |mean period return| = 4.943867989011218** (n = 16 periods)
- **the direction may be right but the magnitude is not trustworthy; do not quote the point estimate**

> The original threshold was CI width < effect. It is mathematically unreachable on this dataset at any holding period -- 404 years of data at 5 days, 30 years at 60. A rule that can only ever say FAIL trains people to ignore the rules, so it was demoted to a mandatory disclosure. See plan.md R16-Q1 and process.md S-45.

## Component IC (production universe, discovery)

| component | sign | IC | NW t | n | positive |
|---|---:|---:|---:|---:|---:|
| `adv20` | +1 | 0.023937925837460437 | 3.48516789624428 | 1129 | 0.6678476527900797 |
| `r20` | +1 | 0.008391114257546115 | 0.6079795304281352 | 1129 | 0.5615589016829052 |
| `r60` | +1 | 0.012614717604635495 | 0.7750572533692752 | 1129 | 0.6102745792736936 |
| `vol20` | -1 | 0.03267098165644222 | 1.828927040113092 | 1129 | 0.5801594331266607 |
| `volume_ratio` | +1 | -0.006109856704985693 | -1.5974770816484585 | 1129 | 0.44729849424269263 |

## Portfolio (non-overlapping periods, net of cost)

| universe | scenario | periods | mean/period | NW t | annualised |
|---|---|---:|---:|---:|---:|
| wide | base | 16 | 0.011171352660809857 | 0.6158338931960967 | 0.03989768807432092 |
| wide | 1.5x | 16 | 0.010676040160809856 | 0.5885127380719308 | 0.038128714860035204 |
| wide | 2.0x | 16 | 0.010180727660809857 | 0.5611931051967768 | 0.03635974164574949 |
| production | base | 16 | 0.01776860761152079 | 0.8821454043269972 | 0.06345931289828854 |
| production | 1.5x | 16 | 0.017290482611520795 | 0.8584240907147189 | 0.06175172361257427 |
| production | 2.0x | 16 | 0.016812357611520792 | 0.834701814074411 | 0.060044134326859976 |

## Walk-forward

3/4 folds agree (K=4, train=0.6, fixed before results).

| fold | variant | test window | periods | annualised net |
|---:|---|---|---:|---:|
| 1 | frozen | 2016-09-30..2017-03-31 | 2 | 0.27069461302048103 |
| 1 | reselect | 2016-09-30..2017-03-31 | 2 | 0.08745151674174055 |
| 2 | frozen | 2018-01-02..2018-07-02 | 2 | -0.04605310543572799 |
| 2 | reselect | 2018-01-02..2018-07-02 | 2 | -0.20138262701996285 |
| 3 | frozen | 2019-04-04..2019-10-02 | 2 | 0.07861835889030597 |
| 3 | reselect | 2019-04-04..2019-10-02 | 2 | 0.06900400828366003 |
| 4 | frozen | 2020-07-06..2020-12-31 | 2 | 0.6716102914671219 |
| 4 | reselect | 2020-07-06..2020-12-31 | 2 | -0.008825127299617 |

## Parameter sweeps

| parameter | verdict | run length | best value | best metric |
|---|---|---:|---:|---:|
| `r60_window` | **PLATEAU** | 3 | 60 | 0.06345931289828854 |

## Regimes

| cell | periods | mean net | annualised net |
|---|---:|---:|---:|
| bull_high_vol | 5 | 0.035839123390958474 | 0.12799686925342313 |
| bull_low_vol | 7 | -0.0015817241749755487 | -0.00564901491062696 |
| bear_high_vol | 2 | 0.020734914214217262 | 0.07405326505077595 |
| bear_low_vol | 2 | 0.03735217281296731 | 0.13340061718916896 |

## Diversification

- basis: daily log returns inside each 70-session window
- mean pairwise correlation among picks: **0.33480550671265435**
- median positions: 20.0
- **effective positions: 2.7169097071699944**
- 20 names is not 20 independent bets; see process.md S-39

## Robustness

- non-overlapping periods constructed: 18 (≈4/year at this holding period)
- periods with a usable return: 16
- mean picks with a forward return: 17.8 of 20
- periods where nothing could be exited: 0
- CI width / |mean| = **4.943867989011218**
- block bootstrap crosses zero: **False**
- drop best 5% flips sign: **False**

## Search cost

```
{
  "components": 5,
  "free_parameters": 5,
  "n_eff": 3.9907286251158776,
  "t_bonf": 2.501406285251366,
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

