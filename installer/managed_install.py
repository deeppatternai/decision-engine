"""Fail-closed identity contract for a Decision Engine managed Git checkout.

The marker inside the checkout is policy metadata, not authority by itself. A
matching private registration outside the checkout binds one install UUID to
one canonical root. This module validates and persists that pair; it never runs
Git and never mutates tracked source files.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from . import config


SCHEMA_VERSION = 1
MARKER_FILENAME = ".managed-install.json"
INSTALL_MODE = "managed-git"
CHANNEL = "stable"
BRANCH = "stable"
REPOSITORY_ID = "deeppatternai/decision-engine"
OFFICIAL_REMOTE_URLS = (
    ("github", "https://github.com/deeppatternai/decision-engine.git"),
    ("gitee", "https://gitee.com/deeppatternai/decision-engine.git"),
)
_MAX_IDENTITY_BYTES = 64 * 1024
_MARKER_KEYS = {
    "schema", "install_id", "repository_id", "install_mode", "channel", "branch", "remotes",
}
_REGISTRATION_KEYS = {
    "schema", "install_id", "canonical_root", "repository_id", "install_mode",
}


class ManagedInstallError(config.ShellError):
    """A safe-to-display managed identity validation failure."""


@dataclass(frozen=True)
class ManagedIdentity:
    install_id: str
    canonical_root: Path
    repository_id: str
    remotes: Dict[str, str]


def _identity_object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ManagedInstallError("managed identity file contains a duplicate JSON key")
        value[key] = item
    return value


def marker_path(root: Path) -> Path:
    return Path(root) / MARKER_FILENAME


def registration_path() -> Path:
    return config.managed_component_root("decision-engine").parent \
        / "installations" / "decision-engine.json"


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_link_like(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ManagedInstallError("could not inspect path safely: %s" % path) from exc
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(reparse and attrs & reparse)


def _is_network_path(path: Path) -> bool:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return True
    if os.name != "nt":
        return False
    try:
        drive = Path(path).anchor
        if not drive:
            raise ManagedInstallError("managed root does not have a Windows drive anchor")
        drive_type = _windows_drive_type(drive)
        if drive_type == 4:
            return True
        if drive_type not in {2, 3, 5, 6}:
            raise ManagedInstallError("could not determine whether managed root is local")
        return False
    except (AttributeError, OSError) as exc:
        raise ManagedInstallError("could not determine whether managed root is local") from exc


def _windows_drive_type(drive: str) -> int:
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(str(drive)))


def _reject_link_components(path: Path) -> None:
    absolute = Path(os.path.abspath(str(path.expanduser())))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_link_like(current):
            raise ManagedInstallError(
                "managed root must not contain a symlink or reparse point: %s" % current
            )


def canonical_managed_root(root: Path) -> Path:
    lexical = Path(os.path.abspath(str(Path(root).expanduser())))
    if _is_network_path(lexical):
        raise ManagedInstallError("managed root must not be a network path")
    _reject_link_components(lexical)
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ManagedInstallError("managed root is not a readable directory: %s" % lexical) from exc
    if not resolved.is_dir():
        raise ManagedInstallError("managed root is not a directory: %s" % lexical)
    if _path_key(resolved) != _path_key(lexical):
        raise ManagedInstallError("managed root must use its canonical path")
    git_path = resolved / ".git"
    if _is_link_like(git_path) or not git_path.is_dir():
        raise ManagedInstallError(
            "managed root must be a normal Git checkout, not a linked worktree"
        )
    return resolved


def _expected_managed_root() -> Path:
    return Path(os.path.abspath(str(config.managed_component_root("decision-engine").expanduser())))


def _require_fixed_managed_root(root: Path) -> None:
    if _path_key(root) != _path_key(_expected_managed_root()):
        raise ManagedInstallError("managed identity requires the canonical install root")


def _validate_install_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ManagedInstallError("managed install ID must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ManagedInstallError("managed install ID must be a UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ManagedInstallError("managed install ID must be a canonical UUIDv4")
    return value


def _validate_private_posix_path(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        return
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ManagedInstallError("managed identity path is unreadable: %s" % path) from exc
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(info.st_mode):
        raise ManagedInstallError("managed identity path has the wrong type: %s" % path)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ManagedInstallError("managed identity path has the wrong owner: %s" % path)
    if stat.S_IMODE(info.st_mode) & 0o077:
        label = "directory permissions" if directory else "file permissions"
        raise ManagedInstallError("managed identity %s are too broad: %s" % (label, path))


def _ensure_private_registration_directory(path: Path) -> None:
    _reject_link_components(path.parent)
    if _is_link_like(path):
        raise ManagedInstallError(
            "managed registration directory must not be a symlink or reparse point: %s" % path
        )
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)
    except OSError as exc:
        raise ManagedInstallError("could not create private registration directory: %s" % path) from exc
    if _is_link_like(path):
        raise ManagedInstallError(
            "managed registration directory must not be a symlink or reparse point: %s" % path
        )
    _validate_private_posix_path(path, directory=True)


def _read_identity_json(
    path: Path, expected_keys: set, *, require_private_parent: bool = False
) -> Dict[str, Any]:
    _reject_link_components(path.parent)
    if require_private_parent:
        _validate_private_posix_path(path.parent, directory=True)
    if _is_link_like(path):
        raise ManagedInstallError("managed identity file must not be a symlink or reparse point: %s" % path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ManagedInstallError("managed identity path is not a regular file: %s" % path)
            if os.name != "nt":
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise ManagedInstallError("managed identity file has the wrong owner: %s" % path)
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise ManagedInstallError(
                        "managed identity file permissions are too broad: %s" % path
                    )
            raw = handle.read(_MAX_IDENTITY_BYTES + 1)
        if _is_link_like(path):
            raise ManagedInstallError(
                "managed identity file must not be a symlink or reparse point: %s" % path
            )
        if len(raw) > _MAX_IDENTITY_BYTES:
            raise ManagedInstallError("managed identity file is too large: %s" % path)
        loaded = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_identity_object_without_duplicate_keys
        )
    except FileNotFoundError as exc:
        raise ManagedInstallError("managed identity file is missing: %s" % path) from exc
    except ManagedInstallError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedInstallError("managed identity file is unreadable: %s" % path) from exc
    if not isinstance(loaded, dict) or set(loaded) != expected_keys:
        raise ManagedInstallError("managed identity file has an unexpected schema: %s" % path)
    if type(loaded.get("schema")) is not int or loaded["schema"] != SCHEMA_VERSION:
        raise ManagedInstallError("managed identity schema is not supported: %s" % path)
    return loaded


def _marker_payload(install_id: str) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "install_id": install_id,
        "repository_id": REPOSITORY_ID,
        "install_mode": INSTALL_MODE,
        "channel": CHANNEL,
        "branch": BRANCH,
        "remotes": [
            {"name": name, "url": url, "priority": priority}
            for priority, (name, url) in enumerate(OFFICIAL_REMOTE_URLS, start=1)
        ],
    }


def _registration_payload(install_id: str, root: Path) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "install_id": install_id,
        "canonical_root": str(root),
        "repository_id": REPOSITORY_ID,
        "install_mode": INSTALL_MODE,
    }


def _atomic_write_private_json(path: Path, payload: Dict[str, Any], *, manage_parent: bool) -> None:
    _reject_link_components(path.parent)
    if manage_parent:
        _ensure_private_registration_directory(path.parent)
    elif not path.parent.is_dir():
        raise ManagedInstallError("managed root is not a directory: %s" % path.parent)
    if _is_link_like(path):
        raise ManagedInstallError("managed identity file must not be a symlink or reparse point: %s" % path)
    rendered = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temp = path.with_name("%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd: Optional[int] = None
    try:
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            path.chmod(0o600)
            _validate_private_posix_path(path, directory=False)
    except OSError as exc:
        raise ManagedInstallError("could not persist managed identity at %s" % path) from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _managed_identity_lock() -> Iterator[None]:
    """Serialize paired identity initialization; OS locks are released on process exit."""
    directory = registration_path().parent
    _ensure_private_registration_directory(directory)
    lock_path = directory / "decision-engine.identity.lock"
    if _is_link_like(lock_path):
        raise ManagedInstallError("managed identity lock must not be a link")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ManagedInstallError("could not open managed identity lock") from exc
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ManagedInstallError("managed identity initialization is already in progress") from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ManagedInstallError("managed identity initialization is already in progress") from exc
        locked = True
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _validate_marker(marker: Dict[str, Any]) -> str:
    install_id = _validate_install_id(marker.get("install_id"))
    if marker.get("repository_id") != REPOSITORY_ID or marker.get("install_mode") != INSTALL_MODE:
        raise ManagedInstallError("managed marker repository or install mode is invalid")
    if marker.get("channel") != CHANNEL or marker.get("branch") != BRANCH:
        raise ManagedInstallError("managed marker channel is invalid")
    expected = _marker_payload(install_id)["remotes"]
    if marker.get("remotes") != expected:
        raise ManagedInstallError("managed marker official remotes are invalid")
    return install_id


def _validate_registration(registration: Dict[str, Any], root: Path) -> str:
    install_id = _validate_install_id(registration.get("install_id"))
    if registration.get("repository_id") != REPOSITORY_ID \
            or registration.get("install_mode") != INSTALL_MODE:
        raise ManagedInstallError("managed registration repository or install mode is invalid")
    recorded_root = registration.get("canonical_root")
    if not isinstance(recorded_root, str) or not Path(recorded_root).is_absolute():
        raise ManagedInstallError("managed registration canonical root must be absolute")
    if _path_key(Path(recorded_root)) != _path_key(root):
        raise ManagedInstallError("managed registration canonical root does not match")
    return install_id


def _validate_official_remotes(observed_remotes: Mapping[str, str]) -> Dict[str, str]:
    actual = dict(observed_remotes) if isinstance(observed_remotes, Mapping) else {}
    expected = dict(OFFICIAL_REMOTE_URLS)
    if actual != expected:
        raise ManagedInstallError("managed checkout does not have the exact official remote set")
    return actual


def write_managed_identity(root: Path, *, install_id: Optional[str] = None) -> str:
    """Persist the paired identity for the canonical default managed root.

    Installation at a non-default root is a later explicit-mode feature; this
    entry point refuses it so a command run from a developer clone cannot mark
    that clone as managed by accident.
    """
    canonical = canonical_managed_root(root)
    _require_fixed_managed_root(canonical)
    with _managed_identity_lock():
        marker_file = marker_path(canonical)
        registration_file = registration_path()
        marker_exists = marker_file.exists() or _is_link_like(marker_file)
        registration_exists = registration_file.exists() or _is_link_like(registration_file)
        if marker_exists != registration_exists:
            raise ManagedInstallError(
                "partial managed identity requires explicit repair of %s and %s"
                % (marker_file, registration_file)
            )
        if marker_exists and registration_exists:
            marker = _read_identity_json(marker_file, _MARKER_KEYS)
            registration = _read_identity_json(
                registration_file, _REGISTRATION_KEYS, require_private_parent=True
            )
            marker_id = _validate_marker(marker)
            registration_id = _validate_registration(registration, canonical)
            if marker_id != registration_id:
                raise ManagedInstallError("managed marker and registration install IDs do not match")
            if install_id is not None and _validate_install_id(install_id) != marker_id:
                raise ManagedInstallError("existing managed identity has a different install ID")
            return marker_id
        value = _validate_install_id(install_id) if install_id is not None else str(uuid.uuid4())
        _atomic_write_private_json(
            registration_file, _registration_payload(value, canonical), manage_parent=True
        )
        _atomic_write_private_json(marker_file, _marker_payload(value), manage_parent=False)
        marker = _read_identity_json(marker_file, _MARKER_KEYS)
        registration = _read_identity_json(
            registration_file, _REGISTRATION_KEYS, require_private_parent=True
        )
        if _validate_marker(marker) != _validate_registration(registration, canonical):
            raise ManagedInstallError("managed identity final verification failed")
        return value


def validate_managed_identity(
    root: Path, observed_remotes: Mapping[str, str]
) -> ManagedIdentity:
    """Verify managed-install identity, not permission to mutate Git.

    The PR3 Git-observing layer must supply the exact name/URL mapping read from
    the checkout and must separately enforce untracked/config boundaries,
    explicit dev mode, signed-release authorization and protected prior state.
    This function deliberately does not accept a caller policy default as
    evidence of live Git configuration.
    """
    canonical = canonical_managed_root(root)
    _require_fixed_managed_root(canonical)
    marker = _read_identity_json(marker_path(canonical), _MARKER_KEYS)
    registration = _read_identity_json(
        registration_path(), _REGISTRATION_KEYS, require_private_parent=True
    )
    marker_id = _validate_marker(marker)
    registration_id = _validate_registration(registration, canonical)
    if marker_id != registration_id:
        raise ManagedInstallError("managed marker and registration install IDs do not match")
    official = _validate_official_remotes(observed_remotes)
    return ManagedIdentity(
        install_id=marker_id,
        canonical_root=canonical,
        repository_id=REPOSITORY_ID,
        remotes=official,
    )
