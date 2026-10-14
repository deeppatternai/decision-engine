from __future__ import annotations

import os
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from installer import doctor, install, mcp_config
from installer.client_hosts.hosts import deepseek_harness
from installer.client_hosts.registry import CLIENT_SPECS
from installer.config import ShellError


class DeepSeekHarnessHostContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dsh_home = self.tmp / ".dsh"
        self.config = self.dsh_home / "cordis.patch.yml"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.tmp),
                "DSH_HOME": str(self.dsh_home),
                "DEEPSEEK_HARNESS_CONFIG": str(self.config),
            },
            clear=False,
        )
        self.environment.start()
        self.root = mock.patch.object(
            mcp_config,
            "registration_root",
            return_value=self.tmp / "managed-de",
        )
        self.root.start()

    def tearDown(self) -> None:
        self.root.stop()
        self.environment.stop()
        self._tmp.cleanup()

    def _write(self, payload: bytes) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_bytes(payload)

    def test_registry_declares_display_observed_identity_and_user_skills(self) -> None:
        spec = CLIENT_SPECS["deepseek-harness"]

        self.assertEqual(spec.config_format, "yaml")
        self.assertEqual(spec.config_renderer, "cordis-yaml-v1")
        self.assertEqual(spec.observed_client_aliases, frozenset({"dsh-mcp-client"}))
        self.assertTrue(spec.require_observed_identity)
        self.assertEqual(
            spec.launcher_capabilities,
            frozenset({"core-mcp", "local-display", "audit-stop-panel"}),
        )
        self.assertTrue(spec.local_display_tools)
        self.assertFalse(spec.popup_followup)
        self.assertTrue(spec.audit_stop_panel)
        self.assertEqual(
            spec.optional_features,
            frozenset({"local-display", "audit-stop-panel"}),
        )
        self.assertEqual(spec.skills_global_path(), self.dsh_home / "skills")

    def test_default_and_explicit_dsh_home(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}, clear=False):
            os.environ.pop("DSH_HOME", None)
            os.environ.pop("DEEPSEEK_HARNESS_CONFIG", None)
            self.assertEqual(
                deepseek_harness.config_path(),
                self.tmp / ".dsh" / "cordis.patch.yml",
            )
        custom = self.tmp / "custom-dsh"
        with mock.patch.dict(os.environ, {"DSH_HOME": str(custom)}, clear=False):
            os.environ.pop("DEEPSEEK_HARNESS_CONFIG", None)
            self.assertEqual(
                deepseek_harness.config_path(),
                custom / "cordis.patch.yml",
            )
        with mock.patch.dict(os.environ, {"DSH_HOME": "relative-dsh"}, clear=False):
            os.environ.pop("DEEPSEEK_HARNESS_CONFIG", None)
            with self.assertRaisesRegex(ShellError, "absolute path"):
                deepseek_harness.config_path()
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_HARNESS_CONFIG": "relative-cordis.patch.yml"},
            clear=False,
        ):
            with self.assertRaisesRegex(ShellError, "absolute path"):
                deepseek_harness.config_path()

    def test_installation_identity_requires_config_or_real_cli(self) -> None:
        self.dsh_home.mkdir(parents=True)
        with mock.patch.object(
            deepseek_harness.shutil,
            "which",
            return_value=None,
        ), mock.patch.object(
            deepseek_harness,
            "_macos_app_cli_path",
            return_value=None,
        ):
            self.assertFalse(deepseek_harness.installed())
            self.config.write_text("[]\n", encoding="utf-8")
            self.assertTrue(deepseek_harness.installed())
            self.config.unlink()
        executable = self.tmp / "dsh"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        with mock.patch.object(
            deepseek_harness.shutil,
            "which",
            return_value=str(executable),
        ):
            self.assertTrue(deepseek_harness.installed())

    def test_cli_identity_allows_first_config_write(self) -> None:
        executable = self.tmp / "dsh"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        with mock.patch.object(
            deepseek_harness.shutil,
            "which",
            return_value=str(executable),
        ):
            result = mcp_config.write_entry(
                "deepseek-harness",
                python=sys.executable,
                cwd=self.tmp / "managed-de",
            )

        self.assertEqual(result["action"], "added")
        self.assertEqual(mcp_config.entry_status("deepseek-harness"), "ready")

    def test_native_macos_app_identity_allows_first_config_write(self) -> None:
        applications = self.tmp / "Applications"
        executable = (
            applications
            / "DeepSeek Harness.app"
            / "Contents"
            / "Resources"
            / "runtime"
            / "cli"
            / "bin"
            / "dsh"
        )
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

        with mock.patch.object(
            deepseek_harness.shutil,
            "which",
            return_value=None,
        ), mock.patch.object(
            deepseek_harness.sys,
            "platform",
            "darwin",
        ), mock.patch.object(
            deepseek_harness,
            "_macos_application_roots",
            return_value=(applications,),
        ):
            result = mcp_config.write_entry(
                "deepseek-harness",
                python=sys.executable,
                cwd=self.tmp / "managed-de",
            )

        self.assertEqual(result["action"], "added")
        self.assertEqual(mcp_config.entry_status("deepseek-harness"), "ready")

    def test_rendered_entry_uses_native_plugin_shape_and_660_seconds(self) -> None:
        entry = mcp_config.expected_entry(
            "deepseek-harness",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
        )

        self.assertEqual(entry["plugin_name"], "@deepseek-ai/dsh-mcp-client")
        self.assertEqual(entry["server_name"], "decision-engine")
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"], ["-m", "installer.launcher"])
        self.assertEqual(entry["cwd"], str(self.tmp / "managed-de"))
        self.assertEqual(entry["env"]["DE_MCP_CLIENT_HOST"], "deepseek-harness")
        self.assertEqual(entry["tool_timeout_sec"], 660)

    def test_apply_repeat_verify_and_remove_preserve_bom_crlf_and_js(self) -> None:
        prefix = (
            b"\xef\xbb\xbf# personal\r\n"
            b"- id: personal\r\n"
            b"  config:\r\n"
            b"    token: !!js process.env.DSH_TOKEN\r\n"
        )
        self._write(prefix)

        first = mcp_config.write_entry(
            "deepseek-harness",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
        )
        applied = self.config.read_bytes()
        self.assertEqual(first["action"], "added")
        self.assertTrue(applied.startswith(prefix))
        self.assertIn(b"@deepseek-ai/dsh-mcp-client", applied)
        self.assertIn(b"toolCallTimeoutMs: 660000", applied)
        self.assertNotIn(b"\n", applied.replace(b"\r\n", b""))
        self.assertEqual(mcp_config.entry_status("deepseek-harness"), "ready")

        second = mcp_config.write_entry(
            "deepseek-harness",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
        )
        self.assertEqual(second["action"], "unchanged")
        self.assertEqual(self.config.read_bytes(), applied)

        removed = mcp_config.remove_entry("deepseek-harness")
        self.assertEqual(removed["action"], "removed")
        self.assertEqual(self.config.read_bytes(), prefix)
        self.assertIsNone(mcp_config.read_entry("deepseek-harness"))

    def test_empty_flow_patch_round_trip_restores_original_bytes(self) -> None:
        original = b"\xef\xbb\xbf[  ]\r\n"
        self._write(original)

        mcp_config.write_entry(
            "deepseek-harness",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
        )
        self.assertEqual(mcp_config.entry_status("deepseek-harness"), "ready")
        mcp_config.remove_entry("deepseek-harness")

        self.assertEqual(self.config.read_bytes(), original)

    def test_zero_byte_patch_round_trip_restores_original_bytes(self) -> None:
        original = b""
        self._write(original)

        mcp_config.write_entry(
            "deepseek-harness",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
        )
        self.assertEqual(mcp_config.entry_status("deepseek-harness"), "ready")
        mcp_config.remove_entry("deepseek-harness")

        self.assertEqual(self.config.read_bytes(), original)

    def test_external_snapshot_removal_can_disable_the_secondary_backup(self) -> None:
        original = b"- id: personal\n"
        self._write(original)
        mcp_config.write_entry(
            "deepseek-harness",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
        )
        backups_before_remove = set(
            self.config.parent.glob("cordis.patch.yml.de-bak.*")
        )

        removed = mcp_config.remove_entry(
            "deepseek-harness",
            create_backup=False,
        )

        self.assertEqual(removed["action"], "removed")
        self.assertIsNone(removed["backup"])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(
            set(self.config.parent.glob("cordis.patch.yml.de-bak.*")),
            backups_before_remove,
        )

    def test_foreign_same_id_or_server_name_is_never_adopted(self) -> None:
        fixtures = (
            b"- insert:\n    - id: decision-engine\n      name: foreign\n",
            b"- insert:\n    - id: foreign\n      name: '@deepseek-ai/dsh-mcp-client'\n      config:\n        serverName: decision-engine\n",
        )
        for original in fixtures:
            with self.subTest(original=original):
                self._write(original)
                with self.assertRaisesRegex(ShellError, "same_name_unowned"):
                    mcp_config.write_entry(
                        "deepseek-harness",
                        python=sys.executable,
                        cwd=self.tmp / "managed-de",
                    )
                self.assertEqual(self.config.read_bytes(), original)

    def test_malformed_or_duplicate_marker_is_refused_without_mutation(self) -> None:
        fixtures = (
            b"- insert: []\n# BEGIN DEEP PATTERN decision-engine (managed)\n",
            b"# BEGIN DEEP PATTERN decision-engine (managed)\n# END DEEP PATTERN decision-engine (managed)\n"
            b"# BEGIN DEEP PATTERN decision-engine (managed)\n# END DEEP PATTERN decision-engine (managed)\n",
        )
        for original in fixtures:
            with self.subTest(original=original):
                self._write(original)
                with self.assertRaises(ShellError):
                    mcp_config.write_entry(
                        "deepseek-harness",
                        python=sys.executable,
                        cwd=self.tmp / "managed-de",
                    )
                self.assertEqual(self.config.read_bytes(), original)

    def test_managed_block_with_extra_launch_field_is_not_adopted(self) -> None:
        self._write(b"- insert: []\n")
        mcp_config.write_entry(
            "deepseek-harness",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
        )
        original = self.config.read_bytes().replace(
            b"        toolCallTimeoutMs: 660000\n",
            b"        toolCallTimeoutMs: 660000\n"
            b"        command: \"/tmp/foreign-python\"\n",
        )
        self.config.write_bytes(original)

        with self.assertRaisesRegex(ShellError, "canonical"):
            mcp_config.read_entry("deepseek-harness")
        with self.assertRaisesRegex(ShellError, "canonical"):
            mcp_config.remove_entry("deepseek-harness")
        self.assertEqual(self.config.read_bytes(), original)

    def test_symlink_config_is_refused_without_touching_target(self) -> None:
        target = self.tmp / "personal.yml"
        target.write_bytes(b"- id: personal\n")
        self.config.parent.mkdir(parents=True)
        self.config.symlink_to(target)

        with mock.patch.object(
            deepseek_harness.shutil,
            "which",
            return_value=str(self.tmp / "dsh"),
        ), mock.patch.object(
            deepseek_harness,
            "_cli_path",
            return_value=self.tmp / "dsh",
        ):
            with self.assertRaisesRegex(ShellError, "regular file"):
                mcp_config.write_entry(
                    "deepseek-harness",
                    python=sys.executable,
                    cwd=self.tmp / "managed-de",
                )

        self.assertEqual(target.read_bytes(), b"- id: personal\n")

    def test_concurrent_rewrite_guard_preserves_changed_file(self) -> None:
        original = b"- id: personal\n"
        changed = b"- id: changed-after-snapshot\n"
        self._write(original)
        native_atomic_write = mcp_config._atomic_write_text

        def race(path, text, **kwargs):
            Path(path).write_bytes(changed)
            return native_atomic_write(path, text, **kwargs)

        with mock.patch.object(mcp_config, "_atomic_write_text", side_effect=race):
            with self.assertRaisesRegex(ShellError, "changed"):
                mcp_config.write_entry(
                    "deepseek-harness",
                    python=sys.executable,
                    cwd=self.tmp / "managed-de",
                )

        self.assertEqual(self.config.read_bytes(), changed)

    def test_doctor_reports_ready_stale_and_unreadable(self) -> None:
        executable = self.tmp / "dsh"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        with mock.patch.object(
            deepseek_harness.shutil,
            "which",
            return_value=str(executable),
        ):
            mcp_config.write_entry(
                "deepseek-harness",
                python=sys.executable,
                cwd=self.tmp / "managed-de",
            )
        with mock.patch.object(mcp_config, "detect_clients", return_value=["deepseek-harness"]):
            ready = doctor.check_mcp_registered()
        self.assertEqual(ready.status, "PASS")
        self.assertIn("deepseek-harness", ready.detail)

        self.config.write_bytes(
            self.config.read_bytes().replace(b"toolCallTimeoutMs: 660000", b"toolCallTimeoutMs: 60000")
        )
        with mock.patch.object(mcp_config, "detect_clients", return_value=["deepseek-harness"]):
            stale = doctor.check_mcp_registered()
        self.assertEqual(stale.status, "WARN")
        self.assertIn("stale", stale.detail)

        with mock.patch.object(
            mcp_config,
            "entry_status",
            side_effect=ShellError("private path must not leak"),
        ), mock.patch.object(
            mcp_config,
            "detect_clients",
            return_value=["deepseek-harness"],
        ):
            unreadable = doctor.check_mcp_registered()
        self.assertEqual(unreadable.status, "WARN")
        self.assertIn("unreadable", unreadable.detail)
        self.assertNotIn("private path", unreadable.detail)

    def test_skill_route_is_idempotent_and_foreign_route_is_preserved(self) -> None:
        managed = self.tmp / "managed-de"
        source = managed / "skills" / "audit"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("managed\n", encoding="utf-8")
        self.dsh_home.mkdir(parents=True)
        self.config.write_text("[]\n", encoding="utf-8")

        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ):
            first = install.repair_client_integration("deepseek-harness", managed)
        route = self.dsh_home / "skills" / "audit"
        self.assertEqual(first["routed_skills"], ["audit"])
        self.assertTrue(install._route_points_to(route, source))
        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ):
            second = install.repair_client_integration("deepseek-harness", managed)
        self.assertEqual(second["routed_skills"], ["audit"])

        route.unlink()
        foreign = self.tmp / "foreign-audit"
        foreign.mkdir()
        route.symlink_to(foreign)
        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ):
            with self.assertRaisesRegex(ShellError, "outside this install"):
                install.repair_client_integration("deepseek-harness", managed)
        self.assertTrue(install._route_points_to(route, foreign))

    def test_observed_alias_matches_with_display_capabilities(self) -> None:
        identity = mcp_config.resolve_host_identity(
            "deepseek-harness",
            {"clientInfo": {"name": "dsh-mcp-client", "version": "0.2.1"}},
        )

        self.assertEqual(identity.status, "matched")
        self.assertTrue(identity.optional_features_enabled)
        self.assertTrue(mcp_config.host_supports_local_display("deepseek-harness"))

    def test_two_initialize_requests_keep_deepseek_harness_display_tools(self) -> None:
        from installer.tests.test_shim import FakeForwarder, _run

        responses = {
            "initialize": {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"serverInfo": {"name": "de"}},
            },
            "tools/list": {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {"tools": [{"name": "audit_skill_submit"}]},
            },
        }
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "dsh-mcp-client", "version": "0.2.1"}
            },
        }
        second_initialize = dict(initialize, id=2)
        tools_list = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}

        output = _run(
            FakeForwarder(responses=responses),
            [json.dumps(initialize), json.dumps(second_initialize), json.dumps(tools_list)],
            client_host="deepseek-harness",
        )

        self.assertEqual(len(output), 3)
        names = {tool["name"] for tool in output[-1]["result"]["tools"]}
        self.assertIn("audit_skill_submit", names)
        self.assertIn("audit_skill_complete", names)
        self.assertTrue(
            {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"} <= names
        )

    def test_shared_display_keeps_popup_followup_disabled(self) -> None:
        from installer import shim
        from installer.tests.test_shim import FakeForwarder, _run

        class _SyncThread:
            def __init__(self, target=None, args=(), **kwargs):
                self._target = target
                self._args = args

            def start(self):
                self._target(*self._args)

            def join(self, timeout=None):
                return None

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "dsh-mcp-client", "version": "0.2.1"}
            },
        }
        display = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "open_ge_popup",
                "arguments": {"run_id": "ge-run"},
            },
        }
        captured = []

        def handle(_forwarder, name, args, **kwargs):
            captured.append((name, args, kwargs))
            return {"status": "open", "popup_id": "deepseek-popup"}

        with mock.patch.object(shim, "_handle_display_call", side_effect=handle), \
             mock.patch.object(shim.threading, "Thread", _SyncThread):
            _run(
                FakeForwarder(responses={
                    "initialize": {"jsonrpc": "2.0", "id": 1, "result": {}},
                }),
                [json.dumps(initialize), json.dumps(display)],
                client_host="deepseek-harness",
            )

        self.assertEqual(captured, [(
            "open_ge_popup",
            {"run_id": "ge-run"},
            {
                "client_host": "deepseek-harness",
                "popup_followup": False,
                "popup_api_profile": "legacy",
            },
        )])

    def test_audit_submit_uses_shared_stop_panel(self) -> None:
        from installer import shim
        from installer.tests.test_shim import FakeForwarder, _run

        class _SyncThread:
            def __init__(self, target=None, args=(), **kwargs):
                self._target = target
                self._args = args

            def start(self):
                self._target(*self._args)

        response = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"run_id": "deepseek-run", "status": "queued"}),
                }],
            },
        }
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "dsh-mcp-client", "version": "0.2.1"}
            },
        }
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"title": "Harness audit", "content": "artifact"},
                },
            },
        }

        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, \
             mock.patch.object(shim.threading, "Thread", _SyncThread):
            _run(
                FakeForwarder(responses={
                    "initialize": {"jsonrpc": "2.0", "id": 1, "result": {}},
                    "tools/call": response,
                }),
                [json.dumps(initialize), json.dumps(request)],
                client_host="deepseek-harness",
            )

        spawn.assert_called_once_with(response)


if __name__ == "__main__":
    unittest.main()
