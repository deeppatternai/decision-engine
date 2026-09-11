#!/usr/bin/env python3
"""Regression locks for Windows installer trust-boundary hardening."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[2]
INSTALL = TOOLS / "dp-install.ps1"
UNINSTALL = TOOLS / "dp-uninstall.ps1"


def embedded_python(source: str) -> tuple[str, ...]:
    return tuple(
        match.group(1)
        for match in re.finditer(r"\$[A-Za-z][A-Za-z0-9]*\s*=\s*@'\n(.*?)\n'@", source, re.S)
    )


class WindowsIntegrityHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.install = INSTALL.read_text(encoding="utf-8")
        cls.uninstall = UNINSTALL.read_text(encoding="utf-8")
        cls.aqg_layout_resolver = next(
            snippet
            for snippet in embedded_python(cls.install)
            if "versions = Path(sys.argv[2])" in snippet
        )
        cls.helper_source = embedded_python(cls.uninstall)[-1]
        cls.helper: dict[str, object] = {"__name__": __name__}
        exec(compile(cls.helper_source, str(UNINSTALL), "exec"), cls.helper)

    def test_generated_python_is_isolated_from_invocation_cwd(self) -> None:
        self.assertNotIn("sys.path.insert(0, str(Path.cwd()))", self.install)
        self.assertIn('@("-I", $scriptPath)', self.install)
        self.assertIn('"-I", $helperPath', self.uninstall)

    def test_checkout_imports_use_an_explicit_root_before_import(self) -> None:
        snippets = embedded_python(self.install)
        importing = [
            snippet
            for snippet in snippets
            if "from installer import" in snippet or "from scripts import" in snippet
        ]
        self.assertGreaterEqual(len(importing), 5)
        for snippet in importing:
            root_binding = snippet.find("root = Path(sys.argv[1]).resolve")
            path_binding = snippet.find("sys.path")
            first_checkout_import = min(
                position
                for position in (
                    snippet.find("from installer import"),
                    snippet.find("from scripts import"),
                )
                if position >= 0
            )
            self.assertGreaterEqual(root_binding, 0)
            self.assertGreater(path_binding, root_binding)
            self.assertLess(path_binding, first_checkout_import)

    def test_all_inherited_git_environment_is_removed(self) -> None:
        for source in (self.install, self.uninstall):
            self.assertIn('GetEnvironmentVariables("Process").Keys', source)
            self.assertIn('$_ -match "^(?i:GIT_)"', source)
        self.assertIn('key.upper().startswith("GIT_")', self.uninstall)

    def run_aqg_layout_resolver(self, target_name: str, version: str | None) -> int:
        with tempfile.TemporaryDirectory() as temporary:
            versions = Path(temporary) / "versions"
            target = versions / target_name
            target.mkdir(parents=True)
            if version is not None:
                (target / "VERSION").write_text(version + "\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    self.aqg_layout_resolver,
                    str(target),
                    str(versions),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            if result.returncode:
                return result.returncode
            name_check = re.search(r"\$aqgNameScript\s*=\s*@'\n(.*?)\n'@", self.install, re.S)
            self.assertIsNotNone(name_check)
            return subprocess.run(
                [sys.executable, '-I', '-c', name_check.group(1), str(target), 'a' * 40],
                capture_output=True, check=False, text=True,
            ).returncode

    def test_aqg_layout_accepts_release_named_target_with_matching_version(self) -> None:
        self.assertEqual(self.run_aqg_layout_resolver("0.14.14", "0.14.14"), 0)

    def test_aqg_layout_rejects_release_named_target_with_mismatched_version(self) -> None:
        self.assertNotEqual(self.run_aqg_layout_resolver("0.14.14", "0.14.13"), 0)

    def test_aqg_layout_continues_to_accept_commit_named_target(self) -> None:
        self.assertEqual(self.run_aqg_layout_resolver("a" * 40, None), 0)

    def test_aqg_layout_rejects_ambiguous_target_name(self) -> None:
        self.assertNotEqual(self.run_aqg_layout_resolver("latest", "latest"), 0)

    def test_path_lookup_is_not_used_for_integrity_critical_tools(self) -> None:
        combined = self.install + self.uninstall
        for command in (
            "Get-Command winget.exe",
            "Get-Command git.exe",
            "Get-Command bash.exe",
            "Get-Command py.exe",
            "Get-Command python.exe",
            "Get-Command python3.exe",
        ):
            self.assertNotIn(command, combined)

    def test_native_tools_require_absolute_authenticode_verified_paths(self) -> None:
        for source in (self.install, self.uninstall):
            self.assertIn("[IO.Path]::IsPathRooted", source)
            self.assertIn("Get-AuthenticodeSignature -LiteralPath", source)
            self.assertIn("SignatureStatus]::Valid", source)
        self.assertIn("Microsoft.DesktopAppInstaller", self.install)
        self.assertIn("Johannes Schindelin|Git for Windows", self.install)
        self.assertNotIn("Resolve-CurrentPowerShell", self.uninstall)

    def test_uninstall_helper_receives_verified_tool_paths(self) -> None:
        helper = self.helper_source
        self.assertNotIn('["powershell.exe"', helper)
        self.assertNotIn('["git.exe"', helper)
        self.assertIn("[GIT_EXE,", helper)
        self.assertIn('parser.add_argument("--git", type=Path)', helper)
        self.assertIn(
            'parser.add_argument("--process-inventory", type=Path, required=True)',
            helper,
        )
        self.assertIn("PowerShell process inventory digest mismatch", helper)
        self.assertIn('$arguments += @("--git", $GitPath)', self.uninstall)
        self.assertIn("Write-ProcessInventory -Path $processInventoryPath", self.uninstall)

    def test_uninstall_helper_accepts_matching_process_inventory(self) -> None:
        rows = [
            {
                "ProcessId": 101,
                "ParentProcessId": 10,
                "Name": "python.exe",
                "ExecutablePath": r"C:\Python\python.exe",
                "CommandLine": "python -m installer.launcher",
            }
        ]
        payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "processes.json"
            path.write_bytes(payload)
            self.helper["PROCESS_INVENTORY_PATH"] = path
            self.helper["PROCESS_INVENTORY_SHA256"] = hashlib.sha256(payload).hexdigest()
            self.assertEqual(self.helper["windows_process_rows"](), tuple(rows))

    def test_uninstall_helper_rejects_tampered_process_inventory(self) -> None:
        original = b"[]"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "processes.json"
            path.write_bytes(original)
            self.helper["PROCESS_INVENTORY_PATH"] = path
            self.helper["PROCESS_INVENTORY_SHA256"] = hashlib.sha256(original).hexdigest()
            path.write_bytes(b'[{"ProcessId":999}]')
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                self.helper["windows_process_rows"]()

    def test_every_embedded_python_block_compiles(self) -> None:
        for path, source in ((INSTALL, self.install), (UNINSTALL, self.uninstall)):
            snippets = embedded_python(source)
            self.assertTrue(snippets)
            for index, snippet in enumerate(snippets):
                compile(snippet, f"{path} embedded block {index}", "exec")


if __name__ == "__main__":
    unittest.main()
