"""Local DE Lite lifecycle bridge for a host with no Decision Engine MCP tools."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Optional, Sequence


_HOST_TEMP_DIR = (
    Path("/private/tmp")
    if platform.system() == "Darwin"
    else Path(tempfile.gettempdir())
)


def _host_runtime_config_path() -> Path:
    identity = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return (
        _HOST_TEMP_DIR / ("decision-engine-host-bridge-%s" % identity) / "config.json"
    )


_CALLER_RUNTIME_OVERRIDES = (
    "DE_ACTIVE_RUN",
    "DE_ACTIVE_RUNS",
    "DE_ACTIVE_RUNS_LOCK",
    "DE_ACTIVE_RUNS_LOCK_PATHS",
    "DE_ACTIVE_RUNS_WRITE_PATHS",
    "DE_ACTIVE_RUN_WRITE_PATHS",
    "DE_STOPPER_SINGLETON_LOCK",
    "DE_SKIP_STOPPER_LAUNCH",
)
_DEVICE_CONFIG_PATH = (
    Path(os.getenv("DEEPPATTERN_HOME", str(Path.home() / ".deeppattern"))).expanduser()
    / "decision-engine"
    / "config.json"
)
for _name in _CALLER_RUNTIME_OVERRIDES:
    os.environ.pop(_name, None)

# Codex runs this bridge inside a workspace sandbox that cannot write the managed
# DE root. A fixed per-user temporary config keeps lifecycle state local and
# prevents caller-selected paths or the real activation config from entering it.
os.environ["DE_CONFIG_PATH"] = str(_host_runtime_config_path())

DE_ROOT = Path(__file__).resolve().parents[3]
if not all(
    (DE_ROOT / "client" / name).is_file()
    for name in ("runner.py", "windows_security.py")
):
    raise SystemExit("de-lite-local-bridge: invalid installed Decision Engine root")
sys.path.insert(0, str(DE_ROOT))

from client import runner, windows_security  # noqa: E402


_HOST_AGENT_LABEL = "com.decision-engine.stopper.hostbridge"
_HOST_AGENT_MARKER = "DE_STOPPER_HOSTBRIDGE_MANAGED"
_MAX_PLIST_BYTES = 128 * 1024
_MAX_CONFIG_BYTES = 128 * 1024
_MAX_REGISTRY_BYTES = 4 * 1024 * 1024
_SENSITIVE_ENV_FRAGMENTS = (
    "ENDPOINT",
    "TOKEN",
    "SECRET",
    "ACCOUNT",
    "CREDENTIAL",
    "PASSWORD",
    "ARTIFACT",
)


def _identity() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else "user"


def _host_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (_HOST_AGENT_LABEL + ".plist")


def _host_runtime_paths() -> Dict[str, Path]:
    root = _HOST_TEMP_DIR / ("decision-engine-host-bridge-%s" % _identity())
    state = root / ".runtime"
    locks = state / "locks"
    logs = state / "logs"
    return {
        "root": root,
        "state": state,
        "locks": locks,
        "logs": logs,
        "config": root / "config.json",
        "active_run": state / "active-run.json",
        "active_runs": state / "active-runs.json",
        "active_runs_lock": locks / "active-runs.lock",
        "stopper_lock": locks / "stopper-panel.lock",
        "log": logs / "stopper.log",
    }


def _host_environment(paths: Dict[str, Path]) -> Dict[str, str]:
    return {
        _HOST_AGENT_MARKER: "1",
        "DE_CONFIG_PATH": str(paths["config"]),
        "DE_ACTIVE_RUN": str(paths["active_run"]),
        "DE_ACTIVE_RUNS": str(paths["active_runs"]),
        "DE_ACTIVE_RUNS_LOCK": str(paths["active_runs_lock"]),
        "DE_ACTIVE_RUNS_LOCK_PATHS": json.dumps([str(paths["active_runs_lock"])]),
        "DE_ACTIVE_RUNS_WRITE_PATHS": json.dumps([str(paths["active_runs"])]),
        "DE_ACTIVE_RUN_WRITE_PATHS": json.dumps([str(paths["active_run"])]),
        "DE_STOPPER_SINGLETON_LOCK": str(paths["stopper_lock"]),
    }


def _expected_host_agent_payload() -> Dict[str, Any]:
    if platform.system() != "Darwin":
        raise OSError("native Stopper host LaunchAgent is macOS-only")
    paths = _host_runtime_paths()
    stopper = runner.shipped_stopper_path(DE_ROOT)
    if stopper is None:
        raise OSError("native Stopper is unavailable for this Mac architecture")
    stopper = stopper.resolve()
    return {
        "Label": _HOST_AGENT_LABEL,
        "ProgramArguments": [str(stopper)],
        "EnvironmentVariables": _host_environment(paths),
        "WatchPaths": [str(paths["active_runs"])],
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(paths["log"]),
        "StandardErrorPath": str(paths["log"]),
    }


def _read_owned_file(path: Path, *, max_bytes: int, private: bool) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(path, flags)
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("not a regular file")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise OSError("not owned by this user")
        if private and os.name == "nt":
            try:
                windows_security.validate_private_data_fd(fd)
            except windows_security.WindowsSecurityError as exc:
                raise OSError("not private") from exc
        elif private and stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise OSError("not private")
        if file_stat.st_size > max_bytes:
            raise OSError("oversized")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise OSError("oversized")
        return raw
    finally:
        if fd >= 0:
            os.close(fd)


def _degrade_reason() -> str:
    """Classify the local pre-call boundary without exposing or forwarding credentials."""
    if not _DEVICE_CONFIG_PATH.exists():
        return "unactivated"
    try:
        raw = _read_owned_file(
            _DEVICE_CONFIG_PATH, max_bytes=_MAX_CONFIG_BYTES, private=True
        )
        config = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        # An unreadable config cannot prove the device is unactivated; retain the
        # MCP-unavailable fallback without making an unsafe activation claim.
        return "mcp_unavailable"
    token = config.get("access_token") if isinstance(config, dict) else None
    return "mcp_unavailable" if isinstance(token, str) and token.strip() else "unactivated"


def _registry_ready(path: Path) -> bool:
    try:
        loaded = json.loads(
            _read_owned_file(path, max_bytes=_MAX_REGISTRY_BYTES, private=True).decode(
                "utf-8"
            )
        )
        updated_at = loaded.get("updated_at") if isinstance(loaded, dict) else None
        return bool(
            isinstance(loaded, dict)
            and type(loaded.get("schema_version")) is int
            and loaded["schema_version"] == 1
            and isinstance(loaded.get("runs"), dict)
            and type(updated_at) in (int, float)
            and math.isfinite(float(updated_at))
            and float(updated_at) <= time.time() + 300.0
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _private_runtime_directories_ready(paths: Dict[str, Path]) -> bool:
    for name in ("root", "state", "locks", "logs"):
        try:
            directory_stat = paths[name].lstat()
        except OSError:
            return False
        if not stat.S_ISDIR(directory_stat.st_mode):
            return False
        if os.name == "nt":
            try:
                windows_security.validate_private_data_acl(paths[name])
            except windows_security.WindowsSecurityError:
                return False
        elif (
            (hasattr(os, "getuid") and directory_stat.st_uid != os.getuid())
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            return False
    return True


def _arguments(lines: list[str]) -> Optional[list[str]]:
    starts = [
        index for index, line in enumerate(lines) if line.strip() == "arguments = {"
    ]
    if len(starts) != 1:
        return None
    values: list[str] = []
    for line in lines[starts[0] + 1 :]:
        stripped = line.strip()
        if stripped == "}":
            return values
        if stripped:
            values.append(stripped)
    return None


def _watch_paths(lines: list[str]) -> Optional[list[str]]:
    starts = [
        index for index, line in enumerate(lines) if line.strip() == '"WatchPaths" => ['
    ]
    if len(starts) != 1:
        return None
    values: list[str] = []
    for line in lines[starts[0] + 1 :]:
        stripped = line.strip()
        if stripped == "]":
            return values
        index_text, separator, encoded = stripped.partition(" = ")
        if not separator or not index_text.isdigit() or int(index_text) != len(values):
            return None
        try:
            value = json.loads(encoded)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, str):
            return None
        values.append(value)
    return None


def _environment(lines: list[str]) -> Optional[Dict[str, str]]:
    starts = [
        index for index, line in enumerate(lines) if line.strip() == "environment = {"
    ]
    if len(starts) != 1:
        return None
    values: Dict[str, str] = {}
    for line in lines[starts[0] + 1 :]:
        stripped = line.strip()
        if stripped == "}":
            return values
        name, separator, value = stripped.partition(" => ")
        if not separator or not name or name in values:
            return None
        values[name] = value
    return None


def _loaded_job_matches(output: str, *, path: Path, payload: Dict[str, Any]) -> bool:
    arguments = payload["ProgramArguments"]
    watched = payload["WatchPaths"]
    expected_environment = payload["EnvironmentVariables"]
    lines = output.splitlines()
    exact_lines = {line.strip() for line in lines}
    if {
        "path = %s" % path,
        "program = %s" % arguments[0],
    } - exact_lines:
        return False
    if _arguments(lines) != arguments or _watch_paths(lines) != watched:
        return False
    loaded_environment = _environment(lines)
    if loaded_environment is None or any(
        loaded_environment.get(name) != value
        for name, value in expected_environment.items()
    ):
        return False
    return not any(
        name not in expected_environment
        and any(fragment in name.upper() for fragment in _SENSITIVE_ENV_FRAGMENTS)
        for name in loaded_environment
    )


def _darwin_host_agent_ready() -> bool:
    try:
        paths = _host_runtime_paths()
        if not _private_runtime_directories_ready(paths):
            return False
        path = _host_launch_agent_path()
        payload = plistlib.loads(
            _read_owned_file(path, max_bytes=_MAX_PLIST_BYTES, private=True)
        )
        expected = _expected_host_agent_payload()
        if payload != expected:
            return False
        program = Path(expected["ProgramArguments"][0])
        registry = paths["active_runs"]
        if not program.is_file() or not os.access(program, os.X_OK):
            return False
        if not _registry_ready(registry):
            return False
        checked = subprocess.run(
            [
                "/bin/launchctl",
                "print",
                "gui/%s/%s" % (_identity(), _HOST_AGENT_LABEL),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            check=False,
        )
        return checked.returncode == 0 and _loaded_job_matches(
            checked.stdout or "", path=path, payload=expected
        )
    except (
        OSError,
        plistlib.InvalidFileException,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ):
        return False


def _launch_host_stopper() -> None:
    if platform.system() == "Darwin":
        if _darwin_host_agent_ready():
            return
        raise OSError("native Stopper host LaunchAgent is unavailable")
    runner.launch_stopper_if_available(prefer_shipped=True)


def begin(title: str, ui_locale: Optional[str] = None) -> Dict[str, Any]:
    """Start one host-only DE Lite advisory without accepting artifact data or contacting Hub."""
    safe_title = title.strip() if isinstance(title, str) else ""
    if not safe_title:
        raise ValueError("explicit audit topic required")
    # On macOS the installed LaunchAgent contract must verify before publishing a
    # watched run; otherwise a failed host wakeup would leave an invisible Running.
    if platform.system() == "Darwin":
        _launch_host_stopper()
    local_id = "local_host_%s" % secrets.token_hex(12)
    reason = _degrade_reason()
    locale = runner.normalize_ui_locale(ui_locale)
    runner.save_local_advisory_run(
        local_id,
        status="running",
        title=safe_title,
        surface="de_lite",
        degrade_reason=reason,
        ui_locale=locale,
    )
    if platform.system() != "Darwin":
        try:
            _launch_host_stopper()
        except (runner.AuditError, OSError, ValueError):
            try:
                runner.complete_local_advisory_run(local_id, status="failed")
            except (runner.AuditError, OSError, ValueError):
                pass
            raise
    return {
        "status": "running",
        "local_id": local_id,
        "local_surface": "de_lite",
        "degrade_reason": reason,
        "fallback_mode": "session-llm",
        "audit_id": None,
        "advisory_only": True,
        "ui_locale": locale,
    }


def complete(local_id: str, status: str) -> Dict[str, Any]:
    """Finish one existing DE Lite advisory through the runner's terminal guard."""
    if not local_id.startswith("local_host_") or not local_id[len("local_host_") :]:
        raise runner.AuditError("host local advisory run not found")
    run = runner.complete_local_advisory_run(local_id, status=status)
    return {
        "status": run["status"],
        "local_id": run["run_id"],
        "local_surface": run["local_surface"],
        "degrade_reason": run.get("degrade_reason", ""),
        "fallback_mode": run["fallback_mode"],
        "audit_id": None,
        "advisory_only": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage a host-only DE Lite local audit lifecycle."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    begin_command = commands.add_parser(
        "begin", help="start one MCP-unavailable local advisory"
    )
    begin_command.add_argument(
        "--title", required=True,
        help="the user's explicit audit topic; artifact content is not accepted",
    )
    begin_command.add_argument(
        "--ui-locale", choices=("zh-CN", "en-US"), default=None,
    )
    finish = commands.add_parser("complete", help="finish an existing local advisory")
    finish.add_argument("--local-id", required=True)
    finish.add_argument(
        "--status", required=True, choices=("completed", "partial", "failed")
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "begin":
            result = begin(args.title, args.ui_locale)
        elif args.command == "complete":
            result = complete(args.local_id, args.status)
        else:  # argparse keeps this unreachable; retain a fail-closed dispatch boundary.
            return 2
    except (runner.AuditError, OSError, ValueError):
        reason = (
            "local-advisory-rejected"
            if args.command == "complete"
            else "local-bridge-failed"
        )
        sys.stderr.write(json.dumps({"status": "failed", "reason": reason}) + "\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
