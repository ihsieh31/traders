import pytest


@pytest.fixture(autouse=True)
def isolate_long_run_state(monkeypatch, tmp_path):
    """Never let tests write the operator's real durable state."""
    monkeypatch.setenv(
        "TRADINGAGENTS_LONG_RUN_DIR", str(tmp_path / "long_run_state")
    )
    monkeypatch.setenv(
        "TRADINGAGENTS_EXECUTION_LOCK_DIR", str(tmp_path / "execution_locks")
    )
    import tradingagents.safety.guardrails as guardrails

    monkeypatch.setattr(guardrails, "_SAFETY_HOME", tmp_path / "safety")
    guardrails.reset_safety_guard()
