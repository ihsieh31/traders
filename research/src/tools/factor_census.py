"""Which factors are still alive, and what is the market itself doing?

Answers two questions the project cannot answer with its own 10.7 years of
survivorship-biased daily bars:

  1. Does ANY documented equity factor still have an effect in 2016-2026?
     Measured on CRSP/Ken French, 1926-2026, dead names included, free.

  2. Our harness measures a **long-only** Top-20 (measure.py:325 takes the
     mean of the top 20 returns; there is no short leg). CRSP publishes
     **long-short** decile spreads. Those are not comparable, and the
     difference is not academic: a long-only top-20 book carries full market
     beta, so in a bull market it can look excellent with no factor at all.

     So the honest comparison is: our Top-20 return minus the market return
     (the beta the book had no choice of carrying), not versus zero.

Read research/plan.md and research/process.md before changing anything here.
"""

from __future__ import annotations

import os
import zipfile
import numpy as np
import pandas as pd

KF = "/private/var/folders/46/3vjy25x94rvgpv7dj_1h3snm0000gn/T/opencode/kenfrench"

# CRSP long-short decile spreads. MomDaily = daily 12-1 momentum.
FILES = {
    "MomDaily": "MomDaily.csv",
    "Momentum_m": "Momentum.csv",
    "STRev_m": "STRev_CSV.csv",
    "LTRev_m": "LTRev_CSV.csv",
    "Factors5Daily": "Factors5Daily.csv",
}

WINDOWS = [
    ("FULL 1926-2026", "1926-01-01", "2026-12-31"),
    ("1950-2026", "1950-01-01", "2026-12-31"),
    ("2005-2026", "2005-01-01", "2026-12-31"),
    ("2016-2026 (視窗)", "2016-01-01", "2026-12-31"),
    ("2016-2020 (探索期)", "2016-01-01", "2020-12-31"),
    ("2021-2025 (驗證期)", "2021-01-01", "2025-12-31"),
]


def load_daily(name: str) -> pd.DataFrame | None:
    path = os.path.join(KF, FILES[name])
    if not os.path.exists(path):
        return None
    if name == "MomDaily":
        # Ken French files carry a prose header, then a blank line, then
        # "yyyymmdd,<factor>" rows with no column header. Parsing by "first
        # 8 chars are digits" is the only robust way -- the header lines are
        # not a fixed number of rows across files.
        rows = []
        for line in open(path, encoding="utf-8", errors="ignore"):
            s = line.strip()
            if len(s) < 10 or not s[:8].isdigit():
                continue
            parts = s.split(",")
            if len(parts) < 2:
                continue
            try:
                # percent -> fraction, same reason as Factors5Daily
                rows.append(
                    [
                        pd.to_datetime(s[:8], format="%Y%m%d"),
                        float(parts[1]) / 100.0,
                    ]
                )
            except ValueError:
                continue
        return pd.DataFrame(rows, columns=["date", "f"]).set_index("date")
    if name == "Factors5Daily":
        # NOTE: Ken French files are in PERCENT, not fractions. "0.09" means
        # 0.09%, so every value must be divided by 100 before annualising.
        # Treating them as fractions inflates every annualised number 100x
        # (the 1926-2026 momentum print comes out as +632% instead of
        # +6.33%). This is a units error, not a data error.
        rows = []
        for line in open(path, encoding="utf-8", errors="ignore"):
            s = line.strip()
            if len(s) < 10 or not s[:8].isdigit():
                continue
            parts = s.split(",")
            if len(parts) < 5:
                continue
            try:
                rows.append(
                    [
                        pd.to_datetime(s[:8], format="%Y%m%d"),
                        *[float(x) / 100.0 for x in parts[1:5]],
                    ]
                )
            except ValueError:
                continue
        cols = ["Mkt-RF", "SMB", "HML", "RF"]
        return pd.DataFrame(rows, columns=["date"] + cols).set_index("date")
    return None


