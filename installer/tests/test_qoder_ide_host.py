"""Behavior locks for the independent macOS Qoder IDE host adapters."""

from __future__ import annotations

import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import mcp_config
from installer.config import ShellError


class QoderIdeHostTestCase(unittest.TestCase):
    CASES = (
        (
            "qoder-ide",
            "qoder_ide",
            "Qoder IDE.app",
            "com.qoder.ide",
            "qoder",
            ".qoder",
            "QODER_IDE_APP_ROOT",
            "QODER_IDE_CONFIG",
            "QODER_IDE_SETTINGS",
            "QODER_IDE_SKILLS_DIR",
            "qoder",
        ),
        (
            "qoder-cn-ide",
            "qoder_cn_ide",
            "Qoder CN IDE.app",
            "com.aliyun.lingma.ide",
            "qoder-cn",
            ".qoder-cn",
            "QODER_CN_IDE_APP_ROOT",
            "QODER_CN_IDE_CONFIG",
            "QODER_CN_IDE_SETTINGS",
            "QODER_CN_IDE_SKILLS_DIR",
            "qoder cn",
        ),
    )

    def _module(self, name: str):
        if name == "qoder_ide":
            from installer.client_hosts.hosts import qoder_ide

            return qoder_ide
        from installer.client_hosts.hosts import qoder_cn_ide

        return qoder_cn_ide

    def _write_bundle(
        self,
        app_root: Path,
        *,
        application_name: str,
        bundle_id: str,
        product_version: str = "1.106.3",
        bundle_version: str = "1.28.0",
    ) -> None:
        product = app_root / "Contents" / "Resources" / "app" / "product.json"
        info = app_root / "Contents" / "Info.plist"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text(
            json.dumps(
                {
                    "applicationName": application_name,
                    "dataFolderName": ".fixture",
                    "version": product_version,
                }
            ),
            encoding="utf-8",
        )
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleShortVersionString": bundle_version,
                }
            )
        )

    def test_ide_specs_are_independent_identity_gated_hosts(self):
        desktop_aliases = (
            mcp_config.CLIENT_SPECS["qoder"].observed_client_aliases
            | mcp_config.CLIENT_SPECS["qoder-cn"].observed_client_aliases
        )
        for client, _, _, _, _, root_name, _, _, _, _, alias in self.CASES:
            with self.subTest(client=client):
                spec = mcp_config.CLIENT_SPECS[client]
                self.assertEqual(spec.host_family, client)
                self.assertEqual(spec.detection, "installation-probe")
                self.assertEqual(spec.observed_client_aliases, frozenset({alias}))
                self.assertTrue(spec.observed_client_aliases.isdisjoint(desktop_aliases))
                self.assertTrue(spec.require_observed_identity)
                self.assertEqual(spec.config_renderer, "json-mcp-v1")
                self.assertEqual(spec.launch_policy, "absolute-bootstrap-v1")
                self.assertFalse(spec.json_include_type)
                self.assertFalse(spec.json_include_cwd)
                self.assertEqual(spec.skills_project_paths, (f"{root_name}/skills",))
                self.assertEqual(spec.skill_delivery_mode, "managed-copy")
                self.assertTrue(spec.repair_skills_on_setup)
                self.assertTrue(spec.skills_require_managed_target)
                self.assertTrue(spec.local_display_tools)
                self.assertTrue(spec.audit_stop_panel)
                self.assertTrue(spec.unverified_lite_stopper)

                identity = mcp_config.resolve_host_identity(
                    client,
                    {"clientInfo": {"name": alias.title(), "version": "1.28.0"}},
                )
                self.assertEqual(identity.status, "matched")
                self.assertEqual(identity.observed_host, client)

    def test_ide_paths_use_product_mcp_and_shared_family_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for (
                client,
                module_name,
                _,
                _,
                _,
                root_name,
                _,
                config_env,
                settings_env,
                skills_env,
                _,
            ) in self.CASES:
                module = self._module(module_name)
                environment = {
                    config_env: "",
                    settings_env: "",
                    skills_env: "",
                }
                with self.subTest(client=client), mock.patch.object(
                    Path, "home", return_value=home
                ), mock.patch.object(module.sys, "platform", "darwin"), mock.patch.dict(
                    os.environ, environment, clear=False
                ):
                    self.assertEqual(
                        mcp_config.agent_config_path(client),
                        home / root_name / "mcp.json",
                    )
                    self.assertEqual(
                        module._settings_path(), home / root_name / "settings.json"
                    )
                    self.assertEqual(
                        mcp_config.CLIENT_SPECS[client].skills_global_path(),
                        home / root_name / "skills",
                    )

    def test_ide_guards_require_exact_product_and_bundle_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for (
                client,
                module_name,
                app_name,
                bundle_id,
                application_name,
                _,
                app_env,
                _,
                _,
                _,
                _,
            ) in self.CASES:
                module = self._module(module_name)
                app_root = base / app_name
                with self.subTest(client=client), mock.patch.object(
                    module.sys, "platform", "darwin"
                ), mock.patch.dict(os.environ, {app_env: str(app_root)}, clear=False):
                    with self.assertRaisesRegex(ShellError, "not_installed"):
                        module._config_write_guard()
                    self.assertFalse(module._installed())

                    self._write_bundle(
                        app_root,
                        application_name="another-product",
                        bundle_id=bundle_id,
                    )
                    with self.assertRaisesRegex(ShellError, "identity_mismatch"):
                        module._config_write_guard()

                    self._write_bundle(
                        app_root,
                        application_name=application_name,
                        bundle_id="com.example.other",
                    )
                    with self.assertRaisesRegex(ShellError, "identity_mismatch"):
                        module._config_write_guard()

                    self._write_bundle(
                        app_root,
                        application_name=application_name,
                        bundle_id=bundle_id,
                    )
                    self.assertIsNone(module._config_write_guard())
                    self.assertTrue(module._installed())

                    self._write_bundle(
                        app_root,
                        application_name=application_name,
                        bundle_id=bundle_id,
                        product_version="1.106.4",
                    )
                    self.assertIn("newer_than_tested", module._config_write_guard())

    def test_ide_write_uses_own_mcp_file_and_shared_settings_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            de_root = base / "managed"
            for (
                client,
                module_name,
                _,
                _,
                _,
                root_name,
                _,
                config_env,
                settings_env,
                skills_env,
                _,
            ) in self.CASES:
                module = self._module(module_name)
                family = home / root_name
                mcp_path = family / "mcp.json"
                settings_path = family / "settings.json"
                other_root = home / (".qoder-cn" if root_name == ".qoder" else ".qoder")
                other_settings = other_root / "settings.json"
                family.mkdir(parents=True, exist_ok=True)
                other_root.mkdir(parents=True, exist_ok=True)
                mcp_path.write_text(
                    json.dumps({"mcpServers": {"keep-ide": {"command": "keep"}}}),
                    encoding="utf-8",
                )
                settings_path.write_text(
                    json.dumps(
                        {
                            "mcpServers": {"keep-desktop": {"command": "keep"}},
                            "enabledPlugins": {"keep": True},
                        }
                    ),
                    encoding="utf-8",
                )
                other_settings.write_text(
                    json.dumps({"mcpServers": {"keep-other-product": {}}}),
                    encoding="utf-8",
                )
                other_before = other_settings.read_bytes()
                environment = {
                    config_env: "",
                    settings_env: "",
                    skills_env: "",
                }
                with self.subTest(client=client), mock.patch.object(
                    Path, "home", return_value=home
                ), mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                    module, "_config_write_guard", return_value=None
                ):
                    result = mcp_config.write_entry(client, dev_root=de_root)

                mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
                settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(result["action"], "added")
                self.assertEqual(result["companion"]["action"], "updated")
                self.assertIn("keep-ide", mcp_data["mcpServers"])
                entry = mcp_data["mcpServers"]["decision-engine"]
                self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], client)
                self.assertNotIn("type", entry)
                self.assertNotIn("cwd", entry)
                self.assertEqual(
                    settings_data["mcpServers"],
                    {"keep-desktop": {"command": "keep"}},
                )
                self.assertTrue(settings_data["enabledPlugins"]["keep"])
                hook = settings_data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
                self.assertEqual(hook["name"], "decision-engine-audit-routing-v1")
                self.assertEqual(
                    hook["args"],
                    [str(de_root / "installer" / "qoder_audit_prompt_hook.py")],
                )
                self.assertEqual(other_settings.read_bytes(), other_before)

                for path in (mcp_path, settings_path):
                    path.unlink()

    def test_ide_hook_conflict_aborts_before_mcp_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            de_root = base / "managed"
            for (
                client,
                module_name,
                _,
                _,
                _,
                root_name,
                _,
                config_env,
                settings_env,
                _,
                _,
            ) in self.CASES:
                module = self._module(module_name)
                family = home / root_name
                mcp_path = family / "mcp.json"
                settings_path = family / "settings.json"
                family.mkdir(parents=True, exist_ok=True)
                settings_path.write_text(
                    json.dumps(
                        {
                            "hooks": {
                                "UserPromptSubmit": [
                                    {
                                        "matcher": "",
                                        "hooks": [
                                            {
                                                "type": "command",
                                                "command": "/foreign/python",
                                                "args": ["/foreign/hook.py"],
                                                "name": "decision-engine-audit-routing-v1",
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                before = settings_path.read_bytes()
                environment = {config_env: "", settings_env: ""}
                with self.subTest(client=client), mock.patch.object(
                    Path, "home", return_value=home
                ), mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                    module, "_config_write_guard", return_value=None
                ):
                    with self.assertRaisesRegex(ShellError, "same_name_unowned"):
                        mcp_config.write_entry(client, dev_root=de_root)

                self.assertFalse(mcp_path.exists())
                self.assertEqual(settings_path.read_bytes(), before)
                settings_path.unlink()

    def test_public_installer_reports_codebuddy_studio_as_unsupported(self):
        script = Path(__file__).resolve().parents[2] / "dp-install.sh"
        body = script.read_text(encoding="utf-8")

        self.assertIn('"/Applications/CodeBuddy Studio.app"', body)
        self.assertIn('"com.codebuddy.ride"', body)
        self.assertIn(
            '"codebuddy-studio (CodeBuddy Studio.app): DE adapter unavailable"',
            body,
        )


if __name__ == "__main__":
    unittest.main()
