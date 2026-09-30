"""Human-readable local device identity defaults."""

from __future__ import annotations

from datetime import date
import os
import platform
import re
import subprocess

_WINDOWS_CURRENT_VERSION_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"


def _component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9.]+", ".", value.strip()).strip(".")
    return cleaned or fallback


def _architecture_category(machine: str) -> str:
    normalized = machine.strip().casefold()
    if normalized in {"arm64", "aarch64"}:
        return "arm"
    if normalized in {"amd64", "x86_64"}:
        return "x64"
    if normalized in {"x86", "i386", "i686"}:
        return "x86"
    return _component(normalized, "unknown")


def _windows_version_metadata() -> tuple[str, str]:
    try:
        import winreg
    except ImportError:
        return "", ""
    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _WINDOWS_CURRENT_VERSION_KEY,
            access=access,
        ) as key:
            display_version = ""
            for value_name in ("DisplayVersion", "ReleaseId"):
                try:
                    value, _kind = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    display_version = value.strip()
                    break
            build_number = ""
            for value_name in ("CurrentBuildNumber", "CurrentBuild"):
                try:
                    value, _kind = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    build_number = value.strip()
                    break
            return display_version, build_number
    except OSError:
        return "", ""


def _windows_release(build_number: str) -> str:
    release = _component(platform.release(), "unknown")
    try:
        if int(build_number) >= 22000 and release in {"10", "unknown"}:
            return "11"
    except ValueError:
        pass
    return release


def _native_machine(system: str) -> str:
    if system == "Windows":
        return (
            os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or platform.machine()
        )
    machine = platform.machine()
    if system != "Darwin" or machine.strip().casefold() not in {"amd64", "x86_64"}:
        return machine
    try:
        translated = subprocess.run(
            ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return machine
    if translated.returncode == 0 and translated.stdout.strip() == "1":
        return "arm64"
    return machine


def device_name_default(*, activation_date: date | None = None) -> str:
    system = platform.system()
    if system == "Darwin":
        system_name = "macOS"
        system_version = platform.mac_ver()[0]
    elif system == "Windows":
        display_version, build_number = _windows_version_metadata()
        system_name = "windows%s" % _windows_release(build_number)
        system_version = display_version or platform.version()
    else:
        system_name = _component(system.casefold(), "device")
        system_version = platform.release()
    return "%s-%s-%s-%s" % (
        system_name,
        _component(system_version, "unknown"),
        _architecture_category(_native_machine(system)),
        (activation_date or date.today()).strftime("%y%m%d"),
    )
