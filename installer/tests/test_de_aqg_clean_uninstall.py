from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from installer import leak_scan


class PublicInstallEntryPointTests(unittest.TestCase):
    def test_public_entrypoints_use_only_canonical_dp_names(self):
        repo_root = Path(__file__).resolve().parents[2]

        for name in (
            "dp-install.sh",
            "dp-uninstall.sh",
            "dp-install.ps1",
            "dp-uninstall.ps1",
        ):
            self.assertTrue((repo_root / name).is_file(), name)
        for name in ("de-aqg-install", "de-aqg-clean-uninstall"):
            self.assertFalse((repo_root / name).exists(), name)

    def test_public_entrypoints_have_only_reviewed_disclosure_findings(self):
        repo_root = Path(__file__).resolve().parents[2]
        expected = {
            "dp-install.sh": [
                ("secret:bearer-hex-token", 348),
                ("secret:bearer-hex-token", 353),
                ("ipv6:::dec", 1363),
            ],
            "dp-install.ps1": [
                ("ipv6:::e", 59),
                # Fixed public SHA-256 digests for the pinned private runtimes.
                ("secret:bearer-hex-token", 515),
                ("secret:bearer-hex-token", 520),
                *(
                    ("ipv6:::e", line)
                    for line in (
                        1644,
                        1870,
                        2044,
                        2053,
                        2063,
                        2071,
                        2091,
                        2101,
                        2166,
                        2255,
                        2263,
                    )
                ),
            ],
            "dp-uninstall.ps1": [
                ("ipv6:::e", line) for line in (39, 139, 487, 2334, 2340)
            ],
        }

        for name, reviewed_findings in expected.items():
            with self.subTest(name=name):
                findings = leak_scan.scan_paths([repo_root / name])
                self.assertEqual(
                    [(finding.kind, finding.line) for finding in findings],
                    reviewed_findings,
                )


