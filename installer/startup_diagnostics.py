"""Best-effort, privacy-safe diagnostics for MCP startup and initialize.

The MCP client owns stderr, so a launcher that exits before replying to
``initialize`` otherwise leaves only a generic "connection closed" message.
This module records a small phase journal below the managed runtime directory.
It deliberately accepts only fixed, non-secret labels; paths, endpoints,
tokens, request bodies, and exception messages never enter the log.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Optional

from . import update_coordination


STARTUP_LOG_RELATIVE_PATH = Path(".runtime") / "logs" / "mcp-startup.jsonl"
STARTUP_LOG_LOCK_RELATIVE_PATH = (
    Path(".runtime") / "locks" / "mcp-startup-diagnostics.lock"
)
MAX_STARTUP_LOG_BYTES = 512 * 1024
_MAX_ENTRY_BYTES = 2048
SESSION_ID = uuid.uuid4().hex

_SAFE_EVENTS = frozenset(
    {
        "launcher_started",
        "config_hardening_started",
        "config_hardening_completed",
        "startup_gate_entered",
        "startup_gate_acquired",
        "recovery_completed",
        "update_started",
        "update_completed",
        "update_refused",
        "shim_admission_started",
        "shim_candidate_verified",
        "forwarder_selected",
        "stdio_loop_ready",
        "initialize_received",
        "initialize_response_emitted",
        "launcher_failed",
        "launcher_exited",
        "handoff_started",
    }
)
_SAFE_PHASES = frozenset(
    {
        "launcher",
        "config",
        "startup_gate",
        "recovery",
        "update",
        "shim_admission",
        "stdio",
        "initialize",
        "handoff",
    }
)
_SAFE_OUTCOMES = frozenset(
    {"started", "ready", "success", "error", "skipped", "refused", "clean_exit"}
)
_SAFE_FORWARDERS = frozenset({"hosted", "lite", "offline", "unknown"})
_SAFE_REASON_CODES = frozenset(
    {
        "activation_required",
        "config_missing",
        "missing_endpoint_and_token",
        "missing_access_token",
        "startup_gate_timeout",
        "shell_error",
        "os_error",
        "value_error",
        "unexpected_error",
        "initialize_forward_error",
        "empty_initialize_response",
        "none",
    }
)


def _safe_label(value: Optional[str], allowed: frozenset[str]) -> Optional[str]:
    if value is None:
        return None
    return value if value in allowed else "unknown"


def _safe_host(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    # Shared launcher infrastructure must not own product identifiers. Presence
    # is enough to distinguish an explicitly registered host from legacy
    # discovery, while avoiding both arbitrary environment text and a duplicate
    # host allowlist outside the reviewed registry boundary.
    return "provided" if normalized else None


def _timestamp() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".%03dZ" % int(
        (now % 1) * 1000
    )


def classify_exception(exc: BaseException) -> str:
    """Return a stable class label without persisting the exception message."""

    if isinstance(exc, update_coordination.InstallTransactionBusy):
        return "startup_gate_timeout"
    name = type(exc).__name__
    if name == "ActivationRequiredError":
        return "activation_required"
    if isinstance(exc, OSError):
        return "os_error"
    if isinstance(exc, ValueError):
        return "value_error"
    # Importing config here would widen the startup graph. ShellError and its
    # subclasses are identified by their stable MRO name instead.
    if any(base.__name__ == "ShellError" for base in type(exc).__mro__):
        return "shell_error"
    return "unexpected_error"


def _open_log(path: Path) -> int:
    if update_coordination._is_link_like(path):
        raise OSError("startup diagnostic path is linked")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("startup diagnostic is not a private regular file")
        if os.name != "nt":
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise OSError("startup diagnostic has the wrong owner")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise OSError("startup diagnostic permissions are too broad")
        elif update_coordination._is_link_like(path):
            raise OSError("startup diagnostic became linked")
        return fd
    except Exception:
        os.close(fd)
        raise


def append_event(
    root: Path,
    event: str,
    phase: str,
    *,
    client_host: Optional[str] = None,
    elapsed_ms: Optional[int] = None,
    attempt: Optional[int] = None,
    forwarder: Optional[str] = None,
    outcome: Optional[str] = None,
    reason_code: Optional[str] = None,
    error_type: Optional[str] = None,
) -> None:
    """Append one bounded event; diagnostics must never gate MCP availability."""

    try:
        safe_event = _safe_label(event, _SAFE_EVENTS)
        safe_phase = _safe_label(phase, _SAFE_PHASES)
        if safe_event == "unknown" or safe_phase == "unknown":
            return
        entry = {
            "schema_version": 1,
            "timestamp": _timestamp(),
            "session_id": SESSION_ID,
            "pid": os.getpid(),
            "event": safe_event,
            "phase": safe_phase,
        }
        host = _safe_host(client_host)
        if host is not None:
            entry["client_host"] = host
        if isinstance(elapsed_ms, int):
            entry["elapsed_ms"] = max(0, min(elapsed_ms, 24 * 60 * 60 * 1000))
        if isinstance(attempt, int):
            entry["attempt"] = max(1, min(attempt, 9))
        if forwarder is not None:
            entry["forwarder"] = _safe_label(forwarder, _SAFE_FORWARDERS)
        if outcome is not None:
            entry["outcome"] = _safe_label(outcome, _SAFE_OUTCOMES)
        if reason_code is not None:
            entry["reason_code"] = _safe_label(reason_code, _SAFE_REASON_CODES)
        if isinstance(error_type, str) and error_type:
            # Class names are useful for diagnosis but remain bounded and contain
            # no exception message or arbitrary server-provided text.
            entry["error_type"] = error_type[:80] if error_type.isidentifier() else "other"

        rendered = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(rendered) > _MAX_ENTRY_BYTES:
            return

        canonical = update_coordination._canonical_root(Path(root))
        log_path = canonical / STARTUP_LOG_RELATIVE_PATH
        lock_path = canonical / STARTUP_LOG_LOCK_RELATIVE_PATH
        update_coordination._ensure_private_directory(log_path.parent)
        update_coordination._ensure_private_directory(lock_path.parent)
        lock_fd = update_coordination._open_lock_file(
            lock_path, initialize_lock_byte=True
        )
        locked = False
        try:
            update_coordination._acquire_until(lock_fd, 0.05)
            locked = True
            log_fd = _open_log(log_path)
            try:
                if os.fstat(log_fd).st_size + len(rendered) > MAX_STARTUP_LOG_BYTES:
                    os.ftruncate(log_fd, 0)
                os.write(log_fd, rendered)
            finally:
                os.close(log_fd)
        finally:
            if locked:
                update_coordination._unlock(lock_fd)
            os.close(lock_fd)
    except Exception:
        # Diagnostics are evidence only. A full disk, ACL refusal, competing
        # process, or malformed path must not become a new startup failure.
        return
