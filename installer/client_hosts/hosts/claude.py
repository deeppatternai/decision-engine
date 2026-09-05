"""Claude Code and Claude Desktop host declarations."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from installer import config
from installer.client_hosts.contract import AgentHostSpec


def _claude_code_config_path() -> Path:
    return Path.home() / ".claude.json"


def _claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _claude_skills_path() -> Path:
    return config.claude_skills_dir()


CLAUDE_CODE = AgentHostSpec(
    id="claude-code",
    transport="stdio",
    config_format="json",
    host_family="claude",
    config_env="CLAUDE_CODE_CONFIG",
    default_path=_claude_code_config_path,
    config_scope="user-global",
    config_path=_claude_code_config_path,
    config_renderer="json-mcp-v1",
    observed_client_aliases=frozenset(
        {"anthropic", "claude", "claude code", "claude desktop"}
    ),
    detection="file-or-sibling-dir",
    skills_path=_claude_skills_path,
    skills_global_path=_claude_skills_path,
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: True,
    skills_check_in_use=lambda: True,
    skill_route_name="claude",
    repair_skills_on_setup=True,
    client_name_patterns=(
        r"(?:^|[^a-z0-9])claude(?:$|[^a-z0-9])",
        r"(?:^|[^a-z0-9])anthropic(?:$|[^a-z0-9])",
    ),
    local_display_tools=True,
    popup_followup=True,
    audit_stop_panel=True,
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "popup-followup", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset({"mcp-entry", "skills"}),
    optional_features=frozenset(
        {"local-display", "popup-followup", "audit-stop-panel"}
    ),
    onboarding_evidence=("skill",),
)

CLAUDE_DESKTOP = AgentHostSpec(
    id="claude-desktop",
    transport="stdio",
    config_format="json",
    host_family="claude",
    config_env="CLAUDE_DESKTOP_CONFIG",
    default_path=_claude_desktop_config_path,
    config_scope="user-global",
    config_path=_claude_desktop_config_path,
    config_renderer="json-mcp-v1",
    observed_client_aliases=frozenset(
        {"anthropic", "claude", "claude code", "claude desktop"}
    ),
    skill_delivery_mode="none",
    routing_kind="mcp-only",
    client_name_patterns=(
        r"(?:^|[^a-z0-9])claude(?:$|[^a-z0-9])",
        r"(?:^|[^a-z0-9])anthropic(?:$|[^a-z0-9])",
    ),
    local_display_tools=True,
    popup_followup=True,
    audit_stop_panel=True,
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "popup-followup", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset({"mcp-entry"}),
    optional_features=frozenset(
        {"local-display", "popup-followup", "audit-stop-panel"}
    ),
)

HOST_SPECS = (CLAUDE_CODE, CLAUDE_DESKTOP)
