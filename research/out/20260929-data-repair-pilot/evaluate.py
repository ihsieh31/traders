"""Reuse the frozen four-method test with repaired, dated financials."""
import importlib.util
from pathlib import Path
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
spec = importlib.util.spec_from_file_location("fixed_mscore_evaluation", ROOT / "research/out/20260929-mscore-validation/evaluate.py")
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)
E.OUT = OUT


if __name__ == "__main__":
    with E.np.errstate(invalid="ignore", divide="ignore"):
        if "--checks-only" in sys.argv:
            print(E.checks())
        else:
            E.main()
