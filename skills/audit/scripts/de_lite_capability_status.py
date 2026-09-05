"""Read the local DE activation state without contacting the Hub or exposing secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Dict


DE_ROOT = Path(__file__).resolve().parents[3]
_MAX_CONFIG_BYTES = 128 * 1024
_STATUSES = ("unactivated", "activated", "unknown")


def _config_path() -> Path:
    explicit = os.environ.get("DE_DEVICE_CONFIG_PATH")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()
    configured_home = os.environ.get("DEEPPATTERN_HOME")
    deeppattern_home = (
        Path(configured_home).expanduser()
        if configured_home is not None
        else Path.home() / ".deeppattern"
    )
    return deeppattern_home / "decision-engine" / "config.json"


def _windows_security_module():
    module_path = DE_ROOT / "client" / "windows_security.py"
    if not module_path.is_file():
        raise OSError("installed Decision Engine root is invalid")
    if str(DE_ROOT) not in sys.path:
        sys.path.insert(0, str(DE_ROOT))
    try:
        from client import windows_security
    except ImportError as exc:
        raise OSError("Windows ACL support is unavailable") from exc
    return windows_security


def _read_private_json(path: Path) -> Dict[str, Any]:
    if path.is_symlink():
        raise OSError("config symlink refused")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("config is not a regular file")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise OSError("config is not user-owned")
        if os.name == "nt":
            windows_security = _windows_security_module()
            try:
                windows_security.validate_private_data_fd(fd)
            except windows_security.WindowsSecurityError as exc:
                raise OSError("config is not private") from exc
        elif stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise OSError("config is not private")
        if file_stat.st_size > _MAX_CONFIG_BYTES:
            raise OSError("config is oversized")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise OSError("config is oversized")
    loaded = json.loads(raw.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("config is not an object")
    return loaded


def capability_status() -> str:
    """Return a conservative local status; no status other than these three is emitted."""
    path = _config_path()
    if not path.exists():
        return "unactivated"
    try:
        config = _read_private_json(path)
    except (OSError, UnicodeError, ValueError, TypeError):
        return "unknown"
    token = config.get("access_token")
    return "activated" if isinstance(token, str) and token.strip() else "unactivated"


def main() -> int:
    sys.stdout.write(json.dumps({"status": capability_status()}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
