"""Behavior locks for the independent Claude third-party (claude-desktop-3p) host.

Every filesystem fixture here is a synthetic temporary HOME. No installed host
configuration, no activation, and no deployment is touched.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import mcp_config
from installer.config import ShellError

CLIENT = "claude-desktop-3p"
CONFIG_ENV = "CLAUDE_DESKTOP_3P_CONFIG"
SERVER = mcp_config.DEFAULT_SERVER_NAME


def _dev_root(base: Path, name: str) -> Path:
    """A synthetic checkout that satisfies the dev-root liveness probe."""

    root = base / name
    (root / "installer").mkdir(parents=True, exist_ok=True)
    (root / "installer" / "launcher.py").write_text("", encoding="utf-8")
    (root / "installer" / "mcp_bootstrap.py").write_text("", encoding="utf-8")
    return root


def _read_servers(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


class ClaudeThirdPartyRegistryTestCase(unittest.TestCase):
    """R1: independent host identity."""

    def test_registry_exposes_an_independent_mcp_only_3p_identity(self):
        self.assertIn(CLIENT, mcp_config.CLIENT_SPECS)
        spec = mcp_config.CLIENT_SPECS[CLIENT]
        desktop = mcp_config.CLIENT_SPECS["claude-desktop"]

        self.assertEqual(spec.id, CLIENT)
        self.assertEqual(spec.host_family, CLIENT)
        self.assertNotEqual(spec.host_family, desktop.host_family)
        self.assertEqual(spec.config_env, CONFIG_ENV)
        self.assertNotEqual(spec.config_env, desktop.config_env)
        # MCP-only: no DE skills, no AQG surface.
        self.assertEqual(spec.skill_delivery_mode, "none")
        self.assertEqual(spec.routing_kind, "mcp-only")
        self.assertIsNone(spec.skills_global_path)
        self.assertIsNone(spec.skill_route_name)
        self.assertEqual(spec.doctor_capabilities, frozenset({"mcp-entry"}))
        self.assertNotIn("skills", spec.doctor_capabilities)
        # Same-name unowned entries must fail closed.
        self.assertEqual(spec.entry_ownership_policy, "replace-marked-de-v1")

    def test_default_config_path_is_the_third_party_support_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with mock.patch.object(
                Path, "home", return_value=home
            ), mock.patch.dict(os.environ, {CONFIG_ENV: ""}, clear=False):
                self.assertEqual(
                    mcp_config.agent_config_path(CLIENT),
                    home
                    / "Library"
                    / "Application Support"
                    / "Claude-3p"
                    / "claude_desktop_config.json",
                )

    def test_3p_registration_marker_does_not_collide_with_claude_desktop(self):
        self.assertEqual(mcp_config.normalize_client_host(CLIENT), CLIENT)
        self.assertEqual(
            mcp_config.normalize_client_host("claude desktop"), "claude"
        )
        self.assertEqual(
            mcp_config.normalize_observed_client_info("claude desktop"), "claude"
        )
        identity = mcp_config.resolve_host_identity(
            "claude", {"clientInfo": {"name": "claude desktop", "version": "1"}}
        )
        self.assertEqual(identity.status, "matched")
        self.assertTrue(identity.optional_features_enabled)

    def test_both_claude_products_are_detected_and_written_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            desktop_config = (
                home
                / "Library"
                / "Application Support"
                / "Claude"
                / "claude_desktop_config.json"
            )
            third_party_config = (
                home
                / "Library"
                / "Application Support"
                / "Claude-3p"
                / "claude_desktop_config.json"
            )
            desktop_config.parent.mkdir(parents=True)
            third_party_config.parent.mkdir(parents=True)
            desktop_config.write_text(
                json.dumps({"mcpServers": {"keep-desktop": {"command": "keep"}}}),
                encoding="utf-8",
            )
            third_party_config.write_text(
                json.dumps({"mcpServers": {"keep-3p": {"command": "keep"}}}),
                encoding="utf-8",
            )
            root = _dev_root(base, "managed")
            environment = {
                "CLAUDE_DESKTOP_CONFIG": str(desktop_config),
                CONFIG_ENV: str(third_party_config),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                detected = mcp_config.detect_clients()
                self.assertIn("claude-desktop", detected)
                self.assertIn(CLIENT, detected)

                mcp_config.write_entry("claude-desktop", dev_root=root)
                mcp_config.write_entry(CLIENT, dev_root=root)

            desktop_servers = _read_servers(desktop_config)
            third_party_servers = _read_servers(third_party_config)

        self.assertEqual(desktop_servers["keep-desktop"], {"command": "keep"})
        self.assertEqual(third_party_servers["keep-3p"], {"command": "keep"})
        self.assertEqual(
            desktop_servers[SERVER]["env"][mcp_config.CLIENT_HOST_ENV], "claude"
        )
        self.assertEqual(
            third_party_servers[SERVER]["env"][mcp_config.CLIENT_HOST_ENV], CLIENT
        )
        self.assertNotIn("keep-3p", desktop_servers)
        self.assertNotIn("keep-desktop", third_party_servers)


class ClaudeThirdPartyLifecycleTestCase(unittest.TestCase):
    """R2: managed MCP lifecycle."""

    def test_first_repeat_and_root_upgrade_are_owned_idempotent_and_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "Claude-3p" / "claude_desktop_config.json"
            config.parent.mkdir(parents=True)
            dev = _dev_root(base, "dev-checkout")
            managed = _dev_root(base, "managed-root")
            with mock.patch.dict(
                os.environ, {CONFIG_ENV: str(config)}, clear=False
            ):
                first = mcp_config.write_entry(CLIENT, dev_root=dev)
                first_status = mcp_config.entry_status(CLIENT)
                repeat = mcp_config.write_entry(CLIENT, dev_root=dev)
                repeat_status = mcp_config.entry_status(CLIENT)
                upgrade = mcp_config.write_entry(CLIENT, dev_root=managed)
                upgrade_status = mcp_config.entry_status(CLIENT)
                entry = mcp_config.read_entry(CLIENT)

        self.assertEqual(first["action"], "added")
        self.assertEqual(first_status, "ready")
        self.assertEqual(repeat["action"], "unchanged")
        self.assertEqual(repeat_status, "ready")
        self.assertEqual(upgrade["action"], "updated")
        self.assertEqual(upgrade_status, "ready")
        self.assertEqual(entry["cwd"], str(managed))
        self.assertEqual(entry["env"]["PYTHONPATH"], str(managed))
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], CLIENT)
        self.assertEqual(entry["type"], "stdio")

    def test_absent_entry_status_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "Claude-3p" / "claude_desktop_config.json"
            config.parent.mkdir(parents=True)
            with mock.patch.dict(
                os.environ, {CONFIG_ENV: str(config)}, clear=False
            ):
                self.assertEqual(mcp_config.entry_status(CLIENT), "absent")

    def test_same_name_unowned_entry_fails_closed_and_preserves_data(self):
        foreign = {
            "command": "/usr/local/bin/other-engine",
            "args": ["--serve"],
            "env": {"OTHER": "1"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "Claude-3p" / "claude_desktop_config.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"mcpServers": {SERVER: foreign}}), encoding="utf-8"
            )
            before = config.read_bytes()
            root = _dev_root(base, "managed")
            with mock.patch.dict(
                os.environ, {CONFIG_ENV: str(config)}, clear=False
            ):
                with self.assertRaisesRegex(ShellError, "same_name_unowned"):
                    mcp_config.write_entry(CLIENT, dev_root=root)
            after = config.read_bytes()

        self.assertEqual(after, before)


class ClaudeThirdPartyNormalizationTestCase(unittest.TestCase):
    """R3: bounded tolerance for host-normalized managed entries."""

    def _managed_entry(self, root: Path, *, host: str = CLIENT) -> dict:
        return {
            "type": "stdio",
            "command": "/usr/bin/python3",
            "args": ["-m", "installer.launcher", "--dev-root", str(root)],
            "cwd": str(root),
            "env": {
                "PYTHONPATH": str(root),
                mcp_config.CLIENT_HOST_ENV: host,
            },
        }

    def _write_config(self, config: Path, entry: dict) -> None:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {"mcpServers": {"keep": {"command": "keep"}, SERVER: entry}},
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_host_removed_type_and_cwd_stay_owned_and_are_repaired(self):
        for removed in (("type",), ("cwd",), ("type", "cwd")):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                config = base / "Claude-3p" / "claude_desktop_config.json"
                root = _dev_root(base, "managed")
                entry = self._managed_entry(root)
                for field in removed:
                    entry.pop(field)
                self._write_config(config, entry)
                with mock.patch.dict(
                    os.environ, {CONFIG_ENV: str(config)}, clear=False
                ):
                    self.assertEqual(mcp_config.entry_status(CLIENT), "stale")
                    result = mcp_config.write_entry(CLIENT, dev_root=root)
                    self.assertEqual(mcp_config.entry_status(CLIENT), "ready")
                servers = _read_servers(config)
                self.assertEqual(result["action"], "updated")
                self.assertEqual(servers["keep"], {"command": "keep"})
                self.assertEqual(servers[SERVER]["type"], "stdio")
                self.assertEqual(servers[SERVER]["cwd"], str(root))

    def test_normalized_managed_root_entry_without_cwd_stays_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "Claude-3p" / "claude_desktop_config.json"
            root = _dev_root(base, "managed")
            entry = {
                "command": "/usr/bin/python3",
                "args": ["-m", "installer.launcher"],
                "env": {
                    "PYTHONPATH": str(root),
                    mcp_config.CLIENT_HOST_ENV: CLIENT,
                },
            }
            self._write_config(config, entry)
            with mock.patch.dict(
                os.environ, {CONFIG_ENV: str(config)}, clear=False
            ):
                result = mcp_config.write_entry(CLIENT, dev_root=root)
            servers = _read_servers(config)

        self.assertEqual(result["action"], "updated")
        self.assertEqual(servers["keep"], {"command": "keep"})
        self.assertEqual(servers[SERVER]["cwd"], str(root))

    def test_legacy_claude_desktop_style_registration_migrates_in_place(self):
        for removed in ((), ("type", "cwd")):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                config = base / "Claude-3p" / "claude_desktop_config.json"
                root = _dev_root(base, "managed")
                entry = self._managed_entry(root, host="claude")
                for field in removed:
                    entry.pop(field)
                self._write_config(config, entry)
                with mock.patch.dict(
                    os.environ, {CONFIG_ENV: str(config)}, clear=False
                ):
                    result = mcp_config.write_entry(CLIENT, dev_root=root)
                    self.assertEqual(mcp_config.entry_status(CLIENT), "ready")
                servers = _read_servers(config)
                self.assertEqual(result["action"], "updated")
                self.assertEqual(servers["keep"], {"command": "keep"})
                self.assertEqual(
                    servers[SERVER]["env"][mcp_config.CLIENT_HOST_ENV], CLIENT
                )

    def test_tolerance_does_not_claim_changed_or_foreign_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _dev_root(base, "managed")
            canonical = self._managed_entry(root)

            changed_args = copy.deepcopy(canonical)
            changed_args["args"] = ["-m", "other.launcher", "--dev-root", str(root)]

            foreign_marker = copy.deepcopy(canonical)
            foreign_marker["env"][mcp_config.CLIENT_HOST_ENV] = "cursor"

            foreign_pythonpath = copy.deepcopy(canonical)
            foreign_pythonpath["env"]["PYTHONPATH"] = str(base / "elsewhere")

            extra_env = copy.deepcopy(canonical)
            extra_env["env"]["INJECTED"] = "1"

            mutated_type = copy.deepcopy(canonical)
            mutated_type["type"] = "http"

            unrelated_field = copy.deepcopy(canonical)
            unrelated_field["description"] = "third-party owned"

            cases = {
                "changed-args": changed_args,
                "foreign-marker": foreign_marker,
                "foreign-pythonpath": foreign_pythonpath,
                "extra-env": extra_env,
                "mutated-type": mutated_type,
                "unrelated-field": unrelated_field,
            }
            for name, entry in cases.items():
                with self.subTest(case=name):
                    config = base / name / "claude_desktop_config.json"
                    self._write_config(config, entry)
                    before = config.read_bytes()
                    with mock.patch.dict(
                        os.environ, {CONFIG_ENV: str(config)}, clear=False
                    ):
                        with self.assertRaisesRegex(ShellError, "same_name_unowned"):
                            mcp_config.write_entry(CLIENT, dev_root=root)
                    self.assertEqual(config.read_bytes(), before)

    def test_normalization_tolerance_is_not_granted_to_claude_desktop(self):
        desktop = mcp_config.CLIENT_SPECS["claude-desktop"]
        third_party = mcp_config.CLIENT_SPECS[CLIENT]

        self.assertEqual(desktop.host_normalized_entry_fields, frozenset())
        self.assertEqual(desktop.legacy_registration_host_families, frozenset())
        self.assertEqual(
            third_party.host_normalized_entry_fields, frozenset({"type", "cwd"})
        )
        self.assertEqual(
            third_party.legacy_registration_host_families, frozenset({"claude"})
        )

    def test_contract_rejects_semantic_normalization_tolerance(self):
        from dataclasses import replace

        spec = mcp_config.CLIENT_SPECS[CLIENT]
        for field, value in (
            ("host_normalized_entry_fields", frozenset({"command"})),
            ("host_normalized_entry_fields", frozenset({"args"})),
            ("host_normalized_entry_fields", frozenset({"env"})),
            ("legacy_registration_host_families", frozenset({CLIENT})),
        ):
            with self.subTest(field=field, value=sorted(value)):
                invalid = replace(spec, **{field: value})
                with self.assertRaises(ShellError):
                    mcp_config.validate_host_specs({CLIENT: invalid})

    def test_normalization_tolerance_requires_owned_json_entries(self):
        from dataclasses import replace

        spec = mcp_config.CLIENT_SPECS[CLIENT]
        invalid = replace(spec, entry_ownership_policy="replace-existing-v1")
        with self.assertRaises(ShellError):
            mcp_config.validate_host_specs({CLIENT: invalid})


class ClaudeThirdPartyRuntimeCapabilityTestCase(unittest.TestCase):
    """R5: standard DE launcher/display/popup/stopper contract is preserved."""

    def test_3p_keeps_the_claude_desktop_runtime_capability_surface(self):
        spec = mcp_config.CLIENT_SPECS[CLIENT]
        desktop = mcp_config.CLIENT_SPECS["claude-desktop"]

        self.assertEqual(spec.transport, desktop.transport)
        self.assertEqual(spec.config_renderer, desktop.config_renderer)
        self.assertEqual(spec.launch_policy, desktop.launch_policy)
        self.assertEqual(spec.launcher_capabilities, desktop.launcher_capabilities)
        self.assertEqual(spec.optional_features, desktop.optional_features)
        self.assertTrue(spec.local_display_tools)
        self.assertTrue(spec.popup_followup)
        self.assertTrue(spec.audit_stop_panel)
        self.assertFalse(spec.require_observed_identity)
        self.assertFalse(spec.unverified_lite_stopper)

    def test_host_adapter_resolves_the_3p_family_with_display_support(self):
        adapter = mcp_config.host_adapter(CLIENT)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.id, CLIENT)
        self.assertTrue(mcp_config.host_supports_local_display(CLIENT))

    def test_rendered_entry_uses_the_standard_de_launcher(self):
        root = Path("/opt/decision-engine")
        entry = mcp_config.render_entry(client=CLIENT, cwd=root)["mcpServers"][SERVER]

        self.assertEqual(entry["args"], ["-m", "installer.launcher"])
        self.assertEqual(entry["cwd"], str(root))
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(entry["env"]["PYTHONPATH"], str(root))
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], CLIENT)


class ClaudeThirdPartyInstallerContractTestCase(unittest.TestCase):
    """R4: dp-install.sh drives the real adapter and reports honestly."""

    @classmethod
    def setUpClass(cls):
        cls.script = Path(__file__).resolve().parents[2] / "dp-install.sh"
        cls.body = cls.script.read_text(encoding="utf-8")

    def test_installer_uses_the_3p_client_id_instead_of_impersonation(self):
        # The third-party profile is wired by its own registered adapter id
        # through the ordinary detected-client path, so no part of the script
        # may point a claude-desktop write at the third-party config file.
        self.assertNotIn('CLAUDE_DESKTOP_CONFIG="$CLAUDE_3P_CONFIG"', self.body)
        self.assertNotIn("run_claude_3p_python", self.body)
        self.assertNotIn("--client claude-desktop\n", self.body)
        self.assertNotIn("set -- \"$@\" --client claude-desktop", self.body)
        self.assertIn("-u CLAUDE_DESKTOP_3P_CONFIG", self.body)
        self.assertIn(
            '--write --client "$client"',
            self.body,
            "detected hosts must be wired by their own client id",
        )

    def test_installer_requires_the_3p_adapter_in_the_signed_release(self):
        self.assertIn(
            '"claude-desktop-3p" in mcp_config.CLIENTS', self.body
        )

    def test_capability_report_separates_connector_disk_and_runtime_state(self):
        line = next(
            (
                candidate
                for candidate in self.body.splitlines()
                if "claude-desktop-3p: DE MCP=" in candidate
            ),
            None,
        )
        self.assertIsNotNone(line, "3p capability report line is missing")
        for token in (
            "DE MCP=connector-written",
            "config=disk-ready",
            "skills=not-supported",
            "routing=mcp-only",
            "AQG=unsupported-for-this-profile",
            "runtime=unverified",
            "runtime-verification=restart-required",
        ):
            with self.subTest(token=token):
                self.assertIn(token, line)
        self.assertNotIn("runtime=verified", line)

    def test_installer_preserves_the_fail_closed_profile_file_guard(self):
        self.assertIn(
            'CLAUDE_3P_ROOT="$HOME/Library/Application Support/Claude-3p"',
            self.body,
        )
        self.assertIn(
            'CLAUDE_3P_CONFIG="$CLAUDE_3P_ROOT/claude_desktop_config.json"',
            self.body,
        )
        self.assertIn("is not a regular configuration file", self.body)
        self.assertNotIn(
            'if [ -d "$CLAUDE_3P_ROOT" ] || [ -e "$CLAUDE_3P_CONFIG" ]',
            self.body,
            "an empty product directory is not a configured host profile",
        )

    def test_installer_executes_the_profile_file_guard(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash executable not found on PATH")
        start = self.body.index("claude_3p_profile_detected=0")
        end = self.body.index("\ndetect_managed_clients()", start)
        guard = self.body[start:end]

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = home / "Library" / "Application Support" / "Claude-3p"
            root.mkdir(parents=True)
            if os.name == "nt":
                home_for_bash = subprocess.run(
                    [bash, "--noprofile", "--norc", "-c", 'cygpath -u "$1"', "test", str(home)],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            else:
                home_for_bash = str(home)

            def detect() -> subprocess.CompletedProcess:
                environment = os.environ.copy()
                environment.update(HOME=home_for_bash)
                return subprocess.run(
                    [bash, "--noprofile", "--norc", "-c", (
                        'CLAUDE_3P_ROOT="$HOME/Library/Application Support/Claude-3p"\n'
                        'CLAUDE_3P_CONFIG="$CLAUDE_3P_ROOT/claude_desktop_config.json"\n'
                        'blocked() { exit 3; }\n'
                        f"{guard}\n"
                        'printf "%s\\n" "$claude_3p_profile_detected"\n'
                    )],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )

            empty = detect()
            self.assertEqual(empty.returncode, 0, empty.stderr)
            self.assertEqual(empty.stdout.strip(), "0")

            config = root / "claude_desktop_config.json"
            config.write_text("{}", encoding="utf-8")
            regular = detect()
            self.assertEqual(regular.returncode, 0, regular.stderr)
            self.assertEqual(regular.stdout.strip(), "1")

            config.unlink()
            config.mkdir()
            invalid = detect()
            self.assertEqual(invalid.returncode, 3, invalid.stderr)


@unittest.skipUnless(os.name == "posix", "dp-uninstall.sh is POSIX-only")
class ClaudeThirdPartyUninstallContractTestCase(unittest.TestCase):
    """Owned uninstall on the independent 3p path."""

    SCRIPT = Path(__file__).resolve().parents[2] / "dp-uninstall.sh"

    def _run(self, home: Path, root: Path) -> subprocess.CompletedProcess:
        launchctl = root / "launchctl"
        launchctl.write_text("#!/bin/sh\nexit 113\n", encoding="utf-8")
        launchctl.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "DEEPPATTERN_HOME": str(home / ".deeppattern"),
                "DE_AQG_BACKUP_ROOT": str(root / "backups"),
                "DE_AQG_UNINSTALL_PROCESS_COMMAND": "/usr/bin/true",
                "DE_AQG_UNINSTALL_LAUNCHCTL_COMMAND": str(launchctl),
            }
        )
        return subprocess.run(
            [str(self.SCRIPT), "--scope", "de", "--apply"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def _config(self, home: Path) -> Path:
        path = (
            home
            / "Library"
            / "Application Support"
            / "Claude-3p"
            / "claude_desktop_config.json"
        )
        path.parent.mkdir(parents=True)
        return path

    def test_uninstall_removes_only_the_owned_normalized_3p_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            config = self._config(home)
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "keep": {"command": "keep"},
                            SERVER: {
                                "command": "/usr/bin/python3",
                                "args": ["-m", "installer.launcher"],
                                "env": {
                                    "PYTHONPATH": "/opt/decision-engine",
                                    "DE_MCP_CLIENT_HOST": CLIENT,
                                },
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            completed = self._run(home, root)
            servers = _read_servers(config)

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(servers, {"keep": {"command": "keep"}})

    def test_uninstall_blocks_and_preserves_unowned_same_name_3p_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            config = self._config(home)
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            SERVER: {
                                "command": "/usr/local/bin/other-engine",
                                "args": ["--serve"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            before = config.read_bytes()
            completed = self._run(home, root)
            after = config.read_bytes()

        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
