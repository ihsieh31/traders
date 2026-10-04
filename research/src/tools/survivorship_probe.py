"""S-52 probe: would production's Top-20 selector ever have bought a stock
that later delisted?

The question this answers is NOT "how big is the survivorship gap" (already
measured, plan.md 4.4 item 1). It is the narrower, decision-relevant one:
**does the gap touch the portfolio?** A dead name that production's
eligibility gate would have rejected anyway cannot bias a Top-20 book, no
matter how many of them are missing. A dead name that cleared the gate AND
reached the top of the score distribution could have been bought, and its
absence is a real hole.

Method
------
Replay the production pipeline verbatim on a synthetic date, with the
delisted name inserted into the real surviving cross-section:

  1. eligibility  (price, ADV20, required_bars)  -- production constants
  2. factors      (adv20, r20, r60, vol20, volume_ratio)
  3. score        (production SCORE_WEIGHTS over cross-sectional percentiles)
  4. rank         (descending, tie-broken by symbol as production does)

Percentiles are computed over the *surviving* eligible set, i.e. the world as
production actually saw it, then the dead name is scored against that
distribution. This is deliberately the **conservative** direction: the
survivors are already ranked without any dead competition, so any dead name
that still reaches the top-20 threshold here would have reached it in a world
that contained the other dead names too. If it fails here, it fails
everywhere.

The $300M market-cap gate is NOT applied -- see panel.py:41. There is no
point-in-time market cap in this dataset, and using today's cap to filter
historical bars is look-ahead (process.md S-35 / plan.md R7-L4). Applying it
would require historical share counts, which is a separate data problem.
This probe therefore measures the **ADV20-only** floor, which is strictly
weaker than the production floor, so it is an upper bound on how many dead
names could have been bought.

Read research/plan.md and research/process.md before changing anything here.
"""

from __future__ import annotations

import glob
import gzip
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

# --- production constants (tradingagents/screening/metrics.py) -------------
MIN_PRICE = 5.0
MIN_ADV20_USD = 20_000_000.0
REQUIRED_BARS = 61
TOP_K = 20
SCORE_WEIGHTS = {
    "adv20": 0.20,
    "r20": 0.25,
    "r60": 0.25,
    "vol20_inverse": 0.15,
    "volume_ratio": 0.15,
}
BARS_DIR = Path("research/out/20260928-external-anchor/tiingo/bars")
TRUSTED = Path("research/out/trusted_symbols.txt")


