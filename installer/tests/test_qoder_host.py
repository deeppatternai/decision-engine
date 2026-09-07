"""Behavior locks for the Alibaba Qoder Desktop host adapter."""

from __future__ import annotations

import json
import ntpath
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import mcp_config
from installer.config import ShellError


class QoderHostTestCase(unittest.TestCase):
    def _write_product(
        self,
        app_root: Path,
        *,
        application_name: str = "qoder",
        version: str = "1.106.3",
    ) -> None:
        product = app_root / "resources" / "app" / "product.json"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text(
            json.dumps(
                {
                    "applicationName": application_name,
                    "version": version,
                }
            ),
            encoding="utf-8",
        )

    def test_qoder_spec_matches_observed_desktop_contract(self):
        spec = mcp_config.CLIENT_SPECS["qoder"]

        self.assertEqual(spec.host_family, "qoder")
        self.assertEqual(spec.config_renderer, "json-mcp-v1")
        self.assertEqual(spec.launch_policy, "absolute-bootstrap-v1")
        self.assertEqual(
            spec.observed_client_aliases,
            frozenset({"mcphost", "qoder-desktop-mcp-host"}),
        )
        self.assertTrue(spec.require_observed_identity)
        self.assertEqual(spec.skills_project_paths, (".qoder/skills",))
        self.assertEqual(spec.skill_delivery_mode, "managed-copy")
        self.assertEqual(spec.routing_kind, "skill")
        self.assertEqual(spec.skill_route_name, "qoder")
        self.assertTrue(spec.repair_skills_on_setup)
        self.assertTrue(spec.skills_require_managed_target)
        self.assertEqual(
            spec.doctor_capabilities,
            frozenset({"mcp-entry", "skills", "workspace-shadow"}),
        )
        self.assertEqual(
            spec.launcher_capabilities,
            frozenset({"core-mcp", "local-display", "audit-stop-panel"}),
        )
        self.assertTrue(spec.local_display_tools)
        self.assertFalse(spec.popup_followup)
        self.assertTrue(spec.audit_stop_panel)

    def test_qoder_paths_are_desktop_owned_and_environment_overridable(self):
        from installer.client_hosts.hosts import qoder

        with mock.patch.dict(
            os.environ,
            {
                "QODER_CONFIG": "",
                "QODER_SKILLS_DIR": r"C:\isolated\qoder-skills",
            },
            clear=False,
        ), mock.patch.object(Path, "home", return_value=Path(r"C:\Users\test")):
            with mock.patch.object(qoder.sys, "platform", "win32"):
                self.assertEqual(
                    mcp_config.agent_config_path("qoder"),
                    Path(r"C:\Users\test") / ".qoder" / "mcp.json",
                )
            with mock.patch.object(qoder.sys, "platform", "darwin"):
                self.assertEqual(
                    mcp_config.agent_config_path("qoder"),
                    Path(r"C:\Users\test") / ".qoder" / "settings.json",
                )
            self.assertEqual(
                mcp_config.CLIENT_SPECS["qoder"].skills_global_path(),
                Path(r"C:\isolated\qoder-skills"),
            )

    def test_qoder_restart_uses_settings_source_not_legacy_mcp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "managed"
            legacy = home / ".qoder" / "mcp.json"
            settings = home / ".qoder" / "settings.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                json.dumps(mcp_config.render_entry(client="qoder", cwd=root)),
                encoding="utf-8",
            )
            settings.write_text(
                json.dumps({"mcpServers": {}}),
                encoding="utf-8",
            )
            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ,
                {"QODER_CONFIG": ""},
                clear=False,
            ):
                self.assertIsNone(mcp_config.read_entry("qoder"))

    def test_qoder_write_updates_desktop_settings_and_preserves_other_fields(self):
        from installer.client_hosts.hosts import qoder

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "managed"
            settings = home / ".qoder" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps({"enabledPlugins": {"keep": True}}),
                encoding="utf-8",
            )
            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ,
                {"QODER_CONFIG": ""},
                clear=False,
            ), mock.patch.object(qoder, "_qoder_config_write_guard", return_value=None):
                result = mcp_config.write_entry("qoder", dev_root=root)

            data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(result["action"], "added")
        self.assertTrue(data["enabledPlugins"]["keep"])
        self.assertIn("decision-engine", data["mcpServers"])
        prompt_hook = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(prompt_hook["name"], "decision-engine-audit-routing-v1")
        self.assertEqual(
            prompt_hook["args"],
            [str(root / "installer" / "qoder_audit_prompt_hook.py")],
        )

    def test_qoder_hook_shape_conflict_aborts_the_combined_config_write(self):
        from installer.client_hosts.hosts import qoder

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "managed"
            settings = home / ".qoder" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps({"hooks": {"UserPromptSubmit": "user-owned"}}),
                encoding="utf-8",
            )
            before = settings.read_bytes()
            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ,
                {"QODER_CONFIG": ""},
                clear=False,
            ), mock.patch.object(qoder, "_qoder_config_write_guard", return_value=None):
                with self.assertRaisesRegex(ShellError, "qoder_hooks_invalid"):
                    mcp_config.write_entry("qoder", dev_root=root)

            self.assertEqual(settings.read_bytes(), before)

    def test_windows_qoder_mcp_file_does_not_receive_macos_prompt_hooks(self):
        from installer.client_hosts.hosts import qoder

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(r"C:\managed")
            config_path = home / ".qoder" / "mcp.json"
            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ,
                {"QODER_CONFIG": ""},
                clear=False,
            ), mock.patch.object(qoder, "_qoder_config_write_guard", return_value=None), mock.patch.object(
                mcp_config.sys, "platform", "win32"
            ):
                mcp_config.write_entry("qoder", dev_root=root)

            data = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIn("decision-engine", data["mcpServers"])
            self.assertNotIn("hooks", data)

    def test_qoder_renderer_matches_verified_stdio_shape(self):
        python_with_spaces = r"C:\Program Files\Python313\python.exe"
        root = Path(r"C:\managed root")
        with (
            mock.patch.object(mcp_config.sys, "platform", "win32"),
            mock.patch.object(mcp_config.sys, "executable", python_with_spaces),
        ):
            entry = mcp_config.render_entry(client="qoder", cwd=root)[
                "mcpServers"
            ]["decision-engine"]

        self.assertEqual(set(entry), {"command", "args", "env"})
        self.assertEqual(entry["command"], python_with_spaces)
        self.assertEqual(
            entry["args"][0],
            ntpath.join(str(root), "installer", "mcp_bootstrap.py"),
        )
        self.assertEqual(entry["args"][1], str(root))
        self.assertEqual(entry["args"][-2:], ["--managed-root", str(root)])
        self.assertFalse(any(";" in argument for argument in entry["args"]))
        self.assertEqual(entry["env"]["PYTHONPATH"], str(root))
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], "qoder")
        self.assertNotIn("type", entry)
        self.assertNotIn("cwd", entry)

    def test_qoder_guard_is_bounded_by_product_identity_and_version(self):
        from installer.client_hosts.hosts import qoder

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "app"
            skills = root / ".qoder" / "skills"
            environment = {
                "QODER_APP_ROOT": str(app_root),
                "QODER_SKILLS_DIR": str(skills),
            }
            with (
                mock.patch.object(qoder.sys, "platform", "win32"),
                mock.patch.dict(os.environ, environment, clear=False),
            ):
                with self.assertRaisesRegex(ShellError, "qoder_not_installed"):
                    qoder._qoder_config_write_guard()
                self.assertFalse(qoder._qoder_skills_in_use())

                self._write_product(app_root, application_name="another-product")
                with self.assertRaisesRegex(ShellError, "qoder_identity_mismatch"):
                    qoder._qoder_config_write_guard()

                self._write_product(app_root, version="1.106.2")
                with self.assertRaisesRegex(ShellError, "unsupported_qoder_version"):
                    qoder._qoder_config_write_guard()
                self.assertFalse(qoder._qoder_skills_in_use())

                self._write_product(app_root)
                skills.parent.mkdir(parents=True)
                self.assertIsNone(qoder._qoder_config_write_guard())
                self.assertTrue(qoder._qoder_skills_in_use())

                self._write_product(app_root, version="1.106.4")
                self.assertEqual(
                    qoder._qoder_config_write_guard(),
                    "qoder_version_newer_than_tested",
                )
                self.assertTrue(qoder._qoder_skills_in_use())

                with mock.patch.object(qoder.sys, "platform", "linux"):
                    with self.assertRaisesRegex(
                        ShellError, "unsupported_qoder_platform"
                    ):
                        qoder._qoder_config_write_guard()
                    self.assertFalse(qoder._qoder_skills_in_use())

    def test_qoder_malformed_product_metadata_is_refused(self):
        from installer.client_hosts.hosts import qoder

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            product = app_root / "resources" / "app" / "product.json"
            product.parent.mkdir(parents=True)
            product.write_text(
                '{"applicationName":"qoder","version":"broken"}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(qoder.sys, "platform", "win32"),
                mock.patch.dict(
                    os.environ,
                    {"QODER_APP_ROOT": str(app_root)},
                    clear=False,
                ),
                self.assertRaisesRegex(ShellError, "qoder_metadata_invalid"),
            ):
                qoder._qoder_config_write_guard()

    def test_qoder_macos_bundle_matches_the_installed_qoder_app_contract(self):
        from installer.client_hosts.hosts import qoder

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "Qoder.app"
            product = app_root / "Contents" / "Resources" / "product.json"
            info = app_root / "Contents" / "Info.plist"
            product.parent.mkdir(parents=True)
            product.write_text(
                json.dumps(
                    {
                        "productId": "qoder",
                        "channel": "stable",
                        "displayName": "Qoder",
                    }
                ),
                encoding="utf-8",
            )
            info.parent.mkdir(parents=True, exist_ok=True)
            info.write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.qoder.app",
                        "CFBundleShortVersionString": "0.1.4",
                        "CFBundleVersion": "0.1.4",
                    }
                )
            )
            with mock.patch.object(qoder.sys, "platform", "darwin"), mock.patch.dict(
                os.environ,
                {"QODER_APP_ROOT": str(app_root)},
                clear=False,
            ):
                self.assertIsNone(qoder._qoder_config_write_guard())
                self.assertTrue(qoder._qoder_installed())
                self.assertEqual(qoder._qoder_version(), (0, 1, 4))

    def test_qoder_macos_bundle_requires_both_product_and_bundle_identity(self):
        from installer.client_hosts.hosts import qoder

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "Qoder.app"
            product = app_root / "Contents" / "Resources" / "product.json"
            info = app_root / "Contents" / "Info.plist"
            product.parent.mkdir(parents=True)
            product.write_text('{"productId":"not-qoder"}', encoding="utf-8")
            info.parent.mkdir(parents=True, exist_ok=True)
            info.write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.qoder.app",
                        "CFBundleShortVersionString": "0.1.3",
                    }
                )
            )
            with mock.patch.object(qoder.sys, "platform", "darwin"), mock.patch.dict(
                os.environ,
                {"QODER_APP_ROOT": str(app_root)},
                clear=False,
            ):
                with self.assertRaisesRegex(ShellError, "qoder_identity_mismatch"):
                    qoder._qoder_config_write_guard()

            product.write_text('{"productId":"qoder"}', encoding="utf-8")
            info.write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.other.app",
                        "CFBundleShortVersionString": "0.1.3",
                    }
                )
            )
            with mock.patch.object(qoder.sys, "platform", "darwin"), mock.patch.dict(
                os.environ,
                {"QODER_APP_ROOT": str(app_root)},
                clear=False,
            ):
                with self.assertRaisesRegex(ShellError, "qoder_identity_mismatch"):
                    qoder._qoder_config_write_guard()

    def test_qoder_doctor_skill_route_requires_its_mcp_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "app"
            config_path = root / ".qoder" / "mcp.json"
            skills = root / ".qoder" / "skills"
            self._write_product(app_root)
            with mock.patch.dict(
                os.environ,
                {
                    "QODER_APP_ROOT": str(app_root),
                    "QODER_CONFIG": str(config_path),
                    "QODER_SKILLS_DIR": str(skills),
                },
                clear=False,
            ):
                self.assertNotIn(
                    "qoder", mcp_config.active_skill_routes(for_doctor=True)
                )
                config_path.parent.mkdir(parents=True)
                config_path.write_text(
                    json.dumps(mcp_config.render_entry(client="qoder", cwd=root)),
                    encoding="utf-8",
                )
                route, excluded = mcp_config.active_skill_routes(
                    for_doctor=True
                )["qoder"]

        self.assertEqual(route, skills)
        self.assertEqual(excluded, frozenset())

    def test_absolute_bootstrap_binds_import_to_its_checkout(self):
        from installer import launcher, mcp_bootstrap

        root = Path(mcp_bootstrap.__file__).resolve().parents[1]
        original_path_zero = sys.path[0]
        try:
            with mock.patch.object(launcher, "main", return_value=7) as run:
                result = mcp_bootstrap.main(
                    [str(root), "--dev-root", str(root)]
                )
        finally:
            sys.path[0] = original_path_zero

        self.assertEqual(result, 7)
        run.assert_called_once_with(["--dev-root", str(root)])

    def test_absolute_bootstrap_rejects_a_different_checkout_root(self):
        from installer import mcp_bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                mcp_bootstrap.main([tmp, "--dev-root", tmp]),
                2,
            )

    def test_absolute_bootstrap_revalidates_persisted_mode_root_and_arity(self):
        from installer import launcher, mcp_bootstrap

        root = Path(mcp_bootstrap.__file__).resolve().parents[1]
        original_path_zero = sys.path[0]
        invalid = (
            [str(root), "--root", str(root)],
            [str(root), "--dev-root", str(root) + "-other"],
            [str(root), "--dev-root", str(root), "extra"],
        )
        try:
            with mock.patch.object(launcher, "main", return_value=7) as run:
                for arguments in invalid:
                    with self.subTest(arguments=arguments):
                        self.assertEqual(mcp_bootstrap.main(arguments), 2)
        finally:
            sys.path[0] = original_path_zero

        run.assert_not_called()

    def test_absolute_bootstrap_binds_import_in_a_fresh_hostile_process(self):
        from installer import mcp_bootstrap

        source = Path(mcp_bootstrap.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bound = base / "bound"
            hostile = base / "hostile"
            for root, exit_code in ((bound, 23), (hostile, 41)):
                package = root / "installer"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("", encoding="utf-8")
                (package / "launcher.py").write_text(
                    "def main(arguments):\n    return %d\n" % exit_code,
                    encoding="utf-8",
                )
            bootstrap = bound / "installer" / "mcp_bootstrap.py"
            bootstrap.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(hostile)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(bootstrap),
                    str(bound),
                    "--dev-root",
                    str(bound),
                ],
                cwd=hostile,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 23, completed.stderr.decode())


if __name__ == "__main__":
    unittest.main()
