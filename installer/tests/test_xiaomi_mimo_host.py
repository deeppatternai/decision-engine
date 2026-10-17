from __future__ import annotations

import json
import os
import plistlib
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from installer import doctor, install, mcp_config
from installer.client_hosts.hosts import xiaomi_mimo
from installer.client_hosts.registry import CLIENT_SPECS
from installer.config import ShellError


class XiaomiMiMoHostContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.config_root = self.home / ".config" / "mimocode"
        self.config = self.config_root / "mimocode.jsonc"
        self.environment = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home)},
            clear=False,
        )
        self.environment.start()
        os.environ.pop("MIMOCODE_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
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

    def test_registry_declares_native_jsonc_display_and_user_skills(self) -> None:
        spec = CLIENT_SPECS["xiaomi-mimo"]

        self.assertEqual(spec.config_format, "jsonc")
        self.assertEqual(spec.config_renderer, "mimocode-jsonc-mcp-v1")
        self.assertEqual(spec.observed_client_aliases, frozenset({"mimocode"}))
        self.assertTrue(spec.require_observed_identity)
        self.assertEqual(
            spec.launcher_capabilities,
            frozenset({"core-mcp", "local-display", "audit-stop-panel"}),
        )
        self.assertTrue(spec.local_display_tools)
        self.assertFalse(spec.popup_followup)
        self.assertTrue(spec.audit_stop_panel)
        self.assertEqual(spec.ge_progress_keepalive_s, 15.0)
        self.assertEqual(spec.skills_global_path(), self.config_root / "skills")

    def test_cross_platform_installers_offer_named_xiaomi_mimo_selection(self) -> None:
        root = Path(__file__).resolve().parents[2]
        posix = (root / "dp-install.sh").read_text(encoding="utf-8")
        windows = (root / "dp-install.ps1").read_text(encoding="utf-8")

        self.assertIn("xiaomi-mimo) printf '%s' 'Xiaomi MiMo [xiaomi-mimo]'", posix)
        self.assertIn('"xiaomi-mimo" = "Xiaomi MiMo [xiaomi-mimo]"', windows)
        self.assertIn('"xiaomi-mimo"', windows)
        for source in (posix, windows):
            self.assertIn("install.complete_development_skill_route_root(", source)
            self.assertIn("replaceable_body_roots=replaceable_roots", source)

    def test_config_root_and_file_precedence(self) -> None:
        self.assertEqual(xiaomi_mimo.config_root(), self.config_root)
        self.assertEqual(xiaomi_mimo.config_path(), self.config)

        xdg = self.tmp / "xdg"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False):
            os.environ.pop("MIMOCODE_HOME", None)
            self.assertEqual(xiaomi_mimo.config_root(), xdg / "mimocode")

        mimo_home = self.tmp / "mimo-home"
        config_root = mimo_home / "config"
        config_root.mkdir(parents=True)
        for name in ("config.json", "mimocode.json", "mimocode.jsonc"):
            (config_root / name).write_text("{}\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"MIMOCODE_HOME": str(mimo_home), "XDG_CONFIG_HOME": str(xdg)},
            clear=False,
        ):
            self.assertEqual(xiaomi_mimo.config_root(), config_root)
            self.assertEqual(xiaomi_mimo.config_path(), config_root / "mimocode.jsonc")

        for name in ("MIMOCODE_HOME", "XDG_CONFIG_HOME"):
            with self.subTest(name=name), mock.patch.dict(
                os.environ, {name: "relative/path"}, clear=False
            ):
                if name == "XDG_CONFIG_HOME":
                    os.environ.pop("MIMOCODE_HOME", None)
                with self.assertRaisesRegex(ShellError, "absolute path"):
                    xiaomi_mimo.config_root()

    def test_installation_identity_requires_config_or_exact_macos_bundle(self) -> None:
        self.config_root.mkdir(parents=True)
        with mock.patch.object(xiaomi_mimo, "_macos_app_installed", return_value=False):
            self.assertFalse(xiaomi_mimo.installed())
            (self.config_root / "config.json").write_text("{}\n", encoding="utf-8")
            self.assertTrue(xiaomi_mimo.installed())
        (self.config_root / "config.json").unlink()

        applications = self.tmp / "Applications"
        info = applications / "Xiaomi MiMo.app" / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True)
        with info.open("wb") as handle:
            plistlib.dump({"CFBundleIdentifier": "com.example.foreign"}, handle)
        with mock.patch.object(xiaomi_mimo, "_macos_application_roots", return_value=(applications,)), mock.patch.object(
            xiaomi_mimo.sys, "platform", "darwin"
        ):
            self.assertFalse(xiaomi_mimo.installed())
            with info.open("wb") as handle:
                plistlib.dump({"CFBundleIdentifier": "com.xiaomi.mimo.desktop"}, handle)
            self.assertTrue(xiaomi_mimo.installed())

    def test_rendered_entry_uses_mimocode_native_shape(self) -> None:
        entry = mcp_config.expected_entry(
            "xiaomi-mimo",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
        )

        self.assertEqual(entry["type"], "local")
        self.assertEqual(
            entry["command"],
            [sys.executable, "-m", "installer.launcher"],
        )
        self.assertEqual(entry["environment"]["PYTHONPATH"], str(self.tmp / "managed-de"))
        self.assertEqual(entry["environment"]["DE_MCP_CLIENT_HOST"], "xiaomi-mimo")
        self.assertIs(entry["enabled"], True)
        self.assertEqual(set(entry), {"type", "command", "environment", "enabled"})

    def test_apply_repeat_and_remove_preserve_foreign_jsonc_bytes(self) -> None:
        original = (
            b"\xef\xbb\xbf{\r\n"
            b"  // synthetic provider credential must remain opaque\r\n"
            b'  "provider": {"token": "SYNTHETIC_SENTINEL"},\r\n'
            b'  "mcp": {\r\n'
            b'    "foreign": {"type": "remote", "url": "https://example.invalid"},\r\n'
            b"  },\r\n"
            b"}\r\n"
        )
        self._write(original)

        first = mcp_config.write_entry(
            "xiaomi-mimo", python=sys.executable, cwd=self.tmp / "managed-de"
        )
        applied = self.config.read_bytes()
        self.assertEqual(first["action"], "added")
        self.assertIn(b'"decision-engine"', applied)
        self.assertIn(b'"SYNTHETIC_SENTINEL"', applied)
        self.assertNotIn(b"\n", applied.replace(b"\r\n", b""))
        self.assertEqual(mcp_config.entry_status("xiaomi-mimo"), "ready")

        second = mcp_config.write_entry(
            "xiaomi-mimo", python=sys.executable, cwd=self.tmp / "managed-de"
        )
        self.assertEqual(second["action"], "unchanged")
        self.assertEqual(self.config.read_bytes(), applied)

        removed = mcp_config.remove_entry("xiaomi-mimo")
        self.assertEqual(removed["action"], "removed")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIsNone(mcp_config.read_entry("xiaomi-mimo"))

    def test_missing_mcp_round_trip_restores_original_bytes(self) -> None:
        original = b'{\n  // keep\n  "theme": "dark",\n}\n'
        self._write(original)

        mcp_config.write_entry(
            "xiaomi-mimo", python=sys.executable, cwd=self.tmp / "managed-de"
        )
        mcp_config.remove_entry("xiaomi-mimo")

        self.assertEqual(self.config.read_bytes(), original)

    def test_absent_and_empty_files_round_trip_without_backups(self) -> None:
        for original in (None, b""):
            with self.subTest(original=original):
                if self.config.exists():
                    self.config.unlink()
                if original is not None:
                    self._write(original)
                with mock.patch.object(xiaomi_mimo, "installed", return_value=True):
                    written = mcp_config.write_entry(
                        "xiaomi-mimo",
                        python=sys.executable,
                        cwd=self.tmp / "managed-de",
                    )
                removed = mcp_config.remove_entry(
                    "xiaomi-mimo", create_backup=False
                )
                self.assertIsNone(written["backup"])
                self.assertIsNone(removed["backup"])
                if original is None:
                    self.assertFalse(self.config.exists())
                else:
                    self.assertEqual(self.config.read_bytes(), original)
                self.assertEqual(list(self.config_root.glob("*.de-bak.*")), [])

    def test_dry_run_never_writes_or_exposes_provider_value(self) -> None:
        original = b'{"provider":{"token":"SYNTHETIC_SENTINEL"}}\n'
        self._write(original)

        result = mcp_config.write_entry(
            "xiaomi-mimo",
            python=sys.executable,
            cwd=self.tmp / "managed-de",
            dry_run=True,
        )

        self.assertEqual(result["action"], "added (dry-run)")
        self.assertNotIn("SYNTHETIC_SENTINEL", repr(result))
        self.assertEqual(self.config.read_bytes(), original)

    def test_stale_managed_entry_updates_without_rewriting_foreign_bytes(self) -> None:
        original = b'{\n  "provider": {"token": "SYNTHETIC_SENTINEL"},\n}\n'
        self._write(original)
        first_python = str(self.tmp / "python-one")
        second_python = str(self.tmp / "python-two")

        with mock.patch.object(mcp_config, "_interpreter_gate"):
            mcp_config.write_entry(
                "xiaomi-mimo", python=first_python, cwd=self.tmp / "managed-de"
            )
            updated = mcp_config.write_entry(
                "xiaomi-mimo", python=second_python, cwd=self.tmp / "managed-de"
            )

        self.assertEqual(updated["action"], "updated")
        self.assertIn(b'"SYNTHETIC_SENTINEL"', self.config.read_bytes())
        self.assertEqual(
            mcp_config.read_entry("xiaomi-mimo")["command"][0], second_python
        )
        mcp_config.remove_entry("xiaomi-mimo", create_backup=False)
        self.assertEqual(self.config.read_bytes(), original)

    def test_oversized_config_is_refused_without_reading_provider_state(self) -> None:
        original = b'{"provider":"SYNTHETIC_SENTINEL"}\n'
        self._write(original)

        with mock.patch.object(mcp_config, "MAX_AGENT_CONFIG_BYTES", 8):
            with self.assertRaisesRegex(ShellError, "too large") as caught:
                mcp_config.write_entry(
                    "xiaomi-mimo",
                    python=sys.executable,
                    cwd=self.tmp / "managed-de",
                )

        self.assertNotIn("SYNTHETIC_SENTINEL", str(caught.exception))
        self.assertEqual(self.config.read_bytes(), original)

    def test_concurrent_rewrite_is_refused_without_overwriting_new_bytes(self) -> None:
        original = b'{"provider":"before"}\n'
        changed = b'{"provider":"after"}\n'
        self._write(original)
        native_atomic_write = mcp_config._atomic_write_text

        def race(path, text, **kwargs):
            Path(path).write_bytes(changed)
            return native_atomic_write(path, text, **kwargs)

        with mock.patch.object(mcp_config, "_atomic_write_text", side_effect=race):
            with self.assertRaisesRegex(ShellError, "changed"):
                mcp_config.write_entry(
                    "xiaomi-mimo",
                    python=sys.executable,
                    cwd=self.tmp / "managed-de",
                )

        self.assertEqual(self.config.read_bytes(), changed)

    def test_doctor_reports_ready_stale_and_redacted_unreadable(self) -> None:
        self._write(b"{}\n")
        mcp_config.write_entry(
            "xiaomi-mimo", python=sys.executable, cwd=self.tmp / "managed-de"
        )
        with mock.patch.object(mcp_config, "detect_clients", return_value=["xiaomi-mimo"]):
            ready = doctor.check_mcp_registered()
        self.assertEqual(ready.status, "PASS")
        self.assertIn("xiaomi-mimo", ready.detail)

        self.config.write_bytes(
            self.config.read_bytes().replace(b'"enabled": true', b'"enabled": false')
        )
        with mock.patch.object(mcp_config, "detect_clients", return_value=["xiaomi-mimo"]):
            stale = doctor.check_mcp_registered()
        self.assertEqual(stale.status, "WARN")
        self.assertIn("stale", stale.detail)

        with mock.patch.object(
            mcp_config,
            "entry_status",
            side_effect=ShellError("SYNTHETIC_PRIVATE_PROVIDER_VALUE"),
        ), mock.patch.object(
            mcp_config,
            "detect_clients",
            return_value=["xiaomi-mimo"],
        ):
            unreadable = doctor.check_mcp_registered()
        self.assertEqual(unreadable.status, "WARN")
        self.assertIn("unreadable", unreadable.detail)
        self.assertNotIn("SYNTHETIC_PRIVATE_PROVIDER_VALUE", unreadable.detail)

    def test_foreign_same_name_is_refused_without_mutation_or_secret_output(self) -> None:
        original = (
            b'{\n  "provider": {"token": "SYNTHETIC_SENTINEL"},\n'
            b'  "mcp": {"decision-engine": {"type": "local", "command": ["foreign"]}}\n}\n'
        )
        self._write(original)

        with self.assertRaisesRegex(ShellError, "same_name_unowned") as caught:
            mcp_config.write_entry(
                "xiaomi-mimo", python=sys.executable, cwd=self.tmp / "managed-de"
            )

        self.assertNotIn("SYNTHETIC_SENTINEL", str(caught.exception))
        self.assertEqual(self.config.read_bytes(), original)

    def test_duplicate_or_malformed_jsonc_is_refused_without_mutation(self) -> None:
        fixtures = (
            b'{"mcp": {}, "mcp": {}}\n',
            b'{"provider": {/* unterminated}\n',
        )
        for original in fixtures:
            with self.subTest(original=original):
                self._write(original)
                with self.assertRaises(ShellError):
                    mcp_config.write_entry(
                        "xiaomi-mimo",
                        python=sys.executable,
                        cwd=self.tmp / "managed-de",
                    )
                self.assertEqual(self.config.read_bytes(), original)

    def test_symlink_config_is_refused_without_touching_target(self) -> None:
        target = self.tmp / "foreign.jsonc"
        target.write_bytes(b"{}\n")
        self.config.parent.mkdir(parents=True)
        self.config.symlink_to(target)

        with mock.patch.object(xiaomi_mimo, "installed", return_value=True):
            with self.assertRaisesRegex(ShellError, "regular file"):
                mcp_config.write_entry(
                    "xiaomi-mimo",
                    python=sys.executable,
                    cwd=self.tmp / "managed-de",
                )

        self.assertEqual(target.read_bytes(), b"{}\n")

    def test_observed_alias_controls_shared_display_capabilities(self) -> None:
        matched = mcp_config.resolve_host_identity(
            "xiaomi-mimo",
            {"clientInfo": {"name": "mimocode", "version": "26.1010"}},
        )
        unknown = mcp_config.resolve_host_identity(
            "xiaomi-mimo",
            {"clientInfo": {"name": "foreign", "version": "1"}},
        )

        self.assertEqual(matched.status, "matched")
        self.assertTrue(matched.optional_features_enabled)
        self.assertFalse(unknown.optional_features_enabled)
        self.assertTrue(mcp_config.host_supports_local_display("xiaomi-mimo"))

    def test_skill_route_is_idempotent_and_foreign_route_is_preserved(self) -> None:
        managed = self.tmp / "managed-de"
        source = managed / "skills" / "audit"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("managed\n", encoding="utf-8")
        self._write(b"{}\n")

        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ):
            first = install.repair_client_integration("xiaomi-mimo", managed)
            second = install.repair_client_integration("xiaomi-mimo", managed)
        route = self.config_root / "skills" / "audit"
        self.assertEqual(first["routed_skills"], ["audit"])
        self.assertEqual(second["routed_skills"], ["audit"])
        self.assertTrue(install._route_points_to(route, source))

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
                install.repair_client_integration("xiaomi-mimo", managed)
        self.assertTrue(install._route_points_to(route, foreign))

    def test_complete_developer_skill_routes_migrate_to_managed_root(self) -> None:
        managed = self.tmp / "managed-de"
        legacy = self.tmp / "developer-checkout"
        for root in (managed, legacy):
            (root / "skills" / "audit").mkdir(parents=True)
            (root / "skills" / "layer-check").mkdir()
        (legacy / ".git").write_text("gitdir: synthetic\n", encoding="utf-8")
        (legacy / "installer").mkdir()
        for name in ("launcher.py", "mcp_config.py"):
            (legacy / "installer" / name).write_text("# synthetic\n", encoding="utf-8")
        self._write(b"{}\n")
        mcp_config.write_entry(
            "xiaomi-mimo", python=sys.executable, dev_root=legacy
        )
        destination = self.config_root / "skills"
        destination.mkdir()
        for name in ("audit", "layer-check"):
            (destination / name).symlink_to(legacy / "skills" / name)
        retired = legacy / "skills" / "retired-de-skill"
        retired.mkdir()
        (destination / retired.name).symlink_to(retired)

        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ):
            result = install.repair_client_integration("xiaomi-mimo", managed)

        self.assertEqual(result["mcp"]["action"], "updated")
        self.assertEqual(set(result["routed_skills"]), {"audit", "layer-check"})
        for name in ("audit", "layer-check"):
            self.assertTrue(
                install._route_points_to(destination / name, managed / "skills" / name)
            )
        self.assertFalse((destination / retired.name).exists())

    def test_incomplete_developer_skill_routes_remain_fail_closed(self) -> None:
        managed = self.tmp / "managed-de"
        legacy = self.tmp / "developer-checkout"
        for root in (managed, legacy):
            (root / "skills" / "audit").mkdir(parents=True)
            (root / "skills" / "layer-check").mkdir()
        (legacy / ".git").write_text("gitdir: synthetic\n", encoding="utf-8")
        (legacy / "installer").mkdir()
        for name in ("launcher.py", "mcp_config.py"):
            (legacy / "installer" / name).write_text("# synthetic\n", encoding="utf-8")
        self._write(b"{}\n")
        destination = self.config_root / "skills"
        destination.mkdir()
        (destination / "audit").symlink_to(legacy / "skills" / "audit")

        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ), self.assertRaisesRegex(ShellError, "outside this install"):
            install.repair_client_integration("xiaomi-mimo", managed)

        self.assertTrue(
            install._route_points_to(
                destination / "audit", legacy / "skills" / "audit"
            )
        )

    def test_mixed_managed_and_developer_routes_complete_interrupted_migration(self) -> None:
        incoming = self.tmp / "decision-engine"
        managed = self.tmp / ".deeppattern" / "decision-engine"
        legacy = self.tmp / "developer-checkout"
        for root in (incoming, managed, legacy):
            (root / "skills" / "audit").mkdir(parents=True)
            (root / "skills" / "layer-check").mkdir()
        (legacy / ".git").write_text("gitdir: synthetic\n", encoding="utf-8")
        (legacy / "installer").mkdir()
        for name in ("launcher.py", "mcp_config.py"):
            (legacy / "installer" / name).write_text("# synthetic\n", encoding="utf-8")
        self._write(b"{}\n")
        destination = self.config_root / "skills"
        destination.mkdir()
        (destination / "audit").symlink_to(managed / "skills" / "audit")
        (destination / "layer-check").symlink_to(legacy / "skills" / "layer-check")

        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ), mock.patch.object(
            mcp_config,
            "active_skill_routes",
            return_value={"xiaomi-mimo": (destination, frozenset())},
        ):
            result = install.install_component("decision-engine", self.tmp)

        self.assertFalse(result["routing_failures"])
        for name in ("audit", "layer-check"):
            self.assertTrue(
                install._route_points_to(destination / name, managed / "skills" / name)
            )

    def test_symlinked_skill_in_developer_root_remains_fail_closed(self) -> None:
        managed = self.tmp / "managed-de"
        legacy = self.tmp / "developer-checkout"
        foreign = self.tmp / "foreign-skill"
        (managed / "skills" / "audit").mkdir(parents=True)
        foreign.mkdir()
        (legacy / "skills").mkdir(parents=True)
        (legacy / "skills" / "audit").symlink_to(foreign)
        (legacy / ".git").write_text("gitdir: synthetic\n", encoding="utf-8")
        (legacy / "installer").mkdir()
        for name in ("launcher.py", "mcp_config.py"):
            (legacy / "installer" / name).write_text("# synthetic\n", encoding="utf-8")
        self._write(b"{}\n")
        destination = self.config_root / "skills"
        destination.mkdir()
        route = destination / "audit"
        route.symlink_to(legacy / "skills" / "audit")

        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ), self.assertRaisesRegex(ShellError, "outside this install"):
            install.repair_client_integration("xiaomi-mimo", managed)

        self.assertTrue(install._route_points_to(route, legacy / "skills" / "audit"))

    def test_symlinked_parent_of_developer_root_remains_fail_closed(self) -> None:
        managed = self.tmp / "managed-de"
        actual_parent = self.tmp / "actual-parent"
        linked_parent = self.tmp / "linked-parent"
        legacy = linked_parent / "developer-checkout"
        (managed / "skills" / "audit").mkdir(parents=True)
        (actual_parent / "developer-checkout" / "skills" / "audit").mkdir(
            parents=True
        )
        (actual_parent / "developer-checkout" / ".git").write_text(
            "gitdir: synthetic\n", encoding="utf-8"
        )
        (actual_parent / "developer-checkout" / "installer").mkdir()
        for name in ("launcher.py", "mcp_config.py"):
            (actual_parent / "developer-checkout" / "installer" / name).write_text(
                "# synthetic\n", encoding="utf-8"
            )
        linked_parent.symlink_to(actual_parent, target_is_directory=True)
        self._write(b"{}\n")
        destination = self.config_root / "skills"
        destination.mkdir()
        route = destination / "audit"
        route.symlink_to(legacy / "skills" / "audit")

        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(self.tmp / ".deeppattern")},
            clear=False,
        ), self.assertRaisesRegex(ShellError, "outside this install"):
            install.repair_client_integration("xiaomi-mimo", managed)

        self.assertTrue(install._route_points_to(route, legacy / "skills" / "audit"))

    def test_two_initializes_keep_shared_display_tools_without_popup_followup(self) -> None:
        from installer.tests.test_shim import FakeForwarder, _run

        responses = {
            "initialize": {"jsonrpc": "2.0", "id": 1, "result": {}},
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
            "params": {"clientInfo": {"name": "mimocode", "version": "26.1010"}},
        }
        output = _run(
            FakeForwarder(responses=responses),
            [
                json.dumps(initialize),
                json.dumps(dict(initialize, id=2)),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}),
            ],
            client_host="xiaomi-mimo",
        )

        names = {tool["name"] for tool in output[-1]["result"]["tools"]}
        self.assertTrue(
            {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"} <= names
        )
        self.assertFalse(CLIENT_SPECS["xiaomi-mimo"].popup_followup)

    def test_graphic_display_reuses_shared_handler_without_popup_followup(self) -> None:
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
            "params": {"clientInfo": {"name": "mimocode", "version": "26.1010"}},
        }
        display = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "open_ge_popup", "arguments": {"run_id": "mimo-ge"}},
        }
        captured = []

        def handle(_forwarder, name, args, **kwargs):
            captured.append((name, args, kwargs))
            return {"status": "open", "popup_id": "mimo-popup"}

        with mock.patch.object(shim, "_handle_display_call", side_effect=handle), mock.patch.object(
            shim.threading, "Thread", _SyncThread
        ):
            _run(
                FakeForwarder(
                    responses={"initialize": {"jsonrpc": "2.0", "id": 1, "result": {}}}
                ),
                [json.dumps(initialize), json.dumps(display)],
                client_host="xiaomi-mimo",
            )

        self.assertEqual(
            captured,
            [
                (
                    "open_ge_popup",
                    {"run_id": "mimo-ge"},
                    {
                        "client_host": "xiaomi-mimo",
                        "popup_followup": False,
                        "popup_api_profile": "legacy",
                    },
                )
            ],
        )

    def test_open_ge_worker_receives_mimo_progress_keepalive_policy(self) -> None:
        from installer import shim
        from installer.tests.test_shim import FakeForwarder, _run

        captured = []

        class _SyncThread:
            def __init__(self, target=None, args=(), **kwargs):
                self._target = target
                self._args = args

            def start(self):
                self._target(*self._args)

            def join(self, timeout=None):
                return None

        def dispatch(*args):
            captured.append(args)

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "mimocode", "version": "26.1014"}},
        }
        request = {
            "jsonrpc": "2.0",
            "id": 17,
            "method": "tools/call",
            "params": {
                "name": "open_ge",
                "arguments": {"mode": "diagram", "spec": {}},
                "_meta": {"progressToken": "mimo-progress-17"},
            },
        }
        with mock.patch.object(shim, "_dispatch_display", side_effect=dispatch), mock.patch.object(
            shim.threading, "Thread", _SyncThread
        ):
            _run(
                FakeForwarder(
                    responses={
                        "initialize": {"jsonrpc": "2.0", "id": 1, "result": {}}
                    }
                ),
                [json.dumps(initialize), json.dumps(request)],
                client_host="xiaomi-mimo",
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1], 17)
        self.assertEqual(captured[0][2], request["params"])
        self.assertEqual(captured[0][-1], 15.0)

    def test_audit_submit_reuses_shared_stop_panel(self) -> None:
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
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"run_id": "mimo-audit", "status": "queued"}),
                    }
                ]
            },
        }
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "mimocode", "version": "26.1010"}},
        }
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"title": "MiMo audit", "content": "artifact"},
                },
            },
        }

        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, mock.patch.object(
            shim.threading, "Thread", _SyncThread
        ):
            _run(
                FakeForwarder(
                    responses={
                        "initialize": {"jsonrpc": "2.0", "id": 1, "result": {}},
                        "tools/call": response,
                    }
                ),
                [json.dumps(initialize), json.dumps(request)],
                client_host="xiaomi-mimo",
            )

        spawn.assert_called_once_with(response)


if __name__ == "__main__":
    unittest.main()
