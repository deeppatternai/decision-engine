from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
import sys
sys.path.insert(0, str(ROOT / "packaging"))
from build_package import SOURCE_FILES, VERSION, build, build_cowork  # noqa: E402


class VisualPackagingTests(unittest.TestCase):
    def test_builds_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, digest = build(ROOT, Path(temporary) / "one")
            second, _ = build(ROOT, Path(temporary) / "two")
            self.assertEqual(first.name, f"de-visual-{VERSION}.zip")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest.read_text().split()[0])
            with zipfile.ZipFile(first) as bundle:
                names = set(bundle.namelist())
                self.assertIn("de-visual/remote-manifest.json", names)
                self.assertEqual(
                    bundle.read("de-visual/LICENSE.md").decode("utf-8").splitlines(),
                    (REPO / "LICENSE.md").read_text(encoding="utf-8").splitlines(),
                )
                for relative in SOURCE_FILES:
                    self.assertIn("de-visual/" + relative, names)
                self.assertIn("de-visual/codex/LICENSE.md", names)
                self.assertIn("de-visual/cowork/LICENSE.md", names)
                self.assertFalse(any("/acceptance/" in name or "/compatibility/" in name for name in names))
                manifest = json.loads(bundle.read("de-visual/remote-manifest.json"))
                self.assertFalse(manifest["runtime_required"])

    def test_cowork_archive_has_root_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive, _ = build_cowork(ROOT, Path(temporary))
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(bundle.namelist()), {".claude-plugin/plugin.json", ".mcp.json", "LICENSE.md", "skills/setup/SKILL.md", "skills/visual/SKILL.md"})
                self.assertEqual(
                    bundle.read("LICENSE.md").decode("utf-8").splitlines(),
                    (REPO / "LICENSE.md").read_text(encoding="utf-8").splitlines(),
                )


if __name__ == "__main__":
    unittest.main()
