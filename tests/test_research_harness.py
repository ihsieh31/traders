"""Run the research harness self-tests with the suite (synthetic, offline).

research/plan.md 3.0 requires both to pass before any formula round; until
now nothing ran them automatically, so a harness regression could ship.

Each runs in its own interpreter: importing ``research.src.common`` rewrites
process-wide durable-state paths (long-run, safety, selection cache) to the
research directory, which must never leak into the rest of the suite.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["research.src.smoke_test", "research.src.test_harness"])
def test_research_self_test_passes(module, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRADINGBUFFETT_")}
    env["TRADINGBUFFETT_RESEARCH_DATA_DIR"] = str(tmp_path / "research_data")
    result = subprocess.run(
        [sys.executable, "-m", module], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
