#!/usr/bin/env python3
"""Build deterministic platform-independent de-visual plugin archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile


PLUGIN_ID = "visual@deeppattern"
REMOTE_RESOURCE = "https://endpoint.deeppattern.ai/plugin/mcp/v1/visual"
DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
    ".mcp.json",
    "LICENSE.md",
    "README.md",
    "after-install.md",
    "codex/.codex-plugin/plugin.json",
    "codex/.mcp.json",
    "codex/LICENSE.md",
    "codex/skills/setup/SKILL.md",
    "codex/skills/visual/SKILL.md",
    "contracts/visual-endpoint-contract.json",
    "cowork/.claude-plugin/plugin.json",
    "cowork/.mcp.json",
    "cowork/LICENSE.md",
    "cowork/skills/setup/SKILL.md",
    "cowork/skills/visual/SKILL.md",
    "cursor.mcp.json",
    "fixtures/comic.json",
    "fixtures/diagram.json",
    "fixtures/infographic.json",
    "mcp.json",
    "plugin.json",
    "skills/setup/SKILL.md",
    "skills/visual/SKILL.md",
)
COWORK_FILES = {
    "cowork/LICENSE.md": "LICENSE.md",
    "cowork/.claude-plugin/plugin.json": ".claude-plugin/plugin.json",
    "cowork/.mcp.json": ".mcp.json",
    "cowork/skills/setup/SKILL.md": "skills/setup/SKILL.md",
    "cowork/skills/visual/SKILL.md": "skills/visual/SKILL.md",
}
FORBIDDEN = ("runtime", "launcher.py", "activation_ui.py", "client_secret")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version(root: Path) -> str:
    value = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    version = value.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("plugin version missing")
    return version


VERSION = _version(DEFAULT_SOURCE_ROOT)


def _validate_manifest(data: bytes, relative: str) -> None:
    value = json.loads(data)
    server = value["mcpServers"]["visual"]
    if server.get("type") != "http" or server.get("url") != REMOTE_RESOURCE:
        raise ValueError(f"{relative} does not use the Visual resource")
    serialized = json.dumps(server).lower()
    for forbidden in ("command", "access_token", "refresh_token", "device_token", "client_secret"):
        if forbidden in serialized:
            raise ValueError(f"{relative} contains forbidden field {forbidden}")


def _write_archive(path: Path, prefix: str, files: dict[str, bytes]) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".de-visual-", suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(prefix + name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                bundle.writestr(info, data)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build(source_root: Path, output_dir: Path) -> tuple[Path, Path]:
    source_root = source_root.resolve()
    for relative in FORBIDDEN:
        if (source_root / relative).exists():
            raise ValueError(f"forbidden runtime path: {relative}")
    files: dict[str, bytes] = {}
    for relative in SOURCE_FILES:
        path = source_root / relative
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        if relative.endswith(("mcp.json", ".mcp.json")):
            _validate_manifest(data, relative)
        files[relative] = data
    files["remote-manifest.json"] = (
        json.dumps(
            {
                "schema_version": 1,
                "plugin_id": PLUGIN_ID,
                "version": _version(source_root),
                "resource": REMOTE_RESOURCE,
                "runtime_required": False,
                "platform_independent": True,
                "credential_owner": "host_oauth_store",
                "files_sha256": {name: _digest(data) for name, data in sorted(files.items())},
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"de-visual-{_version(source_root)}.zip"
    _write_archive(archive, "de-visual/", files)
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(f"{_digest(archive.read_bytes())}  {archive.name}\n", encoding="ascii")
    return archive, checksum


def build_cowork(source_root: Path, output_dir: Path) -> tuple[Path, Path]:
    source_root = source_root.resolve()
    files = {}
    for source, target in COWORK_FILES.items():
        data = (source_root / source).read_bytes()
        if source.endswith(".mcp.json"):
            _validate_manifest(data, source)
        files[target] = data
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"de-visual-cowork-{_version(source_root)}.zip"
    _write_archive(archive, "", files)
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(f"{_digest(archive.read_bytes())}  {archive.name}\n", encoding="ascii")
    return archive, checksum


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    for item in (*build(args.source_root, args.output_dir), *build_cowork(args.source_root, args.output_dir)):
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
