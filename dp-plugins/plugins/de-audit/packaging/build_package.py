#!/usr/bin/env python3
"""Build the deterministic, platform-independent remote de-audit plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path


PLUGIN_ID = "audit@deeppattern"
REMOTE_RESOURCE = "https://endpoint.deeppattern.ai/plugin/mcp/v1/audit"
CLAUDE_TRANSPORT_AUTH_HEADERS = {
    "X-DeepPattern-MCP-Auth-Mode": "transport",
}
OPENAI_OAUTH_EXTENSION = {
    "com.openai": {
        "auth": {
            "type": "oauth",
            "client": {"mode": "cimd"},
            "resource": REMOTE_RESOURCE,
            "baseScopes": ["audit:submit", "audit:read", "audit:events"],
        }
    }
}
DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    "codex/.codex-plugin/plugin.json",
    "codex/.mcp.json",
    "codex/LICENSE.md",
    "codex/skills/audit/SKILL.md",
    "codex/skills/setup/SKILL.md",
    "cowork/.claude-plugin/plugin.json",
    "cowork/.mcp.json",
    "cowork/LICENSE.md",
    "cowork/skills/audit/SKILL.md",
    "cowork/skills/setup/SKILL.md",
    ".cursor-plugin/plugin.json",
    ".mcp.json",
    "LICENSE.md",
    "README.md",
    "after-install.md",
    "contracts/audit-endpoint-contract.json",
    "cursor.mcp.json",
    "mcp.json",
    "plugin.json",
    "skills/audit/SKILL.md",
    "skills/setup/SKILL.md",
)
COWORK_SOURCE_FILES = {
    "cowork/LICENSE.md": "LICENSE.md",
    "cowork/.claude-plugin/plugin.json": ".claude-plugin/plugin.json",
    "cowork/.mcp.json": ".mcp.json",
    "cowork/skills/audit/SKILL.md": "skills/audit/SKILL.md",
    "cowork/skills/setup/SKILL.md": "skills/setup/SKILL.md",
}
FORBIDDEN_SOURCE_PATHS = (
    "runtime",
    "activation_ui.py",
    "audit_progress.py",
    "de_audit_runtime.py",
    "launcher.py",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _plugin_version(source_root: Path) -> str:
    value = json.loads((source_root / "plugin.json").read_text(encoding="utf-8"))
    version = value.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("plugin.json does not declare a non-empty version")
    return version


VERSION = _plugin_version(DEFAULT_SOURCE_ROOT)


def _validate_remote_manifest(data: bytes, relative: str) -> None:
    value = json.loads(data)
    server = value["mcpServers"]["audit"]
    expected = {"type": "http", "url": REMOTE_RESOURCE}
    if relative in {".mcp.json", "cowork/.mcp.json"}:
        expected["headers"] = CLAUDE_TRANSPORT_AUTH_HEADERS
    if relative == "codex/.mcp.json":
        expected["extensions"] = OPENAI_OAUTH_EXTENSION
    if server != expected:
        raise ValueError(f"{relative} does not declare the canonical remote MCP server")
    serialized = json.dumps(value).lower()
    for forbidden in ("command", "access_token", "refresh_token", "device_token", "client_secret"):
        if forbidden in serialized:
            raise ValueError(f"{relative} contains forbidden field: {forbidden}")


def build(source_root: Path, output_dir: Path) -> tuple[Path, Path]:
    source_root = source_root.resolve()
    version = _plugin_version(source_root)
    for relative in FORBIDDEN_SOURCE_PATHS:
        if (source_root / relative).exists():
            raise ValueError(f"remote plugin contains forbidden local runtime path: {relative}")

    files: dict[str, bytes] = {}
    for relative in SOURCE_FILES:
        path = source_root / relative
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        if relative in {
            "mcp.json",
            ".mcp.json",
            "cursor.mcp.json",
            "codex/.mcp.json",
            "cowork/.mcp.json",
        }:
            _validate_remote_manifest(data, relative)
        files[relative] = data

    file_hashes = {name: _sha256(data) for name, data in sorted(files.items())}
    files["remote-manifest.json"] = _json_bytes(
        {
            "schema_version": 1,
            "plugin_id": PLUGIN_ID,
            "version": version,
            "transport": "streamable-http",
            "resource": REMOTE_RESOURCE,
            "runtime_required": False,
            "platform_independent": True,
            "credential_owner": "host_oauth_store",
            "files_sha256": file_hashes,
        }
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"de-audit-{version}.zip"
    with tempfile.NamedTemporaryFile(
        dir=output_dir, prefix=".de-audit-", suffix=".tmp", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as bundle:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo("de-audit/" + name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                bundle.writestr(info, data)
        os.replace(temporary_path, archive)
    finally:
        temporary_path.unlink(missing_ok=True)
    digest = output_dir / (archive.name + ".sha256")
    digest.write_text(f"{_sha256(archive.read_bytes())}  {archive.name}\n", encoding="ascii")
    return archive, digest


def build_cowork(source_root: Path, output_dir: Path) -> tuple[Path, Path]:
    """Build the Claude Cowork upload package with files at archive root."""

    source_root = source_root.resolve()
    version = _plugin_version(source_root)
    files: dict[str, bytes] = {}
    for source_relative, archive_relative in COWORK_SOURCE_FILES.items():
        path = source_root / source_relative
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        if source_relative == "cowork/.mcp.json":
            _validate_remote_manifest(data, source_relative)
        files[archive_relative] = data

    cowork_plugin = json.loads(files[".claude-plugin/plugin.json"])
    if cowork_plugin.get("version") != version:
        raise ValueError("Cowork plugin version does not match plugin.json")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"de-audit-cowork-{version}.zip"
    with tempfile.NamedTemporaryFile(
        dir=output_dir, prefix=".de-audit-cowork-", suffix=".tmp", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as bundle:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                bundle.writestr(info, data)
        os.replace(temporary_path, archive)
    finally:
        temporary_path.unlink(missing_ok=True)
    digest = output_dir / (archive.name + ".sha256")
    digest.write_text(f"{_sha256(archive.read_bytes())}  {archive.name}\n", encoding="ascii")
    return archive, digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    archive, digest = build(args.source_root, args.output_dir)
    cowork_archive, cowork_digest = build_cowork(args.source_root, args.output_dir)
    print(archive)
    print(digest)
    print(cowork_archive)
    print(cowork_digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
