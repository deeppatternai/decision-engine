"""Validated process-launch policies for MCP client hosts.

Some hosts do not pass a configured command to the operating system verbatim.
The policy registry keeps those launch constraints out of shared rendering and
out of product-name conditionals.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Tuple

from installer.config import ShellError


CWD_INDEPENDENT_BOOTSTRAP = (
    "import sys; root = sys.argv.pop(1); sys.path[0] = root; "
    "from installer.launcher import main; raise SystemExit(main(sys.argv[1:]))"
)


@dataclass(frozen=True)
class LaunchRequest:
    command: str
    launcher_args: Tuple[str, ...]
    cwd_independent_args: Tuple[str, ...]
    current_interpreter: str
    python_version: Tuple[int, int]
    platform: str


@dataclass(frozen=True)
class LaunchCommand:
    command: str
    launcher_args: Tuple[str, ...]
    cwd_independent_args: Tuple[str, ...]


def _direct_python(request: LaunchRequest) -> LaunchCommand:
    return LaunchCommand(
        command=request.command,
        launcher_args=request.launcher_args,
        cwd_independent_args=request.cwd_independent_args,
    )


def _absolute_bootstrap(request: LaunchRequest) -> LaunchCommand:
    """Replace inline Python with a checkout-bound bootstrap script.

    Some hosts reject shell metacharacters in every argument, including the
    semicolons in the cwd-independent ``python -c`` bootstrap.  The input
    shape is produced by ``installer.mcp_config``; this policy preserves its
    root/mode binding while moving the code into an absolute tracked file.
    """

    candidate = request.cwd_independent_args
    if (
        len(candidate) != 5
        or candidate[0] != "-c"
        or candidate[1] != CWD_INDEPENDENT_BOOTSTRAP
        or not isinstance(candidate[2], str)
        or not candidate[2]
        or candidate[3] not in {"--dev-root", "--managed-root"}
        or candidate[4] != candidate[2]
    ):
        raise ShellError(
            "the absolute bootstrap policy received an unknown bootstrap shape"
        )
    root = candidate[2]
    is_absolute = (
        ntpath.isabs(root)
        if request.platform == "win32"
        else posixpath.isabs(root)
    )
    if not is_absolute:
        raise ShellError("the absolute bootstrap policy requires an absolute root")
    join = ntpath.join if request.platform == "win32" else posixpath.join
    bootstrap = join(root, "installer", "mcp_bootstrap.py")
    args = (bootstrap, root, candidate[3], root)
    if any(";" in argument for argument in args):
        raise ShellError("the absolute bootstrap policy refuses semicolons in paths")
    return LaunchCommand(
        command=request.command,
        launcher_args=args,
        cwd_independent_args=args,
    )


def _same_windows_path(left: str, right: str) -> bool:
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(
        ntpath.normpath(right)
    )


def _find_windows_py_launcher() -> Optional[str]:
    candidate = shutil.which("py")
    if candidate and not any(character.isspace() for character in candidate):
        return candidate

    windows_root = os.environ.get("SystemRoot")
    if windows_root:
        fallback = Path(windows_root) / "py.exe"
        if fallback.is_file() and not any(
            character.isspace() for character in str(fallback)
        ):
            return str(fallback)
    return None


def _py_launcher_resolves_to_interpreter(
    launcher: str,
    selector: str,
    expected: str,
) -> bool:
    """Confirm a version selector preserves the exact active interpreter."""

    try:
        completed = subprocess.run(
            [launcher, selector, "-I", "-c", "import sys; print(sys.executable)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    resolved = completed.stdout.strip()
    return completed.returncode == 0 and bool(resolved) and _same_windows_path(
        resolved,
        expected,
    )


def _windows_py_no_space(request: LaunchRequest) -> LaunchCommand:
    """Use ``py.exe`` when a host incorrectly tokenizes a command path.

    A caller-supplied interpreter with spaces is deliberately rejected. For the
    active interpreter, the Python Launcher selector is used only after a bounded
    probe proves that it resolves to the exact same executable.
    """

    if not any(character.isspace() for character in request.command):
        return _direct_python(request)
    if request.platform != "win32":
        raise ShellError("the space-free Python launch policy requires Windows")
    if not _same_windows_path(request.command, request.current_interpreter):
        raise ShellError(
            "this host requires a space-free Python command; provide a wrapper "
            "path without spaces"
        )

    py_launcher = _find_windows_py_launcher()
    if py_launcher is None:
        raise ShellError(
            "this host requires the Windows Python Launcher (py.exe) because "
            "the active Python path contains spaces"
        )
    selector = "-%d.%d" % request.python_version
    if not _py_launcher_resolves_to_interpreter(
        py_launcher,
        selector,
        request.current_interpreter,
    ):
        raise ShellError(
            "the Windows Python Launcher resolves to a different interpreter; "
            "provide a space-free wrapper for the active Python executable"
        )
    return LaunchCommand(
        command=py_launcher,
        launcher_args=(selector,) + request.launcher_args,
        cwd_independent_args=(selector,) + request.cwd_independent_args,
    )


def _desktop_python(request: LaunchRequest) -> LaunchCommand:
    """Apply only the launch normalization required by each desktop OS."""

    if request.platform == "win32":
        return _windows_py_no_space(request)
    if request.platform == "darwin":
        return _direct_python(request)
    raise ShellError("the desktop Python launch policy requires Windows or macOS")


_LAUNCH_POLICIES: Mapping[str, Callable[[LaunchRequest], LaunchCommand]] = (
    MappingProxyType(
        {
            "absolute-bootstrap-v1": _absolute_bootstrap,
            "desktop-python-v1": _desktop_python,
            "direct-python-v1": _direct_python,
            "windows-py-no-space-v1": _windows_py_no_space,
        }
    )
)


def resolve_launch(policy_id: str, request: LaunchRequest) -> LaunchCommand:
    policy = _LAUNCH_POLICIES.get(policy_id)
    if policy is None:
        raise ShellError("launch policy %r is unsupported" % policy_id)
    return policy(request)


def registered_launch_policies() -> Mapping[str, Callable[[LaunchRequest], LaunchCommand]]:
    return _LAUNCH_POLICIES
