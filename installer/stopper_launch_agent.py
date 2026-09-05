"""Install the macOS host-side Stopper wakeup agent used when MCP is absent."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterator, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - the mutating path is macOS-only
    fcntl = None  # type: ignore


LABEL = "com.decision-engine.stopper.hostbridge"
PLIST_NAME = LABEL + ".plist"
MANAGED_MARKER_ENV = "DE_STOPPER_HOSTBRIDGE_MANAGED"
_MAX_PLIST_BYTES = 128 * 1024
_MAX_REGISTRY_BYTES = 4 * 1024 * 1024
_ABSENT_MARKERS = ("could not find service", "service not found")
_SENSITIVE_LOADED_ENV_FRAGMENTS = (
    "ENDPOINT",
    "TOKEN",
    "SECRET",
    "ACCOUNT",
    "CREDENTIAL",
    "PASSWORD",
    "ARTIFACT",
)


class LaunchAgentError(RuntimeError):
    """The owned LaunchAgent could not be safely installed or removed."""


def _identity() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else "user"


def _domain() -> str:
    return "gui/%s" % _identity()


def _target() -> str:
    return "%s/%s" % (_domain(), LABEL)


def launch_agent_path(*, home: Optional[Path] = None) -> Path:
    root = Path(home) if home is not None else Path.home()
    return root / "Library" / "LaunchAgents" / PLIST_NAME


def host_runtime_root(*, temp_dir: Optional[Path] = None) -> Path:
    if temp_dir is not None:
        root = Path(temp_dir)
    elif platform.system() == "Darwin":
        root = Path("/private/tmp")
    else:
        root = Path(tempfile.gettempdir())
    return root / ("decision-engine-host-bridge-%s" % _identity())


def _is_link_like(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _ensure_directory(path: Path, *, private: bool = False) -> None:
    if _is_link_like(path):
        raise LaunchAgentError("refusing a symlink directory: %s" % path)
    try:
        path.mkdir(mode=0o700 if private else 0o755, parents=True, exist_ok=True)
    except OSError as exc:
        raise LaunchAgentError("could not create directory: %s" % path) from exc
    try:
        directory_stat = path.lstat()
    except OSError as exc:
        raise LaunchAgentError("runtime directory could not be verified: %s" % path) from exc
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise LaunchAgentError("runtime path is not a real directory: %s" % path)
    if private:
        if hasattr(os, "getuid") and directory_stat.st_uid != os.getuid():
            raise LaunchAgentError("runtime directory is not owned by this user: %s" % path)
        try:
            os.chmod(path, 0o700, follow_symlinks=False)
        except (NotImplementedError, TypeError):
            path.chmod(0o700)
        try:
            directory_stat = path.lstat()
        except OSError as exc:
            raise LaunchAgentError(
                "private runtime directory could not be verified: %s" % path
            ) from exc
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or (hasattr(os, "getuid") and directory_stat.st_uid != os.getuid())
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise LaunchAgentError("runtime directory is not private: %s" % path)


def _require_private_directory(path: Path) -> None:
    try:
        directory_stat = path.lstat()
    except OSError as exc:
        raise LaunchAgentError("private runtime directory is unavailable: %s" % path) from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or (hasattr(os, "getuid") and directory_stat.st_uid != os.getuid())
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise LaunchAgentError("runtime directory is not private: %s" % path)


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    _ensure_directory(path.parent)
    if _is_link_like(path):
        raise LaunchAgentError("refusing a symlink file: %s" % path)
    fd, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, mode, follow_symlinks=False)
    except OSError as exc:
        raise LaunchAgentError("could not publish owned file: %s" % path) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _read_owned_bounded_file(
    path: Path,
    *,
    max_bytes: int,
    description: str,
    private: bool = False,
) -> bytes:
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
            raise LaunchAgentError("%s is not a regular file" % description)
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise LaunchAgentError("%s is not owned by this user" % description)
        if private and stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise LaunchAgentError("%s is not private" % description)
        if file_stat.st_size > max_bytes:
            raise LaunchAgentError("%s is oversized" % description)
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise LaunchAgentError("%s is oversized" % description)
        return raw
    except LaunchAgentError:
        raise
    except OSError as exc:
        raise LaunchAgentError("%s is unreadable" % description) from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _read_plist(path: Path) -> tuple[bytes, Dict[str, Any]]:
    try:
        raw = _read_owned_bounded_file(
            path,
            max_bytes=_MAX_PLIST_BYTES,
            description="LaunchAgent plist",
        )
        payload = plistlib.loads(raw)
    except LaunchAgentError:
        raise
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise LaunchAgentError("LaunchAgent plist is unreadable or invalid") from exc
    if not isinstance(payload, dict):
        raise LaunchAgentError("LaunchAgent plist is not a dictionary")
    return raw, payload


def _is_owned(payload: Dict[str, Any]) -> bool:
    environment = payload.get("EnvironmentVariables")
    return (
        payload.get("Label") == LABEL
        and isinstance(environment, dict)
        and environment.get(MANAGED_MARKER_ENV) == "1"
    )


def _run_launchctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchAgentError("launchctl execution failed") from exc


def _launchctl_reports_absent(
    result: subprocess.CompletedProcess[str],
) -> bool:
    output = "%s\n%s" % (result.stdout or "", result.stderr or "")
    lowered = output.lower()
    return result.returncode != 0 and any(
        marker in lowered for marker in _ABSENT_MARKERS
    )


def _launchctl_arguments(lines: list[str]) -> Optional[list[str]]:
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


def _launchctl_watch_paths(lines: list[str]) -> Optional[list[str]]:
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


def _launchctl_environment(lines: list[str]) -> Optional[Dict[str, str]]:
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


def _loaded_job_matches(
    result: subprocess.CompletedProcess[str],
    *,
    plist_path: Path,
    payload: Dict[str, Any],
) -> bool:
    if result.returncode != 0:
        return False
    arguments = payload.get("ProgramArguments")
    watched = payload.get("WatchPaths")
    environment = payload.get("EnvironmentVariables")
    if not isinstance(arguments, list) or len(arguments) != 1:
        return False
    if not isinstance(watched, list) or len(watched) != 1:
        return False
    if not isinstance(environment, dict):
        return False
    output_lines = (result.stdout or "").splitlines()
    lines = {line.strip() for line in output_lines}
    required = {
        "path = %s" % plist_path,
        "program = %s" % arguments[0],
    }
    if not required.issubset(lines):
        return False
    if _launchctl_arguments(output_lines) != arguments:
        return False
    if _launchctl_watch_paths(output_lines) != watched:
        return False
    loaded_environment = _launchctl_environment(output_lines)
    if loaded_environment is None or any(
        loaded_environment.get(name) != value for name, value in environment.items()
    ):
        return False
    for name in loaded_environment:
        normalized = name.upper()
        if normalized not in environment and any(
            fragment in normalized for fragment in _SENSITIVE_LOADED_ENV_FRAGMENTS
        ):
            return False
    return True


def _ensure_service_absent() -> None:
    _run_launchctl(["/bin/launchctl", "bootout", _target()])
    checked = _run_launchctl(["/bin/launchctl", "print", _target()])
    if not _launchctl_reports_absent(checked):
        raise LaunchAgentError("Stopper LaunchAgent unload could not be verified")


def _service_loaded_state(path: Path, payload: Dict[str, Any]) -> bool:
    checked = _run_launchctl(["/bin/launchctl", "print", _target()])
    if checked.returncode == 0:
        if _loaded_job_matches(checked, plist_path=path, payload=payload):
            return True
        raise LaunchAgentError("existing loaded Stopper LaunchAgent did not verify")
    if _launchctl_reports_absent(checked):
        return False
    raise LaunchAgentError("existing Stopper LaunchAgent state is unknown")


@contextlib.contextmanager
def _management_lock(*, temp_dir: Optional[Path] = None) -> Iterator[None]:
    if fcntl is None:
        raise LaunchAgentError("host LaunchAgent locking is unavailable")
    paths = _runtime_paths(temp_dir=temp_dir)
    for directory in (paths["root"], paths["state"], paths["locks"]):
        _ensure_directory(directory, private=True)
    path = paths["locks"] / "stopper-hostbridge-management.lock"
    if _is_link_like(path):
        raise LaunchAgentError("refusing a symlink management lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise LaunchAgentError("management lock is not a regular file")
        if hasattr(os, "getuid") and lock_stat.st_uid != os.getuid():
            raise LaunchAgentError("management lock is not owned by this user")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except LaunchAgentError:
        if "fd" in locals():
            os.close(fd)
        raise
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        raise LaunchAgentError("could not acquire the host management lock") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _runtime_paths(*, temp_dir: Optional[Path] = None) -> Dict[str, Path]:
    root = host_runtime_root(temp_dir=temp_dir)
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


def _validate_registry(path: Path) -> None:
    try:
        raw = _read_owned_bounded_file(
            path,
            max_bytes=_MAX_REGISTRY_BYTES,
            description="host registry",
            private=True,
        )
        loaded = json.loads(raw.decode("utf-8"))
    except LaunchAgentError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LaunchAgentError("host registry is unreadable or invalid") from exc
    if not isinstance(loaded, dict):
        raise LaunchAgentError("host registry is not a dictionary")
    if type(loaded.get("schema_version")) is not int or loaded["schema_version"] != 1:
        raise LaunchAgentError("host registry schema is unsupported")
    if not isinstance(loaded.get("runs"), dict):
        raise LaunchAgentError("host registry runs are invalid")
    updated_at = loaded.get("updated_at")
    if (
        type(updated_at) not in (int, float)
        or not math.isfinite(float(updated_at))
        or float(updated_at) > time.time() + 300.0
    ):
        raise LaunchAgentError("host registry timestamp is invalid")


def _environment_for_paths(paths: Dict[str, Path]) -> Dict[str, str]:
    return {
        MANAGED_MARKER_ENV: "1",
        "DE_CONFIG_PATH": str(paths["config"]),
        "DE_ACTIVE_RUN": str(paths["active_run"]),
        "DE_ACTIVE_RUNS": str(paths["active_runs"]),
        "DE_ACTIVE_RUNS_LOCK": str(paths["active_runs_lock"]),
        "DE_ACTIVE_RUNS_LOCK_PATHS": json.dumps([str(paths["active_runs_lock"])]),
        "DE_ACTIVE_RUNS_WRITE_PATHS": json.dumps([str(paths["active_runs"])]),
        "DE_ACTIVE_RUN_WRITE_PATHS": json.dumps([str(paths["active_run"])]),
        "DE_STOPPER_SINGLETON_LOCK": str(paths["stopper_lock"]),
    }


def _runtime_contract(*, temp_dir: Optional[Path] = None) -> Dict[str, Any]:
    paths = _runtime_paths(temp_dir=temp_dir)
    root = paths["root"]
    state = paths["state"]
    locks = paths["locks"]
    logs = paths["logs"]
    for directory in (root, state, locks, logs):
        _ensure_directory(directory, private=True)

    active_runs = paths["active_runs"]
    if not active_runs.exists():
        registry = {
            "schema_version": 1,
            "runs": {},
            "updated_at": time.time(),
        }
        _atomic_write(
            active_runs,
            (json.dumps(registry, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    elif _is_link_like(active_runs):
        raise LaunchAgentError("refusing a symlink host registry")
    else:
        try:
            registry_stat = active_runs.stat()
            if not stat.S_ISREG(registry_stat.st_mode):
                raise LaunchAgentError("host registry is not a regular file")
            if hasattr(os, "getuid") and registry_stat.st_uid != os.getuid():
                raise LaunchAgentError("host registry is not owned by this user")
            os.chmod(active_runs, 0o600, follow_symlinks=False)
        except LaunchAgentError:
            raise
        except (NotImplementedError, OSError, TypeError) as exc:
            raise LaunchAgentError("could not secure the host registry") from exc
        _validate_registry(active_runs)

    return {
        "registry": active_runs,
        "environment": _environment_for_paths(paths),
        "log_path": paths["log"],
    }


def _payload(de_root: Path, *, temp_dir: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(de_root).expanduser().resolve()
    from client.runner import (  # lazy: same idiom as the other client imports
        shipped_stopper_arch_supported,
        shipped_stopper_path,
    )

    stopper = shipped_stopper_path(root)
    if stopper is None:
        raise LaunchAgentError("no shipped native Stopper supports this Mac architecture")
    if not stopper.is_file() or not os.access(stopper, os.X_OK):
        raise LaunchAgentError("shipped native Stopper is missing or not executable")
    if not shipped_stopper_arch_supported():
        raise LaunchAgentError(
            "no shipped native Stopper supports this Mac architecture"
        )
    runtime = _runtime_contract(temp_dir=temp_dir)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(stopper)],
        "EnvironmentVariables": runtime["environment"],
        "WatchPaths": [str(runtime["registry"])],
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(runtime["log_path"]),
        "StandardErrorPath": str(runtime["log_path"]),
    }


def _expected_payload(
    de_root: Path, *, temp_dir: Optional[Path] = None
) -> Dict[str, Any]:
    if platform.system() != "Darwin":
        raise LaunchAgentError("native Stopper LaunchAgent is macOS-only")
    root = Path(de_root).expanduser().resolve()
    from client.runner import shipped_stopper_path

    stopper = shipped_stopper_path(root)
    if stopper is None:
        raise LaunchAgentError("no shipped native Stopper supports this Mac architecture")
    paths = _runtime_paths(temp_dir=temp_dir)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(stopper)],
        "EnvironmentVariables": _environment_for_paths(paths),
        "WatchPaths": [str(paths["active_runs"])],
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(paths["log"]),
        "StandardErrorPath": str(paths["log"]),
    }


def _require_plist_bytes(path: Path, expected: bytes) -> Dict[str, Any]:
    raw, payload = _read_plist(path)
    if raw != expected or not _is_owned(payload):
        raise LaunchAgentError("owned LaunchAgent changed during the operation")
    return payload


def _restore(path: Path, previous: Optional[bytes], *, previous_loaded: bool) -> None:
    _ensure_service_absent()
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise LaunchAgentError("could not remove the failed LaunchAgent") from exc
        return
    _atomic_write(path, previous)
    payload = _require_plist_bytes(path, previous)
    if not previous_loaded:
        checked = _run_launchctl(["/bin/launchctl", "print", _target()])
        if not _launchctl_reports_absent(checked):
            raise LaunchAgentError(
                "previous unloaded Stopper LaunchAgent state did not verify"
            )
        return
    restored = _run_launchctl(["/bin/launchctl", "bootstrap", _domain(), str(path)])
    if restored.returncode != 0:
        raise LaunchAgentError("previous Stopper LaunchAgent could not be restored")
    verified = _run_launchctl(["/bin/launchctl", "print", _target()])
    if not _loaded_job_matches(verified, plist_path=path, payload=payload):
        raise LaunchAgentError("restored Stopper LaunchAgent did not verify")


def install(
    de_root: Path,
    *,
    home: Optional[Path] = None,
    temp_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "not-applicable", "platform": platform.system()}
    with _management_lock(temp_dir=temp_dir):
        path = launch_agent_path(home=home)
        previous: Optional[bytes] = None
        previous_loaded = False
        if path.exists() or _is_link_like(path):
            previous, existing = _read_plist(path)
            if not _is_owned(existing):
                raise LaunchAgentError("refusing to replace an unowned LaunchAgent")
            previous_loaded = _service_loaded_state(path, existing)

        payload = _payload(Path(de_root), temp_dir=temp_dir)
        rendered = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
        try:
            _atomic_write(path, rendered)
            _require_plist_bytes(path, rendered)
            _ensure_service_absent()
            loaded = _run_launchctl(
                ["/bin/launchctl", "bootstrap", _domain(), str(path)]
            )
            if loaded.returncode != 0:
                raise LaunchAgentError(
                    "could not bootstrap the owned Stopper LaunchAgent"
                )
            verified = _run_launchctl(["/bin/launchctl", "print", _target()])
            if not _loaded_job_matches(verified, plist_path=path, payload=payload):
                raise LaunchAgentError(
                    "owned Stopper LaunchAgent did not verify after bootstrap"
                )
            _require_plist_bytes(path, rendered)
        except LaunchAgentError:
            try:
                _restore(path, previous, previous_loaded=previous_loaded)
            except LaunchAgentError as rollback_exc:
                raise LaunchAgentError(
                    "Stopper LaunchAgent install failed and rollback did not complete"
                ) from rollback_exc
            raise
        return {
            "status": "installed",
            "path": str(path),
            "label": LABEL,
            "registry": payload["WatchPaths"][0],
        }


def status(
    *,
    home: Optional[Path] = None,
    de_root: Optional[Path] = None,
    temp_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "not-applicable", "platform": platform.system()}
    path = launch_agent_path(home=home)
    if not path.exists() and not _is_link_like(path):
        return {"status": "missing", "path": str(path)}
    try:
        _raw, payload = _read_plist(path)
    except LaunchAgentError:
        return {"status": "invalid", "path": str(path)}
    if not _is_owned(payload):
        return {"status": "unowned", "path": str(path)}
    root = Path(de_root) if de_root is not None else Path(__file__).resolve().parents[1]
    expected = _expected_payload(root, temp_dir=temp_dir)
    paths = _runtime_paths(temp_dir=temp_dir)
    try:
        for directory in (
            paths["root"],
            paths["state"],
            paths["locks"],
            paths["logs"],
        ):
            _require_private_directory(directory)
        _validate_registry(paths["active_runs"])
    except LaunchAgentError:
        return {"status": "stale", "path": str(path), "loaded": False}
    contract_ready = payload == expected
    arguments = payload.get("ProgramArguments")
    program = arguments[0] if isinstance(arguments, list) and arguments else ""
    try:
        checked = _run_launchctl(["/bin/launchctl", "print", _target()])
    except LaunchAgentError:
        return {
            "status": "stale",
            "path": str(path),
            "program": program,
            "loaded": False,
        }
    loaded = _loaded_job_matches(checked, plist_path=path, payload=payload)
    ready = bool(
        contract_ready
        and program
        and Path(program).is_file()
        and os.access(program, os.X_OK)
    )
    return {
        "status": "ready" if loaded and ready else "stale",
        "path": str(path),
        "program": program,
        "loaded": loaded,
    }


def uninstall(
    *,
    home: Optional[Path] = None,
    quarantine_root: Path,
    temp_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "not-applicable", "platform": platform.system()}
    with _management_lock(temp_dir=temp_dir):
        path = launch_agent_path(home=home)
        if not path.exists() and not _is_link_like(path):
            return {"status": "absent", "path": str(path)}
        original, payload = _read_plist(path)
        if not _is_owned(payload):
            raise LaunchAgentError("refusing to quarantine an unowned LaunchAgent")
        quarantine = Path(quarantine_root).expanduser()
        _ensure_directory(quarantine, private=True)
        target = quarantine / PLIST_NAME
        if target.exists() or _is_link_like(target):
            raise LaunchAgentError("LaunchAgent quarantine target already exists")
        _ensure_service_absent()
        _require_plist_bytes(path, original)
        try:
            os.replace(path, target)
        except OSError as exc:
            raise LaunchAgentError(
                "could not quarantine the owned LaunchAgent"
            ) from exc
        return {
            "status": "quarantined",
            "path": str(path),
            "quarantine": str(target),
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the DE Stopper host LaunchAgent."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install")
    setup.add_argument("--de-root", required=True)
    commands.add_parser("status")
    remove = commands.add_parser("uninstall")
    remove.add_argument("--quarantine-root", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "install":
            result = install(Path(args.de_root))
        elif args.command == "status":
            result = status()
        else:
            result = uninstall(quarantine_root=Path(args.quarantine_root))
    except LaunchAgentError as exc:
        sys.stderr.write("de-stopper-hostbridge: %s\n" % exc)
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
