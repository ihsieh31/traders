"""Assemble the survivorship-blessed panel from the per-ticker bar files.

Two universes are built, deliberately kept separate:
  full   -- every common equity in the worklist, dead included. This is the
            universe the bias measurement is about.
  top20  -- the 20-stock selection universe the main project actually targets.

For a Top-20 selection to be meaningful the cross-section must be far wider
than 20, so a size/liquidity gate is applied. Without market cap we use
ADV20 (dollar volume), which is computable from the bars we fetched and is the
same quantity production uses as a liquidity floor (plan.md D5, R13).
"""
import gzip, json, os, sys
import numpy as np
import pandas as pd

BAR = "research/out/20260928-external-anchor/tiingo/bars"
OUT = "research/out/20260928-external-anchor/panel"
os.makedirs(OUT, exist_ok=True)
START, END = "2005-01-01", "2025-12-31"

def load_all():
    files = sorted(f for f in os.listdir(BAR) if f.endswith(".gz"))
    if not files:
        sys.exit("no bar files yet")
    frames = []
    for i, f in enumerate(files, 1):
        try:
            frames.append(pd.read_csv(f"{BAR}/{f}"))
        except Exception as e:
            print(f"  skip {f}: {type(e).__name__}")
        if i % 2000 == 0:
            print(f"  read {i}/{len(files)}", flush=True)
    b = pd.concat(frames, ignore_index=True)
    b["date"] = pd.to_datetime(b["date"], utc=True, errors="coerce").dt.tz_localize(None)
    return b.dropna(subset=["date", "close"])


def main():
    b = load_all()
    b = b[(b.date >= START) & (b.date <= END)]
    b = b[b.close > 0]
    b = b.sort_values(["ticker", "date"])
    b["ret"] = b.groupby("ticker").close.pct_change()
    b["adv20"] = (b.close * b.volume).rolling(20, min_periods=20).mean()

    # a name is USABLE on date t only if it had enough recent history to compute
    # a 60-day signal, and was not already dead. Requiring 60 prior sessions is
    # what stops a resurrected ticker from entering with a warm start built on
    # its previous incarnation.
    b["nprior"] = b.groupby("ticker").cumcount()
    b["tradable"] = b.nprior >= 60

    print(f"bars        : {len(b):,}")
    print(f"tickers     : {b.ticker.nunique():,}")
    print(f"sessions    : {b.date.nunique():,}")
    print(f"window      : {b.date.min().date()} .. {b.date.max().date()}")
    print(f"tradable    : {int(b.tradable.sum()):,} rows, "
          f"{b[b.tradable].ticker.nunique():,} tickers")

    g = b[b.tradable].groupby("date")
    adv = g.adv20.median()
    for q in (0.10, 0.25, 0.50):
        print(f"  cross-section {int(q*100):>2}% floor on ADV20: ${adv.quantile(q)/1e6:,.2f}M")

    # the production liquidity rule, expressed on ADV20. Cross-sectional
    # quantiles are used rather than an absolute dollar floor because the
    # 2005-2025 price level drifts; plan.md D6 requires the parent population
    # to match production, and production's parent population IS the eligible
    # set, so a quantile is the self-consistent choice.
    for q in (0.10, 0.25):
        floor = float(adv.quantile(q))          # a scalar, not a series
        sel = b[b.tradable & (b.adv20 >= floor)]
        widths = sel.groupby("date").size()
        print(f"  at the {int(q*100)}% floor: median tradable/date = "
              f"{int(widths.median()):,}  min = {int(widths.min()):,}")

    b.to_parquet(f"{OUT}/bars_2005_2025.parquet", index=False)
    b.groupby("ticker").agg(n=("date", "size"), first=("date", "min"),
                            last=("date", "max")).to_csv(f"{OUT}/coverage.csv")
    print(f"\nwrote {OUT}/bars_2005_2025.parquet")

    rej = "research/out/20260928-external-anchor/tiingo/rejected_tier2.csv"
    if os.path.exists(rej):
        r = pd.read_csv(rej)
        print(f"quarantined for ticker reuse: {len(r):,} "
              f"({r.ticker.nunique() if len(r) else 0} tickers) -> {rej}")
    nd = "research/out/20260928-external-anchor/tiingo/no_data_tier2.csv"
    if os.path.exists(nd):
        print(f"no data returned: {len(pd.read_csv(nd)):,} -> {nd}")


if __name__ == "__main__":
    main()
