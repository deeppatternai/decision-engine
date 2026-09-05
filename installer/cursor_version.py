"""Read-only Windows Cursor version evidence.

The collector never executes Cursor.  It reads only fixed ``package.json``
leaves below Windows Known Folder installation roots and lexically records
PATH preference among those standard roots.  It deliberately does not claim
an exhaustive machine inventory or probe arbitrary PATH directories.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable, Optional, Tuple

from installer import client_host_version, windows_security


MAX_CURSOR_PACKAGE_BYTES = 64 * 1024
CURSOR_CANDIDATE_MINIMUM_TEXT = "2.4"
CURSOR_CANDIDATE_MINIMUM = client_host_version.NumericVersion.parse(
    CURSOR_CANDIDATE_MINIMUM_TEXT
)
_CURSOR_LAUNCHER_NAMES = frozenset({"cursor", "cursor.cmd", "cursor.exe"})
_MAX_WINDOWS_ENV_PATH_CHARS = 32_767
_MAX_WINDOWS_PATH_ENTRIES = 256
_MAX_WINDOWS_PATH_ENTRY_CHARS = 1_024
_FOLDERID_USER_PROGRAM_FILES = "5CD7AEE2-2219-4A67-B85D-6C9CE15660CB"
_FOLDERID_PROGRAM_FILES = "905E63B6-C1BF-494E-B29C-65B732D3D21A"
_FOLDERID_PROGRAM_FILES_X86 = "7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E"
_KF_FLAG_DONT_VERIFY = 0x00004000


class _CursorVersionEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class CursorVersionAssessment:
    state: str
    detail: str


def _is_link_like(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x00000400
    )


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        getattr(left, "st_mtime_ns", None),
        getattr(left, "st_file_attributes", 0),
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        getattr(right, "st_mtime_ns", None),
        getattr(right, "st_file_attributes", 0),
    )


def _json_object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _CursorVersionEvidenceError(
                "Cursor package metadata contains duplicate fields"
            )
        result[key] = value
    return result


def _parse_package(raw: bytes) -> client_host_version.NumericVersion:
    try:
        loaded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _CursorVersionEvidenceError(
            "Cursor package metadata is unreadable or invalid"
        ) from exc
    if not isinstance(loaded, dict) or loaded.get("name") != "Cursor":
        raise _CursorVersionEvidenceError("Cursor package metadata is invalid")
    version = loaded.get("version")
    try:
        return client_host_version.NumericVersion.parse(version)
    except (TypeError, ValueError) as exc:
        raise _CursorVersionEvidenceError(
            "Cursor package version is invalid"
        ) from exc


def _read_regular_file_bounded(path: Path) -> bytes:
    before = os.lstat(path)
    if _is_link_like(before) or not stat.S_ISREG(before.st_mode):
        raise _CursorVersionEvidenceError("Cursor package metadata is invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            _is_link_like(opened)
            or not stat.S_ISREG(opened.st_mode)
            or not _same_snapshot(before, opened)
            or opened.st_size > MAX_CURSOR_PACKAGE_BYTES
        ):
            raise _CursorVersionEvidenceError(
                "Cursor package metadata is invalid"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_CURSOR_PACKAGE_BYTES + 1)
        after = os.fstat(fd)
        path_after = os.lstat(path)
        if (
            len(raw) > MAX_CURSOR_PACKAGE_BYTES
            or not _same_snapshot(opened, after)
            or not _same_snapshot(after, path_after)
        ):
            raise _CursorVersionEvidenceError(
                "Cursor package metadata changed during diagnosis"
            )
        return raw
    finally:
        os.close(fd)


def _validate_no_reparse_components(
    path: Path,
    *,
    missing_ok: bool = False,
) -> bool:
    if not path.is_absolute() or not path.anchor:
        raise _CursorVersionEvidenceError("Cursor installation root is invalid")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        if _is_link_like(info) or not stat.S_ISDIR(info.st_mode):
            raise _CursorVersionEvidenceError(
                "Cursor installation path is invalid"
            )
    return True


def _validate_selected_launcher(app_root: Path) -> None:
    bin_root = app_root / "bin"
    _validate_no_reparse_components(bin_root)
    with windows_security.PinnedWindowsDirectory(bin_root) as bin_pin:
        found = False
        for launcher_name in _CURSOR_LAUNCHER_NAMES:
            launcher = bin_root / launcher_name
            try:
                info = os.lstat(launcher)
            except FileNotFoundError:
                continue
            if _is_link_like(info) or not stat.S_ISREG(info.st_mode):
                raise _CursorVersionEvidenceError(
                    "standard Cursor launcher is invalid"
                )
            found = True
        if not found:
            raise _CursorVersionEvidenceError(
                "standard Cursor launcher is missing"
            )
        bin_pin.validate()


def _inspect_installation(
    app_root: Path,
    *,
    selected: bool,
) -> Optional[client_host_version.NumericVersion]:
    if not _validate_no_reparse_components(app_root, missing_ok=True):
        return None
    with windows_security.PinnedWindowsDirectory(app_root) as root_pin:
        version = _parse_package(
            _read_regular_file_bounded(app_root / "package.json")
        )
        if selected:
            _validate_selected_launcher(app_root)
        root_pin.validate()
        return version


def _path_key(path: Path) -> str:
    return str(PureWindowsPath(os.fspath(path))).rstrip("\\").casefold()


def _deduplicate_roots(roots: Iterable[Path]) -> Tuple[Path, ...]:
    result = []
    seen = set()
    for value in roots:
        root = Path(value)
        key = _path_key(root)
        if key not in seen:
            result.append(root)
            seen.add(key)
    return tuple(result)


def inspect_cursor_version_evidence(
    candidate_app_roots: Iterable[Path],
    selected_app_root: Optional[Path],
) -> client_host_version.HostVersionEvidence:
    """Inspect explicit known roots; intended for the Windows adapter and tests."""

    roots = _deduplicate_roots(candidate_app_roots)
    complete = True
    selected_index = None
    if selected_app_root is None:
        complete = False
    else:
        matches = [
            index
            for index, root in enumerate(roots)
            if _path_key(selected_app_root) == _path_key(root)
        ]
        if len(matches) == 1:
            selected_index = matches[0]
        else:
            complete = False

    installed = []
    selected_version = None
    installations = 0
    expected_errors = (
        FileNotFoundError,
        NotADirectoryError,
        PermissionError,
        OSError,
        ValueError,
        windows_security.WindowsSecurityError,
    )
    for index, root in enumerate(roots):
        try:
            version = _inspect_installation(
                root,
                selected=index == selected_index,
            )
        except expected_errors:
            complete = False
            continue
        if version is None:
            continue
        installations += 1
        installed.append(version)
        if index == selected_index:
            selected_version = version

    normalized = tuple(sorted(set(installed)))
    if selected_index is not None and selected_version is None:
        complete = False
    return client_host_version.HostVersionEvidence(
        selected=selected_version,
        installed=normalized,
        installations=installations,
        scope_complete=complete,
    )


def _standard_cursor_app_roots() -> Tuple[Tuple[Path, ...], bool]:
    roots = []
    complete = True
    folders = (
        (_FOLDERID_USER_PROGRAM_FILES, ("cursor", "resources", "app")),
        (_FOLDERID_PROGRAM_FILES, ("cursor", "resources", "app")),
        (_FOLDERID_PROGRAM_FILES_X86, ("cursor", "resources", "app")),
    )
    for folder_id, suffix in folders:
        try:
            base = _known_folder_path(folder_id)
        except (OSError, ValueError, _CursorVersionEvidenceError):
            complete = False
            continue
        roots.append(base.joinpath(*suffix))
    return _deduplicate_roots(roots), complete


def _local_windows_absolute_path(value: Optional[str]) -> Optional[Path]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_WINDOWS_ENV_PATH_CHARS
    ):
        return None
    path = PureWindowsPath(value)
    drive = path.drive
    if (
        not path.is_absolute()
        or len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isalpha()
        or any(part in ("", ".", "..") for part in path.parts[1:])
    ):
        return None
    return Path(str(path))


def _known_folder_path(folder_id: str) -> Path:
    import ctypes

    class Guid(ctypes.Structure):
        _fields_ = (
            ("data1", ctypes.c_uint32),
            ("data2", ctypes.c_uint16),
            ("data3", ctypes.c_uint16),
            ("data4", ctypes.c_ubyte * 8),
        )

    try:
        guid = Guid.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    except (AttributeError, ValueError) as exc:
        raise _CursorVersionEvidenceError(
            "Windows known-folder identifier is invalid"
        ) from exc
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = (
        ctypes.POINTER(Guid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole32.CoTaskMemFree.restype = None
    result = ctypes.c_void_p()
    status = shell32.SHGetKnownFolderPath(
        ctypes.byref(guid),
        _KF_FLAG_DONT_VERIFY,  # return the path without touching redirected storage
        None,
        ctypes.byref(result),
    )
    try:
        if status != 0 or not result.value:
            raise _CursorVersionEvidenceError(
                "Windows known folder is unavailable"
            )
        value = ctypes.wstring_at(result.value)
    finally:
        if result.value:
            ole32.CoTaskMemFree(result)
    path = _local_windows_absolute_path(value)
    if path is None:
        raise _CursorVersionEvidenceError(
            "Windows known folder is not a local absolute path"
        )
    return path


def _selected_standard_root_from_path(
    roots: Iterable[Path],
    path_value: Optional[str],
) -> Tuple[Optional[Path], bool]:
    if (
        not isinstance(path_value, str)
        or len(path_value) > _MAX_WINDOWS_ENV_PATH_CHARS
    ):
        return None, False
    entries = path_value.split(";")
    if not entries or len(entries) > _MAX_WINDOWS_PATH_ENTRIES:
        return None, False
    approved = {
        _path_key(Path(root) / "bin"): Path(root)
        for root in roots
    }
    for raw_entry in entries:
        entry = raw_entry.strip()
        if (
            not entry
            or len(entry) > _MAX_WINDOWS_PATH_ENTRY_CHARS
            or "%" in entry
        ):
            return None, False
        if entry.startswith('"') and entry.endswith('"') and len(entry) > 2:
            entry = entry[1:-1]
        path = _local_windows_absolute_path(entry)
        if path is None:
            return None, False
        selected = approved.get(_path_key(path))
        if selected is not None:
            return selected, True
    return None, False


def collect_cursor_version_evidence() -> client_host_version.HostVersionEvidence:
    """Collect offline Windows evidence without invoking Cursor."""

    if os.name != "nt":
        return client_host_version.HostVersionEvidence(
            selected=None,
            installed=(),
            installations=0,
            scope_complete=False,
        )
    roots, roots_complete = _standard_cursor_app_roots()
    selected_root, path_complete = _selected_standard_root_from_path(
        roots,
        os.environ.get("PATH"),
    )
    evidence = inspect_cursor_version_evidence(
        roots,
        selected_root,
    )
    if roots_complete and path_complete:
        return evidence
    return client_host_version.HostVersionEvidence(
        selected=evidence.selected,
        installed=evidence.installed,
        installations=evidence.installations,
        scope_complete=False,
    )


def assess_cursor_version(
    evidence: client_host_version.HostVersionEvidence,
) -> CursorVersionAssessment:
    assessment = client_host_version.assess_version_floor(
        evidence,
        CURSOR_CANDIDATE_MINIMUM,
    )
    state = {
        "floor_met": "candidate_standard_version_floor_met",
        "unsupported": "unsupported_cursor_version",
        "unknown": "cursor_version_unknown",
    }[assessment.state]
    selected = str(evidence.selected) if evidence.selected is not None else "unknown"
    installed = (
        ",".join(str(version) for version in evidence.installed)
        if evidence.installed
        else "unknown"
    )
    detail = (
        "%s; scope=standard_windows_roots; exhaustive=false; evidence=collected; "
        "path_preferred_standard_install=%s; installed=%s; installations=%d; minimum=%s; "
        "compatibility=unverified"
        % (
            state,
            selected,
            installed,
            evidence.installations,
            CURSOR_CANDIDATE_MINIMUM_TEXT,
        )
    )
    return CursorVersionAssessment(state=state, detail=detail)
