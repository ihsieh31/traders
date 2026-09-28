"""Vendor-agnostic bulk fetcher for delisted-name daily bars.

Written because the choice of vendor should not change the pipeline. EODHD and
Tiingo speak different dialects of the same API; the only real differences that
matter are the rate limit and whether delisted names are served, so both are
behind one interface here.

  TIINGO : 50 req/hour, 1000 req/day, 500 unique symbols/MONTH, 1 GB/month
  EODHD  : $19.99/mo -> 100,000 req/day, 1000 req/min, 30+ years, delisted
           data is a listed feature. 7,231 tickers in well under an hour.

The two failure modes that have actually bitten during this work are handled
explicitly, because both are silent:

  1. A 429/quota response must NEVER be recorded as "this symbol has no data".
     It manufactures thousands of false negatives that look exactly like a
     genuine coverage gap. See process.md S-50.
  2. Ticker reuse. A symbol master keyed by ticker with one row per ticker
     cannot represent reuse, and the prices endpoint resolves to whichever
     entity owns the ticker NOW. A reused ticker silently injects a different
     asset's history into a dead-company slot: wrong prices AND look-ahead.
     Every series is therefore checked against the master's death date before it
     is accepted.

usage:
  TIINGO_TOKENS=t1,t2  python bulk_fetch.py --vendor tiingo
  EODHD_TOKEN=xxxx      python bulk_fetch.py --vendor eodhd
  python bulk_fetch.py --vendor eodhd --limit 200      # dry run on a slice
"""
import argparse, gzip, json, os, sys, time, urllib.request, ssl, urllib.error
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BAR = "research/out/20260928-external-anchor/tiingo/bars"
MASTER = "/tmp/dl/tiingo_master.csv"
WORK = "/tmp/dl/worklist_2005_2025.csv"
START, END = "2005-01-01", "2025-12-31"
LISTED = {"NYSE", "NASDAQ", "NYSEARCA", "NYSE ARCA", "AMEX", "BATS", "NYSE MKT"}
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0"}

QUOTA, ABSENT, REUSE = [], [], []


def norm(t):
    return str(t).strip().upper().replace(".", "-")


def safe(t):
    return norm(t).replace("/", "_").replace("\\", "_")


def truth():
    """ticker -> real death date, from the point-in-time symbol master."""
    m = pd.read_csv(MASTER)
    m = m[(m.assetType == "Stock") & (m.exchange.isin(LISTED))].copy()
    m["endDate"] = pd.to_datetime(m.endDate, errors="coerce", utc=True).dt.tz_localize(None)
    m = m.dropna(subset=["endDate"])
    cut = m.endDate.max()
    out = {}
    for t, e in zip(m.ticker, m.endDate):
        if e < cut:                      # only names that actually stopped trading
            out[norm(t)] = e
    return out


# ------------------------------------------------------------------ vendors
def fetch_tiingo(sym, tokens, tries=2):
    """Returns (rows, status). status 429 means quota, NOT absence."""
    path = (f"/tiingo/daily/{sym}/prices?startDate={START}&endDate={END}"
            f"&format=json&resampleFreq=daily")
    for t in tokens:
        for _ in range(tries):
            r = urllib.request.Request(
                "https://api.tiingo.com" + path,
                headers={**UA, "Authorization": "Token " + t, "Content-Type": "application/json"})
            try:
                return json.loads(urllib.request.urlopen(r, timeout=60, context=ctx).read()), 200
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    return None, 429
                if e.code == 404:
                    return None, 404
                return None, e.code
            except Exception:
                time.sleep(2)
    return None, 429


def fetch_eodhd(sym, token, tries=3):
    """EODHD wants the ORIGINAL ticker (BRK.B), not the normalised one."""
    orig = ORIG.get(sym, sym)
    url = (f"https://eodhd.com/api/eod/{orig}.US?api_token={token}"
           f"&from={START}&to={END}&fmt=json&period=d")
    for _ in range(tries):
        try:
            r = urllib.request.Request(url, headers=dict(UA))
            body = urllib.request.urlopen(r, timeout=60, context=ctx).read()
            d = json.loads(body)
            return ([{"date": x["date"], "open": x.get("open"), "high": x.get("high"),
                      "low": x.get("low"), "close": x.get("close"),
                      "volume": x.get("volume"), "divCash": 0.0,
                      "splitFactor": 1.0} for x in d] if d else None), 200
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                return None, 429
            if e.code in (403, 404):
                return None, e.code
            return None, e.code
        except Exception:
            time.sleep(2)
    return None, 429


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", choices=["tiingo", "eodhd"], required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--worklist", default=WORK)
    args = ap.parse_args()

    os.makedirs(BAR, exist_ok=True)
    tokens = [t for t in os.environ.get("TIINGO_TOKENS", "").split(",") if t]
    eod = os.environ.get("EODHD_TOKEN", "")
    if args.vendor == "tiingo" and not tokens:
        sys.exit("set TIINGO_TOKENS=t1,t2")
    if args.vendor == "eodhd" and not eod:
        sys.exit("set EODHD_TOKEN=xxxx")

    wl = pd.read_csv(args.worklist)
    global ORIG
    ORIG = dict(zip(wl.sym, wl.ticker))       # keep the vendor's own spelling
    have = {f[:-3] for f in os.listdir(BAR) if f.endswith(".gz")}
    todo = [s for s in wl.sym if s not in have]
    if args.limit:
        todo = todo[:args.limit]
    TR = truth()
    print(f"{args.vendor}: {len(todo):,} to fetch, {len(have):,} already on disk")

    t0 = time.time()
    for i, sym in enumerate(todo, 1):
        if args.vendor == "tiingo":
            rows, st = fetch_tiingo(sym, tokens)
        else:
            rows, st = fetch_eodhd(sym, eod)
        if st == 429:
            QUOTA.append(sym)
            print(f"  QUOTA at {sym} -- stopping cleanly, nothing recorded as absent")
            break
        if rows is None:
            ABSENT.append({"ticker": safe(sym), "http": st})
        else:
            last = max(pd.Timestamp(x["date"][:10]) for x in rows)
            exp = TR.get(sym)
            if exp is not None and abs((last - exp).days) > 3:
                REUSE.append({"ticker": safe(sym), "raw": sym,
                              "expected_end": str(exp.date()), "got_end": str(last.date())})
            else:
                tmp = f"{BAR}/.{safe(sym)}.part"
                with gzip.open(tmp, "wt", encoding="utf-8") as g:
                    g.write(pd.DataFrame(rows).assign(ticker=sym).to_csv(index=False))
                os.replace(tmp, f"{BAR}/{safe(sym)}.gz")
                have.add(safe(sym))
        if i % 100 == 0:
            rate = i / max(1e-9, time.time() - t0)
            print(f"  {i}/{len(todo)}  kept={len(have):,}  absent={len(ABSENT):,}  "
                  f"reuse={len(REUSE):,}  {rate:.1f}/s", flush=True)
        if args.vendor == "eodhd":
            time.sleep(0.01)   # 1000/min is far above this loop's ceiling

    pd.DataFrame(REUSE).to_csv("research/out/20260928-external-anchor/tiingo/rejected_bulk.csv",
                               index=False)
    pd.DataFrame(ABSENT).to_csv("research/out/20260928-external-anchor/tiingo/absent_bulk.csv",
                                index=False)
    print(f"\ndone: {len(have):,} on disk, {len(ABSENT)} absent, {len(REUSE)} quarantined, "
          f"{len(QUOTA)} unclaimed (quota)")


ORIG = {}
if __name__ == "__main__":
    main()
