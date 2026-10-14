from .base import LLMProvider, LLMRequest
from .mock import MockProvider


def create_provider(spec: dict) -> LLMProvider:
    """Build a provider from a config block: {type, endpoint?, api_key?, api_version?, use_entra_id?}."""
    ptype = spec.get("type", "mock")
    if ptype == "mock":
        return MockProvider()
    if ptype in ("openai", "azure_openai"):
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            azure=ptype == "azure_openai",
            endpoint=spec.get("endpoint") or None,
            api_key=spec.get("api_key") or None,
            api_version=spec.get("api_version"),
            use_entra_id=bool(spec.get("use_entra_id", False)),
        )
    if ptype == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=spec.get("api_key") or None, endpoint=spec.get("endpoint") or None)
    raise ValueError(f"Unknown provider type: {ptype}")


__all__ = ["LLMProvider", "LLMRequest", "MockProvider", "create_provider"]
