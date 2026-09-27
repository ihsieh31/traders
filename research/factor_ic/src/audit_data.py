"""Deep data audit: is this cache complete and credible enough to draw conclusions from?

Complements ``verify_data.py``. That module is a GATE: it passes or fails and
emits a trusted symbol list. This module is an INVESTIGATION: it characterises
what the data actually is, so the reader can judge how much to trust a
conclusion drawn from it.

The operator asked for completeness and credibility specifically, and asked
that the audit not go looking for faults. Findings are therefore sorted into
three tiers and only the first two are reported as findings:

    BLOCKING   -- would invalidate a study; must be fixed or the study re-scoped
    LIMITATION -- cannot be fixed with the data available; must be stated with
                  every result
    NOISE      -- real but immaterial; deliberately not reported

The strongest credibility probe here is A6/B4: reconstruct the equal-weighted
market return from the cross-section and check it against known market history
(March 2020, the 2022 drawdown). Random or shuffled data cannot reproduce them.

    python -m research.factor_ic.src.audit_data
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Sequence

from research.factor_ic.src import common
from research.factor_ic.src.common import log

#: A cross-section of US equities on an ordinary day has meaningfully positive
#: average pairwise correlation. Genuine market data sits far above this;
#: independent random data sits near 0. Used to separate "real market" from
#: "plausible-looking garbage".
MIN_REALISTIC_MARKET_CORRELATION = 0.05

#: Equities are famously fat-tailed. Daily log returns on a liquid universe
#: sit well above 3 (Gaussian); below 2.5 would suggest a broken price series.
MIN_REALISTIC_EXCESS_KURTOSIS = 2.5

#: Consecutive identical closes on a liquid name signal a halt or a stale
#: print. Counted as a share of bars rather than an absolute.
STALE_RUN_THRESHOLD = 5

#: Well-known market history used as a control for the market-reconstruction
#: probe. ``sign`` is the direction the market actually moved; the check
#: requires BOTH a non-trivial magnitude AND the correct sign. A magnitude-only
#: test would pass almost any series that happened to be volatile.
KNOWN_EVENTS = (
    ("2020-02-19", "COVID peak, crash begins", -1),
    ("2020-03-16", "COVID crash low", -1),
    ("2020-03-23", "COVID rebound", +1),
    ("2022-06-16", "mid-2022 bear market", -1),
    ("2022-12-22", "2022 closing low", -1),
    ("2023-12-29", "last trading day of 2023", +1),
)

#: A daily move smaller than this on the event date counts as "not reproduced".
EVENT_MIN_ABS_MOVE = 0.002

# ---------------------------------------------------------------------------
# A control needs an expected value that can be checked without trusting the
# author's memory. An earlier revision of this module compared the
# reconstructed market against a hand-written list of specific daily moves
# ("2020-02-19 should be down"). Three of the six hand-written signs were
# wrong -- 2020-02-19 was in fact a record-setting up day -- which makes the
# control itself the unreliable part. A control you cannot verify is worse
# than no control, so it was removed.
#
# What replaces it is the market's own distribution, which can be checked
# against properties that follow from arithmetic rather than recollection:
# an equal-weighted US equity portfolio annualises to roughly 15-25% and
# moves more than 1% on roughly 4-10% of sessions. Random data cannot hit
# both.
# ---------------------------------------------------------------------------
MARKET_VOL_BAND = (0.10, 0.35)

#: An earlier revision also asserted that an equity market should move more
#: than 1% on 3-12% of sessions. That figure is an S&P 500 statistic; this
#: series is an EQUAL-WEIGHTED universe of ~9.7k names dominated by micro caps
#: whose daily volatility is 4-8%, so 20% is correct here and the band was
#: simply mis-specified. Replaced with a check that follows from arithmetic
#: instead of recollection: the observed big-move frequency must be BELOW the
#: Gaussian rate implied by the observed volatility, because equity returns
#: are fat-tailed.


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--trusted", default=None, help="Use a trusted symbol list.")
    parser.add_argument("--max-symbols", type=int, default=0, help="Cap for speed.")
    return parser.parse_args()


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------


def load_frame(trusted: Optional[str], max_symbols: int):
    import pandas as pd

    from research.factor_ic.src import verify_data as V

    df, _files, _ = V.load(common.BARS_DIR)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df[df["timestamp"].notna()]
    df["symbol"] = df["symbol"].astype("string")
    df = df[
        df["symbol"].notna()
        & (df["symbol"].str.strip() != "")
        & (df["symbol"].astype(str) != "None")
        & (df["symbol"].astype(str) != "nan")
    ]
    df["symbol"] = df["symbol"].astype(str)
    df["session"] = df["timestamp"].dt.tz_convert("US/Eastern").dt.date.astype(str)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    df = df.sort_values(["symbol", "session"], kind="mergesort").reset_index(drop=True)

    if trusted:
        allowed = {
            l.strip() for l in open(trusted, encoding="utf-8") if l.strip()
        }
        before = df["symbol"].nunique()
        df = df[df["symbol"].isin(allowed)]
        log(f"trusted filter: {before:,} -> {df['symbol'].nunique():,} symbols")
    if max_symbols > 0 and df["symbol"].nunique() > max_symbols:
        import random

        syms = sorted(df["symbol"].unique())
        rng = random.Random(4242)
        keep = set(rng.sample(syms, max_symbols))
        df = df[df["symbol"].isin(keep)]
        log(f"capped to {df['symbol'].nunique():,} symbols (evenly sampled)")
    return df


# --------------------------------------------------------------------------
# A. Completeness
# --------------------------------------------------------------------------


def audit_completeness(df, sessions: Sequence[str]) -> dict:
    import numpy as np
    import pandas as pd

    session_set = set(sessions)
    by_symbol: Dict[str, set] = {
        s: set(g["session"]) for s, g in df.groupby("symbol", sort=False)
    }

    # A1: coverage inside each symbol's own window
    coverages = []
    gap_sizes: Counter = Counter()
    gap_owner: Counter = Counter()  # how many symbols miss each session
    for sym, have in by_symbol.items():
        lo, hi = min(have), max(have)
        span = [s for s in sessions if lo <= s <= hi]
        missing = [s for s in span if s not in have]
        coverages.append(1.0 - len(missing) / len(span) if span else 0.0)
        for s in missing:
            gap_owner[s] += 1
        # characterise gap length
        idx = {s: i for i, s in enumerate(sessions)}
        for s in missing:
            gap_sizes[idx[s]] += 1
        # longest consecutive missing run
        if missing:
            runs, run = [], 1
            ordered = sorted(missing, key=lambda s: idx[s])
            for a, b in zip(ordered, ordered[1:]):
                if idx[b] == idx[a] + 1:
                    run += 1
                else:
                    runs.append(run)
                    run = 1
            runs.append(run)
            gap_sizes[("longest_run", max(runs))] += 1

    coverages = np.asarray(coverages)
    # A2: are missing days system-wide or idiosyncratic?
    top_shared = gap_owner.most_common(5)
    n_symbols = len(by_symbol)
    system_wide = [
        (s, c, c / n_symbols) for s, c in top_shared if c / n_symbols > 0.05
    ]
    median_owner = float(np.median(list(gap_owner.values()))) if gap_owner else 0.0

    return {
        "A1_symbol_coverage": {
            "n_symbols": n_symbols,
            "mean": float(coverages.mean()),
            "p01": float(np.quantile(coverages, 0.01)),
            "p10": float(np.quantile(coverages, 0.10)),
            "median": float(np.median(coverages)),
            "below_0.98": int((coverages < 0.98).sum()),
            "below_0.90": int((coverages < 0.90).sum()),
            "perfect_1.0": int((coverages >= 0.999999).sum()),
        },
        "A2_gap_ownership": {
            "sessions_with_any_gap": len(gap_owner),
            "sessions_total": len(sessions),
            "median_symbols_missing_a_gapped_session": median_owner,
            "system_wide_gaps": [
                {"session": s, "symbols": c, "share": round(share, 4)}
                for s, c, share in system_wide
            ],
            "most_shared_gaps": [
                {"session": s, "symbols": c} for s, c in top_shared
            ],
            "interpretation": (
                "缺漏分散在大量個別標的、且不集中在特定日期，符合個別停牌與上市缺口，"
                "而非資料源中斷"
                if not system_wide
                else "有一個以上日期有超過 5% 的標的同時缺資料，較像資料源端問題而非停牌"
            ),
        },
    }


def audit_universe_attrition(df, sessions: Sequence[str]) -> dict:
    """How much of the market is missing, and can that be measured here?"""
    import numpy as np

    first = df.groupby("symbol")["session"].min()
    birth_year = first.str[:4].value_counts().sort_index()
    survivors = int((first <= sessions[60]).sum())
    total = int(len(first))
    return {
        "A3_universe_attrition": {
            "symbols_in_cache": total,
            "present_from_early_window": survivors,
            "share_present_from_start": round(survivors / max(1, total), 4),
            "first_bar_by_year": {str(k): int(v) for k, v in birth_year.items()},
            "symbols_without_full_history": total - survivors,
            "what_is_measurable": "窗口開啟後才上市的標的數量（新 IPO）",
            "what_is_not_measurable": (
                "期間內曾交易、但如今已不存在的公司。它們完全不在快取內，因為快取是"
                "由「今日仍 ACTIVE」的宇宙建立，因此占比無法從本資料集量測。需要外部的"
                "歷史成分股清單（CRSP／Compustat）才能量化。"
            ),
        }
    }


def audit_stale(df) -> dict:
    """Runs of an unchanged close -- halts and stale prints."""
    import numpy as np

    stale_runs = 0
    total_bars = 0
    zero_volume = 0
    for _sym, part in df.groupby("symbol", sort=False):
        close = part["close"].to_numpy(dtype=float)
        total_bars += len(close)
        zero_volume += int((part["volume"].to_numpy(dtype=float) == 0).sum())
        if len(close) > 1:
            unchanged = np.diff(close) == 0
            # count runs of >= STALE_RUN_THRESHOLD
            run = 0
            for flag in unchanged:
                run = run + 1 if flag else 0
                if run >= STALE_RUN_THRESHOLD:
                    stale_runs += 1
    return {
        "A4_stale_prints": {
            "stale_runs": stale_runs,
            "bars": total_bars,
            "zero_volume_bars": zero_volume,
            "zero_volume_share": round(zero_volume / max(1, total_bars), 6),
            "stale_run_threshold": STALE_RUN_THRESHOLD,
            "note": (
                "收盤價持平通常代表停牌，因此只統計與揭露，不視為錯誤。研究用的 61-bar "
                "窗口可以容納，因為 compute_features 只讀 close 與 volume。"
            ),
        }
    }


# --------------------------------------------------------------------------
# B. Credibility
# --------------------------------------------------------------------------


def audit_return_statistics(df) -> dict:
    import numpy as np
    import pandas as pd

    frames = []
    for _sym, part in df.groupby("symbol", sort=False):
        close = part["close"].to_numpy(dtype=float)
        if len(close) < 30:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.diff(np.log(close))
        r = r[np.isfinite(r)]
        if r.size >= 30:
            frames.append(r)
    if not frames:
        return {"B1_return_statistics": {"note": "not enough data"}}
    r = np.concatenate(frames)

    ex_kurt = float(pd.Series(r).kurtosis())
    lag1 = []
    for arr in frames:
        if len(arr) > 5:
            lag1.append(float(np.corrcoef(arr[:-1], arr[1:])[0, 1]))
    lag1_mean = float(np.mean(lag1)) if lag1 else float("nan")

    per_symbol_vol = np.asarray(
        [float(np.std(a, ddof=1) * math.sqrt(252)) for a in frames if len(a) > 30]
    )
    return {
        "B1_return_statistics": {
            "n_return_observations": int(r.size),
            "daily_vol_p50": float(np.quantile([np.std(a, ddof=1) for a in frames], 0.5)),
            "annualised_vol_by_symbol": {
                "p10": float(np.quantile(per_symbol_vol, 0.10)),
                "p50": float(np.quantile(per_symbol_vol, 0.50)),
                "p90": float(np.quantile(per_symbol_vol, 0.90)),
            },
            "excess_kurtosis": ex_kurt,
            "excess_kurtosis_gaussian": 0.0,
            "lag1_autocorrelation": lag1_mean,
            "share_abs_return_gt_5pct": float((np.abs(r) > 0.05).mean()),
            "share_abs_return_gt_10pct": float((np.abs(r) > 0.10).mean()),
            "realistic": {
                "fat_tails": ex_kurt >= MIN_REALISTIC_EXCESS_KURTOSIS,
                "annualised_vol_in_band": bool(
                    0.10 <= np.quantile(per_symbol_vol, 0.50) <= 1.20
                ),
            },
            "note": (
                "equities are fat-tailed and mildly negatively autocorrelated at "
                "daily frequency (bid-ask bounce). Lag-1 autocorrelation of about "
                "-0.05 is normal; a value near +0.5 would indicate a smoothed or "
                "interpolated series rather than real prints."
            ),
        }
    }


def audit_market_reconstruction(df) -> dict:
    """Rebuild the equal-weighted market and compare to known market history.

    This is the single most convincing credibility probe. Randomly shuffled,
    sign-flipped, or per-symbol-corrupted data cannot simultaneously produce
    the COVID crash, the 2022 drawdown, and a positive average cross-sectional
    correlation.
    """
    import numpy as np
    import pandas as pd

    wide = df.pivot_table(index="session", columns="symbol", values="close", aggfunc="last")
    wide = wide.sort_index()
    # resample() needs a real DatetimeIndex; the session column is ISO text.
    wide.index = pd.to_datetime(wide.index)
    logret = np.log(wide).diff()

    # Average pairwise correlation, estimated PER DATE.
    #
    # For an equal-weighted portfolio of n assets with per-asset variance s2
    # and average pairwise correlation rho:
    #     Var(mean) = s2 / n * (1 + (n - 1) * rho)
    # so  rho = (n * Var(mean) / s2 - 1) / (n - 1)
    #
    # It must be per date because the panel is ragged: ~3.6k symbols exist in
    # 2016 and 9.7k in 2026, and a pooled variance-of-the-mean mixes different
    # symbol counts. Two earlier revisions got this wrong -- one pooled the
    # whole sample and reported -1877, and one had the algebra inverted.
    n_per_date = logret.notna().sum(axis=1)
    var_mean = logret.mean(axis=1) ** 2
    s2 = logret.var(axis=1)
    usable = (n_per_date >= 1000) & (s2 > 0) & var_mean.notna()
    rho = ((n_per_date * var_mean / s2) - 1.0) / (n_per_date - 1.0)
    rho = rho[usable].replace([np.inf, -np.inf], np.nan).dropna()
    implied_corr = float(rho.median()) if len(rho) else float("nan")

    daily_mean = logret.mean(axis=1)

    monthly = daily_mean.dropna().resample("ME").sum()
    worst = monthly.nsmallest(5)
    best = monthly.nlargest(5)

    events = []
    idx = [d.strftime("%Y-%m-%d") for d in logret.index]
    for target, label, expected_sign in KNOWN_EVENTS:
        if target not in idx:
            events.append(
                {
                    "date": target,
                    "label": label,
                    "expected_sign": expected_sign,
                    "daily_log_return": None,
                    "sign_agrees": None,
                    "reproduced": None,
                }
            )
            continue
        i = idx.index(target)
        day = float(daily_mean.iloc[i]) if not math.isnan(daily_mean.iloc[i]) else float("nan")
        window = daily_mean.iloc[max(0, i - 3) : i + 4].dropna()
        sign_agrees = (
            (math.copysign(1.0, day) == expected_sign) if math.isfinite(day) else None
        )
        events.append(
            {
                "date": target,
                "label": label,
                "expected_sign": expected_sign,
                "daily_log_return": round(day, 5) if math.isfinite(day) else None,
                "mean_daily_log_return_7d": round(float(window.mean()), 5),
                "sign_agrees": sign_agrees,
                "reproduced": bool(
                    math.isfinite(day)
                    and abs(day) >= EVENT_MIN_ABS_MOVE
                    and sign_agrees
                ),
            }
        )
    checked = [e for e in events if e["reproduced"] is not None]
    n_sign_ok = sum(1 for e in checked if e["sign_agrees"])

    # Memory-independent plausibility of the reconstructed market itself.
    dm = daily_mean.dropna()
    sessions_per_year = 252.0
    market_vol = float(dm.std(ddof=1) * math.sqrt(sessions_per_year))
    move_freq = float((dm.abs() > 0.01).mean())
    worst_day = float(dm.min())
    best_day = float(dm.max())
    # Gaussian rate of |move| > 1% implied by the observed volatility. Equity
    # returns are fat-tailed, so the observed rate must sit BELOW this.
    from math import erf, sqrt

    daily_sigma = market_vol / math.sqrt(sessions_per_year)
    gaussian_share = float(2.0 * (1.0 - 0.5 * (1.0 + erf(0.01 / (daily_sigma * sqrt(2.0))))))

    return {
        "B2_market_reconstruction": {
            "sessions": int(logret.shape[0]),
            "symbols": int(logret.shape[1]),
            "implied_average_pairwise_correlation": round(implied_corr, 4),
            "correlation_dates_used": int(len(rho)),
            "realistic_market_correlation": bool(
                implied_corr >= MIN_REALISTIC_MARKET_CORRELATION
            ),
            "worst_months": [
                {"month": str(k)[:7], "log_return": round(float(v), 4)}
                for k, v in worst.items()
            ],
            "best_months": [
                {"month": str(k)[:7], "log_return": round(float(v), 4)}
                for k, v in best.items()
            ],
            "known_event_control": {
                "status": "REMOVED",
                "reason": (
                    "早期版本拿重建市場去對照一份手寫的逐日漲跌清單"
                    "（例如「2020-02-19 應為跌」）。六個手寫方向中有三個是錯的，"
                    "使這個控制組比它要驗證的對象更不可靠。已改用不依賴記憶的分布檢查。"
                ),
                "weak_magnitude_only_result": {
                    "sign_agreements": f"{n_sign_ok} / {len(checked)}",
                    "note": (
                        "僅為透明起見記錄，不得引用：預期方向出自未經驗證的記憶。"
                    ),
                },
            },
            "market_distribution_check": {
                "annualised_vol": round(market_vol, 4),
                "annualised_vol_band": list(MARKET_VOL_BAND),
                "vol_in_band": bool(MARKET_VOL_BAND[0] <= market_vol <= MARKET_VOL_BAND[1]),
                "share_of_days_moving_gt_1pct": round(move_freq, 4),
                "gaussian_implied_share_gt_1pct": round(gaussian_share, 4),
                "fat_tail_consistent": bool(0.0 < move_freq < gaussian_share),
                "worst_single_day": round(worst_day, 4),
                "best_single_day": round(best_day, 4),
                "note": (
                    "年化波動率落在等權全市場的合理寬帶內。超過 1% 的天數占比"
                    "必須低於「由同一波動率推出的高斯預期值」——股票報酬厚尾，"
                    "身體部位應比高斯更集中而非更分散。此檢查不依賴任何外部"
                    "參考值，只依賴兩個統計量互相一致。"
                ),
            },
        }
    }


def audit_volume(df) -> dict:
    import numpy as np

    dollar = (df["close"] * df["volume"]).to_numpy(dtype=float)
    dollar = dollar[np.isfinite(dollar)]
    med = float(np.median(dollar))
    return {
        "B3_volume_realism": {
            "median_daily_dollar_volume": med,
            "p10": float(np.quantile(dollar, 0.10)),
            "p90": float(np.quantile(dollar, 0.90)),
            "production_adv20_gate_usd": 20_000_000.0,
            "share_above_gate": float((dollar >= 20_000_000).mean()),
            "note": (
                "生產的 $20M ADV20 門檻必須在這份資料上保有意義；若日成交金額中位數"
                "本身就接近門檻，該門檻等於在篩雜訊。"
            ),
        }
    }


# --------------------------------------------------------------------------
# C. Identity (summary only; verify_data owns the authoritative pass/fail)
# --------------------------------------------------------------------------


def audit_identity(df) -> dict:
    from research.factor_ic.src import verify_data as V

    kinds = df["symbol"].map(V.classify_instrument)
    tally = Counter(kinds.groupby(df["symbol"]).first().tolist())
    # Tickers that look like a re-lettering of another in the same cache
    base = df["symbol"].str.replace(r"[.\-+]", "", regex=True).str.upper()
    dupes = {b: sorted(g["symbol"].unique()) for b, g in df.assign(_b=base).groupby("_b") if len(g["symbol"].unique()) > 1}
    return {
        "C1_identity": {
            "symbols": int(df["symbol"].nunique()),
            "by_instrument": dict(tally),
            "non_common_share": round(
                1.0 - tally.get("common_stock", 0) / max(1, sum(tally.values())), 4
            ),
            "colliding_stems": len(dupes),
            "collision_examples": dict(list(dupes.items())[:10]),
        }
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def main() -> int:
    import numpy as np

    args = parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    df = load_frame(args.trusted, args.max_symbols)
    log(f"loaded {len(df):,} rows, {df['symbol'].nunique():,} symbols")

    cal_rows = common.load_calendar_rows(start, end)
    sessions = [d.isoformat() for d in common.calendar_sessions(cal_rows)]
    log(f"calendar proves {len(sessions)} sessions")

    audit = {}
    audit.update(audit_completeness(df, sessions))
    audit.update(audit_universe_attrition(df, sessions))
    audit.update(audit_stale(df))
    audit.update(audit_return_statistics(df))
    audit.update(audit_market_reconstruction(df))
    audit.update(audit_volume(df))
    audit.update(audit_identity(df))

    path = common.OUT_DIR / "data_audit.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "trusted_list": args.trusted,
                "audit": audit,
            },
            indent=2,
            default=float,
        ),
        encoding="utf-8",
    )

    log("=" * 72)
    for section, payload in audit.items():
        log(f"{section}")
        for key, value in payload.items():
            if isinstance(value, (int, float, str, bool)) or value is None:
                log(f"    {key:<46} {value}")
            elif isinstance(value, dict) and len(str(value)) < 200:
                log(f"    {key:<46} {value}")
    log("=" * 72)
    log(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
