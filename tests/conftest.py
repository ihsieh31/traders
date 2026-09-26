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


@pytest.fixture(autouse=True)
def restore_global_trading_config():
    """Snapshots and restores the process-wide trading config around each test.

    The long-run/A-B coordinator installs its runtime as the global config
    (set_config) before any broker-mutating path — by design, since the
    execution gates read it. A/B rounds that now proceed deep enough to do
    that installation must not leak the runtime into later tests: a leaked
    ``auto_screening_enabled`` would entry-gate unrelated tests to a
    selection cache they never wrote.
    """
    from tradingagents.dataflows import config as _config_module

    snapshot = _config_module.get_config()
    yield
    # A merge-only restore would keep keys a test ADDED (e.g. the A/B
    # runtime's ``_alpaca_account_profile``, which reroutes the client
    # factory to per-account credentials). Rebuild from the defaults first,
    # then merge the pre-test snapshot back over it.
    _config_module._config = None
    _config_module.set_config(snapshot)
