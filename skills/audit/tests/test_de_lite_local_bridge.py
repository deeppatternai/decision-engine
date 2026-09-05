"""Behavior tests for the host-only DE Lite audit lifecycle bridge."""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from client import windows_security
from installer import stopper_launch_agent


ROOT = Path(__file__).resolve().parents[3]
BRIDGE = ROOT / "skills" / "audit" / "scripts" / "de_lite_local_bridge.py"
_LOADED_BRIDGE_FIXTURES = []


def _load_bridge(
    *, system: str = "Linux", activation_config: Optional[dict] = None
):
    fixture_parent = Path(tempfile.gettempdir())
    fixture_parent.mkdir(parents=True, exist_ok=True)
    fixture = tempfile.TemporaryDirectory(
        prefix="de-lite-bridge-device-", dir=str(fixture_parent)
    )
    device_config = Path(fixture.name) / "decision-engine" / "config.json"
    if activation_config is None:
        activation_config = {"server_endpoint": "https://hub.example", "access_token": "test-token"}
    if activation_config is not False:
        device_config.parent.mkdir(parents=True)
        if os.name == "nt":
            windows_security.harden_private_data_acl(device_config.parent)
        device_config.write_text(json.dumps(activation_config), encoding="utf-8")
        device_config.chmod(0o600)
    spec = importlib.util.spec_from_file_location("de_lite_local_bridge_test", BRIDGE)
    if spec is None or spec.loader is None:
        raise AssertionError("bridge module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    previous_home = os.environ.get("DEEPPATTERN_HOME")
    os.environ["DEEPPATTERN_HOME"] = fixture.name
    try:
        with mock.patch("platform.system", return_value=system):
            spec.loader.exec_module(module)
    finally:
        if previous_home is None:
            os.environ.pop("DEEPPATTERN_HOME", None)
        else:
            os.environ["DEEPPATTERN_HOME"] = previous_home
    module._test_device_fixture = fixture
    _LOADED_BRIDGE_FIXTURES.append(fixture)
    return module


class DELiteLocalBridgeCLITests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        identity = str(os.getuid()) if hasattr(os, "getuid") else "user"
        self.runtime = self.tmp / ("decision-engine-host-bridge-%s" % identity)
        self.registry = self.runtime / ".runtime" / "active-runs.json"

    def tearDown(self):
        while _LOADED_BRIDGE_FIXTURES:
            _LOADED_BRIDGE_FIXTURES.pop().cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in (
                "DE_ACTIVE_RUN",
                "DE_ACTIVE_RUNS",
                "DE_ACTIVE_RUNS_LOCK",
                "DE_ACTIVE_RUNS_LOCK_PATHS",
                "DE_ACTIVE_RUNS_WRITE_PATHS",
                "DE_ACTIVE_RUN_WRITE_PATHS",
                "DE_STOPPER_SINGLETON_LOCK",
                "DE_SKIP_STOPPER_LAUNCH",
            ):
                os.environ.pop(name, None)
            with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
                bridge = _load_bridge()
                try:
                    with (
                        mock.patch("platform.system", return_value="Linux"),
                        mock.patch.object(bridge.runner, "launch_stopper_if_available"),
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        try:
                            returncode = bridge.main(list(args))
                        except SystemExit as exc:
                            returncode = int(exc.code)
                finally:
                    bridge._test_device_fixture.cleanup()
        return subprocess.CompletedProcess(
            [str(BRIDGE), *args],
            returncode,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )

    def test_begin_creates_one_mcp_unavailable_de_lite_run(self):
        completed = self._run("begin", "--title", "Explicit audit topic")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertTrue(output["local_id"].startswith("local_host_"))
        self.assertEqual(output["status"], "running")
        self.assertEqual(output["local_surface"], "de_lite")
        self.assertEqual(output["degrade_reason"], "mcp_unavailable")
        self.assertEqual(output["fallback_mode"], "session-llm")
        self.assertIsNone(output["audit_id"])
        self.assertIs(output["advisory_only"], True)

        runs = json.loads(self.registry.read_text(encoding="utf-8"))["runs"]
        self.assertEqual(set(runs), {output["local_id"]})
        run = runs[output["local_id"]]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["title"], "Explicit audit topic")
        self.assertEqual(run["local_surface"], "de_lite")
        self.assertEqual(run["degrade_reason"], "mcp_unavailable")
        self.assertEqual(run["fallback_mode"], "session-llm")
        self.assertIs(run["advisory_only"], True)
        self.assertNotIn("audit_id", run)

    def test_begin_preserves_the_user_audit_title(self):
        completed = self._run("begin", "--title", "三边界降级行为审计")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        run = json.loads(self.registry.read_text(encoding="utf-8"))["runs"][
            output["local_id"]
        ]
        self.assertEqual(run["title"], "三边界降级行为审计")
        self.assertEqual(run["degrade_reason"], "mcp_unavailable")

    def test_unactivated_device_takes_priority_over_missing_mcp(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge(activation_config={})
        with mock.patch.object(bridge, "_launch_host_stopper"):
            output = bridge.begin("未激活设备审计")

        self.assertEqual(output["degrade_reason"], "unactivated")
        run = json.loads(self.registry.read_text(encoding="utf-8"))["runs"][
            output["local_id"]
        ]
        self.assertEqual(run["degrade_reason"], "unactivated")

    def test_token_present_activation_config_does_not_claim_unactivated(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge(activation_config={"access_token": "token"})
        self.assertEqual(bridge._degrade_reason(), "mcp_unavailable")

    def test_begin_requires_a_nonempty_user_audit_topic(self):
        for args in (("begin",), ("begin", "--title", "   ")):
            with self.subTest(args=args):
                completed = self._run(*args)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(self.registry.exists())

    def test_installed_body_without_installer_can_load_the_bridge(self):
        installed = self.tmp / "installed-de"
        installed_bridge = installed / "skills" / "audit" / "scripts" / BRIDGE.name
        installed_bridge.parent.mkdir(parents=True)
        shutil.copy2(BRIDGE, installed_bridge)
        (installed / "client").symlink_to(ROOT / "client", target_is_directory=True)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        completed = subprocess.run(
            [sys.executable, str(installed_bridge), "--help"],
            cwd=self.tmp,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("ModuleNotFoundError", completed.stderr)

    def test_self_contained_agent_payload_matches_the_installer_contract(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()

        with mock.patch("platform.system", return_value="Darwin"):
            expected = stopper_launch_agent._expected_payload(ROOT, temp_dir=self.tmp)
            actual = bridge._expected_host_agent_payload()

        self.assertEqual(actual, expected)

    def test_expected_agent_payload_rejects_an_unsupported_architecture(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()

        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="ppc64"),
            self.assertRaises(OSError),
        ):
            bridge._expected_host_agent_payload()

    def test_darwin_runtime_does_not_follow_the_sandbox_tmpdir(self):
        sandbox_tmp = self.tmp / "sandbox-selected-temp"
        with mock.patch("tempfile.gettempdir", return_value=str(sandbox_tmp)):
            bridge = _load_bridge(system="Darwin")

        self.assertEqual(bridge._HOST_TEMP_DIR, Path("/private/tmp"))

    def test_begin_uses_host_runtime_when_installed_root_is_not_writable(self):
        blocked_home = self.tmp / "blocked-home"
        blocked_home.write_text("not a directory", encoding="utf-8")
        host_tmp = self.tmp / "host-tmp"
        host_tmp.mkdir()
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"HOME": str(blocked_home)}),
            mock.patch("tempfile.gettempdir", return_value=str(host_tmp)),
        ):
            bridge = _load_bridge()
            with (
                mock.patch("platform.system", return_value="Linux"),
                mock.patch.object(bridge.runner, "launch_stopper_if_available"),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    bridge.main(["begin", "--title", "Explicit audit topic"]), 0
                )
        output = json.loads(stdout.getvalue())
        self.assertTrue(output["local_id"].startswith("local_host_"))
        identity = str(os.getuid()) if hasattr(os, "getuid") else "user"
        registry = (
            host_tmp
            / ("decision-engine-host-bridge-%s" % identity)
            / ".runtime"
            / "active-runs.json"
        )
        runs = json.loads(registry.read_text(encoding="utf-8"))["runs"]
        self.assertEqual(set(runs), {output["local_id"]})

        with (
            mock.patch.dict(os.environ, {"HOME": str(blocked_home)}),
            mock.patch("tempfile.gettempdir", return_value=str(host_tmp)),
        ):
            bridge = _load_bridge()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    bridge.main(
                        [
                            "complete",
                            "--local-id",
                            output["local_id"],
                            "--status",
                            "completed",
                        ]
                    ),
                    0,
                )
        run = json.loads(registry.read_text(encoding="utf-8"))["runs"][
            output["local_id"]
        ]
        self.assertEqual(run["status"], "completed")
        self.assertAlmostEqual(
            run["hidden_after"] - run["completed_at"], 30.0, delta=0.2
        )

    def test_darwin_begin_uses_the_loaded_owned_watch_agent_without_spawning(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch.object(
                bridge, "_darwin_host_agent_ready", return_value=True
            ) as ready,
            mock.patch.object(
                bridge.runner,
                "launch_stopper_if_available",
                side_effect=AssertionError("direct sandbox child launch"),
            ),
        ):
            output = bridge.begin("Explicit audit topic")

        self.assertTrue(output["local_id"].startswith("local_host_"))
        ready.assert_called_once_with()

    def test_complete_moves_the_existing_run_to_terminal_linger(self):
        started = json.loads(
            self._run("begin", "--title", "Explicit audit topic").stdout
        )

        completed = self._run(
            "complete", "--local-id", started["local_id"], "--status", "completed"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(
            output,
            {
                "status": "completed",
                "local_id": started["local_id"],
                "local_surface": "de_lite",
                "degrade_reason": "mcp_unavailable",
                "fallback_mode": "session-llm",
                "audit_id": None,
                "advisory_only": True,
            },
        )
        run = json.loads(self.registry.read_text(encoding="utf-8"))["runs"][
            started["local_id"]
        ]
        self.assertEqual(run["status"], "completed")
        self.assertAlmostEqual(
            run["hidden_after"] - run["completed_at"], 30.0, delta=0.2
        )

    def test_complete_rejects_a_hosted_id_without_a_traceback(self):
        completed = self._run(
            "complete", "--local-id", "aud_hosted", "--status", "completed"
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            completed.stderr,
            '{"status": "failed", "reason": "local-advisory-rejected"}\n',
        )
        self.assertNotIn("Traceback", completed.stderr)
        self.assertFalse(self.registry.exists())

    def test_complete_cannot_finish_a_local_run_owned_by_the_mcp_path(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()
            bridge.runner.save_local_advisory_run(
                "local_mcp_owned",
                surface="de_lite",
                degrade_reason="service_unavailable",
            )

        completed = self._run(
            "complete", "--local-id", "local_mcp_owned", "--status", "completed"
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            completed.stderr,
            '{"status": "failed", "reason": "local-advisory-rejected"}\n',
        )
        run = json.loads(self.registry.read_text(encoding="utf-8"))["runs"][
            "local_mcp_owned"
        ]
        self.assertEqual(run["status"], "running")

    def test_begin_has_no_artifact_or_credential_input_and_never_calls_hub(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()
        self.assertEqual(
            tuple(inspect.signature(bridge.begin).parameters), ("title", "ui_locale")
        )

        with (
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "must-not-be-read",
                    "DE_ACTIVATION_SECRET": "must-not-be-read",
                },
            ),
            mock.patch.object(
                bridge.runner, "load_config", side_effect=AssertionError("config read")
            ) as load_config,
            mock.patch.object(
                bridge.runner,
                "request_json",
                side_effect=AssertionError("network request"),
            ) as request_json,
            mock.patch.object(bridge, "_launch_host_stopper") as launch,
            mock.patch.object(bridge.secrets, "token_hex", return_value="a" * 24),
        ):
            output = bridge.begin("Explicit audit topic")

        self.assertEqual(output["local_id"], "local_host_" + "a" * 24)
        load_config.assert_not_called()
        request_json.assert_not_called()
        launch.assert_called_once_with()

    def test_begin_rejects_artifact_text_as_an_unknown_argument(self):
        completed = self._run("begin", "--content", "artifact-must-stay-with-host")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.registry.exists())

    def test_terminal_run_cannot_be_reopened_with_a_different_status(self):
        started = json.loads(
            self._run("begin", "--title", "Explicit audit topic").stdout
        )
        first = self._run(
            "complete", "--local-id", started["local_id"], "--status", "completed"
        )
        second = self._run(
            "complete", "--local-id", started["local_id"], "--status", "failed"
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 1)
        run = json.loads(self.registry.read_text(encoding="utf-8"))["runs"][
            started["local_id"]
        ]
        self.assertEqual(run["status"], "completed")

    def test_begin_io_failure_is_a_structured_hard_stop(self):
        bridge = _load_bridge()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                bridge.runner,
                "save_local_advisory_run",
                side_effect=OSError("disk unavailable"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = bridge.main(
                ["begin", "--title", "Explicit audit topic"]
            )

        self.assertEqual(returncode, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            '{"status": "failed", "reason": "local-bridge-failed"}\n',
        )
        self.assertNotIn("disk unavailable", stderr.getvalue())

    def test_begin_does_not_publish_a_run_when_host_agent_is_unavailable(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch.object(bridge.secrets, "token_hex", return_value="b" * 24),
            mock.patch.object(
                bridge,
                "_launch_host_stopper",
                side_effect=OSError("host agent unavailable"),
            ),
        ):
            with self.assertRaises(OSError):
                bridge.begin("Explicit audit topic")

        self.assertFalse(self.registry.exists())

    def test_import_discards_inherited_runner_path_overrides(self):
        attacker_registry = self.tmp / "caller-selected-registry.json"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DE_ACTIVE_RUNS": str(attacker_registry),
                    "DE_ACTIVE_RUN": str(self.tmp / "caller-selected-run.json"),
                    "DE_SKIP_STOPPER_LAUNCH": "1",
                },
            ),
            mock.patch("tempfile.gettempdir", return_value=str(self.tmp)),
        ):
            bridge = _load_bridge()
            self.assertNotEqual(
                bridge.runner.de_lite_active_runs_path(), attacker_registry
            )
            for name in bridge._CALLER_RUNTIME_OVERRIDES:
                self.assertNotIn(name, os.environ)

    def test_darwin_agent_with_extra_sensitive_environment_is_not_ready(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()
        path = self.tmp / "com.decision-engine.stopper.hostbridge.plist"
        with mock.patch("platform.system", return_value="Darwin"):
            payload = bridge._expected_host_agent_payload()
        environment = dict(payload["EnvironmentVariables"])
        environment["DE_ARTIFACT_PATH"] = "forbidden"
        env_lines = "\n".join("%s => %s" % item for item in sorted(environment.items()))
        output = (
            "path = %s\n"
            "program = %s\n"
            "arguments = {\n\t%s\n}\n"
            "environment = {\n%s\n}\n"
            '"WatchPaths" => [\n\t0 = %s\n]\n'
        ) % (
            path,
            payload["ProgramArguments"][0],
            payload["ProgramArguments"][0],
            env_lines,
            json.dumps(payload["WatchPaths"][0]),
        )

        self.assertFalse(bridge._loaded_job_matches(output, path=path, payload=payload))

    def test_darwin_agent_rejects_a_symlink_runtime_root(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()
        target = self.tmp / "attacker-runtime"
        registry = target / ".runtime" / "active-runs.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps(
                {"schema_version": 1, "runs": {}, "updated_at": 1.0}
            ),
            encoding="utf-8",
        )
        registry.chmod(0o600)
        self.runtime.symlink_to(target, target_is_directory=True)
        path = self.tmp / "com.decision-engine.stopper.hostbridge.plist"
        with mock.patch("platform.system", return_value="Darwin"):
            payload = bridge._expected_host_agent_payload()
        path.write_bytes(plistlib.dumps(payload))
        path.chmod(0o600)
        environment = payload["EnvironmentVariables"]
        env_lines = "\n".join(
            "%s => %s" % item for item in sorted(environment.items())
        )
        output = (
            "path = %s\n"
            "program = %s\n"
            "arguments = {\n\t%s\n}\n"
            "environment = {\n%s\n}\n"
            '"WatchPaths" => [\n\t0 = %s\n]\n'
        ) % (
            path,
            payload["ProgramArguments"][0],
            payload["ProgramArguments"][0],
            env_lines,
            json.dumps(payload["WatchPaths"][0]),
        )
        checked = subprocess.CompletedProcess(
            ["launchctl", "print"], 0, stdout=output, stderr=""
        )

        with (
            mock.patch.object(bridge, "_host_launch_agent_path", return_value=path),
            mock.patch.object(bridge.subprocess, "run", return_value=checked),
        ):
            self.assertFalse(bridge._darwin_host_agent_ready())

    def test_private_runtime_directory_check_locks_owner_and_mode(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.tmp)):
            bridge = _load_bridge()
        paths = bridge._host_runtime_paths()
        paths["root"].mkdir(mode=0o700, parents=True, exist_ok=True)
        paths["root"].chmod(0o700)
        if os.name == "nt":
            windows_security.harden_private_data_acl(paths["root"])
        for name in ("state", "locks", "logs"):
            paths[name].mkdir(mode=0o700, parents=True, exist_ok=True)
            paths[name].chmod(0o700)

        self.assertTrue(bridge._private_runtime_directories_ready(paths))
        if os.name == "nt":
            if not shutil.which("icacls"):
                self.skipTest("icacls is required for ACL integration")
            granted = subprocess.run(
                ["icacls", str(paths["state"]), "/grant", "*S-1-1-0:(R)"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(granted.returncode, 0)
            self.assertFalse(bridge._private_runtime_directories_ready(paths))
            return
        paths["state"].chmod(0o755)
        self.assertFalse(bridge._private_runtime_directories_ready(paths))
        paths["state"].chmod(0o700)
        real_stat = paths["root"].lstat()
        foreign_stat = type("ForeignStat", (), {
            "st_mode": real_stat.st_mode,
            "st_uid": os.getuid() + 1,
        })()
        with mock.patch.object(Path, "lstat", return_value=foreign_stat):
            self.assertFalse(bridge._private_runtime_directories_ready(paths))


if __name__ == "__main__":
    unittest.main()
