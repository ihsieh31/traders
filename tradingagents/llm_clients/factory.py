from typing import Optional

from .base_client import BaseLLMClient
from .usage import ensure_usage_callback


_OPENAI_COMPATIBLE = (
    "openai",
    "local_openai",
    "xai",
    "minimax",
    "deepseek",
    "qwen",
    "glm",
    "ollama",
    "openrouter",
)


def create_llm_client(provider: str, model: str, base_url: Optional[str] = None, **kwargs) -> BaseLLMClient:
    provider_lower = (provider or "openai").lower()
    # F15: every constructed LLM carries exactly one usage-accounting
    # callback (in addition to any caller/UI callbacks) so provider-reported
    # token usage reaches the audit log and the daily safety budget once.
    kwargs["callbacks"] = ensure_usage_callback(kwargs.get("callbacks"))

    if provider_lower in _OPENAI_COMPATIBLE:
        from .openai_client import OpenAIClient

        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient

        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient

        return AzureOpenAIClient(model, base_url, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")
