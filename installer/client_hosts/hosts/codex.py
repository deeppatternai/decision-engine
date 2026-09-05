"""Codex host declaration."""

from __future__ import annotations

from pathlib import Path

from installer import config
from installer.client_hosts.contract import AgentHostSpec


def _codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _codex_skills_path() -> Path:
    return config.codex_skills_dir()


def _codex_skills_in_use() -> bool:
    """Resolve dynamically so tests and embedders can override the probe."""

    return config.codex_skills_in_use()


CODEX = AgentHostSpec(
    id="codex",
    transport="stdio",
    config_format="toml",
    host_family="codex",
    config_env="CODEX_CONFIG",
    default_path=_codex_config_path,
    config_scope="user-global",
    config_path=_codex_config_path,
    config_renderer="codex-toml-v1",
    observed_client_aliases=frozenset({"codex", "openai codex"}),
    skills_path=_codex_skills_path,
    skills_global_path=_codex_skills_path,
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _codex_skills_in_use(),
    skills_check_in_use=lambda: _codex_skills_in_use(),
    skill_route_name="codex",
    repair_skills_on_setup=True,
    client_name_patterns=(r"(?:^|[^a-z0-9])codex(?:$|[^a-z0-9])",),
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
    onboarding_evidence=("presence", "skill"),
)

HOST_SPECS = (CODEX,)
