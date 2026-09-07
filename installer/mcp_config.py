"""Emit the MCP server registration for the Decision Engine launcher.

After ``install de`` + ``python3 -m installer.activate``, the last step is
telling the agent (Claude Code / Codex / Cursor) how to launch the transport shim so its
tools appear in-session. The client README shows an *illustrative* entry, but a
copy-paste only works if the agent happens to launch from the shell root. This
helper prints a **resolved** entry — the current interpreter plus an explicit
import root — so ``installer.launcher`` resolves correctly no matter where the
agent starts. The fixed managed root is selected only after
its launcher protocol is activated; legacy installs keep using their source.

Transport-only, like the rest of the shell: it emits a launch command for the
local shim. It bakes in no server endpoint, token, or secret — those live only
in the per-device ``config.json`` the shim reads at runtime.

Usage (CLI):

    python3 -m installer.mcp_config              # Claude Code: print the mcpServers JSON entry
    python3 -m installer.mcp_config --codex      # Codex: print the ~/.codex/config.toml block
    python3 -m installer.mcp_config --name de    # use a custom MCP server name

Without ``--write`` this prints a paste-ready entry. With ``--write`` it merges
the entry into the selected agent config, preserving unrelated settings and
creating a backup before replacing an existing file.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import ntpath
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from installer import client_host_ownership, config, cursor_version, managed_install
from installer.client_hosts import AgentHostSpec, ClientSpec
from installer.client_hosts import launchers as client_host_launchers
from installer.client_hosts.contract import (
    SUPPORTED_DOCTOR_CAPABILITIES as _SUPPORTED_DOCTOR_CAPABILITIES,
    validate_host_specs as _validate_host_specs,
)
from installer.client_hosts.renderers import (
    RendererRequest,
    parse_server_collection,
    render_entry as _render_registered_entry,
    renderer_collection_key,
    renderer_writer,
)
from installer.client_hosts.registry import (
    CLIENTS,
    CLIENT_SPECS as REGISTERED_CLIENT_SPECS,
)
from installer.config import ShellError

DEFAULT_SERVER_NAME = "decision-engine"
CLIENT_HOST_ENV = "DE_MCP_CLIENT_HOST"
MAX_AGENT_CONFIG_BYTES = 4 * 1024 * 1024
# Ownership/security boundary from the Cursor master plan §2.4. Revisit this
# exact set whenever a host adds a field that can affect process launch.
_MCP_MANAGED_ENTRY_FIELDS = client_host_ownership.MANAGED_ENTRY_FIELDS_V1
_DEV_ROOT_DEFAULT = object()  # sentinel: bare --dev-root (no path) means "this source checkout"
_CURSOR_VERSION_UNCHECKED = object()

# Mutable compatibility/test seam for legacy callers. Production code treats
# this copy as read-only; the authoritative source is import-validated and
# immutable in installer.client_hosts.registry.
CLIENT_SPECS = dict(REGISTERED_CLIENT_SPECS)

@dataclass(frozen=True)
class HostIdentity:
    status: str
    declared_host: Optional[str]
    observed_host: Optional[str]
    diagnostic: Optional[str]
    optional_features_enabled: bool


@dataclass(frozen=True)
class CursorWorkspaceShadow:
    global_source: str
    project_source: str
    same_name_conflict: bool


@dataclass(frozen=True)
class CursorWorkspaceSkillShadow:
    cursor_skills: Tuple[str, ...]
    agents_skills: Tuple[str, ...]


@dataclass(frozen=True)
class CursorEntryWritePlan:
    path: Path
    server_name: str
    existed: bool
    pre_mtime_ns: Optional[int]
    pre_file_sha256: Optional[str]
    pre_bytes: bytes
    post_file_sha256: str
    pre_managed_entry: Optional[Dict[str, Any]]
    post_managed_entry: Dict[str, Any]
    target_entry: Dict[str, Any]
    rendered: str
    action: str
    warning: Optional[str]


@dataclass(frozen=True)
class ClientWriteBatchResult:
    written: Tuple[Dict[str, Any], ...]
    failed: Tuple[Tuple[str, str], ...]
    notices: Tuple[str, ...] = ()


def validate_host_specs(
    specs: Optional[Dict[str, AgentHostSpec]] = None,
) -> None:
    """Compatibility wrapper around the modular contract validator."""

    _validate_host_specs(CLIENT_SPECS if specs is None else specs)


validate_host_specs()


def _tomllib():
    """Return the stdlib TOML reader or the bundled legacy fallback."""
    try:
        import tomllib
    except ModuleNotFoundError:
        from installer._vendor import tomli as tomllib
    return tomllib

# Codex enforces a per-tool timeout (`tool_timeout_sec`, default 60s). `open_ge` deliberately waits
# for a terminal server status for up to ten minutes, so keep one minute of host-side headroom for
# submit/fetch/spawn and transport overhead. This also covers the shorter `db_board_result` poll.
_CODEX_TOOL_TIMEOUT_SEC = 11 * 60
# The managed launcher may spend up to 60s on bounded update work plus a 5s
# leader-successor grace window before it enters the MCP stdio loop. Codex's
# host default is 10s, so registrations must carry explicit startup headroom.
_CODEX_STARTUP_TIMEOUT_SEC = 120


def shell_root() -> Path:
    """The current source/package root, used only for explicit developer mode."""

    return Path(__file__).resolve().parent.parent


def managed_root() -> Path:
    """Return the fixed product-managed root."""

    return config.managed_component_root("decision-engine")


def registration_root() -> Path:
    """The activated managed root — the only automatic choice left. Anything
    else must be explicit.

    Legacy ``install.sh`` copies the client body but not the Python ``installer``
    package or ``.git`` checkout into the fixed root.  Pointing an Agent there
    before the signed bootstrap/activation has completed would therefore make
    the MCP command unimportable.  Activation passes its canonical root
    explicitly; this automatic choice mainly keeps later ``mcp_config`` calls
    safe and idempotent.

    This used to fall back to ``shell_root()`` (wherever this source checkout
    physically sits) whenever no managed install was activated yet — which
    meant every fresh/pre-activation install silently pointed the Agent at an
    arbitrary source tree instead of failing loudly. A developer checkout must
    now request that explicitly via ``--dev-root`` (CLI) / ``dev_root=``
    (``render_entry``/``render_codex_entry``/``write_entry``), which renders a
    launcher invocation carrying ``--dev-root`` — the flag ``installer.launcher``
    already uses to run without any managed-update machinery at all.

    Only ``ShellError`` (the family every "not ready yet" signal below raises,
    including a symlinked path component) is treated as "not installed" and
    folded into the refusal message; an unexpected ``OSError``/``ValueError``
    (e.g. a transient EMFILE or an NFS hiccup) propagates instead of being
    silently swallowed into a misleading "not installed" refusal.
    """

    from installer import managed_install, update_coordination, update_transaction

    root = managed_root()
    try:
        managed_install._reject_link_components(root)
        protocol = root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
        launcher = root / "installer" / "launcher.py"
        if (
            protocol.is_file()
            and launcher.is_file()
            and not update_coordination._is_link_like(protocol)
            and not update_coordination._is_link_like(launcher)
        ):
            update_transaction._require_protocol_ready(root)
            return root
    except ShellError:
        pass
    raise ShellError(
        "no activated managed Decision Engine install found at %s — install first "
        "(see AI_SETUP.md), or pass --dev-root for an explicit developer checkout" % root
    )


def _launcher_args(dev_root: Optional[Path]) -> List[str]:
    if dev_root is None:
        return ["-m", "installer.launcher"]
    return ["-m", "installer.launcher", "--dev-root", str(dev_root)]


_CWD_INDEPENDENT_BOOTSTRAP = client_host_launchers.CWD_INDEPENDENT_BOOTSTRAP


def _cwd_independent_launcher_args(
    root: str, dev_root: Optional[Path]
) -> List[str]:
    """Import the launcher from ``root`` even when a host cannot pin ``cwd``.

    ``python -m`` searches the host's working directory before ``PYTHONPATH``.
    Cursor launches global MCP servers from the open workspace, so an
    untrusted workspace could otherwise shadow ``installer.launcher``. Passing
    the root as an argv value (not interpolated code) and replacing
    ``sys.path[0]`` before import keeps system/user site packages available
    while removing that workspace-precedence path.
    """

    mode = "--dev-root" if dev_root is not None else "--managed-root"
    return ["-c", _CWD_INDEPENDENT_BOOTSTRAP, root, mode, root]


def pre_activation_root() -> Path:
    """Return the fixed managed checkout while activation is still pending.

    This is intentionally narrower than ``registration_root``: the only caller
    is the first-install path, and it may publish a transport entry before the
    device has credentials. The launcher itself will select the unactivated
    Lite surface at runtime. Never accept a caller-supplied path here.
    """

    root = managed_root()
    try:
        managed_install._require_fixed_managed_root(root)
        canonical = managed_install.canonical_managed_root(root)
    except (managed_install.ManagedInstallError, OSError) as exc:
        raise ShellError("pre-activation managed install is incomplete") from exc
    required = (
        canonical / "config.json",
        canonical / "installer" / "launcher.py",
        canonical / "installer" / "mcp_bootstrap.py",
    )
    if any(
        not path.is_file() or managed_install._is_link_like(path)
        for path in required
    ):
        raise ShellError("pre-activation managed install is incomplete")
    return canonical


def _render_client_entry(
    client: str,
    *,
    python: Optional[str] = None,
    cwd: Optional[Path] = None,
    dev_root: Optional[Path] = None,
    startup_timeout_sec: int = _CODEX_STARTUP_TIMEOUT_SEC,
    allow_unactivated: bool = False,
    tool_timeout_sec: int = _CODEX_TOOL_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """Render one MCP entry through the host's registered renderer adapter."""

    spec = CLIENT_SPECS.get(client)
    if spec is None:
        raise ShellError("unknown client %r" % client)
    if dev_root is not None and cwd is not None:
        raise ShellError("pass either cwd= or dev_root=, not both")
    if allow_unactivated and (dev_root is not None or cwd is not None):
        raise ShellError(
            "allow_unactivated cannot be combined with cwd= or dev_root="
        )
    if allow_unactivated:
        cwd = pre_activation_root()
    root = str(
        dev_root
        if dev_root is not None
        else (cwd if cwd is not None else registration_root())
    )
    launch = client_host_launchers.resolve_launch(
        spec.launch_policy,
        client_host_launchers.LaunchRequest(
            command=python or sys.executable,
            launcher_args=tuple(_launcher_args(dev_root)),
            cwd_independent_args=tuple(
                _cwd_independent_launcher_args(root, dev_root)
            ),
            current_interpreter=sys.executable,
            python_version=(sys.version_info.major, sys.version_info.minor),
            platform=sys.platform,
        ),
    )
    request = RendererRequest(
        command=launch.command,
        root=root,
        launcher_args=launch.launcher_args,
        cwd_independent_args=launch.cwd_independent_args,
        environment={CLIENT_HOST_ENV: spec.host_family},
        transport=spec.transport,
        json_include_type=spec.json_include_type,
        json_include_cwd=spec.json_include_cwd,
        startup_timeout_sec=startup_timeout_sec,
        tool_timeout_sec=tool_timeout_sec,
    )
    return _render_registered_entry(spec.config_renderer, request)


