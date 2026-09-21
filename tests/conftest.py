import inspect

import pytest


_REMOVED_WEBUI_TEST_MODULES = {
    "test_audit_api_key_semantics.py",
}


def pytest_collection_modifyitems(items):
    """Skip obsolete assertions whose subject was the removed WebUI layer."""
    marker = pytest.mark.skip(reason="WebUI was intentionally removed; test is obsolete")
    needles = ("webui", "run_webui_dash", "from dash", "dash.Dash", "Dash(")
    for item in items:
        path = getattr(item, "path", None)
        if path is not None and path.name in _REMOVED_WEBUI_TEST_MODULES:
            item.add_marker(marker)
            continue
        objects = [getattr(item, "obj", None), getattr(item, "cls", None)]
        source = ""
        for obj in objects:
            if obj is None:
                continue
            try:
                source += inspect.getsource(obj).lower()
            except (OSError, TypeError):
                continue
        if any(needle.lower() in source for needle in needles):
            item.add_marker(marker)


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
