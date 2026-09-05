"""Signed-commit owned-copy payload cache for Cursor skills.

Production callers must pass the ``VerifiedRelease`` returned by
``release_contract.verify_release_signature`` and hold
``cursor_activation.cursor_activation_lock``.  ``VerifiedRelease`` is typed
in-process evidence, not a capability against arbitrary code in this process.

Skill bytes are read directly from blob objects named by the verified commit.
The mutable worktree is never a payload source.  Publication into Cursor's
active skills directory remains a later, journaled transaction step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Optional, Tuple

from installer import (
    client_host_ownership,
    managed_install,
    release_contract,
    updater,
)
from installer.config import ShellError


PAYLOAD_SCHEMA = 1
CURSOR_M1_SKILLS = (
    "audit",
    "audit-adjudication",
    "audit-brainstorming",
    "audit-explore",
    "audit-forecast",
    "audit-market-research",
    "audit-writing-plans",
    "layer-check",
)
CURSOR_M2_SKILLS = CURSOR_M1_SKILLS + (
    "discussion-board",
    "graphic-explanation",
)
MANIFEST_FILENAME = ".decision-engine-manifest.json"
MANIFEST_KEYS = frozenset(
    {
        "schema",
        "release_id",
        "canonical_source",
        "release_manifest_sha256",
        "installed_at",
        "renderer_version",
        "skills",
    }
)
_SKILL_RECORD_KEYS = frozenset({"files", "tree_sha256"})
_FILE_RECORD_KEYS = frozenset({"git_blob_sha1", "sha256", "size"})
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID_RE = re.compile(r"^[1-9][0-9]{0,18}-[0-9a-f]{40}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_MAX_FILES = 512
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_WALK_DEPTH = 32
_MAX_RELATIVE_BYTES = 1024


class CursorSkillPayloadError(ShellError):
    """Safe-to-display refusal from Cursor owned-copy preparation."""


class CursorSkillPayloadUnverifiableError(CursorSkillPayloadError):
    """A transient/read failure that must not be labelled as user modification."""


@dataclass(frozen=True)
class SignedInventory:
    reader: object
    skills: Mapping[str, Tuple[Tuple[str, str], ...]]


@dataclass(frozen=True)
class PreparedPayload:
    root: Path
    manifest_path: Path
    source_root: Path
    verified_release: release_contract.VerifiedRelease
    release_id: str
    manifest_sha256: str
    skills: Tuple[str, ...]


@dataclass(frozen=True)
class StagedSkillPublication:
    skill: str
    staged: Path
    target: Path
    tree_sha256: str
    object_identity: Tuple[int, int, int]


def _state_root() -> Path:
    return managed_install.registration_path().parent / "decision-engine-cursor"


def _release_id(verified: release_contract.VerifiedRelease) -> str:
    manifest = verified.manifest
    return "%d-%s" % (manifest.release_sequence, manifest.commit)


def _directory_identity(path: Path) -> Tuple[int, int, int]:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise CursorSkillPayloadError(
            "Cursor skill directory identity is unavailable"
        ) from exc
    if not stat.S_ISDIR(info.st_mode) or _link_like(path, info):
        raise CursorSkillPayloadError("Cursor skill directory identity is unsafe")
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _has_nondefault_windows_stream(path: Path) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class StreamData(ctypes.Structure):
        _fields_ = [
            ("StreamSize", ctypes.c_longlong),
            ("StreamName", wintypes.WCHAR * 296),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FindFirstStreamW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(StreamData),
        wintypes.DWORD,
    ]
    kernel32.FindFirstStreamW.restype = wintypes.HANDLE
    kernel32.FindNextStreamW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(StreamData),
    ]
    kernel32.FindNextStreamW.restype = wintypes.BOOL
    kernel32.FindClose.argtypes = [wintypes.HANDLE]
    kernel32.FindClose.restype = wintypes.BOOL
    data = StreamData()
    handle = kernel32.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        code = ctypes.get_last_error()
        if code == 38:  # ERROR_HANDLE_EOF
            return False
        raise CursorSkillPayloadUnverifiableError(
            "Cursor skill alternate streams could not be inspected"
        )
    try:
        while True:
            if data.StreamName not in {"::$DATA", ""}:
                return True
            if not kernel32.FindNextStreamW(handle, ctypes.byref(data)):
                code = ctypes.get_last_error()
                if code == 38:  # ERROR_HANDLE_EOF
                    return False
                raise CursorSkillPayloadUnverifiableError(
                    "Cursor skill alternate streams changed during inspection"
                )
    finally:
        kernel32.FindClose(handle)


def _tree_has_nondefault_windows_stream(root: Path) -> bool:
    if _has_nondefault_windows_stream(root):
        return True
    try:
        for current, directories, files in os.walk(root, followlinks=False):
            for name in tuple(directories) + tuple(files):
                if _has_nondefault_windows_stream(Path(current) / name):
                    return True
    except OSError as exc:
        raise CursorSkillPayloadUnverifiableError(
            "Cursor skill alternate streams could not be inspected"
        ) from exc
    return False


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _manifest_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _link_like(path: Path, info: Optional[os.stat_result] = None) -> bool:
    if managed_install._is_link_like(path):
        return True
    if info is None:
        try:
            info = os.lstat(path)
        except OSError:
            return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validated_relative_file(value: str, *, skill: str) -> Tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_RELATIVE_BYTES
        or "\\" in value
    ):
        raise CursorSkillPayloadError("Cursor skill inventory path is invalid")
    try:
        parts = updater._validate_relative_git_path(
            "skills/%s/%s" % (skill, value),
            portable=True,
        )
    except updater.UpdateInspectionError as exc:
        raise CursorSkillPayloadError("Cursor skill inventory path is invalid") from exc
    if len(parts) < 3 or parts[0] != "skills" or parts[1] != skill:
        raise CursorSkillPayloadError("Cursor skill inventory path is invalid")
    return tuple(parts[2:])


def _validate_inventory(
    inventory: Mapping[str, Tuple[Tuple[str, str], ...]],
) -> Dict[str, Tuple[Tuple[str, Tuple[str, ...], str], ...]]:
    if (
        not isinstance(inventory, Mapping)
        or set(inventory) != set(CURSOR_M2_SKILLS)
        or len(inventory) != len(CURSOR_M2_SKILLS)
    ):
        raise CursorSkillPayloadError("Cursor skill inventory does not match the M2 allowlist")
    validated = {}
    total = 0
    for skill in CURSOR_M2_SKILLS:
        values = inventory.get(skill)
        if not isinstance(values, tuple) or not values:
            raise CursorSkillPayloadError("Cursor skill inventory is incomplete")
        records = []
        seen = set()
        for item in values:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[1], str)
                or not _SHA1_RE.fullmatch(item[1])
            ):
                raise CursorSkillPayloadError("Cursor skill inventory record is invalid")
            relative, object_id = item
            parts = _validated_relative_file(relative, skill=skill)
            canonical = "/".join(parts)
            if canonical != relative or canonical in seen:
                raise CursorSkillPayloadError("Cursor skill inventory path is invalid")
            seen.add(canonical)
            records.append((canonical, parts, object_id))
            total += 1
            if total > _MAX_FILES:
                raise CursorSkillPayloadError("Cursor skill inventory exceeds the file limit")
        if "SKILL.md" not in seen:
            raise CursorSkillPayloadError("Cursor skill inventory is incomplete")
        validated[skill] = tuple(sorted(records))
    return validated


def _verified_source_inventory(
    managed_root: Path,
    verified: release_contract.VerifiedRelease,
) -> SignedInventory:
    """Read the exact allowlisted path/blob mapping from the verified commit."""

    try:
        reader = updater._GitReader(managed_root)
        _code, head_output = reader.run("head")
        if updater._single_commit(head_output, "Cursor skill source HEAD") != verified.manifest.commit:
            raise CursorSkillPayloadError(
                "Cursor skill source HEAD does not match the verified release"
            )
        _code, tree_output = reader.run(
            "target_tree",
            commit=verified.manifest.commit,
        )
        inventory: Dict[str, list[Tuple[str, str]]] = {
            skill: [] for skill in CURSOR_M2_SKILLS
        }
        for entry in updater._parse_tree_entries(tree_output):
            parts = entry.path.split("/")
            if len(parts) < 3 or parts[0] != "skills" or parts[1] not in inventory:
                continue
            if entry.mode not in {"100644", "100755"}:
                raise CursorSkillPayloadError(
                    "Cursor skill commit contains a non-regular entry"
                )
            inventory[parts[1]].append(("/".join(parts[2:]), entry.object_id))
    except CursorSkillPayloadError:
        raise
    except updater.UpdateInspectionError as exc:
        raise CursorSkillPayloadError(
            "Cursor skill commit inventory could not be verified"
        ) from exc
    return SignedInventory(
        reader=reader,
        skills={
            skill: tuple(sorted(files))
            for skill, files in inventory.items()
        },
    )


def _read_verified_blob(reader: object, object_id: str) -> bytes:
    """Read one bounded blob through the updater's trusted Git executable."""

    if (
        not isinstance(reader, updater._GitReader)
        or not isinstance(object_id, str)
        or not _SHA1_RE.fullmatch(object_id)
    ):
        raise CursorSkillPayloadError("Cursor skill Git blob request is invalid")
    if updater._git_executable_generation(
        reader.git_executable,
        deadline=reader.deadline,
    ) != reader.git_generation:
        raise CursorSkillPayloadError("trusted Git changed during skill preparation")
    argv = (
        reader.git_executable,
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=0",
        "-c",
        "trace2.normalTarget=",
        "-c",
        "trace2.perfTarget=",
        "-c",
        "trace2.eventTarget=",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=" + os.devnull,
        "-c",
        "core.excludesFile=" + os.devnull,
        "-c",
        "core.untrackedCache=false",
        "-c",
        "submodule.recurse=false",
        "-C",
        str(reader.root),
        "cat-file",
        "blob",
        object_id,
    )
    remaining = reader.deadline - time.monotonic()
    if remaining <= 0:
        raise CursorSkillPayloadError("Cursor skill Git read exceeded its deadline")
    try:
        returncode, raw = updater._run_bounded_git(
            argv,
            reader.environment,
            timeout_seconds=min(10.0, remaining),
        )
    except updater.UpdateInspectionError as exc:
        raise CursorSkillPayloadError("Cursor skill Git blob could not be read") from exc
    if returncode != 0 or len(raw) > _MAX_FILE_BYTES:
        raise CursorSkillPayloadError("Cursor skill Git blob is invalid or too large")
    if updater._git_executable_generation(
        reader.git_executable,
        deadline=reader.deadline,
    ) != reader.git_generation:
        raise CursorSkillPayloadError("trusted Git changed during skill preparation")
    return raw


