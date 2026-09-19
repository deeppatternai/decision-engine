"""Crash-recoverable application of an already-present signed release.

This module performs no network access and is intentionally dormant until the
launcher migration writes the protocol-ready marker.  The public operation
always acquires the install transaction lock, rejects live shim leases, repeats
the complete PR3 inspection under that lock, persists recovery plus a
write-ahead journal, then resets, smokes, commits state or rolls back.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, Mapping, Optional, Tuple, Union

from . import managed_install, release_contract, update_coordination, updater, windows_security


PROTOCOL_READY_RELATIVE_PATH = Path(".runtime") / "update-protocol.json"
JOURNAL_RELATIVE_PATH = update_coordination.UPDATE_JOURNAL_RELATIVE_PATH
RECOVERY_RELATIVE_PATH = Path(".runtime") / "recovery"
_PROTOCOL_KEYS = {"schema", "launcher_protocol"}
_JOURNAL_V1_KEYS = {
    "schema",
    "transaction_id",
    "phase",
    "previous_commit",
    "previous_release_sequence",
    "previous_manifest_sha256",
    "previous_version",
    "target_commit",
    "target_version",
    "release_sequence",
    "manifest_sha256",
    "recovery_relative_path",
}
_JOURNAL_KEYS = _JOURNAL_V1_KEYS | {
    "first_error_code",
    "failure_phase",
    "retry_action",
    "retry_count",
}
_JOURNAL_PHASES = {
    "prepared",
    "reset_started",
    "candidate_applied",
    "smoke_started",
    "rollback_started",
    "retry_pending",
    "repair_required",
}
_TRANSACTION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_MAX_CONTROL_BYTES = 128 * 1024
_MAX_RECOVERY_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_RECOVERY_BYTES = 256 * 1024 * 1024
_MAX_RECOVERY_PATHS = 10_000
_RESET_ATTEMPTS = 3
_RESET_TIMEOUT_SECONDS = 25.0
_SMOKE_TIMEOUT_SECONDS = 10.0


class UpdateTransactionError(updater.UpdateInspectionError):
    """A safe-to-display transaction refusal."""


class GitMutationError(UpdateTransactionError):
    """A bounded Git reset did not establish the requested commit."""


class CandidateSmokeError(UpdateTransactionError):
    """The fixed offline candidate smoke did not pass."""


class RecoveryStateChanged(UpdateTransactionError):
    """The captured rollback inputs no longer describe the live checkout."""


@dataclass(frozen=True)
class UpdateResult:
    status: str
    previous_commit: Optional[str]
    target_commit: Optional[str]
    transaction_id: Optional[str]
    recovery_path: Optional[str]
    error_code: Optional[str] = None
    blockers: Tuple[update_coordination.LiveShimSession, ...] = ()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UpdateTransactionError("transaction JSON contains a duplicate key")
        value[key] = item
    return value


def _ensure_private_directory(path: Path) -> None:
    update_coordination._ensure_private_directory(path)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt" or not getattr(os, "O_DIRECTORY", 0):
        return
    fd = None
    try:
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(fd)
    except OSError as exc:
        raise UpdateTransactionError("could not make transaction metadata durable") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _fsync_directory_chain(start: Path, stop: Path) -> None:
    """Flush each directory from start through stop, inclusive, on POSIX."""

    if os.name == "nt" or not getattr(os, "O_DIRECTORY", 0):
        return
    current = start
    while True:
        fd = None
        try:
            fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(fd)
        except OSError as exc:
            raise UpdateTransactionError("could not make recovery directories durable") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if current == stop:
            return
        if current.parent == current:
            raise UpdateTransactionError("recovery directory chain escapes its root")
        current = current.parent


def _atomic_write_private_json(
    path: Path, payload: dict, *, max_bytes: int = _MAX_CONTROL_BYTES
) -> None:
    _ensure_private_directory(path.parent)
    if update_coordination._is_link_like(path):
        raise UpdateTransactionError("transaction metadata must not be a link or reparse point")
    rendered = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(rendered) > max_bytes:
        raise UpdateTransactionError("transaction metadata exceeds the size limit")
    temporary = path.with_name("%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = None
    try:
        fd = os.open(temporary, flags, 0o600)
        os.set_inheritable(fd, False)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            windows_security.move_write_through(
                temporary, path, replace_existing=True
            )
        except windows_security.WindowsSecurityError as exc:
            raise UpdateTransactionError("could not durably replace transaction metadata") from exc
        if os.name != "nt":
            path.chmod(0o600)
        # Windows' CRT rejects fsync() on a read-only descriptor with EBADF.
        # The writable temporary was already flushed before ReplaceFile; POSIX
        # additionally flushes the renamed inode and parent directory here.
        if os.name != "nt":
            final_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(final_fd)
            finally:
                os.close(final_fd)
        else:
            try:
                windows_security.validate_private_mutation_acl(path)
            except windows_security.WindowsSecurityError as exc:
                raise UpdateTransactionError("transaction metadata ACL is not private") from exc
        _fsync_parent(path)
    except UpdateTransactionError:
        raise
    except OSError as exc:
        raise UpdateTransactionError("could not persist transaction metadata") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_private_json(
    path: Path,
    *,
    expected_keys: set,
    compatible_key_sets: Tuple[set, ...] = (),
    max_bytes: int = _MAX_CONTROL_BYTES,
) -> dict:
    if update_coordination._is_link_like(path):
        raise UpdateTransactionError("transaction metadata must not be a link or reparse point")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UpdateTransactionError("transaction metadata is not a private regular file")
            if os.name != "nt":
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise UpdateTransactionError("transaction metadata has the wrong owner")
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise UpdateTransactionError("transaction metadata permissions are too broad")
            else:
                try:
                    windows_security.validate_private_mutation_acl(path)
                except windows_security.WindowsSecurityError as exc:
                    raise UpdateTransactionError("transaction metadata ACL is not private") from exc
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise UpdateTransactionError("transaction metadata exceeds the size limit")
        loaded = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_without_duplicates)
    except UpdateTransactionError:
        raise
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise UpdateTransactionError("transaction metadata is invalid or unreadable") from exc
    loaded_keys = set(loaded) if isinstance(loaded, dict) else set()
    if not isinstance(loaded, dict) or (
        loaded_keys != expected_keys
        and all(loaded_keys != keys for keys in compatible_key_sets)
    ):
        raise UpdateTransactionError("transaction metadata has an unexpected schema")
    return loaded


def _remove_durable(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UpdateTransactionError("could not remove completed transaction journal") from exc
    _fsync_parent(path)


def _write_protocol_ready(root: Path) -> None:
    """Migration hook for PR5; writing it early does not change MCP wiring."""

    canonical = managed_install.canonical_managed_root(root)
    if os.name == "nt":
        try:
            windows_security.validate_private_mutation_acl(canonical)
        except windows_security.WindowsSecurityError as exc:
            raise UpdateTransactionError("managed root ACL is not private") from exc
    with update_coordination.install_transaction(
        canonical, timeout_seconds=5.0
    ) as transaction:
        _write_protocol_ready_locked(canonical, transaction)


def _write_protocol_ready_locked(
    root: Path, transaction: update_coordination.InstallTransaction
) -> None:
    """Publish readiness inside an already-held PR5 migration transaction."""

    canonical = managed_install.canonical_managed_root(root)
    expected = canonical / update_coordination.INSTALL_LOCK_RELATIVE_PATH
    transaction._validate(expected)
    with _PinnedRoot(canonical):
        _validate_windows_update_boundary(canonical)
        _atomic_write_private_json(
            canonical / PROTOCOL_READY_RELATIVE_PATH,
            {"schema": 1, "launcher_protocol": 1},
        )


def _require_protocol_ready(root: Path) -> None:
    try:
        value = _read_private_json(root / PROTOCOL_READY_RELATIVE_PATH, expected_keys=_PROTOCOL_KEYS)
    except FileNotFoundError as exc:
        raise UpdateTransactionError("launcher protocol is not ready for managed mutation") from exc
    except UpdateTransactionError as exc:
        raise UpdateTransactionError("launcher protocol is not ready for managed mutation") from exc
    if value != {"schema": 1, "launcher_protocol": 1}:
        raise UpdateTransactionError("launcher protocol is not ready for managed mutation")


def _validate_windows_update_boundary(root: Path) -> None:
    if os.name != "nt":
        return
    protected = (
        root,
        root / ".git",
        root / ".git" / "config",
        root / managed_install.MARKER_FILENAME,
        root / ".runtime",
        root / updater.UPDATE_STATE_RELATIVE_PATH,
        root / PROTOCOL_READY_RELATIVE_PATH,
    )
    for path in protected:
        try:
            exists = path.exists()
        except OSError as exc:
            raise UpdateTransactionError("could not inspect a protected Windows path") from exc
        if not exists:
            continue
        if update_coordination._is_link_like(path):
            raise UpdateTransactionError("protected Windows path is a link or reparse point")
        try:
            windows_security.validate_private_mutation_acl(path)
        except windows_security.WindowsSecurityError as exc:
            raise UpdateTransactionError("protected Windows path ACL is not private") from exc


class _PinnedRoot:
    def __init__(self, root: Path) -> None:
        self.root = managed_install.canonical_managed_root(root)
        self._fd = None
        self._windows_handle = None
        self._identity = None

    def __enter__(self):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt":
            try:
                windows_security.validate_private_mutation_acl(self.root)
                self._windows_handle = windows_security.PinnedWindowsDirectory(self.root)
                self._windows_handle.__enter__()
                self._identity = self._snapshot()
                self.validate()
            except windows_security.WindowsSecurityError as exc:
                if self._windows_handle is not None:
                    self._windows_handle.__exit__(None, None, None)
                    self._windows_handle = None
                raise UpdateTransactionError("could not pin private managed Windows root") from exc
            return self
        try:
            self._fd = os.open(self.root, flags)
            os.set_inheritable(self._fd, False)
            info = os.fstat(self._fd)
        except OSError as exc:
            raise UpdateTransactionError("could not pin managed root") from exc
        self._identity = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        self.validate()
        return self

    def _snapshot(self):
        try:
            info = os.lstat(self.root)
        except OSError as exc:
            raise UpdateTransactionError("could not inspect managed root boundary") from exc
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) \
                or bool(reparse and attributes & reparse):
            raise UpdateTransactionError("managed root boundary changed type")
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))

    def validate(self) -> None:
        current = self._snapshot()
        if current != self._identity:
            raise UpdateTransactionError("managed root boundary changed during update")
        if self._fd is not None:
            opened = os.fstat(self._fd)
            if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != self._identity:
                raise UpdateTransactionError("pinned managed root changed during update")
        if self._windows_handle is not None:
            try:
                self._windows_handle.validate()
            except windows_security.WindowsSecurityError as exc:
                raise UpdateTransactionError("pinned managed Windows root changed") from exc
        if managed_install._path_key(managed_install.canonical_managed_root(self.root)) \
                != managed_install._path_key(self.root):
            raise UpdateTransactionError("managed root boundary is no longer canonical")

    def __exit__(self, _type, _value, _traceback):
        if self._fd is not None:
            os.close(self._fd)
        if self._windows_handle is not None:
            self._windows_handle.__exit__(_type, _value, _traceback)


def _state_payload(
    state: updater.UpdateState,
    *,
    result: str,
    previous_commit: Optional[str],
    target_commit: Optional[str],
    transaction_id: Optional[str],
    error_code: Optional[str],
    sequence: Optional[int] = None,
    release_commit: Optional[str] = None,
    manifest_sha256: Optional[str] = None,
    version: Optional[str] = None,
    running_commit: Optional[str] = None,
    running_version: Optional[str] = None,
    source: Optional[str] = None,
) -> dict:
    return {
        "schema": updater.STATE_SCHEMA_VERSION,
        "channel": managed_install.CHANNEL,
        "last_release_sequence": state.last_release_sequence if sequence is None else sequence,
        "last_release_commit": state.last_release_commit if release_commit is None else release_commit,
        "last_manifest_sha256": (
            state.last_manifest_sha256 if manifest_sha256 is None else manifest_sha256
        ),
        "last_version": state.last_version if version is None else version,
        "source": state.source if source is None else source,
        "last_attempt_at": _utc_now(),
        "last_result": result,
        "previous_commit": previous_commit,
        "target_commit": target_commit,
        "running_commit": running_commit,
        "running_version": running_version,
        "error_code": error_code,
        "transaction_id": transaction_id,
    }


def _write_state(root: Path, payload: dict) -> None:
    _atomic_write_private_json(root / updater.UPDATE_STATE_RELATIVE_PATH, payload)
    updater._read_update_state(root)


def _journal_path(root: Path) -> Path:
    return root / JOURNAL_RELATIVE_PATH


def _validate_journal(value: dict) -> dict:
    schema = value.get("schema")
    expected_keys = _JOURNAL_V1_KEYS if schema == 1 else _JOURNAL_KEYS
    if type(schema) is not int or schema not in {1, 2} or set(value) != expected_keys:
        raise UpdateTransactionError("update journal schema is not supported")
    transaction_id = value.get("transaction_id")
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise UpdateTransactionError("update journal transaction ID is invalid")
    if value.get("phase") not in _JOURNAL_PHASES:
        raise UpdateTransactionError("update journal phase is invalid")
    for key in ("previous_commit", "target_commit"):
        item = value.get(key)
        if not isinstance(item, str) or not updater._COMMIT_RE.fullmatch(item):
            raise UpdateTransactionError("update journal commit is invalid")
    for key in ("previous_manifest_sha256", "manifest_sha256"):
        item = value.get(key)
        if not isinstance(item, str) or not updater._DIGEST_RE.fullmatch(item):
            raise UpdateTransactionError("update journal digest is invalid")
    for key in ("previous_version", "target_version"):
        item = value.get(key)
        if not isinstance(item, str) or not updater._VERSION_RE.fullmatch(item):
            raise UpdateTransactionError("update journal version is invalid")
    for key in ("previous_release_sequence", "release_sequence"):
        item = value.get(key)
        if type(item) is not int or item < 1:
            raise UpdateTransactionError("update journal sequence is invalid")
    expected_recovery = (RECOVERY_RELATIVE_PATH / transaction_id).as_posix()
    if value.get("recovery_relative_path") != expected_recovery:
        raise UpdateTransactionError("update journal recovery path is invalid")
    if schema == 2:
        first_error = value.get("first_error_code")
        if first_error is not None and (
            not isinstance(first_error, str)
            or not first_error
            or len(first_error) > 128
        ):
            raise UpdateTransactionError("update journal first error is invalid")
        failure_phase = value.get("failure_phase")
        if failure_phase is not None and failure_phase not in _JOURNAL_PHASES:
            raise UpdateTransactionError("update journal failure phase is invalid")
        if value.get("retry_action") not in {None, "rollback", "forward"}:
            raise UpdateTransactionError("update journal retry action is invalid")
        retry_count = value.get("retry_count")
        if type(retry_count) is not int or not 0 <= retry_count <= 1_000_000:
            raise UpdateTransactionError("update journal retry count is invalid")
    return value


def _read_journal(root: Path) -> Optional[dict]:
    path = _journal_path(root)
    try:
        value = _read_private_json(
            path,
            expected_keys=_JOURNAL_KEYS,
            compatible_key_sets=(_JOURNAL_V1_KEYS,),
        )
    except FileNotFoundError:
        return None
    return _validate_journal(value)


def _write_journal(root: Path, value: dict) -> None:
    _validate_journal(value)
    _atomic_write_private_json(_journal_path(root), value)


def _journal_for(
    transaction_id: str,
    state: updater.UpdateState,
    inspection: updater.UpdateInspection,
    *,
    phase: str,
) -> dict:
    return {
        "schema": 2,
        "transaction_id": transaction_id,
        "phase": phase,
        "previous_commit": state.last_release_commit,
        "previous_release_sequence": state.last_release_sequence,
        "previous_manifest_sha256": state.last_manifest_sha256,
        "previous_version": state.last_version,
        "target_commit": inspection.target_commit,
        "target_version": inspection.target_version,
        "release_sequence": inspection.release_sequence,
        "manifest_sha256": inspection.manifest_sha256,
        "recovery_relative_path": (RECOVERY_RELATIVE_PATH / transaction_id).as_posix(),
        "first_error_code": None,
        "failure_phase": None,
        "retry_action": None,
        "retry_count": 0,
    }


def _copy_to_recovery(
    source: Path, destination: Path, *, recovery_root: Path, limit: int
) -> Tuple[int, str]:
    """Copy one stable regular file with bounded memory and durable destination."""

    if limit < 0 or update_coordination._is_link_like(source):
        raise UpdateTransactionError("recovery source is invalid")
    _ensure_private_directory(destination.parent)
    if destination.exists() or update_coordination._is_link_like(destination):
        raise UpdateTransactionError("recovery destination already exists")
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    write_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    source_fd = destination_fd = None
    completed = False
    try:
        before = os.lstat(source)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise UpdateTransactionError("recovery source is not a private regular file")
        source_fd = os.open(source, read_flags)
        destination_fd = os.open(destination, write_flags, 0o600)
        os.set_inheritable(source_fd, False)
        os.set_inheritable(destination_fd, False)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(
                source_fd, min(1024 * 1024, max(1, limit + 1 - size))
            )
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise UpdateTransactionError("tracked recovery exceeds the size limit")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("short recovery write")
                view = view[written:]
        after = os.fstat(source_fd)
        if not updater._same_stat_identity(before, after) or size != after.st_size:
            raise UpdateTransactionError("recovery source changed during capture")
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        if os.name != "nt":
            destination.chmod(0o600)
        else:
            windows_security.validate_private_mutation_acl(destination)
        _fsync_parent(destination)
        _fsync_directory_chain(destination.parent, recovery_root)
        completed = True
        return size, digest.hexdigest()
    except UpdateTransactionError:
        raise
    except (OSError, windows_security.WindowsSecurityError) as exc:
        raise UpdateTransactionError("could not persist recovery data") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        try:
            if destination.exists() and not completed:
                destination.unlink()
        except OSError:
            pass


def _safe_tracked_path(root: Path, relative: str) -> Path:
    parts = updater._validate_relative_git_path(relative, portable=True)
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) \
                or bool(reparse and attributes & reparse):
            raise UpdateTransactionError("tracked recovery path contains a link or reparse point")
    return root.joinpath(*parts)


def _tracked_drift_snapshot(root: Path) -> Tuple[dict, dict, Tuple[str, ...]]:
    reader = updater._GitReader(root)
    autocrlf = updater._read_autocrlf(reader)
    eol = updater._read_eol(reader)
    symlinks = updater._read_symlinks(reader)
    updater._validate_local_checkout_policy(autocrlf, symlinks)
    _code, head_tree_output = reader.run("head_tree")
    _code, output = reader.run("index")
    _code, index_eol_output = reader.run("index_eol")
    head_entries = updater._parse_tree_entries(head_tree_output)
    entries = updater._parse_index_entries(output)
    if any(entry.stage != 0 for entry in entries):
        raise UpdateTransactionError("conflicted Git indexes require explicit repair")
    head_by_path = {entry.path: entry for entry in head_entries}
    index_by_path = {entry.path: entry for entry in entries}
    drift_paths = updater._tracked_worktree_drift_paths(
        root,
        head_entries,
        entries,
        updater._parse_index_eol(index_eol_output),
        autocrlf,
        eol,
        symlinks,
        deadline=reader.deadline,
    )
    if len(drift_paths) > _MAX_RECOVERY_PATHS:
        raise UpdateTransactionError("tracked recovery exceeds the path-count limit")
    return head_by_path, index_by_path, drift_paths


def _snapshot_tracked(root: Path, recovery: Path) -> Tuple[list, int, int, str]:
    head_by_path, index_by_path, drift_paths = _tracked_drift_snapshot(root)
    tracked = []
    total = 0
    for relative in drift_paths:
        entry = index_by_path.get(relative) or head_by_path.get(relative)
        if entry is None:
            raise UpdateTransactionError("tracked drift is absent from HEAD and the index")
        source = _safe_tracked_path(root, relative)
        record = {"path": relative, "mode": entry.mode}
        try:
            info = os.lstat(source)
        except FileNotFoundError:
            record["kind"] = "missing"
            tracked.append(record)
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(source)
            record.update({"kind": "symlink", "target": target})
            total += len(os.fsencode(target))
        elif stat.S_ISREG(info.st_mode) and not bool(reparse and attributes & reparse):
            if info.st_nlink != 1:
                raise UpdateTransactionError("tracked recovery refuses multiply linked files")
            relative_copy = (Path("tracked") / Path(relative)).as_posix()
            size, digest = _copy_to_recovery(
                source,
                recovery / relative_copy,
                recovery_root=recovery,
                limit=_MAX_RECOVERY_BYTES - total,
            )
            total += size
            record.update(
                {
                    "kind": "file",
                    "size": size,
                    "sha256": digest,
                    "recovery_path": relative_copy,
                    "permissions": stat.S_IMODE(info.st_mode) & 0o7777,
                }
            )
        else:
            raise UpdateTransactionError("tracked recovery contains an unsupported file type")
        if total > _MAX_RECOVERY_BYTES:
            raise UpdateTransactionError("tracked recovery exceeds the size limit")
        tracked.append(record)
    index_size, index_digest = _copy_to_recovery(
        root / ".git" / "index",
        recovery / "git-index.bin",
        recovery_root=recovery,
        limit=_MAX_RECOVERY_BYTES - total,
    )
    total += index_size
    return tracked, total, index_size, index_digest


def _describe_path(path: Path) -> dict:
    digest = hashlib.sha256()
    count = 0
    total = 0
    stack = [(path, "")]
    while stack:
        current, relative = stack.pop()
        info = os.lstat(current)
        count += 1
        if count > _MAX_RECOVERY_PATHS:
            raise UpdateTransactionError("collision recovery exceeds the path-count limit")
        relative_bytes = relative.encode("utf-8")
        digest.update(b"path\0" + struct.pack("<Q", len(relative_bytes)) + relative_bytes)
        if stat.S_ISLNK(info.st_mode):
            target = os.fsencode(os.readlink(current))
            digest.update(b"link\0" + struct.pack("<Q", len(target)) + target)
            total += len(target)
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if bool(reparse and attributes & reparse):
            raise UpdateTransactionError("collision recovery refuses reparse points")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise UpdateTransactionError("collision recovery refuses multiply linked files")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(current, flags)
            except OSError as exc:
                raise UpdateTransactionError("could not read collision recovery data") from exc
            file_size = 0
            digest.update(b"file\0" + struct.pack("<Q", info.st_size))
            with os.fdopen(fd, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    file_size += len(chunk)
                    total += len(chunk)
                    if total > _MAX_RECOVERY_BYTES:
                        raise UpdateTransactionError("collision recovery exceeds the size limit")
                    digest.update(chunk)
                after = os.fstat(handle.fileno())
            if not updater._same_stat_identity(info, after) or file_size != after.st_size:
                raise UpdateTransactionError("collision path changed during recovery capture")
            continue
        if stat.S_ISDIR(info.st_mode):
            digest.update(b"dir\0")
            children = []
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        children.append(Path(entry.path))
                        if count + len(children) > _MAX_RECOVERY_PATHS:
                            raise UpdateTransactionError(
                                "collision recovery exceeds the path-count limit"
                            )
            except UpdateTransactionError:
                raise
            except OSError as exc:
                raise UpdateTransactionError(
                    "could not enumerate collision recovery data"
                ) from exc
            children.sort(key=lambda item: item.name)
            for child in reversed(children):
                child_relative = "%s/%s" % (relative, child.name) if relative else child.name
                stack.append((child, child_relative))
            continue
        raise UpdateTransactionError("collision recovery contains an unsupported file type")
    return {"sha256": digest.hexdigest(), "size": total, "entries": count}


def _sha256_path(path: Path) -> str:
    return _describe_path(path)["sha256"]


def _prepare_recovery(
    root: Path,
    transaction_id: str,
    inspection: updater.UpdateInspection,
) -> Tuple[Path, dict]:
    recovery = root / RECOVERY_RELATIVE_PATH / transaction_id
    if recovery.exists() or update_coordination._is_link_like(recovery):
        raise UpdateTransactionError("recovery transaction path already exists")
    _ensure_private_directory(recovery)
    try:
        return _populate_recovery(root, recovery, transaction_id, inspection)
    except BaseException:
        _remove_unjournaled_recovery(root, recovery)
        raise


def _populate_recovery(
    root: Path,
    recovery: Path,
    transaction_id: str,
    inspection: updater.UpdateInspection,
) -> Tuple[Path, dict]:
    tracked = []
    total = 0
    index_size = None
    index_sha256 = None
    if inspection.tracked_dirty:
        tracked, total, index_size, index_sha256 = _snapshot_tracked(root, recovery)
    collisions = []
    collision_entries = 0
    for relative in inspection.collision_paths:
        source = _safe_tracked_path(root, relative)
        description = _describe_path(source)
        total += description["size"]
        if total > _MAX_RECOVERY_BYTES:
            raise UpdateTransactionError("recovery exceeds the size limit")
        collision_entries += description["entries"]
        if len(tracked) + collision_entries > _MAX_RECOVERY_PATHS:
            raise UpdateTransactionError("recovery exceeds the path-count limit")
        collisions.append({"path": relative, **description})
    manifest = {
        "schema": 1,
        "transaction_id": transaction_id,
        "created_at": _utc_now(),
        "tracked": tracked,
        "collisions": collisions,
        "index_size": index_size,
        "index_sha256": index_sha256,
    }
    _atomic_write_private_json(
        recovery / "manifest.json",
        manifest,
        max_bytes=_MAX_RECOVERY_MANIFEST_BYTES,
    )
    _fsync_directory_chain(recovery, root)
    return recovery, manifest


def _remove_unjournaled_recovery(root: Path, recovery: Path) -> None:
    """Remove a pre-journal capture that can never be used for recovery."""

    expected_parent = root / RECOVERY_RELATIVE_PATH
    if recovery.parent != expected_parent or update_coordination._is_link_like(recovery):
        raise UpdateTransactionError("unjournaled recovery path is invalid")
    try:
        shutil.rmtree(recovery)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UpdateTransactionError("could not remove aborted recovery capture") from exc
    _fsync_directory_chain(expected_parent, root)


def _move_collision(root: Path, recovery: Path, relative: str) -> None:
    source = _safe_tracked_path(root, relative)
    destination = recovery / "collisions" / Path(relative)
    _ensure_private_directory(destination.parent)
    if destination.exists() or update_coordination._is_link_like(destination):
        raise UpdateTransactionError("collision recovery destination already exists")
    _persist_collision_tree(source, root)
    try:
        windows_security.move_write_through(
            source, destination, replace_existing=False
        )
    except windows_security.WindowsSecurityError as exc:
        raise UpdateTransactionError("could not durably move colliding user data") from exc
    except OSError as exc:
        raise UpdateTransactionError("could not move colliding user data into recovery") from exc
    _fsync_parent(destination)
    _fsync_directory_chain(destination.parent, recovery)
    # Only after the recovery namespace is durable may the removal from the
    # worktree be made durable.
    _fsync_parent(source)


_RECOVERY_MANIFEST_KEYS = {
    "schema",
    "transaction_id",
    "created_at",
    "tracked",
    "collisions",
    "index_size",
    "index_sha256",
}


def _read_recovery_manifest(root: Path, journal: dict) -> Tuple[Path, dict]:
    recovery = root / journal["recovery_relative_path"]
    value = _read_private_json(
        recovery / "manifest.json",
        expected_keys=_RECOVERY_MANIFEST_KEYS,
        max_bytes=_MAX_RECOVERY_MANIFEST_BYTES,
    )
    if value.get("schema") != 1 or value.get("transaction_id") != journal["transaction_id"]:
        raise UpdateTransactionError("recovery manifest does not match the update journal")
    if not isinstance(value.get("created_at"), str) or not value["created_at"]:
        raise UpdateTransactionError("recovery manifest timestamp is invalid")
    tracked = value.get("tracked")
    collisions = value.get("collisions")
    if not isinstance(tracked, list) or not isinstance(collisions, list):
        raise UpdateTransactionError("recovery manifest path lists are invalid")
    collision_entries = 0
    for record in collisions:
        if not isinstance(record, dict) or type(record.get("entries")) is not int \
                or record["entries"] < 1:
            raise UpdateTransactionError("recovery manifest collision count is invalid")
        collision_entries += record["entries"]
        if len(tracked) + collision_entries > _MAX_RECOVERY_PATHS:
            raise UpdateTransactionError("recovery manifest exceeds the path-count limit")
    if len(tracked) > _MAX_RECOVERY_PATHS:
        raise UpdateTransactionError("recovery manifest exceeds the path-count limit")
    if tracked:
        if type(value.get("index_size")) is not int or not (
            0 <= value["index_size"] <= _MAX_RECOVERY_BYTES
        ):
            raise UpdateTransactionError("recovery index size is invalid")
        if not isinstance(value.get("index_sha256"), str) \
                or not updater._DIGEST_RE.fullmatch(value["index_sha256"]):
            raise UpdateTransactionError("recovery index digest is invalid")
    elif value.get("index_size") is not None or value.get("index_sha256") is not None:
        raise UpdateTransactionError("clean recovery unexpectedly contains an index snapshot")
    return recovery, value


def _read_regular_file_bounded(path: Path, *, limit: int, label: str) -> bytes:
    if update_coordination._is_link_like(path):
        raise UpdateTransactionError("%s must not be a link or reparse point" % label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise UpdateTransactionError("%s is not a private regular file" % label)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            body = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
    except UpdateTransactionError:
        raise
    except OSError as exc:
        raise UpdateTransactionError("could not read %s" % label) from exc
    if len(body) > limit:
        raise UpdateTransactionError("%s exceeds the size limit" % label)
    if not updater._same_stat_identity(before, after) or len(body) != after.st_size:
        raise UpdateTransactionError("%s changed while it was read" % label)
    return body


def _regular_file_matches(
    path: Path, *, expected_size: int, expected_digest: str, label: str
) -> bool:
    """Stream and authenticate a stable regular file without whole-file allocation."""

    if type(expected_size) is not int or not (0 <= expected_size <= _MAX_RECOVERY_BYTES):
        raise UpdateTransactionError("%s measurements are invalid" % label)
    if not isinstance(expected_digest, str) or not updater._DIGEST_RE.fullmatch(
        expected_digest
    ):
        raise UpdateTransactionError("%s measurements are invalid" % label)
    if update_coordination._is_link_like(path):
        raise UpdateTransactionError("%s must not be a link or reparse point" % label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = None
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise UpdateTransactionError("%s is not a private regular file" % label)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not updater._same_stat_identity(before, opened):
            raise UpdateTransactionError("%s changed before verification" % label)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                return False
            digest.update(chunk)
        after = os.fstat(fd)
        current = os.lstat(path)
    except UpdateTransactionError:
        raise
    except OSError as exc:
        raise UpdateTransactionError("could not verify %s" % label) from exc
    finally:
        if fd is not None:
            os.close(fd)
    if (
        not updater._same_stat_identity(opened, after)
        or not updater._same_stat_identity(after, current)
        or (after.st_size, getattr(after, "st_mtime_ns", None))
        != (current.st_size, getattr(current, "st_mtime_ns", None))
    ):
        raise UpdateTransactionError("%s changed during verification" % label)
    return size == expected_size and digest.hexdigest() == expected_digest


def _validate_recovery_payload(recovery: Path, manifest: dict) -> None:
    """Prove rollback bytes are intact before any destructive reset."""

    total = 0
    for record in manifest["tracked"]:
        if not isinstance(record, dict):
            raise UpdateTransactionError("tracked recovery entry is invalid")
        if record.get("kind") != "file":
            continue
        relative = record.get("path")
        if not isinstance(relative, str):
            raise UpdateTransactionError("tracked recovery path is invalid")
        expected_copy = (Path("tracked") / Path(relative)).as_posix()
        if record.get("recovery_path") != expected_copy:
            raise UpdateTransactionError("tracked recovery copy path is invalid")
        if not _regular_file_matches(
            recovery / Path(expected_copy),
            expected_size=record.get("size"),
            expected_digest=record.get("sha256"),
            label="tracked recovery file",
        ):
            raise UpdateTransactionError("tracked recovery file does not match its manifest")
        total += record["size"]
    if manifest["tracked"]:
        if not _regular_file_matches(
            recovery / "git-index.bin",
            expected_size=manifest["index_size"],
            expected_digest=manifest["index_sha256"],
            label="recovery Git index",
        ):
            raise UpdateTransactionError("recovery Git index does not match its manifest")
        total += manifest["index_size"]
    if total > _MAX_RECOVERY_BYTES:
        raise UpdateTransactionError("tracked recovery exceeds the size limit")


def _tracked_tree_is_clean(root: Path) -> bool:
    """Use the PR3 bounded/no-hook comparison to prove HEAD/index/worktree equality."""

    reader = updater._GitReader(root)
    autocrlf = updater._read_autocrlf(reader)
    eol = updater._read_eol(reader)
    symlinks = updater._read_symlinks(reader)
    updater._validate_local_checkout_policy(autocrlf, symlinks)
    _code, head_tree = reader.run("head_tree")
    _code, index = reader.run("index")
    _code, index_eol = reader.run("index_eol")
    return not updater._tracked_worktree_is_dirty(
        root,
        updater._parse_tree_entries(head_tree),
        updater._parse_index_entries(index),
        updater._parse_index_eol(index_eol),
        autocrlf,
        eol,
        symlinks,
        deadline=reader.deadline,
    )


def _tracked_record_matches(root: Path, record: dict) -> bool:
    relative = record.get("path") if isinstance(record, dict) else None
    if not isinstance(relative, str):
        raise UpdateTransactionError("tracked recovery entry is invalid")
    path = _safe_tracked_path(root, relative)
    kind = record.get("kind")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return kind == "missing"
    if kind == "missing":
        return False
    if kind == "symlink":
        return stat.S_ISLNK(info.st_mode) and os.readlink(path) == record.get("target")
    if kind != "file":
        raise UpdateTransactionError("tracked recovery entry kind is invalid")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return False
    expected_size = record.get("size")
    expected_digest = record.get("sha256")
    if type(expected_size) is not int or not (0 <= expected_size <= _MAX_RECOVERY_BYTES):
        raise UpdateTransactionError("tracked recovery entry measurements are invalid")
    if not isinstance(expected_digest, str) or not updater._DIGEST_RE.fullmatch(
        expected_digest
    ):
        raise UpdateTransactionError("tracked recovery entry measurements are invalid")
    permissions = record.get("permissions")
    if type(permissions) is not int or not (0 <= permissions <= 0o7777):
        raise UpdateTransactionError("tracked recovery permissions are invalid")
    if stat.S_IMODE(info.st_mode) & 0o7777 != permissions:
        return False
    return _regular_file_matches(
        path,
        expected_size=expected_size,
        expected_digest=expected_digest,
        label="tracked worktree file",
    )


def _reentry_is_safe(root: Path, journal: dict) -> bool:
    """Refuse destructive recovery when work changed after the recorded crash point."""

    recovery, manifest = _read_recovery_manifest(root, journal)
    _validate_recovery_payload(recovery, manifest)
    head = _head_commit(root)
    if head == journal["previous_commit"]:
        if manifest["tracked"]:
            if not all(_tracked_record_matches(root, item) for item in manifest["tracked"]):
                return False
            recorded_paths = {item["path"] for item in manifest["tracked"]}
            _head_entries, _index_entries, current_drift = _tracked_drift_snapshot(root)
            if set(current_drift) != recorded_paths:
                return False
            index = _read_regular_file_bounded(
                root / ".git" / "index",
                limit=manifest["index_size"],
                label="Git index",
            )
            if hashlib.sha256(index).hexdigest() != manifest["index_sha256"]:
                return False
        elif not _tracked_tree_is_clean(root):
            return False
    elif head == journal["target_commit"]:
        if not _tracked_tree_is_clean(root):
            return False
    else:
        return False
    for record in manifest["collisions"]:
        relative = record.get("path")
        if not isinstance(relative, str):
            raise UpdateTransactionError("collision recovery entry is invalid")
        source = _safe_tracked_path(root, relative)
        backup = recovery / "collisions" / Path(relative)
        source_exists = source.exists() or update_coordination._is_link_like(source)
        backup_exists = backup.exists() or update_coordination._is_link_like(backup)
        if head == journal["target_commit"]:
            if not backup_exists:
                return False
            present = backup
        else:
            if source_exists == backup_exists:
                return False
            present = source if source_exists else backup
        if _sha256_path(present) != record.get("sha256"):
            return False
    return True


def _ensure_restore_parent(root: Path, parent: Path) -> None:
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise UpdateTransactionError("restore parent escapes the managed root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if update_coordination._is_link_like(current):
            raise UpdateTransactionError("restore parent contains a link or reparse point")
        try:
            current.mkdir(mode=0o700, exist_ok=True)
            information = os.lstat(current)
        except OSError as exc:
            raise UpdateTransactionError("could not establish a restore parent") from exc
        if not stat.S_ISDIR(information.st_mode):
            raise UpdateTransactionError("restore parent is not a directory")
        if os.name == "nt":
            try:
                windows_security.validate_private_mutation_acl(current)
            except windows_security.WindowsSecurityError as exc:
                raise UpdateTransactionError("restore parent ACL is not private") from exc


def _replace_restored_file(
    root: Path,
    path: Path,
    recovery_source: Path,
    *,
    size: int,
    digest: str,
    permissions: int,
) -> None:
    if type(permissions) is not int or permissions < 0 or permissions > 0o7777:
        raise UpdateTransactionError("tracked recovery permissions are invalid")
    if type(size) is not int or not (0 <= size <= _MAX_RECOVERY_BYTES):
        raise UpdateTransactionError("recovery file size is invalid")
    if not isinstance(digest, str) or not updater._DIGEST_RE.fullmatch(digest):
        raise UpdateTransactionError("recovery file digest is invalid")
    if update_coordination._is_link_like(recovery_source):
        raise UpdateTransactionError("recovery file must not be a link or reparse point")
    _ensure_restore_parent(root, path.parent)
    temporary = path.with_name("%s.%s.restore" % (path.name, uuid.uuid4().hex))
    write_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    source_fd = destination_fd = None
    try:
        before = os.lstat(recovery_source)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise UpdateTransactionError("recovery file is not a private regular file")
        source_fd = os.open(recovery_source, read_flags)
        destination_fd = os.open(temporary, write_flags, permissions or 0o600)
        calculated = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(
                source_fd, min(1024 * 1024, max(1, size + 1 - copied))
            )
            if not chunk:
                break
            copied += len(chunk)
            if copied > size:
                raise UpdateTransactionError(
                    "recovery file does not match its durable manifest"
                )
            calculated.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("short restore write")
                view = view[written:]
        after = os.fstat(source_fd)
        if (
            not updater._same_stat_identity(before, after)
            or copied != size
            or copied != after.st_size
            or calculated.hexdigest() != digest
        ):
            raise UpdateTransactionError(
                "recovery file does not match its durable manifest"
            )
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        if os.name != "nt":
            temporary.chmod(permissions)
        windows_security.move_write_through(temporary, path, replace_existing=True)
        _fsync_directory_chain(path.parent, root)
    except windows_security.WindowsSecurityError as exc:
        raise UpdateTransactionError("could not restore tracked user data") from exc
    except OSError as exc:
        raise UpdateTransactionError("could not restore tracked user data") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore_tracked_entry(root: Path, recovery: Path, record: dict) -> None:
    if not isinstance(record, dict):
        raise UpdateTransactionError("tracked recovery entry is invalid")
    relative = record.get("path")
    if not isinstance(relative, str):
        raise UpdateTransactionError("tracked recovery path is invalid")
    destination = _safe_tracked_path(root, relative)
    kind = record.get("kind")
    if kind == "missing":
        if set(record) != {"path", "mode", "kind"}:
            raise UpdateTransactionError("missing-file recovery entry is invalid")
        try:
            information = os.lstat(destination)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(information.st_mode):
            raise UpdateTransactionError("missing-file recovery target changed type")
        try:
            destination.unlink()
        except OSError as exc:
            raise UpdateTransactionError("could not restore a tracked deletion") from exc
        _fsync_parent(destination)
        return
    if kind == "symlink":
        if set(record) != {"path", "mode", "kind", "target"} \
                or not isinstance(record.get("target"), str):
            raise UpdateTransactionError("symlink recovery entry is invalid")
        temporary = destination.with_name(
            "%s.%s.restore" % (destination.name, uuid.uuid4().hex)
        )
        _ensure_restore_parent(root, destination.parent)
        try:
            os.symlink(record["target"], temporary)
            windows_security.move_write_through(
                temporary, destination, replace_existing=True
            )
            _fsync_directory_chain(destination.parent, root)
        except (OSError, windows_security.WindowsSecurityError) as exc:
            raise UpdateTransactionError("could not restore a tracked symlink") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return
    if kind != "file" or set(record) != {
        "path", "mode", "kind", "size", "sha256", "recovery_path", "permissions"
    }:
        raise UpdateTransactionError("file recovery entry is invalid")
    relative_copy = record.get("recovery_path")
    if relative_copy != (Path("tracked") / Path(relative)).as_posix():
        raise UpdateTransactionError("tracked recovery copy path is invalid")
    _replace_restored_file(
        root,
        destination,
        recovery / Path(relative_copy),
        size=record.get("size"),
        digest=record.get("sha256"),
        permissions=record.get("permissions"),
    )


def _restore_collision(root: Path, recovery: Path, record: dict) -> None:
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "size", "entries"}:
        raise UpdateTransactionError("collision recovery entry is invalid")
    relative = record.get("path")
    if not isinstance(relative, str) or not isinstance(record.get("sha256"), str) \
            or not updater._DIGEST_RE.fullmatch(record["sha256"]):
        raise UpdateTransactionError("collision recovery path or digest is invalid")
    if type(record.get("size")) is not int or record["size"] < 0 \
            or type(record.get("entries")) is not int or record["entries"] < 1:
        raise UpdateTransactionError("collision recovery measurements are invalid")
    destination = _safe_tracked_path(root, relative)
    source = recovery / "collisions" / Path(relative)
    source_exists = source.exists() or update_coordination._is_link_like(source)
    destination_exists = destination.exists() or update_coordination._is_link_like(destination)
    if not source_exists:
        if destination_exists and _sha256_path(destination) == record["sha256"]:
            return
        raise UpdateTransactionError("collision recovery data is missing")
    if destination_exists:
        raise UpdateTransactionError("collision restore target is unexpectedly occupied")
    _ensure_restore_parent(root, destination.parent)
    try:
        windows_security.move_write_through(source, destination, replace_existing=False)
    except (OSError, windows_security.WindowsSecurityError) as exc:
        raise UpdateTransactionError("could not restore colliding user data") from exc
    _fsync_directory_chain(destination.parent, root)
    _fsync_parent(source)
    if _sha256_path(destination) != record["sha256"]:
        raise UpdateTransactionError("restored collision does not match its manifest")


def _restore_recovery(root: Path, journal: dict) -> None:
    recovery, manifest = _read_recovery_manifest(root, journal)
    for record in manifest["tracked"]:
        _restore_tracked_entry(root, recovery, record)
    if manifest["tracked"]:
        _replace_restored_file(
            root,
            root / ".git" / "index",
            recovery / "git-index.bin",
            size=manifest["index_size"],
            digest=manifest["index_sha256"],
            permissions=0o600,
        )
    for record in manifest["collisions"]:
        _restore_collision(root, recovery, record)


def _mutation_arguments(root: Path, commit: str, executable: str) -> Tuple[str, ...]:
    if not updater._COMMIT_RE.fullmatch(commit):
        raise GitMutationError("Git reset commit is invalid")
    return (
        executable,
        "--no-pager",
        "--no-replace-objects",
        "-c", "gc.auto=0",
        "-c", "maintenance.auto=0",
        "-c", "core.fsync=all",
        "-c", "core.fsyncMethod=fsync",
        "-c", "core.fsmonitor=false",
        "-c", "core.attributesFile=" + os.devnull,
        "-c", "core.excludesFile=" + os.devnull,
        "-c", "core.worktree=" + str(root),
        "-c", "submodule.recurse=false",
        "-C", str(root),
        "reset", "--hard", commit,
    )


def _head_commit(root: Path) -> str:
    reader = updater._GitReader(root)
    _code, output = reader.run("head")
    return updater._single_commit(output, "current HEAD")


def _fsync_stable_regular_file(path: Path, *, required: bool, label: str) -> None:
    """Flush one regular file while rejecting link swaps and identity changes."""

    access = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    flags = access | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = None
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise UpdateTransactionError("%s is missing during checkout persistence" % label)
        return
    except OSError as exc:
        raise UpdateTransactionError("could not inspect %s for persistence" % label) from exc
    attributes = getattr(before, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or bool(reparse and attributes & reparse)
    ):
        raise UpdateTransactionError("%s is not a stable regular file" % label)
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not updater._same_stat_identity(before, opened):
            raise UpdateTransactionError("%s changed before persistence" % label)
        os.fsync(fd)
        after = os.fstat(fd)
        current = os.lstat(path)
        if (
            not updater._same_stat_identity(opened, after)
            or not updater._same_stat_identity(after, current)
            or (after.st_size, getattr(after, "st_mtime_ns", None))
            != (current.st_size, getattr(current, "st_mtime_ns", None))
        ):
            raise UpdateTransactionError("%s changed during persistence" % label)
    except UpdateTransactionError:
        raise
    except OSError as exc:
        raise UpdateTransactionError("could not persist %s" % label) from exc
    finally:
        if fd is not None:
            os.close(fd)


def _persist_collision_tree(path: Path, root: Path) -> None:
    """Flush collision bytes and directory entries before moving their namespace."""

    stack = [path]
    directories = set()
    count = 0
    while stack:
        current = stack.pop()
        count += 1
        if count > _MAX_RECOVERY_PATHS:
            raise UpdateTransactionError("collision recovery exceeds the path-count limit")
        try:
            information = os.lstat(current)
        except OSError as exc:
            raise UpdateTransactionError(
                "could not inspect collision data for persistence"
            ) from exc
        attributes = getattr(information, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(information.st_mode) and os.name != "nt":
            directories.add(current.parent)
            continue
        if bool(reparse and attributes & reparse):
            raise UpdateTransactionError("collision recovery refuses reparse points")
        if stat.S_ISREG(information.st_mode):
            _fsync_stable_regular_file(
                current, required=True, label="collision recovery file"
            )
            directories.add(current.parent)
            continue
        if not stat.S_ISDIR(information.st_mode):
            raise UpdateTransactionError(
                "collision recovery contains an unsupported file type"
            )
        directories.add(current)
        try:
            with os.scandir(current) as entries:
                children = [Path(entry.path) for entry in entries]
        except OSError as exc:
            raise UpdateTransactionError(
                "could not enumerate collision data for persistence"
            ) from exc
        if count + len(stack) + len(children) > _MAX_RECOVERY_PATHS:
            raise UpdateTransactionError("collision recovery exceeds the path-count limit")
        stack.extend(children)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        if directory == root or root in directory.parents:
            _fsync_directory_chain(directory, directory)


def _persist_checkout(root: Path) -> None:
    """Make the checked-out index/worktree generation durable before state commit."""

    reader = updater._GitReader(root)
    _code, output = reader.run("index")
    entries = updater._parse_index_entries(output)
    if any(entry.stage != 0 for entry in entries):
        raise UpdateTransactionError("candidate index contains unresolved entries")
    directories = {root}
    for entry in entries:
        path = _safe_tracked_path(root, entry.path)
        current = path.parent
        while True:
            directories.add(current)
            if current == root:
                break
            current = current.parent
        try:
            information = os.lstat(path)
        except FileNotFoundError:
            # A rollback may intentionally restore a staged tracked deletion.
            continue
        attributes = getattr(information, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(information.st_mode) and os.name != "nt":
            continue
        if not stat.S_ISREG(information.st_mode) or bool(reparse and attributes & reparse):
            raise UpdateTransactionError("checkout contains an unsupported tracked file type")
        _fsync_stable_regular_file(path, required=True, label="tracked checkout file")

    git_dir = root / ".git"
    if update_coordination._is_link_like(git_dir):
        raise UpdateTransactionError("Git metadata directory is a link or reparse point")
    control_paths = [
        (git_dir / "index", True),
        (git_dir / "HEAD", True),
        (git_dir / "ORIG_HEAD", False),
    ]
    try:
        head_value = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise UpdateTransactionError("could not read Git HEAD for persistence") from exc
    if head_value.startswith("ref: "):
        reference = head_value[5:]
        parts = updater._validate_relative_git_path(reference, portable=True)
        if len(parts) < 3 or parts[0] != "refs" or parts[1] != "heads":
            raise UpdateTransactionError("Git HEAD refers outside the managed branch namespace")
        control_paths.extend(
            (
                # A packed-refs or reftable repository need not have a loose
                # branch ref even though HEAD names the managed branch.
                (git_dir.joinpath(*parts), False),
                (git_dir / "logs" / Path(reference), False),
            )
        )
    control_paths.extend(
        ((git_dir / "logs" / "HEAD", False), (git_dir / "packed-refs", False))
    )
    for path, required in control_paths:
        _fsync_stable_regular_file(path, required=required, label="Git control file")
        directories.add(path.parent)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        if directory == root or root in directory.parents:
            _fsync_directory_chain(directory, directory)


def _validated_mutation_reader(root: Path, *, anchor: _PinnedRoot) -> updater._GitReader:
    """Pin Git and reject execution-capable local config before any mutation."""

    anchor.validate()
    reader = updater._GitReader(root)
    _code, unsafe_config = reader.run("unsafe_config")
    names = updater._read_local_config_names(unsafe_config)
    if any(updater._UNSAFE_LOCAL_CONFIG_RE.fullmatch(name) for name in names):
        raise GitMutationError("checkout Git config contains execution-capable settings")
    anchor.validate()
    return reader


def _reset_to_commit(root: Path, commit: str, *, anchor: _PinnedRoot) -> None:
    reader = _validated_mutation_reader(root, anchor=anchor)
    executable = reader.git_executable
    generation = updater._git_executable_generation(executable)
    arguments = _mutation_arguments(root, commit, executable)
    last_error = None
    for attempt in range(_RESET_ATTEMPTS):
        anchor.validate()
        try:
            code, _output = updater._run_bounded_git(
                arguments,
                updater._git_environment(executable),
                timeout_seconds=_RESET_TIMEOUT_SECONDS,
            )
        except updater.UpdateInspectionError as exc:
            last_error = exc
            code = -1
        if updater._git_executable_generation(executable) != generation:
            raise GitMutationError("trusted Git executable changed during reset")
        if code == 0 and _head_commit(root) == commit:
            anchor.validate()
            return
        if attempt + 1 < _RESET_ATTEMPTS:
            time.sleep(0.1 * (attempt + 1))
    raise GitMutationError("Git reset did not establish the requested commit") from last_error


def _verify_candidate_version(root: Path, version: str) -> None:
    path = root / "VERSION"
    if update_coordination._is_link_like(path):
        raise CandidateSmokeError("candidate VERSION must not be a link or reparse point")
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CandidateSmokeError("candidate VERSION is unreadable") from exc
    if body.strip() != version:
        raise CandidateSmokeError("candidate VERSION does not match the signed release")


def _validate_windows_candidate_tree(root: Path) -> None:
    if os.name != "nt":
        return
    reader = updater._GitReader(root)
    _code, output = reader.run("index")
    entries = updater._parse_index_entries(output)
    if len(entries) > updater._MAX_GIT_PATHS:
        raise UpdateTransactionError("candidate tree exceeds the path-count limit")
    checked = set()
    for entry in entries:
        path = _safe_tracked_path(root, entry.path)
        current = path.parent
        while current != root:
            checked.add(current)
            current = current.parent
        checked.add(path)
    for path in sorted(checked, key=lambda item: (len(item.parts), str(item))):
        if update_coordination._is_link_like(path):
            raise UpdateTransactionError(
                "candidate path is a link or reparse point"
            )
        try:
            windows_security.validate_private_mutation_acl(path)
        except windows_security.WindowsSecurityError as exc:
            raise UpdateTransactionError("candidate path ACL is not private") from exc


def _run_candidate_smoke(
    root: Path, manifest: Union[release_contract.ReleaseManifest, str]
) -> None:
    """Run a fixed offline import check in a fresh interpreter."""

    version = manifest if isinstance(manifest, str) else manifest.version

    code = (
        "import pathlib,sys; "
        "root=pathlib.Path(sys.argv[1]); expected=sys.argv[2]; "
        "actual=(root/'VERSION').read_text(encoding='utf-8').strip(); "
        "assert actual==expected; sys.path.append(str(root)); "
        "import installer.shim as shim; "
        "module=pathlib.Path(shim.__file__).resolve(); "
        "assert module==root or root in module.parents"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "APPDATA",
            "HOMEDRIVE",
            "HOMEPATH",
            "LANG",
            "LC_ALL",
            "LOCALAPPDATA",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TZ",
            "USERPROFILE",
            "WINDIR",
        }
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        }
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code, str(root), version],
            cwd=str(root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SMOKE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateSmokeError("candidate smoke did not complete") from exc
    if completed.returncode != 0:
        raise CandidateSmokeError("candidate smoke failed")


def _state_from_journal(journal: dict) -> updater.UpdateState:
    return updater.UpdateState(
        schema=updater.STATE_SCHEMA_VERSION,
        channel=managed_install.CHANNEL,
        last_release_sequence=journal["previous_release_sequence"],
        last_release_commit=journal["previous_commit"],
        last_manifest_sha256=journal["previous_manifest_sha256"],
        last_version=journal["previous_version"],
    )


def _best_effort_remove_journal(root: Path) -> None:
    try:
        _remove_durable(_journal_path(root))
    except UpdateTransactionError:
        # The committed state is authoritative.  A residual journal is
        # deliberately safe: the next invocation finalizes it idempotently.
        pass


def _journal_failure(
    journal: dict,
    state: Optional[updater.UpdateState],
    *,
    error_code: str,
    failure_phase: Optional[str] = None,
    increment_retry: bool = False,
    retry_action: Optional[str] = None,
) -> dict:
    first_error = journal.get("first_error_code")
    if first_error is None and state is not None and (
        state.transaction_id == journal["transaction_id"]
        and state.error_code not in {None, "repair_required"}
    ):
        first_error = state.error_code
    if first_error is None:
        first_error = error_code
    retries = journal.get("retry_count", 0)
    return {
        **journal,
        "schema": 2,
        "first_error_code": first_error,
        "failure_phase": journal.get("failure_phase") or failure_phase or journal["phase"],
        "retry_action": retry_action or journal.get("retry_action"),
        "retry_count": retries + (1 if increment_retry else 0),
    }


def _mark_retry_pending(
    root: Path,
    state: updater.UpdateState,
    journal: dict,
    *,
    error_code: str,
    failure_phase: Optional[str] = None,
    retry_action: str,
) -> UpdateResult:
    retry_journal = _journal_failure(
        journal,
        state,
        error_code=error_code,
        failure_phase=failure_phase,
        increment_retry=True,
        retry_action=retry_action,
    )
    retry_journal["phase"] = "retry_pending"
    _write_journal(root, retry_journal)
    first_error = retry_journal["first_error_code"]
    _write_state(
        root,
        _state_payload(
            state,
            result="retry_pending",
            previous_commit=retry_journal["previous_commit"],
            target_commit=retry_journal["target_commit"],
            transaction_id=retry_journal["transaction_id"],
            error_code=first_error,
            running_commit=state.running_commit,
            running_version=state.running_version,
        ),
    )
    return UpdateResult(
        "retry_pending",
        retry_journal["previous_commit"],
        retry_journal["target_commit"],
        retry_journal["transaction_id"],
        str(root / retry_journal["recovery_relative_path"]),
        first_error,
    )


def _mark_repair_required(
    root: Path,
    state: Optional[updater.UpdateState],
    journal: Optional[dict],
    *,
    error_code: str,
) -> UpdateResult:
    persisted_error = error_code
    if journal is not None:
        repair_journal = _journal_failure(
            journal, state, error_code=error_code, failure_phase=journal["phase"]
        )
        repair_journal["phase"] = "repair_required"
        _write_journal(root, repair_journal)
        journal = repair_journal
        persisted_error = repair_journal["first_error_code"]
    elif state is not None and state.last_result == "repair_required" \
            and state.error_code not in {None, "repair_required"}:
        persisted_error = state.error_code
    if state is not None:
        previous = journal["previous_commit"] if journal else state.last_release_commit
        target = journal["target_commit"] if journal else state.target_commit
        transaction_id = journal["transaction_id"] if journal else state.transaction_id
        _write_state(
            root,
            _state_payload(
                state,
                result="repair_required",
                previous_commit=previous,
                target_commit=target,
                transaction_id=transaction_id,
                error_code=persisted_error,
                running_commit=state.running_commit,
                running_version=state.running_version,
            ),
        )
    return UpdateResult(
        "repair_required",
        journal["previous_commit"] if journal else (state.last_release_commit if state else None),
        journal["target_commit"] if journal else (state.target_commit if state else None),
        journal["transaction_id"] if journal else (state.transaction_id if state else None),
        str(root / journal["recovery_relative_path"]) if journal else None,
        persisted_error,
    )


def _rollback(
    root: Path,
    state: updater.UpdateState,
    journal: dict,
    *,
    anchor: _PinnedRoot,
    error_code: str,
) -> UpdateResult:
    try:
        rollback_journal = dict(journal, phase="rollback_started")
        _write_journal(root, rollback_journal)
        try:
            _reset_to_commit(root, journal["previous_commit"], anchor=anchor)
        except (GitMutationError, UpdateTransactionError) as reset_error:
            # If the failed reset never changed HEAD, an exact prepared-state
            # reproof lets us restore captures without attempting Git again.
            # This specifically keeps a late unsafe-config change from
            # stranding already-moved collision data in recovery storage.
            try:
                reset_was_non_mutating = (
                    _head_commit(root) == journal["previous_commit"]
                    and _reentry_is_safe(root, dict(journal, phase="prepared"))
                )
            except (UpdateTransactionError, updater.UpdateInspectionError, OSError):
                reset_was_non_mutating = False
            if not reset_was_non_mutating:
                raise reset_error
        _restore_recovery(root, journal)
        _persist_checkout(root)
        payload = _state_payload(
            state,
            result="rolled_back",
            previous_commit=journal["previous_commit"],
            target_commit=journal["target_commit"],
            transaction_id=journal["transaction_id"],
            error_code=error_code,
            sequence=journal["previous_release_sequence"],
            release_commit=journal["previous_commit"],
            manifest_sha256=journal["previous_manifest_sha256"],
            version=journal["previous_version"],
            running_commit=state.running_commit,
            running_version=state.running_version,
        )
        _write_state(root, payload)
        _best_effort_remove_journal(root)
        return UpdateResult(
            "rolled_back",
            journal["previous_commit"],
            journal["target_commit"],
            journal["transaction_id"],
            str(root / journal["recovery_relative_path"]),
            error_code,
        )
    except (GitMutationError, UpdateTransactionError):
        return _mark_retry_pending(
            root,
            state,
            journal,
            error_code=error_code,
            failure_phase=journal["phase"],
            retry_action="rollback",
        )


def _forward_commit_candidate(
    root: Path, state: updater.UpdateState, journal: dict
) -> UpdateResult:
    _validate_windows_candidate_tree(root)
    _verify_candidate_version(root, journal["target_version"])
    _run_candidate_smoke(root, journal["target_version"])
    _persist_checkout(root)
    if _head_commit(root) != journal["target_commit"] or not _tracked_tree_is_clean(root):
        raise RecoveryStateChanged("candidate checkout changed during forward recovery")
    _verify_candidate_version(root, journal["target_version"])
    _write_state(
        root,
        _state_payload(
            state,
            result="candidate_ready",
            previous_commit=journal["previous_commit"],
            target_commit=journal["target_commit"],
            transaction_id=journal["transaction_id"],
            error_code=None,
            sequence=journal["release_sequence"],
            release_commit=journal["target_commit"],
            manifest_sha256=journal["manifest_sha256"],
            version=journal["target_version"],
            running_commit=state.running_commit,
            running_version=state.running_version,
        ),
    )
    _best_effort_remove_journal(root)
    return UpdateResult(
        "candidate_ready",
        journal["previous_commit"],
        journal["target_commit"],
        journal["transaction_id"],
        str(root / journal["recovery_relative_path"]),
    )


def _recover_journal(
    root: Path, state: updater.UpdateState, journal: dict, *, anchor: _PinnedRoot
) -> UpdateResult:
    candidate_state_matches = (
        state.transaction_id == journal["transaction_id"]
        and state.last_release_commit == journal["target_commit"]
        and state.last_release_sequence == journal["release_sequence"]
        and state.last_manifest_sha256 == journal["manifest_sha256"]
        and state.last_version == journal["target_version"]
        and state.last_result == "candidate_ready"
    )
    if candidate_state_matches:
        try:
            candidate_is_complete = (
                _head_commit(root) == journal["target_commit"]
                and _reentry_is_safe(root, journal)
            )
            if candidate_is_complete:
                _verify_candidate_version(root, journal["target_version"])
        except (updater.UpdateInspectionError, OSError):
            candidate_is_complete = False
        if candidate_is_complete:
            _best_effort_remove_journal(root)
            return UpdateResult(
                "candidate_ready",
                journal["previous_commit"],
                journal["target_commit"],
                journal["transaction_id"],
                str(root / journal["recovery_relative_path"]),
            )
    rollback_state_matches = (
        state.transaction_id == journal["transaction_id"]
        and state.last_release_commit == journal["previous_commit"]
        and state.last_release_sequence == journal["previous_release_sequence"]
        and state.last_manifest_sha256 == journal["previous_manifest_sha256"]
        and state.last_version == journal["previous_version"]
        and state.last_result == "rolled_back"
        and journal["phase"] == "rollback_started"
    )
    if rollback_state_matches:
        try:
            rollback_is_complete = (
                _head_commit(root) == journal["previous_commit"]
                and _reentry_is_safe(root, dict(journal, phase="prepared"))
            )
        except (updater.UpdateInspectionError, OSError):
            rollback_is_complete = False
        if rollback_is_complete:
            _best_effort_remove_journal(root)
            return UpdateResult(
                "rolled_back",
                journal["previous_commit"],
                journal["target_commit"],
                journal["transaction_id"],
                str(root / journal["recovery_relative_path"]),
                state.error_code,
            )
    try:
        safe = _reentry_is_safe(root, journal)
        if safe:
            _validated_mutation_reader(root, anchor=anchor)
    except (UpdateTransactionError, updater.UpdateInspectionError):
        safe = False
    if not safe:
        return _mark_repair_required(
            root, state, journal, error_code="reentry_state_changed"
        )
    if journal["phase"] == "rollback_started" or (
        journal["phase"] == "retry_pending"
        and journal.get("retry_action") == "rollback"
    ):
        return _rollback(
            root,
            state,
            journal,
            anchor=anchor,
            error_code=journal.get("first_error_code")
            or state.error_code
            or "crash_reentry",
        )
    if _head_commit(root) == journal["target_commit"]:
        try:
            return _forward_commit_candidate(root, state, journal)
        except (
            CandidateSmokeError,
            GitMutationError,
            UpdateTransactionError,
            updater.UpdateInspectionError,
            OSError,
        ) as exc:
            if isinstance(exc, RecoveryStateChanged):
                return _mark_repair_required(
                    root, state, journal, error_code="recovery_state_changed"
                )
            return _mark_retry_pending(
                root,
                state,
                journal,
                error_code=type(exc).__name__,
                failure_phase=journal["phase"],
                retry_action="forward",
            )
    return _rollback(
        root,
        state,
        journal,
        anchor=anchor,
        error_code=journal.get("first_error_code") or state.error_code or "crash_reentry",
    )


def finalize_present_journal(
    root: Path, *, lock_timeout_seconds: float = 0.0
) -> Optional[UpdateResult]:
    """Finish an already-authorized journal without network or release inputs."""

    canonical = managed_install.canonical_managed_root(Path(root))
    if os.name == "nt":
        try:
            windows_security.validate_private_mutation_acl(canonical)
        except windows_security.WindowsSecurityError as exc:
            raise UpdateTransactionError("managed root ACL is not private") from exc
    try:
        transaction_context = update_coordination.install_transaction(
            canonical, timeout_seconds=lock_timeout_seconds
        )
        transaction = transaction_context.__enter__()
    except update_coordination.InstallTransactionBusy:
        return UpdateResult("skipped_locked", None, None, None, None, "transaction_busy")
    try:
        with _PinnedRoot(canonical) as anchor:
            _validate_windows_update_boundary(canonical)
            journal_path = _journal_path(canonical)
            journal_present = journal_path.exists() or update_coordination._is_link_like(
                journal_path
            )
            try:
                journal = _read_journal(canonical)
            except UpdateTransactionError:
                try:
                    state = updater._read_update_state(canonical)
                except updater.UpdateInspectionError:
                    state = None
                if journal_present:
                    return _mark_repair_required(
                        canonical, state, None, error_code="corrupt_journal"
                    )
                raise
            if journal is None:
                return None
            try:
                state = updater._read_update_state(canonical)
            except updater.UpdateInspectionError:
                state = _state_from_journal(journal)
            live = update_coordination.live_shim_sessions(
                canonical, transaction=transaction
            )
            if live:
                return UpdateResult(
                    "deferred_active_session",
                    journal["previous_commit"],
                    journal["target_commit"],
                    journal["transaction_id"],
                    str(canonical / journal["recovery_relative_path"]),
                    "journal_with_live_session",
                    live,
                )
            return _recover_journal(canonical, state, journal, anchor=anchor)
    finally:
        transaction_context.__exit__(None, None, None)


def mark_running_release(
    root: Path,
    running_commit: str,
    running_version: str,
    *,
    lock_timeout_seconds: float = 5.0,
) -> UpdateResult:
    """Confirm the code that actually crossed the managed shim serve boundary.

    The caller must already hold its shim session lease.  This state-only
    transaction never changes Git and refuses to copy a proposed target into
    ``running_commit`` unless HEAD, VERSION and the protected release state all
    independently identify the same release.
    """

    if not isinstance(running_commit, str) or not updater._COMMIT_RE.fullmatch(
        running_commit
    ):
        raise UpdateTransactionError("running shim commit is invalid")
    if not isinstance(running_version, str) or not updater._VERSION_RE.fullmatch(
        running_version
    ):
        raise UpdateTransactionError("running shim version is invalid")
    canonical = managed_install.canonical_managed_root(Path(root))
    with update_coordination.install_transaction(
        canonical, timeout_seconds=lock_timeout_seconds
    ) as transaction:
        with _PinnedRoot(canonical):
            _validate_windows_update_boundary(canonical)
            expected = canonical / update_coordination.INSTALL_LOCK_RELATIVE_PATH
            transaction._validate(expected)
            _require_protocol_ready(canonical)
            journal_path = _journal_path(canonical)
            if journal_path.exists() or update_coordination._is_link_like(journal_path):
                raise UpdateTransactionError(
                    "unfinished managed update blocks running-release confirmation"
                )
            state = updater._read_update_state(canonical)
            if _head_commit(canonical) != running_commit:
                raise UpdateTransactionError("running shim does not match managed HEAD")
            try:
                version = (canonical / "VERSION").read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise UpdateTransactionError("running VERSION is unreadable") from exc
            if (
                version != running_version
                or state.last_release_commit != running_commit
                or state.last_version != running_version
            ):
                raise UpdateTransactionError(
                    "running shim does not match the protected release state"
                )
            result = state.last_result or "up_to_date"
            if result == "candidate_ready":
                if state.target_commit != running_commit:
                    raise UpdateTransactionError(
                        "candidate target does not match the running shim"
                    )
                result = "updated"
            if (
                state.running_commit == running_commit
                and state.running_version == running_version
                and state.last_result == result
                and state.error_code is None
            ):
                return UpdateResult(
                    result,
                    state.previous_commit,
                    state.target_commit,
                    state.transaction_id,
                    str(canonical / RECOVERY_RELATIVE_PATH / state.transaction_id)
                    if state.transaction_id
                    else None,
                )
            _write_state(
                canonical,
                _state_payload(
                    state,
                    result=result,
                    previous_commit=state.previous_commit,
                    target_commit=state.target_commit,
                    transaction_id=state.transaction_id,
                    error_code=None,
                    running_commit=running_commit,
                    running_version=running_version,
                ),
            )
            return UpdateResult(
                result,
                state.previous_commit,
                state.target_commit,
                state.transaction_id,
                str(canonical / RECOVERY_RELATIVE_PATH / state.transaction_id)
                if state.transaction_id
                else None,
            )


def initialize_release_state(
    root: Path,
    manifest: release_contract.ReleaseManifest,
    signature: release_contract.ReleaseSignature,
    trusted_keys: Mapping[str, release_contract.RsaPublicKey],
    *,
    source: str,
    lock_timeout_seconds: float = 5.0,
    transaction: Optional[update_coordination.InstallTransaction] = None,
) -> updater.UpdateState:
    """Create the protected prior for an already-prepared signed stable checkout."""

    if source not in {"github", "gitee"}:
        raise UpdateTransactionError("initial release source is invalid")
    canonical = managed_install.canonical_managed_root(Path(root))
    state_path = canonical / updater.UPDATE_STATE_RELATIVE_PATH
    transaction_context = (
        contextlib.nullcontext(transaction)
        if transaction is not None
        else update_coordination.install_transaction(
            canonical, timeout_seconds=lock_timeout_seconds
        )
    )
    with transaction_context as transaction:
        expected = canonical / update_coordination.INSTALL_LOCK_RELATIVE_PATH
        transaction._validate(expected)
        with _PinnedRoot(canonical):
            _validate_windows_update_boundary(canonical)
            transaction._validate(
                canonical / update_coordination.INSTALL_LOCK_RELATIVE_PATH
            )
            if state_path.exists() or update_coordination._is_link_like(state_path):
                raise UpdateTransactionError("protected update state already exists")
            if _journal_path(canonical).exists() or update_coordination._is_link_like(
                _journal_path(canonical)
            ):
                raise UpdateTransactionError(
                    "unfinished managed update blocks state initialization"
                )
            live = update_coordination.live_shim_sessions(
                canonical, transaction=transaction
            )
            if live:
                raise UpdateTransactionError(
                    "a live shim session blocks state initialization"
                )
            reader = updater._GitReader(canonical)
            remotes = updater._read_remotes(reader)
            managed_install.validate_managed_identity(canonical, remotes)
            verified = release_contract.verify_release_signature(
                manifest,
                signature,
                trusted_keys,
                expected_repository_id=managed_install.REPOSITORY_ID,
                expected_channel=managed_install.CHANNEL,
            )
            if not release_contract.python_is_compatible(
                verified.manifest, tuple(sys.version_info[:3])
            ):
                raise UpdateTransactionError(
                    "initial release requires a newer Python runtime"
                )
            _code, tag_output = reader.run("target", tag=verified.manifest.tag)
            if updater._single_commit(tag_output, "initial release tag") != verified.manifest.commit:
                raise UpdateTransactionError(
                    "initial release tag does not match the signed commit"
                )
            if _head_commit(canonical) != verified.manifest.commit:
                raise UpdateTransactionError(
                    "managed HEAD does not match the signed initial release"
                )
            try:
                version = (canonical / "VERSION").read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise UpdateTransactionError("initial VERSION is unreadable") from exc
            if version != verified.manifest.version:
                raise UpdateTransactionError(
                    "initial VERSION does not match the signed release"
                )
            payload = {
                "schema": updater.STATE_SCHEMA_VERSION,
                "channel": managed_install.CHANNEL,
                "last_release_sequence": verified.manifest.release_sequence,
                "last_release_commit": verified.manifest.commit,
                "last_manifest_sha256": release_contract.manifest_sha256(
                    verified.manifest
                ),
                "last_version": verified.manifest.version,
                "source": source,
                "last_attempt_at": _utc_now(),
                "last_result": "up_to_date",
                "previous_commit": verified.manifest.commit,
                "target_commit": verified.manifest.commit,
                "running_commit": None,
                "running_version": None,
                "error_code": None,
                "transaction_id": None,
            }
            _write_state(canonical, payload)
            return updater._read_update_state(canonical)


def apply_present_update(
    root: Path,
    manifest: release_contract.ReleaseManifest,
    signature: release_contract.ReleaseSignature,
    trusted_keys: Mapping[str, release_contract.RsaPublicKey],
    *,
    source: str = "local",
    lock_timeout_seconds: float = 0.0,
) -> UpdateResult:
    """Apply one locally present signed release, or return a safe terminal state."""

    if source not in {"github", "gitee", "local"}:
        raise UpdateTransactionError("release source is invalid")
    requested_root = Path(root)
    canonical = managed_install.canonical_managed_root(requested_root)
    if os.name == "nt":
        try:
            windows_security.validate_private_mutation_acl(canonical)
        except windows_security.WindowsSecurityError as exc:
            raise UpdateTransactionError("managed root ACL is not private") from exc
    try:
        transaction_context = update_coordination.install_transaction(
            canonical, timeout_seconds=lock_timeout_seconds
        )
        transaction = transaction_context.__enter__()
    except update_coordination.InstallTransactionBusy:
        return UpdateResult("skipped_locked", None, None, None, None, "transaction_busy")
    try:
        with _PinnedRoot(canonical) as anchor:
            _validate_windows_update_boundary(canonical)
            journal_path = _journal_path(canonical)
            journal_present = journal_path.exists() or update_coordination._is_link_like(
                journal_path
            )
            try:
                journal = _read_journal(canonical)
            except UpdateTransactionError:
                try:
                    state = updater._read_update_state(canonical)
                except updater.UpdateInspectionError:
                    state = None
                if journal_present:
                    return _mark_repair_required(
                        canonical, state, None, error_code="corrupt_journal"
                    )
                raise
            if journal is not None:
                try:
                    state = updater._read_update_state(canonical)
                except updater.UpdateInspectionError:
                    state = _state_from_journal(journal)
                live = update_coordination.live_shim_sessions(
                    canonical, transaction=transaction
                )
                if live:
                    return UpdateResult(
                        "deferred_active_session",
                        journal["previous_commit"],
                        journal["target_commit"],
                        journal["transaction_id"],
                        str(canonical / journal["recovery_relative_path"]),
                        "journal_with_live_session",
                        live,
                    )
                recovered = _recover_journal(canonical, state, journal, anchor=anchor)
                requested_digest = release_contract.manifest_sha256(manifest)
                if recovered.status != "candidate_ready" or (
                    journal["target_commit"] == manifest.commit
                    and journal["manifest_sha256"] == requested_digest
                ):
                    return recovered

            _require_protocol_ready(canonical)
            state = updater._read_update_state(canonical)
            inspected_state = state
            inspection = None
            if (
                state.last_result in {"up_to_date", "updated"}
                and state.running_commit == state.last_release_commit
                and state.running_version == state.last_version
                and state.target_commit == state.last_release_commit
                and state.error_code is None
                and state.transaction_id is None
                and manifest.commit == state.last_release_commit
                and manifest.release_sequence == state.last_release_sequence
                and manifest.version == state.last_version
                and release_contract.manifest_sha256(manifest)
                == state.last_manifest_sha256
            ):
                # A settled release needs no shim handoff. Verify the signed
                # checkout before letting a live session skip the deferral gate.
                inspection = updater.inspect_update(
                    canonical, manifest, signature, trusted_keys
                )
                state = updater._read_update_state(canonical)
            live = update_coordination.live_shim_sessions(canonical, transaction=transaction)
            if live and (
                inspection is None
                or inspection.status != "up_to_date"
                or state != inspected_state
                or any(item.running_commit != state.last_release_commit for item in live)
            ):
                if (
                    state.last_result == "candidate_ready"
                    and state.target_commit == state.last_release_commit
                    and state.transaction_id is not None
                ):
                    return UpdateResult(
                        "deferred_active_session",
                        state.previous_commit,
                        state.target_commit,
                        state.transaction_id,
                        str(canonical / RECOVERY_RELATIVE_PATH / state.transaction_id),
                        "live_shim_session",
                        live,
                    )
                return UpdateResult(
                    "deferred_active_session",
                    state.last_release_commit,
                    None,
                    None,
                    None,
                    "live_shim_session",
                    live,
                )

            if inspection is None:
                inspection = updater.inspect_update(
                    canonical, manifest, signature, trusted_keys
                )
                state = updater._read_update_state(canonical)
            if inspection.status == "up_to_date":
                if (
                    state.last_result == "candidate_ready"
                    and state.target_commit == state.last_release_commit
                    and state.running_commit != state.last_release_commit
                ):
                    return UpdateResult(
                        "candidate_ready",
                        state.previous_commit,
                        state.last_release_commit,
                        state.transaction_id,
                        str(canonical / RECOVERY_RELATIVE_PATH / state.transaction_id)
                        if state.transaction_id
                        else None,
                    )
                _write_state(
                    canonical,
                    _state_payload(
                        state,
                        result="up_to_date",
                        previous_commit=state.last_release_commit,
                        target_commit=state.last_release_commit,
                        transaction_id=None,
                        error_code=None,
                        running_commit=state.running_commit,
                        running_version=state.running_version,
                        source=source,
                    ),
                )
                return UpdateResult(
                    "up_to_date",
                    state.last_release_commit,
                    state.last_release_commit,
                    None,
                    None,
                )

            # Run the mutation safety gate before recovery capture or moving
            # any colliding user path. _reset_to_commit repeats it to close the
            # time-of-check/time-of-use window.
            _validated_mutation_reader(canonical, anchor=anchor)
            transaction_id = uuid.uuid4().hex
            recovery = canonical / RECOVERY_RELATIVE_PATH / transaction_id
            if _head_commit(canonical) != state.last_release_commit:
                raise RecoveryStateChanged("current HEAD changed before recovery capture")
            recovery, recovery_manifest = _prepare_recovery(
                canonical, transaction_id, inspection
            )
            try:
                observed_head = _head_commit(canonical)
                if observed_head != state.last_release_commit:
                    raise RecoveryStateChanged(
                        "current HEAD changed during transaction preparation"
                    )
            except (updater.UpdateInspectionError, OSError):
                _remove_unjournaled_recovery(canonical, recovery)
                raise
            journal = _journal_for(transaction_id, state, inspection, phase="prepared")
            try:
                _write_journal(canonical, journal)
            except (UpdateTransactionError, updater.UpdateInspectionError, OSError):
                journal_path = _journal_path(canonical)
                if not (
                    journal_path.exists()
                    or update_coordination._is_link_like(journal_path)
                ):
                    _remove_unjournaled_recovery(canonical, recovery)
                raise
            try:
                for item in recovery_manifest["collisions"]:
                    _move_collision(canonical, recovery, item["path"])
                    if _sha256_path(
                        recovery / "collisions" / Path(item["path"])
                    ) != item["sha256"]:
                        raise UpdateTransactionError(
                            "collision recovery verification failed"
                        )

                anchor.validate()
                inspection = updater.inspect_update(
                    canonical, manifest, signature, trusted_keys
                )
                if inspection.target_commit != journal["target_commit"]:
                    raise UpdateTransactionError("signed target changed before reset")
                if inspection.collision_paths:
                    raise UpdateTransactionError("collision paths remain before reset")
                if recovery_manifest["tracked"]:
                    if not _reentry_is_safe(canonical, journal):
                        raise RecoveryStateChanged(
                            "tracked recovery changed before reset"
                        )
                elif inspection.tracked_dirty:
                    raise RecoveryStateChanged(
                        "tracked worktree changed after recovery capture"
                    )
                journal = dict(journal, phase="reset_started")
                _write_journal(canonical, journal)
                _reset_to_commit(canonical, inspection.target_commit, anchor=anchor)
                _validate_windows_candidate_tree(canonical)
                _verify_candidate_version(canonical, inspection.target_version)
                journal = dict(journal, phase="candidate_applied")
                _write_journal(canonical, journal)
                journal = dict(journal, phase="smoke_started")
                _write_journal(canonical, journal)
                _run_candidate_smoke(canonical, manifest)
                _persist_checkout(canonical)
                if (
                    _head_commit(canonical) != inspection.target_commit
                    or not _tracked_tree_is_clean(canonical)
                ):
                    raise RecoveryStateChanged(
                        "candidate checkout changed before durable state commit"
                    )
                _verify_candidate_version(canonical, inspection.target_version)
            except (
                GitMutationError,
                UpdateTransactionError,
                updater.UpdateInspectionError,
                OSError,
            ) as exc:
                try:
                    rollback_is_safe = _reentry_is_safe(canonical, journal)
                except (updater.UpdateInspectionError, OSError):
                    rollback_is_safe = False
                if isinstance(exc, RecoveryStateChanged) or not rollback_is_safe:
                    return _mark_repair_required(
                        canonical,
                        state,
                        journal,
                        error_code="recovery_state_changed",
                    )
                return _rollback(
                    canonical,
                    state,
                    journal,
                    anchor=anchor,
                    error_code=type(exc).__name__,
                )

            payload = _state_payload(
                state,
                result="candidate_ready",
                previous_commit=state.last_release_commit,
                target_commit=inspection.target_commit,
                transaction_id=transaction_id,
                error_code=None,
                sequence=inspection.release_sequence,
                release_commit=inspection.target_commit,
                manifest_sha256=inspection.manifest_sha256,
                version=inspection.target_version,
                running_commit=state.running_commit,
                running_version=state.running_version,
                source=source,
            )
            _write_state(canonical, payload)
            _best_effort_remove_journal(canonical)
            return UpdateResult(
                "candidate_ready",
                state.last_release_commit,
                inspection.target_commit,
                transaction_id,
                str(recovery),
            )
    finally:
        transaction_context.__exit__(None, None, None)
