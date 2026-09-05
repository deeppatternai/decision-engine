"""Behavior locks for distinct Qoder CN and TRAE product adapters."""

from __future__ import annotations

import json
import os
import plistlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from installer import mcp_config
from installer.config import ShellError


class MultiDesktopRegistryTestCase(unittest.TestCase):
    def test_requested_products_have_distinct_registry_identities(self):
        expected = {
            "qoder",
            "qoder-cn",
            "trae",
            "trae-work",
            "trae-cn",
            "trae-work-cn",
        }

        self.assertTrue(expected.issubset(mcp_config.CLIENT_SPECS))
        for client in expected:
            with self.subTest(client=client):
                self.assertEqual(mcp_config.CLIENT_SPECS[client].id, client)
                self.assertEqual(mcp_config.CLIENT_SPECS[client].host_family, client)

    def test_runtime_aliases_distinguish_trae_products_and_bound_shared_qoder(self):
        self.assertIsNone(mcp_config.normalize_observed_client_info("mcphost"))
        self.assertIsNone(mcp_config.normalize_observed_client_info("Trae"))
        self.assertEqual(mcp_config.normalize_client_host("Qoder"), "qoder")
        self.assertEqual(
            mcp_config.normalize_client_host("Qoder CN"), "qoder-cn"
        )
        self.assertIsNone(mcp_config.normalize_client_host("Qoder CN IDE"))

        cases = (
            ("qoder", "mcphost"),
            ("qoder-cn", "mcphost"),
            ("trae", "Trae"),
            ("trae-work", "Trae"),
            ("trae-cn", "Trae"),
            ("trae-work-cn", "Trae"),
        )
        for declared, observed in cases:
            with self.subTest(declared=declared):
                identity = mcp_config.resolve_host_identity(
                    declared,
                    {"clientInfo": {"name": observed, "version": "1"}},
                )
                self.assertEqual(identity.status, "matched")
                self.assertEqual(identity.declared_host, declared)
                self.assertEqual(identity.observed_host, declared)
                self.assertTrue(identity.optional_features_enabled)

        qoder = mcp_config.CLIENT_SPECS["qoder"]
        different_capabilities = replace(
            qoder,
            id="different-host",
            host_family="different-host",
            local_display_tools=False,
            audit_stop_panel=False,
            launcher_capabilities=frozenset({"core-mcp"}),
            optional_features=frozenset(),
        )
        with mock.patch.object(
            mcp_config,
            "CLIENT_SPECS",
            {**mcp_config.CLIENT_SPECS, "different-host": different_capabilities},
        ):
            identity = mcp_config.resolve_host_identity(
                "qoder",
                {"clientInfo": {"name": "mcphost", "version": "1"}},
            )

        self.assertEqual(identity.status, "unknown")
        self.assertFalse(identity.optional_features_enabled)

    def test_installation_probe_policy_requires_a_probe(self):
        invalid = replace(
            mcp_config.CLIENT_SPECS["qoder-cn"], installation_probe=None
        )
        with self.assertRaisesRegex(ShellError, "requires an installation probe"):
            mcp_config.validate_host_specs({"qoder-cn": invalid})


