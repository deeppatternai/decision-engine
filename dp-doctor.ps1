#Requires -Version 5.1

param()

$ErrorActionPreference = "Stop"
$ProgramName = "dp-doctor"

function Stop-Doctor {
    param([Parameter(Mandatory = $true)][string]$Message)

    [Console]::Error.WriteLine("{0}: ERROR: {1}" -f $ProgramName, $Message)
    exit 1
}

function Test-TrustedDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force
    return ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0
}

function Test-TrustedFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force
    return ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0
}

$doctorArguments = @()
if ($args.Count -eq 1 -and ([string]$args[0]) -ieq "-Json") {
    $doctorArguments = @("--json")
}
elseif ($args.Count -ne 0) {
    [Console]::Error.WriteLine(
        'Usage: powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$HOME\.deeppattern\decision-engine\dp-doctor.ps1" [-Json]'
    )
    exit 2
}

$userHome = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
if ([string]::IsNullOrWhiteSpace($userHome)) {
    Stop-Doctor "the user profile directory could not be resolved"
}
$deepPatternRoot = Join-Path $userHome ".deeppattern"
$managedRoot = Join-Path $userHome ".deeppattern\decision-engine"
$managedPythonRoot = Join-Path $userHome ".deeppattern\de-python"
$managedPython = Join-Path $managedPythonRoot "Scripts\python.exe"
$managedPythonMarker = Join-Path $managedPythonRoot ".deeppattern-python-env"
$installerRoot = Join-Path $managedRoot "installer"
$doctorModule = Join-Path $installerRoot "doctor.py"

foreach ($directory in @($deepPatternRoot, $managedRoot, $managedPythonRoot, $installerRoot)) {
    if (-not (Test-TrustedDirectory -Path $directory)) {
        Stop-Doctor "the managed Decision Engine installation is incomplete or contains an untrusted reparse point; re-run the official installer, then retry Doctor"
    }
}
if (-not (Test-TrustedFile -Path $managedPython) -or
    -not (Test-TrustedFile -Path $managedPythonMarker)) {
    Stop-Doctor "the private Deep Pattern Python environment is unavailable; re-run the official installer, then retry Doctor"
}
if (-not (Test-TrustedFile -Path $doctorModule)) {
    Stop-Doctor "the installed Doctor module is unavailable or untrusted; re-run the official installer, then retry Doctor"
}

$inheritedNames = @(
    Get-ChildItem Env: | Where-Object {
        $_.Name -match "^(?i:GIT_)" -or
        $_.Name -match "^(?i:PYTHON)" -or
        $_.Name -in @(
            "DE_ENDPOINT",
            "DE_ACTIVATION_SECRET",
            "DE_API_KEY",
            "DE_ACCESS_TOKEN",
            "DE_DEVICE_TOKEN"
        )
    } | Select-Object -ExpandProperty Name
)
foreach ($name in $inheritedNames) {
    Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$prefixProbe = 'import os, sys; raise SystemExit(0 if os.path.realpath(sys.prefix) == os.path.realpath(sys.argv[1]) else 1)'
& $managedPython -I -X utf8 -c $prefixProbe $managedPythonRoot *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-Doctor "the private Deep Pattern Python environment is invalid; re-run the official installer, then retry Doctor"
}

$runner = 'import runpy, sys; root = sys.argv.pop(1); sys.path.insert(0, root); runpy.run_module("installer.doctor", run_name="__main__")'
& $managedPython -I -X utf8 -c $runner $managedRoot @doctorArguments
exit $LASTEXITCODE
