"""Offline test harness for tiingo_loop.py. No network, no quota.

Mocks the API and asserts the invariants that matter when a job runs unattended
for days against a scarce quota:
  T1  dotted class-share tickers must be requested with a DOT, not a dash
  T2  a 429 must park only the offending token, not all of them
  T3  a 429 must never be recorded as "this symbol has no data"
  T4  a symbol returned by the API but ending at the wrong date is quarantined
      (ticker reuse), not written
  T5  resume from a partial run re-fetches nothing already on disk
  T6  a kill mid-run loses at most the in-flight name
  T7  the request date is tz-consistent with the master's endDate
  T8  the worklist ticker actually exists in the symbol master (no silent 404s)
  T9  incremental persistence: reuse/absent survive a kill
"""
import gzip, json, os, shutil, subprocess, sys, tempfile, textwrap
import pandas as pd

ROOT = "/Users/zongen/Downloads/codex/tradingBuffett"
OUT  = f"{ROOT}/research/out/20260928-external-anchor/tiingo"
MOCK = "/tmp/dl/_mock_api.py"
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"   {detail}" if detail and not cond else ""))

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = f"{HERE}/tiingo_mock_api.py"   # standalone file, see its docstring
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"   {detail}" if detail and not cond else ""))

# a copy of the loop with the API base URL and sleep patched, so it is testable
LOOP = open(f"{HERE}/tiingo_fetch_loop.py").read()

def build_loop(port, worklist, outdir, limit_path="worklist_common.csv"):
    s = LOOP
    s = s.replace('OUT = "research/out/20260928-external-anchor/tiingo"', f'OUT = {outdir!r}')
    s = s.replace('"https://api.tiingo.com" + path', f'"http://127.0.0.1:{port}" + path')
    s = s.replace('ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE', 'ctx = None')
    s = s.replace('urllib.request.urlopen(r, timeout=60, context=ctx)', 'urllib.request.urlopen(r, timeout=10)')
    s = s.replace('_p = pd.read_csv(f"{OUT}/worklist_prioritised.csv")',
                  f'_p = pd.read_csv({limit_path!r})')
    s = s.replace('time.sleep(90)', 'time.sleep(0.05)')
    s = s.replace('time.sleep(0.15)', 'time.sleep(0)')
    s = s.replace('time.sleep(2)', 'time.sleep(0)')
    s = s.replace('time.sleep(0.3)', 'time.sleep(0)')
    s = s.replace('COOLDOWN[t] = time.time() + 3300', 'COOLDOWN[t] = time.time() + 9999')
    s = s.replace('LOG = open("/tmp/dl/tier2_loop.log", "a", buffering=1)', 'LOG = open(os.environ["TLOG"], "a", buffering=1)')
    s = s.replace('log = open("/tmp/dl/tier2_loop.log", "a", buffering=1)', 'log = open(os.environ["TLOG"], "a", buffering=1)')
    # the master path must follow `outdir`, not the module-level OUT, or the
    # loop dies on a nonexistent file and silently makes zero requests
    s = s.replace('m = pd.read_csv("research/out/20260928-external-anchor/tiingo_symbol_master.csv")',
                  f'm = pd.read_csv({f"{outdir}/tiingo_symbol_master.csv"!r})')
    p = "/tmp/dl/_loop_test.py"   # generated copy under test
    open(p, "w").write(s)
    return p

# ---------------------------------------------------------------- fixtures
def fixture(tmp, tickers, limit=1000):
    os.makedirs(f"{tmp}/bars", exist_ok=True)
    rows = [{"ticker": t, "exchange": "NYSE", "assetType": "Stock",
             "priceCurrency": "USD", "startDate": "2005-01-03", "endDate": e}
            for t, e in tickers.items()]
    # the loop reads THIS file for TRUTH, so the fixture must own it
    pd.DataFrame(rows).to_csv(f"{tmp}/tiingo_symbol_master.csv", index=False)
    pd.DataFrame({"sym": list(tickers), "ticker": list(tickers),
                  "endDate": list(tickers.values()),
                  "to_fetch": [True] * len(tickers),
                  "startDate": ["2005-01-03"] * len(tickers)}).to_csv(
        f"{tmp}/wl.csv", index=False)
    json.dump({"limit_per_token": limit, "tickers": tickers}, open(f"{tmp}/spec.json", "w"))
    return None

