"""Resumable, quota-aware fetcher.

The 429 body says 'You have run over your hourly request allocation', so the
budget RESETS EVERY HOUR, not daily. This loop therefore:
  - runs until the worklist is exhausted or until --hours elapse
  - on 429 it sleeps and retries the SAME symbol (never records a false absence)
  - logs every quota event so the true limit can be measured, not guessed
  - resumes from bars_tier2.csv.gz on any restart
"""
import os, sys, io, json, time, gzip, urllib.request, ssl, urllib.error
import pandas as pd

TOKS = [t for t in os.environ.get("TIINGO_TOKENS", os.environ.get("TIINGO_TOKEN", "")).split(",") if t.strip()]
if not TOKS:
    print("set TIINGO_TOKENS=token1,token2"); sys.exit(2)
OUT = "research/out/20260928-external-anchor/tiingo"
os.makedirs(OUT, exist_ok=True)
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
LOG = open("/tmp/dl/tier2_loop.log", "a", buffering=1)
log = open("/tmp/dl/tier2_loop.log", "a", buffering=1)
def log(*a): print(*a, file=LOG, flush=True)

# Two tokens, round-robin. Each has its own HOURLY allocation, so a 429 on one
# must not stall the other -- that is the whole point of holding two. A token
# that 429s is parked with a cooldown and skipped until the hour rolls over.
COOLDOWN = {}          # token -> time when it becomes usable again
RETRIES = {}           # token -> consecutive 429s, drives the escalating wait
ACTIVE = list(TOKS)   # both tokens, round-robin; a 429 parks only the offender
def api(path, tries=2):
    """Returns (body, status, token_used). status 429 means THIS token is spent;
    the caller must park it, not treat the symbol as absent."""
    last = 429
    for t in ACTIVE:
        if COOLDOWN.get(t, 0) > time.time():
            continue
        for _ in range(tries):
            try:
                r = urllib.request.Request("https://api.tiingo.com" + path,
                    headers={"Authorization": "Token " + t, "User-Agent": "Mozilla/5.0",
                             "Content-Type": "application/json"})
                body = urllib.request.urlopen(r, timeout=60, context=ctx).read()
                RETRIES.pop(t, None)   # outage over; the next 429 starts low
                return body, 200, t
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    # Park THIS token only; the other stays usable. The wait is
                    # measured, not assumed: an hourly allocation on a rolling
                    # window means the first retry may still fail, so we escalate
                    # the wait (30s, 60s, ... capped at the hour) instead of
                    # sleeping a guessed 55 minutes and then hammering.
                    n = RETRIES.get(t, 0) + 1
                    RETRIES[t] = n
                    wait = min(30 * n, 3300)
                    COOLDOWN[t] = time.time() + wait
                    last = 429
                    break
                return None, e.code, t
            except Exception:
                time.sleep(2)
    return None, last, None

L = {"NYSE","NASDAQ","NYSEARCA","NYSE ARCA","AMEX","BATS","NYSE MKT"}
m = pd.read_csv("research/out/20260928-external-anchor/tiingo_symbol_master.csv")
m = m[(m.assetType == "Stock") & (m.exchange.isin(L))].copy()
m["endDate"] = pd.to_datetime(m.endDate, errors="coerce", utc=True).dt.tz_localize(None)
TRUTH = {str(s).upper().replace(".", "-"): e for s, e in zip(m.ticker, m.endDate) if pd.notna(e)}

# FIX: use the MATCHED worklist. The original list carried 669 preferred /
# warrant / unit / contingent-liability rows, which would have consumed 669 of
# ~2,400 daily requests (28% of a scarce quota) to 404, and which are not in
# our panel's universe anyway. Gap restated 6,936 -> 6,102 (process.md S-49).
# Free tier, read from Tiingo's own pricing bundle: 50 req/hour, 1000 req/day,
# 500 UNIQUE SYMBOLS PER MONTH, 1 GB/month. The unique-symbol cap is the binding
# one and no amount of parallelism or account count gets past it faster.
WEEK_CAP = int(os.environ.get("TIINGO_MONTH_CAP", "500"))
_p = pd.read_csv(f"{OUT}/worklist_prioritised.csv")
todo = _p[_p.to_fetch]["sym"].tolist()[:WEEK_CAP]
log(f"   worklist: {len(todo):,} names this run (monthly unique-symbol cap={WEEK_CAP})")
BAR = f"{OUT}/bars"
os.makedirs(BAR, exist_ok=True)

