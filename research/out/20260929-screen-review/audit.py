"""Offline, fixed-spec diagnostics. Run with the repository .venv-p2 Python."""
from __future__ import annotations

import csv
import json
import socket
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import numpy as np
from research.src import common, factors as F, measure as M, panel as P
from research.src.tools.exclusion_screen import exclude_mask, survivor_score
from research.src.tools.stability_screen import forward_vol, risk

OUT = Path(__file__).resolve().parent
H = 20


def deny_network(*args, **kwargs):
    raise RuntimeError("This audit only permits local cache reads")


socket.socket.connect = deny_network
socket.create_connection = deny_network
common.require_credentials = lambda: None


def cached_calendar(start, end, **kwargs):
    rows = json.loads(common.CALENDAR_PATH.read_text())["rows"]
    assert min(r["date"] for r in rows) <= str(start)
    assert max(r["date"] for r in rows) >= str(end)
    return rows


common.load_calendar_rows = cached_calendar


def allocate_budget(candidates, holdings, budget=20):
    held = list(dict.fromkeys(holdings))
    if len(held) > budget:
        raise ValueError("held-risk review alone exceeds the total analysis budget")
    fresh = [s for s in dict.fromkeys(candidates) if s not in set(held)]
    return held + fresh[:budget - len(held)]


def self_check():
    from tradingagents.screening.pipeline import RoundPlan, ScreeningDeps, _attach_holdings
    symbols = [f"S{i:02}" for i in range(20)]
    plan = RoundPlan(top20=[{"symbol": s} for s in symbols])
    deps = ScreeningDeps(
        positions_fn=lambda: [{"symbol": s, "qty": 1} for s in [symbols[0], "EX1", "EX2"]],
        quarantine_fn=lambda config: lambda symbol: None,
        asset_fn=lambda symbol: {"tradable": True, "status": "active"},
    )
    actual = _attach_holdings(plan, {}, deps)
    assert len(actual.deep_analysis_set) == 22
    assert len(set(allocate_budget(symbols + symbols, [symbols[0], "EX1", "EX2"]))) == 20
    assert set([symbols[0], "EX1", "EX2"]).issubset(allocate_budget(symbols, [symbols[0], "EX1", "EX2"]))
    try:
        allocate_budget(symbols, [f"H{i}" for i in range(21)])
    except ValueError:
        pass
    else:
        raise AssertionError("must expose an infeasible holdings budget")
    return {"production_holdings_union": 22, "proposed_total_budget": 20,
            "holdings_over_budget": "explicit failure", "network": "blocked"}


def choose(scores, mask, ts):
    out = []
    for t in ts:
        cols = np.flatnonzero(mask[t] & np.isfinite(scores[t]))
        order = np.lexsort((cols, -scores[t, cols]))
        selected = cols[order][:20] if len(cols) >= 20 else np.array([], dtype=int)
        assert len(selected) in (0, 20)
        assert len(set(selected)) == len(selected)
        out.append(selected)
    return out


def bootstrap_delta(a, b, block=3):
    x = np.asarray(a) - np.asarray(b)
    x = x[np.isfinite(x)]
    rng = np.random.default_rng(20260929)
    n = len(x)
    samples = []
    for _ in range(2000):
        starts = rng.integers(0, n - block + 1, size=int(np.ceil(n / block)))
        samples.append(np.concatenate([x[s:s + block] for s in starts])[:n].mean())
    lo, hi = np.quantile(samples, [.025, .975])
    return {"delta": float(x.mean()), "ci95": [float(lo), float(hi)], "block_periods": block,
            "paired_periods": n}


def reproduction(panel, feats, prod, keep, scores, ts, start):
    fwd = P.forward_returns(panel, H)
    fv = forward_vol(panel, H)
    out = {"scheduled_periods": len(ts), "models": {}}
    for name in ("current20", "report28"):
        mask = prod if name == "current20" else keep
        pf = M.build_portfolio(scores[name], fwd, mask, horizon=H, top_k=20, as_of=ts)
        selected_vols, disp = [], []
        used_dates = []
        for t, ids in zip(ts, pf.names):
            if ids:
                used_dates.append(str(panel.sessions[t]))
                selected_vols.extend(fv[t, ids][np.isfinite(fv[t, ids])].tolist())
                rs = fwd[t, ids]
                if np.isfinite(rs).sum() >= 5:
                    disp.append(float(np.nanstd(rs)))
        out["models"][name] = {
            "selected_periods": len(used_dates), "first_selection_date": used_dates[0],
            "selection_dates_before_start": [d for d in used_dates if d < str(start)],
            "missing_end_returns": int(sum(len(ids) for ids in pf.names) - pf.n_available.sum()),
            "individual_fwd_vol_median": float(np.median(selected_vols)),
            "dispersion_mean": float(np.mean(disp)),
            "net2x_original_harness": M.cost_table(pf, H)["2.0x"]["annualised_net"],
            "mdd_original_harness": risk(panel, pf, H)["mdd"],
        }
    return out


