# Drawdown / recovery factor mining — results and audit trail

## What was measured

`src/dd_final.py`, 12,427 symbols, 482 as-of sessions (2016-01 .. 2026-08, step 5),
next-open forward return (entry `open[i+1]`, exit `close[i+h]`), Spearman rank IC,
Newey-West t (lag 3). Split at 2021-01-01: **D** = 2016-2020 discovery,
**V** = 2021-2026 validation, never used to choose anything until after the fact.

Two tests per candidate, because a factor can look good for the wrong reason:

1. **Raw IC** — cross-sectional rank correlation with the forward return.
2. **Incremental IC** — the candidate's cross-sectional rank is regressed on the
   ranks of 14 existing library factors (mom_20/60/250, max_drawdown_60,
   low_252_prox, range_pos_252, high_252_prox, hv_60, skew_60, adv_20,
   ma_dist_50, up_days_60, min_ret_60, max_runup_60) and the **residual** is
   correlated with the forward return.

**The decisive diagnostic is whether raw and incremental signs AGREE.** When they
flip, the factor is a re-parameterisation of something already in the library, not
a discovery. That test killed most of the candidates below.

## A bug worth recording

The first two rounds measured a "consecutive new 252-day low" factor whose
trailing-run counter *overwrote* `cur` instead of accumulating, and whose running
minimum looked **forward** (`np.minimum.accumulate(seg[:, ::-1])`) instead of
back. Both errors are silent — no exception, plausible-looking output. The buggy
version reported IC **+0.010 (t=+3.2)** and max |rho| 0.18; the corrected version
reports IC **-0.007 (t=-1.8)**. **The bug inverted the sign of the factor.**

`src/dd_sanity.py` now brute-force-verifies every implementation against naive
Python loops on adversarial inputs (ties, flat, monotone, sawtooth, and the
touching-equals case that the forward-looking minimum got wrong). Run it before
believing any number here.

## Rejected, with evidence

| candidate | verdict | evidence |
|---|---|---|
| current drawdown from window peak, `c[-1]/max(c[-252:]) - 1` | **exact duplicate** | mean rank corr with existing `high_252_prox` = **+1.00**. Same formula, already shipped. |
| **ulcer index** 120d | **not a discovery** | raw IC -0.028 (t-2.4) OOS, but corr **-0.79** `max_drawdown_60`, **-0.78** `high_252_prox`, **+0.75** `hv_60`; incremental IC **+0.001 (t 0.3)**. It is a noisy re-expression of existing vol/drawdown factors. |
| ulcer index / current drawdown | **dead** | raw IC <= 0.0056, t <= 0.5 everywhere. |
| time since running maximum (peak age) 252d | **momentum in disguise** | corr **-0.72** `range_pos_252`, -0.68 `mom_250`, -0.64 `high_252_prox`. Raw IC -0.022 (t-3.0) but incremental IC **+0.003 (t 0.8)** — sign flips. |
| within-3%-of-peak flag | **crude duplicate** | corr +0.68 `high_252_prox`. Raw IC ~0.00. |
| distance above 20d low | **mom_20 in disguise** | corr **+0.67** `mom_20`, +0.61 `ma_dist_50`; incremental IC <= +0.003 (t 0.9). |
| Choi (2021) recovery from MDD trough | **sign flips** | discovery +0.023 / validation **-0.019** at h=20; corr +0.80 `low_252_prox`. |
| recovery / MDD (reclaimed fraction) | **range_pos_252 in disguise** | corr **+0.83** `range_pos_252`. Raw IC +, incremental IC **-0.010 (t-3.7)**, opposite sign. |
| drawdown depth term structure, DD60-DD250 | **ambiguous** | raw IC -0.020 (momentum-like, corr -0.69 `mom_250`), incremental IC **+0.007 (t+1.9)**. Opposite signs = artefact. |
| self-referential DD252/abs(DD60) | **same dimension as above** | raw +, incremental **-0.007 (t-2.1)**. Same flip. |
| 60d versions of the duration family | **dies out of sample** | `tuw_frac_60` D-0.033/-0.023/-0.033 vs V-0.003/-0.001/-0.001; corr -0.69 `mom_60`, -0.67 `max_runup_60`. |

**Notable negative result:** every *recovery*-based construction tested —
recovery from the max-drawdown trough, recovery as a fraction of the decline,
recovery per day underwater — is collinear with `range_pos_252` / `low_252_prox` /
`mom_250` and adds nothing. In this panel the recovery family is barren; only the
**duration** and **new-low** families carry independent signal.

## Proposed

| factor | raw IC (V) h=5/10/20 | incremental IC (V) h=5/10/20 | max abs corr |
|---|---|---|---|
| `tuw_frac_252` | -0.035(-4.5) / -0.040(-4.0) / -0.050(-4.0) | -0.011(-3.0) / -0.013(-2.7) / -0.015(-2.7) | 0.71 (`mom_250`) |
| `dd_duration_252` | -0.029(-3.9) / -0.037(-3.7) / -0.050(-4.1) | -0.005(-1.6) / -0.008(-2.1) / -0.013(-2.8) | 0.66 (`mom_250`) |
| `dd_dur_term_60_252` | +0.022(+3.1) / +0.031(+3.3) / +0.042(+3.8) | +0.005(+1.6) / +0.008(+2.0) / +0.014(+3.0) | 0.40 (`max_runup_60`) |
| `new_low_run_252` | -0.007(-1.8) / -0.009(-2.1) / -0.011(-2.3) | -0.006(-2.8) / -0.008(-3.2) / -0.008(-2.5) | 0.21 (`low_252_prox`) |

All four have **raw and incremental signs in agreement**, which none of the
rejected factors managed. The main contamination is `mom_250`, and the honest
read is that part of `tuw_frac_252` / `dd_duration_252` is a slow-momentum proxy;
the term-structure and new-low factors are the ones that are genuinely new axes.

## Caveats that must travel with these numbers

- **`new_low_run_252` was pre-selected.** I first measured a *buggy* version of
  this family, found it attractive, then fixed the bug. The family is still the
  one with the cleanest incremental profile, but "survived a sign flip when the
  code was corrected" is not the same as "chosen blind". Its discovery-period
  t is only -0.5 to -1.2; only the validation period is significant.
- **`new_low_run_252` has ~5 distinct values per date** (median cross-section
  1,848 names). A Spearman IC on a 5-valued integer with heavy ties is a
  legitimate average-rank statistic but low-resolution, and the bulk of the
  cross-section sits at 0. Treat the magnitude as an upper bound.
- `dd_dur_term_60_252` is positive in validation at every horizon but its
  discovery h=20 raw IC is **-0.0067**, i.e. the sign is not yet stable across
  a full cycle. The 120/252 variant is more sign-consistent (positive in both
  periods at all three horizons, max |rho| only 0.18) with slightly smaller t.
- 20 candidates were screened in this domain. The project already runs a
  Bonferroni threshold of |t| >= 2.62 over ~101 factors; **none of the four
  reaches it on the full sample.** These are directions worth pre-registering,
  not factors to ship.
- Selection is per-date cross-sectional. None of this says anything about
  market timing, where a constant factor has no cross-sectional variance at all.
