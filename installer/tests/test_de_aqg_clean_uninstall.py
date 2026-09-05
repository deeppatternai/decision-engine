from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from installer import leak_scan


class PublicInstallEntryPointTests(unittest.TestCase):
    def test_install_entrypoint_passes_public_disclosure_scan(self):
        repo_root = Path(__file__).resolve().parents[2]
        findings = leak_scan.scan_paths([repo_root / "de-aqg-install"])

        self.assertEqual(
            findings,
            [],
            [(finding.kind, finding.line) for finding in findings],
        )


class CleanUninstallQoderHookTests(unittest.TestCase):
    def test_de_scope_removes_only_owned_qoder_prompt_hook(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "de-aqg-clean-uninstall"
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
        script = repo_root / "de-aqg-clean-uninstall"
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


if __name__ == "__main__":
    unittest.main()
