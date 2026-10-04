"""Repair the frozen pilot and one filing quarter without changing the formula."""
import calendar
from collections import Counter
import csv
from datetime import timedelta
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import zipfile

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
OLD = ROOT / "research/out/20260929-mscore-validation"
spec = importlib.util.spec_from_file_location("original_financial_formula", OLD / "prepare.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


def exact_date(day, datp):
    rounded = P.parse_day(day)
    days = calendar.monthrange(rounded.year, rounded.month)[1]
    delta = round(float(datp)*days)
    if abs(float(datp)-delta/days) >= 1e-6 or abs(delta) > 16:
        raise ValueError("date offset cannot be restored unambiguously")
    return (rounded+timedelta(days=delta)).strftime("%Y%m%d")


def main():
    records = json.loads((OLD / "financials.json").read_text())
    original = {r["adsh"]: r for r in records}
    targets = {r["adsh"]: r for r in records if r["source_quarter"] == "2018q1"}
    sample = json.loads((OUT / "sample.json").read_text())
    sample_ids = {r["adsh"] for r in sample}
    source = json.loads((OUT / "notes_source.json").read_text())
    ignored = Counter()
    notes_path = P.CACHE / "2018q1_notes.zip"
    with notes_path.open("rb") as fh:
        assert hashlib.file_digest(fh, "sha256").hexdigest() == source["sha256"]
    values, provenance, alternatives = ({adsh: {} for adsh in targets} for _ in range(3))

    def add(adsh, key, value, trace):
        prior = values[adsh].get(key, "absent")
        if prior != "absent" and prior != value:
            values[adsh][key] = None
        else:
            values[adsh][key] = value
        provenance[adsh].setdefault("|".join(key), []).append(trace)

    with zipfile.ZipFile(P.CACHE / "2018q1.zip") as z:
        with z.open("num.txt") as fh:
            for frame in pd.read_csv(fh, sep="\t", dtype=str, keep_default_na=False, chunksize=250000):
                mask = frame.adsh.isin(targets) & frame.tag.isin(P.TAGS)
                mask &= frame.version.str.startswith("us-gaap/") & (frame.uom == "USD")
                mask &= (frame.segments == "") & (frame.coreg == "") & frame.qtrs.isin(("0", "4"))
                for index, r in frame.loc[mask].iterrows():
                    if not r.value or not "20150101" <= r.ddate <= targets[r.adsh]["period"]:
                        continue
                    value = float(r.value)
                    if math.isfinite(value):
                        add(r.adsh, (r.tag, r.ddate, r.qtrs), value,
                            dict(source="face", file="num.txt", line=int(index)+2, raw_date=r.ddate, value=value))

    with zipfile.ZipFile(notes_path) as z:
        checked_meta = set()
        with z.open("sub.tsv") as fh:
            for r in csv.DictReader(io.TextIOWrapper(fh), delimiter="\t"):
                if r["adsh"] in targets:
                    for k in ("cik", "period", "accepted", "form", "instance"):
                        assert r[k] == targets[r["adsh"]][k], (r["adsh"], k)
                    checked_meta.add(r["adsh"])
        assert checked_meta == set(targets)
        with z.open("num.tsv") as fh:
            for frame in pd.read_csv(fh, sep="\t", dtype=str, keep_default_na=False, chunksize=250000):
                mask = frame.adsh.isin(targets) & (frame.uom == "USD")
                mask &= (frame.dimn == "0") & (frame.coreg == "") & frame.qtrs.isin(("0", "4"))
                mask &= frame.ddate.between("20150101", "20171231")
                keep = frame.loc[mask]
                for index, r in keep.iterrows():
                    if not r.datp or not r.durp or int(r.dimh, 16) != 0:
                        continue
                    try:
                        day = exact_date(r.ddate, r.datp)
                    except ValueError:
                        ignored["unrecoverable_date_offset"] += 1
                        continue
                    if not "20150101" <= day <= targets[r.adsh]["period"]:
                        continue
                    if r.qtrs == "4" and abs(float(r.durp)) > .04:
                        continue
                    if any(s in r.tag for s in ("Depreciat", "Receiv", "Revenue", "Selling", "Administrat", "LongTermDebt")):
                        alternatives[r.adsh][r.tag] = r.version
                    if r.tag not in P.TAGS or not r.version.startswith("us-gaap/") or not r.value:
                        continue
                    value = float(r.value)
                    if not math.isfinite(value):
                        continue
                    add(r.adsh, (r.tag, day, r.qtrs), value,
                        dict(source="notes", file="num.tsv", line=int(index)+2, raw_date=r.ddate,
                             actual_date=day, tag=r.tag, version=r.version, qtrs=r.qtrs, uom=r.uom,
                             dimh=r.dimh, dimn=r.dimn, coreg=r.coreg, durp=r.durp, datp=r.datp,
                             value=value, source_sha256=source["sha256"]))
                print("notes rows", int(frame.index[-1])+1, flush=True)

    repaired, audit = {}, []
    for adsh, meta in targets.items():
        item = {k: meta[k] for k in ("adsh", "cik", "name", "sic", "form", "period", "filed", "accepted", "instance", "symbol", "source_quarter")}
        try:
            item.update(status="complete", **P.derive(meta, values[adsh]))
            item["fact_sources"] = {k: provenance[adsh][k] for k in item["facts"]}
        except ValueError as exc:
            item.update(status="unavailable", reason=str(exc))
        repaired[adsh] = item
        added = ["|".join(k) for k, v in values[adsh].items() if v is not None
                 and all(t["source"] != "face" for t in provenance[adsh]["|".join(k)])]
        row = dict(adsh=adsh, symbol=meta["symbol"], cik=meta["cik"], accepted=meta["accepted"],
            before_status=meta["status"], before_reason=meta.get("reason"),
            after_status=item["status"], after_reason=item.get("reason"),
            added_keys=added, conflicting_keys=["|".join(k) for k, v in values[adsh].items() if v is None],
            available_related_tags=alternatives[adsh], mscore=item.get("mscore"))
        if adsh in sample_ids:
            row["added_facts"] = {k: dict(value=values[adsh][tuple(k.split("|"))], sources=provenance[adsh][k]) for k in added}
        row["conflicting_facts"] = {k: provenance[adsh][k] for k in row["conflicting_keys"]}
        audit.append(row)
    combined = [repaired.get(r["adsh"], r) for r in records]
    sample_audit = [r for r in audit if r["adsh"] in sample_ids]
    counts = Counter()
    for r in sample_audit:
        counts[r["before_reason"]+" -> "+(r["after_reason"] or "complete")] += 1
    result = dict(sample_n=len(sample), sample_complete=sum(r["after_status"] == "complete" for r in sample_audit),
        sample_transitions=dict(counts), quarter_records=len(targets),
        quarter_complete_before=sum(r["status"] == "complete" for r in targets.values()),
        quarter_complete_after=sum(r["status"] == "complete" for r in repaired.values()),
        newly_complete=sum(r["before_status"] != "complete" and r["after_status"] == "complete" for r in audit),
        lost_complete=sum(r["before_status"] == "complete" and r["after_status"] != "complete" for r in audit),
        all_records=len(combined), all_complete=sum(r["status"] == "complete" for r in combined),
        ignored_notes_rows=dict(ignored),
        method_sha256=hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest(),
        sample_sha256=hashlib.sha256((OUT / "sample.json").read_bytes()).hexdigest(), source=source)
    for name, value in (("financials.json", combined), ("pilot_audit.json", sample_audit),
                        ("quarter_audit.json", audit), ("repair_summary.json", result)):
        (OUT / name).write_text(json.dumps(value, indent=2, allow_nan=False)+"\n")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
