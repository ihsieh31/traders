# short-reversal

> Pre-registered specification. Written BEFORE results were seen;
> if you are reading it after the fact, the round is not a valid test.

## Parameters

- free parameters: **1** (budget 6, plan.md R4)
- holding period: **10** sessions
- selection: top **20** by score, equal weight
- variants tried this round: 2

## Components

| # | name | sign | lookback |
|---:|---|---:|---:|
| 1 | `r5_flipped` | -1 | 5 |

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

r5 with a negative sign. Production computes r5 and weights it at 0.00. The sign prior comes from F-20260927-03, not from this round.

