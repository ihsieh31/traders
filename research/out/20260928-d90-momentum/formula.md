# d90-momentum

> Pre-registered specification. Written BEFORE results were seen;
> if you are reading it after the fact, the round is not a valid test.

## Parameters

- free parameters: **1** (budget 6, plan.md R4)
- holding period: **90** sessions
- selection: top **20** by score, equal weight
- variants tried this round: 2

## Components

| # | name | sign | lookback |
|---:|---|---:|---:|
| 1 | `r90` | +1 | 90 |

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

r90 momentum, 90-day hold. The operator's chosen configuration.

