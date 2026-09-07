"""Thin client for decision-engine."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import math
import os
import platform
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid
from http.client import HTTPException
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener

# Cross-process advisory file locking is POSIX-only via fcntl; Windows offers the
# equivalent through msvcrt byte-range locks. runner.py MUST import on Windows — the
# board-render path pulls USER_AGENT / https_context from this module (see
# client/popup/launcher.fetch_board_html), and a hard top-level ``import fcntl`` made
# that import raise ModuleNotFoundError on Windows, surfacing as a `render-fetch-failed`
# board popup. So the lock primitive is resolved by platform, never as a hard dependency.
# Both names are always bound (to None when absent) so _exclusive_file_lock can branch.
try:
    import fcntl  # POSIX advisory whole-file lock
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt  # Windows byte-range lock (absent on POSIX)
except ImportError:  # pragma: no cover - POSIX has no msvcrt
    msvcrt = None  # type: ignore[assignment]

from client.http_safety import NoRedirect  # the one redirect guard (stdlib-only leaf)
from client.version import CLIENT_VERSION, USER_AGENT  # single source of truth (leaf module, no deps)

# Unified with the MCP shell so one activation / one device token serves both the
# `audit` CLI and the shim (Owner ruling 2026-07-07). Both resolve the SAME file.
DEFAULT_CONFIG_PATH = Path.home() / ".deeppattern" / "decision-engine" / "config.json"
TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}
FINISHED_LINGER_S = 30.0
ACTIVE_RUNS_SCHEMA_VERSION = 1
LOCAL_TERMINAL_TOMBSTONE_TTL_S = 24 * 60 * 60.0


def normalize_ui_locale(value: Any = None) -> str:
    """Return one supported Stopper locale without persisting arbitrary input.

    Thin shim over the single resolver in `client.i18n` (kept for the existing callers that import
    `runner.normalize_ui_locale`). Resolution order: explicit value → $DE_UI_LOCALE → system UI
    language → en-US. The system-language step means an un-tagged run follows the OS instead of
    hard-defaulting to English.
    """
    from client import i18n

    return i18n.resolve_locale(value)


class AuditError(RuntimeError):
    pass


class ActivationPersistError(AuditError):
    """The server bound this device but persisting the token locally failed.

    Terminal by design: retrying would open the popup again and consume ANOTHER device
    slot for the same machine, so ``ensure_device_activated`` re-raises it instead of
    re-entering the activation retry loop."""


def config_path() -> Path:
    # DE_CONFIG_PATH is the shim/installer override — honoring it is what makes the
    # two clients share one config file; otherwise the unified default under
    # ~/.deeppattern/decision-engine/. Net-new release: no legacy path is consulted.
    override = os.getenv("DE_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def config_endpoint(config: Dict[str, Any]) -> str:
    """Resolve the server endpoint from a loaded config (unified ``server_endpoint``)."""
    return config.get("server_endpoint", "")


def runtime_dir() -> Path:
    """Private, Git-ignored state owned by the installed client body."""
    return config_path().parent / ".runtime"


def runtime_locks_dir() -> Path:
    return runtime_dir() / "locks"


def runtime_logs_dir() -> Path:
    return runtime_dir() / "logs"


def config_activation_lock_path() -> Path:
    return runtime_locks_dir() / "config.activation.lock"


def config_activation_lock_paths(config_file: Optional[Path] = None) -> tuple[Path, ...]:
    """Locks to hold during the previous-stable compatibility window.

    The legacy lock is acquired first so a still-running previous client and this
    client remain mutually exclusive while the primary lock moves into `.runtime`.
    """
    target = config_file or config_path()
    legacy = target.with_suffix(target.suffix + ".activation.lock")
    current = target.parent / ".runtime" / "locks" / "config.activation.lock"
    return (legacy, current)


@contextlib.contextmanager
def _exclusive_file_lock(handle) -> Iterable[None]:
    """Hold a cross-process EXCLUSIVE advisory lock on an open file for the block.

    POSIX uses ``fcntl.flock`` (whole-file, blocks until acquired). Windows uses
    ``msvcrt.locking`` on a single byte at offset 0 (``LK_LOCK`` blocks with retry, then
    raises ``OSError`` after ~10s). If neither primitive exists the guard degrades to a
    no-op: the single-flight it protects (one activation popup per machine) is a
    best-effort UX defense, not a correctness invariant, so a lock-less exotic platform
    still runs — it only loses the concurrent-activation guard."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows only
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - no fcntl AND no msvcrt
        yield


@contextlib.contextmanager
def config_activation_lock(config_file: Optional[Path] = None) -> Iterable[None]:
    """Single-flight guard over first-use activation (mirrors ``active_runs_lock``).

    Cross-process advisory locks in both the previous-stable location and `.runtime`
    prevent two concurrent client versions from each opening an activation popup and
    burning two device slots for the same machine. The second caller blocks here until
    the first finishes, then re-checks the config and reuses the persisted token."""
    with _exclusive_path_locks(config_activation_lock_paths(config_file)):
        yield


def active_run_path() -> Path:
    override = os.getenv("DE_ACTIVE_RUN")
    if override:
        return Path(override).expanduser()
    registry_override = os.getenv("DE_ACTIVE_RUNS")
    if registry_override:
        return Path(registry_override).expanduser().with_name("active-run.json")
    return runtime_dir() / "active-run.json"


def active_runs_path() -> Path:
    override = os.getenv("DE_ACTIVE_RUNS")
    if override:
        return Path(override).expanduser()
    run_override = os.getenv("DE_ACTIVE_RUN")
    if run_override:
        return Path(run_override).expanduser().with_name("active-runs.json")
    return runtime_dir() / "active-runs.json"


def _injected_runtime_paths(name: str) -> Optional[tuple[Path, ...]]:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AuditError("invalid %s JSON" % name) from exc
    if not isinstance(loaded, list) or not loaded \
            or any(not isinstance(value, str) or not value.strip() for value in loaded):
        raise AuditError("invalid %s path list" % name)
    return tuple(Path(value).expanduser() for value in loaded)


def _active_paths_are_overridden() -> bool:
    """True only when the effective paths differ from managed defaults.

    Stopper launchers forward the computed paths through the environment (the
    native macOS panel requires that), so mere variable presence is not evidence
    of a caller override.
    """
    managed_run = runtime_dir() / "active-run.json"
    managed_runs = runtime_dir() / "active-runs.json"
    return (
        _path_key(active_run_path()) != _path_key(managed_run)
        or _path_key(active_runs_path()) != _path_key(managed_runs)
    )


def de_lite_active_run_path() -> Path:
    return active_run_path()


def de_lite_active_runs_path() -> Path:
    return active_runs_path()


def de_lite_active_runs_lock_paths() -> tuple[Path, ...]:
    return active_runs_lock_paths()


def de_lite_stopper_lock_path() -> Path:
    return stopper_panel_lock_path()


def _legacy_active_run_path() -> Path:
    return config_path().parent / "active-run.json"


def _legacy_active_runs_path() -> Path:
    return config_path().parent / "active-runs.json"


def _active_run_write_paths() -> tuple[Path, ...]:
    injected = _injected_runtime_paths("DE_ACTIVE_RUN_WRITE_PATHS")
    if injected is not None:
        return injected
    if _active_paths_are_overridden():
        return (active_run_path(),)
    # The registry is authoritative and is written first. For this best-effort
    # singleton projection, favor the current reader if the second copy fails.
    return (active_run_path(), _legacy_active_run_path())


def _active_runs_write_paths() -> tuple[Path, ...]:
    injected = _injected_runtime_paths("DE_ACTIVE_RUNS_WRITE_PATHS")
    if injected is not None:
        return injected
    if _active_paths_are_overridden():
        return (active_runs_path(),)
    return (_legacy_active_runs_path(), active_runs_path())


