"""Stable extension interface for provider-neutral data-source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdapterCapabilities:
    inspect_schema: bool = False
    query: bool = False
    bounded_operations: tuple[str, ...] = ()
    supports_cancellation: bool = False
    supports_explain: bool = False


class DataSourceAdapter(ABC):
    provider_types: tuple[str, ...] = ()
    capabilities = AdapterCapabilities()

    @abstractmethod
    def test_connection(self, connection: dict[str, Any]) -> None:
        pass

    def inspect(self, connection: dict[str, Any], **options) -> str:
        raise NotImplementedError

    def query(self, connection: dict[str, Any], operation: str, **options) -> str:
        raise NotImplementedError


_ADAPTERS: dict[str, DataSourceAdapter] = {}


def register_adapter(adapter: DataSourceAdapter) -> None:
    for provider_type in adapter.provider_types:
        if provider_type in _ADAPTERS:
            raise ValueError(f"Adapter already registered for {provider_type}.")
        _ADAPTERS[provider_type] = adapter


def get_adapter(provider_type: str) -> DataSourceAdapter:
    try:
        return _ADAPTERS[provider_type]
    except KeyError as exc:
        raise ValueError("The data-source provider is unsupported.") from exc


def adapter_capabilities() -> dict[str, AdapterCapabilities]:
    return {provider: adapter.capabilities for provider, adapter in _ADAPTERS.items()}
