"""Behavior tests for the MCP registration emitter (installer.mcp_config)."""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from installer.config import ShellError
from installer import mcp_config, update_transaction


tomllib = mcp_config._tomllib()


class McpConfigTestCase(unittest.TestCase):
    def test_bare_call_with_no_managed_install_refuses_instead_of_guessing(self):
        # Phase 3 behavior lock: this used to silently fall back to shell_root() (wherever this
        # source checkout physically sits). It must now refuse — a caller wants either the real
        # activated managed root, or must say --dev-root/dev_root= explicitly.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-installed"
            with (
                mock.patch.object(mcp_config, "managed_root", return_value=missing),
                self.assertRaisesRegex(
                    ShellError, "no activated managed Decision Engine install"
                ),
            ):
                mcp_config.render_entry()

    def test_dev_root_entry_renders_explicit_dev_mode_args(self):
        dev_root = mcp_config.shell_root()
        entry = mcp_config.render_entry(dev_root=dev_root)
        servers = entry["mcpServers"]
        self.assertIn("decision-engine", servers)
        de = servers["decision-engine"]
        # Uses the current interpreter and launches the updater gate as a module, with the
        # explicit --dev-root flag installer.launcher already implements (no marker/network/
        # updater/lease at all).
        self.assertEqual(de["command"], sys.executable)
        self.assertEqual(de["args"], ["-m", "installer.launcher", "--dev-root", str(dev_root)])
        self.assertEqual(de["cwd"], str(dev_root))
        self.assertEqual(
            de["env"][mcp_config.CLIENT_HOST_ENV], "claude"
        )

    def test_dev_root_and_cwd_together_is_a_caller_error(self):
        with self.assertRaisesRegex(ShellError, "either cwd= or dev_root="):
            mcp_config.render_entry(cwd=Path("/opt/shell"), dev_root=mcp_config.shell_root())

    def test_entry_selects_fixed_root_only_after_managed_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            # .resolve(): on macOS the temp dir's own ancestor (/var) is
            # itself a symlink (-> /private/var) — the new symlink-component
            # check in registration_root() correctly refuses that, matching
            # the same convention test_managed_install.py already uses.
            root = Path(tmp).resolve() / "decision-engine"
            (root / "installer").mkdir(parents=True)
            (root / "installer" / "launcher.py").write_text("# launcher\n", encoding="utf-8")
            protocol = root / ".runtime" / "update-protocol.json"
            protocol.parent.mkdir()
            protocol.write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(mcp_config, "managed_root", return_value=root),
                mock.patch.object(
                    update_transaction,
                    "_require_protocol_ready",
                ),
            ):
                entry = mcp_config.render_entry()
        self.assertEqual(entry["mcpServers"]["decision-engine"]["cwd"], str(root))

    def test_explicit_pre_activation_entry_accepts_only_a_complete_fixed_managed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "decision-engine"
            (root / ".git").mkdir(parents=True)
            (root / "installer").mkdir()
            (root / "installer" / "launcher.py").write_text("# launcher\n", encoding="utf-8")
            (root / "installer" / "mcp_bootstrap.py").write_text(
                "# bootstrap\n", encoding="utf-8"
            )
            (root / "config.json").write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(mcp_config, "managed_root", return_value=root),
                mock.patch.object(
                    mcp_config.managed_install,
                    "_expected_managed_root",
                    return_value=root,
                ),
            ):
                entry = mcp_config.render_entry(allow_unactivated=True)

        self.assertEqual(
            entry["mcpServers"]["decision-engine"]["cwd"], str(root)
        )

    def test_pre_activation_entry_does_not_weaken_default_root_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "decision-engine"
            with (
                mock.patch.object(mcp_config, "managed_root", return_value=root),
                mock.patch.object(
                    mcp_config.managed_install,
                    "_expected_managed_root",
                    return_value=root,
                ),
                self.assertRaisesRegex(ShellError, "no activated managed Decision Engine install"),
            ):
                mcp_config.render_entry()

    def test_pre_activation_entry_rejects_incomplete_managed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "decision-engine"
            (root / ".git").mkdir(parents=True)
            (root / "installer").mkdir()
            with (
                mock.patch.object(mcp_config, "managed_root", return_value=root),
                mock.patch.object(
                    mcp_config.managed_install,
                    "_expected_managed_root",
                    return_value=root,
                ),
                self.assertRaisesRegex(ShellError, "pre-activation managed install is incomplete"),
            ):
                mcp_config.render_entry(allow_unactivated=True)

    def test_explicit_developer_root_is_launchable(self):
        entry = mcp_config.render_entry(cwd=mcp_config.shell_root())
        de = entry["mcpServers"]["decision-engine"]
        self.assertTrue((Path(de["cwd"]) / "installer" / "launcher.py").is_file())

    def test_custom_name_and_overrides(self):
        entry = mcp_config.render_entry("de", python="/usr/bin/python3", cwd=Path("/opt/shell"))
        de = entry["mcpServers"]["de"]
        self.assertEqual(de["command"], "/usr/bin/python3")
        self.assertEqual(de["cwd"], str(Path("/opt/shell")))
        self.assertNotIn("decision-engine", entry["mcpServers"])

    def test_render_is_valid_json(self):
        parsed = json.loads(mcp_config.render(dev_root=mcp_config.shell_root()))
        self.assertEqual(list(parsed["mcpServers"].keys()), ["decision-engine"])

    def test_entry_carries_no_endpoint_or_secret(self):
        # The registration is transport wiring only — no server address / token.
        blob = mcp_config.render(dev_root=mcp_config.shell_root()).lower()
        for forbidden in ("server_endpoint", "access_token", "api_key", "bearer", "https://"):
            self.assertNotIn(forbidden, blob)


