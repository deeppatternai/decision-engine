#Requires -Version 5.1
<#
.SYNOPSIS
Installs Deep Pattern (Decision Engine plus Agent Quality Gates) on native Windows.

.DESCRIPTION
This is a thin Windows bootstrapper. It acquires the installer only from the
Deep Pattern product repositories, verifies the expected main/stable channel
shape, and then delegates host MCP, skill, hook, junction, and ACL work to the
shipped DE/AQG Python adapters. Native Windows installation uses Git for
Windows Bash as required by AI_SETUP.md; WSL is deliberately rejected.

The script accepts no product options. The existing masked activation window is
the only interactive product input surface.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProgramName = "dp-install"
$DecisionEngineRepository = "https://github.com/deeppatternai/decision-engine.git"
$AqgRepository = "https://github.com/deeppatternai/agent-quality-gates.git"
$ManagedRoot = Join-Path $HOME ".deeppattern\decision-engine"
$AqgRoot = Join-Path $HOME ".deeppattern\agent-quality-gates"
$ExitFailure = 1
$ExitUsage = 2
$ExitBlocked = 3
$ExitPartial = 4

function Stop-Install {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [int]$Code = $ExitUsage
    )
    [Console]::Error.WriteLine("{0}: ERROR: {1}" -f $ProgramName, $Message)
    exit $Code
}

function Test-NativeWindows {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function Invoke-WithCleanEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter()][hashtable]$Environment = @{},
        [switch]$Capture
    )

    $remove = @(
        "DE_ENDPOINT", "DE_ACTIVATION_SECRET", "PYTHONPATH",
        "CLAUDE_DESKTOP_CONFIG", "WORKBUDDY_APP_ROOT", "WORKBUDDY_CONFIG",
        "WORKBUDDY_SKILLS_DIR", "BASH_ENV", "ENV"
    )
    $saved = @{}
    foreach ($name in ($remove + @($Environment.Keys) | Select-Object -Unique)) {
        $saved[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
        [System.Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    foreach ($name in $Environment.Keys) {
        [System.Environment]::SetEnvironmentVariable(
            [string]$name,
            [string]$Environment[$name],
            "Process"
        )
    }

    try {
        if ($Capture) {
            $output = @(& $FilePath @ArgumentList 2>&1)
            $code = $LASTEXITCODE
            return [pscustomobject]@{ ExitCode = $code; Output = $output }
        }
        & $FilePath @ArgumentList | ForEach-Object {
            [Console]::Out.WriteLine([string]$_)
        }
        $code = $LASTEXITCODE
        return $code
    }
    finally {
        foreach ($name in $saved.Keys) {
            [System.Environment]::SetEnvironmentVariable(
                [string]$name,
                $saved[$name],
                "Process"
            )
        }
    }
}

function Resolve-Git {
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        Stop-Install "Git for Windows 2.45 or newer is required."
    }
    $git = $command.Source
    $result = Invoke-WithCleanEnvironment -FilePath $git -ArgumentList @("--version") -Capture
    $joined = ($result.Output -join "`n")
    if ($result.ExitCode -ne 0 -or $joined -notmatch "git version ([0-9]+)\.([0-9]+)") {
        Stop-Install "The installed Git version could not be verified."
    }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    if ($major -lt 2 -or ($major -eq 2 -and $minor -lt 45)) {
        Stop-Install "Git for Windows 2.45 or newer is required."
    }
    return (Get-Item -LiteralPath $git).FullName
}

function Resolve-GitBash {
    param([Parameter(Mandatory = $true)][string]$GitPath)

    $gitDirectory = Split-Path -Parent $GitPath
    $gitRoot = Split-Path -Parent $gitDirectory
    $candidates = @(
        (Join-Path $gitRoot "bin\bash.exe"),
        (Join-Path $gitRoot "usr\bin\bash.exe")
    )
    $pathBash = Get-Command bash.exe -ErrorAction SilentlyContinue
    if ($null -ne $pathBash) {
        $candidates += $pathBash.Source
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $probe = Invoke-WithCleanEnvironment -FilePath $candidate -ArgumentList @(
            "-lc", "case `$(uname -s) in MINGW*|MSYS*|CYGWIN*) exit 0;; *) exit 9;; esac"
        ) -Capture
        if ($probe.ExitCode -eq 0) {
            return (Get-Item -LiteralPath $candidate).FullName
        }
    }
    Stop-Install "Git Bash from Git for Windows is required; WSL bash is not supported for native Windows hosts."
}

function Test-PythonExecutable {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $version = Invoke-WithCleanEnvironment -FilePath $Candidate -ArgumentList @(
        "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)"
    ) -Capture
    if ($version.ExitCode -ne 0) {
        return $false
    }
    $modules = Invoke-WithCleanEnvironment -FilePath $Candidate -ArgumentList @(
        "-c", "import ssl, venv, tkinter"
    ) -Capture
    if ($modules.ExitCode -ne 0) {
        return $false
    }
    $pip = Invoke-WithCleanEnvironment -FilePath $Candidate -ArgumentList @(
        "-m", "pip", "--version"
    ) -Capture
    return $pip.ExitCode -eq 0
}

function Resolve-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:DE_PYTHON)) {
        $candidates.Add($env:DE_PYTHON)
    }

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        foreach ($selector in @("-3.14", "-3.13", "-3.12", "-3")) {
            $probe = Invoke-WithCleanEnvironment -FilePath $py.Source -ArgumentList @(
                $selector, "-c", "import sys; print(sys.executable)"
            ) -Capture
            if ($probe.ExitCode -eq 0 -and $probe.Output.Count -gt 0) {
                $resolved = [string]$probe.Output[$probe.Output.Count - 1]
                if (-not [string]::IsNullOrWhiteSpace($resolved)) {
                    $candidates.Add($resolved.Trim())
                }
            }
        }
    }
    foreach ($name in @("python3.exe", "python.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            $candidates.Add($command.Source)
        }
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-PythonExecutable -Candidate $candidate) {
            $identity = Invoke-WithCleanEnvironment -FilePath $candidate -ArgumentList @(
                "-c", "import os, sys; print(os.path.abspath(sys.executable))"
            ) -Capture
            if ($identity.ExitCode -eq 0 -and $identity.Output.Count -gt 0) {
                return ([string]$identity.Output[$identity.Output.Count - 1]).Trim()
            }
        }
    }
    Stop-Install "Python 3.12 or newer with ssl, venv, pip, and tkinter is required."
}

