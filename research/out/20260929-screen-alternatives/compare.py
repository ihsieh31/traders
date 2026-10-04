"""Five fixed, offline screener alternatives; see METHOD.md before interpreting."""
from __future__ import annotations
import csv
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
review_path = ROOT / "research/out/20260929-screen-review/audit.py"
spec = importlib.util.spec_from_file_location("screen_review", review_path)
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
np, P, F = A.np, A.P, A.F
OUT = Path(__file__).resolve().parent
NEW = ("down20", "down20_dd60", "efficient60", "relative80", "core15_plus5")


def new_features(close):
    ret = np.full_like(close, np.nan)
    lr = np.full_like(close, np.nan)
    ret[1:] = close[1:] / close[:-1] - 1
    lr[1:] = np.log(close[1:] / close[:-1])
    down20 = np.sqrt(252 * P._rolling_mean(np.minimum(ret, 0)**2, 20))
    dd60 = np.full_like(close, np.nan)
    for t in range(59, len(close)):
        dd60[t] = close[t] / np.max(close[t-59:t+1], axis=0) - 1
    total = P._rolling_mean(np.abs(lr), 60) * 60
    net = np.full_like(close, np.nan)
    net[60:] = np.log(close[60:] / close[:-60])
    er60 = np.divide(net, total, out=np.zeros_like(net), where=total > 0)
    er60[~np.isfinite(total)] = np.nan
    return down20, dd60, er60


def models(panel, feats, prod):
    down, dd, er = new_features(panel.close)
    report = A.exclude_mask(panel, feats, prod, vol_ceiling=.28)
    downside = prod & (down <= .20) & (feats.trend > 0) & (feats.r60 >= -.25)
    masks = {"current20": prod, "report28": report, "reuse28": report,
             "down20": downside, "down20_dd60": downside & (dd >= -.25),
             "efficient60": prod & (feats.vol20 <= .45) & (dd >= -.25) & (feats.r60 > 0)}
    relative = np.zeros_like(prod)
    for t in range(len(prod)):
        cols = np.flatnonzero(prod[t])
        if len(cols):
            relative[t, cols] = np.asarray(F.percentiles_fast(feats.vol20[t, cols])) <= .80
    masks["relative80"] = relative & (dd >= -.25) & (feats.r60 >= -.25)
    scores = {name: F.production_score(feats, mask, min_n=1) for name, mask in masks.items()}
    scores["report28"] = A.survivor_score(feats, report, min_n=1)
    efficient = np.full_like(panel.close, np.nan)
    for t in range(len(prod)):
        cols = np.flatnonzero(masks["efficient60"][t])
        if len(cols):
            efficient[t, cols] = (np.asarray(F.percentiles_fast(er[t, cols]))
                                 + np.asarray(F.percentiles_fast(feats.adv20[t, cols]))
                                 + 1 - np.asarray(F.percentiles_fast(down[t, cols]))) / 3
    scores["efficient60"] = efficient
    return masks, scores


def pick(sc, mask, t, count=20):
    cols = np.flatnonzero(mask[t] & np.isfinite(sc[t]))
    return cols[np.lexsort((cols, -sc[t, cols]))][:count]


def selections(masks, scores, ts):
    out = {name: [pick(scores[name], masks[name], t) for t in ts] for name in masks}
    core = []
    for t in ts:
        first = pick(scores["reuse28"], masks["reuse28"], t, 15)
        more = pick(scores["down20"], masks["down20"], t, 20)
        more = np.asarray([j for j in more if j not in set(first)], dtype=int)[:5]
        core.append(np.concatenate((first, more)))
    out["core15_plus5"] = core
    for periods in out.values():
        for ids in periods:
            assert len(ids) <= 20 and len(set(ids)) == len(ids)
    return out


def checks():
    # Upward movement is not downside deviation; constant prices have zero ER.
    prices = np.tile(100 * 1.01**np.arange(90)[:, None], (1, 3))
    prices[:, 1] = 100 * .99**np.arange(90)
    prices[:, 2] = 100
    down, dd, er = new_features(prices)
    assert down[-1, 0] == 0 and down[-1, 2] == 0
    assert np.isclose(down[-1, 1], .01 * np.sqrt(252))
    assert np.isclose(er[-1, 0], 1) and np.isclose(er[-1, 1], -1) and er[-1, 2] == 0
    assert dd[-1, 0] == 0 and dd[-1, 2] == 0
    assert np.isclose(dd[-1, 1], .99**59 - 1)
    short = new_features(prices[:75])
    for x, y in zip((down, dd, er), short):
        assert np.allclose(x[:75], y, equal_nan=True)
    fake_masks = {"reuse28": np.ones((1, 30), dtype=bool), "down20": np.ones((1, 30), dtype=bool)}
    fake_scores = {"reuse28": np.arange(30)[None, :], "down20": np.arange(30)[None, :]}
    selected = selections(fake_masks, fake_scores, [0])["core15_plus5"][0]
    assert len(selected) == len(set(selected)) == 20
    return True