MOCKS = []

def free_port():
    import socket as _s
    _x = _s.socket(); _x.bind(("127.0.0.1", 0)); _p = _x.getsockname()[1]; _x.close()
    return _p

def start_mock(tmp, port=None):
    port = port or free_port()
    os.environ["MOCK_SPEC"] = f"{tmp}/spec.json"
    os.environ["MOCK_CALLS"] = f"{tmp}/calls.jsonl"
    open(f"{tmp}/calls.jsonl", "w").close()
    _err = open(f"{tmp}/mock_err.txt", "wb")
    p = subprocess.Popen([sys.executable, MOCK, str(port)], env=dict(os.environ),
                         stdout=_err, stderr=_err)
    import socket, time as _t
    for _ in range(80):
        if p.poll() is not None:
            raise RuntimeError("mock died: " +
                               open(f"{tmp}/mock_err.txt").read()[:600])
        try:
            socket.create_connection(("127.0.0.1", port), 0.25).close()
            p._port = port; MOCKS.append(p); return p
        except OSError:
            _t.sleep(0.05)
    raise RuntimeError("mock did not open the port; spec=" + open(f"{tmp}/spec.json").read()[:200])

def stop(m):
    if m is None: return
    m.terminate()
    try: m.wait(3)
    except Exception: m.kill()

def calls(tmp):
    try:
        return [json.loads(l) for l in open(f"{tmp}/calls.jsonl") if l.strip()]
    except FileNotFoundError:
        return []

# ================================================================ TESTS
print("\n=== T1: dotted class shares must be requested with a DOT ===")
tmp = tempfile.mkdtemp()
tk = {"BRK.B": "2026-09-25", "RDS.A": "2026-09-25"}
fixture(tmp, tk, limit=10)
m = start_mock(tmp)
loop = build_loop(m._port, None, tmp, f"{tmp}/wl.csv")
env = dict(os.environ, TIINGO_TOKENS="aaaa1111", TLOG=f"{tmp}/log.txt", TIINGO_MOCK="1")
r = subprocess.run([sys.executable, loop, "0.01"], env=env, capture_output=True, text=True, cwd=ROOT)
stop(m)
got = [c["path"] for c in calls(tmp)]
dotted_ok = any("/daily/BRK.B/" in p for p in got)
dashed_bad = any("/daily/BRK-B/" in p for p in got)
_lg = open(f"{tmp}/log.txt").read() if os.path.exists(f"{tmp}/log.txt") else "(no log)"
check("T1 requests BRK.B with a dot", dotted_ok,
      f"paths={got}\n    loop_rc={r.returncode} err={r.stderr[-400:]}\n"
      f"    mock_err={open(f'{tmp}/mock_err.txt').read()[-400:] if os.path.exists(f'{tmp}/mock_err.txt') else '?'}"
      f"\n    log={_lg[-300:]}")
check("T1 does NOT request BRK-B with a dash", not dashed_bad, f"paths={got}")

print("\n=== T2/T3: a 429 parks only the offending token and is never a false absence ===")
tmp2 = tempfile.mkdtemp()
tk2 = {f"T{i}": "2016-06-01" for i in range(20)}
fixture(tmp2, tk2, limit=3)          # 3 requests per token
m = start_mock(tmp2)
loop2 = build_loop(m._port, None, tmp2, f"{tmp2}/wl.csv")
env2 = dict(os.environ, TIINGO_TOKENS="tokAAAAAA,tokBBBBBB", TLOG=f"{tmp2}/log.txt")
r2 = subprocess.run([sys.executable, loop2, "0.01"], env=env2, capture_output=True, text=True, cwd=ROOT)
stop(m)
c2 = calls(tmp2)
by_tok = {}
for c in c2: by_tok[c["token"]] = by_tok.get(c["token"], 0) + 1
check("T2 both tokens were used", len(by_tok) == 2, f"per-token={by_tok}")
check("T2 each token stopped at its own limit+1", all(v <= 5 for v in by_tok.values()),
      f"{by_tok} -- a parked token must not be retried within the same hour")
