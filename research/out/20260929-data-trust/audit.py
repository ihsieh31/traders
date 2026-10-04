"""Audit existing frozen inputs, with one explicitly reviewed filing repair.

No new factors, profits, live requests, or holdout data. The live-path demo is
synthetic and clearly separate from the SEC findings.
"""
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PREV = OUT.parent / "20260929-short-formula-search"
sys.path.insert(0,str(ROOT))
from tradingagents.screening.quality import assess_financial_facts, historical_research_gate


def digest(path):
    with path.open("rb") as fh:
        return hashlib.file_digest(fh,"sha256").hexdigest()


def normalize(row, scopes=None):
    normalized={}
    mapping={"net_income":"NetIncomeLoss", "operating_cash_flow":"NetCashProvidedByUsedInOperatingActivities", "assets":"Assets"}
    for concept,tag in mapping.items():
        source=row["fact_sources"][tag][0]
        normalized[concept]=dict(value=row["facts"][tag],unit="USD",qtrs=int(source["qtrs"]),
            period_end=f'{source["date"][:4]}-{source["date"][4:6]}-{source["date"][6:8]}',
            available_at=row["accepted"],scope=(scopes or {}).get(concept),source_tag=tag,
            source=f'{source["archive"]}:num.txt:{source["line"]}',source_sha256=source["sha256"])
    return normalized


def main():
    path=PREV/"financials.json"
    # Hash before parsing: this run must not acquire an altered-year dataset.
    expected="3a13d4e340f90ec503e62d04fe6d7c58592c59b71d708c26f480a34d53a0f986"
    assert digest(path)==expected
    records=json.loads(path.read_text())
    known=json.loads((PREV/"semantic_audit.json").read_text())
    rows=[]
    for row in records:
        if row["status"]=="complete":
            assert "20150101"<=row["period"]<="20201231"
            scope={"net_income":"shareholder_attributable", "operating_cash_flow":"consolidated", "assets":"consolidated"} if row["adsh"]==known["adsh"] else {}
            result=assess_financial_facts(normalize(row,scope),as_of="2021-01-01")
            # 2021-01-01 is only the exclusive cutoff of the authorized panel;
            # it does not open, parse, or evaluate any 2021 data.
        else:
            result={"status":"unknown", "reason":row.get("reason","missing_required_facts")}
        rows.append(dict(adsh=row["adsh"],symbol=row["symbol"],cik=row["cik"],period=row["period"],
            numeric_complete=row["status"]=="complete",quality_status=result["status"],quality_reason=result["reason"]))
    with (OUT/"financial_quality.csv").open("w",newline="") as fh:
        writer=csv.DictWriter(fh,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)

    example=next(r for r in records if r["adsh"]==known["adsh"])
    scopes={"net_income":"shareholder_attributable", "operating_cash_flow":"consolidated", "assets":"consolidated"}
    original=normalize(example,scopes)
    archive=ROOT/"research/data/sec-mscore/2020q1.zip"
    assert digest(archive)==known["source_sha256"]
    targets={f["line"] for f in known["facts"]}|{example["fact_sources"]["Assets"][0]["line"]}
    verified={}
    with zipfile.ZipFile(archive) as z:
        with z.open("num.txt") as binary:
            fh=io.TextIOWrapper(binary)
            header=next(csv.reader([next(fh)],delimiter="\t"))
            for line_number,line in enumerate(fh,2):
                if line_number in targets:
                    raw=dict(zip(header,next(csv.reader([line],delimiter="\t"))))
                    assert raw["adsh"]==known["adsh"] and raw["ddate"]=="20191231"
                    assert raw["uom"]=="USD" and not raw["segments"] and not raw["coreg"]
                    assert raw["version"].startswith("us-gaap/")
                    assert int(raw["qtrs"])==(0 if raw["tag"]=="Assets" else 4)
                    verified[raw["tag"]]=dict(raw,line=line_number)
                if line_number>=max(targets):break
    assert len(verified)==4
    assert float(verified["NetIncomeLoss"]["value"])==original["net_income"]["value"]
    assert float(verified["NetCashProvidedByUsedInOperatingActivities"]["value"])==original["operating_cash_flow"]["value"]
    assert float(verified["Assets"]["value"])==original["assets"]["value"]
    for fact in known["facts"]:
        assert float(verified[fact["tag"]]["value"])==fact["value"]
    canonical=json.loads(json.dumps(original))
    canonical["net_income"].update(value=float(verified["ProfitLoss"]["value"]),scope="consolidated",
        source_tag="ProfitLoss",source=f'2020q1.zip:num.txt:{verified["ProfitLoss"]["line"]}')
    before=assess_financial_facts(original,as_of="2020-03-20")
    after=assess_financial_facts(canonical,as_of="2020-03-20")
    assert before=={"status":"invalid","reason":"scope_mismatch"}
    assert after=={"status":"usable","reason":None}
    repaired=dict(symbol="CVNA",adsh=known["adsh"],filing_url=known["filing_url"],original=original,
        canonical=canonical,before=before,after=after,
        mapping_scope="Only this reviewed 2019 filing; never a global NetIncomeLoss/ProfitLoss alias",
        effect="Separate canonical data example. Prior formulas, selections and returns are unchanged.")
    (OUT/"canonical_cvna_example.json").write_text(json.dumps(repaired,indent=2,allow_nan=False)+"\n")
    proof={"historical_instrument_identity":"unknown", "delisting_outcomes":"unknown",
        "price_adjustments_and_dividends":"unknown", "point_in_time_availability":"partial",
        "financial_period_unit_scope":"partial", "historical_borrow_availability_and_costs":"unknown"}
    gate=historical_research_gate(proof,financials=True,shorts=True)
    summary=dict(financial_records=len(rows),numeric_complete=sum(r["numeric_complete"] for r in rows),
        semantic_before_counts=dict(Counter(r["quality_status"] for r in rows)),
        semantic_reason_counts=dict(Counter(r["quality_reason"] for r in rows)),
        explicitly_repaired_filings=1,existing_numeric_source_verification="8295 referenced numbers; prior verification.json",
        input_readiness=proof,research_gate=gate,
        action="Freeze prior candidate formulas; no new profit tuning until applicable data requirements are verified.",
        source_sha256={"financials.json":expected,"semantic_audit.json":digest(PREV/"semantic_audit.json"),
                       "2020q1.zip":digest(archive),"audit.py":digest(OUT/"audit.py")})
    (OUT/"coverage.json").write_text(json.dumps(summary,indent=2,allow_nan=False)+"\n")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if "--validate-for-research" in sys.argv:
        raise SystemExit(2 if gate["status"]=="STOPPED" else 0)


if __name__=="__main__":
    main()
