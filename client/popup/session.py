"""Detached popup sessions for the shim display tools (see internal design notes, ge-db-enforced-popup §4).

The shim's ``open_ge_popup`` / ``open_db_board`` spawn a native popup as a DETACHED child
(POSIX ``setsid`` / Windows ``DETACHED_PROCESS``) and return a ``popup_id`` immediately — so no
single MCP tool call blocks past Codex's 60s ``tool_timeout`` (design C1). ``poll`` then reads the
per-popup ``result.json`` (the durable source of truth): bounded ≤ ``wait_s`` per call. Current
user-state results are idempotently re-readable until a TTL sweep; one-version legacy shared-temp
results are read-only and left to the platform temporary-file reaper. Neither is deleted on first
read or shim/host shutdown (design §4.3), so a slow user or shim restart never strands the board.

Pure over ``subprocess`` + the filesystem; every side effect is mockable, so this is unit-tested
without opening a real window (the real spawn is exercised by the existing launch smoke test).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from client import windows_security

from . import backend, launcher

try:
    from . import chat_backend  # for the GE context-bundle cap + secret-redaction (optional)
except Exception:  # aqg: import boundary — chat is optional; the bundle then carries no source_text
    chat_backend = None

# Current popups live under the user's durable state root.  The fixed shared-temp
# name is read-only compatibility for one-version legacy results and is accepted
# only when both its parent namespace and the owned private root validate.
_POPUP_ROOT = Path.home() / ".deeppattern" / "popup-sessions"
_LEGACY_POPUP_ROOT = Path(tempfile.gettempdir()) / "de-popups"
_RESULT_TTL_S = 24 * 3600
# Windows: detach the child into its own process group with no console so it survives the shim
# process (mirrors POSIX setsid) and never flashes a black console window beside the FRAMELESS
# popup. DETACHED_PROCESS(0x8) | CREATE_NEW_PROCESS_GROUP(0x200) | CREATE_NO_WINDOW(0x08000000).
# The interpreter swap to pythonw.exe (launcher._popup_python) is the primary console suppressor;
# CREATE_NO_WINDOW is the belt-and-braces guard on the python.exe fallback (matches client.runner).
_WIN_DETACHED = 0x00000008 | 0x00000200 | 0x08000000

# GE chat credentials cross the parent/child boundary exactly once through an owned anonymous
# stdin pipe.  Keep the byte and field ceilings beside the writer so a malformed caller is
# rejected before a process (or a secret-bearing pipe) exists.
_MAX_CHAT_BRIDGE_BYTES = 64 * 1024
_CHAT_BRIDGE_HANDOFF_TIMEOUT_S = 3.0
# This bounded grace is a best-effort detector for a short-lived pre-window
# exit, not a window-readiness proof. Later failures remain child/popup
# lifecycle events.
_NATIVE_SHELL_EXIT_GRACE_S = 0.25
_NATIVE_SHELL_EXIT_POLL_S = 0.005
_POPUP_READY_TIMEOUT_S = 20.0
_LINUX_QT_POPUP_READY_TIMEOUT_S = 90.0
_POPUP_READY_POLL_S = 0.05
_NATIVE_SHELL_STDERR_NAME = "popup.log"
_NATIVE_SHELL_STDERR_TAIL_BYTES = 8192
_NATIVE_SHELL_DIAGNOSTIC_CHARS = 200
_CHAT_BRIDGE_FIELDS = frozenset(
    {"schema_version", "kind", "route", "endpoint", "device_token", "run_id"}
)
_CHAT_BRIDGE_STRING_LIMITS = {
    "endpoint": 2048,
    "device_token": 8192,
    "run_id": 256,
}


def _new_popup_id() -> str:
    """An unguessable, filesystem-safe popup id (also the object capability to retrieve a board)."""
    return "pop_" + secrets.token_hex(16)


def _popup_dir_under(root: Path, popup_id: str) -> Optional[Path]:
    """Resolve a popup_id to its dir, or None for a malformed id. The shape check keeps a
    caller-supplied id from escaping _POPUP_ROOT (path traversal) even though ids are ours."""
    if not (isinstance(popup_id, str) and popup_id.startswith("pop_") and popup_id[4:].isalnum()):
        return None
    return root / popup_id


def _popup_dir(popup_id: str) -> Optional[Path]:
    return _popup_dir_under(_POPUP_ROOT, popup_id)


def _posix_uid() -> Optional[int]:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if os.name == "posix" and callable(geteuid) else None


def _validate_windows_private_data_acl(path: Path) -> bool:
    if os.name != "nt":
        return True
    try:
        windows_security.validate_private_data_acl(path)
    except (OSError, windows_security.WindowsSecurityError):
        return False
    return True


def _validate_windows_private_mutation_acl(path: Path) -> bool:
    if os.name != "nt":
        return True
    try:
        windows_security.validate_private_mutation_acl(path)
    except (OSError, windows_security.WindowsSecurityError):
        return False
    return True


def _harden_windows_private_data_acl(path: Path) -> bool:
    if os.name != "nt":
        return True
    try:
        windows_security.harden_private_data_acl(path)
    except (OSError, windows_security.WindowsSecurityError):
        return False
    return True


def _harden_windows_private_data_file_acl(path: Path) -> bool:
    if os.name != "nt":
        return True
    try:
        windows_security.harden_private_data_file_acl(path)
    except (OSError, windows_security.WindowsSecurityError):
        return False
    return True


@contextmanager
def _pin_windows_popup_directories(*paths: Path):
    """Keep checked Windows directory identities stable during path-based I/O."""
    if os.name != "nt":
        yield
        return
    try:
        with ExitStack() as stack:
            pins = [
                stack.enter_context(windows_security.PinnedWindowsDirectory(path))
                for path in paths
            ]
            for pin in pins:
                pin.validate()
            yield
            for pin in pins:
                pin.validate()
    except windows_security.WindowsSecurityError as exc:
        raise OSError("Windows popup directory identity changed") from exc


def _is_trusted_posix_popup_parent(target: Path, info: os.stat_result) -> bool:
    """Accept an owned private parent, plus the legacy root under trusted sticky temp."""
    uid = _posix_uid()
    if uid is None:
        return True
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid == uid and not mode & 0o022:
        return True
    return bool(
        target == _LEGACY_POPUP_ROOT
        and info.st_uid in {0, uid}
        and mode & stat.S_ISVTX
        and mode & 0o022
    )


def _has_unmodeled_posix_acl(path: Path) -> bool:
    """Fail closed when mode bits do not describe all ancestor access rights."""
    if sys.platform == "darwin":
        try:
            probe = subprocess.run(
                ["/bin/ls", "-lde", os.fspath(path)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=2,
                check=False,
                env={"LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if probe.returncode != 0:
            return True
        # Darwin's default home ACL is commonly a protective deny-delete ACE.
        # Any explicit allow ACE can grant rights beyond st_mode, so refuse it.
        return any(
            line.lstrip()[:1].isdigit() and " allow " in line
            for line in probe.stdout.splitlines()[1:]
        )

    listxattr = getattr(os, "listxattr", None)
    if not callable(listxattr):
        return True
    try:
        attributes = set(listxattr(path, follow_symlinks=False))
    except OSError:
        return True
    return bool(
        attributes
        & {
            "system.posix_acl_access",
            "system.posix_acl_default",
            "system.nfs4_acl",
        }
    )


def _is_trusted_posix_namespace_chain(
    path: Path, uid: int, *, allow_symlinks: bool
) -> bool:
    if not path.is_absolute():
        return False
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except OSError:
            return False
        if info.st_uid not in {0, uid}:
            return False
        if stat.S_ISLNK(info.st_mode):
            if allow_symlinks:
                continue
            return False
        if not stat.S_ISDIR(info.st_mode):
            return False
        if _has_unmodeled_posix_acl(component):
            return False
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022 and not mode & stat.S_ISVTX:
            return False
    return True


def _trusted_posix_namespace_path(path: Path) -> Optional[Path]:
    """Return one canonical ancestor path that an untrusted user cannot replace.

    Each lexical component is checked only after its parent has been shown to
    exclude untrusted mutation (or to be sticky-shared). The canonical chain is
    then checked without links, and callers use the returned canonical path for
    later operations rather than re-following the lexical links. Known POSIX,
    NFSv4, and Darwin allow ACLs fail closed because mode bits alone would not
    describe the namespace trust boundary.
    """
    uid = _posix_uid()
    if uid is None:
        return path
    if not _is_trusted_posix_namespace_chain(path, uid, allow_symlinks=True):
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not _is_trusted_posix_namespace_chain(resolved, uid, allow_symlinks=False):
        return None
    return resolved


def _is_private_popup_directory(path: Path) -> bool:
    """Verify one popup directory without following links or reparse points."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    # Windows junctions/reparse points are not always exposed as symlinks.
    if getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    if not _validate_windows_private_data_acl(path):
        return False
    uid = _posix_uid()
    if uid is not None:
        if info.st_uid != uid:
            return False
        if stat.S_IMODE(info.st_mode) & 0o077:
            return False
        if _has_unmodeled_posix_acl(path):
            return False
    return True


