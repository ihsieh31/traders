"""Three-field, as-filed financial proxies; see the frozen METHOD.md."""
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import zipfile

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
OLD = ROOT / "research/out/20260929-mscore-validation"
CACHE = ROOT / "research/data/sec-mscore"
TAGS = {"NetIncomeLoss": "4", "NetCashProvidedByUsedInOperatingActivities": "4", "Assets": "0"}


def derive(facts):
    if any(facts.get(tag) is None or not math.isfinite(facts[tag]) for tag in TAGS):
        raise ValueError("missing_or_conflicting_required_fact")
    a, ni, cfo = (facts[tag] for tag in ("Assets", "NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities"))
    if a <= 0:
        raise ValueError("nonpositive_assets")
    ac, cb = (ni-cfo)/a, -cfo/a
    if not math.isfinite(ac) or not math.isfinite(cb):
        raise ValueError("nonfinite_ratio")
    return dict(ac=ac, cb=cb, assets=a, net_income=ni, cfo=cfo)


def main():
    records = json.loads((OLD / "financials.json").read_text())
    manifest = json.loads((OLD / "source_manifest.json").read_text())
    sources = {r["quarter"]: r for r in manifest}
    result = []
    for year in range(2015, 2021):
        for quarter in range(1, 5):
            token = f"{year}q{quarter}"
            metas = {r["adsh"]: {k: r[k] for k in ("adsh", "cik", "name", "sic", "form", "period", "filed", "accepted", "instance", "symbol", "source_quarter")}
                     for r in records if r["source_quarter"] == token}
            p = CACHE / f"{token}.zip"
            with p.open("rb") as fh:
                digest = hashlib.file_digest(fh, "sha256").hexdigest()
            assert digest == sources[token]["sha256"]
            values, traces = {s: {} for s in metas}, {s: {} for s in metas}
            with zipfile.ZipFile(p) as z:
                checked = set()
                with z.open("sub.txt") as fh:
                    for row in csv.DictReader(io.TextIOWrapper(fh), delimiter="\t"):
                        if row["adsh"] in metas:
                            assert all(row[k] == metas[row["adsh"]][k] for k in ("cik", "period", "accepted", "form", "instance"))
                            checked.add(row["adsh"])
                assert checked == set(metas)
                with z.open("num.txt") as fh:
                    for frame in pd.read_csv(fh, sep="\t", dtype=str, keep_default_na=False, chunksize=250000):
                        mask = frame.adsh.isin(metas) & frame.tag.isin(TAGS)
                        mask &= frame.version.str.startswith("us-gaap/") & (frame.uom == "USD")
                        mask &= (frame.segments == "") & (frame.coreg == "")
                        mask &= frame.qtrs == frame.tag.map(TAGS)
                        mask &= frame.ddate == frame.adsh.map({s: m["period"] for s, m in metas.items()})
                        mask &= frame.ddate.between("20150101", "20201231")
                        for index, r in frame.loc[mask].iterrows():
                            if not r.value:
                                continue
                            value = float(r.value)
                            if not math.isfinite(value):
                                continue
                            old = values[r.adsh].get(r.tag, "absent")
                            values[r.adsh][r.tag] = None if old != "absent" and old != value else value
                            traces[r.adsh].setdefault(r.tag, []).append(dict(archive=p.name, sha256=digest,
                                file="num.txt", line=int(index)+2, tag=r.tag, date=r.ddate, qtrs=r.qtrs, value=value))
            for adsh, meta in metas.items():
                row = dict(meta)
                try:
                    row.update(status="complete", **derive(values[adsh]), facts=values[adsh], fact_sources=traces[adsh])
                except ValueError as exc:
                    row.update(status="unavailable", reason=str(exc))
                result.append(row)
            print(token, len(metas), "complete", sum(r["status"] == "complete" for r in result if r["source_quarter"] == token), flush=True)
    assert len(result) == len(records)
    result.sort(key=lambda r: (r["accepted"], r["adsh"]))
    coverage = dict(annual_records=len(result), complete_records=sum(r["status"] == "complete" for r in result),
        complete_ciks=len({r["cik"] for r in result if r["status"] == "complete"}),
        method_sha256=hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest(),
        metadata_sha256=hashlib.sha256((OLD / "financials.json").read_bytes()).hexdigest())
    (OUT / "financials.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    (OUT / "financial_coverage.json").write_text(json.dumps(coverage, indent=2)+"\n")
    (OUT / "source_manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(coverage, flush=True)


if __name__ == "__main__":
    main()
