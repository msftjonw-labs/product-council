"""Desktop-specific model selection and council setup, with no on-disk configuration."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .config import CouncilConfig, SeatConfig
from .orchestrator import ClarifyFn, Council, EventFn
from .prompts import parse_json
from .providers import LLMProvider, LLMRequest, create_provider
from .research import AskFn, HumanSource
from .schemas import CouncilOutcome, ProductBrief


@dataclass
class ModelChoice:
    provider: str
    model: str
    api_key: str = field(repr=False)
    endpoint: str = ""

    def spec(self, *, require_model: bool = True) -> dict[str, Any]:
        if self.provider not in ("openai", "anthropic", "azure_openai"):
            raise ValueError("Choose OpenAI, Anthropic, or Azure OpenAI.")
        if not self.api_key.strip():
            raise ValueError("An API key is required.")
        if require_model and not self.model.strip():
            raise ValueError("Load and select a model, or enter a model/deployment name in manual mode.")
        spec: dict[str, Any] = {"type": self.provider, "api_key": self.api_key.strip()}
        if self.provider == "azure_openai":
            url = urlparse(self.endpoint.strip())
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Azure OpenAI requires a valid HTTPS resource endpoint without credentials.")
            spec.update(endpoint=self.endpoint.strip(), api_version="2024-10-21", use_entra_id=False)
        return spec


class CredentialRedactor(logging.Filter):
    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key.strip()

    def filter(self, record: logging.LogRecord) -> bool:
        if self.key:
            record.msg = record.getMessage().replace(self.key, "[REDACTED]")
            record.args = ()
        return True


class DesktopProvider(LLMProvider):
    """Keep SDK exception bodies out of council events, vote rationales, and exports."""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def complete(self, request: LLMRequest) -> str:
        try:
            return await self.provider.complete(request)
        except Exception as exc:
            raise RuntimeError(safe_error(exc)) from None

    async def close(self) -> None:
        await self.provider.close()


def safe_error(exc: Exception) -> str:
    """Never display SDK error bodies, request headers, or credentials."""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "Access denied. Check your API key, model permissions, and selected provider."
    if status == 404:
        return "Model or endpoint not found. Check the model ID (Azure: deployment name) and endpoint."
    if status == 429:
        return "The provider's quota or rate limit was reached. Check billing/quota or try again later."
    return ("The model request failed. Check connectivity, model availability, and whether the model supports "
            "chat and structured JSON responses. No report was saved.")


class ModelDiscoveryError(ValueError):
    pass


async def discover_models(choice: ModelChoice) -> list[str]:
    if choice.provider == "azure_openai":
        raise ModelDiscoveryError("Azure requires a deployment name from your resource administrator.")
    provider = create_provider(choice.spec(require_model=False))
    try:
        models = await provider.list_models()
        if not models:
            raise ModelDiscoveryError(
                "The provider returned no supported chat models. Check account access or use manual mode "
                "with a model supplied by your administrator."
            )
        return models
    finally:
        await provider.close()


async def validate_choice(choice: ModelChoice) -> None:
    """A real inference request checks both credentials and access to the chosen model."""
    provider = create_provider(choice.spec())
    try:
        response = await provider.complete(LLMRequest(
            model=choice.model.strip(), system='Reply with the JSON object {"ok": true}.',
            user='Connection test only. Return {"ok": true}.', temperature=None, max_tokens=256,
        ))
        if parse_json(response).get("ok") is not True:
            raise ValueError("The model did not return the required JSON connection-test response.")
    finally:
        await provider.close()


async def run_session(
    choice: ModelChoice,
    question: str,
    brief: ProductBrief,
    *,
    clarify: ClarifyFn,
    ask_research: AskFn,
    on_event: EventFn,
) -> CouncilOutcome:
    if not question.strip():
        raise ValueError("A productization idea is required.")
    brief.validate()
    cfg = CouncilConfig(providers={"selected": choice.spec()})
    for role in cfg.seats:
        cfg.seats[role] = SeatConfig(provider="selected", model=choice.model.strip(), temperature=None)
    cfg.moderator = SeatConfig(provider="selected", model=choice.model.strip(), temperature=None)
    provider = DesktopProvider(create_provider(cfg.providers["selected"]))
    redactor = CredentialRedactor(choice.api_key)
    logger = logging.getLogger("product_council")
    logger.addFilter(redactor)
    try:
        council = Council(
            cfg, providers={"selected": provider}, sources=[HumanSource(ask_research)],
            clarify=clarify, on_event=on_event,
        )
        return await council.run(question.strip(), brief=brief)
    finally:
        logger.removeFilter(redactor)
        await provider.close()
