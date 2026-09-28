# d70-momentum

> Pre-registered specification. Written BEFORE results were seen;
> if you are reading it after the fact, the round is not a valid test.

## Parameters

- free parameters: **1** (budget 6, plan.md R4)
- holding period: **70** sessions
- selection: top **20** by score, equal weight
- variants tried this round: 2

## Components

| # | name | sign | lookback |
|---:|---|---:|---:|
| 1 | `r60` | +1 | 60 |

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

r60 momentum, 70-day hold. Holding-period probe above 60.