function Convert-ToBashPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return $Path.Replace("\", "/")
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force
    return ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
}

function Invoke-ManagedPython {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$Capture
    )
    if (-not (Test-Path -LiteralPath $ManagedRoot -PathType Container)) {
        Stop-Install "The managed Decision Engine root is missing after installation." $ExitBlocked
    }
    Push-Location -LiteralPath $ManagedRoot
    try {
        return Invoke-WithCleanEnvironment -FilePath $script:PythonPath -ArgumentList $Arguments -Capture:$Capture
    }
    finally {
        Pop-Location
    }
}

function Test-ManagedRootGitState {
    $status = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $ManagedRoot, "status", "--porcelain=v1", "--untracked-files=all"
    ) -Capture
    if ($status.ExitCode -ne 0) {
        return $false
    }
    foreach ($rawLine in $status.Output) {
        $line = ([string]$rawLine).TrimEnd()
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        if ($line -eq "?? stopper-ui.json") {
            $stopperUi = Join-Path $ManagedRoot "stopper-ui.json"
            if ((Test-Path -LiteralPath $stopperUi -PathType Leaf) -and
                -not (Test-ReparsePoint -Path $stopperUi)) {
                continue
            }
        }
        return $false
    }
    return $true
}

function Test-CompleteManagedRoot {
    if (-not (Test-Path -LiteralPath $ManagedRoot -PathType Container) -or
        (Test-ReparsePoint -Path $ManagedRoot)) {
        return $false
    }
    $gitDirectory = Join-Path $ManagedRoot ".git"
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container) -or
        (Test-ReparsePoint -Path $gitDirectory)) {
        return $false
    }
    foreach ($relativePath in @(
        ".managed-install.json",
        ".runtime\update-state.json",
        ".runtime\update-protocol.json",
        "config.json",
        "VERSION",
        "installer\permanent_setup.py",
        "installer\mcp_config.py"
    )) {
        $requiredPath = Join-Path $ManagedRoot $relativePath
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf) -or
            (Test-ReparsePoint -Path $requiredPath)) {
            return $false
        }
    }
    if (-not (Test-ManagedRootGitState)) {
        return $false
    }

    $validationScript = @'
