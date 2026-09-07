"""Tencent WorkBuddy Desktop declaration for Windows and macOS."""

from __future__ import annotations

import locale
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from installer.client_hosts.contract import AgentHostSpec
from installer.config import ShellError


_WORKBUDDY_SETUP_ENV = "DE_WORKBUDDY_SETUP"
_LEGACY_OUTSIDE_SANDBOX_APPROVAL_ENV = "DE_WORKBUDDY_OUTSIDE_SANDBOX_APPROVED"


def _process_ancestor_names(
    processes: Mapping[int, tuple[int, str]],
    current_pid: int,
    max_depth: int = 16,
) -> tuple[str, ...]:
    """Walk a process snapshot without trusting it to stay complete or acyclic."""

    names = []
    seen = {current_pid}
    for _ in range(max_depth):
        current = processes.get(current_pid)
        if current is None:
            break
        parent_pid = current[0]
        if parent_pid <= 0 or parent_pid in seen:
            break
        seen.add(parent_pid)
        parent = processes.get(parent_pid)
        if parent is None:
            break
        names.append(parent[1])
        current_pid = parent_pid
    return tuple(names)


def _windows_process_ancestor_names(max_depth: int = 16) -> tuple[str, ...]:
    """Return executable basenames for this process's Windows ancestor chain."""

    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    processes: dict[int, tuple[int, str]] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not process_first(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            processes[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                entry.szExeFile,
            )
            if not process_next(snapshot, ctypes.byref(entry)):
                break
    finally:
        close_handle(snapshot)

    return _process_ancestor_names(processes, os.getpid(), max_depth)


def _workbuddy_webview_render_guard() -> str | None:
    """Keep the setup WebView out of WorkBuddy's Windows launch path.

    Measured on a real WorkBuddy install: pywebview's EdgeChromium (WebView2)
    backend CREATES its window but can remain permanently blank, even after an
    outside-sandbox launch was approved. The dead frame takes no input and,
    critically, raises no exception, so the ordinary exception-driven Tk
    fallback never triggers and the install stalls until the window is killed.
    The Tk form has none of those dependencies, so the caller degrades to it
    instead of refusing.

    The command-scoped marker is authoritative when an intermediate launcher
    detaches and WorkBuddy disappears from the observable ancestor chain. The
    old approval marker remains accepted so already-installed setup guidance
    also selects the reliable form.
    """

    if sys.platform != "win32":
        # macOS is already covered by permanent_setup's earlier generic
        # sandbox_check gate; this host-owned probe only fills the Windows gap.
        return None
    marked_workbuddy_setup = (
        os.environ.get(_WORKBUDDY_SETUP_ENV) == "1"
        or os.environ.get(_LEGACY_OUTSIDE_SANDBOX_APPROVAL_ENV) == "1"
    )
    fallback_reason = (
        "WorkBuddy's Windows launch path cannot reliably render the permanent "
        "setup WebView; the masked Tk form is used instead"
    )
    try:
        ancestor_names = {
            name.casefold() for name in _windows_process_ancestor_names()
        }
    except Exception:  # aqg: top-level boundary — optional ancestry inspection fails open
        return fallback_reason if marked_workbuddy_setup else None
    # workbuddy.exe ALONE is sufficient, while the explicit marker covers
    # launchers that detach from WorkBuddy before permanent setup starts.
    if "workbuddy.exe" not in ancestor_names and not marked_workbuddy_setup:
        return None
    return fallback_reason


def _workbuddy_config_path() -> Path:
    return Path.home() / ".workbuddy" / "mcp.json"


def _workbuddy_app_root() -> Path:
    configured = os.getenv("WORKBUDDY_APP_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path("/Applications/WorkBuddy.app")
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "WorkBuddy"


def _workbuddy_skills_path() -> Path:
    configured = os.getenv("WORKBUDDY_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".workbuddy" / "skills"


def _workbuddy_executable() -> Path:
    if sys.platform == "darwin":
        return _workbuddy_app_root() / "Contents" / "MacOS" / "Electron"
    return _workbuddy_app_root() / "WorkBuddy.exe"


def _workbuddy_installed() -> bool:
    executable = _workbuddy_executable()
    return (
        sys.platform in {"win32", "darwin"}
        and executable.is_file()
        and not executable.is_symlink()
    )


def _workbuddy_skills_in_use() -> bool:
    return _workbuddy_installed()


def _workbuddy_skills_configured() -> bool:
    from installer import mcp_config

    try:
        return mcp_config.read_entry("workbuddy") is not None
    except ShellError:
        return False


def _workbuddy_config_write_guard():
    if sys.platform not in {"win32", "darwin"}:
        raise ShellError(
            "unsupported_workbuddy_platform: only Windows and macOS Desktop "
            "builds are supported; nothing was written"
        )
    if not _workbuddy_installed():
        raise ShellError(
            "workbuddy_not_installed: Tencent WorkBuddy Desktop was not found at "
            "the configured application root; nothing was written"
        )
    return None


def _windows_user_locale() -> str | None:
    """Read the Windows UI locale without changing process-global locale state."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        locale_name = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(
            locale_name, len(locale_name)
        ):
            return locale_name.value
    except Exception:  # aqg: top-level boundary — optional locale discovery must fail open
        return None
    return None


def _system_ui_locale() -> str | None:
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        configured = os.getenv(variable)
        if configured and configured.strip():
            return configured
    windows_locale = _windows_user_locale()
    if windows_locale:
        return windows_locale
    try:
        current = locale.getlocale()
    except Exception:  # aqg: top-level boundary — optional locale discovery must fail open
        return None
    if (
        isinstance(current, (tuple, list))
        and current
        and isinstance(current[0], str)
    ):
        return current[0]
    return None


def _workbuddy_ui_locale() -> str:
    configured = os.getenv("DE_UI_LOCALE")
    if not configured or not configured.strip():
        configured = _system_ui_locale()
    normalized = (configured or "").strip().replace("_", "-").casefold()
    # The installer currently ships one Chinese translation for every zh locale.
    if normalized == "zh" or normalized.startswith(("zh-", "chinese")):
        return "zh-CN"
    return "en-US"


def _workbuddy_post_mcp_write_notice() -> str:
    if _workbuddy_ui_locale() == "zh-CN":
        return (
            "WorkBuddy 仍需手动信任连接器：打开「连接器管理」→右上角"
            "「自定义连接器（Custom connectors）」；找到 decision-engine，"
            "点击「信任（Trust）」；然后完全重启 WorkBuddy 或新建会话。"
        )
    return (
        "WorkBuddy action required: open Connector Management, select Custom "
        "connectors in the top-right, find decision-engine, select Trust, then "
        "fully restart WorkBuddy or start a new session."
    )


WORKBUDDY = AgentHostSpec(
    id="workbuddy",
    transport="stdio",
    launch_policy="desktop-python-v1",
    config_format="json",
    host_family="workbuddy",
    config_env="WORKBUDDY_CONFIG",
    default_path=_workbuddy_config_path,
    config_scope="user-global",
    config_path=_workbuddy_config_path,
    config_renderer="json-mcp-v1",
    entry_ownership_policy="replace-marked-de-v1",
    config_write_guard_probe=lambda: _workbuddy_config_write_guard(),
    observed_client_aliases=frozenset({"connector:custom-mcp:decision-engine"}),
    # Observed for diagnostics, never enforced. WorkBuddy composes clientInfo.name
    # from its OWN internal connector id, which embeds the key we registered the
    # server under ("connector:custom-mcp:<key>") -- a private string we do not
    # control and that already differs across its builds. Enforcing it silently
    # stripped open_ge, open_ge_popup, open_db_board and db_board_result from real
    # users whenever upstream renamed it, with no signal to the user or the calling
    # model.
    #
    # Premise, explicit because the whole argument rests on it: this spec is reached
    # only over local stdio, spawned by the WorkBuddy process on the user's own
    # machine (transport="stdio" above; there is no remote or relayed connector path
    # to this host). Under that premise the check bought no security -- the alias is
    # a plaintext constant in a public repo, so any peer able to spawn this process
    # can replay it, and such a peer already holds user-level code execution. Should
    # a non-local transport ever reach this host, revisit this and gate by transport
    # instead.
    #
    # Blast radius: identity enforcement gated three capability lanes at once
    # (shim.py closes display, followup and stopper together on a mismatch), so this
    # also ungates the audit stop panel. That panel only cancels the caller's own
    # queued audits and the four display tools take run_id/title/context, so no
    # approval or credential surface widens; popup_followup is already False here.
    #
    # Matches claude-code, claude-desktop, codex and the four trae hosts -- 7 of the
    # 15 registered hosts already exposed these same display tools without identity
    # enforcement before this change. Whether a native window can actually open
    # stays with client/popup/backend.py's sandbox and GUI-session probes.
    require_observed_identity=False,
    detection="installation-probe",
    installation_probe=lambda: _workbuddy_installed(),
    post_mcp_write_notice=_workbuddy_post_mcp_write_notice,
    webview_render_guard_probe=_workbuddy_webview_render_guard,
    json_include_type=False,
    json_include_cwd=False,
    skills_path=_workbuddy_skills_path,
    skills_global_path=_workbuddy_skills_path,
    skill_delivery_mode="managed-copy",
    routing_kind="skill",
    skills_in_use=lambda: _workbuddy_skills_in_use(),
    skills_check_in_use=lambda: _workbuddy_skills_configured(),
    skill_route_name="workbuddy",
    repair_skills_on_setup=True,
    skills_require_managed_target=True,
    client_name_patterns=(r"^\s*workbuddy\s*$",),
    local_display_tools=True,
    popup_followup=False,
    audit_stop_panel=True,
    popup_api_profile="legacy",
    launcher_capabilities=frozenset(
        {"core-mcp", "local-display", "audit-stop-panel"}
    ),
    doctor_capabilities=frozenset({"mcp-entry", "skills"}),
    optional_features=frozenset({"local-display", "audit-stop-panel"}),
    onboarding_evidence=("skill",),
)

HOST_SPECS = (WORKBUDDY,)
