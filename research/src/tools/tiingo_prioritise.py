"""Order the worklist by value per request.

The free tier allows 500 unique symbols per MONTH, so the question is not
"how do I get all 7,231" (impossible) but "which 500 buy the most correction".

A volume prior is unusable here: the volume we can measure comes from names we
have ALREADY paid for, and the worklist by construction excludes those. So
ranking unpriced names by a proxy computed from priced ones is circular.

Use instead the S&P 500 point-in-time membership file, which is free and
already on disk: a name that was an index member is a large cap by
construction, and a missing large cap distorts a cross-sectional study far
more than a missing micro-cap. Tier A = died in 2005-2025 while an S&P member.
"""
import csv, os, re
import pandas as pd

OUT = "research/out/20260928-external-anchor/tiingo"
BAR = f"{OUT}/bars"
PIT = "/tmp/dl/pit/sp500_components.csv"
wl = pd.read_csv("/tmp/dl/worklist_2005_2025.csv")
have = {f[:-3] for f in os.listdir(BAR) if f.endswith(".gz")} if os.path.isdir(BAR) else set()
have |= set(pd.read_csv(f"{OUT}/tier1_coverage.csv")["ticker"]) if os.path.exists(
    f"{OUT}/tier1_coverage.csv") else set()
norm = lambda t: str(t).strip().upper().replace(".", "-")
have_n = {norm(t) for t in have}

# S&P membership, with the date each token left the index
sp = {}
for r in csv.DictReader(open(PIT)):
    for tok in r["tickers"].split(","):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"^(.+)-(\d{6})$", tok)
        key = norm(tok if not m else m.group(1))
        end = m.group(2) if m else None
        if key not in sp or (end and (sp[key] is None or end < sp[key])):
            sp[key] = end

wl["sp_left"] = wl.sym.map(lambda t: sp.get(t))
wl["in_sp"] = wl.sp_left.notna()
wl["end_y"] = pd.to_datetime(wl.endDate).dt.year
wl["to_fetch"] = ~wl.sym.isin(have_n)

# Tier A: left the S&P 500 within 2005-2025 -> the highest-value corrections
wl["tier"] = 0
wl.loc[wl.in_sp & (wl.end_y >= 2005) & (wl.end_y <= 2025), "tier"] = 1
# Tier B: not an S&P member, most recent death first (recent deaths are the
# ones most likely to have been in the production universe while it existed)
wl = wl.sort_values(["to_fetch", "tier", "endDate", "sp_left"],
                    ascending=[False, False, False, True])
wl["priority_rank"] = range(1, len(wl) + 1)
wl.to_csv(f"{OUT}/worklist_prioritised.csv", index=False)

todo = wl[wl.to_fetch]
print(f"worklist total            : {len(wl):,}")
print(f"already held              : {int((~wl.to_fetch).sum()):,}")
print(f"remaining to fetch        : {len(todo):,}")
print(f"  tier A (S&P left 2005-2025): {int(todo.tier.sum()):,}")
print(f"  tier B (rest)             : {len(todo)-int(todo.tier.sum()):,}")
print(f"\nfree tier: 500 unique symbols per MONTH")
print(f"  -> tier A alone is {int(todo.tier.sum())} names, "
      f"{'fits' if todo.tier.sum()<=500 else 'DOES NOT fit'} in one month")
print(f"\nfirst 25 to request: {list(todo.sym.head(25))}")
print(f"\nwrote {OUT}/worklist_prioritised.csv")
