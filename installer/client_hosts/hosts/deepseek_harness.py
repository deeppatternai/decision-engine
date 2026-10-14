"""DeepSeek Harness host declaration for the preview Cordis contract."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.config import ShellError


def dsh_home() -> Path:
    configured = os.getenv("DSH_HOME")
    if configured and configured.strip():
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ShellError("DSH_HOME must be an absolute path")
        return path
    return Path.home() / ".dsh"


def config_path() -> Path:
    configured = os.getenv("DEEPSEEK_HARNESS_CONFIG")
    if configured and configured.strip():
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ShellError("DEEPSEEK_HARNESS_CONFIG must be an absolute path")
        return path
    return dsh_home() / "cordis.patch.yml"


def skills_path() -> Path:
    return dsh_home() / "skills"


def _executable_path(candidate: Path) -> Path | None:
    try:
        resolved = candidate.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        return None
    return resolved


def _cli_path() -> Path | None:
    candidate = shutil.which("dsh")
    if not candidate:
        return None
    return _executable_path(Path(candidate))


def _macos_application_roots() -> tuple[Path, ...]:
    return (Path("/Applications"), Path.home() / "Applications")


def _macos_app_cli_path() -> Path | None:
    if sys.platform != "darwin":
        return None
    relative = Path(
        "DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
    )
    for root in _macos_application_roots():
        executable = _executable_path(root / relative)
        if executable is not None:
            return executable
    return None


def installed() -> bool:
    path = config_path()
    return (
        path.is_file()
        and not path.is_symlink()
    ) or _cli_path() is not None or _macos_app_cli_path() is not None


def _skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("deepseek-harness") is not None
    except ShellError:
        return False


def _write_guard() -> str | None:
    if not installed():
        raise ShellError(
            "deepseek_harness_not_installed: a cordis.patch.yml, dsh CLI, or "
            "native macOS app identity was not found; nothing was written"
        )
    return None


DEEPSEEK_HARNESS = AgentHostSpec(
    id="deepseek-harness",
    transport="stdio",
    launch_policy="direct-python-v1",
    config_format="yaml",
    host_family="deepseek-harness",
    config_env="DEEPSEEK_HARNESS_CONFIG",
    default_path=config_path,
    config_scope="user-global",
    config_path=config_path,
    config_renderer="cordis-yaml-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=_write_guard,
    observed_client_aliases=frozenset({"dsh-mcp-client"}),
    require_observed_identity=True,
    detection="installation-probe",
    installation_probe=installed,
    skills_path=skills_path,
    skills_global_path=skills_path,
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=installed,
    skills_check_in_use=_skills_configured,
    skill_route_name="deepseek-harness",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*(?:deepseek\s+harness|dsh-mcp-client)\s*$",),
    local_display_tools=True,
    popup_followup=False,
    audit_stop_panel=True,
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset({"mcp-entry", "skills"}),
    optional_features=frozenset({"local-display", "audit-stop-panel"}),
    onboarding_evidence=("skill",),
)

HOST_SPECS = (DEEPSEEK_HARNESS,)