class CodexConfigTestCase(unittest.TestCase):
    def test_codex_entry_is_resolved_with_timeout(self):
        dev_root = mcp_config.shell_root()
        entry = mcp_config.render_codex_entry(dev_root=dev_root)
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"], ["-m", "installer.launcher", "--dev-root", str(dev_root)])
        self.assertEqual(
            entry["env"][mcp_config.CLIENT_HOST_ENV], "codex"
        )
        self.assertEqual(entry["cwd"], str(dev_root))
        # The launcher can use a 60s update budget before stdio is ready; Codex's
        # default 10s MCP startup budget is therefore insufficient.
        self.assertGreaterEqual(entry["startup_timeout_sec"], 120)
        # Keep Codex's host timeout beyond open_ge's ten-minute foreground wait.
        self.assertIsInstance(entry["tool_timeout_sec"], int)
        self.assertGreaterEqual(entry["tool_timeout_sec"], 11 * 60)

    def test_codex_toml_parses_to_the_expected_table(self):
        dev_root = mcp_config.shell_root()
        toml_text = mcp_config.render_codex_toml(dev_root=dev_root)
        parsed = tomllib.loads(toml_text)   # authoritative: the emitted block is valid TOML
        de = parsed["mcp_servers"]["decision-engine"]
        self.assertEqual(de["command"], sys.executable)
        self.assertEqual(de["args"], ["-m", "installer.launcher", "--dev-root", str(dev_root)])
        self.assertEqual(de["env"][mcp_config.CLIENT_HOST_ENV], "codex")
        self.assertEqual(de["cwd"], str(dev_root))
        self.assertGreaterEqual(de["startup_timeout_sec"], 120)
        self.assertGreaterEqual(de["tool_timeout_sec"], 11 * 60)

    def test_codex_toml_quotes_a_path_with_spaces(self):
        toml_text = mcp_config.render_codex_toml(
            "de", python="/usr/bin/python3", cwd=Path("/opt/my shell/root"))
        de = tomllib.loads(toml_text)["mcp_servers"]["de"]
        self.assertEqual(de["cwd"], str(Path("/opt/my shell/root")))
        self.assertEqual(de["command"], "/usr/bin/python3")

    def test_codex_toml_handles_non_bare_names_and_unicode(self):
        # audit 5c44d3a4 (convergent 4/4): a table-header key with a space / dot / quote must be quoted so
        # it stays valid TOML and does NOT silently nest a table; a non-BMP char in a path must not \u-escape.
        for name in ("de local", "de.local", 'de"x', "decision-engine"):
            with self.subTest(name=name):
                de = tomllib.loads(
                    mcp_config.render_codex_toml(name, dev_root=mcp_config.shell_root())
                )["mcp_servers"]
                self.assertIn(name, de, "custom name %r did not round-trip as one table key" % name)
        uni = tomllib.loads(
            mcp_config.render_codex_toml(cwd=Path("/opt/项目😀/root")))["mcp_servers"]["decision-engine"]
        self.assertEqual(uni["cwd"], str(Path("/opt/项目😀/root")))

    def test_codex_toml_carries_no_endpoint_or_secret(self):
        blob = mcp_config.render_codex_toml(dev_root=mcp_config.shell_root()).lower()
        for forbidden in ("server_endpoint", "access_token", "api_key", "bearer", "https://"):
            self.assertNotIn(forbidden, blob)

    def test_cli_codex_flag_emits_toml(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mcp_config.main(["--codex", "--dev-root"])
        self.assertEqual(rc, 0)
        self.assertIn("[mcp_servers.decision-engine]", buf.getvalue())
        self.assertIn("startup_timeout_sec", buf.getvalue())
        self.assertIn("tool_timeout_sec", buf.getvalue())

    def test_cli_default_still_emits_claude_json(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mcp_config.main(["--dev-root"])
        self.assertIn("mcpServers", buf.getvalue())
        self.assertNotIn("tool_timeout_sec", buf.getvalue())

    def test_cli_bare_call_with_no_managed_install_refuses(self):
        import contextlib
        import io

        buf = io.StringIO()
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-installed"
            with (
                mock.patch.object(mcp_config, "managed_root", return_value=missing),
                contextlib.redirect_stdout(buf),
                contextlib.redirect_stderr(err),
            ):
                rc = mcp_config.main([])
        self.assertEqual(rc, 1)
        self.assertIn("no activated managed Decision Engine install", err.getvalue())


class CursorConfigTestCase(unittest.TestCase):
    def test_renderer_registry_owns_entry_generation(self):
        from installer.client_hosts import renderers

        request = renderers.RendererRequest(
            command="python-bin",
            root="managed-root",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=("-c", "bootstrap", "managed-root"),
            environment={mcp_config.CLIENT_HOST_ENV: "future-host"},
            transport="stdio",
            json_include_type=True,
            json_include_cwd=True,
            startup_timeout_sec=120,
            tool_timeout_sec=321,
        )

        self.assertEqual(
            renderers.render_entry("json-mcp-v1", request),
            {
                "type": "stdio",
                "command": "python-bin",
                "args": ["-m", "installer.launcher"],
                "cwd": "managed-root",
                "env": {
                    "PYTHONPATH": "managed-root",
                    mcp_config.CLIENT_HOST_ENV: "future-host",
                },
            },
        )
        self.assertEqual(
            renderers.render_entry("codex-toml-v1", request),
            {
                "command": "python-bin",
                "args": ["-m", "installer.launcher"],
                "cwd": "managed-root",
                "env": {mcp_config.CLIENT_HOST_ENV: "future-host"},
                "startup_timeout_sec": 120,
                "tool_timeout_sec": 321,
            },
        )
        with self.assertRaisesRegex(ShellError, "renderer"):
            renderers.render_entry("future-renderer", request)
        self.assertEqual(
            renderers.parse_server_collection(
                "json-mcp-v1",
                '{"mcpServers":{"de":{"command":"python"}}}',
            ),
            {"de": {"command": "python"}},
        )
        self.assertEqual(
            renderers.parse_server_collection(
                "codex-toml-v1",
                '[mcp_servers.de]\ncommand = "python"\n',
            ),
            {"de": {"command": "python"}},
        )

    def test_launch_policy_owns_host_specific_command_resolution(self):
        from installer.client_hosts import launchers

        request = launchers.LaunchRequest(
            command=r"C:\Program Files\Python313\python.exe",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=("-c", "bootstrap", "managed-root"),
            current_interpreter=r"C:\Program Files\Python313\python.exe",
            python_version=(3, 13),
            platform="win32",
        )
        with mock.patch.object(
            launchers,
            "_find_windows_py_launcher",
            return_value=r"C:\Windows\py.exe",
        ), mock.patch.object(
            launchers,
            "_py_launcher_resolves_to_interpreter",
            return_value=True,
        ):
            resolved = launchers.resolve_launch(
                "windows-py-no-space-v1",
                request,
            )

        self.assertEqual(resolved.command, r"C:\Windows\py.exe")
        self.assertEqual(
            resolved.launcher_args,
            ("-3.13", "-m", "installer.launcher"),
        )
        self.assertEqual(
            resolved.cwd_independent_args,
            ("-3.13", "-c", "bootstrap", "managed-root"),
        )

    def test_absolute_bootstrap_policy_removes_inline_python(self):
        from installer.client_hosts import launchers

        root = r"C:\managed root\decision-engine"
        request = launchers.LaunchRequest(
            command=r"C:\Program Files\Python313\python.exe",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=(
                "-c",
                mcp_config._CWD_INDEPENDENT_BOOTSTRAP,
                root,
                "--managed-root",
                root,
            ),
            current_interpreter=r"C:\Program Files\Python313\python.exe",
            python_version=(3, 13),
            platform="win32",
        )

        resolved = launchers.resolve_launch("absolute-bootstrap-v1", request)

        expected = (
            ntpath.join(root, "installer", "mcp_bootstrap.py"),
            root,
            "--managed-root",
            root,
        )
        self.assertEqual(resolved.command, request.command)
        self.assertEqual(resolved.launcher_args, expected)
        self.assertEqual(resolved.cwd_independent_args, expected)
        self.assertFalse(any(";" in argument for argument in expected))

    def test_absolute_bootstrap_policy_rejects_unrecognized_shape(self):
        from installer.client_hosts import launchers

        request = launchers.LaunchRequest(
            command="python.exe",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=("-c", "foreign code", "root"),
            current_interpreter="python.exe",
            python_version=(3, 13),
            platform="win32",
        )
        with self.assertRaisesRegex(ShellError, "bootstrap shape"):
            launchers.resolve_launch("absolute-bootstrap-v1", request)

    def test_absolute_bootstrap_policy_rejects_inline_source_drift(self):
        from installer.client_hosts import launchers

        root = r"C:\managed\decision-engine"
        request = launchers.LaunchRequest(
            command="python.exe",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=(
                "-c",
                "import a different bootstrap",
                root,
                "--managed-root",
                root,
            ),
            current_interpreter="python.exe",
            python_version=(3, 13),
            platform="win32",
        )
        with self.assertRaisesRegex(ShellError, "bootstrap shape"):
            launchers.resolve_launch("absolute-bootstrap-v1", request)

    def test_launch_policy_fails_closed_for_unusable_windows_command(self):
        from installer.client_hosts import launchers

        request = launchers.LaunchRequest(
            command=r"C:\Program Files\Custom Python\python.exe",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=("-c", "bootstrap", "managed-root"),
            current_interpreter=r"C:\Python313\python.exe",
            python_version=(3, 13),
            platform="win32",
        )
        with self.assertRaisesRegex(ShellError, "space-free"):
            launchers.resolve_launch("windows-py-no-space-v1", request)

        current = replace(
            request,
            current_interpreter=request.command,
        )
        with (
            mock.patch.object(
                launchers,
                "_find_windows_py_launcher",
                return_value=None,
            ),
            self.assertRaisesRegex(ShellError, "Python Launcher"),
        ):
            launchers.resolve_launch("windows-py-no-space-v1", current)

        with (
            mock.patch.object(
                launchers,
                "_find_windows_py_launcher",
                return_value=r"C:\Windows\py.exe",
            ),
            mock.patch.object(
                launchers,
                "_py_launcher_resolves_to_interpreter",
                return_value=False,
            ),
            self.assertRaisesRegex(ShellError, "different interpreter"),
        ):
            launchers.resolve_launch("windows-py-no-space-v1", current)

    def test_host_launch_policy_is_metadata_driven(self):
        from installer.client_hosts import launchers

        newcomer = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            id="newcomer",
            host_family="newcomer",
            launch_policy="windows-py-no-space-v1",
        )
        resolved = launchers.LaunchCommand(
            command="space-free-python",
            launcher_args=("selector", "-m", "installer.launcher"),
            cwd_independent_args=("selector", "-c", "bootstrap", "root"),
        )
        with (
            mock.patch.object(mcp_config, "CLIENT_SPECS", {"newcomer": newcomer}),
            mock.patch.object(
                launchers,
                "resolve_launch",
                return_value=resolved,
            ) as resolve_launch,
        ):
            entry = mcp_config.render_entry(
                client="newcomer",
                python="python with spaces",
                cwd=Path("root"),
            )["mcpServers"]["decision-engine"]

        self.assertEqual(entry["command"], "space-free-python")
        self.assertEqual(
            entry["args"],
            ["selector", "-c", "bootstrap", "root"],
        )
        self.assertEqual(resolve_launch.call_args.args[0], "windows-py-no-space-v1")

    def test_client_host_registry_is_owned_by_modular_package(self):
        from installer.client_hosts import registry

        self.assertEqual(mcp_config.CLIENT_SPECS, registry.CLIENT_SPECS)
        self.assertIsNot(mcp_config.CLIENT_SPECS, registry.CLIENT_SPECS)
        self.assertEqual(
            registry.CLIENTS,
            (
                "claude-code",
                "claude-desktop",
                "codex",
                "cursor",
                "qoder",
                "qoder-cn",
                "trae",
                "trae-work",
                "trae-cn",
                "trae-work-cn",
                "workbuddy",
            ),
        )
        with self.assertRaises(TypeError):
            registry.CLIENT_SPECS["new-host"] = registry.CLIENT_SPECS["cursor"]

    def test_trae_work_cn_is_a_display_plus_stop_modular_host(self):
        spec = mcp_config.CLIENT_SPECS["trae-work-cn"]

        self.assertEqual(spec.host_family, "trae-work-cn")
        self.assertEqual(spec.config_renderer, "json-mcp-v1")
        self.assertEqual(spec.launch_policy, "desktop-python-v1")
        self.assertEqual(spec.observed_client_aliases, frozenset({"trae"}))
        self.assertFalse(spec.require_observed_identity)
        self.assertEqual(spec.onboarding_evidence, ("skill",))
        self.assertEqual(spec.skills_project_paths, (".trae/skills",))
        self.assertEqual(spec.skill_delivery_mode, "managed-copy")
        self.assertEqual(spec.routing_kind, "skill")
        self.assertEqual(spec.skill_route_name, "trae-work-cn")
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

    def test_trae_work_is_a_distinct_skill_aware_modular_host(self):
        spec = mcp_config.CLIENT_SPECS["trae-work"]

        self.assertEqual(spec.host_family, "trae-work")
        self.assertEqual(spec.config_renderer, "json-mcp-v1")
        self.assertEqual(spec.launch_policy, "desktop-python-v1")
        self.assertEqual(spec.observed_client_aliases, frozenset({"trae"}))
        self.assertFalse(spec.require_observed_identity)
        self.assertEqual(spec.onboarding_evidence, ("skill",))
        self.assertEqual(spec.skills_project_paths, (".trae/skills",))
        self.assertEqual(spec.skill_delivery_mode, "managed-copy")
        self.assertEqual(spec.routing_kind, "skill")
        self.assertEqual(spec.skill_route_name, "trae-work")
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

    def test_workbuddy_is_a_skill_aware_display_plus_stop_modular_host(self):
        from installer.client_hosts.hosts import workbuddy

        spec = mcp_config.CLIENT_SPECS["workbuddy"]

        self.assertEqual(spec.host_family, "workbuddy")
        self.assertEqual(spec.config_renderer, "json-mcp-v1")
        self.assertEqual(spec.launch_policy, "desktop-python-v1")
        self.assertEqual(
            spec.observed_client_aliases,
            frozenset({"connector:custom-mcp:decision-engine"}),
        )
        self.assertTrue(spec.require_observed_identity)
        self.assertEqual(spec.onboarding_evidence, ("skill",))
        self.assertEqual(spec.skills_project_paths, ())
        self.assertEqual(spec.skill_delivery_mode, "managed-copy")
        self.assertEqual(spec.routing_kind, "skill")
        self.assertEqual(spec.skill_route_name, "workbuddy")
        with mock.patch.dict(os.environ, {"DE_UI_LOCALE": "en-US"}):
            english = spec.post_mcp_write_notice()
        with mock.patch.dict(os.environ, {"DE_UI_LOCALE": "zh-CN"}):
            chinese = spec.post_mcp_write_notice()
        self.assertIn("Custom connectors", english)
        self.assertIn("Trust", english)
        self.assertIn("restart WorkBuddy", english)
        self.assertIn("自定义连接器", chinese)
        self.assertIn("信任", chinese)
        self.assertIn("Custom connectors", chinese)
        self.assertIn("Trust", chinese)

        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DE_UI_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG"}
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}),
        ):
            self.assertIn("自定义连接器", spec.post_mcp_write_notice())
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(workbuddy, "_windows_user_locale", return_value=None),
            mock.patch.object(
                workbuddy.locale, "getlocale", side_effect=workbuddy.locale.Error
            ),
        ):
            self.assertIn("action required", spec.post_mcp_write_notice())
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(workbuddy, "_windows_user_locale", return_value=None),
            mock.patch.object(workbuddy.locale, "getlocale", return_value=()),
        ):
            self.assertIn("action required", spec.post_mcp_write_notice())
        for system_locale in (
            "zh-CN",
            "Chinese (Simplified)_China.936",
            "Chinese (Traditional)_Taiwan.950",
        ):
            with self.subTest(system_locale=system_locale), mock.patch.dict(
                os.environ, environment, clear=True
            ), mock.patch.object(
                workbuddy, "_system_ui_locale", return_value=system_locale
            ):
                self.assertIn("自定义连接器", spec.post_mcp_write_notice())
        with (
            mock.patch.dict(
                os.environ,
                {"DE_UI_LOCALE": "en-US", "LANG": "zh_CN.UTF-8"},
                clear=True,
            ),
        ):
            self.assertIn("action required", spec.post_mcp_write_notice())
        self.assertTrue(spec.repair_skills_on_setup)
        self.assertTrue(spec.skills_require_managed_target)
        self.assertEqual(
            spec.doctor_capabilities, frozenset({"mcp-entry", "skills"})
        )
        self.assertEqual(
            spec.launcher_capabilities,
            frozenset({"core-mcp", "local-display", "audit-stop-panel"}),
        )
        self.assertTrue(spec.local_display_tools)
        self.assertFalse(spec.popup_followup)
        self.assertTrue(spec.audit_stop_panel)
        self.assertEqual(spec.popup_api_profile, "legacy")

    def test_workbuddy_webview_guard_triggers_on_workbuddy_ancestry(self):
        from installer.client_hosts.hosts import workbuddy

        guard = mcp_config.CLIENT_SPECS["workbuddy"].webview_render_guard_probe
        self.assertTrue(callable(guard))

        sandbox_ancestry = ("bash.exe", "sandbox-cli.exe", "WorkBuddy.exe")
        with (
            mock.patch.object(workbuddy.sys, "platform", "win32"),
            mock.patch.object(
                workbuddy,
                "_windows_process_ancestor_names",
                return_value=sandbox_ancestry,
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            reason = guard()

        self.assertIn("WorkBuddy", reason)
        self.assertIn("Windows", reason)
        self.assertIn("reliably", reason)
        self.assertIn("Tk", reason)  # the caller degrades to Tk instead of refusing

        for approval_value in ("1", "true", ""):
            with (
                self.subTest(approval_value=approval_value),
                mock.patch.object(workbuddy.sys, "platform", "win32"),
                mock.patch.object(
                    workbuddy,
                    "_windows_process_ancestor_names",
                    return_value=sandbox_ancestry,
                ),
                mock.patch.dict(
                    os.environ,
                    {"DE_WORKBUDDY_OUTSIDE_SANDBOX_APPROVED": approval_value},
                    clear=True,
                ),
            ):
                self.assertEqual(guard(), reason)

        # The documented WorkBuddy command carries this marker. It must remain
        # sufficient even if an outside-sandbox launcher detaches the process
        # and WorkBuddy disappears from the observable ancestor snapshot.
        with (
            mock.patch.object(workbuddy.sys, "platform", "win32"),
            mock.patch.object(
                workbuddy,
                "_windows_process_ancestor_names",
                return_value=("bash.exe",),
            ),
            mock.patch.dict(
                os.environ,
                {"DE_WORKBUDDY_OUTSIDE_SANDBOX_APPROVED": "1"},
                clear=True,
            ),
        ):
            self.assertIn("WorkBuddy", guard())

        with (
            mock.patch.object(workbuddy.sys, "platform", "win32"),
            mock.patch.object(
                workbuddy,
                "_windows_process_ancestor_names",
                return_value=("bash.exe",),
            ),
            mock.patch.dict(
                os.environ,
                {"DE_WORKBUDDY_SETUP": "1"},
                clear=True,
            ),
        ):
            self.assertEqual(guard(), reason)

        for marker in (
            "DE_WORKBUDDY_SETUP",
            "DE_WORKBUDDY_OUTSIDE_SANDBOX_APPROVED",
        ):
            with (
                self.subTest(marker=marker, ancestry="unavailable"),
                mock.patch.object(workbuddy.sys, "platform", "win32"),
                mock.patch.object(
                    workbuddy,
                    "_windows_process_ancestor_names",
                    side_effect=OSError("snapshot unavailable"),
                ),
                mock.patch.dict(os.environ, {marker: "1"}, clear=True),
            ):
                self.assertEqual(guard(), reason)

        for marker_value in ("true", "0", ""):
            with (
                self.subTest(marker_value=marker_value, ancestry="detached"),
                mock.patch.object(workbuddy.sys, "platform", "win32"),
                mock.patch.object(
                    workbuddy,
                    "_windows_process_ancestor_names",
                    return_value=("bash.exe",),
                ),
                mock.patch.dict(
                    os.environ,
                    {"DE_WORKBUDDY_SETUP": marker_value},
                    clear=True,
                ),
            ):
                self.assertIsNone(guard())

        # workbuddy.exe ALONE must trigger: a sandboxed run may hide or rename
        # the sandbox launcher in its chain, and a missed sandbox must never
        # cost the user a black-box WebView window (measured on a real install).
        with (
            mock.patch.object(workbuddy.sys, "platform", "win32"),
            mock.patch.object(
                workbuddy,
                "_windows_process_ancestor_names",
                return_value=("bash.exe", "WorkBuddy.exe"),
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertIn("WorkBuddy", guard())

        with (
            mock.patch.object(workbuddy.sys, "platform", "win32"),
            mock.patch.object(
                workbuddy,
                "_windows_process_ancestor_names",
                return_value=("bash.exe", "sandbox-cli.exe"),
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertIsNone(guard())

    def test_workbuddy_process_ancestor_walk_handles_boundaries(self):
        from installer.client_hosts.hosts import workbuddy

        processes = {
            100: (90, "python.exe"),
            90: (80, "bash.exe"),
            80: (70, "sandbox-cli.exe"),
            70: (0, "WorkBuddy.exe"),
        }
        self.assertEqual(
            workbuddy._process_ancestor_names(processes, 100),
            ("bash.exe", "sandbox-cli.exe", "WorkBuddy.exe"),
        )
        self.assertEqual(
            workbuddy._process_ancestor_names(processes, 100, max_depth=1),
            ("bash.exe",),
        )
        self.assertEqual(
            workbuddy._process_ancestor_names({100: (90, "python.exe")}, 100),
            (),
        )
        self.assertEqual(
            workbuddy._process_ancestor_names(
                {100: (90, "python.exe"), 90: (100, "bash.exe")},
                100,
            ),
            ("bash.exe",),
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows Toolhelp32 smoke test")
    def test_workbuddy_windows_process_snapshot_returns_real_ancestor_names(self):
        from installer.client_hosts.hosts import workbuddy

        names = workbuddy._windows_process_ancestor_names()

        self.assertIsInstance(names, tuple)
        self.assertTrue(names)
        self.assertTrue(all(isinstance(name, str) and name for name in names))

    def test_only_workbuddy_registers_a_webview_render_guard(self):
        guarded = {
            client
            for client, spec in mcp_config.CLIENT_SPECS.items()
            if spec.webview_render_guard_probe is not None
        }
        self.assertEqual(guarded, {"workbuddy"})

        # The retired total-GUI guard is gone: the Tk form renders inside
        # WorkBuddy's sandbox, so no host may refuse the setup window outright.
        interactive_guards = {
            client
            for client, spec in mcp_config.CLIENT_SPECS.items()
            if spec.interactive_setup_guard_probe is not None
        }
        self.assertEqual(interactive_guards, set())

    def test_workbuddy_setup_guard_is_windows_only_and_fails_open(self):
        from installer.client_hosts.hosts import workbuddy

        guard = mcp_config.CLIENT_SPECS["workbuddy"].webview_render_guard_probe
        with (
            mock.patch.object(workbuddy.sys, "platform", "darwin"),
            mock.patch.object(
                workbuddy,
                "_windows_process_ancestor_names",
            ) as ancestry,
        ):
            self.assertIsNone(guard())
        ancestry.assert_not_called()

        with (
            mock.patch.object(workbuddy.sys, "platform", "win32"),
            mock.patch.object(
                workbuddy,
                "_windows_process_ancestor_names",
                side_effect=OSError("snapshot unavailable"),
            ),
        ):
            self.assertIsNone(guard())

    def test_registry_interactive_setup_guard_dispatches_host_callbacks(self):
        from installer.client_hosts import registry

        callback = mock.Mock(return_value="approved outside-sandbox rerun required")
        synthetic = replace(
            mcp_config.CLIENT_SPECS["codex"],
            interactive_setup_guard_probe=callback,
        )
        with mock.patch.object(registry, "CLIENT_SPECS", {"codex": synthetic}):
            self.assertEqual(
                registry.interactive_setup_blocked_reason(),
                "approved outside-sandbox rerun required",
            )
        callback.assert_called_once_with()

    def test_registry_interactive_setup_guard_fails_open_on_probe_error(self):
        from installer.client_hosts import registry

        callback = mock.Mock(side_effect=OSError("optional probe unavailable"))
        synthetic = replace(
            mcp_config.CLIENT_SPECS["codex"],
            interactive_setup_guard_probe=callback,
        )
        with mock.patch.object(registry, "CLIENT_SPECS", {"codex": synthetic}):
            self.assertIsNone(registry.interactive_setup_blocked_reason())

    def test_host_contract_rejects_non_callable_interactive_setup_guard(self):
        invalid = replace(
            mcp_config.CLIENT_SPECS["codex"],
            interactive_setup_guard_probe="not-callable",
        )
        with self.assertRaisesRegex(ShellError, "interactive setup guard"):
            mcp_config.validate_host_specs({"codex": invalid})

    def test_registry_webview_render_guard_dispatches_host_callbacks(self):
        from installer.client_hosts import registry

        callback = mock.Mock(return_value="webview renders unreliably here")
        synthetic = replace(
            mcp_config.CLIENT_SPECS["codex"],
            webview_render_guard_probe=callback,
        )
        with mock.patch.object(registry, "CLIENT_SPECS", {"codex": synthetic}):
            self.assertEqual(
                registry.webview_render_blocked_reason(),
                "webview renders unreliably here",
            )
        callback.assert_called_once_with()

    def test_registry_webview_render_guard_fails_open_on_probe_error(self):
        from installer.client_hosts import registry

        callback = mock.Mock(side_effect=OSError("optional probe unavailable"))
        synthetic = replace(
            mcp_config.CLIENT_SPECS["codex"],
            webview_render_guard_probe=callback,
        )
        with mock.patch.object(registry, "CLIENT_SPECS", {"codex": synthetic}):
            self.assertIsNone(registry.webview_render_blocked_reason())

    def test_host_contract_rejects_non_callable_webview_render_guard(self):
        invalid = replace(
            mcp_config.CLIENT_SPECS["codex"],
            webview_render_guard_probe="not-callable",
        )
        with self.assertRaisesRegex(ShellError, "webview render guard"):
            mcp_config.validate_host_specs({"codex": invalid})

    def test_host_contract_validates_every_spec_not_just_the_last_one(self):
        # Regression guard (caught by an external audit): the guard-probe checks
        # were once mis-indented to function level, AFTER the per-spec loop, so
        # an invalid probe on a NON-last spec was silently accepted via the
        # leaked loop variable — every single-entry test missed it.
        invalid_probe = replace(
            mcp_config.CLIENT_SPECS["codex"],
            webview_render_guard_probe="not-callable",
        )
        specs = {
            "codex": invalid_probe,
            "claude-code": mcp_config.CLIENT_SPECS["claude-code"],
        }
        with self.assertRaisesRegex(ShellError, "webview render guard"):
            mcp_config.validate_host_specs(specs)

        invalid_policy = replace(
            mcp_config.CLIENT_SPECS["codex"],
            launch_policy="bogus-policy",
        )
        specs = {
            "codex": invalid_policy,
            "claude-code": mcp_config.CLIENT_SPECS["claude-code"],
        }
        with self.assertRaisesRegex(ShellError, "launch policy"):
            mcp_config.validate_host_specs(specs)

        # An empty mapping must not trip over an unbound loop variable either.
        mcp_config.validate_host_specs({})

    def test_workbuddy_paths_are_host_owned_and_environment_overridable(self):
        with mock.patch.dict(
            os.environ,
            {
                "WORKBUDDY_CONFIG": "",
                "WORKBUDDY_SKILLS_DIR": r"C:\isolated\workbuddy-skills",
            },
            clear=False,
        ), mock.patch.object(Path, "home", return_value=Path(r"C:\Users\test")):
            self.assertEqual(
                mcp_config.agent_config_path("workbuddy"),
                Path(r"C:\Users\test") / ".workbuddy" / "mcp.json",
            )
            self.assertEqual(
                mcp_config.CLIENT_SPECS["workbuddy"].skills_global_path(),
                Path(r"C:\isolated\workbuddy-skills"),
            )

    def test_workbuddy_skill_route_and_write_guard_require_desktop_install(self):
        from installer.client_hosts.hosts import workbuddy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "app"
            skills = root / ".workbuddy" / "skills"
            with (
                mock.patch.object(workbuddy.sys, "platform", "win32"),
                mock.patch.dict(
                    os.environ,
                    {
                        "WORKBUDDY_APP_ROOT": str(app_root),
                        "WORKBUDDY_SKILLS_DIR": str(skills),
                    },
                    clear=False,
                ),
            ):
                self.assertFalse(workbuddy._workbuddy_skills_in_use())
                with self.assertRaisesRegex(ShellError, "workbuddy_not_installed"):
                    workbuddy._workbuddy_config_write_guard()

                skills.parent.mkdir(parents=True)
                app_root.mkdir(parents=True)
                (app_root / "WorkBuddy.exe").touch()

                self.assertTrue(workbuddy._workbuddy_skills_in_use())
                self.assertIsNone(workbuddy._workbuddy_config_write_guard())

                with mock.patch.object(workbuddy.sys, "platform", "linux"):
                    self.assertFalse(workbuddy._workbuddy_skills_in_use())
                    with self.assertRaisesRegex(
                        ShellError, "unsupported_workbuddy_platform"
                    ):
                        workbuddy._workbuddy_config_write_guard()

    def test_workbuddy_skill_route_follows_install_when_custom_root_is_new(self):
        from installer.client_hosts.hosts import workbuddy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "app"
            app_root.mkdir()
            (app_root / "WorkBuddy.exe").write_bytes(b"signed-product-probe")
            with (
                mock.patch.object(workbuddy.sys, "platform", "win32"),
                mock.patch.dict(
                    os.environ,
                    {
                        "WORKBUDDY_APP_ROOT": str(app_root),
                        "WORKBUDDY_SKILLS_DIR": str(root / "new" / "skills"),
                    },
                    clear=False,
                ),
            ):
                self.assertTrue(workbuddy._workbuddy_skills_in_use())

    def test_workbuddy_doctor_route_requires_its_mcp_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".workbuddy" / "mcp.json"
            skills = root / ".workbuddy" / "skills"
            skills.parent.mkdir(parents=True)
            with mock.patch.dict(
                os.environ,
                {
                    "WORKBUDDY_APP_ROOT": str(root / "app"),
                    "WORKBUDDY_CONFIG": str(config_path),
                    "WORKBUDDY_SKILLS_DIR": str(skills),
                },
                clear=False,
            ):
                self.assertNotIn(
                    "workbuddy", mcp_config.active_skill_routes(for_doctor=True)
                )
                config_path.write_text(
                    json.dumps(
                        mcp_config.render_entry(client="workbuddy", cwd=root)
                    ),
                    encoding="utf-8",
                )
                route, excluded = mcp_config.active_skill_routes(
                    for_doctor=True
                )["workbuddy"]

        self.assertEqual(route, skills)
        self.assertEqual(excluded, frozenset())

    def test_workbuddy_renderer_matches_verified_stdio_shape(self):
        from installer.client_hosts import launchers

        python_with_spaces = r"C:\Program Files\Python313\python.exe"
        root = Path(r"C:\managed root")
        with (
            mock.patch.object(mcp_config.sys, "platform", "win32"),
            mock.patch.object(mcp_config.sys, "executable", python_with_spaces),
            mock.patch.object(
                launchers,
                "_find_windows_py_launcher",
                return_value=r"C:\Windows\py.exe",
            ),
            mock.patch.object(
                launchers,
                "_py_launcher_resolves_to_interpreter",
                return_value=True,
            ),
        ):
            entry = mcp_config.render_entry(
                client="workbuddy",
                cwd=root,
            )["mcpServers"]["decision-engine"]

        self.assertEqual(
            set(entry),
            {"command", "args", "env"},
            "WorkBuddy's user MCP schema does not require type/cwd extensions",
        )
        self.assertEqual(entry["command"], r"C:\Windows\py.exe")
        self.assertEqual(entry["args"][0], "-%d.%d" % (
            mcp_config.sys.version_info.major,
            mcp_config.sys.version_info.minor,
        ))
        self.assertEqual(entry["args"][1], "-c")
        self.assertEqual(entry["args"][-2:], ["--managed-root", str(root)])
        self.assertEqual(entry["env"]["PYTHONPATH"], str(root))
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], "workbuddy")
        self.assertNotIn("type", entry)
        self.assertNotIn("cwd", entry)

    def test_trae_work_paths_are_host_owned_and_environment_overridable(self):
        from installer.client_hosts.hosts import trae_work

        with (
            mock.patch.object(trae_work.sys, "platform", "win32"),
            mock.patch.dict(
                os.environ,
                {
                    "APPDATA": r"C:\Users\test\AppData\Roaming",
                    "TRAE_WORK_CONFIG": "",
                    "TRAE_WORK_SKILLS_DIR": r"C:\isolated\skills",
                },
                clear=False,
            ),
        ):
            self.assertEqual(
                mcp_config.agent_config_path("trae-work"),
                Path(r"C:\Users\test\AppData\Roaming")
                / "TRAE SOLO"
                / "User"
                / "mcp.json",
            )
            self.assertEqual(
                mcp_config.CLIENT_SPECS["trae-work"].skills_global_path(),
                Path(r"C:\isolated\skills"),
            )

    def test_trae_work_renderer_matches_verified_stdio_shape(self):
        from installer.client_hosts import launchers

        python_with_spaces = r"C:\Program Files\Python313\python.exe"
        with (
            mock.patch.object(mcp_config.sys, "platform", "win32"),
            mock.patch.object(mcp_config.sys, "executable", python_with_spaces),
            mock.patch.object(
                launchers,
                "_find_windows_py_launcher",
                return_value=r"C:\Windows\py.exe",
            ),
            mock.patch.object(
                launchers,
                "_py_launcher_resolves_to_interpreter",
                return_value=True,
            ),
        ):
            entry = mcp_config.render_entry(
                client="trae-work",
                cwd=Path(r"C:\managed root"),
            )["mcpServers"]["decision-engine"]

        self.assertEqual(entry["command"], r"C:\Windows\py.exe")
        self.assertEqual(
            entry["args"][:3],
            [
                "-%d.%d" % (
                    mcp_config.sys.version_info.major,
                    mcp_config.sys.version_info.minor,
                ),
                "-m",
                "installer.launcher",
            ],
        )
        self.assertEqual(entry["cwd"], r"C:\managed root")
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], "trae-work")
        self.assertNotIn("type", entry)

    def test_trae_work_skill_route_requires_product_presence(self):
        from installer.client_hosts.hosts import trae_work

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            skills = base / ".trae" / "skills"
            app_root = base / "app"
            product = app_root / "resources" / "app" / "product.json"
            product.parent.mkdir(parents=True)
            with (
                mock.patch.object(trae_work.sys, "platform", "win32"),
                mock.patch.dict(
                    os.environ,
                    {
                        "TRAE_WORK_SKILLS_DIR": str(skills),
                        "TRAE_WORK_APP_ROOT": str(app_root),
                    },
                    clear=False,
                ),
            ):
                self.assertFalse(trae_work._trae_work_skills_in_use())
                skills.parent.mkdir(parents=True)
                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo-cn",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(trae_work._trae_work_skills_in_use())
                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertTrue(trae_work._trae_work_skills_in_use())

    def test_trae_work_doctor_route_requires_its_mcp_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            trae_config = base / "profile" / "User" / "mcp.json"
            trae_skills = base / ".trae" / "skills"
            trae_skills.parent.mkdir(parents=True)
            with mock.patch.dict(
                os.environ,
                {
                    "TRAE_WORK_CONFIG": str(trae_config),
                    "TRAE_WORK_SKILLS_DIR": str(trae_skills),
                },
                clear=False,
            ):
                self.assertNotIn(
                    "trae-work", mcp_config.active_skill_routes(for_doctor=True)
                )
                trae_config.parent.mkdir(parents=True)
                trae_config.write_text(
                    json.dumps(
                        mcp_config.render_entry(client="trae-work", cwd=base)
                    ),
                    encoding="utf-8",
                )
                route, excluded = mcp_config.active_skill_routes(
                    for_doctor=True
                )["trae-work"]

        self.assertEqual(route, trae_skills)
        self.assertEqual(excluded, frozenset())

    def test_trae_work_cn_paths_are_host_owned_and_environment_overridable(self):
        from installer.client_hosts.hosts import trae_work_cn

        with (
            mock.patch.object(trae_work_cn.sys, "platform", "win32"),
            mock.patch.dict(
                os.environ,
                {
                    "APPDATA": r"C:\Users\test\AppData\Roaming",
                    "TRAE_WORK_CN_CONFIG": "",
                    "TRAE_WORK_CN_SKILLS_DIR": r"C:\isolated\skills",
                },
                clear=False,
            ),
        ):
            self.assertEqual(
                mcp_config.agent_config_path("trae-work-cn"),
                Path(r"C:\Users\test\AppData\Roaming")
                / "TRAE SOLO CN"
                / "User"
                / "mcp.json",
            )
            self.assertEqual(
                mcp_config.CLIENT_SPECS["trae-work-cn"].skills_global_path(),
                Path(r"C:\isolated\skills"),
            )

    def test_trae_work_cn_skill_route_requires_cn_product_presence(self):
        from installer.client_hosts.hosts import trae_work_cn

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            skills = base / ".trae-cn" / "skills"
            app_root = base / "app"
            product = app_root / "resources" / "app" / "product.json"
            product.parent.mkdir(parents=True)
            with (
                mock.patch.object(trae_work_cn.sys, "platform", "win32"),
                mock.patch.dict(
                    os.environ,
                    {
                        "TRAE_WORK_CN_SKILLS_DIR": str(skills),
                        "TRAE_WORK_CN_APP_ROOT": str(app_root),
                    },
                    clear=False,
                ),
            ):
                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo-cn",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(trae_work_cn._trae_work_cn_skills_in_use())
                skills.parent.mkdir(parents=True)
                self.assertTrue(trae_work_cn._trae_work_cn_skills_in_use())
                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(trae_work_cn._trae_work_cn_skills_in_use())
                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo-cn",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertTrue(trae_work_cn._trae_work_cn_skills_in_use())

    def test_trae_work_cn_doctor_route_requires_its_mcp_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            trae_config = base / "profile" / "User" / "mcp.json"
            trae_skills = base / ".trae-cn" / "skills"
            trae_skills.parent.mkdir(parents=True)
            with mock.patch.dict(
                os.environ,
                {
                    "TRAE_WORK_CN_CONFIG": str(trae_config),
                    "TRAE_WORK_CN_SKILLS_DIR": str(trae_skills),
                },
                clear=False,
            ):
                self.assertNotIn(
                    "trae-work-cn", mcp_config.active_skill_routes(for_doctor=True)
                )
                trae_config.parent.mkdir(parents=True)
                trae_config.write_text(
                    json.dumps(
                        mcp_config.render_entry(client="trae-work-cn", cwd=base)
                    ),
                    encoding="utf-8",
                )
                route, excluded = mcp_config.active_skill_routes(
                    for_doctor=True
                )["trae-work-cn"]

        self.assertEqual(route, trae_skills)
        self.assertEqual(excluded, frozenset())

    def test_trae_work_cn_renderer_matches_verified_stdio_shape(self):
        from installer.client_hosts import launchers

        python_with_spaces = r"C:\Program Files\Python313\python.exe"
        with (
            mock.patch.object(mcp_config.sys, "platform", "win32"),
            mock.patch.object(mcp_config.sys, "executable", python_with_spaces),
            mock.patch.object(
                launchers,
                "_find_windows_py_launcher",
                return_value=r"C:\Windows\py.exe",
            ),
            mock.patch.object(
                launchers,
                "_py_launcher_resolves_to_interpreter",
                return_value=True,
            ),
        ):
            entry = mcp_config.render_entry(
                client="trae-work-cn",
                cwd=Path(r"C:\managed root"),
            )["mcpServers"]["decision-engine"]

        self.assertEqual(entry["command"], r"C:\Windows\py.exe")
        self.assertEqual(
            entry["args"][:3],
            [
                "-%d.%d" % (mcp_config.sys.version_info.major, mcp_config.sys.version_info.minor),
                "-m",
                "installer.launcher",
            ],
        )
        self.assertEqual(entry["cwd"], r"C:\managed root")
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], "trae-work-cn")
        self.assertNotIn("type", entry)

    def test_trae_json_write_preserves_siblings(self):
        from installer.client_hosts.hosts import trae_work

        for client, env_name in (
            ("trae-work", "TRAE_WORK_CONFIG"),
            ("trae-work-cn", "TRAE_WORK_CN_CONFIG"),
        ):
            with self.subTest(client=client), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "profile" / "User" / "mcp.json"
                path.parent.mkdir(parents=True)
                path.write_text(
                    json.dumps(
                        {
                            "setting": {"owned": "by-user"},
                            "mcpServers": {"sibling": {"command": "keep-me"}},
                        }
                    ),
                    encoding="utf-8",
                )
                with (
                    mock.patch.dict(os.environ, {env_name: str(path)}, clear=False),
                    mock.patch.object(
                        trae_work, "_trae_work_config_write_guard", return_value=None
                    ),
                ):
                    result = mcp_config.write_entry(
                        client, python=sys.executable, cwd=root
                    )

                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(result["action"], "added")
                self.assertEqual(loaded["setting"], {"owned": "by-user"})
                self.assertEqual(
                    loaded["mcpServers"]["sibling"], {"command": "keep-me"}
                )
                self.assertNotIn("type", loaded["mcpServers"]["decision-engine"])

    def test_trae_marked_entry_can_be_updated_and_repeated(self):
        from installer.client_hosts.hosts import trae_work

        for client, env_name in (
            ("trae-work", "TRAE_WORK_CONFIG"),
            ("trae-work-cn", "TRAE_WORK_CN_CONFIG"),
        ):
            with self.subTest(client=client), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "profile" / "User" / "mcp.json"
                path.parent.mkdir(parents=True)
                old_entry = mcp_config.render_entry(
                    client=client,
                    python="python-bin",
                    cwd=root / "old-managed-root",
                )["mcpServers"]["decision-engine"]
                path.write_text(
                    json.dumps({"mcpServers": {"decision-engine": old_entry}}),
                    encoding="utf-8",
                )
                with (
                    mock.patch.dict(os.environ, {env_name: str(path)}, clear=False),
                    mock.patch.object(
                        trae_work,
                        "_trae_work_config_write_guard",
                        return_value=None,
                    ),
                ):
                    updated = mcp_config.write_entry(
                        client,
                        python=sys.executable,
                        cwd=root,
                    )
                    preimage = path.read_bytes()
                    repeated = mcp_config.write_entry(
                        client,
                        python=sys.executable,
                        cwd=root,
                    )
                    self.assertEqual(path.read_bytes(), preimage)

                self.assertEqual(updated["action"], "updated")
                self.assertEqual(repeated["action"], "unchanged")
                self.assertIsNone(repeated["backup"])

    def test_trae_refuses_a_foreign_same_name_entry(self):
        from installer.client_hosts.hosts import trae_work

        for client, env_name in (
            ("trae-work", "TRAE_WORK_CONFIG"),
            ("trae-work-cn", "TRAE_WORK_CN_CONFIG"),
        ):
            with self.subTest(client=client), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "profile" / "User" / "mcp.json"
                path.parent.mkdir(parents=True)
                original = json.dumps(
                    {
                        "mcpServers": {
                            "decision-engine": {
                                "command": "user-owned-command",
                                "opaque": "keep",
                            }
                        }
                    },
                    separators=(",", ":"),
                )
                path.write_text(original, encoding="utf-8")
                with (
                    mock.patch.dict(os.environ, {env_name: str(path)}, clear=False),
                    mock.patch.object(
                        trae_work,
                        "_trae_work_config_write_guard",
                        return_value=None,
                    ),
                    self.assertRaisesRegex(ShellError, "same_name_unowned"),
                ):
                    mcp_config.write_entry(
                        client,
                        python=sys.executable,
                        cwd=root,
                    )

                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_trae_refuses_a_marked_entry_with_opaque_fields(self):
        from installer.client_hosts.hosts import trae_work

        for client, env_name in (
            ("trae-work", "TRAE_WORK_CONFIG"),
            ("trae-work-cn", "TRAE_WORK_CN_CONFIG"),
        ):
            with self.subTest(client=client), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "profile" / "User" / "mcp.json"
                path.parent.mkdir(parents=True)
                marked_entry = mcp_config.render_entry(
                    client=client,
                    python="python-bin",
                    cwd=root / "old-managed-root",
                )["mcpServers"]["decision-engine"]
                marked_entry["opaque"] = "user-owned"
                original = json.dumps(
                    {"mcpServers": {"decision-engine": marked_entry}},
                    separators=(",", ":"),
                )
                path.write_text(original, encoding="utf-8")
                with (
                    mock.patch.dict(os.environ, {env_name: str(path)}, clear=False),
                    mock.patch.object(
                        trae_work,
                        "_trae_work_config_write_guard",
                        return_value=None,
                    ),
                    self.assertRaisesRegex(ShellError, "same_name_unowned"),
                ):
                    mcp_config.write_entry(
                        client,
                        python=sys.executable,
                        cwd=root,
                    )

                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_host_transport_contract_is_explicit_and_fail_closed(self):
        self.assertTrue(
            all(
                spec.transport == "stdio"
                for spec in mcp_config.CLIENT_SPECS.values()
            )
        )
        invalid = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            transport="streamable-http",
        )
        with self.assertRaisesRegex(ShellError, "transport"):
            mcp_config.validate_host_specs({"cursor": invalid})

    def test_cursor_callbacks_preserve_legacy_late_bound_probe_seams(self):
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        with mock.patch.object(
            mcp_config,
            "_cursor_skills_in_use",
            return_value=True,
        ) as in_use:
            self.assertTrue(cursor.skills_in_use())
            in_use.assert_called_once_with()
        with mock.patch.object(
            mcp_config,
            "_cursor_skills_configured",
            return_value=True,
        ) as configured:
            self.assertTrue(cursor.skills_check_in_use())
            configured.assert_called_once_with()

    def test_cursor_legacy_probe_keeps_original_path_resolution_call_shape(self):
        configured_path = Path("cursor-config.json")
        with (
            mock.patch.object(
                mcp_config,
                "agent_config_path",
                return_value=configured_path,
            ),
            mock.patch.object(
                mcp_config,
                "_client_present",
                return_value=True,
            ) as present,
            mock.patch.dict(os.environ, {"CURSOR_SKILLS_DIR": ""}),
        ):
            self.assertTrue(mcp_config._cursor_skills_in_use())
        present.assert_called_once_with("cursor", configured_path)

    def test_registry_first_import_order_keeps_deferred_cursor_probe_usable(self):
        script = """
import os
os.environ["CURSOR_SKILLS_DIR"] = "configured"
from installer.client_hosts import registry
from installer import mcp_config
assert registry.CLIENT_SPECS["cursor"].skills_in_use()
assert mcp_config.CLIENT_SPECS == registry.CLIENT_SPECS
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[2],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_write_guard_dispatches_by_host_metadata_not_product_id(self):
        newcomer = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            id="newcomer",
            host_family="newcomer",
            config_write_guard="cursor-version-v1",
        )
        write_result = {
            "client": "newcomer",
            "path": "newcomer.json",
            "action": "added (dry-run)",
            "backup": None,
        }
        with (
            mock.patch.object(
                mcp_config,
                "CLIENT_SPECS",
                {"newcomer": newcomer},
            ),
            mock.patch.object(
                mcp_config,
                "agent_config_path",
                return_value=Path("newcomer.json"),
            ),
            mock.patch.object(
                mcp_config,
                "_cursor_write_version_gate",
                return_value="version-warning",
            ) as guard,
            mock.patch.object(
                mcp_config,
                "_write_json_client",
                return_value=write_result,
            ),
        ):
            result = mcp_config._write_entry_unchecked(
                "newcomer",
                cwd=Path("managed-root"),
                dry_run=True,
            )

        guard.assert_called_once_with()
        self.assertEqual(result["warning"], "version-warning")

    def test_host_owned_config_write_guard_callback_is_dispatched(self):
        callback = mock.Mock(return_value="host-version-unknown")
        newcomer = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            id="newcomer",
            host_family="newcomer",
            config_write_guard=None,
            config_write_guard_probe=callback,
        )
        write_result = {
            "client": "newcomer",
            "path": "newcomer.json",
            "action": "added (dry-run)",
            "backup": None,
        }
        with (
            mock.patch.object(mcp_config, "CLIENT_SPECS", {"newcomer": newcomer}),
            mock.patch.object(
                mcp_config,
                "agent_config_path",
                return_value=Path("newcomer.json"),
            ),
            mock.patch.object(
                mcp_config,
                "_write_json_client",
                return_value=write_result,
            ),
        ):
            result = mcp_config._write_entry_unchecked(
                "newcomer",
                cwd=Path("managed-root"),
                dry_run=True,
            )

        callback.assert_called_once_with()
        self.assertEqual(result["warning"], "host-version-unknown")

    def test_trae_work_cn_candidate_version_floor(self):
        from installer.client_hosts.hosts import trae_work_cn

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            product = app_root / "resources" / "app" / "product.json"
            product.parent.mkdir(parents=True)
            with (
                mock.patch.object(trae_work_cn.sys, "platform", "win32"),
                mock.patch.dict(
                    os.environ,
                    {"TRAE_WORK_CN_APP_ROOT": str(app_root)},
                    clear=False,
                ),
            ):
                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo-cn",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertIsNone(
                    trae_work_cn._trae_work_cn_config_write_guard()
                )

                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo-cn",
                            "appVersion": "0.1.49",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertIsNone(
                    trae_work_cn._trae_work_cn_config_write_guard()
                )

                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo-cn",
                            "appVersion": "0.1.50",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    trae_work_cn._trae_work_cn_config_write_guard(),
                    "trae_work_cn_version_newer_than_tested",
                )

                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "different-product",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ShellError, "trae_work_cn_identity_mismatch"
                ):
                    trae_work_cn._trae_work_cn_config_write_guard()

                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo-cn",
                            "appVersion": "0.1.47",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ShellError, "unsupported_trae_work_cn_version"
                ):
                    trae_work_cn._trae_work_cn_config_write_guard()

                product.unlink()
                self.assertEqual(
                    trae_work_cn._trae_work_cn_config_write_guard(),
                    "trae_work_cn_version_unknown",
                )

            with mock.patch.object(trae_work_cn.sys, "platform", "linux"):
                with self.assertRaisesRegex(
                    ShellError, "unsupported_trae_work_cn_platform"
                ):
                    trae_work_cn._trae_work_cn_config_write_guard()

    def test_trae_work_candidate_version_floor_and_product_identity(self):
        from installer.client_hosts.hosts import trae_work

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            product = app_root / "resources" / "app" / "product.json"
            product.parent.mkdir(parents=True)
            with (
                mock.patch.object(trae_work.sys, "platform", "win32"),
                mock.patch.dict(
                    os.environ,
                    {"TRAE_WORK_APP_ROOT": str(app_root)},
                    clear=False,
                ),
            ):
                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertIsNone(trae_work._trae_work_config_write_guard())

                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo-cn",
                            "appVersion": "0.1.48",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ShellError, "trae_work_identity_mismatch"
                ):
                    trae_work._trae_work_config_write_guard()

                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo",
                            "appVersion": "0.1.49",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    trae_work._trae_work_config_write_guard(),
                    "trae_work_version_newer_than_tested",
                )

                product.write_text(
                    json.dumps(
                        {
                            "applicationName": "trae-solo",
                            "appVersion": "0.1.47",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ShellError,
                    "unsupported_trae_work_version",
                ):
                    trae_work._trae_work_config_write_guard()

                product.unlink()
                self.assertEqual(
                    trae_work._trae_work_config_write_guard(),
                    "trae_work_version_unknown",
                )

                with mock.patch.object(trae_work.sys, "platform", "linux"):
                    with self.assertRaisesRegex(
                        ShellError, "unsupported_trae_work_platform"
                    ):
                        trae_work._trae_work_config_write_guard()

    def test_trae_work_below_floor_aborts_before_config_write(self):
        from installer.client_hosts.hosts import trae_work

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "app"
            product = app_root / "resources" / "app" / "product.json"
            product.parent.mkdir(parents=True)
            product.write_text(
                json.dumps(
                    {
                        "applicationName": "trae-solo",
                        "appVersion": "0.1.47",
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "profile" / "User" / "mcp.json"
            with (
                mock.patch.object(trae_work.sys, "platform", "win32"),
                mock.patch.dict(
                    os.environ,
                    {
                        "TRAE_WORK_APP_ROOT": str(app_root),
                        "TRAE_WORK_CONFIG": str(config_path),
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(
                    ShellError, "unsupported_trae_work_version"
                ),
            ):
                mcp_config.write_entry("trae-work", cwd=root)

            self.assertFalse(config_path.exists())

    def test_non_json_writer_rejects_json_only_write_policies(self):
        codex = mcp_config.CLIENT_SPECS["codex"]
        for invalid in (
            replace(codex, config_write_guard_probe=lambda: None),
            replace(codex, entry_ownership_policy="replace-marked-de-v1"),
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ShellError, "JSON writer"),
            ):
                mcp_config.validate_host_specs({"codex": invalid})

    def test_json_writer_uses_renderer_collection_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"servers": {"sibling": {"command": "keep"}}}),
                encoding="utf-8",
            )
            result = mcp_config._write_json_client(
                "synthetic",
                path,
                "decision-engine",
                {"command": "python-bin"},
                False,
                collection_key="servers",
            )
            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(result["action"], "added")
        self.assertEqual(loaded["servers"]["sibling"], {"command": "keep"})
        self.assertEqual(
            loaded["servers"]["decision-engine"],
            {"command": "python-bin"},
        )
        self.assertNotIn("mcpServers", loaded)

    def test_marked_entry_ownership_recognizes_cwd_independent_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "mcp.json"
            old_root = str(root / "old-managed")
            new_root = str(root / "new-managed")
            old_entry = {
                "command": "old-python",
                "args": [
                    "-3.13",
                    "-c",
                    mcp_config._CWD_INDEPENDENT_BOOTSTRAP,
                    old_root,
                    "--managed-root",
                    old_root,
                ],
                "env": {
                    "PYTHONPATH": old_root,
                    mcp_config.CLIENT_HOST_ENV: "workbuddy",
                },
            }
            desired = {
                "command": "new-python",
                "args": [
                    "-3.13",
                    "-c",
                    mcp_config._CWD_INDEPENDENT_BOOTSTRAP,
                    new_root,
                    "--managed-root",
                    new_root,
                ],
                "env": {
                    "PYTHONPATH": new_root,
                    mcp_config.CLIENT_HOST_ENV: "workbuddy",
                },
            }
            path.write_text(
                json.dumps({"mcpServers": {"decision-engine": old_entry}}),
                encoding="utf-8",
            )

            result = mcp_config._write_json_client(
                "workbuddy",
                path,
                "decision-engine",
                desired,
                False,
                ownership_policy="replace-marked-de-v1",
            )

            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["action"], "updated")
            self.assertEqual(loaded["mcpServers"]["decision-engine"], desired)

    def test_marked_entry_ownership_rejects_malformed_cwd_independent_launcher(self):
        root = r"C:\managed\decision-engine"
        desired = {
            "command": "python.exe",
            "args": [
                "-3.13",
                "-c",
                mcp_config._CWD_INDEPENDENT_BOOTSTRAP,
                root,
                "--managed-root",
                root,
            ],
            "env": {
                "PYTHONPATH": root,
                mcp_config.CLIENT_HOST_ENV: "workbuddy",
            },
        }
        malformed = {
            "wrong bootstrap": {
                **desired,
                "args": ["-c", "import installer.launcher", root, "--managed-root", root],
            },
            "root mismatch": {
                **desired,
                "args": [
                    "-c",
                    mcp_config._CWD_INDEPENDENT_BOOTSTRAP,
                    root,
                    "--managed-root",
                    root + "-other",
                ],
            },
            "unexpected cwd": {**desired, "cwd": root},
            "pythonpath mismatch": {
                **desired,
                "env": {
                    "PYTHONPATH": root + "-other",
                    mcp_config.CLIENT_HOST_ENV: "workbuddy",
                },
            },
            "host marker mismatch": {
                **desired,
                "env": {
                    "PYTHONPATH": root,
                    mcp_config.CLIENT_HOST_ENV: "another-host",
                },
            },
            "unsupported root flag": {
                **desired,
                "args": [
                    "-c",
                    mcp_config._CWD_INDEPENDENT_BOOTSTRAP,
                    root,
                    "--root",
                    root,
                ],
            },
        }

        for label, existing in malformed.items():
            with self.subTest(label=label):
                self.assertFalse(mcp_config._is_marked_de_entry(existing, desired))

    def test_marked_entry_ownership_recognizes_absolute_bootstrap_launcher(self):
        root = r"C:\managed\decision-engine"
        entry = {
            "command": "python.exe",
            "args": [
                ntpath.join(root, "installer", "mcp_bootstrap.py"),
                root,
                "--managed-root",
                root,
            ],
            "env": {
                "PYTHONPATH": root,
                mcp_config.CLIENT_HOST_ENV: "qoder",
            },
        }
        self.assertTrue(mcp_config._is_marked_de_entry(entry, entry))

        foreign = {
            **entry,
            "args": [
                ntpath.join(root, "foreign", "mcp_bootstrap.py"),
                root,
                "--managed-root",
                root,
            ],
        }
        self.assertFalse(mcp_config._is_marked_de_entry(foreign, entry))

    def test_absolute_bootstrap_ownership_preserves_posix_case_and_separators(self):
        root = "/opt/DecisionEngine"
        entry = {
            "command": "/usr/bin/python3",
            "args": [
                root + "/installer/mcp_bootstrap.py",
                root,
                "--managed-root",
                root,
            ],
            "env": {
                "PYTHONPATH": root,
                mcp_config.CLIENT_HOST_ENV: "synthetic",
            },
        }
        self.assertTrue(mcp_config._is_marked_de_entry(entry, entry))

        case_different_script = {
            **entry,
            "args": [
                "/opt/decisionengine/installer/mcp_bootstrap.py",
                root,
                "--managed-root",
                root,
            ],
        }
        self.assertFalse(
            mcp_config._is_marked_de_entry(case_different_script, entry)
        )

    def test_marked_entry_ownership_preserves_cwd_based_host_shapes(self):
        root = r"C:\managed\decision-engine"
        for label, args in {
            "module launcher": ["-3.13", "-m", "installer.launcher"],
            "dev root launcher": [
                "-3.13",
                "-m",
                "installer.launcher",
                "--dev-root",
                root,
            ],
        }.items():
            entry = {
                "command": "python.exe",
                "args": args,
                "cwd": root,
                "env": {
                    "PYTHONPATH": root,
                    mcp_config.CLIENT_HOST_ENV: "trae-work",
                },
            }
            with self.subTest(label=label):
                self.assertTrue(mcp_config._is_marked_de_entry(entry, entry))

    def test_marked_entry_ownership_conflict_keeps_foreign_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            root = str(Path(tmp) / "managed")
            foreign = {
                "command": "python.exe",
                "args": [
                    "-c",
                    mcp_config._CWD_INDEPENDENT_BOOTSTRAP,
                    root,
                    "--managed-root",
                    root,
                ],
                "env": {
                    "PYTHONPATH": root + "-foreign",
                    mcp_config.CLIENT_HOST_ENV: "workbuddy",
                },
            }
            desired = {
                **foreign,
                "env": {
                    "PYTHONPATH": root,
                    mcp_config.CLIENT_HOST_ENV: "workbuddy",
                },
            }
            original = {"mcpServers": {"decision-engine": foreign}}
            path.write_text(json.dumps(original), encoding="utf-8")

            with self.assertRaisesRegex(ShellError, "same_name_unowned"):
                mcp_config._write_json_client(
                    "workbuddy",
                    path,
                    "decision-engine",
                    desired,
                    False,
                    ownership_policy="replace-marked-de-v1",
                )

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                original,
            )

    def test_agent_host_specs_publish_complete_master_plan_metadata(self):
        expected = {
            "claude-code": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {
                        "core-mcp",
                        "local-display",
                        "popup-followup",
                        "audit-stop-panel",
                    }
                ),
                "doctor_capabilities": frozenset({"mcp-entry", "skills"}),
                "optional_features": frozenset(
                    {"local-display", "popup-followup", "audit-stop-panel"}
                ),
            },
            "claude-desktop": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (),
                "skill_delivery_mode": "none",
                "routing_kind": "mcp-only",
                "launcher_capabilities": frozenset(
                    {
                        "core-mcp",
                        "local-display",
                        "popup-followup",
                        "audit-stop-panel",
                    }
                ),
                "doctor_capabilities": frozenset({"mcp-entry"}),
                "optional_features": frozenset(
                    {"local-display", "popup-followup", "audit-stop-panel"}
                ),
            },
            "codex": {
                "config_renderer": "codex-toml-v1",
                "skills_project_paths": (),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {
                        "core-mcp",
                        "local-display",
                        "popup-followup",
                        "audit-stop-panel",
                    }
                ),
                "doctor_capabilities": frozenset({"mcp-entry", "skills"}),
                "optional_features": frozenset(
                    {"local-display", "popup-followup", "audit-stop-panel"}
                ),
            },
            "cursor": {
                "config_renderer": "cursor-json-mcp-v1",
                "skills_project_paths": (
                    ".cursor/skills",
                    ".agents/skills",
                ),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {
                        "core-mcp",
                        "local-display",
                        "popup-followup",
                        "audit-stop-panel",
                    }
                ),
                "doctor_capabilities": frozenset(
                    {
                        "mcp-entry",
                        "skills",
                        "workspace-shadow",
                        "version",
                    }
                ),
                "optional_features": frozenset(
                    {"local-display", "popup-followup", "audit-stop-panel"}
                ),
            },
            "qoder": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (".qoder/skills",),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {"core-mcp", "local-display", "audit-stop-panel"}
                ),
                "doctor_capabilities": frozenset(
                    {"mcp-entry", "skills", "workspace-shadow"}
                ),
                "optional_features": frozenset(
                    {"local-display", "audit-stop-panel"}
                ),
            },
            "qoder-cn": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (".qoder/skills",),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {"core-mcp", "local-display", "audit-stop-panel"}
                ),
                "doctor_capabilities": frozenset(
                    {"mcp-entry", "skills", "workspace-shadow"}
                ),
                "optional_features": frozenset(
                    {"local-display", "audit-stop-panel"}
                ),
            },
            "trae": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (".trae/skills",),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {"core-mcp", "local-display", "audit-stop-panel"}
                ),
                "doctor_capabilities": frozenset(
                    {"mcp-entry", "skills", "workspace-shadow"}
                ),
                "optional_features": frozenset(
                    {"local-display", "audit-stop-panel"}
                ),
            },
            "trae-work-cn": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (".trae/skills",),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {"core-mcp", "local-display", "audit-stop-panel"}
                ),
                "doctor_capabilities": frozenset(
                    {"mcp-entry", "skills", "workspace-shadow"}
                ),
                "optional_features": frozenset(
                    {"local-display", "audit-stop-panel"}
                ),
            },
            "trae-work": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (".trae/skills",),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {"core-mcp", "local-display", "audit-stop-panel"}
                ),
                "doctor_capabilities": frozenset(
                    {"mcp-entry", "skills", "workspace-shadow"}
                ),
                "optional_features": frozenset(
                    {"local-display", "audit-stop-panel"}
                ),
            },
            "trae-cn": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (".trae/skills",),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {"core-mcp", "local-display", "audit-stop-panel"}
                ),
                "doctor_capabilities": frozenset(
                    {"mcp-entry", "skills", "workspace-shadow"}
                ),
                "optional_features": frozenset(
                    {"local-display", "audit-stop-panel"}
                ),
            },
            "workbuddy": {
                "config_renderer": "json-mcp-v1",
                "skills_project_paths": (),
                "skill_delivery_mode": "managed-copy",
                "routing_kind": "skill",
                "launcher_capabilities": frozenset(
                    {"core-mcp", "local-display", "audit-stop-panel"}
                ),
                "doctor_capabilities": frozenset({"mcp-entry", "skills"}),
                "optional_features": frozenset(
                    {"local-display", "audit-stop-panel"}
                ),
            },
        }

        for client, values in expected.items():
            with self.subTest(client=client):
                spec = mcp_config.CLIENT_SPECS[client]
                self.assertEqual(spec.config_scope, "user-global")
                self.assertIs(spec.config_path, spec.default_path)
                self.assertIs(spec.skills_global_path, spec.skills_path)
                for field, value in values.items():
                    self.assertEqual(getattr(spec, field), value)

        self.assertEqual(
            mcp_config.clients_with_doctor_capability("mcp-entry"),
            (
                "claude-code",
                "claude-desktop",
                "codex",
                "cursor",
                "qoder",
                "qoder-cn",
                "trae",
                "trae-work",
                "trae-cn",
                "trae-work-cn",
                "workbuddy",
            ),
        )
        with self.assertRaisesRegex(ShellError, "unsupported Doctor capability"):
            mcp_config.clients_with_doctor_capability("future-capability")

    def test_master_plan_paths_are_authoritative_for_consumers(self):
        config_path = Path("registry-config.json")
        skills_path = Path("registry-skills")
        cursor = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            config_path=lambda: config_path,
            skills_global_path=lambda: skills_path,
            skills_in_use=lambda: True,
        )
        with (
            mock.patch.dict(
                os.environ,
                {"CURSOR_CONFIG": "", "CURSOR_SKILLS_DIR": ""},
                clear=False,
            ),
            mock.patch.object(
                mcp_config,
                "CLIENT_SPECS",
                {"cursor": cursor},
            ),
        ):
            self.assertEqual(mcp_config.agent_config_path("cursor"), config_path)
            self.assertEqual(
                mcp_config.active_skill_routes(),
                {"cursor": (skills_path, frozenset())},
            )

        invalid_project_path = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            skills_project_paths=(".cursor/skills", "../private"),
        )
        with (
            mock.patch.object(
                mcp_config,
                "CLIENT_SPECS",
                {"cursor": invalid_project_path},
            ),
            self.assertRaisesRegex(
                ShellError,
                "project skill metadata is invalid",
            ),
        ):
            mcp_config.cursor_workspace_skill_shadow(
                Path("missing-workspace"),
                ("audit",),
            )

    def test_master_plan_metadata_validation_rejects_drift(self):
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        invalid_specs = (
            replace(cursor, config_scope=""),
            replace(
                cursor,
                optional_features=frozenset(
                    {"runtime-display-policy", "undeclared-feature"}
                ),
            ),
            replace(cursor, skills_project_paths=("../private",)),
            replace(cursor, skills_project_paths=("C:/private/skills",)),
            replace(cursor, skill_delivery_mode="typo"),
            replace(cursor, routing_kind="typo"),
            replace(cursor, detection="typo"),
            replace(cursor, client_name_patterns=("[",)),
            replace(cursor, skills_in_use=None),
            replace(cursor, config_write_guard="typo"),
            replace(cursor, launch_policy="typo"),
            replace(cursor, entry_ownership_policy="typo"),
            replace(cursor, config_write_guard_probe=lambda: None),
            replace(cursor, onboarding_routing_probe=lambda: True),
            replace(
                cursor,
                doctor_capabilities=frozenset(
                    set(cursor.doctor_capabilities) | {"typo"}
                ),
            ),
        )
        for invalid in invalid_specs:
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(ShellError),
            ):
                mcp_config.validate_host_specs({"cursor": invalid})

    def test_cursor_project_skill_consumer_uses_validated_registry_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".workspace" / "skills" / "audit").mkdir(parents=True)
            (
                workspace
                / ".fallback"
                / "skills"
                / "discussion-board"
            ).mkdir(parents=True)
            cursor = replace(
                mcp_config.CLIENT_SPECS["cursor"],
                skills_project_paths=(
                    ".workspace/skills",
                    ".fallback/skills",
                ),
            )
            with mock.patch.object(
                mcp_config,
                "CLIENT_SPECS",
                {"cursor": cursor},
            ):
                shadow = mcp_config.cursor_workspace_skill_shadow(
                    workspace,
                    ("audit", "discussion-board"),
                )

        self.assertEqual(shadow.cursor_skills, ("audit",))
        self.assertEqual(shadow.agents_skills, ("discussion-board",))

    def test_cursor_project_skill_consumer_keeps_its_two_path_contract_local(self):
        cursor = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            skills_project_paths=(".cursor/skills",),
        )
        with (
            mock.patch.object(mcp_config, "CLIENT_SPECS", {"cursor": cursor}),
            self.assertRaisesRegex(
                ShellError,
                "Cursor project skill metadata must contain exactly two paths",
            ),
        ):
            mcp_config.cursor_workspace_skill_shadow(Path("missing-workspace"), ("audit",))

    def test_codex_renderer_missing_from_registry_fails_with_shell_error(self):
        with (
            mock.patch.object(
                mcp_config,
                "CLIENT_SPECS",
                {"cursor": mcp_config.CLIENT_SPECS["cursor"]},
            ),
            self.assertRaisesRegex(ShellError, "Codex registry"),
        ):
            mcp_config.render_codex_entry(
                python="python-bin",
                cwd=Path("managed-root"),
            )

    def test_legacy_mcp_renderer_bytes_are_locked_during_registry_migration(self):
        expected_json = """{
  "mcpServers": {
    "decision-engine": {
      "args": [
        "-m",
        "installer.launcher"
      ],
      "command": "python-bin",
      "cwd": "managed-root",
      "env": {
        "DE_MCP_CLIENT_HOST": "claude",
        "PYTHONPATH": "managed-root"
      },
      "type": "stdio"
    }
  }
}"""
        for client in ("claude-code", "claude-desktop"):
            with self.subTest(client=client):
                self.assertEqual(
                    json.dumps(
                        mcp_config.render_entry(
                            client=client,
                            python="python-bin",
                            cwd=Path("managed-root"),
                        ),
                        indent=2,
                        sort_keys=True,
                    ),
                    expected_json,
                )
        self.assertEqual(
            mcp_config.render_codex_toml(
                python="python-bin",
                cwd=Path("managed-root"),
            ),
            """[mcp_servers.decision-engine]
