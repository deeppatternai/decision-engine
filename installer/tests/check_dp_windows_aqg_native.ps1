#Requires -Version 5.1
<# Offline native checks. Only temporary fixtures and inherited test variables are changed. #>
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$PythonPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$toolsRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$installer = Join-Path $toolsRoot "dp-install.ps1"
$ast = $null
foreach ($file in @($installer, (Join-Path $toolsRoot "dp-uninstall.ps1"))) {
    $tokens = $null
    $errors = $null
    $parsed = [System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)
    if ($errors.Count -ne 0) { throw ($errors | Out-String) }
    if ($file -eq $installer) { $ast = $parsed }
}
if (-not [IO.Path]::IsPathRooted($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Supply an absolute path to an installed Python 3.12+ executable."
}
$script:PythonPath = $PythonPath
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$functions = @("Invoke-WithCleanEnvironment", "Invoke-PythonScript", "Test-AqgManagedTargetName", "Test-ReparsePoint", "Assert-OwnedAqgDirectory")
foreach ($name in $functions) {
    $nodes = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true))
    if ($nodes.Count -ne 1) { throw "Expected one function: $name" }
    Invoke-Expression $nodes[0].Extent.Text
}
$ExitBlocked = 3
function Stop-Install { param([string]$Message, [int]$Code) throw $Message }
$temp = Join-Path ([IO.Path]::GetTempPath()) ("dp-aqg-native-" + [Guid]::NewGuid().ToString("N"))
$saved = @{}
try {
    New-Item -ItemType Directory -Path $temp | Out-Null
    $poison = @{
        "GIT_DIR" = "poison"; "GIT_WORK_TREE" = "poison"; "GIT_CONFIG_COUNT" = "1"
        "GIT_CONFIG_KEY_0" = "core.worktree"; "GIT_CONFIG_VALUE_0" = "poison"
        "GIT_FUTURE_TEST_VARIABLE" = "poison"; "AQG_BACKUP_DIR" = "poison"
    }
    foreach ($key in $poison.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $poison[$key], "Process")
    }
    $probe = Invoke-PythonScript -PythonPath $PythonPath -Capture -ScriptText @'
import json, os, sys
print(json.dumps({"git_keys": [k for k in os.environ if k.upper().startswith("GIT_")], "backup": os.environ.get("AQG_BACKUP_DIR"), "isolated": sys.flags.isolated}))
'@
    if ($probe.ExitCode -ne 0) { throw "Python environment probe failed" }
    $data = ($probe.Output -join "`n") | ConvertFrom-Json
    if ($data.git_keys.Count -ne 0 -or $data.isolated -ne 1 -or
        $data.backup -ne (Join-Path $HOME ".deeppattern\aqg-backups")) { throw "Environment contract failed" }
    foreach ($key in $poison.Keys) {
        if ([Environment]::GetEnvironmentVariable($key, "Process") -ne $poison[$key]) { throw "Environment was not restored: $key" }
    }
    $head = "a" * 40
    foreach ($name in @("0.14.17", $head, ("0.14.17-" + $head.Substring(0, 12)), "0.14.17-0123456789abcdef")) {
        $target = Join-Path $temp $name
        New-Item -ItemType Directory -Path $target | Out-Null
        [IO.File]::WriteAllText((Join-Path $target "VERSION"), "0.14.17`n", $script:Utf8NoBom)
        if (-not (Test-AqgManagedTargetName -Target $target -Head $head)) { throw "Valid name rejected: $name" }
    }
    $bad = Join-Path $temp "0.14.16"
    New-Item -ItemType Directory -Path $bad | Out-Null
    [IO.File]::WriteAllText((Join-Path $bad "VERSION"), "0.14.17`n", $script:Utf8NoBom)
    if (Test-AqgManagedTargetName -Target $bad -Head $head) { throw "VERSION mismatch accepted" }
    $junction = Join-Path $temp "fixture-junction"
    New-Item -ItemType Junction -Path $junction -Target $bad | Out-Null
    try {
        $rejected = $false
        try { Assert-OwnedAqgDirectory -Path $junction } catch { $rejected = $true }
        if (-not $rejected) { throw "Backup junction was not rejected" }
    }
    finally { [IO.DirectoryInfo]::new($junction).Delete() }
    Write-Host "PASS: both PowerShell parsers; isolated Python; GIT_* removal/restoration; backup root; version names; backup junction rejection."
}
finally {
    foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], "Process") }
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