def stats(series: pd.Series) -> dict:
    s = series.dropna()
    if len(s) < 2:
        return dict(n=0, ann=np.nan, vol=np.nan, sr=np.nan, t=np.nan)
    ann = s.mean() * 252
    vol = s.std(ddof=1) * np.sqrt(252)
    sr = s.mean() / s.std(ddof=1) * np.sqrt(252) if s.std(ddof=1) > 0 else np.nan
    t = s.mean() / s.std(ddof=1) * np.sqrt(len(s)) if s.std(ddof=1) > 0 else np.nan
    return dict(n=len(s), ann=ann, vol=vol, sr=sr, t=t)


def main() -> int:
    print("=" * 78)
    print("CRSP / Ken French 因子普查 — 100 年、含全部死亡股、免費")
    print("=" * 78)

    mom = load_daily("MomDaily")
    f5 = load_daily("Factors5Daily")

    series: dict[str, pd.Series] = {}
    if mom is not None:
        series["Mom (LS decile)"] = mom["f"]
    if f5 is not None:
        # The daily Factors5 file ships Mkt-RF/SMB/HML/RF only -- RMW and CMA
        # are monthly-only. Iterate what is actually present rather than
        # assuming the five-factor set.
        for c in f5.columns:
            if c == "RF":
                continue  # risk-free rate, not a traded factor
            series[c] = f5[c]

    print()
    print("因子：CRSP 多空十分位價差（日頻，252 日年化）")
    print()
    hdr = f"{'':22}" + "".join(f"{w[0]:>20}" for w in WINDOWS[:1])
    print(f"{'因子':<22}{'期間':<12}{'n':>7}{'年化':>9}{'Sharpe':>9}{'t':>8}")
    print("-" * 78)
    for name, s in series.items():
        for wname, lo, hi in WINDOWS:
            sub = s.loc[lo:hi]
            st = stats(sub)
            if st["n"] == 0:
                continue
            print(
                f"{name if wname==WINDOWS[0][0] else '':<22}{wname:<12}"
                f"{st['n']:>7}{st['ann']*100:>8.1f}%{st['sr']:>9.2f}{st['t']:>8.2f}"
            )
        print()

    # --- the beta question ------------------------------------------------
    print("=" * 78)
    print("我們量的是「只買 Top-20」＝ 多頭部位。市場本身的報酬是多少？")
    print("=" * 78)
    if "Mkt-RF" in series:
        mkt_ex = series["Mkt-RF"]
        for wname, lo, hi in WINDOWS:
            st = stats(mkt_ex.loc[lo:hi])
            if st["n"] == 0:
                continue
            print(
                f"  Mkt-RF（超額）  {wname:<22} n={st['n']:>6}  "
                f"年化 {st['ann']*100:>6.1f}%  SR {st['sr']:>5.2f}  t {st['t']:>5.2f}"
            )
    print()
    ours = {
        "2016-2020 (探索期)": 0.179,
        "2020-2025": 0.137,
        "2016-2023": 0.090,
    }
    print("我們量到的「Top-20 多頭淨年化」對照：")
    for wname, lo, hi in [
        ("2016-2020 (探索期)", "2016-01-01", "2020-12-31"),
        ("2020-2025", "2020-01-01", "2025-12-31"),
        ("2016-2023", "2016-01-01", "2023-12-31"),
    ]:
        if "Mkt-RF" not in series:
            break
        st = stats(series["Mkt-RF"].loc[lo:hi])
        o = ours.get(wname)
        if st["n"] == 0 or o is None:
            continue
        print(
            f"  {wname:<22} 我們 {o*100:>6.1f}%   市場超額 {st['ann']*100:>6.1f}%"
            f"   → 超過市場的部分 {(o-st['ann'])*100:>+6.1f} pp/年"
        )
    print()
    print("註：CRSP 因子是多空價差；我們的是純多頭。兩者不可直接相減，")
    print("    上表的「超過市場部分」是能公平比較的最小量級 —— 一個只買 20 檔的")
    print("    多頭組合，無論選股公式寫成什麼，都必然先拿到市場 beta。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
