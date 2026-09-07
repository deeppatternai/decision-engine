"""Tencent WorkBuddy AI Desktop declaration for macOS."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.client_hosts.product_metadata import bounded_plist_metadata
from installer.config import ShellError


_MINIMUM_VERSION = (5, 5, 2)
_TESTED_VERSION = (5, 5, 2)


def _workbuddy_ai_config_path() -> Path:
    return Path.home() / ".workbuddy-ai" / "mcp.json"


def _workbuddy_ai_app_root() -> Path:
    configured = os.getenv("WORKBUDDY_AI_APP_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path("/Applications/WorkBuddy AI.app")


def _workbuddy_ai_skills_path() -> Path:
    configured = os.getenv("WORKBUDDY_AI_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".workbuddy-ai" / "skills"


def _workbuddy_ai_settings_path() -> Path:
    configured = os.getenv("WORKBUDDY_AI_SETTINGS")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".workbuddy-ai" / "settings.json"


def _workbuddy_ai_product_metadata():
    return bounded_plist_metadata(
        _workbuddy_ai_app_root() / "Contents" / "Info.plist",
        expected_bundle_identifier="com.workbuddy.workbuddy-ai",
    )


def _workbuddy_ai_installed() -> bool:
    if sys.platform != "darwin":
        return False
    probe = _workbuddy_ai_product_metadata()
    return (
        probe.status == "matched"
        and probe.version is not None
        and probe.version >= _MINIMUM_VERSION
    )


def _workbuddy_ai_skills_in_use() -> bool:
    return _workbuddy_ai_installed()


def _workbuddy_ai_skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("workbuddy-ai") is not None
    except ShellError:
        return False


def _workbuddy_ai_config_write_guard():
    if sys.platform != "darwin":
        raise ShellError(
            "unsupported_workbuddy_ai_platform: only the verified macOS Desktop "
            "build is supported; nothing was written"
        )
    probe = _workbuddy_ai_product_metadata()
    if probe.status == "unavailable":
        raise ShellError(
            "workbuddy_ai_not_installed: WorkBuddy AI Desktop was not found at "
            "the configured application root; nothing was written"
        )
    if probe.status == "identity-mismatch":
        raise ShellError(
            "workbuddy_ai_identity_mismatch: the configured application root "
            "belongs to a different product; nothing was written"
        )
    if probe.status != "matched" or probe.version is None:
        raise ShellError(
            "workbuddy_ai_metadata_invalid: WorkBuddy AI Desktop metadata is "
            "malformed; nothing was written"
        )
    if probe.version < _MINIMUM_VERSION:
        version_label = ".".join(map(str, _MINIMUM_VERSION))
        raise ShellError(
            "unsupported_workbuddy_ai_version: WorkBuddy AI Desktop is below "
            f"the tested {version_label} support floor; nothing was written"
        )
    if probe.version > _TESTED_VERSION:
        return "workbuddy_ai_version_newer_than_tested"
    return None


def _workbuddy_ai_post_mcp_write(
    entry: dict[str, object], dry_run: bool
) -> dict[str, object]:
    from installer.client_hosts.hosts import workbuddy_ai_prompt_hook

    environment = entry.get("env")
    command = entry.get("command")
    de_root = environment.get("PYTHONPATH") if isinstance(environment, dict) else None
    if (
        not isinstance(command, str)
        or not command.strip()
        or not isinstance(de_root, str)
        or not Path(de_root).is_absolute()
    ):
        raise ShellError(
            "rendered WorkBuddy AI entry cannot identify its DE hook runtime"
        )
    return workbuddy_ai_prompt_hook.install_hook(
        _workbuddy_ai_settings_path(),
        de_root=Path(de_root),
        python_executable=command,
        dry_run=dry_run,
    )


WORKBUDDY_AI = AgentHostSpec(
    id="workbuddy-ai",
    transport="stdio",
    launch_policy="desktop-python-v1",
    config_format="json",
    host_family="workbuddy-ai",
    config_env="WORKBUDDY_AI_CONFIG",
    default_path=_workbuddy_ai_config_path,
    config_scope="user-global",
    config_path=_workbuddy_ai_config_path,
    config_renderer="json-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=lambda: _workbuddy_ai_config_write_guard(),
    observed_client_aliases=frozenset({"codebuddy"}),
    require_observed_identity=True,
    detection="installation-probe",
    installation_probe=lambda: _workbuddy_ai_installed(),
    json_include_type=False,
    json_include_cwd=False,
    skills_path=_workbuddy_ai_skills_path,
    skills_global_path=_workbuddy_ai_skills_path,
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _workbuddy_ai_skills_in_use(),
    skills_check_in_use=lambda: _workbuddy_ai_skills_configured(),
    skill_route_name="workbuddy-ai",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*workbuddy\s+ai\s*$",),
    local_display_tools=True,
    popup_followup=False,
    audit_stop_panel=True,
    popup_api_profile="legacy",
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset({"mcp-entry", "skills"}),
    optional_features=frozenset({"local-display", "audit-stop-panel"}),
    onboarding_evidence=("skill",),
    host_owned_entry_fields=frozenset({"disabled"}),
    unverified_lite_stopper=True,
    post_mcp_write=_workbuddy_ai_post_mcp_write,
)

HOST_SPECS = (WORKBUDDY_AI,)
