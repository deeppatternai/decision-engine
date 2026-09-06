"""Behavior locks for the Tencent CodeBuddy Agent CLI host adapter."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import mcp_config
from installer.config import ShellError


class CodeBuddyHostTestCase(unittest.TestCase):
    def _write_executable(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    def test_codebuddy_spec_is_an_independent_cli_host(self):
        spec = mcp_config.CLIENT_SPECS["codebuddy"]

        self.assertEqual(spec.host_family, "codebuddy")
        self.assertEqual(spec.detection, "installation-probe")
        self.assertEqual(spec.config_renderer, "json-mcp-v1")
        self.assertEqual(spec.launch_policy, "direct-python-v1")
        self.assertEqual(spec.observed_client_aliases, frozenset({"codebuddy"}))
        self.assertTrue(spec.require_observed_identity)
        self.assertEqual(spec.skills_project_paths, (".codebuddy/skills",))
        self.assertEqual(spec.skill_delivery_mode, "managed-copy")
        self.assertEqual(spec.routing_kind, "skill")
        self.assertEqual(spec.skill_route_name, "codebuddy")
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
        self.assertTrue(spec.unverified_lite_stopper)

    def test_codebuddy_paths_are_cli_owned_and_overridable(self):
        from installer.client_hosts.hosts import codebuddy

        with mock.patch.dict(
            os.environ,
            {
                "CODEBUDDY_CONFIG": "",
                "CODEBUDDY_SKILLS_DIR": "/isolated/codebuddy-skills",
            },
            clear=False,
        ), mock.patch.object(Path, "home", return_value=Path("/Users/test")):
            self.assertEqual(
                mcp_config.agent_config_path("codebuddy"),
                Path("/Users/test/.codebuddy/mcp.json"),
            )
            self.assertEqual(
                codebuddy._codebuddy_skills_path(),
                Path("/isolated/codebuddy-skills"),
            )

    def test_codebuddy_detection_requires_an_independent_cli_executable(self):
        from installer.client_hosts.hosts import codebuddy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            residual_home = root / "home"
            (residual_home / ".codebuddy").mkdir(parents=True)
            cli_name = "codebuddy.cmd" if os.name == "nt" else "codebuddy"
            independent = root / "bin" / cli_name
            bundled = (
                root
                / "Applications"
                / "WorkBuddy AI.app"
                / "Contents"
                / "Resources"
                / "app.asar.unpacked"
                / "cli"
                / "bin"
                / cli_name
            )
            self._write_executable(independent)
            self._write_executable(bundled)

            with mock.patch.object(Path, "home", return_value=residual_home), mock.patch.dict(
                os.environ,
                {"CODEBUDDY_CLI": "", "CODEBUDDY_CONFIG": ""},
                clear=False,
            ), mock.patch.object(codebuddy.shutil, "which", return_value=None):
                self.assertFalse(codebuddy._codebuddy_installed())
                self.assertFalse(mcp_config.client_present("codebuddy"))

            with mock.patch.dict(
                os.environ, {"CODEBUDDY_CLI": ""}, clear=False
            ), mock.patch.object(
                codebuddy.shutil, "which", return_value=str(bundled)
            ):
                self.assertFalse(codebuddy._codebuddy_installed())

            with mock.patch.dict(
                os.environ, {"CODEBUDDY_CLI": ""}, clear=False
            ), mock.patch.object(
                codebuddy.shutil, "which", return_value=str(independent)
            ):
                self.assertTrue(codebuddy._codebuddy_installed())
                self.assertIsNone(codebuddy._codebuddy_config_write_guard())

    def test_codebuddy_explicit_cli_override_is_bounded_by_executable_state(self):
        from installer.client_hosts.hosts import codebuddy

        with tempfile.TemporaryDirectory() as tmp:
            cli_name = "codebuddy.cmd" if os.name == "nt" else "codebuddy"
            cli = Path(tmp) / "custom" / cli_name
            with mock.patch.dict(
                os.environ, {"CODEBUDDY_CLI": str(cli)}, clear=False
            ):
                self.assertFalse(codebuddy._codebuddy_installed())
                with self.assertRaisesRegex(
                    ShellError, "codebuddy_not_installed"
                ):
                    codebuddy._codebuddy_config_write_guard()

                self._write_executable(cli)
                self.assertTrue(codebuddy._codebuddy_installed())
                self.assertIsNone(codebuddy._codebuddy_config_write_guard())

                if os.name == "nt":
                    non_executable = cli.with_suffix(".txt")
                    self._write_executable(non_executable)
                    with mock.patch.dict(
                        os.environ,
                        {"CODEBUDDY_CLI": str(non_executable)},
                        clear=False,
                    ):
                        self.assertFalse(codebuddy._codebuddy_installed())
                else:
                    cli.chmod(0o644)
                    self.assertFalse(codebuddy._codebuddy_installed())

    def test_codebuddy_renderer_is_credential_free_and_host_specific(self):
        root = Path("/managed/decision-engine")
        entry = mcp_config.render_entry(client="codebuddy", cwd=root)[
            "mcpServers"
        ]["decision-engine"]

        self.assertEqual(set(entry), {"command", "args", "env"})
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], "codebuddy")
        self.assertEqual(entry["env"]["PYTHONPATH"], str(root))
        self.assertNotIn("DE_ENDPOINT", entry["env"])
        self.assertNotIn("DE_ACTIVATION_SECRET", entry["env"])
        self.assertNotIn("type", entry)
        self.assertNotIn("cwd", entry)

    def test_codebuddy_write_does_not_touch_workbuddy_roots(self):
        from installer.client_hosts.hosts import codebuddy

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "managed"
            current = home / ".codebuddy" / "mcp.json"
            siblings = (
                home / ".workbuddy" / "mcp.json",
                home / ".workbuddy-ai" / "mcp.json",
            )
            before = {}
            for sibling in siblings:
                sibling.parent.mkdir(parents=True)
                sibling.write_text(
                    '{"mcpServers":{"keep":{"command":"keep"}}}',
                    encoding="utf-8",
                )
                before[sibling] = sibling.read_bytes()
            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ,
                {"CODEBUDDY_CONFIG": ""},
                clear=False,
            ), mock.patch.object(
                codebuddy, "_codebuddy_config_write_guard", return_value=None
            ):
                result = mcp_config.write_entry("codebuddy", dev_root=root)

            data = json.loads(current.read_text(encoding="utf-8"))
            self.assertEqual(result["action"], "added")
            self.assertIn("decision-engine", data["mcpServers"])
            for sibling in siblings:
                self.assertEqual(sibling.read_bytes(), before[sibling])

    def test_codebuddy_doctor_skill_route_requires_its_mcp_entry(self):
        from installer.client_hosts.hosts import codebuddy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".codebuddy" / "mcp.json"
            skills = root / ".codebuddy" / "skills"
            environment = {
                "CODEBUDDY_CONFIG": str(config_path),
                "CODEBUDDY_SKILLS_DIR": str(skills),
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                codebuddy, "_codebuddy_installed", return_value=True
            ):
                self.assertNotIn(
                    "codebuddy", mcp_config.active_skill_routes(for_doctor=True)
                )
                config_path.parent.mkdir(parents=True)
                config_path.write_text(
                    json.dumps(mcp_config.render_entry(client="codebuddy", cwd=root)),
                    encoding="utf-8",
                )
                route, excluded = mcp_config.active_skill_routes(
                    for_doctor=True
                )["codebuddy"]

        self.assertEqual(route, skills)
        self.assertEqual(excluded, frozenset())


if __name__ == "__main__":
    unittest.main()
