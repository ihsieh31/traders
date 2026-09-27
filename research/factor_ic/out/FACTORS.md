# 候選因子公式 reference（自動產生，勿手改）

本文件由 `python -m research.factor_ic.src.factor_report` 從 `src/factor_lib.py` 的註冊表產生，因此**不可能與實作不一致**。

因子總數：**93**，另加 8 個生產因子作為基準線。
取樣視窗：**253 根日線**（約 12.0 個月）。

## 記號

```
c[-1]          as_of 當日收盤
c[-1-k]        k 個交易日前的收盤
d              對數報酬 = diff(log(c))，長度 W-1，d[-1] 為 as_of 當日
mean(x[-k:])   尾端 k 個元素的平均
std(x[-k:])    尾端 k 個元素的樣本標準差（ddof=1）
HV(k)          sqrt(mean(d[-k:]**2) * 252)  年化實現波動
mkt            等權市場對數報酬（全宇宙橫斷面平均）
```

## 期望方向

`期望方向` 是**文獻先驗**，用於解讀結果，**不是**用來翻轉符號。若實測方向與先驗相反，報告會如實呈現並標記，不做修正。

| 期望符號 | 意思 |
|:-:|---|
| `+` | 值越高，預期未來報酬越高 |
| `-` | 值越高，預期未來報酬越低（低波動、低 beta 等溢價） |

## A. 短期反轉（7 個）

| 因子 | 方向 | 公式 | 說明 |
|:-:|:-:|---|---|
| `mom_1` | `-` | `c[-1] / c[-1-1] - 1` |  |
| `mom_2` | `-` | `c[-1] / c[-1-2] - 1` |  |
| `mom_3` | `-` | `c[-1] / c[-1-3] - 1` |  |
| `mom_5` | `-` | `c[-1] / c[-1-5] - 1` |  |
| `mom_10` | `-` | `c[-1] / c[-1-10] - 1` |  |
| `mom_15` | `-` | `c[-1] / c[-1-15] - 1` |  |
| `mom_20` | `-` | `c[-1] / c[-1-20] - 1` | <=20d is treated as reversal, >20d as momentum |

## B. 動能（22 個）

| 因子 | 方向 | 公式 | 說明 |
|:-:|:-:|---|---|
| `mom_30` | `+` | `c[-1] / c[-1-30] - 1` | <=20d is treated as reversal, >20d as momentum |
| `mom_40` | `+` | `c[-1] / c[-1-40] - 1` |  |
| `mom_60` | `+` | `c[-1] / c[-1-60] - 1` |  |
| `mom_90` | `+` | `c[-1] / c[-1-90] - 1` |  |
| `mom_120` | `+` | `c[-1] / c[-1-120] - 1` |  |
| `mom_180` | `+` | `c[-1] / c[-1-180] - 1` |  |
| `mom_250` | `+` | `c[-1] / c[-1-250] - 1` |  |
| `mom_6m_skip_1m` | `+` | `c[-21] / c[-126] - 1` | classic 6-1 momentum: skips the most recent month to avoid the one-month reversal contaminating it |
| `mom_accel_20_60` | `+` | `(c[-1]/c[-21] - 1) - (c[-1]/c[-61] - 1)` | short leg minus long leg; positive means the recent move is accelerating relative to the quarter |
| `ma_dist_5` | `+` | `c[-1] / mean(c[-5:]) - 1` |  |
| `ma_dist_10` | `+` | `c[-1] / mean(c[-10:]) - 1` |  |
| `ma_dist_20` | `+` | `c[-1] / mean(c[-20:]) - 1` |  |
| `ma_dist_50` | `+` | `c[-1] / mean(c[-50:]) - 1` |  |
| `ma_dist_100` | `+` | `c[-1] / mean(c[-100:]) - 1` |  |
| `ma_dist_200` | `+` | `c[-1] / mean(c[-200:]) - 1` |  |
| `ma_cross_5_20` | `+` | `mean(c[-5:]) / mean(c[-20:]) - 1` |  |
| `ma_cross_10_50` | `+` | `mean(c[-10:]) / mean(c[-50:]) - 1` |  |
| `ma_cross_20_60` | `+` | `mean(c[-20:]) / mean(c[-60:]) - 1` |  |
| `ma_cross_50_200` | `+` | `mean(c[-50:]) / mean(c[-200:]) - 1` |  |
| `high_252_prox` | `+` | `c[-1] / max(c[-252:]) - 1` | distance below the 52-week high; 0 means at the high |
| `range_pos_252` | `+` | `(c[-1] - min(c[-252:])) / (max(c[-252:]) - min(c[-252:]))` | position of the current close inside its own 52-week range, 0..1 |
| `low_252_prox` | `+` | `c[-1] / min(c[-252:]) - 1` | distance above the 52-week low |