command = "python-bin"
args = ["-m", "installer.launcher"]
cwd = "managed-root"
env = { DE_MCP_CLIENT_HOST = "codex" }
startup_timeout_sec = 120
tool_timeout_sec = 660
""",
        )

    def test_agent_host_specs_have_stable_ids_and_exact_observed_aliases(self):
        self.assertIs(mcp_config.ClientSpec, mcp_config.AgentHostSpec)
        self.assertEqual(
            set(mcp_config.CLIENT_SPECS),
            {
                "claude-code",
                "claude-desktop",
                "codex",
                "cursor",
                "qoder",
                "qoder-cn",
                "trae",
                "trae-work",
                "trae-cn",
                "trae-work-cn",
                "workbuddy",
            },
        )
        self.assertTrue(
            all(key == spec.id for key, spec in mcp_config.CLIENT_SPECS.items())
        )
        self.assertEqual(
            mcp_config.CLIENT_SPECS["cursor"].observed_client_aliases,
            frozenset({"cursor", "cursor-vscode"}),
        )
        self.assertEqual(
            {
                key: spec.require_observed_identity
                for key, spec in mcp_config.CLIENT_SPECS.items()
            },
            {
                "claude-code": False,
                "claude-desktop": False,
                "codex": False,
                "cursor": True,
                "qoder": True,
                "qoder-cn": True,
                "trae": False,
                "trae-work": False,
                "trae-cn": False,
                "trae-work-cn": False,
                "workbuddy": True,
            },
        )

    def test_client_spec_alias_preserves_legacy_constructor_shape(self):
        spec = mcp_config.ClientSpec(
            "json",
            "legacy",
            "LEGACY_CONFIG",
            lambda: Path("legacy.json"),
            "legacy-id",
            frozenset({"legacy-alias"}),
        )

        self.assertEqual(spec.config_format, "json")
        self.assertEqual(spec.host_family, "legacy")
        self.assertEqual(spec.id, "legacy-id")
        self.assertEqual(spec.observed_client_aliases, frozenset({"legacy-alias"}))
        self.assertEqual(spec.transport, "stdio")

    def test_host_identity_resolution_is_exact_and_fail_closed(self):
        cases = (
            (
                "cursor",
                {"clientInfo": {"name": " Cursor ", "version": "1"}},
                ("matched", "cursor", None, True),
            ),
            (
                "cursor",
                {"clientInfo": {"name": "cursor-vscode", "version": "1"}},
                ("matched", "cursor", None, True),
            ),
            (
                "cursor",
                {},
                ("missing", None, None, False),
            ),
            (
                "cursor",
                {"clientInfo": {"name": 7}},
                ("malformed", None, None, False),
            ),
            (
                "cursor",
                {"clientInfo": {"name": "Cursor Agent"}},
                ("unknown", None, None, False),
            ),
            (
                "cursor",
                {"clientInfo": {"name": "OpenAI Codex"}},
                ("conflicting", "codex", "host_identity_conflict", False),
            ),
            (
                "workbuddy",
                {
                    "clientInfo": {
                        "name": "connector:custom-mcp:decision-engine",
                        "version": "1.0.0",
                    }
                },
                ("matched", "workbuddy", None, True),
            ),
            (
                "qoder",
                {"clientInfo": {"name": "mcphost", "version": "0.1.0"}},
                ("matched", "qoder", None, True),
            ),
            (
                "codex",
                None,
                ("malformed", None, None, False),
            ),
            (
                "codex",
                [],
                ("malformed", None, None, False),
            ),
            (
                "codex",
                "not-an-object",
                ("malformed", None, None, False),
            ),
        )
        for declared, params, expected in cases:
            with self.subTest(params=params):
                identity = mcp_config.resolve_host_identity(declared, params)
                self.assertEqual(
                    (
                        identity.status,
                        identity.observed_host,
                        identity.diagnostic,
                        identity.optional_features_enabled,
                    ),
                    expected,
                )

    def test_shared_trae_alias_uses_declared_registration(self):
        self.assertIsNone(mcp_config.normalize_observed_client_info(" Trae "))
        self.assertEqual(mcp_config.normalize_client_host("Trae"), "trae")
        self.assertEqual(
            mcp_config.normalize_client_host("trae-work"), "trae-work"
        )
        self.assertEqual(
            mcp_config.normalize_client_host(" TRAE-WORK-CN "), "trae-work-cn"
        )

        identity = mcp_config.resolve_host_identity(
            "trae-work",
            {"clientInfo": {"name": "Trae", "version": "0.1.61"}},
        )

        self.assertEqual(identity.status, "matched")
        self.assertEqual(identity.declared_host, "trae-work")
        self.assertEqual(identity.observed_host, "trae-work")
        self.assertTrue(identity.optional_features_enabled)

        conflict = mcp_config.resolve_host_identity(
            "trae-work",
            {"clientInfo": {"name": "Cursor", "version": "1"}},
        )
        self.assertEqual(conflict.status, "conflicting")
        self.assertEqual(conflict.observed_host, "cursor")
        self.assertEqual(conflict.diagnostic, "host_identity_conflict")
        self.assertFalse(conflict.optional_features_enabled)

    def test_cursor_entry_uses_documented_json_shape_and_shared_host_marker(self):
        root = Path("/opt/decision-engine")
        entry = mcp_config.render_entry(client="cursor", cwd=root)["mcpServers"][
            "decision-engine"
        ]

        self.assertEqual(
            set(entry),
            {"command", "args", "env"},
            "Cursor's documented stdio JSON shape has no Claude-only type/cwd fields",
        )
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"][0], "-c")
        self.assertEqual(
            entry["args"][-2:],
            ["--managed-root", str(root)],
        )
        self.assertEqual(entry["env"]["PYTHONPATH"], str(root))
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], "cursor")
        for forbidden in ("server_endpoint", "access_token", "api_key", "bearer"):
            self.assertNotIn(forbidden, json.dumps(entry).lower())

    def test_client_capabilities_are_allowlisted_and_unknown_defaults_off(self):
        self.assertTrue(mcp_config.host_supports_local_display("claude"))
        self.assertTrue(mcp_config.host_supports_local_display("codex"))
        self.assertTrue(mcp_config.host_supports_local_display("cursor"))
        self.assertFalse(mcp_config.host_supports_local_display(None))
        self.assertFalse(mcp_config.host_supports_local_display("future-host"))

    def test_host_adapters_split_display_followup_stopper_and_runtime_policy(self):
        claude = mcp_config.host_adapter("claude")
        codex = mcp_config.host_adapter("codex")
        cursor = mcp_config.host_adapter("cursor")

        for adapter in (claude, codex, cursor):
            self.assertTrue(adapter.local_display_tools)
            self.assertTrue(adapter.popup_followup)
            self.assertTrue(adapter.audit_stop_panel)
            self.assertIsNone(adapter.runtime_display_policy)
            self.assertEqual(adapter.popup_api_profile, "legacy")
        self.assertIsNone(cursor.lifecycle_adapter)

    def test_shared_host_family_rejects_conflicting_adapter_metadata(self):
        first = mcp_config.CLIENT_SPECS["claude-code"]
        conflicting_values = (
            replace(first, popup_followup=not first.popup_followup),
            replace(
                first,
                require_observed_identity=not first.require_observed_identity,
            ),
        )
        for conflicting in conflicting_values:
            with (
                self.subTest(conflicting=conflicting),
                mock.patch.object(
                    mcp_config,
                    "CLIENT_SPECS",
                    {"first": first, "conflicting": conflicting},
                ),
                self.assertRaisesRegex(
                    ShellError, "conflicting host adapter metadata"
                ),
            ):
                mcp_config.host_adapter(first.host_family)

    def test_cursor_explicit_setup_no_longer_excludes_m2_skills(self):
        spec = mcp_config.CLIENT_SPECS["cursor"]
        self.assertEqual(spec.excluded_skills, frozenset())

    def test_cursor_doctor_route_requires_an_explicit_mcp_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cursor_config = base / ".cursor" / "mcp.json"
            cursor_skills = base / ".cursor" / "skills"
            cursor_config.parent.mkdir(parents=True)
            with mock.patch.dict(
                os.environ,
                {
                    "CURSOR_CONFIG": str(cursor_config),
                    "CURSOR_SKILLS_DIR": str(cursor_skills),
                },
                clear=False,
            ):
                self.assertNotIn(
                    "cursor", mcp_config.active_skill_routes(for_doctor=True)
                )
                cursor_config.write_text(
                    json.dumps(
                        mcp_config.render_entry(client="cursor", cwd=base)
                    ),
                    encoding="utf-8",
                )
                route, excluded = mcp_config.active_skill_routes(
                    for_doctor=True
                )["cursor"]

        self.assertEqual(route, cursor_skills)
        self.assertEqual(excluded, frozenset())

    def test_cursor_dev_root_bootstrap_is_explicit(self):
        root = Path("/opt/cursor-dev")
        entry = mcp_config.render_entry(
            client="cursor", dev_root=root
        )["mcpServers"]["decision-engine"]
        self.assertEqual(entry["args"][-2:], ["--dev-root", str(root)])

    def test_cursor_bootstrap_cannot_be_shadowed_by_open_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            managed = base / "managed"
            workspace = base / "workspace"
            safe_marker = base / "safe.txt"
            evil_marker = base / "evil.txt"
            for root in (managed, workspace):
                (root / "installer").mkdir(parents=True)
                (root / "installer" / "__init__.py").write_text("", encoding="utf-8")
            (managed / "installer" / "launcher.py").write_text(
                "from pathlib import Path\n"
                "def main(argv=None):\n"
                "    Path(%r).write_text(repr(argv), encoding='utf-8')\n"
                "    return 0\n" % str(safe_marker),
                encoding="utf-8",
            )
            (workspace / "installer" / "launcher.py").write_text(
                "from pathlib import Path\n"
                "Path(%r).write_text('shadowed', encoding='utf-8')\n"
                "def main(argv=None):\n"
                "    return 0\n" % str(evil_marker),
                encoding="utf-8",
            )
            entry = mcp_config.render_entry(client="cursor", cwd=managed)[
                "mcpServers"
            ]["decision-engine"]
            environment = os.environ.copy()
            environment.update(entry["env"])

            completed = subprocess.run(
                [entry["command"], *entry["args"]],
                cwd=workspace,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(safe_marker.exists())
            self.assertFalse(evil_marker.exists())
            self.assertIn(
                "--managed-root", safe_marker.read_text(encoding="utf-8")
            )


if __name__ == "__main__":
    unittest.main()
