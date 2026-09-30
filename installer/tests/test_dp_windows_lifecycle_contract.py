"""Behavior locks for the Windows install and uninstall lifecycle entrypoints."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def powershell_ast_function_loader(names: tuple[str, ...]) -> str:
    quoted_names = ", ".join(f'"{name}"' for name in names)
    return f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:FIXTURE_FUNCTION_SOURCE, [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) {{ throw ($errors | Out-String) }}
foreach ($name in @({quoted_names})) {{
    $nodes = @($ast.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }}, $true))
    if ($nodes.Count -ne 1) {{ throw "Expected exactly one function: $name" }}
    Invoke-Expression $nodes[0].Extent.Text
}}
""".strip()


class WindowsLifecycleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.install = INSTALL.read_text(encoding="utf-8")
        cls.uninstall = UNINSTALL.read_text(encoding="utf-8")
        cls.helper_source = embedded_variable(cls.uninstall, "helperSource")
        cls.helper: dict[str, object] = {"__name__": __name__}
        exec(compile(cls.helper_source, str(UNINSTALL), "exec"), cls.helper)

    def test_agent_terminal_modes_preserve_the_no_argument_install_path(self) -> None:
        parameter_block = self.install[: self.install.index("Set-StrictMode")]
        for expected in (
            "[switch]$AgentTerminal",
            "[switch]$AgentActivate",
            "[switch]$Activate",
            "[Parameter(DontShow = $true)][switch]$ActivationOnly",
            "[Parameter(DontShow = $true)][switch]$AgentTerminalChild",
        ):
            self.assertIn(expected, parameter_block)

        handoff = powershell_function(self.install, "Invoke-AgentTerminalHandoff")
        for expected in (
            "[IO.Path]::GetTempPath()",
            "[Guid]::NewGuid()",
            "FileAttributes]::ReparsePoint",
            "Copy-Item -LiteralPath",
            "Get-FileHash -LiteralPath",
            "Microsoft.PowerShell.Utility\\Get-FileHash",
            'Join-Path $PSHOME "powershell.exe"',
            '"-AgentTerminalChild"',
            '"-ActivationOnly"',
            "-WindowStyle Normal",
            "-Wait",
            "-PassThru",
            "Remove-Item -LiteralPath $handoffRoot -Recurse -Force",
            "exit $childStatus",
        ):
            self.assertIn(expected, handoff)
        self.assertEqual(handoff.count("Start-Process"), 1)
        self.assertIn("$sourceHash -ne $stagedHash", handoff)

        mode_validation = self.install.index("$entryModeCount = 0")
        platform_validation = self.install.index("if (-not (Test-NativeWindows))")
        handoff_gate = self.install.index("if ($AgentTerminal -or $AgentActivate)")
        activation_gate = self.install.index("if ($Activate -or $ActivationOnly)")
        normal_install = self.install.index("$GitPath = Resolve-Git", activation_gate)
        self.assertLess(mode_validation, platform_validation)
        self.assertLess(platform_validation, handoff_gate)
        self.assertLess(handoff_gate, activation_gate)
        self.assertLess(activation_gate, normal_install)

    def test_activation_only_is_fail_closed_and_preserves_host_configuration(self) -> None:
        activation = powershell_function(self.install, "Invoke-ManagedActivationOnly")
        for expected in (
            "Test-CompleteManagedRoot",
            "Test-ManagedActivationRecoveryPending",
            "Get-ManagedActivationState",
            "Invoke-ManagedPermanentSetup",
            "Show-ActivationCompletionDialog",
            "existing Agent configuration was preserved",
        ):
            self.assertIn(expected, activation)
        for forbidden in (
            "Resolve-Git",
            "Resolve-Python",
            "Resolve-Aqg",
            "Select-InstallClients",
            "Invoke-AqgClientWrapper",
        ):
            self.assertNotIn(forbidden, activation)

        activation_gate = self.install.index("if ($Activate -or $ActivationOnly)")
        normal_install = self.install.index("$GitPath = Resolve-Git", activation_gate)
        entry = self.install[activation_gate:normal_install]
        self.assertIn("$GitPath = Find-Git", entry)
        self.assertIn("Test-ManagedPythonEnvironment", entry)
        self.assertIn("Install-WindowsNativeTrustBridge", entry)
        self.assertIn("Invoke-ManagedActivationOnly", entry)
        self.assertNotIn("Resolve-Python", entry)

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
        self.assertIn('"configure_hosts"', setup_script)
        self.assertIn('"--activation-only"', setup_script)
        self.assertIn("_configure_agent_hosts", setup_script)
        self.assertIn("_doctor_has_blocking_failure", setup_script)

    def test_activation_wrapper_suppresses_post_activation_error_dialog(self) -> None:
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

def _managed_config_path():
    return "config.json"

def _load_managed_config(_path):
    return {"activated": True}

class activate:
    @staticmethod
    def is_permanently_activated(config):
        return bool(config.get("activated"))