def active_runs_lock_path() -> Path:
    override = os.getenv("DE_ACTIVE_RUNS_LOCK")
    if override:
        return Path(override).expanduser()
    if _active_paths_are_overridden():
        return active_runs_path().with_suffix(active_runs_path().suffix + ".lock")
    return runtime_locks_dir() / "active-runs.lock"


def active_runs_lock_paths() -> tuple[Path, ...]:
    injected = _injected_runtime_paths("DE_ACTIVE_RUNS_LOCK_PATHS")
    if injected is not None:
        return injected
    primary = active_runs_lock_path()
    if _active_paths_are_overridden():
        return (primary,)
    return (
        _legacy_active_runs_path().with_suffix(".json.lock"),
        primary,
    )


def stopper_panel_lock_path() -> Path:
    override = os.getenv("DE_STOPPER_SINGLETON_LOCK")
    if override:
        return Path(override).expanduser()
    return runtime_locks_dir() / "stopper-panel.lock"


def stopper_panel_lock_paths() -> tuple[Path, ...]:
    if os.getenv("DE_STOPPER_SINGLETON_LOCK"):
        return (stopper_panel_lock_path(),)
    return (config_path().parent / "stopper-panel.lock", stopper_panel_lock_path())


def stopper_log_path() -> Path:
    return runtime_logs_dir() / "stopper.log"


def _ensure_private_dir(path: Path) -> None:
    """Create a directory inside `.runtime` and restrict each runtime component."""
    runtime = runtime_dir()
    try:
        relative = path.relative_to(runtime)
    except ValueError as exc:
        raise AuditError("refusing to manage a non-runtime directory: %s" % path) from exc
    try:
        if _is_link_like(runtime):
            raise AuditError("runtime path must not be a symlink or reparse point: %s" % runtime)
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _is_link_like(runtime):
            raise AuditError("runtime path must not be a symlink or reparse point: %s" % runtime)
        current = runtime
        for part in relative.parts:
            if part in ("", ".", ".."):
                raise AuditError("invalid runtime directory component: %s" % path)
            current = current / part
            if _is_link_like(current):
                raise AuditError("runtime path must not be a symlink or reparse point: %s" % current)
            current.mkdir(mode=0o700, exist_ok=True)
            if _is_link_like(current):
                raise AuditError("runtime path must not be a symlink or reparse point: %s" % current)
        for candidate in (runtime, path):
            try:
                os.chmod(candidate, 0o700, follow_symlinks=False)
            except (NotImplementedError, TypeError):  # Windows/Python variants
                candidate.chmod(0o700)
    except AuditError:
        raise
    except (FileExistsError, NotADirectoryError) as exc:
        raise AuditError("runtime path is not a directory: %s" % path) from exc
    except OSError:
        # Directory permissions are best-effort on Windows. Creation errors that
        # make the path unusable still surface later at open/write with the path.
        if not path.is_dir():
            raise


def _path_key(path: Path) -> str:
    # Lexical normalization is sufficient for comparing our own computed/env paths
    # and avoids a filesystem resolve on every active-run save.
    return os.path.normcase(os.path.abspath(str(path.expanduser())))