def _ensure_private_popup_root(
    *,
    create: bool = True,
    root: Optional[Path] = None,
) -> bool:
    """Fail closed unless the user-state popup root is a private real directory.

    POSIX enforces current ownership and 0700-style mode bits.  Windows rejects
    reparse points and installs a protected owner/System/Admin inheritable DACL.
    """
    configured_target = _POPUP_ROOT if root is None else root
    try:
        try:
            configured_info = configured_target.lstat()
            configured_existed = True
        except FileNotFoundError:
            configured_existed = False
            configured_info = None
        if configured_existed and (
            not stat.S_ISDIR(configured_info.st_mode)
            or getattr(configured_info, "st_file_attributes", 0) & 0x400
        ):
            return False
        if not configured_existed and not create:
            return False
        if create:
            configured_target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with _pin_windows_popup_directories(configured_target.parent):
            trusted_parent = _trusted_posix_namespace_path(configured_target.parent)
            if trusted_parent is None:
                return False
            target = trusted_parent / configured_target.name
            try:
                info = target.lstat()
                existed = True
            except FileNotFoundError:
                existed = False
                info = None
            if existed and (
                not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                return False
            if not existed and not create:
                return False
            legacy_read_only = bool(
                existed and not create and configured_target == _LEGACY_POPUP_ROOT
            )
            parent_info = trusted_parent.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or getattr(parent_info, "st_file_attributes", 0) & 0x400
                or not _is_trusted_posix_popup_parent(configured_target, parent_info)
                or (
                    not legacy_read_only
                    and not _validate_windows_private_mutation_acl(trusted_parent)
                )
            ):
                return False
            if existed:
                # Never repair a pre-existing root by path. It may have an untrusted
                # owner or a retained attacker handle from before this process.
                return _is_private_popup_directory(target)
            try:
                target.mkdir(mode=0o700)
            except FileExistsError:
                # A trusted concurrent creator may win after the lstat.  Preserve
                # fail-closed semantics by accepting only the now-safe real root.
                return _is_private_popup_directory(target)
            if not _harden_windows_private_data_acl(target):
                try:
                    target.rmdir()
                except OSError:
                    pass
                return False
            return _is_private_popup_directory(target)
    except OSError:
        return False