from pathlib import Path
import sys
from installer import managed_install, update_transaction, updater

root = Path(sys.argv[1])
reader = updater._GitReader(root)
identity = managed_install.validate_managed_identity(
    root, updater._read_remotes(reader)
)
state = updater._read_update_state(identity.canonical_root)
update_transaction._require_protocol_ready(identity.canonical_root)
_code, head_output = reader.run("head")
head = updater._single_commit(head_output, "managed HEAD")
version = (identity.canonical_root / "VERSION").read_text(encoding="utf-8").strip()
if head != state.last_release_commit or version != state.last_version:
    raise SystemExit(1)
'@
    $validation = Invoke-ManagedPython -Arguments @(
        "-c", $validationScript, $ManagedRoot
    ) -Capture
    return $validation.ExitCode -eq 0
}

function Get-ManagedActivationState {
    $probe = Invoke-ManagedPython -Arguments @(
        "-c",
        "from installer import activate, config; print('activated' if activate.is_permanently_activated(config.load_json(config.de_config_path())) else 'unactivated')"
    ) -Capture
    if ($probe.ExitCode -ne 0) {
        return $null
    }
    $state = ($probe.Output -join "").Trim()
    if ($state -notin @("activated", "unactivated")) {
        return $null
    }
    return $state
}

function Test-ManagedActivationRecoveryPending {
    $probe = Invoke-ManagedPython -Arguments @(
        "-c",
        "from installer import activate, config; raise SystemExit(0 if activate.activation_recovery_marker_path(config.de_config_path()).exists() else 1)"
    ) -Capture
    return $probe.ExitCode -eq 0
}

