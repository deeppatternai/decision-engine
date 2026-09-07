#Requires -Version 5.1
<#
.SYNOPSIS
Safely uninstalls or quarantines Deep Pattern managed state on native Windows.

.DESCRIPTION
The default mode is read-only. Pass -Apply only after reviewing the dry-run
plan. The uninstaller removes only integrations whose current managed fields
still match Decision Engine ownership evidence. Foreign or modified entries,
unexpected reparse points, active host processes, and unknown root layouts stop
the entire operation before mutation.

Source/worktree checkouts are always preserved. Removed managed roots and
configuration snapshots are retained under the current user's private
.deeppattern\uninstall-backups directory.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("de", "aqg", "both")]
    [string]$Scope,

    [switch]$Apply,

    [Parameter(DontShow = $true)]
    [string]$HomePath = $HOME
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgramName = "dp-uninstall"

function Stop-Uninstall {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine("{0}: ERROR: {1}" -f $ProgramName, $Message)
    exit 2
}

function Invoke-Clean {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @()
    )
    $names = @("DE_ENDPOINT", "DE_ACTIVATION_SECRET", "PYTHONPATH")
    $saved = @{}
    foreach ($name in $names) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    try {
        & $FilePath @ArgumentList | ForEach-Object {
            [Console]::Out.WriteLine([string]$_)
        }
        $code = $LASTEXITCODE
        return $code
    }
    finally {
        foreach ($name in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
        }
    }
}

function Test-Python {
    param([Parameter(Mandatory = $true)][string]$Candidate)
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $output = @(& $Candidate -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)" 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    return $true
}

function Resolve-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:DE_AQG_PYTHON)) {
        $candidates.Add($env:DE_AQG_PYTHON)
    }
    $managedPython = Join-Path $HomePath ".deeppattern\decision-engine\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $managedPython -PathType Leaf) {
        $candidates.Add($managedPython)
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        foreach ($selector in @("-3.14", "-3.13", "-3.12", "-3")) {
            $resolved = @(& $py.Source $selector -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $resolved.Count -gt 0) {
                $candidates.Add(([string]$resolved[$resolved.Count - 1]).Trim())
            }
        }
    }
    foreach ($name in @("python3.exe", "python.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            $candidates.Add($command.Source)
        }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Python -Candidate $candidate) {
            return (Get-Item -LiteralPath $candidate).FullName
        }
    }
    Stop-Uninstall "Python 3.12 or newer is required."
}

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    Stop-Uninstall "This entrypoint supports native Windows only."
}

