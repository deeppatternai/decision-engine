#!/bin/zsh
# Deep Pattern (DP) managed-install cleanup tool.
# macOS is implemented first; Linux and Windows remain fail-closed until their
# host integration contracts are added.
# Repository hosting is deliberately irrelevant here: ownership is proven from
# managed local roots and host entries, so an organization migration can never
# broaden what this tool is allowed to remove.
set -euo pipefail

python_bin="${DE_AQG_PYTHON:-}"
if [[ -z "${python_bin}" ]]; then
  python_bin="$(command -v python3 2>/dev/null || true)"
fi
if [[ -z "${python_bin}" ]]; then
  python_bin="$(command -v python 2>/dev/null || true)"
fi
if [[ -z "${python_bin}" || ! -x "${python_bin}" ]]; then
  print -u2 'dp-uninstall: Python 3 is required.'
  exit 2
fi

exec "${python_bin}" - "$@" <<'PY'
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 3
EXIT_UNSUPPORTED = 4

HOSTBRIDGE_LABEL = "com.decision-engine.stopper.hostbridge"
HOSTBRIDGE_MARKER = "DE_STOPPER_HOSTBRIDGE_MANAGED"
ROUTING_START = "<!-- BEGIN decision-engine routing (managed by installer.codex_routing; do not edit inside) -->"
ROUTING_END = "<!-- END decision-engine routing -->"
HOST_BACKUP_PATTERNS = ("*.de-bak.*", "*.de-bak-*")
QODER_HOOK_NAMES = {
    "decision-engine-audit-routing-v1",
    "decision-engine-audit-routing-experiment",
}
QODER_HOOK_SCRIPT_SUFFIX = "/installer/qoder_audit_prompt_hook.py"
COMMAND_PROMPT_HOOK_SPECS = {
    "decision-engine-workbuddy-ai-audit-routing-v1": (
        "/installer/workbuddy_audit_prompt_hook.py"
    ),
    "decision-engine-trae-cn-audit-routing-v1": (
        "/installer/trae_cn_audit_prompt_hook.py"
    ),
}
AQG_SUPPORT_DESCRIPTION = (
    "AQG diagnostic connector placeholder; lifecycle gates remain hook/rule driven."
)
AQG_HOOK_MARKERS = (
    "--managed-id aqg-",
    "cursor_aqg_hook.py",
    "agent_client_aqg_hook.py",
    "qoder_hook_adapter.py",
    "run_aqg_codex_hook.py",
    "$AQG_ROOT/agent-packs/claude-code/hooks/",
)
AQG_ROOT_PATH_MARKERS = (
    "/scripts/aqg_doctor.py",
    "/scripts/cursor_aqg_hook.py",
    "/scripts/agent_client_aqg_hook.py",
    "/scripts/run_aqg_codex_hook.py",
    "/agent-packs/qoder/hooks/qoder_hook_adapter.py",
    "/agent-packs/claude-code/",
)
AQG_WORK_MARKER_MANAGERS = {"AQG", "aqg-work-client-support"}
AQG_PRODUCT_REMOTES = {
    "https://github.com/deeppatternai/agent-quality-gates.git",
    "git@github.com:deeppatternai/agent-quality-gates.git",
    "ssh://git@github.com/deeppatternai/agent-quality-gates.git",
}
QODER_DE_PLUGIN_ID = "decision-engine@de-bundler"
QODER_DE_PLUGIN_HOMEPAGES = {
    "https://github.com/deeppatternai/decision-engine",
}
QODER_PROCESS_MARKERS = (
    "/Applications/Qoder.app/",
    "/Applications/Qoder IDE.app/",
    "/Applications/Qoder CN.app/",
    "/Applications/Qoder CN IDE.app/",
)


