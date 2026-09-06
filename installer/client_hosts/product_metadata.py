"""Bounded metadata probes for packaged desktop agent hosts."""

from __future__ import annotations

import json
import os
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


MAX_PRODUCT_METADATA_BYTES = 1024 * 1024
MAX_VERSION_COMPONENT_DIGITS = 18


class _ObjectPairs(list):
    """Retain top-level duplicate keys while parsing bounded product JSON."""


@dataclass(frozen=True)
class ProductMetadataProbe:
    status: str
    version: Optional[Tuple[int, int, int]] = None


def _parse_semantic_version(value: object) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value)
    if match is None or any(
        len(part) > MAX_VERSION_COMPONENT_DIGITS for part in match.groups()
    ):
        return None
    return tuple(int(part) for part in match.groups())


def bounded_product_metadata(
    product: Path,
    *,
    expected_application_name: str,
    identity_field: str = "applicationName",
    version_field: Optional[str] = "appVersion",
) -> ProductMetadataProbe:
    """Read one bounded product identity/version record with explicit failure state.

    ``version_field=None`` is used for bundle formats that keep their version in
    a separate bounded metadata file while retaining the product identity check
    in ``product.json``.
    """

    try:
        if not product.is_file() or product.is_symlink():
            return ProductMetadataProbe("unavailable")
        with product.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_PRODUCT_METADATA_BYTES:
                return ProductMetadataProbe("unavailable")
            raw = handle.read(MAX_PRODUCT_METADATA_BYTES + 1)
        if len(raw) > MAX_PRODUCT_METADATA_BYTES:
            return ProductMetadataProbe("unavailable")
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        return ProductMetadataProbe("unavailable")

    try:
        root = json.loads(text, object_pairs_hook=_ObjectPairs)
    except (TypeError, ValueError, RecursionError):
        return ProductMetadataProbe("malformed")
    if not isinstance(root, _ObjectPairs):
        return ProductMetadataProbe("malformed")
    names = [value for key, value in root if key == identity_field]
    versions = [value for key, value in root if key == version_field]
    if len(names) != 1 or not isinstance(names[0], str):
        return ProductMetadataProbe("malformed")
    if names[0] != expected_application_name:
        return ProductMetadataProbe("identity-mismatch")
    if version_field is None:
        return ProductMetadataProbe("matched")
    if len(versions) != 1 or not isinstance(versions[0], str):
        return ProductMetadataProbe("malformed")
    version = _parse_semantic_version(versions[0])
    if version is None:
        return ProductMetadataProbe("malformed")
    return ProductMetadataProbe("matched", version)


def bounded_plist_metadata(
    info: Path,
    *,
    expected_bundle_identifier: str,
    version_field: str = "CFBundleShortVersionString",
) -> ProductMetadataProbe:
    """Read a bounded macOS bundle identity/version record."""

    try:
        if not info.is_file() or info.is_symlink():
            return ProductMetadataProbe("unavailable")
        with info.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_PRODUCT_METADATA_BYTES:
                return ProductMetadataProbe("unavailable")
            raw = handle.read(MAX_PRODUCT_METADATA_BYTES + 1)
        if len(raw) > MAX_PRODUCT_METADATA_BYTES:
            return ProductMetadataProbe("unavailable")
        root = plistlib.loads(raw)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return ProductMetadataProbe("unavailable")
    if not isinstance(root, dict):
        return ProductMetadataProbe("malformed")
    if root.get("CFBundleIdentifier") != expected_bundle_identifier:
        return ProductMetadataProbe("identity-mismatch")
    version = root.get(version_field)
    if not isinstance(version, str):
        return ProductMetadataProbe("malformed")
    parsed_version = _parse_semantic_version(version)
    if parsed_version is None:
        return ProductMetadataProbe("malformed")
    return ProductMetadataProbe("matched", parsed_version)


def bounded_plist_identifier(
    info: Path,
    *,
    expected_bundle_identifier: str,
) -> ProductMetadataProbe:
    """Read a bounded macOS bundle identifier record without requiring a version.

    Distinct from ``bounded_plist_metadata``: some bundles have no confirmed
    version-format contract, so this probe verifies ``CFBundleIdentifier``
    alone and never returns ``malformed`` for an absent/non-semver version.
    """

    try:
        if not info.is_file() or info.is_symlink():
            return ProductMetadataProbe("unavailable")
        with info.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_PRODUCT_METADATA_BYTES:
                return ProductMetadataProbe("unavailable")
            raw = handle.read(MAX_PRODUCT_METADATA_BYTES + 1)
        if len(raw) > MAX_PRODUCT_METADATA_BYTES:
            return ProductMetadataProbe("unavailable")
        root = plistlib.loads(raw)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return ProductMetadataProbe("unavailable")
    if not isinstance(root, dict):
        return ProductMetadataProbe("malformed")
    if root.get("CFBundleIdentifier") != expected_bundle_identifier:
        return ProductMetadataProbe("identity-mismatch")
    return ProductMetadataProbe("matched")


def bounded_product_version(
    product: Path,
    *,
    expected_application_name: str,
    version_field: str = "appVersion",
) -> Optional[Tuple[int, int, int]]:
    """Compatibility wrapper returning only an exactly matched semantic version."""

    return bounded_product_metadata(
        product,
        expected_application_name=expected_application_name,
        version_field=version_field,
    ).version