def _write_blob(destination: Path, raw: bytes) -> str:
    if not isinstance(raw, bytes) or len(raw) > _MAX_FILE_BYTES:
        raise CursorSkillPayloadError("Cursor skill blob exceeds the size limit")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(destination, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CursorSkillPayloadError("Cursor skill blob could not be staged") from exc
    return hashlib.sha256(raw).hexdigest()


def _stable_read(path: Path, *, maximum: int, label: str) -> Tuple[bytes, int]:
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or _link_like(path, before)
            or before.st_nlink != 1
            or before.st_size > maximum
        ):
            raise CursorSkillPayloadError("%s must be a bounded regular file" % label)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            raw = handle.read(maximum + 1)
            opened_after = os.fstat(handle.fileno())
        after = os.lstat(path)
    except CursorSkillPayloadError:
        raise
    except OSError as exc:
        raise CursorSkillPayloadUnverifiableError(
            "%s is unreadable" % label
        ) from exc
    identity = lambda info: (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        getattr(info, "st_mtime_ns", None),
        getattr(info, "st_file_attributes", 0),
    )
    if len(raw) > maximum or not (
        identity(before)
        == identity(opened)
        == identity(opened_after)
        == identity(after)
    ):
        raise CursorSkillPayloadUnverifiableError(
            "%s changed while being read" % label
        )
    return raw, opened.st_size


def _skill_tree_digest(files: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(files)).hexdigest()


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CursorSkillPayloadError("cached payload manifest has a duplicate key")
        value[key] = item
    return value