def main(argv):
    _show_gui_message("post-activation", "repair failed", error=True)
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
        self.assertEqual(result.stdout.strip(), "")

    def test_activation_wrapper_uses_activation_only_when_supported(self) -> None:
        setup_script = embedded_variable(self.install, "setupScript")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            package = root / "installer"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "permanent_setup.py").write_text(
                """
def _show_gui_message(title, message, *, error=False):
    raise AssertionError("status dialog should be suppressed")

def run_permanent_setup(*, configure_hosts=True):
    raise AssertionError("main fixture owns this probe")

def main(argv):
    print("arguments=" + ",".join(argv))
    return 0
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

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "arguments=--activation-only")

    def test_activation_wrapper_short_circuits_legacy_host_repair(self) -> None:
        setup_script = embedded_variable(self.install, "setupScript")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            package = root / "installer"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "permanent_setup.py").write_text(
                """
def _show_gui_message(title, message, *, error=False):
    raise AssertionError("status dialog should be suppressed")

def run_permanent_setup():
    raise AssertionError("signature probe only")

def _configure_agent_hosts():
    raise AssertionError("legacy host repair was not replaced")

def _doctor_has_blocking_failure(_failures):
    raise AssertionError("legacy Doctor gate was not replaced")

def main(argv):
    result = _configure_agent_hosts()
    if _doctor_has_blocking_failure(result.failures):
        return 23
    print(f"legacy={len(result.failures)}:{len(result.notices)}")
    return 0
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

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "legacy=0:0")

    def test_configured_codex_forces_final_hook_reconciliation(self) -> None:
        repair = powershell_function(self.install, "Repair-AqgCodexHookEntrance")
        self.assertIn("[switch]$Required", repair)
        self.assertIn('len(sys.argv) > 4 and sys.argv[4] == "1"', repair)
        self.assertIn('arguments = ["--target", str(target)', repair)
        self.assertIn('features.get("hooks", features.get("codex_hooks", True))', repair)
        self.assertIn("definitions across", repair)
        self.assertIn(
            'if not required and "codex" not in '
            "install_aqg_clients.installed_supported_clients()",
            repair,
        )

        configured = self.install.index('$configuredClients.Add($client)')
        final_repair = self.install.index(
            'Repair-AqgCodexHookEntrance -Required', configured
        )
        wiring_gate = self.install.index('if ($wiringFailed)', configured)
        doctor = self.install.index(
            '$doctorCode = Invoke-ScopedDecisionEngineDoctor', configured
        )
        self.assertLess(wiring_gate, final_repair)
        self.assertLess(final_repair, doctor)
        final_block = self.install[configured:doctor]
        self.assertIn('$configuredClients.Contains("codex")', final_block)
        self.assertIn(
            "AQG Hooks=installed-on-disk",
            self.install,
        )
        self.assertIn("pending-user-review", self.install)
        self.assertNotIn("AQG Hooks=verified-on-disk", self.install)

    def test_required_hook_reconciliation_bypasses_early_agent_detector(self) -> None:
        hook_script = embedded_variable(self.install, "aqgHookScript")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "__init__.py").write_text("", encoding="utf-8")
            (scripts / "install_aqg_clients.py").write_text(
                "def installed_supported_clients():\n    return []\n",
                encoding="utf-8",
            )
            (scripts / "install_aqg_codex_hooks.py").write_text(
                """
from pathlib import Path
import json
import os

def _owned_script(hook):
    return hook.get("script") if isinstance(hook, dict) else None

def main(args):
    log = Path(os.environ["HOOK_CALL_LOG"])
    prior = log.read_text(encoding="utf-8") if log.exists() else ""
    log.write_text(prior + args[0] + "\\n", encoding="utf-8")
    target = Path(args[args.index("--target") + 1])
    if args[0] == "--verify" and not prior:
        return 1
    if args[0] == "--apply":
        target.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"script": "owned"}]}]}}))
    return 0
""".lstrip(),
                encoding="utf-8",
            )
            log = root / "calls.log"
            target = root / "codex" / "hooks.json"
            target.parent.mkdir()
            config = target.parent / "config.toml"
            config.write_text("[features]\nhooks = true\n", encoding="utf-8")
            environment = {**os.environ, "HOOK_CALL_LOG": str(log)}
            required = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    hook_script,
                    str(root),
                    str(target),
                    str(config),
                    "1",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=15,
                env=environment,
            )
            self.assertEqual(
                required.returncode, 0, required.stdout + required.stderr
            )
            calls = log.read_text(encoding="utf-8").splitlines()
            log.unlink()
            optional = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    hook_script,
                    str(root),
                    str(target),
                    str(config),
                    "0",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=15,
                env=environment,
            )
            config.write_text("[features]\nhooks = false\n", encoding="utf-8")
            disabled = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    hook_script,
                    str(root),
                    str(target),
                    str(config),
                    "1",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=15,
                env=environment,
            )

        self.assertEqual(calls, ["--verify", "--apply", "--verify"])
        self.assertEqual(optional.returncode, 0, optional.stdout + optional.stderr)
        self.assertFalse(log.exists())
        self.assertIn("1 definitions across 1 events", required.stdout)
        self.assertEqual(disabled.returncode, 5, disabled.stdout + disabled.stderr)
        self.assertIn("installed but disabled", disabled.stderr)

    def test_codex_hooks_share_the_active_codex_config_directory(self) -> None:
        resolver = powershell_function(self.install, "Initialize-CodexHookTarget")
        wrapper = powershell_function(self.install, "Invoke-AqgClientWrapper")
        self.assertIn('mcp_config.agent_config_path("codex")', resolver)
        self.assertIn('config_path.parent / "hooks.json"', resolver)
        self.assertIn("CODEX_CONFIG must be an absolute path", resolver)
        self.assertIn("Codex configuration and Hooks targets do not share", self.install)
        self.assertIn('$wrapperEnvironment["CODEX_HOME"] = $script:CodexHome', wrapper)
        self.assertIn('"CODEX_HOME" = $script:CodexHome', self.install)

    def test_windows_desktop_only_codex_does_not_claim_hook_activation(self) -> None:
        capability = powershell_function(
            self.install, "Initialize-CodexHookReviewCapability"
        )
        self.assertIn(
            "Get-Command codex -CommandType Application", capability
        )
        self.assertIn("$script:CodexHookReviewUnavailable = $true", capability)
        self.assertIn(
            '"review-unavailable-no-codex-cli"', self.install
        )
        self.assertIn(
            "this Windows host has Codex Desktop only and no codex CLI on PATH",
            self.install,
        )
        self.assertIn(
            "$script:CodexHookReviewUnavailable", self.install
        )

    def test_windows_installer_normalizes_codex_toml_before_stable_mcp_write(self) -> None:
        repair = powershell_function(self.install, "Repair-CodexTomlNewlines")
        for expected in (
            'New-Object System.Text.UTF8Encoding($false, $true)',
            '$text.Replace("`r`n", "`n").Replace("`r", "`n")',
            "tomllib.loads",
            "Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256",
            "[IO.File]::Replace",
            ".de-newline-bak.",
        ):
            self.assertIn(expected, repair)

        initialize = self.install.index(
            "Initialize-CodexHookTarget -SourceRoot $sourceRoot"
        )
        repair_call = self.install.index(
            "Repair-CodexTomlNewlines -SourceRoot $sourceRoot", initialize
        )
        wiring = self.install.index("foreach ($client in $clients)", repair_call)
        self.assertLess(initialize, repair_call)
        self.assertLess(repair_call, wiring)

    def test_disabled_codex_hooks_are_reported_as_partial(self) -> None:
        repair = powershell_function(self.install, "Repair-AqgCodexHookEntrance")
        self.assertIn("$script:CodexHooksDisabled = $true", repair)
        self.assertIn("disabled-by-config", self.install)
        self.assertIn("Enable [features] hooks = true", self.install)
        partial_gate = self.install[self.install.index("if ($script:CodexHooksDisabled)") :]
        self.assertIn("-or $script:CodexHooksDisabled", partial_gate)

    def test_active_session_defers_update_without_process_prompt(self) -> None:
        deferred = self.install.index(
            '$managedUpdate.Status -eq "deferred_active_session"'
        )
        activation = self.install.index(
            'Write-Host "Opening the masked Decision Engine activation window..."',
            deferred,
        )
        self.assertLess(deferred, activation)
        continuation = self.install[deferred:activation]
        self.assertIn("setup will continue without stopping Agent applications", continuation)
        self.assertNotIn("Confirm-UserAction", continuation)
        self.assertNotIn("Stop-Process", continuation)
        self.assertIn("SUCCESS_WITH_RESTART_REQUIRED", self.install)

    def test_install_requires_a_full_agent_restart_before_runtime_verification(self) -> None:
        self.assertIn(
            "Do not verify MCP tools or Hooks in an Agent process that remained open",
            self.install,
        )
        self.assertIn("Starting only a new chat is not sufficient", self.install)
        self.assertIn(
            "After reopening the Codex CLI at {0}, open /hooks and review the AQG definitions loaded from",
            self.install,
        )
        self.assertIn("if ($script:CodexHookReviewAvailable)", self.install)
        self.assertIn(
            "The current Desktop surface does not expose the documented /hooks trust command",
            self.install,
        )
        self.assertIn("Trust is a Codex user action and cannot be granted", self.install)

    def test_agent_targets_are_selected_before_any_host_mutation(self) -> None:
        snapshot = powershell_function(self.install, "Get-InstalledAgentSnapshot")
        self.assertIn("mcp_config.detect_clients()", snapshot)
        self.assertIn("claude_code_has_independent_evidence", snapshot)
        self.assertIn('other_servers.pop("decision-engine", None)', snapshot)
        self.assertIn('shutil.which("claude")', snapshot)
        self.assertIn("ConvertFrom-Json", snapshot)
        self.assertIn("$ValidClients", snapshot)

        captured = self.install.index(
            "$installedClients = @(Get-InstalledAgentSnapshot"
        )
        routing = self.install[captured:]
        self.assertIn("$selection = Select-InstallClients", routing)
        self.assertIn("$selectedClients = @($selection.Effective)", routing)
        self.assertIn("$clients = @($selectedClients)", routing)
        self.assertIn(
            "$configuredClientsBefore = @(Get-ConfiguredAgentSnapshot",
            routing,
        )
        self.assertIn(
            "Agent preflight found no supported installed host",
            routing,
        )
        selection = self.install.index("$selection = Select-InstallClients", captured)
        first_host_mutation = self.install.index("\n    Repair-AqgBackupResidue", selection)
        self.assertLess(selection, first_host_mutation)

    def test_unscoped_aqg_doctor_cannot_fail_clients_outside_frozen_targets(self) -> None:
        verify = self.install.index("$aqgVerifyCode = Invoke-AqgClientWrapper")
        doctor = self.install.index("$aqgGlobalDoctorClients", verify)
        install_de = self.install.index(
            'Write-Host "Installing the signed Decision Engine stable release..."',
            doctor,
        )
        block = self.install[doctor:install_de]

        self.assertIn('$aqgGlobalDoctorClients = @("claude-code", "codex")', block)
        self.assertIn("Where-Object { $aqgClients -notcontains $_ }", block)
        self.assertIn("if ($aqgGlobalDoctorApplicable)", block)
        self.assertIn('-ArgumentList @($aqgDoctor, "--no-cli")', block)
        self.assertIn("AQG target-specific verification passed for:", block)
        self.assertIn("clients outside this frozen install target", block)

        captured = self.install.index(
            "$installedClients = @(Get-InstalledAgentSnapshot"
        )
        first_host_mutation = self.install.index("\n    Repair-AqgBackupResidue", captured)
        self.assertLess(captured, first_host_mutation)

        routing = self.install[captured:]
        self.assertNotIn('"--installed-supported"', routing)
        self.assertNotIn("installing the default Codex routes", routing)

    def test_bootstrap_and_doctor_use_only_the_frozen_agent_snapshot(self) -> None:
        bootstrap_start = self.install.index(
            "function Invoke-ManagedBootstrapInstall"
        )
        doctor_start = self.install.index(
            "function Invoke-ScopedDecisionEngineDoctor", bootstrap_start
        )
        permanent_setup_start = self.install.index(
            "function Invoke-ManagedPermanentSetup", doctor_start
        )
        bootstrap = self.install[bootstrap_start:doctor_start]
        doctor = self.install[doctor_start:permanent_setup_start]
        for block in (bootstrap, doctor):
            self.assertIn("allowed_routes", block)
            self.assertIn("scoped_active_skill_routes", block)
            self.assertIn("if name in allowed_routes", block)
            self.assertIn("mcp_config.CLIENT_SPECS", block)
        self.assertIn('arguments.extend(("--client", client))', bootstrap)
        self.assertIn("mcp_config.detect_clients = lambda: tuple(clients)", doctor)
        self.assertIn(
            "Invoke-ManagedBootstrapInstall `", self.install
        )
        self.assertIn(
            "Invoke-ScopedDecisionEngineDoctor `", self.install
        )

    def test_only_configured_clients_receive_managed_skill_routes(self) -> None:
        route_start = self.install.index("$routeScript = @'")
        route_end = self.install.index("\n'@", route_start)
        route_script = self.install[route_start:route_end]
        self.assertIn("clients = sys.argv[2:]", route_script)
        self.assertIn("for client in clients:", route_script)
        self.assertIn("install._route_skills(", route_script)
        self.assertNotIn("desired_destinations", route_script)
        self.assertNotIn("visited_destinations", route_script)
        self.assertNotIn("install._remove_skill_route(route)", route_script)
        self.assertIn(
            "@([string[]]$configuredClients.ToArray())",
            self.install[route_end : route_end + 1600],
        )

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 selection behavior")
    def test_native_agent_selection_parser_and_retention(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        loader = powershell_ast_function_loader(
            (
                "Get-AgentDisplayName",
                "Merge-AgentClientsByCatalog",
                "Test-SharedSkillPairSelection",
                "Select-InstallClients",
            )
        )
        harness = f"""
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ExitPartial = 4
{loader}

function Read-Host {{
    param([string]$Prompt)
    if ($script:SelectionAnswers.Count -eq 0) {{ throw "fixture input exhausted" }}
    return [string]$script:SelectionAnswers.Dequeue()
}}

function Invoke-SelectionCase {{
    param(
        [string]$Name,
        [string[]]$Available,
        [string[]]$Configured,
        [string[]]$Answers
    )
    $script:SelectionAnswers = New-Object System.Collections.Queue
    foreach ($answer in $Answers) {{ $script:SelectionAnswers.Enqueue($answer) }}
    $result = Select-InstallClients -Available $Available -Configured $Configured
    Write-Output ("CASE:" + $Name + ":" + ($result | ConvertTo-Json -Compress))
}}

$catalog = @("claude-code", "codex", "cursor")
Invoke-SelectionCase "default" $catalog @() @((""))
Invoke-SelectionCase "all" $catalog @() @("ALL")
Invoke-SelectionCase "separators" $catalog @() @("3,,   1,3")
Invoke-SelectionCase "retained" $catalog @("codex") @("3")
Invoke-SelectionCase "pair" @("qoder", "qoder-ide", "codex") @("codex") @("1", "1,2")
"""
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "selection.ps1"
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
                env={**os.environ, "FIXTURE_FUNCTION_SOURCE": str(INSTALL)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cases = {}
        for line in result.stdout.splitlines():
            if line.startswith("CASE:"):
                _, name, payload = line.split(":", 2)
                cases[name] = json.loads(payload)
        self.assertEqual(set(cases), {"default", "all", "separators", "retained", "pair"})
        for name in ("default", "all"):
            self.assertEqual(cases[name]["Requested"], ["claude-code", "codex", "cursor"])
            self.assertEqual(cases[name]["Effective"], ["claude-code", "codex", "cursor"])
        self.assertEqual(cases["separators"]["Requested"], ["claude-code", "cursor"])
        self.assertEqual(cases["retained"]["Effective"], ["codex", "cursor"])
        self.assertEqual(cases["retained"]["New"], ["cursor"])
        self.assertEqual(cases["pair"]["Requested"], ["qoder", "qoder-ide"])
        self.assertEqual(cases["pair"]["Effective"], ["qoder", "qoder-ide", "codex"])

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 handoff behavior")
    def test_native_agent_terminal_handoff_stages_once_and_propagates_status(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        handoff_function = powershell_function(self.install, "Invoke-AgentTerminalHandoff")
        harness = f"""
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ExitBlocked = 3
$ProgramName = "dp-install"
. $env:FIXTURE_FUNCTION_SOURCE

function Stop-Install {{
    param([string]$Message, [int]$Code = 1)
    Write-Output ("dp-install: ERROR: " + $Message)
    exit $Code
}}

function Start-Process {{
    [CmdletBinding()]
    param(
        [string]$FilePath,
        [string]$ArgumentList,
        [string]$WindowStyle,
        [switch]$Wait,
        [switch]$PassThru
    )
    [ordered]@{{
        FilePath = $FilePath
        ArgumentList = $ArgumentList
        WindowStyle = $WindowStyle
        Wait = [bool]$Wait
        PassThru = [bool]$PassThru
    }} | ConvertTo-Json -Compress | Set-Content -LiteralPath $env:FIXTURE_CALL_LOG -Encoding UTF8
    return [pscustomobject]@{{ ExitCode = [int]$env:FIXTURE_CHILD_EXIT }}
}}

function Get-FileHash {{
    throw "unqualified Get-FileHash resolution was used"
}}

Invoke-AgentTerminalHandoff -Activation:([bool][int]$env:FIXTURE_ACTIVATION)
"""
        cases = ((False, 0, "-AgentTerminalChild"), (True, 4, "-ActivationOnly"))
        for activation, child_exit, child_mode in cases:
            with self.subTest(activation=activation, child_exit=child_exit):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    script = root / "handoff.ps1"
                    function_source = root / "handoff-function.ps1"
                    call_log = root / "call.json"
                    script.write_text(harness, encoding="utf-8-sig")
                    function_source.write_text(handoff_function, encoding="utf-8-sig")
                    environment = {
                        **os.environ,
                        "FIXTURE_FUNCTION_SOURCE": str(function_source),
                        "FIXTURE_CALL_LOG": str(call_log),
                        "FIXTURE_CHILD_EXIT": str(child_exit),
                        "FIXTURE_ACTIVATION": "1" if activation else "0",
                        "TEMP": str(root),
                        "TMP": str(root),
                    }
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
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=20,
                        env=environment,
                    )
                    self.assertEqual(result.returncode, child_exit, result.stdout + result.stderr)
                    self.assertTrue(call_log.is_file(), result.stdout + result.stderr)
                    payload = json.loads(call_log.read_text(encoding="utf-8-sig"))
                    leftovers = tuple(root.glob("dp-install-agent-terminal-*"))

                self.assertEqual(Path(payload["FilePath"]).name.lower(), "powershell.exe")
                self.assertEqual(payload["WindowStyle"], "Normal")
                self.assertTrue(payload["Wait"])
                self.assertTrue(payload["PassThru"])
                self.assertIn(child_mode, payload["ArgumentList"])
                self.assertEqual(leftovers, ())

    def test_restart_warning_is_only_emitted_when_host_state_changed(self) -> None:
        self.assertIn("$runtimeRestartRequired = $false", self.install)
        self.assertIn(
            '$preflight.action -ne "unchanged"',
            self.install,
        )
        warning = self.install.index(
            'Write-Warning "Do not verify MCP tools or Hooks in an Agent process'
        )
        gate = self.install.rfind("if ($runtimeRestartRequired)", 0, warning)
        self.assertGreater(gate, 0)
        self.assertLess(gate, warning)

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

    def test_python_entrypoints_force_utf8_even_in_isolated_mode(self) -> None:
        generated = powershell_function(self.install, "Invoke-PythonScript")
        managed_install = powershell_function(self.install, "Invoke-ManagedPython")
        managed_uninstall = powershell_function(self.uninstall, "Invoke-ManagedPython")

        self.assertIn('@("-I", "-X", "utf8", $scriptPath)', generated)
        for block in (managed_install, managed_uninstall):
            self.assertIn('@("-X", "utf8") + @($Arguments)', block)
        self.assertIn('"-I", "-X", "utf8", $helperPath', self.uninstall)

    def test_legacy_elevated_owner_is_accepted_without_broad_write_acl(self) -> None:
        assertion = powershell_function(self.install, "Assert-PrivateDirectory")
        self.assertIn('"S-1-5-18"', assertion)
        self.assertIn('"S-1-5-32-544"', assertion)
        self.assertIn('"S-1-1-0", "S-1-5-11", "S-1-5-32-545"', assertion)
        self.assertIn("grants broad write access", assertion)
        self.assertIn("trusted legacy Windows installer owner", assertion)

    def test_legacy_uninstall_bootstraps_a_fixed_temporary_runtime(self) -> None:
        bootstrap = powershell_function(
            self.uninstall, "New-DownloadedPrivateUninstallBootstrap"
        )
        resolve = powershell_function(self.uninstall, "Resolve-Python")
        for expected in (
            '$PrivatePythonVersion = "3.13.15"',
            '$PrivatePythonBuild = "20260901"',
            "9bcc038a0bf180612ed56dec93d4977d0"
            "35e80b8d9320ef51a38c287baf134b7",
            "ce87247378f43f88e0202a0fa6d3cdb5"
            "f5fb246a3bc61b2fb604bd49b7862508",
            '"--proto", "=https", "--tlsv1.2"',
            '"--progress-bar"',
            "Get-FileHash -LiteralPath $archive -Algorithm SHA256",
            "temporary Python archive has an unexpected path layout",
            "FileAttributes]::ReparsePoint",
        ):
            self.assertIn(expected, self.uninstall)
        self.assertIn("return New-DownloadedPrivateUninstallBootstrap", resolve)
        self.assertIn("does not change system Python", bootstrap)
        self.assertNotIn('"--silent"', bootstrap)

    def test_empty_uninstall_preflight_runs_before_python_resolution(self) -> None:
        preflight = powershell_function(
            self.uninstall, "Test-FastEmptyUninstallState"
        )
        config_probe = powershell_function(
            self.uninstall, "Test-FastHostConfigReference"
        )
        for expected in (
            '"decision-engine-root"',
            '"uninstall-backups"',
            '"popup-sessions"',
            '"decision-engine"',
            '"agent-quality-gates"',
            '".claude.json"',
            '".codex\\config.toml"',
            '".claude\\settings.json"',
            '".cursor\\mcp.json"',
            'FileAttributes]::ReparsePoint',
            'Get-Content -LiteralPath $configPath -Raw -ErrorAction Stop',
        ):
            self.assertIn(expected, preflight)
        self.assertIn(
            '$retainedNames = @("decision-engine-root", "uninstall-backups")',
            preflight,
        )
        self.assertIn("Test-FastHostConfigReference", preflight)
        self.assertIn("Test-FastOwnedSkillJunction", preflight)
        self.assertIn("ConvertFrom-Json", config_probe)
        self.assertIn('Properties["mcpServers"]', config_probe)
        self.assertIn('Properties["hooks"]', config_probe)
        self.assertIn('".deeppattern\\\\agent-quality-gates"', config_probe)
        self.assertNotIn('"aqg-"', config_probe)
        self.assertNotIn('"run_aqg_codex_hook.py"', config_probe)

        platform_gate = self.uninstall.index(
            "[System.Environment]::OSVersion.Platform"
        )
        preflight_call = self.uninstall.index(
            "$fastEmptyUninstall = Test-FastEmptyUninstallState", platform_gate
        )
        resolve = self.uninstall.index("$PythonPath = Resolve-Python", platform_gate)
        self.assertLess(preflight_call, resolve)
        fast_path = self.uninstall[preflight_call:resolve]
        self.assertIn(
            "PASS: uninstall verified; no in-scope installation found", fast_path
        )
        self.assertIn("exit 0", fast_path)
        self.assertNotIn("Resolve-Python", fast_path)
        self.assertNotIn("New-DownloadedPrivateUninstallBootstrap", fast_path)
        self.assertIn("Full uninstall verification required:", fast_path)
        self.assertIn("$script:FastEmptyUninstallReason", fast_path)
        self.assertIn("$script:FastEmptyUninstallActiveProcess", fast_path)
        self.assertIn("Resolve-ActiveManagedSessions", fast_path)
        self.assertIn("Test-FastEmptyUninstallState", fast_path)
        self.assertIn("affected Agent recreated", fast_path)
        self.assertIn("Remove-FastOwnedSkillJunctions", fast_path)
        self.assertIn("without downloading Python", fast_path)

    def test_process_probe_can_run_before_python_resolution(self) -> None:
        lease_probe = powershell_function(self.uninstall, "Get-LiveManagedLeasePids")
        self.assertIn("Get-Variable -Name PythonPath", lease_probe)
        self.assertIn("return @()", lease_probe)

    @unittest.skipUnless(
        os.name == "nt", "Windows PowerShell 5.1 empty-state preflight behavior"
    )
    def test_native_empty_uninstall_preflight_is_conservative(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        functions = "\n\n".join(
            powershell_function(self.uninstall, name)
            for name in (
                "Test-ReparsePoint",
                "Require-FullUninstallVerification",
                "Test-FastHostConfigReference",
                "Test-FastOwnedSkillJunction",
                "Test-FastEmptyUninstallState",
            )
        )
        harness = f"""
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
{functions}

$script:HomePath = $env:FAST_EMPTY_HOME
$script:Scope = "both"
function Get-CimInstance {{ @() }}
$dp = Join-Path $script:HomePath ".deeppattern"
New-Item -ItemType Directory -Path (Join-Path $dp "uninstall-backups") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $dp "decision-engine-root") -Force | Out-Null
$claude = Join-Path $script:HomePath ".claude"
New-Item -ItemType Directory -Path $claude -Force | Out-Null
$settings = Join-Path $claude "settings.json"
[IO.File]::WriteAllText($settings, '{{"note":"ordinary aqg-user preference"}}')
if (-not (Test-FastEmptyUninstallState)) {{ throw "retained roots were not fast-empty" }}

[IO.File]::WriteAllText($settings, '{{"hooks":{{"Stop":[{{"hooks":[{{"command":"C:\\\\Users\\\\test\\\\.deeppattern\\\\agent-quality-gates\\\\scripts\\\\run_aqg_codex_hook.py"}}]}}]}}}}')
if (Test-FastEmptyUninstallState) {{ throw "managed AQG hook was ignored" }}
[IO.File]::WriteAllText($settings, '{{"note":"ordinary aqg-user preference"}}')

New-Item -ItemType Directory -Path (Join-Path $dp "decision-engine") -Force | Out-Null
if (Test-FastEmptyUninstallState) {{ throw "managed DE root was ignored" }}
Remove-Item -LiteralPath (Join-Path $dp "decision-engine") -Recurse -Force

$codex = Join-Path $script:HomePath ".codex"
New-Item -ItemType Directory -Path $codex -Force | Out-Null
$config = Join-Path $codex "config.toml"
[IO.File]::WriteAllLines($config, @('[mcp_servers.decision-engine]', 'command = "python"'))
if (Test-FastEmptyUninstallState) {{ throw "managed config reference was ignored" }}
Remove-Item -LiteralPath $config -Force

$skills = Join-Path $codex "skills"
New-Item -ItemType Directory -Path $skills -Force | Out-Null
$target = Join-Path $dp "decision-engine-root\\skills\\audit"
New-Item -ItemType Directory -Path $target -Force | Out-Null
$route = Join-Path $skills "audit"
New-Item -ItemType Junction -Path $route -Target $target | Out-Null
if (Test-FastEmptyUninstallState) {{ throw "managed skill junction was ignored" }}
Write-Host "PASS"
"""
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "fast-empty.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            home = Path(temporary) / "home"
            environment = {
                (key.upper() if os.name == "nt" else key): value
                for key, value in os.environ.items()
            }
            environment.update(
                FAST_EMPTY_HOME=str(home),
                HOME=str(home),
                USERPROFILE=str(home),
                APPDATA=str(home / "AppData" / "Roaming"),
                LOCALAPPDATA=str(home / "AppData" / "Local"),
            )
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
                env=environment,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_fast_orphan_junction_cleanup_precedes_python_download(self) -> None:
        platform_gate = self.uninstall.index(
            "[System.Environment]::OSVersion.Platform"
        )
        resolve = self.uninstall.index("$PythonPath = Resolve-Python", platform_gate)
        fast_path = self.uninstall[platform_gate:resolve]
        cleanup = powershell_function(
            self.uninstall, "Remove-FastOwnedSkillJunctions"
        )

        self.assertIn("$script:FastOwnedSkillJunctions.Count -gt 0", fast_path)
        self.assertIn("Remove-FastOwnedSkillJunctions", fast_path)
        self.assertIn("[IO.Directory]" + "::" + "Delete", cleanup)
        self.assertIn("Test-FastOwnedSkillJunction", cleanup)
        self.assertNotIn("Remove-Item", cleanup)

    def test_dirty_legacy_official_de_checkout_is_quarantined_without_import(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("Git is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            root = home / ".deeppattern" / "decision-engine"
            for path in (root / "installer", root / "skills"):
                path.mkdir(parents=True, exist_ok=True)
            for name in ("pyproject.toml", "VERSION"):
                (root / name).write_text("fixture\n", encoding="utf-8")
            subprocess.run([git, "-C", str(root), "init", "-q"], check=True)
            subprocess.run(
                [
                    git, "-C", str(root), "remote", "add", "origin",
                    "https://github.com/deeppatternai/decision-engine.git",
                ],
                check=True,
            )
            (root / "local.patch").write_text("preserve\n", encoding="utf-8")
            self.helper["GIT_EXE"] = git
            inventory = self.helper["Inventory"](home, "de")
            inventory.inspect_root(root, "de")

        self.assertEqual(inventory.blockers, [])
        self.assertFalse(inventory.de_root_proven)
        self.assertTrue(
            any(
                action.kind == "quarantine-root" and action.path == root
                for action in inventory.actions
            )
        )
        self.assertTrue(
            any("modified official DE checkout" in note for note in inventory.notes)
        )

    def test_foreign_de_checkout_still_blocks_uninstall(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("Git is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            root = home / ".deeppattern" / "decision-engine"
            for path in (root / "installer", root / "skills"):
                path.mkdir(parents=True, exist_ok=True)
            for name in ("pyproject.toml", "VERSION"):
                (root / name).write_text("fixture\n", encoding="utf-8")
            subprocess.run([git, "-C", str(root), "init", "-q"], check=True)
            subprocess.run(
                [git, "-C", str(root), "remote", "add", "origin", "https://example.test/foreign.git"],
                check=True,
            )
            self.helper["GIT_EXE"] = git
            inventory = self.helper["Inventory"](home, "de")
            inventory.inspect_root(root, "de")

        self.assertTrue(
            any("source identity is unknown" in blocker for blocker in inventory.blockers)
        )
        self.assertEqual(inventory.actions, [])

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
            popup_action = next(
                action
                for action in inventory.actions
                if action.path == dp / "popup-sessions"
            )
            self.assertEqual(popup_action.kind, "remove-popup-sessions")
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

    def test_remove_tree_retries_transient_windows_directory_not_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "retention"
            root.mkdir()
            (root / "payload.txt").write_text("fixture", encoding="utf-8")
            real_rmtree = shutil.rmtree
            calls = 0

            def flaky_rmtree(path, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    error = OSError("directory is not empty")
                    error.winerror = 145
                    raise error
                return real_rmtree(path, *args, **kwargs)

            original = self.helper["shutil"].rmtree
            self.helper["shutil"].rmtree = flaky_rmtree
            try:
                self.helper["remove_tree"](root)
            finally:
                self.helper["shutil"].rmtree = original

        self.assertEqual(calls, 2)
        self.assertFalse(root.exists())

    def test_old_backup_filesystem_contention_does_not_invalidate_uninstall(self) -> None:
        error = OSError("directory is not empty")
        error.winerror = 145
        original = self.helper["prune_uninstall_backups"]
        self.helper["prune_uninstall_backups"] = mock.Mock(side_effect=error)
        try:
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                removed = self.helper["prune_uninstall_backups_best_effort"](
                    Path("synthetic-home"), Path("synthetic-dp")
                )
        finally:
            self.helper["prune_uninstall_backups"] = original

        self.assertEqual(removed, 0)
        self.assertIn("winerror=145", stderr.getvalue())
        self.assertIn("verified product uninstall remains valid", stderr.getvalue())

    def test_aqg_skill_junctions_are_planned_before_managed_target_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            target = home / ".deeppattern" / "versions" / "0.14.23"
            source = target / "skills" / "aqg-example"
            source.mkdir(parents=True)
            entrance = home / ".deeppattern" / "agent-quality-gates"
            entrance.symlink_to(target, target_is_directory=True)
            destination = home / ".codex" / "skills"
            destination.mkdir(parents=True)
            physical_route = destination / source.name
            physical_route.symlink_to(source, target_is_directory=True)
            logical_source = target / "skills" / "aqg-logical"
            logical_source.mkdir()
            logical_route = destination / logical_source.name
            logical_route.symlink_to(
                entrance / "skills" / logical_source.name,
                target_is_directory=True,
            )

            inventory = self.helper["Inventory"](home, "aqg")
            inventory.aqg_target = target
            inventory.aqg_uninstaller = target / "scripts" / "install_aqg_clients.py"
            inventory.inspect_aqg_skill_routes()

        self.assertEqual(inventory.blockers, [])
        planned_routes = {
            action.path
            for action in inventory.actions
            if action.kind == "remove-skill-route"
            and action.detail == "owned AQG skill junction"
        }
        self.assertEqual(
            planned_routes,
            {physical_route, logical_route},
        )

    def test_current_uninstall_backup_is_never_pruned_by_clock_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            dp = home / ".deeppattern"
            backup_root = dp / "uninstall-backups"
            backup_root.mkdir(parents=True)
            roots = []
            for index in range(7):
                path = backup_root / f"209901{index + 1:02d}-120000-dp-uninstall-both"
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
                roots.append(path)
            current = backup_root / "20260923-120000-dp-uninstall-both"
            current.mkdir()
            (current / "manifest.json").write_text(
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

            removed = self.helper["prune_uninstall_backups"](home, dp, current)
            remaining = set(backup_root.iterdir())

        self.assertEqual(removed, 3)
        self.assertIn(current, remaining)
        self.assertEqual(len(remaining), 5)

    def test_root_quarantine_precedes_agent_integration_mutation(self) -> None:
        apply_source = self.helper_source[
            self.helper_source.index("def apply_inventory(") :
            self.helper_source.index("def verify_after_apply(")
        ]
        root_stage = apply_source.index("pre_aqg_root_actions")
        aqg_uninstall = apply_source.index("invoke_aqg_uninstaller(inv)")
        config_apply = apply_source.index("apply_config(")
        route_remove = apply_source.index("remove_skill_route(")
        self.assertLess(root_stage, aqg_uninstall)
        self.assertLess(root_stage, config_apply)
        self.assertLess(root_stage, route_remove)
        self.assertIn("prepare_tree_for_quarantine", self.helper_source)
        self.assertIn("remove_tree", self.helper_source)

    def test_popup_sessions_are_removed_instead_of_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            popup_root = home / ".deeppattern" / "popup-sessions"
            popup = popup_root / "pop_1234"
            popup.mkdir(parents=True)
            (popup / "result.json").write_text("{}\n", encoding="utf-8")
            backup = home / ".deeppattern" / "uninstall-backups" / "candidate"
            backup.mkdir(parents=True)
            action = self.helper["Action"](
                "remove-popup-sessions",
                popup_root,
                "remove transient popup session data",
            )
            manifest = self.helper["Manifest"](backup, home, "de")

            self.helper["remove_popup_sessions"](
                action,
                backup,
                manifest,
                1,
                home,
            )

            self.assertFalse(popup_root.exists())
            transient = backup / "transient"
            self.assertFalse(transient.exists() and any(transient.iterdir()))
            self.assertEqual(
                manifest.data["items"][0]["operation"],
                "remove-transient-popup-sessions",
            )

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
