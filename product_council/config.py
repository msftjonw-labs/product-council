"""Council configuration (TOML) with ${ENV_VAR} expansion."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .roles import ROLES


@dataclass
class SeatConfig:
    provider: str = "mock"
    model: str = "mock"
    temperature: float | None = 0.3
    max_tokens: int = 2000


@dataclass
class CouncilConfig:
    consensus_threshold: float = 0.75
    max_iterations: int = 4
    max_clarifying_questions: int = 5
    initial_top_k: int = 2
    research_top_k: int = 2
    max_research_requests: int = 8
    stop_when_research_exhausted: bool = True
    skeptic_votes: bool = True
    providers: dict[str, dict[str, Any]] = field(default_factory=lambda: {"mock": {"type": "mock"}})
    seats: dict[str, SeatConfig] = field(default_factory=lambda: {k: SeatConfig() for k in ROLES})
    moderator: SeatConfig = field(default_factory=SeatConfig)
    sources: list[dict[str, str]] = field(default_factory=list)

    def validate(self) -> None:
        if not 0 < self.consensus_threshold <= 1:
            raise ValueError("consensus_threshold must be in (0, 1]")
        missing = [k for k in ROLES if k not in self.seats]
        if missing:
            raise ValueError(f"Config is missing seats for roles: {missing}")
        for name, seat in [*self.seats.items(), ("moderator", self.moderator)]:
            if seat.provider not in self.providers:
                raise ValueError(f"Seat '{name}' references unknown provider '{seat.provider}'")


_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str | Path) -> CouncilConfig:
    path = Path(path)
    raw = _expand(tomllib.loads(path.read_text(encoding="utf-8")))
    council = raw.get("council", {})
    cfg = CouncilConfig(**{k: v for k, v in council.items() if k in CouncilConfig.__dataclass_fields__})
    cfg.providers = raw.get("providers", cfg.providers)
    if "seats" in raw:
        cfg.seats = {k: SeatConfig(**v) for k, v in raw["seats"].items()}
    if "moderator" in raw:
        cfg.moderator = SeatConfig(**raw["moderator"])
    base = path.parent
    cfg.sources = [
        {**s, "path": str((base / s["path"]).resolve())} if "path" in s else s for s in raw.get("sources", [])
    ]
    cfg.validate()
    return cfg


def force_mock(cfg: CouncilConfig) -> CouncilConfig:
    cfg.providers = {"mock": {"type": "mock"}}
    for seat in [*cfg.seats.values(), cfg.moderator]:
        seat.provider, seat.model = "mock", "mock"
    return cfg