def _is_link_like(path: Path) -> bool:
    """Detect POSIX symlinks and Windows reparse points without following them."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(reparse and attrs & reparse)


def _open_lock_file(path: Path):
    """Open a lock sentinel without following a pre-existing link where supported."""
    if _is_link_like(path):
        raise OSError("refusing linked lock path: %s" % path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    return os.fdopen(fd, "r+", encoding="utf-8")


def _open_append_log(path: Path, *, binary: bool = False):
    """Append to a private diagnostic without following a linked final path on POSIX."""
    if _is_link_like(path):
        raise OSError("refusing linked log path: %s" % path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    if binary:
        flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    if binary:
        return os.fdopen(fd, "ab")
    return os.fdopen(fd, "a", encoding="utf-8")


@contextlib.contextmanager
def _exclusive_path_locks(paths: Iterable[Path]) -> Iterable[None]:
    """Acquire every distinct lock in order and release all of them on failure."""
    with contextlib.ExitStack() as stack:
        seen = set()
        for path in paths:
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            if _path_key(path.parent) == _path_key(runtime_locks_dir()):
                _ensure_private_dir(path.parent)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context(_open_lock_file(path))
            stack.enter_context(_exclusive_file_lock(handle))
        yield


@contextlib.contextmanager
def active_runs_lock() -> Iterable[None]:
    with _exclusive_path_locks(active_runs_lock_paths()):
        yield


def load_config() -> Dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuditError("invalid config JSON at %s: %s" % (path, exc))
    return loaded if isinstance(loaded, dict) else {}


def save_config(config: Dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(rendered, encoding="utf-8")
    tmp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _atomic_write_text(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix("%s.%s.%s.tmp" % (path.suffix, os.getpid(), uuid.uuid4().hex))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _audit_depth(run: Dict[str, Any]) -> str:
    """The audit DEPTH tier (fast/standard/deep) for the stopper, WITHOUT exposing voice count.

    Prefer an explicit ``audit_mode``/``mode`` if the hub ever sends one; otherwise derive it from the
    run's ``profile`` — the hub's caller-independent audit rosters are named exactly ``fast`` / ``standard``
    / ``deep`` (possibly with a suffix like ``deep_ds``), and those tier words carry no vendor token so the
    server's de-vendor redaction leaves them intact. MR / connectivity profiles (fast_smoke, collection…)
    and anything unrecognized return "" → the stopper shows a generic cross-vendor label."""
    explicit = (run.get("audit_mode") or run.get("mode") or "").strip().lower()
    if explicit in ("fast", "standard", "deep"):
        return explicit
    profile = (run.get("profile") or "").strip().lower()
    if profile in ("fast", "standard", "deep"):
        return profile
    # DeepSeek opt-in appends a `_ds` suffix (e.g. `deep_ds`). Match ONLY that known variant — an
    # explicit allow-set so a non-audit profile like `fast_smoke` (connectivity) or `collection`/`mr_*`
    # is NOT misread as a tier (it returns "" → generic label).
    if profile in ("fast_ds", "standard_ds", "deep_ds"):
        return profile.split("_", 1)[0]
    return ""


# The stop-panel registry has a hard 4 MiB ceiling (installer/stopper_launch_agent.py
# `_MAX_REGISTRY_BYTES`) and NOTHING trims it: once a write pushes the file past that ceiling every
# later read raises `LaunchAgentError`, so the host bridge stays broken until a human deletes the
# file by hand — one accident, permanently unavailable. A title is ONE line on a panel row, so the
# cheap defence is to bound it where it is written rather than to defend the ceiling downstream.
_MAX_TITLE_CHARS = 200
_TITLE_ELLIPSIS = "\u2026"


def _bounded_title(value: Any) -> str:
    """A registry-safe title: bounded, and a string whatever the caller passed.

    Non-strings are dropped rather than coerced with ``str()``: a caller handing over a dict or a
    list would otherwise serialise its whole body into the registry — the same ceiling failure by
    another route, and ``str()`` would preserve every byte of it.
    """
    if not isinstance(value, str):
        return ""
    if len(value) <= _MAX_TITLE_CHARS:
        return value
    return value[: _MAX_TITLE_CHARS - len(_TITLE_ELLIPSIS)] + _TITLE_ELLIPSIS


def active_run_payload(run: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    status = run.get("status")
    payload = {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "title": _bounded_title(run.get("title", "")),
        "caller": run.get("caller", ""),
        "profile": run.get("profile", ""),
        # Audit DEPTH (fast/standard/deep) so the stopper can show the tier the user chose WITHOUT
        # exposing the number of voices. Derived from the run's `profile` (the hub's audit rosters are
        # named exactly fast/standard/deep — no vendor token, so redaction leaves the tier word intact);
        # falls back to an explicit audit_mode if a future hub sends one. Empty → the stopper renders a
        # generic label. Not vendor-identifying → privacy-safe.
        "mode": _audit_depth(run),
        "auditors": run.get("auditors", []),
        # A LOCAL advisory run (offline single-model sanity read; design §11/§17) — persisted so the
        # stop panel can render it with its honest 🔶 label and skip the hub poll a real run gets.
        # Defaults False so every existing (hub) run is untouched.
        "local": bool(run.get("local")),
        "ui_locale": normalize_ui_locale(run.get("ui_locale")),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "updated_at": now,
    }
    if "debug_authorized" in run:
        payload["debug_authorized"] = run.get("debug_authorized")
    if payload["local"]:
        payload["local_surface"] = run.get("local_surface", "")
        payload["fallback_mode"] = run.get("fallback_mode", "session-llm")
        payload["advisory_only"] = True
        if run.get("degrade_reason") is not None:
            payload["degrade_reason"] = run.get("degrade_reason")
        if run.get("degrade_action") is not None:
            payload["degrade_action"] = run.get("degrade_action")
    if status in TERMINAL_STATUSES:
        payload["finished_at"] = now
        payload["hidden_after"] = now + FINISHED_LINGER_S
    elif run.get("hidden_after") is not None:
        # A non-terminal caller-set TTL — a local advisory run's safety hidden_after (see
        # save_local_advisory_run). Existing hub runs never set this, so they are unchanged.
        payload["hidden_after"] = run.get("hidden_after")
    return payload


def _empty_active_runs_registry() -> Dict[str, Any]:
    return {
        "schema_version": ACTIVE_RUNS_SCHEMA_VERSION,
        "runs": {},
        "updated_at": time.time(),
    }


def _prune_local_terminal_tombstones(
    registry: Dict[str, Any], now: Optional[float] = None
) -> Dict[str, Any]:
    now = time.time() if now is None else now
    tombstones = registry.get("local_terminal_tombstones")
    if tombstones is None:
        return registry
    if not isinstance(tombstones, dict):
        registry.pop("local_terminal_tombstones", None)
        return registry
    for run_id, expires_at in list(tombstones.items()):
        if not isinstance(run_id, str) or not isinstance(expires_at, (int, float)) \
                or not math.isfinite(expires_at) or expires_at <= now:
            tombstones.pop(run_id, None)
    if not tombstones:
        registry.pop("local_terminal_tombstones", None)
    return registry


def _read_active_runs_registry(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    if _is_link_like(path):
        raise AuditError("active-run registry must not be a symlink or reparse point: %s" % path)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    schema = loaded.get("schema_version", ACTIVE_RUNS_SCHEMA_VERSION)
    if type(schema) is int and schema > ACTIVE_RUNS_SCHEMA_VERSION:
        raise AuditError(
            "active-run registry schema %s is newer than supported schema %s"
            % (schema, ACTIVE_RUNS_SCHEMA_VERSION)
        )
    if type(schema) is not int or schema < 1:
        return None
    loaded["schema_version"] = ACTIVE_RUNS_SCHEMA_VERSION
    if "runs" not in loaded:
        loaded["runs"] = {}
    elif not isinstance(loaded.get("runs"), dict):
        return None
    return loaded


def load_active_runs_registry() -> Dict[str, Any]:
    primary = active_runs_path()
    read_paths = [primary]
    if not _active_paths_are_overridden():
        read_paths.append(_legacy_active_runs_path())

    candidates = []
    had_copy = False
    now = time.time()
    for path in read_paths:
        had_copy = had_copy or path.exists()
        loaded = _read_active_runs_registry(path)
        if loaded is not None:
            updated = loaded.get("updated_at")
            timestamp = float(updated) if isinstance(updated, (int, float)) else 0.0
            if not math.isfinite(timestamp) or timestamp > now + 300.0:
                continue
            candidates.append((timestamp, path == primary, loaded))
    if not candidates:
        if had_copy:
            raise AuditError("no valid active-run registry copy is readable")
        return _empty_active_runs_registry()
    # A sequential dual-write can stop between copies. Whichever valid copy has
    # the newest update wins; ties prefer legacy because an old client can update
    # only that copy, while a completed new-client write makes both copies equal.
    return max(candidates, key=lambda item: (item[0], not item[1]))[2]


def _write_active_runs_registry(registry: Dict[str, Any]) -> None:
    registry["schema_version"] = ACTIVE_RUNS_SCHEMA_VERSION
    rendered = json.dumps(registry, indent=2, sort_keys=True) + "\n"
    for path in _active_runs_write_paths():
        if not _active_paths_are_overridden() and _path_key(path) == _path_key(active_runs_path()):
            _ensure_private_dir(path.parent)
        _atomic_write_text(path, rendered)


def _write_active_run_payload(payload: Dict[str, Any]) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    for path in _active_run_write_paths():
        if not _active_paths_are_overridden() and _path_key(path) == _path_key(active_run_path()):
            _ensure_private_dir(path.parent)
        _atomic_write_text(path, rendered)


def _load_de_lite_active_runs_registry() -> Dict[str, Any]:
    path = de_lite_active_runs_path()
    loaded = _read_active_runs_registry(path)
    if loaded is not None:
        updated = loaded.get("updated_at")
        timestamp = float(updated) if isinstance(updated, (int, float)) else 0.0
        if math.isfinite(timestamp) and timestamp <= time.time() + 300.0:
            return loaded
    if path.exists():
        raise AuditError("no valid DE Lite active-run registry is readable")
    return _empty_active_runs_registry()


def _write_de_lite_active_runs_registry(registry: Dict[str, Any]) -> None:
    path = de_lite_active_runs_path()
    registry["schema_version"] = ACTIVE_RUNS_SCHEMA_VERSION
    if not _active_paths_are_overridden():
        _ensure_private_dir(path.parent)
    _atomic_write_text(path, json.dumps(registry, indent=2, sort_keys=True) + "\n")


def _write_de_lite_active_run_payload(payload: Dict[str, Any]) -> None:
    path = de_lite_active_run_path()
    if not _active_paths_are_overridden():
        _ensure_private_dir(path.parent)
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _remove_active_run_projection(run_id: str) -> None:
    """Remove singleton projections for ``run_id`` while the caller holds the dual lock."""
    for path in _active_run_write_paths():
        if _is_link_like(path):
            raise AuditError(
                "active-run projection must not be a symlink or reparse point: %s" % path
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        if not run_id or payload.get("run_id") == run_id:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def prune_active_runs(registry: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    _prune_local_terminal_tombstones(registry, now)
    runs = registry.get("runs")
    if not isinstance(runs, dict):
        registry["runs"] = {}
        return registry
    for run_id, run in list(runs.items()):
        if not isinstance(run, dict):
            runs.pop(run_id, None)
            continue
        hidden_after = run.get("hidden_after")
        if isinstance(hidden_after, (int, float)) and hidden_after <= now:
            runs.pop(run_id, None)
    return registry


def _save_active_run_locked(
    run: Dict[str, Any], registry: Optional[Dict[str, Any]] = None,
    *,
    registry_loader=None,
    registry_writer=None,
    payload_writer=None,
) -> None:
    """Persist one run while the caller holds ``active_runs_lock()``."""
    payload = active_run_payload(run)
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        return
    registry_loader = registry_loader or load_active_runs_registry
    registry_writer = registry_writer or _write_active_runs_registry
    payload_writer = payload_writer or _write_active_run_payload
    registry = (
        prune_active_runs(registry_loader())
        if registry is None
        else registry
    )
    registry["runs"][run_id] = payload
    registry["updated_at"] = time.time()
    registry_writer(registry)
    payload_writer(payload)


def save_active_run(run: Dict[str, Any]) -> None:
    with active_runs_lock():
        _save_active_run_locked(run)


# Safety ceiling for a LOCAL advisory entry: no hub poll can ever reap it (design §11/§17), so it
# carries a self-prune TTL. A single-model advisory read is quick; 10 min is a generous ceiling that
# still cleans up an abandoned entry (caller crashed / never marked it done) without manual action.
LOCAL_ADVISORY_TTL_S = 600.0


def save_local_advisory_run(
    local_id: str,
    *,
    depth: Optional[str] = None,
    status: str = "running",
    started_at: Optional[float] = None,
    ttl_s: float = LOCAL_ADVISORY_TTL_S,
    title: Optional[str] = None,
    surface: Optional[str] = None,
    degrade_reason: Optional[str] = None,
    degrade_action: Optional[str] = None,
    ui_locale: Optional[str] = None,
) -> None:
    """Seed/update a LOCAL advisory run in the stop-panel registry (design §11/§17).

    An offline single-model sanity read that NEVER closes an audit gate — this is display-only
    bookkeeping. The entry carries ``local=True`` (the panel renders the honest 🔶 label and skips
    the hub poll a real run gets) and a ``hidden_after`` WATCHDOG so an abandoned entry self-prunes
    even though nothing polls it. Best-effort: seeding the panel must never break the advisory.

    The watchdog is measured from write-time (``now``), NOT from ``started_at``: re-saving a live
    run (a status update) pushes the deadline out, so only an entry with no write for ``ttl_s``
    self-prunes — a live read is never dropped mid-run. ``started_at`` stays as given so the panel's
    elapsed is correct and immune to a stale value. A non-finite / non-positive ``ttl_s`` falls back
    to the default ceiling rather than seeding an entry that prunes instantly or never."""
    now = time.time()
    lock = active_runs_lock()
    registry_loader = load_active_runs_registry
    registry_writer = _write_active_runs_registry
    payload_writer = _write_active_run_payload
    with lock:
        registry = prune_active_runs(registry_loader(), now)
        tombstones = registry.setdefault("local_terminal_tombstones", {})
        if tombstones.get(local_id, 0) > now:
            return
        runs = registry.get("runs") if isinstance(registry, dict) else None
        candidate = runs.get(local_id) if isinstance(runs, dict) else None
        previous = (
            candidate
            if isinstance(candidate, dict) and candidate.get("local") is True
            else {}
        )
        if previous.get("status") in TERMINAL_STATUSES:
            return
        started = (
            started_at
            if started_at is not None
            else previous.get("started_at") or now
        )
        ttl = ttl_s if isinstance(ttl_s, (int, float)) and ttl_s > 0 \
            and ttl_s != float("inf") else LOCAL_ADVISORY_TTL_S
        run = {
            "run_id": local_id,
            "local": True,
            "local_surface": surface if surface is not None else previous.get("local_surface", ""),
            "fallback_mode": "session-llm",
            "audit_id": None,
            "advisory_only": True,
            "title": title if title is not None else previous.get("title", ""),
            "degrade_reason": (
                degrade_reason
                if degrade_reason is not None
                else previous.get("degrade_reason", "")
            ),
            "degrade_action": (
                degrade_action
                if degrade_action is not None
                else previous.get("degrade_action", "")
            ),
            "ui_locale": normalize_ui_locale(
                ui_locale if ui_locale is not None else previous.get("ui_locale")
            ),
            "profile": depth if depth is not None else previous.get("profile", ""),
            "status": status,
            "started_at": started,
            "hidden_after": now + ttl,
        }
        if status in TERMINAL_STATUSES:
            run["completed_at"] = previous.get("completed_at") or now
            tombstones[local_id] = now + LOCAL_TERMINAL_TOMBSTONE_TTL_S
        _save_active_run_locked(
            run,
            registry,
            registry_loader=registry_loader,
            registry_writer=registry_writer,
            payload_writer=payload_writer,
        )


def complete_local_advisory_run(
    local_id: str,
    *,
    status: str,
    surface: str = "de_lite",
) -> Dict[str, Any]:
    """Move one existing DE Lite advisory into a terminal state.

    Completion is deliberately separate from ``save_local_advisory_run``: a host callback must not
    be able to create an arbitrary row, complete a hosted run, or reopen a terminal local row. The
    persisted terminal payload uses the normal 30-second stopper linger; the 600-second local TTL
    remains the abandoned-run watchdog when this callback never arrives.
    """
    if (
        surface != "de_lite"
        or not isinstance(local_id, str)
        or not local_id.startswith("local_")
        or not local_id[6:]
        or status not in {"completed", "partial", "failed"}
    ):
        raise AuditError("invalid local advisory completion")

    lock = active_runs_lock()
    registry_loader = load_active_runs_registry
    registry_writer = _write_active_runs_registry
    payload_writer = _write_active_run_payload
    with lock:
        registry = prune_active_runs(registry_loader())
        runs = registry.get("runs")
        current = runs.get(local_id) if isinstance(runs, dict) else None
        if (
            not isinstance(current, dict)
            or current.get("local") is not True
            or current.get("local_surface") != surface
            or current.get("audit_id") is not None
        ):
            raise AuditError("local advisory run not found")
        current_status = current.get("status")
        if current_status in TERMINAL_STATUSES:
            if current_status != status:
                raise AuditError("local advisory run is already terminal")
            return active_run_payload(current)
        if current_status not in {"queued", "running"}:
            raise AuditError("local advisory run has an invalid state")

        now = time.time()
        completed = dict(current)
        completed["status"] = status
        completed["completed_at"] = now
        registry.setdefault("local_terminal_tombstones", {})[local_id] = (
            now + LOCAL_TERMINAL_TOMBSTONE_TTL_S
        )
        _save_active_run_locked(
            completed,
            registry,
            registry_loader=registry_loader,
            registry_writer=registry_writer,
            payload_writer=payload_writer,
        )
        return active_run_payload(completed)


def forget_active_run(run_id: str) -> None:
    """Drop a run from the registry outright — the panel calls this once it has finished showing it.

    The registry is the panel's data source, and nothing else reaps it on the MCP path: the shim
    seeds a run at submit (status queued/running, so no `hidden_after`) and never writes again, so
    `prune_active_runs` — which only drops entries whose `hidden_after` has passed — can never
    remove it, and `clear_active_run` is only reached from the CLI. The entry therefore outlived
    every audit, and each panel start re-seeded the whole history as "running": rows that never
    went away and stacked up. This is the client-side counterpart of the A-repo server deleting an
    audit's state file once the run is over — the row's SOURCE goes away, so the row cannot return.

    Unlike clear_active_run this does not preserve a terminal entry for its linger: the linger is
    over, that is precisely why the caller is here.
    """
    with active_runs_lock():
        registry = prune_active_runs(load_active_runs_registry())
        runs = registry.get("runs")
        if isinstance(runs, dict):
            runs.pop(run_id, None)
        registry["updated_at"] = time.time()
        _write_active_runs_registry(registry)
        if run_id:
            _remove_active_run_projection(run_id)


def clear_active_run(run_id: str) -> None:
    with active_runs_lock():
        registry = prune_active_runs(load_active_runs_registry())
        runs = registry.get("runs") if isinstance(registry.get("runs"), dict) else {}
        run = runs.get(run_id) if run_id and isinstance(runs, dict) else None
        if isinstance(run, dict):
            hidden_after = run.get("hidden_after")
            if run.get("status") not in TERMINAL_STATUSES or not isinstance(hidden_after, (int, float)):
                runs.pop(run_id, None)
        registry["updated_at"] = time.time()
        _write_active_runs_registry(registry)

        # Keep projection cleanup under the same dual lock as registry mutation;
        # otherwise a concurrent save can be deleted after it commits the registry.
        _remove_active_run_projection(run_id)


def normalize_server_url(server_url: str) -> str:
    value = server_url.strip().rstrip("/")
    if not value:
        raise AuditError("server URL is required")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def https_context() -> Optional[ssl.SSLContext]:
    ca_file = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    try:
        import certifi  # type: ignore
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def request_json(
    method: str,
    path: str,
    *,
    server_url: Optional[str] = None,
    token: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: int = 60,
) -> Dict[str, Any]:
    config = load_config()
    base_url = normalize_server_url(server_url or config_endpoint(config))
    # Lazy-activation seam: a call that would use the STORED device identity (token is None
    # → "act as me") but has no local token yet triggers first-use activation — a native
    # popup for the activation secret → POST /v1/devices/activate → persist — instead of a
    # guaranteed 401. Callers that pass an explicit token="" (login / doctor health / the
    # activate POST itself) are anonymous by design and never enter this branch, so the
    # activation POST below cannot recurse. On cancel / failure ensure_device_activated
    # raises AuditError (fail-closed: no token saved, no browser opened).
    if token is None and not has_valid_device_token(config):
        config = ensure_device_activated(base_url, timeout_s=timeout_s)
    from client import i18n

    data = None
    request_headers = {
        "Accept": "application/json",
        "Accept-Language": i18n.accept_language(),
        "User-Agent": USER_AGENT,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    access_token = token if token is not None else config.get("access_token")
    if access_token:
        request_headers["Authorization"] = "Bearer " + access_token
    if headers:
        request_headers.update(headers)
    try:
        request = Request(base_url + path, data=data, headers=request_headers, method=method)
    except ValueError as exc:
        # Kept out of the transport ladder below on purpose. Everything it catches carries
        # SERVER-chosen bytes, so it reports types only; base_url is the USER's own config,
        # so naming the typo is the whole point and costs nothing.
        raise AuditError("invalid server URL %r: %s" % (base_url, exc)) from None
    try:
        # Built per call (not module-level) so https_context() is evaluated now and honours
        # SSL_CERT_FILE / certifi — mirrors the /db/render opener at launcher.fetch_board_html.
        # NoRedirect refuses the 3xx that would otherwise hand the Bearer token above to
        # whatever host a hostile Location names; for http:// (tests) the default handler serves.
        opener = build_opener(NoRedirect, HTTPSHandler(context=https_context()))
        with opener.open(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        # exc owns an OPEN response fp. A completed read() closes it at EOF, but the two
        # paths below that don't complete one (a refused redirect, a truncated read) would
        # otherwise leave the socket to the GC — so the whole arm closes on its way out.
        try:
            if 300 <= exc.code < 400:
                # The refused-redirect path (NoRedirect raises HTTPError carrying the 3xx
                # code), plus any 3xx the stdlib declined to treat as a redirect (a 304, a
                # Location-less 302). The body is deliberately NOT read. The trust boundary
                # is NOT "server bytes vs not" — the 4xx arm below deliberately echoes the
                # hub's own {"error": ...} contract. It's WHO answered: a 4xx comes from the
                # TLS-authenticated hub, a 3xx body comes from whoever a Location named, and
                # an `audit` CLI error is read by the agent driving it. `from None` for the
                # same reason: the HTTPError carries the response headers, that Location
                # among them.
                raise AuditError(
                    "HTTP %s from %s: refused — this client never follows redirects on an "
                    "authenticated call; check that your configured server URL is the hub's "
                    "real https:// endpoint" % (exc.code, path)) from None
            try:
                raw_error = exc.read().decode("utf-8", errors="replace")
            except (HTTPException, OSError):
                # A truncated / cut-off error body must not escape as a raw IncompleteRead
                # from inside this handler: fall through to exc.reason below.
                raw_error = ""
            error_code = None
            try:
                parsed = json.loads(raw_error)
                # `isinstance`: a hostile body of `123` parses fine, and then 123.get() is an
                # AttributeError — which subclasses nothing this function catches.
                parsed_error = parsed.get("error") if isinstance(parsed, dict) else None
                if isinstance(parsed_error, str) and parsed_error:
                    error_code = parsed_error
                message = parsed_error or raw_error
            except json.JSONDecodeError:
                message = raw_error or exc.reason
            # Carry the HTTP status on the raised error (additive attribute — every other raise
            # site leaves it unset, callers read it via getattr). The stop panel uses it to tell a
            # definitive 404 (the hub purged / never registered a run) from a transient network
            # AuditError, so it can reap a vanished run instead of polling it forever.
            err = AuditError("HTTP %s from %s: %s" % (exc.code, path, message))
            err.status_code = exc.code
            if error_code is not None:
                err.error_code = error_code
            raise err
        finally:
            try:
                exc.close()
            except Exception:  # aqg: top-level boundary — cleanup must never replace the AuditError
                pass
    except TimeoutError:
        raise AuditError("request timed out for %s after %ss" % (path, timeout_s))
    except URLError as exc:
        raise AuditError("request failed for %s: %s" % (path, exc.reason))
    except OSError as exc:
        raise AuditError("request failed for %s: %s" % (path, exc))
    except (HTTPException, ValueError) as exc:
        # These carry SERVER-CONTROLLED bytes and subclass neither URLError nor OSError, so
        # they used to escape raw — breaking the every-failure-is-an-AuditError contract:
        #   UnicodeDecodeError (a ValueError) — a non-UTF-8 body; str() echoes the offending byte
        #   IncompleteRead (an HTTPException) — a body truncated mid-read
        #   ValueError — a malformed Location, raised inside urllib BEFORE NoRedirect is consulted
        # Report the exception TYPE only, for the same reason the 3xx arm withholds the body.
        raise AuditError("request failed for %s: %s" % (path, type(exc).__name__)) from None
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditError("invalid JSON response from %s: %s" % (path, exc))
    return loaded if isinstance(loaded, dict) else {"data": loaded}


def device_name_default() -> str:
    hostname = socket.gethostname().split(".")[0] or "device"
    return "%s-%s" % (getpass.getuser(), hostname)


def device_fingerprint() -> str:
    raw = "|".join(
        [
            socket.gethostname(),
            platform.platform(),
            platform.machine(),
            getpass.getuser(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_focus(values: Iterable[str]) -> list[str]:
    focus: list[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item:
                focus.append(item)
    return focus


def read_artifact(args: argparse.Namespace) -> Dict[str, str]:
    chunks: list[str] = []
    artifact_type = args.type
    for filename in args.file or []:
        path = Path(filename).expanduser()
        content = path.read_text(encoding="utf-8")
        chunks.append("--- file: %s ---\n%s" % (path, content))
    if args.text:
        chunks.append(args.text)
    if not chunks and not sys.stdin.isatty():
        chunks.append(sys.stdin.read())
        artifact_type = artifact_type or "stdin"
    if not chunks:
        raise AuditError("provide --file, --text, or pipe content on stdin")
    content = "\n\n".join(chunks)
    # Defense-in-depth: don't submit an empty/whitespace artifact (e.g. an empty pipe).
    # The server treats an empty artifact as a benign content_required error, but failing
    # fast here avoids a needless round-trip and the historical strike-on-empty over-block.
    if not content.strip():
        raise AuditError(
            "artifact is empty — provide non-empty --file / --text or piped content to audit"
        )
    return {"type": artifact_type or "text", "content": content}


def print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def stopper_binary_path() -> Path:
    override = os.getenv("DE_STOPPER_BIN")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "bin" / "decision-engine-stopper"


def stopper_app_path() -> Path:
    override = os.getenv("DE_STOPPER_APP")
    if override:
        return Path(override).expanduser()
    return config_path().parent / "app" / "Decision Engine Stopper.app"


# Rosetta translates x86_64 for Apple Silicon, never arm64 for Intel, so each Mac architecture
# needs native code. Keep the mapping here as the one contract shared by direct launch, the
# LaunchAgent installer, and the sandbox bridge.
_SHIPPED_STOPPER_BY_ARCH = {
    "arm64": "decision-engine-stopper-arm64",
    "aarch64": "decision-engine-stopper-arm64",
    "x86_64": "decision-engine-stopper-x86_64",
    "amd64": "decision-engine-stopper-x86_64",
}


def shipped_stopper_arch_supported() -> bool:
    """Whether THIS machine can actually execute the shipped native Stopper."""
    if platform.system() != "Darwin":
        return False
    return platform.machine().lower() in _SHIPPED_STOPPER_BY_ARCH


def shipped_stopper_path(root: Path) -> Optional[Path]:
    """Return the interpreter-architecture-matched Stopper path without probing the filesystem."""
    name = _SHIPPED_STOPPER_BY_ARCH.get(platform.machine().lower())
    if name is None:
        return None
    return Path(root) / "desktop" / "macos" / "bin" / name


def shipped_stopper_binary() -> Optional[Path]:
    """The prebuilt stop-panel binary that SHIPS in the client body (``desktop/macos/bin/``).

    Used as a LAST RESORT when nothing is installed at the runtime launch locations (launch agent /
    ``.app`` / ``~/.local/bin``) — which is the default state, because the installer never places the
    stopper anywhere. The body is delivered by ``git pull`` (the silent self-update), so this binary is
    already on disk, keeps its committed ``+x`` bit, is **ad-hoc signed** (so Apple Silicon permits it to
    execute) and carries **no** ``com.apple.quarantine`` xattr (that is set only for browser/curl
    downloads, never for git-written files) — so it runs directly with no Gatekeeper prompt. macOS-only;
    Returns ``None`` when the file is absent (e.g. a partial checkout) or when this Mac cannot
    execute it — see ``shipped_stopper_arch_supported``."""
    if not shipped_stopper_arch_supported():
        return None
    # client/runner.py → the body root is two levels up; the binaries ship under desktop/macos/bin/.
    cand = shipped_stopper_path(Path(__file__).resolve().parent.parent)
    if cand is None:
        return None
    return cand if cand.exists() else None


def stopper_ui_visible() -> bool:
    script = (
        'tell application "System Events" to '
        '((count of (application processes whose bundle identifier is "com.decision-engine.stopper")) > 0) '
        'or ((count of (application processes whose name is "Decision Engine Stopper")) > 0) '
        'or ((count of (application processes whose name is "DecisionEngineStopper")) > 0) '
        'or ((count of (application processes whose name is "decision-engine-stopper")) > 0)'
    )
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.stdout.strip().lower() == "true"


def wait_for_stopper_ui(timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if stopper_ui_visible():
            return True
        time.sleep(0.5)
    return stopper_ui_visible()


def kickstart_stopper_launch_agent() -> bool:
    target = "gui/%s/com.decision-engine.stopper" % os.getuid()
    try:
        completed = subprocess.run(
            ["launchctl", "kickstart", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and wait_for_stopper_ui()


def launch_stopper_app() -> bool:
    app_path = stopper_app_path()
    if not app_path.exists():
        return False
    try:
        completed = subprocess.run(
            ["open", "-g", str(app_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and wait_for_stopper_ui()


def _note_stopper_miss(reason: str) -> None:
    """Append ONE diagnostic line to stopper.log when the panel could not be launched. The original bug
    was that every launch failure returned silently, so it took code archaeology to find. If the shipped
    fallback ALSO misses (binary absent from a partial checkout, unsupported architecture, or not
    executable), leave a breadcrumb instead of the same invisible no-op. Fail-safe — a
    diagnostic must never raise into audit start."""
    try:
        log_path = stopper_log_path()
        _ensure_private_dir(log_path.parent)
        with _open_append_log(log_path) as fh:
            fh.write("stopper NOT launched: %s\n" % reason)
    except Exception:  # aqg: top-level boundary — logging must never break the caller
        pass


def _gui_python() -> str:
    """A GUI (console-less) Python for the panel subprocess. On Windows prefer ``pythonw.exe``
    beside the running interpreter so no console window flashes next to the panel; elsewhere the
    running interpreter serves."""
    exe = Path(sys.executable)
    if os.name == "nt":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return sys.executable


def _detached_spawn_kwargs() -> Dict[str, Any]:
    """Detached, console-less spawn kwargs so the panel outlives the shim and shows no console.
    Windows uses ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`` — those
    constants exist ONLY on Windows, so getattr-default them to 0 (the SHAPE stays testable on a
    POSIX host). POSIX uses ``start_new_session`` (the same detach the macOS binary path uses)."""
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"creationflags": flags}
    return {"start_new_session": True}


def _stopper_runtime_env() -> Dict[str, str]:
    """Return runtime paths for either stopper implementation.

    Export the exact lock path selected by the runner instead of asking the native
    process to independently reimplement Python path normalization and branch logic.
    """
    env = os.environ.copy()
    env.setdefault("DE_CONFIG_PATH", str(config_path()))
    env.setdefault("DE_ACTIVE_RUN", str(active_run_path()))
    env.setdefault("DE_ACTIVE_RUNS", str(active_runs_path()))
    env["DE_ACTIVE_RUNS_LOCK"] = str(active_runs_lock_path())
    env["DE_ACTIVE_RUNS_LOCK_PATHS"] = json.dumps(
        [str(path) for path in active_runs_lock_paths()]
    )
    env["DE_ACTIVE_RUNS_WRITE_PATHS"] = json.dumps(
        [str(path) for path in _active_runs_write_paths()]
    )
    env["DE_ACTIVE_RUN_WRITE_PATHS"] = json.dumps(
        [str(path) for path in _active_run_write_paths()]
    )
    return env


def _de_lite_stopper_runtime_env() -> Dict[str, str]:
    """Use the shared registry/singleton so hosted and DE Lite rows share one panel."""
    return _stopper_runtime_env()


def _launch_stopper_panel_subprocess(*, de_lite: bool = False) -> None:
    """Launch the cross-platform tkinter stop panel (``client.stopper.panel``) — the client's stop
    panel on any platform without the native macOS binary (Windows first; Linux too). Best-effort:
    a missing Tk / no display makes the panel exit cleanly, and a spawn failure is logged, never
    raised (the panel is UX, it must not break audit start). The panel dedups its own instance, so
    re-launching per audit is safe."""
    body_root = Path(__file__).resolve().parent.parent  # repo root → `-m client.stopper.panel` resolves
    env = _de_lite_stopper_runtime_env() if de_lite else _stopper_runtime_env()
    # Make the client package importable even when it is not pip-installed (clone / thin client).
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(body_root) + (os.pathsep + existing_pp if existing_pp else "")
    log_path = stopper_log_path()
    try:
        _ensure_private_dir(log_path.parent)
        log = _open_append_log(log_path, binary=True)
    except (OSError, AuditError) as exc:
        _note_stopper_miss("panel log unavailable: %r" % exc)
        log = None
    try:
        subprocess.Popen(
            [_gui_python(), "-m", "client.stopper.panel"],
            cwd=str(body_root),
            stdin=subprocess.DEVNULL,
            stdout=(log or subprocess.DEVNULL),
            stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
            env=env,
            close_fds=True,
            **_detached_spawn_kwargs(),
        )
    except OSError as exc:
        _note_stopper_miss("panel spawn failed: %r" % exc)
    finally:
        if log is not None:
            log.close()  # the child keeps its own inherited handle open


def launch_stopper_if_available(*, prefer_shipped: bool = False) -> None:
    """Launch the stopper that matches the current client contract.

    Hosted audits keep the established launch-agent/app/installed-binary precedence. DE Lite local
    runs set ``prefer_shipped`` so an older installed stopper cannot misrender or hub-poll the new
    local-only registry shape; that path fails closed when this client body has no compatible binary.
    """
    if os.getenv("DE_SKIP_STOPPER_LAUNCH") == "1":
        return
    if platform.system() != "Darwin":
        # No native binary off macOS — use the cross-platform tkinter panel (Windows / Linux).
        if prefer_shipped:
            _launch_stopper_panel_subprocess(de_lite=True)
        else:
            _launch_stopper_panel_subprocess()
        return
    if prefer_shipped:
        shipped = shipped_stopper_binary()
        if shipped is None:
            _note_stopper_miss(
                "compatible shipped stopper required for DE Lite but unavailable"
            )
            return
        stopper = shipped
    else:
        if kickstart_stopper_launch_agent():
            return
        if launch_stopper_app():
            return

        stopper = stopper_binary_path()
        if not (stopper.exists() and os.access(stopper, os.X_OK)):
            # Nothing installed at ~/.local/bin (the installer never places it), and no launch agent / .app —
            # fall back to the binary that ships in the client body, so a git-pulled client gets the stop panel
            # with zero install steps. This is the common thin-client path (agent-driven audit on a fresh box).
            shipped = shipped_stopper_binary()
            if shipped is None:
                _note_stopper_miss(
                    "no installed binary at %s and none shipped for this platform" % stopper
                )
                return
            stopper = shipped
    if not os.access(stopper, os.X_OK):
        _note_stopper_miss("stopper binary not executable: %s" % stopper)
        return

    log_path = stopper_log_path()
    env = _de_lite_stopper_runtime_env() if prefer_shipped else _stopper_runtime_env()
    try:
        _ensure_private_dir(log_path.parent)
        with _open_append_log(log_path, binary=True) as log:
            subprocess.Popen(
                [str(stopper)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
    except (OSError, AuditError) as exc:
        _note_stopper_miss("spawn failed for %s: %r" % (stopper, exc))
        return


def has_valid_device_token(config: Dict[str, Any]) -> bool:
    """A device is activated on THIS machine iff its config carries a device access token.

    A local presence check (not a server round-trip): activation persists ``access_token``,
    so its presence is the idempotency signal that first-use activation already happened and
    must not be re-prompted. A token the server later rejects surfaces as a normal 401 from
    the actual request, which is a separate re-auth concern, not first-use onboarding. A
    blank / whitespace-only token counts as MISSING (fail-closed) so it re-triggers the
    popup rather than proceeding to a guaranteed 401."""
    token = config.get("access_token")
    return bool(token) and bool(str(token).strip())


def activate_with_secret(
    server_url: str,
    secret: str,
    device_name: str,
    timeout_s: int,
    *,
    fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """Activate + bind THIS machine with ``secret``, persist the device token, return config.

    The single activation core shared by the ``login`` CLI and the lazy-activation popup:
    it POSTs {device_name, device_fingerprint, activation_secret, client_version} to
    ``/v1/devices/activate`` (anonymous — token="") and, on success, writes the returned
    device token to the SAME config file the whole client reads. The secret is sent once and
    is never logged nor stored. Raises ``AuditError`` on a wrong/expired/at-capacity secret
    (the server 4xx is surfaced by ``request_json``) or a malformed success body."""
    if secret.startswith("auth_"):
        raise AuditError(
            "activation secret should start with act_; auth_... is the authorization id, "
            "not the device activation secret"
        )
    body = {
        "device_name": device_name,
        "device_fingerprint": fingerprint or device_fingerprint(),
        "activation_secret": secret,
        "client_version": CLIENT_VERSION,
    }
    result = request_json(
        "POST",
        "/v1/devices/activate",
        server_url=server_url,
        token="",
        body=body,
        timeout_s=timeout_s,
    )
    device_id = result.get("device_id")
    access_token = result.get("access_token")
    if not device_id or not access_token:
        # Fail-closed: never persist a partial identity or fabricate a token.
        raise AuditError("activation response missing device token")
    config = load_config()
    config.update(
        {
            "server_endpoint": server_url,
            "device_id": device_id,
            "access_token": access_token,
            "refresh_token": result.get("refresh_token", ""),
            "device_name": device_name,
        }
    )
    try:
        save_config(config)
    except OSError as exc:
        # The server already bound this device; a failed local write is TERMINAL (see
        # ActivationPersistError) — re-prompting would consume another device slot.
        raise ActivationPersistError(
            "device activated on server but saving the local token failed: %s" % exc
        )
    return config


def run_activation_popup(
    server_url: str,
    *,
    device_name_default_value: str,
    error: str = "",
    timeout_s: float = 1800.0,
) -> Dict[str, Any]:
    """Open the native activation-form popup and return ``open_popup``'s outcome dict.

    Lazy-imports the popup launcher so importing this module never probes pywebview. The
    server host is shown for context; ``error`` (caller-authored, generic) drives the retry
    notice. NEVER opens a system browser — an absent native backend returns the structured
    ``no-webview-backend`` outcome, which the caller maps to a fail-closed AuditError."""
    from client.popup.launcher import PopupSpec, open_popup, render_activation_html

    spec = PopupSpec(
        kind="activation",
        title="激活 Decision Engine",
        note="首次使用需激活本设备。请输入 Owner 提供的授权码（服务器：%s）。" % server_url,
        payload={"device_name_default": device_name_default_value, "error": error},
    )
    return open_popup(spec, render=render_activation_html, timeout_s=timeout_s)


def ensure_device_activated(server_url: str, *, timeout_s: int = 20) -> Dict[str, Any]:
    """First-use gate: return an activated config, prompting the popup only when needed.

    Idempotent — if the local config already has a device token, return it untouched (no
    popup). Otherwise loop: open the activation popup → on a committed secret, activate +
    bind this machine and return; on an empty secret or a failed activation, re-open the
    popup with a GENERIC retry notice (the server's raw error / the secret are never shown).
    A cancel / closed window / timeout / missing native backend is fail-closed: raise
    ``AuditError`` without persisting a token and without any browser fallback."""
    config = load_config()
    if has_valid_device_token(config):
        return config

    # Serialize first-use activation across concurrent client processes (see
    # config_activation_lock) and re-check under the lock: a peer that activated while we
    # waited means we reuse its token with no popup.
    with config_activation_lock():
        config = load_config()
        if has_valid_device_token(config):
            return config

        error = ""
        while True:
            outcome = run_activation_popup(
                server_url, device_name_default_value=device_name_default(), error=error
            )
            result = outcome.get("result")
            if outcome.get("outcome") != "committed" or not isinstance(result, dict):
                # dismissed / closed / timeout / no-webview-backend → stop, stay unactivated.
                if outcome.get("outcome") == "no-webview-backend":
                    raise AuditError(
                        "cannot open the activation window (no native popup backend); "
                        "activate manually — set DE_ACTIVATION_SECRET in the environment and "
                        "run: audit login --server-url %s" % server_url
                    )
                raise AuditError("activation cancelled; device not activated")

            secret = (result.get("activation_secret") or "").strip()
            device_name = (result.get("device_name") or "").strip() or device_name_default()
            if not secret:
                error = "请输入授权码后再激活。"
                continue
            try:
                return activate_with_secret(server_url, secret, device_name, timeout_s)
            except ActivationPersistError:
                # Server bound this device but the local token write failed — TERMINAL.
                # Retrying would consume another device slot, so surface it, don't loop.
                raise
            except AuditError:
                # Fail-closed: do NOT surface the server's raw error (may hint at the secret /
                # internal state). A generic retry notice keeps the loop going until success
                # or an explicit cancel.
                error = "激活失败，请检查授权码后重试。"
                continue


def cmd_login(args: argparse.Namespace) -> int:
    server_url = normalize_server_url(args.server_url)
    secret = args.activation_secret or os.getenv("DE_ACTIVATION_SECRET")
    if not secret:
        raise AuditError("--activation-secret is required")
    device_name = args.device_name or device_name_default()
    print("Activating %s with %s ..." % (device_name, server_url), file=sys.stderr)
    activate_with_secret(
        server_url,
        secret,
        device_name,
        args.timeout_s,
        fingerprint=args.device_fingerprint or device_fingerprint(),
    )
    config = load_config()
    print("Activated %s as %s" % (device_name, config.get("device_id")))
    print("Config saved to %s" % config_path())
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    server_url = normalize_server_url(args.server_url or config_endpoint(config))
    health = request_json("GET", "/healthz", server_url=server_url, token="")
    print("server: %s" % server_url)
    print("health: %s" % ("ok" if health.get("ok") else "unexpected"))
    if config.get("access_token"):
        me = request_json("GET", "/v1/devices/me", server_url=server_url)
        print("device: %s (%s)" % (me.get("device_id"), me.get("status")))
    else:
        print("device: not logged in")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    _ = args
    config = load_config()
    if not config:
        raise AuditError("not logged in")
    me = request_json("GET", "/v1/devices/me")
    print_json(me)
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    artifact = read_artifact(args)
    caller = args.caller or args.use or "codex"
    body: Dict[str, Any] = {
        "title": args.title or "Untitled audit",
        "caller": caller,
        "audit_mode": args.audit_mode,
        "tier": args.tier,
        "artifact": artifact,
        "context": args.context or "",
        "focus": split_focus(args.focus or []),
    }
    if args.profile:
        body["profile"] = args.profile
    if args.use:
        body["use"] = args.use
    if getattr(args, "acknowledge_secrets", False):
        body["acknowledge_secrets"] = True
    idempotency_key = args.idempotency_key or str(uuid.uuid4())
    run = request_json(
        "POST",
        "/v1/audits",
        body=body,
        headers={"Idempotency-Key": idempotency_key},
        timeout_s=args.timeout_s,
    )
    save_active_run(run)
    launch_stopper_if_available()
    if args.json and not args.wait:
        print_json(run)
    elif not args.json:
        print("run_id: %s" % run["run_id"])
        print("status: %s" % run["status"])
    if args.wait:
        return wait_and_print_result(run["run_id"], args.poll_s, json_output=args.json)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run = request_json("GET", "/v1/audits/%s" % args.run_id)
    if run.get("status") in TERMINAL_STATUSES:
        save_active_run(run)
        clear_active_run(args.run_id)
    else:
        save_active_run(run)
    print_json(run) if args.json else print("%s %s" % (run["run_id"], run["status"]))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    events = request_json("GET", "/v1/audits/%s/events" % args.run_id)
    print_json(events)
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    result = request_json("GET", "/v1/audits/%s/result" % args.run_id)
    if args.json:
        print_json(result)
    else:
        print(result.get("markdown") or json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    result = request_json("POST", "/v1/audits/%s/cancel" % args.run_id, body={})
    stopped = isinstance(result, dict) and result.get("stopped") is True
    if stopped:
        clear_active_run(args.run_id)
    elif isinstance(result, dict):
        # A rejected queued-only cancel may carry the authoritative current state. Preserve it so
        # the next panel launch does not rediscover an obsolete queued registry entry.
        status = str(result.get("status") or "").lower()
        if status in TERMINAL_STATUSES or status == "running":
            try:
                registry = load_active_runs_registry()
                runs = registry.get("runs") if isinstance(registry, dict) else None
                current = runs.get(args.run_id) if isinstance(runs, dict) else None
                if isinstance(current, dict):
                    save_active_run({**current, "run_id": args.run_id, "status": status})
            except Exception:  # aqg: top-level boundary — CLI output must survive registry damage
                pass
    print_json(result)
    return 0 if stopped else 1


def wait_and_print_result(run_id: str, poll_s: float, *, json_output: bool = False) -> int:
    last_status = ""
    while True:
        run = request_json("GET", "/v1/audits/%s" % run_id)
        status = run.get("status")
        if status in TERMINAL_STATUSES:
            save_active_run(run)
            break
        save_active_run(run)
        if status != last_status:
            print("status: %s" % status, file=sys.stderr)
            last_status = status
        time.sleep(poll_s)
    clear_active_run(run_id)
    result = request_json("GET", "/v1/audits/%s/result" % run_id)
    if json_output:
        print_json(result)
    else:
        print(result.get("markdown") or json.dumps(result, indent=2, sort_keys=True))
    return 0 if run.get("status") == "completed" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audit")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Activate this device with the hub")
    login.add_argument("--server-url", required=True)
    login.add_argument("--activation-secret", default="")
    login.add_argument("--device-name", default="")
    login.add_argument("--device-fingerprint", default="")
    login.add_argument("--timeout-s", type=int, default=20)
    login.set_defaults(func=cmd_login)

    doctor = sub.add_parser("doctor", help="Check server and device auth")
    doctor.add_argument("--server-url", default="")
    doctor.set_defaults(func=cmd_doctor)

    whoami = sub.add_parser("whoami", help="Show the active device")
    whoami.set_defaults(func=cmd_whoami)

    submit = sub.add_parser("submit", help="Submit an audit")
    submit.add_argument("--title", default="")
    submit.add_argument("--caller", default="")
    submit.add_argument("--use", choices=("codex", "claude"), default="")
    submit.add_argument("--audit-mode", choices=("fast", "standard", "deep"), default="standard")
    # --tier is retained for backward-compat but is roster-IRRELEVANT: effort is baked into
    # each audit-mode's roster server-side. audit-mode is the sole panel knob now.
    submit.add_argument("--tier", choices=("fast", "standard", "deep", "reasoning"), default="reasoning")
    submit.add_argument("--profile", default="")
    submit.add_argument("--file", action="append")
    submit.add_argument("--text", default="")
    submit.add_argument("--type", default="")
    submit.add_argument("--context", default="")
    submit.add_argument("--focus", action="append")
    submit.add_argument("--idempotency-key", default="")
    submit.add_argument("--acknowledge-secrets", action="store_true",
                        help="Bypass server-side secret scanner (use only for fixtures/intentional examples)")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--poll-s", type=float, default=1.0)
    submit.add_argument("--timeout-s", type=int, default=60)
    submit.add_argument("--json", action="store_true")
    submit.set_defaults(func=cmd_submit)

    status = sub.add_parser("status", help="Show audit status")
    status.add_argument("run_id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    events = sub.add_parser("events", help="Show audit events")
    events.add_argument("run_id")
    events.set_defaults(func=cmd_events)

    result = sub.add_parser("result", help="Fetch audit result")
    result.add_argument("run_id")
    result.add_argument("--json", action="store_true")
    result.set_defaults(func=cmd_result)

    cancel = sub.add_parser("cancel", help="Cancel a queued audit")
    cancel.add_argument("run_id")
    cancel.set_defaults(func=cmd_cancel)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except AuditError as exc:
        print("audit: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
