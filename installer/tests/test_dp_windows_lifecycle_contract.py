"""Behavior locks for the Windows install and uninstall lifecycle entrypoints."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from installer.tests.test_dp_windows_integrity_hardening import (
    INSTALL,
    UNINSTALL,
    embedded_python,
)


def embedded_variable(source: str, name: str) -> str:
    match = re.search(rf"\${re.escape(name)}\s*=\s*@'\n(.*?)\n'@", source, re.S)
    if match is None:
        raise AssertionError(f"missing embedded variable: {name}")
    return match.group(1)


def powershell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^function {re.escape(name)} \{{.*?^\}}", source)
    if match is None:
        raise AssertionError(f"missing PowerShell function: {name}")
    return match.group(0)


class WindowsLifecycleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.install = INSTALL.read_text(encoding="utf-8")
        cls.uninstall = UNINSTALL.read_text(encoding="utf-8")
        cls.helper_source = embedded_variable(cls.uninstall, "helperSource")
        cls.helper: dict[str, object] = {"__name__": __name__}
        exec(compile(cls.helper_source, str(UNINSTALL), "exec"), cls.helper)

    def test_activation_wrapper_suppresses_only_success_status_dialog(self) -> None:
        setup_script = embedded_variable(self.install, "setupScript")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            package = root / "installer"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "permanent_setup.py").write_text(
                """
def _show_gui_message(title, message, *, error=False):
    print(f"dialog:{title}:{error}")

def main(argv):
    _show_gui_message("success", "done")
    _show_gui_message("failure", "failed", error=True)
    return 23
""".lstrip(),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", setup_script, str(root)],
                capture_output=True,
                check=False,
                text=True,
                timeout=15,
            )
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "dialog:failure:True")

    def test_active_session_defers_update_without_process_prompt(self) -> None:
        deferred = self.install.index(
            '$managedUpdate.Status -eq "deferred_active_session"'
        )
        activation = self.install.index(
            'Write-Host "Opening the masked Decision Engine activation window..."'
        )
        self.assertLess(deferred, activation)
        continuation = self.install[deferred:activation]
        self.assertIn("setup will continue without stopping Agent applications", continuation)
        self.assertNotIn("Confirm-UserAction", continuation)
        self.assertNotIn("Stop-Process", continuation)
        self.assertIn("SUCCESS_WITH_RESTART_REQUIRED", self.install)

    def test_private_runtime_is_pinned_and_reused_through_managed_environment(self) -> None:
        for expected in (
            '$PrivatePythonVersion = "3.13.15"',
            '$PrivatePythonBuild = "20260901"',
            '"9bcc038a0bf180612ed56dec93d4977d0'
            '35e80b8d9320ef51a38c287baf134b7"',
            '"ce87247378f43f88e0202a0fa6d3cdb5'
            'f5fb246a3bc61b2fb604bd49b7862508"',
            '"--fail", "--location", "--show-error", "--progress-bar"',
            '"--proto", "=https", "--tlsv1.2"',
            'Reusing the private Deep Pattern Python environment',
        ):
            self.assertIn(expected, self.install)
        self.assertLess(
            self.install.index("if (Test-ManagedPythonEnvironment)"),
            self.install.index("$basePython = Install-PrivatePythonRuntime"),
        )

    def test_uninstall_inventory_cleans_owned_state_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            dp = home / ".deeppattern"
            dp.mkdir(parents=True)
            (dp / ".install.lock").write_bytes(b"1")
            source = dp / "decision-engine-root"
            source.mkdir()
            (source / "personal.txt").write_text("preserve", encoding="utf-8")

            managed_python = dp / "de-python"
            managed_python.mkdir()
            (managed_python / ".deeppattern-python-env").write_text(
                "schema=1\n", encoding="utf-8"
            )
            runtime = dp / "runtimes" / "runtime"
            runtime.mkdir(parents=True)
            (runtime / ".deeppattern-python-runtime").write_text(
                "schema=1\n", encoding="utf-8"
            )
            runtime_backup = dp / "runtime-backups" / "backup"
            runtime_backup.mkdir(parents=True)
            (runtime_backup / ".deeppattern-python-env").write_text(
                "schema=1\n", encoding="utf-8"
            )
            installations = dp / "installations"
            installations.mkdir()
            for name in ("decision-engine.json", "decision-engine.identity.lock"):
                (installations / name).write_text("{}\n", encoding="utf-8")
            (dp / "popup-sessions").mkdir()
            (dp / "aqg-state").mkdir()
            (dp / "aqg-backups").mkdir()
            (dp / "versions" / "aqg-backups").mkdir(parents=True)

            inventory = self.helper["Inventory"](home, "both")
            inventory.inspect_managed_state_cleanup()
            planned = {action.path for action in inventory.actions}

            expected = {
                dp / ".install.lock",
                dp / "de-python",
                dp / "runtimes",
                dp / "runtime-backups",
                installations / "decision-engine.json",
                installations / "decision-engine.identity.lock",
                dp / "installations",
                dp / "popup-sessions",
                dp / "aqg-state",
                dp / "aqg-backups",
                dp / "versions" / "aqg-backups",
                dp / "versions",
            }
            self.assertTrue(expected.issubset(planned))
            self.assertNotIn(source, planned)
            self.assertEqual(inventory.blockers, [])

    def test_uninstall_backup_retention_keeps_latest_five(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            dp = home / ".deeppattern"
            backup_root = dp / "uninstall-backups"
            backup_root.mkdir(parents=True)
            for index in range(7):
                path = backup_root / f"202609{index + 1:02d}-120000-dp-uninstall-both"
                path.mkdir()
                (path / "manifest.json").write_text(
                    json.dumps(
                        {
                            "schema": 1,
                            "platform": "win32",
                            "scope": "both",
                            "home": str(home),
                            "items": [],
                        }
                    ),
                    encoding="utf-8",
                )

            removed = self.helper["prune_uninstall_backups"](home, dp)
            remaining = sorted(path.name for path in backup_root.iterdir())

            self.assertEqual(removed, 2)
            self.assertEqual(len(remaining), 5)
            self.assertEqual(remaining[0], "20260903-120000-dp-uninstall-both")

    @unittest.skipUnless(
        os.name == "nt", "Windows PowerShell 5.1 lifecycle behavior"
    )
    def test_native_powershell_process_collection_and_child_first_cleanup(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        functions = "\n\n".join(
            powershell_function(self.uninstall, name)
            for name in (
                "Get-VisibleLauncherSnapshots",
                "Get-ManagedTerminationOrder",
                "Resolve-ActiveManagedSessions",
            )
        )
        harness = f"""
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
{functions}

