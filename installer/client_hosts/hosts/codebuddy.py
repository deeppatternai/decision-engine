"""Tencent CodeBuddy Agent CLI host declaration."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.config import ShellError


def _codebuddy_config_path() -> Path:
    return Path.home() / ".codebuddy" / "mcp.json"


def _codebuddy_skills_path() -> Path:
    configured = os.getenv("CODEBUDDY_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".codebuddy" / "skills"


def _inside_workbuddy_ai_bundle(path: Path) -> bool:
    return any(part.casefold() == "workbuddy ai.app" for part in path.parts)


def _is_executable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name != "nt":
        return os.access(path, os.X_OK)
    raw_pathext = os.getenv("PATHEXT")
    suffixes = {
        suffix.strip().casefold()
        for suffix in (raw_pathext or ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        if suffix.strip()
    }
    return path.suffix.casefold() in suffixes


def _codebuddy_cli_path() -> Path | None:
    configured = os.getenv("CODEBUDDY_CLI")
    candidate = configured.strip() if configured and configured.strip() else None
    if candidate is None:
        candidate = shutil.which("codebuddy")
    if not candidate:
        return None
    try:
        path = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if (
        not _is_executable_file(path)
        or _inside_workbuddy_ai_bundle(path)
    ):
        return None
    return path


def _codebuddy_installed() -> bool:
    return _codebuddy_cli_path() is not None


def _codebuddy_skills_in_use() -> bool:
    return _codebuddy_installed()


def _codebuddy_skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("codebuddy") is not None
    except ShellError:
        return False


def _codebuddy_config_write_guard():
    if _codebuddy_cli_path() is None:
        raise ShellError(
            "codebuddy_not_installed: an independent CodeBuddy Agent CLI "
            "executable was not found; CodeBuddy Studio, WorkBuddy AI's bundled "
            "CLI, and residual configuration directories are not installation "
            "evidence; nothing was written"
        )
    return None


CODEBUDDY = AgentHostSpec(
    id="codebuddy",
    transport="stdio",
    launch_policy="direct-python-v1",
    config_format="json",
    host_family="codebuddy",
    config_env="CODEBUDDY_CONFIG",
    default_path=_codebuddy_config_path,
    config_scope="user-global",
    config_path=_codebuddy_config_path,
    config_renderer="json-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=lambda: _codebuddy_config_write_guard(),
    observed_client_aliases=frozenset({"codebuddy"}),
    require_observed_identity=True,
    detection="installation-probe",
    installation_probe=lambda: _codebuddy_installed(),
    json_include_type=False,
    json_include_cwd=False,
    skills_path=_codebuddy_skills_path,
    skills_global_path=_codebuddy_skills_path,
    skills_project_paths=(".codebuddy/skills",),
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _codebuddy_skills_in_use(),
    skills_check_in_use=lambda: _codebuddy_skills_configured(),
    skill_route_name="codebuddy",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*codebuddy(?:\s+(?:agent|cli))?\s*$",),
    local_display_tools=True,
    popup_followup=False,
    audit_stop_panel=True,
    popup_api_profile="legacy",
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset(
        {"mcp-entry", "skills", "workspace-shadow"}
    ),
    optional_features=frozenset({"local-display", "audit-stop-panel"}),
    onboarding_evidence=("skill",),
    unverified_lite_stopper=True,
)

HOST_SPECS = (CODEBUDDY,)
