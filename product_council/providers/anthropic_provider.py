"""Anthropic Messages API provider."""
from __future__ import annotations

from .base import LLMProvider, LLMRequest


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, endpoint: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("Install the 'anthropic' package: pip install product-council[anthropic]") from exc
        self._client = anthropic.AsyncAnthropic(api_key=api_key, base_url=endpoint)

    async def complete(self, request: LLMRequest) -> str:
        user = request.user
        if request.json_mode:
            user += "\n\nRespond with a single valid JSON object only."
        kwargs = {
            "model": request.model,
            "system": request.system,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        resp = await self._client.messages.create(**kwargs)
        return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
