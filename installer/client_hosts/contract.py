"""Client-host contracts and fail-closed registry validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, FrozenSet, Mapping, Optional, Tuple

from installer.client_hosts.renderers import renderer_format, renderer_writer
from installer.client_hosts.launchers import registered_launch_policies
from installer.client_hosts.transports import transport_supports_renderer
from installer.config import ShellError


SUPPORTED_DOCTOR_CAPABILITIES = frozenset(
    {"mcp-entry", "skills", "workspace-shadow", "version", "ownership"}
)

_SUPPORTED_CONFIG_SCOPES = frozenset({"user-global"})
_SUPPORTED_DETECTION_POLICIES = frozenset(
    {"file-or-parent", "file-or-sibling-dir", "installation-probe"}
)
_SUPPORTED_SKILL_DELIVERY_MODES = frozenset(
    {
        "none",
        "managed-copy",
        "transactional-managed-copy",
    }
)
_DELIVERY_ROUTING_KINDS = {
    "none": frozenset({"mcp-only"}),
    "managed-copy": frozenset({"skill"}),
    "transactional-managed-copy": frozenset({"skill"}),
}
_SUPPORTED_LAUNCHER_CAPABILITIES = frozenset(
    {
        "core-mcp",
        "local-display",
        "popup-followup",
        "audit-stop-panel",
        "runtime-display-policy",
    }
)
_SUPPORTED_LIFECYCLE_ADAPTERS = frozenset({"cursor-owned-v1"})
_SUPPORTED_CONFIG_WRITE_GUARDS = frozenset({"cursor-version-v1"})
_SUPPORTED_ONBOARDING_EVIDENCE = frozenset({"presence", "skill"})
_SUPPORTED_ENTRY_OWNERSHIP_POLICIES = frozenset(
    {"replace-existing-v1", "replace-marked-de-v1"}
)


@dataclass(frozen=True)
class AgentHostSpec:
    config_format: str
    host_family: str
    config_env: str
    default_path: Callable[[], Path]
    id: str = ""
    observed_client_aliases: FrozenSet[str] = frozenset()
    require_observed_identity: bool = False
    detection: str = "file-or-parent"
    json_include_type: bool = True
    json_include_cwd: bool = True
    skills_path: Optional[Callable[[], Path]] = None
    skills_in_use: Optional[Callable[[], bool]] = None
    skills_check_in_use: Optional[Callable[[], bool]] = None
    skill_route_name: Optional[str] = None
    excluded_skills: FrozenSet[str] = frozenset()
    repair_skills_on_setup: bool = False
    skills_require_managed_target: bool = False
    client_name_patterns: Tuple[str, ...] = ()
    local_display_tools: bool = False
    popup_followup: bool = False
    audit_stop_panel: bool = False
    runtime_display_policy: Optional[str] = None
    popup_api_profile: str = "legacy"
    config_scope: str = ""
    config_path: Optional[Callable[[], Path]] = None
    config_renderer: str = ""
    skills_global_path: Optional[Callable[[], Path]] = None
    skills_project_paths: Tuple[str, ...] = ()
    skill_delivery_mode: str = "none"
    routing_kind: str = "mcp-only"
    launcher_capabilities: FrozenSet[str] = frozenset({"core-mcp"})
    doctor_capabilities: FrozenSet[str] = frozenset()
    optional_features: FrozenSet[str] = frozenset()
    lifecycle_adapter: Optional[str] = None
    # Appended after every legacy field to preserve ClientSpec positional calls.
    transport: str = "stdio"
    config_write_guard: Optional[str] = None
    launch_policy: str = "direct-python-v1"
    onboarding_evidence: Tuple[str, ...] = ()
    # Retained in its original positional slot for ClientSpec compatibility. Global routing
    # probes are retired; validation below rejects any new use.
    onboarding_routing_probe: Optional[Callable[[], bool]] = None
    entry_ownership_policy: str = "replace-existing-v1"
    config_write_guard_probe: Optional[Callable[[], Optional[str]]] = None
    installation_probe: Optional[Callable[[], bool]] = None
    post_mcp_write_notice: Optional[Callable[[], str]] = None
    interactive_setup_guard_probe: Optional[Callable[[], Optional[str]]] = None
    # Blocks ONLY the WebView backend of the setup GUI (the Tk form still renders).
    # Distinct from interactive_setup_guard_probe, which refuses any window at all.
    # interactive_setup_guard_probe is currently registered by NO host: it is
    # retained deliberately for future hosts whose sessions must refuse every
    # window (not merely degrade the backend). Do not mistake it for the
    # WebView gate — see webview_render_guard_probe below.
    webview_render_guard_probe: Optional[Callable[[], Optional[str]]] = None
    json_config_transform: Optional[Callable[[dict, dict], bool]] = None
    unverified_lite_stopper: bool = False


# Compatibility for callers/tests that imported the pre-WP1 type name.
ClientSpec = AgentHostSpec


def _invalid_host_spec(client: str, reason: str) -> None:
    raise ShellError("host %r %s" % (client, reason))


def validate_host_specs(specs: Mapping[str, AgentHostSpec]) -> None:
    """Fail closed when declarative host metadata drifts from executable flags."""

    for client, spec in specs.items():
        if spec.id != client:
            _invalid_host_spec(client, "registry id does not match its key")
        if not transport_supports_renderer(spec.transport, spec.config_renderer):
            _invalid_host_spec(client, "transport or renderer is unsupported")
        if spec.detection not in _SUPPORTED_DETECTION_POLICIES:
            _invalid_host_spec(client, "detection policy is unsupported")
        if spec.installation_probe is not None and not callable(
            spec.installation_probe
        ):
            _invalid_host_spec(client, "installation probe is not callable")
        if spec.detection == "installation-probe" and spec.installation_probe is None:
            _invalid_host_spec(
                client, "installation-probe detection requires an installation probe"
            )
        if spec.post_mcp_write_notice is not None and not callable(
            spec.post_mcp_write_notice
        ):
            _invalid_host_spec(client, "post-MCP-write notice is invalid")
        if spec.config_scope not in _SUPPORTED_CONFIG_SCOPES:
            _invalid_host_spec(client, "config scope is missing or unsupported")
        if not callable(spec.config_path):
            _invalid_host_spec(client, "config path is missing")
        if spec.config_path is not spec.default_path:
            _invalid_host_spec(
                client,
                "config path metadata conflicts with compatibility field",
            )
        if not spec.config_renderer:
            _invalid_host_spec(client, "config renderer is missing")
        if renderer_format(spec.config_renderer) != spec.config_format:
            _invalid_host_spec(client, "config renderer conflicts with format")
        if not spec.launcher_capabilities.issubset(
            _SUPPORTED_LAUNCHER_CAPABILITIES
        ):
            _invalid_host_spec(client, "launcher capability metadata is unsupported")
        if "core-mcp" not in spec.launcher_capabilities:
            _invalid_host_spec(client, "launcher capability metadata is incomplete")
        if not spec.doctor_capabilities.issubset(SUPPORTED_DOCTOR_CAPABILITIES):
            _invalid_host_spec(client, "Doctor capability metadata is unsupported")
        if (
            spec.lifecycle_adapter is not None
            and spec.lifecycle_adapter not in _SUPPORTED_LIFECYCLE_ADAPTERS
        ):
            _invalid_host_spec(client, "lifecycle adapter metadata is unsupported")
        if (spec.skill_delivery_mode == "transactional-managed-copy") != (
            spec.lifecycle_adapter is not None
        ):
            _invalid_host_spec(
                client,
                "lifecycle adapter conflicts with skill delivery metadata",
            )
        if (
            spec.config_write_guard is not None
            and spec.config_write_guard not in _SUPPORTED_CONFIG_WRITE_GUARDS
        ):
            _invalid_host_spec(client, "config write guard is unsupported")
        if spec.config_write_guard_probe is not None and not callable(
            spec.config_write_guard_probe
        ):
            _invalid_host_spec(client, "config write guard probe is invalid")
        if (
            spec.interactive_setup_guard_probe is not None
            and not callable(spec.interactive_setup_guard_probe)
        ):
            _invalid_host_spec(client, "interactive setup guard probe is invalid")
        if (
            spec.webview_render_guard_probe is not None
            and not callable(spec.webview_render_guard_probe)
        ):
            _invalid_host_spec(client, "webview render guard probe is invalid")
        if (
            spec.json_config_transform is not None
            and not callable(spec.json_config_transform)
        ):
            _invalid_host_spec(client, "JSON config transform is invalid")
        if (
            spec.config_write_guard is not None
            and spec.config_write_guard_probe is not None
        ):
            _invalid_host_spec(client, "config write guard metadata conflicts")
        if spec.launch_policy not in registered_launch_policies():
            _invalid_host_spec(client, "launch policy is unsupported")
        if (
            len(set(spec.onboarding_evidence)) != len(spec.onboarding_evidence)
            or not set(spec.onboarding_evidence).issubset(
                _SUPPORTED_ONBOARDING_EVIDENCE
            )
        ):
            _invalid_host_spec(client, "onboarding evidence metadata is unsupported")
        if spec.onboarding_routing_probe is not None:
            _invalid_host_spec(client, "global routing probes are retired")
        if "skill" in spec.onboarding_evidence and spec.skill_delivery_mode == "none":
            _invalid_host_spec(client, "onboarding skill evidence has no delivery")
        if (
            spec.skill_delivery_mode != "none"
            and "mcp-entry" in spec.doctor_capabilities
            and "skill" not in spec.onboarding_evidence
        ):
            _invalid_host_spec(client, "delivers skills but declares no onboarding evidence")
        if spec.entry_ownership_policy not in _SUPPORTED_ENTRY_OWNERSHIP_POLICIES:
            _invalid_host_spec(client, "entry ownership policy is unsupported")
        if renderer_writer(spec.config_renderer) != "json-merge-v1" and (
            spec.config_write_guard is not None
            or spec.config_write_guard_probe is not None
            or spec.entry_ownership_policy != "replace-existing-v1"
        ):
            _invalid_host_spec(client, "write policy requires the JSON writer")
        if (
            spec.json_config_transform is not None
            and renderer_writer(spec.config_renderer) != "json-merge-v1"
        ):
            _invalid_host_spec(client, "JSON config transform requires the JSON writer")
        if spec.unverified_lite_stopper and (
            not spec.require_observed_identity or not spec.audit_stop_panel
        ):
            _invalid_host_spec(
                client,
                "unverified Lite Stopper requires identity enforcement and a stop panel",
            )

        expected_optional = set()
        if spec.local_display_tools:
            expected_optional.add("local-display")
        if spec.popup_followup:
            expected_optional.add("popup-followup")
        if spec.audit_stop_panel:
            expected_optional.add("audit-stop-panel")
        if spec.runtime_display_policy:
            expected_optional.add("runtime-display-policy")
        if spec.optional_features != frozenset(expected_optional):
            _invalid_host_spec(
                client,
                "optional feature metadata conflicts with behavior",
            )
        if not spec.optional_features.issubset(spec.launcher_capabilities):
            _invalid_host_spec(
                client,
                "launcher capability metadata conflicts with features",
            )

        if spec.skill_delivery_mode not in _SUPPORTED_SKILL_DELIVERY_MODES:
            _invalid_host_spec(client, "skill delivery metadata is unsupported")
        if spec.skill_delivery_mode == "none":
            if spec.skills_global_path is not None:
                _invalid_host_spec(
                    client,
                    "skill delivery metadata conflicts with its path",
                )
        elif not callable(spec.skills_global_path):
            _invalid_host_spec(client, "global skill path is missing")
        if spec.skills_global_path is not spec.skills_path:
            _invalid_host_spec(
                client,
                "global skill metadata conflicts with compatibility field",
            )
        has_skill_delivery = spec.skill_delivery_mode != "none"
        if has_skill_delivery:
            if not callable(spec.skills_in_use) or not callable(
                spec.skills_check_in_use
            ):
                _invalid_host_spec(client, "skill probe metadata is incomplete")
            if not spec.skill_route_name:
                _invalid_host_spec(client, "skill route metadata is missing")
        elif any(
            value is not None
            for value in (
                spec.skills_in_use,
                spec.skills_check_in_use,
                spec.skill_route_name,
            )
        ):
            _invalid_host_spec(client, "skill probe metadata conflicts with delivery mode")
        if spec.routing_kind not in _DELIVERY_ROUTING_KINDS[spec.skill_delivery_mode]:
            _invalid_host_spec(
                client,
                "routing metadata conflicts with skill delivery mode",
            )
        if ("skills" in spec.doctor_capabilities) != (
            spec.skill_delivery_mode != "none"
        ):
            _invalid_host_spec(
                client,
                "Doctor skill capability conflicts with delivery mode",
            )
        for raw_path in spec.skills_project_paths:
            path = PurePosixPath(raw_path)
            windows_path = PureWindowsPath(raw_path)
            if (
                path.is_absolute()
                or bool(windows_path.drive)
                or windows_path.is_absolute()
                or raw_path != path.as_posix()
                or ".." in path.parts
                or len(path.parts) != 2
                or path.name != "skills"
            ):
                _invalid_host_spec(client, "project skill metadata is invalid")
        has_workspace_shadow = "workspace-shadow" in spec.doctor_capabilities
        if has_workspace_shadow != bool(spec.skills_project_paths):
            _invalid_host_spec(
                client,
                "workspace-shadow capability conflicts with project skill paths",
            )
        for pattern in spec.client_name_patterns:
            try:
                re.compile(pattern)
            except re.error:
                _invalid_host_spec(client, "client name pattern is invalid")
