"""Independent macOS Qoder CN IDE host declaration."""

from __future__ import annotations

import sys
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.client_hosts.hosts import qoder_ide_common
from installer.config import ShellError


def _app_root() -> Path:
    return qoder_ide_common.configured_path(
        "QODER_CN_IDE_APP_ROOT", Path("/Applications/Qoder CN IDE.app")
    )


def _config_path() -> Path:
    return qoder_ide_common.configured_path(
        "QODER_CN_IDE_CONFIG", Path.home() / ".qoder-cn" / "mcp.json"
    )


def _settings_path() -> Path:
    return qoder_ide_common.configured_path(
        "QODER_CN_IDE_SETTINGS", Path.home() / ".qoder-cn" / "settings.json"
    )


def _skills_path() -> Path:
    return qoder_ide_common.configured_path(
        "QODER_CN_IDE_SKILLS_DIR", Path.home() / ".qoder-cn" / "skills"
    )


def _product_metadata():
    return qoder_ide_common.product_metadata(
        _app_root(),
        application_name="qoder-cn",
        bundle_identifier="com.aliyun.lingma.ide",
    )


def _installed() -> bool:
    return qoder_ide_common.installed(_product_metadata(), platform=sys.platform)


def _skills_in_use() -> bool:
    return _installed()


def _skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("qoder-cn-ide") is not None
    except ShellError:
        return False


def _config_write_guard():
    return qoder_ide_common.config_write_guard(
        _product_metadata(),
        platform=sys.platform,
        error_prefix="qoder_cn_ide",
        product_label="Qoder CN IDE",
    )


def _post_mcp_write(entry: dict[str, object], dry_run: bool) -> dict[str, object]:
    return qoder_ide_common.install_prompt_hook(
        entry, dry_run, settings_path=_settings_path()
    )


QODER_CN_IDE = AgentHostSpec(
    id="qoder-cn-ide",
    transport="stdio",
    launch_policy="absolute-bootstrap-v1",
    config_format="json",
    host_family="qoder-cn-ide",
    config_env="QODER_CN_IDE_CONFIG",
    default_path=_config_path,
    config_scope="user-global",
    config_path=_config_path,
    config_renderer="json-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=lambda: _config_write_guard(),
    observed_client_aliases=frozenset({"qoder cn"}),
    require_observed_identity=True,
    detection="installation-probe",
    installation_probe=lambda: _installed(),
    json_include_type=False,
    json_include_cwd=False,
    skills_path=_skills_path,
    skills_global_path=_skills_path,
    skills_project_paths=(".qoder-cn/skills",),
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _skills_in_use(),
    skills_check_in_use=lambda: _skills_configured(),
    skill_route_name="qoder-cn-ide",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*qoder\s+cn\s+ide\s*$",),
    local_display_tools=True,
    popup_followup=False,
    audit_stop_panel=True,
    popup_api_profile="legacy",
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset({"mcp-entry", "skills", "workspace-shadow"}),
    optional_features=frozenset({"local-display", "audit-stop-panel"}),
    onboarding_evidence=("skill",),
    unverified_lite_stopper=True,
    post_mcp_write=_post_mcp_write,
)

HOST_SPECS = (QODER_CN_IDE,)
