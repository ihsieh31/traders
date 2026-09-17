"""L03/L04 offline contract regressions for the custom LLM adapter."""

import pytest
import tradingagents.agents  # noqa: F401


def test_l03_chat_branch_receives_request_timeout():
    """L03: get_chat_model pops `timeout` and only forwarded it to the
    Responses branch; the Chat Completions branch built its model without
    any timeout, so llm_request_timeout_seconds was silently dropped for
    non-Responses models."""
    from tradingagents.agents.utils.gpt5_llm import get_chat_model

    model = get_chat_model("gpt-4.1", api_key="k", timeout=42.0)
    assert getattr(model, "request_timeout", None) == 42.0


def test_l03_responses_branch_keeps_timeout():
    from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel, get_chat_model

    model = get_chat_model("gpt-5-mini", api_key="k", timeout=7.5)
    assert isinstance(model, GPT5ChatModel)
    assert model.timeout == 7.5