def render_entry(
    server_name: str = DEFAULT_SERVER_NAME,
    *,
    python: Optional[str] = None,
    cwd: Optional[Path] = None,
    client: str = "claude-code",
    dev_root: Optional[Path] = None,
    allow_unactivated: bool = False,
) -> Dict[str, Any]:
    """Build the ``{"mcpServers": {...}}`` registration dict for the shim.

    ``env.PYTHONPATH`` is set to the managed root ALONGSIDE ``cwd``: ``installer.launcher`` is
    launched as ``-m installer.launcher`` and so needs the managed root importable, but not every
    agent honours ``cwd`` — a verified Claude **Desktop** entry carries only command/args, no
    cwd. PYTHONPATH makes the package import resolve regardless of the working directory, so the
    same entry works in Claude Code (honours cwd) and Claude Desktop (may not). ``type: stdio``
    is explicit for those Claude clients. A JSON host that does not support ``cwd`` uses a
    fixed argv-only bootstrap that replaces Python's workspace import slot with this root before
    importing the launcher; paths are not interpolated into executable code.

    ``dev_root``: an explicit developer checkout. Renders ``--dev-root <path>`` in ``args`` (the
    flag ``installer.launcher`` already uses to run with no marker/network/updater/lease at all)
    and uses that same path as ``cwd``/``PYTHONPATH`` — this is the only supported way to point an
    Agent at a source checkout instead of the activated managed root; passing both ``cwd`` and
    ``dev_root`` is a caller error (which one would win is not obvious, so this refuses instead
    of guessing).
    """
    spec = CLIENT_SPECS.get(client)
    if (
        spec is None
        or renderer_collection_key(spec.config_renderer) != "mcpServers"
    ):
        raise ShellError("client %r does not use the JSON MCP format" % client)
    entry = _render_client_entry(
        client,
        python=python,
        cwd=cwd,
        dev_root=dev_root,
        allow_unactivated=allow_unactivated,
    )
    return {"mcpServers": {server_name: entry}}


def render(server_name: str = DEFAULT_SERVER_NAME, **kw: Any) -> str:
    return json.dumps(render_entry(server_name, **kw), indent=2, sort_keys=True)


def render_codex_entry(
    server_name: str = DEFAULT_SERVER_NAME,
    *,
    python: Optional[str] = None,
    cwd: Optional[Path] = None,
    dev_root: Optional[Path] = None,
    startup_timeout_sec: int = _CODEX_STARTUP_TIMEOUT_SEC,
    allow_unactivated: bool = False,
    tool_timeout_sec: int = _CODEX_TOOL_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """The Codex ``[mcp_servers.<name>]`` registration for the shim, as a plain dict.

    Same launch triple as the Claude entry (interpreter + ``-m installer.launcher`` + pinned
    ``cwd``), plus explicit startup and tool timeout headroom (design §2 C1). Baked-in
    no endpoint/token/secret — those stay in the per-device ``config.json`` the shim reads.

    ``dev_root`` behaves exactly as in ``render_entry`` — see there."""
    spec = CLIENT_SPECS.get("codex")
    if (
        spec is None
        or renderer_collection_key(spec.config_renderer) != "mcp_servers"
    ):
        raise ShellError("Codex registry does not use the TOML MCP renderer")
    return _render_client_entry(
        "codex",
        python=python,
        cwd=cwd,
        dev_root=dev_root,
        startup_timeout_sec=startup_timeout_sec,
        allow_unactivated=allow_unactivated,
        tool_timeout_sec=tool_timeout_sec,
    )


_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_value(value: Any) -> str:
    """Emit a single TOML scalar/array value for the fixed shapes this file produces. Strings /
    string-arrays reuse ``json.dumps(..., ensure_ascii=False)`` — TOML basic strings and JSON strings
    share the double-quote + backslash-escape grammar, so a path with spaces / backslashes is quoted
    safely; ``ensure_ascii=False`` keeps non-ASCII literal (a JSON `\\uXXXX` surrogate pair is invalid
    in a TOML basic string, so a non-BMP char in a path must NOT be \\u-escaped)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            "%s = %s" % (_toml_key(str(k)), _toml_value(v))
            for k, v in value.items()
        ) + " }"
    return json.dumps(str(value), ensure_ascii=False)   # basic string, JSON-compatible escaping


def _toml_key(name: str) -> str:
    """A TOML table-path key: a bare key (``[A-Za-z0-9_-]``) verbatim, else a quoted basic-string key.
    A raw interpolation would make ``--name 'my de'`` invalid TOML and ``--name a.b`` a SILENTLY nested
    table (`a` → `b`) rather than a server named `a.b` (audit 5c44d3a4 convergent 4/4)."""
    return name if _TOML_BARE_KEY.match(name or "") else _toml_value(name)


def render_codex_toml(server_name: str = DEFAULT_SERVER_NAME, **kw: Any) -> str:
    """The ready-to-paste ``~/.codex/config.toml`` block registering the shim under
    ``[mcp_servers.<server_name>]``. Print-to-paste (like the Claude entry) — this NEVER writes
    the user's config file; the user pastes it into ``~/.codex/config.toml`` themselves."""
    entry = render_codex_entry(server_name, **kw)
    return _render_toml_entry_block(server_name, entry)


def _render_toml_entry_block(
    server_name: str,
    entry: Dict[str, Any],
) -> str:
    """Serialize one already-rendered TOML MCP entry in stable key order."""

    lines = ["[mcp_servers.%s]" % _toml_key(server_name)]
    # stable key order so the emitted block is deterministic.
    for key in (
        "command",
        "args",
        "cwd",
        "env",
        "startup_timeout_sec",
        "tool_timeout_sec",
    ):
        lines.append("%s = %s" % (key, _toml_value(entry[key])))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# --write: merge the shim registration INTO the agent's own config file
# (idempotent, backup-first, merge-not-clobber). This is what the print-to-paste
# path never did — the gap that leaves a customer with no `decision-engine` tools.
# ---------------------------------------------------------------------------

def agent_config_path(client: str) -> Path:
    """The config file to merge into for ``client``. Env-overridable (tests point these at a
    temp HOME); Claude Desktop resolves per-OS to the app's support dir."""
    spec = CLIENT_SPECS.get(client)
    if spec is None:
        raise ShellError(
            "unknown client %r (expected one of %s)"
            % (client, ", ".join(CLIENT_SPECS))
        )
    path_resolver = spec.config_path or spec.default_path
    return Path(os.getenv(spec.config_env) or path_resolver()).expanduser()


def _client_present(client: str, path: Path) -> bool:
    """A conservative "is this agent installed" signal. For claude-code the config's parent is
    ``$HOME`` (always present), so the parent-dir heuristic would fire on EVERY machine and make
    bare ``--write`` pollute ``~/.claude.json`` — audit 6c6ba7d8 convergent (claude/grok/gemini).
    So claude-code needs its config file OR the ``~/.claude`` dir; the other clients keep the
    parent-dir signal (their parents are app-specific: ``~/.codex``, the Claude support dir)."""
    spec = CLIENT_SPECS[client]
    if spec.detection == "installation-probe":
        present = False
    elif spec.detection == "file-or-sibling-dir":
        # sibling of the config file (``~/.claude.json`` ↔ ``~/.claude/``) so it tracks the
        # (test-)overridden path rather than the real $HOME.
        present = path.exists() or path.with_name(".claude").is_dir()
    else:
        present = path.exists() or path.parent.exists()
    if present or spec.installation_probe is None:
        return present
    try:
        return bool(spec.installation_probe())
    except (OSError, ShellError):
        return False


def client_present(client: str) -> bool:
    """Public form of the presence probe for one registry client (unknown client raises)."""
    return _client_present(client, agent_config_path(client))


def detect_clients() -> List[str]:
    """Which registry clients look present on this machine, in registry order."""
    return [client for client in CLIENT_SPECS if client_present(client)]


def clients_with_doctor_capability(
    capability: str,
    *,
    detected_only: bool = False,
) -> Tuple[str, ...]:
    """Return registry-ordered clients that opt into one Doctor probe."""

    if capability not in _SUPPORTED_DOCTOR_CAPABILITIES:
        raise ShellError("unsupported Doctor capability")
    detected = frozenset(detect_clients()) if detected_only else None
    return tuple(
        client
        for client, spec in CLIENT_SPECS.items()
        if capability in spec.doctor_capabilities
        and (detected is None or client in detected)
    )


def _cursor_skills_in_use() -> bool:
    """Compatibility seam retained for callers that patch the legacy probe."""

    configured = os.getenv("CURSOR_SKILLS_DIR")
    if configured and configured.strip():
        return True
    return _client_present("cursor", agent_config_path("cursor"))


def _cursor_skills_configured() -> bool:
    """Compatibility seam retained for callers that patch the legacy probe."""

    try:
        return read_entry("cursor") is not None
    except ShellError:
        return False


def active_skill_routes(
    *, for_doctor: bool = False, setup_only: bool = False
) -> Dict[str, Tuple[Path, FrozenSet[str]]]:
    """Return active per-host skill destinations in registry order.

    Explicit install/setup uses host presence. Doctor checks Cursor only after
    its DE MCP entry exists, so a normal update/diagnostic never onboards an
    otherwise unrelated Cursor installation.
    """

    routes: Dict[str, Tuple[Path, FrozenSet[str]]] = {}
    for spec in CLIENT_SPECS.values():
        if setup_only and not spec.repair_skills_on_setup:
            continue
        if for_doctor and "skills" not in spec.doctor_capabilities:
            continue
        predicate = spec.skills_check_in_use if for_doctor else spec.skills_in_use
        skills_path = spec.skills_global_path
        if (
            skills_path is None
            or spec.skill_route_name is None
            or predicate is None
            or not predicate()
        ):
            continue
        if spec.skill_route_name in routes:
            raise ShellError("duplicate skill route name %r" % spec.skill_route_name)
        routes[spec.skill_route_name] = (
            skills_path(),
            spec.excluded_skills,
        )
    return routes


def managed_skill_route_names() -> FrozenSet[str]:
    """Routes whose Doctor check must resolve to the managed DE payload."""

    return frozenset(
        spec.skill_route_name
        for spec in CLIENT_SPECS.values()
        if spec.skill_route_name is not None
        and spec.skills_require_managed_target
    )


def normalize_client_host(value: Any) -> Optional[str]:
    """Map a client name/registration marker to one unambiguous host family."""

    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower()
    host_families = frozenset(spec.host_family for spec in CLIENT_SPECS.values())
    if normalized in host_families:
        # The installer-authored registration marker is already the canonical,
        # unambiguous host id.  Pattern matching is only for external client names;
        # shared aliases such as ``Trae`` must continue to fail closed.
        return normalized
    matches = {
        spec.host_family
        for spec in CLIENT_SPECS.values()
        if any(
            re.search(pattern, value, re.IGNORECASE)
            for pattern in spec.client_name_patterns
        )
    }
    return next(iter(matches)) if len(matches) == 1 else None


def normalize_observed_client_info(value: Any) -> Optional[str]:
    """Resolve one exact configured clientInfo alias after trim/lowercase."""

    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower()
    matches = {
        spec.host_family
        for spec in CLIENT_SPECS.values()
        if normalized in spec.observed_client_aliases
    }
    return next(iter(matches)) if len(matches) == 1 else None


_ADAPTER_CAPABILITY_FIELDS = (
    "local_display_tools",
    "popup_followup",
    "audit_stop_panel",
    "runtime_display_policy",
    "popup_api_profile",
    "require_observed_identity",
    "launcher_capabilities",
    "optional_features",
)


def _adapter_capability_signature(spec: AgentHostSpec) -> Tuple[Any, ...]:
    return tuple(getattr(spec, field) for field in _ADAPTER_CAPABILITY_FIELDS)


def resolve_host_identity(
    declared_host: Any, initialize_params: Any
) -> HostIdentity:
    """Compare explicit registration metadata with bounded clientInfo evidence."""

    host_families = frozenset(spec.host_family for spec in CLIENT_SPECS.values())
    declared = (
        declared_host.strip().lower()
        if isinstance(declared_host, str)
        and declared_host.strip().lower() in host_families
        else None
    )
    if not isinstance(initialize_params, dict):
        return HostIdentity("malformed", declared, None, None, False)
    if "clientInfo" not in initialize_params:
        return HostIdentity("missing", declared, None, None, False)
    info = initialize_params.get("clientInfo")
    if (
        not isinstance(info, dict)
        or not isinstance(info.get("name"), str)
        or not info["name"].strip()
    ):
        return HostIdentity("malformed", declared, None, None, False)
    normalized_name = info["name"].strip().lower()
    matching_specs = tuple(
        spec
        for spec in CLIENT_SPECS.values()
        if normalized_name in spec.observed_client_aliases
    )
    observed_families = frozenset(spec.host_family for spec in matching_specs)
    observed = (
        next(iter(observed_families))
        if len(observed_families) == 1
        else None
    )
    if declared is None or not matching_specs:
        return HostIdentity("unknown", declared, observed, None, False)
    if declared not in observed_families:
        if observed is None:
            return HostIdentity("unknown", declared, None, None, False)
        return HostIdentity(
            "conflicting",
            declared,
            observed,
            "host_identity_conflict",
            False,
        )
    if len(observed_families) > 1:
        signatures = {
            _adapter_capability_signature(spec) for spec in matching_specs
        }
        if len(signatures) != 1:
            return HostIdentity("unknown", declared, None, None, False)
    return HostIdentity("matched", declared, declared, None, True)


def host_supports_local_display(host_family: Optional[str]) -> bool:
    """Local display is allowlisted; unknown/unidentified hosts fail closed."""

    if not host_family:
        return False
    return any(
        spec.host_family == host_family and spec.local_display_tools
        for spec in CLIENT_SPECS.values()
    )


def host_adapter(host_family: Optional[str]) -> Optional[AgentHostSpec]:
    """Return one internally consistent adapter for a normalized host family."""
    if not host_family:
        return None
    matches = [
        spec for spec in CLIENT_SPECS.values() if spec.host_family == host_family
    ]
    if not matches:
        return None
    expected = _adapter_capability_signature(matches[0])
    if any(
        _adapter_capability_signature(spec) != expected
        for spec in matches[1:]
    ):
        raise ShellError("conflicting host adapter metadata for %s" % host_family)
    return matches[0]


def read_entry(
    client: str, server_name: str = DEFAULT_SERVER_NAME
) -> Optional[Dict[str, Any]]:
    """Read one configured MCP entry without exposing sibling config values."""

    spec = CLIENT_SPECS.get(client)
    if spec is None:
        raise ShellError("unknown client %r" % client)
    path = agent_config_path(client)
    if not path.exists():
        return None
    invalid = False
    try:
        with path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_AGENT_CONFIG_BYTES:
                raise ShellError("refusing to parse oversized Agent configuration %s" % path)
            payload = handle.read(MAX_AGENT_CONFIG_BYTES + 1)
        if len(payload) > MAX_AGENT_CONFIG_BYTES:
            raise ShellError("refusing to parse oversized Agent configuration %s" % path)
        text = payload.decode("utf-8")
        servers = parse_server_collection(spec.config_renderer, text)
    except ShellError:
        raise
    except (OSError, UnicodeError, ValueError):
        invalid = True
    if invalid:
        raise ShellError("Agent configuration is unreadable or invalid: %s" % path)
    if servers is None:
        return None
    if not isinstance(servers, dict):
        raise ShellError("Agent MCP server collection is not a table or object")
    entry = servers.get(server_name)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ShellError("Decision Engine MCP entry is not a table or object")
    return entry


def _cursor_workspace_entry(
    workspace: Path,
    server_name: str,
) -> Optional[Dict[str, Any]]:
    """Read one explicit workspace's Cursor entry without returning file data."""

    try:
        root = _resolved_workspace_root(workspace)
        cursor_dir = root / ".cursor"
        try:
            cursor_before = os.lstat(cursor_dir)
        except FileNotFoundError:
            return None
        if _is_link_like(cursor_dir) or not stat.S_ISDIR(cursor_before.st_mode):
            raise ShellError("Cursor workspace metadata directory is invalid")

        path = cursor_dir / "mcp.json"
        try:
            path_before = os.lstat(path)
        except FileNotFoundError:
            return None
        if _is_link_like(path) or not stat.S_ISREG(path_before.st_mode):
            raise ShellError("Cursor workspace MCP configuration is invalid")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not _same_file_snapshot(path_before, opened)
            ):
                raise ShellError("Cursor workspace MCP configuration changed during read")
            if opened.st_size > MAX_AGENT_CONFIG_BYTES:
                raise ShellError("Cursor workspace MCP configuration is oversized")
            payload = handle.read(MAX_AGENT_CONFIG_BYTES + 1)
        if len(payload) > MAX_AGENT_CONFIG_BYTES:
            raise ShellError("Cursor workspace MCP configuration is oversized")
        cursor_after = os.lstat(cursor_dir)
        path_after = os.lstat(path)
        if (
            _is_link_like(cursor_dir)
            or _is_link_like(path)
            or not _same_file_snapshot(cursor_before, cursor_after)
            or not _same_file_snapshot(opened, path_after)
        ):
            raise ShellError("Cursor workspace MCP configuration changed during read")
        loaded = json.loads(payload.decode("utf-8")) if payload.strip() else {}
    except ShellError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ShellError("Cursor workspace MCP configuration is unreadable or invalid") from exc
    if not isinstance(loaded, dict):
        raise ShellError("Cursor workspace MCP configuration is unreadable or invalid")
    if "mcpServers" not in loaded:
        return None
    servers = loaded["mcpServers"]
    if not isinstance(servers, dict):
        raise ShellError("Cursor workspace MCP server collection is invalid")
    if server_name not in servers:
        return None
    entry = servers[server_name]
    if not isinstance(entry, dict):
        raise ShellError("Cursor workspace Decision Engine entry is invalid")
    return entry