function Get-ManagedConfigDigest {
    $configPath = Join-Path $ManagedRoot "config.json"
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf) -or
        (Test-ReparsePoint -Path $configPath)) {
        return $null
    }
    return (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-ManagedStableUpdate {
    $updateScript = @'
from pathlib import Path
import sys
import time

from installer import (
    launcher,
    update_coordination,
    update_transaction,
    updater,
)
from installer.config import ShellError
from installer.release_acquisition import load_trusted_release_keys

root = Path(sys.argv[1])
deadline = time.monotonic() + launcher.STARTUP_UPDATE_BUDGET_SECONDS
wait_budget = (
    launcher.STARTUP_UPDATE_BUDGET_SECONDS
    + launcher.STARTUP_LEADER_GRACE_SECONDS
)
try:
    with update_coordination.startup_update_gate(
        root, timeout_seconds=wait_budget
    ) as startup_gate:
        recovery = launcher._finalize_journal(root)
        if recovery is not None and recovery.status in {
            "repair_required",
            "retry_pending",
            "deferred_active_session",
            "skipped_locked",
        }:
            raise ShellError(f"managed update recovery returned {recovery.status}")
        if getattr(startup_gate, "waited", False):
            raise ShellError(
                "another managed update attempt completed; retry to verify the current signed stable release"
            )
        if not launcher._updates_enabled(root):
            raise ShellError("managed update protocol is not ready")
        state = updater._read_update_state(root)
        if launcher._head_commit(root) != state.last_release_commit:
            raise ShellError("managed HEAD differs from the protected release state")
        trusted_keys = load_trusted_release_keys(
            root,
            deadline=deadline,
            expected_commit=state.last_release_commit,
        )
        if not trusted_keys:
            raise ShellError("managed release trust store contains no active key")
        result = launcher._attempt_update(root, trusted_keys, deadline=deadline)
except (
    ShellError,
    OSError,
    ValueError,
    update_coordination.InstallTransactionBusy,
) as exc:
    print(f"dp-install: managed stable update refused: {exc}", file=sys.stderr)
    raise SystemExit(1)

accepted_statuses = {"up_to_date", "candidate_ready", "updated"}
print(result.status)
if result.status not in accepted_statuses:
    blocker_pids = ",".join(
        str(blocker.pid) for blocker in result.blockers if blocker.pid is not None
    )
    details = [f"status={result.status}"]
    if result.error_code:
        details.append(f"error_code={result.error_code}")
    if blocker_pids:
        details.append(f"active_shim_pids={blocker_pids}")
    print(
        "dp-install: managed stable update did not apply: " + ", ".join(details),
        file=sys.stderr,
    )
raise SystemExit(0 if result.status in accepted_statuses else 1)
'@
    $result = Invoke-ManagedPython -Arguments @(
        "-c", $updateScript, $ManagedRoot
    ) -Capture
    $acceptedStatuses = @("up_to_date", "candidate_ready", "updated")
    $status = $null
    foreach ($rawLine in $result.Output) {
        $line = ([string]$rawLine).Trim()
        if ($line -in $acceptedStatuses) {
            $status = $line
        }
        elseif (-not [string]::IsNullOrWhiteSpace($line)) {
            [Console]::Error.WriteLine($line)
        }
    }
    return [pscustomobject]@{ ExitCode = $result.ExitCode; Status = $status }
}

if (-not (Test-NativeWindows)) {
    Stop-Install "This entrypoint supports native Windows only."
}
if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
    Stop-Install "Run the 64-bit Windows PowerShell host."
}
if ([string]::IsNullOrWhiteSpace($env:APPDATA) -or [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Stop-Install "APPDATA and LOCALAPPDATA must identify the current Windows user profile."
}

$GitPath = Resolve-Git
$BashPath = Resolve-GitBash -GitPath $GitPath
$script:PythonPath = Resolve-Python
$PythonForBash = Convert-ToBashPath -Path $script:PythonPath

if (Test-ReparsePoint -Path (Join-Path $HOME ".deeppattern")) {
    Stop-Install "$HOME\.deeppattern is a reparse point; preserve it and use an owner-guided install." $ExitBlocked
}
if (Test-Path -LiteralPath $AqgRoot) {
    if (-not (Test-Path -LiteralPath $AqgRoot -PathType Container) -or (Test-ReparsePoint -Path $AqgRoot)) {
        Stop-Install "$AqgRoot is not a regular AQG checkout; preserve it and use the managed replacement flow." $ExitBlocked
    }
    $aqgOrigin = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "remote", "get-url", "origin"
    ) -Capture
    if ($aqgOrigin.ExitCode -ne 0 -or (($aqgOrigin.Output -join "").Trim() -ne $AqgRepository)) {
        Stop-Install "$AqgRoot is not owned by the approved AQG product repository; it was preserved." $ExitBlocked
    }
    $aqgStatus = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "status", "--porcelain"
    ) -Capture
    if ($aqgStatus.ExitCode -ne 0 -or -not [string]::IsNullOrWhiteSpace(($aqgStatus.Output -join ""))) {
        Stop-Install "$AqgRoot contains local changes; it was preserved." $ExitBlocked
    }
}

