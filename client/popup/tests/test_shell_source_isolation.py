"""Regression coverage for detached popup source-tree isolation."""

from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

from client.popup import launcher


class ShellSourceIsolationTestCase(unittest.TestCase):
    def test_shell_command_pins_launchers_source_tree_before_module_load(self):
        cmd = launcher.build_shell_command(
            sys.executable,
            "popup.html",
            "Popup title",
            "result.json",
            chat_context_path="context.json",
            on_close_dismiss=True,
        )
        source_root = str(Path(launcher.__file__).resolve().parents[2])

        self.assertEqual(cmd[:2], [sys.executable, "-c"])
        self.assertIn("sys.path.insert(0,", cmd[2])
        self.assertIn(repr(source_root), cmd[2])
        self.assertIn("runpy.run_module", cmd[2])
        self.assertIn(repr(launcher.NATIVE_SHELL_MODULE), cmd[2])
        self.assertIn("alter_sys=True", cmd[2])
        self.assertEqual(
            cmd[3:],
            [
                "--html",
                "popup.html",
                "--title",
                "Popup title",
                "--result-path",
                "result.json",
                "--context",
                "context.json",
                "--on-close-dismiss",
            ],
        )

    def test_shell_bootstrap_executes_pinned_module_over_shadow_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            shadow = Path(root, "client", "popup")
            shadow.mkdir(parents=True)
            Path(root, "client", "__init__.py").write_text("", encoding="utf-8")
            Path(shadow, "__init__.py").write_text("", encoding="utf-8")
            Path(shadow, "native_shell.py").write_text(
                "raise SystemExit(73)\n", encoding="utf-8"
            )
            cmd = launcher.build_shell_command(
                sys.executable, "unused.html", "Shadow test", "unused.json"
            )
            completed = subprocess.run(
                [*cmd, "--self-check"],
                cwd=root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("native_shell self-check: ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