def _resolved_workspace_root(workspace: Path) -> Path:
    """Resolve one explicit existing workspace without echoing its path."""

    try:
        root = Path(workspace).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ShellError("Cursor workspace path is missing or invalid")
        return root
    except ShellError:
        raise
    except OSError as exc:
        raise ShellError("Cursor workspace path is missing or invalid") from exc


def _is_link_like(path: Path) -> bool:
    """Recognize symlinks and Windows reparse points without following them."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return _stat_is_link_like(info)


def _stat_is_link_like(info: os.stat_result) -> bool:
    """Recognize a link/reparse point from an already captured snapshot."""

    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare identity and mutation-sensitive fields around a bounded read."""

    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
        left.st_size,
        getattr(left, "st_mtime_ns", None),
        getattr(left, "st_file_attributes", 0),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
        right.st_size,
        getattr(right, "st_mtime_ns", None),
        getattr(right, "st_file_attributes", 0),
    )


def _validated_skill_names(skill_names: Tuple[str, ...]) -> Tuple[str, ...]:
    """Validate a bounded list of single path components before workspace access."""

    if not isinstance(skill_names, (tuple, list)) or len(skill_names) > 64:
        raise ShellError("Cursor workspace skill-name allowlist is invalid")
    validated: List[str] = []
    for name in skill_names:
        if (
            not isinstance(name, str)
            or len(name) > 128
            or name in ("", ".", "..")
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None
            or Path(name).name != name
        ):
            raise ShellError("Cursor workspace skill-name allowlist is invalid")
        if name not in validated:
            validated.append(name)
    return tuple(validated)


def _workspace_skill_names(
    root: Path,
    metadata_name: str,
    skill_names: Tuple[str, ...],
) -> Tuple[str, ...]:
    """Check fixed known children in one project skill root without enumeration."""

    if os.name == "nt":
        return _workspace_skill_names_windows(root, metadata_name, skill_names)
    return _workspace_skill_names_dir_fd(root, metadata_name, skill_names)


def _workspace_skill_names_windows(
    root: Path,
    metadata_name: str,
    skill_names: Tuple[str, ...],
) -> Tuple[str, ...]:
    """Pin every Windows ancestor so child metadata cannot cross a junction race."""

    from installer import windows_security

    metadata = root / metadata_name
    try:
        with windows_security.PinnedWindowsDirectory(root) as root_pin:
            try:
                metadata_info = os.lstat(metadata)
            except FileNotFoundError:
                return ()
            if _stat_is_link_like(metadata_info) or not stat.S_ISDIR(
                metadata_info.st_mode
            ):
                raise ShellError("Cursor workspace skill scope is unreadable or invalid")
            with windows_security.PinnedWindowsDirectory(metadata) as metadata_pin:
                skills = metadata / "skills"
                try:
                    skills_info = os.lstat(skills)
                except FileNotFoundError:
                    return ()
                if _stat_is_link_like(skills_info) or not stat.S_ISDIR(
                    skills_info.st_mode
                ):
                    raise ShellError(
                        "Cursor workspace skill scope is unreadable or invalid"
                    )
                with windows_security.PinnedWindowsDirectory(skills) as skills_pin:
                    found = _skill_leaf_names_by_path(skills, skill_names)
                    try:
                        skills_pin.validate()
                        metadata_pin.validate()
                        root_pin.validate()
                    except windows_security.WindowsSecurityError as exc:
                        raise ShellError(
                            "Cursor workspace skill scope changed during diagnosis"
                        ) from exc
                    return found
    except ShellError:
        raise
    except (OSError, windows_security.WindowsSecurityError) as exc:
        raise ShellError("Cursor workspace skill scope is unreadable or invalid") from exc


