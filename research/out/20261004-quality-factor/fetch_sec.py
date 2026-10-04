"""Cache the official SEC Financial Statement Data Sets, 2021Q1..2026Q2.

SEC requires a declared User-Agent with contact details. It is read from the
SEC_USER_AGENT environment variable so no contact address lands in the repo:

    SEC_USER_AGENT="Name email" .venv-p2/bin/python research/out/20261004-quality-factor/fetch_sec.py

Resumable: an existing, valid zip is skipped. Every zip is checked for
sub.txt/num.txt before it replaces the .part file.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "research/data/sec-mscore"
OUT = Path(__file__).resolve().parent
BASE = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/"
QUARTERS = [f"{y}q{q}" for y in range(2021, 2027) for q in range(1, 5)][:22]  # .. 2026q2
MAX_BYTES = 250 * 1024 * 1024


def main() -> int:
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in agent:
        print("set SEC_USER_AGENT to 'Name email' (SEC fair-access policy)", file=sys.stderr)
        return 2
    headers = {"User-Agent": agent, "Accept-Encoding": "identity"}
    CACHE.mkdir(parents=True, exist_ok=True)
    rows = []
    for token in QUARTERS:
        path = CACHE / f"{token}.zip"
        row = {"quarter": token, "url": BASE + token + ".zip"}
        try:
            if not path.exists():
                time.sleep(0.5)  # far below SEC's 10 requests/second
                temp = path.with_suffix(".part")
                request = urllib.request.Request(row["url"], headers=headers)
                with urllib.request.urlopen(request, timeout=120) as response, temp.open("wb") as fh:
                    if response.geturl().split("/")[2] != "www.sec.gov":
                        raise ValueError("redirected away from www.sec.gov")
                    while chunk := response.read(1 << 20):
                        fh.write(chunk)
                        if fh.tell() > MAX_BYTES:
                            raise ValueError("zip exceeds the bounded size")
                with zipfile.ZipFile(temp) as z:
                    if not {"sub.txt", "num.txt"} <= set(z.namelist()):
                        raise ValueError("zip lacks sub.txt/num.txt")
                temp.replace(path)
            with zipfile.ZipFile(path) as z:
                names = sorted(z.namelist())
            row.update(status="ok", bytes=path.stat().st_size, files=names,
                       sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        except Exception as exc:  # recorded, never silently skipped
            row.update(status="error", error=f"{type(exc).__name__}: {exc}")
        rows.append(row)
        print(token, row["status"], row.get("bytes", row.get("error")), flush=True)
    (OUT / "sec_manifest.json").write_text(json.dumps(rows, indent=2) + "\n")
    return 0 if all(r["status"] == "ok" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
