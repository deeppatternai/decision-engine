"""Bounded Win32 evidence collector for the conditional Cursor display policy.

The runtime collector never enables a host by itself.  It produces immutable
evidence for ``client_host_runtime.evaluate_cursor_windows_preflight`` and a
terminal-process lease for the call-time visible-window check.  Native DLL loading is
lazy, so non-Windows M1 transports remain unaffected at RUNTIME — but this module itself
is imported eagerly by ``installer.shim`` on every platform, so everything at module scope
here (structs, GUID parsing) must be portable.  It once was not, and the shim stopped
importing on macOS; see ``_GUID``.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
import threading
import time
import uuid
from ctypes import wintypes
from datetime import date
from pathlib import PureWindowsPath
from typing import Any, Dict, Mapping, Optional, Tuple

from .client_host_runtime import (
    CursorProcessEvidence,
    CursorWindowsEvidence,
    CursorWindowsFixture,
    VolatileResult,
)


_REMOTE_MARKERS = (
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "SSH_TTY",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
    "VSCODE_REMOTE_NAME",
    "VSCODE_REMOTE_CONTAINERS_SESSION",
    "REMOTE_CONTAINERS",
    "REMOTE_CONTAINERS_IPC",
    "CODESPACES",
    "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN",
)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_TH32CS_SNAPPROCESS = 0x00000002
_WAIT_TIMEOUT = 0x00000102
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_DRIVE_FIXED = 3
_PROCESS_COMMAND_LINE_INFORMATION = 60
_DESKTOP_READOBJECTS = 0x0001
_GW_OWNER = 4
_DWMWA_CLOAKED = 14
_MAX_COMMAND_LINE_BYTES = 64 * 1024
_MAX_VERSION_BYTES = 1024 * 1024


class _FILETIME(ctypes.Structure):
    _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    )


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = (
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    )


class _GUID(ctypes.Structure):
    """A Win32 GUID, spelled in FIXED-WIDTH types so it is 16 bytes on every platform.

    Deliberately not ``wintypes.DWORD``/``WORD`` like the structs above: off Windows
    ``DWORD`` aliases ``c_ulong``, 8 bytes on 64-bit macOS and Linux (``WORD`` is
    ``c_ushort`` and stays 2 everywhere — Data1 alone, plus alignment, is what did it).
    This struct therefore measured 24 bytes, the module-level ``parse`` calls below raised
    ``ValueError: Buffer size too small (16 instead of at least 24 bytes)``, and that took
    ``installer.shim`` — and therefore the whole MCP server — down at import time on macOS
    (v0.2.29 through v0.2.31). ``c_uint32``/``c_uint16`` are byte-identical to the Windows
    definition, so Windows behaviour is unchanged.

    The neighbouring wintypes structs are left alone because nothing instantiates them at
    import and they are only used inside Windows-only call paths. That is an invariant, not
    a hope: ``test_no_module_scope_struct_uses_platform_variable_field_types`` walks this
    file's module-scope statements and fails if any struct reached from there carries a
    field type whose width depends on the platform.
    """

    _fields_ = (
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def parse(cls, value: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


_KNOWN_FOLDER_GUIDS = {
    "LocalAppData": _GUID.parse("F1B32785-6FBA-4FCF-9D55-7B8E7F157091"),
    "ProgramFiles": _GUID.parse("905E63B6-C1BF-494E-B29C-65B732D3D21A"),
    "ProgramFilesX64": _GUID.parse("6D809377-6AF0-444B-8957-A3773F02200E"),
}


def _filetime_value(value: _FILETIME) -> int:
    return (int(value.high) << 32) | int(value.low)


def _deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise TimeoutError("Cursor native probe deadline exceeded")


class _OwnedHandle:
    def __init__(self, value: int, close_handle, pid: int) -> None:
        self.value = value
        self.pid = pid
        self._close_handle = close_handle
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._close_handle(self.value)


class CursorTerminalLease:
    """The one process handle retained by an eligible Cursor transport."""

    def __init__(
        self,
        handle: Any,
        api: Any,
        fixture: CursorWindowsFixture,
        session_id: int,
    ) -> None:
        self.handle = handle
        self._api = api
        self._fixture = fixture
        self._session_id = session_id
        self._closed = False
        self._check_inflight = False
        self._lock = threading.Lock()

    def check_display(self) -> VolatileResult:
        with self._lock:
            if self._closed:
                return VolatileResult.revoked("terminal-exit")
            if date.today() > self._fixture.expires_on:
                return VolatileResult.revoked("fixture-expired")
            if self._check_inflight:
                return VolatileResult.unavailable()
            self._check_inflight = True

        done = threading.Event()
        publication: Dict[str, VolatileResult] = {}
        deadline = time.monotonic() + 0.15

        def run_check() -> None:
            try:
                observed = self._api.volatile_check(
                    self.handle,
                    self._fixture,
                    self._session_id,
                    deadline,
                )
                if not isinstance(observed, VolatileResult):
                    observed = VolatileResult.unavailable()
            except BaseException:  # aqg: top-level boundary -- native failure denies display
                observed = VolatileResult.unavailable()

            close_after = False
            with self._lock:
                self._check_inflight = False
                if self._closed:
                    observed = VolatileResult.revoked("terminal-exit")
                    close_after = True
                publication["value"] = observed
            try:
                if close_after:
                    self.handle.close()
            except BaseException:  # aqg: top-level boundary -- cleanup cannot grant
                with self._lock:
                    publication["value"] = VolatileResult.unavailable()
            finally:
                done.set()

        try:
            threading.Thread(
                target=run_check,
                name="de-cursor-volatile",
                daemon=True,
            ).start()
        except BaseException:  # aqg: top-level boundary -- thread-start failure denies
            close_after = False
            with self._lock:
                self._check_inflight = False
                close_after = self._closed
            if close_after:
                self.handle.close()
            return VolatileResult.unavailable()

        if not done.wait(max(0.0, deadline - time.monotonic())):
            return VolatileResult.unavailable()
        with self._lock:
            if self._closed:
                return VolatileResult.revoked("terminal-exit")
            return publication.get("value", VolatileResult.unavailable())

    def close(self) -> None:
        close_now = False
        with self._lock:
            if self._closed:
                return
            self._closed = True
            close_now = not self._check_inflight
        if close_now:
            self.handle.close()


class CursorWindowsNativeProbes:
    """Collect stable evidence and transfer one terminal-handle lease.

    The caller owns the returned lease and must close it on every outcome other
    than ``PreflightResult.ready``; the runtime evaluator and late-result path
    enforce that ownership rule.
    """

    def __init__(self, api: Any = None) -> None:
        self._api = api if api is not None else WindowsNativeApi()

    def collect(
        self, fixture: CursorWindowsFixture, deadline: float
    ) -> CursorWindowsEvidence:
        api = self._api
        handles = []
        terminal_lease = None
        try:
            _deadline(deadline)
            observed_platform = api.platform()
            if observed_platform != "win32":
                return CursorWindowsEvidence(
                    platform=observed_platform,
                    remote_environment=False,
                    shim_session_id=0,
                    active_console_session_id=0,
                    current_process_creation_time=0,
                    ancestors=(),
                    known_roots={},
                    terminal_lease=None,
                    runtime_architecture="",
                )
            if not (
                1
                <= fixture.terminal_hop
                <= fixture.max_ancestor_hops
                <= 8
            ):
                raise ValueError("Cursor fixture hop bounds are invalid")
            current_pid = api.current_pid()
            remote_environment = api.remote_environment()
            shim_session = api.process_session_id(current_pid)
            active_session = api.active_console_session_id()
            current_creation = api.current_process_creation_time(deadline)
            runtime_architecture = api.runtime_architecture()
            if (
                remote_environment
                or shim_session in (0, 0xFFFFFFFF)
                or active_session in (0, 0xFFFFFFFF)
                or shim_session != active_session
            ):
                return CursorWindowsEvidence(
                    platform=observed_platform,
                    remote_environment=remote_environment,
                    shim_session_id=shim_session,
                    active_console_session_id=active_session,
                    current_process_creation_time=current_creation,
                    ancestors=(),
                    known_roots={},
                    terminal_lease=None,
                    runtime_architecture=runtime_architecture,
                )
            parents = api.process_parents(deadline)
            ancestors = []
            child_pid = current_pid
            for hop in range(1, fixture.terminal_hop + 1):
                _deadline(deadline)
                parent_pid = parents.get(child_pid)
                if type(parent_pid) is not int or parent_pid <= 0:
                    raise OSError("required parent unavailable")
                handle = api.open_process(parent_pid)
                handles.append(handle)
                evidence = api.inspect_process(handle, deadline)
                ancestors.append(evidence)
                child_pid = parent_pid
                _deadline(deadline)
                if hop == fixture.terminal_hop:
                    terminal_lease = CursorTerminalLease(
                        handle, api, fixture, shim_session
                    )
            if terminal_lease is None:
                raise OSError("terminal process unavailable")
            for handle in handles:
                if handle is not terminal_lease.handle:
                    handle.close()
            return CursorWindowsEvidence(
                platform=observed_platform,
                remote_environment=remote_environment,
                shim_session_id=shim_session,
                active_console_session_id=active_session,
                current_process_creation_time=current_creation,
                ancestors=tuple(ancestors),
                known_roots=api.known_roots(deadline),
                terminal_lease=terminal_lease,
                runtime_architecture=runtime_architecture,
            )
        except BaseException:  # aqg: top-level boundary -- close ownership then propagate
            for handle in handles:
                try:
                    handle.close()
                except BaseException:  # aqg: top-level boundary -- cleanup cannot replace probe failure
                    pass
            raise


def redacted_capture_projection(
    fixture: CursorWindowsFixture,
    evidence: CursorWindowsEvidence,
) -> Dict[str, Any]:
    """Build an opt-in review helper without emitting or persisting raw values.

    This helper is intentionally not wired to runtime logs or automatic
    persistence. It reports only fixture-rule match counts; rule values and
    observed argv never leave the in-memory evaluator boundary.
    """

    def rule_matches(rule, argv: Tuple[str, ...]) -> bool:
        if rule.match == "exact":
            return rule.value in argv
        expected = rule.value.casefold()
        return any(item.casefold() == expected for item in argv)

    ancestors = []
    child_creation = evidence.current_process_creation_time
    for hop, process in enumerate(evidence.ancestors, 1):
        required_matches = sum(
            1
            for rule in fixture.required_argv_rules
            if (
                rule.scope == "any-hop"
                or (rule.scope == "direct-parent" and hop == 1)
                or (
                    rule.scope == "terminal"
                    and hop == fixture.terminal_hop
                )
            )
            and rule_matches(rule, process.argv)
        )
        forbidden_matches = sum(
            1
            for rule in fixture.forbidden_argv_rules
            if rule_matches(rule, process.argv)
        )
        ancestors.append(
            {
                "hop": hop,
                "image_basename": process.image_basename,
                "required_rule_matches": required_matches,
                "forbidden_rule_matches": forbidden_matches,
                "alive": process.alive,
                "same_session": process.session_id == evidence.shim_session_id,
                "older_than_child": process.creation_time < child_creation,
                "terminal_hop": hop == fixture.terminal_hop,
            }
        )
        child_creation = process.creation_time
    return {
        "schema_version": 1,
        "fixture_id": fixture.fixture_id,
        "platform": evidence.platform,
        "remote_marker_present": evidence.remote_environment,
        "session_match": (
            evidence.shim_session_id == evidence.active_console_session_id
        ),
        "runtime_architecture": evidence.runtime_architecture,
        "ancestor_count": len(evidence.ancestors),
        "ancestors": ancestors,
    }


class WindowsNativeApi:
    """Thin ctypes bindings; every caller is bounded by the collector deadline."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows native probes require Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self.version = ctypes.WinDLL("version", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        k = self.kernel32
        k.GetCurrentProcessId.restype = wintypes.DWORD
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.ProcessIdToSessionId.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        k.ProcessIdToSessionId.restype = wintypes.BOOL
        k.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        )
        k.GetProcessTimes.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.Process32FirstW.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESSENTRY32W),
        )
        k.Process32FirstW.restype = wintypes.BOOL
        k.Process32NextW.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESSENTRY32W),
        )
        k.Process32NextW.restype = wintypes.BOOL
        k.GetFileAttributesW.argtypes = (wintypes.LPCWSTR,)
        k.GetFileAttributesW.restype = wintypes.DWORD
        k.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
        k.GetDriveTypeW.restype = wintypes.UINT
        self.ntdll.NtQueryInformationProcess.argtypes = (
            wintypes.HANDLE,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        )
        self.ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        self.shell32.CommandLineToArgvW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_int),
        )
        self.shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        k.LocalFree.argtypes = (wintypes.HLOCAL,)
        k.LocalFree.restype = wintypes.HLOCAL
        self.shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.LPWSTR),
        )
        self.shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        self.ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        self.version.GetFileVersionInfoSizeW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        self.version.GetFileVersionInfoW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        self.version.GetFileVersionInfoW.restype = wintypes.BOOL
        self.version.VerQueryValueW.argtypes = (
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        )
        self.version.VerQueryValueW.restype = wintypes.BOOL
        self.user32.OpenInputDesktop.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self.user32.OpenInputDesktop.restype = wintypes.HANDLE
        self.user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
        self.user32.CloseDesktop.restype = wintypes.BOOL
        self.user32.EnumDesktopWindows.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.LPARAM,
        )
        self.user32.EnumDesktopWindows.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetClassNameW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = (wintypes.HWND,)
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user32.GetWindow.restype = wintypes.HWND
        self.dwmapi.DwmGetWindowAttribute.argtypes = (
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        self.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    def current_pid(self) -> int:
        return int(self.kernel32.GetCurrentProcessId())

    def platform(self) -> str:
        return sys.platform

    def remote_environment(self) -> bool:
        return any(bool(os.environ.get(name)) for name in _REMOTE_MARKERS)

    def process_parents(self, deadline: float) -> Mapping[int, int]:
        _deadline(deadline)
        snapshot = self.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot == _INVALID_HANDLE_VALUE:
            raise OSError(ctypes.get_last_error(), "process snapshot failed")
        parents: Dict[int, int] = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = self.kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                _deadline(deadline)
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = self.kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.kernel32.CloseHandle(snapshot)
        return parents

    def process_session_id(self, pid: int) -> int:
        value = wintypes.DWORD()
        if not self.kernel32.ProcessIdToSessionId(pid, ctypes.byref(value)):
            raise OSError(ctypes.get_last_error(), "session query failed")
        return int(value.value)

    def active_console_session_id(self) -> int:
        self.kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        return int(self.kernel32.WTSGetActiveConsoleSessionId())

    def current_process_creation_time(self, deadline: float) -> int:
        _deadline(deadline)
        result = self._creation_time(self.kernel32.GetCurrentProcess())
        _deadline(deadline)
        return result

    def runtime_architecture(self) -> str:
        machine = platform.machine().casefold()
        if machine in ("amd64", "x86_64"):
            return "x64"
        if machine in ("arm64", "aarch64"):
            return "arm64"
        return machine

    def open_process(self, pid: int) -> _OwnedHandle:
        value = self.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
            False,
            pid,
        )
        if not value:
            raise OSError(ctypes.get_last_error(), "process open failed")
        return _OwnedHandle(value, self.kernel32.CloseHandle, pid)

    def _creation_time(self, handle: int) -> int:
        creation, exit_time, kernel, user = (
            _FILETIME(),
            _FILETIME(),
            _FILETIME(),
            _FILETIME(),
        )
        if not self.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError(ctypes.get_last_error(), "process time query failed")
        return _filetime_value(creation)

    def _image_path(self, handle: int, deadline: float) -> str:
        _deadline(deadline)
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            raise OSError(ctypes.get_last_error(), "process image query failed")
        _deadline(deadline)
        return buffer.value

    def _command_line(
        self, handle: int, deadline: float
    ) -> Tuple[str, ...]:
        _deadline(deadline)
        needed = wintypes.ULONG()
        self.ntdll.NtQueryInformationProcess(
            handle,
            _PROCESS_COMMAND_LINE_INFORMATION,
            None,
            0,
            ctypes.byref(needed),
        )
        if needed.value < ctypes.sizeof(_UNICODE_STRING) or needed.value > _MAX_COMMAND_LINE_BYTES:
            raise OSError("process command line size invalid")
        _deadline(deadline)
        buffer = ctypes.create_string_buffer(needed.value)
        status = self.ntdll.NtQueryInformationProcess(
            handle,
            _PROCESS_COMMAND_LINE_INFORMATION,
            buffer,
            needed.value,
            ctypes.byref(needed),
        )
        if status < 0:
            raise OSError("process command line query failed")
        _deadline(deadline)
        value = ctypes.cast(buffer, ctypes.POINTER(_UNICODE_STRING)).contents
        start = ctypes.addressof(buffer)
        end = start + ctypes.sizeof(buffer)
        if (
            value.Length % 2
            or value.Length > value.MaximumLength
            or not start <= int(value.Buffer or 0) <= end
            or int(value.Buffer or 0) + value.Length > end
        ):
            raise OSError("process command line pointer invalid")
        command_line = ctypes.wstring_at(value.Buffer, value.Length // 2)
        count = ctypes.c_int()
        argv = self.shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
        if not argv or count.value <= 0 or count.value > 4096:
            raise OSError("process command line parse failed")
        try:
            parsed = tuple(argv[index] for index in range(count.value))
            _deadline(deadline)
            return parsed
        finally:
            self.kernel32.LocalFree(argv)

    def _path_properties(
        self, path: str, deadline: float
    ) -> Tuple[bool, bool]:
        _deadline(deadline)
        if path.startswith("\\\\"):
            return False, True
        parsed = PureWindowsPath(path)
        drive = parsed.drive
        if not drive or self.kernel32.GetDriveTypeW(drive + "\\") != _DRIVE_FIXED:
            return False, True
        current = PureWindowsPath(drive + "\\")
        reparse = False
        for part in parsed.parts[1:]:
            _deadline(deadline)
            current /= part
            attrs = self.kernel32.GetFileAttributesW(str(current))
            if attrs == _INVALID_FILE_ATTRIBUTES:
                raise OSError(ctypes.get_last_error(), "path attribute query failed")
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                reparse = True
        return True, reparse

    def _version_strings(
        self, path: str, deadline: float
    ) -> Tuple[str, str, str]:
        _deadline(deadline)
        ignored = wintypes.DWORD()
        size = self.version.GetFileVersionInfoSizeW(path, ctypes.byref(ignored))
        if size <= 0 or size > _MAX_VERSION_BYTES:
            return "", "", ""
        _deadline(deadline)
        buffer = ctypes.create_string_buffer(size)
        if not self.version.GetFileVersionInfoW(path, 0, size, buffer):
            return "", "", ""
        _deadline(deadline)
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not self.version.VerQueryValueW(
            buffer,
            r"\VarFileInfo\Translation",
            ctypes.byref(pointer),
            ctypes.byref(length),
        ) or length.value < 4:
            return "", "", ""
        language, codepage = ctypes.cast(
            pointer, ctypes.POINTER(wintypes.WORD * 2)
        ).contents
        prefix = "\\StringFileInfo\\%04x%04x\\" % (language, codepage)

        def query(name: str) -> str:
            _deadline(deadline)
            value_pointer = ctypes.c_void_p()
            value_length = wintypes.UINT()
            if not self.version.VerQueryValueW(
                buffer,
                prefix + name,
                ctypes.byref(value_pointer),
                ctypes.byref(value_length),
            ) or value_length.value <= 1:
                return ""
            result = ctypes.wstring_at(value_pointer, value_length.value - 1)
            _deadline(deadline)
            return result

        return query("ProductName"), query("CompanyName"), query("FileVersion")

    def inspect_process(
        self, handle: _OwnedHandle, deadline: float
    ) -> CursorProcessEvidence:
        _deadline(deadline)
        path = self._image_path(handle.value, deadline)
        fixed, reparse = self._path_properties(path, deadline)
        product, company, file_version = self._version_strings(path, deadline)
        argv = self._command_line(handle.value, deadline)
        _deadline(deadline)
        return CursorProcessEvidence(
            pid=handle.pid,
            image_basename=PureWindowsPath(path).name,
            argv=argv,
            session_id=self.process_session_id(handle.pid),
            creation_time=self._creation_time(handle.value),
            alive=(
                self.kernel32.WaitForSingleObject(handle.value, 0)
                == _WAIT_TIMEOUT
            ),
            final_path=path,
            fixed_volume=fixed,
            reparse_target=reparse,
            product=product,
            company=company,
            file_version=file_version,
        )

    def known_roots(self, deadline: float) -> Mapping[str, str]:
        roots = {}
        for name, folder_id in _KNOWN_FOLDER_GUIDS.items():
            _deadline(deadline)
            rendered = wintypes.LPWSTR()
            result = self.shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id),
                0,
                None,
                ctypes.byref(rendered),
            )
            if result >= 0 and rendered:
                try:
                    roots[name] = rendered.value
                finally:
                    self.ole32.CoTaskMemFree(rendered)
        _deadline(deadline)
        return roots

    def volatile_check(
        self,
        handle: _OwnedHandle,
        fixture: CursorWindowsFixture,
        expected_session_id: int,
        deadline: float,
    ) -> VolatileResult:
        _deadline(deadline)
        if self.kernel32.WaitForSingleObject(handle.value, 0) != _WAIT_TIMEOUT:
            return VolatileResult.revoked("terminal-exit")
        if (
            self.process_session_id(handle.pid) != expected_session_id
            or self.active_console_session_id() != expected_session_id
        ):
            return VolatileResult.revoked("session-changed")
        desktop = self.user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
        if not desktop:
            return VolatileResult.unavailable()
        found = ctypes.c_bool(False)
        failed = ctypes.c_bool(False)
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def inspect_window(hwnd):
            if time.monotonic() > deadline:
                return False
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) != handle.pid:
                return True
            buffer = ctypes.create_unicode_buffer(256)
            length = self.user32.GetClassNameW(hwnd, buffer, len(buffer))
            class_name = buffer.value if length > 0 else ""
            if class_name not in fixture.window_class_names:
                return True
            if (
                not self.user32.IsWindowVisible(hwnd)
                or self.user32.IsIconic(hwnd)
                or self.user32.GetWindow(hwnd, _GW_OWNER)
            ):
                return True
            cloaked = wintypes.DWORD()
            if self.dwmapi.DwmGetWindowAttribute(
                hwnd,
                _DWMWA_CLOAKED,
                ctypes.byref(cloaked),
                ctypes.sizeof(cloaked),
            ) != 0 or cloaked.value:
                return True
            found.value = True
            return False

        def inspect(hwnd, _lparam):
            try:
                return inspect_window(hwnd)
            except BaseException:  # aqg: top-level boundary -- contain native callback
                failed.value = True
                return False

        callback = callback_type(inspect)
        try:
            self.user32.EnumDesktopWindows(desktop, callback, 0)
        finally:
            self.user32.CloseDesktop(desktop)
        _deadline(deadline)
        if failed.value:
            return VolatileResult.unavailable()
        return (
            VolatileResult.available()
            if found.value
            else VolatileResult.unavailable()
        )


def default_cursor_windows_probes() -> CursorWindowsNativeProbes:
    return CursorWindowsNativeProbes()
