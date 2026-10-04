"""Bounded download of one public SEC historical notes archive."""
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
URL = "https://www.sec.gov/files/dera/data/financial-statement-notes-data-sets/2018q1_notes.zip?download=1"
PATH = ROOT / "research/data/sec-mscore/2018q1_notes.zip"


def main():
    if not PATH.exists():
        temp = PATH.with_suffix(".part")
        offset, total = 0, None
        with temp.open("wb") as fh:
            while total is None or offset < total:
                end = offset + 16*1024*1024 - 1
                request = urllib.request.Request(URL, headers={
                    "User-Agent": "TradingBuffett offline-research verification/1.0",
                    "Accept": "application/zip", "Range": f"bytes={offset}-{end}"})
                with urllib.request.urlopen(request, timeout=40) as response:
                    assert response.geturl().startswith("https://www.sec.gov/files/")
                    if response.status == 200 and offset == 0:
                        while chunk := response.read(1024*1024):
                            fh.write(chunk)
                            assert fh.tell() <= 600*1024*1024
                        total = offset = fh.tell()
                    else:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers["Content-Range"])
                        assert response.status == 206 and match is not None
                        start, stop, size = map(int, match.groups())
                        assert start == offset and stop <= end and size <= 600*1024*1024
                        chunk = response.read(16*1024*1024+1)
                        assert len(chunk) == stop-start+1
                        assert total is None or total == size
                        fh.write(chunk)
                        offset, total = stop+1, size
                print("cached bytes", offset, "/", total, flush=True)
                time.sleep(.15)
        assert temp.stat().st_size == total
        with zipfile.ZipFile(temp) as z:
            assert {"sub.tsv", "num.tsv"} <= set(z.namelist())
        temp.replace(PATH)
    with PATH.open("rb") as fh:
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
    (OUT / "notes_source.json").write_text(json.dumps(dict(url=URL, sha256=digest,
        bytes=PATH.stat().st_size, quarter="2018q1"), indent=2)+"\n")
    with zipfile.ZipFile(PATH) as z:
        for name in ("num.tsv", "sub.tsv"):
            with z.open(name) as fh:
                print(name, repr(fh.readline()), flush=True)
    print("source cached", digest, flush=True)


if __name__ == "__main__":
    main()