def evaluate(panel, prod, keep, scores, ts):
    selections = {name: choose(sc, prod if name == "current20" else keep, ts)
                  for name, sc in scores.items()}
    totals, periods, examples = {}, [], []
    rate_series = {}
    cohorts = ["eligible", "keep", "removed", *scores]
    for name in cohorts:
        totals[name] = {key: 0 for key in ("n", "unknown_path", "unknown_end", "bad_path10", "bad_end10", "up_end10")}
        totals[name]["individual_vols"] = []
        totals[name]["dispersion"] = []
        rate_series[name] = []
    for i, t in enumerate(ts):
        entry = panel.open[t + 1]
        prices = panel.close[t + 1:t + H + 1]
        end_known = np.isfinite(entry) & (entry > 0) & np.isfinite(prices[-1])
        path_known = end_known & np.isfinite(prices).all(axis=0)
        endret = prices[-1] / entry - 1
        adverse = np.min(prices, axis=0) / entry - 1
        lv = np.log(panel.close[t + 1:t + H + 1] / panel.close[t:t + H])
        fvol = np.std(lv, axis=0, ddof=1) * np.sqrt(252)
        ids = {"eligible": np.flatnonzero(prod[t]), "keep": np.flatnonzero(keep[t]),
               "removed": np.flatnonzero(prod[t] & ~keep[t])}
        ids.update({name: picks[i] for name, picks in selections.items()})
        for name, cols in ids.items():
            n = len(cols)
            knownp, knowne = path_known[cols], end_known[cols]
            bp = int(np.count_nonzero(knownp & (adverse[cols] <= -.10)))
            be = int(np.count_nonzero(knowne & (endret[cols] <= -.10)))
            up = int(np.count_nonzero(knowne & (endret[cols] >= .10)))
            unknownp, unknowne = int(n - knownp.sum()), int(n - knowne.sum())
            counts = dict(n=n, unknown_path=unknownp, unknown_end=unknowne,
                          bad_path10=bp, bad_end10=be, up_end10=up)
            for key, value in counts.items():
                totals[name][key] += value
            vf = fvol[cols]
            totals[name]["individual_vols"].extend(vf[np.isfinite(vf)].tolist())
            if n == 20 and knowne.all():
                totals[name]["dispersion"].append(float(np.std(endret[cols])))
            periods.append(dict(date=str(panel.sessions[t]), model=name, **counts,
                                bad_path_rate_upper=(bp + unknownp) / n if n else None))
            rate_series[name].append((bp + unknownp) / n if n else np.nan)
            if name in scores:
                for j in cols:
                    if path_known[j] and adverse[j] <= -.10:
                        examples.append({"model": name, "date": str(panel.sessions[t]),
                                         "symbol": panel.symbols[j], "adverse_close": float(adverse[j]),
                                         "end_return": float(endret[j])})
        assert len(ids["keep"]) + len(ids["removed"]) == len(ids["eligible"])
    for name, counts in totals.items():
        n = counts["n"]
        for key, unk in (("bad_path10", "unknown_path"), ("bad_end10", "unknown_end")):
            counts[key + "_bounds"] = [counts[key] / n, (counts[key] + counts[unk]) / n]
        counts["up_end10_rate"] = counts["up_end10"] / n
        vols = counts.pop("individual_vols")
        counts["individual_fwd_vol_median"] = float(np.median(vols))
        disp = counts.pop("dispersion")
        counts["dispersion_mean"] = float(np.mean(disp)) if disp else None
        finite_rates = np.asarray(rate_series[name]); finite_rates = finite_rates[np.isfinite(finite_rates)]
        counts["mean_date_bad_path_upper"] = float(finite_rates.mean())
    paired = {}
    for a, b in (("report28", "current20"), ("reuse28", "current20"),
                 ("reuse28", "report28"), ("keep", "removed")):
        paired[a + "_minus_" + b] = [bootstrap_delta(rate_series[a], rate_series[b], block=k) for k in (1, 3, 6)]
    common_dates = np.isfinite(rate_series["report28"]) & np.isfinite(rate_series["current20"])
    matched = {name: {"periods": int(common_dates.sum()),
                     "bad_path_upper_rate": float(np.mean(np.asarray(rate_series[name])[common_dates]))}
               for name in scores}
    for row in periods:
        row["split"] = str(panel.sessions[ts[0]].year) + "-" + str(panel.sessions[-1].year)
    return totals, paired, matched, periods, sorted(examples, key=lambda x: x["adverse_close"])


