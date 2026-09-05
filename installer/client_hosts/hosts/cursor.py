"""Cursor host declaration and deferred presence predicates."""

from __future__ import annotations

from pathlib import Path

from installer import config
from installer.client_hosts.contract import AgentHostSpec


def _cursor_config_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _cursor_skills_path() -> Path:
    return config.cursor_skills_dir()


def _cursor_skills_in_use() -> bool:
    # This callback runs only after mcp_config finishes importing the static
    # registry. The invocation-time lookup preserves its legacy monkeypatch seam.
    from installer import mcp_config

    return mcp_config._cursor_skills_in_use()


def _cursor_skills_configured() -> bool:
    # See _cursor_skills_in_use: validation must not execute host callbacks.
    from installer import mcp_config

    return mcp_config._cursor_skills_configured()


CURSOR = AgentHostSpec(
    id="cursor",
    transport="stdio",
    config_write_guard="cursor-version-v1",
    config_format="json",
    host_family="cursor",
    config_env="CURSOR_CONFIG",
    default_path=_cursor_config_path,
    config_scope="user-global",
    config_path=_cursor_config_path,
    config_renderer="cursor-json-mcp-v1",
    observed_client_aliases=frozenset({"cursor", "cursor-vscode"}),
    require_observed_identity=True,
    json_include_type=False,
    json_include_cwd=False,
    skills_path=_cursor_skills_path,
    skills_global_path=_cursor_skills_path,
    skills_project_paths=(".cursor/skills", ".agents/skills"),
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _cursor_skills_in_use(),
    skills_check_in_use=lambda: _cursor_skills_configured(),
    skill_route_name="cursor",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"(?:^|[^a-z0-9])cursor(?:$|[^a-z0-9])",),
    local_display_tools=True,
    popup_followup=True,
    audit_stop_panel=True,
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "popup-followup", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset(
        {"mcp-entry", "skills", "workspace-shadow", "version"}
    ),
    optional_features=frozenset(
        {"local-display", "popup-followup", "audit-stop-panel"}
    ),
    onboarding_evidence=("skill",),
)

HOST_SPECS = (CURSOR,)