function Get-CimInstance {{
    return @([pscustomobject]@{{ ProcessId = 10; CommandLine = "python -m installer.launcher" }})
}}
function Get-ProcessSnapshot {{
    param([int]$ProcessId)
    if ($script:running.ContainsKey($ProcessId)) {{ return $script:running[$ProcessId] }}
    return $null
}}
function Test-ManagedLauncherSnapshot {{ param($Snapshot) return $true }}
function Test-ManagedLeasePid {{ param([int]$ProcessId) return $true }}
function Test-ManagedCommandReference {{ param($Snapshot) return $true }}
function Test-SameProcessSnapshot {{ param($Expected, $Actual) return $true }}
function Get-ManagedProcessCandidatePids {{ return @(100, 200) }}
function Write-ManagedProcessSummary {{ param($Snapshots) }}
function Confirm-UserAction {{ param([string]$Message) return $true }}
function Start-Sleep {{ param([int]$Milliseconds) }}
function Stop-Process {{
    param([int]$Id, [switch]$Force, $ErrorAction)
    $script:calls += $Id
    if ($Id -eq 200) {{
        $script:running.Remove(200)
        $script:running.Remove(100)
    }}
}}

$script:running = @{{
    10 = [pscustomobject]@{{ ProcessId = 10; ParentProcessId = 0; ExecutablePath = "visible.exe" }}
    100 = [pscustomobject]@{{ ProcessId = 100; ParentProcessId = 0; ExecutablePath = "parent.exe" }}
    200 = [pscustomobject]@{{ ProcessId = 200; ParentProcessId = 100; ExecutablePath = "child.exe" }}
}}
$script:calls = @()
$visible = @(Get-VisibleLauncherSnapshots)
if ($visible.Count -ne 1) {{ throw "generic process collection failed" }}
if (-not (Resolve-ActiveManagedSessions)) {{ throw "managed cleanup failed" }}
if (($script:calls -join ",") -ne "200") {{ throw "unexpected termination order: $($script:calls -join ',')" }}
Write-Host "PASS"
"""
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "lifecycle.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
