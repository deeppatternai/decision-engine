#!/usr/bin/env python3
"""Behavior locks for managed shim identity checks in dp-uninstall.sh."""

from __future__ import annotations

import contextlib
import io
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "dp-uninstall.sh"


def load_embedded_uninstaller() -> dict[str, object]:
    source = SCRIPT.read_text(encoding="utf-8")
    embedded = source.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
    namespace: dict[str, object] = {"__name__": "dp_uninstall_embedded"}
    exec(compile(embedded, str(SCRIPT), "exec"), namespace)
    return namespace


class ManagedShimIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_embedded_uninstaller()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.root = self.home / ".deeppattern" / "decision-engine"
        self._write_launcher(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _write_launcher(root: Path) -> None:
        package = root / "installer"
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

    def _spawn(
        self,
        *,
        host: str,
        python_root: Path | None = None,
        extra_arg: bool = False,
    ) -> subprocess.Popen[str]:
        root = python_root or self.root
        if python_root is not None and python_root != self.root:
            self._write_launcher(python_root)
        ready_read, ready_write = os.pipe()
        env = {
            **os.environ,
            "PYTHONPATH": str(root),
            "DE_MCP_CLIENT_HOST": host,
            "READY_FD": str(ready_write),
        }
        command = [sys.executable, "-m", "installer.launcher"]
        if extra_arg:
            command.append("unexpected")
        process = subprocess.Popen(
            command,
            cwd="/",
            env=env,
            text=True,
            pass_fds=(ready_write,),
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

    @staticmethod
    def _command(process: subprocess.Popen[str]) -> str:
        result = subprocess.run(
            ["/bin/ps", "-p", str(process.pid), "-o", "command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()

    def _inventory(self):
        return self.module["Inventory"](self.home, "de")

    def test_accepts_and_revalidates_managed_launcher_when_host_drops_cwd(self) -> None:
        process = self._spawn(host="claude-desktop-3p")
        inventory = self._inventory()
        command = self._command(process)
        host = inventory.managed_launcher_environment_host(str(process.pid), command)
        self.assertEqual(host, "claude-desktop-3p")

        identity = self.module["read_process_identity"](
            inventory, {"pid": str(process.pid)}
        )
        self.assertIsNotNone(identity)
        candidate = {
            **identity,
            "proof": "environment",
            "environment_host": host,
            "term_eligible": "true",
        }
        valid, reason = self.module["revalidate_term_target"](
            inventory, candidate
        )
        self.assertTrue(valid, reason)

    def test_process_identity_forces_c_locale_for_stable_lstart_parsing(self) -> None:
        fake_ps = Path(self.temp.name) / "ps"
        fake_ps.write_text(
            "#!/bin/sh\n"
            "test \"$LC_ALL\" = C || exit 7\n"
            "printf '%s\\n' '123 45 501 Thu Sep 10 13:26:37 2026 "
            "/usr/bin/python3 -m installer.launcher'\n",
            encoding="utf-8",
        )
        fake_ps.chmod(0o700)
        original = self.module["trusted_system_tool"]
        self.module["trusted_system_tool"] = lambda *_candidates: str(fake_ps)
        try:
            identity = self.module["read_process_identity"](
                self._inventory(), {"pid": "123"}
            )
        finally:
            self.module["trusted_system_tool"] = original

        self.assertIsNotNone(identity)
        self.assertEqual(identity["start_time"], "Thu Sep 10 13:26:37 2026")

    def test_inventory_marks_environment_proven_launcher_term_eligible(self) -> None:
        process = self._spawn(host="claude-desktop-3p")
        command = self._command(process)
        identity = subprocess.run(
            ["/bin/ps", "-p", str(process.pid), "-o", "ppid="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        fake_ps = Path(self.temp.name) / "ps"
        fake_ps.write_text(
            "#!/bin/sh\n"
            + "printf '%s\\n' "
            + repr(f"{process.pid} {identity.stdout.strip()} {command}")
            + "\n",
            encoding="utf-8",
        )
        fake_ps.chmod(0o700)
        fake_lsof = Path(self.temp.name) / "lsof"
        fake_lsof.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'n/private/tmp/foreign-launcher'\n",
            encoding="utf-8",
        )
        fake_lsof.chmod(0o700)
        previous_ps = os.environ.get("DE_AQG_UNINSTALL_PROCESS_COMMAND")
        previous_lsof = os.environ.get("DE_AQG_UNINSTALL_PROCESS_FILES_COMMAND")
        os.environ["DE_AQG_UNINSTALL_PROCESS_COMMAND"] = str(fake_ps)
        os.environ["DE_AQG_UNINSTALL_PROCESS_FILES_COMMAND"] = str(fake_lsof)
        try:
            inventory = self._inventory()
            inventory.inspect_processes()
        finally:
            if previous_ps is None:
                os.environ.pop("DE_AQG_UNINSTALL_PROCESS_COMMAND", None)
            else:
                os.environ["DE_AQG_UNINSTALL_PROCESS_COMMAND"] = previous_ps
            if previous_lsof is None:
                os.environ.pop("DE_AQG_UNINSTALL_PROCESS_FILES_COMMAND", None)
            else:
                os.environ["DE_AQG_UNINSTALL_PROCESS_FILES_COMMAND"] = previous_lsof
        self.assertEqual(len(inventory.processes), 1)
        candidate = inventory.processes[0]
        self.assertEqual(candidate["proof"], "environment")
        self.assertEqual(candidate["host"], "Claude Desktop 3p")
        self.assertEqual(candidate["term_eligible"], "true")
        valid, reason = self.module["revalidate_term_target"](
            inventory, candidate
        )
        self.assertTrue(valid, reason)

    def test_exact_claude_supervisor_is_not_a_separate_term_target(self) -> None:
        child = {
            "pid": "102",
            "ppid": "101",
            "family": "mcp-launcher",
            "host": "Claude Desktop 3p",
            "command_line": f"{sys.executable} -m installer.launcher",
            "proof": "environment",
            "environment_host": "claude-desktop-3p",
            "term_eligible": "true",
        }
        wrapper = {
            "pid": "101",
            "ppid": "100",
            "family": "unverified-de-launcher",
            "host": "Claude",
            "command_line": (
                "/Applications/Claude.app/Contents/Helpers/disclaimer "
                f"--pgroup -- {child['command_line']}"
            ),
            "proof": "unknown",
            "term_eligible": "false",
        }
        self.assertTrue(
            self.module["Inventory"].is_verified_claude_launcher_supervisor(
                wrapper, [child]
            )
        )

    def test_claude_supervisor_rejects_an_unverified_child(self) -> None:
        child = {
            "pid": "102",
            "ppid": "101",
            "command_line": f"{sys.executable} -m installer.launcher",
            "proof": "files",
            "environment_host": "claude-desktop-3p",
            "term_eligible": "true",
        }
        wrapper = {
            "pid": "101",
            "host": "Claude",
            "command_line": (
                "/Applications/Claude.app/Contents/Helpers/disclaimer "
                f"--pgroup -- {child['command_line']}"
            ),
        }
        self.assertFalse(
            self.module["Inventory"].is_verified_claude_launcher_supervisor(
                wrapper, [child]
            )
        )

    def test_process_only_gate_is_order_independent(self) -> None:
        inventory = self._inventory()
        inventory.blockers = ["process b", "process a"]
        inventory.process_blockers = ["process a", "process b"]
        self.assertTrue(inventory.has_only_process_blockers())

    def test_apply_with_only_process_blockers_enters_guided_resolution(self) -> None:
        inventory = self._inventory()
        inventory.processes = [
            {
                "pid": "102",
                "ppid": "101",
                "family": "mcp-launcher",
                "host": "Claude Desktop 3p",
                "term_eligible": "true",
            }
        ]
        inventory.process_blockers = [
            "live mcp-launcher process must be stopped before uninstall: pid=102"
        ]
        inventory.blockers = list(inventory.process_blockers)
        inventory.collect = lambda: inventory
        calls: list[str] = []

        def inventory_factory(_home: Path, _scope: str):
            return inventory

        def resolver(candidate):
            calls.append("resolve")
            return candidate

        def unexpected_apply(_candidate):
            self.fail("apply_inventory must not run while a process blocker remains")

        originals = {
            name: self.module[name]
            for name in ("Inventory", "resolve_process_blockers", "apply_inventory")
        }
        self.module["Inventory"] = inventory_factory
        self.module["resolve_process_blockers"] = resolver
        self.module["apply_inventory"] = unexpected_apply
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                result = self.module["main"](
                    ["--scope", "both", "--apply", "--home", str(self.home)]
                )
        finally:
            self.module.update(originals)

        self.assertEqual(result, self.module["EXIT_BLOCKED"])
        self.assertEqual(calls, ["resolve"])
        self.assertIn(
            "ACTION REQUIRED: close the listed Agent hosts; guided process cleanup follows.",
            output.getvalue(),
        )

    def test_rejects_launcher_with_wrong_managed_root(self) -> None:
        process = self._spawn(
            host="claude-desktop-3p", python_root=self.root.parent / "other-root"
        )
        host = self._inventory().managed_launcher_environment_host(
            str(process.pid), self._command(process)
        )
        self.assertIsNone(host)

    def test_rejects_launcher_with_unknown_host_marker(self) -> None:
        process = self._spawn(host="unknown-agent")
        host = self._inventory().managed_launcher_environment_host(
            str(process.pid), self._command(process)
        )
        self.assertIsNone(host)

    def test_rejects_launcher_with_extra_argument(self) -> None:
        process = self._spawn(host="claude-desktop-3p", extra_arg=True)
        host = self._inventory().managed_launcher_environment_host(
            str(process.pid), self._command(process)
        )
        self.assertIsNone(host)


if __name__ == "__main__":
    unittest.main()
