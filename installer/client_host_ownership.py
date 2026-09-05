"""Ownership evidence for managed client-host integrations.

This module validates and publishes sidecar records and computes canonical
managed-field hashes. Publication is a low-level primitive: production callers
must hold their host activation lock and journal the paired MCP transition.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from . import config, managed_install


RECORD_SCHEMA = 1
MANAGED_ENTRY_SCHEMA = 1
MANAGED_ENTRY_FIELDS_V1 = ("command", "args", "type", "cwd", "env")
_RECORD_KEYS = {
    "schema",
    "install_id",
    "client",
    "config_path",
    "server_name",
    "managed_entry_schema",
    "managed_fields_sha256",
    "skill_release_id",
    "skill_manifest_sha256",
}
_HOST_FAMILY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECORD_BYTES = 64 * 1024
_MAX_HOST_FAMILY_LENGTH = 64
_PUBLICATION_LOCK_HELD: ContextVar[bool] = ContextVar(
    "cursor_ownership_publication_lock_held",
    default=False,
)


class OwnershipError(config.ShellError):
    """Safe-to-display ownership evidence validation failure."""


@contextmanager
def _activation_publication_scope():
    """Mark the current context as holding the persistent activation lock."""

    token = _PUBLICATION_LOCK_HELD.set(True)
    try:
        yield
    finally:
        _PUBLICATION_LOCK_HELD.reset(token)


@dataclass(frozen=True)
class OwnershipRecord:
    install_id: str
    client: str
    config_path: Path
    server_name: str
    managed_fields_sha256: str
    skill_release_id: str
    skill_manifest_sha256: str


def _valid_host_family(host_family: Any) -> bool:
    return (
        isinstance(host_family, str)
        and len(host_family) <= _MAX_HOST_FAMILY_LENGTH
        and _HOST_FAMILY_RE.fullmatch(host_family) is not None
    )


def ownership_record_path(host_family: str) -> Path:
    if not _valid_host_family(host_family):
        raise OwnershipError("client host family is invalid")
    return managed_install.registration_path().with_name(
        "decision-engine-%s.json" % host_family
    )


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(Path(path).expanduser())))


def managed_fields_sha256(entry: Dict[str, Any], fields: Iterable[str]) -> str:
    if not isinstance(entry, dict):
        raise OwnershipError("managed MCP entry must be an object")
    names = tuple(fields)
    if any(not isinstance(name, str) or not name for name in names):
        raise OwnershipError("managed field names are invalid")
    if len(set(names)) != len(names):
        raise OwnershipError("managed field names contain duplicates")
    projection = {name: entry[name] for name in names if name in entry}
    try:
        canonical = json.dumps(
            projection,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise OwnershipError("managed MCP entry is not canonical JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


def managed_entry_sha256_v1(entry: Dict[str, Any]) -> str:
    """Hash exactly the managed fields defined by ownership schema v1."""

    return managed_fields_sha256(entry, MANAGED_ENTRY_FIELDS_V1)


def _paired_install_id(managed_root: Path) -> str:
    # managed_install is frozen for Cursor M2, so this read-only adapter wraps
    # its existing strict identity primitives instead of adding a new public
    # mutation-capable API there.
    try:
        canonical = managed_install.canonical_managed_root(managed_root)
        managed_install._require_fixed_managed_root(canonical)
        marker = managed_install._read_identity_json(
            managed_install.marker_path(canonical),
            managed_install._MARKER_KEYS,
        )
        registration = managed_install._read_identity_json(
            managed_install.registration_path(),
            managed_install._REGISTRATION_KEYS,
            require_private_parent=True,
        )
        marker_id = managed_install._validate_marker(marker)
        registration_id = managed_install._validate_registration(
            registration,
            canonical,
        )
    except managed_install.ManagedInstallError as exc:
        raise OwnershipError("managed install identity is invalid") from exc
    if marker_id != registration_id:
        raise OwnershipError("managed install identity IDs do not match")
    return marker_id


def _validated_label(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise OwnershipError("%s is invalid" % label)
    return value


def build_record(
    host_family: str,
    *,
    managed_root: Path,
    config_path: Path,
    server_name: str,
    managed_entry: Dict[str, Any],
    skill_release_id: str,
    skill_manifest_sha256: str,
) -> OwnershipRecord:
    """Build a schema-v1 record bound to the paired managed installation."""

    ownership_record_path(host_family)
    canonical_config = Path(
        os.path.abspath(os.path.normpath(str(Path(config_path).expanduser())))
    )
    if not canonical_config.is_absolute():
        raise OwnershipError("ownership record config path must be absolute")
    server = _validated_label(server_name, "ownership record server name")
    release_id = _validated_label(
        skill_release_id,
        "ownership record skill release ID",
    )
    if (
        not isinstance(skill_manifest_sha256, str)
        or not _SHA256_RE.fullmatch(skill_manifest_sha256)
    ):
        raise OwnershipError("ownership record skill manifest hash is invalid")
    return OwnershipRecord(
        install_id=_paired_install_id(managed_root),
        client=host_family,
        config_path=canonical_config,
        server_name=server,
        managed_fields_sha256=managed_entry_sha256_v1(managed_entry),
        skill_release_id=release_id,
        skill_manifest_sha256=skill_manifest_sha256,
    )


def record_payload(record: OwnershipRecord) -> Dict[str, Any]:
    if not isinstance(record, OwnershipRecord):
        raise OwnershipError("ownership record is invalid")
    return {
        "schema": RECORD_SCHEMA,
        "install_id": record.install_id,
        "client": record.client,
        "config_path": str(record.config_path),
        "server_name": record.server_name,
        "managed_entry_schema": MANAGED_ENTRY_SCHEMA,
        "managed_fields_sha256": record.managed_fields_sha256,
        "skill_release_id": record.skill_release_id,
        "skill_manifest_sha256": record.skill_manifest_sha256,
    }


def _render_record(record: OwnershipRecord) -> bytes:
    return (
        json.dumps(record_payload(record), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def record_sha256(record: OwnershipRecord) -> str:
    return hashlib.sha256(_render_record(record)).hexdigest()


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
        left.st_size,
        getattr(left, "st_mtime_ns", None),
        getattr(left, "st_file_attributes", 0),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
        right.st_size,
        getattr(right, "st_mtime_ns", None),
        getattr(right, "st_file_attributes", 0),
    )


def _record_snapshot_if_present(
    path: Path,
) -> Optional[tuple[bytes, os.stat_result]]:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError) as exc:
        raise OwnershipError("ownership record could not be inspected safely") from exc
    if managed_install._is_link_like(path) or not stat.S_ISREG(before.st_mode):
        raise OwnershipError("ownership record is not a private regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OwnershipError("ownership record could not be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_file(before, opened)
            or opened.st_size > _MAX_RECORD_BYTES
        ):
            raise OwnershipError("ownership record changed while being inspected")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_RECORD_BYTES + 1)
        after = os.fstat(fd)
        try:
            path_after = os.lstat(path)
        except OSError as exc:
            raise OwnershipError(
                "ownership record changed while being inspected"
            ) from exc
        if (
            len(raw) > _MAX_RECORD_BYTES
            or not _same_file(opened, after)
            or not _same_file(after, path_after)
        ):
            raise OwnershipError("ownership record changed while being inspected")
        return raw, after
    finally:
        os.close(fd)


def _record_bytes_if_present(path: Path) -> Optional[bytes]:
    snapshot = _record_snapshot_if_present(path)
    return snapshot[0] if snapshot is not None else None


def record_file_sha256_if_present(host_family: str) -> Optional[str]:
    raw = _record_bytes_if_present(ownership_record_path(host_family))
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _atomic_publish_record(
    path: Path,
    rendered: bytes,
    *,
    expected_exists: bool,
    expected_sha256: Optional[str],
) -> None:
    managed_install._reject_link_components(path.parent)
    managed_install._ensure_private_registration_directory(path.parent)
    if managed_install._is_link_like(path):
        raise OwnershipError("ownership record must not be a link")
    temp = path.with_name("%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd: Optional[int] = None
    replaced = False
    try:
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        current = _record_bytes_if_present(path)
        if (current is not None) != expected_exists:
            raise OwnershipError(
                "ownership record appeared or disappeared while being published"
            )
        current_sha256 = (
            hashlib.sha256(current).hexdigest()
            if current is not None
            else None
        )
        if current_sha256 != expected_sha256:
            raise OwnershipError("ownership record changed while being published")
        if os.name == "nt":
            from installer import windows_security

            windows_security.move_write_through(
                temp,
                path,
                replace_existing=True,
            )
        else:
            os.replace(temp, path)
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        replaced = True
        if os.name != "nt":
            path.chmod(0o600)
            managed_install._validate_private_posix_path(path, directory=False)
    except OwnershipError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise OwnershipError("ownership record could not be published") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if not replaced:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def publish_record(
    record: OwnershipRecord,
    *,
    managed_root: Path,
    expected_exists: bool,
    expected_sha256: Optional[str],
) -> str:
    """Publish one expected-state record under the caller's activation lock."""

    if not _PUBLICATION_LOCK_HELD.get():
        raise OwnershipError(
            "ownership record publication requires the host activation lock"
        )
    if type(expected_exists) is not bool:
        raise OwnershipError("ownership record expected state is invalid")
    if expected_exists:
        if (
            not isinstance(expected_sha256, str)
            or not _SHA256_RE.fullmatch(expected_sha256)
        ):
            raise OwnershipError("ownership record expected hash is invalid")
    elif expected_sha256 is not None:
        raise OwnershipError("absent ownership record cannot have an expected hash")
    expected_install_id = _paired_install_id(managed_root)
    if (
        not isinstance(record, OwnershipRecord)
        or record.install_id != expected_install_id
        or not _valid_host_family(record.client)
        or not record.config_path.is_absolute()
        or not _SHA256_RE.fullmatch(record.managed_fields_sha256)
        or not _SHA256_RE.fullmatch(record.skill_manifest_sha256)
    ):
        raise OwnershipError("ownership record publication binding is invalid")
    _validated_label(record.server_name, "ownership record server name")
    _validated_label(
        record.skill_release_id,
        "ownership record skill release ID",
    )
    rendered = _render_record(record)
    path = ownership_record_path(record.client)
    _atomic_publish_record(
        path,
        rendered,
        expected_exists=expected_exists,
        expected_sha256=expected_sha256,
    )
    persisted = read_record_if_present(
        record.client,
        managed_root=managed_root,
        config_path=record.config_path,
        server_name=record.server_name,
    )
    if persisted != record or _record_bytes_if_present(path) != rendered:
        raise OwnershipError("published ownership record verification failed")
    return hashlib.sha256(rendered).hexdigest()