## C. 波動率（17 個）

| 因子 | 方向 | 公式 | 說明 |
|:-:|:-:|---|---|
| `hv_5` | `-` | `sqrt(mean(d[-5:]**2) * 252)` |  |
| `hv_10` | `-` | `sqrt(mean(d[-10:]**2) * 252)` |  |
| `hv_20` | `-` | `sqrt(mean(d[-20:]**2) * 252)` |  |
| `hv_60` | `-` | `sqrt(mean(d[-60:]**2) * 252)` |  |
| `hv_120` | `-` | `sqrt(mean(d[-120:]**2) * 252)` |  |
| `hv_250` | `-` | `sqrt(mean(d[-250:]**2) * 252)` |  |
| `parkinson_20` | `-` | `sqrt(mean(log(h[-20:]/l[-20:])**2) / (4*ln2) * 252)` | range-based volatility; uses only high/low, so it is far less noisy than close-to-close realised vol on the same window |
| `parkinson_60` | `-` | `sqrt(mean(log(h[-60:]/l[-60:])**2) / (4*ln2) * 252)` | range-based volatility; uses only high/low, so it is far less noisy than close-to-close realised vol on the same window |
| `parkinson_120` | `-` | `sqrt(mean(log(h[-120:]/l[-120:])**2) / (4*ln2) * 252)` | range-based volatility; uses only high/low, so it is far less noisy than close-to-close realised vol on the same window |
| `gkvol_20` | `-` | `sqrt(mean(0.5*log(h/l)**2 - (2*ln2-1)*log(c/l)**2, 20) * 252)` | Garman-Klass: Parkinson corrected for the drift induced by closing prices inside the range |
| `gkvol_60` | `-` | `sqrt(mean(0.5*log(h/l)**2 - (2*ln2-1)*log(c/l)**2, 60) * 252)` | Garman-Klass: Parkinson corrected for the drift induced by closing prices inside the range |
| `gkvol_120` | `-` | `sqrt(mean(0.5*log(h/l)**2 - (2*ln2-1)*log(c/l)**2, 120) * 252)` | Garman-Klass: Parkinson corrected for the drift induced by closing prices inside the range |
| `yzvol_60` | `-` | `Yang-Zhang overnight+open-to-close variance over 60d, annualised` | the most efficient daily volatility estimator available from OHLC; decomposes overnight gap risk from intraday risk |
| `hv_ratio_20_60` | `-` | `HV(20) / HV(60)` | volatility expansion: recent vol relative to the quarter. Rising volatility is classically negative for returns |
| `vol_of_vol_60` | `+` | `std(d[-60:]) / mean(|d[-60:]|) * sqrt(60)` | dispersion of daily return magnitudes; high values mean a few large moves dominate the window |
| `downside_upside_60` | `-` | `std(d[d<0]) / std(d[d>0]) over 60d` | asymmetry of return magnitudes. >1 means downside moves are larger |
| `hl_range_20` | `+` | `mean((h[-20:] - l[-20:]) / c[-21:-1])` | average intraday range relative to the prior close; a direct liquidity/friction proxy |

## D. 流動性（9 個）