def _load_manifest(path: Path) -> Dict[str, object]:
    try:
        raw, _size = _stable_read(
            path,
            maximum=_MAX_MANIFEST_BYTES,
            label="cached payload manifest",
        )
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except CursorSkillPayloadError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CursorSkillPayloadError("cached payload manifest is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != MANIFEST_KEYS:
        raise CursorSkillPayloadError("cached payload manifest schema is invalid")
    return payload


def _validated_manifest_records(
    skills: object,
) -> Dict[str, Tuple[Tuple[str, Tuple[str, ...], Dict[str, object]], ...]]:
    if (
        not isinstance(skills, dict)
        or set(skills) != set(CURSOR_M2_SKILLS)
        or len(skills) != len(CURSOR_M2_SKILLS)
    ):
        raise CursorSkillPayloadError("cached payload skills do not match the M2 allowlist")
    records = {}
    total = 0
    for skill in CURSOR_M2_SKILLS:
        record = skills.get(skill)
        if not isinstance(record, dict) or set(record) != _SKILL_RECORD_KEYS:
            raise CursorSkillPayloadError("cached payload skill record is invalid")
        files = record.get("files")
        if not isinstance(files, dict) or not files or "SKILL.md" not in files:
            raise CursorSkillPayloadError("cached payload skill record is incomplete")
        entries = []
        for relative, file_record in files.items():
            parts = _validated_relative_file(relative, skill=skill)
            if "/".join(parts) != relative or not isinstance(file_record, dict):
                raise CursorSkillPayloadError("cached payload file record is invalid")
            if set(file_record) != _FILE_RECORD_KEYS:
                raise CursorSkillPayloadError("cached payload file record is invalid")
            if (
                not isinstance(file_record.get("git_blob_sha1"), str)
                or not _SHA1_RE.fullmatch(file_record["git_blob_sha1"])
                or not isinstance(file_record.get("sha256"), str)
                or not _SHA256_RE.fullmatch(file_record["sha256"])
                or type(file_record.get("size")) is not int
                or not 0 <= file_record["size"] <= _MAX_FILE_BYTES
            ):
                raise CursorSkillPayloadError("cached payload file record is invalid")
            entries.append((relative, parts, file_record))
            total += 1
            if total > _MAX_FILES:
                raise CursorSkillPayloadError("cached payload exceeds the file limit")
        if record.get("tree_sha256") != _skill_tree_digest(files):
            raise CursorSkillPayloadError("cached payload skill tree hash does not match")
        records[skill] = tuple(sorted(entries))
    return records


def _bounded_tree_paths(
    root: Path,
    *,
    expected_files: set[str],
    expected_directories: set[str],
) -> None:
    stack = [(root, "", 0)]
    visited = 0
    while stack:
        directory, prefix, depth = stack.pop()
        if depth > _MAX_WALK_DEPTH:
            raise CursorSkillPayloadError("cached payload tree exceeds the depth limit")
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise CursorSkillPayloadUnverifiableError(
                "cached payload could not be enumerated"
            ) from exc
        for entry in entries:
            visited += 1
            if visited > _MAX_FILES * 2 + len(CURSOR_M2_SKILLS) + 1:
                raise CursorSkillPayloadError("cached payload tree exceeds the entry limit")
            relative = entry.name if not prefix else prefix + "/" + entry.name
            if len(relative.encode("utf-8")) > _MAX_RELATIVE_BYTES:
                raise CursorSkillPayloadError("cached payload path exceeds the size limit")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise CursorSkillPayloadUnverifiableError(
                    "cached payload entry is unreadable"
                ) from exc
            path = Path(entry.path)
            if _link_like(path, info):
                raise CursorSkillPayloadError("cached payload contains an unsafe link")
            if stat.S_ISREG(info.st_mode):
                if relative not in expected_files:
                    raise CursorSkillPayloadError("cached payload contains unexpected files")
            elif stat.S_ISDIR(info.st_mode):
                if relative not in expected_directories:
                    raise CursorSkillPayloadError("cached payload contains unexpected files")
                stack.append((path, relative, depth + 1))
            else:
                raise CursorSkillPayloadError("cached payload contains a special file")


def verify_cached_payload(
    root: Path,
    *,
    expected_release: Optional[release_contract.VerifiedRelease] = None,
    managed_root: Optional[Path] = None,
    expected_signed_inventory: Optional[SignedInventory] = None,
) -> str:
    root = Path(root)
    if _link_like(root) or not root.is_dir():
        raise CursorSkillPayloadError("cached payload root is unsafe")
    payload = _load_manifest(root / MANIFEST_FILENAME)
    if (
        type(payload.get("schema")) is not int
        or payload.get("schema") != PAYLOAD_SCHEMA
        or payload.get("renderer_version") != "none"
        or not isinstance(payload.get("canonical_source"), str)
        or not Path(payload["canonical_source"]).is_absolute()
        or not isinstance(payload.get("release_id"), str)
        or not _RELEASE_ID_RE.fullmatch(payload["release_id"])
        or not isinstance(payload.get("release_manifest_sha256"), str)
        or not _SHA256_RE.fullmatch(payload["release_manifest_sha256"])
        or not isinstance(payload.get("installed_at"), str)
        or not _UTC_RE.fullmatch(payload["installed_at"])
    ):
        raise CursorSkillPayloadError("cached payload manifest schema is invalid")
    try:
        datetime.strptime(payload["installed_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise CursorSkillPayloadError("cached payload manifest time is invalid") from exc
    if expected_release is not None:
        if (
            not isinstance(expected_release, release_contract.VerifiedRelease)
            or managed_root is None
        ):
            raise CursorSkillPayloadError("expected release binding is incomplete")
        if (
            payload.get("release_id") != _release_id(expected_release)
            or payload.get("release_manifest_sha256")
            != release_contract.manifest_sha256(expected_release.manifest)
            or payload.get("canonical_source")
            != str(Path(managed_root).resolve(strict=True) / "skills")
        ):
            raise CursorSkillPayloadError(
                "cached payload does not match the verified release"
            )
        if expected_signed_inventory is None:
            expected_signed_inventory = _verified_source_inventory(
                Path(managed_root),
                expected_release,
            )
    validated_inventory = (
        _validate_inventory(expected_signed_inventory.skills)
        if expected_signed_inventory is not None
        else None
    )
    records = _validated_manifest_records(payload.get("skills"))
    expected_files = {MANIFEST_FILENAME}
    expected_directories = set(CURSOR_M2_SKILLS)
    total = 0
    for skill in CURSOR_M2_SKILLS:
        expected_by_path = (
            {
                relative: object_id
                for relative, _parts, object_id in validated_inventory[skill]
            }
            if validated_inventory is not None
            else None
        )
        for relative, parts, file_record in records[skill]:
            if (
                expected_by_path is not None
                and expected_by_path.get(relative) != file_record["git_blob_sha1"]
            ):
                raise CursorSkillPayloadError(
                    "cached payload Git inventory does not match the verified release"
                )
            path = root / skill
            for part in parts:
                path = path / part
            raw, size = _stable_read(
                path,
                maximum=_MAX_FILE_BYTES,
                label="cached payload file",
            )
            if (
                size != file_record["size"]
                or hashlib.sha256(raw).hexdigest() != file_record["sha256"]
            ):
                raise CursorSkillPayloadError("cached payload file hash does not match")
            if expected_by_path is not None:
                trusted = _read_verified_blob(
                    expected_signed_inventory.reader,
                    expected_by_path[relative],
                )
                if (
                    len(trusted) != size
                    or hashlib.sha256(trusted).hexdigest()
                    != file_record["sha256"]
                ):
                    raise CursorSkillPayloadError(
                        "cached payload bytes do not match the verified Git blob"
                    )
            total += size
            if total > _MAX_TOTAL_BYTES:
                raise CursorSkillPayloadError("cached payload exceeds bounded limits")
            expected_files.add("%s/%s" % (skill, relative))
            parent = Path(skill) / Path(relative).parent
            while parent.as_posix() not in {".", skill}:
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        if expected_by_path is not None and set(expected_by_path) != {
            relative for relative, _parts, _record in records[skill]
        }:
            raise CursorSkillPayloadError(
                "cached payload Git inventory does not match the verified release"
            )
    _bounded_tree_paths(
        root,
        expected_files=expected_files,
        expected_directories=expected_directories,
    )
    return _manifest_digest(payload)


def verify_active_skills_for_release(
    managed_root: Path,
    verified_release: release_contract.VerifiedRelease,
    destination: Path,
) -> str:
    """Verify the exact active skill trees directly against signed Git blobs."""

    if not isinstance(verified_release, release_contract.VerifiedRelease):
        raise CursorSkillPayloadError(
            "Cursor active skills require a verified release"
        )
    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor active skill verification requires the activation lock"
        )
    try:
        managed_root = Path(managed_root).resolve(strict=True)
    except OSError as exc:
        raise CursorSkillPayloadError(
            "Cursor active skill source is unavailable"
        ) from exc
    signed = _verified_source_inventory(managed_root, verified_release)
    inventory = _validate_inventory(signed.skills)
    destination = Path(destination)
    if _link_like(destination) or not destination.is_dir():
        raise CursorSkillPayloadError(
            "Cursor active skills destination is unsafe"
        )
    total = 0
    for skill in CURSOR_M2_SKILLS:
        root = destination / skill
        if (
            _link_like(root)
            or not root.is_dir()
            or _tree_has_nondefault_windows_stream(root)
        ):
            raise CursorSkillPayloadError(
                "Cursor active skill root is unsafe"
            )
        expected_files = set()
        expected_directories = set()
        for relative, parts, object_id in inventory[skill]:
            trusted = _read_verified_blob(signed.reader, object_id)
            path = root
            for part in parts:
                path = path / part
            active, size = _stable_read(
                path,
                maximum=_MAX_FILE_BYTES,
                label="Cursor active skill file",
            )
            if size != len(trusted) or active != trusted:
                raise CursorSkillPayloadError(
                    "Cursor active skill file does not match the verified release"
                )
            total += size
            if total > _MAX_TOTAL_BYTES:
                raise CursorSkillPayloadError(
                    "Cursor active skills exceed bounded limits"
                )
            expected_files.add(relative)
            parent = Path(relative).parent
            while parent.as_posix() != ".":
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        _bounded_tree_paths(
            root,
            expected_files=expected_files,
            expected_directories=expected_directories,
        )
    return _release_id(verified_release)


def _safe_remove_staging(transaction_root: Path, staging_root: Path) -> None:
    try:
        if (
            transaction_root.parent != staging_root
            or _link_like(transaction_root)
            or not transaction_root.is_dir()
        ):
            return
        for directory, directories, files in os.walk(
            transaction_root,
            topdown=True,
            followlinks=False,
        ):
            for name in (*directories, *files):
                if _link_like(Path(directory) / name):
                    return
        shutil.rmtree(transaction_root)
    except OSError:
        return


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise CursorSkillPayloadError("Cursor skill directory could not be flushed") from exc


def _fsync_payload_directories(root: Path) -> None:
    if os.name == "nt":
        return
    directories = []
    for directory, children, _files in os.walk(
        root,
        topdown=False,
        followlinks=False,
    ):
        directories.append(Path(directory))
        for child in children:
            if _link_like(Path(directory) / child):
                raise CursorSkillPayloadError(
                    "Cursor skill staging tree contains an unsafe link"
                )
    for directory in directories:
        _fsync_directory(directory)


def prepare_verified_payload(
    managed_root: Path,
    verified_release: release_contract.VerifiedRelease,
    *,
    transaction_id: str,
    installed_at: str,
    state_root: Optional[Path] = None,
) -> PreparedPayload:
    if not isinstance(verified_release, release_contract.VerifiedRelease):
        raise CursorSkillPayloadError("Cursor skills require a verified release")
    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor skill preparation requires the activation lock"
        )
    try:
        transaction_id = str(uuid.UUID(transaction_id))
    except (ValueError, AttributeError) as exc:
        raise CursorSkillPayloadError("Cursor skill transaction ID is invalid") from exc
    if not isinstance(installed_at, str) or not _UTC_RE.fullmatch(installed_at):
        raise CursorSkillPayloadError("Cursor skill installation time is invalid")
    try:
        datetime.strptime(installed_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise CursorSkillPayloadError("Cursor skill installation time is invalid") from exc
    managed_root = Path(managed_root).resolve(strict=True)
    signed = _verified_source_inventory(managed_root, verified_release)
    inventory = _validate_inventory(signed.skills)
    state = Path(state_root) if state_root is not None else _state_root()
    try:
        managed_install._ensure_private_registration_directory(state)
        payloads = state / "skill-payloads"
        managed_install._ensure_private_registration_directory(payloads)
        staging_base = state / "staging"
        managed_install._ensure_private_registration_directory(staging_base)
        transaction_root = staging_base / transaction_id
        managed_install._ensure_private_registration_directory(transaction_root)
    except managed_install.ManagedInstallError as exc:
        raise CursorSkillPayloadError("Cursor skill state root is unsafe") from exc
    release_id = _release_id(verified_release)
    final = payloads / release_id
    if final.exists() or _link_like(final):
        try:
            digest = verify_cached_payload(
                final,
                expected_release=verified_release,
                managed_root=managed_root,
                expected_signed_inventory=signed,
            )
        except CursorSkillPayloadError as exc:
            _safe_remove_staging(transaction_root, staging_base)
            raise CursorSkillPayloadError(
                "existing cached payload is invalid and was not overwritten"
            ) from exc
        try:
            transaction_root.rmdir()
        except OSError:
            pass
        return PreparedPayload(
            final,
            final / MANIFEST_FILENAME,
            managed_root,
            verified_release,
            release_id,
            digest,
            CURSOR_M2_SKILLS,
        )
    staging = transaction_root / "skills"
    if staging.exists() or _link_like(staging):
        raise CursorSkillPayloadError("Cursor skill staging root already exists")
    try:
        staging.mkdir(mode=0o700)
        records = {}
        remaining = _MAX_TOTAL_BYTES
        for skill in CURSOR_M2_SKILLS:
            target_skill = staging / skill
            target_skill.mkdir(mode=0o700)
            file_records = {}
            for relative, parts, object_id in inventory[skill]:
                raw = _read_verified_blob(signed.reader, object_id)
                if len(raw) > remaining:
                    raise CursorSkillPayloadError(
                        "Cursor skill payload exceeds the total size limit"
                    )
                target_parent = target_skill
                for part in parts[:-1]:
                    target_parent = target_parent / part
                    target_parent.mkdir(mode=0o700, exist_ok=True)
                digest = _write_blob(target_parent / parts[-1], raw)
                file_records[relative] = {
                    "git_blob_sha1": object_id,
                    "sha256": digest,
                    "size": len(raw),
                }
                remaining -= len(raw)
            records[skill] = {
                "files": file_records,
                "tree_sha256": _skill_tree_digest(file_records),
            }
        payload = {
            "schema": PAYLOAD_SCHEMA,
            "release_id": release_id,
            "canonical_source": str(managed_root / "skills"),
            "release_manifest_sha256": release_contract.manifest_sha256(
                verified_release.manifest
            ),
            "installed_at": installed_at,
            "renderer_version": "none",
            "skills": records,
        }
        rendered = _canonical_json(payload)
        if len(rendered) > _MAX_MANIFEST_BYTES:
            raise CursorSkillPayloadError("Cursor skill manifest exceeds the size limit")
        _write_blob(staging / MANIFEST_FILENAME, rendered)
        digest = verify_cached_payload(
            staging,
            expected_release=verified_release,
            managed_root=managed_root,
            expected_signed_inventory=signed,
        )
        if final.exists() or _link_like(final):
            raise CursorSkillPayloadError(
                "Cursor skill cache appeared while being published"
            )
        _fsync_payload_directories(staging)
        from installer import windows_security

        windows_security.move_write_through(
            staging,
            final,
            replace_existing=False,
        )
        _fsync_directory(payloads)
        try:
            transaction_root.rmdir()
        except OSError:
            pass
    except CursorSkillPayloadError:
        _safe_remove_staging(transaction_root, staging_base)
        raise
    except (OSError, RuntimeError) as exc:
        _safe_remove_staging(transaction_root, staging_base)
        raise CursorSkillPayloadError("Cursor skill payload could not be published") from exc
    return PreparedPayload(
        final,
        final / MANIFEST_FILENAME,
        managed_root,
        verified_release,
        release_id,
        digest,
        CURSOR_M2_SKILLS,
    )


def preflight_initial_publication(
    prepared: PreparedPayload,
    destination: Path,
) -> Tuple[Tuple[str, Path, Path], ...]:
    """Diagnostic preflight; the later publisher must recheck every target."""

    if not isinstance(prepared, PreparedPayload):
        raise CursorSkillPayloadError("Cursor prepared payload is invalid")
    if verify_cached_payload(
        prepared.root,
        expected_release=prepared.verified_release,
        managed_root=prepared.source_root,
    ) != prepared.manifest_sha256:
        raise CursorSkillPayloadError("Cursor prepared payload changed before publication")
    destination = Path(destination)
    if _link_like(destination) or (destination.exists() and not destination.is_dir()):
        raise CursorSkillPayloadError("Cursor skills destination is unsafe")
    targets = []
    for skill in CURSOR_M2_SKILLS:
        target = destination / skill
        if target.exists() or _link_like(target):
            raise CursorSkillPayloadError(
                "refusing to overwrite pre-existing Cursor skill %s" % target
            )
        targets.append((skill, prepared.root / skill, target))
    return tuple(targets)


def _manifest_skill_record(
    cache_root: Path,
    skill: str,
    *,
    expected_manifest_sha256: str,
) -> Tuple[
    Dict[str, object],
    Tuple[Tuple[str, Tuple[str, ...], Dict[str, object]], ...],
]:
    if skill not in CURSOR_M2_SKILLS:
        raise CursorSkillPayloadError("Cursor managed skill name is invalid")
    if not isinstance(expected_manifest_sha256, str) or not _SHA256_RE.fullmatch(
        expected_manifest_sha256
    ):
        raise CursorSkillPayloadError("Cursor cached payload manifest changed")
    payload = _load_manifest(Path(cache_root) / MANIFEST_FILENAME)
    if _manifest_digest(payload) != expected_manifest_sha256:
        raise CursorSkillPayloadError("Cursor cached payload manifest changed")
    records = _validated_manifest_records(payload.get("skills"))
    record = payload["skills"][skill]
    if not isinstance(record, dict) or not records.get(skill):
        raise CursorSkillPayloadError("Cursor cached skill record is invalid")
    return record, records[skill]


def verify_skill_copy(
    cache_root: Path,
    skill_root: Path,
    skill: str,
    *,
    expected_manifest_sha256: str,
) -> str:
    """Verify one active/staged skill against the immutable cache manifest."""

    record, records = _manifest_skill_record(
        Path(cache_root),
        skill,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    root = Path(skill_root)
    if _link_like(root) or not root.is_dir():
        raise CursorSkillPayloadError("Cursor managed skill root is unsafe")
    files = record["files"]
    expected_files = set()
    expected_directories = set()
    total = 0
    for relative, parts, file_record in records:
        path = root
        for part in parts:
            path = path / part
        raw, size = _stable_read(
            path,
            maximum=_MAX_FILE_BYTES,
            label="Cursor managed skill file",
        )
        if (
            size != file_record["size"]
            or hashlib.sha256(raw).hexdigest() != file_record["sha256"]
        ):
            raise CursorSkillPayloadError("Cursor managed skill file hash does not match")
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise CursorSkillPayloadError("Cursor managed skill exceeds bounded limits")
        expected_files.add(relative)
        parent = Path(relative).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    _bounded_tree_paths(
        root,
        expected_files=expected_files,
        expected_directories=expected_directories,
    )
    if record.get("tree_sha256") != _skill_tree_digest(files):
        raise CursorSkillPayloadError("Cursor managed skill tree hash does not match")
    return str(record["tree_sha256"])


def stage_initial_publication(
    prepared: PreparedPayload,
    destination: Path,
    *,
    transaction_id: str,
    replace_owned_from: Optional[Path] = None,
    replace_owned_manifest_sha256: Optional[str] = None,
) -> Tuple[StagedSkillPublication, ...]:
    """Copy a verified cache into the fixed journal staging root.

    The default remains first-publication/no-replace.  Owned update callers may
    supply the exact previous signed cache and manifest; every active target is
    then verified against that cache before any staging path is created.
    """

    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor skill staging requires the activation lock"
        )
    if not isinstance(prepared, PreparedPayload):
        raise CursorSkillPayloadError("Cursor prepared payload is invalid")
    try:
        transaction_id = str(uuid.UUID(transaction_id))
    except (ValueError, AttributeError) as exc:
        raise CursorSkillPayloadError("Cursor skill transaction ID is invalid") from exc
    if verify_cached_payload(
        prepared.root,
        expected_release=prepared.verified_release,
        managed_root=prepared.source_root,
    ) != prepared.manifest_sha256:
        raise CursorSkillPayloadError("Cursor prepared payload changed before staging")
    destination = Path(
        os.path.abspath(os.path.normpath(str(Path(destination).expanduser())))
    )
    try:
        managed_install._reject_link_components(destination)
        if _link_like(destination):
            raise CursorSkillPayloadError("Cursor skills destination is unsafe")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    except CursorSkillPayloadError:
        raise
    except (OSError, managed_install.ManagedInstallError) as exc:
        raise CursorSkillPayloadError("Cursor skills destination is unsafe") from exc
    if not destination.is_dir() or _link_like(destination):
        raise CursorSkillPayloadError("Cursor skills destination is unsafe")
    if replace_owned_from is None:
        if replace_owned_manifest_sha256 is not None:
            raise CursorSkillPayloadError(
                "Cursor owned replacement binding is incomplete"
            )
        preflight_initial_publication(prepared, destination)
    else:
        if (
            not isinstance(replace_owned_manifest_sha256, str)
            or not _SHA256_RE.fullmatch(replace_owned_manifest_sha256)
        ):
            raise CursorSkillPayloadError(
                "Cursor owned replacement binding is incomplete"
            )
        previous = Path(replace_owned_from)
        if verify_cached_payload(previous) != replace_owned_manifest_sha256:
            raise CursorSkillPayloadError(
                "Cursor previous cached payload changed before staging"
            )
        for skill in CURSOR_M2_SKILLS:
            target = destination / skill
            try:
                verify_skill_copy(
                    previous,
                    target,
                    skill,
                    expected_manifest_sha256=replace_owned_manifest_sha256,
                )
                if _tree_has_nondefault_windows_stream(target):
                    raise CursorSkillPayloadError(
                        "Cursor active skill contains alternate streams"
                    )
            except CursorSkillPayloadError as exc:
                raise CursorSkillPayloadError(
                    "user_modified: Cursor active skill is not exact owned content"
                ) from exc

    state = prepared.root.parent.parent
    staging_base = state / "staging"
    transaction_root = staging_base / transaction_id
    staging = transaction_root / "skills"
    try:
        managed_install._ensure_private_registration_directory(staging_base)
        managed_install._ensure_private_registration_directory(transaction_root)
        if staging.exists() or _link_like(staging):
            raise CursorSkillPayloadError(
                "Cursor skill transaction staging already exists"
            )
        staging.mkdir(mode=0o700)
        if os.stat(staging).st_dev != os.stat(destination).st_dev:
            raise CursorSkillPayloadError(
                "Cursor skills destination is on a different filesystem"
            )
        payload = _load_manifest(prepared.manifest_path)
        records = _validated_manifest_records(payload.get("skills"))
        publications = []
        for skill in CURSOR_M2_SKILLS:
            staged_skill = staging / skill
            staged_skill.mkdir(mode=0o700)
            for _relative, parts, _file_record in records[skill]:
                source = prepared.root / skill
                target_parent = staged_skill
                for part in parts[:-1]:
                    source = source / part
                    target_parent = target_parent / part
                    target_parent.mkdir(mode=0o700, exist_ok=True)
                source = source / parts[-1]
                raw, _size = _stable_read(
                    source,
                    maximum=_MAX_FILE_BYTES,
                    label="cached payload file",
                )
                _write_blob(target_parent / parts[-1], raw)
            tree = verify_skill_copy(
                prepared.root,
                staged_skill,
                skill,
                expected_manifest_sha256=prepared.manifest_sha256,
            )
            publications.append(
                StagedSkillPublication(
                    skill=skill,
                    staged=staged_skill,
                    target=destination / skill,
                    tree_sha256=tree,
                    object_identity=_directory_identity(staged_skill),
                )
            )
        _fsync_payload_directories(staging)
        return tuple(publications)
    except CursorSkillPayloadError:
        _safe_remove_staging(transaction_root, staging_base)
        raise
    except (OSError, RuntimeError, managed_install.ManagedInstallError) as exc:
        _safe_remove_staging(transaction_root, staging_base)
        raise CursorSkillPayloadError(
            "Cursor skill transaction could not be staged"
        ) from exc


def initial_skill_state(
    cache_root: Path,
    target: Path,
    skill: str,
    *,
    expected_manifest_sha256: str,
    expected_object_identity: Tuple[int, int, int],
) -> str:
    """Return ``pre``, ``target``, or ``third`` for an initial-install skill."""

    target = Path(target)
    if not target.exists() and not _link_like(target):
        return "pre"
    try:
        if _directory_identity(target) != expected_object_identity:
            return "third"
        verify_skill_copy(
            cache_root,
            target,
            skill,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        if _tree_has_nondefault_windows_stream(target):
            return "third"
    except CursorSkillPayloadError:
        return "third"
    return "target"


def owned_skill_state(
    cache_root: Path,
    target: Path,
    skill: str,
    *,
    expected_manifest_sha256: str,
    expected_object_identity: Optional[Tuple[int, int, int]] = None,
) -> str:
    """Return ``missing``, ``owned``, or ``third`` for an owned skill tree."""

    target = Path(target)
    if not target.exists() and not _link_like(target):
        return "missing"
    try:
        managed_install._reject_link_components(target)
        if (
            expected_object_identity is not None
            and _directory_identity(target) != expected_object_identity
        ):
            return "third"
        verify_skill_copy(
            cache_root,
            target,
            skill,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        if _tree_has_nondefault_windows_stream(target):
            return "third"
    except (CursorSkillPayloadError, managed_install.ManagedInstallError):
        return "third"
    return "owned"


def quarantine_owned_skill(
    cache_root: Path,
    target: Path,
    skill: str,
    *,
    expected_manifest_sha256: str,
    expected_object_identity: Tuple[int, int, int],
    quarantine_path: Path,
) -> None:
    """Move one exact owned tree to a transaction-bounded quarantine."""

    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor skill quarantine requires the activation lock"
        )
    target = Path(target)
    quarantine = Path(quarantine_path)
    try:
        managed_install._reject_link_components(target)
        managed_install._reject_link_components(quarantine.parent)
    except managed_install.ManagedInstallError as exc:
        raise CursorSkillPayloadError(
            "repair_required: Cursor skill quarantine path is unsafe"
        ) from exc
    if quarantine.exists() or _link_like(quarantine):
        if target.exists() or _link_like(target):
            raise CursorSkillPayloadError(
                "repair_required: Cursor skill has two live generations"
            )
    else:
        if owned_skill_state(
            cache_root,
            target,
            skill,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_object_identity=expected_object_identity,
        ) != "owned":
            raise CursorSkillPayloadError(
                "user_modified: Cursor active skill is not exact owned content"
            )
        try:
            quarantine.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            from installer import windows_security

            windows_security.move_write_through(
                target,
                quarantine,
                replace_existing=False,
            )
            _fsync_directory(target.parent)
        except (OSError, RuntimeError) as exc:
            raise CursorSkillPayloadError(
                "Cursor owned skill could not be quarantined"
            ) from exc
    if owned_skill_state(
        cache_root,
        quarantine,
        skill,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_object_identity=expected_object_identity,
    ) != "owned":
        raise CursorSkillPayloadError(
            "repair_required: Cursor quarantined skill changed"
        )


def restore_quarantined_skill(
    cache_root: Path,
    target: Path,
    skill: str,
    *,
    expected_manifest_sha256: str,
    expected_object_identity: Tuple[int, int, int],
    quarantine_path: Path,
) -> None:
    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor skill restore requires the activation lock"
        )
    target = Path(target)
    quarantine = Path(quarantine_path)
    try:
        managed_install._reject_link_components(target)
        managed_install._reject_link_components(quarantine)
    except managed_install.ManagedInstallError as exc:
        raise CursorSkillPayloadError(
            "repair_required: Cursor skill restore path is unsafe"
        ) from exc
    state = owned_skill_state(
        cache_root,
        target,
        skill,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_object_identity=expected_object_identity,
    )
    if state == "owned" and not (quarantine.exists() or _link_like(quarantine)):
        return
    if state != "missing" or owned_skill_state(
        cache_root,
        quarantine,
        skill,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_object_identity=expected_object_identity,
    ) != "owned":
        raise CursorSkillPayloadError(
            "repair_required: Cursor skill cannot be restored safely"
        )
    try:
        from installer import windows_security

        windows_security.move_write_through(
            quarantine,
            target,
            replace_existing=False,
        )
        _fsync_directory(target.parent)
    except (OSError, RuntimeError) as exc:
        raise CursorSkillPayloadError(
            "repair_required: Cursor skill restore failed"
        ) from exc


def delete_quarantined_skill(
    cache_root: Path,
    quarantine_path: Path,
    skill: str,
    *,
    expected_manifest_sha256: str,
    expected_object_identity: Tuple[int, int, int],
) -> None:
    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor skill cleanup requires the activation lock"
        )
    quarantine = Path(quarantine_path)
    try:
        managed_install._reject_link_components(quarantine)
    except managed_install.ManagedInstallError as exc:
        raise CursorSkillPayloadError(
            "repair_required: Cursor skill cleanup path is unsafe"
        ) from exc
    if not quarantine.exists() and not _link_like(quarantine):
        return
    if owned_skill_state(
        cache_root,
        quarantine,
        skill,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_object_identity=expected_object_identity,
    ) != "owned":
        raise CursorSkillPayloadError(
            "repair_required: Cursor quarantined skill changed"
        )
    try:
        shutil.rmtree(quarantine)
        _fsync_directory(quarantine.parent)
    except OSError as exc:
        raise CursorSkillPayloadError(
            "Cursor quarantined skill could not be removed"
        ) from exc


def cleanup_committed_payloads(
    state_root: Path,
    *,
    keep_release_ids: FrozenSet[str],
) -> int:
    """Retain up to two named caches and remove only verified older caches."""

    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor payload cleanup requires the activation lock"
        )
    if (
        not isinstance(keep_release_ids, frozenset)
        or len(keep_release_ids) > 2
        or any(not _RELEASE_ID_RE.fullmatch(value) for value in keep_release_ids)
    ):
        raise CursorSkillPayloadError("Cursor payload retention set is invalid")
    payloads = Path(state_root) / "skill-payloads"
    try:
        managed_install._reject_link_components(payloads)
        entries = tuple(os.scandir(payloads))
    except (OSError, managed_install.ManagedInstallError) as exc:
        raise CursorSkillPayloadError(
            "Cursor payload cache could not be enumerated"
        ) from exc
    removable = []
    skipped = 0
    for entry in entries:
        path = Path(entry.path)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise CursorSkillPayloadError(
                "Cursor payload cache entry is unreadable"
            ) from exc
        if entry.name in keep_release_ids:
            if not stat.S_ISDIR(info.st_mode) or _link_like(path, info):
                raise CursorSkillPayloadError(
                    "Cursor retained payload cache is unsafe"
                )
            continue
        if not _RELEASE_ID_RE.fullmatch(entry.name):
            continue
        if not stat.S_ISDIR(info.st_mode) or _link_like(path, info):
            raise CursorSkillPayloadError("Cursor payload cache entry is unsafe")
        try:
            managed_install._reject_link_components(path)
            identity = _directory_identity(path)
            verify_cached_payload(path)
        except (
            CursorSkillPayloadError,
            managed_install.ManagedInstallError,
        ):
            skipped += 1
            continue
        removable.append((path, identity))
    for path, identity in removable:
        try:
            managed_install._reject_link_components(path)
            if _directory_identity(path) != identity:
                raise CursorSkillPayloadError(
                    "Cursor old payload cache changed before cleanup"
                )
            shutil.rmtree(path)
            _fsync_directory(payloads)
        except CursorSkillPayloadError:
            raise
        except managed_install.ManagedInstallError as exc:
            raise CursorSkillPayloadError(
                "Cursor old payload cache path is unsafe"
            ) from exc
        except OSError as exc:
            raise CursorSkillPayloadError(
                "Cursor old payload cache could not be removed"
            ) from exc
    return skipped


