"""Provider abstraction so every council seat can run on a different frontier model."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMRequest:
    model: str
    system: str
    user: str
    json_mode: bool = True
    temperature: float | None = 0.3
    max_tokens: int = 2000
    # Structured context. Real providers ignore it; the mock provider uses it to behave deterministically.
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def complete(self, request: LLMRequest) -> str:
        """Return the raw text completion for the request."""

    async def close(self) -> None:
        """Release provider resources when a desktop session finishes."""

    async def list_models(self) -> list[str]:
        """List chat model candidates, newest first. Inference access must still be validated."""
        raise NotImplementedError("This provider does not support chat model discovery.")