def _trusted_private_popup_root(root: Path) -> Optional[Path]:
    """Return the verified operational root without re-following lexical links."""
    trusted_parent = _trusted_posix_namespace_path(root.parent)
    if trusted_parent is None:
        return None
    candidate = trusted_parent / root.name
    return candidate if _is_private_popup_directory(candidate) else None


def _find_private_popup_workdir(popup_id: str) -> Optional[Path]:
    """Find a current or one-version legacy popup without trusting an unsafe root."""
    if _popup_dir_under(_POPUP_ROOT, popup_id) is None:
        return None
    for root in (_POPUP_ROOT, _LEGACY_POPUP_ROOT):
        if not _ensure_private_popup_root(create=False, root=root):
            continue
        trusted_root = _trusted_private_popup_root(root)
        if trusted_root is None:
            continue
        workdir = _popup_dir_under(trusted_root, popup_id)
        if workdir is None:
            return None
        if _is_private_popup_directory(workdir):
            return workdir
    return None


def _remove_private_workdir(workdir: Path) -> bool:
    """Remove only a still-verified popup leaf; never follow a replaced path."""
    if not _is_private_popup_directory(workdir):
        return False
    shutil.rmtree(workdir, ignore_errors=True)
    try:
        workdir.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _detach_kwargs() -> Dict[str, Any]:
    """Spawn flags that detach the popup from the shim's process group (design C2)."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": _WIN_DETACHED}


def _new_cursor_bridge_thread(target) -> threading.Thread:
    return threading.Thread(
        target=target,
        name="de-cursor-board-bridge",
        daemon=True,
    )


def _start_bridge_feeder(
    process: subprocess.Popen, bridge_bytes: bytes
) -> bool:
    """Feed the owned popup pipe without making the MCP display call wait on pipe capacity."""

    def feed() -> None:
        pipe = process.stdin
        if pipe is None:
            return
        try:
            pipe.write(bridge_bytes)
        except (OSError, ValueError):
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    thread = _new_cursor_bridge_thread(feed)
    try:
        thread.start()
    except (RuntimeError, OSError):
        pipe = process.stdin
        if pipe is not None:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        return False
    return True


def _validate_chat_bridge_envelope(value: Any) -> Dict[str, Any]:
    """Return a shallow validated GE server envelope without formatting sensitive values."""
    if not isinstance(value, dict) or set(value) != _CHAT_BRIDGE_FIELDS:
        raise ValueError("chat bridge shape")
    if (
        isinstance(value.get("schema_version"), bool)
        or value.get("schema_version") != 1
        or value.get("kind") != "ge-chat"
        or value.get("route") != "server"
    ):
        raise ValueError("chat bridge schema")
    for field, limit in _CHAT_BRIDGE_STRING_LIMITS.items():
        item = value.get(field)
        if (
            not isinstance(item, str)
            or not item
            or any(ord(char) < 0x20 for char in item)
            or len(item.encode("utf-8")) > limit
        ):
            raise ValueError("chat bridge field")
    return dict(value)


def _encode_chat_bridge_envelope(value: Any) -> bytes:
    checked = _validate_chat_bridge_envelope(value)
    rendered = json.dumps(
        checked,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if not rendered or len(rendered) > _MAX_CHAT_BRIDGE_BYTES:
        raise ValueError("chat bridge size")
    return rendered


def _terminate_spawned_process(process: subprocess.Popen) -> None:
    """Best-effort bounded teardown of a child whose private bridge did not complete."""
    try:
        process.terminate()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=1.0)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=1.0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass


def _early_native_shell_returncode(
    process: subprocess.Popen, *, timeout_s: Optional[float] = None
) -> Optional[int]:
    """Spend a bounded startup grace observing a short-lived native-shell exit."""
    wait_s = _NATIVE_SHELL_EXIT_GRACE_S if timeout_s is None else max(0.0, timeout_s)
    deadline = time.monotonic() + wait_s
    while True:
        returncode = process.poll()
        if returncode is not None:
            return returncode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(_NATIVE_SHELL_EXIT_POLL_S, remaining))


def _native_shell_exit_response(
    workdir: Path, popup_id: str, returncode: int
) -> Dict[str, Any]:
    """Preserve any already-written terminal result; fail an exit without one."""
    terminal = _read_result(workdir / "result.json")
    if terminal is not None and terminal.get("outcome") in {
        "committed",
        "dismissed",
    }:
        return {"status": "open", "popup_id": popup_id}
    detail = _native_shell_stderr_detail(workdir)
    _append_ready_diagnostic(
        workdir.parent,
        popup_id,
        reason="native-shell-exited",
        detail=detail,
        returncode=returncode,
    )
    _remove_private_workdir(workdir)
    return {
        "status": "failed",
        "reason": "native-shell-exited",
        "returncode": returncode,
    }


def _open_native_shell_stderr(workdir: Path):
    """Create the child stderr target inside its already-private popup directory."""
    path = workdir / _NATIVE_SHELL_STDERR_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        stream = os.fdopen(descriptor, "wb", buffering=0)
    except (OSError, ValueError):
        os.close(descriptor)
        raise
    if not _harden_windows_private_data_file_acl(path):
        stream.close()
        try:
            path.unlink()
        except OSError:
            pass
        raise OSError("native shell diagnostic ACL could not be secured")
    return stream


def _native_shell_stderr_detail(workdir: Path) -> str:
    """Return a bounded, redacted tail suitable for the privacy-safe summary log."""
    path = workdir / _NATIVE_SHELL_STDERR_NAME
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _NATIVE_SHELL_STDERR_TAIL_BYTES))
            raw = handle.read(_NATIVE_SHELL_STDERR_TAIL_BYTES)
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if chat_backend is not None:
        text = chat_backend.redact_secrets(text)
    text = " ".join(
        part.strip()
        for part in text.splitlines()
        if part.strip()
    )
    text = "".join(char if ord(char) >= 0x20 else " " for char in text)
    return text[-_NATIVE_SHELL_DIAGNOSTIC_CHARS:]


def _popup_ready_timeout_s() -> float:
    """Allow the heavier Linux Qt/WebEngine process to realise its first window."""
    if (
        sys.platform.startswith("linux")
        and os.environ.get("PYWEBVIEW_GUI") == "qt"
    ):
        return _LINUX_QT_POPUP_READY_TIMEOUT_S
    return _POPUP_READY_TIMEOUT_S


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _append_ready_diagnostic(
    popup_root: Path,
    popup_id: str,
    *,
    reason: str,
    detail: str = "",
    returncode: Optional[int] = None,
) -> None:
    """Append a privacy-safe startup diagnostic under the private popup root."""
    try:
        payload: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "popup_id": popup_id,
            "reason": reason,
        }
        if detail:
            payload["detail"] = detail[:200]
        if returncode is not None:
            payload["returncode"] = returncode
        with open(popup_root / "popup-ready.log", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:  # aqg: top-level boundary — diagnostics must never block popup launch
        pass


def _wait_for_popup_ready(
    process: subprocess.Popen,
    workdir: Path,
    popup_id: str,
    *,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Wait for the child to prove pywebview loaded and the DOM is reachable."""
    if timeout_s is None:
        timeout_s = _popup_ready_timeout_s()
    ready_path = workdir / "ready.json"
    diagnostic_path = workdir / "ready-diagnostic.json"
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        ready = _read_json_file(ready_path)
        if ready is not None and ready.get("ok") is True:
            return {"status": "open", "popup_id": popup_id}
        terminal = _read_result(workdir / "result.json")
        if terminal is not None and terminal.get("outcome") in {"committed", "dismissed"}:
            return {"status": "open", "popup_id": popup_id}
        returncode = process.poll()
        if returncode is not None:
            return _native_shell_exit_response(workdir, popup_id, returncode)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            child_diagnostic = _read_json_file(diagnostic_path) or {}
            reason = str(child_diagnostic.get("reason") or "ready-timeout")
            detail = str(child_diagnostic.get("detail") or "")
            if not detail:
                detail = _native_shell_stderr_detail(workdir)
            _append_ready_diagnostic(
                workdir.parent,
                popup_id,
                reason=reason,
                detail=detail,
                returncode=process.poll(),
            )
            _terminate_spawned_process(process)
            _remove_private_workdir(workdir)
            return {"status": "failed", "reason": "popup-not-ready"}
        time.sleep(min(_POPUP_READY_POLL_S, remaining))


