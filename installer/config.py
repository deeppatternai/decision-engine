"""Filesystem + config helpers for the Decision Engine public shell.

Kept intentionally tiny and dependency-free (stdlib only) so the shell stays
auditable and portable. All user-facing paths are environment-overridable so
tests can run fully hermetically against a temp HOME.

No server IP, secret, or device token is ever hardcoded here — the endpoint and
credentials come from the owner-issued API key flow and live only in the
per-device ``config.json`` written at install/activation time.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import secrets
import socket
from pathlib import Path
from typing import Any, Dict

from client import windows_security

# Real bodies install side-by-side under here (release design §9):
#   ~/.deeppattern/decision-engine/   ~/.deeppattern/agent-quality-gates/
# Only the first is this module's business — component_root() is for bodies THIS installer lays
# down. AQG is cloned and installed by its own repo's scripts/install.sh (install.sh drives it),
# and is named for that repo; ~/.deeppattern/aqg/ is a path nothing creates.
DEFAULT_DEEPPATTERN_HOME = Path.home() / ".deeppattern"

# Skills are routed (symlinked) into each supported agent's skills dir
# (release design §9, implemented here in stdlib Python).
DEFAULT_CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
DEFAULT_CODEX_SKILLS_DIR = Path.home() / ".codex" / "skills"
ACTIVATION_RECOVERY_RELATIVE_PATH = Path(".runtime") / "activation-recovery-required.json"
DEFAULT_CURSOR_SKILLS_DIR = Path.home() / ".cursor" / "skills"


class ShellError(RuntimeError):
    """User-facing installer/shim error (message is safe to print)."""


def deeppattern_home() -> Path:
    return Path(
        os.getenv("DEEPPATTERN_HOME", str(DEFAULT_DEEPPATTERN_HOME))
    ).expanduser()


def claude_skills_dir() -> Path:
    return Path(
        os.getenv("CLAUDE_SKILLS_DIR", str(DEFAULT_CLAUDE_SKILLS_DIR))
    ).expanduser()


def codex_skills_dir() -> Path:
    configured = os.getenv("CODEX_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return DEFAULT_CODEX_SKILLS_DIR.expanduser()


def codex_skills_in_use() -> bool:
    """Whether Codex skill routing was explicitly requested or Codex exists."""

    configured = os.getenv("CODEX_SKILLS_DIR")
    return bool(configured and configured.strip()) or codex_skills_dir().parent.exists()


def cursor_skills_dir() -> Path:
    configured = os.getenv("CURSOR_SKILLS_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return DEFAULT_CURSOR_SKILLS_DIR.expanduser()


def component_root(component: str) -> Path:
    """Install root for a component body, e.g. ~/.deeppattern/decision-engine."""
    return deeppattern_home() / component


def managed_component_root(component: str) -> Path:
    """Fixed product-managed root; deliberately ignores DEEPPATTERN_HOME.

    Hermetic developer/test installs may use ``component_root``. A checkout
    that can later authorize managed Git repair must use this fixed entry point.
    """
    return DEFAULT_DEEPPATTERN_HOME / component


def de_config_path() -> Path:
    """Per-device Decision Engine config (API key/binding/endpoint).

    Release design §9: token stored in
    ``~/.deeppattern/decision-engine/config.json``.
    """
    override = os.getenv("DE_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return component_root("decision-engine") / "config.json"


def device_name_default() -> str:
    hostname = socket.gethostname().split(".")[0] or "device"
    return "%s-%s" % (getpass.getuser(), hostname)


def device_fingerprint() -> str:
    """Stable-ish device fingerprint (release design §9, honest soft binding).

    Software-level only: copying the config file or resetting a VM defeats it.
    That residual is accepted upstream (§9) and bounded by max-devices + revoke
    + per-account daily caps. This value is a plain hash of non-secret host
    attributes — it is not a credential.
    """
    raw = "|".join(
        [
            socket.gethostname(),
            platform.platform(),
            platform.machine(),
            getpass.getuser(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ShellError("invalid JSON at %s: %s" % (path, exc))
    return loaded if isinstance(loaded, dict) else {}


def atomic_write_json(path: Path, payload: Dict[str, Any], *, mode: int = 0o600) -> None:
    """Write JSON atomically with restrictive perms (dir 0700, file 0600).

    The secret-bearing config (api_key / device_fingerprint / access_token)
    must never transiently exist at the process umask (often world-readable).
    So the temp file is *created* with ``mode`` via an ``os.open`` opener — the
    kernel applies the mode at creation, and ``os.replace`` preserves it onto
    the final path. umask can only *remove* bits, never widen them, so the file
    is at most ``mode`` at every instant; there is no post-hoc chmod window to
    fail open. The best-effort chmods only *restore* owner bits a tight umask
    may have stripped.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    windows_guard = None
    if os.name != "nt":
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_name(
        ".%s.%d.%s.tmp" % (path.name, os.getpid(), secrets.token_hex(8))
    )
    tmp_created = False

    def _opener(name: str, flags: int) -> int:
        return os.open(name, flags | os.O_EXCL, mode)

    try:
        if os.name == "nt":
            try:
                windows_guard = windows_security.PinnedWindowsDirectory(path.parent)
                windows_guard.__enter__()
                windows_guard.validate()
                windows_security.harden_private_data_acl(path.parent)
                windows_guard.validate()
            except windows_security.WindowsSecurityError as exc:
                raise ShellError("could not secure the Windows config directory") from exc
        with open(tmp_path, "w", encoding="utf-8", opener=_opener) as handle:
            tmp_created = True
            if os.name == "nt":
                try:
                    windows_security.validate_private_data_fd(handle.fileno())
                except windows_security.WindowsSecurityError as exc:
                    raise ShellError("could not create a private Windows config file") from exc
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp_path.chmod(mode)
        except OSError:
            pass
        os.replace(tmp_path, path)
        if os.name == "nt":
            try:
                windows_security.validate_private_data_acl(path)
                windows_guard.validate()
            except windows_security.WindowsSecurityError as exc:
                raise ShellError("could not verify the private Windows config file") from exc
        try:
            path.chmod(mode)
        except OSError:
            pass
    finally:
        if windows_guard is not None:
            windows_guard.__exit__(None, None, None)
        if tmp_created:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def harden_existing_windows_config(path: Path) -> bool:
    """Repair the ACL on an existing Windows device config without rewriting it."""

    if os.name != "nt":
        return False
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    try:
        with windows_security.PinnedWindowsDirectory(path.parent) as pinned:
            pinned.validate()
            windows_security.harden_private_data_acl(path.parent)
            pinned.validate()
            windows_security.harden_private_data_file_acl(path)
            windows_security.validate_private_data_acl(path)
    except windows_security.WindowsSecurityError as exc:
        raise ShellError("could not secure the existing Windows config") from exc
    return True


def normalize_endpoint(endpoint: str) -> str:
    """Normalize an owner-provided server endpoint URL (no hardcoded default)."""
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        raise ShellError("server endpoint is required (owner-issued, e.g. https://<host>)")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


# Hosts for which plaintext http:// is tolerated (local dev only). Shared by the
# shim (device token) and the activation client (API key) so both apply the
# same cleartext-credential red line.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def is_local_host(endpoint: str) -> bool:
    """True when ``endpoint`` targets a loopback host (dev-only plaintext ok)."""
    from urllib.parse import urlsplit

    return (urlsplit(endpoint).hostname or "") in _LOCAL_HOSTS
