"""TRAE Work CN host declaration.

The retained product evidence covers the ``TRAE SOLO CN`` / ``.trae-cn``
application/profile identity on Windows and macOS. Windows x64 was live-tested
at 0.1.48 through 0.1.49; the macOS bundle contract is retained separately.
Product-specific paths and launch constraints remain confined to this adapter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.client_hosts.product_metadata import (
    ProductMetadataProbe,
    bounded_plist_metadata,
    bounded_product_metadata,
)
from installer.config import ShellError


_MINIMUM_VERSION = (0, 1, 48)
_TESTED_VERSION = (0, 1, 49)


def _trae_work_cn_config_path() -> Path:
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "TRAE SOLO CN" / "User" / "mcp.json"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "TRAE SOLO CN"
            / "User"
            / "mcp.json"
        )
    return Path.home() / ".config" / "TRAE SOLO CN" / "User" / "mcp.json"


def _trae_work_cn_app_root() -> Path:
    configured = os.getenv("TRAE_WORK_CN_APP_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path("/Applications/TRAE SOLO CN.app")
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "TRAE SOLO CN"


def _trae_work_cn_skills_path() -> Path:
    configured = os.getenv("TRAE_WORK_CN_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".trae-cn" / "skills"


def _trae_work_cn_product_metadata():
    relative = (
        Path("Contents") / "Resources" / "app" / "product.json"
        if sys.platform == "darwin"
        else Path("resources") / "app" / "product.json"
    )
    product = bounded_product_metadata(
        _trae_work_cn_app_root() / relative,
        expected_application_name="trae-solo-cn",
    )
    if sys.platform != "darwin" or product.status != "matched":
        return product
    bundle = bounded_plist_metadata(
        _trae_work_cn_app_root() / "Contents" / "Info.plist",
        expected_bundle_identifier="cn.trae.solo.app",
    )
    if bundle.status != "matched" or bundle.version != product.version:
        return ProductMetadataProbe(
            "identity-mismatch"
            if bundle.status == "identity-mismatch"
            else "malformed"
        )
    return product


def _trae_work_cn_version():
    return _trae_work_cn_product_metadata().version


def _trae_work_cn_installed() -> bool:
    if sys.platform not in {"win32", "darwin"}:
        return False
    probe = _trae_work_cn_product_metadata()
    return (
        probe.status == "matched"
        and probe.version is not None
        and probe.version >= _MINIMUM_VERSION
    )


def _trae_work_cn_skills_in_use() -> bool:
    if sys.platform not in {"win32", "darwin"}:
        return False
    configured = os.getenv("TRAE_WORK_CN_SKILLS_DIR")
    if (
        configured
        and configured.strip()
        and not _trae_work_cn_skills_path().parent.exists()
    ):
        return False
    return _trae_work_cn_installed()


def _trae_work_cn_skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("trae-work-cn") is not None
    except ShellError:
        return False


def _trae_work_cn_config_write_guard():
    if sys.platform not in {"win32", "darwin"}:
        raise ShellError(
            "unsupported_trae_work_cn_platform: only Windows and macOS Desktop "
            "builds are supported; nothing was written"
        )
    probe = _trae_work_cn_product_metadata()
    if probe.status == "identity-mismatch":
        raise ShellError(
            "trae_work_cn_identity_mismatch: the configured application root "
            "belongs to a different product; nothing was written"
        )
    version = probe.version
    if version is None:
        return "trae_work_cn_version_unknown"
    if version < _MINIMUM_VERSION:
        raise ShellError(
            "unsupported_trae_work_cn_version: TRAE Work CN is below the "
            "tested 0.1.48 candidate floor; nothing was written"
        )
    if version > _TESTED_VERSION:
        return "trae_work_cn_version_newer_than_tested"
    return None


TRAE_WORK_CN = AgentHostSpec(
    id="trae-work-cn",
    transport="stdio",
    launch_policy="desktop-python-v1",
    config_format="json",
    host_family="trae-work-cn",
    config_env="TRAE_WORK_CN_CONFIG",
    default_path=_trae_work_cn_config_path,
    config_scope="user-global",
    config_path=_trae_work_cn_config_path,
    config_renderer="json-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=lambda: _trae_work_cn_config_write_guard(),
    observed_client_aliases=frozenset({"trae"}),
    require_observed_identity=False,
    detection="file-or-parent",
    installation_probe=lambda: _trae_work_cn_installed(),
    json_include_type=False,
    json_include_cwd=True,
    skills_path=_trae_work_cn_skills_path,
    skills_global_path=_trae_work_cn_skills_path,
    skills_project_paths=(".trae/skills",),
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _trae_work_cn_skills_in_use(),
    skills_check_in_use=lambda: _trae_work_cn_skills_configured(),
    skill_route_name="trae-work-cn",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*trae(?:[\s_-]+solo[\s_-]+cn)\s*$",),
    local_display_tools=True,
    popup_followup=False,
    audit_stop_panel=True,
    popup_api_profile="legacy",
    launcher_capabilities=frozenset({"core-mcp", "local-display", "audit-stop-panel"}),
    doctor_capabilities=frozenset({"mcp-entry", "skills", "workspace-shadow"}),
    optional_features=frozenset({"local-display", "audit-stop-panel"}),
    onboarding_evidence=("skill",),
)

HOST_SPECS = (TRAE_WORK_CN,)
