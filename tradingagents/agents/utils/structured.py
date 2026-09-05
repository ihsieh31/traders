from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel

from tradingagents.llm_clients.retry import ProviderFailure

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Optional[Any]:
    try:
        return llm.with_structured_output(schema)
    except Exception as exc:
        # Any bind failure (not only missing-method errors) must surface as
        # None so the strict Risk path emits INVALID/NO_TRADE instead of
        # raising outside the fail-closed boundary. Binding makes no HTTP
        # request, so this cannot mask a provider access failure.
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
        except ProviderFailure:
            # Phase B: a provider access failure must stop the run. Falling
            # back here would issue another request and bypass the retry cap.
            raise
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
        except ProviderFailure:
            # Phase B: provider access failures propagate (stop the run);
            # only schema/bind problems fall back to free text.
            raise
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
    """Strict Risk Manager boundary with Phase B failure separation.

    Two distinct failure classes:

    - **Provider access failure** (transport/timeout/429/5xx exhausted or a
      permanent 401/403/invalid request): raises :class:`ProviderFailure`
      so the whole run stops. It must never be reported as a normal
      NO_TRADE and must never reuse a checkpoint's stale decision.
    - **Successful response but schema/bind/validation/empty output**: the
      Phase A behavior stands — return (None, None, reason) so the caller
      emits INVALID/NO_TRADE with zero broker calls. No repair request, no
      free-text guessing.
    """
    if structured_llm is None:
        logger.warning("%s structured bind unavailable; emitting NO_TRADE", agent_name)
        return None, None, "structured_bind_failed"
    try:
        structured_value = structured_llm.invoke(prompt)
    except ProviderFailure:
        raise
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
