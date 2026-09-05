"""Alibaba Qoder CN Desktop host declaration.

Qoder CN is a separate product from the international Qoder application. The
macOS Desktop bundle owns ``~/.qoder-cn/settings.json`` and identifies itself
with ``productId=qoder-cn`` plus bundle id ``com.qodercn.app``. Qoder CN IDE
uses a different bundle, version line, and ``mcp.json`` surface, so it is not
silently accepted by this Desktop adapter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.client_hosts.product_metadata import (
    bounded_plist_metadata,
    bounded_product_metadata,
)
from installer.config import ShellError


_MACOS_MINIMUM_VERSION = (0, 1, 4)
_MACOS_TESTED_VERSION = (0, 1, 4)


def _qoder_cn_config_path() -> Path:
    return Path.home() / ".qoder-cn" / "settings.json"


def _qoder_cn_app_root() -> Path:
    configured = os.getenv("QODER_CN_APP_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path("/Applications/Qoder CN.app")
    return Path("/Applications/Qoder CN.app")


def _qoder_cn_skills_path() -> Path:
    configured = os.getenv("QODER_CN_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".qoder-cn" / "skills"


def _qoder_cn_product_metadata():
    app_root = _qoder_cn_app_root()
    if sys.platform == "darwin":
        product_probe = bounded_product_metadata(
            app_root / "Contents" / "Resources" / "product.json",
            expected_application_name="qoder-cn",
            identity_field="productId",
            version_field=None,
        )
        if product_probe.status != "matched":
            return product_probe
        return bounded_plist_metadata(
            app_root / "Contents" / "Info.plist",
            expected_bundle_identifier="com.qodercn.app",
        )
    return bounded_product_metadata(
        app_root / "Contents" / "Resources" / "product.json",
        expected_application_name="qoder-cn",
        identity_field="productId",
        version_field=None,
    )


def _qoder_cn_version():
    return _qoder_cn_product_metadata().version


def _qoder_cn_installed() -> bool:
    if sys.platform != "darwin":
        return False
    probe = _qoder_cn_product_metadata()
    return (
        probe.status == "matched"
        and probe.version is not None
        and probe.version >= _MACOS_MINIMUM_VERSION
    )


def _qoder_cn_skills_in_use() -> bool:
    if sys.platform != "darwin":
        return False
    configured = os.getenv("QODER_CN_SKILLS_DIR")
    if (
        configured
        and configured.strip()
        and not _qoder_cn_skills_path().parent.exists()
    ):
        return False
    return _qoder_cn_installed()


def _qoder_cn_skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("qoder-cn") is not None
    except ShellError:
        return False


def _qoder_cn_config_write_guard():
    if sys.platform != "darwin":
        raise ShellError(
            "unsupported_qoder_cn_platform: only the macOS Qoder CN Desktop "
            "build is supported; nothing was written"
        )
    probe = _qoder_cn_product_metadata()
    if probe.status == "unavailable":
        raise ShellError(
            "qoder_cn_not_installed: Qoder CN Desktop was not found at the "
            "configured application root; nothing was written"
        )
    if probe.status == "identity-mismatch":
        raise ShellError(
            "qoder_cn_identity_mismatch: the configured application root "
            "belongs to a different product; nothing was written"
        )
    if probe.status != "matched" or probe.version is None:
        raise ShellError(
            "qoder_cn_metadata_invalid: Qoder CN Desktop product metadata is "
            "malformed; nothing was written"
        )
    if probe.version < _MACOS_MINIMUM_VERSION:
        version_label = ".".join(map(str, _MACOS_MINIMUM_VERSION))
        raise ShellError(
            "unsupported_qoder_cn_version: Qoder CN Desktop is below the "
            f"tested {version_label} support floor; nothing was written"
        )
    if probe.version > _MACOS_TESTED_VERSION:
        return "qoder_cn_version_newer_than_tested"
    return None


def _qoder_cn_json_config_transform(data: dict, entry: dict) -> bool:
    from installer.client_hosts.hosts import qoder_prompt_hook

    return qoder_prompt_hook.merge_prompt_hook_from_entry(data, entry)


QODER_CN = AgentHostSpec(
    id="qoder-cn",
    transport="stdio",
    launch_policy="absolute-bootstrap-v1",
    config_format="json",
    host_family="qoder-cn",
    config_env="QODER_CN_CONFIG",
    default_path=_qoder_cn_config_path,
    config_scope="user-global",
    config_path=_qoder_cn_config_path,
    config_renderer="json-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=lambda: _qoder_cn_config_write_guard(),
    observed_client_aliases=frozenset({"mcphost"}),
    require_observed_identity=True,
    detection="installation-probe",
    installation_probe=lambda: _qoder_cn_installed(),
    json_include_type=False,
    json_include_cwd=False,
    skills_path=_qoder_cn_skills_path,
    skills_global_path=_qoder_cn_skills_path,
    skills_project_paths=(".qoder/skills",),
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _qoder_cn_skills_in_use(),
    skills_check_in_use=lambda: _qoder_cn_skills_configured(),
    skill_route_name="qoder-cn",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*qoder(?:[\s_-]*cn)\s*$",),
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
    json_config_transform=_qoder_cn_json_config_transform,
    unverified_lite_stopper=True,
)

HOST_SPECS = (QODER_CN,)