def remove_record(
    host_family: str,
    *,
    managed_root: Path,
    expected_sha256: str,
    quarantine_path: Path,
) -> None:
    """Atomically quarantine and remove one exact host ownership record."""

    if not _PUBLICATION_LOCK_HELD.get():
        raise OwnershipError(
            "ownership record removal requires the host activation lock"
        )
    _paired_install_id(managed_root)
    if (
        not isinstance(expected_sha256, str)
        or not _SHA256_RE.fullmatch(expected_sha256)
    ):
        raise OwnershipError("ownership record expected hash is invalid")
    path = ownership_record_path(host_family)
    quarantine = Path(quarantine_path)
    try:
        managed_install._reject_link_components(quarantine.parent)
        if quarantine.exists() or managed_install._is_link_like(quarantine):
            if path.exists() or managed_install._is_link_like(path):
                raise OwnershipError(
                    "repair_required: ownership rollback has two live records"
                )
        else:
            snapshot = _record_snapshot_if_present(path)
            if snapshot is None:
                return
            current, current_identity = snapshot
            if hashlib.sha256(current).hexdigest() != expected_sha256:
                raise OwnershipError(
                    "repair_required: ownership record contains user changes"
                )
            from installer import windows_security

            windows_security.move_write_through(
                path,
                quarantine,
                replace_existing=False,
            )
            try:
                moved_identity = os.lstat(quarantine)
            except OSError as exc:
                raise OwnershipError(
                    "repair_required: ownership record move is unverifiable"
                ) from exc
            if not _same_file(current_identity, moved_identity):
                if not (path.exists() or managed_install._is_link_like(path)):
                    windows_security.move_write_through(
                        quarantine,
                        path,
                        replace_existing=False,
                    )
                raise OwnershipError(
                    "repair_required: ownership record changed during removal"
                )
            if os.name != "nt":
                for parent in {path.parent, quarantine.parent}:
                    directory_fd = os.open(
                        parent,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        raw = _record_bytes_if_present(quarantine)
        if (
            raw is None
            or hashlib.sha256(raw).hexdigest() != expected_sha256
        ):
            if not (path.exists() or managed_install._is_link_like(path)):
                from installer import windows_security

                windows_security.move_write_through(
                    quarantine,
                    path,
                    replace_existing=False,
                )
            raise OwnershipError(
                "repair_required: ownership record contains user changes"
            )
        quarantine.unlink()
        if os.name != "nt":
            directory_fd = os.open(
                quarantine.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OwnershipError:
        raise
    except managed_install.ManagedInstallError as exc:
        raise OwnershipError("ownership record quarantine path is unsafe") from exc
    except (OSError, RuntimeError) as exc:
        raise OwnershipError(
            "ownership record could not be removed during recovery"
        ) from exc
    if (
        _record_bytes_if_present(path) is not None
        or _record_bytes_if_present(quarantine) is not None
    ):
        raise OwnershipError("ownership record removal verification failed")


def remove_initial_record(
    *,
    managed_root: Path,
    expected_sha256: str,
    quarantine_path: Path,
) -> None:
    """Compatibility wrapper for the original Cursor initial transaction."""

    remove_record(
        "cursor",
        managed_root=managed_root,
        expected_sha256=expected_sha256,
        quarantine_path=quarantine_path,
    )


def read_record_if_present(
    host_family: str,
    *,
    managed_root: Path,
    config_path: Path,
    server_name: str,
) -> Optional[OwnershipRecord]:
    path = ownership_record_path(host_family)
    if not path.exists() and not managed_install._is_link_like(path):
        return None
    try:
        payload = managed_install._read_identity_json(
            path,
            _RECORD_KEYS,
            require_private_parent=True,
        )
    except managed_install.ManagedInstallError as exc:
        raise OwnershipError("client-host ownership record is invalid") from exc

    if type(payload.get("schema")) is not int or payload["schema"] != RECORD_SCHEMA:
        raise OwnershipError("client-host ownership record schema is unsupported")
    expected_install_id = _paired_install_id(managed_root)
    if payload.get("install_id") != expected_install_id:
        raise OwnershipError("ownership record install ID does not match")
    if payload.get("client") != host_family:
        raise OwnershipError("ownership record client does not match")
    recorded_config = payload.get("config_path")
    if not isinstance(recorded_config, str) or not Path(recorded_config).is_absolute():
        raise OwnershipError("ownership record config path must be absolute")
    if _path_key(Path(recorded_config)) != _path_key(config_path):
        raise OwnershipError("ownership record config path does not match")
    if payload.get("server_name") != server_name:
        raise OwnershipError("ownership record server name does not match")
    if type(payload.get("managed_entry_schema")) is not int or (
        payload["managed_entry_schema"] != MANAGED_ENTRY_SCHEMA
    ):
        raise OwnershipError("ownership record managed entry schema is unsupported")
    managed_hash = payload.get("managed_fields_sha256")
    skill_hash = payload.get("skill_manifest_sha256")
    if not isinstance(managed_hash, str) or not _SHA256_RE.fullmatch(managed_hash):
        raise OwnershipError("ownership record managed fields hash is invalid")
    if not isinstance(skill_hash, str) or not _SHA256_RE.fullmatch(skill_hash):
        raise OwnershipError("ownership record skill manifest hash is invalid")

    return OwnershipRecord(
        install_id=expected_install_id,
        client=host_family,
        config_path=Path(recorded_config),
        server_name=server_name,
        managed_fields_sha256=managed_hash,
        skill_release_id=_validated_label(
            payload.get("skill_release_id"),
            "ownership record skill release ID",
        ),
        skill_manifest_sha256=skill_hash,
    )
