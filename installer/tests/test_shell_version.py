"""Drift guard: every version marker in the published shell agrees with the VERSION file (the SoT).

Publish-repo-only (the source repo has no VERSION file / README badge), and kept in its OWN file so a
source→published sync of installer/tests never clobbers it. If this fails, run
`python3 installer/set_version.py` to re-sync all markers from VERSION.
"""
from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ShellVersionDriftTest(unittest.TestCase):
    def setUp(self):
        self.v = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    def test_client_version_py_matches_version_file(self):
        text = (ROOT / "client" / "version.py").read_text(encoding="utf-8")
        self.assertIn('CLIENT_VERSION = "%s"' % self.v, text,
                      "client/version.py CLIENT_VERSION != VERSION (%s) — run installer/set_version.py" % self.v)

    def test_pyproject_matches_version_file(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["version"], self.v)

    def test_pyproject_requires_python_312_or_later(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["requires-python"], ">=3.12")

    def test_config_example_matches_version_file(self):
        data = json.loads((ROOT / "installer" / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(data["client_version"], self.v)

    def test_readme_badges_match_version_file(self):
        for readme in ("README.md", "README.zh-CN.md"):
            with self.subTest(readme=readme):
                text = (ROOT / readme).read_text(encoding="utf-8")
                m = re.search(r"\*\*v(\d+\.\d+\.\d+)\*\*", text)
                self.assertIsNotNone(m, "%s has no **vX.Y.Z** version badge" % readme)
                self.assertEqual(m.group(1), self.v,
                                 "%s badge out of sync with VERSION (%s) — run installer/set_version.py"
                                 % (readme, self.v))


if __name__ == "__main__":
    unittest.main()
