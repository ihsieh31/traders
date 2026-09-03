from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Optional[Any]:
    try:
        return llm.with_structured_output(schema)
    except Exception as exc:
        # Any bind failure (not only missing-method errors) must surface as
        # None so the strict Risk path emits INVALID/NO_TRADE instead of
        # raising outside the fail-closed boundary.
        logger.warning("%s structured output unavailable; using free text (%s)", agent_name, exc)
        return None


def invoke_structured_or_freetext(
    structured_llm: Optional[Any],
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    if structured_llm is not None:
        try:
            return render(structured_llm.invoke(prompt))
        except Exception as exc:
            logger.warning("%s structured output failed; retrying as free text (%s)", agent_name, exc)

    response = plain_llm.invoke(prompt)
    return response.content if hasattr(response, "content") else str(response)


def invoke_structured_object_or_freetext(
    structured_llm: Optional[Any],
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> tuple[str, Optional[T]]:
    """Return rendered text plus the structured object when the provider supports it."""
    if structured_llm is not None:
        try:
            structured_value = structured_llm.invoke(prompt)
            return render(structured_value), structured_value
        except Exception as exc:
            logger.warning("%s structured output failed; retrying as free text (%s)", agent_name, exc)

    response = plain_llm.invoke(prompt)
    content = response.content if hasattr(response, "content") else str(response)
    return content, None


def invoke_risk_structured_strict(
    structured_llm: Optional[Any],
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
    *,
    schema: Optional[type[T]] = None,
) -> tuple[Optional[str], Optional[T], Optional[str]]:
    """Strict Risk Manager boundary: structured success or explicit INVALID.

    Never falls back to free text. Any bind/invoke/validation/timeout/
    provider/empty/illegal failure returns (None, None, reason) so the
    caller must emit INVALID/NO_TRADE with zero broker calls. Analyst,
    Research Manager and Trader keep the free-text fallback; only risk uses
    this strict path.
    """
    if structured_llm is None:
        logger.warning("%s structured bind unavailable; emitting NO_TRADE", agent_name)
        return None, None, "structured_bind_failed"
    try:
        structured_value = structured_llm.invoke(prompt)
    except Exception as exc:
        logger.warning("%s structured invoke failed; emitting NO_TRADE (%s)", agent_name, exc)
        return None, None, f"structured_invoke_failed: {exc}"
    try:
        if schema is not None and not isinstance(structured_value, schema):
            # Provider returned a plain dict/string despite the bind: validate
            # strictly instead of guessing from text.
            structured_value = schema.model_validate(structured_value)
        rendered = render(structured_value)
    except Exception as exc:
        logger.warning("%s structured validation failed; emitting NO_TRADE (%s)", agent_name, exc)
        return None, None, f"structured_validation_failed: {exc}"
    if rendered is None or not str(rendered).strip():
        return None, None, "structured_empty_output"
    return str(rendered), structured_value, None