def _skill_leaf_names_by_path(
    skills: Path,
    skill_names: Tuple[str, ...],
) -> Tuple[str, ...]:
    found: List[str] = []
    for name in skill_names:
        try:
            info = os.lstat(skills / name)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode) or _stat_is_link_like(info):
            found.append(name)
    return tuple(found)


def _workspace_skill_names_dir_fd(
    root: Path,
    metadata_name: str,
    skill_names: Tuple[str, ...],
) -> Tuple[str, ...]:
    """Use no-follow directory descriptors on non-Windows platforms."""

    supported = (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
    )
    if not supported:
        raise ShellError("Cursor workspace skill scope cannot be inspected safely")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: List[int] = []
    try:
        root_fd = os.open(root, flags)
        descriptors.append(root_fd)
        try:
            metadata_fd = os.open(metadata_name, flags, dir_fd=root_fd)
        except FileNotFoundError:
            return ()
        descriptors.append(metadata_fd)
        try:
            skills_fd = os.open("skills", flags, dir_fd=metadata_fd)
        except FileNotFoundError:
            return ()
        descriptors.append(skills_fd)
        found = []
        for name in skill_names:
            try:
                info = os.stat(name, dir_fd=skills_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode) or _stat_is_link_like(info):
                found.append(name)
        return tuple(found)
    except ShellError:
        raise
    except OSError as exc:
        raise ShellError("Cursor workspace skill scope is unreadable or invalid") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def cursor_workspace_skill_shadow(
    workspace: Path,
    skill_names: Tuple[str, ...],
) -> Optional[CursorWorkspaceSkillShadow]:
    """Diagnose fixed project skill names without reading or claiming content."""

    validated = _validated_skill_names(skill_names)
    cursor_spec = CLIENT_SPECS.get("cursor")
    if cursor_spec is None:
        raise ShellError("Cursor project skill metadata is invalid")
    validate_host_specs({"cursor": cursor_spec})
    project_paths = cursor_spec.skills_project_paths
    if len(project_paths) != 2:
        raise ShellError("Cursor project skill metadata must contain exactly two paths")
    metadata_names = tuple(
        PurePosixPath(raw_path).parent.as_posix()
        for raw_path in project_paths
    )
    root = _resolved_workspace_root(workspace)
    cursor_skills = _workspace_skill_names(
        root,
        metadata_names[0],
        validated,
    )
    agents_skills = _workspace_skill_names(
        root,
        metadata_names[1],
        validated,
    )
    if not cursor_skills and not agents_skills:
        return None
    return CursorWorkspaceSkillShadow(
        cursor_skills=cursor_skills,
        agents_skills=agents_skills,
    )


def _entry_source_summary(entry: Optional[Dict[str, Any]]) -> str:
    if entry is None:
        return "absent"
    command = entry.get("command")
    args = entry.get("args")
    if not isinstance(command, str) or not Path(command).is_absolute():
        return "foreign"
    if isinstance(args, list):
        if (
            len(args) == 5
            and args[:2] == ["-c", _CWD_INDEPENDENT_BOOTSTRAP]
            and isinstance(args[2], str)
            and Path(args[2]).is_absolute()
            and args[3] in ("--managed-root", "--dev-root")
            and args[4] == args[2]
        ):
            return "launcher-shape"
        if args == ["-m", "installer.launcher"] or (
            len(args) == 4
            and args[:3] == ["-m", "installer.launcher", "--dev-root"]
            and isinstance(args[3], str)
            and Path(args[3]).is_absolute()
        ):
            return "launcher-shape"
        if args == ["-m", "installer.shim"]:
            return "legacy-shim-shape"
    return "foreign"


def cursor_workspace_shadow(
    workspace: Path,
    server_name: str = DEFAULT_SERVER_NAME,
) -> Optional[CursorWorkspaceShadow]:
    """Diagnose one explicit Cursor project entry without claiming or changing it."""

    project_entry = _cursor_workspace_entry(workspace, server_name)
    if project_entry is None:
        return None
    try:
        global_entry = read_entry("cursor", server_name)
    except ShellError as exc:
        raise ShellError(
            "Cursor user-global MCP configuration is unreadable or invalid"
        ) from exc
    return CursorWorkspaceShadow(
        global_source=_entry_source_summary(global_entry),
        project_source=_entry_source_summary(project_entry),
        # Cursor project scope is read-only and has no ownership record. Any
        # same-name project entry is therefore unowned, even if its bytes
        # happen to match the user-global managed entry.
        same_name_conflict=True,
    )


def dev_root_from_args(args: Any) -> Optional[str]:
    """Extract the ``--dev-root <path>`` value from a launcher ``args`` list, if present.

    Used to make ``entry_status``/doctor dev-mode-aware: an already-registered dev-root entry
    must be compared against what a correct dev-root entry for THAT SAME path looks like, not
    against the managed-root expectation (which would raise or always read as mismatched)."""
    if not isinstance(args, list):
        return None
    for i, item in enumerate(args):
        if item == "--dev-root" and i + 1 < len(args) and isinstance(args[i + 1], str):
            return args[i + 1]
    return None


def expected_entry(
    client: str,
    server_name: str = DEFAULT_SERVER_NAME,
    *,
    python: Optional[str] = None,
    cwd: Optional[Path] = None,
    dev_root: Optional[Path] = None,
    allow_unactivated: bool = False,
) -> Dict[str, Any]:
    spec = CLIENT_SPECS.get(client)
    if spec is None:
        raise ShellError("unknown client %r" % client)
    return _render_client_entry(
        client,
        python=python,
        cwd=cwd,
        dev_root=dev_root,
        allow_unactivated=allow_unactivated,
    )


def entry_status(
    client: str,
    server_name: str = DEFAULT_SERVER_NAME,
    *,
    allow_unactivated: bool = False,
) -> str:
    """Return the diagnostic state for one client registration.

    The common states are ``absent``, ``ready`` and ``stale``. Cursor can also
    return ``same_name_unowned`` or ``same_name_user_modified``. A present but
    invalid ownership record raises ``ShellError`` instead of weakening a
    fail-closed ownership decision into a status string.

    If the currently-registered entry is already a ``--dev-root`` entry, the comparison is made
    against a correct dev-root entry for that SAME path — not the managed-root expectation, which
    would either raise (no managed install activated) or always read as a mismatch. Structural
    comparison against a "desired" entry re-derived from that same path can never by itself catch
    a dev-root that has since been moved or deleted, so that path is independently checked for
    still looking like a real Decision Engine checkout before anything is allowed to read
    ``ready``."""

    actual = read_entry(client, server_name)
    if actual is None:
        return "absent"
    dev_root = dev_root_from_args(actual.get("args"))
    if dev_root is not None and not dev_root:
        return "stale"
    dev_root_path = Path(dev_root) if dev_root is not None else None
    if dev_root_path is not None and not (
        dev_root_path / "installer" / "launcher.py"
    ).is_file():
        return "stale"
    desired = expected_entry(
        client,
        server_name,
        dev_root=dev_root_path,
        allow_unactivated=allow_unactivated and dev_root_path is None,
    )
    if not isinstance(actual.get("command"), str) or not actual["command"].strip():
        return "stale"
    for key in ("command", "type", "args", "cwd"):
        if (key in actual) != (key in desired) or (
            key in desired and actual.get(key) != desired[key]
        ):
            return "stale"
    desired_env = desired.get("env", {})
    actual_env = actual.get("env")
    if not isinstance(actual_env, dict) or actual_env != desired_env:
        return "stale"
    for timeout_key in ("startup_timeout_sec", "tool_timeout_sec"):
        minimum_timeout = desired.get(timeout_key)
        if minimum_timeout is not None:
            actual_timeout = actual.get(timeout_key)
            if not isinstance(actual_timeout, int) or actual_timeout < minimum_timeout:
                return "stale"
    return "ready"