| 因子 | 方向 | 公式 | 說明 |
|:-:|:-:|---|---|
| `adv_5` | `+` | `log(mean(c[-5:] * v[-5:]))` |  |
| `adv_20` | `+` | `log(mean(c[-20:] * v[-20:]))` |  |
| `adv_60` | `+` | `log(mean(c[-60:] * v[-60:]))` |  |
| `adv_z_252` | `+` | `(ADV(20) - mean(ADV(20) over 252d)) / std(ADV(20) over 252d)` | a stock's current liquidity versus its OWN history, which strips out the permanent size differences between a large and a small issuer |
| `adv_trend_20_60` | `+` | `mean(c[-20:]*v[-20:]) / mean(c[-60:]*v[-60:]) - 1` | is traded value rising relative to the quarter |
| `amihud_20` | `-` | `mean(|d[-20:]| / (c*v)[-20:]) * 1e6` | Amihud illiquidity: price impact per dollar traded. The canonical illiquidity measure; higher means less liquid, which predicts LOWER returns |
| `amihud_60` | `-` | `mean(|d[-60:]| / (c*v)[-60:]) * 1e6` |  |
| `zero_volume_days_60` | `-` | `count(v[-60:] == 0) over 60d` | sessions with no reported volume in the window; a stale or effectively untraded instrument |
| `amihud_ratio_20_252` | `-` | `amihud_20 / median(amihud_20 over 252d) - 1` | current price impact versus the stock's own one-year norm |

## E. 量能（12 個）

| 因子 | 方向 | 公式 | 說明 |
|:-:|:-:|---|---|
| `volume_ratio_5_20` | `+` | `mean(v[-5:]) / mean(v[-20:]) - 1` | production's volume_ratio; rising volume is associated with higher subsequent returns |
| `volume_ratio_20_60` | `+` | `mean(v[-20:]) / mean(v[-60:]) - 1` |  |
| `volume_ratio_60_250` | `+` | `mean(v[-60:]) / mean(v[-250:]) - 1` |  |
| `obv_slope_10` | `+` | `standardised slope of cumulative signed volume over 10d` | on-balance volume trend, normalised so it is comparable across a $1M and a $1B stock |
| `obv_slope_20` | `+` | `standardised slope of cumulative signed volume over 20d` | on-balance volume trend, normalised so it is comparable across a $1M and a $1B stock |
| `obv_slope_60` | `+` | `standardised slope of cumulative signed volume over 60d` | on-balance volume trend, normalised so it is comparable across a $1M and a $1B stock |
| `signed_vol_5` | `+` | `sum(sign(d[-5:]) * v[-5:]) / sum(v[-5:])` | net buying pressure: the share of volume traded on up-closes minus the share on down-closes |
| `signed_vol_20` | `+` | `sum(sign(d[-20:]) * v[-20:]) / sum(v[-20:])` | net buying pressure: the share of volume traded on up-closes minus the share on down-closes |
| `signed_vol_60` | `+` | `sum(sign(d[-60:]) * v[-60:]) / sum(v[-60:])` | net buying pressure: the share of volume traded on up-closes minus the share on down-closes |
| `mfi_14` | `+` | `Money Flow Index over 14d: 100 - 100/(1 + sum(typical*volume up / sum(typical*volume down))` |  |
| `price_vol_corr_20` | `+` | `corr(d[-20:], v[-20:] / mean(v[-20:]) - 1) over 20d` | do price moves and volume moves happen together? a persistent positive value implies informed flow rather than noise trading |
| `dollar_vol_boost_5` | `+` | `(c*v)[-1] / mean((c*v)[-20:]) - 1` | today's traded value against its own 20-day norm |

## F. 日內結構（7 個）