def lex(path: Path) -> Path:
    """Normalize without resolving the final path through a symlink."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def is_under(path: Path, parent: Path) -> bool:
    try:
        lex(path).relative_to(lex(parent))
        return True
    except ValueError:
        return False


def mode_text(path: Path) -> str:
    try:
        return stat.filemode(path.lstat().st_mode)
    except OSError:
        return "unknown"


def safe_size(path: Path) -> int | None:
    try:
        return path.lstat().st_size
    except OSError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_has_symlink_component(path: Path, floor: Path) -> Path | None:
    path = lex(path)
    floor = lex(floor)
    try:
        relative = path.relative_to(floor)
    except ValueError:
        return path
    current = floor
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def flatten_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from flatten_strings(key)
            yield from flatten_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from flatten_strings(item)


class Inventory:
    def __init__(self, home: Path, scope: str) -> None:
        self.home = lex(home)
        self.scope = scope
        self.dp = self.home / ".deeppattern"
        self.de = self.dp / "decision-engine"
        self.aqg = self.dp / "agent-quality-gates"
        self.de_source = self.dp / "decision-engine-root"
        self.protected = (self.de_source,)
        self.de_refs = (self.de, self.de_source)
        self.aqg_refs = (self.aqg,)
        self.actions: list[dict[str, Any]] = []
        self.blockers: list[str] = []
        self.notes: list[str] = []
        self.json_targets: list[tuple[Path, tuple[tuple[str, ...], ...]]] = []
        self.toml_ranges: list[tuple[Path, tuple[tuple[int, int], ...]]] = []
        self.agents_target: Path | None = None
        self.skill_links: list[Path] = []
        self.aqg_skill_markers: list[tuple[Path, str]] = []
        self.host_backups: list[Path] = []
        self.de_aux: list[Path] = []
        self.aqg_aux: list[Path] = []
        self.runtime_roots: list[Path] = []
        self.launchagent: Path | None = None
        self.launchagent_loaded = False
        self.processes: list[dict[str, str]] = []
        self.aqg_uninstaller: Path | None = None
        self.aqg_managed_target: Path | None = None
        self.qoder_residue_paths: list[tuple[Path, str]] = []

    def add_action(self, kind: str, path: Path, detail: str = "") -> None:
        self.actions.append({"kind": kind, "path": str(path), "detail": detail})

    def path_ref(self, path: Path, *, component: str) -> bool:
        path = lex(path)
        refs = self.de_refs if component == "de" else self.aqg_refs
        # Keep the lexical identity even when the target is dangling. An
        # earlier failed uninstall can leave a symlink behind after its root
        # has already moved; that link is still an owned residue.
        if any(is_under(path, ref) for ref in refs):
            return True
        for parent in (path, *path.parents):
            if parent.name != "skills":
                continue
            root = parent.parent
            if component == "de" and all(
                (root / item).exists()
                for item in ("installer", "client", "desktop", "pyproject.toml")
            ):
                return True
            if component == "aqg" and all(
                (root / item).exists()
                for item in ("scripts", "VERSION", "requirements.txt")
            ):
                return True
            if component == "aqg" and root.name == "claude-code" and root.parent.name == "agent-packs":
                checkout = root.parent.parent
                if all(
                    (checkout / item).exists()
                    for item in ("scripts", "VERSION", "requirements.txt")
                ):
                    return True
        return False

    def text_has_ref(self, text: str, *, component: str) -> bool:
        expanded = os.path.expanduser(text)
        refs = self.de_refs if component == "de" else self.aqg_refs
        for ref in refs:
            ref_text = str(lex(ref))
            if ref_text in expanded:
                return True
            if ref_text.startswith(str(self.home) + os.sep):
                tilde = "~" + ref_text[len(str(self.home)) :]
                if tilde in text:
                    return True
        if component == "de":
            if "from installer.launcher import main" in expanded:
                return True
            if re.search(r"(?:^|[\s'\"])-m[\s'\"]+installer\.launcher(?:[\s'\"]|$)", expanded):
                return True
            if re.search(r"/(?:decision-engine|decision-engine-root)(?:/|['\"` ]|$)", expanded):
                return True
        return False

    @staticmethod
    def python_command(entry: dict[str, Any]) -> bool:
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            return False
        return re.fullmatch(r"python(?:3(?:\.\d+)*)?", Path(command).name) is not None

    @classmethod
    def de_connector_owned(cls, entry: dict[str, Any]) -> bool:
        if not cls.python_command(entry):
            return False
        args = entry.get("args")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            return False
        module_launcher = any(
            args[index : index + 2] == ["-m", "installer.launcher"]
            for index in range(max(0, len(args) - 1))
        )
        inline_launcher = any("from installer.launcher import main" in item for item in args)
        absolute_bootstrap = False
        if len(args) == 4 and args[2] in {"--dev-root", "--managed-root"}:
            bootstrap = Path(args[0])
            root = Path(args[1])
            bound_root = Path(args[3])
            absolute_bootstrap = (
                bootstrap.is_absolute()
                and root.is_absolute()
                and bound_root.is_absolute()
                and lex(root) == lex(bound_root)
                and lex(bootstrap) == lex(root / "installer" / "mcp_bootstrap.py")
            )
        return module_launcher or inline_launcher or absolute_bootstrap

    @classmethod
    def aqg_connector_owned(cls, entry: dict[str, Any]) -> bool:
        if not cls.python_command(entry):
            return False
        if not set(entry).issubset({"command", "args", "description", "disabled"}):
            return False
        if "disabled" in entry and not isinstance(entry["disabled"], bool):
            return False
        args = entry.get("args")
        if not isinstance(args, list) or len(args) != 2:
            return False
        script, option = args
        if not isinstance(script, str) or not Path(script).is_absolute():
            return False
        normalized = script.replace("\\", "/")
        return (
            normalized.endswith("/scripts/aqg_doctor.py")
            and option == "--no-cli"
            and entry.get("description") == AQG_SUPPORT_DESCRIPTION
        )

    def entry_owned(
        self,
        entry: Any,
        *,
        component: str = "de",
        server_name: str = "",
    ) -> bool:
        if not isinstance(entry, dict):
            return False
        if any(self.text_has_ref(item, component=component) for item in flatten_strings(entry)):
            return True
        if component == "de" and self._looks_like_de_server_name(server_name):
            return self.de_connector_owned(entry)
        if component == "aqg" and server_name == "aqg-support":
            return self.aqg_connector_owned(entry)
        return False

    def inspect_root(self, root: Path, component: str) -> None:
        if not lexists(root):
            self.notes.append(f"preserve absent {root}")
            return
        if component == "aqg" and root == self.aqg and root.is_symlink():
            target = self.proven_managed_aqg_target(root)
            if target is None:
                self.blockers.append(
                    f"{component} root ownership is unknown through symlink: {root}"
                )
                return
            self.aqg_managed_target = target
            self.aqg_refs = (self.aqg, target)
            self.add_action("quarantine-root-link", root, "aqg managed root link")
            self.add_action("quarantine-root", target, "aqg managed version target")
            return
        parent_link = path_has_symlink_component(root, self.home)
        if parent_link:
            self.blockers.append(f"{component} root ownership is unknown through symlink: {parent_link}")
            return
        if root.is_symlink():
            self.blockers.append(f"{component} root is a symlink and ownership is unknown: {root}")
            return
        if not root.is_dir():
            self.blockers.append(f"{component} root is not a directory: {root}")
            return
        if component == "de":
            required = (root / "skills", root / "client", root / "desktop", root / "pyproject.toml")
        else:
            required = (root / "skills", root / "scripts", root / "VERSION", root / "requirements.txt")
        missing = [str(path) for path in required if not path.is_dir() and not path.is_file()]
        if missing:
            self.blockers.append(f"{component} root has unexpected layout; ownership is unknown: {root}")
            return
        self.add_action("quarantine-root", root, component)

    def proven_managed_aqg_target(self, root: Path) -> Path | None:
        if root != self.aqg or not root.is_symlink():
            return None
        try:
            link_stat = root.lstat()
            link_text = os.readlink(root)
        except OSError:
            return None
        if hasattr(os, "getuid") and link_stat.st_uid != os.getuid():
            return None
        target = Path(link_text)
        if not target.is_absolute():
            target = root.parent / target
        target = lex(target)
        versions = self.dp / "versions"
        if target.parent != versions or re.fullmatch(r"[0-9a-f]{40}", target.name) is None:
            return None
        if (
            path_has_symlink_component(versions, self.home)
            or target.is_symlink()
            or not target.is_dir()
        ):
            return None
        try:
            versions_stat = versions.stat()
            target_stat = target.stat()
        except OSError:
            return None
        if hasattr(os, "getuid") and (
            versions_stat.st_uid != os.getuid() or target_stat.st_uid != os.getuid()
        ):
            return None
        required = (
            target / "scripts" / "install_aqg_clients.py",
            target / "AI_SETUP.md",
            target / "VERSION",
            target / "requirements.txt",
        )
        if not all(path.is_file() and not path.is_symlink() for path in required):
            return None
        git = shutil.which("git")
        if not git:
            return None
        try:
            head = subprocess.run(
                [git, "-C", str(target), "rev-parse", "HEAD"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            remote = subprocess.run(
                [git, "-C", str(target), "remote", "get-url", "origin"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            status_result = subprocess.run(
                [git, "-C", str(target), "status", "--porcelain=v1", "--untracked-files=all"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if (
            head.returncode != 0
            or head.stdout.strip() != target.name
            or remote.returncode != 0
            or remote.stdout.strip() not in AQG_PRODUCT_REMOTES
            or status_result.returncode != 0
            or bool(status_result.stdout.strip())
        ):
            return None
        return target

    def inspect_aux(self) -> None:
        if self.scope in ("de", "both"):
            candidates = [
                self.dp / ".install.lock",
                self.dp / "de-python",
                self.dp / "installations" / "decision-engine.json",
                self.dp / "installations" / "decision-engine.identity.lock",
            ]
            candidates.extend(sorted(self.dp.glob(".decision-engine.bootstrapping.failed-*")))
            for path in candidates:
                if not lexists(path):
                    continue
                if path.is_symlink() or path_has_symlink_component(path, self.home):
                    self.blockers.append(f"DE auxiliary ownership is unknown through symlink: {path}")
                    continue
                self.de_aux.append(path)
                detail = "DE install lock" if path.name == ".install.lock" else "proven DE auxiliary"
                self.add_action("quarantine-aux", path, detail)
        if self.scope in ("aqg", "both"):
            path = self.dp / "aqg-backups"
            if lexists(path):
                if path.is_symlink() or path_has_symlink_component(path, self.home):
                    self.blockers.append(f"AQG backup ownership is unknown through symlink: {path}")
                elif not path.is_dir():
                    self.blockers.append(f"AQG backup path is not a directory: {path}")
                else:
                    self.aqg_aux.append(path)
                    self.add_action("quarantine-aux", path, "AQG user-scope uninstall backups")

    def qoder_de_plugin_cache_owned(self, cache: Path) -> bool:
        if cache.is_symlink() or not cache.is_dir():
            return False
        if path_has_symlink_component(cache, self.home):
            return False
        manifest_path = cache / ".qoder-plugin" / "plugin.json"
        mcp_path = cache / ".mcp.json"
        if (
            manifest_path.is_symlink()
            or mcp_path.is_symlink()
            or not manifest_path.is_file()
            or not mcp_path.is_file()
        ):
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(manifest, dict) or not isinstance(mcp, dict):
            return False
        author = manifest.get("author")
        servers = mcp.get("mcpServers")
        entry = servers.get("decision-engine") if isinstance(servers, dict) else None
        return bool(
            manifest.get("name") == "decision-engine"
            and isinstance(author, dict)
            and author.get("name") == "DeepPattern"
            and manifest.get("homepage") in QODER_DE_PLUGIN_HOMEPAGES
            and manifest.get("mcpServers") == "./.mcp.json"
            and self.entry_owned(
                entry,
                component="de",
                server_name="decision-engine",
            )
        )

    def inspect_qoder_plugin_root(self, plugin_root: Path) -> None:
        registry = plugin_root / "installed_plugins_v2.json"
        cache = plugin_root / "cache" / "de-bundler" / "decision-engine"
        data = plugin_root / "data" / "decision-engine-de-bundler"
        cache_present = lexists(cache)
        cache_owned = self.qoder_de_plugin_cache_owned(cache) if cache_present else False
        registry_owned = False

        if lexists(registry):
            link = path_has_symlink_component(registry, self.home)
            if link or registry.is_symlink() or not registry.is_file():
                self.blockers.append(
                    f"Qoder plugin registry is not a trusted regular file: {registry}"
                )
                return
            try:
                payload = json.loads(registry.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.blockers.append(
                    f"cannot parse Qoder plugin registry {registry}: {exc.__class__.__name__}"
                )
                return
            plugins = payload.get("plugins") if isinstance(payload, dict) else None
            records = plugins.get(QODER_DE_PLUGIN_ID) if isinstance(plugins, dict) else None
            if records is not None:
                expected_cache = lex(cache)
                registry_owned = bool(
                    isinstance(records, list)
                    and records
                    and all(
                        isinstance(record, dict)
                        and record.get("scope") == "user"
                        and isinstance(record.get("installPath"), str)
                        and lex(Path(record["installPath"])) == expected_cache
                        for record in records
                    )
                )
                if not registry_owned or (cache_present and not cache_owned):
                    self.blockers.append(
                        f"Qoder Decision Engine plugin ownership is unknown: {registry}"
                    )
                    return
                pointer = (("plugins", QODER_DE_PLUGIN_ID),)
                self.json_targets.append((registry, pointer))
                self.add_action(
                    "edit-json",
                    registry,
                    f"remove owned Qoder plugin registration {QODER_DE_PLUGIN_ID}",
                )

        if cache_present:
            if not cache_owned:
                self.blockers.append(
                    f"Qoder Decision Engine plugin cache ownership is unknown: {cache}"
                )
                return
            self.qoder_residue_paths.append((cache, "owned Qoder Decision Engine plugin cache"))
            self.add_action("quarantine-qoder-plugin", cache, "owned Decision Engine plugin cache")

        if lexists(data):
            if data.is_symlink() or not data.is_dir() or path_has_symlink_component(data, self.home):
                self.blockers.append(
                    f"Qoder Decision Engine plugin data ownership is unknown: {data}"
                )
                return
            if not (registry_owned or cache_owned):
                try:
                    empty = not any(data.iterdir())
                except OSError:
                    empty = False
                if not empty:
                    self.blockers.append(
                        f"orphaned Qoder Decision Engine plugin data is not empty: {data}"
                    )
                    return
            self.qoder_residue_paths.append((data, "owned Qoder Decision Engine plugin data"))
            self.add_action("quarantine-qoder-plugin", data, "owned Decision Engine plugin data")

    def inspect_qoder_plugins(self) -> None:
        if self.scope not in ("de", "both"):
            return
        for relative in (".qoder/plugins", ".qoder-cn/plugins"):
            plugin_root = self.home / relative
            if lexists(plugin_root):
                self.inspect_qoder_plugin_root(plugin_root)
                quarantine_root = plugin_root / "cache"
                try:
                    quarantined = tuple(
                        quarantine_root.glob("_de-quarantine*/decision-engine")
                    ) if quarantine_root.is_dir() and not quarantine_root.is_symlink() else ()
                except OSError as exc:
                    self.blockers.append(
                        f"cannot inspect Qoder plugin quarantine {quarantine_root}: {exc.__class__.__name__}"
                    )
                    continue
                for cache in quarantined:
                    if not self.qoder_de_plugin_cache_owned(cache):
                        self.blockers.append(
                            f"Qoder quarantined Decision Engine plugin ownership is unknown: {cache}"
                        )
                        continue
                    self.qoder_residue_paths.append(
                        (cache, "owned quarantined Qoder Decision Engine plugin cache")
                    )
                    self.add_action(
                        "quarantine-qoder-plugin",
                        cache,
                        "owned quarantined Decision Engine plugin cache",
                    )

    def inspect_qoder_runtime_caches(self) -> None:
        if self.scope not in ("de", "both"):
            return
        for product in ("Qoder", "QoderCN"):
            projects = (
                self.home
                / "Library"
                / "Application Support"
                / product
                / "SharedClientCache"
                / "projects"
            )
            if not projects.is_dir() or projects.is_symlink():
                continue
            try:
                candidates = tuple(projects.glob("*/mcps/decision-engine"))
            except OSError as exc:
                self.blockers.append(
                    f"cannot inspect Qoder MCP runtime cache {projects}: {exc.__class__.__name__}"
                )
                continue
            for cache in candidates:
                metadata_path = cache / "SERVER_METADATA.json"
                if (
                    cache.is_symlink()
                    or not cache.is_dir()
                    or path_has_symlink_component(cache, self.home)
                    or metadata_path.is_symlink()
                    or not metadata_path.is_file()
                ):
                    self.blockers.append(
                        f"Qoder Decision Engine runtime cache ownership is unknown: {cache}"
                    )
                    continue
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    self.blockers.append(
                        f"cannot parse Qoder Decision Engine runtime cache {metadata_path}: {exc.__class__.__name__}"
                    )
                    continue
                if not (
                    isinstance(metadata, dict)
                    and metadata.get("name") == "decision-engine"
                    and metadata.get("source") in {"user", "plugin"}
                    and isinstance(metadata.get("toolCount"), int)
                ):
                    self.blockers.append(
                        f"Qoder Decision Engine runtime cache ownership is unknown: {cache}"
                    )
                    continue
                self.qoder_residue_paths.append(
                    (cache, "owned Qoder Decision Engine runtime cache")
                )
                self.add_action(
                    "quarantine-qoder-cache",
                    cache,
                    "owned Decision Engine runtime cache",
                )

    def json_paths(self) -> tuple[Path, ...]:
        # These are user-level config locations used by the DE MCP writer or by
        # supported host adapters. Project-local configs are intentionally absent.
        # Some desktop hosts keep their MCP file beside the app data rather than
        # under the host's dot-directory; include those known user-level paths.
        relative = (
            ".claude.json",
            ".qoder/mcp.json",
            ".qoder/settings.json",
            ".qoder-cn/mcp.json",
            ".qoder-cn/settings.json",
            ".cursor/mcp.json",
            ".trae/mcp.json",
            ".trae-cn/mcp.json",
            ".trae-cn/hooks.json",
            ".trae-work/mcp.json",
            ".trae-work-cn/mcp.json",
            ".workbuddy/mcp.json",
            ".workbuddy-ai/settings.json",
            ".codebuddy/mcp.json",
            ".kimi/mcp.json",
            ".kimi-code/mcp.json",
            ".qoderwork/mcp.json",
            ".qoderwake/mcp.json",
            ".devin/mcp.json",
            ".pi/mcp.json",
            ".config/zed/settings.json",
            "Library/Application Support/Claude/claude_desktop_config.json",
            "Library/Application Support/Claude-3p/claude_desktop_config.json",
            "Library/Application Support/Qoder/User/mcp.json",
            "Library/Application Support/QoderCN/User/mcp.json",
            "Library/Application Support/Cursor/User/mcp.json",
            "Library/Application Support/TRAE SOLO/User/mcp.json",
            "Library/Application Support/TRAE SOLO CN/User/mcp.json",
            "Library/Application Support/Trae/User/mcp.json",
            "Library/Application Support/Trae CN/User/mcp.json",
            "Library/Application Support/WorkBuddy/mcp.json",
            "Library/Application Support/WorkBuddy AI/mcp.json",
            "Library/Application Support/Qoder/SharedClientCache/extension/local/mcp.json",
            "Library/Application Support/QoderCN/SharedClientCache/extension/local/mcp.json",
            ".gemini/settings.json",
        )
        paths = {self.home / item for item in relative}
        # Catch user-level host variants that are not in the installer's core
        # client registry yet. Only the conventional MCP filenames are
        # discovered; project files and arbitrary settings are excluded.
        try:
            hidden_dirs = tuple(
                child for child in self.home.iterdir()
                if child.name.startswith(".") and child.is_dir()
            )
        except OSError as exc:
            self.blockers.append(f"cannot inspect user host directories: {exc.__class__.__name__}")
            hidden_dirs = ()
        paths.update(child / "mcp.json" for child in hidden_dirs)
        app_support = self.home / "Library" / "Application Support"
        try:
            app_dirs = tuple(child for child in app_support.iterdir() if child.is_dir()) if app_support.is_dir() else ()
        except OSError as exc:
            self.blockers.append(f"cannot inspect Application Support host directories: {exc.__class__.__name__}")
            app_dirs = ()
        for app in app_dirs:
            paths.update(
                {
                    app / "mcp.json",
                    app / "User" / "mcp.json",
                    app / "SharedClientCache" / "extension" / "local" / "mcp.json",
                }
            )
        # WorkBuddy and similar hosts materialize one user-level config per
        # connector. Restrict discovery to the host-owned connector roots so
        # project files and marketplace fixtures are never scanned.
        connector_roots = (
            ".workbuddy/connectors",
            ".workbuddy-ai/connectors",
            ".codebuddy/connectors",
            ".codebuddy-ai/connectors",
            ".qoder/connectors",
            ".qoder-cn/connectors",
        )
        for root in connector_roots:
            directory = self.home / root
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                paths.update(item for item in directory.glob("*/mcp.json") if item.is_file())
            except OSError as exc:
                self.blockers.append(f"cannot inspect host connector directory {directory}: {exc.__class__.__name__}")
        return tuple(sorted(paths))

    def inspect_json_file(self, path: Path) -> None:
        if not lexists(path):
            return
        link = path_has_symlink_component(path, self.home)
        if link:
            self.blockers.append(f"JSON config ownership is unknown through symlink: {link}")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.blockers.append(f"cannot parse JSON config {path}: {exc.__class__.__name__}")
            return
        if not isinstance(data, (dict, list)):
            self.blockers.append(f"JSON config has unexpected top-level type: {path}")
            return
        targets: list[tuple[str, ...]] = []
        unknown: list[str] = []

        def walk(node: Any, pointer: tuple[str, ...]) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    next_pointer = pointer + (str(key),)
                    if (
                        self.scope in ("de", "both")
                        and key == "UserPromptSubmit"
                        and pointer
                        and pointer[-1] == "hooks"
                        and isinstance(value, list)
                    ):
                        for index, group in enumerate(value):
                            ownership = self.de_prompt_hook_ownership(group)
                            hook_pointer = next_pointer + (str(index),)
                            if ownership == "owned":
                                targets.append(hook_pointer)
                            elif ownership == "unknown":
                                unknown.append(".".join(hook_pointer))
                    if key in ("mcpServers", "mcp_servers") and isinstance(value, dict):
                        for server_name, entry in value.items():
                            entry_pointer = next_pointer + (str(server_name),)
                            if any(
                                self.entry_owned(
                                    entry,
                                    component=component,
                                    server_name=str(server_name),
                                )
                                for component in self.json_components()
                            ):
                                targets.append(entry_pointer)
                            elif any(
                                (
                                    component == "de"
                                    and self._looks_like_de_server_name(str(server_name))
                                )
                                or (
                                    component == "aqg"
                                    and self._looks_like_aqg_server_name(str(server_name))
                                )
                                for component in self.json_components()
                            ):
                                unknown.append(".".join(entry_pointer))
                    walk(value, next_pointer)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, pointer + (str(index),))

        walk(data, ())
        if unknown:
            self.blockers.extend(
                f"{path} {pointer}: managed host entry ownership is unknown" for pointer in unknown
            )
            return
        if targets:
            frozen = tuple(targets)
            self.json_targets.append((path, frozen))
            suffix = "entry" if len(targets) == 1 else "entries"
            self.add_action("edit-json", path, f"remove {len(targets)} owned decision-engine {suffix}")

    @staticmethod
    def de_prompt_hook_ownership(group: Any) -> str:
        if not isinstance(group, dict) or group.get("matcher") != "":
            return "none"
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            return "none"
        named = [
            hook
            for hook in hooks
            if isinstance(hook, dict) and hook.get("name") in QODER_HOOK_NAMES
        ]
        if named:
            if len(hooks) != 1 or len(named) != 1:
                return "unknown"
            hook = named[0]
            args = hook.get("args")
            if (
                hook.get("type") == "command"
                and isinstance(hook.get("command"), str)
                and bool(hook["command"].strip())
                and isinstance(args, list)
                and len(args) == 1
                and isinstance(args[0], str)
                and args[0].replace("\\", "/").endswith(QODER_HOOK_SCRIPT_SUFFIX)
            ):
                return "owned"
            return "unknown"

        if len(hooks) != 1 or not isinstance(hooks[0], dict):
            return "none"
        hook = hooks[0]
        command = hook.get("command")
        if not isinstance(command, str):
            return "none"
        managed_ids = [
            managed_id
            for managed_id in COMMAND_PROMPT_HOOK_SPECS
            if managed_id in command
        ]
        if not managed_ids:
            return "none"
        if len(managed_ids) != 1:
            return "unknown"
        managed_id = managed_ids[0]
        try:
            tokens = shlex.split(command)
        except ValueError:
            return "unknown"
        if (
            hook.get("type") == "command"
            and hook.get("timeout") == 30
            and len(tokens) == 4
            and bool(tokens[0])
            and tokens[1].replace("\\", "/").endswith(
                COMMAND_PROMPT_HOOK_SPECS[managed_id]
            )
            and tokens[2:] == ["--managed-id", managed_id]
        ):
            return "owned"
        return "unknown"

    @staticmethod
    def _looks_like_de_server_name(name: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return normalized == "decision-engine" or normalized.startswith("decision-engine-")

    @staticmethod
    def _looks_like_aqg_server_name(name: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return (
            normalized == "aqg"
            or normalized.startswith("aqg-")
            or normalized == "agent-quality-gates"
            or normalized.startswith("agent-quality-gates-")
        )

    def json_components(self) -> tuple[str, ...]:
        # AQG's official adapter owns its connector, hook, rule, and skill
        # lifecycle as one transaction. Once available, leave every AQG edit
        # to it so this outer uninstaller cannot race a second JSON deletion.
        aqg_delegated = self.aqg_uninstaller is not None
        if self.scope == "de":
            return ("de",)
        if self.scope == "aqg":
            return () if aqg_delegated else ("aqg",)
        return ("de",) if aqg_delegated else ("de", "aqg")

    def inspect_json(self) -> None:
        if self.scope not in ("de", "aqg", "both"):
            return
        for path in self.json_paths():
            self.inspect_json_file(path)

    def host_backup_paths(self) -> tuple[Path, ...]:
        roots = (
            self.home,
            self.home / ".codex",
            self.home / ".claude",
            self.home / ".cursor",
            self.home / ".qoder",
            self.home / ".qoder-cn",
            self.home / ".trae",
            self.home / ".trae-cn",
            self.home / ".workbuddy",
            self.home / ".workbuddy-ai",
            self.home / ".codebuddy",
            self.home / ".codebuddy-ai",
            self.home / ".kimi",
            self.home / ".kimi-code",
            self.home / ".devin",
            self.home / ".pi",
            self.home / ".gemini",
            self.home / ".lingma",
            self.home / "Library" / "Application Support" / "Claude",
            self.home / "Library" / "Application Support" / "Claude-3p",
            self.home / "Library" / "Application Support" / "TRAE SOLO" / "User",
            self.home / "Library" / "Application Support" / "TRAE SOLO CN" / "User",
            self.home / "Library" / "Application Support" / "Trae" / "User",
            self.home / "Library" / "Application Support" / "Trae CN" / "User",
            self.home / "Library" / "Application Support" / "Qoder" / "SharedClientCache" / "extension" / "local",
            self.home / "Library" / "Application Support" / "QoderCN" / "SharedClientCache" / "extension" / "local",
        )
        try:
            hidden_dirs = tuple(
                child for child in self.home.iterdir()
                if child.name.startswith(".") and child.is_dir()
            )
        except OSError as exc:
            self.blockers.append(f"cannot inspect user host backup directories: {exc.__class__.__name__}")
            hidden_dirs = ()
        roots = roots + hidden_dirs
        app_support = self.home / "Library" / "Application Support"
        try:
            app_dirs = tuple(child for child in app_support.iterdir() if child.is_dir()) if app_support.is_dir() else ()
        except OSError as exc:
            self.blockers.append(f"cannot inspect Application Support backup directories: {exc.__class__.__name__}")
            app_dirs = ()
        for app in app_dirs:
            roots = roots + (
                app,
                app / "User",
                app / "SharedClientCache" / "extension" / "local",
            )
        paths: set[Path] = set()
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for pattern in HOST_BACKUP_PATTERNS:
                try:
                    paths.update(item for item in root.glob(pattern) if lexists(item))
                except OSError as exc:
                    self.blockers.append(f"cannot inspect host backup directory {root}: {exc.__class__.__name__}")
        for relative in (
            ".workbuddy/connectors",
            ".workbuddy-ai/connectors",
            ".codebuddy/connectors",
            ".codebuddy-ai/connectors",
            ".qoder/connectors",
            ".qoder-cn/connectors",
        ):
            directory = self.home / relative
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                for connector in directory.iterdir():
                    if connector.is_dir() and not connector.is_symlink():
                        for pattern in HOST_BACKUP_PATTERNS:
                            paths.update(item for item in connector.glob(pattern) if lexists(item))
            except OSError as exc:
                self.blockers.append(f"cannot inspect host connector backup directory {directory}: {exc.__class__.__name__}")
        return tuple(sorted(paths))

    def inspect_host_backups(self) -> None:
        components = ("de",) if self.scope == "de" else ("aqg",) if self.scope == "aqg" else ("de", "aqg")
        for path in self.host_backup_paths():
            link = path_has_symlink_component(path, self.home)
            if link:
                self.blockers.append(f"host backup ownership is unknown through symlink: {link}")
                continue
            if not path.is_file() or path.is_symlink():
                self.blockers.append(f"host backup is not a regular file: {path}")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                self.blockers.append(f"cannot read host backup {path}: {exc.__class__.__name__}")
                continue
            if any(self.text_has_ref(text, component=component) for component in components):
                self.host_backups.append(path)
                self.add_action("quarantine-host-backup", path, "DE/AQG-owned installer backup")

    @staticmethod
    def toml_section_name(raw: str) -> str:
        value = raw.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return value.replace('"."', ".").replace("'.'", ".")

    def inspect_codex(self) -> None:
        if self.scope not in ("de", "both"):
            return
        path = self.home / ".codex" / "config.toml"
        if not lexists(path):
            return
        link = path_has_symlink_component(path, self.home)
        if link:
            self.blockers.append(f"Codex config ownership is unknown through symlink: {link}")
            return
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self.blockers.append(f"cannot read Codex config {path}: {exc.__class__.__name__}")
            return
        try:
            import tomllib
        except ModuleNotFoundError:
            # macOS system Python can be older than 3.11. The DE writer emits
            # section tables, so the bounded fallback below is sufficient; it
            # still blocks unsupported inline forms instead of guessing.
            parsed = {}
        else:
            try:
                parsed = tomllib.loads(text)
            except Exception as exc:
                self.blockers.append(f"cannot parse Codex TOML {path}: {exc.__class__.__name__}")
                return
        lines = text.splitlines(keepends=True)
        headers: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = re.match(r"^\s*\[([^\]]+)\]\s*$", line.rstrip("\n"))
            if match:
                headers.append((index, self.toml_section_name(match.group(1))))
        servers = parsed.get("mcp_servers") if isinstance(parsed, dict) else None
        parsed_root = servers.get("decision-engine") if isinstance(servers, dict) else None
        matching_ranges: list[tuple[int, int, str]] = []
        unknown_section = False
        root_found = False
        for index, (start, name) in enumerate(headers):
            if name != "mcp_servers.decision-engine" and not name.startswith("mcp_servers.decision-engine."):
                continue
            end = headers[index + 1][0] if index + 1 < len(headers) else len(lines)
            matching_ranges.append((start, end, name))
            if name == "mcp_servers.decision-engine":
                root_found = True
                section = "".join(lines[start:end])
                parsed_owned = self.entry_owned(
                    parsed_root,
                    component="de",
                    server_name="decision-engine",
                )
                fallback_owned = self.text_has_ref(section, component="de") or bool(
                    re.search(
                        r"['\"]-m['\"]\s*,\s*['\"]installer\.launcher['\"]",
                        section,
                    )
                )
                if not parsed_owned and not fallback_owned:
                    unknown_section = True
                    self.blockers.append(f"{path} [{name}]: DE host entry ownership is unknown")
        if matching_ranges and not root_found:
            unknown_section = True
            self.blockers.append(
                f"{path}: Decision Engine child TOML sections exist without an owned root section"
            )
        if isinstance(servers, dict) and "decision-engine" in servers and not root_found:
            unknown_section = True
            self.blockers.append(f"{path}: inline decision-engine entry ownership is unknown")
        if matching_ranges and not unknown_section:
            frozen = tuple((start, end) for start, end, _name in matching_ranges)
            self.toml_ranges.append((path, frozen))
            self.add_action("edit-toml", path, f"remove {len(frozen)} owned Decision Engine TOML section(s)")

    def inspect_agents(self) -> None:
        if self.scope not in ("de", "both"):
            return
        path = self.home / ".codex" / "AGENTS.md"
        if not lexists(path):
            return
        link = path_has_symlink_component(path, self.home)
        if link:
            self.blockers.append(f"Codex routing ownership is unknown through symlink: {link}")
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeError) as exc:
            self.blockers.append(f"cannot read Codex routing file {path}: {exc.__class__.__name__}")
            return
        starts = [i for i, line in enumerate(lines) if line.strip() == ROUTING_START]
        ends = [i for i, line in enumerate(lines) if line.strip() == ROUTING_END]
        if not starts and not ends:
            return
        if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
            self.blockers.append(f"{path}: managed DE routing markers are ambiguous")
            return
        self.agents_target = path
        self.add_action("edit-agents", path, "remove the marked DE routing block")

    def skill_dirs(self) -> tuple[Path, ...]:
        relative = (
            ".codex/skills",
            ".claude/skills",
            ".cursor/skills",
            ".qoder/skills",
            ".qoder-cn/skills",
            ".lingma/skills",
            ".trae/skills",
            ".trae-cn/skills",
            ".trae-work/skills",
            ".trae-work-cn/skills",
            ".workbuddy/skills",
            ".codebuddy/skills",
            ".kimi/skills",
            ".kimi-code/skills",
            ".qoderwork/skills",
            ".qoderwake/skills",
            ".agents/skills",
            ".config/zed/skills",
            ".config/devin/skills",
            ".devin/skills",
            ".pi/skills",
            ".gemini/skills",
            ".workbuddy/skills",
            ".workbuddy-ai/skills",
            ".codebuddy/skills",
            ".codebuddy-ai/skills",
            "Library/Application Support/Qoder/User/skills",
            "Library/Application Support/QoderCN/User/skills",
            "Library/Application Support/TRAE SOLO/User/skills",
            "Library/Application Support/TRAE SOLO CN/User/skills",
            "Library/Application Support/Trae/User/skills",
            "Library/Application Support/Trae CN/User/skills",
            "Library/Application Support/WorkBuddy/skills",
            "Library/Application Support/WorkBuddy AI/skills",
        )
        paths = {self.home / item for item in relative}
        try:
            paths.update(
                child / "skills"
                for child in self.home.iterdir()
                if child.name.startswith(".") and child.is_dir()
            )
        except OSError as exc:
            self.blockers.append(f"cannot inspect user skill directories: {exc.__class__.__name__}")
        app_support = self.home / "Library" / "Application Support"
        try:
            app_dirs = tuple(child for child in app_support.iterdir() if child.is_dir()) if app_support.is_dir() else ()
        except OSError as exc:
            self.blockers.append(f"cannot inspect Application Support skill directories: {exc.__class__.__name__}")
            app_dirs = ()
        for app in app_dirs:
            paths.update({app / "skills", app / "User" / "skills"})
        return tuple(sorted(paths))

    def orphaned_de_bootstrap_skill_target(self, link: Path) -> Path | None:
        if not link.is_symlink() or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", link.name) is None:
            return None
        try:
            if hasattr(os, "getuid") and link.lstat().st_uid != os.getuid():
                return None
            raw_target = Path(os.readlink(link))
        except OSError:
            return None
        target = raw_target if raw_target.is_absolute() else link.parent / raw_target
        if target.exists():
            return None
        temporary_root = Path(
            os.path.realpath(os.environ.get("TMPDIR") or tempfile.gettempdir())
        )
        normalized_target = Path(os.path.realpath(target))
        try:
            relative = normalized_target.relative_to(temporary_root)
        except ValueError:
            return None
        parts = relative.parts
        if (
            len(parts) != 5
            or not parts[0].startswith("tmp")
            or parts[1:4] != ("deeppattern", "decision-engine", "skills")
            or parts[4] != link.name
        ):
            return None
        return lex(target)

    def inspect_skills(self) -> None:
        components = ("de",) if self.scope == "de" else ("aqg",) if self.scope == "aqg" else ("de", "aqg")
        for directory in self.skill_dirs():
            if not lexists(directory):
                continue
            if directory.is_symlink() or path_has_symlink_component(directory, self.home):
                self.blockers.append(f"skill directory ownership is unknown through symlink: {directory}")
                continue
            if not directory.is_dir():
                self.blockers.append(f"skill directory is not a directory: {directory}")
                continue
            try:
                children = tuple(directory.iterdir())
            except OSError as exc:
                self.blockers.append(f"cannot inspect skill directory {directory}: {exc.__class__.__name__}")
                continue
            for child in children:
                if not child.is_symlink():
                    continue
                target = Path(os.path.realpath(child))
                if any(self.path_ref(target, component=component) for component in components):
                    self.skill_links.append(child)
                    self.add_action("unlink-skill", child, f"owned route -> {target}")
                elif "de" in components:
                    orphaned_target = self.orphaned_de_bootstrap_skill_target(child)
                    if orphaned_target is not None:
                        self.skill_links.append(child)
                        self.add_action(
                            "unlink-skill",
                            child,
                            f"orphaned managed bootstrap route -> {orphaned_target}",
                        )

    def launchctl_path(self) -> str | None:
        return os.environ.get("DE_AQG_UNINSTALL_LAUNCHCTL_COMMAND") or shutil.which("launchctl")

    def launchctl_probe(self) -> tuple[str, str]:
        command = self.launchctl_path()
        if not command:
            return "unknown", "launchctl is unavailable"
        try:
            result = subprocess.run(
                [command, "print", f"gui/{os.getuid()}/{HOSTBRIDGE_LABEL}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except OSError as exc:
            return "unknown", f"launchctl probe failed: {exc.__class__.__name__}"
        output = result.stdout.strip()
        if result.returncode == 0:
            return "loaded", output
        if result.returncode == 113 or "could not find" in output.lower() or "no such service" in output.lower():
            return "not-loaded", output
        return "unknown", output or f"exit {result.returncode}"

    def runtime_from_plist(self, payload: dict[str, Any]) -> None:
        values: list[Any] = []
        environment = payload.get("EnvironmentVariables")
        if isinstance(environment, dict):
            values.extend(environment.values())
        values.extend(payload.get("WatchPaths", []))
        expected_names = {f"decision-engine-host-bridge-{os.getuid()}"}
        roots: set[Path] = set()
        for value in values:
            raw_values: list[str]
            if isinstance(value, str) and value.startswith("["):
                try:
                    decoded = json.loads(value)
                    raw_values = [item for item in decoded if isinstance(item, str)] if isinstance(decoded, list) else [value]
                except json.JSONDecodeError:
                    raw_values = [value]
            elif isinstance(value, str):
                raw_values = [value]
            else:
                continue
            for raw in raw_values:
                candidate = Path(os.path.expanduser(raw))
                parts = candidate.parts
                for index, part in enumerate(parts):
                    if part not in expected_names:
                        continue
                    root = Path(*parts[: index + 1])
                    allowed = (
                        Path("/private/tmp") / part,
                        Path("/tmp") / part,
                        Path(tempfile.gettempdir()) / part,
                    )
                    if any(lex(root) == lex(item) for item in allowed):
                        roots.add(lex(root))
                    else:
                        self.blockers.append(f"LaunchAgent runtime path is outside the private registry root: {raw}")
                    break
        self.runtime_roots = sorted(roots)
        for root in self.runtime_roots:
            if lexists(root):
                # /tmp is commonly a symlink on macOS; the generated registry
                # name and private temp root are the ownership proof here.
                if root.is_symlink():
                    self.blockers.append(f"LaunchAgent runtime root is a symlink: {root}")
                else:
                    self.add_action("quarantine-runtime", root, "private DE HostBridge registry")

    def inspect_launchagent(self) -> None:
        if self.scope not in ("de", "both") or sys.platform != "darwin":
            return
        path = self.home / "Library" / "LaunchAgents" / f"{HOSTBRIDGE_LABEL}.plist"
        state, detail = self.launchctl_probe()
        if state == "unknown":
            self.blockers.append(f"LaunchAgent state is unknown: {detail}")
        elif state == "loaded":
            self.launchagent_loaded = True
        if not lexists(path):
            if state == "loaded":
                self.blockers.append(f"LaunchAgent is loaded but its plist is absent: {path}")
            return
        link = path_has_symlink_component(path, self.home)
        if link:
            self.blockers.append(f"LaunchAgent ownership is unknown through symlink: {link}")
            return
        if path.is_symlink():
            self.blockers.append(f"LaunchAgent plist is a symlink: {path}")
            return
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            self.blockers.append(f"cannot parse LaunchAgent plist {path}: {exc.__class__.__name__}")
            return
        if not isinstance(payload, dict):
            self.blockers.append(f"LaunchAgent plist has unexpected shape: {path}")
            return
        environment = payload.get("EnvironmentVariables")
        if payload.get("Label") != HOSTBRIDGE_LABEL or not isinstance(environment, dict) or environment.get(HOSTBRIDGE_MARKER) != "1":
            self.blockers.append(f"same-name LaunchAgent lacks the DE ownership proof: {path}")
            return
        args = payload.get("ProgramArguments")
        if not isinstance(args, list) or not args or not any(self.text_has_ref(str(item), component="de") for item in args):
            self.blockers.append(f"LaunchAgent program path is not a proven DE path: {path}")
            return
        self.launchagent = path
        self.runtime_from_plist(payload)
        self.add_action("quarantine-launchagent", path, f"loaded={state == 'loaded'}")

    def process_command(self) -> str | None:
        return os.environ.get("DE_AQG_UNINSTALL_PROCESS_COMMAND") or shutil.which("ps")

    def process_files_command(self) -> str | None:
        return os.environ.get("DE_AQG_UNINSTALL_PROCESS_FILES_COMMAND") or shutil.which("lsof")

    def process_file_refs(self, pid: str) -> tuple[str, ...] | None:
        command = self.process_files_command()
        if not command:
            return None
        try:
            result = subprocess.run(
                [command, "-a", "-n", "-P", "-p", pid, "-d", "cwd,txt,mem", "-Fn"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return tuple(line[1:] for line in result.stdout.splitlines() if line.startswith("n") and len(line) > 1)

    @staticmethod
    def looks_like_de_launcher(command_line: str) -> bool:
        normalized = command_line.lower()
        return (
            "-m installer.launcher" in normalized
            or "installer/launcher.py" in normalized
            or "stopper_launch_agent" in normalized
            or "decision-engine-stopper" in normalized
        )

    def inspect_processes(self) -> None:
        command = self.process_command()
        if not command:
            self.blockers.append("live process state is unknown: ps is unavailable")
            return
        try:
            result = subprocess.run(
                [command, "-axo", "pid=,ppid=,command="],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except OSError as exc:
            self.blockers.append(f"live process state is unknown: ps failed ({exc.__class__.__name__})")
            return
        if result.returncode != 0:
            self.blockers.append("live process state is unknown: ps returned a non-zero status")
            return
        components = ("de",) if self.scope == "de" else ("aqg",) if self.scope == "aqg" else ("de", "aqg")
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            match = re.match(r"^(\d+)\s+(?:\d+\s+)?(.+)$", line)
            if not match:
                continue
            pid = match.group(1)
            command_line = match.group(2)
            if self.qoder_residue_paths and any(
                marker in command_line for marker in QODER_PROCESS_MARKERS
            ):
                self.processes.append({"pid": pid, "family": "qoder-plugin-host"})
                self.add_action("process-blocker", Path(pid), "qoder-plugin-host")
                self.blockers.append(
                    f"Qoder host must be stopped before plugin cleanup: pid={pid}"
                )
                continue
            owned = any(self.text_has_ref(command_line, component=component) for component in components)
            if not owned and self.looks_like_de_launcher(command_line):
                file_refs = self.process_file_refs(pid)
                owned = bool(
                    file_refs
                    and any(
                        self.text_has_ref(path, component=component)
                        for path in file_refs
                        for component in components
                    )
                )
                if not owned:
                    self.blockers.append(
                        f"live process ownership is unknown: pid={pid} command={command_line}"
                    )
                    continue
            if owned:
                family = "stopper" if "stopper" in command_line.lower() else "mcp-launcher"
                self.processes.append({"pid": pid, "family": family})
                self.add_action("process-blocker", Path(pid), family)
                self.blockers.append(f"live {family} process must be stopped before uninstall: pid={pid}")

    def aqg_hook_paths(self) -> tuple[Path, ...]:
        relative = (
            ".claude/settings.json",
            ".codex/hooks.json",
            ".cursor/hooks.json",
            ".qoder/settings.json",
            ".qoder-cn/settings.json",
            ".trae/hooks.json",
            ".trae-cn/hooks.json",
            ".trae-work/hooks.json",
            ".trae-work-cn/hooks.json",
            ".codebuddy/settings.json",
            ".codebuddy-ai/settings.json",
            ".workbuddy-ai/settings.json",
            ".kimi-code/config.toml",
            ".qoderwork/settings.json",
            ".qoderwake/settings.json",
            ".devin/hooks.json",
            ".config/zed/settings.json",
        )
        return tuple(self.home / item for item in relative)

    @staticmethod
    def aqg_root_from_path(value: str) -> Path | None:
        normalized = value.strip("'\"()[],;").replace("\\", "/")
        if not normalized.startswith("/"):
            return None
        for marker in AQG_ROOT_PATH_MARKERS:
            if marker in normalized:
                return lex(Path(normalized.split(marker, 1)[0]))
        return None

    @classmethod
    def aqg_roots_from_text(cls, text: str) -> set[Path]:
        roots: set[Path] = set()
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            tokens = text.split()
        for token in tokens:
            root = cls.aqg_root_from_path(token)
            if root is not None:
                roots.add(root)
        return roots

    @staticmethod
    def valid_aqg_uninstaller_root(root: Path) -> bool:
        if not root.is_dir() or root.is_symlink():
            return False
        required = (
            root / "scripts" / "install_aqg_clients.py",
            root / "AI_SETUP.md",
            root / "VERSION",
        )
        return all(path.is_file() and not path.is_symlink() for path in required)

    @staticmethod
    def aqg_marker_source(
        marker: Any,
        *,
        expected_skill: str,
        expected_skills_root: Path,
    ) -> str | None:
        if not isinstance(marker, dict):
            return None
        manager = marker.get("manager")
        managed_by = marker.get("managed_by")
        if manager == "aqg-cursor-support":
            if (
                marker.get("schema_version") != 2
                or marker.get("skill") != expected_skill
                or marker.get("install_mode") != "link"
            ):
                return None
            source = marker.get("source_path")
        elif managed_by in {"aqg-qoder-v1", "aqg-agent-client-v1"}:
            if marker.get("mode") != "link":
                return None
            source = marker.get("source")
        elif manager in AQG_WORK_MARKER_MANAGERS:
            if (
                marker.get("schema_version") != 2
                or marker.get("skill") != expected_skill
                or (marker.get("effective_mode") or marker.get("install_mode")) != "link"
                or not isinstance(marker.get("resolved_skills_root"), str)
                or lex(Path(marker["resolved_skills_root"])) != lex(expected_skills_root)
            ):
                return None
            source = marker.get("source_path") or marker.get("source")
        else:
            return None
        if not isinstance(source, str):
            return None
        source_path = Path(source)
        if not source_path.is_absolute() or source_path.name != expected_skill:
            return None
        return source

    def discover_aqg_roots(self) -> tuple[set[Path], bool]:
        candidates: set[Path] = set()
        residue_found = False
        for path in self.aqg_hook_paths():
            if not lexists(path):
                continue
            link = path_has_symlink_component(path, self.home)
            if link:
                self.blockers.append(f"AQG hook config ownership is unknown through symlink: {link}")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                self.blockers.append(f"cannot read AQG hook config {path}: {exc.__class__.__name__}")
                continue
            if any(marker in text for marker in AQG_HOOK_MARKERS):
                residue_found = True
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    values = text.splitlines()
                else:
                    values = flatten_strings(payload)
                for value in values:
                    candidates.update(self.aqg_roots_from_text(value))

        for directory in self.skill_dirs():
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                children = tuple(directory.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_symlink() or not child.name.startswith("aqg-"):
                    continue
                residue_found = True
                target = Path(os.path.realpath(child))
                normalized = str(target).replace("\\", "/")
                for marker in ("/agent-packs/claude-code/skills/", "/skills/"):
                    if marker in normalized:
                        candidates.add(lex(Path(normalized.split(marker, 1)[0])))
                        break

            marker_dir = directory.parent / "managed-links"
            if not marker_dir.is_dir() or marker_dir.is_symlink():
                continue
            try:
                markers = tuple(marker_dir.glob("aqg-*.json"))
            except OSError:
                continue
            for marker_path in markers:
                if marker_path.is_symlink() or not marker_path.is_file():
                    self.blockers.append(f"AQG skill marker ownership is unknown: {marker_path}")
                    continue
                try:
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    self.blockers.append(
                        f"cannot parse AQG skill marker {marker_path}: {exc.__class__.__name__}"
                    )
                    continue
                expected_skill = marker_path.stem
                source = self.aqg_marker_source(
                    marker,
                    expected_skill=expected_skill,
                    expected_skills_root=directory,
                )
                if source is None:
                    self.blockers.append(f"AQG skill marker ownership is unknown: {marker_path}")
                    continue
                residue_found = True
                self.aqg_skill_markers.append((marker_path, sha256_file(marker_path)))
                self.add_action(
                    "unlink-skill-marker",
                    marker_path,
                    f"owned AQG skill record for {expected_skill}",
                )
                normalized = source.replace("\\", "/")
                if "/skills/" in normalized:
                    candidates.add(lex(Path(normalized.split("/skills/", 1)[0])))
        return candidates, residue_found

    def inspect_aqg_uninstaller(self) -> None:
        if self.scope not in ("aqg", "both"):
            return

        candidates, residue_found = self.discover_aqg_roots()
        if lexists(self.aqg):
            candidates.add(self.aqg)
        if self.aqg_managed_target is not None:
            candidates = {
                self.aqg_managed_target if root == self.aqg else root
                for root in candidates
            }
        valid = {root for root in candidates if self.valid_aqg_uninstaller_root(root)}
        invalid = candidates - valid
        if invalid:
            paths = ", ".join(str(path) for path in sorted(invalid))
            self.blockers.append(f"AQG references point to untrusted installer root(s): {paths}")
            return
        if len(valid) > 1:
            paths = ", ".join(str(path) for path in sorted(valid))
            self.blockers.append(f"AQG references do not converge on one installer root: {paths}")
            return
        if not valid:
            if residue_found:
                self.blockers.append(
                    "AQG managed hooks or skills remain, but their official uninstaller root cannot be proven"
                )
            return

        root = next(iter(valid))
        script = root / "scripts" / "install_aqg_clients.py"
        self.aqg_uninstaller = script
        self.add_action("aqg-official-uninstall", script, "all detected user-scope adapters; project scope is preserved")

    def collect(self) -> "Inventory":
        if self.scope in ("de", "both"):
            self.inspect_root(self.de, "de")
        if self.scope in ("aqg", "both"):
            self.inspect_root(self.aqg, "aqg")
        self.inspect_aux()
        self.inspect_aqg_uninstaller()
        self.inspect_qoder_plugins()
        self.inspect_qoder_runtime_caches()
        self.inspect_json()
        self.inspect_codex()
        self.inspect_agents()
        self.inspect_skills()
        self.inspect_host_backups()
        self.inspect_launchagent()
        self.inspect_processes()
        for protected in self.protected:
            if lexists(protected):
                self.notes.append(f"preserve protected source/worktree {protected}")
        return self


def print_inventory(inv: Inventory, *, apply: bool) -> None:
    print("Deep Pattern uninstall plan")
    print(f"platform={sys.platform} scope={inv.scope} mode={'APPLY' if apply else 'DRY-RUN'}")
    for item in inv.actions:
        kind = item["kind"]
        path = item["path"]
        detail = f" ({item['detail']})" if item.get("detail") else ""
        if kind == "process-blocker":
            print(f"BLOCKED PROCESS pid={path} {item['detail']}")
        elif kind == "aqg-official-uninstall":
            print(f"RUN {path}{detail}")
        else:
            print(f"REMOVE {path}{detail}")
    for note in inv.notes:
        print(f"PRESERVE {note.removeprefix('preserve ')}")
    for blocker in inv.blockers:
        print(f"BLOCKED {blocker}")
    if not inv.actions:
        print("NO IN-SCOPE INSTALLATION FOUND")
    if inv.blockers:
        print("STOP: ownership or runtime state is not fully provable; no mutation is allowed.")
    elif not apply:
        print("DRY-RUN only: add --apply after reviewing this plan.")


def backup_root(home: Path, dp: Path, scope: str) -> Path:
    configured = os.environ.get("DE_AQG_BACKUP_ROOT")
    if configured:
        base = lex(Path(configured))
    else:
        base = dp / "uninstall-backups"
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = base / f"{stamp}-dp-uninstall-{scope}"
    suffix = 0
    while lexists(candidate):
        suffix += 1
        candidate = base / f"{stamp}-{suffix}-dp-uninstall-{scope}"
    candidate.mkdir(mode=0o700)
    candidate.chmod(0o700)
    (candidate / "config").mkdir(mode=0o700)
    (candidate / "quarantine").mkdir(mode=0o700)
    (candidate / "manifest.json").touch(mode=0o600)
    return candidate


class Manifest:
    def __init__(self, root: Path, home: Path, scope: str) -> None:
        self.root = root
        self.path = root / "manifest.json"
        self.data: dict[str, Any] = {
            "schema": 1,
            "scope": scope,
            "platform": sys.platform,
            "home": str(home),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "entries": [],
        }
        self.flush()

    def flush(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.path.chmod(0o600)

    def add(self, entry: dict[str, Any]) -> None:
        self.data["entries"].append(entry)
        self.flush()

    def record(self, source: Path, destination: Path, *, operation: str) -> None:
        item: dict[str, Any] = {
            "operation": operation,
            "source": str(source),
            "destination": str(destination),
            "type": "symlink" if source.is_symlink() else "directory" if source.is_dir() else "file",
            "mode": mode_text(source),
            "size": safe_size(source),
        }
        if source.is_symlink():
            item["target"] = os.readlink(source)
        elif source.is_file():
            item["sha256"] = sha256_file(source)
        self.add(item)

    def record_moved(self, original: Path, destination: Path, *, operation: str) -> None:
        item: dict[str, Any] = {
            "operation": operation,
            "source": str(original),
            "destination": str(destination),
            "type": "directory" if destination.is_dir() else "file",
            "mode": mode_text(destination),
            "size": safe_size(destination),
        }
        if destination.is_file():
            item["sha256"] = sha256_file(destination)
        self.add(item)

    def record_tree_moved(self, original_root: Path, destination_root: Path) -> None:
        self.record_moved(original_root, destination_root, operation="quarantine-root")
        for path in sorted(destination_root.rglob("*"), key=lambda item: item.as_posix()):
            original = original_root / path.relative_to(destination_root)
            if path.is_symlink():
                self.add(
                    {
                        "operation": "quarantine-entry",
                        "source": str(original),
                        "destination": str(path),
                        "type": "symlink",
                        "mode": mode_text(path),
                        "size": safe_size(path),
                        "target": os.readlink(path),
                    }
                )
            elif path.is_file():
                self.add(
                    {
                        "operation": "quarantine-entry",
                        "source": str(original),
                        "destination": str(path),
                        "type": "file",
                        "mode": mode_text(path),
                        "size": safe_size(path),
                        "sha256": sha256_file(path),
                    }
                )


def backup_file(manifest: Manifest, source: Path, label: str, index: int) -> Path:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"cannot backup non-regular config file: {source}")
    destination = manifest.root / "config" / f"{index:03d}-{label}"
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    manifest.record(source, destination, operation="backup-config")
    return destination


def pointer_delete(node: Any, pointer: tuple[str, ...]) -> bool:
    if not pointer:
        return False
    if isinstance(node, dict):
        key = pointer[0]
        if len(pointer) == 1:
            if key in node:
                del node[key]
                return True
            return False
        return key in node and pointer_delete(node[key], pointer[1:])
    if isinstance(node, list):
        try:
            index = int(pointer[0])
        except ValueError:
            return False
        if len(pointer) == 1:
            if 0 <= index < len(node):
                del node[index]
                return True
            return False
        return 0 <= index < len(node) and pointer_delete(node[index], pointer[1:])
    return False


def atomic_write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.de-uninstall-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def edit_json(path: Path, pointers: tuple[tuple[str, ...], ...]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    def deletion_order(pointer: tuple[str, ...]) -> tuple[int, int]:
        index = int(pointer[-1]) if pointer and pointer[-1].isdigit() else -1
        return len(pointer), index

    for pointer in sorted(pointers, key=deletion_order, reverse=True):
        if not pointer_delete(data, pointer):
            raise RuntimeError(f"owned JSON entry disappeared before edit: {path}")
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def edit_toml(path: Path, ranges: tuple[tuple[int, int], ...]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    selected = {line for start, end in ranges for line in range(start, end)}
    atomic_write(path, "".join(line for index, line in enumerate(lines) if index not in selected))


def edit_agents(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.strip() == ROUTING_START)
    end = next(index for index, line in enumerate(lines) if line.strip() == ROUTING_END)
    atomic_write(path, "".join(line for index, line in enumerate(lines) if not start <= index <= end))


def launchctl_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    command = os.environ.get("DE_AQG_UNINSTALL_LAUNCHCTL_COMMAND") or shutil.which("launchctl")
    if not command:
        raise RuntimeError("launchctl is unavailable")
    return subprocess.run([command, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)


def bootout_launchagent(inv: Inventory) -> None:
    if not inv.launchagent_loaded:
        return
    result = launchctl_run(["bootout", f"gui/{os.getuid()}/{HOSTBRIDGE_LABEL}"])
    if result.returncode != 0:
        raise RuntimeError(f"LaunchAgent bootout failed: exit {result.returncode}")
    state, detail = inv.launchctl_probe()
    if state != "not-loaded":
        raise RuntimeError(f"LaunchAgent remains loaded after bootout: {detail[:160]}")


def invoke_aqg_uninstaller(inv: Inventory) -> None:
    if not inv.aqg_uninstaller:
        return
    script = inv.aqg_uninstaller
    root = script.parent.parent
    environment = os.environ.copy()
    environment["AQG_ROOT"] = str(root)
    environment["HOME"] = str(inv.home)
    environment.pop("PROJECT_ROOT", None)
    command = [
        sys.executable,
        str(script),
        "--installed-supported",
        "--uninstall",
        "--aqg-root",
        str(root),
        "--home",
        str(inv.home),
    ]
    result = subprocess.run(
        command,
        cwd=str(root),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.returncode != 0:
        raise RuntimeError(f"AQG official user-scope uninstaller failed: exit {result.returncode}")


def apply_inventory(inv: Inventory) -> Path:
    backup = backup_root(inv.home, inv.dp, inv.scope)
    manifest = Manifest(backup, inv.home, inv.scope)
    config_index = 0
    try:
        backup_paths: list[Path] = []
        backup_paths.extend(path for path, _ in inv.json_targets)
        backup_paths.extend(path for path, _ in inv.toml_ranges)
        backup_paths.extend(path for path, _digest in inv.aqg_skill_markers)
        if inv.agents_target:
            backup_paths.append(inv.agents_target)
        backup_paths.extend(inv.host_backups)
        if inv.launchagent:
            backup_paths.append(inv.launchagent)
        seen: set[Path] = set()
        for path in backup_paths:
            if path in seen:
                continue
            seen.add(path)
            config_index += 1
            backup_file(manifest, path, path.name.replace("/", "_"), config_index)

        bootout_launchagent(inv)
        invoke_aqg_uninstaller(inv)

        json_targets = inv.json_targets
        toml_ranges = inv.toml_ranges
        agents_target = inv.agents_target
        qoder_residue_paths = inv.qoder_residue_paths
        if inv.scope == "both" and inv.aqg_uninstaller:
            # AQG and DE can own separate entries in the same host file. The
            # official AQG uninstaller may remove a list item and shift the
            # frozen DE pointer, so rediscover DE targets after that trusted
            # mutation instead of applying stale array indexes.
            refreshed_de = Inventory(inv.home, "de").collect()
            if refreshed_de.blockers:
                raise RuntimeError(
                    "DE configuration changed to an unprovable state after AQG uninstall: "
                    + "; ".join(refreshed_de.blockers)
                )
            refreshed_paths = {
                path for path, _targets in refreshed_de.json_targets
            } | {
                path for path, _ranges in refreshed_de.toml_ranges
            }
            if refreshed_de.agents_target:
                refreshed_paths.add(refreshed_de.agents_target)
            unbacked = sorted(path for path in refreshed_paths if path not in seen)
            if unbacked:
                raise RuntimeError(
                    "AQG uninstall exposed an unbacked DE configuration target: "
                    + ", ".join(str(path) for path in unbacked)
                )
            original_residue_paths = {path for path, _detail in inv.qoder_residue_paths}
            new_residue_paths = {
                path for path, _detail in refreshed_de.qoder_residue_paths
            } - original_residue_paths
            if new_residue_paths:
                raise RuntimeError(
                    "AQG uninstall exposed an unreviewed Qoder residue path: "
                    + ", ".join(str(path) for path in sorted(new_residue_paths))
                )
            json_targets = refreshed_de.json_targets
            toml_ranges = refreshed_de.toml_ranges
            agents_target = refreshed_de.agents_target
            qoder_residue_paths = refreshed_de.qoder_residue_paths

        for path, pointers in json_targets:
            edit_json(path, pointers)
            manifest.add({"operation": "edit-json", "source": str(path), "destination": None})
        for path, ranges in toml_ranges:
            edit_toml(path, ranges)
            manifest.add({"operation": "edit-toml", "source": str(path), "destination": None})
        if agents_target:
            edit_agents(agents_target)
            manifest.add({"operation": "edit-agents", "source": str(agents_target), "destination": None})

        for path in inv.skill_links:
            # The AQG official uninstaller may remove its own links before this
            # pass. Treat that expected race as idempotent; fail closed if a
            # planned link was replaced by a non-symlink.
            if not lexists(path):
                manifest.add({"operation": "unlink-skill", "source": str(path), "status": "already-removed"})
                continue
            if not path.is_symlink():
                raise RuntimeError(f"owned skill path changed before unlink: {path}")
            target = os.readlink(path)
            path.unlink()
            manifest.add({"operation": "unlink-skill", "source": str(path), "target": target})

        marker_dirs: set[Path] = set()
        for path, expected_digest in inv.aqg_skill_markers:
            marker_dirs.add(path.parent)
            if not lexists(path):
                manifest.add(
                    {"operation": "unlink-skill-marker", "source": str(path), "status": "already-removed"}
                )
                continue
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"owned AQG skill marker changed type before unlink: {path}")
            if sha256_file(path) != expected_digest:
                raise RuntimeError(f"owned AQG skill marker changed before unlink: {path}")
            path.unlink()
            manifest.add({"operation": "unlink-skill-marker", "source": str(path)})
        for directory in marker_dirs:
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()
                manifest.add({"operation": "remove-empty-marker-dir", "source": str(directory)})

        for path, detail in qoder_residue_paths:
            if not lexists(path):
                manifest.add(
                    {
                        "operation": "quarantine-qoder-residue",
                        "source": str(path),
                        "status": "already-removed",
                    }
                )
                continue
            relative = path.relative_to(inv.home)
            label = "__".join(relative.parts)
            destination = backup / "quarantine" / "qoder-residue" / label
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            manifest.record_tree_moved(path, destination)
            manifest.add(
                {
                    "operation": "quarantine-qoder-residue",
                    "source": str(path),
                    "destination": str(destination),
                    "detail": detail,
                }
            )

        for index, original in enumerate(inv.host_backups, start=1):
            if not lexists(original):
                continue
            destination = backup / "quarantine" / "host-backups" / f"{index:03d}-{original.name}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original), str(destination))
            manifest.record_moved(original, destination, operation="quarantine-host-backup")

        if inv.launchagent:
            original = inv.launchagent
            destination = backup / "quarantine" / "launchagents" / original.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original), str(destination))
            manifest.record_moved(original, destination, operation="quarantine-launchagent")

        for root in inv.runtime_roots:
            if not lexists(root):
                continue
            destination = backup / "quarantine" / "runtime" / root.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root), str(destination))
            manifest.record_tree_moved(root, destination)

        for path in inv.de_aux:
            if not lexists(path):
                continue
            destination = backup / "quarantine" / "aux" / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            if destination.is_dir():
                manifest.record_tree_moved(path, destination)
            else:
                manifest.record_moved(path, destination, operation="quarantine-aux")

        for path in inv.aqg_aux:
            if not lexists(path):
                continue
            destination = backup / "quarantine" / "aux" / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            manifest.record_tree_moved(path, destination)

        roots: list[Path] = []
        if inv.scope in ("de", "both"):
            roots.append(inv.de)
        if inv.scope in ("aqg", "both") and inv.aqg_managed_target is None:
            roots.append(inv.aqg)
        for source in roots:
            if not lexists(source):
                continue
            destination = backup / "quarantine" / "managed" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            manifest.record_tree_moved(source, destination)

        if inv.scope in ("aqg", "both") and inv.aqg_managed_target is not None:
            source = inv.aqg
            target = inv.aqg_managed_target
            try:
                same_target = os.path.samefile(source, target)
            except OSError:
                same_target = False
            if not source.is_symlink() or not same_target:
                raise RuntimeError("managed AQG root link changed before quarantine")
            link_destination = backup / "quarantine" / "managed" / source.name
            link_destination.parent.mkdir(parents=True, exist_ok=True)
            link_text = os.readlink(source)
            shutil.move(str(source), str(link_destination))
            manifest.add(
                {
                    "operation": "quarantine-root-link",
                    "source": str(source),
                    "destination": str(link_destination),
                    "type": "symlink",
                    "target": link_text,
                }
            )
            if not target.is_dir() or target.is_symlink():
                raise RuntimeError("managed AQG version target changed before quarantine")
            target_destination = (
                backup / "quarantine" / "managed-versions" / target.name
            )
            target_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(target_destination))
            manifest.record_tree_moved(
                target, target_destination
            )

        manifest.add({"operation": "apply-complete", "source": None, "destination": str(backup)})
        verify_after_apply(inv)
        return backup
    except Exception as exc:
        manifest.add({"operation": "apply-failed", "error": str(exc)})
        raise


def verify_after_apply(inv: Inventory) -> None:
    time.sleep(0.2)
    check = Inventory(inv.home, inv.scope).collect()
    remaining = list(check.actions)
    if check.blockers:
        raise RuntimeError("post-uninstall verification blocked: " + "; ".join(check.blockers))
    if remaining:
        paths = ", ".join(item["path"] for item in remaining[:8])
        raise RuntimeError(f"post-uninstall verification found remaining in-scope state: {paths}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dp-uninstall",
        description="Safely quarantine Deep Pattern managed installs and remove owned host integrations.",
    )
    parser.add_argument("--scope", choices=("de", "aqg", "both"), required=True)
    parser.add_argument("--apply", action="store_true", help="perform the reviewed cleanup")
    parser.add_argument("--home", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if sys.platform != "darwin":
        print(f"unsupported platform: {sys.platform}; macOS cleanup is the only implemented target")
        return EXIT_UNSUPPORTED
    home = lex(Path(args.home)) if args.home else lex(Path.home())
    if not home.is_dir():
        print(f"home directory does not exist: {home}")
        return EXIT_USAGE
    inventory = Inventory(home, args.scope).collect()
    print_inventory(inventory, apply=args.apply)
    if inventory.blockers:
        return EXIT_BLOCKED
    if not args.apply:
        return EXIT_OK
    try:
        backup = apply_inventory(inventory)
    except Exception as exc:
        print(f"ERROR: uninstall stopped without a clean verification: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    print(f"PASS: uninstall verified; quarantine={backup}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY
