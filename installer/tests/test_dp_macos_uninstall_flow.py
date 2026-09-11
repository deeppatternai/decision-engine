#!/usr/bin/env python3
"""Behavior tests for the macOS-first DE/AQG uninstall wrapper."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "dp-uninstall.sh"
HOST_MCP_RELATIVES = (
    ".claude.json",
    ".qoder/mcp.json",
    ".qoder/settings.json",
    ".qoder-cn/mcp.json",
    ".qoder-cn/settings.json",
    ".cursor/mcp.json",
    ".trae/mcp.json",
    ".trae-cn/mcp.json",
    ".trae-work/mcp.json",
    ".trae-work-cn/mcp.json",
    ".workbuddy/mcp.json",
    ".codebuddy/mcp.json",
    ".kimi/mcp.json",
    ".kimi-code/mcp.json",
    ".qoderwork/mcp.json",
    ".qoderwake/mcp.json",
    ".devin/mcp.json",
    ".pi/mcp.json",
    ".config/zed/settings.json",
    ".gemini/settings.json",
    "Library/Application Support/Claude/claude_desktop_config.json",
    "Library/Application Support/Claude-3p/claude_desktop_config.json",
    "Library/Application Support/Qoder/User/mcp.json",
    "Library/Application Support/QoderCN/User/mcp.json",
    "Library/Application Support/Cursor/User/mcp.json",
    "Library/Application Support/TRAE SOLO/User/mcp.json",
    "Library/Application Support/TRAE SOLO CN/User/mcp.json",
    "Library/Application Support/Trae/User/mcp.json",
    "Library/Application Support/Trae CN/User/mcp.json",
    "Library/Application Support/WorkBuddy/mcp.json",
    "Library/Application Support/WorkBuddy AI/mcp.json",
    "Library/Application Support/Qoder/SharedClientCache/extension/local/mcp.json",
    "Library/Application Support/QoderCN/SharedClientCache/extension/local/mcp.json",
)


class UninstallScriptTests(unittest.TestCase):
    def test_uninstaller_is_remote_independent(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for command in (
            "git clone",
            "git fetch",
            "git pull",
            "gh api",
            "curl ",
            "wget ",
        ):
            self.assertNotIn(command, source)

    def setUp(self) -> None:
        temp_parent = "/private/tmp" if Path("/private/tmp").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(dir=temp_parent)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self._write_command(
            "ps",
            "#!/bin/sh\nprintf '%s\\n' '  PID COMMAND'\n",
        )
        self._write_command(
            "launchctl",
            "#!/bin/sh\ncase \"$1\" in print) exit 113;; bootout) exit 0;; *) exit 2;; esac\n",
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "DE_AQG_UNINSTALL_PROCESS_COMMAND": str(self.bin / "ps"),
                "DE_AQG_UNINSTALL_LAUNCHCTL_COMMAND": str(self.bin / "launchctl"),
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_command(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), *args],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _install_fixture(self, *, unknown_de_entry: bool = False) -> tuple[Path, Path]:
        dp = self.home / ".deeppattern"
        de = dp / "decision-engine"
        aqg = dp / "agent-quality-gates"
        (de / "skills").mkdir(parents=True)
        (de / "client").mkdir()
        (de / "desktop").mkdir()
        (de / "pyproject.toml").write_text("[project]\nname='decision-engine'\n")
        (de / "config.json").write_text("{}\n")
        (aqg / "skills").mkdir(parents=True)
        (aqg / "scripts").mkdir()
        (aqg / "scripts" / "install_aqg_clients.py").write_text(
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "home = Path.home()\n"
            "root = Path(__file__).resolve().parents[1]\n"
            "config = home / '.trae' / 'mcp.json'\n"
            "if config.is_file():\n"
            "    data = json.loads(config.read_text())\n"
            "    servers = data.get('mcpServers', {})\n"
            "    entry = servers.get('aqg-support')\n"
            "    if entry is not None:\n"
            "        args = entry.get('args') if isinstance(entry, dict) else None\n"
            "        if not isinstance(args, list) or not args or not str(args[0]).startswith(str(root) + os.sep):\n"
            "            raise SystemExit(9)\n"
            "        del servers['aqg-support']\n"
            "        config.write_text(json.dumps(data) + '\\n')\n"
            "target = os.environ.get('DE_AQG_UNINSTALL_TEST_REMOVE')\n"
            "if target and Path(target).is_symlink():\n"
            "    Path(target).unlink()\n"
            "print('AQG official user uninstall')\n"
        )
        (aqg / "AI_SETUP.md").write_text("# Test AQG checkout\n")
        (aqg / "VERSION").write_text("0.0.0\n")
        (aqg / "requirements.txt").write_text("\n")
        (aqg / ".git").mkdir()

        (dp / "installations").mkdir()
        (dp / "installations" / "decision-engine.json").write_text("{}\n")

        codex = self.home / ".codex"
        (codex / "skills").mkdir(parents=True)
        (codex / "skills" / "audit").symlink_to(de / "skills" / "audit")
        (codex / "skills" / "aqg-test").symlink_to(aqg / "skills" / "aqg-test")
        (self.home / ".agents" / "skills").mkdir(parents=True)
        (self.home / ".agents" / "skills" / "audit").symlink_to(de / "skills" / "audit")
        for relative in (
            ".gemini/skills/audit",
            ".workbuddy-ai/skills/audit",
            "Library/Application Support/TRAE SOLO/User/skills/audit",
        ):
            link = self.home / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(de / "skills" / "audit")
        (codex / "AGENTS.md").write_text(
            "before\n"
            "<!-- BEGIN decision-engine routing (managed by installer.codex_routing; do not edit inside) -->\n"
            "managed\n"
            "<!-- END decision-engine routing -->\n"
            "after\n"
        )
        (codex / "config.toml").write_text(
            "[mcp_servers.other]\ncommand='other'\n\n"
            "[mcp_servers.decision-engine]\n"
            f"command='python'\nargs=['-m','installer.launcher','--managed-root','{de}']\n"
        )

        claude_entry = {
            "command": "python",
            "args": ["-m", "installer.launcher", "--managed-root", str(de)],
            "env": {"PYTHONPATH": str(de)},
        }
        if unknown_de_entry:
            claude_entry["args"] = ["-m", "foreign.launcher", "--dev-root", "/tmp/foreign"]
            claude_entry["env"] = {"PYTHONPATH": "/tmp/foreign"}
        for relative in map(Path, HOST_MCP_RELATIVES):
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "other": {"command": "other"},
                            "decision-engine": claude_entry,
                        }
                    }
                )
                + "\n"
            )
        extra_host_config = self.home / ".trae" / "mcp.json"
        extra_host_config.parent.mkdir(parents=True, exist_ok=True)
        extra_host_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "decision-engine": claude_entry,
                        "aqg-support": {
                            "command": "python3",
                            "args": [str(aqg / "scripts" / "aqg_doctor.py")],
                        },
                    }
                }
            )
            + "\n"
        )
        connector_config = self.home / ".workbuddy-ai" / "connectors" / "session-1" / "mcp.json"
        connector_config.parent.mkdir(parents=True, exist_ok=True)
        connector_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "connector:other": {"command": "other"},
                        "de-local-alias": claude_entry,
                    }
                }
            )
            + "\n"
        )
        backup_config = self.home / ".qoder" / "mcp.json.de-bak.20260901-120000"
        backup_config.write_text(
            json.dumps({"mcpServers": {"decision-engine": claude_entry}}) + "\n"
        )

        launch_agents = self.home / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        plist = {
            "Label": "com.decision-engine.stopper.hostbridge",
            "ProgramArguments": [str(de / "desktop" / "macos" / "bin" / "decision-engine-stopper-arm64")],
            "EnvironmentVariables": {
                "DE_STOPPER_HOSTBRIDGE_MANAGED": "1",
                "DE_CONFIG_PATH": str(Path(tempfile.gettempdir()) / f"decision-engine-host-bridge-{os.getuid()}" / "config.json"),
            },
            "WatchPaths": [str(Path(tempfile.gettempdir()) / f"decision-engine-host-bridge-{os.getuid()}" / ".runtime" / "active-runs.json")],
        }
        with (launch_agents / "com.decision-engine.stopper.hostbridge.plist").open("wb") as handle:
            plistlib.dump(plist, handle)

        protected = self.home / "YJ" / "worktrees" / "de-source"
        protected.mkdir(parents=True)
        (protected / "README.md").write_text("preserve\n")
        return de, protected

    def test_requires_explicit_scope(self) -> None:
        result = self._run("--apply")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("--scope", result.stdout)

    def test_dry_run_is_read_only_and_lists_owned_items(self) -> None:
        de, _ = self._install_fixture()
        result = self._run("--scope", "de")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"REMOVE {de}", result.stdout)
        self.assertIn("DRY-RUN", result.stdout)
        self.assertTrue(de.exists())
        self.assertTrue((self.home / ".codex" / "skills" / "audit").is_symlink())

    def test_unknown_host_entry_blocks_without_mutation(self) -> None:
        de, _ = self._install_fixture(unknown_de_entry=True)
        result = self._run("--scope", "de")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("ownership is unknown", result.stdout)
        self.assertTrue(de.exists())

    def test_malformed_unrelated_host_configs_are_preserved_without_blocking(self) -> None:
        for relative in (
            ".config/zed/settings.json",
            "Library/Application Support/Code/User/mcp.json",
        ):
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"unrelated": ', encoding="utf-8")

        result = self._run("--scope", "both", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("PRESERVE malformed unrelated JSON config"), 2)
        for relative in (
            ".config/zed/settings.json",
            "Library/Application Support/Code/User/mcp.json",
        ):
            self.assertEqual((self.home / relative).read_text(), '{"unrelated": ')

    def test_malformed_host_config_with_de_marker_still_blocks(self) -> None:
        path = self.home / ".config" / "zed" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"mcpServers":{"decision-engine":', encoding="utf-8")

        result = self._run("--scope", "both", "--apply")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("containing managed ownership markers", result.stdout)
        self.assertEqual(path.read_text(), '{"mcpServers":{"decision-engine":')

    def test_zed_jsonc_removes_owned_de_entry_and_preserves_user_content(self) -> None:
        de = self.home / ".deeppattern" / "decision-engine"
        path = self.home / ".config" / "zed" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            "{\n"
            "  // Keep this user comment.\n"
            '  "theme": "One Dark",\n'
            '  "mcpServers": {\n'
            '    "other": {"command": "other"},\n'
            '    "decision-engine": {\n'
            '      "command": "python3",\n'
            '      "args": ["-m", "installer.launcher"],\n'
            f'      "env": {{"PYTHONPATH": "{de}"}},\n'
            "    },\n"
            "  },\n"
            "}\n",
            encoding="utf-8",
        )

        result = self._run("--scope", "de", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout)
        updated = path.read_text(encoding="utf-8")
        self.assertIn("// Keep this user comment.", updated)
        self.assertIn('"theme": "One Dark"', updated)
        self.assertIn('"other": {"command": "other"}', updated)
        self.assertNotIn('"decision-engine"', updated)
        self.assertNotIn("STOP:", result.stdout)

    def test_zed_jsonc_marker_in_comment_is_ignored(self) -> None:
        path = self.home / ".config" / "zed" / "settings.json"
        path.parent.mkdir(parents=True)
        original = (
            "{\n"
            "  // Example text only: decision-engine uses installer.launcher.\n"
            '  "theme": "One Dark",\n'
            "}\n"
        )
        path.write_text(original, encoding="utf-8")

        result = self._run("--scope", "de", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertNotIn("BLOCKED", result.stdout)

    def test_managed_aqg_alias_env_is_removed_after_official_uninstall(self) -> None:
        dp = self.home / ".deeppattern"
        target = dp / "versions" / "0.14.14"
        scripts = target / "scripts"
        scripts.mkdir(parents=True)
        (target / "skills").mkdir()
        (target / "AI_SETUP.md").write_text("setup\n", encoding="utf-8")
        (target / "VERSION").write_text("0.14.14\n", encoding="utf-8")
        (target / "requirements.txt").write_text("\n", encoding="utf-8")
        (scripts / "install_aqg_clients.py").write_text(
            "print('AQG official user uninstall')\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "add", "."], check=True)
        subprocess.run(
            [
                "git", "-C", str(target),
                "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "fixture",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(target), "remote", "add", "origin",
                "https://github.com/deeppatternai/agent-quality-gates.git",
            ],
            check=True,
        )
        (dp / "agent-quality-gates").symlink_to(target)

        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps(
                {
                    "env": {"AQG_ROOT": str(dp / "agent-quality-gates")},
                    "permissions": {"allow": ["Read"]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = self._run("--scope", "both", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout)
        updated = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("AQG_ROOT", updated.get("env", {}))
        self.assertEqual(updated["permissions"], {"allow": ["Read"]})
        self.assertFalse((dp / "agent-quality-gates").exists())
        self.assertFalse(target.exists())
        self.assertIn("PASS: uninstall verified", result.stdout)

    def test_orphaned_managed_aqg_alias_env_is_removed_on_retry(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps(
                {
                    "env": {
                        "AQG_ROOT": str(
                            self.home / ".deeppattern" / "agent-quality-gates"
                        )
                    },
                    "permissions": {"allow": ["Read"]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = self._run("--scope", "both", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout)
        updated = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("AQG_ROOT", updated.get("env", {}))
        self.assertEqual(updated["permissions"], {"allow": ["Read"]})
        self.assertIn("PASS: uninstall verified", result.stdout)

    def test_orphaned_managed_aqg_alias_and_hooks_are_removed_on_retry(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        managed_command = (
            'if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; '
            'CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}" bash '
            '"$AQG_ROOT/agent-packs/claude-code/hooks/sessionstart_preflight.sh" '
            '"${CLAUDE_PROJECT_DIR:-}" || true'
        )
        unrelated_group = {
            "matcher": "startup",
            "hooks": [
                {
                    "type": "command",
                    "command": "printf 'keep-user-hook\\n'",
                }
            ],
        }
        settings.write_text(
            json.dumps(
                {
                    "env": {
                        "AQG_ROOT": str(
                            self.home / ".deeppattern" / "agent-quality-gates"
                        ),
                        "KEEP_ME": "yes",
                    },
                    "hooks": {
                        "SessionStart": [
                            {
                                "matcher": "",
                                "hooks": [
                                    {"type": "command", "command": managed_command}
                                ],
                            },
                            unrelated_group,
                        ]
                    },
                    "permissions": {"allow": ["Read"]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        original_text = settings.read_text(encoding="utf-8")

        result = self._run("--scope", "both", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout)
        updated = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("AQG_ROOT", updated["env"])
        self.assertEqual(updated["env"]["KEEP_ME"], "yes")
        self.assertEqual(updated["hooks"]["SessionStart"], [unrelated_group])
        self.assertEqual(updated["permissions"], {"allow": ["Read"]})
        backups = list(
            (self.home / ".deeppattern" / "uninstall-backups").glob(
                "*-dp-uninstall-both/config/*-settings.json"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original_text)
        self.assertIn("PASS: uninstall verified", result.stdout)

    def test_orphaned_foreign_aqg_hook_root_still_blocks_without_mutation(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        original = {
            "env": {
                "AQG_ROOT": str(
                    self.home / ".deeppattern" / "agent-quality-gates"
                )
            },
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    'bash "/opt/company/custom-aqg/agent-packs/'
                                    'claude-code/hooks/sessionstart_preflight.sh"'
                                ),
                            }
                        ],
                    }
                ]
            },
        }
        settings.write_text(json.dumps(original) + "\n", encoding="utf-8")

        result = self._run("--scope", "both", "--apply")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("AQG references point to untrusted installer root", result.stdout)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)

    def test_orphaned_mixed_aqg_hook_group_still_blocks_without_mutation(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        managed_command = (
            'if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; '
            'CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}" bash '
            '"$AQG_ROOT/agent-packs/claude-code/hooks/sessionstart_preflight.sh" '
            '"${CLAUDE_PROJECT_DIR:-}" || true'
        )
        original = {
            "env": {
                "AQG_ROOT": str(
                    self.home / ".deeppattern" / "agent-quality-gates"
                )
            },
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": managed_command},
                            {
                                "type": "command",
                                "command": "printf 'keep-user-hook\\n'",
                            },
                        ],
                    }
                ]
            },
        }
        settings.write_text(json.dumps(original) + "\n", encoding="utf-8")

        result = self._run("--scope", "both", "--apply")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("AQG managed hooks or skills remain", result.stdout)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)

    def test_custom_aqg_root_env_is_preserved(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        original = {
            "env": {"AQG_ROOT": "/opt/company/custom-aqg"},
            "permissions": {"allow": ["Read"]},
        }
        settings.write_text(json.dumps(original) + "\n", encoding="utf-8")

        result = self._run("--scope", "both", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)

    def test_oversized_host_config_still_blocks(self) -> None:
        path = self.home / ".config" / "zed" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b" " * (8 * 1024 * 1024 + 1))

        result = self._run("--scope", "both", "--apply")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("exceeds the safe inspection limit", result.stdout)
        self.assertTrue(path.exists())

    def test_apply_quarantines_de_and_preserves_source(self) -> None:
        de, protected = self._install_fixture()
        result = self._run("--scope", "de", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(de.exists(), result.stdout)
        self.assertTrue(protected.exists())
        self.assertTrue((self.home / ".deeppattern" / "agent-quality-gates").exists())
        self.assertFalse(
            (self.home / "Library" / "LaunchAgents" / "com.decision-engine.stopper.hostbridge.plist").exists()
        )
        self.assertFalse((self.home / ".codex" / "skills" / "audit").exists())
        self.assertFalse((self.home / ".agents" / "skills" / "audit").exists())
        self.assertIn("other", json.loads((self.home / ".claude.json").read_text())["mcpServers"])
        self.assertNotIn("decision-engine", json.loads((self.home / ".claude.json").read_text())["mcpServers"])
        for relative in HOST_MCP_RELATIVES:
            data = json.loads((self.home / relative).read_text())
            self.assertNotIn("decision-engine", data["mcpServers"], relative)
        trae_data = json.loads((self.home / ".trae" / "mcp.json").read_text())
        self.assertIn("aqg-support", trae_data["mcpServers"])
        connector_data = json.loads(
            (self.home / ".workbuddy-ai" / "connectors" / "session-1" / "mcp.json").read_text()
        )
        self.assertIn("connector:other", connector_data["mcpServers"])
        self.assertNotIn("de-local-alias", connector_data["mcpServers"])
        self.assertFalse((self.home / ".qoder" / "mcp.json.de-bak.20260901-120000").exists())
        backup_roots = list(
            (self.home / ".deeppattern" / "uninstall-backups").glob(
                "*-dp-uninstall-de"
            )
        )
        self.assertEqual(len(backup_roots), 1)
        self.assertTrue(
            list(
                (backup_roots[0] / "quarantine" / "host-backups").glob(
                    "*mcp.json.de-bak.20260901-120000"
                )
            )
        )

        rerun = self._run("--scope", "de", "--apply")
        self.assertEqual(rerun.returncode, 0, rerun.stdout)

    def test_both_runs_official_aqg_user_uninstall_and_removes_all_routes(self) -> None:
        de, _ = self._install_fixture()
        result = self._run("--scope", "both", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("AQG official user uninstall", result.stdout)
        self.assertFalse(de.exists())
        self.assertFalse((self.home / ".deeppattern" / "agent-quality-gates").exists())
        self.assertNotIn(
            "aqg-support",
            json.loads((self.home / ".trae" / "mcp.json").read_text())["mcpServers"],
        )
        self.assertFalse((self.home / ".codex" / "skills" / "aqg-test").exists())
        for relative in (
            ".gemini/skills/audit",
            ".workbuddy-ai/skills/audit",
            "Library/Application Support/TRAE SOLO/User/skills/audit",
        ):
            self.assertFalse((self.home / relative).exists())

    def test_both_is_idempotent_when_aqg_uninstaller_removed_planned_skill(self) -> None:
        de, _ = self._install_fixture()
        removed_by_official = self.home / ".codex" / "skills" / "aqg-test"
        self.env["DE_AQG_UNINSTALL_TEST_REMOVE"] = str(removed_by_official)
        result = self._run("--scope", "both", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(de.exists(), result.stdout)
        self.assertFalse((self.home / ".deeppattern" / "agent-quality-gates").exists())

    def test_both_quarantines_install_lock_and_aqg_backup_residue(self) -> None:
        self._install_fixture()
        dp = self.home / ".deeppattern"
        (dp / ".install.lock").write_text("")
        backup = dp / "aqg-backups" / "codex" / "user" / "run-1"
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_text("{}\n")
        result = self._run("--scope", "both", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((dp / ".install.lock").exists())
        self.assertFalse((dp / "aqg-backups").exists())
        backup_roots = list((dp / "uninstall-backups").glob("*-dp-uninstall-both"))
        self.assertEqual(len(backup_roots), 1)
        self.assertTrue((backup_roots[0] / "quarantine" / "aux" / ".install.lock").exists())
        self.assertTrue((backup_roots[0] / "quarantine" / "aux" / "aqg-backups").exists())

    def test_unknown_aqg_host_entry_blocks_without_mutation(self) -> None:
        de, _ = self._install_fixture()
        path = self.home / ".trae" / "mcp.json"
        data = json.loads(path.read_text())
        data["mcpServers"]["aqg-support"] = {
            "command": "python3",
            "args": ["/tmp/foreign/aqg_doctor.py"],
        }
        path.write_text(json.dumps(data) + "\n")
        result = self._run("--scope", "both", "--apply")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("AQG official user-scope uninstaller failed", result.stdout)
        self.assertTrue(de.exists())

    def test_unqualified_launcher_blocks_when_process_ownership_is_unknown(self) -> None:
        self._install_fixture()
        self._write_command(
            "ps",
            "#!/bin/sh\nprintf '%s\\n' '900 1 /opt/python -m installer.launcher'\n",
        )
        self._write_command(
            "lsof",
            "#!/bin/sh\nprintf '%s\\n' 'n/private/tmp/foreign-launcher'\n",
        )
        result = self._run("--scope", "de")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("live process ownership is unknown", result.stdout)

    def test_unqualified_launcher_is_blocked_when_lsof_proves_de_ownership(self) -> None:
        de, _ = self._install_fixture()
        self._write_command(
            "ps",
            "#!/bin/sh\nprintf '%s\\n' '901 1 /opt/python -m installer.launcher'\n",
        )
        self._write_command(
            "lsof",
            f"#!/bin/sh\nprintf '%s\\n' 'n{de}/installer/launcher.py'\n",
        )
        result = self._run("--scope", "de")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("BLOCKED PROCESS pid=901", result.stdout)


if __name__ == "__main__":
    unittest.main()