class QoderCnHostTestCase(unittest.TestCase):
    def _write_bundle(
        self,
        app_root: Path,
        *,
        product_id: str = "qoder-cn",
        bundle_id: str = "com.qodercn.app",
        version: str = "0.1.4",
    ) -> None:
        product = app_root / "Contents" / "Resources" / "product.json"
        info = app_root / "Contents" / "Info.plist"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text(json.dumps({"productId": product_id}), encoding="utf-8")
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleShortVersionString": version,
                }
            )
        )

    def test_qoder_cn_owns_cn_paths_and_preserves_international_paths(self):
        from installer.client_hosts.hosts import qoder_cn

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                qoder_cn.sys, "platform", "darwin"
            ), mock.patch.dict(
                os.environ,
                {
                    "QODER_CN_CONFIG": "",
                    "QODER_CN_SKILLS_DIR": "",
                    "QODER_CN_APP_ROOT": "",
                },
                clear=False,
            ):
                self.assertEqual(
                    mcp_config.agent_config_path("qoder-cn"),
                    home / ".qoder-cn" / "settings.json",
                )
                self.assertEqual(
                    mcp_config.CLIENT_SPECS["qoder-cn"].skills_global_path(),
                    home / ".qoder-cn" / "skills",
                )
                self.assertEqual(
                    qoder_cn._qoder_cn_app_root(),
                    Path("/Applications/Qoder CN.app"),
                )
                self.assertEqual(
                    mcp_config.CLIENT_SPECS["qoder-cn"].skills_project_paths,
                    (".qoder/skills",),
                )
                self.assertTrue(
                    mcp_config.CLIENT_SPECS["qoder-cn"].unverified_lite_stopper
                )

    def test_qoder_cn_guard_requires_cn_product_and_bundle_identity(self):
        from installer.client_hosts.hosts import qoder_cn

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "Qoder CN.app"
            environment = {"QODER_CN_APP_ROOT": str(app_root)}
            with mock.patch.object(qoder_cn.sys, "platform", "darwin"), mock.patch.dict(
                os.environ, environment, clear=False
            ):
                self._write_bundle(app_root)
                self.assertIsNone(qoder_cn._qoder_cn_config_write_guard())
                self.assertTrue(qoder_cn._qoder_cn_installed())

                self._write_bundle(app_root, product_id="qoder")
                with self.assertRaisesRegex(ShellError, "qoder_cn_identity_mismatch"):
                    qoder_cn._qoder_cn_config_write_guard()

                self._write_bundle(app_root, bundle_id="com.qoder.app")
                with self.assertRaisesRegex(ShellError, "qoder_cn_identity_mismatch"):
                    qoder_cn._qoder_cn_config_write_guard()

    def test_qoder_cn_write_updates_only_cn_settings(self):
        from installer.client_hosts.hosts import qoder_cn

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "managed"
            cn_settings = home / ".qoder-cn" / "settings.json"
            international = home / ".qoder" / "settings.json"
            cn_settings.parent.mkdir(parents=True)
            international.parent.mkdir(parents=True)
            cn_settings.write_text(
                json.dumps({"enabledPlugins": {"keep-cn": True}}),
                encoding="utf-8",
            )
            international.write_text(
                json.dumps({"mcpServers": {"keep-international": {}}}),
                encoding="utf-8",
            )
            before = international.read_bytes()
            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                qoder_cn.sys, "platform", "darwin"
            ), mock.patch.dict(
                os.environ, {"QODER_CN_CONFIG": ""}, clear=False
            ), mock.patch.object(
                qoder_cn, "_qoder_cn_config_write_guard", return_value=None
            ):
                result = mcp_config.write_entry("qoder-cn", dev_root=root)

            data = json.loads(cn_settings.read_text(encoding="utf-8"))
            international_after = international.read_bytes()
        self.assertEqual(result["action"], "added")
        self.assertTrue(data["enabledPlugins"]["keep-cn"])
        self.assertIn("decision-engine", data["mcpServers"])
        prompt_groups = data["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(prompt_groups), 1)
        prompt_hook = prompt_groups[0]["hooks"][0]
        self.assertEqual(prompt_hook["name"], "decision-engine-audit-routing-v1")
        self.assertEqual(
            prompt_hook["args"],
            [str(root / "installer" / "qoder_audit_prompt_hook.py")],
        )
        self.assertEqual(international_after, before)

    def test_qoder_cn_detection_ignores_stale_profile_without_app(self):
        from installer.client_hosts.hosts import qoder_cn

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".qoder-cn" / "settings.json"
            config.parent.mkdir(parents=True)
            config.write_text("{}", encoding="utf-8")
            with mock.patch.object(qoder_cn.sys, "platform", "darwin"), mock.patch.dict(
                os.environ,
                {
                    "QODER_CN_CONFIG": str(config),
                    "QODER_CN_APP_ROOT": str(root / "missing.app"),
                },
                clear=False,
            ):
                self.assertFalse(mcp_config.client_present("qoder-cn"))

    def test_qoder_cn_renderer_uses_cn_marker_and_absolute_bootstrap(self):
        root = Path("/opt/decision-engine")
        entry = mcp_config.render_entry(client="qoder-cn", cwd=root)["mcpServers"][
            "decision-engine"
        ]

        self.assertEqual(set(entry), {"command", "args", "env"})
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], "qoder-cn")
        self.assertTrue(entry["args"][0].endswith("installer/mcp_bootstrap.py"))
        self.assertNotIn("cwd", entry)
        self.assertNotIn("type", entry)


