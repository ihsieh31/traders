import os
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from tradingagents.llm_clients import create_llm_client
from tradingagents.llm_clients.anthropic_client import AnthropicClient
from tradingagents.llm_clients.google_client import GoogleClient
from tradingagents.llm_clients.openai_client import DeepSeekChatOpenAI


class LLMClientFactoryTests(unittest.TestCase):
    def test_factory_supports_all_configured_providers(self):
        provider_models = {
            "openai": "gpt-4.1",
            "local_openai": "gpt-4.1",
            "google": "gemini-2.5-flash",
            "anthropic": "claude-sonnet-4-6",
            "xai": "grok-4.5",
            "minimax": "MiniMax-M2.7",
            "deepseek": "deepseek-chat",
            "qwen": "qwen-plus",
            "glm": "glm-5",
            "openrouter": "custom/openrouter-model",
            "ollama": "qwen3:latest",
            "azure": "deployment-name",
        }

        for provider, model in provider_models.items():
            with self.subTest(provider=provider):
                client = create_llm_client(provider, model, api_key="test-key")
                self.assertEqual(client.model, model)

    def test_missing_api_keys_raise_clear_errors(self):
        required_key_cases = {
            "openai": ("gpt-4.1", "TRADINGBUFFETT_OPENAI_API_KEY"),
            "google": ("gemini-2.5-flash", "TRADINGBUFFETT_GOOGLE_API_KEY"),
            "anthropic": ("claude-sonnet-4-6", "TRADINGBUFFETT_ANTHROPIC_API_KEY"),
            "xai": ("grok-4.5", "TRADINGBUFFETT_XAI_API_KEY"),
            "minimax": ("MiniMax-M2.7", "TRADINGBUFFETT_MINIMAX_API_KEY"),
            "deepseek": ("deepseek-chat", "TRADINGBUFFETT_DEEPSEEK_API_KEY"),
            "qwen": ("qwen-plus", "TRADINGBUFFETT_DASHSCOPE_API_KEY"),
            "glm": ("glm-5", "TRADINGBUFFETT_ZHIPU_API_KEY"),
            "openrouter": ("custom/openrouter-model", "TRADINGBUFFETT_OPENROUTER_API_KEY"),
            "azure": ("deployment-name", "TRADINGBUFFETT_AZURE_OPENAI_API_KEY"),
        }

        home_env = {
            key: value
            for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
            if (value := os.environ.get(key))
        }
        home_env["PYTHON_DOTENV_DISABLED"] = "1"
        with patch.dict(os.environ, home_env, clear=True):
            for provider, (model, env_name) in required_key_cases.items():
                with self.subTest(provider=provider):
                    client = create_llm_client(provider, model)
                    with self.assertRaisesRegex(ValueError, env_name):
                        client.get_llm()

    def test_deepseek_reasoning_content_round_trip(self):
        llm = DeepSeekChatOpenAI(
            model="deepseek-chat",
            api_key="test-key",
            base_url="http://localhost/v1",
        )
        request_payload = llm._get_request_payload(
            [AIMessage(content="answer", additional_kwargs={"reasoning_content": "why"})]
        )
        self.assertEqual(request_payload["messages"][0]["reasoning_content"], "why")

        response = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": "why",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        result = llm._create_chat_result(response)
        self.assertEqual(
            result.generations[0].message.additional_kwargs["reasoning_content"],
            "why",
        )

    def test_google_thinking_level_maps_by_model_family(self):
        with patch("tradingagents.llm_clients.google_client.NormalizedChatGoogleGenerativeAI") as chat_cls:
            GoogleClient("gemini-2.5-flash", api_key="test-key", thinking_level="high").get_llm()
            kwargs = chat_cls.call_args.kwargs
            self.assertEqual(kwargs["thinking_budget"], -1)
            self.assertNotIn("thinking_level", kwargs)

        with patch("tradingagents.llm_clients.google_client.NormalizedChatGoogleGenerativeAI") as chat_cls:
            GoogleClient("gemini-3.1-pro-preview", api_key="test-key", thinking_level="minimal").get_llm()
            kwargs = chat_cls.call_args.kwargs
            self.assertEqual(kwargs["thinking_level"], "low")
            self.assertNotIn("thinking_budget", kwargs)

    def test_current_provider_models_build_with_expected_native_clients(self):
        with patch("tradingagents.llm_clients.google_client.NormalizedChatGoogleGenerativeAI") as chat_cls:
            GoogleClient("gemini-3.5-flash", api_key="test-key").get_llm()
            self.assertEqual(chat_cls.call_args.kwargs["model"], "gemini-3.5-flash")

        with patch("tradingagents.llm_clients.anthropic_client.NormalizedChatAnthropic") as chat_cls:
            AnthropicClient("claude-sonnet-5", api_key="test-key").get_llm()
            self.assertEqual(chat_cls.call_args.kwargs["model"], "claude-sonnet-5")

        with patch("tradingagents.llm_clients.openai_client.NormalizedChatOpenAI") as chat_cls:
            create_llm_client("xai", "grok-4.5", api_key="test-key").get_llm()
            self.assertEqual(chat_cls.call_args.kwargs["model"], "grok-4.5")
            self.assertEqual(chat_cls.call_args.kwargs["base_url"], "https://api.x.ai/v1")

    def test_minimax_uses_official_openai_compatible_endpoint_and_custom_override(self):
        with patch("tradingagents.llm_clients.openai_client.NormalizedChatOpenAI") as chat_cls:
            create_llm_client("minimax", "MiniMax-M2.7", api_key="test-key").get_llm()
            kwargs = chat_cls.call_args.kwargs
            self.assertEqual(kwargs["model"], "MiniMax-M2.7")
            self.assertEqual(kwargs["api_key"], "test-key")
            self.assertEqual(kwargs["base_url"], "https://api.minimax.io/v1")

        with patch("tradingagents.llm_clients.openai_client.NormalizedChatOpenAI") as chat_cls:
            create_llm_client(
                "minimax",
                "MiniMax-future-model",
                base_url="https://minimax-proxy.example/v1",
                api_key="test-key",
            ).get_llm()
            self.assertEqual(chat_cls.call_args.kwargs["model"], "MiniMax-future-model")
            self.assertEqual(
                chat_cls.call_args.kwargs["base_url"],
                "https://minimax-proxy.example/v1",
            )


if __name__ == "__main__":
    unittest.main()