nd = f"{tmp2}/no_data_tier2.csv"
absent_n = len(pd.read_csv(nd)["ticker"]) if os.path.exists(nd) else 0
bars_n = len(os.listdir(f"{tmp2}/bars"))
check("T3 no false absences recorded from 429", absent_n == 0,
      f"absent={absent_n} (bars={bars_n}) -- a 429 must never write no_data")

print("\n=== T4: a series ending at the wrong date is quarantined, not written ===")
tmp3 = tempfile.mkdtemp()
tk3 = {"GOOD1": "2016-06-01", "REUSED": "2016-06-01"}
fixture(tmp3, tk3, limit=50)
# REUSED returns 2026 data => wrong entity
spec = json.load(open(f"{tmp3}/spec.json")); spec["tickers"]["REUSED"] = "2026-09-25"
json.dump(spec, open(f"{tmp3}/spec.json", "w"))
m = start_mock(tmp3)
loop3 = build_loop(m._port, None, tmp3, f"{tmp3}/wl.csv")
env3 = dict(os.environ, TIINGO_TOKENS="cccc1111", TLOG=f"{tmp3}/log.txt")
subprocess.run([sys.executable, loop3, "0.01"], env=env3, capture_output=True, text=True, cwd=ROOT)
stop(m)
files = sorted(os.listdir(f"{tmp3}/bars"))
rej = pd.read_csv(f"{tmp3}/rejected_tier2.csv") if os.path.exists(f"{tmp3}/rejected_tier2.csv") else pd.DataFrame()
check("T4 GOOD1 written", "GOOD1.gz" in files, f"files={files}")
check("T4 REUSED not written", "REUSED.gz" not in files, f"files={files}")
check("T4 REUSED quarantined with both dates", len(rej) == 1 and "expected_end" in rej.columns,
      f"rejected={rej.to_dict('records')}")

print("\n=== T5: resume re-fetches nothing already on disk ===")
before = calls(tmp3)
n_files = len(os.listdir(f"{tmp3}/bars"))
env3b = dict(env3)
m = start_mock(tmp3, 8733)
subprocess.run([sys.executable, loop3, "0.01"], env=env3b, capture_output=True, text=True, cwd=ROOT)
stop(m)
after = calls(tmp3)
new = [c for c in after[len(before):] if "REUSED" in c["path"] or "GOOD1" in c["path"]]
check("T5 no re-request of an already-written name", len(new) == 0, f"re-requested={new}")
check("T5 file count unchanged", len(os.listdir(f"{tmp3}/bars")) == n_files)

