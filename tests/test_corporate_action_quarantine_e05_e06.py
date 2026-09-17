"""E05/E06 corporate-action quarantine regressions (second-opinion audit).

E05: re-quarantining the same reason must persist the EARLIEST effective
time (None = immediate), not let a later future-dated report push the
activation out.

E06: all_active() must reflect records another store instance wrote after
this instance was created.
"""

from datetime import datetime, timedelta, timezone

from tradingagents.risk.corporate_actions import QuarantineStore


def test_e05_earlier_effective_time_wins(tmp_path):
    store = QuarantineStore(tmp_path / "quarantine.json")
    future = datetime.now(timezone.utc) + timedelta(days=3)
    first = store.quarantine(symbol="AAPL", reason="split", effective_at=future)
    assert first["effective_at"] is not None

    immediate = store.quarantine(symbol="AAPL", reason="split")
    assert immediate["effective_at"] is None, "None means active now and must win"
    assert QuarantineStore(tmp_path / "quarantine.json").is_quarantined("AAPL")

    again_future = store.quarantine(symbol="AAPL", reason="split", effective_at=future)
    assert again_future["effective_at"] is None, "later report must not push activation out"


def test_e06_all_active_reloads_before_enumerating_symbols(tmp_path):
    reader = QuarantineStore(tmp_path / "quarantine.json")
    writer = QuarantineStore(tmp_path / "quarantine.json")
    writer.quarantine(symbol="AAPL", reason="split")
    assert [row["symbol"] for row in reader.all_active()] == ["AAPL"]
