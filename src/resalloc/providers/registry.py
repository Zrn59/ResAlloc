"""Register extra providers without changing the hub."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from resalloc.providers.base import Provider

Factory = Callable[[Path], Provider]

_FACTORIES: dict[str, Factory] = {}


def register_provider_if_absent(name: str, factory: Factory) -> None:
    if name not in _FACTORIES:
        register_provider(name, factory)


def register_provider(name: str, factory: Factory) -> None:
    """Register a provider. The factory receives the hub root."""

    if not name or name != name.strip():
        raise ValueError("provider name must be a non-empty string")
    _FACTORIES[name] = factory


def registered_names() -> list[str]:
    return sorted(_FACTORIES)


def build_registered(root: Path) -> dict[str, Provider]:
    return {name: factory(root) for name, factory in _FACTORIES.items()}