def _handoff_chat_bridge(
    process: subprocess.Popen,
    bridge_bytes: bytes,
    *,
    timeout_s: Optional[float] = None,
) -> tuple[bool, Optional[int]]:
    """Synchronously write/flush/close one GE envelope before the display call returns.

    A helper thread makes the wait bounded even if an unexpected child never drains its pipe;
    success is still synchronous because this function does not return until the writer finished.
    """
    pipe = process.stdin
    if pipe is None:
        returncode = _early_native_shell_returncode(process)
        if returncode is None:
            _terminate_spawned_process(process)
        return False, returncode
    finished = threading.Event()
    outcome = {"ok": False, "returncode": None}

    def feed() -> None:
        ok = False
        try:
            written = pipe.write(bridge_bytes)
            if written != len(bridge_bytes):
                raise OSError("short bridge write")
            pipe.flush()
            ok = True
        except (OSError, ValueError, TypeError):
            ok = False
            outcome["returncode"] = process.poll()
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                ok = False
            outcome["ok"] = ok
            finished.set()

    try:
        threading.Thread(
            target=feed,
            name="de-ge-chat-bridge",
            daemon=True,
        ).start()
    except (RuntimeError, OSError):
        returncode = _early_native_shell_returncode(process)
        try:
            pipe.close()
        except (OSError, ValueError):
            pass
        if returncode is None:
            _terminate_spawned_process(process)
        return False, returncode

    wait_s = _CHAT_BRIDGE_HANDOFF_TIMEOUT_S if timeout_s is None else max(0.0, timeout_s)
    if not finished.wait(timeout=wait_s) or not outcome["ok"]:
        # The writer owns the pipe close.  Closing a buffered pipe here can block on the
        # writer's internal lock, defeating the timeout that brought us here.  Terminating
        # the child closes its read end, which releases a real blocked write with EPIPE; the
        # daemon writer then closes the parent end in its ``finally`` block.
        returncode = outcome["returncode"]
        if returncode is None:
            # BrokenPipe can precede waitpid visibility by a scheduler tick.
            # Re-observe before terminating so a native exit keeps its real
            # return code instead of being mislabeled as a bridge failure.
            returncode = _early_native_shell_returncode(process)
        if returncode is None:
            _terminate_spawned_process(process)
        return False, returncode
    return True, None