def measure(panel, selected, ts):
    rows = []
    for i, t in enumerate(ts):
        entry = panel.open[t+1]
        future = panel.close[t+1:t+21]
        known_end = np.isfinite(entry) & (entry > 0) & np.isfinite(future[-1])
        known_path = known_end & np.isfinite(future).all(axis=0)
        end = future[-1] / entry - 1
        adverse = np.min(future, axis=0) / entry - 1
        for name, choices in selected.items():
            ids = choices[i]
            n = len(ids)
            counts = {"n": n, "unknown_path": int((~known_path[ids]).sum()),
                      "unknown_end": int((~known_end[ids]).sum()),
                      "bad_path10": int((known_path[ids] & (adverse[ids] <= -.10)).sum()),
                      "bad_path20": int((known_path[ids] & (adverse[ids] <= -.20)).sum()),
                      "bad_end10": int((known_end[ids] & (end[ids] <= -.10)).sum()),
                      "up_end10": int((known_end[ids] & (end[ids] >= .10)).sum())}
            rows.append(dict(date=str(panel.sessions[t]), model=name, **counts))
    return rows


def summarize(rows, masks, days, prod):
    out = {}
    by = {name: {r["date"]: r for r in rows if r["model"] == name}
          for name in [*masks, "core15_plus5"]}
    for name, data in by.items():
        totals = {key: sum(r[key] for r in data.values())
                  for key in ("n", "unknown_path", "unknown_end", "bad_path10", "bad_path20", "bad_end10", "up_end10")}
        totals["full20_periods"] = sum(r["n"] == 20 for r in data.values())
        totals["partial_periods"] = sum(0 < r["n"] < 20 for r in data.values())
        totals["zero_periods"] = sum(r["n"] == 0 for r in data.values())
        if name in masks:
            pool = masks[name][days].sum(axis=1)
            ready = prod[days].sum(axis=1) >= 20
            totals["pool_median_ready"] = float(np.median(pool[ready]))
            totals["pool_min_ready"] = int(pool[ready].min())
            totals["pool_below20_days_ready"] = int((pool[ready] < 20).sum())
        out[name] = totals
    paired = {}
    for name in NEW:
        for baseline in ("report28", "reuse28", "current20"):
            ds = sorted(d for d, r in by[name].items() if r["n"] == 20 and by[baseline][d]["n"] == 20)
            a = [by[name][d] for d in ds]
            b = [by[baseline][d] for d in ds]
            metrics = {}
            for metric, unknown in (("bad_path10", "unknown_path"), ("up_end10", "unknown_end")):
                av = [(r[metric] + (r[unknown] if metric == "bad_path10" else 0))/20 for r in a]
                bv = [(r[metric] + (r[unknown] if metric == "bad_path10" else 0))/20 for r in b]
                delta = A.bootstrap_delta(av, bv)
                delta.update(candidate_rate=float(np.mean(av)), baseline_rate=float(np.mean(bv)))
                metrics[metric] = delta
            metrics["n_dates"] = len(ds)
            metrics["observed_dominance"] = (metrics["bad_path10"]["delta"] <= 0
                                              and metrics["up_end10"]["delta"] >= 0)
            paired[name + "_vs_" + baseline] = metrics
    return out, paired


def main():
    result = {"self_check": checks(), "new_variants": list(NEW), "splits": {},
              "status": "exploratory comparisons on previously used data"}
    all_rows = []
    for start, end in ((date(2016, 1, 4), date(2020, 12, 31)), (date(2021, 1, 4), date(2025, 12, 31))):
        label = f"{start.year}-{end.year}"
        print(label, flush=True)
        panel = P.build_panel(start=start, end=end, symbols_file=str(ROOT / "research/out/trusted_symbols.txt"))
        feats = P.compute_features(panel)
        prod = P.eligible_mask(panel, feats)
        masks, scores = models(panel, feats, prod)
        ts = np.array([t for t in P.valid_as_of(panel, 20) if panel.sessions[t] >= start][::20])
        days = np.array([t for t, day in enumerate(panel.sessions) if day >= start])
        chosen = selections(masks, scores, ts)
        rows = measure(panel, chosen, ts)
        summary, paired = summarize(rows, masks, days, prod)
        result["splits"][label] = dict(scheduled_periods=len(ts), models=summary, paired=paired)
        for row in rows:
            row["split"] = label
        all_rows.extend(rows)
        print(json.dumps(result["splits"][label]), flush=True)
        del chosen, masks, scores, prod, feats, panel
    result["observed_dominates_report_both_splits"] = [
        name for name in NEW if all(v["paired"][name+"_vs_report28"]["observed_dominance"] for v in result["splits"].values())]
    (OUT / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (OUT / "periods.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_rows[0]))
        writer.writeheader(); writer.writerows(all_rows)
    print("ALL FIVE VARIANTS COMPLETE", flush=True)


if __name__ == "__main__":
    with np.errstate(invalid="ignore", divide="ignore"):
        main()
