"""Normalize analyst tool requests without losing provider message metadata."""
import json

from langchain_core.messages import AIMessage


def result_tool_calls(result):
    calls = getattr(result, "tool_calls", None)
    if not calls:
        calls = (getattr(result, "additional_kwargs", {}) or {}).get("tool_calls") or []
    normalized = []
    seen = set()
    for call in calls:
        if not isinstance(call, dict):
            call = {key: getattr(call, key, None) for key in ("name", "args", "id")}
        function = call.get("function") or call
        name = function.get("name")
        arguments = function.get("args", function.get("arguments", {}))
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        call_id = call.get("id") or call.get("tool_call_id")
        if not name or not call_id or call_id in seen or not isinstance(arguments, dict):
            raise ValueError("Tool requests require a name, unique call ID and object arguments")
        seen.add(call_id)
        normalized.append({"name": name, "args": arguments, "id": call_id, "type": "tool_call"})
    return normalized


def assistant_tool_message(result, calls):
    """One assistant turn owns all calls; keep native content/signatures intact."""
    if isinstance(result, AIMessage):
        return result.model_copy(update={"tool_calls": calls})
    return AIMessage(content=getattr(result, "content", "") or "", tool_calls=calls,
                     additional_kwargs=getattr(result, "additional_kwargs", {}) or {})
