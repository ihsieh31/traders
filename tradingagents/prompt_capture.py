"""Core prompt audit capture, independent of any presentation layer."""

from __future__ import annotations

from tradingagents.run_logger import get_run_audit_logger


def capture_agent_prompt(
    report_type: str, prompt_content: str, symbol: str | None = None
) -> None:
    """Persist the exact agent prompt in the active run audit log."""
    try:
        get_run_audit_logger().log_prompt(
            report_type=report_type,
            prompt_text=prompt_content,
            symbol=symbol,
            metadata={"source": "core_prompt_capture"},
        )
    except Exception as exc:
        print(f"[PROMPT_CAPTURE] Prompt audit skipped for {report_type}: {exc}")


__all__ = ["capture_agent_prompt"]
