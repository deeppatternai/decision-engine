#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$GitPath,
    [Parameter(Mandatory = $true)][string]$FixtureRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$saved = @{}
$poison = @{
    "GIT_DIR" = (Join-Path $FixtureRoot "decoy/.git")
    "GIT_WORK_TREE" = (Join-Path $FixtureRoot "decoy")
    "GIT_COMMON_DIR" = (Join-Path $FixtureRoot "decoy/.git")
    "GIT_CONFIG_COUNT" = "1"
    "GIT_CONFIG_KEY_0" = "remote.origin.url"
    "GIT_CONFIG_VALUE_0" = "https://example.invalid/poison"
    "GIT_CONFIG_PARAMETERS" = "'remote.origin.url=https://example.invalid/poison'"
    "GIT_CONFIG_GLOBAL" = (Join-Path $FixtureRoot "missing")
    "GIT_CONFIG_SYSTEM" = (Join-Path $FixtureRoot "missing")
    "GIT_OBJECT_DIRECTORY" = (Join-Path $FixtureRoot "decoy/.git/objects")
    "GIT_ALTERNATE_OBJECT_DIRECTORIES" = (Join-Path $FixtureRoot "decoy/.git/objects")
    "GIT_EXEC_PATH" = $FixtureRoot
    "GIT_SSH_COMMAND" = "must-not-run"
    "GIT_FUTURE_TEST_VARIABLE" = "first`nGIT_DIR=second"
    "git_lowercase_probe" = "poison"
}
function Assert-Restored {
    foreach ($key in $poison.Keys) {
        if ([Environment]::GetEnvironmentVariable($key, "Process") -ne $poison[$key]) {
            throw "Environment not restored: $key"
        }
    }
}
function Invoke-ThrowFixture { throw "fixture exception" }
try {
    foreach ($key in $poison.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $poison[$key], "Process")
    }
    foreach ($file in @("dp-install.ps1", "dp-uninstall.ps1")) {
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root $file), [ref]$tokens, [ref]$errors)
        if ($errors.Count -ne 0) { throw ($errors | Out-String) }
        $cleaner = if ($file -eq "dp-install.ps1") { "Invoke-WithCleanEnvironment" } else { "Invoke-Clean" }
        $names = @($cleaner, "Get-VerifiedAuthenticodePath")
        if ($file -eq "dp-install.ps1") { $names += "Invoke-PythonScript" }
        foreach ($name in $names) {
            $nodes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true))
            if ($nodes.Count -ne 1) { throw "Expected exactly one function $name" }
            Invoke-Expression $nodes[0].Extent.Text
        }
        $absentBefore = @("DE_ENDPOINT", "DE_ACTIVATION_SECRET", "PYTHONPATH", "PYTHONIOENCODING", "AQG_BACKUP_DIR") |
            Where-Object { -not (Test-Path -LiteralPath "Env:$_") }
        $probe = & $cleaner -FilePath $PythonPath -ArgumentList @("-I", "-c", "import os; print(sum(k.upper().startswith('GIT_') for k in os.environ))") -Capture
        if ($probe.ExitCode -ne 0 -or ($probe.Output -join "") -ne "0") { throw "$file Git environment probe failed: exit=$($probe.ExitCode); output=$($probe.Output -join ' ')" }
        $gitResult = & $cleaner -FilePath $GitPath -ArgumentList @("-C", (Join-Path $FixtureRoot "actual"), "remote", "get-url", "origin") -Capture
        if ($gitResult.ExitCode -ne 0 -or ($gitResult.Output -join "") -ne "https://example.invalid/actual") { throw "$file Git identity redirected" }
        Assert-Restored
        $failed = & $cleaner -FilePath $PythonPath -ArgumentList @("-I", "-c", "import sys; sys.exit(17)") -Capture
        if ($failed.ExitCode -ne 17) { throw "$file child exit code lost" }
        Assert-Restored
        $caught = $false
        try { & $cleaner -FilePath "Invoke-ThrowFixture" -Capture | Out-Null } catch { $caught = $true }
        if (-not $caught) { throw "$file exception fixture did not run" }
        Assert-Restored
        foreach ($key in $absentBefore) {
            if (Test-Path -LiteralPath "Env:$key") { throw "$file created an originally absent variable: $key" }
        }

        # Signature decisions are tested with controlled results, not a real Windows certificate chain.
        $script:SignatureStatus = [System.Management.Automation.SignatureStatus]::NotSigned
        $script:Signer = "CN=Fixture Publisher"
        function Get-AuthenticodeSignature {
            param([string]$LiteralPath)
            return [pscustomobject]@{ Status = $script:SignatureStatus; SignerCertificate = [pscustomobject]@{ Subject = $script:Signer } }
        }
        $argsForTool = @{ Path = $PythonPath; PublisherPattern = "Fixture Publisher" }
        if ($null -ne (Get-VerifiedAuthenticodePath @argsForTool)) { throw "$file accepted unsigned tool" }
        $script:SignatureStatus = [System.Management.Automation.SignatureStatus]::Valid
        $script:Signer = "CN=Wrong Publisher"
        if ($null -ne (Get-VerifiedAuthenticodePath @argsForTool)) { throw "$file accepted wrong publisher" }
        $script:Signer = "CN=Fixture Publisher"
        if ($null -eq (Get-VerifiedAuthenticodePath @argsForTool)) { throw "$file rejected valid fixture" }
        if ($null -ne (Get-VerifiedAuthenticodePath -Path "relative.exe" -PublisherPattern "Fixture Publisher")) { throw "$file accepted relative tool" }
        Remove-Item Function:Get-AuthenticodeSignature
    }
    $probeText = @'
import json, os, sys
print(json.dumps({"isolated": sys.flags.isolated, "git": [k for k in os.environ if k.upper().startswith("GIT_")], "cwd": os.getcwd()}))
'@
    $isolated = Invoke-PythonScript -PythonPath $PythonPath -ScriptText $probeText -WorkingDirectory $FixtureRoot -Capture
    if ($isolated.ExitCode -ne 0) { throw "Isolated script failed" }
    $payload = ($isolated.Output -join "`n") | ConvertFrom-Json
    if ($payload.isolated -ne 1 -or $payload.git.Count -ne 0 -or $payload.cwd -ne $FixtureRoot) { throw "Python isolation contract failed" }
    Assert-Restored
    Write-Host "PASS: both parsers; both Git boundaries; real Git identity; restore after exit/exception; isolated Python; signature decision fixtures."
}
finally {
    foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], "Process") }
}
