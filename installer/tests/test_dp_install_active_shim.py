#!/usr/bin/env python3
"""Behavior locks for managed shim identity checks in dp-install.sh."""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "dp-install.sh"


def shell_function(source: str, name: str) -> str:
    start = re.search(rf"(?m)^{re.escape(name)}\(\) \{{\n", source)
    if start is None:
        return ""
    next_function = re.search(
        r"(?m)^[A-Za-z_][A-Za-z0-9_]*\(\) \{\n", source[start.end() :]
    )
    end = len(source) if next_function is None else start.end() + next_function.start()
    return source[start.start() : end]


class ManagedShimIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "decision engine"
        package = self.root / "installer"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "launcher.py").write_text(
            "import os\n"
            "import signal\n"
            "ready_fd = int(os.environ['READY_FD'])\n"
            "os.write(ready_fd, b'1')\n"
            "os.close(ready_fd)\n"
            "signal.pause()\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _spawn(
        self, *, host: str, launcher: bool = True, extra_arg: bool = False
    ) -> subprocess.Popen[str]:
        ready_read, ready_write = os.pipe()
        env = {
            **os.environ,
            "PYTHONPATH": str(self.root),
            "DE_MCP_CLIENT_HOST": host,
            "READY_FD": str(ready_write),
        }
        if launcher:
            command = [sys.executable, "-m", "installer.launcher"]
            if extra_arg:
                command.append("PYTHONPATH=" + str(self.root))
        else:
            command = [
                sys.executable,
                "-c",
                "import os, signal; ready_fd=int(os.environ['READY_FD']); "
                "os.write(ready_fd, b'1'); os.close(ready_fd); signal.pause()",
            ]
        process = subprocess.Popen(
            command, cwd="/", env=env, text=True, pass_fds=(ready_write,)
        )
        os.close(ready_write)
        ready, _, _ = select.select([ready_read], [], [], 3)
        self.assertTrue(ready, "child process did not signal readiness")
        self.assertEqual(os.read(ready_read, 1), b"1")
        os.close(ready_read)
        self.assertIsNone(process.poll())
        self.addCleanup(self._stop, process)
        return process

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def _verify(self, process: subprocess.Popen[str], managed_root: Path) -> subprocess.CompletedProcess[str]:
        source = SCRIPT.read_text(encoding="utf-8")
        names = (
            "clean_exec",
            "process_snapshot",
            "managed_shim_has_root_ref",
            "managed_shim_has_root_env",
            "verified_managed_shim_snapshot",
        )
        functions = []
        for name in names:
            function = shell_function(source, name)
            if function:
                functions.append(function)
            elif name == "managed_shim_has_root_env":
                functions.append("managed_shim_has_root_env() { return 1; }\n")
            else:
                self.fail(f"missing shell function: {name}")
        harness = "".join(
            (
                "set -euo pipefail\n",
                'MANAGED_ROOT="$1"\n',
                'PYTHON_BIN="$2"\n',
                *functions,
                'verified_managed_shim_snapshot "$3" >/dev/null\n',
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", harness, "test", str(managed_root), sys.executable, str(process.pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def test_accepts_managed_launcher_when_host_drops_cwd(self) -> None:
        process = self._spawn(host="claude-desktop-3p")
        result = self._verify(process, self.root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "")

    def test_rejects_launcher_with_wrong_managed_root(self) -> None:
        process = self._spawn(host="claude-desktop-3p")
        result = self._verify(process, self.root.parent / "other-root")
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rejects_launcher_with_unknown_host_marker(self) -> None:
        process = self._spawn(host="unknown-agent")
        result = self._verify(process, self.root)
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rejects_non_launcher_command(self) -> None:
        process = self._spawn(host="claude-desktop-3p", launcher=False)
        result = self._verify(process, self.root)
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rejects_launcher_with_extra_argument(self) -> None:
        process = self._spawn(
            host="claude-desktop-3p", launcher=True, extra_arg=True
        )
        result = self._verify(process, self.root)
        self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
