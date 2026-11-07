from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
HARNESS_ROOT = ROOT / "deepseek-harness"
sys.path.insert(0, str(ROOT / "packaging"))

from build_harness_package import build  # noqa: E402


class HarnessPackageTests(unittest.TestCase):
    def test_install_documentation_requires_a_full_restart_for_client_module(self):
        readme = (HARNESS_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("fully quit every Harness window", readme)
        self.assertIn("reopen Harness", readme)
        self.assertIn("reload alone does not reliably activate", readme)

    def test_client_module_is_materialized_during_web_boot(self):
        package = json.loads((HARNESS_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertIs(package["dsh"]["client"]["immediately"], True)

    def test_build_is_deterministic_runtime_free_and_rooted_as_npm_package(self):
        version = json.loads((HARNESS_ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        with tempfile.TemporaryDirectory() as temporary:
            first, first_digest = build(HARNESS_ROOT, Path(temporary) / "first")
            second, second_digest = build(HARNESS_ROOT, Path(temporary) / "second")
            self.assertEqual(first.name, f"de-visual-harness-{version}.tgz")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                first_digest.read_text(encoding="ascii").split()[0],
            )
            self.assertEqual(first_digest.read_text(), second_digest.read_text())
            with tarfile.open(first, "r:gz") as bundle:
                names = set(bundle.getnames())
                self.assertIn("package/package.json", names)
                self.assertEqual(
                    bundle.extractfile("package/LICENSE.md").read().decode("utf-8").splitlines(),
                    (REPO / "LICENSE.md").read_text(encoding="utf-8").splitlines(),
                )
                self.assertIn("package/cordis.patch.yml", names)
                self.assertIn("package/dist/index.js", names)
                self.assertIn("package/dist/client.js", names)
                self.assertIn("package/dist/client.d.ts", names)
                self.assertIn("package/skills/visual/SKILL.md", names)
                self.assertFalse(any("/acceptance/" in name or "/compatibility/" in name for name in names))
                self.assertFalse(any("node_modules" in name for name in names))
                self.assertFalse(
                    any(
                        part == ".DS_Store" or part == "__MACOSX" or part.startswith("._")
                        for name in names
                        for part in Path(name).parts
                    )
                )
                self.assertFalse(
                    any(name.endswith((".py", ".exe", ".dll", ".dylib", ".so", ".node")) for name in names)
                )


if __name__ == "__main__":
    unittest.main()
