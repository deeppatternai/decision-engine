"""Behavior locks for the Tencent WorkBuddy AI Desktop host adapter."""

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


class WorkBuddyAIHostTestCase(unittest.TestCase):
    def _write_bundle(
        self,
        app_root: Path,
        *,
        bundle_id: str = "com.workbuddy.workbuddy-ai",
        version: str = "5.5.2",
    ) -> None:
        info = app_root / "Contents" / "Info.plist"
        executable = app_root / "Contents" / "MacOS" / "Electron"
        info.parent.mkdir(parents=True, exist_ok=True)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"workbuddy-ai-test-binary")
        info.write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleExecutable": "Electron",
                    "CFBundleShortVersionString": version,
                    "CFBundleVersion": version,
                }
            )
        )

    def test_workbuddy_ai_spec_is_independent_from_legacy_workbuddy(self):
        spec = mcp_config.CLIENT_SPECS["workbuddy-ai"]
        legacy = mcp_config.CLIENT_SPECS["workbuddy"]

        self.assertEqual(spec.host_family, "workbuddy-ai")
        self.assertEqual(spec.config_renderer, "json-mcp-v1")
        self.assertEqual(spec.launch_policy, "desktop-python-v1")
        self.assertTrue(spec.require_observed_identity)
        self.assertEqual(spec.skill_delivery_mode, "managed-copy")
        self.assertEqual(spec.routing_kind, "skill")
        self.assertEqual(spec.skill_route_name, "workbuddy-ai")
        self.assertTrue(spec.repair_skills_on_setup)
        self.assertTrue(spec.skills_require_managed_target)
        self.assertEqual(
            spec.doctor_capabilities,
            frozenset({"mcp-entry", "skills"}),
        )
        self.assertEqual(
            spec.launcher_capabilities,
            frozenset({"core-mcp", "local-display", "audit-stop-panel"}),
        )
        self.assertTrue(spec.local_display_tools)
        self.assertFalse(spec.popup_followup)
        self.assertTrue(spec.audit_stop_panel)
        self.assertTrue(spec.unverified_lite_stopper)
        self.assertIsNotNone(spec.post_mcp_write)
        self.assertNotEqual(spec.config_env, legacy.config_env)
        self.assertNotEqual(spec.skill_route_name, legacy.skill_route_name)
        self.assertEqual(
            mcp_config.normalize_client_host("WorkBuddy AI"), "workbuddy-ai"
        )
        self.assertEqual(mcp_config.normalize_client_host("WorkBuddy"), "workbuddy")

    def test_workbuddy_ai_paths_are_product_owned_and_overridable(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with mock.patch.dict(
            os.environ,
            {
                "WORKBUDDY_AI_CONFIG": "",
                "WORKBUDDY_AI_SKILLS_DIR": "/isolated/workbuddy-ai-skills",
            },
            clear=False,
        ), mock.patch.object(Path, "home", return_value=Path("/Users/test")):
            self.assertEqual(
                mcp_config.agent_config_path("workbuddy-ai"),
                Path("/Users/test/.workbuddy-ai/mcp.json"),
            )
            self.assertEqual(
                workbuddy_ai._workbuddy_ai_skills_path(),
                Path("/isolated/workbuddy-ai-skills"),
            )
            self.assertEqual(
                workbuddy_ai._workbuddy_ai_settings_path(),
                Path("/Users/test/.workbuddy-ai/settings.json"),
            )

    def test_workbuddy_ai_guard_checks_exact_bundle_identity_and_version(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "WorkBuddy AI.app"
            environment = {"WORKBUDDY_AI_APP_ROOT": str(app_root)}
            with mock.patch.object(
                workbuddy_ai.sys, "platform", "darwin"
            ), mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(
                    ShellError, "workbuddy_ai_not_installed"
                ):
                    workbuddy_ai._workbuddy_ai_config_write_guard()
                self.assertFalse(workbuddy_ai._workbuddy_ai_installed())

                self._write_bundle(app_root, bundle_id="com.workbuddy.legacy")
                with self.assertRaisesRegex(
                    ShellError, "workbuddy_ai_identity_mismatch"
                ):
                    workbuddy_ai._workbuddy_ai_config_write_guard()

                self._write_bundle(app_root, version="5.5.1")
                with self.assertRaisesRegex(
                    ShellError, "unsupported_workbuddy_ai_version"
                ):
                    workbuddy_ai._workbuddy_ai_config_write_guard()
                self.assertFalse(workbuddy_ai._workbuddy_ai_installed())

                self._write_bundle(app_root)
                self.assertIsNone(workbuddy_ai._workbuddy_ai_config_write_guard())
                self.assertTrue(workbuddy_ai._workbuddy_ai_installed())

                self._write_bundle(app_root, version="5.5.3")
                self.assertEqual(
                    workbuddy_ai._workbuddy_ai_config_write_guard(),
                    "workbuddy_ai_version_newer_than_tested",
                )
                self.assertTrue(workbuddy_ai._workbuddy_ai_installed())

                with mock.patch.object(workbuddy_ai.sys, "platform", "linux"):
                    with self.assertRaisesRegex(
                        ShellError, "unsupported_workbuddy_ai_platform"
                    ):
                        workbuddy_ai._workbuddy_ai_config_write_guard()
                    self.assertFalse(workbuddy_ai._workbuddy_ai_installed())

    def test_workbuddy_ai_malformed_bundle_metadata_is_refused(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "WorkBuddy AI.app"
            info = app_root / "Contents" / "Info.plist"
            info.parent.mkdir(parents=True)
            info.write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.workbuddy.workbuddy-ai",
                        "CFBundleShortVersionString": "broken",
                    }
                )
            )
            with mock.patch.object(
                workbuddy_ai.sys, "platform", "darwin"
            ), mock.patch.dict(
                os.environ,
                {"WORKBUDDY_AI_APP_ROOT": str(app_root)},
                clear=False,
            ), self.assertRaisesRegex(
                ShellError, "workbuddy_ai_metadata_invalid"
            ):
                workbuddy_ai._workbuddy_ai_config_write_guard()

    def test_workbuddy_ai_renderer_is_credential_free_and_host_specific(self):
        root = Path("/managed/decision-engine")
        with mock.patch.object(mcp_config.sys, "platform", "darwin"):
            entry = mcp_config.render_entry(client="workbuddy-ai", cwd=root)[
                "mcpServers"
            ]["decision-engine"]

        self.assertEqual(set(entry), {"command", "args", "env"})
        self.assertEqual(
            entry["env"][mcp_config.CLIENT_HOST_ENV], "workbuddy-ai"
        )
        self.assertEqual(entry["env"]["PYTHONPATH"], str(root))
        self.assertNotIn("DE_ENDPOINT", entry["env"])
        self.assertNotIn("DE_ACTIVATION_SECRET", entry["env"])
        self.assertNotIn("type", entry)
        self.assertNotIn("cwd", entry)

    def test_workbuddy_ai_write_does_not_touch_legacy_workbuddy_root(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "managed"
            current = home / ".workbuddy-ai" / "mcp.json"
            legacy = home / ".workbuddy" / "mcp.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text('{"mcpServers":{"keep":{"command":"keep"}}}', encoding="utf-8")
            before = legacy.read_bytes()
            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ,
                {"WORKBUDDY_AI_CONFIG": ""},
                clear=False,
            ), mock.patch.object(
                workbuddy_ai, "_workbuddy_ai_config_write_guard", return_value=None
            ):
                result = mcp_config.write_entry("workbuddy-ai", dev_root=root)

            data = json.loads(current.read_text(encoding="utf-8"))
            self.assertEqual(result["action"], "added")
            self.assertIn("decision-engine", data["mcpServers"])
            self.assertEqual(legacy.read_bytes(), before)

    def test_workbuddy_ai_upgrade_preserves_host_disabled_state(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mcp_path = root / "home" / ".workbuddy-ai" / "mcp.json"
            settings = root / "home" / ".workbuddy-ai" / "settings.json"
            old_de = root / "old-de"
            new_de = root / "new-de"
            environment = {
                "WORKBUDDY_AI_CONFIG": str(mcp_path),
                "WORKBUDDY_AI_SETTINGS": str(settings),
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                workbuddy_ai, "_workbuddy_ai_config_write_guard", return_value=None
            ):
                mcp_config.write_entry("workbuddy-ai", dev_root=old_de)
                data = json.loads(mcp_path.read_text(encoding="utf-8"))
                data["mcpServers"]["decision-engine"]["disabled"] = False
                mcp_path.write_text(json.dumps(data), encoding="utf-8")

                result = mcp_config.write_entry("workbuddy-ai", dev_root=new_de)

            entry = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"][
                "decision-engine"
            ]
            self.assertEqual(result["action"], "updated")
            self.assertFalse(entry["disabled"])
            self.assertEqual(entry["env"]["PYTHONPATH"], str(new_de))

    def test_workbuddy_ai_upgrade_rejects_invalid_host_disabled_state(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mcp_path = root / "home" / ".workbuddy-ai" / "mcp.json"
            settings = root / "home" / ".workbuddy-ai" / "settings.json"
            environment = {
                "WORKBUDDY_AI_CONFIG": str(mcp_path),
                "WORKBUDDY_AI_SETTINGS": str(settings),
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                workbuddy_ai, "_workbuddy_ai_config_write_guard", return_value=None
            ):
                mcp_config.write_entry("workbuddy-ai", dev_root=root / "old-de")
                data = json.loads(mcp_path.read_text(encoding="utf-8"))
                data["mcpServers"]["decision-engine"]["disabled"] = "false"
                mcp_path.write_text(json.dumps(data), encoding="utf-8")
                before = mcp_path.read_bytes()

                with self.assertRaisesRegex(ShellError, "same_name_unowned"):
                    mcp_config.write_entry("workbuddy-ai", dev_root=root / "new-de")

            self.assertEqual(mcp_path.read_bytes(), before)

    def test_workbuddy_ai_write_installs_owned_audit_hook_and_preserves_existing_hooks(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "managed"
            settings = home / ".workbuddy-ai" / "settings.json"
            aqg_group = {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/bin/python3 /opt/aqg/hook.py userPromptSubmit",
                    }
                ],
            }
            user_group = {
                "matcher": "custom",
                "hooks": [{"type": "command", "command": "/opt/user/hook"}],
            }
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps(
                    {"hooks": {"UserPromptSubmit": [aqg_group, user_group]}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            environment = {
                "WORKBUDDY_AI_CONFIG": "",
                "WORKBUDDY_AI_SETTINGS": "",
            }
            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ, environment, clear=False
            ), mock.patch.object(
                workbuddy_ai, "_workbuddy_ai_config_write_guard", return_value=None
            ):
                first = mcp_config.write_entry("workbuddy-ai", dev_root=root)
                first_bytes = settings.read_bytes()
                second = mcp_config.write_entry("workbuddy-ai", dev_root=root)

            data = json.loads(first_bytes.decode("utf-8"))
            groups = data["hooks"]["UserPromptSubmit"]
            self.assertIn(aqg_group, groups)
            self.assertIn(user_group, groups)
            managed = [
                group
                for group in groups
                if "decision-engine-workbuddy-ai-audit-routing-v1"
                in json.dumps(group)
            ]
            self.assertEqual(len(managed), 1)
            command = managed[0]["hooks"][0]["command"]
            self.assertIn(str(root / "installer" / "workbuddy_audit_prompt_hook.py"), command)
            self.assertEqual(first["companion"]["action"], "updated")
            self.assertEqual(second["companion"]["action"], "unchanged")
            self.assertEqual(settings.read_bytes(), first_bytes)

    def test_workbuddy_ai_invalid_hook_settings_fail_before_mcp_is_written(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "managed"
            mcp_path = home / ".workbuddy-ai" / "mcp.json"
            settings = home / ".workbuddy-ai" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{invalid", encoding="utf-8")
            environment = {
                "WORKBUDDY_AI_CONFIG": "",
                "WORKBUDDY_AI_SETTINGS": "",
            }
            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ, environment, clear=False
            ), mock.patch.object(
                workbuddy_ai, "_workbuddy_ai_config_write_guard", return_value=None
            ), self.assertRaisesRegex(
                ShellError, "invalid WorkBuddy AI settings"
            ):
                mcp_config.write_entry("workbuddy-ai", dev_root=root)

            self.assertFalse(mcp_path.exists())
            self.assertEqual(settings.read_text(encoding="utf-8"), "{invalid")

    def test_workbuddy_ai_doctor_skill_route_requires_its_mcp_entry(self):
        from installer.client_hosts.hosts import workbuddy_ai

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".workbuddy-ai" / "mcp.json"
            skills = root / ".workbuddy-ai" / "skills"
            environment = {
                "WORKBUDDY_AI_CONFIG": str(config_path),
                "WORKBUDDY_AI_SKILLS_DIR": str(skills),
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                workbuddy_ai, "_workbuddy_ai_installed", return_value=True
            ):
                self.assertNotIn(
                    "workbuddy-ai", mcp_config.active_skill_routes(for_doctor=True)
                )
                config_path.parent.mkdir(parents=True)
                config_path.write_text(
                    json.dumps(
                        mcp_config.render_entry(client="workbuddy-ai", cwd=root)
                    ),
                    encoding="utf-8",
                )
                route, excluded = mcp_config.active_skill_routes(
                    for_doctor=True
                )["workbuddy-ai"]

        self.assertEqual(route, skills)
        self.assertEqual(excluded, frozenset())


if __name__ == "__main__":
    unittest.main()