def build_ge_context_bundle(raw_context: Any, title: Any, artifact_kind: Any,
                            caller: Optional[str] = None) -> Dict[str, Any]:
    """Build the GE follow-up-chat context bundle (written to ``--context``): ``{title, mode, [caller],
    [source_text]}``. The conversation ``raw_context`` becomes ``source_text`` — CAPPED
    (``chat_backend._MAX_SOURCE_CHARS``) and SECRET-REDACTED (key shapes + PEM, via
    ``chat_backend.redact_secrets`` — NOT vendor names, which are legitimate ground truth) before it can be
    written to disk / fed to the local LLM (design §5). A non-str / empty / all-redacted context is dropped
    so the bundle stays light. ``caller`` is omitted unless given (ChatSession defaults to claude)."""
    mode = "diagram" if artifact_kind == "svg" else "image"
    bundle: Dict[str, Any] = {"title": title if isinstance(title, str) else "", "mode": mode}
    if caller:
        bundle["caller"] = caller
    if chat_backend is not None and isinstance(raw_context, str) and raw_context.strip():
        # REDACT the FULL context FIRST, then cap. Capping first would truncate a secret that straddles
        # the _MAX_SOURCE_CHARS boundary — stripping its terminating marker (e.g. `-----END … KEY-----`)
        # so the regex no longer matches the surviving prefix, leaking a partial credential (audit
        # bcbb0346 convergent 4/4). Redacting the whole string first closes that boundary hole.
        redacted = chat_backend.redact_secrets(raw_context)
        capped = redacted[:chat_backend._MAX_SOURCE_CHARS]
        if capped and capped.strip():
            bundle["source_text"] = capped
    return bundle


