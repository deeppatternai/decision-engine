from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
import sys

sys.path.insert(0, str(ROOT / "packaging"))
from build_package import (  # noqa: E402
    OPENAI_OAUTH_EXTENSION,
    REMOTE_RESOURCE,
    SOURCE_FILES,
    VERSION,
    build,
    build_cowork,
)


class RemotePackageTests(unittest.TestCase):
    def _copy_source(self, destination: Path) -> None:
        destination.mkdir()
        for relative in SOURCE_FILES:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)

    def test_build_is_deterministic_and_runtime_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, first_digest = build(ROOT, Path(temporary) / "first")
            second, second_digest = build(ROOT, Path(temporary) / "second")
            self.assertEqual(first.name, f"de-audit-{VERSION}.zip")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                first_digest.read_text(encoding="ascii").split()[0],
            )
            self.assertEqual(
                first_digest.read_text(encoding="ascii"),
                second_digest.read_text(encoding="ascii"),
            )
            with zipfile.ZipFile(first) as bundle:
                names = set(bundle.namelist())
                self.assertIn("de-audit/remote-manifest.json", names)
                self.assertEqual(
                    bundle.read("de-audit/LICENSE.md").decode("utf-8").splitlines(),
                    (REPO / "LICENSE.md").read_text(encoding="utf-8").splitlines(),
                )
                for relative in SOURCE_FILES:
                    self.assertIn("de-audit/" + relative, names)
                self.assertIn("de-audit/codex/LICENSE.md", names)
                self.assertIn("de-audit/cowork/LICENSE.md", names)
                self.assertFalse(any("/acceptance/" in name or "/compatibility/" in name for name in names))
                self.assertFalse(any("/runtime/" in name for name in names))
                self.assertFalse(any(name.endswith((".py", ".exe", ".dll", ".dylib", ".so")) for name in names))
                manifest = json.loads(bundle.read("de-audit/remote-manifest.json"))
                self.assertEqual(manifest["resource"], REMOTE_RESOURCE)
                self.assertFalse(manifest["runtime_required"])
                self.assertTrue(manifest["platform_independent"])

    def test_builder_preserves_public_oauth_metadata_without_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive, _digest = build(ROOT, Path(temporary) / "out")
            with zipfile.ZipFile(archive) as bundle:
                value = json.loads(bundle.read("de-audit/codex/.mcp.json"))
                server = value["mcpServers"]["audit"]
                self.assertEqual(server["extensions"], OPENAI_OAUTH_EXTENSION)
                serialized = json.dumps(server).lower()
                self.assertNotIn("access_token", serialized)
                self.assertNotIn("refresh_token", serialized)
                self.assertNotIn("client_secret", serialized)
                cowork = json.loads(bundle.read("de-audit/cowork/.mcp.json"))
                self.assertEqual(
                    cowork["mcpServers"]["audit"],
                    {
                        "type": "http",
                        "url": REMOTE_RESOURCE,
                        "headers": {
                            "X-DeepPattern-MCP-Auth-Mode": "transport",
                        },
                    },
                )

    def test_builds_deterministic_cowork_upload_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, first_digest = build_cowork(ROOT, Path(temporary) / "first")
            second, second_digest = build_cowork(ROOT, Path(temporary) / "second")
            self.assertEqual(first.name, f"de-audit-cowork-{VERSION}.zip")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                first_digest.read_text(encoding="ascii").split()[0],
            )
            self.assertEqual(
                first_digest.read_text(encoding="ascii"),
                second_digest.read_text(encoding="ascii"),
            )
            with zipfile.ZipFile(first) as bundle:
                self.assertEqual(
                    set(bundle.namelist()),
                    {
                        ".claude-plugin/plugin.json",
                        ".mcp.json",
                        "LICENSE.md",
                        "skills/audit/SKILL.md",
                        "skills/setup/SKILL.md",
                    },
                )
                self.assertEqual(
                    bundle.read("LICENSE.md").decode("utf-8").splitlines(),
                    (REPO / "LICENSE.md").read_text(encoding="utf-8").splitlines(),
                )
                server = json.loads(bundle.read(".mcp.json"))["mcpServers"]["audit"]
                self.assertEqual(
                    server,
                    {
                        "type": "http",
                        "url": REMOTE_RESOURCE,
                        "headers": {
                            "X-DeepPattern-MCP-Auth-Mode": "transport",
                        },
                    },
                )
                plugin = json.loads(bundle.read(".claude-plugin/plugin.json"))
                self.assertEqual(plugin["version"], VERSION)

    def test_builder_rejects_runtime_reintroduction(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            self._copy_source(source)
            (source / "runtime").mkdir()
            with self.assertRaisesRegex(ValueError, "forbidden local runtime"):
                build(source, Path(temporary) / "out")

    def test_builder_rejects_noncanonical_claude_headers(self):
        for headers in (
            {"X-DeepPattern-MCP-Auth-Mode": "transport", "X-Other": "value"},
            {"Authorization": "Bearer credential"},
            {"X-DeepPattern-MCP-Auth-Mode": "transport", "X-Api-Key": "credential"},
        ):
            with self.subTest(headers=headers), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "source"
                self._copy_source(source)
                manifest_path = source / ".mcp.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["mcpServers"]["audit"]["headers"] = headers
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "canonical remote MCP server"):
                    build(source, Path(temporary) / "out")


if __name__ == "__main__":
    unittest.main()