class TraeDesktopHostTestCase(unittest.TestCase):
    CASES = (
        (
            "trae",
            "trae",
            "Trae",
            ".trae",
            "TRAE_APP_ROOT",
            "TRAE_CONFIG",
            "TRAE_SKILLS_DIR",
            "3.5.81",
            "com.trae.app",
        ),
        (
            "trae-cn",
            "trae-cn",
            "Trae CN",
            ".trae-cn",
            "TRAE_CN_APP_ROOT",
            "TRAE_CN_CONFIG",
            "TRAE_CN_SKILLS_DIR",
            "3.3.95",
            "cn.trae.app",
        ),
    )

    def _write_product(
        self,
        app_root: Path,
        *,
        application_name: str,
        version: str,
        bundle_id: str,
    ) -> None:
        product = app_root / "Contents" / "Resources" / "app" / "product.json"
        info = app_root / "Contents" / "Info.plist"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text(
            json.dumps(
                {"applicationName": application_name, "appVersion": version}
            ),
            encoding="utf-8",
        )
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleShortVersionString": version,
                }
            )
        )

    def test_trae_desktop_products_have_distinct_app_and_mcp_paths(self):
        from installer.client_hosts.hosts import trae, trae_cn

        modules = {"trae": trae, "trae-cn": trae_cn}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for (
                client,
                _application_name,
                app_name,
                skills_root,
                app_env,
                config_env,
                skills_env,
                _version,
                _bundle_id,
            ) in self.CASES:
                module = modules[client]
                with self.subTest(client=client), mock.patch.object(
                    Path, "home", return_value=home
                ), mock.patch.object(module.sys, "platform", "darwin"), mock.patch.dict(
                    os.environ,
                    {app_env: "", config_env: "", skills_env: ""},
                    clear=False,
                ):
                    self.assertEqual(
                        mcp_config.agent_config_path(client),
                        home
                        / "Library"
                        / "Application Support"
                        / app_name
                        / "User"
                        / "mcp.json",
                    )
                    self.assertEqual(
                        mcp_config.CLIENT_SPECS[client].skills_global_path(),
                        home / skills_root / "skills",
                    )
                    self.assertEqual(
                        module._app_root(), Path("/Applications") / f"{app_name}.app"
                    )

    def test_trae_desktop_guards_keep_international_and_cn_distinct(self):
        from installer.client_hosts.hosts import trae, trae_cn

        modules = {"trae": trae, "trae-cn": trae_cn}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for (
                client,
                application_name,
                app_name,
                _skills_root,
                app_env,
                _config_env,
                _skills_env,
                version,
                bundle_id,
            ) in self.CASES:
                module = modules[client]
                app_root = base / f"{app_name}.app"
                with self.subTest(client=client), mock.patch.object(
                    module.sys, "platform", "darwin"
                ), mock.patch.dict(
                    os.environ, {app_env: str(app_root)}, clear=False
                ):
                    self._write_product(
                        app_root,
                        application_name=application_name,
                        version=version,
                        bundle_id=bundle_id,
                    )
                    self.assertIsNone(module._config_write_guard())
                    self.assertTrue(module._installed())

                    other = "trae-cn" if application_name == "trae" else "trae"
                    self._write_product(
                        app_root,
                        application_name=other,
                        version=version,
                        bundle_id=bundle_id,
                    )
                    with self.assertRaisesRegex(
                        ShellError, f"{client.replace('-', '_')}_identity_mismatch"
                    ):
                        module._config_write_guard()

    def test_trae_desktop_renderers_use_product_specific_markers(self):
        root = Path("/opt/decision-engine")
        for client, *_rest in self.CASES:
            with self.subTest(client=client):
                entry = mcp_config.render_entry(client=client, cwd=root)[
                    "mcpServers"
                ]["decision-engine"]
                self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], client)
                self.assertEqual(entry["cwd"], str(root))
                self.assertNotIn("type", entry)

    def test_trae_desktop_detection_ignores_stale_profiles(self):
        from installer.client_hosts.hosts import trae, trae_cn

        cases = (
            ("trae", trae, "TRAE_CONFIG", "TRAE_APP_ROOT"),
            ("trae-cn", trae_cn, "TRAE_CN_CONFIG", "TRAE_CN_APP_ROOT"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for client, module, config_env, app_env in cases:
                config = root / client / "mcp.json"
                config.parent.mkdir(parents=True)
                config.write_text("{}", encoding="utf-8")
                for platform in ("darwin", "linux"):
                    with self.subTest(client=client, platform=platform), mock.patch.object(
                        module.sys, "platform", platform
                    ), mock.patch.dict(
                        os.environ,
                        {config_env: str(config), app_env: str(root / "missing.app")},
                        clear=False,
                    ):
                        self.assertFalse(mcp_config.client_present(client))

    def test_existing_trae_work_adapters_warn_above_retained_evidence_ceiling(self):
        from installer.client_hosts.hosts import trae_work, trae_work_cn

        self.assertEqual(trae_work._TESTED_VERSION, (0, 1, 48))
        self.assertEqual(trae_work_cn._TESTED_VERSION, (0, 1, 49))


if __name__ == "__main__":
    unittest.main()
