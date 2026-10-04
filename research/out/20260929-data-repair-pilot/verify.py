"""Source-line audit plus the previous independent scalar price/ratio audit."""
import calendar
import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
OLD = ROOT / "research/out/20260929-mscore-validation"


def audit_sources():
    records = json.loads((OUT / "financials.json").read_text())
    audit = json.loads((OUT / "pilot_audit.json").read_text())
    source = json.loads((OUT / "notes_source.json").read_text())
    face_source = next(s for s in json.loads((OLD / "source_manifest.json").read_text())
                       if s["quarter"] == "2018q1")
    archives = {}
    metadata = {r["adsh"]: r for r in records if r["source_quarter"] == "2018q1"}
    metadata_checked = set()
    wanted = {"face": {}, "notes": {}}
    items = []
    for r in records:
        if r.get("fact_sources"):
            for key, traces in r["fact_sources"].items():
                for t in traces:
                    items.append((r["adsh"], key, r["facts"][key], t))
    for r in audit:
        for key, fact in r.get("added_facts", {}).items():
            for t in fact["sources"]:
                items.append((r["adsh"], key, fact["value"], t))
    for adsh, key, value, trace in items:
        wanted[trace["source"]].setdefault(trace["line"], []).append((adsh, key, value, trace))
    checked = 0
    for kind, filename, archive in (("face", "num.txt", "2018q1.zip"), ("notes", "num.tsv", "2018q1_notes.zip")):
        p = ROOT / "research/data/sec-mscore" / archive
        with p.open("rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
        assert digest == (source if kind == "notes" else face_source)["sha256"]
        archives[archive] = digest
        if kind == "notes":
            with zipfile.ZipFile(p) as z, z.open("sub.tsv") as fh:
                for raw in csv.DictReader(io.TextIOWrapper(fh), delimiter="\t"):
                    if raw["adsh"] in metadata:
                        for field in ("cik", "period", "accepted", "form", "instance"):
                            assert raw[field] == metadata[raw["adsh"]][field]
                        metadata_checked.add(raw["adsh"])
        found = set()
        with zipfile.ZipFile(p) as z, z.open(filename) as fh:
            reader = csv.DictReader(io.TextIOWrapper(fh), delimiter="\t")
            for line, raw in enumerate(reader, 2):
                if line not in wanted[kind]:
                    continue
                for adsh, key, value, trace in wanted[kind][line]:
                    tag, day, q = key.split("|")
                    assert raw["adsh"] == adsh and raw["tag"] == tag and raw["qtrs"] == q
                    assert raw["uom"] == "USD" and raw["version"].startswith("us-gaap/") and not raw["coreg"]
                    assert float(raw["value"]) == value and raw["ddate"] == trace["raw_date"]
                    if kind == "notes":
                        assert int(raw["dimh"], 16) == 0 and raw["dimn"] == "0"
                        rounded = date.fromisoformat(f'{raw["ddate"][:4]}-{raw["ddate"][4:6]}-{raw["ddate"][6:]}')
                        offset = round(float(raw["datp"])*calendar.monthrange(rounded.year, rounded.month)[1])
                        assert (rounded+timedelta(days=offset)).strftime("%Y%m%d") == day
                        assert q == "0" or abs(float(raw["durp"])) <= .04
                    else:
                        assert not raw["segments"] and raw["ddate"] == day
                    checked += 1
                found.add(line)
                if len(found) == len(wanted[kind]):
                    break
        assert found == set(wanted[kind])
        print("source lines verified", kind, len(found), flush=True)
    assert metadata_checked == set(metadata)
    return dict(source_references_verified=checked,
                source_archive_sha256=archives,
                filing_metadata_verified=len(metadata_checked),
                complete_repaired_accessions=sum(bool(r.get("fact_sources")) for r in records),
                pilot_added_values=sum(len(r.get("added_facts", {})) for r in audit),
                used_and_pilot_added_values_match_source=True,
                sample_sha256=hashlib.sha256((OUT / "sample.json").read_bytes()).hexdigest(),
                method_sha256=hashlib.sha256((OUT / "METHOD.md").read_bytes()).hexdigest())


def main():
    sources = audit_sources()
    (OUT / "source_verification.json").write_text(json.dumps(sources, indent=2)+"\n")
    spec = importlib.util.spec_from_file_location("scalar_verification", OLD / "verify.py")
    V = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(V)
    V.OUT = OUT
    original_source_check = V.verify_raw_financials

    def verify_financials(records):
        repaired = [r for r in records if r.get("fact_sources")]
        unchanged = [r for r in records if not r.get("fact_sources")]
        assert all(r["status"] == "complete" for r in repaired)
        # Repaired records were already checked line-by-line above; unchanged
        # main-strategy records retain the original direct quarterly TSV audit.
        return dict(repaired_main_accessions=len(repaired), repaired_source_audit=sources,
                    unchanged=original_source_check(unchanged) if unchanged else {})

    V.verify_raw_financials = verify_financials
    V.main()
    result = json.loads((OUT / "verification.json").read_text())
    summary = json.loads((OUT / "summary.json").read_text())
    assert result["method_sha256"] == summary["method_sha256"] == sources["method_sha256"]
    assert hashlib.sha256((OUT / "financials.json").read_bytes()).hexdigest() == summary["financials_sha256"]
    print(json.dumps(sources, indent=2), flush=True)


if __name__ == "__main__":
    main()
