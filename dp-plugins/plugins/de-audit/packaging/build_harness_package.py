#!/usr/bin/env python3
"""Build the deterministic DeepSeek Harness de-audit npm tarball."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path


DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "deepseek-harness"
TOP_LEVEL_FILES = ("package.json", "cordis.patch.yml", "README.md", "LICENSE.md")
TREE_FILES = ("dist", "skills")
FORBIDDEN_SUFFIXES = (".py", ".exe", ".dll", ".dylib", ".so", ".node")


def _is_platform_metadata(path: Path, root: Path) -> bool:
    return any(
        part == ".DS_Store" or part == "__MACOSX" or part.startswith("._")
        for part in path.relative_to(root).parts
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _package_version(source_root: Path) -> str:
    value = json.loads((source_root / "package.json").read_text(encoding="utf-8"))
    version = value.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("package.json does not declare a non-empty version")
    return version


def _source_files(source_root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for relative in TOP_LEVEL_FILES:
        path = source_root / relative
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(path)
        files[f"package/{relative}"] = path.read_bytes()
    for tree in TREE_FILES:
        root = source_root / tree
        if not root.is_dir() or root.is_symlink():
            raise FileNotFoundError(root)
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or _is_platform_metadata(path, root):
                continue
            relative = path.relative_to(source_root).as_posix()
            files[f"package/{relative}"] = path.read_bytes()
    if not any(name.startswith("package/dist/") and name.endswith(".js") for name in files):
        raise ValueError("Harness package has no compiled JavaScript")
    for name, data in files.items():
        if name.lower().endswith(FORBIDDEN_SUFFIXES):
            raise ValueError(f"Harness package contains a forbidden runtime file: {name}")
        lowered = data.lower()
        for secret_name in (b"access_token", b"refresh_token", b"client_secret", b"device_token"):
            if secret_name in lowered and not name.endswith(("README.md", "SKILL.md", ".d.ts", ".js")):
                raise ValueError(f"Harness package contains a forbidden credential field: {name}")
    return files


def build(source_root: Path, output_dir: Path) -> tuple[Path, Path]:
    source_root = source_root.resolve()
    version = _package_version(source_root)
    files = _source_files(source_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"de-audit-harness-{version}.tgz"

    with tempfile.NamedTemporaryFile(dir=output_dir, prefix=".harness-", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with temporary_path.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as zipped:
                with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as bundle:
                    for name, data in sorted(files.items()):
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mode = 0o644
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        bundle.addfile(info, io.BytesIO(data))
        os.replace(temporary_path, archive)
    finally:
        temporary_path.unlink(missing_ok=True)

    digest = output_dir / f"{archive.name}.sha256"
    digest.write_text(f"{_sha256(archive.read_bytes())}  {archive.name}\n", encoding="ascii")
    return archive, digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    archive, digest = build(args.source_root, args.output_dir)
    print(archive)
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