def prefix_check(panel, feats, prod):
    # Features and both orderings must agree when future rows are absent.
    available = np.flatnonzero(prod.sum(axis=1) >= 40)
    assert len(available)
    t = int(available[min(40, len(available) - 1)])
    cols = np.flatnonzero(prod[t])[:40]
    assert len(cols) == 40
    kwargs = {key: getattr(panel, key)[:t + 1, cols].copy() for key in P.FIELDS}
    prefix = P.Panel(sessions=panel.sessions[:t + 1], symbols=[panel.symbols[j] for j in cols],
                     calendar_rows=panel.calendar_rows[:t + 1], **kwargs)
    pf = P.compute_features(prefix)
    for key in ("price", "adv20", "r60", "vol20", "trend"):
        assert np.allclose(getattr(pf, key)[t], getattr(feats, key)[t, cols], equal_nan=True, atol=1e-9)
    pp = P.eligible_mask(prefix, pf)
    full = SimpleNamespace(**{key: getattr(feats, key)[:, cols] for key in feats.__dataclass_fields__})
    pm = exclude_mask(prefix, pf, pp, vol_ceiling=.28)
    fm = exclude_mask(panel, full, prod[:, cols], vol_ceiling=.28)
    assert np.array_equal(pm[t], fm[t])
    assert np.allclose(survivor_score(pf, pm)[t], survivor_score(full, fm)[t], equal_nan=True)
    assert np.allclose(F.production_score(pf, pm)[t], F.production_score(full, fm)[t], equal_nan=True)
    return True


def main():
    result = {"self_check": self_check(), "splits": {},
              "note": "historical diagnosis, neither split is an untouched holdout"}
    all_periods = []
    all_examples = []
    for start, end in ((date(2016, 1, 4), date(2020, 12, 31)),
                       (date(2021, 1, 4), date(2025, 12, 31))):
        label = f"{start.year}-{end.year}"
        print(f"Evaluating {label}", flush=True)
        panel = P.build_panel(start=start, end=end, symbols_file=str(ROOT / "research/out/trusted_symbols.txt"))
        feats = P.compute_features(panel)
        prod = P.eligible_mask(panel, feats)
        keep = exclude_mask(panel, feats, prod, vol_ceiling=.28)
        scores = {"current20": F.production_score(feats, prod),
                  "report28": survivor_score(feats, keep),
                  "reuse28": F.production_score(feats, keep)}
        legacy_ts = np.flatnonzero(P.valid_as_of(panel, H))[::H]
        orig = reproduction(panel, feats, prod, keep, scores, legacy_ts, start)
        ts = np.array([t for t in P.valid_as_of(panel, H) if start <= panel.sessions[t] <= end][::H])
        assert all(start <= panel.sessions[t] and panel.sessions[t + H] <= end for t in ts)
        totals, paired, matched, periods, examples = evaluate(panel, prod, keep, scores, ts)
        days = np.array([i for i, d in enumerate(panel.sessions) if start <= d <= end])
        pool = keep[days].sum(axis=1)
        result["splits"][label] = {
            "sessions": len(days), "periods": len(ts), "first_as_of": str(panel.sessions[ts[0]]),
            "last_as_of": str(panel.sessions[ts[-1]]), "prefix_invariance": prefix_check(panel, feats, prod),
            "pool_min": int(pool.min()), "pool_median": float(np.median(pool)),
            "pool_max": int(pool.max()), "days_pool_below20": int((pool < 20).sum()),
            "insufficient_after_eligible_ready": [
                {"date": str(panel.sessions[t]), "survivors": int(keep[t].sum()),
                 "eligible": int(prod[t].sum())}
                for t in days if keep[t].sum() < 20 and prod[t].sum() >= 20],
            "reproduction": orig, "cohorts": totals, "paired_bad_path_delta": paired,
            "matched_selection_rates": matched,
        }
        all_periods.extend(periods)
        all_examples.extend(examples)
        print(json.dumps(result["splits"][label], ensure_ascii=False), flush=True)
        del scores, keep, prod, feats, panel
    (OUT / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (OUT / "periods.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_periods[0]))
        writer.writeheader(); writer.writerows(all_periods)
    (OUT / "bad_examples.json").write_text(json.dumps(all_examples, ensure_ascii=False, indent=2) + "\n")
    print("OFFLINE AUDIT COMPLETE", flush=True)


if __name__ == "__main__":
    with np.errstate(invalid="ignore", divide="ignore"):
        main()