# RESUME HAZARD (hit for real, 2026-09-28): a run killed mid-write leaves a
# 0-byte bars_*.csv.gz behind. os.path.exists() is then True, and the next
# resume dies on EmptyDataError -- or worse, would append after it and produce
# a file that looks complete but is missing every bar from the killed run.
# A resume test must check SIZE and PARSEABILITY, not existence. A truncated
# file is discarded, and the corresponding names are re-fetched.
# Per-ticker files make resume exact: "which names already landed on disk" is a
# directory listing, not a parse of a possibly-truncated stream. An earlier
# single-gzip design lost 25 completed names when a run was killed, because
# gzip buffers were unflushed and the torn file had to be discarded wholesale.
# Integrity is not worth losing work over; write atomically per name instead.
def safe_name(t):
    return t.replace("/", "_").replace("\\", "_")
seen = {f[:-3] for f in os.listdir(BAR) if f.endswith(".gz")}
log(f"   resumed {len(seen):,} names from {BAR}/")
reuse_f = f"{OUT}/rejected_tier2.csv"
reuse = pd.read_csv(reuse_f).to_dict("records") if os.path.exists(reuse_f) else []
absent = []
qd = f"{OUT}/no_data_tier2.csv"
if os.path.exists(qd):
    absent = pd.read_csv(qd)["ticker"].tolist()
log(f"\n=== start {time.strftime('%H:%M:%S')}  have={len(seen)}  todo={len(todo)}  "
    f"reuse={len(reuse)}  absent={len(absent)}  run_for={HOURS}h ===")