def spawn(html_body: str, title: str, *, python: Optional[str] = None,
          chat_context: Optional[Dict[str, Any]] = None,
          chat_bootstrap: Optional[Dict[str, Any]] = None,
          api_profile: str = "legacy",
          initial_state: Optional[Dict[str, Any]] = None,
          forbidden_token: Optional[str] = None) -> Dict[str, Any]:
    """Spawn a detached popup showing ``html_body``; return ``{status:'open', popup_id}`` or a
    structured failure. QUICK-RETURN: never waits for the window. A missing native backend yields
    ``no-webview-backend`` (the shim surfaces-and-stops — never a browser fallback).

    ``chat_context`` (GE only) is written into the popup dir as ``chat_context.json`` and passed to the
    shell via ``--context`` so the popup wires a follow-up ChatSession. native_shell deletes that file
    right after reading it (it can hold the user's source_text)."""
    python = python or sys.executable
    cursor_bridge_bytes = None
    chat_bridge_bytes = None
    if chat_bootstrap is not None:
        if chat_context is not None or initial_state is not None or forbidden_token is not None:
            return {"status": "failed", "reason": "bridge-state-invalid"}
        try:
            chat_bridge_bytes = _encode_chat_bridge_envelope(chat_bootstrap)
        except (TypeError, ValueError, UnicodeError):
            return {"status": "failed", "reason": "bridge-state-invalid"}
    if initial_state is not None or forbidden_token is not None:
        if (
            api_profile != "cursor-db"
            or not isinstance(initial_state, dict)
            or not isinstance(forbidden_token, str)
            or not forbidden_token
        ):
            return {"status": "failed", "reason": "bridge-state-invalid"}
        try:
            launcher._validate_cursor_board_value(
                initial_state, token=forbidden_token
            )
            cursor_bridge_bytes = json.dumps(
                {
                    "initial_state": initial_state,
                    "forbidden_token": forbidden_token,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, launcher.BoardFetchError):
            return {"status": "failed", "reason": "bridge-state-invalid"}
        if len(cursor_bridge_bytes) > launcher._MAX_CURSOR_BOARD_JSON_BYTES + 4096:
            return {"status": "failed", "reason": "bridge-state-invalid"}
    if not backend.ensure_webview(python, "de-popup"):
        return {"status": "failed", "reason": "no-webview-backend"}
    if not _ensure_private_popup_root():
        return {"status": "failed", "reason": "workdir-failed"}
    popup_root = _trusted_private_popup_root(_POPUP_ROOT)
    if popup_root is None:
        return {"status": "failed", "reason": "workdir-failed"}
    popup_id = _new_popup_id()
    workdir = popup_root / popup_id
    workdir_created = False
    try:
        with _pin_windows_popup_directories(popup_root):
            if not _is_private_popup_directory(popup_root):
                raise OSError("unsafe popup root")
            # mode=0o700 on the mkdir itself → the dir is NEVER briefly group/world-readable (the
            # artifact/board can be sensitive); a fresh unguessable leaf under a 0700 root.
            workdir.mkdir(mode=0o700)
            workdir_created = True
            with _pin_windows_popup_directories(workdir):
                if not _is_private_popup_directory(workdir):
                    # This leaf was just created and is still empty.  rmdir never follows
                    # a replacement symlink and avoids leaving an unsweepable failed leaf.
                    try:
                        workdir.rmdir()
                    except OSError:
                        pass
                    raise OSError("unsafe popup workdir")
                (workdir / "popup.html").write_text(html_body, encoding="utf-8")
                chat_context_path: Optional[str] = None
                if chat_context is not None:
                    cc_path = workdir / "chat_context.json"
                    cc_path.write_text(
                        json.dumps(chat_context, ensure_ascii=False), encoding="utf-8"
                    )
                    chat_context_path = str(cc_path)
    except (OSError, TypeError, ValueError):
        if workdir_created:
            _remove_private_workdir(workdir)
        return {"status": "failed", "reason": "workdir-failed"}   # detail → stderr-only, never model-facing
    # on_close_dismiss=True: this is the DETACHED poll path — a bare OS-chrome close must land a terminal
    # `dismissed` in result.json so `poll` returns a terminal state instead of hanging `open` until the TTL
    # sweep (design §4.2). (The blocking `launcher.open_popup` path leaves it off — its OS-close = `closed`.)
    command_kwargs = {
        "chat_context_path": chat_context_path,
        "on_close_dismiss": True,
        "api_profile": api_profile,
        "bridge_state_stdin": cursor_bridge_bytes is not None,
        "ready_path": str(workdir / "ready.json"),
    }
    if chat_bridge_bytes is not None:
        # Frozen integration seam owned by launcher.py's work package.  Do not synthesize argv here.
        command_kwargs["chat_bridge_stdin"] = True
    try:
        cmd = launcher.build_shell_command(
            launcher._popup_python(python),
            str(workdir / "popup.html"),
            title,
            str(workdir / "result.json"),
            **command_kwargs,
        )
    except (TypeError, ValueError):
        _remove_private_workdir(workdir)
        return {"status": "failed", "reason": "launch-failed"}
    try:
        # stdin=DEVNULL: the detached child MUST NOT inherit the shim's stdin (the MCP JSON-RPC
        # pipe) or a GUI toolkit reading stdin would steal host messages.
        stderr_stream = _open_native_shell_stderr(workdir)
        popen_kwargs = {
            "stdin": (
                subprocess.PIPE
                if cursor_bridge_bytes is not None or chat_bridge_bytes is not None
                else subprocess.DEVNULL
            ),
            "stdout": subprocess.DEVNULL,
            "stderr": stderr_stream,
            **_detach_kwargs(),
        }
        if api_profile in ("cursor-ge", "cursor-db"):
            child_env = os.environ.copy()
            child_env["WEBVIEW2_USER_DATA_FOLDER"] = str(workdir / "webview2-data")
            popen_kwargs["env"] = child_env
        try:
            process = subprocess.Popen(cmd, **popen_kwargs)
        finally:
            stderr_stream.close()
        if chat_bridge_bytes is not None:
            # GE delivery plus the post-handoff grace provide the bounded startup
            # observation; retain a zero-wait check so a process already known
            # dead is never written to.
            returncode = process.poll()
            if returncode is not None:
                return _native_shell_exit_response(workdir, popup_id, returncode)
            handoff_ok, handoff_returncode = _handoff_chat_bridge(
                process, chat_bridge_bytes
            )
            if not handoff_ok:
                if handoff_returncode is not None:
                    return _native_shell_exit_response(
                        workdir, popup_id, handoff_returncode
                    )
                _remove_private_workdir(workdir)
                return {"status": "failed", "reason": "bridge-handoff-failed"}
            returncode = _early_native_shell_returncode(process)
            if returncode is not None:
                return _native_shell_exit_response(workdir, popup_id, returncode)
        elif cursor_bridge_bytes is not None:
            returncode = process.poll()
            if returncode is not None:
                return _native_shell_exit_response(workdir, popup_id, returncode)
            if process.stdin is None:
                _terminate_spawned_process(process)
                _remove_private_workdir(workdir)
                return {"status": "failed", "reason": "bridge-handoff-failed"}
            if not _start_bridge_feeder(process, cursor_bridge_bytes):
                _terminate_spawned_process(process)
                _remove_private_workdir(workdir)
                return {"status": "failed", "reason": "bridge-handoff-failed"}
            returncode = _early_native_shell_returncode(process)
            if returncode is not None:
                return _native_shell_exit_response(workdir, popup_id, returncode)
        else:
            returncode = _early_native_shell_returncode(process)
            if returncode is not None:
                return _native_shell_exit_response(workdir, popup_id, returncode)
    except OSError:
        _remove_private_workdir(workdir)   # no orphan workdir on a launch failure
        return {"status": "failed", "reason": "launch-failed"}
    return _wait_for_popup_ready(process, workdir, popup_id)


def _read_result(result_path: Path) -> Optional[Dict[str, Any]]:
    """The popup's atomically-written result.json, or None if not yet written / unreadable."""
    try:
        loaded = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _poll_popup_workdir(
    workdir: Path, wait_s: float, interval: float
) -> Dict[str, Any]:
    result_path = workdir / "result.json"
    # hard ceiling < Codex's 60s tool_timeout, inside poll too (defense-in-depth — not only the
    # shim caller), so no future caller can block a thread long enough to trip the timeout.
    wait_s = max(0.0, min(float(wait_s), 55.0))
    deadline = time.monotonic() + wait_s
    while True:
        data = _read_result(result_path)
        if data is not None:
            outcome = data.get("outcome")
            if outcome == "committed":
                return {"status": "done", "result": data.get("result")}
            if outcome == "dismissed":
                return {"status": "dismissed"}
            # any other/partial shape is NOT a terminal state → keep waiting (never strand as dismissed)
        if time.monotonic() >= deadline:
            return {"status": "open"}
        time.sleep(interval)


def poll(popup_id: str, wait_s: float = 50.0, _interval: float = 0.25) -> Dict[str, Any]:
    """BOUNDED poll of a popup's result (≤ ``wait_s``, < Codex's 60s tool_timeout). Returns:
      ``{status:'open'}``               — not yet submitted/dismissed (the caller polls again)
      ``{status:'done', result}``       — user Submitted (the edited board / commit payload)
      ``{status:'dismissed'}``          — user dismissed or closed the window
      ``{status:'unknown'}``            — no such popup (bad / expired id)
    Terminal results are re-readable until the TTL sweep (never consumed on read), so a retry or a
    later re-check returns the same board (design §4.3 — closes the v2 'stranded result')."""
    workdir = _find_private_popup_workdir(popup_id)
    if workdir is None:
        return {"status": "unknown"}
    try:
        with _pin_windows_popup_directories(workdir.parent, workdir):
            if not _is_private_popup_directory(
                workdir.parent
            ) or not _is_private_popup_directory(workdir):
                return {"status": "unknown"}
            return _poll_popup_workdir(workdir, wait_s, _interval)
    except OSError:
        return {"status": "unknown"}


def _sweep_verified_popup_root(root: Path, now: float) -> int:
    removed = 0
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if (
                _is_private_popup_directory(entry)
                and (now - entry.stat().st_mtime) > _RESULT_TTL_S
                and _remove_private_workdir(entry)
            ):
                removed += 1
        except OSError:
            continue
    return removed


def _sweep_popup_root(root: Path, now: float) -> int:
    if not _ensure_private_popup_root(create=False, root=root):
        return 0
    trusted_root = _trusted_private_popup_root(root)
    if trusted_root is None:
        return 0
    try:
        with _pin_windows_popup_directories(trusted_root):
            if not _is_private_popup_directory(trusted_root):
                return 0
            return _sweep_verified_popup_root(trusted_root, now)
    except OSError:
        return 0


def sweep(now: Optional[float] = None) -> int:
    """TTL cleanup for current user-state popups older than ``_RESULT_TTL_S``.

    Cleanup happens only here, by age — never on first read or shim/host shutdown — so current
    results survive a restart. One-version legacy shared-temp results remain read-only and outside
    client TTL cleanup. Returns confirmed current-root removals; failures are skipped.
    """
    now = time.time() if now is None else now
    # The legacy shared-temp root is one-version, read-only compatibility.
    # Polling may read validated terminal results there; client cleanup never
    # mutates it, and retired data is left to the platform temporary-file reaper.
    return _sweep_popup_root(_POPUP_ROOT, now)