def _backup(path: Path) -> Path:
    """Copy ``path`` to a timestamped sidecar (perms preserved) before we rewrite it."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + ".de-bak." + stamp)
    shutil.copy2(path, bak)
    return bak


def _atomic_write_text(
    path: Path,
    text: str,
    *,
    expect_mtime_ns: Optional[int] = None,
    expect_sha256: Optional[str] = None,
    expect_exists: Optional[bool] = None,
    newline: Optional[str] = None,
) -> None:
    """Write ``text`` to ``path`` atomically, preserving the file's existing mode (a fresh file
    gets 0600 — these configs can hold tokens). Unlike ``config.atomic_write_json`` this does NOT
    chmod ``$HOME`` (the parent of ``~/.claude.json``); it only tightens a parent dir it just
    CREATED to 0700 (a permissive umask must not leave an agent-config dir world-writable —
    audit 6c6ba7d8 gpt f5).

    ``expect_mtime_ns`` and ``expect_sha256``: the file generation is re-checked immediately
    before the replace and the write ABORTS (``ShellError``) when it changed since the caller read it. The
    content digest is required because Windows can preserve the same timestamp across rapid writes — a fail-closed
    guard against clobbering a concurrent edit by a live agent (audit 6c6ba7d8 convergent
    claude/gpt). Not a full lock; it narrows the read→replace race to the sub-millisecond window."""
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed and path.parent != Path.home():
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())

    def _opener(name: str, flags: int) -> int:
        return os.open(name, flags, mode)

    replaced = False
    try:
        with open(
            tmp,
            "w",
            encoding="utf-8",
            newline=newline,
            opener=_opener,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        current_exists = path.exists()
        if expect_exists is not None and current_exists != expect_exists:
            raise ShellError(
                "%s appeared or disappeared while being updated; nothing was written" % path
            )
        if expect_mtime_ns is not None or expect_sha256 is not None:
            try:
                current_mtime = os.stat(path).st_mtime_ns
                current_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                current_mtime = None
                current_digest = None
            except OSError as exc:
                raise ShellError(
                    "%s could not be re-read safely; nothing was written" % path
                ) from exc
            mtime_changed = (expect_mtime_ns is not None
                             and current_mtime != expect_mtime_ns)
            digest_changed = (expect_sha256 is not None
                              and current_digest != expect_sha256)
            if mtime_changed or digest_changed:
                raise ShellError("%s changed while being updated (a running agent may have rewritten "
                                 "it); nothing was written — quit the agent and retry." % path)
        os.replace(tmp, path)
        replaced = True
    finally:
        if not replaced:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def _publish_bytes_no_replace(
    path: Path,
    raw: bytes,
) -> None:
    """Publish exact bytes only while the destination remains absent."""

    if not isinstance(raw, bytes) or len(raw) > MAX_AGENT_CONFIG_BYTES:
        raise ShellError("Cursor configuration preimage is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    temp = path.with_name("%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd: Optional[int] = None
    replaced = False
    try:
        fd = os.open(temp, flags, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        from installer import windows_security

        windows_security.move_write_through(
            temp,
            path,
            replace_existing=False,
        )
        if os.name != "nt":
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        replaced = True
    finally:
        if fd is not None:
            os.close(fd)
        if not replaced:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def _managed_entry_projection(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    return {
        field: entry[field]
        for field in _MCP_MANAGED_ENTRY_FIELDS
        if field in entry
    }


def _cursor_write_version_gate() -> Optional[str]:
    """Enforce the candidate floor and return a fixed warning for unknown evidence."""

    try:
        evidence = cursor_version.collect_cursor_version_evidence()
        assessment = cursor_version.assess_cursor_version(evidence)
    except Exception as exc:  # aqg: top-level boundary
        raise ShellError(
            "cursor_version_probe_failed: Cursor version could not be checked; "
            "nothing was written"
        ) from exc
    if assessment.state == "unsupported_cursor_version":
        raise ShellError(
            "unsupported_cursor_version: Cursor is below the candidate minimum; "
            "nothing was written"
        )
    if assessment.state == "candidate_standard_version_floor_met":
        return None
    return "cursor_version_unknown"


_CONFIG_WRITE_GUARDS = {
    "cursor-version-v1": lambda: _cursor_write_version_gate(),
}


def _config_write_warning(spec: AgentHostSpec) -> Optional[str]:
    if spec.config_write_guard_probe is not None:
        return spec.config_write_guard_probe()
    if spec.config_write_guard is not None:
        guard = _CONFIG_WRITE_GUARDS.get(spec.config_write_guard)
        if guard is None:
            raise ShellError("config write guard is unsupported")
        return guard()
    return None


def _authorize_cursor_entry_write(
    actual: Any,
    desired: Dict[str, Any],
    *,
    path: Path,
    server_name: str,
    dev_root: Optional[Path],
    allow_owned_update: bool,
) -> None:
    if actual is None:
        return
    if not isinstance(actual, dict):
        raise ShellError(
            "same_name_unowned: Cursor decision-engine entry is not an object; "
            "nothing was written"
        )
    projections_match = (
        _managed_entry_projection(actual) == _managed_entry_projection(desired)
    )
    if dev_root is not None:
        if projections_match:
            return
        raise ShellError(
            "same_name_unowned: Cursor developer entry does not match the "
            "requested checkout; nothing was written"
        )
    record = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=registration_root(),
        config_path=path,
        server_name=server_name,
    )
    if record is None:
        if projections_match:
            return
        raise ShellError(
            "same_name_unowned: Cursor decision-engine entry has no matching "
            "ownership record; nothing was written"
        )
    actual_hash = client_host_ownership.managed_entry_sha256_v1(actual)
    if actual_hash != record.managed_fields_sha256:
        raise ShellError(
            "same_name_user_modified: Cursor managed fields changed after "
            "ownership was recorded; nothing was written"
        )
    if not projections_match and not allow_owned_update:
        raise ShellError(
            "owned_write_requires_transaction: Cursor managed fields may only "
            "be changed by the transactional repair path; nothing was written"
        )


def _merge_cursor_managed_fields(
    actual: Any,
    desired: Dict[str, Any],
) -> Dict[str, Any]:
    merged = dict(actual) if isinstance(actual, dict) else {}
    for field in _MCP_MANAGED_ENTRY_FIELDS:
        if field in desired:
            if field not in merged or merged[field] != desired[field]:
                merged[field] = desired[field]
        else:
            merged.pop(field, None)
    return merged


def _json_object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ShellError("Cursor configuration contains duplicate JSON fields")
        value[key] = item
    return value


def _read_cursor_config_snapshot(
    path: Path,
) -> Tuple[bool, Optional[int], Optional[str], Dict[str, Any], bytes]:
    try:
        managed_install._reject_link_components(path.parent)
    except managed_install.ManagedInstallError as exc:
        raise ShellError("Cursor configuration path is unsafe") from exc
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return False, None, None, {}, b""
    except OSError as exc:
        raise ShellError("Cursor configuration could not be inspected safely") from exc
    if _stat_is_link_like(before) or not stat.S_ISREG(before.st_mode):
        raise ShellError("Cursor configuration must be a regular file")
    if before.st_size > MAX_AGENT_CONFIG_BYTES:
        raise ShellError("Cursor configuration is too large")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ShellError("Cursor configuration could not be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_file_snapshot(before, opened)
            or opened.st_size > MAX_AGENT_CONFIG_BYTES
        ):
            raise ShellError("Cursor configuration changed while being read")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_AGENT_CONFIG_BYTES + 1)
        after = os.fstat(fd)
        try:
            path_after = os.lstat(path)
        except OSError as exc:
            raise ShellError("Cursor configuration changed while being read") from exc
        if (
            len(raw) > MAX_AGENT_CONFIG_BYTES
            or not _same_file_snapshot(opened, after)
            or not _same_file_snapshot(after, path_after)
        ):
            raise ShellError("Cursor configuration changed while being read")
    finally:
        os.close(fd)
    data: Dict[str, Any] = {}
    if raw.strip():
        try:
            loaded = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_json_object_without_duplicates,
            )
        except ShellError:
            raise
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ShellError("Cursor configuration is not valid bounded JSON") from exc
        if not isinstance(loaded, dict):
            raise ShellError("Cursor configuration top level must be an object")
        data = loaded
    digest = hashlib.sha256(raw).hexdigest()
    return True, before.st_mtime_ns, digest, data, raw


def prepare_cursor_entry_write(
    desired: Dict[str, Any],
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    dev_root: Optional[Path] = None,
    version_warning: Any = _CURSOR_VERSION_UNCHECKED,
    allow_owned_update: bool = False,
) -> CursorEntryWritePlan:
    """Prepare an exact CAS plan without mutating Cursor configuration."""

    if not isinstance(desired, dict):
        raise ShellError("Cursor managed entry must be an object")
    path = agent_config_path("cursor")
    existed, mtime_ns, pre_digest, data, raw = _read_cursor_config_snapshot(path)
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise ShellError("Cursor configuration mcpServers must be an object")
    actual = servers.get(server_name)
    _authorize_cursor_entry_write(
        actual,
        desired,
        path=path,
        server_name=server_name,
        dev_root=dev_root,
        allow_owned_update=allow_owned_update,
    )
    warning = (
        _cursor_write_version_gate()
        if version_warning is _CURSOR_VERSION_UNCHECKED
        else version_warning
    )
    target_entry = _merge_cursor_managed_fields(actual, desired)
    action = (
        "unchanged"
        if actual == target_entry
        else ("updated" if server_name in servers else "added")
    )
    if action == "unchanged":
        rendered = raw.decode("utf-8")
        post_digest = pre_digest
    else:
        updated_servers = dict(servers)
        updated_servers[server_name] = target_entry
        updated = dict(data)
        updated["mcpServers"] = updated_servers
        try:
            rendered = json.dumps(
                updated,
                allow_nan=False,
                indent=2,
                ensure_ascii=False,
            ) + "\n"
        except (TypeError, UnicodeError, ValueError) as exc:
            raise ShellError("Cursor configuration cannot be serialized safely") from exc
        rendered_bytes = (
            rendered.replace("\n", os.linesep).encode("utf-8")
            if os.linesep != "\n"
            else rendered.encode("utf-8")
        )
        post_digest = hashlib.sha256(rendered_bytes).hexdigest()
    if post_digest is None:
        raise ShellError("Cursor configuration hash is unavailable")
    return CursorEntryWritePlan(
        path=path,
        server_name=server_name,
        existed=existed,
        pre_mtime_ns=mtime_ns,
        pre_file_sha256=pre_digest,
        pre_bytes=raw,
        post_file_sha256=post_digest,
        pre_managed_entry=_managed_entry_projection(actual),
        post_managed_entry=_managed_entry_projection(target_entry) or {},
        target_entry=target_entry,
        rendered=rendered,
        action=action,
        warning=warning,
    )


def apply_cursor_entry_write(
    plan: CursorEntryWritePlan,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply a previously prepared exact plan with the existing file CAS."""

    if not isinstance(plan, CursorEntryWritePlan):
        raise ShellError("Cursor entry write plan is invalid")
    if plan.action == "unchanged" or dry_run:
        action = plan.action if not dry_run else plan.action + " (dry-run)"
        return {
            "client": "cursor",
            "path": str(plan.path),
            "action": action,
            "backup": None,
            "warning": plan.warning,
            "file_sha256": (
                plan.pre_file_sha256
                if plan.action == "unchanged"
                else plan.post_file_sha256
            ),
        }
    backup = _backup(plan.path) if plan.existed else None
    _atomic_write_text(
        plan.path,
        plan.rendered,
        expect_mtime_ns=plan.pre_mtime_ns,
        expect_sha256=plan.pre_file_sha256,
        expect_exists=plan.existed,
    )
    existed, _mtime, digest, data, _raw = _read_cursor_config_snapshot(plan.path)
    actual = data.get("mcpServers", {}).get(plan.server_name)
    if (
        not existed
        or digest != plan.post_file_sha256
        or actual != plan.target_entry
    ):
        raise ShellError("Cursor configuration post-write verification failed")
    return {
        "client": "cursor",
        "path": str(plan.path),
        "action": plan.action,
        "backup": str(backup) if backup else None,
        "warning": plan.warning,
        "file_sha256": digest,
    }


def cursor_config_file_sha256(path: Optional[Path] = None) -> Optional[str]:
    target = agent_config_path("cursor") if path is None else Path(path)
    existed, _mtime, digest, _data, _raw = _read_cursor_config_snapshot(target)
    return digest if existed else None


def rollback_cursor_initial_entry_preserving_unrelated(
    path: Path,
    *,
    expected_target_entry: Dict[str, Any],
    server_name: str = DEFAULT_SERVER_NAME,
) -> None:
    """Remove the exact initial entry while preserving unrelated JSON edits."""

    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise ShellError(
            "Cursor configuration recovery requires the activation lock"
        )
    if (
        not isinstance(expected_target_entry, dict)
        or _managed_entry_projection(expected_target_entry)
        != expected_target_entry
    ):
        raise ShellError("Cursor configuration rollback target is invalid")
    path = Path(path)
    existed, mtime_ns, digest, data, _raw = _read_cursor_config_snapshot(path)
    if not existed or digest is None:
        raise ShellError(
            "repair_required: Cursor configuration rollback target is missing"
        )
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise ShellError(
            "repair_required: Cursor configuration mcpServers changed"
        )
    actual = servers.get(server_name)
    if actual != expected_target_entry:
        raise ShellError(
            "repair_required: Cursor managed entry contains user changes"
        )
    updated_servers = dict(servers)
    updated_servers.pop(server_name)
    updated = dict(data)
    updated["mcpServers"] = updated_servers
    try:
        rendered = json.dumps(
            updated,
            allow_nan=False,
            indent=2,
            ensure_ascii=False,
        ) + "\n"
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ShellError(
            "Cursor configuration rollback could not be serialized"
        ) from exc
    _atomic_write_text(
        path,
        rendered,
        expect_mtime_ns=mtime_ns,
        expect_sha256=digest,
        expect_exists=True,
    )
    persisted, _mtime, _digest, persisted_data, _raw = (
        _read_cursor_config_snapshot(path)
    )
    persisted_servers = persisted_data.get("mcpServers")
    if (
        not persisted
        or persisted_data != updated
        or not isinstance(persisted_servers, dict)
        or server_name in persisted_servers
    ):
        raise ShellError(
            "Cursor configuration rollback verification failed"
        )


