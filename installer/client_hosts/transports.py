"""Static transport-to-renderer compatibility registry."""

from __future__ import annotations

from types import MappingProxyType
from typing import FrozenSet, Mapping


_TRANSPORT_RENDERERS = MappingProxyType(
    {
        "stdio": frozenset(
            {"json-mcp-v1", "cursor-json-mcp-v1", "codex-toml-v1"}
        ),
    }
)


def transport_supports_renderer(transport_id: str, renderer_id: str) -> bool:
    """Return whether a registered transport can use ``renderer_id``."""

    return renderer_id in _TRANSPORT_RENDERERS.get(transport_id, frozenset())


def registered_transports() -> Mapping[str, FrozenSet[str]]:
    """Expose immutable transport metadata for conformance checks."""

    return _TRANSPORT_RENDERERS
