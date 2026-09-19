"""Private, non-authoritative startup receipts and signed release cache.

Neither file grants permission to change the checkout.  A cached release is
authorized again against the current protected state before every application.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Optional

from . import (
    managed_install,
    release_acquisition,
    release_contract,
    update_coordination,
    update_transaction,
    updater,
    windows_security,
)


STAGED_RELEASE_RELATIVE_PATH = Path(".runtime") / "staged-release.json"
COMPLETION_RELATIVE_PATH = Path(".runtime") / "update-check.json"
_MAX_RECORD_BYTES = 160 * 1024
_COMPLETION_STATUSES = frozenset(updater._UPDATE_RESULTS)


class UpdateStagingError(updater.UpdateInspectionError):
    """A private startup record is invalid or cannot be persisted safely."""


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UpdateStagingError("startup record has a duplicate key")
        value[key] = item
    return value


def _read_record(root: Path, relative: Path) -> Optional[dict]:
    root = Path(root)
    runtime = root / ".runtime"
    path = root / relative
    if any(update_coordination._is_link_like(item) for item in (root, runtime, path)):
        raise UpdateStagingError("startup record path must not be linked")
    if not path.exists():
        return None
    try:
        directory = os.lstat(runtime)
        if not stat.S_ISDIR(directory.st_mode):
            raise UpdateStagingError("startup record directory is invalid")
        if os.name != "nt" and (
            directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) & 0o077
        ):
            raise UpdateStagingError("startup record directory is not private")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise UpdateStagingError("startup record is not a regular private file")
            if os.name != "nt":
                if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077:
                    raise UpdateStagingError("startup record is not private")
            else:
                windows_security.validate_private_mutation_acl(path)
            if before.st_size > _MAX_RECORD_BYTES:
                raise UpdateStagingError("startup record exceeds the size limit")
            raw = handle.read(_MAX_RECORD_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = os.lstat(path)
        if (
            len(raw) > _MAX_RECORD_BYTES
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or update_coordination._is_link_like(path)
        ):
            raise UpdateStagingError("startup record changed during read")
        loaded = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(loaded, dict):
            raise UpdateStagingError("startup record is not an object")
        return loaded
    except UpdateStagingError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError, MemoryError,
            windows_security.WindowsSecurityError) as exc:
        raise UpdateStagingError("startup record is invalid or unreadable") from exc


def _write_record(root: Path, relative: Path, payload: dict) -> None:
    try:
        update_transaction._atomic_write_private_json(
            Path(root) / relative, payload, max_bytes=_MAX_RECORD_BYTES
        )
    except (updater.UpdateInspectionError, OSError, ValueError) as exc:
        raise UpdateStagingError("could not persist startup record") from exc


def read_attempt(root: Path) -> Optional[tuple[str, str]]:
    """Return a well-formed receipt, including an unfinished attempt."""
    try:
        value = _read_record(root, COMPLETION_RELATIVE_PATH)
    except UpdateStagingError:
        return None
    if value is None or frozenset(value) != {"schema", "attempt_id", "status"}:
        return None
    attempt_id = value.get("attempt_id")
    status = value.get("status")
    if (type(value.get("schema")) is not int or value["schema"] != 1
            or not isinstance(attempt_id, str)
            or len(attempt_id) != 32 or any(c not in "0123456789abcdef" for c in attempt_id)
            or not isinstance(status, str)
            or status not in _COMPLETION_STATUSES | {"started"}):
        return None
    return attempt_id, status


def read_completion(root: Path) -> Optional[tuple[str, str]]:
    """Only a finished attempt can suppress a follower's check."""
    attempt = read_attempt(root)
    return attempt if attempt is not None and attempt[1] != "started" else None


def begin_attempt(root: Path) -> str:
    attempt_id = uuid.uuid4().hex
    _write_record(root, COMPLETION_RELATIVE_PATH, {
        "schema": 1, "attempt_id": attempt_id, "status": "started",
    })
    return attempt_id


def finish_attempt(root: Path, attempt_id: str, status: str) -> None:
    if (not isinstance(attempt_id, str) or len(attempt_id) != 32
            or any(c not in "0123456789abcdef" for c in attempt_id)
            or status not in _COMPLETION_STATUSES):
        raise UpdateStagingError("startup completion receipt is invalid")
    _write_record(root, COMPLETION_RELATIVE_PATH, {
        "schema": 1, "attempt_id": attempt_id, "status": status,
    })


def _authorize(acquired: release_acquisition.AcquiredRelease,
               state: updater.UpdateState, trusted_keys: Mapping) -> None:
    if not isinstance(acquired, release_acquisition.AcquiredRelease):
        raise UpdateStagingError("staged release has an invalid contract")
    if acquired.source not in release_acquisition.RELEASE_SOURCES:
        raise UpdateStagingError("staged release source is invalid")
    try:
        release_contract.authorize_release(
            acquired.manifest, acquired.signature, trusted_keys,
            expected_repository_id=managed_install.REPOSITORY_ID,
            expected_channel=state.channel,
            last_sequence=state.last_release_sequence,
            running_python=tuple(sys.version_info[:3]),
            last_commit=state.last_release_commit,
            last_manifest_sha256=state.last_manifest_sha256,
        )
    except (release_contract.ReleaseContractError, ValueError, TypeError) as exc:
        raise UpdateStagingError("staged release is not authorized") from exc


def stage_release(root: Path, acquired: release_acquisition.AcquiredRelease,
                  state: updater.UpdateState, trusted_keys: Mapping) -> None:
    _authorize(acquired, state, trusted_keys)
    _write_record(root, STAGED_RELEASE_RELATIVE_PATH, {
        "schema": 1,
        "source": acquired.source.name,
        "manifest": asdict(acquired.manifest),
        "signature": asdict(acquired.signature),
    })


def load_release(root: Path, state: updater.UpdateState,
                 trusted_keys: Mapping) -> Optional[release_acquisition.AcquiredRelease]:
    value = _read_record(root, STAGED_RELEASE_RELATIVE_PATH)
    if value is None:
        return None
    if (frozenset(value) != {"schema", "source", "manifest", "signature"}
            or type(value["schema"]) is not int or value["schema"] != 1):
        raise UpdateStagingError("staged release has an unexpected schema")
    if not isinstance(value["source"], str):
        raise UpdateStagingError("staged release source is invalid")
    source = release_acquisition._SOURCE_BY_NAME.get(value["source"])
    if source is None:
        raise UpdateStagingError("staged release source is invalid")
    try:
        manifest = release_contract.parse_release_manifest(value["manifest"])
        signature = release_contract.parse_release_signature(value["signature"])
    except (release_contract.ReleaseContractError, ValueError, TypeError) as exc:
        raise UpdateStagingError("staged release documents are invalid") from exc
    acquired = release_acquisition.AcquiredRelease(source, manifest, signature)
    _authorize(acquired, state, trusted_keys)
    return acquired


def clear_release(root: Path) -> None:
    path = Path(root) / STAGED_RELEASE_RELATIVE_PATH
    if update_coordination._is_link_like(path):
        raise UpdateStagingError("staged release path must not be linked")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise UpdateStagingError("could not clear staged release") from exc
