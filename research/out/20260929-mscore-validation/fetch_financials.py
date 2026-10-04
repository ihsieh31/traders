"""Cache official 2015-2020 SEC financial datasets; prices remain untouched."""
from pathlib import Path
import hashlib
import json
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "research/data/sec-mscore"
OUT = Path(__file__).resolve().parent
HEADERS = {"User-Agent": "TradingBuffett offline-research verification/1.0",
           "Accept": "application/json,application/zip"}


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    rows = []
    for year in range(2015, 2021):
        for quarter in range(1, 5):
            token = f"{year}q{quarter}"
            url = f"https://www.sec.gov/files/dera/data/financial-statement-data-sets/{token}.zip"
            path = CACHE / f"{token}.zip"
            row = dict(quarter=token, url=url)
            try:
                if not path.exists():
                    time.sleep(.25)
                    request = urllib.request.Request(url, headers=HEADERS)
                    temp = path.with_suffix(".part")
                    with urllib.request.urlopen(request, timeout=40) as response, temp.open("wb") as fh:
                        if response.geturl().split('/')[2] != 'www.sec.gov':
                            raise ValueError("nonofficial redirect")
                        while chunk := response.read(1024*1024):
                            fh.write(chunk)
                            if fh.tell() > 180*1024*1024:
                                raise ValueError("quarter zip exceeds bounded size")
                    with zipfile.ZipFile(temp) as z:
                        assert {"sub.txt", "num.txt"} <= set(z.namelist())
                    temp.replace(path)
                with zipfile.ZipFile(path) as z:
                    assert {"sub.txt", "num.txt"} <= set(z.namelist())
                row.update(status="cached", bytes=path.stat().st_size,
                           sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                print(token, "ready", row["bytes"], flush=True)
            except Exception as exc:
                row.update(status="unavailable", error=f"{type(exc).__name__}: {exc}")
                print(token, row["error"], flush=True)
            rows.append(row)
            (OUT / "source_manifest.json").write_text(json.dumps(rows, indent=2)+"\n")
    assert all(r["status"] == "cached" for r in rows), "Missing quarters; no silent substitution"


if __name__ == "__main__":
    main()
