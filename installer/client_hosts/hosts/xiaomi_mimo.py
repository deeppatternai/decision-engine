"""Xiaomi MiMo host declaration for the native MiMoCode contract."""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.config import ShellError


CONFIG_NAMES = ("mimocode.jsonc", "mimocode.json", "config.json")
MACOS_APP_NAME = "Xiaomi MiMo.app"
MACOS_BUNDLE_ID = "com.xiaomi.mimo.desktop"
_PROCESS_HOME = Path.home().expanduser().resolve()


def _absolute_env_path(name: str, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ShellError(f"{name} must be an absolute path")
    return path


def config_root() -> Path:
    mimocode_home = os.getenv("MIMOCODE_HOME")
    if mimocode_home and mimocode_home.strip():
        return _absolute_env_path("MIMOCODE_HOME", mimocode_home) / "config"
    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home and xdg_config_home.strip():
        return _absolute_env_path("XDG_CONFIG_HOME", xdg_config_home) / "mimocode"
    return Path.home() / ".config" / "mimocode"


def config_path() -> Path:
    root = config_root()
    for name in CONFIG_NAMES:
        candidate = root / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return root / CONFIG_NAMES[0]


def skills_path() -> Path:
    return config_root() / "skills"


def _macos_application_roots() -> tuple[Path, ...]:
    configured = os.getenv("XIAOMI_MIMO_APP_DIRS")
    if configured:
        roots = tuple(
            _absolute_env_path("XIAOMI_MIMO_APP_DIRS", value)
            for value in configured.split(os.pathsep)
            if value
        )
        return roots
    home = Path.home().expanduser().resolve()
    if home != _PROCESS_HOME:
        return (home / "Applications",)
    return (Path("/Applications"), home / "Applications")


def _macos_app_installed() -> bool:
    if sys.platform != "darwin":
        return False
    for root in _macos_application_roots():
        app = root / MACOS_APP_NAME
        info = app / "Contents" / "Info.plist"
        if app.is_symlink() or info.is_symlink() or not info.is_file():
            continue
        try:
            with info.open("rb") as handle:
                metadata = plistlib.load(handle)
        except (OSError, ValueError):
            continue
        if (
            isinstance(metadata, dict)
            and metadata.get("CFBundleIdentifier") == MACOS_BUNDLE_ID
        ):
            return True
    return False


def installed() -> bool:
    root = config_root()
    if any(
        path.is_file() and not path.is_symlink()
        for path in (root / name for name in CONFIG_NAMES)
    ):
        return True
    return _macos_app_installed()


def _skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("xiaomi-mimo") is not None
    except ShellError:
        return False


def _write_guard() -> str | None:
    if not installed():
        raise ShellError(
            "xiaomi_mimo_not_installed: an exact Xiaomi MiMo app identity or "
            "native config file was not found; nothing was written"
        )
    return None


XIAOMI_MIMO = AgentHostSpec(
    id="xiaomi-mimo",
    transport="stdio",
    launch_policy="direct-python-v1",
    config_format="jsonc",
    host_family="xiaomi-mimo",
    config_env="",
    default_path=config_path,
    config_scope="user-global",
    config_path=config_path,
    config_renderer="mimocode-jsonc-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=_write_guard,
    observed_client_aliases=frozenset({"mimocode"}),
    require_observed_identity=True,
    detection="installation-probe",
    installation_probe=installed,
    skills_path=skills_path,
    skills_global_path=skills_path,
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=installed,
    skills_check_in_use=_skills_configured,
    skill_route_name="xiaomi-mimo",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*(?:xiaomi\s+mimo|mimocode)\s*$",),
    local_display_tools=True,
    popup_followup=False,
    audit_stop_panel=True,
    ge_progress_keepalive_s=15.0,
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset({"mcp-entry", "skills"}),
    optional_features=frozenset({"local-display", "audit-stop-panel"}),
    onboarding_evidence=("skill",),
)

HOST_SPECS = (XIAOMI_MIMO,)
