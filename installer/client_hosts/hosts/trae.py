"""TRAE international desktop host declaration for Windows and macOS."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.client_hosts.product_metadata import (
    ProductMetadataProbe,
    bounded_plist_metadata,
    bounded_product_metadata,
)
from installer.config import ShellError


_MACOS_MINIMUM_VERSION = (3, 5, 81)
_MACOS_TESTED_VERSION = (3, 5, 81)
_WINDOWS_MINIMUM_VERSION = (3, 5, 81)
_WINDOWS_TESTED_VERSION = (3, 5, 81)
_SUPPORTED_PLATFORMS = {"darwin", "win32"}
_WINDOWS_PRODUCT_METADATA = Path("resources") / "app" / "product.json"


def _minimum_version() -> tuple[int, int, int]:
    return (
        _WINDOWS_MINIMUM_VERSION
        if sys.platform == "win32"
        else _MACOS_MINIMUM_VERSION
    )


def _tested_version() -> tuple[int, int, int]:
    return (
        _WINDOWS_TESTED_VERSION
        if sys.platform == "win32"
        else _MACOS_TESTED_VERSION
    )


def _config_path() -> Path:
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Trae" / "User" / "mcp.json"
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Trae"
        / "User"
        / "mcp.json"
    )


def _windows_default_app_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    local = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    roots.append(Path(local) / "Programs" / "Trae")
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        configured = os.getenv(env_name)
        if configured and configured.strip():
            roots.append(Path(configured) / "Trae")
    return tuple(roots)


def _windows_product_metadata(root: Path) -> ProductMetadataProbe:
    return bounded_product_metadata(
        root / _WINDOWS_PRODUCT_METADATA,
        expected_application_name="trae",
    )


def _windows_app_root_is_supported(root: Path) -> bool:
    probe = _windows_product_metadata(root)
    return (
        probe.status == "matched"
        and probe.version is not None
        and probe.version >= _WINDOWS_MINIMUM_VERSION
    )


def _windows_cli_app_root() -> Path | None:
    candidate = shutil.which("trae")
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    for depth, parent in enumerate(path.parents):
        if depth > 3:
            break
        if _windows_app_root_is_supported(parent):
            return parent
    return None


def _app_root() -> Path:
    configured = os.getenv("TRAE_APP_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()
    if sys.platform == "win32":
        cli_root = _windows_cli_app_root()
        if cli_root is not None:
            return cli_root
        default_roots = _windows_default_app_roots()
        for candidate in default_roots:
            if _windows_app_root_is_supported(candidate):
                return candidate
        return default_roots[0]
    return Path("/Applications/Trae.app")


def _skills_path() -> Path:
    configured = os.getenv("TRAE_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".trae" / "skills"


def _product_metadata() -> ProductMetadataProbe:
    root = _app_root()
    if sys.platform == "win32":
        return _windows_product_metadata(root)
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
    if sys.platform not in _SUPPORTED_PLATFORMS:
        return False
    probe = _product_metadata()
    return (
        probe.status == "matched"
        and probe.version is not None
        and probe.version >= _minimum_version()
    )


def _skills_in_use() -> bool:
    if sys.platform not in _SUPPORTED_PLATFORMS:
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
    if sys.platform not in _SUPPORTED_PLATFORMS:
        raise ShellError(
            "unsupported_trae_platform: only Windows and macOS Trae Desktop "
            "builds are supported; nothing was written"
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
    minimum = _minimum_version()
    tested = _tested_version()
    if probe.version < minimum:
        version_label = ".".join(map(str, minimum))
        raise ShellError(
            "unsupported_trae_version: Trae Desktop is below the tested "
            f"{version_label} support floor; nothing was written"
        )
    if probe.version > tested:
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
    supported_platforms=frozenset(_SUPPORTED_PLATFORMS),
)

HOST_SPECS = (TRAE,)
