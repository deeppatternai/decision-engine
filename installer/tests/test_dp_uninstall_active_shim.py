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

    def test_process_only_refresh_preserves_proven_development_root(self) -> None:
        inventory = self._inventory()
        development_root = Path(self.temp.name) / "candidate"
        inventory.de_refs = (*inventory.de_refs, development_root)
        original_inspect = self.module["Inventory"].inspect_processes
        captured: list[tuple[Path, ...]] = []

        def capture(candidate) -> None:
            captured.append(candidate.de_refs)

        self.module["Inventory"].inspect_processes = capture
        try:
            refreshed = self.module["process_only_inventory"](inventory)
        finally:
            self.module["Inventory"].inspect_processes = original_inspect

        self.assertIsNot(refreshed, inventory)
        self.assertEqual(captured, [inventory.de_refs])
        self.assertEqual(refreshed.de_refs, inventory.de_refs)

    def test_apply_with_only_process_blockers_returns_pending_without_mutation(self) -> None:
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
            return candidate, True

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

        self.assertEqual(result, self.module["EXIT_PENDING"])
        self.assertEqual(calls, ["resolve"])
        self.assertIn(
            "UNINSTALL_PENDING: fully quit every listed Agent host and rerun the uninstaller.",
            output.getvalue(),
        )

    def test_declining_verified_process_termination_returns_pending(self) -> None:
        current = self._inventory()
        current.processes = [
            {
                "pid": "102",
                "family": "mcp-launcher",
                "term_eligible": "true",
            }
        ]
        original_process_only = self.module["process_only_inventory"]
        original_prompt = self.module["prompt_tty"]
        original_terminate = self.module["terminate_verified_processes"]
        self.module["process_only_inventory"] = lambda _inventory: current
        self.module["prompt_tty"] = lambda _message: "n"
        self.module["terminate_verified_processes"] = lambda *_args: self.fail(
            "declining termination must not send TERM"
        )
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                refreshed, pending = self.module["resolve_process_blockers"](
                    self._inventory()
                )
        finally:
            self.module["process_only_inventory"] = original_process_only
            self.module["prompt_tty"] = original_prompt
            self.module["terminate_verified_processes"] = original_terminate

        self.assertIs(refreshed, current)
        self.assertTrue(pending)
        self.assertIn("process termination was declined", output.getvalue())

    def test_accepting_termination_passes_only_eligible_processes(self) -> None:
        current = self._inventory()
        eligible = {
            "pid": "102",
            "family": "mcp-launcher",
            "term_eligible": "true",
        }
        ineligible = {
            "pid": "103",
            "family": "unverified-de-launcher",
            "term_eligible": "false",
        }
        current.processes = [eligible, ineligible]
        refreshed = self._inventory()
        refreshed.processes = []
        captured: list[dict[str, str]] = []

        class RefreshedInventory:
            def __init__(self, _home: Path, _scope: str) -> None:
                pass

            def collect(self):
                return refreshed

        original_process_only = self.module["process_only_inventory"]
        original_prompt = self.module["prompt_tty"]
        original_pause = self.module["pause_verified_supervisors"]
        original_terminate = self.module["terminate_verified_processes"]
        original_inventory = self.module["Inventory"]
        original_sleep = self.module["time"].sleep
        initial = self._inventory()
        self.module["process_only_inventory"] = lambda _inventory: current
        self.module["prompt_tty"] = lambda message: (
            self.assertIn("[Y/N]", message) or "y"
        )
        paused = [{"pid": "201"}]
        self.module["pause_verified_supervisors"] = (
            lambda _inventory, items: paused if items == [eligible] else None
        )
        self.module["terminate_verified_processes"] = lambda _inventory, items: (
            captured.extend(items) or True
        )
        self.module["Inventory"] = RefreshedInventory
        self.module["time"].sleep = lambda _seconds: None
        try:
            result, pending = self.module["resolve_process_blockers"](
                initial
            )
        finally:
            self.module["process_only_inventory"] = original_process_only
            self.module["prompt_tty"] = original_prompt
            self.module["pause_verified_supervisors"] = original_pause
            self.module["terminate_verified_processes"] = original_terminate
            self.module["Inventory"] = original_inventory
            self.module["time"].sleep = original_sleep

        self.assertIs(result, refreshed)
        self.assertFalse(pending)
        self.assertEqual(captured, [eligible])
        self.assertEqual(result.paused_supervisors, paused)

    def test_chatgpt_codex_parent_is_reported_as_known_host(self) -> None:
        inventory = self._inventory()
        inventory.processes = [
            {
                "pid": "9778",
                "ppid": "8981",
                "family": "mcp-launcher",
                "host": "Codex (ChatGPT.app)",
                "executable": str(self.home / ".deeppattern/de-python/bin/python3"),
                "app_name": "ChatGPT.app",
                "app_path": "/Applications/ChatGPT.app",
                "parent_pid": "8981",
                "parent_executable": "/Applications/ChatGPT.app/Contents/Resources/codex",
            }
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.module["print_process_summary"](
                inventory, "ACTIVE AGENT SESSIONS:"
            )

        rendered = output.getvalue()
        self.assertIn("Codex (ChatGPT.app):", rendered)
        self.assertNotIn("Unknown Agent (host could not be identified)", rendered)

    def test_chatgpt_codex_executable_maps_to_codex_host(self) -> None:
        label = self.module["Inventory"].process_host_label(
            "/Applications/ChatGPT.app/Contents/Resources/codex"
        )
        self.assertEqual(label, "Codex (ChatGPT.app)")

    def test_unknown_verified_child_does_not_tell_user_to_quit_host(self) -> None:
        inventory = self._inventory()
        inventory.processes = [
            {
                "pid": "102",
                "ppid": "201",
                "family": "mcp-launcher",
                "host": "Unknown Agent",
                "term_eligible": "true",
                "executable": sys.executable,
            }
        ]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            self.module["print_process_summary"](
                inventory, "ACTIVE AGENT SESSIONS:"
            )

        rendered = output.getvalue()
        self.assertIn("can be stopped after confirmation", rendered)
        self.assertNotIn("Please quit the application", rendered)

    def test_term_target_is_revalidated_immediately_before_signal(self) -> None:
        candidates = [
            {
                "pid": "102",
                "family": "mcp-launcher",
                "term_eligible": "true",
            }
        ]
        validations = iter(((True, ""), (False, "identity changed")))
        original_revalidate = self.module["revalidate_term_target"]
        original_kill = self.module["os"].kill
        self.module["revalidate_term_target"] = lambda *_args: next(validations)
        self.module["os"].kill = lambda *_args: self.fail(
            "TERM must not be sent after the second identity check fails"
        )
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                result = self.module["terminate_verified_processes"](
                    self._inventory(), candidates
                )
        finally:
            self.module["revalidate_term_target"] = original_revalidate
            self.module["os"].kill = original_kill

        self.assertFalse(result)
        self.assertIn("changed before TERM: identity changed", output.getvalue())

    def test_verified_supervisors_are_paused_once_and_resumed_by_identity(self) -> None:
        inventory = self._inventory()
        candidates = [
            {
                "pid": "102",
                "ppid": "201",
                "family": "mcp-launcher",
                "term_eligible": "true",
            },
            {
                "pid": "103",
                "ppid": "201",
                "family": "mcp-launcher",
                "term_eligible": "true",
            },
        ]
        parent = {
            "pid": "201",
            "ppid": "1",
            "uid": str(os.getuid()),
            "start_time": "frozen-start",
            "command_line": "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness",
        }
        signals: list[tuple[int, int]] = []
        original_revalidate = self.module["revalidate_term_target"]
        original_read = self.module["read_process_identity"]
        original_kill = self.module["os"].kill
        self.module["revalidate_term_target"] = lambda *_args: (True, "")
        self.module["read_process_identity"] = lambda _inv, process: (
            dict(parent) if process.get("pid") == "201" else None
        )
        self.module["os"].kill = lambda pid, sig: signals.append((pid, sig))
        try:
            paused = self.module["pause_verified_supervisors"](
                inventory, candidates
            )
            self.assertEqual(paused, [parent])
            self.module["resume_paused_supervisors"](inventory, paused)
        finally:
            self.module["revalidate_term_target"] = original_revalidate
            self.module["read_process_identity"] = original_read
            self.module["os"].kill = original_kill

        self.assertEqual(
            signals,
            [(201, self.module["signal"].SIGSTOP), (201, self.module["signal"].SIGCONT)],
        )

    def test_main_resumes_paused_supervisor_when_apply_fails(self) -> None:
        initial = self._inventory()
        initial.processes = [
            {
                "pid": "102",
                "ppid": "201",
                "family": "mcp-launcher",
                "term_eligible": "true",
            }
        ]
        initial.process_blockers = ["process"]
        initial.blockers = ["process"]
        refreshed = self._inventory()
        refreshed.actions = [
            {"kind": "remove-root", "path": str(refreshed.de), "detail": "de"}
        ]
        refreshed.paused_supervisors = [{"pid": "201"}]
        resumed: list[dict[str, str]] = []

        class InitialInventory:
            def __init__(self, _home: Path, _scope: str) -> None:
                pass

            def collect(self):
                return initial

        originals = {
            name: self.module[name]
            for name in (
                "Inventory",
                "resolve_process_blockers",
                "apply_inventory",
                "resume_paused_supervisors",
            )
        }
        self.module["Inventory"] = InitialInventory
        self.module["resolve_process_blockers"] = lambda _inv: (refreshed, False)
        self.module["apply_inventory"] = lambda _inv: (_ for _ in ()).throw(
            RuntimeError("apply failed")
        )
        self.module["resume_paused_supervisors"] = (
            lambda _inv, supervisors: resumed.extend(supervisors)
        )
        try:
            result = self.module["main"](
                ["--scope", "de", "--apply", "--home", str(self.home)]
            )
        finally:
            self.module.update(originals)

        self.assertEqual(result, self.module["EXIT_BLOCKED"])
        self.assertEqual(resumed, [{"pid": "201"}])

    def test_absent_paths_are_not_reported_as_preserved(self) -> None:
        inventory = self._inventory()
        inventory.notes = [
            f"absent {inventory.de}",
            f"preserve protected source/worktree {inventory.dp / 'decision-engine-root'}",
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.module["print_inventory"](inventory, apply=True)

        rendered = output.getvalue()
        self.assertIn(f"ABSENT {inventory.de}", rendered)
        self.assertNotIn(f"PRESERVE absent {inventory.de}", rendered)
        self.assertIn("PRESERVE protected source/worktree", rendered)

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
