"""Behavior tests for the macOS DE Lite host Stopper LaunchAgent."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
import types
import unittest
from unittest import mock

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows collection compatibility
    fcntl = None  # type: ignore

from installer import stopper_launch_agent
from client import runner


@unittest.skipUnless(
    hasattr(os, "getuid") and fcntl is not None,
    "macOS LaunchAgent tests require POSIX ownership and locking APIs",
)
class StopperLaunchAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.runtime_tmp = self.root / "tmp"
        self.de_root = self.root / "decision-engine"
        self.stopper = runner.shipped_stopper_path(self.de_root)
        assert self.stopper is not None
        self.stopper.parent.mkdir(parents=True)
        self.stopper.write_bytes(b"native-stopper")
        self.stopper.chmod(0o755)
        self.launchctl_calls: list[list[str]] = []
        self.service_loaded = False

    def _registry_path(self) -> Path:
        runtime = self.runtime_tmp / ("decision-engine-host-bridge-%s" % os.getuid())
        return runtime / ".runtime" / "active-runs.json"

    @mock.patch("platform.system", return_value="Darwin")
    @mock.patch("tempfile.gettempdir", return_value="/var/folders/caller-specific/T")
    def test_darwin_default_runtime_does_not_follow_caller_tmpdir(
        self, _gettempdir, _system
    ) -> None:
        expected = Path("/private/tmp") / (
            "decision-engine-host-bridge-%s" % os.getuid()
        )

        self.assertEqual(stopper_launch_agent.host_runtime_root(), expected)

    def _loaded_job_output(self) -> str:
        plist_path = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        environment = payload["EnvironmentVariables"]
        env_lines = "\n".join(
            "%s => %s" % (name, value) for name, value in sorted(environment.items())
        )
        return (
            "path = %s\n"
            "program = %s\n"
            "arguments = {\n"
            "\t%s\n"
            "}\n"
            "environment = {\n"
            "%s\n"
            "}\n"
            "event triggers = {\n"
            "\tcom.apple.launchd.WatchPaths => {\n"
            "\t\tdescriptor = {\n"
            '\t\t\t"WatchPaths" => [\n'
            "\t\t\t\t0 = %s\n"
            "\t\t\t]\n"
            "\t\t}\n"
            "\t}\n"
            "}\n"
        ) % (
            plist_path,
            payload["ProgramArguments"][0],
            payload["ProgramArguments"][0],
            env_lines,
            json.dumps(payload["WatchPaths"][0]),
        )

    def _launchctl_ok(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.launchctl_calls.append(argv)
        if argv[1] == "bootout":
            self.service_loaded = False
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1] == "bootstrap":
            self.service_loaded = True
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1] == "print" and not self.service_loaded:
            return subprocess.CompletedProcess(
                argv, 113, stdout="", stderr="Could not find service"
            )
        if argv[1] == "print":
            return subprocess.CompletedProcess(
                argv, 0, stdout=self._loaded_job_output(), stderr=""
            )
        raise AssertionError("unexpected launchctl command: %r" % argv)

    def test_plist_read_rejects_a_file_not_owned_by_the_current_user(self) -> None:
        plist_path = self.root / stopper_launch_agent.PLIST_NAME
        plist_path.write_bytes(plistlib.dumps({"Label": "test"}))
        real_stat = plist_path.stat()
        foreign_stat = types.SimpleNamespace(
            st_mode=real_stat.st_mode,
            st_size=real_stat.st_size,
            st_uid=os.getuid() + 1,
        )

        with mock.patch("os.fstat", return_value=foreign_stat):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent._read_plist(plist_path)

    def test_registry_validation_rejects_group_readable_state(self) -> None:
        registry = self.root / "registry.json"
        registry.write_text(
            json.dumps({"schema_version": 1, "runs": {}, "updated_at": 1.0}),
            encoding="utf-8",
        )
        registry.chmod(0o640)

        with self.assertRaises(stopper_launch_agent.LaunchAgentError):
            stopper_launch_agent._validate_registry(registry)

    def test_private_runtime_directory_rejects_foreign_ownership(self) -> None:
        runtime = self.root / "runtime"
        runtime.mkdir()
        real_stat = runtime.lstat()
        foreign_stat = types.SimpleNamespace(
            st_mode=real_stat.st_mode,
            st_uid=os.getuid() + 1,
        )

        with mock.patch.object(Path, "lstat", return_value=foreign_stat):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent._ensure_directory(runtime, private=True)

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_rejects_a_symlink_runtime_root_before_launchctl(
        self, _system
    ) -> None:
        target = self.root / "attacker-runtime"
        target.mkdir()
        runtime = self.runtime_tmp / (
            "decision-engine-host-bridge-%s" % os.getuid()
        )
        self.runtime_tmp.mkdir()
        runtime.symlink_to(target, target_is_directory=True)

        with mock.patch.object(stopper_launch_agent, "_run_launchctl") as launchctl:
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        launchctl.assert_not_called()

    @mock.patch("platform.system", return_value="Darwin")
    def test_uninstall_secures_the_runtime_root_before_locking(self, _system) -> None:
        runtime = self.runtime_tmp / (
            "decision-engine-host-bridge-%s" % os.getuid()
        )
        runtime.mkdir(parents=True)
        runtime.chmod(0o755)

        result = stopper_launch_agent.uninstall(
            home=self.home,
            quarantine_root=self.root / "quarantine",
            temp_dir=self.runtime_tmp,
        )

        self.assertEqual(result["status"], "absent")
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_writes_private_watch_agent_and_loads_it(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            result = stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )

        plist_path = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        runtime = self.runtime_tmp / ("decision-engine-host-bridge-%s" % os.getuid())
        registry = runtime / ".runtime" / "active-runs.json"

        self.assertEqual(result["status"], "installed")
        self.assertEqual(payload["Label"], stopper_launch_agent.LABEL)
        self.assertEqual(payload["ProgramArguments"], [str(self.stopper.resolve())])
        self.assertEqual(payload["WatchPaths"], [str(registry)])
        self.assertIs(payload["KeepAlive"], False)
        self.assertIs(payload["RunAtLoad"], False)
        self.assertEqual(payload["ProcessType"], "Interactive")
        self.assertEqual(
            payload["EnvironmentVariables"][stopper_launch_agent.MANAGED_MARKER_ENV],
            "1",
        )
        self.assertEqual(
            payload["EnvironmentVariables"]["DE_ACTIVE_RUNS"], str(registry)
        )
        rendered = plist_path.read_text(encoding="utf-8").lower()
        for forbidden in ("artifact", "endpoint", "activation_secret", "token"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(stat.S_IMODE(plist_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(registry.stat().st_mode), 0o600)
        self.assertEqual(json.loads(registry.read_text(encoding="utf-8"))["runs"], {})
        self.assertEqual(self.launchctl_calls[0][:2], ["/bin/launchctl", "bootout"])
        self.assertEqual(self.launchctl_calls[1][:2], ["/bin/launchctl", "print"])
        self.assertEqual(
            self.launchctl_calls[2][:3],
            ["/bin/launchctl", "bootstrap", "gui/%s" % os.getuid()],
        )
        self.assertEqual(self.launchctl_calls[3][:2], ["/bin/launchctl", "print"])

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_is_stale_when_the_runtime_root_is_not_private(
        self, _system
    ) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
            runtime = self.runtime_tmp / (
                "decision-engine-host-bridge-%s" % os.getuid()
            )
            runtime.chmod(0o755)
            result = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(result["status"], "stale")
        self.assertIs(result["loaded"], False)

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_refuses_an_unowned_same_name_plist_without_mutation(
        self, _system
    ) -> None:
        plist_path = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        plist_path.parent.mkdir(parents=True)
        original = plistlib.dumps(
            {
                "Label": stopper_launch_agent.LABEL,
                "ProgramArguments": ["/user/managed/program"],
            }
        )
        plist_path.write_bytes(original)

        with mock.patch.object(stopper_launch_agent, "_run_launchctl") as launchctl:
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        self.assertEqual(plist_path.read_bytes(), original)
        launchctl.assert_not_called()

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_rolls_back_a_new_plist_when_launchctl_raises(
        self, _system
    ) -> None:
        calls = []

        def launchctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[1] == "bootstrap":
                raise stopper_launch_agent.LaunchAgentError(
                    "launchctl execution failed"
                )
            if argv[1] == "print":
                return subprocess.CompletedProcess(
                    argv, 113, stdout="", stderr="Could not find service"
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=launchctl
        ):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        plist_path = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        self.assertFalse(plist_path.exists())
        self.assertEqual(calls[0][1], "bootout")
        self.assertEqual(calls[1][1], "print")
        self.assertEqual(calls[2][1], "bootstrap")
        self.assertEqual(calls[3][1], "bootout")
        self.assertEqual(calls[4][1], "print")

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_rejects_an_old_program_or_registry_path(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
            ready = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(ready["status"], "ready")
        plist_path = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        payload["ProgramArguments"] = [str(self.root / "old" / "stopper")]
        plist_path.write_bytes(plistlib.dumps(payload))

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stale = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(stale["status"], "stale")

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_rejects_extra_sensitive_environment_and_stale_loaded_job(
        self, _system
    ) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        plist_path = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        payload["EnvironmentVariables"]["DE_ENDPOINT"] = "https://forbidden.invalid"
        plist_path.write_bytes(plistlib.dumps(payload))

        stale_print = subprocess.CompletedProcess(
            ["/bin/launchctl", "print"],
            0,
            stdout="program = /old/decision-engine-stopper\n",
            stderr="",
        )
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", return_value=stale_print
        ):
            result = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(result["status"], "stale")

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_rejects_a_corrupt_existing_registry_without_repair(
        self, _system
    ) -> None:
        runtime = self.runtime_tmp / ("decision-engine-host-bridge-%s" % os.getuid())
        registry = runtime / ".runtime" / "active-runs.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("not-json\n", encoding="utf-8")

        with mock.patch.object(stopper_launch_agent, "_run_launchctl") as launchctl:
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        self.assertEqual(registry.read_text(encoding="utf-8"), "not-json\n")
        launchctl.assert_not_called()

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_rejects_a_boolean_registry_timestamp(self, _system) -> None:
        runtime = self.runtime_tmp / ("decision-engine-host-bridge-%s" % os.getuid())
        registry = runtime / ".runtime" / "active-runs.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps({"schema_version": 1, "runs": {}, "updated_at": True}),
            encoding="utf-8",
        )

        with mock.patch.object(stopper_launch_agent, "_run_launchctl") as launchctl:
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        launchctl.assert_not_called()

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_rejects_wrong_loaded_environment(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        loaded = self._loaded_job_output().replace(
            "DE_CONFIG_PATH => ", "DE_CONFIG_PATH => /stale/"
        )
        checked = subprocess.CompletedProcess(
            ["/bin/launchctl", "print"], 0, stdout=loaded, stderr=""
        )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", return_value=checked
        ):
            result = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(result["status"], "stale")

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_rejects_a_loaded_program_with_the_expected_prefix(
        self, _system
    ) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        loaded = self._loaded_job_output().replace(
            "program = %s" % self.stopper.resolve(),
            "program = %s.old" % self.stopper.resolve(),
        )
        checked = subprocess.CompletedProcess(
            ["/bin/launchctl", "print"], 0, stdout=loaded, stderr=""
        )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", return_value=checked
        ):
            result = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(result["status"], "stale")

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_rejects_an_unlisted_sensitive_loaded_environment(
        self, _system
    ) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        loaded = self._loaded_job_output().replace(
            "environment = {\n",
            "environment = {\nDE_ARTIFACT_PATH => forbidden\n",
            1,
        )
        checked = subprocess.CompletedProcess(
            ["/bin/launchctl", "print"], 0, stdout=loaded, stderr=""
        )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", return_value=checked
        ):
            result = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(result["status"], "stale")

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_rejects_an_extra_loaded_program_argument(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        loaded = self._loaded_job_output().replace(
            "arguments = {\n\t%s\n}" % self.stopper.resolve(),
            "arguments = {\n\t%s\n\t--unexpected\n}" % self.stopper.resolve(),
        )
        checked = subprocess.CompletedProcess(
            ["/bin/launchctl", "print"], 0, stdout=loaded, stderr=""
        )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", return_value=checked
        ):
            result = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(result["status"], "stale")

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_rejects_an_extra_loaded_watch_path(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        expected = "\t\t\t\t0 = %s\n" % json.dumps(str(self._registry_path()))
        loaded = self._loaded_job_output().replace(
            expected,
            expected + '\t\t\t\t1 = "/unexpected/watch"\n',
        )
        checked = subprocess.CompletedProcess(
            ["/bin/launchctl", "print"], 0, stdout=loaded, stderr=""
        )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", return_value=checked
        ):
            result = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(result["status"], "stale")

    @mock.patch("platform.system", return_value="Darwin")
    def test_status_contains_a_launchctl_probe_failure(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )

        with mock.patch.object(
            stopper_launch_agent,
            "_run_launchctl",
            side_effect=stopper_launch_agent.LaunchAgentError(
                "launchctl execution failed"
            ),
        ):
            result = stopper_launch_agent.status(
                home=self.home,
                de_root=self.de_root,
                temp_dir=self.runtime_tmp,
            )

        self.assertEqual(result["status"], "stale")
        self.assertIs(result["loaded"], False)

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_restricts_an_existing_registry_to_the_current_user(
        self, _system
    ) -> None:
        runtime = self.runtime_tmp / ("decision-engine-host-bridge-%s" % os.getuid())
        registry = runtime / ".runtime" / "active-runs.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps({"schema_version": 1, "runs": {}, "updated_at": 1.0}),
            encoding="utf-8",
        )
        registry.chmod(0o644)

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )

        self.assertEqual(stat.S_IMODE(registry.stat().st_mode), 0o600)

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_uses_a_private_exclusive_management_lock(self, _system) -> None:
        if fcntl is None:
            self.skipTest("fcntl is required for the macOS LaunchAgent path")
        with (
            mock.patch.object(
                stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
            ),
            mock.patch("fcntl.flock") as flock,
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )

        lock_path = (
            self.runtime_tmp
            / ("decision-engine-host-bridge-%s" % os.getuid())
            / ".runtime"
            / "locks"
            / "stopper-hostbridge-management.lock"
        )
        self.assertTrue(lock_path.is_file())
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
        self.assertEqual(
            [call.args[1] for call in flock.call_args_list],
            [fcntl.LOCK_EX, fcntl.LOCK_UN],
        )

    @mock.patch("platform.system", return_value="Darwin")
    def test_failed_update_reports_when_previous_agent_cannot_be_restored(
        self, _system
    ) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )

        def restore_fails(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[1] == "bootstrap":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="failed")
            if argv[1] == "print":
                return subprocess.CompletedProcess(
                    argv,
                    113,
                    stdout="",
                    stderr="Could not find service",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            mock.patch.object(
                stopper_launch_agent, "_service_loaded_state", return_value=True
            ),
            mock.patch.object(
                stopper_launch_agent, "_run_launchctl", side_effect=restore_fails
            ),
        ):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError) as caught:
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        self.assertIn("rollback did not complete", str(caught.exception))

    @mock.patch("platform.system", return_value="Darwin")
    def test_install_rolls_back_when_publish_raises_after_replace(
        self, _system
    ) -> None:
        real_write = stopper_launch_agent._atomic_write
        plist_publish_calls = 0

        def published_then_failed(path, content, *, mode=0o600):
            nonlocal plist_publish_calls
            real_write(path, content, mode=mode)
            if path.name == stopper_launch_agent.PLIST_NAME:
                plist_publish_calls += 1
            if plist_publish_calls == 1:
                raise stopper_launch_agent.LaunchAgentError(
                    "post-replace permission failure"
                )

        with (
            mock.patch.object(
                stopper_launch_agent, "_atomic_write", side_effect=published_then_failed
            ),
            mock.patch.object(
                stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
            ),
        ):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        plist_path = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        self.assertFalse(plist_path.exists())

    @mock.patch("platform.system", return_value="Darwin")
    def test_failed_update_restores_the_original_unloaded_state(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        plist_path = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        original = plist_path.read_bytes()
        self.service_loaded = False
        bootstrap_calls = 0

        def bootstrap_fails(argv: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal bootstrap_calls
            if argv[1] == "bootstrap":
                bootstrap_calls += 1
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="failed")
            if argv[1] == "print":
                return subprocess.CompletedProcess(
                    argv, 113, stdout="", stderr="Could not find service"
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=bootstrap_fails
        ):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        self.assertEqual(plist_path.read_bytes(), original)
        self.assertEqual(bootstrap_calls, 1)

    @mock.patch("platform.system", return_value="Darwin")
    def test_failed_update_restores_the_original_loaded_state(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        plist_path = (
            self.home / "Library" / "LaunchAgents" / stopper_launch_agent.PLIST_NAME
        )
        original = plist_path.read_bytes()
        loaded = True
        bootstrap_calls = 0

        def first_bootstrap_fails(argv: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal bootstrap_calls, loaded
            if argv[1] == "bootout":
                loaded = False
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[1] == "bootstrap":
                bootstrap_calls += 1
                if bootstrap_calls == 1:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="failed"
                    )
                loaded = True
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[1] == "print" and loaded:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._loaded_job_output(), stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 113, stdout="", stderr="Could not find service"
            )

        with mock.patch.object(
            stopper_launch_agent,
            "_run_launchctl",
            side_effect=first_bootstrap_fails,
        ):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError) as caught:
                stopper_launch_agent.install(
                    self.de_root, home=self.home, temp_dir=self.runtime_tmp
                )

        self.assertNotIn("rollback did not complete", str(caught.exception))
        self.assertEqual(plist_path.read_bytes(), original)
        self.assertTrue(loaded)
        self.assertEqual(bootstrap_calls, 2)

    @mock.patch("platform.system", return_value="Darwin")
    def test_uninstall_quarantines_only_an_owned_agent(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        source = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        quarantine = self.root / "quarantine"
        self.launchctl_calls.clear()

        def unloaded(argv: list[str]) -> subprocess.CompletedProcess[str]:
            self.launchctl_calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                113 if argv[1] == "print" else 0,
                stdout="",
                stderr="Could not find service" if argv[1] == "print" else "",
            )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=unloaded
        ):
            result = stopper_launch_agent.uninstall(
                home=self.home,
                quarantine_root=quarantine,
                temp_dir=self.runtime_tmp,
            )

        target = quarantine / stopper_launch_agent.PLIST_NAME
        self.assertEqual(result["status"], "quarantined")
        self.assertFalse(source.exists())
        self.assertTrue(target.is_file())
        self.assertEqual(self.launchctl_calls[0][:2], ["/bin/launchctl", "bootout"])
        self.assertEqual(self.launchctl_calls[1][:2], ["/bin/launchctl", "print"])

    @mock.patch("platform.system", return_value="Darwin")
    def test_uninstall_keeps_the_plist_when_the_service_remains_loaded(
        self, _system
    ) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        source = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )

        def still_loaded(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                0 if argv[1] == "print" else 1,
                stdout="",
                stderr="",
            )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=still_loaded
        ):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.uninstall(
                    home=self.home,
                    quarantine_root=self.root / "quarantine",
                    temp_dir=self.runtime_tmp,
                )

        self.assertTrue(source.is_file())

    @mock.patch("platform.system", return_value="Darwin")
    def test_uninstall_rejects_an_ambiguous_launchctl_print_failure(
        self, _system
    ) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        source = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )

        def ambiguous(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                64 if argv[1] == "print" else 1,
                stdout="",
                stderr="domain unavailable",
            )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=ambiguous
        ):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.uninstall(
                    home=self.home,
                    quarantine_root=self.root / "quarantine",
                    temp_dir=self.runtime_tmp,
                )

        self.assertTrue(source.is_file())

    @mock.patch("platform.system", return_value="Darwin")
    def test_uninstall_rechecks_plist_identity_after_bootout(self, _system) -> None:
        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=self._launchctl_ok
        ):
            stopper_launch_agent.install(
                self.de_root, home=self.home, temp_dir=self.runtime_tmp
            )
        source = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        replacement = plistlib.dumps(
            {
                "Label": stopper_launch_agent.LABEL,
                "ProgramArguments": ["/user/replacement"],
            }
        )

        def replaced(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[1] == "bootout":
                source.write_bytes(replacement)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                argv,
                113,
                stdout="",
                stderr="Could not find service",
            )

        with mock.patch.object(
            stopper_launch_agent, "_run_launchctl", side_effect=replaced
        ):
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.uninstall(
                    home=self.home,
                    quarantine_root=self.root / "quarantine",
                    temp_dir=self.runtime_tmp,
                )

        self.assertEqual(source.read_bytes(), replacement)

    @mock.patch("platform.system", return_value="Darwin")
    def test_uninstall_refuses_an_unowned_same_name_plist(self, _system) -> None:
        source = (
            self.home / "Library" / "LaunchAgents" / (stopper_launch_agent.PLIST_NAME)
        )
        source.parent.mkdir(parents=True)
        original = plistlib.dumps(
            {
                "Label": stopper_launch_agent.LABEL,
                "ProgramArguments": ["/user/managed/program"],
            }
        )
        source.write_bytes(original)

        with mock.patch.object(stopper_launch_agent, "_run_launchctl") as launchctl:
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent.uninstall(
                    home=self.home,
                    quarantine_root=self.root / "quarantine",
                    temp_dir=self.runtime_tmp,
                )

        self.assertEqual(source.read_bytes(), original)
        launchctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
