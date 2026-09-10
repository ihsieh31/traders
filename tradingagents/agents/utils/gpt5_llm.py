"""Custom LangChain wrapper for OpenAI reasoning models using Responses API."""

from typing import Any, Dict, List, Optional
from pydantic import ConfigDict
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from openai import OpenAI
import json
import os
import time

from pydantic import BaseModel

from tradingagents.openai_model_registry import (
    apply_responses_model_params,
    describe_model_params as describe_registry_model_params,
    get_default_model_params,
    get_model_spec,
    is_responses_model,
    normalize_model_params,
)


class ToolBindingError(ValueError):
    """A bound tool/schema could not be converted to a Responses API function tool.

    Raised at bind/invoke time instead of silently omitting the tool: a
    structured Screening/Risk schema that disappears would turn every
    decision into unusable free text.
    """


def _to_responses_function_tool(tool: Any, *, strict: Optional[bool] = None) -> Dict[str, Any]:
    """Convert one bound tool into a Responses API function-tool dict.

    Supported inputs:
      - Pydantic ``BaseModel`` subclasses (ScreeningOutput, RiskDecision, ...):
        name = class name, parameters = model_json_schema(), description =
        class docstring or schema description.
      - LangChain-style tools with ``.name`` / ``.description`` / ``.args_schema``.
      - Already-normalized ``{"type": "function", "name": ...}`` dict schemas.
    """
    if isinstance(tool, type) and issubclass(tool, BaseModel):
        name = getattr(tool, "__name__", None)
        if not name:
            raise ToolBindingError(f"Pydantic schema {tool!r} has no __name__")
        try:
            parameters = tool.model_json_schema()
        except Exception as exc:
            raise ToolBindingError(
                f"cannot build a Responses function schema from {name}: {exc}"
            ) from exc
        description = (tool.__doc__ or "").strip()
        if not description:
            description = str(parameters.get("description") or f"Tool: {name}")
        schema: Dict[str, Any] = {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": parameters,
        }
        if strict is not None:
            schema["strict"] = bool(strict)
        return schema

    if isinstance(tool, dict):
        if str(tool.get("type") or "").lower() == "function" and tool.get("name"):
            normalized = dict(tool)
            if strict is not None and "strict" not in normalized:
                normalized["strict"] = bool(strict)
            return normalized
        raise ToolBindingError(f"unsupported bound tool dict (needs type=function and name): {tool!r}")

    tool_name = getattr(tool, "name", None)
    if not tool_name:
        func = getattr(tool, "func", None)
        tool_name = getattr(func, "__name__", None)
    if not tool_name:
        raise ToolBindingError(
            f"cannot convert bound tool to a Responses function schema: {tool!r}"
        )
    description = getattr(tool, "description", None)
    if not description:
        description = (getattr(getattr(tool, "func", None), "__doc__", None) or f"Tool: {tool_name}")
    parameters: Any = {"type": "object", "properties": {}}
    args_schema = getattr(tool, "args_schema", None)
    if args_schema is not None:
        try:
            parameters = args_schema.schema()
        except Exception as exc:
            raise ToolBindingError(
                f"cannot build parameters for bound tool {tool_name}: {exc}"
            ) from exc
    schema = {
        "type": "function",
        "name": tool_name,
        "description": description,
        "parameters": parameters,
    }
    if strict is not None:
        schema["strict"] = bool(strict)
    return schema