print("\n=== T6: a kill mid-run loses at most the in-flight name ===")
tmp4 = tempfile.mkdtemp()
tk4 = {f"K{i}": "2016-06-01" for i in range(8)}
fixture(tmp4, tk4, limit=50)
m = start_mock(tmp4)
loop4 = build_loop(m._port, None, tmp4, f"{tmp4}/wl.csv")
env4 = dict(os.environ, TIINGO_TOKENS="dddd1111", TLOG=f"{tmp4}/log.txt")
p = subprocess.Popen([sys.executable, loop4, "1"], env=env4, cwd=ROOT,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import time as _t; _t.sleep(0.6); p.kill(); p.wait()
mid = [f for f in os.listdir(f"{tmp4}/bars") if f.endswith(".gz")]
unreadable = []
for f in mid:
    try:
        d = pd.read_csv(f"{tmp4}/bars/{f}")
        if len(d) == 0 or "ticker" not in d.columns:
            unreadable.append((f, "empty/wrong shape"))
    except Exception as e:
        unreadable.append((f, type(e).__name__))
check("T6 every landed .gz is fully readable (a kill never leaves a torn file)",
      len(unreadable) == 0, f"unreadable={unreadable}")
check("T6 no .part temp file is visible as a completed name",
      all(not f.startswith(".") for f in mid), f"files={mid}")
# and a resume must not lose what landed
env4b = dict(env4)
m2 = start_mock(tmp4)
subprocess.run([sys.executable, loop4, "0.01"], env=env4b, capture_output=True, cwd=ROOT)
stop(m2)
final = len([f for f in os.listdir(f"{tmp4}/bars") if f.endswith(".gz")])
check("T6 resume completes the set", final == 8, f"final={final}/8")

print("\n=== T7: the gate compares like-for-like across tz handling ===")
s = LOOP
check("T7 master endDate is tz-naive", ".dt.tz_localize(None)" in s)
check("T7 gate uses (last-exp).days on Timestamps", "abs((last - exp).days)" in s)
check("T7 no leftover .date() on a Timedelta", ".date().toordinal()" not in s)

print("\n=== T8: every worklist ticker exists in the symbol master ===")
real = pd.read_csv("/tmp/dl/worklist_common.csv")
mst = pd.read_csv("/tmp/dl/tiingo_master.csv")
mkeys = {str(s).strip().upper().replace(".", "-") for s in mst.ticker}
missing = [t for t in real["sym"] if t not in mkeys]
check("T8 all worklist syms present in master", len(missing) == 0,
      f"missing={missing[:5]} ({len(missing)})")
orig_ok = all(
    str(a).strip().upper().replace(".", "-") == str(b).strip().upper().replace(".", "-")
    for a, b in zip(real["sym"], real["ticker"]))
check("T8 sym and ticker are consistent (so either can be used as a key)", orig_ok)

print("\n=== T9: reject/absent are persisted incrementally, not only at exit ===")
# the first reuse/abort write must appear AFTER the `while` header, i.e. inside
# the loop body, not only in the epilogue
_w = s.index("while i < len(todo)")
_rr = s.index("to_csv(reuse_f")
_aa = s.index("to_csv(qd")
check("T9 reuse written inside the loop", _rr > _w,
      f"first reuse write at {s[:_rr].count(chr(10))+1}, while-loop at {s[:_w].count(chr(10))+1}"
      " -- a kill would lose the quarantine list and re-spend quota re-discovering it")
check("T9 absent written inside the loop", _aa > _w,
      f"first absent write at {s[:_aa].count(chr(10))+1}")
check("T9 no double-opened log handle", "LOG = open(" not in s.split("def log")[0].split("\n")[-2]
      or s.count('open("/tmp/dl/tier2_loop.log", "a", buffering=1)') == 1,
      "the log file is opened twice (LOG and log); LOG is never closed")
check("T9 dead helper removed or used", ("def pick(" not in s) or (s.count("pick(") > 1),
      "pick() is defined but never called")

print("\n=== T10: the escalating backoff resets after a success ===")
check("T10 RETRIES is cleared on a successful response",
      "RETRIES.pop(t, None)" in s or "RETRIES.pop(t,None)" in s,
      "once a token has hit the 3300s cap it would wait the full hour forever, "
      "even inside a fresh allocation -- the loop would never make progress again")
check("T10 backoff is measured, not a guessed constant",
      "min(30 * n, 3300)" in s,
      "a guessed 55-minute park means either idle wall-clock or re-probing "
      "inside a still-closed rolling hour")
check("T10 retry counter is per token, not global",
      "RETRIES = {}" in s and "RETRIES[t] = n" in s)

print(f"\n{'='*64}\nPASSED {len(PASS)}   FAILED {len(FAIL)}")
if FAIL:
    print("FAILING:")
    for f in FAIL: print("   -", f)
for _m in MOCKS: stop(_m)
sys.exit(1 if FAIL else 0)
