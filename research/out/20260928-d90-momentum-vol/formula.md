# d90-momentum-vol

> Pre-registered specification. Written BEFORE results were seen;
> if you are reading it after the fact, the round is not a valid test.

## Parameters

- free parameters: **2** (budget 6, plan.md R4)
- holding period: **90** sessions
- selection: top **20** by score, equal weight
- variants tried this round: 3

## Components

| # | name | sign | lookback |
|---:|---|---:|---:|
| 1 | `r90` | +1 | 90 |
| 2 | `vol20` | -1 | 20 |

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

r90 momentum plus a low-volatility tilt, 90-day hold.