class CleanUninstallQoderHookTests(unittest.TestCase):
    def test_de_scope_removes_qoder_ide_connectors_and_shared_hooks(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "dp-uninstall.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            for family, client in (
                (".qoder", "qoder-ide"),
                (".qoder-cn", "qoder-cn-ide"),
            ):
                profile = home / family
                profile.mkdir(parents=True)
                (profile / "mcp.json").write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                "keep": {"command": "keep"},
                                "decision-engine": {
                                    "command": "/usr/bin/python3",
                                    "args": [
                                        "/opt/decision-engine/installer/mcp_bootstrap.py",
                                        "/opt/decision-engine",
                                        "--managed-root",
                                        "/opt/decision-engine",
                                    ],
                                    "env": {"DE_MCP_CLIENT_HOST": client},
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (profile / "settings.json").write_text(
                    json.dumps(
                        {
                            "hooks": {
                                "UserPromptSubmit": [
                                    {
                                        "matcher": "",
                                        "hooks": [
                                            {
                                                "type": "command",
                                                "command": "/usr/bin/python3",
                                                "args": [
                                                    "/opt/decision-engine/installer/qoder_audit_prompt_hook.py"
                                                ],
                                                "name": "decision-engine-audit-routing-v1",
                                            }
                                        ],
                                    }
                                ]
                            },
                            "enabledPlugins": {"keep": True},
                        }
                    ),
                    encoding="utf-8",
                )
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

            completed = subprocess.run(
                [str(script), "--scope", "de", "--apply"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout)
            for family in (".qoder", ".qoder-cn"):
                profile = home / family
                mcp = json.loads((profile / "mcp.json").read_text(encoding="utf-8"))
                settings = json.loads(
                    (profile / "settings.json").read_text(encoding="utf-8")
                )
                self.assertEqual(mcp["mcpServers"], {"keep": {"command": "keep"}})
                self.assertEqual(settings["hooks"]["UserPromptSubmit"], [])
                self.assertEqual(settings["enabledPlugins"], {"keep": True})

    def test_de_scope_removes_only_owned_qoder_prompt_hook(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "dp-uninstall.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            settings = home / ".qoder-cn" / "settings.json"
            settings.parent.mkdir(parents=True)
            aqg_group = {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/bin/python3 /opt/aqg/qoder_hook_adapter.py",
                    }
                ],
            }
            de_group = {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/bin/python3",
                        "args": [
                            "/opt/decision-engine/installer/qoder_audit_prompt_hook.py"
                        ],
                        "name": "decision-engine-audit-routing-v1",
                    }
                ],
            }
            legacy_group = {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/bin/python3",
                        "args": [
                            "/old/decision-engine/installer/qoder_audit_prompt_hook.py"
                        ],
                        "name": "decision-engine-audit-routing-experiment",
                    }
                ],
            }
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [aqg_group, de_group, legacy_group]
                        },
                        "enabledPlugins": {"keep": True},
                    }
                ),
                encoding="utf-8",
            )
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

            completed = subprocess.run(
                [str(script), "--scope", "de", "--apply"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"]["UserPromptSubmit"], [aqg_group])
            self.assertTrue(data["enabledPlugins"]["keep"])
            self.assertIn("PASS: uninstall verified", completed.stdout)

    def test_de_scope_blocks_foreign_same_name_qoder_hook(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "dp-uninstall.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            settings = home / ".qoder" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "matcher": "",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "/opt/foreign/hook",
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
            before = settings.read_bytes()
            launchctl = root / "launchctl"
            launchctl.write_text("#!/bin/sh\nexit 113\n", encoding="utf-8")
            launchctl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "DEEPPATTERN_HOME": str(home / ".deeppattern"),
                    "DE_AQG_UNINSTALL_PROCESS_COMMAND": "/usr/bin/true",
                    "DE_AQG_UNINSTALL_LAUNCHCTL_COMMAND": str(launchctl),
                }
            )

            completed = subprocess.run(
                [str(script), "--scope", "de", "--apply"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertEqual(completed.returncode, 3, completed.stdout)
            self.assertIn("ownership is unknown", completed.stdout)
            self.assertEqual(settings.read_bytes(), before)


class PublicUninstallWorkBuddyAIHookTests(unittest.TestCase):
    def test_de_scope_blocks_foreign_same_id_workbuddy_ai_prompt_hook(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "dp-uninstall.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            settings = home / ".workbuddy-ai" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "matcher": "",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "/opt/foreign/hook --managed-id "
                                                "decision-engine-workbuddy-ai-audit-routing-v1"
                                            ),
                                            "timeout": 30,
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            before = settings.read_bytes()
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

            completed = subprocess.run(
                [str(script), "--scope", "de", "--apply"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("ownership is unknown", completed.stdout)
            self.assertIn("STOP:", completed.stdout)
            self.assertEqual(settings.read_bytes(), before)

    def test_de_scope_removes_only_owned_workbuddy_ai_prompt_hook(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "dp-uninstall.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            settings = home / ".workbuddy-ai" / "settings.json"
            settings.parent.mkdir(parents=True)
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
            de_group = {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "/usr/bin/python3 "
                            "/opt/decision-engine/installer/workbuddy_audit_prompt_hook.py "
                            "--managed-id decision-engine-workbuddy-ai-audit-routing-v1"
                        ),
                        "timeout": 30,
                    }
                ],
            }
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [aqg_group, de_group, user_group]
                        }
                    }
                ),
                encoding="utf-8",
            )
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

            completed = subprocess.run(
                [str(script), "--scope", "de", "--apply"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(
                data["hooks"]["UserPromptSubmit"], [aqg_group, user_group]
            )
            self.assertIn("PASS: uninstall verified", completed.stdout)

    def test_de_scope_removes_only_owned_trae_cn_prompt_hook(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "dp-uninstall.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            hooks = home / ".trae-cn" / "hooks.json"
            hooks.parent.mkdir(parents=True)
            aqg_group = {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/bin/python3 /opt/aqg/hook.py userPromptSubmit",
                    }
                ],
            }
            de_group = {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "/usr/bin/python3 "
                            "/opt/decision-engine/installer/trae_cn_audit_prompt_hook.py "
                            "--managed-id decision-engine-trae-cn-audit-routing-v1"
                        ),
                        "timeout": 30,
                    }
                ],
            }
            hooks.write_text(
                json.dumps(
                    {"hooks": {"UserPromptSubmit": [aqg_group, de_group]}}
                ),
                encoding="utf-8",
            )
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

            completed = subprocess.run(
                [str(script), "--scope", "de", "--apply"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout)
            data = json.loads(hooks.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"]["UserPromptSubmit"], [aqg_group])
            self.assertIn("PASS: uninstall verified", completed.stdout)


if __name__ == "__main__":
    unittest.main()
