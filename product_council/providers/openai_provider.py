"""OpenAI and Azure OpenAI (Azure AI Foundry) chat-completions provider."""
from __future__ import annotations

from typing import Any

from .base import LLMProvider, LLMRequest


def _is_chat_model(model_id: str) -> bool:
    prefixes = ("gpt-", "chatgpt-", "o1", "o3", "o4")
    excluded = ("audio", "realtime", "transcrib", "tts", "image", "instruct", "search", "codex", "-pro")
    return model_id.startswith(prefixes) and not any(part in model_id for part in excluded)


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        *,
        azure: bool = False,
        endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str | None = None,
        use_entra_id: bool = False,
    ) -> None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("Install the 'openai' package: pip install product-council[openai]") from exc

        self.name = "azure_openai" if azure else "openai"
        if azure:
            kwargs: dict[str, Any] = {"azure_endpoint": endpoint, "api_version": api_version or "2024-10-21"}
            if use_entra_id or not api_key:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider

                kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
                )
            else:
                kwargs["api_key"] = api_key
            self._client = openai.AsyncAzureOpenAI(**kwargs)
        else:
            self._client = openai.AsyncOpenAI(api_key=api_key, base_url=endpoint)

    async def complete(self, request: LLMRequest) -> str:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_completion_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    async def close(self) -> None:
        await self._client.close()

    async def list_models(self) -> list[str]:
        if self.name == "azure_openai":
            raise NotImplementedError("Azure requires a deployment name from your resource administrator.")
        page = await self._client.models.list()
        models = [model async for model in page if _is_chat_model(model.id)]
        models.sort(key=lambda model: (model.created, model.id), reverse=True)
        return list(dict.fromkeys(model.id for model in models))
