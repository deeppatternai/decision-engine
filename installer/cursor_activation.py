"""Cursor-owned first activation and interpreter-repair transactions."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from installer import (
    client_host_lifecycle,
    client_host_ownership,
    cursor_skill_payload,
    managed_install,
    mcp_config,
    release_acquisition,
    release_contract,
    update_coordination,
    update_transaction,
    updater,
)
from installer.config import ShellError


JOURNAL_SCHEMA = 1
INSTALL_JOURNAL_SCHEMA = 2
LIFECYCLE_JOURNAL_SCHEMA = 3
_MINIMUM_PYTHON = (3, 12)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPAIR_PHASES = (
    "intent",
    "mcp_publishing",
    "mcp_published",
    "ownership_publishing",
    "ownership_published",
    "verifying",
    "committed",
)
_INSTALL_PHASES = (
    "intent",
    "skills_publishing",
    "skills_published",
    "mcp_publishing",
    "mcp_published",
    "ownership_publishing",
    "ownership_published",
    "verifying",
    "committed",
)
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_JOURNAL_KEYS = {
    "schema",
    "transaction_id",
    "install_id",
    "operation",
    "phase",
    "target_present",
    "staging_root",
    "pre_mcp_managed_entry",
    "pre_mcp_file_hash",
    "post_mcp_managed_entry",
    "post_mcp_file_hash",
    "pre_ownership_record",
    "pre_ownership_record_hash",
    "post_ownership_record",
    "post_ownership_record_hash",
    "pre_skill_release_id",
    "target_skill_release_id",
    "pre_skill_manifest_sha256",
    "target_skill_manifest_sha256",
    "per_skill_publish_state",
}
_FaultInjector = Optional[Callable[[str], None]]


class CursorActivationError(ShellError):
    """Safe-to-display Cursor transaction refusal."""


def cursor_activation_root() -> Path:
    return (
        managed_install.registration_path().parent
        / "decision-engine-cursor"
    )


def activation_journal_path() -> Path:
    return cursor_activation_root() / "activation-journal.json"


def activation_lock_path() -> Path:
    return cursor_activation_root() / "activation.lock"


def _ensure_state_root() -> Path:
    root = cursor_activation_root()
    try:
        managed_install._ensure_private_registration_directory(root)
    except managed_install.ManagedInstallError as exc:
        raise CursorActivationError(
            "Cursor activation state directory is unsafe"
        ) from exc
    return root


def _is_link_like_info(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


@contextlib.contextmanager
def cursor_activation_lock() -> Iterator[None]:
    """Acquire the persistent Cursor single-writer lock without waiting."""

    _ensure_state_root()
    path = activation_lock_path()
    if managed_install._is_link_like(path):
        raise CursorActivationError("Cursor activation lock is unsafe")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CursorActivationError("Cursor activation lock is unavailable") from exc
    locked = False
    try:
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _is_link_like_info(info)
        ):
            raise CursorActivationError("Cursor activation lock is unsafe")
        if info.st_size == 0:
            try:
                os.write(fd, b"0")
                os.fsync(fd)
            except OSError as exc:
                raise CursorActivationError(
                    "Cursor activation lock could not be initialized"
                ) from exc
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                    13,
                    36,
                }:
                    raise CursorActivationError(
                        "Cursor activation is already in progress"
                    ) from exc
                raise CursorActivationError(
                    "Cursor activation lock could not be acquired"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise CursorActivationError(
                        "Cursor activation is already in progress"
                    ) from exc
                raise CursorActivationError(
                    "Cursor activation lock could not be acquired"
                ) from exc
        locked = True
        with client_host_ownership._activation_publication_scope():
            yield
    finally:
        if locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _write_journal(journal: Dict[str, Any]) -> None:
    path = activation_journal_path()
    rendered = (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
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
        _ensure_state_root()
        if managed_install._is_link_like(path):
            raise CursorActivationError(
                "Cursor activation journal path is unsafe"
            )
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
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
    except (OSError, RuntimeError, managed_install.ManagedInstallError) as exc:
        raise CursorActivationError(
            "Cursor activation journal could not be persisted"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
        if not replaced:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def _phase(journal: Dict[str, Any], phase: str) -> Dict[str, Any]:
    phases = (
        _INSTALL_PHASES
        if journal.get("operation") in {"install", "update", "uninstall"}
        else _REPAIR_PHASES
    )
    if phase not in phases:
        raise CursorActivationError("Cursor activation phase is invalid")
    updated = dict(journal)
    updated["phase"] = phase
    _write_journal(updated)
    return updated


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(Path(path).expanduser())))


def _validated_repair_journal(
    payload: Dict[str, Any],
    *,
    managed_root: Path,
) -> Dict[str, Any]:
    if set(payload) != _JOURNAL_KEYS or payload.get("schema") != JOURNAL_SCHEMA:
        raise CursorActivationError(
            "repair_required: Cursor activation journal schema is invalid"
        )
    try:
        transaction_id = str(uuid.UUID(payload.get("transaction_id")))
        install_id = managed_install._validate_install_id(payload.get("install_id"))
    except (ValueError, AttributeError, managed_install.ManagedInstallError) as exc:
        raise CursorActivationError(
            "repair_required: Cursor activation journal identity is invalid"
        ) from exc
    if (
        transaction_id != payload.get("transaction_id")
        or payload.get("operation") != "interpreter_repair"
        or payload.get("phase") not in _REPAIR_PHASES
        or payload.get("target_present") is not True
    ):
        raise CursorActivationError(
            "repair_required: Cursor activation journal state is invalid"
        )
    paired = client_host_ownership._paired_install_id(managed_root)
    if install_id != paired:
        raise CursorActivationError(
            "repair_required: Cursor activation journal install ID does not match"
        )
    expected_staging = (
        cursor_activation_root()
        / "staging"
        / transaction_id
        / "skills"
    )
    staging = payload.get("staging_root")
    if (
        not isinstance(staging, str)
        or _path_key(Path(staging)) != _path_key(expected_staging)
    ):
        raise CursorActivationError(
            "repair_required: Cursor activation staging root is invalid"
        )
    for name in (
        "pre_mcp_file_hash",
        "post_mcp_file_hash",
        "pre_ownership_record_hash",
        "post_ownership_record_hash",
        "pre_skill_manifest_sha256",
        "target_skill_manifest_sha256",
    ):
        value = payload.get(name)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise CursorActivationError(
                "repair_required: Cursor activation journal hash is invalid"
            )
    if (
        not isinstance(payload.get("pre_mcp_managed_entry"), dict)
        or not isinstance(payload.get("post_mcp_managed_entry"), dict)
        or not isinstance(payload.get("pre_ownership_record"), dict)
        or not isinstance(payload.get("post_ownership_record"), dict)
        or payload.get("per_skill_publish_state") != {}
    ):
        raise CursorActivationError(
            "repair_required: Cursor activation journal artifact state is invalid"
        )
    for name in ("pre_skill_release_id", "target_skill_release_id"):
        value = payload.get(name)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        ):
            raise CursorActivationError(
                "repair_required: Cursor activation journal release is invalid"
            )
    return payload


def _validated_install_journal(
    payload: Dict[str, Any],
    *,
    managed_root: Path,
) -> Dict[str, Any]:
    if (
        set(payload) != _JOURNAL_KEYS
        or payload.get("schema") != INSTALL_JOURNAL_SCHEMA
    ):
        raise CursorActivationError(
            "repair_required: Cursor activation journal schema is invalid"
        )
    try:
        transaction_id = str(uuid.UUID(payload.get("transaction_id")))
        install_id = managed_install._validate_install_id(
            payload.get("install_id")
        )
    except (ValueError, AttributeError, managed_install.ManagedInstallError) as exc:
        raise CursorActivationError(
            "repair_required: Cursor activation journal identity is invalid"
        ) from exc
    if (
        transaction_id != payload.get("transaction_id")
        or payload.get("operation") != "install"
        or payload.get("phase") not in _INSTALL_PHASES
        or payload.get("target_present") is not True
    ):
        raise CursorActivationError(
            "repair_required: Cursor activation journal state is invalid"
        )
    if install_id != client_host_ownership._paired_install_id(managed_root):
        raise CursorActivationError(
            "repair_required: Cursor activation journal install ID does not match"
        )
    expected_staging = (
        cursor_activation_root()
        / "staging"
        / transaction_id
        / "skills"
    )
    staging = payload.get("staging_root")
    if (
        not isinstance(staging, str)
        or _path_key(Path(staging)) != _path_key(expected_staging)
    ):
        raise CursorActivationError(
            "repair_required: Cursor activation staging root is invalid"
        )
    optional_hashes = {
        "pre_mcp_file_hash",
        "pre_ownership_record_hash",
        "pre_skill_manifest_sha256",
    }
    for name in (
        "pre_mcp_file_hash",
        "post_mcp_file_hash",
        "pre_ownership_record_hash",
        "post_ownership_record_hash",
        "pre_skill_manifest_sha256",
        "target_skill_manifest_sha256",
    ):
        value = payload.get(name)
        if name in optional_hashes and value is None:
            continue
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise CursorActivationError(
                "repair_required: Cursor activation journal hash is invalid"
            )
    pre_entry = payload.get("pre_mcp_managed_entry")
    if (
        pre_entry is not None
        or payload.get("pre_mcp_file_hash")
        == payload.get("post_mcp_file_hash")
        or not isinstance(payload.get("post_mcp_managed_entry"), dict)
        or payload.get("pre_ownership_record") is not None
        or not isinstance(payload.get("post_ownership_record"), dict)
        or payload.get("pre_skill_release_id") is not None
        or payload.get("pre_skill_manifest_sha256") is not None
    ):
        raise CursorActivationError(
            "repair_required: Cursor initial pre-state is invalid"
        )
    release_id = payload.get("target_skill_release_id")
    if (
        not isinstance(release_id, str)
        or not cursor_skill_payload._RELEASE_ID_RE.fullmatch(release_id)
    ):
        raise CursorActivationError(
            "repair_required: Cursor activation journal release is invalid"
        )
    states = payload.get("per_skill_publish_state")
    active = payload.get("phase") != "committed"
    if not isinstance(states, dict) or (
        active
        and payload.get("phase") != "intent"
        and set(states) != set(cursor_skill_payload.CURSOR_M2_SKILLS)
    ) or (
        payload.get("phase") == "intent"
        and states
        and set(states) != set(cursor_skill_payload.CURSOR_M2_SKILLS)
    ):
        raise CursorActivationError(
            "repair_required: Cursor skill journal state is invalid"
        )
    destination = mcp_config.CLIENT_SPECS["cursor"].skills_path()
    for skill in states:
        item = states.get(skill)
        expected_target = (
            Path(destination) / skill
            if isinstance(skill, str)
            else Path(destination)
        )
        identity = item.get("object_identity") if isinstance(item, dict) else None
        if (
            not isinstance(skill, str)
            or not _SKILL_NAME_RE.fullmatch(skill)
            or not isinstance(item, dict)
            or set(item)
            != {"state", "target_path", "tree_sha256", "object_identity"}
            or item.get("state")
            not in {"pending", "publishing", "target_published"}
            or not isinstance(item.get("target_path"), str)
            or _path_key(Path(item["target_path"])) != _path_key(expected_target)
            or not isinstance(item.get("tree_sha256"), str)
            or not _SHA256_RE.fullmatch(item["tree_sha256"])
            or not isinstance(identity, (list, tuple))
            or len(identity) != 3
            or any(type(value) is not int or value < 0 for value in identity)
        ):
            raise CursorActivationError(
                "repair_required: Cursor skill journal state is invalid"
            )
    return payload


def _validated_lifecycle_journal(
    payload: Dict[str, Any],
    *,
    managed_root: Path,
) -> Dict[str, Any]:
    if set(payload) != _JOURNAL_KEYS or payload.get("schema") != LIFECYCLE_JOURNAL_SCHEMA:
        raise CursorActivationError(
            "repair_required: Cursor lifecycle journal schema is invalid"
        )
    try:
        transaction_id = str(uuid.UUID(payload.get("transaction_id")))
        install_id = managed_install._validate_install_id(payload.get("install_id"))
    except (ValueError, AttributeError, managed_install.ManagedInstallError) as exc:
        raise CursorActivationError(
            "repair_required: Cursor lifecycle journal identity is invalid"
        ) from exc
    operation = payload.get("operation")
    if operation not in {"update", "uninstall"}:
        raise CursorActivationError(
            "requires_newer_installer: Cursor lifecycle operation is unsupported"
        )
    if (
        transaction_id != payload.get("transaction_id")
        or payload.get("phase") not in _INSTALL_PHASES
        or payload.get("target_present") is not (operation == "update")
        or install_id != client_host_ownership._paired_install_id(managed_root)
    ):
        raise CursorActivationError(
            "repair_required: Cursor lifecycle journal state is invalid"
        )
    transaction_root = cursor_activation_root() / "staging" / transaction_id
    expected_staging = transaction_root / "skills"
    if (
        not isinstance(payload.get("staging_root"), str)
        or _path_key(Path(payload["staging_root"])) != _path_key(expected_staging)
    ):
        raise CursorActivationError(
            "repair_required: Cursor lifecycle staging root is invalid"
        )
    required_hashes = (
        "pre_mcp_file_hash",
        "pre_ownership_record_hash",
        "pre_skill_manifest_sha256",
    )
    optional_hashes = (
        "post_mcp_file_hash",
        "post_ownership_record_hash",
        "target_skill_manifest_sha256",
    )
    for name in required_hashes:
        value = payload.get(name)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise CursorActivationError(
                "repair_required: Cursor lifecycle journal hash is invalid"
            )
    for name in optional_hashes:
        value = payload.get(name)
        if value is not None and (
            not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
        ):
            raise CursorActivationError(
                "repair_required: Cursor lifecycle journal hash is invalid"
            )
    if (
        not isinstance(payload.get("pre_mcp_managed_entry"), dict)
        or not isinstance(payload.get("pre_ownership_record"), dict)
        or not isinstance(payload.get("pre_skill_release_id"), str)
        or not cursor_skill_payload._RELEASE_ID_RE.fullmatch(
            payload["pre_skill_release_id"]
        )
    ):
        raise CursorActivationError(
            "repair_required: Cursor lifecycle pre-state is invalid"
        )
    pre_record = _record_from_journal(
        payload["pre_ownership_record"],
        managed_root=managed_root,
        managed_entry=payload["pre_mcp_managed_entry"],
    )
    if (
        _path_key(pre_record.config_path)
        != _path_key(mcp_config.agent_config_path("cursor"))
        or pre_record.server_name != mcp_config.DEFAULT_SERVER_NAME
        or client_host_ownership.record_sha256(pre_record)
        != payload["pre_ownership_record_hash"]
    ):
        raise CursorActivationError(
            "repair_required: Cursor lifecycle ownership binding is invalid"
        )
    if operation == "update":
        if (
            payload.get("post_mcp_managed_entry")
            != payload.get("pre_mcp_managed_entry")
            or payload.get("post_mcp_file_hash")
            != payload.get("pre_mcp_file_hash")
            or not isinstance(payload.get("post_ownership_record"), dict)
            or not isinstance(payload.get("post_ownership_record_hash"), str)
            or not isinstance(payload.get("target_skill_release_id"), str)
            or not cursor_skill_payload._RELEASE_ID_RE.fullmatch(
                payload["target_skill_release_id"]
            )
            or not isinstance(payload.get("target_skill_manifest_sha256"), str)
        ):
            raise CursorActivationError(
                "repair_required: Cursor lifecycle update target is invalid"
            )
        target_record = _record_from_journal(
            payload["post_ownership_record"],
            managed_root=managed_root,
            managed_entry=payload["post_mcp_managed_entry"],
        )
        if (
            _path_key(target_record.config_path)
            != _path_key(mcp_config.agent_config_path("cursor"))
            or target_record.server_name != mcp_config.DEFAULT_SERVER_NAME
            or client_host_ownership.record_sha256(target_record)
            != payload["post_ownership_record_hash"]
        ):
            raise CursorActivationError(
                "repair_required: Cursor lifecycle target ownership is invalid"
            )
        try:
            client_host_lifecycle.require_monotonic_release(
                pre_record.skill_release_id,
                target_record.skill_release_id,
            )
        except ValueError as exc:
            raise CursorActivationError(
                "repair_required: Cursor lifecycle target is not newer"
            ) from exc
    elif any(
        payload.get(name) is not None
        for name in (
            "post_mcp_managed_entry",
            "post_ownership_record",
            "post_ownership_record_hash",
            "target_skill_release_id",
            "target_skill_manifest_sha256",
        )
    ):
        raise CursorActivationError(
            "repair_required: Cursor lifecycle uninstall target is invalid"
        )
    states = payload.get("per_skill_publish_state")
    if not isinstance(states, dict) or set(states) != set(
        cursor_skill_payload.CURSOR_M2_SKILLS
    ):
        raise CursorActivationError(
            "repair_required: Cursor lifecycle skill state is invalid"
        )
    destination = mcp_config.CLIENT_SPECS["cursor"].skills_path()
    quarantine_root = transaction_root / "backups" / "skills"
    for skill, item in states.items():
        expected_target = destination / skill
        expected_quarantine = quarantine_root / skill
        try:
            client_host_lifecycle.require_authorized_child(
                expected_target,
                destination,
            )
            client_host_lifecycle.require_authorized_child(
                expected_quarantine,
                quarantine_root,
            )
        except ValueError as exc:
            raise CursorActivationError(
                "repair_required: Cursor lifecycle path is unauthorized"
            ) from exc
        pre_identity = item.get("pre_object_identity") if isinstance(item, dict) else None
        target_identity = item.get("target_object_identity") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "state",
                "target_path",
                "quarantine_path",
                "pre_tree_sha256",
                "pre_object_identity",
                "target_tree_sha256",
                "target_object_identity",
            }
            or item.get("state")
            not in {"pending", "quarantining", "pre_backed_up", "target_published"}
            or _path_key(Path(item.get("target_path", "")))
            != _path_key(expected_target)
            or _path_key(Path(item.get("quarantine_path", "")))
            != _path_key(expected_quarantine)
            or not isinstance(item.get("pre_tree_sha256"), str)
            or not _SHA256_RE.fullmatch(item["pre_tree_sha256"])
            or not isinstance(pre_identity, (list, tuple))
            or len(pre_identity) != 3
            or any(type(value) is not int or value < 0 for value in pre_identity)
            or (
                operation == "update"
                and (
                    not isinstance(item.get("target_tree_sha256"), str)
                    or not _SHA256_RE.fullmatch(item["target_tree_sha256"])
                    or not isinstance(target_identity, (list, tuple))
                    or len(target_identity) != 3
                    or any(
                        type(value) is not int or value < 0
                        for value in target_identity
                    )
                )
            )
            or (
                operation == "uninstall"
                and (
                    item.get("target_tree_sha256") is not None
                    or item.get("target_object_identity") is not None
                )
            )
        ):
            raise CursorActivationError(
                "repair_required: Cursor lifecycle skill state is invalid"
            )
    return payload


def _load_journal_if_present(
    *,
    managed_root: Path,
) -> Optional[Dict[str, Any]]:
    path = activation_journal_path()
    if not path.exists() and not managed_install._is_link_like(path):
        return None
    try:
        managed_install._reject_link_components(path.parent)
        managed_install._validate_private_posix_path(
            path.parent,
            directory=True,
        )
        raw, _size = cursor_skill_payload._stable_read(
            path,
            maximum=512 * 1024,
            label="Cursor activation journal",
        )
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=mcp_config._json_object_without_duplicates,
        )
        if not isinstance(payload, dict) or set(payload) != _JOURNAL_KEYS:
            raise CursorActivationError(
                "repair_required: Cursor activation journal schema is invalid"
            )
    except CursorActivationError:
        raise
    except (
        managed_install.ManagedInstallError,
        cursor_skill_payload.CursorSkillPayloadError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ShellError,
    ) as exc:
        raise CursorActivationError(
            "repair_required: Cursor activation journal is invalid"
        ) from exc
    if payload.get("schema") == JOURNAL_SCHEMA:
        return _validated_repair_journal(payload, managed_root=managed_root)
    if payload.get("schema") == INSTALL_JOURNAL_SCHEMA:
        return _validated_install_journal(payload, managed_root=managed_root)
    if payload.get("schema") == LIFECYCLE_JOURNAL_SCHEMA:
        return _validated_lifecycle_journal(payload, managed_root=managed_root)
    if type(payload.get("schema")) is int and payload.get("schema") > LIFECYCLE_JOURNAL_SCHEMA:
        raise CursorActivationError(
            "requires_newer_installer: Cursor activation journal is newer"
        )
    raise CursorActivationError(
        "repair_required: Cursor activation journal schema is invalid"
    )


def _validate_repair_interpreter(
    python: Path,
    managed_root: Path,
    *,
    minimum_python: Tuple[int, int] = _MINIMUM_PYTHON,
) -> str:
    candidate = Path(python).expanduser()
    if not candidate.is_absolute():
        raise CursorActivationError("repair interpreter must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
        running = Path(sys.executable).resolve(strict=True)
    except OSError as exc:
        raise CursorActivationError("repair interpreter is unavailable") from exc
    if (
        _path_key(resolved) != _path_key(running)
        or managed_install._is_link_like(resolved)
        or not resolved.is_file()
        or tuple(sys.version_info[:2]) < tuple(minimum_python)
    ):
        raise CursorActivationError(
            "repair interpreter is not the compatible running Python"
        )
    launcher = managed_root / "installer" / "launcher.py"
    if managed_install._is_link_like(launcher) or not launcher.is_file():
        raise CursorActivationError("managed launcher is unavailable")
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import installer.launcher"
    )
    try:
        completed = subprocess.run(
            [str(resolved), "-I", "-c", probe, str(managed_root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CursorActivationError("managed launcher import probe failed") from exc
    if completed.returncode != 0:
        raise CursorActivationError("managed launcher import probe failed")
    return str(resolved)


def _record_from_journal(
    payload: Dict[str, Any],
    *,
    managed_root: Path,
    managed_entry: Dict[str, Any],
) -> client_host_ownership.OwnershipRecord:
    try:
        record = client_host_ownership.build_record(
            "cursor",
            managed_root=managed_root,
            config_path=Path(payload["config_path"]),
            server_name=payload["server_name"],
            managed_entry=managed_entry,
            skill_release_id=payload["skill_release_id"],
            skill_manifest_sha256=payload["skill_manifest_sha256"],
        )
    except (KeyError, TypeError, client_host_ownership.OwnershipError) as exc:
        raise CursorActivationError(
            "repair_required: journal ownership target is invalid"
        ) from exc
    if client_host_ownership.record_payload(record) != payload:
        raise CursorActivationError(
            "repair_required: journal ownership target does not match"
        )
    return record


def _current_record(
    *,
    managed_root: Path,
    config_path: Path,
    server_name: str,
) -> Tuple[client_host_ownership.OwnershipRecord, str]:
    record = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=managed_root,
        config_path=config_path,
        server_name=server_name,
    )
    if record is None:
        raise CursorActivationError(
            "same_name_unowned: Cursor ownership record is missing"
        )
    digest = client_host_ownership.record_file_sha256_if_present("cursor")
    repeated = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=managed_root,
        config_path=config_path,
        server_name=server_name,
    )
    if digest is None or repeated != record:
        raise CursorActivationError(
            "repair_required: Cursor ownership record changed while being read"
        )
    return record, digest


def _verify_target(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
) -> None:
    config_hash = mcp_config.cursor_config_file_sha256()
    record_hash = client_host_ownership.record_file_sha256_if_present("cursor")
    if (
        config_hash != journal["post_mcp_file_hash"]
        or record_hash != journal["post_ownership_record_hash"]
    ):
        raise CursorActivationError(
            "repair_required: Cursor transaction artifacts do not match target"
        )
    target_record = _record_from_journal(
        journal["post_ownership_record"],
        managed_root=managed_root,
        managed_entry=journal["post_mcp_managed_entry"],
    )
    persisted = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=managed_root,
        config_path=target_record.config_path,
        server_name=target_record.server_name,
    )
    actual = mcp_config.read_entry("cursor", target_record.server_name)
    if (
        persisted != target_record
        or mcp_config._managed_entry_projection(actual)
        != journal["post_mcp_managed_entry"]
    ):
        raise CursorActivationError(
            "repair_required: Cursor target verification failed"
        )


def _resume_incomplete(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
    fault_injector: _FaultInjector,
) -> Dict[str, Any]:
    config_hash = mcp_config.cursor_config_file_sha256()
    record_hash = client_host_ownership.record_file_sha256_if_present("cursor")
    pre = (
        journal["pre_mcp_file_hash"],
        journal["pre_ownership_record_hash"],
    )
    target = (
        journal["post_mcp_file_hash"],
        journal["post_ownership_record_hash"],
    )
    observed = (config_hash, record_hash)
    if observed == pre:
        plan = mcp_config.prepare_cursor_entry_write(
            journal["post_mcp_managed_entry"],
            server_name=journal["post_ownership_record"]["server_name"],
            allow_owned_update=True,
        )
        if (
            plan.pre_file_sha256 != journal["pre_mcp_file_hash"]
            or plan.post_file_sha256 != journal["post_mcp_file_hash"]
            or plan.post_managed_entry != journal["post_mcp_managed_entry"]
        ):
            raise CursorActivationError(
                "repair_required: Cursor pre-state no longer reproduces target"
            )
        journal = _phase(journal, "mcp_publishing")
        if fault_injector is not None:
            fault_injector("before_mcp_apply")
        mcp_config.apply_cursor_entry_write(plan)
        if fault_injector is not None:
            fault_injector("after_mcp_apply_before_phase")
        journal = _phase(journal, "mcp_published")
        if fault_injector is not None:
            fault_injector("after_mcp_published")
        observed = (
            mcp_config.cursor_config_file_sha256(),
            client_host_ownership.record_file_sha256_if_present("cursor"),
        )
    if observed == (
        journal["post_mcp_file_hash"],
        journal["pre_ownership_record_hash"],
    ):
        target_record = _record_from_journal(
            journal["post_ownership_record"],
            managed_root=managed_root,
            managed_entry=journal["post_mcp_managed_entry"],
        )
        journal = _phase(journal, "ownership_publishing")
        if fault_injector is not None:
            fault_injector("before_ownership_apply")
        client_host_ownership.publish_record(
            target_record,
            managed_root=managed_root,
            expected_exists=True,
            expected_sha256=journal["pre_ownership_record_hash"],
        )
        if fault_injector is not None:
            fault_injector("after_ownership_apply_before_phase")
        journal = _phase(journal, "ownership_published")
        if fault_injector is not None:
            fault_injector("after_ownership_published")
        observed = (
            mcp_config.cursor_config_file_sha256(),
            client_host_ownership.record_file_sha256_if_present("cursor"),
        )
    if observed != target:
        raise CursorActivationError(
            "repair_required: Cursor transaction contains third-state artifacts"
        )
    journal = _phase(journal, "verifying")
    _verify_target(journal, managed_root=managed_root)
    journal = _phase(journal, "committed")
    return {"action": "recovered", "phase": journal["phase"]}


def _transaction_root(journal: Dict[str, Any]) -> Path:
    return Path(journal["staging_root"]).parent


def _preimage_path(journal: Dict[str, Any]) -> Path:
    return _transaction_root(journal) / "mcp-preimage.bin"


def _mcp_quarantine_path(journal: Dict[str, Any]) -> Path:
    return _transaction_root(journal) / "mcp-config.rollback"


def _ownership_quarantine_path(journal: Dict[str, Any]) -> Path:
    return _transaction_root(journal) / "ownership-record.rollback"


def _skill_quarantine_path(
    journal: Dict[str, Any],
    skill: str,
) -> Path:
    return _transaction_root(journal) / ("skill-%s.rollback" % skill)


def _write_preimage(path: Path, raw: bytes) -> None:
    if not isinstance(raw, bytes) or len(raw) > mcp_config.MAX_AGENT_CONFIG_BYTES:
        raise CursorActivationError("Cursor MCP preimage is invalid")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        cursor_skill_payload._fsync_directory(path.parent)
    except OSError as exc:
        raise CursorActivationError(
            "Cursor MCP preimage could not be staged"
        ) from exc


def _read_preimage(journal: Dict[str, Any]) -> Optional[bytes]:
    path = _preimage_path(journal)
    expected = journal["pre_mcp_file_hash"]
    if expected is None:
        if path.exists() or managed_install._is_link_like(path):
            raise CursorActivationError(
                "repair_required: unexpected Cursor MCP preimage"
            )
        return None
    try:
        raw, _size = cursor_skill_payload._stable_read(
            path,
            maximum=mcp_config.MAX_AGENT_CONFIG_BYTES,
            label="Cursor MCP preimage",
        )
    except cursor_skill_payload.CursorSkillPayloadError as exc:
        raise CursorActivationError(
            "repair_required: Cursor MCP preimage is invalid"
        ) from exc
    if hashlib.sha256(raw).hexdigest() != expected:
        raise CursorActivationError(
            "repair_required: Cursor MCP preimage hash does not match"
        )
    return raw


def _install_cache_root(journal: Dict[str, Any]) -> Path:
    release_id = journal["target_skill_release_id"]
    if not cursor_skill_payload._RELEASE_ID_RE.fullmatch(release_id):
        raise CursorActivationError(
            "repair_required: Cursor skill release ID is invalid"
        )
    root = (
        cursor_activation_root()
        / "skill-payloads"
        / release_id
    )
    try:
        digest = cursor_skill_payload.verify_cached_payload(root)
    except cursor_skill_payload.CursorSkillPayloadError as exc:
        raise CursorActivationError(
            "repair_required: Cursor cached skill payload is invalid"
        ) from exc
    if digest != journal["target_skill_manifest_sha256"]:
        raise CursorActivationError(
            "repair_required: Cursor cached skill manifest changed"
        )
    return root


def _install_cache_root_for_recovery(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
) -> Tuple[Path, Dict[str, Any], bool]:
    """Return the exact cache, rebuilding and retaining one for rollback only."""

    try:
        return _install_cache_root(journal), journal, False
    except CursorActivationError:
        root = (
            cursor_activation_root()
            / "skill-payloads"
            / journal["target_skill_release_id"]
        )
        if root.exists() or managed_install._is_link_like(root):
            raise
    verified = _verified_current_release(managed_root)
    if (
        cursor_skill_payload._release_id(verified)
        != journal["target_skill_release_id"]
    ):
        raise CursorActivationError(
            "repair_required: missing Cursor cache release does not match "
            "protected state"
        )
    prepared = cursor_skill_payload.prepare_verified_payload(
        managed_root,
        verified,
        transaction_id=str(uuid.uuid4()),
        installed_at=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
    )
    recovered = dict(journal)
    recovered["target_skill_manifest_sha256"] = prepared.manifest_sha256
    for skill, item in recovered["per_skill_publish_state"].items():
        record, _records = cursor_skill_payload._manifest_skill_record(
            prepared.root,
            skill,
            expected_manifest_sha256=prepared.manifest_sha256,
        )
        if record.get("tree_sha256") != item["tree_sha256"]:
            raise CursorActivationError(
                "repair_required: rebuilt Cursor cache does not match journal"
            )
    _validated_install_journal(recovered, managed_root=managed_root)
    _write_journal(recovered)
    return prepared.root, recovered, True


def _install_publications(
    journal: Dict[str, Any],
) -> Tuple[cursor_skill_payload.StagedSkillPublication, ...]:
    staging = Path(journal["staging_root"])
    if not journal["per_skill_publish_state"]:
        return ()
    values = []
    for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
        item = journal["per_skill_publish_state"][skill]
        values.append(
            cursor_skill_payload.StagedSkillPublication(
                skill=skill,
                staged=staging / skill,
                target=Path(item["target_path"]),
                tree_sha256=item["tree_sha256"],
                object_identity=tuple(item["object_identity"]),
            )
        )
    return tuple(values)


def _observed_skill_states(
    journal: Dict[str, Any],
    cache_root: Path,
) -> Dict[str, str]:
    return {
        publication.skill: cursor_skill_payload.initial_skill_state(
            cache_root,
            publication.target,
            publication.skill,
            expected_manifest_sha256=journal[
                "target_skill_manifest_sha256"
            ],
            expected_object_identity=publication.object_identity,
        )
        for publication in _install_publications(journal)
    }


def _cleanup_transaction_staging(journal: Dict[str, Any]) -> None:
    transaction_root = _transaction_root(journal)
    staging_base = transaction_root.parent
    cursor_skill_payload._safe_remove_staging(
        transaction_root,
        staging_base,
    )
    if transaction_root.exists() or managed_install._is_link_like(
        transaction_root
    ):
        raise CursorActivationError(
            "repair_required: Cursor transaction staging could not be removed"
        )


def _remove_journal_and_staging(journal: Dict[str, Any]) -> None:
    _cleanup_transaction_staging(journal)
    path = activation_journal_path()
    try:
        if managed_install._is_link_like(path):
            raise CursorActivationError(
                "repair_required: Cursor activation journal is unsafe"
            )
        path.unlink()
        if os.name != "nt":
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except CursorActivationError:
        raise
    except OSError as exc:
        raise CursorActivationError(
            "Cursor activation journal could not be removed"
        ) from exc


def _observed_initial_mcp_state(
    journal: Dict[str, Any],
) -> Tuple[str, bool]:
    """Return pre/target/third plus whether the full file hash is exact."""

    existed, _mtime, digest, data, _raw = (
        mcp_config._read_cursor_config_snapshot(
            mcp_config.agent_config_path("cursor")
        )
    )
    config_hash = digest if existed else None
    servers = data.get("mcpServers")
    if servers is None:
        actual = None
    elif not isinstance(servers, dict):
        raise CursorActivationError(
            "repair_required: Cursor MCP server collection is invalid"
        )
    else:
        actual = servers.get(mcp_config.DEFAULT_SERVER_NAME)
        if actual is not None and not isinstance(actual, dict):
            raise CursorActivationError(
                "repair_required: Cursor MCP entry is invalid"
            )
    projection = mcp_config._managed_entry_projection(actual)
    pre_hash = journal["pre_mcp_file_hash"]
    post_hash = journal["post_mcp_file_hash"]
    pre_projection = journal["pre_mcp_managed_entry"]
    post_projection = journal["post_mcp_managed_entry"]
    if config_hash == pre_hash and projection == pre_projection:
        return "pre", True
    if config_hash == post_hash and projection == post_projection:
        return "target", True
    if projection == pre_projection:
        return "pre", False
    if projection == post_projection:
        return "target", False
    return "third", False


def _rollback_initial(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
    cache_root: Path,
) -> Dict[str, Any]:
    states = _observed_skill_states(journal, cache_root)
    if any(state == "third" for state in states.values()):
        raise CursorActivationError(
            "repair_required: Cursor skill contains a third-state artifact"
        )
    pre_hash = journal["pre_mcp_file_hash"]
    post_hash = journal["post_mcp_file_hash"]
    config_quarantine = _mcp_quarantine_path(journal)
    mcp_state, exact_mcp_hash = _observed_initial_mcp_state(journal)
    if mcp_state == "third":
        raise CursorActivationError(
            "repair_required: Cursor MCP configuration is a third state"
        )
    record_hash = client_host_ownership.record_file_sha256_if_present("cursor")
    record_quarantine = _ownership_quarantine_path(journal)
    if (
        record_hash == journal["post_ownership_record_hash"]
        or record_quarantine.exists()
        or managed_install._is_link_like(record_quarantine)
    ):
        client_host_ownership.remove_initial_record(
            managed_root=managed_root,
            expected_sha256=journal["post_ownership_record_hash"],
            quarantine_path=record_quarantine,
        )
    elif record_hash is not None:
        raise CursorActivationError(
            "repair_required: Cursor ownership record is a third state"
        )

    if config_quarantine.exists() or managed_install._is_link_like(
        config_quarantine
    ):
        preimage = _read_preimage(journal)
        mcp_config.restore_cursor_config_preimage(
            mcp_config.agent_config_path("cursor"),
            pre_existed=pre_hash is not None,
            pre_bytes=preimage,
            pre_file_sha256=pre_hash,
            expected_current_sha256=post_hash,
            quarantine_path=config_quarantine,
        )
    elif mcp_state == "target":
        if exact_mcp_hash and post_hash != pre_hash:
            preimage = _read_preimage(journal)
            mcp_config.restore_cursor_config_preimage(
                mcp_config.agent_config_path("cursor"),
                pre_existed=pre_hash is not None,
                pre_bytes=preimage,
                pre_file_sha256=pre_hash,
                expected_current_sha256=post_hash,
                quarantine_path=config_quarantine,
            )
        elif journal["pre_mcp_managed_entry"] is None:
            mcp_config.rollback_cursor_initial_entry_preserving_unrelated(
                mcp_config.agent_config_path("cursor"),
                expected_target_entry=journal["post_mcp_managed_entry"],
            )

    for publication in reversed(_install_publications(journal)):
        quarantine = _skill_quarantine_path(journal, publication.skill)
        if (
            states[publication.skill] == "target"
            or quarantine.exists()
            or managed_install._is_link_like(quarantine)
        ):
            cursor_skill_payload.remove_initial_skill(
                cache_root,
                publication.target,
                publication.skill,
                expected_manifest_sha256=journal[
                    "target_skill_manifest_sha256"
                ],
                expected_object_identity=publication.object_identity,
                quarantine_path=quarantine,
            )
    final_states = _observed_skill_states(journal, cache_root)
    if any(state != "pre" for state in final_states.values()):
        raise CursorActivationError(
            "repair_required: Cursor skill contains a third-state artifact"
        )
    _remove_journal_and_staging(journal)
    return {"action": "rolled_back", "phase": "pre"}


def _target_record_from_install(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
) -> client_host_ownership.OwnershipRecord:
    return _record_from_journal(
        journal["post_ownership_record"],
        managed_root=managed_root,
        managed_entry=journal["post_mcp_managed_entry"],
    )


def _verify_initial_target(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
    cache_root: Path,
) -> None:
    mcp_state, _exact_mcp_hash = _observed_initial_mcp_state(journal)
    if mcp_state != "target":
        raise CursorActivationError(
            "repair_required: Cursor MCP target verification failed"
        )
    if (
        client_host_ownership.record_file_sha256_if_present("cursor")
        != journal["post_ownership_record_hash"]
    ):
        raise CursorActivationError(
            "repair_required: Cursor ownership target verification failed"
        )
    if set(_observed_skill_states(journal, cache_root).values()) != {"target"}:
        raise CursorActivationError(
            "repair_required: Cursor skill target verification failed"
        )
    target_record = _target_record_from_install(
        journal,
        managed_root=managed_root,
    )
    persisted = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=managed_root,
        config_path=target_record.config_path,
        server_name=target_record.server_name,
    )
    actual = mcp_config.read_entry("cursor", target_record.server_name)
    if (
        persisted != target_record
        or mcp_config._managed_entry_projection(actual)
        != journal["post_mcp_managed_entry"]
    ):
        raise CursorActivationError(
            "repair_required: Cursor initial target does not match"
        )


def _verify_committed_initial_target(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
) -> None:
    """Verify durable ownership without pinning unrelated user bytes or old cache."""

    target_record = _target_record_from_install(
        journal,
        managed_root=managed_root,
    )
    persisted = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=managed_root,
        config_path=target_record.config_path,
        server_name=target_record.server_name,
    )
    actual = mcp_config.read_entry("cursor", target_record.server_name)
    if (
        persisted != target_record
        or mcp_config._managed_entry_projection(actual)
        != journal["post_mcp_managed_entry"]
    ):
        raise CursorActivationError(
            "repair_required: Cursor committed ownership does not match"
        )


def _owned_initial_matches_release(
    *,
    managed_root: Path,
    verified_release: release_contract.VerifiedRelease,
) -> bool:
    config_path = mcp_config.agent_config_path("cursor")
    record = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=managed_root,
        config_path=config_path,
        server_name=mcp_config.DEFAULT_SERVER_NAME,
    )
    if record is None:
        return False
    actual = mcp_config.read_entry("cursor", record.server_name)
    if (
        not isinstance(actual, dict)
        or client_host_ownership.managed_entry_sha256_v1(actual)
        != record.managed_fields_sha256
    ):
        raise CursorActivationError(
            "same_name_user_modified: Cursor managed fields changed"
        )
    expected_release_id = cursor_skill_payload._release_id(verified_release)
    if record.skill_release_id != expected_release_id:
        return False
    try:
        observed_release_id = (
            cursor_skill_payload.verify_active_skills_for_release(
                managed_root,
                verified_release,
                mcp_config.CLIENT_SPECS["cursor"].skills_path(),
            )
        )
    except cursor_skill_payload.CursorSkillPayloadError as exc:
        raise CursorActivationError(
            "repair_required: Cursor active skill integrity does not match "
            "the verified release"
        ) from exc
    if observed_release_id != record.skill_release_id:
        raise CursorActivationError(
            "repair_required: Cursor active skill release does not match "
            "ownership"
        )
    return True


def _verify_owned_active_skills(
    record: client_host_ownership.OwnershipRecord,
    *,
    managed_root: Path,
) -> None:
    """Verify owned active skills from cache, or from protected release state."""

    if not cursor_skill_payload._RELEASE_ID_RE.fullmatch(
        record.skill_release_id
    ):
        raise CursorActivationError(
            "repair_required: Cursor owned skill release is invalid"
        )
    cache_root = (
        cursor_activation_root()
        / "skill-payloads"
        / record.skill_release_id
    )
    destination = mcp_config.CLIENT_SPECS["cursor"].skills_path()
    try:
        digest = cursor_skill_payload.verify_cached_payload(cache_root)
    except cursor_skill_payload.CursorSkillPayloadError:
        digest = None
    if digest == record.skill_manifest_sha256:
        try:
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                root = destination / skill
                cursor_skill_payload.verify_skill_copy(
                    cache_root,
                    root,
                    skill,
                    expected_manifest_sha256=record.skill_manifest_sha256,
                )
                if cursor_skill_payload._tree_has_nondefault_windows_stream(
                    root
                ):
                    raise cursor_skill_payload.CursorSkillPayloadError(
                        "owned active skill contains alternate streams"
                    )
            return
        except cursor_skill_payload.CursorSkillPayloadError as exc:
            raise CursorActivationError(
                "repair_required: Cursor active skill integrity does not match "
                "the owned payload"
            ) from exc
    verified = _verified_current_release(managed_root)
    if (
        cursor_skill_payload._release_id(verified)
        != record.skill_release_id
    ):
        raise CursorActivationError(
            "repair_required: Cursor owned skill release does not match "
            "protected state"
        )
    try:
        cursor_skill_payload.verify_active_skills_for_release(
            managed_root,
            verified,
            destination,
        )
    except cursor_skill_payload.CursorSkillPayloadError as exc:
        raise CursorActivationError(
            "repair_required: Cursor active skill integrity does not match "
            "the verified release"
        ) from exc


def verify_owned_active_skills_for_doctor(
    record: client_host_ownership.OwnershipRecord,
    *,
    managed_root: Path,
) -> None:
    """Read-only owned-skill snapshot for Doctor; never repairs or publishes."""

    try:
        canonical = managed_install.canonical_managed_root(managed_root)
        managed_install._require_fixed_managed_root(canonical)
    except managed_install.ManagedInstallError as exc:
        raise CursorActivationError("managed install identity is invalid") from exc
    _verify_owned_active_skills(record, managed_root=canonical)


def _verified_current_release(
    managed_root: Path,
) -> release_contract.VerifiedRelease:
    """Reconstruct typed release authority from protected state and Git objects."""

    try:
        state = updater._read_update_state(managed_root)
        if state.channel != managed_install.CHANNEL:
            raise CursorActivationError(
                "repair_required: protected release channel is not stable"
            )
        reader = updater._GitReader(managed_root)
        _code, head_output = reader.run("head")
        head = updater._single_commit(head_output, "Cursor activation HEAD")
        if head != state.last_release_commit:
            raise CursorActivationError(
                "repair_required: protected release does not match HEAD"
            )
        _code, tree_output = reader.run(
            "target_tree",
            commit=state.last_release_commit,
        )
        wanted = {
            "release/manifest.json": None,
            "release/manifest.sig.json": None,
        }
        for entry in updater._parse_tree_entries(tree_output):
            if entry.path in wanted:
                if entry.mode not in {"100644", "100755"}:
                    raise CursorActivationError(
                        "repair_required: release authority is not a regular blob"
                    )
                wanted[entry.path] = entry.object_id
        if any(value is None for value in wanted.values()):
            raise CursorActivationError(
                "repair_required: release authority is missing"
            )
        manifest = release_contract.parse_release_manifest(
            cursor_skill_payload._read_verified_blob(
                reader,
                wanted["release/manifest.json"],
            )
        )
        signature = release_contract.parse_release_signature(
            cursor_skill_payload._read_verified_blob(
                reader,
                wanted["release/manifest.sig.json"],
            )
        )
        trusted = release_acquisition.load_trusted_release_keys(managed_root)
        verified = release_contract.verify_release_signature(
            manifest,
            signature,
            trusted,
            expected_repository_id=managed_install.REPOSITORY_ID,
            expected_channel=state.channel,
        )
    except CursorActivationError:
        raise
    except (
        cursor_skill_payload.CursorSkillPayloadError,
        managed_install.ManagedInstallError,
        release_acquisition.ReleaseAcquisitionError,
        release_contract.ReleaseContractError,
        updater.UpdateInspectionError,
    ) as exc:
        raise CursorActivationError(
            "repair_required: current protected release could not be verified"
        ) from exc
    if (
        verified.manifest.commit != state.last_release_commit
        or verified.manifest.release_sequence != state.last_release_sequence
        or verified.manifest.version != state.last_version
        or release_contract.manifest_sha256(verified.manifest)
        != state.last_manifest_sha256
        or not release_contract.python_is_compatible(
            verified.manifest,
            tuple(sys.version_info[:3]),
        )
    ):
        raise CursorActivationError(
            "repair_required: current protected release state does not match"
        )
    return verified


def _resume_initial(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
    fault_injector: _FaultInjector = None,
) -> Dict[str, Any]:
    cache_root, recovery_journal, rebuilt_cache = (
        _install_cache_root_for_recovery(
            journal,
            managed_root=managed_root,
        )
    )
    states = _observed_skill_states(recovery_journal, cache_root)
    if rebuilt_cache:
        return _rollback_initial(
            recovery_journal,
            managed_root=managed_root,
            cache_root=cache_root,
        )
    record_hash = client_host_ownership.record_file_sha256_if_present("cursor")
    mcp_state, _exact_mcp_hash = _observed_initial_mcp_state(journal)
    if mcp_state == "third":
        raise CursorActivationError(
            "repair_required: Cursor MCP configuration is a third state"
        )
    if record_hash not in {None, journal["post_ownership_record_hash"]}:
        raise CursorActivationError(
            "repair_required: Cursor ownership record is a third state"
        )
    target_mcp_observed = mcp_state == "target"
    if not target_mcp_observed:
        return _rollback_initial(
            journal,
            managed_root=managed_root,
            cache_root=cache_root,
        )
    if set(states.values()) != {"target"}:
        return _rollback_initial(
            journal,
            managed_root=managed_root,
            cache_root=cache_root,
        )
    if record_hash is None:
        target_record = _target_record_from_install(
            journal,
            managed_root=managed_root,
        )
        journal = _phase(journal, "ownership_publishing")
        if fault_injector is not None:
            fault_injector("before_ownership_apply")
        client_host_ownership.publish_record(
            target_record,
            managed_root=managed_root,
            expected_exists=False,
            expected_sha256=None,
        )
        if fault_injector is not None:
            fault_injector("after_ownership_apply_before_phase")
        journal = _phase(journal, "ownership_published")
    journal = _phase(journal, "verifying")
    _verify_initial_target(
        journal,
        managed_root=managed_root,
        cache_root=cache_root,
    )
    journal = _phase(journal, "committed")
    _cleanup_transaction_staging(journal)
    return {"action": "recovered", "phase": journal["phase"]}


def _execute_initial(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
    plan: mcp_config.CursorEntryWritePlan,
    publications: Tuple[cursor_skill_payload.StagedSkillPublication, ...],
    target_record: client_host_ownership.OwnershipRecord,
    fault_injector: _FaultInjector,
) -> Dict[str, Any]:
    cache_root = _install_cache_root(journal)
    journal = _phase(journal, "skills_publishing")
    for publication in publications:
        item = dict(journal["per_skill_publish_state"][publication.skill])
        item["state"] = "publishing"
        states = dict(journal["per_skill_publish_state"])
        states[publication.skill] = item
        updated = dict(journal)
        updated["per_skill_publish_state"] = states
        _write_journal(updated)
        journal = updated
        if fault_injector is not None:
            fault_injector("before_skill_apply:%s" % publication.skill)
        cursor_skill_payload.publish_staged_skill(
            publication,
            cache_root,
            expected_manifest_sha256=journal[
                "target_skill_manifest_sha256"
            ],
        )
        if fault_injector is not None:
            fault_injector("after_skill_publish:%s" % publication.skill)
        item = dict(journal["per_skill_publish_state"][publication.skill])
        item["state"] = "target_published"
        states = dict(journal["per_skill_publish_state"])
        states[publication.skill] = item
        updated = dict(journal)
        updated["per_skill_publish_state"] = states
        _write_journal(updated)
        journal = updated
    if set(_observed_skill_states(journal, cache_root).values()) != {"target"}:
        raise CursorActivationError("Cursor staged skill publication is incomplete")
    journal = _phase(journal, "skills_published")
    if fault_injector is not None:
        fault_injector("after_skills_published")
    journal = _phase(journal, "mcp_publishing")
    if fault_injector is not None:
        fault_injector("before_mcp_apply")
    mcp_config.apply_cursor_entry_write(plan)
    if fault_injector is not None:
        fault_injector("after_mcp_apply_before_phase")
    journal = _phase(journal, "mcp_published")
    if fault_injector is not None:
        fault_injector("after_mcp_published")
    journal = _phase(journal, "ownership_publishing")
    if fault_injector is not None:
        fault_injector("before_ownership_apply")
    client_host_ownership.publish_record(
        target_record,
        managed_root=managed_root,
        expected_exists=False,
        expected_sha256=None,
    )
    if fault_injector is not None:
        fault_injector("after_ownership_apply_before_phase")
    journal = _phase(journal, "ownership_published")
    journal = _phase(journal, "verifying")
    _verify_initial_target(
        journal,
        managed_root=managed_root,
        cache_root=cache_root,
    )
    journal = _phase(journal, "committed")
    _cleanup_transaction_staging(journal)
    return {"action": "installed", "phase": journal["phase"]}


def activate_cursor_initial(
    *,
    managed_root: Path,
    verified_release: release_contract.VerifiedRelease,
    python: Optional[Path] = None,
    installed_at: Optional[str] = None,
    fault_injector: _FaultInjector = None,
) -> Dict[str, Any]:
    """Publish first-install Cursor skills, MCP entry and ownership together."""

    try:
        canonical = managed_install.canonical_managed_root(managed_root)
        managed_install._require_fixed_managed_root(canonical)
    except managed_install.ManagedInstallError as exc:
        raise CursorActivationError("managed install identity is invalid") from exc
    if not isinstance(verified_release, release_contract.VerifiedRelease):
        raise CursorActivationError("Cursor activation requires a verified release")
    timestamp = installed_at or time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(),
    )
    with cursor_activation_lock():
        existing = _load_journal_if_present(managed_root=canonical)
        if existing is not None and existing["phase"] != "committed":
            if existing["operation"] != "install":
                raise CursorActivationError(
                    "repair_required: another Cursor transaction is incomplete"
                )
            recovered = _resume_initial(
                existing,
                managed_root=canonical,
                fault_injector=fault_injector,
            )
            if recovered["action"] == "recovered":
                return recovered
        elif existing is not None and existing["operation"] == "install":
            _verify_committed_initial_target(
                existing,
                managed_root=canonical,
            )
            _cleanup_transaction_staging(existing)
        elif (
            existing is not None
            and existing.get("schema") == LIFECYCLE_JOURNAL_SCHEMA
            and existing.get("operation") == "uninstall"
        ):
            _verify_committed_lifecycle_state(existing, managed_root=canonical)
            if _cleanup_committed_lifecycle(
                existing,
                managed_root=canonical,
            ):
                raise CursorActivationError(
                    "repair_required: committed Cursor cleanup is pending"
                )

        requested = _validate_repair_interpreter(
            Path(sys.executable) if python is None else Path(python),
            canonical,
        )
        if _owned_initial_matches_release(
            managed_root=canonical,
            verified_release=verified_release,
        ):
            return {"action": "unchanged", "phase": "committed"}
        config_path = mcp_config.agent_config_path("cursor")
        if client_host_ownership.read_record_if_present(
            "cursor",
            managed_root=canonical,
            config_path=config_path,
            server_name=mcp_config.DEFAULT_SERVER_NAME,
        ) is not None:
            raise CursorActivationError(
                "same_name_owned: Cursor is already managed"
            )
        transaction_id = str(uuid.uuid4())
        prepared = cursor_skill_payload.prepare_verified_payload(
            canonical,
            verified_release,
            transaction_id=transaction_id,
            installed_at=timestamp,
        )
        desired = mcp_config.render_entry(
            client="cursor",
            python=requested,
            cwd=canonical,
        )["mcpServers"][mcp_config.DEFAULT_SERVER_NAME]
        plan = mcp_config.prepare_cursor_entry_write(desired)
        target_record = client_host_ownership.build_record(
            "cursor",
            managed_root=canonical,
            config_path=config_path,
            server_name=mcp_config.DEFAULT_SERVER_NAME,
            managed_entry=plan.target_entry,
            skill_release_id=prepared.release_id,
            skill_manifest_sha256=prepared.manifest_sha256,
        )
        staging_root = cursor_activation_root() / "staging" / transaction_id / "skills"
        journal = {
            "schema": INSTALL_JOURNAL_SCHEMA,
            "transaction_id": transaction_id,
            "install_id": target_record.install_id,
            "operation": "install",
            "phase": "intent",
            "target_present": True,
            "staging_root": str(staging_root),
            "pre_mcp_managed_entry": plan.pre_managed_entry,
            "pre_mcp_file_hash": plan.pre_file_sha256,
            "post_mcp_managed_entry": plan.post_managed_entry,
            "post_mcp_file_hash": plan.post_file_sha256,
            "pre_ownership_record": None,
            "pre_ownership_record_hash": None,
            "post_ownership_record": client_host_ownership.record_payload(
                target_record
            ),
            "post_ownership_record_hash": client_host_ownership.record_sha256(
                target_record
            ),
            "pre_skill_release_id": None,
            "target_skill_release_id": prepared.release_id,
            "pre_skill_manifest_sha256": None,
            "target_skill_manifest_sha256": prepared.manifest_sha256,
            "per_skill_publish_state": {},
        }
        _validated_install_journal(journal, managed_root=canonical)
        _write_journal(journal)
        if fault_injector is not None:
            fault_injector("after_intent_before_staging")
        publications = cursor_skill_payload.stage_initial_publication(
            prepared,
            mcp_config.CLIENT_SPECS["cursor"].skills_path(),
            transaction_id=transaction_id,
        )
        updated = dict(journal)
        updated["per_skill_publish_state"] = {
                publication.skill: {
                    "state": "pending",
                    "target_path": str(publication.target),
                    "tree_sha256": publication.tree_sha256,
                    "object_identity": publication.object_identity,
                }
                for publication in publications
        }
        _validated_install_journal(updated, managed_root=canonical)
        _write_journal(updated)
        journal = updated
        if fault_injector is not None:
            fault_injector("after_intent_before_preimage")
        if plan.existed:
            _write_preimage(
                staging_root.parent / "mcp-preimage.bin",
                plan.pre_bytes,
            )
        if fault_injector is not None:
            fault_injector("after_preimage")
        if fault_injector is not None:
            fault_injector("after_intent")
        return _execute_initial(
            journal,
            managed_root=canonical,
            plan=plan,
            publications=publications,
            target_record=target_record,
            fault_injector=fault_injector,
        )


def _owned_cache_root(
    record: client_host_ownership.OwnershipRecord,
) -> Path:
    root = cursor_activation_root() / "skill-payloads" / record.skill_release_id
    try:
        digest = cursor_skill_payload.verify_cached_payload(root)
    except cursor_skill_payload.CursorSkillPayloadError as exc:
        raise CursorActivationError(
            "repair_required: Cursor owned payload cache is invalid"
        ) from exc
    if digest != record.skill_manifest_sha256:
        raise CursorActivationError(
            "repair_required: Cursor owned payload cache does not match ownership"
        )
    return root


def _owned_record_and_entry(
    managed_root: Path,
) -> Tuple[client_host_ownership.OwnershipRecord, Dict[str, Any], str, str]:
    config_path = mcp_config.agent_config_path("cursor")
    record = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=managed_root,
        config_path=config_path,
        server_name=mcp_config.DEFAULT_SERVER_NAME,
    )
    if record is None:
        raise CursorActivationError(
            "same_name_unowned: Cursor ownership record is absent"
        )
    entry = mcp_config.read_entry("cursor", record.server_name)
    if (
        not isinstance(entry, dict)
        or client_host_ownership.managed_entry_sha256_v1(entry)
        != record.managed_fields_sha256
    ):
        raise CursorActivationError(
            "same_name_user_modified: Cursor managed fields changed"
        )
    config_hash = mcp_config.cursor_config_file_sha256(config_path)
    record_hash = client_host_ownership.record_file_sha256_if_present("cursor")
    if config_hash is None or record_hash is None:
        raise CursorActivationError(
            "repair_required: Cursor owned artifacts are incomplete"
        )
    return record, mcp_config._managed_entry_projection(entry), config_hash, record_hash


def _pre_skill_states(
    record: client_host_ownership.OwnershipRecord,
    cache_root: Path,
) -> Dict[str, Dict[str, Any]]:
    destination = mcp_config.CLIENT_SPECS["cursor"].skills_path()
    states: Dict[str, Dict[str, Any]] = {}
    for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
        target = destination / skill
        try:
            identity = cursor_skill_payload._directory_identity(target)
            tree = cursor_skill_payload.verify_skill_copy(
                cache_root,
                target,
                skill,
                expected_manifest_sha256=record.skill_manifest_sha256,
            )
            if cursor_skill_payload._tree_has_nondefault_windows_stream(target):
                raise cursor_skill_payload.CursorSkillPayloadError(
                    "owned active skill contains alternate streams"
                )
        except cursor_skill_payload.CursorSkillPayloadError as exc:
            raise CursorActivationError(
                "user_modified: Cursor active skill is not exact owned content"
            ) from exc
        states[skill] = {
            "state": "pending",
            "target_path": str(target),
            "quarantine_path": "",
            "pre_tree_sha256": tree,
            "pre_object_identity": list(identity),
            "target_tree_sha256": None,
            "target_object_identity": None,
        }
    return states


def _set_lifecycle_skill_state(
    journal: Dict[str, Any],
    skill: str,
    state: str,
) -> Dict[str, Any]:
    item = dict(journal["per_skill_publish_state"][skill])
    item["state"] = state
    states = dict(journal["per_skill_publish_state"])
    states[skill] = item
    updated = dict(journal)
    updated["per_skill_publish_state"] = states
    _write_journal(updated)
    return updated


def _require_protected_target(
    managed_root: Path,
    verified_release: release_contract.VerifiedRelease,
) -> None:
    protected = _verified_current_release(managed_root)
    if protected != verified_release:
        raise CursorActivationError(
            "repair_required: target release does not match protected state"
        )


def _lifecycle_journal_base(
    *,
    operation: str,
    transaction_id: str,
    managed_root: Path,
    record: client_host_ownership.OwnershipRecord,
    entry: Dict[str, Any],
    config_hash: str,
    record_hash: str,
    states: Dict[str, Dict[str, Any]],
    target_record: Optional[client_host_ownership.OwnershipRecord],
    target_manifest_sha256: Optional[str],
) -> Dict[str, Any]:
    transaction_root = cursor_activation_root() / "staging" / transaction_id
    for skill, item in states.items():
        item["quarantine_path"] = str(
            transaction_root / "backups" / "skills" / skill
        )
    return {
        "schema": LIFECYCLE_JOURNAL_SCHEMA,
        "transaction_id": transaction_id,
        "install_id": client_host_ownership._paired_install_id(managed_root),
        "operation": operation,
        "phase": "intent",
        "target_present": operation == "update",
        "staging_root": str(transaction_root / "skills"),
        "pre_mcp_managed_entry": entry,
        "pre_mcp_file_hash": config_hash,
        "post_mcp_managed_entry": entry if operation == "update" else None,
        "post_mcp_file_hash": config_hash if operation == "update" else None,
        "pre_ownership_record": client_host_ownership.record_payload(record),
        "pre_ownership_record_hash": record_hash,
        "post_ownership_record": (
            client_host_ownership.record_payload(target_record)
            if target_record is not None
            else None
        ),
        "post_ownership_record_hash": (
            client_host_ownership.record_sha256(target_record)
            if target_record is not None
            else None
        ),
        "pre_skill_release_id": record.skill_release_id,
        "target_skill_release_id": (
            target_record.skill_release_id if target_record is not None else None
        ),
        "pre_skill_manifest_sha256": record.skill_manifest_sha256,
        "target_skill_manifest_sha256": target_manifest_sha256,
        "per_skill_publish_state": states,
    }


def _cleanup_lifecycle_backups(
    journal: Dict[str, Any],
    *,
    pre_cache_root: Path,
) -> None:
    for skill, item in journal["per_skill_publish_state"].items():
        cursor_skill_payload.delete_quarantined_skill(
            pre_cache_root,
            Path(item["quarantine_path"]),
            skill,
            expected_manifest_sha256=journal["pre_skill_manifest_sha256"],
            expected_object_identity=tuple(item["pre_object_identity"]),
        )
    _cleanup_transaction_staging(journal)


def _verify_committed_lifecycle_state(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
) -> None:
    """Verify the durable target/absence before retrying best-effort cleanup."""

    projection = mcp_config._managed_entry_projection(
        mcp_config.read_entry("cursor")
    )
    if journal["operation"] == "uninstall":
        if (
            client_host_ownership.record_file_sha256_if_present("cursor")
            is not None
            or projection not in (None, {})
            or any(
                Path(item["target_path"]).exists()
                or managed_install._is_link_like(Path(item["target_path"]))
                for item in journal["per_skill_publish_state"].values()
            )
        ):
            raise CursorActivationError(
                "repair_required: committed Cursor uninstall is inconsistent"
            )
        return
    target_record = _record_from_journal(
        journal["post_ownership_record"],
        managed_root=managed_root,
        managed_entry=journal["post_mcp_managed_entry"],
    )
    if (
        client_host_ownership.record_file_sha256_if_present("cursor")
        != journal["post_ownership_record_hash"]
        or projection != journal["post_mcp_managed_entry"]
    ):
        raise CursorActivationError(
            "repair_required: committed Cursor update is inconsistent"
        )
    target_cache = _owned_cache_root(target_record)
    for skill, item in journal["per_skill_publish_state"].items():
        if cursor_skill_payload.owned_skill_state(
            target_cache,
            Path(item["target_path"]),
            skill,
            expected_manifest_sha256=journal["target_skill_manifest_sha256"],
            expected_object_identity=tuple(item["target_object_identity"]),
        ) != "owned":
            raise CursorActivationError(
                "repair_required: committed Cursor update is inconsistent"
            )


def _cleanup_committed_lifecycle(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
) -> bool:
    """Retry safe cleanup; return True without undoing durable success."""

    pre_record = _record_from_journal(
        journal["pre_ownership_record"],
        managed_root=managed_root,
        managed_entry=journal["pre_mcp_managed_entry"],
    )
    pending = False
    backups_clean = True
    try:
        _cleanup_lifecycle_backups(
            journal,
            pre_cache_root=(
                cursor_activation_root()
                / "skill-payloads"
                / pre_record.skill_release_id
            ),
        )
    except (CursorActivationError, cursor_skill_payload.CursorSkillPayloadError):
        pending = True
        backups_clean = False
    if journal["operation"] == "uninstall" and not backups_clean:
        return True
    keep_release_ids = (
        frozenset(
            {
                journal["pre_skill_release_id"],
                journal["target_skill_release_id"],
            }
        )
        if journal["operation"] == "update"
        else frozenset()
    )
    try:
        skipped = cursor_skill_payload.cleanup_committed_payloads(
            cursor_activation_root(),
            keep_release_ids=keep_release_ids,
        )
    except cursor_skill_payload.CursorSkillPayloadError:
        pending = True
    else:
        pending = pending or skipped > 0
    return pending


def update_cursor_owned(
    *,
    managed_root: Path,
    verified_release: release_contract.VerifiedRelease,
    installed_at: Optional[str] = None,
    fault_injector: _FaultInjector = None,
) -> Dict[str, Any]:
    """Replace exact owned Cursor skills with one newer protected release."""

    try:
        canonical = managed_install.canonical_managed_root(managed_root)
        managed_install._require_fixed_managed_root(canonical)
    except managed_install.ManagedInstallError as exc:
        raise CursorActivationError("managed install identity is invalid") from exc
    timestamp = installed_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with cursor_activation_lock():
        existing = _load_journal_if_present(managed_root=canonical)
        if existing is not None and existing["phase"] != "committed":
            raise CursorActivationError(
                "repair_required: incomplete Cursor lifecycle transaction exists"
            )
        if (
            existing is not None
            and existing.get("schema") == LIFECYCLE_JOURNAL_SCHEMA
        ):
            _verify_committed_lifecycle_state(existing, managed_root=canonical)
            cleanup_pending = _cleanup_committed_lifecycle(
                existing,
                managed_root=canonical,
            )
            requested_release_id = cursor_skill_payload._release_id(
                verified_release
            )
            if (
                existing["operation"] == "update"
                and existing["target_skill_release_id"] == requested_release_id
            ):
                result = {"action": "unchanged", "phase": "committed"}
                if cleanup_pending:
                    result["cleanup_pending"] = True
                return result
            if cleanup_pending:
                raise CursorActivationError(
                    "repair_required: committed Cursor cleanup is pending"
                )
        _require_protected_target(canonical, verified_release)
        record, entry, config_hash, record_hash = _owned_record_and_entry(canonical)
        target_release_id = cursor_skill_payload._release_id(verified_release)
        try:
            client_host_lifecycle.require_monotonic_release(
                record.skill_release_id,
                target_release_id,
            )
        except ValueError as exc:
            raise CursorActivationError(str(exc)) from exc
        pre_cache = _owned_cache_root(record)
        states = _pre_skill_states(record, pre_cache)
        if cursor_skill_payload.cleanup_committed_payloads(
            cursor_activation_root(),
            keep_release_ids=frozenset({record.skill_release_id}),
        ):
            raise CursorActivationError(
                "repair_required: invalid Cursor payload cache blocks update"
            )
        transaction_id = str(uuid.uuid4())
        try:
            prepared = cursor_skill_payload.prepare_verified_payload(
                canonical,
                verified_release,
                transaction_id=transaction_id,
                installed_at=timestamp,
                state_root=cursor_activation_root(),
            )
            publications = cursor_skill_payload.stage_initial_publication(
                prepared,
                mcp_config.CLIENT_SPECS["cursor"].skills_path(),
                transaction_id=transaction_id,
                replace_owned_from=pre_cache,
                replace_owned_manifest_sha256=record.skill_manifest_sha256,
            )
        except cursor_skill_payload.CursorSkillPayloadError as exc:
            raise CursorActivationError(str(exc)) from exc
        publication_by_skill = {item.skill: item for item in publications}
        for skill, item in states.items():
            publication = publication_by_skill[skill]
            item["target_tree_sha256"] = publication.tree_sha256
            item["target_object_identity"] = list(publication.object_identity)
        target_record = client_host_ownership.build_record(
            "cursor",
            managed_root=canonical,
            config_path=mcp_config.agent_config_path("cursor"),
            server_name=record.server_name,
            managed_entry=entry,
            skill_release_id=prepared.release_id,
            skill_manifest_sha256=prepared.manifest_sha256,
        )
        journal = _lifecycle_journal_base(
            operation="update",
            transaction_id=transaction_id,
            managed_root=canonical,
            record=record,
            entry=entry,
            config_hash=config_hash,
            record_hash=record_hash,
            states=states,
            target_record=target_record,
            target_manifest_sha256=prepared.manifest_sha256,
        )
        _validated_lifecycle_journal(journal, managed_root=canonical)
        _write_journal(journal)
        if fault_injector is not None:
            fault_injector("after_intent")
        journal = _phase(journal, "skills_publishing")
        for publication in publications:
            item = journal["per_skill_publish_state"][publication.skill]
            journal = _set_lifecycle_skill_state(
                journal, publication.skill, "quarantining"
            )
            cursor_skill_payload.quarantine_owned_skill(
                pre_cache,
                publication.target,
                publication.skill,
                expected_manifest_sha256=record.skill_manifest_sha256,
                expected_object_identity=tuple(item["pre_object_identity"]),
                quarantine_path=Path(item["quarantine_path"]),
            )
            journal = _set_lifecycle_skill_state(
                journal, publication.skill, "pre_backed_up"
            )
            if fault_injector is not None:
                fault_injector("after_skill_backup:%s" % publication.skill)
            cursor_skill_payload.publish_staged_skill(
                publication,
                prepared.root,
                expected_manifest_sha256=prepared.manifest_sha256,
            )
            journal = _set_lifecycle_skill_state(
                journal, publication.skill, "target_published"
            )
            if fault_injector is not None:
                fault_injector("after_skill_publish:%s" % publication.skill)
        journal = _phase(journal, "skills_published")
        journal = _phase(journal, "mcp_publishing")
        journal = _phase(journal, "mcp_published")
        if fault_injector is not None:
            fault_injector("after_mcp_published")
        journal = _phase(journal, "ownership_publishing")
        client_host_ownership.publish_record(
            target_record,
            managed_root=canonical,
            expected_exists=True,
            expected_sha256=record_hash,
        )
        if fault_injector is not None:
            fault_injector("after_ownership_apply_before_phase")
        journal = _phase(journal, "ownership_published")
        if fault_injector is not None:
            fault_injector("after_ownership_published")
        journal = _phase(journal, "verifying")
        try:
            cursor_skill_payload.verify_active_skills_for_release(
                canonical,
                verified_release,
                mcp_config.CLIENT_SPECS["cursor"].skills_path(),
            )
        except cursor_skill_payload.CursorSkillPayloadError as exc:
            raise CursorActivationError(
                "repair_required: Cursor updated skills failed verification"
            ) from exc
        if (
            client_host_ownership.record_file_sha256_if_present("cursor")
            != journal["post_ownership_record_hash"]
            or mcp_config._managed_entry_projection(mcp_config.read_entry("cursor"))
            != entry
        ):
            raise CursorActivationError(
                "repair_required: Cursor update target failed verification"
            )
        journal = _phase(journal, "committed")
        cleanup_pending = _cleanup_committed_lifecycle(
            journal,
            managed_root=canonical,
        )
        result = {"action": "updated", "phase": "committed"}
        if cleanup_pending:
            result["cleanup_pending"] = True
        return result


def uninstall_cursor_owned(
    *,
    managed_root: Path,
    fault_injector: _FaultInjector = None,
) -> Dict[str, Any]:
    """Remove only exact Cursor-owned fields, skills, and ownership proof."""

    try:
        canonical = managed_install.canonical_managed_root(managed_root)
        managed_install._require_fixed_managed_root(canonical)
    except managed_install.ManagedInstallError as exc:
        raise CursorActivationError("managed install identity is invalid") from exc
    with cursor_activation_lock():
        existing = _load_journal_if_present(managed_root=canonical)
        if (
            existing is not None
            and existing.get("schema") == LIFECYCLE_JOURNAL_SCHEMA
            and existing.get("operation") == "uninstall"
            and existing.get("phase") == "committed"
        ):
            _verify_committed_lifecycle_state(existing, managed_root=canonical)
            cleanup_pending = _cleanup_committed_lifecycle(
                existing,
                managed_root=canonical,
            )
            result = {"action": "unchanged", "phase": "committed"}
            if cleanup_pending:
                result["cleanup_pending"] = True
            return result
        if existing is not None and existing["phase"] != "committed":
            raise CursorActivationError(
                "repair_required: incomplete Cursor lifecycle transaction exists"
            )
        record, entry, config_hash, record_hash = _owned_record_and_entry(canonical)
        pre_cache = _owned_cache_root(record)
        states = _pre_skill_states(record, pre_cache)
        transaction_id = str(uuid.uuid4())
        transaction_root = cursor_activation_root() / "staging" / transaction_id
        try:
            managed_install._ensure_private_registration_directory(
                transaction_root
            )
            managed_install._ensure_private_registration_directory(
                transaction_root / "skills"
            )
        except managed_install.ManagedInstallError as exc:
            raise CursorActivationError(
                "Cursor uninstall staging root is unsafe"
            ) from exc
        journal = _lifecycle_journal_base(
            operation="uninstall",
            transaction_id=transaction_id,
            managed_root=canonical,
            record=record,
            entry=entry,
            config_hash=config_hash,
            record_hash=record_hash,
            states=states,
            target_record=None,
            target_manifest_sha256=None,
        )
        _validated_lifecycle_journal(journal, managed_root=canonical)
        _write_journal(journal)
        if fault_injector is not None:
            fault_injector("after_intent")
        journal = _phase(journal, "skills_publishing")
        for skill, item in tuple(journal["per_skill_publish_state"].items()):
            journal = _set_lifecycle_skill_state(journal, skill, "quarantining")
            cursor_skill_payload.quarantine_owned_skill(
                pre_cache,
                Path(item["target_path"]),
                skill,
                expected_manifest_sha256=record.skill_manifest_sha256,
                expected_object_identity=tuple(item["pre_object_identity"]),
                quarantine_path=Path(item["quarantine_path"]),
            )
            journal = _set_lifecycle_skill_state(
                journal, skill, "pre_backed_up"
            )
            if fault_injector is not None:
                fault_injector("after_skill_backup:%s" % skill)
        journal = _phase(journal, "skills_published")
        journal = _phase(journal, "mcp_publishing")
        post_hash = mcp_config.remove_cursor_owned_fields_preserving_unrelated(
            mcp_config.agent_config_path("cursor"),
            expected_managed_entry=entry,
            server_name=record.server_name,
        )
        updated = dict(journal)
        updated["post_mcp_file_hash"] = post_hash
        _write_journal(updated)
        journal = updated
        if fault_injector is not None:
            fault_injector("after_mcp_apply_before_phase")
        journal = _phase(journal, "mcp_published")
        if fault_injector is not None:
            fault_injector("after_mcp_published")
        journal = _phase(journal, "ownership_publishing")
        client_host_ownership.remove_record(
            record.client,
            managed_root=canonical,
            expected_sha256=record_hash,
            quarantine_path=transaction_root / "backups" / "ownership.json",
        )
        if fault_injector is not None:
            fault_injector("after_ownership_apply_before_phase")
        journal = _phase(journal, "ownership_published")
        if fault_injector is not None:
            fault_injector("after_ownership_published")
        journal = _phase(journal, "verifying")
        if client_host_ownership.record_file_sha256_if_present("cursor") is not None:
            raise CursorActivationError(
                "repair_required: Cursor ownership removal failed verification"
            )
        if mcp_config._managed_entry_projection(mcp_config.read_entry("cursor")) not in (
            None,
            {},
        ):
            raise CursorActivationError(
                "repair_required: Cursor MCP removal failed verification"
            )
        for skill, item in journal["per_skill_publish_state"].items():
            if Path(item["target_path"]).exists() or managed_install._is_link_like(
                Path(item["target_path"])
            ):
                raise CursorActivationError(
                    "repair_required: Cursor skill removal failed verification"
                )
        journal = _phase(journal, "committed")
        cleanup_pending = _cleanup_committed_lifecycle(
            journal,
            managed_root=canonical,
        )
        result = {"action": "uninstalled", "phase": "committed"}
        if cleanup_pending:
            result["cleanup_pending"] = True
        return result


def _rollback_owned_lifecycle(
    journal: Dict[str, Any],
    *,
    managed_root: Path,
) -> Dict[str, Any]:
    """Converge an incomplete update/uninstall to its exact pre generation."""

    pre_record = _record_from_journal(
        journal["pre_ownership_record"],
        managed_root=managed_root,
        managed_entry=journal["pre_mcp_managed_entry"],
    )
    pre_cache = _owned_cache_root(pre_record)
    target_cache = (
        cursor_activation_root()
        / "skill-payloads"
        / journal["target_skill_release_id"]
        if journal["operation"] == "update"
        else None
    )
    if target_cache is not None:
        try:
            target_digest = cursor_skill_payload.verify_cached_payload(target_cache)
        except cursor_skill_payload.CursorSkillPayloadError as exc:
            raise CursorActivationError(
                "repair_required: Cursor update target cache is invalid"
            ) from exc
        if target_digest != journal["target_skill_manifest_sha256"]:
            raise CursorActivationError(
                "repair_required: Cursor update target cache changed"
            )
    transaction_root = _transaction_root(journal)
    for skill in reversed(cursor_skill_payload.CURSOR_M2_SKILLS):
        item = journal["per_skill_publish_state"][skill]
        target = Path(item["target_path"])
        quarantine = Path(item["quarantine_path"])
        pre_state = cursor_skill_payload.owned_skill_state(
            pre_cache,
            target,
            skill,
            expected_manifest_sha256=journal["pre_skill_manifest_sha256"],
            expected_object_identity=tuple(item["pre_object_identity"]),
        )
        backup_state = cursor_skill_payload.owned_skill_state(
            pre_cache,
            quarantine,
            skill,
            expected_manifest_sha256=journal["pre_skill_manifest_sha256"],
            expected_object_identity=tuple(item["pre_object_identity"]),
        )
        if pre_state == "owned" and backup_state == "missing":
            continue
        target_state = "missing"
        if target_cache is not None:
            target_state = cursor_skill_payload.owned_skill_state(
                target_cache,
                target,
                skill,
                expected_manifest_sha256=journal["target_skill_manifest_sha256"],
                expected_object_identity=tuple(item["target_object_identity"]),
            )
        if backup_state != "owned" or target_state not in {"owned", "missing"}:
            raise CursorActivationError(
                "repair_required: Cursor skill is a third-state artifact"
            )
        if target_state == "owned":
            discard = transaction_root / "rollback-targets" / skill
            cursor_skill_payload.quarantine_owned_skill(
                target_cache,
                target,
                skill,
                expected_manifest_sha256=journal[
                    "target_skill_manifest_sha256"
                ],
                expected_object_identity=tuple(item["target_object_identity"]),
                quarantine_path=discard,
            )
            cursor_skill_payload.delete_quarantined_skill(
                target_cache,
                discard,
                skill,
                expected_manifest_sha256=journal[
                    "target_skill_manifest_sha256"
                ],
                expected_object_identity=tuple(item["target_object_identity"]),
            )
        cursor_skill_payload.restore_quarantined_skill(
            pre_cache,
            target,
            skill,
            expected_manifest_sha256=journal["pre_skill_manifest_sha256"],
            expected_object_identity=tuple(item["pre_object_identity"]),
            quarantine_path=quarantine,
        )

    actual_projection = mcp_config._managed_entry_projection(
        mcp_config.read_entry("cursor")
    )
    if actual_projection != journal["pre_mcp_managed_entry"]:
        if journal["operation"] == "uninstall" and actual_projection in (
            None,
            {},
        ):
            mcp_config.restore_cursor_owned_fields_preserving_unrelated(
                mcp_config.agent_config_path("cursor"),
                expected_managed_entry=journal["pre_mcp_managed_entry"],
            )
        else:
            raise CursorActivationError(
                "repair_required: Cursor MCP configuration is a third state"
            )

    record_hash = client_host_ownership.record_file_sha256_if_present("cursor")
    if record_hash != journal["pre_ownership_record_hash"]:
        if journal["operation"] == "update" and record_hash == journal[
            "post_ownership_record_hash"
        ]:
            client_host_ownership.publish_record(
                pre_record,
                managed_root=managed_root,
                expected_exists=True,
                expected_sha256=record_hash,
            )
        elif journal["operation"] == "uninstall" and record_hash is None:
            client_host_ownership.publish_record(
                pre_record,
                managed_root=managed_root,
                expected_exists=False,
                expected_sha256=None,
            )
        else:
            raise CursorActivationError(
                "repair_required: Cursor ownership record is a third state"
            )
    if (
        client_host_ownership.record_file_sha256_if_present("cursor")
        != journal["pre_ownership_record_hash"]
        or mcp_config._managed_entry_projection(mcp_config.read_entry("cursor"))
        != journal["pre_mcp_managed_entry"]
    ):
        raise CursorActivationError(
            "repair_required: Cursor lifecycle rollback verification failed"
        )
    for skill, item in journal["per_skill_publish_state"].items():
        if cursor_skill_payload.owned_skill_state(
            pre_cache,
            Path(item["target_path"]),
            skill,
            expected_manifest_sha256=journal["pre_skill_manifest_sha256"],
            expected_object_identity=tuple(item["pre_object_identity"]),
        ) != "owned":
            raise CursorActivationError(
                "repair_required: Cursor lifecycle rollback verification failed"
            )
    _remove_journal_and_staging(journal)
    return {"action": "rolled_back", "phase": "pre"}


def repair_cursor_activation(
    *,
    managed_root: Path,
    python: Optional[Path] = None,
    fault_injector: _FaultInjector = None,
) -> Dict[str, Any]:
    """Recover any incomplete Cursor lifecycle, else repair the interpreter."""
    try:
        canonical = managed_install.canonical_managed_root(managed_root)
        managed_install._require_fixed_managed_root(canonical)
    except managed_install.ManagedInstallError as exc:
        raise CursorActivationError("managed install identity is invalid") from exc
    retry_initial = False
    with cursor_activation_lock():
        journal = _load_journal_if_present(managed_root=canonical)
        if (
            journal is not None
            and journal.get("schema") == LIFECYCLE_JOURNAL_SCHEMA
        ):
            if journal["phase"] != "committed":
                return _rollback_owned_lifecycle(
                    journal,
                    managed_root=canonical,
                )
            _verify_committed_lifecycle_state(journal, managed_root=canonical)
            cleanup_pending = _cleanup_committed_lifecycle(
                journal,
                managed_root=canonical,
            )
            result = {"action": "unchanged", "phase": "committed"}
            if cleanup_pending:
                result["cleanup_pending"] = True
            return result
        record = client_host_ownership.read_record_if_present(
            "cursor",
            managed_root=canonical,
            config_path=mcp_config.agent_config_path("cursor"),
            server_name=mcp_config.DEFAULT_SERVER_NAME,
        )
        protocol = canonical / update_transaction.PROTOCOL_READY_RELATIVE_PATH
        retry_initial = (
            journal is None
            and record is None
            and (
                protocol.exists()
                or update_coordination._is_link_like(protocol)
            )
        )
    if retry_initial:
        verified = _verified_current_release(canonical)
        return activate_cursor_initial(
            managed_root=canonical,
            verified_release=verified,
            python=Path(sys.executable),
            fault_injector=fault_injector,
        )
    return repair_cursor_interpreter(
        managed_root=canonical,
        python=python,
        fault_injector=fault_injector,
    )


def repair_cursor_interpreter(
    *,
    managed_root: Path,
    python: Optional[Path] = None,
    minimum_python: Tuple[int, int] = _MINIMUM_PYTHON,
    fault_injector: _FaultInjector = None,
) -> Dict[str, Any]:
    """Repair only an already-owned Cursor entry's absolute interpreter."""

    try:
        canonical = managed_install.canonical_managed_root(managed_root)
        managed_install._require_fixed_managed_root(canonical)
    except managed_install.ManagedInstallError as exc:
        raise CursorActivationError("managed install identity is invalid") from exc
    with cursor_activation_lock():
        journal = _load_journal_if_present(managed_root=canonical)
        if journal is not None and journal["operation"] == "install":
            if journal["phase"] != "committed":
                return _resume_initial(
                    journal,
                    managed_root=canonical,
                    fault_injector=fault_injector,
                )
            _verify_committed_initial_target(
                journal,
                managed_root=canonical,
            )
            _cleanup_transaction_staging(journal)
        requested = _validate_repair_interpreter(
            Path(sys.executable) if python is None else Path(python),
            canonical,
            minimum_python=minimum_python,
        )
        if journal is not None and journal["phase"] != "committed":
            if journal["operation"] != "interpreter_repair":
                raise CursorActivationError(
                    "repair_required: use the Cursor activation repair entrypoint"
                )
            if journal["post_mcp_managed_entry"].get("command") != requested:
                raise CursorActivationError(
                    "repair_required: requested interpreter conflicts with "
                    "the incomplete Cursor transaction"
                )
            owned_record = _record_from_journal(
                journal["pre_ownership_record"],
                managed_root=canonical,
                managed_entry=journal["pre_mcp_managed_entry"],
            )
            _verify_owned_active_skills(
                owned_record,
                managed_root=canonical,
            )
            return _resume_incomplete(
                journal,
                managed_root=canonical,
                fault_injector=fault_injector,
            )
        config_path = mcp_config.agent_config_path("cursor")
        actual = mcp_config.read_entry("cursor")
        if not isinstance(actual, dict):
            raise CursorActivationError(
                "same_name_unowned: Cursor decision-engine entry is missing"
            )
        record, pre_record_hash = _current_record(
            managed_root=canonical,
            config_path=config_path,
            server_name=mcp_config.DEFAULT_SERVER_NAME,
        )
        actual_hash = client_host_ownership.managed_entry_sha256_v1(actual)
        if actual_hash != record.managed_fields_sha256:
            raise CursorActivationError(
                "same_name_user_modified: Cursor managed fields changed"
            )
        _verify_owned_active_skills(
            record,
            managed_root=canonical,
        )
        if actual.get("command") == requested:
            return {"action": "unchanged", "phase": "committed"}

        desired = dict(actual)
        desired["command"] = requested
        plan = mcp_config.prepare_cursor_entry_write(
            desired,
            allow_owned_update=True,
        )
        if (
            plan.pre_file_sha256 is None
            or plan.pre_managed_entry
            != mcp_config._managed_entry_projection(actual)
        ):
            raise CursorActivationError(
                "repair_required: Cursor configuration changed during planning"
            )
        target_record = replace(
            record,
            managed_fields_sha256=client_host_ownership.managed_entry_sha256_v1(
                plan.target_entry
            ),
        )
        transaction_id = str(uuid.uuid4())
        journal = {
            "schema": JOURNAL_SCHEMA,
            "transaction_id": transaction_id,
            "install_id": record.install_id,
            "operation": "interpreter_repair",
            "phase": "intent",
            "target_present": True,
            "staging_root": str(
                cursor_activation_root()
                / "staging"
                / transaction_id
                / "skills"
            ),
            "pre_mcp_managed_entry": plan.pre_managed_entry,
            "pre_mcp_file_hash": plan.pre_file_sha256,
            "post_mcp_managed_entry": plan.post_managed_entry,
            "post_mcp_file_hash": plan.post_file_sha256,
            "pre_ownership_record": client_host_ownership.record_payload(record),
            "pre_ownership_record_hash": pre_record_hash,
            "post_ownership_record": client_host_ownership.record_payload(
                target_record
            ),
            "post_ownership_record_hash": client_host_ownership.record_sha256(
                target_record
            ),
            "pre_skill_release_id": record.skill_release_id,
            "target_skill_release_id": target_record.skill_release_id,
            "pre_skill_manifest_sha256": record.skill_manifest_sha256,
            "target_skill_manifest_sha256": target_record.skill_manifest_sha256,
            "per_skill_publish_state": {},
        }
        _validated_repair_journal(journal, managed_root=canonical)
        _write_journal(journal)
        if fault_injector is not None:
            fault_injector("after_intent")
        journal = _phase(journal, "mcp_publishing")
        if fault_injector is not None:
            fault_injector("before_mcp_apply")
        mcp_config.apply_cursor_entry_write(plan)
        if fault_injector is not None:
            fault_injector("after_mcp_apply_before_phase")
        journal = _phase(journal, "mcp_published")
        if fault_injector is not None:
            fault_injector("after_mcp_published")
        journal = _phase(journal, "ownership_publishing")
        if fault_injector is not None:
            fault_injector("before_ownership_apply")
        client_host_ownership.publish_record(
            target_record,
            managed_root=canonical,
            expected_exists=True,
            expected_sha256=pre_record_hash,
        )
        if fault_injector is not None:
            fault_injector("after_ownership_apply_before_phase")
        journal = _phase(journal, "ownership_published")
        if fault_injector is not None:
            fault_injector("after_ownership_published")
        journal = _phase(journal, "verifying")
        _verify_target(journal, managed_root=canonical)
        journal = _phase(journal, "committed")
        return {"action": "repaired", "phase": journal["phase"]}