| 因子 | 方向 | 公式 | 說明 |
|:-:|:-:|---|---|
| `close_pos_1` | `+` | `(c[-1] - o[-1]) / (h[-1] - l[-1])` | where the close landed inside the as-of day's range. Near +1 means the session ended on its high |
| `close_pos_20` | `+` | `mean over 20d of (c - o) / (h - l)` |  |
| `gap_20` | `+` | `mean(o[-20:-1] / c[-21:-2] - 1) over 20d` | average overnight gap. Persistent positive gaps indicate a stock that tends to be bought before the open |
| `intraday_20` | `+` | `mean(c[-20:] / o[-20:] - 1) over 20d` | average open-to-close move |
| `overnight_minus_intraday_20` | `+` | `gap_20 - intraday_20` | does the stock move more overnight than during the session? a preference for one or the other is a behavioural signature |
| `overnight_vol_60` | `-` | `std(log(o[-60:-1] / c[-61:-2])) * sqrt(252)` | volatility of the overnight gap alone |
| `intraday_share_60` | `+` | `std(log(c/o)[-60:]) / std(log(c/o[-61:-1]) + log(c[-60:]/o[-60:]))` | the fraction of total volatility arising during the session rather than overnight. High values mean intraday-driven stocks |

## G. 市場相對風險（8 個）

| 因子 | 方向 | 公式 | 說明 |
|:-:|:-:|---|---|
| `beta_20` | `-` | `cov(d[-20:], mkt[-20:]) / var(mkt[-20:])` | market sensitivity. Low beta predicts higher returns (the betting-against-beta effect) |
| `beta_60` | `-` | `cov(d[-60:], mkt[-60:]) / var(mkt[-60:])` | market sensitivity. Low beta predicts higher returns (the betting-against-beta effect) |
| `beta_120` | `-` | `cov(d[-120:], mkt[-120:]) / var(mkt[-120:])` | market sensitivity. Low beta predicts higher returns (the betting-against-beta effect) |
| `corr_mkt_60` | `+` | `corr(d[-60:], mkt[-60:])` | how tightly the stock moves WITH the market, independent of how much it moves. High values mean the stock is a market proxy |
| `idio_vol_60` | `-` | `std(d[-60:] - beta_60 * mkt[-60:]) * sqrt(252)` | volatility not explained by the market. This is what a diversified investor actually bears, and it is the cleaner risk measure |
| `r2_mkt_60` | `+` | `corr(d[-60:], mkt[-60:]) ** 2` | share of the stock's 60d variance that is market variance |
| `max_drawdown_60` | `+` | `min over t of c[t] / c[0] - 1, trailing 60d` | worst peak-to-trough decline inside the window |
| `max_runup_60` | `+` | `max over t of c[t] / c[0] - 1, trailing 60d` |  |

## H. 高階矩與尾端形狀（11 個）

| 因子 | 方向 | 公式 | 說明 |
|:-:|:-:|---|---|
| `skew_20` | `-` | `third standardised moment of d[-20:]` | negative skew means a history of small gains punctuated by rare large losses, which historically predicts LOWER returns |
| `skew_60` | `-` | `third standardised moment of d[-60:]` | negative skew means a history of small gains punctuated by rare large losses, which historically predicts LOWER returns |
| `skew_120` | `-` | `third standardised moment of d[-120:]` | negative skew means a history of small gains punctuated by rare large losses, which historically predicts LOWER returns |
| `kurt_60` | `+` | `fourth standardised moment of d[-60:]` | tail heaviness |
| `skew_over_vol_60` | `-` | `skew_60 / HV(60)` | skew normalised by volatility, so a high value is unusual asymmetry rather than merely a volatile stock |
| `tail_ratio_120` | `+` | `(p95 - p50) / (p50 - p05) of d[-120:]` | is the right tail fatter than the left? a lottery-like payoff shape |
| `up_days_60` | `+` | `mean(d[-60:] > 0)` |  |
| `max_ret_60` | `-` | `max(d[-60:])` | a single very large up-move in the window. Attention-grabbing moves tend to reverse |
| `min_ret_60` | `+` | `min(d[-60:])` | a single very large down-move. Sharp drops tend to bounce |
| `consec_up_20` | `+` | `current run length of consecutive up sessions, capped at 20` |  |
| `pct_up_days_20` | `+` | `mean(d[-20:] > 0)` |  |
