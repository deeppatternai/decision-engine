"""Cross-process admission and live-session coordination for managed updates.

File existence is never authority.  The permanent install lock and every
ephemeral shim lease are backed by an OS advisory lock held on an open handle.
The install lock serializes writers and the short shim-admission window; the
lease handle remains locked for the complete shim session.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple

from . import managed_install, windows_security


INSTALL_LOCK_RELATIVE_PATH = Path(".runtime") / "locks" / "install-transaction.lock"
STARTUP_UPDATE_LOCK_RELATIVE_PATH = Path(".runtime") / "locks" / "startup-update.lock"
LEASES_RELATIVE_PATH = Path(".runtime") / "shim-leases"
UPDATE_JOURNAL_RELATIVE_PATH = Path(".runtime") / "update-journal.json"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_MAX_LEASE_BYTES = 64 * 1024
_MAX_LEASE_FILES = 4096
_LOCK_POLL_SECONDS = 0.025
_TRANSACTION_CAPABILITY = object()


class UpdateCoordinationError(managed_install.ManagedInstallError):
    """A safe-to-display coordination refusal."""


class InstallTransactionBusy(UpdateCoordinationError):
    """Another installer/updater/repair or shim admission holds the gate."""


@dataclass(frozen=True)
class InstallTransaction:
    path: Path
    _fd: int
    _identity: Tuple[int, int, int]
    _capability: object
    _released: threading.Event

    def fileno(self) -> int:
        return self._fd

    def _validate(self, expected: Path) -> None:
        if self._capability is not _TRANSACTION_CAPABILITY:
            raise UpdateCoordinationError("install transaction capability is invalid")
        if self._released.is_set():
            raise UpdateCoordinationError("install transaction is no longer active")
        try:
            opened = os.fstat(self._fd)
            current = os.lstat(expected)
        except OSError as exc:
            raise UpdateCoordinationError("install transaction is no longer active") from exc
        identity = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
        current_identity = (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))
        if identity != self._identity or current_identity != self._identity:
            raise UpdateCoordinationError("install transaction lock identity changed")


@dataclass(frozen=True)
class LiveShimSession:
    path: Path
    pid: Optional[int]
    started_at: Optional[float]
    running_commit: Optional[str]
    heartbeat_at: Optional[float]


class ShimSessionLease:
    def __init__(self, path: Path, fd: int, stop: threading.Event, thread) -> None:
        self.path = path
        self._fd = fd
        self._stop = stop
        self._thread = thread

    def fileno(self) -> int:
        return self._fd


@dataclass(frozen=True)
class StartupUpdateGate:
    waited: bool


def _is_link_like(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UpdateCoordinationError("could not inspect coordination path safely") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(reparse and attributes & reparse)


def _validate_private_directory(path: Path) -> None:
    if _is_link_like(path):
        raise UpdateCoordinationError("coordination directory must not be a link or reparse point")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise UpdateCoordinationError("coordination directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise UpdateCoordinationError("coordination path is not a directory")
    if os.name == "nt":
        try:
            windows_security.validate_private_mutation_acl(path)
        except windows_security.WindowsSecurityError as exc:
            raise UpdateCoordinationError("coordination directory ACL is not private") from exc
    else:
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise UpdateCoordinationError("coordination directory has the wrong owner")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise UpdateCoordinationError("coordination directory permissions are too broad")


def _ensure_private_directory(path: Path) -> None:
    managed_install._reject_link_components(path.parent)
    if _is_link_like(path):
        raise UpdateCoordinationError("coordination directory must not be a link or reparse point")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)
    except OSError as exc:
        raise UpdateCoordinationError("could not create coordination directory") from exc
    _validate_private_directory(path)


def _canonical_root(root: Path) -> Path:
    try:
        return managed_install.canonical_managed_root(root)
    except managed_install.ManagedInstallError as exc:
        raise UpdateCoordinationError("managed root is not safe for coordination") from exc


def _open_lock_file(
    path: Path, *, exclusive_create: bool = False, initialize_lock_byte: bool = False
) -> int:
    if _is_link_like(path):
        raise UpdateCoordinationError("coordination lock must not be a link or reparse point")
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise UpdateCoordinationError("could not inspect coordination lock") from exc
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if exclusive_create:
        flags |= os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise UpdateCoordinationError("could not open coordination lock") from exc
    try:
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UpdateCoordinationError("coordination lock is not a private regular file")
        opened_identity = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        if before is not None and opened_identity != (
            before.st_dev,
            before.st_ino,
            stat.S_IFMT(before.st_mode),
        ):
            raise UpdateCoordinationError("coordination lock changed while it was opened")
        if os.name != "nt":
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise UpdateCoordinationError("coordination lock has the wrong owner")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise UpdateCoordinationError("coordination lock permissions are too broad")
        else:
            try:
                windows_security.validate_private_mutation_acl(path)
            except windows_security.WindowsSecurityError as exc:
                raise UpdateCoordinationError("coordination lock ACL is not private") from exc
        if _is_link_like(path):
            raise UpdateCoordinationError("coordination lock became a link or reparse point")
        current = os.lstat(path)
        if opened_identity != (
            current.st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
        ):
            raise UpdateCoordinationError("coordination lock identity changed")
        if initialize_lock_byte and info.st_size == 0:
            os.write(fd, b"0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except UpdateCoordinationError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise UpdateCoordinationError("could not configure coordination lock") from exc


def _try_lock(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                return False
            raise UpdateCoordinationError("could not acquire Windows coordination lock") from exc
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise UpdateCoordinationError("could not acquire POSIX coordination lock") from exc
    return True


def _unlock(fd: int) -> None:
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


def _acquire_until(fd: int, timeout_seconds: float) -> bool:
    if timeout_seconds < 0:
        raise UpdateCoordinationError("coordination timeout must not be negative")
    deadline = time.monotonic() + timeout_seconds
    waited = False
    while True:
        if _try_lock(fd):
            return waited
        if time.monotonic() >= deadline:
            raise InstallTransactionBusy("managed install transaction is already in progress")
        waited = True
        time.sleep(min(_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


@contextlib.contextmanager
def install_transaction(
    root: Path, *, timeout_seconds: float = 0.0
) -> Iterator[InstallTransaction]:
    """Acquire the permanent per-install writer/admission gate."""

    canonical = _canonical_root(root)
    lock_path = canonical / INSTALL_LOCK_RELATIVE_PATH
    _ensure_private_directory(lock_path.parent)
    fd = _open_lock_file(lock_path, initialize_lock_byte=True)
    locked = False
    try:
        _acquire_until(fd, timeout_seconds)
        locked = True
        information = os.fstat(fd)
        identity = (
            information.st_dev,
            information.st_ino,
            stat.S_IFMT(information.st_mode),
        )
        released = threading.Event()
        yield InstallTransaction(
            lock_path, fd, identity, _TRANSACTION_CAPABILITY, released
        )
    finally:
        if "released" in locals():
            released.set()
        if locked:
            _unlock(fd)
        os.close(fd)


@contextlib.contextmanager
def startup_update_gate(
    root: Path, *, timeout_seconds: float
) -> Iterator[StartupUpdateGate]:
    """Serialize launcher recovery/update work before any shim lease is admitted."""

    canonical = _canonical_root(root)
    lock_path = canonical / STARTUP_UPDATE_LOCK_RELATIVE_PATH
    _ensure_private_directory(lock_path.parent)
    fd = _open_lock_file(lock_path, initialize_lock_byte=True)
    locked = False
    try:
        waited = _acquire_until(fd, timeout_seconds)
        locked = True
        yield StartupUpdateGate(waited=waited)
    finally:
        if locked:
            _unlock(fd)
        os.close(fd)


def _lease_payload(running_commit: str, *, started_at: float) -> dict:
    now = time.time()
    return {
        "schema": 1,
        "pid": os.getpid(),
        "started_at": started_at,
        "running_commit": running_commit,
        "heartbeat_at": now,
    }


def _write_locked_payload(fd: int, payload: dict) -> None:
    rendered = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(rendered) > _MAX_LEASE_BYTES:
        raise UpdateCoordinationError("shim lease payload is too large")
    try:
        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"0")
        # Byte zero is the lifetime lock.  Metadata starts at byte one so a
        # scanner can read it even on Windows, where the locked range is
        # mandatory for overlapping I/O.
        os.ftruncate(fd, 1)
        os.lseek(fd, 1, os.SEEK_SET)
        view = memoryview(rendered)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short lease write")
            view = view[written:]
        os.fsync(fd)
    except OSError as exc:
        raise UpdateCoordinationError("could not persist shim lease") from exc


@contextlib.contextmanager
def shim_session_lease(
    root: Path,
    running_commit: str,
    *,
    admission_timeout_seconds: float = 5.0,
    heartbeat_seconds: Optional[float] = 15.0,
) -> Iterator[ShimSessionLease]:
    """Admit a shim under the install gate, then hold an OS lease until exit."""

    if not isinstance(running_commit, str) or not _COMMIT_RE.fullmatch(running_commit):
        raise UpdateCoordinationError("running shim commit is invalid")
    if heartbeat_seconds is not None and heartbeat_seconds <= 0:
        raise UpdateCoordinationError("shim heartbeat interval must be positive")
    canonical = _canonical_root(root)
    leases = canonical / LEASES_RELATIVE_PATH
    stop = threading.Event()
    thread = None
    fd = None
    lease_path = None
    locked = False
    started_at = time.time()
    try:
        with install_transaction(canonical, timeout_seconds=admission_timeout_seconds):
            journal_path = canonical / UPDATE_JOURNAL_RELATIVE_PATH
            if journal_path.exists() or _is_link_like(journal_path):
                raise UpdateCoordinationError(
                    "unfinished managed update blocks shim admission"
                )
            _ensure_private_directory(leases)
            for _attempt in range(8):
                candidate = leases / ("%d-%s.json" % (os.getpid(), uuid.uuid4().hex))
                try:
                    fd = _open_lock_file(candidate, exclusive_create=True)
                    lease_path = candidate
                    break
                except UpdateCoordinationError as exc:
                    if candidate.exists():
                        continue
                    raise exc
            if fd is None or lease_path is None:
                raise UpdateCoordinationError("could not allocate a unique shim lease")
            _write_locked_payload(fd, _lease_payload(running_commit, started_at=started_at))
            if not _try_lock(fd):
                raise UpdateCoordinationError("new shim lease could not be locked")
            locked = True

        def heartbeat() -> None:
            while not stop.wait(heartbeat_seconds):
                try:
                    _write_locked_payload(
                        fd, _lease_payload(running_commit, started_at=started_at)
                    )
                except UpdateCoordinationError:
                    return

        if heartbeat_seconds is not None:
            thread = threading.Thread(
                target=heartbeat, name="decision-engine-shim-lease", daemon=True
            )
            thread.start()
        yield ShimSessionLease(lease_path, fd, stop, thread)
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=max(1.0, min(heartbeat_seconds * 2, 5.0)))

        def release_resources() -> None:
            if locked:
                _unlock(fd)
            if fd is not None:
                os.close(fd)
            if lease_path is None:
                return
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

        if thread is not None and thread.is_alive():
            def release_after_heartbeat() -> None:
                thread.join()
                release_resources()

            threading.Thread(
                target=release_after_heartbeat,
                name="decision-engine-shim-lease-cleanup",
                daemon=True,
            ).start()
        else:
            release_resources()


def _read_live_metadata(path: Path, fd: int) -> LiveShimSession:
    for attempt in range(5):
        try:
            os.lseek(fd, 1, os.SEEK_SET)
            raw = os.read(fd, _MAX_LEASE_BYTES + 1)
            if len(raw) > _MAX_LEASE_BYTES:
                raise ValueError("oversized")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("not an object")
            pid = value.get("pid") if type(value.get("pid")) is int else None
            started = value.get("started_at")
            heartbeat = value.get("heartbeat_at")
            commit = value.get("running_commit")
            return LiveShimSession(
                path=path,
                pid=pid if pid is not None and pid > 0 else None,
                started_at=float(started) if type(started) in {int, float} else None,
                running_commit=(
                    commit
                    if isinstance(commit, str) and _COMMIT_RE.fullmatch(commit)
                    else None
                ),
                heartbeat_at=(
                    float(heartbeat)
                    if type(heartbeat) in {int, float}
                    else None
                ),
            )
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            if attempt < 4:
                time.sleep(0.002)
    return LiveShimSession(path, None, None, None, None)


def _scan_live_sessions(canonical: Path) -> Tuple[LiveShimSession, ...]:
    directory = canonical / LEASES_RELATIVE_PATH
    if not directory.exists():
        return ()
    _validate_private_directory(directory)
    live = []
    try:
        paths = []
        scanned = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > _MAX_LEASE_FILES:
                    raise UpdateCoordinationError(
                        "shim lease directory exceeds the entry-count limit"
                    )
                if not entry.name.endswith(".json"):
                    continue
                paths.append(Path(entry.path))
        paths.sort(key=lambda item: item.name)
    except UpdateCoordinationError:
        raise
    except OSError as exc:
        raise UpdateCoordinationError("could not enumerate shim leases") from exc
    for path in paths:
        if _is_link_like(path):
            raise UpdateCoordinationError("shim lease must not be a link or reparse point")
        try:
            fd = _open_lock_file(path)
        except UpdateCoordinationError:
            raise
        acquired = False
        try:
            acquired = _try_lock(fd)
            if not acquired:
                live.append(_read_live_metadata(path, fd))
        finally:
            if acquired:
                _unlock(fd)
            os.close(fd)
        if acquired:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise UpdateCoordinationError("could not reclaim an unlocked shim lease") from exc
    return tuple(live)


def live_shim_sessions(
    root: Path, *, transaction: Optional[InstallTransaction] = None
) -> Tuple[LiveShimSession, ...]:
    """Return OS-locked leases and reclaim unlocked records.

    An updater passes its already-held transaction.  Diagnostic callers omit it
    and acquire the same gate around the scan, preventing lease-create races.
    """

    canonical = _canonical_root(root)
    if transaction is not None:
        expected = canonical / INSTALL_LOCK_RELATIVE_PATH
        if managed_install._path_key(transaction.path) != managed_install._path_key(expected):
            raise UpdateCoordinationError("install transaction belongs to another root")
        transaction._validate(expected)
        return _scan_live_sessions(canonical)
    with install_transaction(canonical, timeout_seconds=5.0):
        return _scan_live_sessions(canonical)