def publish_staged_skill(
    publication: StagedSkillPublication,
    cache_root: Path,
    *,
    expected_manifest_sha256: str,
) -> None:
    """Atomically publish one staged skill without replacing any target."""

    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor skill publication requires the activation lock"
        )
    if not isinstance(publication, StagedSkillPublication):
        raise CursorSkillPayloadError("Cursor skill publication record is invalid")
    if publication.skill not in CURSOR_M2_SKILLS:
        raise CursorSkillPayloadError("Cursor managed skill name is invalid")
    if _directory_identity(publication.staged) != publication.object_identity:
        raise CursorSkillPayloadError("Cursor staged skill identity changed")
    if publication.target.exists() or _link_like(publication.target):
        raise CursorSkillPayloadError(
            "refusing to overwrite pre-existing Cursor skill %s"
            % publication.target
        )
    tree = verify_skill_copy(
        cache_root,
        publication.staged,
        publication.skill,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    if tree != publication.tree_sha256:
        raise CursorSkillPayloadError("Cursor staged skill tree changed")
    try:
        if os.stat(publication.staged).st_dev != os.stat(
            publication.target.parent
        ).st_dev:
            raise CursorSkillPayloadError(
                "Cursor skills destination is on a different filesystem"
            )
        from installer import windows_security

        windows_security.move_write_through(
            publication.staged,
            publication.target,
            replace_existing=False,
        )
        _fsync_directory(publication.target.parent)
    except FileExistsError as exc:
        raise CursorSkillPayloadError(
            "refusing to overwrite pre-existing Cursor skill %s"
            % publication.target
        ) from exc
    except CursorSkillPayloadError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CursorSkillPayloadError("Cursor skill could not be published") from exc
    if initial_skill_state(
        cache_root,
        publication.target,
        publication.skill,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_object_identity=publication.object_identity,
    ) != "target":
        raise CursorSkillPayloadError("published Cursor skill verification failed")


def remove_initial_skill(
    cache_root: Path,
    target: Path,
    skill: str,
    *,
    expected_manifest_sha256: str,
    expected_object_identity: Tuple[int, int, int],
    quarantine_path: Path,
) -> None:
    """Quarantine and remove only the exact directory published by this transaction."""

    if not client_host_ownership._PUBLICATION_LOCK_HELD.get():
        raise CursorSkillPayloadError(
            "Cursor skill recovery requires the activation lock"
        )
    target = Path(target)
    quarantine = Path(quarantine_path)
    if quarantine.exists() or _link_like(quarantine):
        if target.exists() or _link_like(target):
            raise CursorSkillPayloadError(
                "repair_required: Cursor skill rollback has two live objects"
            )
    else:
        state = initial_skill_state(
            cache_root,
            target,
            skill,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_object_identity=expected_object_identity,
        )
        if state == "pre":
            return
        if state != "target":
            raise CursorSkillPayloadError(
                "repair_required: Cursor managed skill contains user changes"
            )
        try:
            from installer import windows_security

            windows_security.move_write_through(
                target,
                quarantine,
                replace_existing=False,
            )
            _fsync_directory(target.parent)
        except CursorSkillPayloadError:
            raise
        except (OSError, RuntimeError) as exc:
            raise CursorSkillPayloadError(
                "Cursor managed skill could not be quarantined"
            ) from exc
    quarantined_state = initial_skill_state(
        cache_root,
        quarantine,
        skill,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_object_identity=expected_object_identity,
    )
    if quarantined_state != "target":
        if not (target.exists() or _link_like(target)):
            try:
                from installer import windows_security

                windows_security.move_write_through(
                    quarantine,
                    target,
                    replace_existing=False,
                )
                _fsync_directory(target.parent)
            except (OSError, RuntimeError) as exc:
                raise CursorSkillPayloadError(
                    "repair_required: changed Cursor skill is preserved in quarantine"
                ) from exc
        raise CursorSkillPayloadError(
            "repair_required: Cursor managed skill contains user changes"
        )
    try:
        shutil.rmtree(quarantine)
        _fsync_directory(quarantine.parent)
    except OSError as exc:
        raise CursorSkillPayloadError(
            "Cursor managed skill could not be rolled back"
        ) from exc
    if target.exists() or _link_like(target):
        raise CursorSkillPayloadError(
            "repair_required: Cursor skill target reappeared during rollback"
        )