def load_dead_series(min_bars: int = REQUIRED_BARS) -> dict[str, pd.DataFrame]:
    """Every delisted series we actually hold, keyed by ticker."""
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(glob.glob(str(BARS_DIR / "*.gz"))):
        ticker = os.path.basename(path)[:-3]
        try:
            with gzip.open(path, "rt") as fh:
                df = pd.read_csv(fh)
        except Exception:
            continue
        if len(df) < min_bars:
            continue
        df = df.rename(columns={df.columns[0]: "date"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        close_col = next((c for c in df.columns if "close" in c.lower()), None)
        vol_col = next(
            (c for c in df.columns if c.lower() in ("volume", "vol")), None
        )
        if close_col is None or vol_col is None:
            continue
        df = df[["date", close_col, vol_col]].copy()
        df.columns = ["date", "close", "volume"]
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df.dropna().sort_values("date").reset_index(drop=True)
        if len(df) >= min_bars:
            out[ticker] = df
    return out


def factors_from_history(df: pd.DataFrame, i: int) -> dict | None:
    """Production factor row at bar ``i``, or None if the window is short.

    Mirrors compute_features(): the trailing REQUIRED_BARS window only, so
    nothing here can see a bar after ``i``.
    """
    if i < REQUIRED_BARS - 1:
        return None
    w = df.iloc[i - (REQUIRED_BARS - 1) : i + 1]
    if len(w) < REQUIRED_BARS or w["close"].isna().any():
        return None
    close = w["close"].to_numpy(dtype=float)
    volume = w["volume"].to_numpy(dtype=float)
    if close[-1] <= 0:
        return None
    dollar = close * volume
    # r5/r20/r60 need 60 prior closes for r60; the 61-bar window covers it.
    if len(close) < 61:
        return None
    r5 = close[-1] / close[-6] - 1.0
    r20 = close[-1] / close[-21] - 1.0
    r60 = close[-1] / close[-61] - 1.0
    rets = close[1:] / close[:-1] - 1.0
    tail = rets[-20:]
    vol20 = float(np.std(tail, ddof=1) * np.sqrt(252))
    adv20 = float(np.mean(dollar[-20:]))
    mean_vol20 = float(np.mean(volume[-20:]))
    # A zero 20-day mean volume makes volume_ratio undefined; production's
    # rolling window would carry a non-finite value, which the eligibility
    # path treats as unusable. Treat it as ineligible rather than inventing
    # a number (process.md S-9/S-23).
    if not np.isfinite(mean_vol20) or mean_vol20 <= 0:
        return None
    volume_ratio = float(np.mean(volume[-5:])) / mean_vol20
    if not np.isfinite(volume_ratio):
        return None
    return {
        "price": float(close[-1]),
        "adv20": adv20,
        "r20": r20,
        "r60": r60,
        "vol20": vol20,
        "volume_ratio": volume_ratio,
    }


def ascending_percentiles(values: list[float]) -> list[float]:
    """Verbatim port of production's ascending_percentiles (ties -> avg rank)."""
    n = len(values)
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return [(r - 1.0) / (n - 1.0) for r in ranks]


def production_score(rows: list[dict]) -> list[float]:
    """Production score over a cross-section of factor rows."""
    n = len(rows)
    if n == 0:
        return []
    pct = {}
    for key in ("adv20", "r20", "r60", "vol20", "volume_ratio"):
        pct[key] = (
            [0.5] * n if n == 1 else ascending_percentiles([r[key] for r in rows])
        )
    return [
        100.0
        * (
            SCORE_WEIGHTS["adv20"] * pct["adv20"][i]
            + SCORE_WEIGHTS["r20"] * pct["r20"][i]
            + SCORE_WEIGHTS["r60"] * pct["r60"][i]
            + SCORE_WEIGHTS["vol20_inverse"] * (1.0 - pct["vol20"][i])
            + SCORE_WEIGHTS["volume_ratio"] * pct["volume_ratio"][i]
        )
        for i in range(n)
    ]


def eligible(row: dict) -> bool:
    """Production eligibility, minus the market-cap gate we cannot apply."""
    if not np.isfinite(row["price"]) or row["price"] < MIN_PRICE:
        return False
    if not np.isfinite(row["adv20"]) or row["adv20"] < MIN_ADV20_USD:
        return False
    return True


def main() -> int:
    if not TRUSTED.exists():
        print(f"missing {TRUSTED}", file=sys.stderr)
        return 1
    dead = load_dead_series()
    if not dead:
        print("no delisted series with a full 61-bar window", file=sys.stderr)
        return 1
    print(f"delisted series with >= {REQUIRED_BARS} bars: {len(dead)}")

    # Survival cross-section: one representative date per dead name, drawn
    # from the surviving panel on the SAME calendar day. We use the panel's
    # own sessions so the comparison is like-for-like.
    from research.src import panel as rpanel

    # A single representative evaluation date keeps the cross-section
    # comparison honest; we probe each dead name near its own death and pair
    # it with the survivors alive on that same date.
    probe_dates = sorted(
        {
            df["date"].iloc[-1]
            for df in dead.values()
            if len(df) >= REQUIRED_BARS
        }
    )
    print(f"distinct death dates: {len(probe_dates)}")

    # --- Test 1: would the dead name clear production eligibility at all? ---
    print()
    print("=== TEST 1: 生產資格閘門（price>=5, ADV20>=$20M）===")
    gate_pass = 0
    gate_rows = []
    for ticker, df in dead.items():
        # evaluate across the last 120 bars, count how often it is eligible
        elig_days = 0
        total = 0
        for i in range(len(df) - 1, max(len(df) - 121, REQUIRED_BARS - 1), -1):
            row = factors_from_history(df, i)
            if row is None:
                continue
            total += 1
            if eligible(row):
                elig_days += 1
        if total and elig_days > 0:
            gate_pass += 1
            gate_rows.append((ticker, elig_days, total))
    print(
        f"死亡前 120 個交易日內曾通過資格閘門: "
        f"{gate_pass}/{len(dead)} ({gate_pass/len(dead)*100:.1f}%)"
    )
    if gate_rows:
        med = np.median([g / t for _, g, t in gate_rows])
        print(f"  通過者的通過比例中位數: {med*100:.0f}%")
        top = sorted(gate_rows, key=lambda x: -x[1] / x[2])[:10]
        print("  最常通過的前 10 檔:")
        for t, g, tt in top:
            print(f"    {t:<10} {g}/{tt} 天")

    # --- Test 2: would it reach the Top-20 of the real cross-section? ---
    print()
    print("=== TEST 2: 放回真實橫斷面後的排名 ===")
    print("(需要載入 275 MB bars cache，約 1-3 分鐘)")

    # Restrict the panel build to the dead names' death window to keep the
    # cross-section large enough to be meaningful but the load bounded.
    lo = min(probe_dates)
    hi = max(probe_dates)
    lo_s = (lo - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    hi_s = (hi + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"panel window {lo_s} .. {hi_s}")

    p = rpanel.build_panel(
        start=pd.Timestamp(lo_s).date(),
        end=pd.Timestamp(hi_s).date(),
        symbols_file=str(TRUSTED),
    )
    feats = rpanel.compute_features(p)
    print(f"panel: {p.shape[0]} sessions x {p.shape[1]} symbols")

    # Map session -> index for fast lookup
    sess_idx = {d: i for i, d in enumerate(p.sessions)}
    sym_idx = {s: i for i, s in enumerate(p.symbols)}

    # Build the surviving eligible cross-section per session (lazily, on
    # the dates we actually probe).
    wanted = set()
    for ticker, df in dead.items():
        for i in range(max(len(df) - 121, REQUIRED_BARS - 1), len(df)):
            row = factors_from_history(df, i)
            if row and eligible(row):
                d = df["date"].iloc[i].date()
                if d in sess_idx:
                    wanted.add(d)
    wanted = sorted(wanted)
    print(f"probe sessions with an eligible dead name: {len(wanted)}")

    def cross_section(ti: int) -> tuple[list[dict], list[str]]:
        rows, names = [], []
        px = p.close[ti]
        for si in range(p.shape[1]):
            c = px[si]
            if not np.isfinite(c) or c < MIN_PRICE:
                continue
            row = {
                "symbol": p.symbols[si],
                "price": float(c),
                "adv20": float(feats.adv20[ti, si]),
                "r20": float(feats.r20[ti, si]),
                "r60": float(feats.r60[ti, si]),
                "vol20": float(feats.vol20[ti, si]),
                "volume_ratio": float(feats.volume_ratio[ti, si]),
            }
            if eligible(row):
                rows.append(row)
                names.append(p.symbols[si])
        return rows, names

    cache_cs: dict[int, tuple] = {}
    for d in wanted:
        cache_cs[sess_idx[d]] = cross_section(sess_idx[d])

    results = []
    for ticker, df in dead.items():
        best_rank = None
        for i in range(max(len(df) - 121, REQUIRED_BARS - 1), len(df)):
            row = factors_from_history(df, i)
            if not row or not eligible(row):
                continue
            d = df["date"].iloc[i].date()
            ti = sess_idx.get(d)
            if ti is None or ti not in cache_cs:
                continue
            surv_rows, _ = cache_cs[ti]
            if len(surv_rows) < TOP_K:
                continue
            allrows = surv_rows + [row]
            scores = production_score(allrows)
            dead_score = scores[-1]
            surv_scores = sorted(scores[:-1], reverse=True)
            if len(surv_scores) < TOP_K:
                continue
            # how many survivors beat the dead name
            rank = 1 + sum(1 for s in surv_scores if s > dead_score)
            if best_rank is None or rank < best_rank:
                best_rank = rank
        if best_rank is not None:
            # The bars cache carries tz-aware stamps; the study window is a
            # naive date, so drop the zone before any comparison. Comparing
            # across the two raises rather than silently shifting a day.
            death = pd.Timestamp(df["date"].iloc[-1]).tz_localize(None)
            px = df["close"].to_numpy(dtype=float)
            r20 = (px[-1] / px[-21] - 1.0) if len(px) > 21 and px[-21] > 0 else np.nan
            adv20 = float(np.mean((px * df["volume"].to_numpy(dtype=float))[-20:]))
            results.append(
                {
                    "ticker": ticker,
                    "rank": best_rank,
                    "death": death,
                    "year": death.year,
                    "r20": r20,
                    "adv20": adv20,
                }
            )

    res = pd.DataFrame(results)
    if len(res) == 0:
        print("  無法評估（沒有退市股通過資格閘門）")
        return 0

    res = res.sort_values("rank")
    print()
    print(f"曾進入前 {TOP_K} 名的退市股: {(res['rank']<=TOP_K).sum()}/{len(res)}")
    print()
    print("=== 只看研究視窗內死亡者（death >= 2016-01-04）===")
    in_win = res[res["death"] >= pd.Timestamp("2016-01-04")]
    print(f"評估到的退市股: {len(res)} 檔；其中 2016 年後死亡: {len(in_win)} 檔")
    if len(in_win):
        hit = in_win[in_win["rank"] <= TOP_K]
        print(f"曾進入前 {TOP_K} 名: {len(hit)}/{len(in_win)} "
              f"({len(hit)/len(in_win)*100:.0f}%)")
        print()
        print(f"{'ticker':<10}{'最高排名':>8}{'死亡日':>12}{'死前20日':>10}{'ADV20':>12}")
        for _, r in in_win.iterrows():
            mark = " ★" if r["rank"] <= TOP_K else ""
            print(
                f"{r['ticker']:<10}{int(r['rank']):>8}"
                f"{r['death'].strftime('%Y-%m-%d'):>12}"
                f"{r['r20']*100:>9.1f}%{r['adv20']/1e6:>10.1f}M{mark}"
            )
        print()
        star = "★ = 若補齊資料，現行 Top-20 就會選到它"

    print()
    print("=== 全部評估到的退市股，依死亡年份 ===")
    print(res.groupby("year").agg(
        n=("rank", "size"),
        reached_top20=("rank", lambda s: int((s <= TOP_K).sum())),
    ).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