reuse_keys = {r["ticker"] for r in reuse}   # hoisted: was rebuilt every iteration
absent_keys = set(absent)
deadline = time.time() + HOURS * 3600
quota_events = []   # measures the true hourly limit, per token
per_token = {t: 0 for t in TOKS}
i = 0
while i < len(todo) and time.time() < deadline:
    t = todo[i]
    if safe_name(t) in seen or safe_name(t) in absent or safe_name(t) in reuse_keys:
        i += 1; continue
    raw, st, used = api(f"/tiingo/daily/{t}/prices?startDate=2005-01-01&endDate=2025-12-31&format=json&resampleFreq=daily")
    if used is None:
        parked = sum(1 for k in COOLDOWN if COOLDOWN[k] > time.time())
        quota_events.append({"at": time.strftime("%H:%M:%S"),
                             "tokens_parked": parked, "n_tokens": len(TOKS),
                             "per_token": dict(per_token),
                             "kept": len(seen), "reuse": len(reuse), "absent": len(absent)})
        log(f"[{time.strftime('%H:%M:%S')}] ALL {len(TOKS)} tokens spent (parked={parked}). "
            f"per_token={ {k[:8]: v for k, v in per_token.items()} }. sleep 90s.")
        time.sleep(30)
        continue
    per_token[used] = per_token.get(used, 0) + 1
    i += 1

    # --- ONE-TIME DEPTH PROBE (fires on the first successful fetch after the
    # hourly reset). The 2005 floor in the worklist requests is OURS, not
    # Tiingo's: we never tested whether the free tier reaches further back.
    # This decides whether a survivorship-blessed panel can span 1990-2026
    # instead of only 2016-2026, which is the project's real constraint.
    if not os.path.exists(f"{OUT}/depth_probe.json"):
        try:
            rb, stb, _ = api("/tiingo/daily/IBM/prices?startDate=1950-01-01&endDate=2025-12-31&format=json&resampleFreq=daily")
            if rb and stb == 200:
                dd = json.loads(rb)
                if dd:
                    ss = pd.to_datetime([x["date"] for x in dd])
                    span = (ss.max() - ss.min()).days / 365.25
                    json.dump({"ticker": "IBM", "requested_from": "1950-01-01",
                               "bars": len(dd), "first_bar": str(ss.min().date()),
                               "last_bar": str(ss.max().date()),
                               "span_years": round(span, 1),
                               "expected_bars": int(span * 252),
                               "completeness": round(len(dd) / (span * 252), 4),
                               "note": "live name, so this is the HISTORY FLOOR of the "
                                       "free tier, not a delisted-name question"},
                              open(f"{OUT}/depth_probe.json", "w"), indent=2)
                    log(f"  DEPTH PROBE: IBM from 1950 -> {len(dd):,} bars, "
                        f"{ss.min().date()} .. {ss.max().date()} ({span:.1f}y, "
                        f"{len(dd)/(span*252):.1%} complete)")
            else:
                log(f"  depth probe: HTTP {stb}")
        except Exception as e:
            log(f"  depth probe failed: {type(e).__name__}")
        time.sleep(0.3)
    if raw is None:
        absent.append(safe_name(t))
        absent_keys.add(safe_name(t))
        pd.Series(sorted(set(absent)), name="ticker").to_csv(qd, index=False)
        continue
    d = json.loads(raw)
    if not d:
        absent.append(safe_name(t))
        absent_keys.add(safe_name(t))
        pd.Series(sorted(set(absent)), name="ticker").to_csv(qd, index=False)
        continue
    # Do not trust the vendor's ordering for a gate that decides whether to
    # quarantine the whole series. Taking d[-1] is right for Tiingo today, but
    # a single unsorted response would make every series look like ticker reuse
    # and silently discard good data (or, worse, pass a bad series).
    last = max(pd.Timestamp(x["date"][:10]) for x in d)
    exp = TRUTH.get(t)
    if exp is not None and abs((last - exp).days) > 3:
        reuse.append({"ticker": safe_name(t), "raw_ticker": t,
                      "expected_end": str(exp.date()), "got_end": str(last.date())})
        reuse_keys.add(safe_name(t))
        # persist IMMEDIATELY: a kill must not lose the quarantine list, or the
        # next run re-spends quota re-discovering the same reused tickers
        pd.DataFrame(reuse).drop_duplicates("ticker").to_csv(reuse_f, index=False)
        continue
    # atomic-ish: write a temp file then rename, so a kill mid-write can never
    # leave a half-written .gz that a later resume mistakes for complete
    tmp = f"{BAR}/.{safe_name(t)}.part"
    with gzip.open(tmp, "wt", encoding="utf-8") as g:
        g.write(pd.DataFrame(d)[["date","open","high","low","close","volume",
                                 "divCash","splitFactor"]].assign(ticker=t)
                .to_csv(index=False))
    os.replace(tmp, f"{BAR}/{safe_name(t)}.gz")
    seen.add(safe_name(t))
    if len(seen) % 25 == 0:
        log(f"  kept {len(seen):,}  reuse {len(reuse):,}  absent {len(absent):,}  at {t}  "
            f"per_token={ {k[:8]: v for k, v in per_token.items()} }")
    time.sleep(0.15)
if reuse: pd.DataFrame(reuse).drop_duplicates("ticker").to_csv(reuse_f, index=False)
if absent: pd.Series(sorted(set(absent)), name="ticker").to_csv(qd, index=False)
on_disk = {f[:-3] for f in os.listdir(BAR) if f.endswith(".gz")}
json.dump({"quota_events": quota_events, "kept": len(on_disk), "verified_on_disk": len(on_disk), "reuse": len(reuse),
           "absent": len(absent),
           "per_token": {k[:8] + "...": v for k, v in per_token.items()},
           "n_tokens": len(TOKS), "bar_dir": BAR,
           "stopped_reason": "worklist done" if i >= len(todo) else "time budget"},
          open(f"{OUT}/tier2_progress.json", "w"), indent=2)
log(f"=== stop {time.strftime('%H:%M:%S')}  on_disk={len(on_disk):,} reuse={len(reuse):,} "
    f"absent={len(absent):,}  quota_events={len(quota_events)} ===")
