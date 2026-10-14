"""Validated MCP entry renderer registry.

Renderer algorithms live here so a host selects behavior by renderer ID. Shared
orchestration supplies launch inputs and never branches on a product name.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from installer.config import ShellError
from installer.client_hosts import jsonc


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
    server_name: str = "decision-engine"


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


def _render_mimocode_jsonc(request: RendererRequest) -> Dict[str, Any]:
    environment = {"PYTHONPATH": request.root}
    environment.update(request.environment)
    return {
        "type": "local",
        "command": [request.command, *request.launcher_args],
        "environment": environment,
        "enabled": True,
    }


def _parse_jsonc(text: str) -> Any:
    return jsonc.parse(text).data


CORDIS_MANAGED_BEGIN = "# BEGIN DEEP PATTERN decision-engine (managed)"
CORDIS_MANAGED_END = "# END DEEP PATTERN decision-engine (managed)"
CORDIS_PLUGIN_NAME = "@deepseek-ai/dsh-mcp-client"
CORDIS_ORIGINAL_FILE = "# DE-ORIGINAL-FILE-BASE64:"
CORDIS_ORIGINAL_ABSENT = "# DE-ORIGINAL-FILE-ABSENT: true"
CORDIS_ADDED_SEPARATOR = "# DE-ADDED-SEPARATOR:"


def _render_cordis_mcp(request: RendererRequest) -> Dict[str, Any]:
    return {
        "plugin_name": CORDIS_PLUGIN_NAME,
        "server_name": request.server_name,
        "type": request.transport,
        "command": request.command,
        "args": list(request.launcher_args),
        "cwd": request.root,
        "env": dict(request.environment),
        "tool_timeout_sec": int(request.tool_timeout_sec),
    }


def _yaml_value(value: Any) -> str:
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_cordis_managed_block(
    entry: Mapping[str, Any],
    *,
    newline: str = "\n",
    metadata: Tuple[str, ...] = (),
) -> str:
    """Render one canonical Cordis patch insertion without parsing user YAML."""

    environment = entry.get("env")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ShellError("Cordis MCP environment is invalid")
    if any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None
        for key in environment
    ):
        raise ShellError("Cordis MCP environment key is invalid")
    required = {
        "plugin_name": str,
        "server_name": str,
        "type": str,
        "command": str,
        "args": list,
        "cwd": str,
        "tool_timeout_sec": int,
    }
    if any(not isinstance(entry.get(key), kind) for key, kind in required.items()):
        raise ShellError("Cordis MCP entry is invalid")
    lines = [CORDIS_MANAGED_BEGIN, *metadata]
    lines.extend(
        [
            "- insert:",
            "    - id: %s" % _yaml_value(entry["server_name"]),
            "      name: %s" % _yaml_value(entry["plugin_name"]),
            "      config:",
            "        serverName: %s" % _yaml_value(entry["server_name"]),
            "        transport: %s" % _yaml_value(entry["type"]),
            "        command: %s" % _yaml_value(entry["command"]),
            "        args: %s" % _yaml_value(entry["args"]),
            "        cwd: %s" % _yaml_value(entry["cwd"]),
            "        env:",
        ]
    )
    lines.extend(
        "          %s: %s" % (key, _yaml_value(value))
        for key, value in sorted(environment.items())
    )
    lines.extend(
        [
            "        toolCallTimeoutMs: %d"
            % (int(entry["tool_timeout_sec"]) * 1000),
            CORDIS_MANAGED_END,
        ]
    )
    return newline.join(lines) + newline


def _managed_span(text: str) -> Optional[Tuple[int, int]]:
    begin_count = text.count(CORDIS_MANAGED_BEGIN)
    end_count = text.count(CORDIS_MANAGED_END)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise ShellError("Cordis managed markers are malformed or duplicated")
    start = text.index(CORDIS_MANAGED_BEGIN)
    finish = text.index(CORDIS_MANAGED_END)
    if finish < start:
        raise ShellError("Cordis managed markers are out of order")
    finish += len(CORDIS_MANAGED_END)
    if text.startswith("\r\n", finish):
        finish += 2
    elif text.startswith("\n", finish):
        finish += 1
    return start, finish


def cordis_managed_span(text: str) -> Optional[Tuple[int, int]]:
    return _managed_span(text)


def _cordis_scalar(raw: str) -> Optional[str]:
    value = raw.strip()
    if not value:
        return None
    if value.startswith('"'):
        try:
            loaded = json.loads(value)
        except ValueError:
            return None
        return loaded if isinstance(loaded, str) else None
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value.split(" #", 1)[0].strip()


def validate_cordis_patch_text(text: str) -> None:
    """Validate the bounded shape DE relies on while leaving !!js opaque."""

    if "\x00" in text:
        raise ShellError("Cordis patch contains a NUL byte")
    body = text[1:] if text.startswith("\ufeff") else text
    span = _managed_span(body)
    if span is not None:
        parse_cordis_managed_entry(body)
    outside = body if span is None else body[: span[0]] + body[span[1] :]
    stripped = outside.strip()
    if not stripped:
        return
    if re.fullmatch(r"\[\s*\]", stripped):
        return
    saw_row = False
    for line in outside.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indentation = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in indentation:
            raise ShellError("Cordis patch uses unsafe tab indentation")
        if not indentation:
            if not line.startswith("-"):
                raise ShellError("Cordis patch must be a top-level sequence")
            saw_row = True
        if re.search(r":\s*\[[^\]\r\n]*$", line):
            raise ShellError("Cordis patch contains an unterminated flow sequence")
    if not saw_row:
        raise ShellError("Cordis patch must be a top-level sequence")


def cordis_foreign_conflict(text: str, server_name: str) -> Optional[str]:
    body = text[1:] if text.startswith("\ufeff") else text
    span = _managed_span(body)
    outside = body if span is None else body[: span[0]] + body[span[1] :]
    for field in ("id", "serverName"):
        for match in re.finditer(
            r"(?m)^\s*(?:-\s*)?" + field + r":\s*(.+?)\s*$",
            outside,
        ):
            if _cordis_scalar(match.group(1)) == server_name:
                return field
    return None


def _json_field(block: str, field: str) -> Any:
    match = re.search(
        r"(?m)^\s*(?:-\s*)?" + re.escape(field) + r":\s*(.+?)\s*$",
        block,
    )
    if match is None:
        raise ShellError("Cordis managed entry is incomplete")
    try:
        return json.loads(match.group(1))
    except (TypeError, ValueError) as exc:
        raise ShellError("Cordis managed entry contains an invalid scalar") from exc


def parse_cordis_managed_entry(text: str) -> Optional[Dict[str, Any]]:
    span = _managed_span(text)
    if span is None:
        return None
    block = text[span[0] : span[1]]
    env: Dict[str, str] = {}
    env_match = re.search(
        r"(?ms)^\s{8}env:\s*\r?\n(?P<body>(?:\s{10}[^\r\n]+\r?\n)*)",
        block,
    )
    if env_match is None:
        raise ShellError("Cordis managed entry environment is missing")
    for line in env_match.group("body").splitlines():
        key, separator, raw = line.strip().partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ShellError("Cordis managed entry environment is invalid")
        try:
            value = json.loads(raw.strip())
        except ValueError as exc:
            raise ShellError("Cordis managed entry environment is invalid") from exc
        if not isinstance(value, str):
            raise ShellError("Cordis managed entry environment is invalid")
        env[key] = value
    timeout_ms = _json_field(block, "toolCallTimeoutMs")
    if not isinstance(timeout_ms, int) or timeout_ms <= 0 or timeout_ms % 1000:
        raise ShellError("Cordis managed entry timeout is invalid")
    entry = {
        "plugin_name": _json_field(block, "name"),
        "server_name": _json_field(block, "serverName"),
        "type": _json_field(block, "transport"),
        "command": _json_field(block, "command"),
        "args": _json_field(block, "args"),
        "cwd": _json_field(block, "cwd"),
        "env": env,
        "tool_timeout_sec": timeout_ms // 1000,
    }
    row_id = _json_field(block, "id")
    if (
        row_id != entry["server_name"]
        or entry["plugin_name"] != CORDIS_PLUGIN_NAME
        or entry["type"] != "stdio"
        or not isinstance(entry["command"], str)
        or not isinstance(entry["args"], list)
        or not all(isinstance(value, str) for value in entry["args"])
        or not isinstance(entry["cwd"], str)
    ):
        raise ShellError("Cordis managed entry identity is invalid")
    metadata = tuple(
        line
        for line in block.splitlines()[1:]
        if line.startswith(
            (CORDIS_ORIGINAL_FILE, CORDIS_ORIGINAL_ABSENT, CORDIS_ADDED_SEPARATOR)
        )
    )
    if len(metadata) > 1:
        raise ShellError("Cordis managed restoration metadata is invalid")
    if metadata:
        value = metadata[0]
        if value.startswith(CORDIS_ORIGINAL_FILE):
            encoded = value[len(CORDIS_ORIGINAL_FILE) :].strip()
            try:
                restored = base64.b64decode(encoded, validate=True)
                restored.decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise ShellError("Cordis managed restoration metadata is invalid")
        elif value.startswith(CORDIS_ADDED_SEPARATOR) and value not in {
            CORDIS_ADDED_SEPARATOR + " LF",
            CORDIS_ADDED_SEPARATOR + " CRLF",
        }:
            raise ShellError("Cordis managed restoration metadata is invalid")
    newline = "\r\n" if "\r\n" in block else "\n"
    canonical = render_cordis_managed_block(
        entry,
        newline=newline,
        metadata=metadata,
    )
    if block != canonical:
        raise ShellError("Cordis managed entry is not canonical")
    return entry


def _parse_cordis(text: str) -> Any:
    validate_cordis_patch_text(text)
    entry = parse_cordis_managed_entry(text)
    if entry is None:
        return {"plugins": {}}
    return {"plugins": {entry["server_name"]: entry}}


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
        "cordis-yaml-v1": ConfigRenderer(
            id="cordis-yaml-v1",
            config_format="yaml",
            collection_key="plugins",
            writer="cordis-managed-block-v1",
            render=_render_cordis_mcp,
            parse=_parse_cordis,
        ),
        "mimocode-jsonc-mcp-v1": ConfigRenderer(
            id="mimocode-jsonc-mcp-v1",
            config_format="jsonc",
            collection_key="mcp",
            writer="mimocode-jsonc-splice-v1",
            render=_render_mimocode_jsonc,
            parse=_parse_jsonc,
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