def remove_cursor_owned_fields_preserving_unrelated(
    path: Path,
    *,
    expected_managed_entry: Dict[str, Any],
    server_name: str = DEFAULT_SERVER_NAME,
) -> Optional[str]:
    """Remove only exact owned launch fields, retaining all opaque JSON."""

    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise ShellError("Cursor configuration removal requires the activation lock")
    if (
        not isinstance(expected_managed_entry, dict)
        or _managed_entry_projection(expected_managed_entry)
        != expected_managed_entry
    ):
        raise ShellError("Cursor managed removal target is invalid")
    path = Path(path)
    existed, mtime_ns, digest, data, _raw = _read_cursor_config_snapshot(path)
    if not existed or digest is None:
        raise ShellError("repair_required: Cursor configuration is missing")
    servers = data.get("mcpServers")
    actual = servers.get(server_name) if isinstance(servers, dict) else None
    if (
        not isinstance(servers, dict)
        or not isinstance(actual, dict)
        or _managed_entry_projection(actual) != expected_managed_entry
    ):
        raise ShellError(
            "same_name_user_modified: Cursor managed fields changed; nothing was removed"
        )
    remainder = {
        key: value
        for key, value in actual.items()
        if key not in _MCP_MANAGED_ENTRY_FIELDS
    }
    updated_servers = dict(servers)
    if remainder:
        updated_servers[server_name] = remainder
    else:
        updated_servers.pop(server_name)
    updated = dict(data)
    updated["mcpServers"] = updated_servers
    rendered = json.dumps(
        updated,
        allow_nan=False,
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    _atomic_write_text(
        path,
        rendered,
        expect_mtime_ns=mtime_ns,
        expect_sha256=digest,
        expect_exists=True,
    )
    persisted, _mtime, post_digest, persisted_data, _raw = (
        _read_cursor_config_snapshot(path)
    )
    persisted_servers = persisted_data.get("mcpServers")
    if (
        not persisted
        or post_digest is None
        or not isinstance(persisted_servers, dict)
        or _managed_entry_projection(persisted_servers.get(server_name))
        not in (None, {})
    ):
        raise ShellError("Cursor configuration removal verification failed")
    return post_digest


def restore_cursor_owned_fields_preserving_unrelated(
    path: Path,
    *,
    expected_managed_entry: Dict[str, Any],
    server_name: str = DEFAULT_SERVER_NAME,
) -> str:
    """Restore exact managed launch fields into an absent/opaque-only entry."""

    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise ShellError("Cursor configuration restore requires the activation lock")
    if (
        not isinstance(expected_managed_entry, dict)
        or _managed_entry_projection(expected_managed_entry)
        != expected_managed_entry
    ):
        raise ShellError("Cursor managed restore target is invalid")
    path = Path(path)
    existed, mtime_ns, digest, data, _raw = _read_cursor_config_snapshot(path)
    if not existed or digest is None:
        raise ShellError("repair_required: Cursor configuration is missing")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise ShellError("repair_required: Cursor configuration mcpServers changed")
    actual = servers.get(server_name)
    projection = _managed_entry_projection(actual)
    if projection not in (None, {}):
        raise ShellError(
            "repair_required: Cursor managed entry is neither absent nor removed"
        )
    restored = dict(actual) if isinstance(actual, dict) else {}
    restored.update(expected_managed_entry)
    updated_servers = dict(servers)
    updated_servers[server_name] = restored
    updated = dict(data)
    updated["mcpServers"] = updated_servers
    rendered = json.dumps(
        updated,
        allow_nan=False,
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    _atomic_write_text(
        path,
        rendered,
        expect_mtime_ns=mtime_ns,
        expect_sha256=digest,
        expect_exists=True,
    )
    persisted, _mtime, post_digest, persisted_data, _raw = (
        _read_cursor_config_snapshot(path)
    )
    persisted_entry = persisted_data.get("mcpServers", {}).get(server_name)
    if (
        not persisted
        or post_digest is None
        or _managed_entry_projection(persisted_entry) != expected_managed_entry
    ):
        raise ShellError("Cursor configuration restore verification failed")
    return post_digest


def restore_cursor_config_preimage(
    path: Path,
    *,
    pre_existed: bool,
    pre_bytes: Optional[bytes],
    pre_file_sha256: Optional[str],
    expected_current_sha256: str,
    quarantine_path: Path,
) -> None:
    """Quarantine the journal target, then restore its exact preimage."""

    if type(pre_existed) is not bool:
        raise ShellError("Cursor configuration preimage state is invalid")
    if (
        not isinstance(expected_current_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_current_sha256)
    ):
        raise ShellError("Cursor configuration target hash is invalid")
    if pre_existed:
        if (
            not isinstance(pre_bytes, bytes)
            or not isinstance(pre_file_sha256, str)
            or hashlib.sha256(pre_bytes).hexdigest() != pre_file_sha256
        ):
            raise ShellError("Cursor configuration preimage hash is invalid")
    elif pre_bytes is not None or pre_file_sha256 is not None:
        raise ShellError("absent Cursor configuration has an invalid preimage")
    path = Path(path)
    quarantine = Path(quarantine_path)
    try:
        managed_install._reject_link_components(quarantine.parent)
    except managed_install.ManagedInstallError as exc:
        raise ShellError("Cursor configuration quarantine path is unsafe") from exc
    current_exists, _mtime, current_digest, _data, _raw = (
        _read_cursor_config_snapshot(path)
    )
    quarantine_exists, _q_mtime, quarantine_digest, _q_data, _q_raw = (
        _read_cursor_config_snapshot(quarantine)
    )
    pre_digest = pre_file_sha256 if pre_existed else None
    if quarantine_exists:
        if current_exists:
            if (
                current_digest == pre_digest
                and quarantine_digest == expected_current_sha256
            ):
                try:
                    quarantine.unlink()
                except OSError as exc:
                    raise ShellError(
                        "Cursor configuration quarantine could not be removed"
                    ) from exc
                return
            raise ShellError(
                "repair_required: Cursor configuration rollback has two live files"
            )
    else:
        if current_digest == pre_digest:
            return
        if not current_exists or current_digest != expected_current_sha256:
            raise ShellError(
                "repair_required: Cursor configuration is not the journal target"
            )
        try:
            from installer import windows_security

            windows_security.move_write_through(
                path,
                quarantine,
                replace_existing=False,
            )
        except (OSError, RuntimeError) as exc:
            raise ShellError(
                "Cursor configuration could not be quarantined during recovery"
            ) from exc
        quarantine_exists, _q_mtime, quarantine_digest, _q_data, _q_raw = (
            _read_cursor_config_snapshot(quarantine)
        )
    if not quarantine_exists or quarantine_digest != expected_current_sha256:
        if not (path.exists() or managed_install._is_link_like(path)):
            try:
                from installer import windows_security

                windows_security.move_write_through(
                    quarantine,
                    path,
                    replace_existing=False,
                )
            except (OSError, RuntimeError) as exc:
                raise ShellError(
                    "repair_required: changed Cursor configuration is preserved "
                    "in quarantine"
                ) from exc
        raise ShellError(
            "repair_required: Cursor configuration changed during journal recovery"
        )
    if pre_existed:
        try:
            _publish_bytes_no_replace(path, pre_bytes)
        except (OSError, RuntimeError) as exc:
            raise ShellError(
                "Cursor configuration preimage could not be restored"
            ) from exc
        restored, _mtime, restored_digest, _data, restored_raw = (
            _read_cursor_config_snapshot(path)
        )
        if (
            not restored
            or restored_digest != pre_file_sha256
            or restored_raw != pre_bytes
        ):
            raise ShellError(
                "Cursor configuration preimage verification failed"
            )
    try:
        quarantine.unlink()
        if os.name != "nt":
            directory_fd = os.open(
                quarantine.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise ShellError(
            "Cursor configuration quarantine could not be removed"
        ) from exc
    if cursor_config_file_sha256(path) != pre_file_sha256:
        raise ShellError("Cursor configuration preimage restoration failed")


def _managed_launcher_root(args: Any, cwd: Any) -> Optional[str]:
    if not isinstance(args, list):
        return None
    candidate = list(args)
    if candidate and isinstance(candidate[0], str) and re.fullmatch(
        r"-\d+\.\d+",
        candidate[0],
    ):
        candidate.pop(0)
    if candidate == ["-m", "installer.launcher"]:
        return cwd if isinstance(cwd, str) and cwd else None
    if (
        len(candidate) == 4
        and candidate[:3] == ["-m", "installer.launcher", "--dev-root"]
        and isinstance(cwd, str)
        and candidate[3] == cwd
    ):
        return cwd
    if (
        len(candidate) == 5
        and candidate[0] == "-c"
        and candidate[1] == _CWD_INDEPENDENT_BOOTSTRAP
        and isinstance(candidate[2], str)
        and bool(candidate[2])
        and candidate[3] in {"--dev-root", "--managed-root"}
        and candidate[4] == candidate[2]
        and cwd is None
    ):
        return candidate[2]
    if (
        len(candidate) == 4
        and isinstance(candidate[0], str)
        and isinstance(candidate[1], str)
        and bool(candidate[1])
        and candidate[2] in {"--dev-root", "--managed-root"}
        and candidate[3] == candidate[1]
        and cwd is None
    ):
        root = candidate[1]
        drive, _tail = ntpath.splitdrive(root)
        if drive or root.startswith("\\\\"):
            path_module = ntpath
        elif posixpath.isabs(root):
            path_module = posixpath
        else:
            return None
        configured = path_module.normcase(path_module.normpath(candidate[0]))
        expected = path_module.normcase(
            path_module.normpath(
                path_module.join(root, "installer", "mcp_bootstrap.py")
            )
        )
        if configured == expected:
            return root
    return None


def _managed_launcher_args(args: Any, cwd: Any) -> bool:
    return _managed_launcher_root(args, cwd) is not None


def _is_marked_de_entry(
    existing: Any,
    desired: Dict[str, Any],
    host_owned_fields: FrozenSet[str] = frozenset(),
) -> bool:
    """Recognize a prior generated JSON entry without trusting its dynamic paths."""

    extra_fields = (
        set(existing).difference(desired) if isinstance(existing, dict) else set()
    )
    if (
        not isinstance(existing, dict)
        or not set(desired).issubset(existing)
        or not extra_fields.issubset(host_owned_fields)
        or ("disabled" in extra_fields and type(existing["disabled"]) is not bool)
    ):
        return False
    existing_env = existing.get("env")
    desired_env = desired.get("env")
    if (
        not isinstance(existing_env, dict)
        or not isinstance(desired_env, dict)
        or set(existing_env) != set(desired_env)
        or existing_env.get(CLIENT_HOST_ENV) != desired_env.get(CLIENT_HOST_ENV)
        or not isinstance(existing.get("command"), str)
        or not existing["command"]
    ):
        return False
    launcher_root = _managed_launcher_root(
        existing.get("args"), existing.get("cwd")
    )
    if launcher_root is None or existing_env.get("PYTHONPATH") != launcher_root:
        return False
    if "type" in desired and existing.get("type") != desired.get("type"):
        return False
    return True


def _write_json_client(
    client: str,
    path: Path,
    server_name: str,
    entry: Dict[str, Any],
    dry_run: bool,
    *,
    ownership_policy: str = "replace-existing-v1",
    host_owned_fields: FrozenSet[str] = frozenset(),
    collection_key: str = "mcpServers",
    config_transform: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Dict[str, Any]:
    """Merge ``entry`` into a JSON client's top-level ``mcpServers[server_name]``.

    Fail-closed: if the file exists but isn't a JSON object (or ``mcpServers`` isn't an object),
    ABORT rather than risk clobbering the user's config. Idempotent: an identical existing entry
    is a no-op. Every other server and top-level key is preserved (order kept — no sort)."""
    existed = path.exists()
    data: Dict[str, Any] = {}
    mtime_ns: Optional[int] = None
    original_digest: Optional[str] = None
    if existed:
        if os.stat(path).st_size > MAX_AGENT_CONFIG_BYTES:
            raise ShellError("refusing to parse oversized Agent configuration %s" % path)
        mtime_ns = os.stat(path).st_mtime_ns   # snapshot for the pre-replace clobber guard
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode("utf-8")
        original_digest = hashlib.sha256(raw_bytes).hexdigest()
        if raw.strip():
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ShellError("refusing to write %s: existing file is not valid JSON (%s)" % (path, exc))
            if not isinstance(loaded, dict):
                raise ShellError("refusing to write %s: top-level JSON is a %s, not an object"
                                 % (path, type(loaded).__name__))
            data = loaded
    servers = data.get(collection_key)
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise ShellError(
            "refusing to write %s: %r is a %s, not an object"
            % (path, collection_key, type(servers).__name__)
        )
    entry_existed = server_name in servers
    existing_entry = servers.get(server_name)
    if (
        ownership_policy == "replace-marked-de-v1"
        and server_name in servers
        and not _is_marked_de_entry(existing_entry, entry, host_owned_fields)
    ):
        raise ShellError(
            "same_name_unowned: Decision Engine entry differs from the requested "
            "registration; nothing was written"
        )
    target_entry = dict(entry)
    if isinstance(existing_entry, dict):
        for field in host_owned_fields:
            if field in existing_entry:
                target_entry[field] = existing_entry[field]
    servers[server_name] = target_entry
    data[collection_key] = servers
    transformed = config_transform(data) if config_transform is not None else False
    if existing_entry == target_entry and not transformed:
        return {"client": client, "path": str(path), "action": "unchanged", "backup": None}
    if ownership_policy not in {"replace-existing-v1", "replace-marked-de-v1"}:
        raise ShellError("entry ownership policy is unsupported")
    action = "updated" if entry_existed else "added"
    if dry_run:
        return {"client": client, "path": str(path), "action": action + " (dry-run)", "backup": None}
    backup = _backup(path) if existed else None
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       expect_mtime_ns=mtime_ns, expect_sha256=original_digest,
                       expect_exists=existed)
    verify = json.loads(path.read_text(encoding="utf-8"))
    if verify.get(collection_key, {}).get(server_name) != target_entry:
        if backup is not None:
            shutil.copy2(backup, path)   # restore the pre-write file rather than leave a bad merge
        raise ShellError("post-write verification failed for %s (restored backup %s)"
                         % (path, backup))
    return {"client": client, "path": str(path), "action": action,
            "backup": str(backup) if backup else None}


def _splice_toml_table(original: str, header: str, block: str) -> str:
    """Replace the ``header`` (``[mcp_servers.<name>]``) table in ``original`` with ``block``, or
    append ``block`` if that table is absent. A table runs from its header line up to (but not
    including) the next ``[...]`` line that is NOT a descendant of the target — so the target's own
    sub-tables (``[mcp_servers.<name>.env]`` etc.) are consumed WITH it rather than orphaned after
    the replacement (audit 6c6ba7d8 convergent claude/gpt/grok). Every unrelated table and any
    comment outside the replaced range survives verbatim."""
    descendant = header[:-1] + "."   # "[mcp_servers.<name>." — a child table of the target
    lines = original.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    block_text = block if block.endswith("\n") else block + "\n"
    if start is None:
        if original and not original.endswith("\n"):
            original += "\n"
        sep = "\n" if original else ""
        return original + sep + block_text
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("[") and not stripped.startswith(descendant):
            end = j
            break
    return "".join(lines[:start]) + block_text + "".join(lines[end:])


def _write_codex_client(client: str, path: Path, server_name: str, block: str,
                        desired: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """Merge the ``[mcp_servers.<name>]`` ``block`` into a Codex ``config.toml`` via text surgery
    (stdlib has no TOML writer). Fail-closed: abort if the existing file isn't valid TOML, and roll
    back if the merged text no longer parses. Idempotent on an already-equal table."""
    tomllib = _tomllib()

    existed = path.exists()
    if existed and os.stat(path).st_size > MAX_AGENT_CONFIG_BYTES:
        raise ShellError("refusing to parse oversized Agent configuration %s" % path)
    mtime_ns = os.stat(path).st_mtime_ns if existed else None
    original_bytes = path.read_bytes() if existed else b""
    original = original_bytes.decode("utf-8")
    original_digest = hashlib.sha256(original_bytes).hexdigest() if existed else None
    existing_servers: Dict[str, Any] = {}
    if original.strip():
        try:
            parsed = tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            raise ShellError("refusing to write %s: existing file is not valid TOML (%s)" % (path, exc))
        servers = parsed.get("mcp_servers", {})
        if not isinstance(servers, dict):   # mirror the JSON guard — fail closed, don't AttributeError
            raise ShellError("refusing to write %s: 'mcp_servers' is a %s, not a table"
                             % (path, type(servers).__name__))
        existing_servers = servers
        if existing_servers.get(server_name) == desired:
            return {"client": client, "path": str(path), "action": "unchanged", "backup": None}
    action = "updated" if server_name in existing_servers else "added"
    merged = _splice_toml_table(original, "[mcp_servers.%s]" % _toml_key(server_name), block)
    try:
        after = tomllib.loads(merged)
    except tomllib.TOMLDecodeError as exc:
        raise ShellError("merging into %s produced invalid TOML, aborting (%s)" % (path, exc))
    if after.get("mcp_servers", {}).get(server_name) != desired:
        raise ShellError("post-merge verification failed for %s "
                         "(a hand-edited decision-engine table may need manual removal)" % path)
    if dry_run:
        return {"client": client, "path": str(path), "action": action + " (dry-run)", "backup": None}
    backup = _backup(path) if existed else None
    _atomic_write_text(
        path,
        merged,
        expect_mtime_ns=mtime_ns,
        expect_sha256=original_digest,
        expect_exists=existed,
    )
    return {"client": client, "path": str(path), "action": action,
            "backup": str(backup) if backup else None}


_MINIMUM_PYTHON = (3, 12)

# The interpreter must ANSWER, not merely exit 0: an exit code alone is satisfied by any
# executable that ignores ``-c``, which would let a wrapper script — or anything else on the
# filesystem — be recorded as "a working Python". The sentinel makes a real Python the only
# thing that can pass.
#
# This deliberately does NOT mirror install.sh's PEP 668 check. "Externally managed" means
# nothing can be installed INTO the interpreter — an install-time property. The launcher
# installs nothing at start-up, so the marker is not evidence that this entry cannot run.
# Testing it here also mis-fires: `Path(sys.executable).resolve()` on a venv yields the BASE
# interpreter, because symlink resolution destroys the very venv identity the marker test
# depends on, so a caller that merely normalises a good venv path would be refused.
# install.sh rejects an uninstallable interpreter before it is ever selected; that is the
# right layer for it.
_INTERPRETER_PROBE = "import sys; print('DE_PY', sys.version_info[0], sys.version_info[1])"


def _interpreter_defect(command: str) -> Optional[str]:
    """Why ``command`` cannot host the launcher, or ``None`` when it can.

    The reason is host-facing, so it names the defect and never the path — this module
    withholds private filesystem paths from client-visible failures (see
    ``client_write_failure_reason``).

    ``sys.executable`` gets no shortcut. Answering the default case from ``sys.version_info``
    would confirm the version of a process while saying nothing about whether its executable
    is still on disk, and a running interpreter outlives its own binary — an upgrade that
    moved or removed it would leave this recording a dead path.
    """
    if not os.path.isabs(command):
        # this probes with the installer's PATH and cwd, but the recorded string is resolved
        # later by the Agent — commonly a GUI launch with a minimal PATH, where a bare name
        # resolves to nothing, or to a different interpreter than the one verified here
        return "not an absolute path"
    try:
        probe = subprocess.run(
            [command, "-c", _INTERPRETER_PROBE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "not runnable"
    if probe.returncode != 0:
        return "not runnable"
    fields = (probe.stdout or "").split()
    if len(fields) != 3 or fields[0] != "DE_PY":
        return "not a Python interpreter"
    try:
        version = tuple(int(field) for field in fields[1:])
    except ValueError:
        return "not a Python interpreter"
    if version < _MINIMUM_PYTHON:
        return "older than Python %d.%d" % _MINIMUM_PYTHON
    return None


def _interpreter_gate(python: Optional[str]) -> None:
    """Refuse to RECORD an interpreter that cannot host the launcher.

    ``render_entry`` bakes ``python or sys.executable`` into the entry verbatim, so whichever
    interpreter happened to run the registration becomes the one every Agent launches from
    then on. Recording a bad one persists a shim that cannot start, or one that starts but can
    never receive a dependency, and either failure surfaces later and elsewhere — in the
    Agent, as a broken server. Gate it here, at the single write chokepoint, the way
    ``_cursor_write_version_gate`` refuses an unsupported Cursor: fail before anything reaches
    disk.
    """
    defect = _interpreter_defect(python or sys.executable)
    if defect is not None:
        raise ShellError(
            "unsupported_python: the interpreter for this entry is %s; nothing was written"
            % defect
        )
def _write_json_renderer(
    client: str,
    path: Path,
    server_name: str,
    *,
    python: Optional[str],
    cwd: Optional[Path],
    dev_root: Optional[Path],
    dry_run: bool,
) -> Dict[str, Any]:
    spec = CLIENT_SPECS[client]
    entry = _render_client_entry(
        client,
        python=python,
        cwd=cwd,
        dev_root=dev_root,
    )
    warning = _config_write_warning(spec)
    collection_key = renderer_collection_key(spec.config_renderer)
    if not collection_key:
        raise ShellError("configuration renderer collection is unsupported")
    config_transform = None
    if spec.json_config_transform is not None:
        config_transform = lambda data: spec.json_config_transform(data, entry)
    companion = None
    if spec.post_mcp_write is not None:
        companion = spec.post_mcp_write(entry, True)
        if not isinstance(companion, dict) or not isinstance(
            companion.get("action"), str
        ):
            raise ShellError("post-MCP-write companion returned an invalid result")
    result = _write_json_client(
        client,
        path,
        server_name,
        entry,
        dry_run,
        ownership_policy=spec.entry_ownership_policy,
        host_owned_fields=spec.host_owned_entry_fields,
        collection_key=collection_key,
        config_transform=config_transform,
    )
    if (
        spec.config_write_guard is not None
        or spec.config_write_guard_probe is not None
    ):
        result["warning"] = warning
    if spec.post_mcp_write is not None:
        if not dry_run:
            companion = spec.post_mcp_write(entry, False)
            if not isinstance(companion, dict) or not isinstance(
                companion.get("action"), str
            ):
                raise ShellError("post-MCP-write companion returned an invalid result")
        assert companion is not None
        result["companion"] = companion
    return result


def _write_toml_renderer(
    client: str,
    path: Path,
    server_name: str,
    *,
    python: Optional[str],
    cwd: Optional[Path],
    dev_root: Optional[Path],
    dry_run: bool,
) -> Dict[str, Any]:
    spec = CLIENT_SPECS[client]
    desired = _render_client_entry(
        client,
        python=python,
        cwd=cwd,
        dev_root=dev_root,
    )
    block = _render_toml_entry_block(server_name, desired)
    parsed_servers = parse_server_collection(spec.config_renderer, block)
    if not isinstance(parsed_servers, dict) or not isinstance(
        parsed_servers.get(server_name),
        dict,
    ):
        raise ShellError("rendered TOML entry did not round-trip")
    desired = parsed_servers[server_name]
    return _write_codex_client(
        client,
        path,
        server_name,
        block,
        desired,
        dry_run,
    )


_CONFIG_WRITERS = {
    "json-merge-v1": _write_json_renderer,
    "toml-splice-v1": _write_toml_renderer,
}


def _write_entry_unchecked(client: str, server_name: str = DEFAULT_SERVER_NAME, *,
                           python: Optional[str] = None, cwd: Optional[Path] = None,
                           dev_root: Optional[Path] = None,
                           allow_unactivated: bool = False,
                           dry_run: bool = False) -> Dict[str, Any]:
    """Register the shim under ``server_name`` in ``client``'s config, idempotently. Returns a
    small status dict ``{client, path, action, backup}`` (action ∈ added / updated / unchanged,
    plus a ``(dry-run)`` suffix when nothing was written).

    ``dev_root`` behaves exactly as in ``render_entry`` — see there."""
    if allow_unactivated:
        if dev_root is not None or cwd is not None:
            raise ShellError(
                "allow_unactivated cannot be combined with cwd= or dev_root="
            )
        cwd = pre_activation_root()
    path = agent_config_path(client)
    spec = CLIENT_SPECS.get(client)
    if spec is None:
        raise ShellError("unknown client %r" % client)
    # above the format fork, so no host (JSON or TOML) can be registered around it, and
    # ahead of `dry_run` too — a plan that reports a rejected interpreter as clean would
    # just send the caller into a write that fails.
    _interpreter_gate(python)
    writer_id = renderer_writer(spec.config_renderer)
    writer = _CONFIG_WRITERS.get(writer_id or "")
    if writer is None:
        raise ShellError("configuration writer is unsupported")
    return writer(
        client,
        path,
        server_name,
        python=python,
        cwd=cwd,
        dev_root=dev_root,
        dry_run=dry_run,
    )


def client_write_failure_reason(exc: BaseException) -> str:
    """Return a useful host failure without exposing private filesystem paths."""
    if isinstance(exc, ShellError) and str(exc) in {
        "unsupported_cursor_version",
        "cursor_version_probe_failed",
    }:
        return str(exc)
    return "client MCP configuration failed"


def write_entry(client: str, server_name: str = DEFAULT_SERVER_NAME, *,
                python: Optional[str] = None, cwd: Optional[Path] = None,
                dev_root: Optional[Path] = None,
                allow_unactivated: bool = False,
                dry_run: bool = False) -> Dict[str, Any]:
    """Write one client entry and normalize native filesystem failures."""
    try:
        return _write_entry_unchecked(
            client,
            server_name,
            python=python,
            cwd=cwd,
            dev_root=dev_root,
            allow_unactivated=allow_unactivated,
            dry_run=dry_run,
        )
    except OSError as exc:
        raise ShellError(client_write_failure_reason(exc)) from exc


def write_entries(
    clients,
    server_name: str = DEFAULT_SERVER_NAME,
    *,
    dev_root: Optional[Path] = None,
    allow_unactivated: bool = False,
    dry_run: bool = False,
) -> ClientWriteBatchResult:
    """Write hosts independently and retain an explicit partial-failure result."""
    written = []
    failed = []
    for client in clients:
        try:
            written.append(
                write_entry(
                    client,
                    server_name,
                    dev_root=dev_root,
                    allow_unactivated=allow_unactivated,
                    dry_run=dry_run,
                )
            )
        except (ShellError, OSError) as exc:
            failed.append((client, client_write_failure_reason(exc)))
    notices = () if dry_run else post_mcp_write_notices(
        item["client"] for item in written
    )
    return ClientWriteBatchResult(tuple(written), tuple(failed), notices)


def post_mcp_write_notices(clients) -> Tuple[str, ...]:
    """Return deduplicated host-declared steps for successfully configured MCPs."""
    notices = []
    for client in clients:
        spec = CLIENT_SPECS.get(client)
        notice_factory = spec.post_mcp_write_notice if spec is not None else None
        try:
            notice = notice_factory() if notice_factory is not None else None
        except Exception:  # aqg: top-level boundary — optional notice cannot undo a completed write
            continue
        if isinstance(notice, str) and notice.strip() and notice not in notices:
            notices.append(notice)
    return tuple(notices)


def console_safe_text(value: str, *, encoding: Optional[str] = None) -> str:
    """Make optional localized output printable on a strict legacy-codepage stream."""
    output_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return value.encode(output_encoding, errors="replace").decode(
            output_encoding, errors="replace"
        )
    except (LookupError, UnicodeError):
        return value.encode("ascii", errors="replace").decode("ascii")


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="de-mcp-config",
        description="Print, or --write, the MCP server registration for the Decision Engine shim",
    )
    parser.add_argument(
        "--name",
        dest="server_name",
        default=DEFAULT_SERVER_NAME,
        help="MCP server name to register under (default: %(default)s). NOTE: the shipped skills "
             "hardcode the `mcp__decision-engine__` tool prefix, so a non-default name only works if "
             "you also rewrite those skills — otherwise a tester's first tool call misses the server.",
    )
    parser.add_argument(
        "--codex",
        action="store_true",
        help="emit the Codex ~/.codex/config.toml block (with tool_timeout_sec) instead of the "
             "Claude Code mcpServers JSON",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="MERGE the registration into the agent's own config file (idempotent, backup-first, "
             "never clobbering other servers) instead of printing it. Targets --client, or every "
             "detected client when --client is omitted.",
    )
    parser.add_argument(
        "--client",
        choices=CLIENTS,
        help="with --write, the single client to configure; omit to configure every detected one.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --write, report what WOULD change without writing anything.",
    )
    parser.add_argument(
        "--dev-root",
        nargs="?",
        const=_DEV_ROOT_DEFAULT,
        default=None,
        metavar="PATH",
        help="register an explicit developer checkout instead of the activated managed install "
             "(renders --dev-root in the launcher args, which runs with no marker/network/updater/"
             "lease at all). Bare --dev-root defaults to this source checkout's own root; "
             "--dev-root PATH points at a different one. Without this, --write / the printed entry "
             "now REFUSE when no managed install is activated yet, instead of silently guessing.",
    )
    parser.add_argument(
        "--allow-unactivated",
        action="store_true",
        help=(
            "with --write, publish the fixed managed install before device activation; "
            "this is for the first-install path only and cannot be combined with --dev-root"
        ),
    )
    args = parser.parse_args(argv)
    dev_root = shell_root() if args.dev_root is _DEV_ROOT_DEFAULT else args.dev_root
    if args.allow_unactivated and args.dev_root is not None:
        parser.error("--allow-unactivated cannot be combined with --dev-root")

    if args.write:
        targets = [args.client] if args.client else detect_clients()
        if not targets:
            print(
                "de-mcp-config: no agent client detected (looked for registered hosts: %s). "
                "Pass --client <name> to force one." % ", ".join(CLIENT_SPECS),
                file=sys.stderr,
            )
            return 1
        failed = False
        written_clients = []
        for client in targets:
            try:
                result = write_entry(
                    client,
                    args.server_name,
                    dev_root=dev_root,
                    allow_unactivated=args.allow_unactivated,
                    dry_run=args.dry_run,
                )
            except ShellError as exc:   # user-facing, actionable — report and keep going to the next client
                print("de-mcp-config: %s: %s" % (client, exc), file=sys.stderr)
                failed = True
                continue
            written_clients.append(client)
            note = "" if not result["backup"] else "  (backup: %s)" % result["backup"]
            print("de-mcp-config: %-15s %-9s %s%s" % (client, result["action"], result["path"], note))
            if result.get("warning"):
                print(
                    "de-mcp-config: %s: %s; compatibility is unverified"
                    % (client, result["warning"]),
                    file=sys.stderr,
                )
        if not args.dry_run and not failed:
            print("de-mcp-config: done — RESTART the agent so it loads the new MCP server.")
        if not args.dry_run:
            for notice in post_mcp_write_notices(written_clients):
                print(console_safe_text("de-mcp-config: %s" % notice))
        return 1 if failed else 0

    try:
        if args.codex:
            print(
                render_codex_toml(
                    args.server_name,
                    dev_root=dev_root,
                    allow_unactivated=args.allow_unactivated,
                )
            )
        else:
            print(
                render(
                    args.server_name,
                    dev_root=dev_root,
                    allow_unactivated=args.allow_unactivated,
                )
            )
    except ShellError as exc:
        print("de-mcp-config: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