def _normalize_responses_tool_choice(tool_choice: Any) -> Any:
    """Map Chat Completions tool_choice values to Responses API shapes."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        lowered = tool_choice.strip().lower()
        if lowered == "any":
            return "required"
        if lowered in ("auto", "none", "required"):
            return lowered
        # A bare string names a specific function.
        return {"type": "function", "name": tool_choice}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            inner = tool_choice.get("function")
            name = (
                tool_choice.get("name")
                or (inner.get("name") if isinstance(inner, dict) else None)
            )
            if name:
                return {"type": "function", "name": str(name)}
            return tool_choice
        if tool_choice.get("type") in ("auto", "none", "required", "allowed_tools"):
            return tool_choice
    raise ToolBindingError(f"unsupported tool_choice for the Responses API: {tool_choice!r}")


def _endpoint_compatible_tool_choice(model_name: str, tool_choice: Any) -> Any:
    """Clamp a normalized tool_choice to what the model's endpoint accepts.

    Some OpenAI-compatible upstreams (OpenCode Zen's muse models) accept only
    ``"auto"``: a forced choice ("required" or a named function, which is what
    structured-output bindings emit) fails the whole request with a 400. For
    such models the forced value degrades to "auto" — the model still sees
    the bound tool, it just decides whether to call it. "none" stays "none":
    an explicit no-tools request is equally valid on auto-only endpoints.
    """
    modes = get_model_spec(model_name).get("tool_choice_modes") or (
        "auto", "none", "required", "named",
    )
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice in modes else "auto"
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return tool_choice if "named" in modes else "auto"
    return tool_choice


# Some third-party OpenAI-compatible routers/WAFs block the OpenAI SDK's
# default User-Agent ("OpenAI/Python ...") with 403 while the same key and
# body pass with a neutral one. Caller-configured endpoints (explicit
# base_url) therefore get a neutral UA: the official OpenAI endpoint keeps
# the SDK default, and an explicit caller User-Agent always wins.
_NEUTRAL_USER_AGENT = "traders-paper/1.0"


def _env_extra_headers() -> Dict[str, str]:
    """Parse OPENAI_EXTRA_HEADERS ("Name: Value; Name2: Value2") into a dict.

    Some OpenAI-compatible endpoints require per-request headers beyond the
    standard auth header (e.g. OpenCode Zen's free tier requires
    ``X-Session-ID``). An entry with an empty value is skipped; malformed
    entries without a colon are ignored rather than failing every call.
    """
    raw = os.getenv("OPENAI_EXTRA_HEADERS", "")
    headers: Dict[str, str] = {}
    for part in raw.replace("\n", ";").split(";"):
        entry = part.strip()
        if not entry or ":" not in entry:
            continue
        name, value = entry.split(":", 1)
        name, value = name.strip(), value.strip()
        if name and value:
            headers[name] = value
    return headers


def _endpoint_headers(
    base_url: Optional[str],
    custom_headers: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """Headers for one client construction; neutral UA only on custom endpoints."""
    headers = dict(custom_headers) if custom_headers else {}
    headers.update(_env_extra_headers())
    if any(k.lower() == "user-agent" for k in headers):
        # A caller- or env-configured User-Agent; never clobber it.
        return headers or None
    if not base_url:
        # Official provider default endpoint: SDK default headers untouched.
        return headers or None
    headers["User-Agent"] = _NEUTRAL_USER_AGENT
    return headers


def get_model_params_for_depth(
    model_name: str,
    research_depth: str,
    model_role: str = "quick"
) -> Dict[str, Any]:
    """Backward-compatible shim.

    Research depth now controls debate rounds only. Model parameters come from
    per-model defaults or explicit UI overrides.
    """
    return get_default_model_params(model_name, model_role)


def describe_model_params(
    model_name: str,
    research_depth: str,
    model_role: str = "quick"
) -> str:
    """Backward-compatible description helper."""
    return describe_registry_model_params(model_name, None, model_role)


class GPT5ChatModel(BaseChatModel):
    """ChatModel wrapper for OpenAI reasoning models using responses.create()."""
    
    model: str = "gpt-5-mini"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    default_headers: Optional[Dict[str, str]] = None
    reasoning_effort: str = "medium"
    verbosity: str = "medium"  # low, medium, high
    summary: str = "auto"  # concise, detailed, auto, none
    max_output_tokens: Optional[int] = None
    store: bool = False
    parallel_tool_calls: bool = True
    timeout: Optional[float] = None  # bounded per-request timeout
    
    # Internal client - not a pydantic field
    _client: Optional[OpenAI] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Initialize the OpenAI client. SDK-level retries are pinned to 0:
        # the single bounded retry owner is tradingagents.llm_clients.retry.
        client_kwargs = {}
        if self.api_key:
            client_kwargs["api_key"] = self.api_key
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        if self.timeout:
            client_kwargs["timeout"] = self.timeout
        headers = _endpoint_headers(self.base_url, self.default_headers)
        if headers:
            client_kwargs["default_headers"] = headers
        client_kwargs["max_retries"] = 0
        self._client = OpenAI(**client_kwargs) if client_kwargs else OpenAI(max_retries=0)
    
    @property
    def _llm_type(self) -> str:
        return "openai-responses-chat"
    
    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "verbosity": self.verbosity,
            "summary": self.summary,
            "max_output_tokens": self.max_output_tokens,
            "store": self.store,
            "parallel_tool_calls": self.parallel_tool_calls,
        }
    
    def _convert_messages_to_input(self, messages: List[BaseMessage]) -> List[Dict]:
        """Convert LangChain messages, dicts, or strings to GPT-5 input format."""
        input_messages = []
        
        for message in messages:
            # Support dict-style messages used in some agents
            if isinstance(message, dict):
                role = message.get("role", "user")
                content = message.get("content", "")
                # If content is already structured, pass through
                if isinstance(content, list):
                    input_messages.append({
                        "role": "developer" if role == "system" else role,
                        "content": content
                    })
                else:
                    content_type = "output_text" if role == "assistant" else "input_text"
                    input_messages.append({
                        "role": "developer" if role == "system" else role,
                        "content": [
                            {
                                "type": content_type,
                                "text": content
                            }
                        ]
                    })
                continue
            
            # Support raw string messages
            if isinstance(message, str):
                input_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": message
                        }
                    ]
                })
                continue

            # Support generic message objects with role/content attributes
            if hasattr(message, "role") and hasattr(message, "content"):
                role = getattr(message, "role") or "user"
                content = getattr(message, "content", "")
                content_type = "output_text" if role == "assistant" else "input_text"
                input_messages.append({
                    "role": "developer" if role == "system" else role,
                    "content": [
                        {
                            "type": content_type,
                            "text": content
                        }
                    ]
                })
                continue

            if isinstance(message, SystemMessage):
                # GPT-5 uses "developer" role instead of "system"
                input_messages.append({
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": message.content
                        }
                    ]
                })
            elif isinstance(message, HumanMessage):
                input_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": message.content
                        }
                    ]
                })
            elif isinstance(message, AIMessage):
                input_messages.append({
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": message.content
                        }
                    ]
                })
        
        return input_messages
    
    def _extract_content_from_response(self, response) -> tuple:
        """
        Extract text content and tool calls from GPT-5 response.
        Returns (content, tool_calls) tuple.
        
        Priority for content:
        1. response.output_text (convenience property with all text)
        2. Iterate through response.output items
        
        Tool calls are always extracted from response.output items.
        """
        tool_calls = []
        content_parts = []  # Use list to avoid duplication
        
        # Always iterate through output for tool calls
        if hasattr(response, 'output') and response.output:
            for item in response.output:
                item_type = getattr(item, 'type', None)
                
                # Extract function calls
                if item_type == 'function_call':
                    call_id = getattr(item, 'call_id', None) or getattr(item, 'id', f'call_{len(tool_calls)+1}')
                    func_name = getattr(item, 'name', None)
                    func_args = getattr(item, 'arguments', {})
                    
                    if func_name:
                        tool_calls.append({
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": func_name,
                                "arguments": json.dumps(func_args) if isinstance(func_args, dict) else str(func_args)
                            }
                        })
        
        # Get content - prefer output_text if available (it's the combined text)
        if hasattr(response, 'output_text') and response.output_text:
            return response.output_text.strip(), tool_calls
        
        # Fallback: extract text from output items
        if hasattr(response, 'output') and response.output:
            for item in response.output:
                item_type = getattr(item, 'type', None)
                
                # Skip function calls and reasoning
                if item_type in ('function_call', 'reasoning'):
                    continue
                
                # Handle message content
                if item_type == 'message':
                    if hasattr(item, 'content') and item.content:
                        for content_item in item.content:
                            text = None
                            # Get text from content item
                            if hasattr(content_item, 'text') and content_item.text:
                                text = content_item.text
                            elif isinstance(content_item, dict):
                                text = content_item.get('text', '')
                            elif isinstance(content_item, str):
                                text = content_item
                            
                            if text and text not in content_parts:
                                content_parts.append(text)
                
                # Handle direct text output
                elif item_type in ('text', 'output_text'):
                    text = getattr(item, 'text', '')
                    if text and text not in content_parts:
                        content_parts.append(text)
        
        content = ''.join(content_parts)
        return content.strip(), tool_calls
    
    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate a response using GPT-5 responses.create() API."""
        
        # Convert messages to GPT-5 format
        input_messages = self._convert_messages_to_input(messages)
        if not input_messages and messages:
            # Fallback to prevent empty input errors
            input_messages = [{
                "role": "user",
                "content": [{"type": "input_text", "text": str(messages)}]
            }]
        
        # Build base API parameters
        api_params = {
            "model": self.model,
            "input": input_messages,
            "text": {
                "format": {"type": "text"}
            },
        }

        runtime_params = normalize_model_params(
            self.model,
            {
                "reasoning_effort": self.reasoning_effort,
                "text_verbosity": self.verbosity,
                "reasoning_summary": self.summary,
                "max_output_tokens": self.max_output_tokens,
                "store": self.store,
                "parallel_tool_calls": self.parallel_tool_calls,
            },
            role="deep",
        )
        apply_responses_model_params(api_params, self.model, runtime_params, role="deep")
        reasoning_effort = runtime_params.get("reasoning_effort")
        runtime_verbosity = runtime_params.get("text_verbosity") or runtime_params.get("verbosity")

        print(
            f"[OPENAI RESPONSES] Model: {self.model}, "
            f"{describe_registry_model_params(self.model, runtime_params, role='deep')}"
        )
        input_chars = len(str(input_messages))

        def _extract_usage_dict(resp) -> Dict[str, int]:
            usage_dict = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            usage = getattr(resp, "usage", None)
            if usage is None:
                return usage_dict

            if isinstance(usage, dict):
                usage_dict["input_tokens"] = int(usage.get("input_tokens", 0) or 0)
                usage_dict["output_tokens"] = int(usage.get("output_tokens", 0) or 0)
                usage_dict["total_tokens"] = int(
                    usage.get("total_tokens", usage_dict["input_tokens"] + usage_dict["output_tokens"]) or 0
                )
                return usage_dict

            for key in ("input_tokens", "output_tokens", "total_tokens"):
                value = getattr(usage, key, None)
                if value is not None:
                    usage_dict[key] = int(value or 0)

            if usage_dict["total_tokens"] == 0:
                usage_dict["total_tokens"] = usage_dict["input_tokens"] + usage_dict["output_tokens"]
            return usage_dict

        def _record_llm_call(
            *,
            status: str,
            latency_seconds: float,
            output_chars: int,
            usage: Dict[str, int] | None = None,
            error_message: str | None = None,
        ) -> None:
            payload = {
                "model": self.model,
                "purpose": "gpt5_responses",
                "status": status,
                "latency_seconds": round(float(latency_seconds), 4),
                "input_chars": input_chars,
                "output_chars": int(output_chars or 0),
                "effort": reasoning_effort,
                "verbosity": runtime_verbosity,
                "usage": usage or {},
                "error_message": error_message,
            }

            # F15: UI counters update without re-emitting an audit event; the
            # single token-bearing llm_call audit event is emitted here so
            # Responses-model usage reaches the daily budget exactly once
            # (the common usage callback skips adapter-marked results).
            try:
                from webui.utils.state import app_state

                app_state.register_llm_call(
                    model_name=self.model,
                    purpose="gpt5_responses",
                    latency_seconds=payload["latency_seconds"],
                    input_chars=payload["input_chars"],
                    output_chars=payload["output_chars"],
                    effort=payload["effort"],
                    verbosity=payload["verbosity"],
                    usage=payload["usage"],
                    status=status,
                    error_message=error_message,
                    write_audit=False,
                )
            except Exception:
                pass

            if not (usage or {}).get("total_tokens"):
                return
            try:
                from tradingagents.run_logger import get_run_audit_logger

                get_run_audit_logger().log_event(event_type="llm_call", payload=payload)
            except Exception:
                pass
        
        # Bound-tool options (stored by bind_tools) apply when the caller
        # does not override them, so with_structured_output keeps its forced
        # tool choice and timeout across the clone.
        bound_tools = getattr(self, "_bound_tools", None)
        tools = kwargs.pop("tools", None)
        if tools is None:
            tools = bound_tools or []
        if tools:
            # Convert every bound tool to the Responses API function format.
            # An unconvertible schema fails here instead of silently
            # disappearing from the request.
            openai_tools = [
                _to_responses_function_tool(tool, strict=kwargs.get("strict"))
                for tool in tools
            ]
            api_params["tools"] = openai_tools

        tool_choice = kwargs.pop("tool_choice", None) or getattr(self, "_bound_tool_choice", None)
        if tool_choice is not None:
            normalized_choice = _normalize_responses_tool_choice(tool_choice)
            if normalized_choice is not None:
                normalized_choice = _endpoint_compatible_tool_choice(
                    self.model, normalized_choice
                )
                if normalized_choice is not None:
                    api_params["tool_choice"] = normalized_choice
        parallel_calls = kwargs.pop("parallel_tool_calls", None)
        if parallel_calls is None and tools:
            parallel_calls = self.parallel_tool_calls
        if parallel_calls is not None:
            api_params["parallel_tool_calls"] = bool(parallel_calls)
        
        try:
            # Make the API call
            call_started = time.time()
            response = self._client.responses.create(**api_params)
            latency_seconds = time.time() - call_started
            
            # Debug: Print response structure
            if hasattr(response, 'output') and response.output:
                print(f"[GPT5] Response has {len(response.output)} output items")
                for i, item in enumerate(response.output):
                    item_type = getattr(item, 'type', 'unknown')
                    print(f"[GPT5]   Item {i}: type={item_type}")
                    if item_type == 'function_call':
                        print(f"[GPT5]     Function: {getattr(item, 'name', 'unknown')}")
                    elif item_type == 'message':
                        # Debug: show message structure
                        if hasattr(item, 'content'):
                            print(f"[GPT5]     Message has content: {type(item.content)}")
                            if isinstance(item.content, list):
                                for j, c in enumerate(item.content):
                                    c_type = getattr(c, 'type', type(c).__name__)
                                    c_text = getattr(c, 'text', str(c)[:100] if c else 'None')
                                    print(f"[GPT5]       Content[{j}]: type={c_type}, text_len={len(c_text) if c_text else 0}")
                            elif isinstance(item.content, str):
                                print(f"[GPT5]       Content is string, len={len(item.content)}")
                        else:
                            print(f"[GPT5]     Message has no content attr, dir: {[a for a in dir(item) if not a.startswith('_')]}")
            
            # Extract content and tool calls
            content, tool_calls = self._extract_content_from_response(response)
            
            # Debug: Show extracted content
            if content:
                print(f"[GPT5] Extracted content: {len(content)} chars")
                print(f"[GPT5]   Content preview: {content[:200]}..." if len(content) > 200 else f"[GPT5]   Content: {content}")
            else:
                print(f"[GPT5] WARNING: No content extracted from response!")
                # Try to get output_text directly
                if hasattr(response, 'output_text') and response.output_text:
                    print(f"[GPT5]   But output_text exists: {len(response.output_text)} chars")
                    content = response.output_text
            
            if tool_calls:
                print(f"[GPT5] Found {len(tool_calls)} tool calls")
                for tc in tool_calls:
                    print(f"[GPT5]   Tool: {tc['function']['name']}")

            usage_dict = _extract_usage_dict(response)
            _record_llm_call(
                status="success",
                latency_seconds=latency_seconds,
                output_chars=len(content or ""),
                usage=usage_dict,
            )
            
            # Create the AI message
            additional_kwargs = {}
            if tool_calls:
                additional_kwargs["tool_calls"] = tool_calls
            # F15: mark adapter-accounted results so the common usage
            # callback never double counts this call.
            additional_kwargs["usage_accounted_by_adapter"] = True

            # Also add tool_calls attribute for LangChain compatibility
            ai_message = AIMessage(
                content=content,
                additional_kwargs=additional_kwargs,
                usage_metadata=dict(usage_dict)
                if (usage_dict.get("total_tokens") or usage_dict.get("input_tokens")
                    or usage_dict.get("output_tokens"))
                else None,
                response_metadata={"model_name": self.model},
            )
            
            # Set tool_calls attribute directly for better compatibility
            if tool_calls:
                ai_message.tool_calls = [
                    {
                        "name": tc["function"]["name"],
                        "args": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                        "id": tc["id"],
                        "type": "tool_call"
                    }
                    for tc in tool_calls
                ]
            
            generation = ChatGeneration(message=ai_message)
            return ChatResult(generations=[generation])
            
        except Exception as e:
            # Phase B: provider failures must propagate so the single retry
            # owner can classify them and the run stops instead of turning a
            # transport error into normal-looking message content.
            error_message = f"Error calling GPT-5 API: {str(e)}"
            print(f"[GPT5] {error_message}")
            latency_seconds = 0.0
            try:
                latency_seconds = max(0.0, time.time() - call_started)
            except Exception:
                pass
            _record_llm_call(
                status="error",
                latency_seconds=latency_seconds,
                output_chars=len(error_message),
                usage={},
                error_message=error_message,
            )
            raise
    
    def bind_tools(self, tools: List[Any], **kwargs) -> "GPT5ChatModel":
        """Bind tools to the model for function calling.

        The clone preserves tool_choice/strict plus every generation
        parameter, the client timeout, and the construction callbacks, so
        with_structured_output keeps its forced tool binding instead of
        silently dropping it.
        """
        new_model = GPT5ChatModel(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=self.default_headers,
            reasoning_effort=self.reasoning_effort,
            verbosity=self.verbosity,
            summary=self.summary,
            max_output_tokens=self.max_output_tokens,
            store=self.store,
            parallel_tool_calls=self.parallel_tool_calls,
            timeout=self.timeout,
            callbacks=self.callbacks,
        )
        new_model._bound_tools = list(tools or [])
        if "tool_choice" in kwargs:
            new_model._bound_tool_choice = kwargs["tool_choice"]
        if "strict" in kwargs:
            new_model._bound_strict = kwargs["strict"]
        # Fail fast at bind time when nothing here can become a Responses
        # function tool; waiting until invoke would hide the misconfiguration.
        try:
            for tool in new_model._bound_tools:
                _to_responses_function_tool(
                    tool, strict=kwargs.get("strict"),
                )
        except ToolBindingError:
            raise
        return new_model

    def invoke(self, input: Any, config: Optional[Dict] = None, **kwargs) -> AIMessage:
        """Invoke the model with input."""
        # Handle string input
        if isinstance(input, str):
            messages = [HumanMessage(content=input)]
        elif isinstance(input, list):
            messages = input
        else:
            messages = [HumanMessage(content=str(input))]

        result = self._generate(messages, **kwargs)
        return result.generations[0].message


def is_gpt5_model(model_name: str) -> bool:
    """Check if core chat calls should use the Responses API wrapper."""
    return is_responses_model(model_name)


def get_chat_model(model_name: str, api_key: Optional[str] = None, **kwargs):
    """Factory function to get the appropriate chat model."""
    base_url = kwargs.pop("base_url", None)
    model_role = kwargs.pop("model_role", "deep")
    # F08: the configured finite request timeout must survive into the
    # Responses adapter (and its bound clones), not just the ChatOpenAI path.
    timeout = kwargs.pop("timeout", None)
    callbacks = kwargs.pop("callbacks", None)

    if is_responses_model(model_name):
        params = normalize_model_params(model_name, kwargs, role=model_role)
        reasoning_effort = params.get("reasoning_effort", "medium")
        verbosity = params.get("text_verbosity") or params.get("verbosity", "medium")
        summary = params.get("reasoning_summary") or params.get("summary", "auto")
        max_output_tokens = params.get("max_output_tokens")
        store = bool(params.get("store", False))
        parallel_tool_calls = bool(params.get("parallel_tool_calls", True))

        return GPT5ChatModel(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            default_headers=_endpoint_headers(base_url, kwargs.get("default_headers")),
            reasoning_effort=reasoning_effort,
            verbosity=verbosity,
            summary=summary,
            max_output_tokens=max_output_tokens,
            store=store,
            parallel_tool_calls=parallel_tool_calls,
            timeout=timeout,
            callbacks=callbacks,
        )
    else:
        from langchain_openai import ChatOpenAI
        # Chat Completions uses max_tokens; the UI uses max_output_tokens.
        if "max_output_tokens" in kwargs and "max_tokens" not in kwargs:
            kwargs["max_tokens"] = kwargs.pop("max_output_tokens")
        for unsupported in (
            "reasoning_effort",
            "text_verbosity",
            "verbosity",
            "reasoning_summary",
            "summary",
            "store",
            "parallel_tool_calls",
        ):
            kwargs.pop(unsupported, None)
        # Phase B: SDK retries pinned to 0; single retry owner owns the cap.
        kwargs["max_retries"] = 0
        if callbacks:
            kwargs["callbacks"] = callbacks
        chat_kwargs = {"model": model_name, **kwargs}
        if api_key is not None:
            chat_kwargs["openai_api_key"] = api_key
        if base_url:
            chat_kwargs["openai_api_base"] = base_url
        chat_headers = _endpoint_headers(base_url, chat_kwargs.get("default_headers"))
        if chat_headers:
            chat_kwargs["default_headers"] = chat_headers
        # NormalizedChatOpenAI (deferred import: it imports this module) forces
        # with_structured_output onto the function-calling method. The default
        # ChatOpenAI path prefers native json_schema structured outputs, which
        # several OpenAI-compatible upstreams (e.g. Novita-hosted models) do
        # not support, while bound-tool function calling is universally
        # available and already verified per endpoint.
        try:
            from tradingagents.llm_clients.openai_client import (
                NormalizedChatOpenAI as _NormalizedChatOpenAI,
            )
        except Exception:
            _NormalizedChatOpenAI = ChatOpenAI
        return _NormalizedChatOpenAI(**chat_kwargs)