# Repeated installs must update through the signed transaction before the
# temporary main checkout performs bootstrap repair or writes host state.
$managedRootItem = Get-Item -LiteralPath $ManagedRoot -Force -ErrorAction SilentlyContinue
$managedRootWasPresent = $null -ne $managedRootItem
if ($managedRootWasPresent) {
    if (-not (Test-CompleteManagedRoot)) {
        Stop-Install "$ManagedRoot exists but is not a complete, clean, verified managed stable install. Preserve it and use the managed uninstall or replacement flow." $ExitBlocked
    }
    $activationStateBeforeUpdate = Get-ManagedActivationState
    if ($null -eq $activationStateBeforeUpdate) {
        Stop-Install "The managed activation state could not be verified before update. Preserve the managed root and use the owner-guided repair flow." $ExitBlocked
    }
    if (Test-ManagedActivationRecoveryPending) {
        Stop-Install "The managed root has an activation recovery marker. Do not retry automatically; use the owner-guided activation recovery flow." $ExitBlocked
    }
    $configDigestBeforeUpdate = Get-ManagedConfigDigest
    if ($null -eq $configDigestBeforeUpdate) {
        Stop-Install "The Decision Engine configuration could not be fingerprinted before update." $ExitBlocked
    }

    if ($activationStateBeforeUpdate -eq "activated") {
        Write-Host "Found a complete signed and activated Decision Engine install; updating signed stable before host repair without reopening activation."
    }
    else {
        Write-Host "Found a complete signed Decision Engine stable install with activation pending; updating signed stable before resuming setup."
    }
    Write-Host "Checking and applying the newest signed Decision Engine stable release before continuing..."
    $managedUpdate = Invoke-ManagedStableUpdate
    if ($managedUpdate.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($managedUpdate.Status)) {
        $preserved = Test-CompleteManagedRoot
        if ($preserved) {
            $activationStateAfterFailure = Get-ManagedActivationState
            $configDigestAfterFailure = Get-ManagedConfigDigest
            $preserved = (
                $activationStateAfterFailure -eq $activationStateBeforeUpdate -and
                $configDigestAfterFailure -eq $configDigestBeforeUpdate
            )
        }
        if ($preserved) {
            $failedStatus = if ($null -eq $managedUpdate.Status) { "unknown" } else { $managedUpdate.Status }
            Stop-Install "Signed stable update status $failedStatus; the existing release and activation state were verified and preserved. Close every configured Agent host and retry." $ExitFailure
        }
        Stop-Install "Signed stable update did not complete and the previous release could not be re-verified. Preserve the managed root and use the owner-guided recovery flow." $ExitBlocked
    }
    if (-not (Test-CompleteManagedRoot)) {
        Stop-Install "Signed stable update returned $($managedUpdate.Status), but the managed release no longer validates." $ExitBlocked
    }
    $activationStateAfterUpdate = Get-ManagedActivationState
    if ($activationStateAfterUpdate -ne $activationStateBeforeUpdate) {
        Stop-Install "Signed stable update returned $($managedUpdate.Status), but the activation state changed." $ExitBlocked
    }
    $configDigestAfterUpdate = Get-ManagedConfigDigest
    if ($configDigestAfterUpdate -ne $configDigestBeforeUpdate) {
        Stop-Install "Signed stable update returned $($managedUpdate.Status), but the protected activation configuration changed." $ExitBlocked
    }
    Write-Host "Decision Engine signed stable update status: $($managedUpdate.Status)"
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("dp-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
    Write-Host "Checking the official Decision Engine source and signed stable channel..."
    $refs = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "ls-remote", $DecisionEngineRepository, "refs/heads/main", "refs/heads/stable"
    ) -Capture
    $refText = $refs.Output -join "`n"
    if ($refs.ExitCode -ne 0) {
        Stop-Install "The Decision Engine product repository could not be read."
    }
    if ($refText -notmatch "(?m)\srefs/heads/main\s*$" -or $refText -notmatch "(?m)\srefs/heads/stable\s*$") {
        Stop-Install "The product repository did not expose both refs/heads/main and refs/heads/stable."
    }

    $sourceRoot = Join-Path $tempRoot "decision-engine"
    $clone = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "clone", "--depth", "1", "--branch", "main", "--single-branch",
        "--", $DecisionEngineRepository, $sourceRoot
    )
    if ($clone -ne 0) {
        Stop-Install "The official Decision Engine installer source could not be obtained."
    }

    $origin = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $sourceRoot, "remote", "get-url", "origin"
    ) -Capture
    if ($origin.ExitCode -ne 0 -or (($origin.Output -join "").Trim() -ne $DecisionEngineRepository)) {
        Stop-Install "The downloaded installer source has an unexpected origin." $ExitBlocked
    }
    foreach ($required in @("install.sh", "AI_SETUP.md", "installer\managed_install.py")) {
        $requiredPath = Join-Path $sourceRoot $required
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf) -or (Test-ReparsePoint -Path $requiredPath)) {
            Stop-Install "The downloaded installer source is incomplete or link-like: $required" $ExitBlocked
        }
    }
    $status = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $sourceRoot, "status", "--porcelain"
    ) -Capture
    if ($status.ExitCode -ne 0 -or -not [string]::IsNullOrWhiteSpace(($status.Output -join ""))) {
        Stop-Install "The downloaded installer source is not clean." $ExitBlocked
    }
    $contract = [System.IO.File]::ReadAllText((Join-Path $sourceRoot "installer\managed_install.py"))
    if ($contract -notmatch [regex]::Escape("https://github.com/deeppatternai/decision-engine.git")) {
        Stop-Install "The installer source does not declare the approved product repository." $ExitBlocked
    }

    $sourceShaResult = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $sourceRoot, "rev-parse", "HEAD"
    ) -Capture
    if ($sourceShaResult.ExitCode -ne 0) {
        Stop-Install "The installer source identity could not be recorded." $ExitBlocked
    }
    $sourceSha = (($sourceShaResult.Output -join "").Trim())

    Write-Host "Installing Agent Quality Gates and the signed Decision Engine stable release..."
    $bashSource = Convert-ToBashPath -Path $sourceRoot
    $installEnvironment = @{
        "DE_PYTHON" = $PythonForBash
        "WITH_AQG" = "1"
        "WITH_MCP" = "1"
        "DE_DEV_MODE" = "0"
        "AQG_REPO" = $AqgRepository
    }
    $installCode = Invoke-WithCleanEnvironment -FilePath $BashPath -ArgumentList @(
        "--noprofile", "--norc", "-lc",
        'set -e; cd "$1"; exec ./install.sh de', "dp-install", $bashSource
    ) -Environment $installEnvironment
    if ($installCode -ne 0) {
        Stop-Install "The core installer failed. Preserve its output and run dp-uninstall.ps1 -Scope both -Apply before a clean retry." $ExitBlocked
    }

    if (-not (Test-Path -LiteralPath $ManagedRoot -PathType Container) -or (Test-ReparsePoint -Path $ManagedRoot)) {
        Stop-Install "The managed Decision Engine root is missing or link-like after installation." $ExitBlocked
    }

    $activationProbe = Invoke-ManagedPython -Arguments @(
        "-c",
        "from installer import activate, config; print('activated' if activate.is_permanently_activated(config.load_json(config.de_config_path())) else 'pending')"
    ) -Capture
    $activationState = ($activationProbe.Output -join "").Trim()
    if ($activationProbe.ExitCode -ne 0) {
        Stop-Install "The managed activation state could not be verified." $ExitBlocked
    }

    if ($activationState -ne "activated") {
        Write-Host "Opening the masked Decision Engine activation window..."
        $setupCode = Invoke-ManagedPython -Arguments @("-m", "installer.permanent_setup")
        $activationProbe = Invoke-ManagedPython -Arguments @(
            "-c",
            "from installer import activate, config; print('activated' if activate.is_permanently_activated(config.load_json(config.de_config_path())) else 'pending')"
        ) -Capture
        $activationState = ($activationProbe.Output -join "").Trim()
        if ($setupCode -ne 0 -and $activationState -eq "activated") {
            Write-Warning "Activation completed, but a post-activation check failed; Doctor output below is authoritative."
        }
        elseif ($setupCode -ne 0) {
            Write-Host "Activation remains pending. The credential-free DE Lite transport stays installed."
        }
    }

    $clientsResult = Invoke-ManagedPython -Arguments @(
        "-c", "from installer import mcp_config; print('\n'.join(mcp_config.detect_clients()))"
    ) -Capture
    if ($clientsResult.ExitCode -ne 0) {
        Stop-Install "Installed host detection failed." $ExitBlocked
    }
    $clients = @($clientsResult.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    $wiringFailed = $false
    foreach ($client in $clients) {
        $wireArguments = @("-m", "installer.mcp_config", "--write", "--client", $client)
        if ($activationState -ne "activated") {
            $wireArguments += "--allow-unactivated"
        }
        $wireCode = Invoke-ManagedPython -Arguments $wireArguments
        if ($wireCode -ne 0) {
            [Console]::Error.WriteLine("{0}: ERROR: MCP wiring failed for {1}" -f $ProgramName, $client)
            $wiringFailed = $true
        }
    }
    $routeCode = Invoke-ManagedPython -Arguments @(
        "-c",
        "from installer import config, install; r=install.repair_detected_skill_routes(config.managed_component_root('decision-engine')); print(r); raise SystemExit(1 if r.failed else 0)"
    )
    if ($routeCode -ne 0) {
        [Console]::Error.WriteLine("{0}: ERROR: managed skill routing failed" -f $ProgramName)
        $wiringFailed = $true
    }
    if ($wiringFailed) {
        Stop-Install "Core installation is complete, but one or more host integrations failed." $ExitBlocked
    }

    $aqgVerifyFailed = $false
    $aqgAdapter = Join-Path $AqgRoot "scripts\install_aqg_clients.py"
    if (Test-Path -LiteralPath $aqgAdapter -PathType Leaf) {
        $aqgCode = Invoke-WithCleanEnvironment -FilePath $script:PythonPath -ArgumentList @(
            $aqgAdapter, "--installed-supported", "--apply", "--aqg-root", $AqgRoot, "--home", $HOME
        )
        if ($aqgCode -ne 0) {
            $aqgVerifyFailed = $true
            [Console]::Error.WriteLine("{0}: ERROR: AQG host adapter installation or verification failed" -f $ProgramName)
        }
    }
    else {
        $aqgVerifyFailed = $true
        [Console]::Error.WriteLine("{0}: ERROR: AQG adapter entrypoint is missing" -f $ProgramName)
    }

    $doctorCode = Invoke-ManagedPython -Arguments @("-m", "installer.doctor")
    if ($activationState -eq "activated" -and $doctorCode -ne 0) {
        Stop-Install "Decision Engine is activated, but Doctor reports a failure." $ExitBlocked
    }

    Write-Host "Decision Engine host capability report:"
    foreach ($client in $clients) {
        $entry = Invoke-ManagedPython -Arguments @(
            "-c",
            "from installer import mcp_config; import sys; print(mcp_config.entry_status(sys.argv[1], allow_unactivated=True))",
            $client
        ) -Capture
        $state = ($entry.Output -join "").Trim()
        if ($entry.ExitCode -ne 0) {
            $state = "unverifiable"
        }
        Write-Host ("{0}: DE MCP={1}; runtime=restart-required" -f $client, $state)
    }

    $unsupported = New-Object System.Collections.Generic.List[string]
    $expectedProducts = @(
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs\TRAE SOLO"); Client = "trae-work"; Name = "TRAE Work" },
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs\TRAE SOLO CN"); Client = "trae-work-cn"; Name = "TRAE Work CN" },
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs\WorkBuddy"); Client = "workbuddy"; Name = "WorkBuddy" },
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs\Qoder"); Client = "qoder"; Name = "Qoder" }
    )
    foreach ($product in $expectedProducts) {
        if ((Test-Path -LiteralPath $product.Path -PathType Container) -and ($clients -notcontains $product.Client)) {
            $unsupported.Add("$($product.Name): installed but not configured by the current signed adapter")
        }
    }
    foreach ($product in @(
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs\Qoder CN"); Name = "Qoder CN" },
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs\Trae"); Name = "TRAE Code" },
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs\Trae CN"); Name = "TRAE Code CN" },
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs\WorkBuddy AI"); Name = "WorkBuddy AI" }
    )) {
        if (Test-Path -LiteralPath $product.Path -PathType Container) {
            $unsupported.Add("$($product.Name): Windows DE adapter unavailable in the current signed release")
        }
    }

    Write-Host ("{0}: source={1}" -f $ProgramName, $sourceSha)
    Write-Host ("{0}: activation={1}" -f $ProgramName, $activationState)
    Write-Host ("{0}: restart every configured host before runtime verification" -f $ProgramName)
    if ($unsupported.Count -gt 0 -or $aqgVerifyFailed) {
        if ($unsupported.Count -gt 0) {
            Write-Host "Unsupported or unconfigured installed products:"
            foreach ($item in $unsupported) {
                Write-Host $item
            }
        }
        [Console]::Error.WriteLine("{0}: PARTIAL: supported components were installed, but one or more detected hosts are unsupported, unconfigured, or unverifiable." -f $ProgramName)
        exit $ExitPartial
    }
    Write-Host ("{0}: PASS: installation completed; runtime verification requires host restart" -f $ProgramName)
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
