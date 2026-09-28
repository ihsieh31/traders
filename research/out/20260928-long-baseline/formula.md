# long-baseline

> Pre-registered specification. Written BEFORE results were seen;
> if you are reading it after the fact, the round is not a valid test.

## Parameters

- free parameters: **5** (budget 6, plan.md R4)
- holding period: **60** sessions
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

Production's five components at a 60-day holding period. The control for every long-horizon candidate.

