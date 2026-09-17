"""L03/L04 offline contract regressions for the custom LLM adapter."""

import pytest
import tradingagents.agents  # noqa: F401
from langchain_core.messages import AIMessage
from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel


class _ChatResultFixture:
    def __init__(self, message):
        self.generations = [type("G", (), {"message": message})()]


def test_l03_chat_branch_receives_request_timeout():
    """L03: get_chat_model pops `timeout` and only forwarded it to the
    Responses branch; the Chat Completions branch built its model without
    any timeout, so llm_request_timeout_seconds was silently dropped for
    non-Responses models."""
    from tradingagents.agents.utils.gpt5_llm import get_chat_model

    model = get_chat_model("gpt-4.1", api_key="k", timeout=42.0)
    assert getattr(model, "request_timeout", None) == 42.0


def test_l03_responses_branch_keeps_timeout():
    from tradingagents.agents.utils.gpt5_llm import get_chat_model

    model = get_chat_model("gpt-5-mini", api_key="k", timeout=7.5)
    assert isinstance(model, GPT5ChatModel)
    assert model.timeout == 7.5


def test_l04_chat_prompt_value_keeps_roles_and_tool_messages():
    """L04: invoke(ChatPromptValue) previously str()-collapsed a
    system+human prompt into one HumanMessage and turned ToolMessages into
    "[]". Message roles must survive the invoke boundary so bound-tool
    agents keep their tool call/output protocol."""
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.messages import ToolMessage

    captured = []

    class _RecordingGPT5(GPT5ChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured.append(list(messages))
            return _ChatResultFixture(AIMessage(content="ok"))

    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are {name}."),
        ("placeholder", "{history}"),
        ("human", "{question}"),
    ])
    tool_msg = ToolMessage(content="tool says hi", tool_call_id="call-1")
    value = prompt.invoke({"name": "T", "history": [tool_msg], "question": "why?"})
    model = _RecordingGPT5(model="gpt-5-mini", api_key="k")
    model.invoke(value)
    roles = [type(m).__name__ for m in captured[0]]
    assert roles == ["SystemMessage", "ToolMessage", "HumanMessage"]
    assert captured[0][0].content == "You are T."
