"""Validated MCP entry renderer registry.

Renderer algorithms live here so a host selects behavior by renderer ID. Shared
orchestration supplies launch inputs and never branches on a product name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from installer.config import ShellError


@dataclass(frozen=True)
class RendererRequest:
    """Product-neutral inputs required to render one MCP server entry."""

    command: str
    root: str
    launcher_args: Tuple[str, ...]
    cwd_independent_args: Tuple[str, ...]
    environment: Mapping[str, str]
    transport: str
    json_include_type: bool
    json_include_cwd: bool
    startup_timeout_sec: int
    tool_timeout_sec: int


@dataclass(frozen=True)
class ConfigRenderer:
    id: str
    config_format: str
    collection_key: str
    writer: str
    render: Callable[[RendererRequest], Dict[str, Any]]
    parse: Callable[[str], Any]


def _tomllib():
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        from installer._vendor import tomli as tomllib
    return tomllib


def _parse_json(text: str) -> Any:
    return json.loads(text) if text.strip() else {}


def _parse_toml(text: str) -> Any:
    return _tomllib().loads(text) if text.strip() else {}


def _render_json_mcp(request: RendererRequest) -> Dict[str, Any]:
    entry: Dict[str, Any] = {}
    if request.json_include_type:
        entry["type"] = request.transport
    entry["command"] = request.command
    entry["args"] = list(
        request.launcher_args
        if request.json_include_cwd
        else request.cwd_independent_args
    )
    if request.json_include_cwd:
        entry["cwd"] = request.root
    environment = {"PYTHONPATH": request.root}
    environment.update(request.environment)
    entry["env"] = environment
    return entry


def _render_codex_toml(request: RendererRequest) -> Dict[str, Any]:
    return {
        "command": request.command,
        "args": list(request.launcher_args),
        "cwd": request.root,
        "env": dict(request.environment),
        "startup_timeout_sec": int(request.startup_timeout_sec),
        "tool_timeout_sec": int(request.tool_timeout_sec),
    }


_RENDERERS = MappingProxyType(
    {
        "json-mcp-v1": ConfigRenderer(
            id="json-mcp-v1",
            config_format="json",
            collection_key="mcpServers",
            writer="json-merge-v1",
            render=_render_json_mcp,
            parse=_parse_json,
        ),
        "cursor-json-mcp-v1": ConfigRenderer(
            id="cursor-json-mcp-v1",
            config_format="json",
            collection_key="mcpServers",
            writer="json-merge-v1",
            render=_render_json_mcp,
            parse=_parse_json,
        ),
        "codex-toml-v1": ConfigRenderer(
            id="codex-toml-v1",
            config_format="toml",
            collection_key="mcp_servers",
            writer="toml-splice-v1",
            render=_render_codex_toml,
            parse=_parse_toml,
        ),
    }
)


def renderer(renderer_id: str) -> Optional[ConfigRenderer]:
    """Return one registered renderer adapter, if present."""

    return _RENDERERS.get(renderer_id)


def render_entry(renderer_id: str, request: RendererRequest) -> Dict[str, Any]:
    """Render one entry through the registered adapter or fail closed."""

    adapter = renderer(renderer_id)
    if adapter is None:
        raise ShellError("configuration renderer %r is unsupported" % renderer_id)
    return adapter.render(request)


def renderer_format(renderer_id: str) -> Optional[str]:
    """Return the registered config format, or ``None`` for unknown IDs."""

    adapter = renderer(renderer_id)
    return adapter.config_format if adapter is not None else None


def renderer_collection_key(renderer_id: str) -> Optional[str]:
    """Return the top-level MCP server collection key for a renderer."""

    adapter = renderer(renderer_id)
    return adapter.collection_key if adapter is not None else None


def renderer_writer(renderer_id: str) -> Optional[str]:
    """Return the registered config-writer strategy for a renderer."""

    adapter = renderer(renderer_id)
    return adapter.writer if adapter is not None else None


def parse_server_collection(renderer_id: str, text: str) -> Any:
    """Parse a config and return its renderer-defined MCP collection."""

    adapter = renderer(renderer_id)
    if adapter is None:
        raise ShellError("configuration renderer %r is unsupported" % renderer_id)
    loaded = adapter.parse(text)
    return loaded.get(adapter.collection_key) if isinstance(loaded, dict) else None


def registered_renderers() -> Mapping[str, ConfigRenderer]:
    """Expose immutable renderer adapters for conformance checks."""

    return _RENDERERS
