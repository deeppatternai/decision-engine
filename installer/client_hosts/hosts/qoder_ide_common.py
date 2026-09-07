"""Shared implementation for independently identified macOS Qoder IDE hosts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from installer.client_hosts.product_metadata import (
    ProductMetadataProbe,
    bounded_plist_metadata,
    bounded_product_metadata,
)
from installer.config import ShellError


MINIMUM_VERSION = (1, 106, 3)
TESTED_VERSION = (1, 106, 3)


def configured_path(environment_name: str, default: Path) -> Path:
    configured = os.getenv(environment_name)
    if configured and configured.strip():
        return Path(configured).expanduser()
    return default


def product_metadata(
    app_root: Path,
    *,
    application_name: str,
    bundle_identifier: str,
) -> ProductMetadataProbe:
    product = bounded_product_metadata(
        app_root / "Contents" / "Resources" / "app" / "product.json",
        expected_application_name=application_name,
        version_field="version",
    )
    if product.status != "matched":
        return product
    bundle = bounded_plist_metadata(
        app_root / "Contents" / "Info.plist",
        expected_bundle_identifier=bundle_identifier,
    )
    return product if bundle.status == "matched" else bundle


def installed(probe: ProductMetadataProbe, *, platform: str) -> bool:
    return (
        platform == "darwin"
        and probe.status == "matched"
        and probe.version is not None
        and probe.version >= MINIMUM_VERSION
    )


def config_write_guard(
    probe: ProductMetadataProbe,
    *,
    platform: str,
    error_prefix: str,
    product_label: str,
) -> str | None:
    if platform != "darwin":
        raise ShellError(
            f"unsupported_{error_prefix}_platform: only the verified macOS "
            f"{product_label} build is supported; nothing was written"
        )
    if probe.status == "unavailable":
        raise ShellError(
            f"{error_prefix}_not_installed: {product_label} was not found at "
            "the configured application root; nothing was written"
        )
    if probe.status == "identity-mismatch":
        raise ShellError(
            f"{error_prefix}_identity_mismatch: the configured application "
            "root belongs to a different product; nothing was written"
        )
    if probe.status != "matched" or probe.version is None:
        raise ShellError(
            f"{error_prefix}_metadata_invalid: {product_label} metadata is "
            "malformed; nothing was written"
        )
    if probe.version < MINIMUM_VERSION:
        floor = ".".join(map(str, MINIMUM_VERSION))
        raise ShellError(
            f"unsupported_{error_prefix}_version: {product_label} is below "
            f"the tested {floor} support floor; nothing was written"
        )
    if probe.version > TESTED_VERSION:
        return f"{error_prefix}_version_newer_than_tested"
    return None


def install_prompt_hook(
    entry: dict[str, Any],
    dry_run: bool,
    *,
    settings_path: Path,
) -> dict[str, object]:
    from installer.client_hosts.hosts import qoder_prompt_hook

    command = entry.get("command")
    args = entry.get("args")
    if (
        not isinstance(command, str)
        or not command.strip()
        or not isinstance(args, list)
        or len(args) < 2
        or not isinstance(args[1], str)
        or not Path(args[1]).is_absolute()
    ):
        raise ShellError("rendered Qoder IDE entry cannot identify its DE hook runtime")
    return qoder_prompt_hook.install_hook(
        settings_path,
        de_root=Path(args[1]),
        python_executable=command,
        dry_run=dry_run,
    )
