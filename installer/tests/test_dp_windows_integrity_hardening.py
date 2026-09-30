#!/usr/bin/env python3
"""Regression locks for Windows installer trust-boundary hardening."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
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


def named_embedded_python(source: str, name: str) -> str:
    match = re.search(rf"\${re.escape(name)}\s*=\s*@'\n(.*?)\n'@", source, re.S)
    if match is None:
        raise AssertionError(f"missing embedded Python block: {name}")
    return match.group(1)


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
        self.assertIn('@("-I", "-X", "utf8", $scriptPath)', self.install)
        self.assertIn('"-I", "-X", "utf8", $helperPath', self.uninstall)

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
        self.assertIn('parser.add_argument("--bash", type=Path)', helper)
        self.assertIn(
            'parser.add_argument("--process-inventory", type=Path, required=True)',
            helper,
        )
        self.assertIn("PowerShell process inventory digest mismatch", helper)
        self.assertIn('$arguments += @("--git", $GitPath)', self.uninstall)
        self.assertIn('$arguments += @("--bash", $BashPath)', self.uninstall)
        self.assertIn("Write-ProcessInventory -Path $processInventoryPath", self.uninstall)

    def test_installer_native_trust_fallback_remains_fail_closed(self) -> None:
        bridge = named_embedded_python(self.install, "bridgeScript")
        tree = ast.parse(bridge)
        module_source = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "module_content"
                for target in node.targets
            )
        )
        compile(module_source, "deeppattern_windows_native_trust.py", "exec")
        probe = r'''
import builtins
import ssl
import sys

source = sys.argv[1]
certificate = ssl.create_default_context().get_ca_certs(binary_form=True)[0]
original_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name.startswith("pip._vendor.truststore"):
        raise ImportError("fixture: vendored truststore unavailable")
    return original_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
ssl.enum_certificates = lambda name: (
    ((certificate, "x509_asn", True),) if name == "ROOT" else ()
)
namespace = {"__name__": "deeppattern_windows_native_trust"}
exec(compile(source, "deeppattern_windows_native_trust.py", "exec"), namespace)
context = ssl.create_default_context()
assert ssl.create_default_context.__module__ == "deeppattern_windows_native_trust"
assert context.verify_mode == ssl.CERT_REQUIRED
assert context.check_hostname
assert context.get_ca_certs()
'''
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, module_source],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_agentless_bootstrap_does_not_publish_a_placeholder_host(self) -> None:
        bootstrap = named_embedded_python(self.install, "bootstrapScript")
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "installer"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "mcp_config.py").write_text(
                "CLIENTS = ('codex',)\n"
                "CLIENT_SPECS = {}\n"
                "def detect_clients(): raise AssertionError('detection must be frozen')\n"
                "def active_skill_routes(*, for_doctor=False, setup_only=False): return {}\n",
                encoding="utf-8",
            )
            (package / "managed_activation.py").write_text(
                "from dataclasses import dataclass\n"
                "@dataclass(frozen=True)\n"
                "class Result: clients: tuple\n"
                "def activate_prepared_install(root=None, *, clients=None, startup_budget_seconds=10.0):\n"
                "    managed_install.write_managed_identity(root)\n"
                "    update_transaction._write_protocol_ready_locked(root, None)\n"
                "    if clients != ('codex',): raise AssertionError(clients)\n"
                "    return Result(tuple(clients))\n"
                "class managed_install:\n"
                "    write_managed_identity = staticmethod(lambda root: None)\n"
                "class update_transaction:\n"
                "    _write_protocol_ready_locked = staticmethod(lambda root, tx: None)\n",
                encoding="utf-8",
            )
            (package / "bootstrap_managed_install.py").write_text(
                "from . import managed_activation\n"
                "def _run_permanent_setup_from_env(*args, **kwargs):\n"
                "    raise AssertionError('redundant host setup must be suppressed')\n"
                "def main(arguments):\n"
                "    result = managed_activation.activate_prepared_install('root', clients=())\n"
                "    if result.clients != (): raise AssertionError(result.clients)\n"
                "    if _run_permanent_setup_from_env() != 0: raise AssertionError('not suppressed')\n"
                "    return 0\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", bootstrap, temporary],
                capture_output=True,
                check=False,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_activation_completion_dialog_runs_only_after_doctor(self) -> None:
        doctor = self.install.index("$doctorCode = Invoke-ScopedDecisionEngineDoctor")
        completion = self.install.index("Show-ActivationCompletionDialog", doctor)
        self.assertGreater(completion, doctor)
        self.assertIn("$activationCompletedThisRun -and -not $doctorVerifyFailed", self.install)

    def test_aqg_wrapper_uses_only_the_verified_bash(self) -> None:
        bootstrap = self.helper["aqg_wrapper_bootstrap_source"]()
        bash = "/bin/bash"
        if sys.platform == "win32":
            git = shutil.which("git")
            self.assertIsNotNone(git, "Git for Windows is required by this fixture")
            git_path = Path(git).resolve()
            bash_path = next(
                (
                    candidate
                    for parent in git_path.parents
                    for candidate in (
                        parent / "bin" / "bash.exe",
                        parent / "usr" / "bin" / "bash.exe",
                    )
                    if candidate.is_file()
                ),
                None,
            )
            self.assertIsNotNone(
                bash_path,
                f"paired Git for Windows Bash not found for {git_path}",
            )
            bash = str(bash_path)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "__init__.py").write_text("", encoding="utf-8")
            wrapper = scripts / "install_aqg_clients.py"
            wrapper.write_text(
                "import os\n"
                "def _run_command(command, env):\n"
                "    raise AssertionError('ambient Bash discovery must be bypassed')\n"
                "def main(arguments):\n"
                "    if arguments != ['--fixture']: raise AssertionError(arguments)\n"
                "    _run_command('printf COMMAND', os.environ.copy())\n"
                "    return 0\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    "-c",
                    bootstrap,
                    str(root),
                    str(wrapper),
                    "printf PREFIX; ",
                    bash,
                    "--fixture",
                ],
                capture_output=True,
                check=False,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "PREFIXCOMMAND")

    def test_historical_aqg_skill_route_is_owned_but_foreign_route_is_preserved(self) -> None:
        def inventory_for(home: Path, route_target: Path):
            target = home / ".deeppattern" / "versions" / "0.14.24"
            source = target / "skills" / "aqg-example"
            source.mkdir(parents=True, exist_ok=True)
            route = home / ".claude" / "skills" / source.name
            route.parent.mkdir(parents=True)
            route.symlink_to(route_target, target_is_directory=True)
            inventory = self.helper["Inventory"](home, "aqg")
            inventory.aqg_target = target
            inventory.aqg_uninstaller = target / "scripts" / "install_aqg_clients.py"
            inventory.inspect_aqg_skill_routes()
            return inventory, route

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            historical = (
                home / ".deeppattern" / "versions" / "0.14.23" / "skills" / "aqg-example"
            )
            inventory, route = inventory_for(home, historical)
            self.assertEqual(inventory.blockers, [])
            self.assertTrue(
                any(
                    action.path == route
                    and action.detail == "owned historical AQG skill junction"
                    for action in inventory.actions
                )
            )

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            inventory, route = inventory_for(
                home, home / "foreign" / "skills" / "aqg-example"
            )
            self.assertEqual(inventory.blockers, [])
            self.assertEqual(inventory.actions, [])
            self.assertEqual(
                inventory.notes, [f"PRESERVE foreign AQG skill junction {route}"]
            )

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
