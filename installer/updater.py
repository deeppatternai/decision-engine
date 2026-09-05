"""Read-only authorization and diagnostics for the managed Git updater.

This module deliberately has no apply, fetch, checkout, reset, recovery, smoke,
or state-advance API.  ``UpdateInspection`` is ephemeral diagnostic data, never
an authorization capability.  The PR4 mutator must acquire its transaction
lock, prove there is no live shim lease, and repeat every check here immediately
before journaling or changing the checkout.

Installation precondition: ``managed_install`` created the fixed per-user root
inside the user's protected profile/home.  This reader validates the POSIX
owner/mode of protected state, but it does not establish an ACL/mode policy for
the whole checkout.  Python exposes no portable Windows DACL or pinned
directory-handle traversal API, so ``update_transaction`` supplies those native
mutation-time checks.
System and global Git config are deliberately suppressed; install/migration
must pin ``core.autocrlf`` and ``core.symlinks`` in managed Windows checkouts;
``core.eol`` may remain unset and then follows Git's native rule.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from . import managed_install, release_contract
from .config import ShellError


STATE_SCHEMA_VERSION = 1
UPDATE_STATE_RELATIVE_PATH = Path(".runtime") / "update-state.json"
_MAX_STATE_BYTES = 64 * 1024
_GIT_OUTPUT_LIMIT = 8 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 10
_GIT_REAP_TIMEOUT_SECONDS = 2
_INSPECTION_TIMEOUT_SECONDS = 60
_MAX_GIT_PATHS = 250_000
_MAX_UNKNOWN_PATH_BYTES = _GIT_OUTPUT_LIMIT
_MAX_PATH_COMPONENTS = 128
_MAX_PATH_CHARACTERS = 4096
_MAX_TRACKED_FILE_BYTES = 128 * 1024 * 1024
_MAX_TRACKED_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_GIT_EXECUTABLE_BYTES = 128 * 1024 * 1024
_TRACKED_HASH_TIMEOUT_SECONDS = 15
_TREE_OPERATION_TIMEOUT_SECONDS = 20
_MINIMUM_GIT_VERSION = (2, 45, 0)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_UNSAFE_LOCAL_CONFIG_PATTERN = (
    r"^(filter\..*\.(clean|smudge|process|required)|include(if)?\..*|"
    r"alias\..*|credential(\..*)?\.helper|core\.(alternaterefscommand|alternaterefsprefixes|"
    r"askpass|attributesfile|editor|excludesfile|fsmonitor|gitproxy|hookspath|pager|sshcommand|worktree)|"
    r"diff\.(external|.*\.(command|textconv))|difftool\..*\.cmd|gpg(\..*)?\.program|"
    r"interactive\.difffilter|merge\..*\.driver|mergetool\..*\.cmd|protocol\..*\.allow|"
    r"extensions\.worktreeconfig|receivepack\.packobjectshook|remote\..*\.(uploadpack|receivepack)|"
    r"sequence\.editor|trace2\..*|uploadpack\.packobjectshook|submodule\..*\.update|"
    r"url\..*\.(insteadof|pushinsteadof))$"
)
_UNSAFE_LOCAL_CONFIG_RE = re.compile(_UNSAFE_LOCAL_CONFIG_PATTERN)
_STATE_BASE_KEYS = {
    "schema",
    "channel",
    "last_release_sequence",
    "last_release_commit",
    "last_manifest_sha256",
    "last_version",
}
_STATE_DIAGNOSTIC_KEYS = {
    "source",
    "last_attempt_at",
    "last_result",
    "previous_commit",
    "target_commit",
    "running_commit",
    "running_version",
    "error_code",
    "transaction_id",
}
_STATE_KEYS = _STATE_BASE_KEYS | _STATE_DIAGNOSTIC_KEYS
_UPDATE_RESULTS = {
    "up_to_date",
    "candidate_ready",
    "updated",
    "deferred_slow_network",
    "deferred_active_session",
    "retry_pending",
    "skipped_locked",
    "incompatible_runtime",
    "signature_failed",
    "update_failed",
    "rolled_back",
    "rollback_failed",
    "quarantined",
    "repair_required",
}
_CONFIG_FILENAME = "config.json"
_PROTECTED_TARGETS = {
    managed_install.MARKER_FILENAME.casefold(),
    UPDATE_STATE_RELATIVE_PATH.parts[0].casefold(),
    _CONFIG_FILENAME.casefold(),
}
_WINDOWS_DEVICE_NAMES = {
    "aux", "con", "nul", "prn",
    *("com%d" % number for number in range(1, 10)),
    *("lpt%d" % number for number in range(1, 10)),
    *("com%s" % number for number in ("\u00b9", "\u00b2", "\u00b3")),
    *("lpt%s" % number for number in ("\u00b9", "\u00b2", "\u00b3")),
}

# The operation name, verb and every option are frozen. Dynamic values may only
# replace the two explicit placeholders after release-contract validation.
_GIT_OPERATION_TEMPLATES = {
    "autocrlf": ("config", "--local", "--no-includes", "--get", "core.autocrlf"),
    "eol": ("config", "--local", "--no-includes", "--get", "core.eol"),
    "head": ("rev-parse", "--verify", "HEAD^{commit}"),
    "head_tree": ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
    "index": ("ls-files", "--stage", "-v", "-z"),
    "index_eol": ("ls-files", "--eol", "-z"),
    "remotes": (
        "config", "-z", "--local", "--no-includes", "--get-regexp", r"^remote\..*\.url$",
    ),
    "target": ("rev-parse", "--verify", "refs/tags/{tag}^{commit}"),
    "target_tree": ("ls-tree", "-r", "-z", "--full-tree", "{commit}"),
    "trust_store": ("show", "{commit}:installer/release-trust.json"),
    "top_level": ("rev-parse", "--show-toplevel"),
    "symlinks": ("config", "--local", "--no-includes", "--get", "core.symlinks"),
    "unsafe_config": (
        "config", "-z", "--local", "--no-includes", "--name-only", "--list",
    ),
    # No exclude rules: this single bounded walk includes ignored and ordinary
    # untracked paths.  Whole unknown directories collapse to one ancestor
    # record, which collision classification handles conservatively.
    "unknown": ("ls-files", "-z", "--others", "--directory", "--no-empty-directory"),
    "version": ("--version",),
}
_GIT_OPERATION_TIMEOUTS = {
    operation: (
        _TREE_OPERATION_TIMEOUT_SECONDS
        if operation in {
            "head_tree", "index", "index_eol", "target_tree", "unknown"
        }
        else _GIT_TIMEOUT_SECONDS
    )
    for operation in _GIT_OPERATION_TEMPLATES
}


class UpdateInspectionError(ShellError):
    """A safe-to-display refusal from the read-only updater inspector."""


@dataclass(frozen=True)
class UpdateState:
    schema: int
    channel: str
    last_release_sequence: int
    last_release_commit: str
    last_manifest_sha256: str
    last_version: str
    source: Optional[str] = None
    last_attempt_at: Optional[str] = None
    last_result: Optional[str] = None
    previous_commit: Optional[str] = None
    target_commit: Optional[str] = None
    running_commit: Optional[str] = None
    running_version: Optional[str] = None
    error_code: Optional[str] = None
    transaction_id: Optional[str] = None


@dataclass(frozen=True)
class UpdateInspection:
    """Ephemeral diagnostics only; PR4 must not use this object as authority.

    ``status`` reports release currency only.  ``tracked_dirty`` and
    ``collision_paths`` are separate applicability diagnostics and never grant
    permission to write.  Gitlinks and filtered worktree transforms other than
    a CRLF checkout authorized by Git's EOL report plus active conversion policy
    are conservatively reported dirty.
    """

    status: str
    current_commit: str
    target_commit: str
    target_version: str
    release_sequence: int
    manifest_sha256: str
    tracked_dirty: bool
    collision_paths: Tuple[str, ...]


def _state_object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UpdateInspectionError("update state contains a duplicate JSON key")
        value[key] = item
    return value


def _is_link_like(path: Path) -> bool:
    try:
        return managed_install._is_link_like(path)
    except managed_install.ManagedInstallError as exc:
        raise UpdateInspectionError("could not inspect update-state path safely") from exc


def _validate_private_posix_path(path: Path, info: os.stat_result, *, directory: bool) -> None:
    if os.name == "nt":
        return
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise UpdateInspectionError("update-state path has the wrong type")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise UpdateInspectionError("update-state path has the wrong owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise UpdateInspectionError("update-state permissions are too broad")


def _same_stat_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    ) == (
        after.st_dev,
        after.st_ino,
        stat.S_IFMT(after.st_mode),
    )


def _read_bounded_state(handle) -> bytes:
    captured = bytearray()
    while len(captured) <= _MAX_STATE_BYTES:
        chunk = handle.read(min(8192, _MAX_STATE_BYTES + 1 - len(captured)))
        if not chunk:
            break
        captured.extend(chunk)
    if len(captured) > _MAX_STATE_BYTES:
        raise UpdateInspectionError("update-state file exceeds the size limit")
    return bytes(captured)


def _supports_safe_dir_fd() -> bool:
    return (
        os.name != "nt"
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
    )


def _read_update_state(root: Path) -> UpdateState:
    root = Path(root)
    runtime = root / UPDATE_STATE_RELATIVE_PATH.parent
    path = root / UPDATE_STATE_RELATIVE_PATH
    if _is_link_like(root) or _is_link_like(runtime) or _is_link_like(path):
        raise UpdateInspectionError("update-state path must not be a link or reparse point")
    runtime_fd = None
    try:
        root_info = os.lstat(root)
        if _supports_safe_dir_fd():
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            runtime_fd = os.open(runtime, directory_flags)
            runtime_info = os.fstat(runtime_fd)
        else:
            runtime_info = os.lstat(runtime)
    except OSError as exc:
        if runtime_fd is not None:
            os.close(runtime_fd)
        raise UpdateInspectionError("protected update state is missing or unreadable") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        if runtime_fd is not None:
            os.close(runtime_fd)
        raise UpdateInspectionError("managed root has the wrong type")
    try:
        _validate_private_posix_path(runtime, runtime_info, directory=True)
    except UpdateInspectionError:
        if runtime_fd is not None:
            os.close(runtime_fd)
        raise

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = None
    try:
        if runtime_fd is not None:
            fd = os.open(UPDATE_STATE_RELATIVE_PATH.name, flags, dir_fd=runtime_fd)
        else:
            fd = os.open(path, flags)
        try:
            handle = os.fdopen(fd, "rb")
        except OSError:
            os.close(fd)
            fd = None
            raise
        fd = None
        with handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise UpdateInspectionError("update-state path is not a regular file")
            if opened.st_nlink != 1:
                raise UpdateInspectionError("update-state file must have exactly one link")
            _validate_private_posix_path(path, opened, directory=False)
            if opened.st_size > _MAX_STATE_BYTES:
                raise UpdateInspectionError("update-state file exceeds the size limit")
            raw = _read_bounded_state(handle)
            opened_after = os.fstat(handle.fileno())
        if _is_link_like(root) or _is_link_like(runtime) or _is_link_like(path):
            raise UpdateInspectionError("update-state path must not be a link or reparse point")
        root_after = os.lstat(root)
        if runtime_fd is not None:
            runtime_after = os.fstat(runtime_fd)
            current = os.stat(
                UPDATE_STATE_RELATIVE_PATH.name,
                dir_fd=runtime_fd,
                follow_symlinks=False,
            )
        else:
            runtime_after = os.lstat(runtime)
            current = os.lstat(path)
        if not (
            _same_stat_identity(root_info, root_after)
            and _same_stat_identity(runtime_info, runtime_after)
            and _same_stat_identity(opened, opened_after)
            and _same_stat_identity(opened, current)
        ):
            raise UpdateInspectionError("update-state file changed while it was being read")
        if (
            opened.st_size,
            getattr(opened, "st_mtime_ns", None),
        ) != (
            opened_after.st_size,
            getattr(opened_after, "st_mtime_ns", None),
        ):
            raise UpdateInspectionError("update-state file changed while it was being read")
        loaded = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_state_object_without_duplicate_keys
        )
    except UpdateInspectionError:
        raise
    except FileNotFoundError as exc:
        raise UpdateInspectionError("protected update state is missing or unreadable") from exc
    except (OSError, UnicodeError, ValueError, RecursionError, MemoryError) as exc:
        raise UpdateInspectionError("protected update state is invalid or unreadable") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if runtime_fd is not None:
            os.close(runtime_fd)

    loaded_keys = frozenset(loaded) if isinstance(loaded, dict) else frozenset()
    if not isinstance(loaded, dict) or loaded_keys not in {
        frozenset(_STATE_BASE_KEYS), frozenset(_STATE_KEYS)
    }:
        raise UpdateInspectionError("update-state file has an unexpected schema")
    if type(loaded.get("schema")) is not int or loaded["schema"] != STATE_SCHEMA_VERSION:
        raise UpdateInspectionError("update-state schema is not supported")
    if loaded.get("channel") != managed_install.CHANNEL:
        raise UpdateInspectionError("update-state channel is invalid")
    sequence = loaded.get("last_release_sequence")
    if type(sequence) is not int or sequence < 1:
        raise UpdateInspectionError("update-state release sequence is invalid")
    commit = loaded.get("last_release_commit")
    digest = loaded.get("last_manifest_sha256")
    version = loaded.get("last_version")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise UpdateInspectionError("update-state release commit is invalid")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise UpdateInspectionError("update-state manifest digest is invalid")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise UpdateInspectionError("update-state version is invalid")
    diagnostics = {key: None for key in _STATE_DIAGNOSTIC_KEYS}
    if loaded_keys == frozenset(_STATE_KEYS):
        diagnostics.update({key: loaded[key] for key in _STATE_DIAGNOSTIC_KEYS})
        if diagnostics["source"] not in {None, "github", "gitee", "local"}:
            raise UpdateInspectionError("update-state source is invalid")
        if not isinstance(diagnostics["last_attempt_at"], str) \
                or not diagnostics["last_attempt_at"]:
            raise UpdateInspectionError("update-state attempt time is invalid")
        if diagnostics["last_result"] not in _UPDATE_RESULTS:
            raise UpdateInspectionError("update-state result is invalid")
        for key in ("previous_commit", "target_commit", "running_commit"):
            value = diagnostics[key]
            if value is not None and (not isinstance(value, str) or not _COMMIT_RE.fullmatch(value)):
                raise UpdateInspectionError("update-state %s is invalid" % key.replace("_", " "))
        running_version = diagnostics["running_version"]
        if running_version is not None and (
            not isinstance(running_version, str) or not _VERSION_RE.fullmatch(running_version)
        ):
            raise UpdateInspectionError("update-state running version is invalid")
        for key in ("error_code", "transaction_id"):
            value = diagnostics[key]
            if value is not None and (not isinstance(value, str) or not value or len(value) > 128):
                raise UpdateInspectionError("update-state %s is invalid" % key.replace("_", " "))
    return UpdateState(
        schema=STATE_SCHEMA_VERSION,
        channel=managed_install.CHANNEL,
        last_release_sequence=sequence,
        last_release_commit=commit,
        last_manifest_sha256=digest,
        last_version=version,
        **diagnostics,
    )


def _windows_system_root() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise UpdateInspectionError("Windows system directory could not be resolved safely")
    value = Path(buffer.value)
    if not value.is_absolute():
        raise UpdateInspectionError("Windows system directory could not be resolved safely")
    return value


def _git_environment(git_executable: Optional[str] = None) -> Dict[str, str]:
    allowed = {
        "LANG", "LC_ALL", "TEMP", "TMP", "TZ",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    executable_parent = str(Path(git_executable).parent) if git_executable else ""
    if os.name == "nt":
        system_root = _windows_system_root()
        environment["SYSTEMROOT"] = str(system_root)
        environment["WINDIR"] = str(system_root)
        safe_path = [executable_parent, str(system_root / "System32")]
        safe_home = system_root / "System32" / "config" / "systemprofile" / ".deeppattern-no-home"
    else:
        safe_path = [executable_parent, "/usr/bin", "/bin"]
        safe_home = Path("/nonexistent/deeppattern-no-home")
    environment["PATH"] = os.pathsep.join(item for item in safe_path if item)
    environment["HOME"] = str(safe_home)
    environment.update({
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "",
        "GIT_TERMINAL_PROMPT": "0",
        "PAGER": "",
    })
    return environment


def _ambient_git_environment(git_executable: Optional[str] = None) -> Dict[str, str]:
    """Environment for git operations that must be able to resolve the
    caller's own already-authenticated credentials (HTTPS credential helper
    / SSH agent) — the private-beta-only, knowingly accepted exception to
    ``_git_environment``'s "never touch personal credentials" hardening (see
    ``release_acquisition.discover_initial_release_via_git`` /
    ``discover_release_via_git`` and their callers for the exact, narrow
    scope this is used in).

    Unlike ``_git_environment`` (which sets ``HOME`` to a nonexistent path
    and points ``GIT_CONFIG_GLOBAL`` at ``os.devnull`` specifically so a
    credential helper or global ``credential.helper``/SSH config can never be
    found), this starts from the real ambient environment so those DO
    resolve. It still strips every ``GIT_*`` variable (anti-injection, same
    as ``de_private_repo.py``'s equivalent) and — deliberately different from
    that already-reviewed, directly-human-run CLI's own choice of
    ``GIT_TERMINAL_PROMPT=1`` — forces ``GIT_TERMINAL_PROMPT=0`` here: every
    caller of this environment invokes git from a Python subprocess, not a
    terminal a human is watching, so a credential that is not already cached
    must fail fast into a catchable error instead of hanging (fatal for the
    ongoing per-MCP-start launcher path, whose stdin is wired for MCP
    protocol messages, not a git password prompt).
    """
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.pop("SSH_ASKPASS", None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_PAGER"] = ""
    environment["PAGER"] = ""
    environment["SSH_ASKPASS_REQUIRE"] = "never"
    return environment


def _trusted_git_candidates() -> Tuple[Path, ...]:
    if os.name == "nt":
        import winreg

        roots = []
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion",
                access=winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
            ) as key:
                for name in ("ProgramFilesDir", "ProgramFilesDir (x86)", "ProgramW6432Dir"):
                    try:
                        value, _kind = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if isinstance(value, str) and Path(value).is_absolute():
                        roots.append(Path(value))
        except OSError:
            pass
        candidates = [
            root / "Git" / leaf / "git.exe"
            for root in roots
            for leaf in ("cmd", "bin")
        ]
    else:
        candidates = [
            Path("/usr/bin/git"),
            Path("/usr/local/bin/git"),
            Path("/opt/homebrew/bin/git"),
            Path("/opt/local/bin/git"),
            Path("/snap/bin/git"),
        ]
    return tuple(dict.fromkeys(candidates))


def _resolve_git_executables() -> Tuple[str, ...]:
    executables = []
    for candidate in _trusted_git_candidates():
        try:
            resolved = candidate.resolve(strict=True)
            info = os.stat(resolved, follow_symlinks=False)
        except OSError:
            continue
        if not resolved.is_absolute() or not stat.S_ISREG(info.st_mode):
            continue
        if os.name == "nt":
            if resolved.suffix.casefold() != ".exe":
                continue
            try:
                trusted_root = candidate.parents[2].resolve(strict=True)
                resolved.relative_to(trusted_root)
            except (OSError, ValueError):
                continue
            current = trusted_root
            unsafe_component = False
            for part in resolved.relative_to(trusted_root).parts:
                current = current / part
                try:
                    current_info = os.lstat(current)
                except OSError:
                    unsafe_component = True
                    break
                attrs = getattr(current_info, "st_file_attributes", 0)
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if stat.S_ISLNK(current_info.st_mode) or bool(
                    reparse and attrs & reparse
                ):
                    unsafe_component = True
                    break
            if unsafe_component:
                continue
        else:
            allowed_owners = {0}
            if hasattr(os, "getuid"):
                allowed_owners.add(os.getuid())
            trusted_path = True
            for component in reversed((resolved, *resolved.parents)):
                try:
                    component_info = os.lstat(component)
                except OSError:
                    trusted_path = False
                    break
                if (
                    stat.S_ISLNK(component_info.st_mode)
                    or component_info.st_uid not in allowed_owners
                    or stat.S_IMODE(component_info.st_mode) & 0o022
                ):
                    trusted_path = False
                    break
            if not trusted_path:
                continue
            if not os.access(resolved, os.X_OK):
                continue
        value = str(resolved)
        if value not in executables:
            executables.append(value)
    if not executables:
        raise UpdateInspectionError(
            "Git is not installed in a trusted system location"
        )
    return tuple(executables)


def _resolve_git_executable() -> str:
    return _resolve_git_executables()[0]


def _git_executable_generation(
    path: str, *, deadline: Optional[float] = None
) -> Tuple[object, ...]:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise UpdateInspectionError("trusted Git executable is unavailable") from exc
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(reparse and attrs & reparse)
    ):
        raise UpdateInspectionError("trusted Git executable changed type")
    if info.st_size > _MAX_GIT_EXECUTABLE_BYTES:
        raise UpdateInspectionError("trusted Git executable exceeds the size limit")
    digest = hashlib.sha256()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = None
    try:
        fd = os.open(path, flags)
        try:
            handle = os.fdopen(fd, "rb")
        except OSError:
            os.close(fd)
            fd = None
            raise
        fd = None
        with handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or not _same_stat_identity(info, opened):
                raise UpdateInspectionError(
                    "trusted Git executable changed while being verified"
                )
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    raise UpdateInspectionError(
                        "Git inspection exceeded the overall deadline"
                    )
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            opened_after = os.fstat(handle.fileno())
    except OSError as exc:
        raise UpdateInspectionError("trusted Git executable is unreadable") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if not (
        _same_stat_identity(info, opened)
        and _same_stat_identity(opened, opened_after)
        and (opened.st_size, getattr(opened, "st_mtime_ns", None))
        == (opened_after.st_size, getattr(opened_after, "st_mtime_ns", None))
    ):
        raise UpdateInspectionError("trusted Git executable changed while being verified")
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        getattr(info, "st_mtime_ns", None),
        digest.hexdigest(),
    )


def _git_arguments(
    operation: str, root: Path, *, git_executable: Optional[str] = None, **values: str
) -> Tuple[str, ...]:
    template = _GIT_OPERATION_TEMPLATES.get(operation)
    if template is None:
        raise UpdateInspectionError("unregistered Git inspection operation")
    expected_values = {
        "target": {"tag"},
        "target_tree": {"commit"},
        "trust_store": {"commit"},
    }.get(operation, set())
    if set(values) != expected_values:
        raise UpdateInspectionError("invalid Git inspection operation arguments")
    if "tag" in values and not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", values["tag"]):
        raise UpdateInspectionError("target tag is invalid")
    if "commit" in values and not _COMMIT_RE.fullmatch(values["commit"]):
        raise UpdateInspectionError("target commit is invalid")
    if operation == "target":
        suffix = tuple(item.replace("{tag}", values["tag"]) for item in template)
    elif operation in {"target_tree", "trust_store"}:
        suffix = tuple(item.replace("{commit}", values["commit"]) for item in template)
    else:
        suffix = template
    if operation == "version":
        return (git_executable or _resolve_git_executable(), *suffix)
    worktree_override = () if operation == "top_level" else (
        "-c", "core.worktree=" + str(root),
    )
    return (
        git_executable or _resolve_git_executable(),
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-pager",
        "--no-replace-objects",
        "-c", "gc.auto=0",
        "-c", "maintenance.auto=0",
        "-c", "trace2.normalTarget=",
        "-c", "trace2.perfTarget=",
        "-c", "trace2.eventTarget=",
        "-c", "core.fsmonitor=false",
        "-c", "core.attributesFile=" + os.devnull,
        "-c", "core.excludesFile=" + os.devnull,
        *worktree_override,
        "-c", "core.untrackedCache=false",
        "-c", "submodule.recurse=false",
        "-C", str(root),
        *suffix,
    )


def _bounded_reap(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=_GIT_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise UpdateInspectionError("Git inspection process did not stop") from exc


def _run_bounded_git(
    argv: Sequence[str],
    environment: Mapping[str, str],
    *,
    timeout_seconds: Optional[float] = None,
) -> Tuple[int, bytes]:
    timeout_seconds = _GIT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    process = subprocess.Popen(
        list(argv),
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    captured = bytearray()
    reader_errors = []
    overflow = threading.Event()
    saw_eof = threading.Event()

    def drain() -> None:
        try:
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    saw_eof.set()
                    break
                remaining = _GIT_OUTPUT_LIMIT + 1 - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                if len(captured) > _GIT_OUTPUT_LIMIT or len(chunk) > remaining:
                    overflow.set()
        except (OSError, ValueError) as exc:
            reader_errors.append(exc)
        finally:
            if process.stdout is not None:
                process.stdout.close()

    reader = threading.Thread(target=drain, name="updater-git-output", daemon=True)
    reader.start()
    timed_out = False
    returncode = None
    try:
        deadline = time.monotonic() + timeout_seconds
        while returncode is None and not overflow.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                returncode = process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
        if returncode is None:
            try:
                process.kill()
            except OSError:
                pass
            _bounded_reap(process)
            returncode = process.returncode
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            _bounded_reap(process)
        reader.join(timeout=2)
        reader_was_stuck = reader.is_alive()
        if not reader_was_stuck and process.stdout is not None:
            process.stdout.close()
    if reader_was_stuck:
        raise UpdateInspectionError("Git output reader did not stop")
    if overflow.is_set() or len(captured) > _GIT_OUTPUT_LIMIT:
        raise UpdateInspectionError("Git inspection output exceeds the size limit")
    if timed_out:
        raise UpdateInspectionError("Git inspection timed out")
    if reader_errors:
        raise UpdateInspectionError("could not read Git inspection output") from reader_errors[0]
    if not saw_eof.is_set():
        raise UpdateInspectionError("Git inspection output ended unexpectedly")
    if returncode is None:
        raise UpdateInspectionError("Git inspection did not return a status")
    return returncode, bytes(captured)


class _GitReader:
    def __init__(self, root: Path, *, deadline: Optional[float] = None):
        self.root = Path(root)
        inspection_deadline = time.monotonic() + _INSPECTION_TIMEOUT_SECONDS
        self.deadline = (
            inspection_deadline
            if deadline is None
            else min(inspection_deadline, float(deadline))
        )
        for executable in _resolve_git_executables():
            self.git_executable = executable
            try:
                self.git_generation = _git_executable_generation(
                    executable, deadline=self.deadline
                )
                self.environment = _git_environment(executable)
                _code, version_output = self.run("version")
                _require_supported_git_version(version_output)
            except UpdateInspectionError:
                continue
            return
        raise UpdateInspectionError(
            "Git 2.45 or newer is required in a trusted system location"
        )

    def run(self, operation: str, *, allowed: Iterable[int] = (0,), **values: str) -> Tuple[int, bytes]:
        if _git_executable_generation(
            self.git_executable, deadline=self.deadline
        ) != self.git_generation:
            raise UpdateInspectionError("trusted Git executable changed during inspection")
        argv = _git_arguments(
            operation, self.root, git_executable=self.git_executable, **values
        )
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise UpdateInspectionError("Git inspection exceeded the overall deadline")
        timeout_seconds = min(_GIT_OPERATION_TIMEOUTS[operation], remaining)
        try:
            returncode, output = _run_bounded_git(
                argv, self.environment, timeout_seconds=timeout_seconds
            )
        except UpdateInspectionError as exc:
            raise UpdateInspectionError(
                "Git inspection operation failed: %s: %s" % (operation, exc)
            ) from exc
        except FileNotFoundError as exc:
            raise UpdateInspectionError("Git is not installed") from exc
        except OSError as exc:
            raise UpdateInspectionError("Git inspection could not be started") from exc
        if _git_executable_generation(
            self.git_executable, deadline=self.deadline
        ) != self.git_generation:
            raise UpdateInspectionError("trusted Git executable changed during inspection")
        if returncode not in set(allowed):
            raise UpdateInspectionError("Git inspection operation failed: %s" % operation)
        return returncode, output


def _require_supported_git_version(output: bytes) -> Tuple[int, int, int]:
    try:
        value = output.decode("ascii").strip()
    except UnicodeError as exc:
        raise UpdateInspectionError("Git version output is invalid") from exc
    match = re.fullmatch(
        r"git version ([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?:[.\-+ ].*)?",
        value,
    )
    if match is None:
        raise UpdateInspectionError("Git version output is invalid")
    version = tuple(int(item or 0) for item in match.groups()[:3])
    if version < _MINIMUM_GIT_VERSION:
        raise UpdateInspectionError("Git 2.45 or newer is required for safe update inspection")
    return version


def _single_commit(output: bytes, label: str) -> str:
    try:
        value = output.decode("ascii").strip()
    except UnicodeError as exc:
        raise UpdateInspectionError("%s commit output is invalid" % label) from exc
    if not _COMMIT_RE.fullmatch(value):
        raise UpdateInspectionError("%s commit output is invalid" % label)
    return value


def _nul_records(output: bytes, label: str) -> Tuple[bytes, ...]:
    if not output:
        return ()
    records = output.split(b"\x00")
    if records[-1] != b"":
        raise UpdateInspectionError("%s output is incomplete" % label)
    if any(not record for record in records[:-1]):
        raise UpdateInspectionError("%s output contains an empty record" % label)
    return tuple(records[:-1])


def _read_local_config_names(output: bytes) -> Tuple[str, ...]:
    names = []
    for raw in _nul_records(output, "Git config-name"):
        try:
            name = raw.decode("utf-8")
        except UnicodeError as exc:
            raise UpdateInspectionError("checkout Git config contains an invalid name") from exc
        normalized = unicodedata.normalize("NFC", name).casefold()
        if not normalized or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized):
            raise UpdateInspectionError("checkout Git config contains an invalid name")
        names.append(normalized)
    return tuple(names)


def _read_remotes(reader: _GitReader) -> Dict[str, str]:
    returncode, output = reader.run("remotes", allowed=(0, 1))
    if returncode == 1 and output:
        raise UpdateInspectionError("Git remote configuration is invalid")
    remotes: Dict[str, str] = {}
    for raw in _nul_records(output, "Git remote configuration"):
        if b"\n" not in raw:
            raise UpdateInspectionError("Git remote configuration is invalid")
        raw_key, raw_value = raw.split(b"\n", 1)
        try:
            key = raw_key.decode("utf-8")
            value = raw_value.decode("utf-8")
        except UnicodeError as exc:
            raise UpdateInspectionError("Git remote configuration is invalid") from exc
        match = re.fullmatch(r"remote\.([^\s]+)\.url", key)
        if match is None or match.group(1) in remotes or not value:
            raise UpdateInspectionError("Git remote configuration is invalid")
        remotes[match.group(1)] = value
    return remotes


def _strip_hfs_ignorables(value: str) -> str:
    # Git's HFS protection removes formatting code points that the filesystem
    # treats as ignorable.  Conservatively apply the same identity on every OS
    # because a stable release must remain safe when installed cross-platform.
    ignored_ranges = (
        (0x200C, 0x200F),
        (0x202A, 0x202E),
        (0x2060, 0x206F),
    )
    return "".join(
        character
        for character in value
        if ord(character) != 0xFEFF
        and not any(start <= ord(character) <= end for start, end in ignored_ranges)
    )


def _portable_component_identity(value: str) -> str:
    return _strip_hfs_ignorables(unicodedata.normalize("NFC", value)).casefold()


def _validate_relative_git_path(value: str, *, portable: bool = True) -> Tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UpdateInspectionError("Git tree contains an invalid path")
    if value.startswith("/") or re.match(r"[A-Za-z]:/", value):
        raise UpdateInspectionError("Git tree contains an absolute path")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise UpdateInspectionError("Git tree contains an invalid path component")
    if len(value) > _MAX_PATH_CHARACTERS or len(parts) > _MAX_PATH_COMPONENTS:
        raise UpdateInspectionError("Git path exceeds the complexity limit")
    if portable:
        if any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for part in parts
            for character in part
        ):
            raise UpdateInspectionError("Git tree contains a control character in a path")
        for part in parts:
            if "\\" in part or part.rstrip(" .") != part or ":" in part or "~" in part:
                raise UpdateInspectionError("Git tree contains a non-portable path component")
            if part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES:
                raise UpdateInspectionError("Git tree contains a reserved path component")
            if _portable_component_identity(part) == ".git":
                raise UpdateInspectionError("Git tree contains a protected internal path")
    elif _local_filesystem_path_key(parts[0]) == _local_filesystem_path_key(".git"):
        raise UpdateInspectionError("Git tree contains a protected internal path")
    return parts


def _parse_nul_paths(
    output: bytes, *, portable: bool, directory_markers: bool = False
) -> Tuple[str, ...]:
    if not output:
        return ()
    records = output.split(b"\x00")
    if records[-1] != b"":
        raise UpdateInspectionError("Git path output is incomplete")
    values = []
    for raw in records[:-1]:
        if len(values) >= _MAX_GIT_PATHS:
            raise UpdateInspectionError("Git path output exceeds the path-count limit")
        if not raw:
            raise UpdateInspectionError("Git path output contains an empty path")
        if portable:
            try:
                value = raw.decode("utf-8")
            except UnicodeError as exc:
                raise UpdateInspectionError("Git tree path is not portable UTF-8") from exc
        else:
            value = os.fsdecode(raw)
        if directory_markers and value.endswith("/"):
            value = value[:-1]
        _validate_relative_git_path(value, portable=portable)
        values.append(value)
    if len(set(values)) != len(values):
        raise UpdateInspectionError("Git path output contains duplicates")
    return tuple(values)


@dataclass(frozen=True)
class _GitTreeEntry:
    mode: str
    object_id: str
    path: str


@dataclass(frozen=True)
class _GitIndexEntry:
    flag: str
    mode: str
    object_id: str
    stage: int
    path: str


@dataclass(frozen=True)
class _GitEolInfo:
    index: str
    worktree: str
    attribute: str


def _parse_tree_entries(output: bytes) -> Tuple[_GitTreeEntry, ...]:
    values = []
    seen_paths = set()
    pattern = re.compile(
        rb"([0-9]{6}) (blob|commit) ([0-9a-f]{40})\t(.+)\Z", re.DOTALL
    )
    for raw in _nul_records(output, "Git tree"):
        if len(values) >= _MAX_GIT_PATHS:
            raise UpdateInspectionError("Git tree output exceeds the path-count limit")
        match = pattern.fullmatch(raw)
        if match is None:
            raise UpdateInspectionError("Git tree output is invalid")
        mode = match.group(1).decode("ascii")
        object_type = match.group(2).decode("ascii")
        object_id = match.group(3).decode("ascii")
        try:
            path = match.group(4).decode("utf-8")
        except UnicodeError as exc:
            raise UpdateInspectionError("Git tree path is not portable UTF-8") from exc
        _validate_relative_git_path(path, portable=True)
        if path in seen_paths:
            raise UpdateInspectionError("Git tree output contains duplicate paths")
        if (mode == "160000") != (object_type == "commit"):
            raise UpdateInspectionError("Git tree entry type is invalid")
        seen_paths.add(path)
        values.append(_GitTreeEntry(mode, object_id, path))
    return tuple(values)


def _parse_index_entries(output: bytes) -> Tuple[_GitIndexEntry, ...]:
    values = []
    stages_by_path: Dict[str, set[int]] = {}
    pattern = re.compile(
        rb"([A-Za-z?]) ([0-9]{6}) ([0-9a-f]{40}) ([0-3])\t(.+)\Z", re.DOTALL
    )
    for raw in _nul_records(output, "Git index-flag"):
        if len(values) >= _MAX_GIT_PATHS:
            raise UpdateInspectionError("Git index output exceeds the path-count limit")
        match = pattern.fullmatch(raw)
        if match is None:
            raise UpdateInspectionError("Git index-flag output is invalid")
        try:
            flag = match.group(1).decode("ascii")
            mode = match.group(2).decode("ascii")
            object_id = match.group(3).decode("ascii")
            stage = int(match.group(4))
            path = match.group(5).decode("utf-8")
        except UnicodeError as exc:
            raise UpdateInspectionError("Git index-flag output is invalid") from exc
        _validate_relative_git_path(path, portable=True)
        stages = stages_by_path.setdefault(path, set())
        if stage in stages:
            raise UpdateInspectionError("Git index output contains duplicate paths")
        if (stage == 0 and stages) or (stage != 0 and 0 in stages):
            raise UpdateInspectionError("Git index output contains incoherent stages")
        stages.add(stage)
        values.append(_GitIndexEntry(flag, mode, object_id, stage, path))
    return tuple(values)


def _parse_index_eol(output: bytes) -> Dict[str, _GitEolInfo]:
    values: Dict[str, _GitEolInfo] = {}
    pattern = re.compile(
        rb"i/([^ \t]*)[ \t]+w/([^ \t]*)[ \t]+attr/([^\t]*)\t(.+)\Z",
        re.DOTALL,
    )
    for raw in _nul_records(output, "Git index-eol"):
        if len(values) >= _MAX_GIT_PATHS:
            raise UpdateInspectionError(
                "Git index-eol output exceeds the path-count limit"
            )
        match = pattern.fullmatch(raw)
        if match is None:
            raise UpdateInspectionError("Git index-eol output is invalid")
        try:
            index = match.group(1).decode("ascii")
            worktree = match.group(2).decode("ascii")
            attribute = match.group(3).decode("ascii").strip()
            path = match.group(4).decode("utf-8")
        except UnicodeError as exc:
            raise UpdateInspectionError("Git index-eol output is invalid") from exc
        _validate_relative_git_path(path, portable=True)
        if path in values:
            raise UpdateInspectionError("Git index-eol output contains duplicate paths")
        values[path] = _GitEolInfo(
            index=index, worktree=worktree, attribute=attribute
        )
    return values


def _read_autocrlf(reader: _GitReader) -> str:
    returncode, output = reader.run("autocrlf", allowed=(0, 1))
    if returncode == 1:
        if output:
            raise UpdateInspectionError("core.autocrlf configuration is invalid")
        return "unset"
    try:
        value = output.decode("ascii").strip().casefold()
    except UnicodeError as exc:
        raise UpdateInspectionError("core.autocrlf configuration is invalid") from exc
    if value in {"true", "yes", "on", "1"}:
        return "true"
    if value in {"false", "no", "off", "0"}:
        return "false"
    if value == "input":
        return "input"
    raise UpdateInspectionError("core.autocrlf configuration is invalid")


def _read_eol(reader: _GitReader) -> str:
    returncode, output = reader.run("eol", allowed=(0, 1))
    if returncode == 1:
        if output:
            raise UpdateInspectionError("core.eol configuration is invalid")
        return "unset"
    try:
        value = output.decode("ascii").strip().casefold()
    except UnicodeError as exc:
        raise UpdateInspectionError("core.eol configuration is invalid") from exc
    if value in {"lf", "crlf", "native"}:
        return value
    raise UpdateInspectionError("core.eol configuration is invalid")


def _read_symlinks(reader: _GitReader) -> str:
    returncode, output = reader.run("symlinks", allowed=(0, 1))
    if returncode == 1:
        if output:
            raise UpdateInspectionError("core.symlinks configuration is invalid")
        return "unset"
    try:
        value = output.decode("ascii").strip().casefold()
    except UnicodeError as exc:
        raise UpdateInspectionError("core.symlinks configuration is invalid") from exc
    if value in {"true", "yes", "on", "1"}:
        return "true"
    if value in {"false", "no", "off", "0"}:
        return "false"
    raise UpdateInspectionError("core.symlinks configuration is invalid")


def _validate_local_checkout_policy(autocrlf: str, symlinks: str) -> None:
    if os.name == "nt" and (autocrlf == "unset" or symlinks == "unset"):
        raise UpdateInspectionError(
            "managed Windows checkout must pin local core.autocrlf and core.symlinks"
        )


def _crlf_conversion_authorized(info: _GitEolInfo, autocrlf: str, eol: str) -> bool:
    if info.index != "lf" or info.worktree != "crlf":
        return False
    attributes = set(info.attribute.split())
    if "-text" in attributes or "eol=lf" in attributes:
        return False
    if "eol=crlf" in attributes:
        return True
    if attributes & {"text", "text=auto"}:
        if autocrlf == "true":
            return True
        if autocrlf == "input":
            return False
        return eol == "crlf" or (
            os.name == "nt" and eol in {"native", "unset"}
        )
    return not attributes and autocrlf == "true"


def _normalized_crlf_chunks(handle, deadline: float, byte_count: int):
    pending_cr = False
    remaining = byte_count
    while remaining:
        if time.monotonic() > deadline:
            raise UpdateInspectionError(
                "tracked-file comparison exceeded its time budget"
            )
        chunk = handle.read(min(1024 * 1024, remaining))
        if not chunk:
            raise UpdateInspectionError("tracked file changed during comparison")
        remaining -= len(chunk)
        if pending_cr:
            chunk = b"\r" + chunk
            pending_cr = False
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
            pending_cr = True
        if chunk:
            yield chunk.replace(b"\r\n", b"\n")
    if pending_cr:
        yield b"\r"


def _tracked_leaf_anchor(
    root: Path, parts: Sequence[str]
) -> Tuple[Path, Optional[int], str, os.stat_result]:
    path = Path(root).joinpath(*parts)
    if not _supports_safe_dir_fd():
        current = Path(root)
        for part in parts[:-1]:
            current = current / part
            info = os.lstat(current)
            attrs = getattr(info, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or bool(reparse and attrs & reparse)
            ):
                raise NotADirectoryError(str(current))
        return path, None, parts[-1], os.lstat(path)

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = None
    try:
        root_info = os.lstat(root)
        parent_fd = os.open(root, directory_flags)
        opened_root = os.fstat(parent_fd)
        if not stat.S_ISDIR(opened_root.st_mode) or not _same_stat_identity(
            root_info, opened_root
        ):
            raise UpdateInspectionError("tracked path changed during comparison")
        for part in parts[:-1]:
            info = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise NotADirectoryError(part)
            child_fd = None
            try:
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                opened_child = os.fstat(child_fd)
                if not stat.S_ISDIR(opened_child.st_mode) or not _same_stat_identity(
                    info, opened_child
                ):
                    raise UpdateInspectionError(
                        "tracked path changed during comparison"
                    )
            except (OSError, UpdateInspectionError):
                if child_fd is not None:
                    os.close(child_fd)
                raise
            previous_fd = parent_fd
            parent_fd = child_fd
            os.close(previous_fd)
        before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        return path, parent_fd, parts[-1], before
    except (OSError, UpdateInspectionError):
        if parent_fd is not None:
            os.close(parent_fd)
        raise


def _git_symlink_target_bytes(target: str) -> bytes:
    if os.name == "nt":
        target = target.replace("\\", "/")
    return os.fsencode(target)


def _working_tree_blob_ids(
    root: Path,
    entry: _GitIndexEntry,
    *,
    allow_crlf_normalization: bool = False,
    allow_regular_symlink_file: bool = False,
    remaining_bytes: int,
    deadline: float,
) -> Tuple[Tuple[str, ...], int, bool]:
    parts = _validate_relative_git_path(entry.path, portable=True)
    parent_fd = None
    try:
        path, parent_fd, leaf_name, before = _tracked_leaf_anchor(root, parts)
    except OSError:
        return (), 0, False
    attrs = getattr(before, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse and attrs & reparse and not stat.S_ISLNK(before.st_mode):
        if parent_fd is not None:
            os.close(parent_fd)
        return (), 0, False

    if entry.mode == "160000":
        if parent_fd is not None:
            os.close(parent_fd)
        return (), 0, False
    if entry.mode == "120000":
        if stat.S_ISREG(before.st_mode) and allow_regular_symlink_file:
            if parent_fd is not None:
                os.close(parent_fd)
            regular_entry = _GitIndexEntry(
                entry.flag, "100644", entry.object_id, entry.stage, entry.path
            )
            return _working_tree_blob_ids(
                root,
                regular_entry,
                remaining_bytes=remaining_bytes,
                deadline=deadline,
            )
        if not stat.S_ISLNK(before.st_mode):
            if parent_fd is not None:
                os.close(parent_fd)
            return (), 0, False
        try:
            if parent_fd is not None:
                if os.readlink not in os.supports_dir_fd:
                    return (), 0, False
                target = os.readlink(leaf_name, dir_fd=parent_fd)
                after = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
            else:
                target = os.readlink(path)
                after = os.lstat(path)
        except OSError:
            return (), 0, False
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        body = _git_symlink_target_bytes(target)
        if not _same_stat_identity(before, after):
            return (), 0, False
        digest = hashlib.sha1(b"blob %d\x00" % len(body) + body).hexdigest()
        return (digest,), len(body), True
    if entry.mode not in {"100644", "100755"} or not stat.S_ISREG(before.st_mode):
        if parent_fd is not None:
            os.close(parent_fd)
        return (), 0, False
    if before.st_size > _MAX_TRACKED_FILE_BYTES or before.st_size > remaining_bytes:
        if parent_fd is not None:
            os.close(parent_fd)
        return (), 0, False
    if os.name != "nt":
        executable = bool(stat.S_IMODE(before.st_mode) & 0o111)
        if executable != (entry.mode == "100755"):
            if parent_fd is not None:
                os.close(parent_fd)
            return (), 0, False

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        if parent_fd is not None:
            fd = os.open(leaf_name, flags, dir_fd=parent_fd)
        else:
            fd = os.open(path, flags)
        try:
            handle = os.fdopen(fd, "rb")
        except OSError:
            os.close(fd)
            raise
        with handle:
            opened = os.fstat(handle.fileno())
            if not _same_stat_identity(before, opened) or opened.st_size != before.st_size:
                return (), 0, False
            raw_hash = hashlib.sha1(b"blob %d\x00" % opened.st_size)
            crlf_pairs = 0
            previous_ended_with_cr = False
            remaining_to_read = opened.st_size
            while remaining_to_read:
                if time.monotonic() > deadline:
                    raise UpdateInspectionError(
                        "tracked-file comparison exceeded its time budget"
                    )
                chunk = handle.read(min(1024 * 1024, remaining_to_read))
                if not chunk:
                    return (), 0, False
                remaining_to_read -= len(chunk)
                raw_hash.update(chunk)
                crlf_pairs += chunk.count(b"\r\n")
                if previous_ended_with_cr and chunk.startswith(b"\n"):
                    crlf_pairs += 1
                previous_ended_with_cr = chunk.endswith(b"\r")
            raw_digest = raw_hash.hexdigest()
            digests = [raw_digest]
            consumed = opened.st_size

            # Git's documented --eol view is the conversion authority.  It
            # permits this second digest only when Git reports an LF index blob
            # and a CRLF worktree file for the same path.  Other clean/smudge
            # transforms remain conservatively dirty.
            if raw_digest != entry.object_id and allow_crlf_normalization:
                if opened.st_size > remaining_bytes - consumed:
                    return (), 0, False
                normalized_size = opened.st_size - crlf_pairs
                handle.seek(0)
                normalized_hash = hashlib.sha1(
                    b"blob %d\x00" % normalized_size
                )
                for chunk in _normalized_crlf_chunks(
                    handle, deadline, opened.st_size
                ):
                    normalized_hash.update(chunk)
                digests.append(normalized_hash.hexdigest())
                consumed += opened.st_size
            opened_after = os.fstat(handle.fileno())
        if parent_fd is not None:
            after = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
        else:
            after = os.lstat(path)
    except OSError:
        return (), 0, False
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    stable = (
        _same_stat_identity(before, opened)
        and _same_stat_identity(opened, opened_after)
        and _same_stat_identity(opened, after)
        and (opened.st_size, getattr(opened, "st_mtime_ns", None))
        == (opened_after.st_size, getattr(opened_after, "st_mtime_ns", None))
    )
    return tuple(digests), consumed, stable


def _tracked_worktree_is_dirty(
    root: Path,
    tree_entries: Iterable[_GitTreeEntry],
    index_entries: Iterable[_GitIndexEntry],
    eol_info: Mapping[str, _GitEolInfo],
    autocrlf: str,
    eol: str,
    symlinks: str,
    *,
    deadline: Optional[float] = None,
) -> bool:
    return bool(
        _tracked_worktree_drift_paths(
            root,
            tree_entries,
            index_entries,
            eol_info,
            autocrlf,
            eol,
            symlinks,
            deadline=deadline,
        )
    )


def _tracked_worktree_drift_paths(
    root: Path,
    tree_entries: Iterable[_GitTreeEntry],
    index_entries: Iterable[_GitIndexEntry],
    eol_info: Mapping[str, _GitEolInfo],
    autocrlf: str,
    eol: str,
    symlinks: str,
    *,
    deadline: Optional[float] = None,
) -> Tuple[str, ...]:
    """Return the exact content-based drift set used by transactional recovery."""

    if deadline is None:
        deadline = time.monotonic() + _TRACKED_HASH_TIMEOUT_SECONDS
    elif time.monotonic() > deadline:
        raise UpdateInspectionError(
            "tracked-file comparison exceeded the overall deadline"
        )
    tree = {}
    for entry in tree_entries:
        if time.monotonic() > deadline:
            raise UpdateInspectionError(
                "tracked-file comparison exceeded the overall deadline"
            )
        if len(tree) >= _MAX_GIT_PATHS or entry.path in tree:
            raise UpdateInspectionError("tracked-file comparison is structurally invalid")
        tree[entry.path] = (entry.mode, entry.object_id)
    index = {}
    drift = set()
    for entry in index_entries:
        if time.monotonic() > deadline:
            raise UpdateInspectionError(
                "tracked-file comparison exceeded the overall deadline"
            )
        if len(index) >= _MAX_GIT_PATHS or entry.stage != 0 or entry.path in index:
            raise UpdateInspectionError("tracked-file comparison is structurally invalid")
        index[entry.path] = entry
        if entry.flag != "H":
            drift.add(entry.path)
    for path in set(tree) | set(index):
        entry = index.get(path)
        if entry is None or tree.get(path) != (entry.mode, entry.object_id):
            drift.add(path)
    if len(eol_info) != len(index) or set(eol_info) != set(index):
        raise UpdateInspectionError("tracked-file EOL metadata is structurally invalid")
    deadline = min(deadline, time.monotonic() + _TRACKED_HASH_TIMEOUT_SECONDS)
    remaining = _MAX_TRACKED_TOTAL_BYTES
    for entry in index.values():
        if time.monotonic() > deadline:
            raise UpdateInspectionError(
                "tracked-file comparison exceeded the overall deadline"
            )
        digests, consumed, stable = _working_tree_blob_ids(
            root,
            entry,
            allow_crlf_normalization=_crlf_conversion_authorized(
                eol_info[entry.path], autocrlf, eol
            ),
            allow_regular_symlink_file=os.name == "nt" and symlinks == "false",
            remaining_bytes=remaining,
            deadline=deadline,
        )
        if not stable or entry.object_id not in digests:
            drift.add(entry.path)
        remaining -= consumed
    if len(drift) > _MAX_GIT_PATHS:
        raise UpdateInspectionError("tracked drift exceeds the path-count limit")
    return tuple(sorted(drift))


def _tracked_files_generation(
    root: Path,
    index_entries: Iterable[_GitIndexEntry],
    *,
    deadline: Optional[float] = None,
) -> Tuple[Tuple[object, ...], ...]:
    if deadline is None:
        deadline = time.monotonic() + _INSPECTION_TIMEOUT_SECONDS
    if time.monotonic() > deadline:
        raise UpdateInspectionError("tracked generation exceeded the overall deadline")
    generation = []
    scanned = 0
    for entry in index_entries:
        if time.monotonic() > deadline:
            raise UpdateInspectionError("tracked generation exceeded the overall deadline")
        scanned += 1
        if scanned > _MAX_GIT_PATHS:
            raise UpdateInspectionError("tracked generation exceeded the path limit")
        path = Path(root).joinpath(*_validate_relative_git_path(entry.path, portable=True))
        try:
            info = os.lstat(path)
        except OSError:
            generation.append((entry.path, None))
            continue
        generation.append((
            entry.path,
            info.st_dev,
            info.st_ino,
            stat.S_IFMT(info.st_mode),
            info.st_size,
            getattr(info, "st_mtime_ns", None),
        ))
    return tuple(generation)


def _local_filesystem_path_key(value: str) -> str:
    if os.name == "nt":
        return os.path.normcase(value)
    # POSIX mounts can be case- and normalization-sensitive even on Darwin.
    # Exact membership may conservatively refuse on a case-insensitive native
    # volume, but it never merges two distinct local paths.
    return value


def _sorted_paths_have_descendant(paths: Sequence[str], directory: str) -> bool:
    prefix = directory + ("\\" if os.name == "nt" else "/")
    insertion = bisect_left(paths, prefix)
    return insertion < len(paths) and paths[insertion].startswith(prefix)


def _filesystem_unknown_paths(
    root: Path,
    index_entries: Iterable[_GitIndexEntry],
    *,
    deadline: Optional[float] = None,
) -> Tuple[str, ...]:
    """Supplement Git's view with no-follow entries, collapsing unknown roots."""
    if deadline is None:
        deadline = time.monotonic() + _INSPECTION_TIMEOUT_SECONDS
    if time.monotonic() > deadline:
        raise UpdateInspectionError("filesystem walk exceeded the overall deadline")
    entry_count = 0
    tracked = set()
    for entry in index_entries:
        if time.monotonic() > deadline:
            raise UpdateInspectionError("filesystem walk exceeded the overall deadline")
        if entry_count >= _MAX_GIT_PATHS:
            raise UpdateInspectionError("filesystem walk exceeded the path limit")
        entry_count += 1
        tracked.add(_local_filesystem_path_key(entry.path))
    tracked_paths = tuple(sorted(tracked))

    unknown = []
    encoded_bytes = 0
    scanned = 0
    stack = [(Path(root), ())]
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            raise UpdateInspectionError("filesystem walk exceeded the overall deadline")
        directory, parent_parts = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if deadline is not None and time.monotonic() > deadline:
                        raise UpdateInspectionError(
                            "filesystem walk exceeded the overall deadline"
                        )
                    if not parent_parts and _local_filesystem_path_key(
                        entry.name
                    ) == _local_filesystem_path_key(".git"):
                        continue
                    scanned += 1
                    if scanned > _MAX_GIT_PATHS:
                        raise UpdateInspectionError(
                            "filesystem walk exceeds the path-count limit"
                        )
                    parts = (*parent_parts, entry.name)
                    relative = "/".join(parts)
                    relative_key = _local_filesystem_path_key(relative)
                    _validate_relative_git_path(relative, portable=False)
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise UpdateInspectionError(
                            "could not inspect filesystem paths safely"
                        ) from exc
                    attrs = getattr(info, "st_file_attributes", 0)
                    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    traversable = (
                        stat.S_ISDIR(info.st_mode)
                        and not stat.S_ISLNK(info.st_mode)
                        and not bool(reparse and attrs & reparse)
                    )
                    if relative_key in tracked and not traversable:
                        continue
                    if (
                        relative_key not in tracked
                        and traversable
                        and _sorted_paths_have_descendant(tracked_paths, relative_key)
                    ):
                        stack.append((Path(entry.path), parts))
                        continue
                    encoded_bytes += len(os.fsencode(relative)) + 1
                    if encoded_bytes > _MAX_UNKNOWN_PATH_BYTES:
                        raise UpdateInspectionError(
                            "filesystem walk exceeds the path-byte limit"
                        )
                    unknown.append(relative)
                    if len(unknown) > _MAX_GIT_PATHS:
                        raise UpdateInspectionError(
                            "filesystem walk exceeds the path-count limit"
                        )
        except UpdateInspectionError:
            raise
        except OSError as exc:
            raise UpdateInspectionError(
                "could not inspect filesystem paths safely"
            ) from exc
    return tuple(sorted(unknown))


def _identity_parts(path: str, *, fold_case: bool, normalize_unicode: bool) -> Tuple[str, ...]:
    parts = _validate_relative_git_path(path, portable=False)
    normalized = []
    for part in parts:
        item = unicodedata.normalize("NFC", part) if normalize_unicode else part
        item = _strip_hfs_ignorables(item)
        normalized.append(item.casefold() if fold_case else item)
    return tuple(normalized)


def _classify_collisions(
    target_paths: Iterable[str],
    unknown_paths: Iterable[str],
    *,
    fold_case: bool,
    normalize_unicode: bool,
    deadline: Optional[float] = None,
) -> Tuple[str, ...]:
    if deadline is not None and time.monotonic() > deadline:
        raise UpdateInspectionError("collision scan exceeded the overall deadline")
    target_identity_set = set()
    for path in target_paths:
        if deadline is not None and time.monotonic() > deadline:
            raise UpdateInspectionError("collision scan exceeded the overall deadline")
        target_identity_set.add(
            _identity_parts(
                path, fold_case=fold_case, normalize_unicode=normalize_unicode
            )
        )
    target_identities = sorted(target_identity_set)
    target_exact = set(target_identities)
    collisions = []
    for path in unknown_paths:
        if deadline is not None and time.monotonic() > deadline:
            raise UpdateInspectionError("collision scan exceeded the overall deadline")
        identity = _identity_parts(
            path, fold_case=fold_case, normalize_unicode=normalize_unicode
        )
        target_is_ancestor = any(
            identity[:length] in target_exact
            for length in range(1, len(identity) + 1)
        )
        insertion = bisect_left(target_identities, identity)
        unknown_is_ancestor = (
            insertion < len(target_identities)
            and target_identities[insertion][:len(identity)] == identity
        )
        if target_is_ancestor or unknown_is_ancestor:
            collisions.append(path)
    return tuple(sorted(set(collisions)))


def _target_has_protected_path(
    paths: Iterable[str], *, deadline: Optional[float] = None
) -> bool:
    if deadline is not None and time.monotonic() > deadline:
        raise UpdateInspectionError("target scan exceeded the overall deadline")
    for path in paths:
        if deadline is not None and time.monotonic() > deadline:
            raise UpdateInspectionError("target scan exceeded the overall deadline")
        parts = _validate_relative_git_path(path)
        first = _portable_component_identity(parts[0])
        if first in _PROTECTED_TARGETS:
            return True
    return False


def _validate_target_identities(
    paths: Iterable[str], *, deadline: Optional[float] = None
) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise UpdateInspectionError("target scan exceeded the overall deadline")
    identities = []
    for path in paths:
        if deadline is not None and time.monotonic() > deadline:
            raise UpdateInspectionError("target scan exceeded the overall deadline")
        identities.append(
            _identity_parts(path, fold_case=True, normalize_unicode=True)
        )
    identities.sort()
    for previous, identity in zip(identities, identities[1:]):
        if deadline is not None and time.monotonic() > deadline:
            raise UpdateInspectionError("target scan exceeded the overall deadline")
        if previous == identity:
            raise UpdateInspectionError("signed target contains normalized path collisions")
        if identity[:len(previous)] == previous:
            raise UpdateInspectionError("signed target contains path ancestry collisions")


def _reject_collision_links(
    root: Path,
    collisions: Iterable[str],
    *,
    deadline: Optional[float] = None,
) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise UpdateInspectionError("collision scan exceeded the overall deadline")
    if _supports_safe_dir_fd():
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        root_fd = None
        try:
            root_info = os.lstat(root)
            root_fd = os.open(root, directory_flags)
            opened_root = os.fstat(root_fd)
        except OSError as exc:
            if root_fd is not None:
                os.close(root_fd)
            raise UpdateInspectionError("could not inspect collision root safely") from exc
        if not stat.S_ISDIR(opened_root.st_mode) or not _same_stat_identity(
            root_info, opened_root
        ):
            os.close(root_fd)
            raise UpdateInspectionError("collision root changed during inspection")
        try:
            for relative in collisions:
                if deadline is not None and time.monotonic() > deadline:
                    raise UpdateInspectionError(
                        "collision scan exceeded the overall deadline"
                    )
                parts = _validate_relative_git_path(relative, portable=False)
                parent_fd = root_fd
                opened_directories = []
                try:
                    for index, part in enumerate(parts):
                        if deadline is not None and time.monotonic() > deadline:
                            raise UpdateInspectionError(
                                "collision scan exceeded the overall deadline"
                            )
                        try:
                            info = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            break
                        except OSError as exc:
                            raise UpdateInspectionError(
                                "could not inspect collision path safely"
                            ) from exc
                        if stat.S_ISLNK(info.st_mode):
                            raise UpdateInspectionError(
                                "collision path contains a link or reparse point"
                            )
                        if index == len(parts) - 1:
                            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                                raise UpdateInspectionError(
                                    "collision path contains a multiply linked file"
                                )
                            break
                        if not stat.S_ISDIR(info.st_mode):
                            break
                        child_fd = None
                        try:
                            child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                            opened_child = os.fstat(child_fd)
                        except OSError as exc:
                            if child_fd is not None:
                                os.close(child_fd)
                            raise UpdateInspectionError(
                                "could not inspect collision path safely"
                            ) from exc
                        if not stat.S_ISDIR(opened_child.st_mode) or not _same_stat_identity(
                            info, opened_child
                        ):
                            os.close(child_fd)
                            raise UpdateInspectionError(
                                "collision path changed during inspection"
                            )
                        opened_directories.append(child_fd)
                        parent_fd = child_fd
                finally:
                    for child_fd in reversed(opened_directories):
                        os.close(child_fd)
        finally:
            os.close(root_fd)
        return

    # Platforms without the required no-follow dir_fd traversal use a
    # conservative path-based diagnostic only. PR4 must repeat it with pinned
    # native directory handles immediately before any write.
    for relative in collisions:
        if deadline is not None and time.monotonic() > deadline:
            raise UpdateInspectionError("collision scan exceeded the overall deadline")
        parts = _validate_relative_git_path(relative, portable=False)
        current = Path(root)
        for index, part in enumerate(parts):
            if deadline is not None and time.monotonic() > deadline:
                raise UpdateInspectionError("collision scan exceeded the overall deadline")
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise UpdateInspectionError("could not inspect collision path safely") from exc
            attrs = getattr(info, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if stat.S_ISLNK(info.st_mode) or bool(reparse and attrs & reparse):
                raise UpdateInspectionError("collision path contains a link or reparse point")
            if index == len(parts) - 1 and stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise UpdateInspectionError("collision path contains a multiply linked file")
            if index != len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                break


def inspect_update(
    root: Path,
    manifest: release_contract.ReleaseManifest,
    signature: release_contract.ReleaseSignature,
    trusted_keys: Mapping[str, release_contract.RsaPublicKey],
) -> UpdateInspection:
    """Return non-authoritative update diagnostics without changing Git or state.

    The protected prior is loaded internally; callers cannot inject an
    anti-rollback floor.  PR4 must repeat this function's checks under its
    exclusive lock rather than trusting the returned object.
    """
    requested_root = Path(root)
    reader = _GitReader(requested_root)
    _config_code, config_output = reader.run("unsafe_config")
    config_names = _read_local_config_names(config_output)
    if any(_UNSAFE_LOCAL_CONFIG_RE.fullmatch(name) for name in config_names):
        raise UpdateInspectionError("checkout Git config contains execution-capable settings")
    autocrlf = _read_autocrlf(reader)
    eol = _read_eol(reader)
    symlinks = _read_symlinks(reader)
    _validate_local_checkout_policy(autocrlf, symlinks)
    _code, top_output = reader.run("top_level")
    top_level = Path(os.fsdecode(top_output).strip())
    if managed_install._path_key(top_level) != managed_install._path_key(requested_root):
        raise UpdateInspectionError("managed root is not the Git top-level checkout")

    remotes = _read_remotes(reader)
    try:
        identity = managed_install.validate_managed_identity(requested_root, remotes)
    except managed_install.ManagedInstallError as exc:
        raise UpdateInspectionError("managed install identity is invalid") from exc
    if managed_install._path_key(identity.canonical_root) != managed_install._path_key(requested_root):
        raise UpdateInspectionError("managed identity root does not match the inspected checkout")

    _code, head_output = reader.run("head")
    head = _single_commit(head_output, "current HEAD")
    state = _read_update_state(identity.canonical_root)
    if head != state.last_release_commit:
        raise UpdateInspectionError("current HEAD does not match protected last-known-good state")

    verified = release_contract.authorize_release(
        manifest,
        signature,
        trusted_keys,
        expected_repository_id=identity.repository_id,
        expected_channel=managed_install.CHANNEL,
        last_sequence=state.last_release_sequence,
        last_commit=state.last_release_commit,
        last_manifest_sha256=state.last_manifest_sha256,
        running_python=tuple(sys.version_info[:3]),
    )
    if verified.manifest != manifest:
        raise UpdateInspectionError("authorized release does not match the inspected manifest")
    candidate = verified.manifest
    candidate_digest = release_contract.manifest_sha256(candidate)
    if candidate.release_sequence < state.last_release_sequence:
        raise UpdateInspectionError("authorized release sequence is older than local state")
    if candidate.release_sequence == state.last_release_sequence and (
        candidate.commit,
        candidate_digest,
        candidate.version,
    ) != (
        state.last_release_commit,
        state.last_manifest_sha256,
        state.last_version,
    ):
        raise UpdateInspectionError("authorized release sequence conflicts with local state")

    _code, target_output = reader.run("target", tag=candidate.tag)
    resolved_target = _single_commit(target_output, "release tag")
    if resolved_target != candidate.commit:
        raise UpdateInspectionError("release tag does not resolve to the signed commit")

    _code, tree_output = reader.run("target_tree", commit=candidate.commit)
    target_entries = _parse_tree_entries(tree_output)
    if any(entry.mode == "160000" for entry in target_entries):
        raise UpdateInspectionError("signed target contains an unsupported gitlink")
    target_paths = tuple(entry.path for entry in target_entries)
    if _target_has_protected_path(target_paths, deadline=reader.deadline):
        raise UpdateInspectionError("signed target tracks a protected local path")
    _validate_target_identities(target_paths, deadline=reader.deadline)

    _code, head_tree_output = reader.run("head_tree")
    _code, index_output = reader.run("index")
    _code, index_eol_output = reader.run("index_eol")
    head_tree_entries = _parse_tree_entries(head_tree_output)
    index_entries = _parse_index_entries(index_output)
    eol_info = _parse_index_eol(index_eol_output)
    tracked_generation = _tracked_files_generation(
        identity.canonical_root, index_entries, deadline=reader.deadline
    )
    tracked_dirty = _tracked_worktree_is_dirty(
        identity.canonical_root,
        head_tree_entries,
        index_entries,
        eol_info,
        autocrlf,
        eol,
        symlinks,
        deadline=reader.deadline,
    )
    _code, unknown_output = reader.run("unknown")
    unknown_paths = _parse_nul_paths(
        unknown_output, portable=False, directory_markers=True
    )
    filesystem_unknown_paths = _filesystem_unknown_paths(
        identity.canonical_root, index_entries, deadline=reader.deadline
    )
    unknown_paths = tuple(sorted(set(unknown_paths) | set(filesystem_unknown_paths)))
    collisions = _classify_collisions(
        target_paths,
        unknown_paths,
        fold_case=True,
        normalize_unicode=True,
        deadline=reader.deadline,
    )
    _reject_collision_links(
        identity.canonical_root, collisions, deadline=reader.deadline
    )

    # This result remains non-authoritative, but do not knowingly return a
    # collage of different repository generations.  PR4 still repeats every
    # check under its transaction lock and live-shim lease gate.
    _code, final_config_output = reader.run("unsafe_config")
    final_config_names = _read_local_config_names(final_config_output)
    final_autocrlf = _read_autocrlf(reader)
    final_eol = _read_eol(reader)
    final_symlinks = _read_symlinks(reader)
    _code, final_top_output = reader.run("top_level")
    final_top_level = Path(os.fsdecode(final_top_output).strip())
    final_remotes = _read_remotes(reader)
    try:
        final_identity = managed_install.validate_managed_identity(
            requested_root, final_remotes
        )
    except managed_install.ManagedInstallError as exc:
        raise UpdateInspectionError("managed install identity is invalid") from exc
    _code, final_head_output = reader.run("head")
    final_head = _single_commit(final_head_output, "current HEAD")
    final_state = _read_update_state(final_identity.canonical_root)
    _code, final_target_output = reader.run("target", tag=candidate.tag)
    final_target = _single_commit(final_target_output, "release tag")
    _code, final_tree_output = reader.run("target_tree", commit=candidate.commit)
    _code, final_head_tree_output = reader.run("head_tree")
    _code, final_index_output = reader.run("index")
    _code, final_index_eol_output = reader.run("index_eol")
    _code, final_unknown_output = reader.run("unknown")
    final_head_tree_entries = _parse_tree_entries(final_head_tree_output)
    final_index_entries = _parse_index_entries(final_index_output)
    final_eol_info = _parse_index_eol(final_index_eol_output)
    final_filesystem_unknown_paths = _filesystem_unknown_paths(
        final_identity.canonical_root,
        final_index_entries,
        deadline=reader.deadline,
    )
    final_tracked_generation = _tracked_files_generation(
        final_identity.canonical_root,
        final_index_entries,
        deadline=reader.deadline,
    )
    final_tracked_dirty = _tracked_worktree_is_dirty(
        final_identity.canonical_root,
        final_head_tree_entries,
        final_index_entries,
        final_eol_info,
        final_autocrlf,
        final_eol,
        final_symlinks,
        deadline=reader.deadline,
    )
    if (
        final_config_output != config_output
        or final_config_names != config_names
        or final_autocrlf != autocrlf
        or final_eol != eol
        or final_symlinks != symlinks
        or any(_UNSAFE_LOCAL_CONFIG_RE.fullmatch(name) for name in final_config_names)
        or managed_install._path_key(final_top_level)
        != managed_install._path_key(top_level)
        or final_remotes != remotes
        or final_identity != identity
        or final_head != head
        or final_state != state
        or final_target != resolved_target
        or final_tree_output != tree_output
        or final_head_tree_output != head_tree_output
        or final_index_output != index_output
        or final_index_eol_output != index_eol_output
        or final_unknown_output != unknown_output
        or final_filesystem_unknown_paths != filesystem_unknown_paths
        or final_tracked_dirty != tracked_dirty
        or final_tracked_generation != tracked_generation
    ):
        raise UpdateInspectionError("managed checkout changed during update inspection")
    _reject_collision_links(
        identity.canonical_root, collisions, deadline=reader.deadline
    )

    status = "up_to_date" if (
        candidate.release_sequence == state.last_release_sequence
        and candidate.commit == head
        and candidate_digest == state.last_manifest_sha256
        and candidate.version == state.last_version
    ) else "update_available"
    return UpdateInspection(
        status=status,
        current_commit=head,
        target_commit=candidate.commit,
        target_version=candidate.version,
        release_sequence=candidate.release_sequence,
        manifest_sha256=candidate_digest,
        tracked_dirty=tracked_dirty,
        collision_paths=collisions,
    )
