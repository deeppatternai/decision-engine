"""Alibaba Qoder Desktop host declaration.

The retained Windows x64 evidence covers Qoder IDE 1.106.3. The installed
macOS Desktop bundle uses ``Contents/Resources/product.json`` for
``productId=qoder`` and ``Contents/Info.plist`` for its bundle identity and
version. Qoder Desktop 0.1.4 reads MCP entries from the top-level
``mcpServers`` field in ``~/.qoder/settings.json`` on macOS; the retained
Windows build continues to use ``~/.qoder/mcp.json``. It reports MCP client
identity ``mcphost``, accepts command/args/env, and ignores configured ``cwd``.
It rejects semicolons in arguments, so this adapter selects the checkout-bound
absolute bootstrap policy instead of inline Python. Product-specific paths and
version bounds remain in this adapter.
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


_MINIMUM_VERSION = (1, 106, 3)
_TESTED_VERSION = (1, 106, 3)
_MACOS_MINIMUM_VERSION = (0, 1, 3)
_MACOS_TESTED_VERSION = (0, 1, 4)


def _qoder_config_path() -> Path:
    # Qoder Desktop 0.1.4 on macOS reads settings.json; the retained Windows
    # host contract continues to use mcp.json.
    filename = "settings.json" if sys.platform == "darwin" else "mcp.json"
    return Path.home() / ".qoder" / filename


def _qoder_app_root() -> Path:
    configured = os.getenv("QODER_APP_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path("/Applications/Qoder.app")
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "Qoder"


def _qoder_skills_path() -> Path:
    configured = os.getenv("QODER_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".qoder" / "skills"


def _qoder_product_metadata():
    app_root = _qoder_app_root()
    if sys.platform == "darwin":
        product_probe = bounded_product_metadata(
            app_root / "Contents" / "Resources" / "product.json",
            expected_application_name="qoder",
            identity_field="productId",
            version_field=None,
        )
        if product_probe.status != "matched":
            return product_probe
        return bounded_plist_metadata(
            app_root / "Contents" / "Info.plist",
            expected_bundle_identifier="com.qoder.app",
        )
    relative = Path("resources") / "app" / "product.json"
    return bounded_product_metadata(
        app_root / relative,
        expected_application_name="qoder",
        version_field="version",
    )


def _qoder_version():
    return _qoder_product_metadata().version


def _qoder_installed() -> bool:
    if sys.platform not in {"win32", "darwin"}:
        return False
    probe = _qoder_product_metadata()
    minimum_version = (
        _MACOS_MINIMUM_VERSION if sys.platform == "darwin" else _MINIMUM_VERSION
    )
    return (
        probe.status == "matched"
        and probe.version is not None
        and probe.version >= minimum_version
    )


def _qoder_skills_in_use() -> bool:
    if sys.platform not in {"win32", "darwin"}:
        return False
    configured = os.getenv("QODER_SKILLS_DIR")
    if (
        configured
        and configured.strip()
        and not _qoder_skills_path().parent.exists()
    ):
        return False
    return _qoder_installed()


def _qoder_skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("qoder") is not None
    except ShellError:
        return False


def _qoder_config_write_guard():
    if sys.platform not in {"win32", "darwin"}:
        raise ShellError(
            "unsupported_qoder_platform: only Windows and macOS Desktop builds "
            "are supported; nothing was written"
        )
    probe = _qoder_product_metadata()
    if probe.status == "unavailable":
        raise ShellError(
            "qoder_not_installed: Qoder Desktop was not found at the configured "
            "application root; nothing was written"
        )
    if probe.status == "identity-mismatch":
        raise ShellError(
            "qoder_identity_mismatch: the configured application root belongs "
            "to a different product; nothing was written"
        )
    if probe.status != "matched" or probe.version is None:
        raise ShellError(
            "qoder_metadata_invalid: Qoder Desktop product metadata is malformed; "
            "nothing was written"
        )
    minimum_version = (
        _MACOS_MINIMUM_VERSION if sys.platform == "darwin" else _MINIMUM_VERSION
    )
    tested_version = (
        _MACOS_TESTED_VERSION if sys.platform == "darwin" else _TESTED_VERSION
    )
    if probe.version < minimum_version:
        version_label = ".".join(map(str, minimum_version))
        raise ShellError(
            "unsupported_qoder_version: Qoder Desktop is below the tested "
            f"{version_label} support floor; nothing was written"
        )
    if probe.version > tested_version:
        return "qoder_version_newer_than_tested"
    return None


def _qoder_json_config_transform(data: dict, entry: dict) -> bool:
    if sys.platform != "darwin":
        return False
    from installer.client_hosts.hosts import qoder_prompt_hook

    return qoder_prompt_hook.merge_prompt_hook_from_entry(data, entry)


QODER = AgentHostSpec(
    id="qoder",
    transport="stdio",
    launch_policy="absolute-bootstrap-v1",
    config_format="json",
    host_family="qoder",
    config_env="QODER_CONFIG",
    default_path=_qoder_config_path,
    config_scope="user-global",
    config_path=_qoder_config_path,
    config_renderer="json-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=lambda: _qoder_config_write_guard(),
    observed_client_aliases=frozenset(
        {"mcphost", "qoder-desktop-mcp-host"}
    ),
    require_observed_identity=True,
    detection="file-or-parent",
    installation_probe=lambda: _qoder_installed(),
    json_include_type=False,
    json_include_cwd=False,
    skills_path=_qoder_skills_path,
    skills_global_path=_qoder_skills_path,
    skills_project_paths=(".qoder/skills",),
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _qoder_skills_in_use(),
    skills_check_in_use=lambda: _qoder_skills_configured(),
    skill_route_name="qoder",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*qoder\s*$",),
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
    json_config_transform=_qoder_json_config_transform,
)

HOST_SPECS = (QODER,)
