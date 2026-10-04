"""Fixed five-method diagnostics; only the 2016-2020 price sandbox."""
import csv
from datetime import date
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("prior_short_diagnostics", ROOT / "research/out/20260929-short-screen/compare.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
spec = importlib.util.spec_from_file_location("three_field_financials", OUT / "prepare.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)
np, P = A.np, A.P
NAMES = ("price_weak", "accrual_weak", "cashburn_weak", "cashburn_rebound", "residual_rally")


def latest_at(records, day):
    latest = {}
    for r in records:
        if r["accepted"][:10] >= str(day):
            continue
        old = latest.get(r["cik"])
        if old is None or (r["period"], r["accepted"], r["adsh"]) > (old["period"], old["accepted"], old["adsh"]):
            latest[r["cik"]] = r
    return latest


def residual(close, market, t, ids):
    mr = market[t-59:t+1]/market[t-60:t]-1
    r5m = market[t]/market[t-5]-1
    r5 = close[t, ids]/close[t-5, ids]-1
    sr = close[t-59:t+1, ids]/close[t-60:t, ids]-1
    var = np.mean((mr-mr.mean())**2)
    if not np.isfinite(mr).all() or not np.isfinite(r5m) or not var > 0:
        return r5, np.full(len(ids), np.nan), np.full(len(ids), np.nan)
    beta = np.mean((sr-sr.mean(axis=0))*(mr-mr.mean())[:, None], axis=0)/var
    return r5, beta, r5-beta*r5m


def models(feats, t, ids, ac, cb, r5, e5):
    weak = (feats.trend[t, ids] < 0) & (feats.r20[t, ids] < 0)
    pp = {k: A.C.percentiles_fast(getattr(feats, k)[t, ids]) for k in ("adv20", "r20", "r60", "vol20", "volume_ratio")}
    price = 100*(.20*pp["adv20"]+.25*(1-pp["r20"])+.25*(1-pp["r60"])+.15*(1-pp["vol20"])+.15*pp["volume_ratio"])
    return {
        "price_weak": (weak, price),
        "accrual_weak": (weak & (ac > 0), ac),
        "cashburn_weak": (weak & (cb > 0), cb),
        "cashburn_rebound": (weak & (cb > 0) & (r5 > 0), r5),
        "residual_rally": ((r5 > 0) & (e5 > 0) & np.isfinite(e5), e5),
    }


def checks():
    d = D.derive({"Assets": 100, "NetIncomeLoss": 10, "NetCashProvidedByUsedInOperatingActivities": -5})
    assert d["ac"] == .15 and d["cb"] == .05
    s = D.derive({"Assets": 1e8, "NetIncomeLoss": 1e7, "NetCashProvidedByUsedInOperatingActivities": -5e6})
    assert s["ac"] == d["ac"] and s["cb"] == d["cb"]
    for bad in (None, float("nan"), float("inf")):
        try:
            D.derive({"Assets": 100, "NetIncomeLoss": bad, "NetCashProvidedByUsedInOperatingActivities": -5})
        except ValueError:
            pass
        else:
            raise AssertionError("missing/nonfinite must be rejected")
    r = dict(cik="1", period="20161231", accepted="2017-03-01 12:00", adsh="a", status="complete")
    later = dict(r, accepted="2017-03-02 12:00", adsh="b", status="unavailable")
    future = dict(r, period="20181231", accepted="2019-03-01 12:00", adsh="c")
    assert not latest_at([r], date(2017, 3, 1))
    assert latest_at([r, future], date(2017, 3, 2))["1"] == r
    assert latest_at([r, later], date(2017, 3, 3))["1"]["status"] == "unavailable"
    mr = np.sin(np.arange(80))*.01
    market = 100*np.cumprod(1+mr)
    stock = 50*np.cumprod(1+2*mr)
    close = stock[:, None]
    at = residual(close, market, 60, np.array([0]))
    assert math.isclose(at[1][0], 2, abs_tol=1e-12)
    assert all(np.allclose(x, y) for x, y in zip(at, residual(close[:61], market[:61], 60, np.array([0]))))
    assert np.isnan(residual(close, np.ones(80), 60, np.array([0]))[2][0])
    assert np.lexsort((np.array([1, 0]), -np.array([10., 10.]), -np.array([.1, .1]))).tolist() == [1, 0]
    return dict(financial_scale=True, missing_rejected=True, same_day_excluded=True, future_filing_invariant=True,
                latest_missing_no_fallback=True, beta_oracle=True, price_prefix_invariant=True,
                flat_market_rejected=True, tie_break=True, short_pnl=A.checks())


def summarize(rows, picks):
    out = A.aggregate(rows)
    active = [r for r in rows if r["n"]]
    out.update(active_periods=len(active), unique_companies=len({p["cik"] for p in picks}),
        mean_capital_fraction=float(np.mean([r["n"]/20 for r in rows])),
        per_invested_net5=A.bootstrap([r["net_rt10bp_borrow5pct"]*20/r["n"] for r in active if r["net_rt10bp_borrow5pct"] is not None]),
        worst_single_gross=min((p["single_gross"] for p in picks if p["single_gross"] is not None), default=None))
    totals = {}
    for p in picks:
        if p["single_net5"] is not None:
            totals[p["symbol"]] = totals.get(p["symbol"], 0)+p["single_net5"]/20
    best = max(totals, key=totals.get) if totals else None
    kept = []
    for r in rows:
        values = [p["single_net5"] for p in picks if p["date"] == r["date"] and p["symbol"] != best]
        if values and all(v is not None for v in values):
            kept.append(float(np.mean(values)))
    out.update(best_total_contributor=best, without_best_symbol=A.bootstrap(kept))
    return out


def write_csv(name, rows):
    with (OUT / name).open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["date", "model"])
        writer.writeheader()
        writer.writerows(rows)


def main():
    checked = checks()
    financials_path = OUT / "financials.json"
    records = json.loads(financials_path.read_text())
    panel, meta = A.build_panel()
    feats = P.compute_features(panel)
    eligible = P.eligible_mask(panel, feats)
    o, h, lo, c = panel.open, panel.high, panel.low, panel.close
    valid = np.isfinite(o) & np.isfinite(h) & np.isfinite(lo) & (o > 0) & (lo > 0) & (lo <= np.minimum(o, c)) & (h >= np.maximum(o, c))
    eligible &= P._trailing_complete(np.where(valid, c, np.nan), 61)
    for k in ("adv20", "r20", "r60", "vol20", "volume_ratio", "trend"):
        eligible &= np.isfinite(getattr(feats, k))
    ts = [t for t in P.valid_as_of(panel, 20) if t >= 60][::20]
    s_ix = panel.symbol_index()
    assert "SPY" in s_ix
    market = panel.close[:, s_ix["SPY"]]
    periods, picks, coverage, universe = [], [], [], []
    for t in ts:
        day = panel.sessions[t]
        latest = latest_at(records, day)
        rs = sorted((r for r in latest.values() if r["status"] == "complete" and r["symbol"] in s_ix
                     and eligible[t, s_ix[r["symbol"]]] and 0 <= (day-date.fromisoformat(f'{r["period"][:4]}-{r["period"][4:6]}-{r["period"][6:8]}')).days <= 500), key=lambda r: r["symbol"])
        ids = np.array([s_ix[r["symbol"]] for r in rs], dtype=int)
        ac, cb = np.array([r["ac"] for r in rs]), np.array([r["cb"] for r in rs])
        r5, beta, e5 = residual(panel.close, market, t, ids)
        pools = models(feats, t, ids, ac, cb, r5, e5) if len(ids) else {name: (np.array([], dtype=bool), np.array([])) for name in NAMES}
        coverage.append(dict(date=str(day), price_eligible=int(eligible[t].sum()), annual_asof=len(latest), complete_price_eligible=len(ids), **{name: int(mask.sum()) for name, (mask, _) in pools.items()}))
        for k, r in enumerate(rs):
            universe.append(dict(date=str(day), symbol=r["symbol"], cik=r["cik"], adsh=r["adsh"], accepted=r["accepted"],
                ac=r["ac"], cb=r["cb"], r5=float(r5[k]), beta=float(beta[k]), e5=float(e5[k]),
                **{key: float(getattr(feats, key)[t, ids[k]]) for key in ("adv20", "r20", "r60", "vol20", "volume_ratio", "trend")}))
        for name, (mask, score) in pools.items():
            at = np.flatnonzero(mask)
            at = at[np.lexsort((ids[at], -feats.adv20[t, ids[at]], -score[at]))][:20]
            chosen = ids[at]
            assert len(chosen) <= 20 and len({rs[k]["cik"] for k in at}) == len(chosen)
            for horizon in (20, 5):
                A.H = horizon
                row = dict(date=str(day), year=day.year, horizon=horizon, model=name, pool_n=int(mask.sum()), **A.measure(panel, t, chosen))
                periods.append(row)
                for rank, k in enumerate(at, 1):
                    r = rs[k]
                    col = ids[k]
                    entry, exit_ = panel.open[t+1, col], panel.close[t+horizon, col]
                    single = float(1-exit_/entry) if np.isfinite(entry) and entry > 0 and np.isfinite(exit_) and exit_ >= 0 else None
                    picks.append(dict(date=str(day), year=day.year, horizon=horizon, model=name, rank=rank, symbol=r["symbol"], cik=r["cik"],
                        score=float(score[k]), adsh=r["adsh"], accepted=r["accepted"], period=r["period"], ac=r["ac"], cb=r["cb"],
                        r5=float(r5[k]), beta=float(beta[k]), e5=float(e5[k]), single_gross=single,
                        single_net5=None if single is None else single-.001-.05*row["calendar_days"]/365,
                        historical_borrow="unknown", identity="current_SEC_mapping_not_historical_master"))
        print(day, "complete_company_pool", len(ids), flush=True)
    result = dict(status="exploratory; no independent holdout or historical borrow proof", checks=checked,
        periods=len(ts), method_sha256=hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest(),
        financials_sha256=hashlib.sha256(financials_path.read_bytes()).hexdigest(), data=meta, horizons={})
    for horizon in (20, 5):
        rr = [r for r in periods if r["horizon"] == horizon]
        pp = [p for p in picks if p["horizon"] == horizon]
        models_summary = {n: summarize([r for r in rr if r["model"] == n], [p for p in pp if p["model"] == n]) for n in NAMES}
        baseline = {r["date"]: r for r in rr if r["model"] == "price_weak"}
        paired = {}
        for n in NAMES[1:]:
            pairs = [(r, baseline[r["date"]]) for r in rr if r["model"] == n and r["n"] and baseline[r["date"]]["n"] and r["gross"] is not None and baseline[r["date"]]["gross"] is not None]
            paired[n] = dict(per_invested_delta=A.bootstrap([a["net_rt10bp_borrow5pct"]*20/a["n"]-b["net_rt10bp_borrow5pct"]*20/b["n"] for a, b in pairs]),
                full20_delta=A.bootstrap([a["net_rt10bp_borrow5pct"]-b["net_rt10bp_borrow5pct"] for a, b in pairs if a["n"] == b["n"] == 20]))
        groups = {}
        for label, pred in (("2016_2018", lambda y: y <= 2018), ("2019_2020", lambda y: y >= 2019), ("exclude2020", lambda y: y < 2020)):
            groups[label] = {n: summarize([r for r in rr if r["model"] == n and pred(r["year"])], [p for p in pp if p["model"] == n and pred(p["year"])]) for n in NAMES}
        criteria = {}
        for n in NAMES[1:]:
            m = models_summary[n]
            net, delta = m["per_invested_net5"], paired[n]["per_invested_delta"]
            tests = dict(net_positive=net["mean"] is not None and net["mean"] > 0,
                paired_ci_lower_positive=delta["ci95"] is not None and delta["ci95"][0] > 0,
                both_segments_positive=all(groups[g][n]["per_invested_net5"]["mean"] is not None and groups[g][n]["per_invested_net5"]["mean"] > 0 for g in ("2016_2018", "2019_2020")),
                enough_active=m["active_periods"] >= 20, enough_companies=m["unique_companies"] >= 10)
            criteria[n] = dict(tests, meets_exploratory_criteria=all(tests.values()), production_approved=False)
        result["horizons"][str(horizon)] = dict(models=models_summary, paired=paired, groups=groups,
            annual={str(y): {n: summarize([r for r in rr if r["model"] == n and r["year"] == y], [p for p in pp if p["model"] == n and p["year"] == y]) for n in NAMES} for y in range(2016, 2021)}, criteria=criteria)
    for name, rows in (("periods.csv", periods), ("selections.csv", picks), ("coverage.csv", coverage), ("universe.csv", universe)):
        write_csv(name, rows)
    (OUT / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    for horizon, item in result["horizons"].items():
        for n, m in item["models"].items():
            print(horizon, n, "active", m["active_periods"], "companies", m["unique_companies"], "net5", m["per_invested_net5"], flush=True)


if __name__ == "__main__":
    with np.errstate(divide="ignore", invalid="ignore"):
        if "--checks-only" in sys.argv:
            print(json.dumps(checks(), indent=2))
        else:
            main()
