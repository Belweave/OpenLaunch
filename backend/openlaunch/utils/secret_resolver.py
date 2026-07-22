"""Pluggable secret references for persisted connection metadata."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class SecretResolutionError(RuntimeError):
    pass


class SecretResolver(ABC):
    @abstractmethod
    def resolve(self, reference: dict[str, Any]) -> str:
        """Resolve a reference without logging or returning it to callers."""


class EnvironmentSecretResolver(SecretResolver):
    def resolve(self, reference: dict[str, Any]) -> str:
        name = str(reference.get("name") or "")
        if not name or name not in os.environ:
            raise SecretResolutionError("Secret is unavailable.")
        return os.environ[name]


class FileSecretResolver(SecretResolver):
    def resolve(self, reference: dict[str, Any]) -> str:
        path = Path(str(reference.get("path") or ""))
        if not path.is_absolute() or not path.is_file():
            raise SecretResolutionError("Secret is unavailable.")
        value = path.read_text(encoding="utf-8")
        key = reference.get("key")
        if key:
            try:
                document = json.loads(value)
                value = document[key]
            except Exception as exc:
                raise SecretResolutionError("Secret is unavailable.") from exc
        return str(value).strip()


class LiteralSecretResolver(SecretResolver):
    """Compatibility-only resolver for migration; admin APIs never return its value."""

    def resolve(self, reference: dict[str, Any]) -> str:
        value = reference.get("value")
        if not isinstance(value, str) or not value:
            raise SecretResolutionError("Secret is unavailable.")
        return value


_RESOLVERS: dict[str, SecretResolver] = {
    "env": EnvironmentSecretResolver(),
    "file": FileSecretResolver(),
    "literal": LiteralSecretResolver(),
}


def register_secret_resolver(kind: str, resolver: SecretResolver) -> None:
    if not kind or kind in _RESOLVERS:
        raise ValueError("Secret resolver names must be unique.")
    _RESOLVERS[kind] = resolver


def resolve_secret(reference: dict[str, Any] | None) -> str:
    if not isinstance(reference, dict):
        raise SecretResolutionError("Secret is unavailable.")
    resolver = _RESOLVERS.get(str(reference.get("type") or ""))
    if resolver is None:
        raise SecretResolutionError("Secret is unavailable.")
    return resolver.resolve(reference)


def inject_connection_secret(metadata: dict[str, Any], reference: dict[str, Any] | None) -> dict[str, Any]:
    """Copy safe metadata and place a resolved secret in its configured field."""
    result = dict(metadata)
    if reference:
        field = str(reference.get("field") or "url")
        if field not in {
            "url",
            "connection_string",
            "config",
            "password",
            "token",
            "private_key",
        }:
            raise SecretResolutionError("Secret reference field is invalid.")
        value = resolve_secret(reference)
        if field == "config":
            try:
                value = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise SecretResolutionError("Secret is unavailable.") from exc
            if not isinstance(value, dict):
                raise SecretResolutionError("Secret is unavailable.")
        result[field] = value
    return result
