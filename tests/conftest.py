import pytest


@pytest.fixture(autouse=True)
def isolate_long_run_state(monkeypatch, tmp_path):
    """Never let tests write the operator's real durable state."""
    monkeypatch.setenv(
        "TRADINGBUFFETT_LONG_RUN_DIR", str(tmp_path / "long_run_state")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_EXECUTION_LOCK_DIR", str(tmp_path / "execution_locks")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_RESULTS_DIR", str(tmp_path / "results")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_CACHE_DIR", str(tmp_path / "cache")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_MEMORY_LOG_PATH", str(tmp_path / "memory.md")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_AGENT_MEMORY_DIR", str(tmp_path / "agent_memory")
    )
    monkeypatch.setenv(
        "TRADINGBUFFETT_EXECUTION_DB", str(tmp_path / "execution.sqlite3")
    )
    import tradingagents.safety.guardrails as guardrails

    monkeypatch.setattr(guardrails, "_SAFETY_HOME", tmp_path / "safety")
    guardrails.reset_safety_guard()


@pytest.fixture(autouse=True)
def isolate_operator_llm_endpoint(monkeypatch):
    """Keep the operator's local .env routing from changing unit-test defaults."""
    monkeypatch.delenv("TRADINGBUFFETT_OPENAI_USE_LOCAL", raising=False)
    monkeypatch.delenv("TRADINGBUFFETT_OPENAI_BASE_URL", raising=False)
