"""TRAE international desktop host declaration for macOS."""

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


_MINIMUM_VERSION = (3, 5, 81)
_TESTED_VERSION = (3, 5, 81)


def _config_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Trae"
        / "User"
        / "mcp.json"
    )


def _app_root() -> Path:
    configured = os.getenv("TRAE_APP_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path("/Applications/Trae.app")


def _skills_path() -> Path:
    configured = os.getenv("TRAE_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".trae" / "skills"


def _product_metadata() -> ProductMetadataProbe:
    root = _app_root()
    product = bounded_product_metadata(
        root / "Contents" / "Resources" / "app" / "product.json",
        expected_application_name="trae",
    )
    if product.status != "matched" or product.version is None:
        return product
    bundle = bounded_plist_metadata(
        root / "Contents" / "Info.plist",
        expected_bundle_identifier="com.trae.app",
    )
    if bundle.status != "matched" or bundle.version != product.version:
        return ProductMetadataProbe(
            "identity-mismatch"
            if bundle.status == "identity-mismatch"
            else "malformed"
        )
    return product


def _version():
    return _product_metadata().version


def _installed() -> bool:
    if sys.platform != "darwin":
        return False
    probe = _product_metadata()
    return (
        probe.status == "matched"
        and probe.version is not None
        and probe.version >= _MINIMUM_VERSION
    )


def _skills_in_use() -> bool:
    if sys.platform != "darwin":
        return False
    configured = os.getenv("TRAE_SKILLS_DIR")
    if configured and configured.strip() and not _skills_path().parent.exists():
        return False
    return _installed()


def _skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("trae") is not None
    except ShellError:
        return False


def _config_write_guard():
    if sys.platform != "darwin":
        raise ShellError(
            "unsupported_trae_platform: only the macOS Trae Desktop build is "
            "supported; nothing was written"
        )
    probe = _product_metadata()
    if probe.status == "unavailable":
        raise ShellError(
            "trae_not_installed: Trae Desktop was not found at the configured "
            "application root; nothing was written"
        )
    if probe.status == "identity-mismatch":
        raise ShellError(
            "trae_identity_mismatch: the configured application root belongs "
            "to a different product; nothing was written"
        )
    if probe.status != "matched" or probe.version is None:
        raise ShellError(
            "trae_metadata_invalid: Trae Desktop product metadata is malformed; "
            "nothing was written"
        )
    if probe.version < _MINIMUM_VERSION:
        raise ShellError(
            "unsupported_trae_version: Trae Desktop is below the tested "
            "3.5.81 support floor; nothing was written"
        )
    if probe.version > _TESTED_VERSION:
        return "trae_version_newer_than_tested"
    return None


TRAE = AgentHostSpec(
    id="trae",
    transport="stdio",
    launch_policy="desktop-python-v1",
    config_format="json",
    host_family="trae",
    config_env="TRAE_CONFIG",
    default_path=_config_path,
    config_scope="user-global",
    config_path=_config_path,
    config_renderer="json-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=lambda: _config_write_guard(),
    observed_client_aliases=frozenset({"trae"}),
    require_observed_identity=False,
    detection="installation-probe",
    installation_probe=lambda: _installed(),
    json_include_type=False,
    json_include_cwd=True,
    skills_path=_skills_path,
    skills_global_path=_skills_path,
    skills_project_paths=(".trae/skills",),
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _skills_in_use(),
    skills_check_in_use=lambda: _skills_configured(),
    skill_route_name="trae",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*trae\s*$",),
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
)

HOST_SPECS = (TRAE,)