$PythonPath = Resolve-Python
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("dp-uninstall-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
$helperPath = Join-Path $temporaryRoot "dp_windows_uninstall.py"

$helperSource = @'
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 3
SERVER_NAME = "decision-engine"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
HOOK_MARKERS = (
    "decision-engine-audit-routing-v1",
    "decision-engine-audit-routing-experiment",
    "decision-engine-workbuddy-ai-audit-routing-v1",
    "decision-engine-trae-cn-audit-routing-v1",
)
HOOK_SCRIPTS = (
    "qoder_audit_prompt_hook.py",
    "workbuddy_audit_prompt_hook.py",
    "trae_cn_audit_prompt_hook.py",
)
HOST_PROCESSES = {
    "claude.exe",
    "codebuddy.exe",
    "codex.exe",
    "cursor.exe",
    "qoder.exe",
    "trae.exe",
    "workbuddy.exe",
}
DE_PRODUCT_REMOTES = {
    "github": "https://github.com/deeppatternai/decision-engine.git",
    "gitee": "https://gitee.com/deeppatternai/decision-engine.git",
}
AQG_PRODUCT_REMOTE = "https://github.com/deeppatternai/agent-quality-gates.git"


def known_windows_config_paths(home: Path) -> tuple[Path, ...]:
    appdata = Path(os.getenv("APPDATA") or home / "AppData" / "Roaming")
    return (
        home / ".claude.json",
        appdata / "Claude" / "claude_desktop_config.json",
        home / ".codex" / "config.toml",
        home / ".cursor" / "mcp.json",
        home / ".codebuddy" / "mcp.json",
        home / ".qoder" / "mcp.json",
        home / ".qoder-cn" / "settings.json",
        appdata / "TRAE SOLO" / "User" / "mcp.json",
        appdata / "TRAE SOLO CN" / "User" / "mcp.json",
        home / ".workbuddy" / "mcp.json",
    )


def known_windows_skill_roots(home: Path) -> tuple[Path, ...]:
    return (
        home / ".claude" / "skills",
        home / ".codex" / "skills",
        home / ".cursor" / "skills",
        home / ".codebuddy" / "skills",
        home / ".qoder" / "skills",
        home / ".qoder-cn" / "skills",
        home / ".trae" / "skills",
        home / ".trae-cn" / "skills",
        home / ".workbuddy" / "skills",
    )


def lex(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def is_under(path: Path, parent: Path) -> bool:
    try:
        lex(path).relative_to(lex(parent))
        return True
    except ValueError:
        return False


def is_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def reject_reparse_components(path: Path, floor: Path) -> Path | None:
    path = lex(path)
    floor = lex(floor)
    try:
        relative = path.relative_to(floor)
    except ValueError:
        return path
    current = floor
    for part in relative.parts:
        current = current / part
        if is_reparse(current):
            return current
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def git_output(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git.exe", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("git inspection failed")
    return completed.stdout.strip()


def checkout_is_clean(root: Path) -> bool:
    try:
        return not git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def de_checkout_has_product_remotes(root: Path) -> bool:
    try:
        names = tuple(line for line in git_output(root, "remote").splitlines() if line)
        if set(names) != set(DE_PRODUCT_REMOTES):
            return False
        return all(
            git_output(root, "remote", "get-url", name) == url
            for name, url in DE_PRODUCT_REMOTES.items()
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def aqg_checkout_has_product_remote(root: Path) -> bool:
    try:
        return git_output(root, "remote", "get-url", "origin") == AQG_PRODUCT_REMOTE
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def read_json(path: Path) -> dict[str, Any]:
    before = path.lstat()
    if is_reparse(path) or not stat.S_ISREG(before.st_mode):
        raise ValueError("configuration is not a regular file")
    if before.st_size > MAX_CONFIG_BYTES:
        raise ValueError("configuration is too large")
    data = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=json_object_without_duplicates,
    )
    if not isinstance(data, dict):
        raise ValueError("configuration root is not an object")
    return data


def atomic_write(path: Path, text: str) -> None:
    link = reject_reparse_components(path.parent, Path.home())
    if link is not None:
        raise ValueError("configuration parent contains a reparse point")
    fd, name = tempfile.mkstemp(prefix=".%s.dp-uninstall-" % path.name, dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def hook_is_owned(item: Any, managed_root: Path) -> bool:
    if not isinstance(item, dict):
        return False
    command = item.get("command")
    if not isinstance(command, str):
        return False
    normalized = command.replace("\\", "/")
    root = str(managed_root).replace("\\", "/")
    return (
        root in normalized
        and any(marker in normalized for marker in HOOK_MARKERS + HOOK_SCRIPTS)
    )


def remove_owned_hooks(data: dict[str, Any], managed_root: Path) -> bool:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        next_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                next_groups.append(group)
                continue
            old_items = group["hooks"]
            new_items = [item for item in old_items if not hook_is_owned(item, managed_root)]
            if len(new_items) != len(old_items):
                changed = True
                if new_items:
                    replacement = dict(group)
                    replacement["hooks"] = new_items
                    next_groups.append(replacement)
            else:
                next_groups.append(group)
        if next_groups:
            hooks[event] = next_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    return changed


def toml_without_server(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    header = re.compile(r'^\s*\[\s*mcp_servers\.decision-engine(?:\.|\s*\])')
    any_table = re.compile(r'^\s*\[')
    output: list[str] = []
    removing = False
    changed = False
    for line in lines:
        if header.match(line):
            removing = True
            changed = True
            continue
        if removing and any_table.match(line):
            removing = False
        if not removing:
            output.append(line)
    return "".join(output), changed


@dataclass
class Action:
    kind: str
    path: Path
    detail: str
    client: str | None = None
    expected_sha256: str | None = None


@dataclass
class Inventory:
    home: Path
    scope: str
    actions: list[Action] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    de_root: Path = field(init=False)
    aqg_root: Path = field(init=False)
    source_root: Path = field(init=False)
    aqg_target: Path | None = None
    aqg_uninstaller: Path | None = None

    def __post_init__(self) -> None:
        self.home = lex(self.home)
        self.dp = self.home / ".deeppattern"
        self.de_root = self.dp / "decision-engine"
        self.aqg_root = self.dp / "agent-quality-gates"
        self.source_root = self.dp / "decision-engine-root"

    def add(self, kind: str, path: Path, detail: str, **kw: Any) -> None:
        key = (kind, path_key(path), kw.get("client"))
        if any((a.kind, path_key(a.path), a.client) == key for a in self.actions):
            return
        self.actions.append(Action(kind, lex(path), detail, **kw))

    def inspect_processes(self) -> None:
        roots = []
        if self.scope in ("de", "both"):
            roots.append(self.de_root)
        if self.scope in ("aqg", "both"):
            roots.append(self.aqg_root)
        if not any(lexists(root) for root in roots):
            return
        try:
            result = subprocess.run(
                ["tasklist.exe", "/fo", "csv", "/nh"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self.blockers.append("running host processes could not be inspected")
            return
        if result.returncode != 0:
            self.blockers.append("running host processes could not be inspected")
            return
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) < 2:
                continue
            image = row[0].strip().lower()
            if image in HOST_PROCESSES:
                self.blockers.append("live host process must be stopped before uninstall: %s pid=%s" % (row[0], row[1]))

    def inspect_root(self, root: Path, component: str) -> None:
        if not lexists(root):
            self.notes.append("PRESERVE absent %s" % root)
            return
        if is_reparse(root):
            if component != "aqg":
                self.blockers.append("%s root is a reparse point: %s" % (component, root))
                return
            try:
                raw = os.readlink(root)
                target = Path(raw)
                if not target.is_absolute():
                    target = root.parent / target
                target = lex(target)
            except OSError:
                self.blockers.append("AQG root reparse target cannot be read: %s" % root)
                return
            versions = self.dp / "versions"
            if not is_under(target, versions) or is_reparse(target):
                self.blockers.append("AQG root reparse target is outside managed versions: %s" % root)
                return
            required = (target / "scripts" / "install_aqg_clients.py", target / "VERSION", target / "requirements.txt")
            if not all(path.is_file() and not is_reparse(path) for path in required):
                self.blockers.append("AQG managed target has unexpected layout: %s" % target)
                return
            try:
                target_head = git_output(target, "rev-parse", "HEAD")
            except (OSError, subprocess.SubprocessError, ValueError):
                target_head = ""
            if (
                not aqg_checkout_has_product_remote(target)
                or not checkout_is_clean(target)
                or not re.fullmatch(r"[0-9a-f]{40}", target.name)
                or target_head != target.name
            ):
                self.blockers.append("AQG managed target source identity is unknown: %s" % target)
                return
            self.aqg_target = target
            self.aqg_uninstaller = required[0]
            self.add("quarantine-root-link", root, "AQG managed root link")
            self.add("quarantine-root", target, "AQG managed version target")
            return
        if not root.is_dir():
            self.blockers.append("%s root is not a directory: %s" % (component, root))
            return
        if reject_reparse_components(root, self.home) is not None:
            self.blockers.append("%s root has a reparse path component: %s" % (component, root))
            return
        if component == "de":
            required = (root / ".git", root / "installer", root / "skills", root / "pyproject.toml", root / "VERSION")
        else:
            required = (root / ".git", root / "scripts" / "install_aqg_clients.py", root / "VERSION", root / "requirements.txt")
        if not all(path.exists() and not is_reparse(path) for path in required):
            self.blockers.append("%s root has unexpected layout; ownership is unknown: %s" % (component, root))
            return
        if component == "de" and (
            not de_checkout_has_product_remotes(root) or not checkout_is_clean(root)
        ):
            self.blockers.append("DE managed checkout source identity is unknown or dirty: %s" % root)
            return
        if component == "aqg" and (
            not aqg_checkout_has_product_remote(root) or not checkout_is_clean(root)
        ):
            self.blockers.append("AQG checkout source identity is unknown or dirty: %s" % root)
            return
        if component == "aqg":
            self.aqg_uninstaller = root / "scripts" / "install_aqg_clients.py"
        self.add("quarantine-root", root, component)

    def import_de(self):
        if not self.de_root.is_dir() or is_reparse(self.de_root):
            return None
        sys.path.insert(0, str(self.de_root))
        try:
            from installer import client_host_ownership, managed_install, mcp_config
            return client_host_ownership, managed_install, mcp_config
        except Exception as exc:
            self.blockers.append("Decision Engine ownership modules cannot be loaded: %s" % type(exc).__name__)
            return None

    def inspect_de_integrations(self) -> None:
        modules = self.import_de()
        if modules is None:
            self.inspect_unprovable_residue()
            return
        client_host_ownership, managed_install, mcp_config = modules
        inspected_paths: set[str] = set()
        record_paths: set[str] = set()
        try:
            clients = tuple(mcp_config.CLIENTS)
        except Exception:
            self.blockers.append("Decision Engine host registry cannot be read")
            return
        for client in clients:
            try:
                path = lex(mcp_config.agent_config_path(client))
                actual = mcp_config.read_entry(client)
            except Exception as exc:
                self.blockers.append("%s MCP configuration cannot be inspected: %s" % (client, type(exc).__name__))
                continue
            if actual is None:
                continue
            if not isinstance(actual, dict):
                self.blockers.append("%s decision-engine entry is not an object" % client)
                continue
            owned = False
            spec = mcp_config.CLIENT_SPECS[client]
            try:
                record = client_host_ownership.read_record_if_present(
                    spec.host_family,
                    managed_root=self.de_root,
                    config_path=path,
                    server_name=SERVER_NAME,
                )
                if record is not None:
                    actual_hash = client_host_ownership.managed_entry_sha256_v1(actual)
                    owned = actual_hash == record.managed_fields_sha256
                    record_path = client_host_ownership.ownership_record_path(spec.host_family)
                    if path_key(record_path) not in record_paths:
                        self.add(
                            "quarantine-record",
                            record_path,
                            "owned DE host record",
                            client=client,
                            expected_sha256=sha256_file(record_path),
                        )
                        record_paths.add(path_key(record_path))
            except Exception:
                owned = False
            if not owned:
                try:
                    desired = mcp_config._render_client_entry(
                        client,
                        python=actual.get("command"),
                        cwd=self.de_root,
                    )
                    owned = (
                        client_host_ownership.managed_entry_sha256_v1(actual)
                        == client_host_ownership.managed_entry_sha256_v1(desired)
                    )
                except Exception:
                    owned = False
            if not owned:
                self.blockers.append("%s %s entry ownership is unknown: %s" % (client, SERVER_NAME, path))
                continue
            if path_key(path) not in inspected_paths:
                if not path.is_file() or is_reparse(path):
                    self.blockers.append("owned host configuration is not a regular file: %s" % path)
                    continue
                kind = "edit-toml" if path.suffix.lower() == ".toml" else "edit-json"
                self.add(
                    kind,
                    path,
                    "remove owned Decision Engine MCP entry and hook",
                    client=client,
                    expected_sha256=sha256_file(path),
                )
                inspected_paths.add(path_key(path))

        for path in known_windows_config_paths(self.home):
            if path_key(path) in inspected_paths or not path.is_file() or is_reparse(path):
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            normalized = raw.replace("\\", "/")
            root_text = str(self.de_root).replace("\\", "/")
            if root_text not in normalized or not any(
                marker in normalized for marker in HOOK_MARKERS + HOOK_SCRIPTS
            ):
                continue
            try:
                data = read_json(path)
                probe = json.loads(json.dumps(data))
            except (OSError, UnicodeError, ValueError, TypeError):
                self.blockers.append("DE hook configuration cannot be inspected safely: %s" % path)
                continue
            if not remove_owned_hooks(probe, self.de_root):
                self.blockers.append("DE hook ownership is unknown: %s" % path)
                continue
            self.add(
                "edit-json",
                path,
                "remove owned Decision Engine hook",
                expected_sha256=sha256_file(path),
            )
            inspected_paths.add(path_key(path))

        skill_names = []
        skills_root = self.de_root / "skills"
        if skills_root.is_dir() and not is_reparse(skills_root):
            skill_names = [path.name for path in skills_root.iterdir() if path.is_dir() and not is_reparse(path)]
        destinations: dict[str, Path] = {}
        for spec in mcp_config.CLIENT_SPECS.values():
            if spec.skills_global_path is None:
                continue
            try:
                destination = lex(spec.skills_global_path())
            except Exception:
                continue
            destinations[path_key(destination)] = destination
        for destination in destinations.values():
            if reject_reparse_components(destination.parent, self.home) is not None:
                self.blockers.append("skill root parent contains a reparse point: %s" % destination)
                continue
            for name in skill_names:
                route = destination / name
                if not lexists(route):
                    continue
                if not is_reparse(route):
                    self.notes.append("PRESERVE foreign real skill directory %s" % route)
                    continue
                try:
                    target = Path(os.readlink(route))
                    if not target.is_absolute():
                        target = route.parent / target
                    target = lex(target)
                except OSError:
                    self.blockers.append("skill reparse target cannot be read: %s" % route)
                    continue
                expected = lex(skills_root / name)
                if path_key(target) != path_key(expected):
                    self.blockers.append("skill route points outside this install: %s" % route)
                    continue
                self.add("remove-skill-route", route, "owned DE skill junction")

        try:
            registration = managed_install.registration_path()
            if lexists(registration):
                if is_reparse(registration) or not registration.is_file():
                    self.blockers.append("managed registration is not a regular file: %s" % registration)
                else:
                    self.add(
                        "quarantine-record",
                        registration,
                        "owned DE managed registration",
                        expected_sha256=sha256_file(registration),
                    )
        except Exception as exc:
            self.blockers.append("managed registration cannot be inspected: %s" % type(exc).__name__)

    def inspect_unprovable_residue(self) -> None:
        found = False
        for path in known_windows_config_paths(self.home):
            if not path.is_file() or is_reparse(path):
                continue
            try:
                if SERVER_NAME in path.read_text(encoding="utf-8"):
                    self.blockers.append(
                        "Decision Engine root is unavailable, so host entry ownership cannot be proven: %s" % path
                    )
                    found = True
            except (OSError, UnicodeError):
                self.blockers.append("host configuration cannot be inspected safely: %s" % path)
                found = True
        expected_parent = path_key(self.de_root / "skills")
        for skills_root in known_windows_skill_roots(self.home):
            if not skills_root.is_dir() or is_reparse(skills_root):
                continue
            try:
                entries = tuple(skills_root.iterdir())
            except OSError:
                self.blockers.append("skill root cannot be inspected safely: %s" % skills_root)
                found = True
                continue
            for route in entries:
                if not is_reparse(route):
                    continue
                try:
                    target = Path(os.readlink(route))
                    if not target.is_absolute():
                        target = route.parent / target
                    target = lex(target)
                except OSError:
                    self.blockers.append("skill reparse target cannot be read: %s" % route)
                    found = True
                    continue
                if path_key(target.parent) == expected_parent:
                    self.blockers.append(
                        "Decision Engine root is unavailable, so orphaned skill ownership requires recovery: %s" % route
                    )
                    found = True
        if not found:
            self.notes.append("PRESERVE no provable DE integration residue without a managed root")

    def inspect(self) -> None:
        self.inspect_processes()
        self.notes.append("PRESERVE protected source/worktree %s" % self.source_root)
        if self.scope in ("de", "both"):
            self.inspect_root(self.de_root, "de")
            self.inspect_de_integrations()
        if self.scope in ("aqg", "both"):
            self.inspect_root(self.aqg_root, "aqg")
            if lexists(self.aqg_root) and self.aqg_uninstaller is None:
                self.blockers.append("AQG official uninstaller ownership cannot be proven")


def print_inventory(inv: Inventory, apply: bool) -> None:
    print("Deep Pattern uninstall plan")
    print("platform=win32 scope=%s mode=%s" % (inv.scope, "APPLY" if apply else "DRY-RUN"))
    for action in inv.actions:
        if action.kind == "aqg-uninstall":
            print("RUN %s (%s)" % (action.path, action.detail))
        else:
            print("REMOVE %s (%s)" % (action.path, action.detail))
    for note in inv.notes:
        print(note)
    for blocker in inv.blockers:
        print("BLOCKED %s" % blocker)
    if inv.blockers:
        print("STOP: ownership or runtime state is not fully provable; no mutation is allowed.")
    elif not apply:
        print("DRY-RUN only: add -Apply after reviewing this plan.")


class Manifest:
    def __init__(self, root: Path, scope: str) -> None:
        self.root = root
        self.data: dict[str, Any] = {
            "schema": 1,
            "platform": "win32",
            "scope": scope,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "items": [],
        }

    def add(self, source: Path, destination: Path | None, operation: str, digest: str | None = None) -> None:
        self.data["items"].append(
            {
                "source": str(source),
                "destination": str(destination) if destination else None,
                "operation": operation,
                "sha256": digest,
            }
        )

    def write(self) -> None:
        (self.root / "manifest.json").write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def create_backup_root(inv: Inventory) -> Path:
    base = inv.dp / "uninstall-backups"
    if reject_reparse_components(base.parent, inv.home) is not None:
        raise RuntimeError("backup parent contains a reparse point")
    base.mkdir(parents=True, exist_ok=True)
    if is_reparse(base):
        raise RuntimeError("backup root is a reparse point")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    root = base / (stamp + "-dp-uninstall-" + inv.scope)
    suffix = 1
    while lexists(root):
        root = base / (stamp + "-%d-dp-uninstall-%s" % (suffix, inv.scope))
        suffix += 1
    root.mkdir(mode=0o700)
    (root / "quarantine").mkdir()
    return root


def copy_config_backup(path: Path, backup: Path, manifest: Manifest, index: int) -> None:
    destination = backup / "quarantine" / "configs" / ("%03d-%s" % (index, path.name))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    manifest.add(path, destination, "configuration-snapshot", sha256_file(destination))


def apply_config(action: Action, managed_root: Path, backup: Path, manifest: Manifest, index: int) -> None:
    path = action.path
    if not path.is_file() or is_reparse(path):
        raise RuntimeError("configuration changed before apply: %s" % path)
    if sha256_file(path) != action.expected_sha256:
        raise RuntimeError("configuration changed after review: %s" % path)
    copy_config_backup(path, backup, manifest, index)
    if action.kind == "edit-json":
        data = read_json(path)
        servers = data.get("mcpServers")
        changed = False
        if isinstance(servers, dict) and SERVER_NAME in servers:
            servers.pop(SERVER_NAME)
            changed = True
            if not servers:
                data.pop("mcpServers", None)
        changed = remove_owned_hooks(data, managed_root) or changed
        if not changed:
            raise RuntimeError("owned JSON entry disappeared before apply: %s" % path)
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    else:
        text = path.read_text(encoding="utf-8")
        rendered, changed = toml_without_server(text)
        if not changed:
            raise RuntimeError("owned TOML entry disappeared before apply: %s" % path)
        atomic_write(path, rendered)
    manifest.add(path, None, "remove-owned-host-entry", sha256_file(path))


def remove_skill_route(action: Action, manifest: Manifest) -> None:
    if not is_reparse(action.path):
        raise RuntimeError("skill junction changed before apply: %s" % action.path)
    target = os.readlink(action.path)
    os.rmdir(action.path)
    if lexists(action.path):
        raise RuntimeError("skill junction removal could not be verified: %s" % action.path)
    manifest.add(action.path, None, "remove-owned-skill-junction", hashlib.sha256(target.encode("utf-8")).hexdigest())


def quarantine_path(action: Action, backup: Path, manifest: Manifest, index: int) -> None:
    source = action.path
    if not lexists(source):
        return
    if action.expected_sha256 is not None:
        if is_reparse(source) or not source.is_file() or sha256_file(source) != action.expected_sha256:
            raise RuntimeError("ownership record changed after review: %s" % source)
    destination = backup / "quarantine" / action.kind / ("%03d-%s" % (index, source.name))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if action.kind == "quarantine-root-link":
        target = os.readlink(source)
        os.rmdir(source)
        manifest.add(source, None, action.kind, hashlib.sha256(target.encode("utf-8")).hexdigest())
        return
    os.replace(source, destination)
    manifest.add(source, destination, action.kind, action.expected_sha256)


def invoke_aqg_uninstaller(inv: Inventory) -> None:
    if inv.aqg_uninstaller is None:
        return
    root = inv.aqg_target or inv.aqg_root
    command = [
        sys.executable,
        str(inv.aqg_uninstaller),
        "--installed-supported",
        "--uninstall",
        "--aqg-root",
        str(root),
        "--home",
        str(inv.home),
    ]
    environment = os.environ.copy()
    for key in ("DE_ENDPOINT", "DE_ACTIVATION_SECRET", "PYTHONPATH"):
        environment.pop(key, None)
    completed = subprocess.run(command, env=environment, check=False)
    if completed.returncode != 0:
        raise RuntimeError("AQG official user-scope uninstaller failed: exit %d" % completed.returncode)


def apply_inventory(inv: Inventory) -> Path:
    backup = create_backup_root(inv)
    manifest = Manifest(backup, inv.scope)
    manifest.write()
    if inv.scope in ("aqg", "both"):
        invoke_aqg_uninstaller(inv)

    de_actions: list[Action] = []
    if inv.scope in ("de", "both"):
        fresh = Inventory(inv.home, "de")
        fresh.inspect()
        fresh.blockers = [item for item in fresh.blockers if not item.startswith("live host process")]
        if fresh.blockers:
            raise RuntimeError("DE state changed after AQG uninstall: " + "; ".join(fresh.blockers))
        de_actions = fresh.actions

    config_actions = [a for a in de_actions if a.kind in {"edit-json", "edit-toml"}]
    route_actions = [a for a in de_actions if a.kind == "remove-skill-route"]
    record_actions = [a for a in de_actions if a.kind == "quarantine-record"]
    de_root_actions = [a for a in de_actions if a.kind == "quarantine-root"]

    for index, action in enumerate(config_actions, 1):
        apply_config(action, inv.de_root, backup, manifest, index)
        manifest.write()
    for action in route_actions:
        remove_skill_route(action, manifest)
        manifest.write()
    for index, action in enumerate(record_actions, 1):
        quarantine_path(action, backup, manifest, index)
        manifest.write()

    if inv.scope in ("aqg", "both"):
        aqg_actions = [
            a for a in inv.actions if a.kind in {"quarantine-root", "quarantine-root-link"}
            and (a.path == inv.aqg_root or a.path == inv.aqg_target)
        ]
        for index, action in enumerate(aqg_actions, 1):
            quarantine_path(action, backup, manifest, index)
            manifest.write()

    for index, action in enumerate(de_root_actions, 1):
        quarantine_path(action, backup, manifest, index)
        manifest.write()

    manifest.write()
    return backup


def verify_after_apply(home: Path, scope: str) -> None:
    dp = home / ".deeppattern"
    remaining = []
    if scope in ("de", "both") and lexists(dp / "decision-engine"):
        remaining.append(str(dp / "decision-engine"))
    if scope in ("aqg", "both") and lexists(dp / "agent-quality-gates"):
        remaining.append(str(dp / "agent-quality-gates"))
    if remaining:
        raise RuntimeError("post-uninstall verification found remaining roots: " + ", ".join(remaining))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dp-uninstall")
    parser.add_argument("--scope", choices=("de", "aqg", "both"), required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--home", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if os.name != "nt" or sys.platform != "win32":
        print("unsupported platform: native Windows is required", file=sys.stderr)
        return EXIT_USAGE
    inv = Inventory(args.home, args.scope)
    inv.inspect()
    if inv.scope in ("aqg", "both") and inv.aqg_uninstaller is not None:
        inv.actions.insert(0, Action("aqg-uninstall", inv.aqg_uninstaller, "all detected user-scope adapters; project scope is preserved"))
    print_inventory(inv, args.apply)
    if inv.blockers:
        return EXIT_BLOCKED
    if not args.apply:
        return EXIT_OK
    try:
        backup = apply_inventory(inv)
        verify_after_apply(inv.home, inv.scope)
    except Exception as exc:
        print("ERROR: uninstall stopped without a clean verification: %s" % exc, file=sys.stderr)
        return EXIT_BLOCKED
    print("PASS: uninstall verified; quarantine=%s" % backup)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'@

try {
    [IO.File]::WriteAllText(
        $helperPath,
        $helperSource,
        (New-Object System.Text.UTF8Encoding($false))
    )
    $arguments = @($helperPath, "--scope", $Scope, "--home", $HomePath)
    if ($Apply) {
        $arguments += "--apply"
    }
    $code = Invoke-Clean -FilePath $PythonPath -ArgumentList $arguments
    exit $code
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
