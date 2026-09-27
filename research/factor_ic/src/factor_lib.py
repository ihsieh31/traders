"""Candidate factor library: explicit formulas over daily OHLCV windows.

Every factor is a closed-form expression in the notation below, computed
vectorised across the cross-section so that scanning ~90 factors costs little
more than scanning one. The docstrings here are the single source of truth:
``factor_report.py`` GENERATES ``FACTORS.md`` from this registry, so the
documented formula cannot drift from the implemented one.

Notation (window is oldest-first, last row is the ``as_of`` session):

    c[-1]        as_of close
    c[-1-k]      close k sessions earlier
    d            log returns, ``diff(log(c))``, length W-1, so ``d[-1]`` is
                 the as_of session's return
    mean(x[-k:]) mean of the trailing k elements
    std(x[-k:])  sample standard deviation (ddof=1) of the trailing k
    MA(k)        ``mean(c[-k:])``
    HV(k)        ``sqrt(mean(d[-k:]**2) * 252)`` -- annualised realised vol

Why a library at all: the production screen is seven hand-picked factors. The
question here is whether ANY measurable structure exists in daily bars at the
5-15 day holding period, so the library deliberately spans short-horizon
reversal, longer momentum, volatility shape, liquidity, volume interaction,
intraday microstructure, market-relative risk, and return higher moments --
including several families production does not use at all.

A factor with literature support is marked with a ``sign_hint``: +1 when a
HIGHER value is expected to predict a HIGHER forward return, -1 for the
inverse. This is metadata for reading the output; it is never used to flip a
sign, and a factor whose measured sign contradicts its hint is reported, not
silently corrected.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

import numpy as np

#: Longest lookback any factor needs. The window handed to the library must be
#: one bar longer than this, because ``Ctx.has(k)`` requires ``w >= k + 1``.
LIBRARY_LOOKBACK = 252

#: Bars actually fetched per (symbol, as_of) for the library.
LIBRARY_WINDOW = LIBRARY_LOOKBACK + 1

#: Trailing annualisation factor for realised volatility.
TRADING_DAYS = 252


@dataclass
class Ctx:
    """One as-of cross-section, as aligned (n_symbols, W) matrices."""

    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    mkt: np.ndarray  # (W,) market log returns, broadcast against rows

    @property
    def n(self) -> int:
        return self.c.shape[0]

    @property
    def w(self) -> int:
        return self.c.shape[1]

    def ret(self, k: int) -> np.ndarray:
        """Simple k-session return ending at the as-of session."""
        return self.c[:, -1] / self.c[:, -1 - k] - 1.0

    def logret(self, k: int) -> np.ndarray:
        return np.log(self.c[:, -1] / self.c[:, -1 - k])

    def d(self, k: int) -> np.ndarray:
        """Trailing k log returns, shape (n, k)."""
        full = np.diff(np.log(self.c), axis=1)
        return full[:, -k:]

    def hv(self, k: int) -> np.ndarray:
        """Annualised realised volatility over the trailing k sessions."""
        return np.sqrt(np.nanmean(self.d(k) ** 2, axis=1) * TRADING_DAYS)

    def ma(self, k: int) -> np.ndarray:
        return np.nanmean(self.c[:, -k:], axis=1)

    def dollar(self, k: int) -> np.ndarray:
        return np.nanmean(self.c[:, -k:] * self.v[:, -k:], axis=1)

    def has(self, k: int) -> bool:
        return self.w >= k + 1


@dataclass
class Factor:
    name: str
    family: str
    formula: str
    fn: Callable[[Ctx], np.ndarray]
    sign_hint: int = 1
    note: str = ""


REGISTRY: List[Factor] = []


def register(name, family, formula, sign_hint=1, note=""):
    def deco(fn):
        REGISTRY.append(Factor(name, family, formula, fn, sign_hint, note))
        return fn

    return deco


# ---------------------------------------------------------------------------
# A. Short-horizon reversal and longer momentum
# ---------------------------------------------------------------------------


def _mk_mom(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        return ctx.ret(k)

    return fn


for _k in (1, 2, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120, 180, 250):
    _hint = -1 if _k <= 20 else 1
    _fam = "A_reversal" if _k <= 20 else "B_momentum"
    register(
        f"mom_{_k}",
        _fam,
        f"c[-1] / c[-1-{_k}] - 1",
        sign_hint=_hint,
        note="<=20d is treated as reversal, >20d as momentum" if _k in (20, 30) else "",
    )(  # noqa: E501
        _mk_mom(_k)
    )


@register(
    "mom_6m_skip_1m",
    "B_momentum",
    "c[-21] / c[-126] - 1",
    sign_hint=1,
    note="classic 6-1 momentum: skips the most recent month to avoid the "
    "one-month reversal contaminating it",
)
def _mom_6m_skip_1m(ctx: Ctx):
    if not ctx.has(126):
        return np.full(ctx.n, np.nan)
    return ctx.c[:, -21] / ctx.c[:, -126] - 1.0


@register(
    "mom_accel_20_60",
    "B_momentum",
    "(c[-1]/c[-21] - 1) - (c[-1]/c[-61] - 1)",
    note="short leg minus long leg; positive means the recent move is "
    "accelerating relative to the quarter",
)
def _mom_accel(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    return ctx.ret(20) - ctx.ret(60)


def _mk_ma_dist(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        ma = ctx.ma(k)
        return ctx.c[:, -1] / ma - 1.0

    return fn


for _k in (5, 10, 20, 50, 100, 200):
    register(f"ma_dist_{_k}", "B_momentum", f"c[-1] / mean(c[-{_k}:]) - 1")(  # noqa: E501
        _mk_ma_dist(_k)
    )


def _mk_ma_cross(fast: int, slow: int):
    def fn(ctx: Ctx):
        if not ctx.has(slow):
            return np.full(ctx.n, np.nan)
        return ctx.ma(fast) / ctx.ma(slow) - 1.0

    return fn


for _f, _s in ((5, 20), (10, 50), (20, 60), (50, 200)):
    register(
        f"ma_cross_{_f}_{_s}",
        "B_momentum",
        f"mean(c[-{_f}:]) / mean(c[-{_s}:]) - 1",
    )(_mk_ma_cross(_f, _s))


@register(
    "high_252_prox",
    "B_momentum",
    "c[-1] / max(c[-252:]) - 1",
    sign_hint=1,
    note="distance below the 52-week high; 0 means at the high",
)
def _high252(ctx: Ctx):
    if not ctx.has(252):
        return np.full(ctx.n, np.nan)
    return ctx.c[:, -1] / np.nanmax(ctx.c[:, -252:], axis=1) - 1.0


@register(
    "range_pos_252",
    "B_momentum",
    "(c[-1] - min(c[-252:])) / (max(c[-252:]) - min(c[-252:]))",
    note="position of the current close inside its own 52-week range, 0..1",
)
def _rangepos(ctx: Ctx):
    if not ctx.has(252):
        return np.full(ctx.n, np.nan)
    lo = np.nanmin(ctx.c[:, -252:], axis=1)
    hi = np.nanmax(ctx.c[:, -252:], axis=1)
    return np.where(hi > lo, (ctx.c[:, -1] - lo) / (hi - lo), np.nan)


@register(
    "low_252_prox",
    "B_momentum",
    "c[-1] / min(c[-252:]) - 1",
    sign_hint=1,
    note="distance above the 52-week low",
)
def _low252(ctx: Ctx):
    if not ctx.has(252):
        return np.full(ctx.n, np.nan)
    return ctx.c[:, -1] / np.nanmin(ctx.c[:, -252:], axis=1) - 1.0


# ---------------------------------------------------------------------------
# C. Volatility
# ---------------------------------------------------------------------------


def _mk_hv(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        return ctx.hv(k)

    return fn


for _k in (5, 10, 20, 60, 120, 250):
    # The low-volatility anomaly: LOW realised vol predicts HIGHER returns.
    register(f"hv_{_k}", "C_volatility", f"sqrt(mean(d[-{_k}:]**2) * 252)", sign_hint=-1)(  # noqa: E501
        _mk_hv(_k)
    )


def _mk_parkinson(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        hl = np.log(ctx.h[:, -k:] / ctx.l[:, -k:]) ** 2
        return np.sqrt(np.nanmean(hl, axis=1) / (4.0 * math.log(2.0)) * TRADING_DAYS)

    return fn


for _k in (20, 60, 120):
    register(
        f"parkinson_{_k}",
        "C_volatility",
        f"sqrt(mean(log(h[-{_k}:]/l[-{_k}:])**2) / (4*ln2) * 252)",
        sign_hint=-1,
        note="range-based volatility; uses only high/low, so it is far less "
        "noisy than close-to-close realised vol on the same window",
    )(_mk_parkinson(_k))


def _mk_garman_klass(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        h, l, c = ctx.h[:, -k:], ctx.l[:, -k:], ctx.c[:, -k:]
        term = 0.5 * np.log(h / l) ** 2 - (2.0 * math.log(2.0) - 1.0) * np.log(c / l) ** 2
        return np.sqrt(np.nanmean(term, axis=1) * TRADING_DAYS)

    return fn


for _k in (20, 60, 120):
    register(
        f"gkvol_{_k}",
        "C_volatility",
        f"sqrt(mean(0.5*log(h/l)**2 - (2*ln2-1)*log(c/l)**2, {_k}) * 252)",
        sign_hint=-1,
        note="Garman-Klass: Parkinson corrected for the drift induced by "
        "closing prices inside the range",
    )(_mk_garman_klass(_k))


@register(
    "yzvol_60",
    "C_volatility",
    "Yang-Zhang overnight+open-to-close variance over 60d, annualised",
    sign_hint=-1,
    note="the most efficient daily volatility estimator available from OHLC; "
    "decomposes overnight gap risk from intraday risk",
)
def _yzvol(ctx: Ctx):
    k = 60
    if not ctx.has(k + 1):
        return np.full(ctx.n, np.nan)
    o, h, l, c = ctx.o, ctx.h, ctx.l, ctx.c
    oo = o[:, -(k + 1) : -1]
    cc = c[:, -(k + 1) : -1]
    h2, l2 = h[:, -k:], l[:, -k:]
    c2, o2 = c[:, -k:], o[:, -k:]
    # Overnight component
    sigma_o = np.nanvar(np.log(oo / cc), axis=1, ddof=1)
    # Open-to-close component
    sigma_c = np.nanvar(np.log(c2 / o2), axis=1, ddof=1)
    # Rogers-Satchell intraday component
    rs = np.log(h2 / c2) * np.log(h2 / o2) + np.log(l2 / c2) * np.log(l2 / o2)
    sigma_rs = np.nanmean(rs, axis=1)
    # Elementwise guard, not the builtin max(): sigma_rs is a vector, and
    # max(v, scalar) on an array raises "truth value is ambiguous".
    denom = np.maximum(sigma_rs * 1e4, 1e-12)
    kappa = 0.34 / (1.34 + (sigma_o + sigma_c) / denom)
    kappa = np.clip(kappa, 0.0, 1.0)
    var = sigma_o + kappa * sigma_c + (1.0 - kappa) * sigma_rs
    return np.sqrt(np.maximum(var, 0.0) * TRADING_DAYS)


@register(
    "hv_ratio_20_60",
    "C_volatility",
    "HV(20) / HV(60)",
    note="volatility expansion: recent vol relative to the quarter. Rising "
    "volatility is classically negative for returns",
    sign_hint=-1,
)
def _hvratio(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    a, b = ctx.hv(20), ctx.hv(60)
    return np.where(b > 0, a / b, np.nan)


@register(
    "vol_of_vol_60",
    "C_volatility",
    "std(d[-60:]) / mean(|d[-60:]|) * sqrt(60)",
    note="dispersion of daily return magnitudes; high values mean a few "
    "large moves dominate the window",
)
def _volofvol(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(60)
    denom = np.nanmean(np.abs(dd), axis=1)
    return np.where(denom > 0, np.nanstd(dd, axis=1, ddof=1) / denom * math.sqrt(60), np.nan)


@register(
    "downside_upside_60",
    "C_volatility",
    "std(d[d<0]) / std(d[d>0]) over 60d",
    note="asymmetry of return magnitudes. >1 means downside moves are larger",
    sign_hint=-1,
)
def _downup(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(60)
    with np.errstate(invalid="ignore"):
        down = np.nanstd(np.where(dd < 0, dd, np.nan), axis=1)
        up = np.nanstd(np.where(dd > 0, dd, np.nan), axis=1)
    return np.where(up > 0, down / up, np.nan)


@register(
    "hl_range_20",
    "C_volatility",
    "mean((h[-20:] - l[-20:]) / c[-21:-1])",
    note="average intraday range relative to the prior close; a direct "
    "liquidity/friction proxy",
)
def _hlrange(ctx: Ctx):
    if not ctx.has(21):
        return np.full(ctx.n, np.nan)
    rng = (ctx.h[:, -20:] - ctx.l[:, -20:]) / ctx.c[:, -21:-1]
    return np.nanmean(rng, axis=1)


# ---------------------------------------------------------------------------
# D. Liquidity
# ---------------------------------------------------------------------------


def _mk_adv(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.log(np.maximum(ctx.dollar(k), 1.0))

    return fn


for _k in (5, 20, 60):
    # The liquidity premium: MORE traded value predicts HIGHER returns.
    register(
        f"adv_{_k}",
        "D_liquidity",
        f"log(mean(c[-{_k}:] * v[-{_k}:]))",
        sign_hint=1,
    )(_mk_adv(_k))


@register(
    "adv_z_252",
    "D_liquidity",
    "(ADV(20) - mean(ADV(20) over 252d)) / std(ADV(20) over 252d)",
    note="a stock's current liquidity versus its OWN history, which strips "
    "out the permanent size differences between a large and a small issuer",
)
def _advz(ctx: Ctx):
    if not ctx.has(252):
        return np.full(ctx.n, np.nan)
    dollar = ctx.c * ctx.v
    adv20 = np.nanmean(dollar[:, -20:], axis=1)
    hist = np.stack(
        [np.nanmean(dollar[:, -20 - s : -s if s else None], axis=1) for s in range(0, 252, 5)],
        axis=1,
    )
    mu = np.nanmean(hist, axis=1)
    sd = np.nanstd(hist, axis=1, ddof=1)
    return np.where(sd > 0, (adv20 - mu) / sd, np.nan)


@register(
    "adv_trend_20_60",
    "D_liquidity",
    "mean(c[-20:]*v[-20:]) / mean(c[-60:]*v[-60:]) - 1",
    sign_hint=1,
    note="is traded value rising relative to the quarter",
)
def _advtrend(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    a, b = ctx.dollar(20), ctx.dollar(60)
    return np.where(b > 0, a / b - 1.0, np.nan)


@register(
    "amihud_20",
    "D_liquidity",
    "mean(|d[-20:]| / (c*v)[-20:]) * 1e6",
    sign_hint=-1,
    note="Amihud illiquidity: price impact per dollar traded. The canonical "
    "illiquidity measure; higher means less liquid, which predicts LOWER returns",
)
def _amihud(ctx: Ctx):
    if not ctx.has(20):
        return np.full(ctx.n, np.nan)
    dollar = ctx.c[:, -20:] * ctx.v[:, -20:]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.nanmean(np.abs(ctx.d(20)) / np.maximum(dollar, 1.0), axis=1) * 1e6


@register(
    "amihud_60",
    "D_liquidity",
    "mean(|d[-60:]| / (c*v)[-60:]) * 1e6",
    sign_hint=-1,
)
def _amihud60(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    dollar = ctx.c[:, -60:] * ctx.v[:, -60:]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.nanmean(np.abs(ctx.d(60)) / np.maximum(dollar, 1.0), axis=1) * 1e6


@register(
    "zero_volume_days_60",
    "D_liquidity",
    "count(v[-60:] == 0) over 60d",
    sign_hint=-1,
    note="sessions with no reported volume in the window; a stale or "
    "effectively untraded instrument",
)
def _zerovol(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    return (ctx.v[:, -60:] == 0).sum(axis=1).astype(float)


@register(
    "amihud_ratio_20_252",
    "D_liquidity",
    "amihud_20 / median(amihud_20 over 252d) - 1",
    sign_hint=-1,
    note="current price impact versus the stock's own one-year norm",
)
def _amihudratio(ctx: Ctx):
    if not ctx.has(252):
        return np.full(ctx.n, np.nan)
    dollar = ctx.c * ctx.v
    num = np.abs(np.diff(np.log(ctx.c), axis=1))
    amihud = np.nanmean(num / np.maximum(dollar[:, 1:], 1.0), axis=1)
    cur = np.nanmean(num[:, -20:] / np.maximum(dollar[:, -20:], 1.0), axis=1)
    hist = np.stack(
        [np.nanmean(num[:, -20 - s : -s if s else None], axis=1)
         / np.maximum(np.nanmean(dollar[:, -20 - s : -s if s else None], axis=1), 1.0)
         for s in range(0, 252, 10)],
        axis=1,
    )
    med = np.nanmedian(hist, axis=1)
    return np.where(med > 0, cur / med - 1.0, np.nan)


# ---------------------------------------------------------------------------
# E. Volume / price interaction
# ---------------------------------------------------------------------------


@register(
    "volume_ratio_5_20",
    "E_volume",
    "mean(v[-5:]) / mean(v[-20:]) - 1",
    sign_hint=1,
    note="production's volume_ratio; rising volume is associated with "
    "higher subsequent returns",
)
def _vr520(ctx: Ctx):
    if not ctx.has(20):
        return np.full(ctx.n, np.nan)
    a, b = np.nanmean(ctx.v[:, -5:], axis=1), np.nanmean(ctx.v[:, -20:], axis=1)
    return np.where(b > 0, a / b - 1.0, np.nan)


@register(
    "volume_ratio_20_60",
    "E_volume",
    "mean(v[-20:]) / mean(v[-60:]) - 1",
    sign_hint=1,
)
def _vr2060(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    a, b = np.nanmean(ctx.v[:, -20:], axis=1), np.nanmean(ctx.v[:, -60:], axis=1)
    return np.where(b > 0, a / b - 1.0, np.nan)


@register(
    "volume_ratio_60_250",
    "E_volume",
    "mean(v[-60:]) / mean(v[-250:]) - 1",
    sign_hint=1,
)
def _vr60250(ctx: Ctx):
    if not ctx.has(250):
        return np.full(ctx.n, np.nan)
    a, b = np.nanmean(ctx.v[:, -60:], axis=1), np.nanmean(ctx.v[:, -250:], axis=1)
    return np.where(b > 0, a / b - 1.0, np.nan)


def _mk_obv_slope(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k + 1):
            return np.full(ctx.n, np.nan)
        sign = np.sign(np.diff(ctx.c[:, -(k + 1) :], axis=1))
        obv = np.cumsum(sign * ctx.v[:, -k:], axis=1)
        t = np.arange(k, dtype=float)
        t = (t - t.mean()) / (np.sqrt((t**2).sum()) or 1.0)
        return np.nansum(obv, axis=1) / (np.nanmean(ctx.v[:, -k:], axis=1) * math.sqrt(k))

    return fn


for _k in (10, 20, 60):
    register(
        f"obv_slope_{_k}",
        "E_volume",
        f"standardised slope of cumulative signed volume over {_k}d",
        sign_hint=1,
        note="on-balance volume trend, normalised so it is comparable across "
        "a $1M and a $1B stock",
    )(_mk_obv_slope(_k))


def _mk_signed_vol(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        dd = ctx.d(k)
        vol = ctx.v[:, -k:]
        num = np.nansum(np.sign(dd) * vol, axis=1)
        den = np.nansum(vol, axis=1)
        return np.where(den > 0, num / den, np.nan)

    return fn


for _k in (5, 20, 60):
    register(
        f"signed_vol_{_k}",
        "E_volume",
        f"sum(sign(d[-{_k}:]) * v[-{_k}:]) / sum(v[-{_k}:])",
        sign_hint=1,
        note="net buying pressure: the share of volume traded on up-closes "
        "minus the share on down-closes",
    )(_mk_signed_vol(_k))


@register(
    "mfi_14",
    "E_volume",
    "Money Flow Index over 14d: 100 - 100/(1 + sum(typical*volume up / sum(typical*volume down))",
    sign_hint=1,
)
def _mfi(ctx: Ctx):
    k = 14
    if not ctx.has(k + 1):
        return np.full(ctx.n, np.nan)
    tp = (ctx.h[:, -k:] + ctx.l[:, -k:] + ctx.c[:, -k:]) / 3.0
    flow = tp * ctx.v[:, -k:]
    # d has exactly k elements aligned index-for-index with flow: d[j] is the
    # return of the session that produced tp[j]. Slicing flow[:, 1:] here
    # (an earlier revision) broadcast a (n,14) against a (n,13) and threw.
    d = np.diff(ctx.c[:, -(k + 1) :], axis=1)
    up = np.where(d > 0, flow, 0.0)
    dn = np.where(d < 0, flow, 0.0)
    pos, neg = np.nansum(up, axis=1), np.nansum(dn, axis=1)
    return np.where(neg > 0, 100.0 - 100.0 / (1.0 + pos / neg), np.where(pos > 0, 100.0, np.nan))


@register(
    "price_vol_corr_20",
    "E_volume",
    "corr(d[-20:], v[-20:] / mean(v[-20:]) - 1) over 20d",
    note="do price moves and volume moves happen together? a persistent "
    "positive value implies informed flow rather than noise trading",
)
def _pvcorr(ctx: Ctx):
    if not ctx.has(21):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(20)
    vv = ctx.v[:, -20:] / np.maximum(np.nanmean(ctx.v[:, -20:], axis=1, keepdims=True), 1.0) - 1.0
    dd = dd - np.nanmean(dd, axis=1, keepdims=True)
    vv = vv - np.nanmean(vv, axis=1, keepdims=True)
    num = np.nansum(dd * vv, axis=1)
    den = np.sqrt(np.nansum(dd**2, axis=1) * np.nansum(vv**2, axis=1))
    return np.where(den > 0, num / den, np.nan)


@register(
    "dollar_vol_boost_5",
    "E_volume",
    "(c*v)[-1] / mean((c*v)[-20:]) - 1",
    sign_hint=1,
    note="today's traded value against its own 20-day norm",
)
def _dvb(ctx: Ctx):
    if not ctx.has(20):
        return np.full(ctx.n, np.nan)
    dv = ctx.c * ctx.v
    a = dv[:, -1]
    b = np.nanmean(dv[:, -20:], axis=1)
    return np.where(b > 0, a / b - 1.0, np.nan)


# ---------------------------------------------------------------------------
# F. Intraday / microstructure
# ---------------------------------------------------------------------------


@register(
    "close_pos_1",
    "F_microstructure",
    "(c[-1] - o[-1]) / (h[-1] - l[-1])",
    note="where the close landed inside the as-of day's range. Near +1 means "
    "the session ended on its high",
)
def _cp1(ctx: Ctx):
    rng = ctx.h[:, -1] - ctx.l[:, -1]
    return np.where(rng > 0, (ctx.c[:, -1] - ctx.o[:, -1]) / rng, np.nan)


@register(
    "close_pos_20",
    "F_microstructure",
    "mean over 20d of (c - o) / (h - l)",
)
def _cp20(ctx: Ctx):
    if not ctx.has(20):
        return np.full(ctx.n, np.nan)
    rng = ctx.h[:, -20:] - ctx.l[:, -20:]
    with np.errstate(divide="ignore", invalid="ignore"):
        pos = np.where(rng > 0, (ctx.c[:, -20:] - ctx.o[:, -20:]) / np.where(rng > 0, rng, np.nan), np.nan)
    return np.nanmean(pos, axis=1)


@register(
    "gap_20",
    "F_microstructure",
    "mean(o[-20:-1] / c[-21:-2] - 1) over 20d",
    note="average overnight gap. Persistent positive gaps indicate a stock "
    "that tends to be bought before the open",
)
def _gap20(ctx: Ctx):
    if not ctx.has(21):
        return np.full(ctx.n, np.nan)
    g = ctx.o[:, -20:] / ctx.c[:, -21:-1] - 1.0
    return np.nanmean(g, axis=1)


@register(
    "intraday_20",
    "F_microstructure",
    "mean(c[-20:] / o[-20:] - 1) over 20d",
    note="average open-to-close move",
)
def _intra20(ctx: Ctx):
    if not ctx.has(20):
        return np.full(ctx.n, np.nan)
    return np.nanmean(ctx.c[:, -20:] / ctx.o[:, -20:] - 1.0, axis=1)


@register(
    "overnight_minus_intraday_20",
    "F_microstructure",
    "gap_20 - intraday_20",
    note="does the stock move more overnight than during the session? a "
    "preference for one or the other is a behavioural signature",
)
def _ominus(ctx: Ctx):
    if not ctx.has(21):
        return np.full(ctx.n, np.nan)
    g = np.nanmean(ctx.o[:, -20:] / ctx.c[:, -21:-1] - 1.0, axis=1)
    i = np.nanmean(ctx.c[:, -20:] / ctx.o[:, -20:] - 1.0, axis=1)
    return g - i


@register(
    "overnight_vol_60",
    "F_microstructure",
    "std(log(o[-60:-1] / c[-61:-2])) * sqrt(252)",
    sign_hint=-1,
    note="volatility of the overnight gap alone",
)
def _onvol(ctx: Ctx):
    if not ctx.has(61):
        return np.full(ctx.n, np.nan)
    return np.nanstd(np.log(ctx.o[:, -60:] / ctx.c[:, -61:-1]), axis=1, ddof=1) * math.sqrt(TRADING_DAYS)


@register(
    "intraday_share_60",
    "F_microstructure",
    "std(log(c/o)[-60:]) / std(log(c/o[-61:-1]) + log(c[-60:]/o[-60:]))",
    note="the fraction of total volatility arising during the session rather "
    "than overnight. High values mean intraday-driven stocks",
)
def _ishare(ctx: Ctx):
    if not ctx.has(61):
        return np.full(ctx.n, np.nan)
    intraday = np.log(ctx.c[:, -60:] / ctx.o[:, -60:])
    overnight = np.log(ctx.o[:, -60:] / ctx.c[:, -61:-1])
    total = np.log(ctx.c[:, -60:] / ctx.c[:, -61:-1])
    si = np.nanstd(intraday, axis=1, ddof=1)
    st = np.nanstd(total, axis=1, ddof=1)
    return np.where(st > 0, si / st, np.nan)


# ---------------------------------------------------------------------------
# G. Market-relative risk
# ---------------------------------------------------------------------------


def _mk_market(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        dd = ctx.d(k)
        m = ctx.mkt[-k:][None, :]
        m = m - np.nanmean(m, axis=1, keepdims=True)
        dd = dd - np.nanmean(dd, axis=1, keepdims=True)
        cov = np.nansum(dd * m, axis=1)
        var_m = np.nansum(m * m, axis=1)
        return np.where(var_m > 0, cov / var_m, np.nan)

    return fn


for _k in (20, 60, 120):
    register(
        f"beta_{_k}",
        "G_market_relative",
        f"cov(d[-{_k}:], mkt[-{_k}:]) / var(mkt[-{_k}:])",
        sign_hint=-1,
        note="market sensitivity. Low beta predicts higher returns "
        "(the betting-against-beta effect)",
    )(_mk_market(_k))


@register(
    "corr_mkt_60",
    "G_market_relative",
    "corr(d[-60:], mkt[-60:])",
    note="how tightly the stock moves WITH the market, independent of how "
    "much it moves. High values mean the stock is a market proxy",
)
def _corrmkt(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(60)
    m = ctx.mkt[-60:][None, :]
    m = m - np.nanmean(m, axis=1, keepdims=True)
    dd = dd - np.nanmean(dd, axis=1, keepdims=True)
    den = np.sqrt(np.nansum(dd**2, axis=1) * np.nansum(m * m, axis=1))
    return np.where(den > 0, np.nansum(dd * m, axis=1) / den, np.nan)


@register(
    "idio_vol_60",
    "G_market_relative",
    "std(d[-60:] - beta_60 * mkt[-60:]) * sqrt(252)",
    sign_hint=-1,
    note="volatility not explained by the market. This is what a diversified "
    "investor actually bears, and it is the cleaner risk measure",
)
def _idio(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(60)
    m = ctx.mkt[-60:][None, :]
    m = m - np.nanmean(m, axis=1, keepdims=True)
    dd_c = dd - np.nanmean(dd, axis=1, keepdims=True)
    var_m = np.nansum(m * m, axis=1)
    beta = np.where(var_m > 0, np.nansum(dd_c * m, axis=1) / np.where(var_m > 0, var_m, np.nan), np.nan)
    resid = dd - np.where(np.isfinite(beta), beta, 0.0)[:, None] * m
    return np.sqrt(np.nanvar(resid, axis=1, ddof=1) * TRADING_DAYS)


@register(
    "r2_mkt_60",
    "G_market_relative",
    "corr(d[-60:], mkt[-60:]) ** 2",
    note="share of the stock's 60d variance that is market variance",
)
def _r2(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(60)
    m = ctx.mkt[-60:][None, :]
    m = m - np.nanmean(m, axis=1, keepdims=True)
    dd = dd - np.nanmean(dd, axis=1, keepdims=True)
    den = np.sqrt(np.nansum(dd**2, axis=1) * np.nansum(m * m, axis=1))
    corr = np.where(den > 0, np.nansum(dd * m, axis=1) / np.where(den > 0, den, np.nan), np.nan)
    return corr**2

# NOTE: a "market's own trailing return" probe was removed from this registry.
# A cross-sectional IC is a rank correlation WITHIN one date, so a factor that
# is constant across symbols on a given date has zero cross-sectional variance
# and its Spearman IC is NaN by construction. It is a legitimate TIMING signal,
# but testing timing needs a time-series design, not this one. Worse, its all-NaN
# row and column silently removed 101 of the 102 pairwise correlations and
# destroyed the PSD structure of the correlation matrix, which corrupted the
# effective-number-of-tests estimate (N_eff). One constant factor, one wrong
# headline number.


@register(
    "max_drawdown_60",
    "G_market_relative",
    "min over t of c[t] / c[0] - 1, trailing 60d",
    sign_hint=1,
    note="worst peak-to-trough decline inside the window",
)
def _maxdd(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    seg = ctx.c[:, -60:]
    run = np.maximum.accumulate(seg, axis=1)
    return np.nanmin(seg / run - 1.0, axis=1)


@register(
    "max_runup_60",
    "G_market_relative",
    "max over t of c[t] / c[0] - 1, trailing 60d",
)
def _maxru(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    seg = ctx.c[:, -60:]
    return np.nanmax(seg / seg[:, :1] - 1.0, axis=1)


# ---------------------------------------------------------------------------
# H. Higher moments and tail shape
# ---------------------------------------------------------------------------


def _mk_skew(k: int):
    def fn(ctx: Ctx):
        if not ctx.has(k):
            return np.full(ctx.n, np.nan)
        dd = ctx.d(k)
        mu = np.nanmean(dd, axis=1, keepdims=True)
        sd = np.nanstd(dd, axis=1, ddof=1, keepdims=True)
        z = (dd - mu) / np.where(sd > 0, sd, np.nan)
        return np.nanmean(z**3, axis=1)

    return fn


for _k in (20, 60, 120):
    register(
        f"skew_{_k}",
        "H_moments",
        f"third standardised moment of d[-{_k}:]",
        sign_hint=-1,
        note="negative skew means a history of small gains punctuated by rare "
        "large losses, which historically predicts LOWER returns",
    )(_mk_skew(_k))


@register(
    "kurt_60",
    "H_moments",
    "fourth standardised moment of d[-60:]",
    note="tail heaviness",
)
def _kurt(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(60)
    mu = np.nanmean(dd, axis=1, keepdims=True)
    sd = np.nanstd(dd, axis=1, ddof=1, keepdims=True)
    z = (dd - mu) / np.where(sd > 0, sd, np.nan)
    return np.nanmean(z**4, axis=1)


@register(
    "skew_over_vol_60",
    "H_moments",
    "skew_60 / HV(60)",
    note="skew normalised by volatility, so a high value is unusual "
    "asymmetry rather than merely a volatile stock",
    sign_hint=-1,
)
def _skewvol(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(60)
    mu = np.nanmean(dd, axis=1, keepdims=True)
    sd = np.nanstd(dd, axis=1, ddof=1, keepdims=True)
    z = (dd - mu) / np.where(sd > 0, sd, np.nan)
    sk = np.nanmean(z**3, axis=1)
    vol = ctx.hv(60)
    return np.where(vol > 0, sk / vol, np.nan)


@register(
    "tail_ratio_120",
    "H_moments",
    "(p95 - p50) / (p50 - p05) of d[-120:]",
    note="is the right tail fatter than the left? a lottery-like payoff shape",
)
def _tailratio(ctx: Ctx):
    if not ctx.has(120):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(120)
    p05 = np.nanquantile(dd, 0.05, axis=1)
    p50 = np.nanquantile(dd, 0.50, axis=1)
    p95 = np.nanquantile(dd, 0.95, axis=1)
    den = p50 - p05
    return np.where(den > 0, (p95 - p50) / den, np.nan)


@register(
    "up_days_60",
    "H_moments",
    "mean(d[-60:] > 0)",
    sign_hint=1,
)
def _updays(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    return np.nanmean(ctx.d(60) > 0, axis=1)


@register(
    "max_ret_60",
    "H_moments",
    "max(d[-60:])",
    sign_hint=-1,
    note="a single very large up-move in the window. Attention-grabbing moves "
    "tend to reverse",
)
def _maxret(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    return np.nanmax(ctx.d(60), axis=1)


@register(
    "min_ret_60",
    "H_moments",
    "min(d[-60:])",
    sign_hint=1,
    note="a single very large down-move. Sharp drops tend to bounce",
)
def _minret(ctx: Ctx):
    if not ctx.has(60):
        return np.full(ctx.n, np.nan)
    return np.nanmin(ctx.d(60), axis=1)


@register(
    "consec_up_20",
    "H_moments",
    "current run length of consecutive up sessions, capped at 20",
)
def _consec(ctx: Ctx):
    if not ctx.has(20):
        return np.full(ctx.n, np.nan)
    dd = ctx.d(20) > 0
    # count trailing Trues
    run = np.zeros(ctx.n)
    alive = np.ones(ctx.n, dtype=bool)
    for j in range(dd.shape[1] - 1, -1, -1):
        run = run + (dd[:, j] & alive)
        alive = alive & dd[:, j]
    return run


@register(
    "pct_up_days_20",
    "H_moments",
    "mean(d[-20:] > 0)",
    sign_hint=1,
)
def _pctup(ctx: Ctx):
    if not ctx.has(20):
        return np.full(ctx.n, np.nan)
    return np.nanmean(ctx.d(20) > 0, axis=1)


# ---------------------------------------------------------------------------
# I. Public API
# ---------------------------------------------------------------------------


def factor_names() -> List[str]:
    return [f.name for f in REGISTRY]


_LOOKBACK_CACHE: Dict[str, int] = {}


def _probe_window() -> Tuple[np.ndarray, ...]:
    """A synthetic panel long enough to probe every factor's history need."""
    rng = np.random.default_rng(20260927)
    n, w = 8, LIBRARY_WINDOW + 8
    # A random walk with a mild upward drift. Realised volatility and the
    # price level stay in ranges where no factor degenerates to all-NaN for a
    # reason unrelated to history length (e.g. a zero denominator).
    c = 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, (n, w)), axis=1))
    o = c * np.exp(rng.normal(0.0, 0.003, (n, w)))
    h = np.maximum(o, c) * 1.01
    l = np.minimum(o, c) * 0.99
    v = rng.lognormal(15.0, 1.0, (n, w))
    mkt = np.diff(np.log(np.nanmean(c, axis=0)), prepend=np.log(np.nanmean(c[:, 0])))
    return o, h, l, c, v, mkt


def lookback_of(name: str) -> int:
    """Fewest sessions of history this factor needs, derived by measurement.

    Declaring this by hand next to each formula is a 94-entry chance to be
    wrong, and being wrong is silent: too small and a factor reads stale data,
    too large and a recent IPO is needlessly dropped from it. So it is probed
    instead -- shrink the window until the factor stops producing a value.

    Returns the number of sessions of history required, i.e. the same ``k``
    that appears in the formula strings.
    """
    if _LOOKBACK_CACHE:
        return _LOOKBACK_CACHE[name]
    o, h, l, c, v, mkt = _probe_window()
    by_name = {f.name: f for f in REGISTRY}
    for f in REGISTRY:
        best = LIBRARY_LOOKBACK
        for width in range(3, LIBRARY_WINDOW + 1):
            ctx = Ctx(
                o=o[:, -width:], h=h[:, -width:], l=l[:, -width:],
                c=c[:, -width:], v=v[:, -width:], mkt=mkt[-width:],
            )
            try:
                with np.errstate(all="ignore"):
                    vals = np.asarray(f.fn(ctx), dtype=float)
            except Exception:  # noqa: BLE001
                best = width
                break
            if vals.size and np.isfinite(vals).any():
                best = max(0, width - 1)
                break
        _LOOKBACK_CACHE[f.name] = best
    if name not in by_name:
        raise KeyError(f"unknown factor {name!r}")
    return _LOOKBACK_CACHE[name]


def families() -> List[str]:
    seen: List[str] = []
    for f in REGISTRY:
        if f.family not in seen:
            seen.append(f.family)
    return seen


def compute_all(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    v: np.ndarray,
    mkt: np.ndarray,
) -> Tuple[Dict[str, np.ndarray], List[Tuple[str, str]]]:
    """Evaluate every registered factor on one cross-section.

    All factors are computed regardless of the window length; those needing
    more history than is available return all-NaN, which the caller drops
    per factor so that a short-history name cannot silently shrink the sample
    for everyone else.

    Returns ``(values, errors)``. ``errors`` lists ``(factor, message)`` for
    any factor that RAISED. A bare ``except`` that quietly substitutes NaN is
    how the two bugs in this module's first draft stayed invisible: a broken
    factor is indistinguishable from a factor with no signal, and an
    all-NaN series would have been reported as "no IC found" -- a clean-looking
    negative result that was actually a crash. The caller must log ``errors``.
    """
    ctx = Ctx(o=o, h=h, l=l, c=c, v=v, mkt=mkt)
    out: Dict[str, np.ndarray] = {}
    errors: List[Tuple[str, str]] = []
    with warnings.catch_warnings():
        # numpy's nan-functions emit RuntimeWarning on an all-NaN slice. That
        # is the INTENDED outcome for a symbol without enough trailing history
        # -- the caller drops it per factor via the run-length mask. Leaving
        # ~10 warnings per date on stderr would bury anything that matters, so
        # they are suppressed here. Suppression is warnings-only: the values
        # are still NaN and still dropped, never quietly replaced.
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(all="ignore"):
            for f in REGISTRY:
                try:
                    vals = np.asarray(f.fn(ctx), dtype=float).ravel()
                except Exception as exc:  # noqa: BLE001 - one bad factor must not kill the scan
                    errors.append((f.name, f"{type(exc).__name__}: {exc}"))
                    vals = np.full(ctx.n, np.nan)
                if vals.shape[0] != ctx.n:
                    errors.append((f.name, f"returned {vals.shape[0]} values, expected {ctx.n}"))
                    vals = np.full(ctx.n, np.nan)
                # infeasible values become NaN rather than poisoning a rank
                out[f.name] = np.where(np.isfinite(vals), vals, np.nan)
    return out, errors
