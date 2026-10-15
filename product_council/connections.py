"""Opt-in connection persistence exclusively in Windows Credential Manager."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Protocol

from .desktop_session import ModelChoice


class Vault(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class ConnectionStoreError(RuntimeError):
    pass


class ConnectionStore:
    SERVICE = "ProductCouncil"
    ACCOUNT = "remembered-connections-v1"

    def __init__(self, vault: Vault | None = None) -> None:
        self.vault = vault

    def backend(self) -> Vault:
        if self.vault is None:
            # Never use automatic backend selection: plaintext/third-party fallbacks are not acceptable.
            from keyring.backends.Windows import WinVaultKeyring

            self.vault = WinVaultKeyring()
        return self.vault

    def _read(self) -> dict:
        raw = self.backend().get_password(self.SERVICE, self.ACCOUNT)
        if raw is None:
            return {"preferred": None, "connections": {}}
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"preferred", "connections"}:
            raise ValueError("Invalid saved connection data")
        if not isinstance(data["connections"], dict):
            raise ValueError("Invalid saved connections")
        for provider, fields in data["connections"].items():
            choice = ModelChoice(**fields)
            choice.spec()
            if choice.provider != provider:
                raise ValueError("Saved provider mismatch")
        if data["preferred"] is not None and data["preferred"] not in data["connections"]:
            raise ValueError("Invalid preferred provider")
        return data

    def load(self, provider: str | None = None) -> ModelChoice | None:
        try:
            data = self._read()
            fields = data["connections"].get(provider or data["preferred"])
            return ModelChoice(**fields) if fields else None
        except Exception:
            raise ConnectionStoreError(
                "Windows Credential Manager could not read the saved connection. "
                "Use a session-only connection or ask your administrator for help."
            ) from None

    def save(self, choice: ModelChoice) -> None:
        choice.spec()
        try:
            data = self._read()
            data["connections"][choice.provider] = asdict(choice)
            data["preferred"] = choice.provider
            self.backend().set_password(self.SERVICE, self.ACCOUNT, json.dumps(data))
        except Exception:
            raise ConnectionStoreError(
                "The connection could not be saved in Windows Credential Manager. "
                "No plaintext fallback was used. Retry or uncheck Remember for a session-only connection."
            ) from None

    def forget(self, provider: str) -> None:
        try:
            data = self._read()
            if provider not in data["connections"]:
                return
            del data["connections"][provider]
            if not data["connections"]:
                self.backend().delete_password(self.SERVICE, self.ACCOUNT)
            else:
                if data["preferred"] == provider:
                    data["preferred"] = next(iter(data["connections"]))
                self.backend().set_password(self.SERVICE, self.ACCOUNT, json.dumps(data))
        except Exception:
            raise ConnectionStoreError(
                "The saved connection could not be removed from Windows Credential Manager. "
                "Ask your administrator for help; it may still be stored."
            ) from None
