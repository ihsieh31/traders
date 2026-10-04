# lit-quality-composite

> Pre-registered specification. Written BEFORE results were seen;
> if you are reading it after the fact, the round is not a valid test.

## Parameters

- free parameters: **4** (budget 6, plan.md R4)
- holding period: **10** sessions
- selection: top **20** by score, equal weight
- variants tried this round: 4

## Components

| # | name | sign | lookback |
|---:|---|---:|---:|
| 1 | `gpa` | +1 | 0 |
| 2 | `cfoa` | +1 | 0 |
| 3 | `lev` | -1 | 0 |

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

screen-alternatives 2026-09-29 proposal, cross-sectional instead of sector percentiles.

