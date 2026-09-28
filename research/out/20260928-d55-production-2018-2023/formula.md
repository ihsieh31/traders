# d55-production

> Pre-registered specification. Written BEFORE results were seen;
> if you are reading it after the fact, the round is not a valid test.

## Parameters

- free parameters: **5** (budget 6, plan.md R4)
- holding period: **55** sessions
- selection: top **20** by score, equal weight
- variants tried this round: 6

## Components

| # | name | sign | lookback |
|---:|---|---:|---:|
| 1 | `adv20` | +1 | 20 |
| 2 | `r20` | +1 | 20 |
| 3 | `r60` | +1 | 60 |
| 4 | `vol20` | -1 | 20 |
| 5 | `volume_ratio` | +1 | 5 |

## Composition

```
score = 100 * mean_over_components( sign * percentile_cs(component) )
        percentile computed over the ELIGIBLE SET ONLY (process.md S-12)
```

## Entry and exit

```
entry = open[as_of + 1]
exit  = close[as_of + horizon]
```

Peak of the 35..70 discovery sweep. Being re-measured on 2018-2023 and 2020-2025, neither of which the sweep used.

