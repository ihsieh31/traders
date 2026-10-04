"""Offline, fixed M-score plus price tests; see METHOD.md for all boundaries."""
from __future__ import annotations
import csv
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT))
from prepare import WEIGHTS, derive, mscore

spec = importlib.util.spec_from_file_location("short_diagnostics", ROOT / "research/out/20260929-short-screen/compare.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
np, P = A.np, A.P
NAMES = ("price_matched", "m_only_raw", "m_weak_raw", "m_weak_winsor")


def latest_at(records, as_of):
    latest = {}
    for r in records:
        if r["accepted"][:10] >= str(as_of):
            continue
        previous = latest.get(r["cik"])
        key = r["period"], r["accepted"], r["adsh"]
        if previous is None or key > (previous["period"], previous["accepted"], previous["adsh"]):
            latest[r["cik"]] = r
    return latest


def oracle(ratios):
    total = Decimal("-4.84")
    for key, coefficient in WEIGHTS.items():
        total += Decimal(str(coefficient))*Decimal(str(ratios[key]))
    return float(total)


def checks():
    values = {key: 1.0 for key in WEIGHTS}
    values["TATA"] = 0.0
    assert np.isclose(mscore(values), -2.48) and oracle(values) == -2.48
    values["TATA"] = .2
    assert np.isclose(mscore(values), -1.5442) and oracle(values) == -1.5442
    facts = {}
    bs = {"AccountsReceivableNetCurrent": 10, "Assets": 1000, "AssetsCurrent": 500,
          "PropertyPlantAndEquipmentNet": 300, "LiabilitiesCurrent": 200, "LongTermDebtNoncurrent": 100}
    flow = {"SalesRevenueNet": 100, "GrossProfit": 40, "Depreciation": 30,
            "SellingGeneralAndAdministrativeExpense": 10, "NetIncomeLoss": 10,
            "NetCashProvidedByUsedInOperatingActivities": 10}
    for day in ("20161231", "20151231"):
        facts.update({(tag, day, "0"): value for tag, value in bs.items()})
        facts.update({(tag, day, "4"): value for tag, value in flow.items()})
    derived = derive({"period": "20161231"}, facts)
    assert np.isclose(derived["mscore"], -2.48)
    scaled = derive({"period": "20161231"}, {key: value*1e6 for key, value in facts.items()})
    assert np.isclose(scaled["mscore"], derived["mscore"])
    missing = facts.copy()
    missing.pop(("Depreciation", "20161231", "4"))
    missing["DepreciationAndAmortization", "20161231", "4"] = 30
    try:
        derive({"period": "20161231"}, missing)
    except ValueError:
        pass
    else:
        raise AssertionError("D&A must not silently replace pure depreciation")
    original = dict(cik="1", accepted="2017-03-01 10:00:00", adsh="a", period="20161231", status="complete")
    future = dict(original, accepted="2019-03-01 10:00:00", adsh="c", period="20181231")
    assert latest_at([original, future], date(2017, 3, 2))["1"] == original
    assert not latest_at([original], date(2017, 3, 1))
    missing_latest = dict(original, accepted="2017-03-02 10:00:00", adsh="b", status="unavailable")
    assert latest_at([original, missing_latest], date(2017, 3, 3))["1"]["status"] == "unavailable"
    older_period_amendment = dict(original, accepted="2017-04-01 10:00:00", adsh="d", period="20151231")
    assert latest_at([original, older_period_amendment], date(2017, 4, 2))["1"] == original
    assert not (-1.78 > -1.78) and -1.5442 > -1.78
    return dict(decimal_oracle=True, scale_invariance=True, missing_rejected=True,
                no_DA_substitution=True, same_day_filing_excluded=True,
                future_filing_invariance=True, latest_missing_no_fallback=True,
                older_amendment_no_period_rollback=True, strict_threshold=True,
                short_measurement_checks=A.checks())


def summarize(rows):
    result = A.aggregate(rows)
    active = [r for r in rows if r["n"] > 0]
    result["active_periods"] = len(active)
    result["mean_capital_fraction"] = float(np.mean([r["n"]/20 for r in rows]))
    result["active_gross"] = A.bootstrap([r["gross"] for r in active if r["gross"] is not None])
    result["active_net5"] = A.bootstrap([r["net_rt10bp_borrow5pct"] for r in active if r["net_rt10bp_borrow5pct"] is not None])
    result["per_invested_notional_net5"] = A.bootstrap([
        r["net_rt10bp_borrow5pct"]*20/r["n"] for r in active if r["net_rt10bp_borrow5pct"] is not None])
    result["positive_active_net5_periods"] = sum(r["net_rt10bp_borrow5pct"] is not None and r["net_rt10bp_borrow5pct"] > 0 for r in active)
    return result


def main():
    checks_result = checks()
    financials_path = OUT / "financials.json"
    records = json.loads(financials_path.read_text())
    panel, panel_meta = A.build_panel()
    feats = P.compute_features(panel)
    price_eligible = P.eligible_mask(panel, feats)
    o, h, lo, c = panel.open, panel.high, panel.low, panel.close
    valid = (np.isfinite(o) & np.isfinite(h) & np.isfinite(lo) & (o > 0) & (lo > 0)
             & (lo <= np.minimum(o, c)) & (h >= np.maximum(o, c)))
    price_eligible &= P._trailing_complete(np.where(valid, c, np.nan), 61)
    ts = np.array([t for t in P.valid_as_of(panel, 20) if t >= 60][::20])
    s_ix = panel.symbol_index()
    rows, picks, coverage, oracle_errors = [], [], [], []
    for t in ts:
        day = panel.sessions[t]
        latest = latest_at(records, day)
        universe_records = [r for r in latest.values() if r["status"] == "complete"
                            and 0 <= (day-date.fromisoformat(f'{r["period"][:4]}-{r["period"][4:6]}-{r["period"][6:8]}')).days <= 500
                            and r["symbol"] in s_ix and price_eligible[t, s_ix[r["symbol"]]]]
        universe_records.sort(key=lambda r: r["symbol"])
        ids = np.array([s_ix[r["symbol"]] for r in universe_records], dtype=int)
        raw = np.array([r["mscore"] for r in universe_records])
        weak = (feats.trend[t, ids] < 0) & (feats.r20[t, ids] < 0)
        winsor = np.array([], dtype=float)
        if len(ids):
            ratios = np.array([[r["ratios"][key] for key in WEIGHTS] for r in universe_records])
            boundaries = np.quantile(ratios, [.01, .99], axis=0)
            clipped = np.clip(ratios, boundaries[0], boundaries[1])
            winsor = -4.84 + clipped @ np.array(list(WEIGHTS.values()))
            pp = {key: A.C.percentiles_fast(getattr(feats, key)[t, ids])
                  for key in ("adv20", "r20", "r60", "vol20", "volume_ratio")}
            price_score = 100*(.20*pp["adv20"]+.25*(1-pp["r20"])+.25*(1-pp["r60"])
                              +.15*(1-pp["vol20"])+.15*pp["volume_ratio"])
        else:
            price_score = np.array([])
        masks = {"price_matched": weak, "m_only_raw": raw > -1.78,
                 "m_weak_raw": weak & (raw > -1.78), "m_weak_winsor": weak & (winsor > -1.78)}
        score_map = dict(price_matched=price_score, m_only_raw=raw, m_weak_raw=raw, m_weak_winsor=winsor)
        coverage.append(dict(date=str(day), price_eligible=int(price_eligible[t].sum()),
                             annual_ciks_asof=len(latest), complete_price_eligible=len(ids),
                             price_weak_count=int(weak.sum()), raw_m_flagged=int((raw > -1.78).sum()),
                             raw_m_and_weak=int(masks["m_weak_raw"].sum()),
                             winsor_m_and_weak=int(masks["m_weak_winsor"].sum())))
        for name in NAMES:
            at = np.flatnonzero(masks[name])
            score = score_map[name]
            order = np.lexsort((ids[at], -feats.adv20[t, ids[at]], -score[at]))
            at = at[order][:20]
            chosen = ids[at]
            assert len(chosen) <= 20 and len({universe_records[k]["cik"] for k in at}) == len(chosen)
            row = dict(date=str(day), year=day.year, model=name, pool_n=int(masks[name].sum()),
                       **A.measure(panel, t, chosen))
            rows.append(row)
            for rank, k in enumerate(at, 1):
                r = universe_records[k]
                assert r["accepted"][:10] < str(day)
                delta = abs(oracle(r["ratios"])-r["mscore"])
                oracle_errors.append(delta)
                assert math.isclose(oracle(r["ratios"]), r["mscore"], rel_tol=1e-12, abs_tol=1e-10)
                picks.append(dict(date=str(day), model=name, rank=rank, symbol=r["symbol"],
                                  cik=r["cik"], name=r["name"], score=float(score[k]),
                                  raw_mscore=r["mscore"], accepted=r["accepted"], period=r["period"],
                                  previous_period=r["previous_period"], adsh=r["adsh"],
                                  asset_identity="current_SEC_mapping_to_historical_prices",
                                  historical_borrow="unknown", **r["ratios"]))
    result = dict(status="conditional exploratory test; no independently validated tradable alpha",
                  checks=checks_result, periods=len(ts), data=panel_meta,
                  method_sha256=hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest(),
                  financials_sha256=hashlib.sha256(financials_path.read_bytes()).hexdigest(),
                  maximum_decimal_oracle_error=max(oracle_errors, default=0),
                  models={name: summarize([r for r in rows if r["model"] == name]) for name in NAMES},
                  annual={str(y): {name: summarize([r for r in rows if r["model"] == name and r["year"] == y])
                                  for name in NAMES} for y in range(2016, 2021)},
                  exclude2020={name: summarize([r for r in rows if r["model"] == name and r["year"] < 2020]) for name in NAMES})
    baseline = {r["date"]: r for r in rows if r["model"] == "price_matched"}
    result["paired"] = {}
    for name in NAMES[1:]:
        pairs = [(r, baseline[r["date"]]) for r in rows if r["model"] == name
                 and r["n"] > 0 and baseline[r["date"]]["n"] > 0
                 and r["gross"] is not None and baseline[r["date"]]["gross"] is not None]
        full = [(a, b) for a, b in pairs if a["n"] == b["n"] == 20]
        result["paired"][name] = dict(
            cash_net5_delta=A.bootstrap([a["net_rt10bp_borrow5pct"]-b["net_rt10bp_borrow5pct"] for a, b in pairs]),
            per_invested_net5_delta=A.bootstrap([a["net_rt10bp_borrow5pct"]*20/a["n"]
                                                -b["net_rt10bp_borrow5pct"]*20/b["n"] for a, b in pairs]),
            full20_net5_delta=A.bootstrap([a["net_rt10bp_borrow5pct"]-b["net_rt10bp_borrow5pct"] for a, b in full]))
    for filename, items in (("periods.csv", rows), ("selections.csv", picks), ("coverage.csv", coverage)):
        with (OUT / filename).open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(items[0]) if items else ["date", "model", "symbol"])
            writer.writeheader()
            writer.writerows(items)
    (OUT / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    for name, item in result["models"].items():
        print(name, "active", item["active_periods"], "full20", item["full20_periods"],
              "names", item["total_name_observations"], "net5", item["returns"]["net_rt10bp_borrow5pct"], flush=True)


if __name__ == "__main__":
    with np.errstate(invalid="ignore", divide="ignore"):
        if "--checks-only" in sys.argv:
            print(json.dumps(checks(), indent=2))
        else:
            main()
