#!/usr/bin/env python3
"""Behavior locks for AQG release-named managed layouts."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[2] / "dp-install.sh"
UNINSTALLER = Path(__file__).resolve().parents[2] / "dp-uninstall.sh"
AQG_REMOTE = "https://github.com/deeppatternai/agent-quality-gates.git"


def shell_function(source: str, name: str) -> str:
    start = re.search(rf"(?m)^{re.escape(name)}\(\) \{{\n", source)
    if start is None:
        return ""
    next_function = re.search(
        r"(?m)^[A-Za-z_][A-Za-z0-9_]*\(\) \{\n", source[start.end() :]
    )
    end = len(source) if next_function is None else start.end() + next_function.start()
    return source[start.start() : end]


def load_embedded_uninstaller() -> dict[str, object]:
    source = UNINSTALLER.read_text(encoding="utf-8")
    embedded = source.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
    namespace: dict[str, object] = {"__name__": "dp_uninstall_embedded"}
    exec(compile(embedded, str(UNINSTALLER), "exec"), namespace)
    return namespace


class AqgManagedLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.uninstaller = load_embedded_uninstaller()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.dp = self.home / ".deeppattern"
        self.versions = self.dp / "versions"
        self.versions.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _managed_checkout(self, name: str, version: str) -> tuple[Path, Path]:
        target = self.versions / name
        (target / "scripts").mkdir(parents=True)
        for relative in (
            "AI_SETUP.md",
            "requirements.txt",
            "scripts/install_aqg_clients.py",
            "scripts/aqg_doctor.py",
        ):
            (target / relative).write_text("\n", encoding="utf-8")
        (target / "VERSION").write_text(version + "\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", AQG_REMOTE],
            check=True,
        )
        subprocess.run(["git", "-C", str(target), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-qm", "fixture"],
            check=True,
        )
        root = self.dp / "agent-quality-gates"
        root.symlink_to(Path("versions") / name)
        return root, target

    def _verify_installer(self, root: Path) -> subprocess.CompletedProcess[str]:
        source = INSTALLER.read_text(encoding="utf-8")
        harness = "".join(
            (
                "set -euo pipefail\n",
                shell_function(source, "clean_exec"),
                'fail() { printf "%s\\n" "$1" >&2; return 1; }\n',
                'AQG_ROOT="$1"\n',
                f'AQG_REPO="{AQG_REMOTE}"\n',
                'PYTHON_BIN="$2"\n',
                'GIT_BIN="$3"\n',
                'AQG_LAYOUT=""\nAQG_MANAGED_TARGET=""\n',
                shell_function(source, "verify_aqg_checkout"),
                "verify_aqg_checkout\n",
                'printf "%s\\t%s\\n" "$AQG_LAYOUT" "$AQG_MANAGED_TARGET"\n',
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", harness, "test", str(root), sys.executable, "git"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def test_release_named_layout_is_accepted_by_install_and_uninstall(self) -> None:
        root, target = self._managed_checkout("0.14.14", "0.14.14")
        install = self._verify_installer(root)
        self.assertEqual(install.returncode, 0, install.stdout)
        canonical_target = target.resolve()
        self.assertIn("managed\t" + str(canonical_target), install.stdout)

        inventory = self.uninstaller["Inventory"](self.home, "aqg")
        uninstall_target = inventory.proven_managed_aqg_target(root)
        self.assertIsNotNone(uninstall_target)
        self.assertTrue(os.path.samefile(uninstall_target, canonical_target))

    def test_release_named_layout_rejects_version_mismatch(self) -> None:
        root, _target = self._managed_checkout("0.14.14", "0.14.13")
        install = self._verify_installer(root)
        self.assertNotEqual(install.returncode, 0, install.stdout)

        inventory = self.uninstaller["Inventory"](self.home, "aqg")
        self.assertIsNone(inventory.proven_managed_aqg_target(root))

    def _rename_target(self, root: Path, target: Path, name: str) -> Path:
        root.unlink()
        renamed = target.with_name(name)
        target.rename(renamed)
        root.symlink_to(Path('versions') / name)
        return renamed

    def test_official_reissue_and_retry_names_are_accepted(self) -> None:
        root, target = self._managed_checkout('0.14.14', '0.14.14')
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        for name in (head, '0.14.14-' + head[:12],
                     '0.14.14-0123456789abcdef',
                     '0.14.14-' + head[:12] + '-0123456789abcdef'):
            with self.subTest(name=name):
                target = self._rename_target(root, target, name)
                result = self._verify_installer(root)
                self.assertEqual(result.returncode, 0, result.stdout)
                inventory = self.uninstaller['Inventory'](self.home, 'aqg')
                self.assertIsNotNone(inventory.proven_managed_aqg_target(root))

    def test_unrelated_suffix_or_commit_is_rejected(self) -> None:
        root, target = self._managed_checkout('0.14.14', '0.14.14')
        for name in ('0.14.14-unknown', '0.14.14-deadbeef0000',
                     '0.14.13-0123456789abcdef', 'a' * 40):
            with self.subTest(name=name):
                target = self._rename_target(root, target, name)
                self.assertNotEqual(self._verify_installer(root).returncode, 0)
                inventory = self.uninstaller['Inventory'](self.home, 'aqg')
                self.assertIsNone(inventory.proven_managed_aqg_target(root))


if __name__ == "__main__":
    unittest.main()
