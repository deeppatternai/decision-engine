#Requires -Version 5.1
<#
.SYNOPSIS
Safely uninstalls or quarantines Deep Pattern managed state on native Windows.

.DESCRIPTION
The default mode is read-only. Pass -Apply only after reviewing the dry-run
plan. The uninstaller removes only integrations whose current managed fields
still match Decision Engine ownership evidence. Foreign or modified entries,
unexpected reparse points, unverified runtime processes, and unknown root
layouts stop the entire operation before mutation. Apply mode can terminate
only revalidated, current-user managed DE/AQG child processes after explicit
confirmation; Agent applications themselves are never terminated.

Source/worktree checkouts are always preserved. Removed managed roots and
configuration snapshots are retained under the current user's private
.deeppattern\uninstall-backups directory.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("de", "aqg", "both")]
    [string]$Scope,

    [switch]$Apply,

    [Parameter(DontShow = $true)]
    [string]$HomePath = $HOME
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgramName = "dp-uninstall"
$ExitProcessBlocked = 5
$script:PrivateBootstrapRoot = $null

function Stop-Uninstall {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine(("{0}: ERROR: {1}" -f $ProgramName, $Message))
    exit 2
}

function Get-VerifiedAuthenticodePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$PublisherPattern
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or
        -not [IO.Path]::IsPathRooted($Path) -or
        -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $fullPath = (Get-Item -LiteralPath $Path -Force).FullName
        $signature = Get-AuthenticodeSignature -LiteralPath $fullPath -ErrorAction Stop
        if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
            $null -eq $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch $PublisherPattern) {
            return $null
        }
        return $fullPath
    }
    catch {
        return $null
    }
}

function Invoke-Clean {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [switch]$Capture
    )
    $names = @("DE_ENDPOINT", "DE_ACTIVATION_SECRET", "PYTHONPATH")
    $names += @(
        [Environment]::GetEnvironmentVariables("Process").Keys |
            ForEach-Object { [string]$_ } |
            Where-Object { $_ -match "^(?i:GIT_)" }
    )
    $saved = @{}
    foreach ($name in $names) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    try {
        # Delete the variable; an empty GIT_* value still changes Git behavior.
        foreach ($name in $saved.Keys) {
            if (Test-Path -LiteralPath "Env:$name") {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction Stop
            }
        }
        if ($Capture) {
            $previousErrorActionPreference = $ErrorActionPreference
            try {
                # Native stderr is diagnostic output during prerequisite probing;
                # the process exit code remains the authoritative result.
                $ErrorActionPreference = "Continue"
                $output = @(& $FilePath @ArgumentList 2>&1)
                $code = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }
            return [pscustomobject]@{ ExitCode = $code; Output = $output }
        }
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $FilePath @ArgumentList 2>&1 | ForEach-Object {
                [Console]::Out.WriteLine([string]$_)
            }
            $code = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        return $code
    }
    finally {
        foreach ($name in $saved.Keys) {
            if ($null -eq $saved[$name]) {
                if (Test-Path -LiteralPath "Env:$name") {
                    Remove-Item -LiteralPath "Env:$name" -ErrorAction Stop
                }
            }
            else {
                [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
            }
        }
    }
}

function Test-ManagedPrivatePythonCandidate {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    $root = Join-Path $HomePath ".deeppattern\de-python"
    $expected = Join-Path $root "Scripts\python.exe"
    $marker = Join-Path $root ".deeppattern-python-env"
    if (-not [string]::Equals(
            [IO.Path]::GetFullPath($Candidate),
            [IO.Path]::GetFullPath($expected),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not (Test-Path -LiteralPath $root -PathType Container) -or
        (Test-ReparsePoint -Path $root) -or
        -not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        (Test-ReparsePoint -Path $marker)) {
        return $false
    }
    return @(Get-Content -LiteralPath $marker -ErrorAction SilentlyContinue) -contains "schema=1"
}

function Test-Python {
    param([Parameter(Mandatory = $true)][string]$Candidate)
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $verified = Get-VerifiedAuthenticodePath `
        -Path $Candidate `
        -PublisherPattern "(?i:Python Software Foundation|Microsoft Corporation|Anaconda)"
    if ($null -eq $verified) {
        if (-not (Test-ManagedPrivatePythonCandidate -Candidate $Candidate)) {
            return $false
        }
        $verified = (Get-Item -LiteralPath $Candidate -Force).FullName
    }
    $probe = Invoke-Clean -FilePath $verified -ArgumentList @(
        "-I", "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)"
    ) -Capture
    if ($probe.ExitCode -ne 0) {
        return $false
    }
    return $true
}

function New-PrivateUninstallBootstrap {
    $managedRoot = Join-Path $HomePath ".deeppattern\de-python"
    $marker = Join-Path $managedRoot ".deeppattern-python-env"
    $baseLine = @(
        Get-Content -LiteralPath $marker -ErrorAction Stop |
            Where-Object { $_ -like "base_python=*" }
    )
    if ($baseLine.Count -ne 1) {
        Stop-Uninstall "The private Python environment does not identify one base runtime."
    }
    $basePython = [string]$baseLine[0].Substring("base_python=".Length)
    $verifiedBase = Get-VerifiedAuthenticodePath `
        -Path $basePython `
        -PublisherPattern "(?i:Python Software Foundation|Microsoft Corporation|Anaconda)"
    if ($null -ne $verifiedBase) {
        return $verifiedBase
    }

    $runtimeRoot = Join-Path $HomePath ".deeppattern\runtimes"
    $fullBase = [IO.Path]::GetFullPath($basePython)
    $fullRuntimeRoot = [IO.Path]::GetFullPath($runtimeRoot).TrimEnd("\") + "\"
    if (-not $fullBase.StartsWith($fullRuntimeRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($fullBase) -ne "python.exe") {
        Stop-Uninstall "The private Python base runtime identity cannot be proven."
    }
    $pythonDirectory = Split-Path -Parent $fullBase
    $runtimeDirectory = Split-Path -Parent $pythonDirectory
    $runtimeMarker = Join-Path $runtimeDirectory ".deeppattern-python-runtime"
    if (-not (Test-Path -LiteralPath $runtimeMarker -PathType Leaf) -or
        (Test-ReparsePoint -Path $runtimeDirectory) -or
        (Test-ReparsePoint -Path $runtimeMarker) -or
        (@(Get-Content -LiteralPath $runtimeMarker -ErrorAction Stop) -notcontains "schema=1")) {
        Stop-Uninstall "The private Python base runtime marker cannot be verified."
    }

    $script:PrivateBootstrapRoot = Join-Path (
        [IO.Path]::GetTempPath()
    ) ("dp-uninstall-python-" + [Guid]::NewGuid().ToString("N"))
    $bootstrapDirectory = Join-Path $script:PrivateBootstrapRoot "python"
    Write-Host "Preparing a temporary verified Deep Pattern Python runtime for uninstall."
    Write-Host "This copies the private runtime before it is removed and may take a moment; no input is required."
    $bootstrapTimer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        New-Item -ItemType Directory -Path $script:PrivateBootstrapRoot | Out-Null
        Copy-Item -LiteralPath $pythonDirectory -Destination $bootstrapDirectory -Recurse -Force
        $bootstrapPython = Join-Path $bootstrapDirectory "python.exe"
        $probe = Invoke-Clean -FilePath $bootstrapPython -ArgumentList @(
            "-I", "-c", "import ssl, sys, tkinter; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)"
        ) -Capture
        if ($probe.ExitCode -ne 0) {
            throw "temporary private Python verification failed"
        }
        $bootstrapTimer.Stop()
        Write-Host ("Temporary uninstall runtime ready in {0:N1} seconds." -f $bootstrapTimer.Elapsed.TotalSeconds)
        return $bootstrapPython
    }
    catch {
        Remove-Item -LiteralPath $script:PrivateBootstrapRoot -Recurse -Force -ErrorAction SilentlyContinue
        $script:PrivateBootstrapRoot = $null
        Stop-Uninstall "The temporary private Python uninstall runtime could not be prepared."
    }
}

function Resolve-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:DE_AQG_PYTHON)) {
        $candidates.Add($env:DE_AQG_PYTHON)
    }
    foreach ($managedPython in @(
        (Join-Path $HomePath ".deeppattern\de-python\Scripts\python.exe"),
        (Join-Path $HomePath ".deeppattern\decision-engine\.venv\Scripts\python.exe")
    )) {
        if (Test-Path -LiteralPath $managedPython -PathType Leaf) {
            $candidates.Add($managedPython)
        }
    }
    $launchers = New-Object System.Collections.Generic.List[string]
    foreach ($entry in @(
        @($env:WINDIR, "py.exe"),
        @($env:LOCALAPPDATA, "Programs\Python\Launcher\py.exe"),
        @($env:ProgramFiles, "Python Launcher\py.exe")
    )) {
        if (-not [string]::IsNullOrWhiteSpace([string]$entry[0])) {
            $launchers.Add((Join-Path ([string]$entry[0]) ([string]$entry[1])))
        }
    }
    foreach ($launcher in ($launchers | Select-Object -Unique)) {
        $py = Get-VerifiedAuthenticodePath `
            -Path $launcher `
            -PublisherPattern "(?i:Python Software Foundation|Microsoft Corporation)"
        if ($null -eq $py) {
            continue
        }
        foreach ($selector in @("-3.14", "-3.13", "-3.12", "-3")) {
            $probe = Invoke-Clean -FilePath $py -ArgumentList @(
                $selector, "-I", "-c", "import sys; print(sys.executable)"
            ) -Capture
            if ($probe.ExitCode -eq 0 -and $probe.Output.Count -gt 0) {
                $resolved = $probe.Output
                $candidates.Add(([string]$resolved[$resolved.Count - 1]).Trim())
            }
        }
    }
    foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
        if ([string]::IsNullOrWhiteSpace($root)) {
            continue
        }
        foreach ($relative in @(
            "Programs\Python\Python314\python.exe",
            "Programs\Python\Python313\python.exe",
            "Programs\Python\Python312\python.exe",
            "Python\pythoncore-3.14-64\python.exe",
            "Python\pythoncore-3.13-64\python.exe",
            "Python\pythoncore-3.12-64\python.exe",
            "Python314\python.exe",
            "Python313\python.exe",
            "Python312\python.exe"
        )) {
            $candidates.Add((Join-Path $root $relative))
        }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Python -Candidate $candidate) {
            $resolved = (Get-Item -LiteralPath $candidate).FullName
            if (Test-ManagedPrivatePythonCandidate -Candidate $resolved) {
                return New-PrivateUninstallBootstrap
            }
            return $resolved
        }
    }
    Stop-Uninstall "Python 3.12 or newer is required."
}

function Get-GitCandidates {
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($entry in @(
        @([Environment]::GetEnvironmentVariable("ProgramFiles"), "Git\cmd\git.exe"),
        @([Environment]::GetEnvironmentVariable("ProgramFiles(x86)"), "Git\cmd\git.exe"),
        @($env:LOCALAPPDATA, "Programs\Git\cmd\git.exe"),
        @($env:LOCALAPPDATA, "Git\cmd\git.exe")
    )) {
        if (-not [string]::IsNullOrWhiteSpace([string]$entry[0])) {
            $candidates.Add((Join-Path ([string]$entry[0]) ([string]$entry[1])))
        }
    }
    foreach ($registryPath in @(
        "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\GitForWindows",
        "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\GitForWindows",
        "Registry::HKEY_CURRENT_USER\SOFTWARE\GitForWindows"
    )) {
        try {
            $installPath = [string](Get-ItemProperty -LiteralPath $registryPath -ErrorAction Stop).InstallPath
            if (-not [string]::IsNullOrWhiteSpace($installPath)) {
                $candidates.Add((Join-Path $installPath "cmd\git.exe"))
            }
        }
        catch {
            # Non-default installs may omit one or more registry views.
        }
    }
    return @($candidates | Select-Object -Unique)
}

function Resolve-Git {
    foreach ($candidate in (Get-GitCandidates)) {
        $verified = Get-VerifiedAuthenticodePath `
            -Path $candidate `
            -PublisherPattern "(?i:Johannes Schindelin|Git for Windows)"
        if ($null -ne $verified) {
            return $verified
        }
    }
    return $null
}

function Write-ProcessInventory {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $rows = @(
            Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
                Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine
        )
        $json = ConvertTo-Json -InputObject $rows -Compress -Depth 3
        [IO.File]::WriteAllText(
            $Path,
            [string]$json,
            (New-Object System.Text.UTF8Encoding($false))
        )
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    catch {
        Stop-Uninstall ("Windows process inventory failed: {0}" -f $_.Exception.GetType().Name)
    }
}

function Confirm-UserAction {
    param([Parameter(Mandatory = $true)][string]$Message)

    try {
        $answer = Read-Host ("{0} [Y/N]" -f $Message)
    }
    catch {
        return $false
    }
    return $answer -match "^(?i:y|yes)$"
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force
    return ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
}

function Invoke-ManagedPython {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$Capture
    )

    $managedRoot = Join-Path $HomePath ".deeppattern\decision-engine"
    if (-not (Test-Path -LiteralPath $managedRoot -PathType Container) -or
        (Test-ReparsePoint -Path $managedRoot)) {
        return $null
    }
    Push-Location -LiteralPath $managedRoot
    try {
        return Invoke-Clean -FilePath $PythonPath -ArgumentList $Arguments -Capture:$Capture
    }
    finally {
        Pop-Location
    }
}

function Get-LiveManagedLeasePids {
    $managedRoot = Join-Path $HomePath ".deeppattern\decision-engine"
    $script = @'
from pathlib import Path
import sys
from installer import update_coordination

root = Path(sys.argv[1])
for session in update_coordination.live_shim_sessions(root):
    if session.pid is not None:
        print(session.pid)
'@
    $probe = Invoke-ManagedPython -Arguments @("-c", $script, $managedRoot) -Capture
    if ($null -eq $probe -or $probe.ExitCode -ne 0) {
        return @()
    }
    return @(
        $probe.Output |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { $_ -match "^[0-9]+$" } |
            ForEach-Object { [int]$_ } |
            Select-Object -Unique
    )
}

function Test-ManagedLeasePid {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return (Get-LiveManagedLeasePids) -contains $ProcessId
}

function Get-ProcessSnapshot {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    try {
        $process = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop
        if ($null -eq $process) {
            return $null
        }
        $ownerResult = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
        if ($ownerResult.ReturnValue -ne 0) {
            return $null
        }
        $owner = if ([string]::IsNullOrWhiteSpace([string]$ownerResult.Domain)) {
            [string]$ownerResult.User
        }
        else {
            "{0}\{1}" -f $ownerResult.Domain, $ownerResult.User
        }
        $started = if ($process.CreationDate -is [datetime]) {
            $process.CreationDate.ToUniversalTime().ToString("o")
        }
        else {
            [string]$process.CreationDate
        }
        return [pscustomobject]@{
            ProcessId = [int]$process.ProcessId
            ParentProcessId = [int]$process.ParentProcessId
            Name = [string]$process.Name
            ExecutablePath = [string]$process.ExecutablePath
            CommandLine = [string]$process.CommandLine
            Owner = $owner
            Started = $started
        }
    }
    catch {
        return $null
    }
}

function Test-ManagedLauncherSnapshot {
    param([Parameter(Mandatory = $true)]$Snapshot)

    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    if (-not [string]::Equals($Snapshot.Owner, $currentIdentity, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    if ([string]::IsNullOrWhiteSpace($Snapshot.ExecutablePath) -or
        [string]::IsNullOrWhiteSpace($Snapshot.CommandLine)) {
        return $false
    }
    $executableName = [IO.Path]::GetFileName($Snapshot.ExecutablePath)
    if ($executableName -notmatch "^(?i:python(?:w)?(?:[0-9.]*)?\.exe)$") {
        return $false
    }
    return $Snapshot.CommandLine -match "(?i:installer\.(?:launcher|shim)|mcp_bootstrap\.py)"
}

function Get-LikelyHostForProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $current = $ProcessId
    for ($depth = 0; $depth -lt 8 -and $current -gt 0; $depth++) {
        $snapshot = Get-ProcessSnapshot -ProcessId $current
        if ($null -eq $snapshot) {
            break
        }
        switch -Regex ($snapshot.Name) {
            "^(?i:claude\.exe)$" { return "Claude Desktop" }
            "^(?i:chatgpt\.exe)$" { return "Codex (ChatGPT)" }
            "^(?i:codebuddy.*\.exe)$" { return "CodeBuddy" }
            "^(?i:codex.*\.exe)$" { return "Codex" }
            "^(?i:cursor.*\.exe)$" { return "Cursor" }
            "^(?i:qoder.*\.exe)$" { return "Qoder" }
            "^(?i:trae.*\.exe)$" { return "TRAE" }
            "^(?i:workbuddy.*\.exe)$" { return "WorkBuddy" }
        }
        $identityText = ("{0} {1} {2}" -f $snapshot.Name, $snapshot.ExecutablePath, $snapshot.CommandLine)
        switch -Regex ($identityText) {
            "(?i:ChatGPT)" { return "Codex (ChatGPT)" }
            "(?i:Claude)" { return "Claude Desktop" }
            "(?i:CodeBuddy)" { return "CodeBuddy" }
            "(?i:Cursor)" { return "Cursor" }
            "(?i:Qoder)" { return "Qoder" }
            "(?i:TRAE)" { return "TRAE" }
            "(?i:WorkBuddy)" { return "WorkBuddy" }
        }
        $current = $snapshot.ParentProcessId
    }
    return "Unknown Agent"
}

function Test-SameProcessSnapshot {
    param(
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)]$Actual
    )

    return (
        $Expected.ProcessId -eq $Actual.ProcessId -and
        $Expected.ParentProcessId -eq $Actual.ParentProcessId -and
        $Expected.Started -eq $Actual.Started -and
        $Expected.ExecutablePath -eq $Actual.ExecutablePath -and
        $Expected.CommandLine -eq $Actual.CommandLine -and
        $Expected.Owner -eq $Actual.Owner
    )
}

function Test-ManagedCommandReference {
    param([Parameter(Mandatory = $true)]$Snapshot)

    if ([string]::IsNullOrWhiteSpace($Snapshot.CommandLine)) { return $false }
    $command = $Snapshot.CommandLine.Replace("\", "/").ToLowerInvariant()
    foreach ($root in @(
        (Join-Path $HomePath ".deeppattern\decision-engine"),
        (Join-Path $HomePath ".deeppattern\agent-quality-gates"),
        (Join-Path $HomePath ".deeppattern\de-python")
    )) {
        if ($command.Contains($root.Replace("\", "/").ToLowerInvariant())) {
            return $true
        }
    }
    return $false
}

function Get-ManagedProcessCandidatePids {
    $ids = New-Object System.Collections.Generic.HashSet[int]
    foreach ($processId in @(Get-LiveManagedLeasePids)) {
        $null = $ids.Add([int]$processId)
    }
    try {
        foreach ($process in @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)) {
            if ([string]::IsNullOrWhiteSpace([string]$process.CommandLine) -or
                [string]$process.CommandLine -notmatch "(?i:installer\.(?:launcher|shim)|mcp_bootstrap\.py)") {
                continue
            }
            $snapshot = Get-ProcessSnapshot -ProcessId ([int]$process.ProcessId)
            if ($null -ne $snapshot -and
                (Test-ManagedLauncherSnapshot -Snapshot $snapshot) -and
                (Test-ManagedCommandReference -Snapshot $snapshot)) {
                $null = $ids.Add([int]$snapshot.ProcessId)
            }
        }
    }
    catch {
        # Lease-backed PIDs remain usable if the broad inventory cannot be read.
    }
    return @($ids | Sort-Object)
}

function Get-VisibleLauncherSnapshots {
    $snapshots = New-Object System.Collections.Generic.List[object]
    try {
        foreach ($process in @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)) {
            if ([string]::IsNullOrWhiteSpace([string]$process.CommandLine) -or
                [string]$process.CommandLine -notmatch "(?i:installer\.(?:launcher|shim)|mcp_bootstrap\.py)") {
                continue
            }
            $snapshot = Get-ProcessSnapshot -ProcessId ([int]$process.ProcessId)
            if ($null -ne $snapshot -and (Test-ManagedLauncherSnapshot -Snapshot $snapshot)) {
                $snapshots.Add($snapshot)
            }
        }
    }
    catch {
        return @()
    }
    # Windows PowerShell 5.1 can throw "Argument types do not match" when its
    # dynamic binder applies @() directly to a generic List[object].
    return $snapshots.ToArray()
}

function Write-ManagedProcessSummary {
    param([Parameter(Mandatory = $true)]$Snapshots)

    Write-Host "ACTIVE AGENT SESSIONS:"
    $groups = $Snapshots | Group-Object { Get-LikelyHostForProcess -ProcessId $_.ProcessId }
    foreach ($group in $groups) {
        $hostName = [string]$group.Name
        if ($hostName -eq "Unknown Agent") {
            Write-Host "  Unknown Agent (host could not be identified):"
        }
        else {
            Write-Host ("  {0}:" -f $hostName)
        }
        Write-Host ("    {0} active MCP session(s)" -f $group.Count)
        foreach ($snapshot in $group.Group) {
            Write-Host ("    PID={0} PPID={1} executable={2}" -f
                $snapshot.ProcessId, $snapshot.ParentProcessId, $snapshot.ExecutablePath)
            if ($hostName -eq "Unknown Agent") {
                $ancestorId = $snapshot.ParentProcessId
                $visited = New-Object System.Collections.Generic.HashSet[int]
                for ($depth = 0; $depth -lt 5 -and $ancestorId -gt 0; $depth++) {
                    if (-not $visited.Add([int]$ancestorId)) { break }
                    $ancestor = Get-ProcessSnapshot -ProcessId $ancestorId
                    if ($null -eq $ancestor) { break }
                    Write-Host ("    ancestor={0} executable={1} (PID={2})" -f
                        $ancestor.Name, $ancestor.ExecutablePath, $ancestor.ProcessId)
                    $ancestorId = $ancestor.ParentProcessId
                }
            }
        }
    }
}

function Get-ManagedTerminationOrder {
    param([Parameter(Mandatory = $true)]$Snapshots)

    $byPid = @{}
    foreach ($snapshot in $Snapshots) {
        $byPid[[int]$snapshot.ProcessId] = $snapshot
    }
    $ranked = foreach ($snapshot in $Snapshots) {
        $depth = 0
        $parentId = [int]$snapshot.ParentProcessId
        $visited = @{}
        while ($parentId -gt 0 -and $byPid.ContainsKey($parentId) -and -not $visited.ContainsKey($parentId)) {
            $visited[$parentId] = $true
            $depth++
            $parentId = [int]$byPid[$parentId].ParentProcessId
        }
        [pscustomobject]@{
            Depth = $depth
            Snapshot = $snapshot
        }
    }
    return @(
        $ranked |
            Sort-Object -Property `
                @{ Expression = { $_.Depth }; Descending = $true }, `
                @{ Expression = { $_.Snapshot.ProcessId }; Descending = $false } |
            ForEach-Object { $_.Snapshot }
    )
}

function Resolve-ActiveManagedSessions {
    $processIds = @(Get-ManagedProcessCandidatePids)
    $visible = @(Get-VisibleLauncherSnapshots)
    if ($visible.Count -gt 0) {
        Write-ManagedProcessSummary -Snapshots $visible
    }
    if ($processIds.Count -eq 0) {
        Write-Host "Live DE/AQG processes were reported, but no process identity was safe to terminate automatically."
        return $false
    }

    $frozen = New-Object System.Collections.Generic.List[object]
    foreach ($processId in $processIds) {
        $snapshot = Get-ProcessSnapshot -ProcessId $processId
        if ($null -ne $snapshot -and
            (Test-ManagedLauncherSnapshot -Snapshot $snapshot) -and
            ((Test-ManagedLeasePid -ProcessId $processId) -or
             (Test-ManagedCommandReference -Snapshot $snapshot))) {
            $frozen.Add($snapshot)
        }
    }
    if ($frozen.Count -eq 0) {
        Write-Host "No active process is eligible for managed termination. Close the identified Agent application and rerun the uninstaller."
        return $false
    }

    if ($visible.Count -eq 0) {
        Write-ManagedProcessSummary -Snapshots $frozen
    }
    Write-Host "The listed processes are verified DE/AQG child processes. Agent applications themselves will not be closed."
    if (-not (Confirm-UserAction -Message ("Terminate {0} verified Deep Pattern process(es) now?" -f $frozen.Count))) {
        Write-Host "UNINSTALL_PENDING: process termination was declined; no files or settings were changed."
        return $false
    }
    $terminationOrder = @(Get-ManagedTerminationOrder -Snapshots $frozen.ToArray())
    foreach ($snapshot in $terminationOrder) {
        $current = Get-ProcessSnapshot -ProcessId $snapshot.ProcessId
        if ($null -eq $current) {
            Write-Host ("Verified Deep Pattern process pid={0} already exited." -f $snapshot.ProcessId)
            continue
        }
        if (-not (Test-SameProcessSnapshot -Expected $snapshot -Actual $current) -or
            -not (Test-ManagedLauncherSnapshot -Snapshot $current) -or
            (-not (Test-ManagedLeasePid -ProcessId $snapshot.ProcessId) -and
             -not (Test-ManagedCommandReference -Snapshot $current))) {
            Write-Host ("BLOCKED PROCESS pid={0} termination refused because its identity changed or ownership could not be re-proven." -f $snapshot.ProcessId)
            return $false
        }
        try {
            Stop-Process -Id $snapshot.ProcessId -Force -ErrorAction Stop
            Write-Host ("Terminated verified Deep Pattern process pid={0}." -f $snapshot.ProcessId)
        }
        catch {
            Write-Host ("BLOCKED PROCESS pid={0} termination failed: {1}" -f $snapshot.ProcessId, $_.Exception.GetType().Name)
            return $false
        }
    }
    Start-Sleep -Milliseconds 500
    return $true
}

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    Stop-Uninstall "This entrypoint supports native Windows only."
}

Write-Host "Preparing the verified Deep Pattern uninstall environment..."
$PythonPath = Resolve-Python
$GitPath = Resolve-Git
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("dp-uninstall-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
$helperPath = Join-Path $temporaryRoot "dp_windows_uninstall.py"
$processInventoryPath = Join-Path $temporaryRoot "process-inventory.json"

$helperSource = @'
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 3
EXIT_PROCESS_BLOCKED = 5
SERVER_NAME = "decision-engine"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_UNINSTALL_BACKUPS = 5
GIT_EXE = ""
PROCESS_INVENTORY_PATH: Path | None = None
PROCESS_INVENTORY_SHA256 = ""
GUIDABLE_PROCESS_BLOCKER_PREFIXES = (
    "live managed DE MCP process must be stopped before uninstall:",
    "live DE launcher process ownership is unknown:",
)
HOOK_MARKERS = (
    "decision-engine-audit-routing-v1",
    "decision-engine-audit-routing-experiment",
    "decision-engine-workbuddy-ai-audit-routing-v1",
    "decision-engine-trae-cn-audit-routing-v1",
)
HOOK_SCRIPTS = (
    "qoder_audit_prompt_hook.py",
    "workbuddy_audit_prompt_hook.py",
    "trae_cn_audit_prompt_hook.py",
)
ORPHAN_DE_SKILLS = frozenset(
    {
        "audit",
        "audit-adjudication",
        "audit-brainstorming",
        "audit-explore",
        "audit-forecast",
        "audit-market-research",
        "audit-writing-plans",
        "discussion-board",
        "graphic-explanation",
        "layer-check",
    }
)
ORPHAN_LAUNCHER_MARKERS = (
    "installer.launcher",
    "installer/shim",
    "installer\\shim",
    "mcp_bootstrap.py",
)
DE_PRODUCT_REMOTES = {
    "github": "https://github.com/deeppatternai/decision-engine.git",
    "gitee": "https://gitee.com/deeppatternai/decision-engine.git",
}
AQG_PRODUCT_REMOTE = "https://github.com/deeppatternai/agent-quality-gates.git"
UNINSTALL_BACKUP_NAME = re.compile(
    r"(?P<stamp>[0-9]{8}-[0-9]{6})(?:-(?P<suffix>[0-9]+))?"
    r"-dp-uninstall-(?P<scope>de|aqg|both)"
)


def known_windows_config_paths(home: Path) -> tuple[Path, ...]:
    appdata = Path(os.getenv("APPDATA") or home / "AppData" / "Roaming")
    return (
        home / ".claude.json",
        appdata / "Claude" / "claude_desktop_config.json",
        home / ".codex" / "config.toml",
        home / ".cursor" / "mcp.json",
        home / ".codebuddy" / "mcp.json",
        home / ".qoder" / "mcp.json",
        home / ".qoder-cn" / "settings.json",
        appdata / "TRAE SOLO" / "User" / "mcp.json",
        appdata / "TRAE SOLO CN" / "User" / "mcp.json",
        home / ".workbuddy" / "mcp.json",
    )


def known_windows_skill_roots(home: Path) -> tuple[Path, ...]:
    return (
        home / ".claude" / "skills",
        home / ".codex" / "skills",
        home / ".cursor" / "skills",
        home / ".codebuddy" / "skills",
        home / ".qoder" / "skills",
        home / ".qoder-cn" / "skills",
        home / ".trae" / "skills",
        home / ".trae-cn" / "skills",
        home / ".workbuddy" / "skills",
    )


def _strip_windows_device_prefix(value: str) -> str:
    """Make Win32 device-form paths comparable to ordinary drive paths."""
    if os.name != "nt":
        return value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\") or value.startswith("\\??\\"):
        return value[4:]
    return value


def lex(path: Path) -> Path:
    value = _strip_windows_device_prefix(os.path.expanduser(str(path)))
    return Path(os.path.abspath(value))


def lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def is_under(path: Path, parent: Path) -> bool:
    try:
        lex(path).relative_to(lex(parent))
        return True
    except ValueError:
        return False


def is_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def reject_reparse_components(path: Path, floor: Path) -> Path | None:
    path = lex(path)
    floor = lex(floor)
    try:
        relative = path.relative_to(floor)
    except ValueError:
        return path
    current = floor
    for part in relative.parts:
        current = current / part
        if is_reparse(current):
            return current
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def windows_process_rows() -> tuple[dict[str, Any], ...]:
    if PROCESS_INVENTORY_PATH is None:
        raise RuntimeError("PowerShell process inventory path is unavailable")
    before = PROCESS_INVENTORY_PATH.lstat()
    if is_reparse(PROCESS_INVENTORY_PATH) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError("PowerShell process inventory is not a regular file")
    if before.st_size > MAX_CONFIG_BYTES:
        raise RuntimeError("PowerShell process inventory is too large")
    payload = PROCESS_INVENTORY_PATH.read_bytes()
    after = PROCESS_INVENTORY_PATH.lstat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise RuntimeError("PowerShell process inventory changed while being read")
    if hashlib.sha256(payload).hexdigest() != PROCESS_INVENTORY_SHA256:
        raise RuntimeError("PowerShell process inventory digest mismatch")
    decoded = json.loads(payload.decode("utf-8"))
    if isinstance(decoded, dict):
        decoded = [decoded]
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise RuntimeError("PowerShell process inventory has an unexpected shape")
    return tuple(decoded)


def looks_like_de_launcher(command_line: str) -> bool:
    normalized = command_line.lower().replace("\\", "/")
    return (
        "-m installer.launcher" in normalized
        or "-m installer.shim" in normalized
        or "installer/mcp_bootstrap.py" in normalized
        or (" -c " in normalized and " --managed-root " in normalized)
    )


def git_output(root: Path, *args: str) -> str:
    if not GIT_EXE:
        raise ValueError("trusted Git for Windows is unavailable")
    completed = subprocess.run(
        [GIT_EXE, "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("git inspection failed")
    return completed.stdout.strip()


def checkout_is_clean(root: Path) -> bool:
    try:
        return not git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def de_checkout_has_product_remotes(root: Path) -> bool:
    try:
        names = tuple(line for line in git_output(root, "remote").splitlines() if line)
        if set(names) != set(DE_PRODUCT_REMOTES):
            return False
        return all(
            git_output(root, "remote", "get-url", name) == url
            for name, url in DE_PRODUCT_REMOTES.items()
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def aqg_checkout_has_product_remote(root: Path) -> bool:
    try:
        return git_output(root, "remote", "get-url", "origin") == AQG_PRODUCT_REMOTE
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def aqg_managed_target_name_matches(root: Path, head: str) -> bool:
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        return False
    if re.fullmatch(r"[0-9a-f]{40}", root.name):
        return root.name == head
    if re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", root.name) is None:
        return False
    try:
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return False
    def release(value):
        return len(value) <= 40 and re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", value
        ) is not None
    reissue = f"{version}-{head[:12]}"
    bases = {version if release(version) else head, reissue if release(reissue) else head}
    legacy = root.name == version and re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?", version)
    return bool(legacy or root.name in bases or any(
        re.fullmatch(re.escape(base[:23]) + r"-[0-9a-f]{16}", root.name) for base in bases
    ))


def json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def read_json(path: Path) -> dict[str, Any]:
    before = path.lstat()
    if is_reparse(path) or not stat.S_ISREG(before.st_mode):
        raise ValueError("configuration is not a regular file")
    if before.st_size > MAX_CONFIG_BYTES:
        raise ValueError("configuration is too large")
    data = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=json_object_without_duplicates,
    )
    if not isinstance(data, dict):
        raise ValueError("configuration root is not an object")
    return data


def _orphan_path_present(value: str, managed_root: Path) -> bool:
    normalized = value.replace("\\", "/").lower()
    root = str(lex(managed_root)).replace("\\", "/").lower().rstrip("/")
    if not root or root not in normalized:
        return False
    suffix = normalized.split(root, 1)[1]
    return not suffix or suffix[0] in "/\\\"' :;,)]}"


def _orphan_entry_strings(entry: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    command = entry.get("command")
    if isinstance(command, str):
        values.append(command)
    args = entry.get("args")
    if isinstance(args, list) and all(isinstance(item, str) for item in args):
        values.extend(args)
    cwd = entry.get("cwd")
    if isinstance(cwd, str):
        values.append(cwd)
    env = entry.get("env")
    if isinstance(env, dict):
        for value in env.values():
            if isinstance(value, str):
                values.append(value)
    return tuple(values)


def orphan_mcp_entry_is_owned(entry: Any, managed_root: Path) -> bool:
    """Prove a server entry belongs to a removed DE root without using its name alone."""
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        return False
    values = _orphan_entry_strings(entry)
    if not any(_orphan_path_present(value, managed_root) for value in values):
        return False
    launch_text = " ".join(values).lower().replace("\\", "/")
    return any(marker.replace("\\", "/") in launch_text for marker in ORPHAN_LAUNCHER_MARKERS)


def orphan_json_entry(path: Path, managed_root: Path) -> bool:
    data = read_json(path)
    servers = data.get("mcpServers")
    return isinstance(servers, dict) and orphan_mcp_entry_is_owned(
        servers.get(SERVER_NAME), managed_root
    )


def orphan_toml_entry(path: Path, managed_root: Path) -> bool:
    try:
        import tomllib
    except ImportError:
        return False
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    servers = data.get("mcp_servers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and orphan_mcp_entry_is_owned(
        servers.get(SERVER_NAME), managed_root
    )


def orphan_entry_state(path: Path, managed_root: Path) -> bool | None:
    """Return owned/unowned for a real DE MCP entry, or None when absent."""
    if path.suffix.lower() == ".toml":
        try:
            import tomllib
        except ImportError:
            raise ValueError("TOML parser is unavailable")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcp_servers") if isinstance(data, dict) else None
    else:
        data = read_json(path)
        servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return None
    return orphan_mcp_entry_is_owned(servers[SERVER_NAME], managed_root)


def atomic_write(path: Path, text: str) -> None:
    link = reject_reparse_components(path.parent, Path.home())
    if link is not None:
        raise ValueError("configuration parent contains a reparse point")
    fd, name = tempfile.mkstemp(prefix=".%s.dp-uninstall-" % path.name, dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def hook_is_owned(item: Any, managed_root: Path) -> bool:
    if not isinstance(item, dict):
        return False
    command = item.get("command")
    if not isinstance(command, str):
        return False
    normalized = command.replace("\\", "/")
    root = str(managed_root).replace("\\", "/")
    return (
        root in normalized
        and any(marker in normalized for marker in HOOK_MARKERS + HOOK_SCRIPTS)
    )


def remove_owned_hooks(data: dict[str, Any], managed_root: Path) -> bool:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        next_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                next_groups.append(group)
                continue
            old_items = group["hooks"]
            new_items = [item for item in old_items if not hook_is_owned(item, managed_root)]
            if len(new_items) != len(old_items):
                changed = True
                if new_items:
                    replacement = dict(group)
                    replacement["hooks"] = new_items
                    next_groups.append(replacement)
            else:
                next_groups.append(group)
        if next_groups:
            hooks[event] = next_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    return changed


def toml_without_server(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    header = re.compile(r'^\s*\[\s*mcp_servers\.decision-engine(?:\.|\s*\])')
    any_table = re.compile(r'^\s*\[')
    output: list[str] = []
    removing = False
    changed = False
    for line in lines:
        if header.match(line):
            removing = True
            changed = True
            continue
        if removing and any_table.match(line):
            removing = False
        if not removing:
            output.append(line)
    return "".join(output), changed


def safe_managed_directory(path: Path, home: Path) -> bool:
    return (
        path.is_dir()
        and not is_reparse(path)
        and reject_reparse_components(path, home) is None
    )


def marker_has_schema(path: Path, marker_name: str) -> bool:
    marker = path / marker_name
    if not marker.is_file() or is_reparse(marker):
        return False
    try:
        return "schema=1" in marker.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False


def managed_runtime_path_owned(path: Path, dp: Path, home: Path) -> bool:
    if not safe_managed_directory(path, home):
        return False
    if path == dp / "de-python":
        return marker_has_schema(path, ".deeppattern-python-env")
    if path == dp / "runtimes":
        try:
            children = tuple(path.iterdir())
        except OSError:
            return False
        return all(
            safe_managed_directory(child, home)
            and marker_has_schema(child, ".deeppattern-python-runtime")
            for child in children
        )
    if path == dp / "runtime-backups":
        try:
            children = tuple(path.iterdir())
        except OSError:
            return False
        return all(
            safe_managed_directory(child, home)
            and (
                marker_has_schema(child, ".deeppattern-python-runtime")
                or marker_has_schema(child, ".deeppattern-python-env")
            )
            for child in children
        )
    return False


def managed_uninstall_backups(home: Path, dp: Path) -> list[Path]:
    base = dp / "uninstall-backups"
    if not lexists(base):
        return []
    if not safe_managed_directory(base, home):
        raise RuntimeError("uninstall backup root is not a safe directory: %s" % base)
    roots: list[tuple[str, int, str, Path]] = []
    for child in base.iterdir():
        match = UNINSTALL_BACKUP_NAME.fullmatch(child.name)
        if match is None or not safe_managed_directory(child, home):
            continue
        manifest = child / "manifest.json"
        if not manifest.is_file() or is_reparse(manifest):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            data.get("schema") != 1
            or data.get("platform") != "win32"
            or data.get("scope") != match.group("scope")
            or not isinstance(data.get("items"), list)
        ):
            continue
        recorded_home = data.get("home")
        if recorded_home is not None and path_key(Path(recorded_home)) != path_key(home):
            continue
        roots.append((match.group("stamp"), int(match.group("suffix") or 0), child.name, child))
    return [item[3] for item in sorted(roots)]


def prune_uninstall_backups(home: Path, dp: Path, current: Path | None = None) -> int:
    roots = managed_uninstall_backups(home, dp)
    if current is not None and current not in roots:
        raise RuntimeError("current uninstall backup cannot be revalidated: %s" % current)
    obsolete = roots[:-MAX_UNINSTALL_BACKUPS]
    for path in obsolete:
        shutil.rmtree(path)
    if obsolete:
        print(
            "PRUNED %d old uninstall backup(s); retained latest %d"
            % (len(obsolete), MAX_UNINSTALL_BACKUPS)
        )
    return len(obsolete)


@dataclass
class Action:
    kind: str
    path: Path
    detail: str
    client: str | None = None
    expected_sha256: str | None = None


@dataclass
class Inventory:
    home: Path
    scope: str
    actions: list[Action] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    de_root: Path = field(init=False)
    aqg_root: Path = field(init=False)
    source_root: Path = field(init=False)
    aqg_target: Path | None = None
    aqg_uninstaller: Path | None = None
    de_root_proven: bool = False

    def __post_init__(self) -> None:
        self.home = lex(self.home)
        self.dp = self.home / ".deeppattern"
        self.de_root = self.dp / "decision-engine"
        self.aqg_root = self.dp / "agent-quality-gates"
        self.source_root = self.dp / "decision-engine-root"

    def add(self, kind: str, path: Path, detail: str, **kw: Any) -> None:
        key = (kind, path_key(path), kw.get("client"))
        if any((a.kind, path_key(a.path), a.client) == key for a in self.actions):
            return
        self.actions.append(Action(kind, lex(path), detail, **kw))

    def inspect_processes(self) -> None:
        if self.scope not in ("de", "both"):
            return
        if not self.de_root_proven:
            try:
                rows = windows_process_rows()
            except Exception as exc:
                self.blockers.append(
                    "running DE MCP sessions could not be inspected: %s"
                    % type(exc).__name__
                )
                return
            root_markers = tuple(
                str(root).lower().replace("\\", "/")
                for root in (self.de_root, self.aqg_root)
            )
            for row in rows:
                raw_pid = row.get("ProcessId")
                command_line = row.get("CommandLine")
                if not isinstance(raw_pid, int) or not isinstance(command_line, str):
                    continue
                if not looks_like_de_launcher(command_line):
                    continue
                normalized = command_line.lower().replace("\\", "/")
                if any(marker in normalized for marker in root_markers):
                    self.blockers.append(
                        "live managed DE MCP process must be stopped before uninstall: pid=%s"
                        % raw_pid
                    )
                else:
                    self.blockers.append(
                        "live DE launcher process ownership is unknown: pid=%s" % raw_pid
                    )
            return
        try:
            sys.path.insert(0, str(self.de_root))
            from installer import update_coordination

            sessions = update_coordination.live_shim_sessions(self.de_root)
            rows = windows_process_rows()
        except Exception as exc:
            self.blockers.append(
                "running managed MCP sessions could not be inspected: %s"
                % type(exc).__name__
            )
            return
        live_pids = {session.pid for session in sessions if session.pid is not None}
        reported_pids: set[int] = set()
        root_markers = tuple(
            str(root).lower().replace("\\", "/")
            for root in (self.de_root, self.aqg_root)
        )
        for row in rows:
            raw_pid = row.get("ProcessId")
            command_line = row.get("CommandLine")
            if not isinstance(raw_pid, int) or not isinstance(command_line, str):
                continue
            if not looks_like_de_launcher(command_line):
                continue
            normalized = command_line.lower().replace("\\", "/")
            if raw_pid in live_pids or any(marker in normalized for marker in root_markers):
                self.blockers.append(
                    "live managed DE MCP process must be stopped before uninstall: pid=%s"
                    % raw_pid
                )
            else:
                self.blockers.append(
                    "live DE launcher process ownership is unknown: pid=%s" % raw_pid
                )
            reported_pids.add(raw_pid)
        for session in sessions:
            if session.pid is None:
                self.blockers.append("live managed DE MCP session has no provable process id")
            elif session.pid not in reported_pids:
                self.blockers.append(
                    "live managed DE MCP process must be stopped before uninstall: pid=%s"
                    % session.pid
                )

    def inspect_root(self, root: Path, component: str) -> None:
        if not lexists(root):
            self.notes.append("ABSENT %s" % root)
            return
        if is_reparse(root):
            if component != "aqg":
                self.blockers.append("%s root is a reparse point: %s" % (component, root))
                return
            try:
                raw = os.readlink(root)
                target = Path(raw)
                if not target.is_absolute():
                    target = root.parent / target
                target = lex(target)
            except OSError:
                self.blockers.append("AQG root reparse target cannot be read: %s" % root)
                return
            versions = self.dp / "versions"
            if not is_under(target, versions) or is_reparse(target):
                self.blockers.append("AQG root reparse target is outside managed versions: %s" % root)
                return
            required = (target / "scripts" / "install_aqg_clients.py", target / "VERSION", target / "requirements.txt")
            if not all(path.is_file() and not is_reparse(path) for path in required):
                self.blockers.append("AQG managed target has unexpected layout: %s" % target)
                return
            try:
                target_head = git_output(target, "rev-parse", "HEAD")
            except (OSError, subprocess.SubprocessError, ValueError):
                target_head = ""
            if (
                not aqg_checkout_has_product_remote(target)
                or not checkout_is_clean(target)
                or not aqg_managed_target_name_matches(target, target_head)
            ):
                self.blockers.append("AQG managed target source identity is unknown: %s" % target)
                return
            self.aqg_target = target
            self.aqg_uninstaller = required[0]
            self.add("quarantine-root-link", root, "AQG managed root link")
            self.add("quarantine-root", target, "AQG managed version target")
            return
        if not root.is_dir():
            self.blockers.append("%s root is not a directory: %s" % (component, root))
            return
        if reject_reparse_components(root, self.home) is not None:
            self.blockers.append("%s root has a reparse path component: %s" % (component, root))
            return
        if component == "de":
            required = (root / ".git", root / "installer", root / "skills", root / "pyproject.toml", root / "VERSION")
        else:
            required = (root / ".git", root / "scripts" / "install_aqg_clients.py", root / "VERSION", root / "requirements.txt")
        if not all(path.exists() and not is_reparse(path) for path in required):
            self.blockers.append("%s root has unexpected layout; ownership is unknown: %s" % (component, root))
            return
        if component == "de" and (
            not de_checkout_has_product_remotes(root) or not checkout_is_clean(root)
        ):
            self.blockers.append("DE managed checkout source identity is unknown or dirty: %s" % root)
            return
        if component == "aqg" and (
            not aqg_checkout_has_product_remote(root) or not checkout_is_clean(root)
        ):
            self.blockers.append("AQG checkout source identity is unknown or dirty: %s" % root)
            return
        if component == "aqg":
            self.aqg_uninstaller = root / "scripts" / "install_aqg_clients.py"
        else:
            self.de_root_proven = True
        self.add("quarantine-root", root, component)

    def import_de(self):
        if not self.de_root.is_dir() or is_reparse(self.de_root):
            return None
        sys.path.insert(0, str(self.de_root))
        try:
            from installer import client_host_ownership, managed_install, mcp_config
            return client_host_ownership, managed_install, mcp_config
        except Exception as exc:
            self.blockers.append("Decision Engine ownership modules cannot be loaded: %s" % type(exc).__name__)
            return None

    def inspect_de_integrations(self) -> None:
        if not self.de_root_proven:
            self.inspect_unprovable_residue()
            return
        modules = self.import_de()
        if modules is None:
            self.inspect_unprovable_residue()
            return
        client_host_ownership, managed_install, mcp_config = modules
        inspected_paths: set[str] = set()
        record_paths: set[str] = set()
        try:
            clients = tuple(mcp_config.CLIENTS)
        except Exception:
            self.blockers.append("Decision Engine host registry cannot be read")
            return
        for client in clients:
            try:
                path = lex(mcp_config.agent_config_path(client))
                actual = mcp_config.read_entry(client)
            except Exception as exc:
                self.blockers.append("%s MCP configuration cannot be inspected: %s" % (client, type(exc).__name__))
                continue
            if actual is None:
                continue
            if not isinstance(actual, dict):
                self.blockers.append("%s decision-engine entry is not an object" % client)
                continue
            owned = False
            spec = mcp_config.CLIENT_SPECS[client]
            try:
                record = client_host_ownership.read_record_if_present(
                    spec.host_family,
                    managed_root=self.de_root,
                    config_path=path,
                    server_name=SERVER_NAME,
                )
                if record is not None:
                    actual_hash = client_host_ownership.managed_entry_sha256_v1(actual)
                    owned = actual_hash == record.managed_fields_sha256
                    record_path = client_host_ownership.ownership_record_path(spec.host_family)
                    if path_key(record_path) not in record_paths:
                        self.add(
                            "quarantine-record",
                            record_path,
                            "owned DE host record",
                            client=client,
                            expected_sha256=sha256_file(record_path),
                        )
                        record_paths.add(path_key(record_path))
            except Exception:
                owned = False
            if not owned:
                try:
                    desired = mcp_config._render_client_entry(
                        client,
                        python=actual.get("command"),
                        cwd=self.de_root,
                    )
                    owned = (
                        client_host_ownership.managed_entry_sha256_v1(actual)
                        == client_host_ownership.managed_entry_sha256_v1(desired)
                    )
                except Exception:
                    owned = False
            if not owned:
                self.blockers.append("%s %s entry ownership is unknown: %s" % (client, SERVER_NAME, path))
                continue
            if path_key(path) not in inspected_paths:
                if not path.is_file() or is_reparse(path):
                    self.blockers.append("owned host configuration is not a regular file: %s" % path)
                    continue
                kind = "edit-toml" if path.suffix.lower() == ".toml" else "edit-json"
                self.add(
                    kind,
                    path,
                    "remove owned Decision Engine MCP entry and hook",
                    client=client,
                    expected_sha256=sha256_file(path),
                )
                inspected_paths.add(path_key(path))

        for path in known_windows_config_paths(self.home):
            if path_key(path) in inspected_paths or not path.is_file() or is_reparse(path):
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            normalized = raw.replace("\\", "/")
            root_text = str(self.de_root).replace("\\", "/")
            if root_text not in normalized or not any(
                marker in normalized for marker in HOOK_MARKERS + HOOK_SCRIPTS
            ):
                continue
            try:
                data = read_json(path)
                probe = json.loads(json.dumps(data))
            except (OSError, UnicodeError, ValueError, TypeError):
                self.blockers.append("DE hook configuration cannot be inspected safely: %s" % path)
                continue
            if not remove_owned_hooks(probe, self.de_root):
                self.blockers.append("DE hook ownership is unknown: %s" % path)
                continue
            self.add(
                "edit-json",
                path,
                "remove owned Decision Engine hook",
                expected_sha256=sha256_file(path),
            )
            inspected_paths.add(path_key(path))

        skill_names = []
        skills_root = self.de_root / "skills"
        if skills_root.is_dir() and not is_reparse(skills_root):
            skill_names = [path.name for path in skills_root.iterdir() if path.is_dir() and not is_reparse(path)]
        destinations: dict[str, Path] = {}
        for spec in mcp_config.CLIENT_SPECS.values():
            if spec.skills_global_path is None:
                continue
            try:
                destination = lex(spec.skills_global_path())
            except Exception:
                continue
            destinations[path_key(destination)] = destination
        for destination in destinations.values():
            if reject_reparse_components(destination.parent, self.home) is not None:
                self.blockers.append("skill root parent contains a reparse point: %s" % destination)
                continue
            for name in skill_names:
                route = destination / name
                if not lexists(route):
                    continue
                if not is_reparse(route):
                    self.notes.append("PRESERVE foreign real skill directory %s" % route)
                    continue
                try:
                    target = Path(os.readlink(route))
                    if not target.is_absolute():
                        target = route.parent / target
                    target = lex(target)
                except OSError:
                    self.blockers.append("skill reparse target cannot be read: %s" % route)
                    continue
                expected = lex(skills_root / name)
                if path_key(target) != path_key(expected):
                    self.blockers.append("skill route points outside this install: %s" % route)
                    continue
                self.add("remove-skill-route", route, "owned DE skill junction")

        try:
            registration = managed_install.registration_path()
            if lexists(registration):
                if is_reparse(registration) or not registration.is_file():
                    self.blockers.append("managed registration is not a regular file: %s" % registration)
                else:
                    self.add(
                        "quarantine-record",
                        registration,
                        "owned DE managed registration",
                        expected_sha256=sha256_file(registration),
                    )
        except Exception as exc:
            self.blockers.append("managed registration cannot be inspected: %s" % type(exc).__name__)

    def inspect_unprovable_residue(self) -> None:
        found = False
        for path in known_windows_config_paths(self.home):
            if not path.is_file() or is_reparse(path):
                continue
            try:
                owned = orphan_entry_state(path, self.de_root)
                if owned is None:
                    continue
                if owned:
                    self.add(
                        "edit-toml" if path.suffix.lower() == ".toml" else "edit-json",
                        path,
                        "remove orphaned owned Decision Engine MCP entry and hook",
                        expected_sha256=sha256_file(path),
                    )
                    found = True
                else:
                    self.blockers.append(
                        "Decision Engine root is unavailable, so host entry ownership cannot be proven: %s" % path
                    )
                    found = True
            except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
                self.blockers.append("host configuration cannot be inspected safely: %s" % path)
                found = True
        expected_skills = self.de_root / "skills"
        for skills_root in known_windows_skill_roots(self.home):
            if not skills_root.is_dir() or is_reparse(skills_root):
                continue
            try:
                entries = tuple(skills_root.iterdir())
            except OSError:
                self.blockers.append("skill root cannot be inspected safely: %s" % skills_root)
                found = True
                continue
            for route in entries:
                if not is_reparse(route):
                    continue
                try:
                    target = Path(os.readlink(route))
                    if not target.is_absolute():
                        target = route.parent / target
                    target = lex(target)
                except OSError:
                    self.blockers.append("skill reparse target cannot be read: %s" % route)
                    found = True
                    continue
                expected = lex(expected_skills / route.name)
                if path_key(target) == path_key(expected) and route.name in ORPHAN_DE_SKILLS:
                    self.add("remove-skill-route", route, "orphaned owned DE skill junction")
                    found = True
                elif path_key(target.parent) == path_key(expected_skills):
                    self.blockers.append(
                        "Decision Engine root is unavailable, so orphaned skill ownership requires recovery: %s" % route
                    )
                    found = True
        if not found:
            self.notes.append("PRESERVE no provable DE integration residue without a managed root")

    def proven_aqg_version_target(self, path: Path) -> bool:
        if not safe_managed_directory(path, self.home):
            return False
        required = (
            path / ".git",
            path / "scripts" / "install_aqg_clients.py",
            path / "VERSION",
            path / "requirements.txt",
        )
        if not all(item.exists() and not is_reparse(item) for item in required):
            return False
        try:
            head = git_output(path, "rev-parse", "HEAD")
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return (
            aqg_checkout_has_product_remote(path)
            and checkout_is_clean(path)
            and aqg_managed_target_name_matches(path, head)
        )

    def inspect_managed_state_cleanup(self) -> None:
        if self.scope in ("de", "both"):
            install_lock = self.dp / ".install.lock"
            if lexists(install_lock):
                if (
                    is_reparse(install_lock)
                    or not install_lock.is_file()
                    or reject_reparse_components(install_lock, self.home) is not None
                ):
                    self.blockers.append(
                        "DE install lock ownership cannot be proven: %s" % install_lock
                    )
                else:
                    self.add(
                        "quarantine-aux",
                        install_lock,
                        "DE install lock",
                        expected_sha256=sha256_file(install_lock),
                    )
            for path in (
                self.dp / "de-python",
                self.dp / "runtimes",
                self.dp / "runtime-backups",
            ):
                if not lexists(path):
                    continue
                if managed_runtime_path_owned(path, self.dp, self.home):
                    self.add("quarantine-aux", path, "owned Deep Pattern private Python state")
                else:
                    self.blockers.append(
                        "Deep Pattern runtime ownership cannot be proven: %s" % path
                    )
            installations = self.dp / "installations"
            for name in ("decision-engine.json", "decision-engine.identity.lock"):
                path = installations / name
                if not lexists(path):
                    continue
                if is_reparse(path) or not path.is_file():
                    self.blockers.append("DE installation record ownership is unknown: %s" % path)
                else:
                    self.add("quarantine-aux", path, "owned DE installation record")

        if self.scope in ("aqg", "both"):
            for path, detail in (
                (self.dp / "aqg-state", "AQG managed state"),
                (self.dp / "aqg-backups", "AQG legacy backup archive"),
            ):
                if not lexists(path):
                    continue
                if safe_managed_directory(path, self.home):
                    self.add("quarantine-aux", path, detail)
                else:
                    self.blockers.append("AQG auxiliary ownership cannot be proven: %s" % path)

            versions = self.dp / "versions"
            if lexists(versions):
                if not safe_managed_directory(versions, self.home):
                    self.blockers.append("AQG versions path is not a safe directory: %s" % versions)
                else:
                    for child in sorted(versions.iterdir(), key=lambda item: item.name.lower()):
                        if self.aqg_target is not None and path_key(child) == path_key(self.aqg_target):
                            continue
                        owned_backup = (
                            child.name == "aqg-backups"
                            and safe_managed_directory(child, self.home)
                        )
                        if owned_backup or self.proven_aqg_version_target(child):
                            self.add(
                                "quarantine-aqg-version",
                                child,
                                "unreferenced AQG managed version or backup residue",
                            )
                        else:
                            self.blockers.append(
                                "AQG versions contains unproven content that must be preserved: %s"
                                % child
                            )

        planned = {path_key(action.path) for action in self.actions}
        directories: list[Path] = []
        if self.scope in ("de", "both"):
            directories.extend((self.dp / "installations", self.dp / "popup-sessions"))
        if self.scope in ("aqg", "both"):
            directories.append(self.dp / "versions")
        for directory in directories:
            if not lexists(directory):
                continue
            if not safe_managed_directory(directory, self.home):
                self.blockers.append("managed state path is not a safe directory: %s" % directory)
                continue
            children = tuple(directory.iterdir())
            if children and not all(path_key(child) in planned for child in children):
                self.notes.append("PRESERVE non-empty managed state directory %s" % directory)
                continue
            self.add("remove-empty-dir", directory, "remove after managed contents are quarantined")

    def inspect_backup_retention(self) -> None:
        try:
            roots = managed_uninstall_backups(self.home, self.dp)
        except RuntimeError as exc:
            self.blockers.append(str(exc))
            return
        creates_backup = any(action.kind != "backup-retention" for action in self.actions)
        if len(roots) + int(creates_backup) > MAX_UNINSTALL_BACKUPS:
            self.add(
                "backup-retention",
                self.dp / "uninstall-backups",
                "retain latest %d managed backups after successful uninstall"
                % MAX_UNINSTALL_BACKUPS,
            )

    def inspect(self) -> None:
        if lexists(self.source_root):
            self.notes.append("PRESERVE protected source/worktree %s" % self.source_root)
        else:
            self.notes.append("ABSENT protected source/worktree %s" % self.source_root)
        if self.scope in ("de", "both"):
            self.inspect_root(self.de_root, "de")
            self.inspect_de_integrations()
            self.inspect_processes()
        if self.scope in ("aqg", "both"):
            self.inspect_root(self.aqg_root, "aqg")
            if lexists(self.aqg_root) and self.aqg_uninstaller is None:
                self.blockers.append("AQG official uninstaller ownership cannot be proven")
        self.inspect_managed_state_cleanup()
        self.inspect_backup_retention()


def has_only_guidable_process_blockers(blockers: Iterable[str]) -> bool:
    items = tuple(blockers)
    return bool(items) and all(
        any(item.startswith(prefix) for prefix in GUIDABLE_PROCESS_BLOCKER_PREFIXES)
        for item in items
    )


def print_inventory(inv: Inventory, apply: bool) -> None:
    print("Deep Pattern uninstall plan")
    print("platform=win32 scope=%s mode=%s" % (inv.scope, "APPLY" if apply else "DRY-RUN"))
    for action in inv.actions:
        if action.kind == "aqg-uninstall":
            print("RUN %s (%s)" % (action.path, action.detail))
        elif action.kind == "backup-retention":
            print("PRUNE %s (%s)" % (action.path, action.detail))
        else:
            print("REMOVE %s (%s)" % (action.path, action.detail))
    for note in inv.notes:
        print(note)
    for blocker in inv.blockers:
        print("BLOCKED %s" % blocker)
    if inv.blockers and apply and has_only_guidable_process_blockers(inv.blockers):
        print("ACTION REQUIRED: confirmation for verified managed process cleanup follows.")
    elif inv.blockers:
        print("STOP: ownership or runtime state is not fully provable; no mutation is allowed.")
    elif not apply:
        print("DRY-RUN only: add -Apply after reviewing this plan.")


class Manifest:
    def __init__(self, root: Path, home: Path, scope: str) -> None:
        self.root = root
        self.data: dict[str, Any] = {
            "schema": 1,
            "platform": "win32",
            "scope": scope,
            "home": str(home),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "items": [],
        }

    def add(self, source: Path, destination: Path | None, operation: str, digest: str | None = None) -> None:
        self.data["items"].append(
            {
                "source": str(source),
                "destination": str(destination) if destination else None,
                "operation": operation,
                "sha256": digest,
            }
        )

    def write(self) -> None:
        (self.root / "manifest.json").write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def create_backup_root(inv: Inventory) -> Path:
    base = inv.dp / "uninstall-backups"
    if reject_reparse_components(base.parent, inv.home) is not None:
        raise RuntimeError("backup parent contains a reparse point")
    base.mkdir(parents=True, exist_ok=True)
    if is_reparse(base):
        raise RuntimeError("backup root is a reparse point")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    root = base / (stamp + "-dp-uninstall-" + inv.scope)
    suffix = 1
    while lexists(root):
        root = base / (stamp + "-%d-dp-uninstall-%s" % (suffix, inv.scope))
        suffix += 1
    root.mkdir(mode=0o700)
    (root / "quarantine").mkdir()
    return root


def copy_config_backup(path: Path, backup: Path, manifest: Manifest, index: int) -> None:
    destination = backup / "quarantine" / "configs" / ("%03d-%s" % (index, path.name))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    manifest.add(path, destination, "configuration-snapshot", sha256_file(destination))


def apply_config(action: Action, managed_root: Path, backup: Path, manifest: Manifest, index: int) -> None:
    path = action.path
    if not path.is_file() or is_reparse(path):
        raise RuntimeError("configuration changed before apply: %s" % path)
    if sha256_file(path) != action.expected_sha256:
        raise RuntimeError("configuration changed after review: %s" % path)
    orphan_recovery = action.detail.startswith("remove orphaned owned")
    if orphan_recovery:
        owned = (
            orphan_json_entry(path, managed_root)
            if action.kind == "edit-json"
            else orphan_toml_entry(path, managed_root)
        )
        if not owned:
            raise RuntimeError("orphaned configuration ownership could not be re-proven: %s" % path)
    copy_config_backup(path, backup, manifest, index)
    if action.kind == "edit-json":
        data = read_json(path)
        servers = data.get("mcpServers")
        changed = False
        if isinstance(servers, dict) and SERVER_NAME in servers:
            servers.pop(SERVER_NAME)
            changed = True
            if not servers:
                data.pop("mcpServers", None)
        changed = remove_owned_hooks(data, managed_root) or changed
        if not changed:
            raise RuntimeError("owned JSON entry disappeared before apply: %s" % path)
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    else:
        text = path.read_text(encoding="utf-8")
        rendered, changed = toml_without_server(text)
        if not changed:
            raise RuntimeError("owned TOML entry disappeared before apply: %s" % path)
        atomic_write(path, rendered)
    manifest.add(path, None, "remove-owned-host-entry", sha256_file(path))


def remove_skill_route(action: Action, manifest: Manifest) -> None:
    if not is_reparse(action.path):
        raise RuntimeError("skill junction changed before apply: %s" % action.path)
    target = os.readlink(action.path)
    os.rmdir(action.path)
    if lexists(action.path):
        raise RuntimeError("skill junction removal could not be verified: %s" % action.path)
    manifest.add(action.path, None, "remove-owned-skill-junction", hashlib.sha256(target.encode("utf-8")).hexdigest())


def quarantine_path(action: Action, backup: Path, manifest: Manifest, index: int) -> None:
    source = action.path
    if not lexists(source):
        return
    if action.expected_sha256 is not None:
        if is_reparse(source) or not source.is_file() or sha256_file(source) != action.expected_sha256:
            raise RuntimeError("ownership record changed after review: %s" % source)
    destination = backup / "quarantine" / action.kind / ("%03d-%s" % (index, source.name))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if action.kind == "quarantine-root-link":
        target = os.readlink(source)
        os.rmdir(source)
        manifest.add(source, None, action.kind, hashlib.sha256(target.encode("utf-8")).hexdigest())
        return
    os.replace(source, destination)
    manifest.add(source, destination, action.kind, action.expected_sha256)


def invoke_aqg_uninstaller(inv: Inventory) -> None:
    if inv.aqg_uninstaller is None:
        return
    root = inv.aqg_target or inv.aqg_root
    script = inv.aqg_uninstaller
    environment = os.environ.copy()
    for key in tuple(environment):
        if (
            key in ("DE_ENDPOINT", "DE_ACTIVATION_SECRET")
            or key.upper().startswith("PYTHON")
            or key.upper().startswith("GIT_")
        ):
            environment.pop(key, None)
    environment["AQG_ROOT"] = str(root)
    environment["HOME"] = str(inv.home)
    environment.pop("PROJECT_ROOT", None)
    # AQG's registry intentionally expresses cross-platform adapter commands as
    # ``python3 ...`` shell snippets.  On Windows that name can resolve to a
    # different launcher/interpreter than this already-verified process, and a
    # Windows PATH prefix is not a reliable Git Bash command override. Import
    # the verified official wrapper, then prefix each of its registry-owned Bash
    # commands with a local python3() function that invokes the runpy relay.
    shim_root = Path(tempfile.mkdtemp(prefix="dp-aqg-python-"))
    try:
        executable = Path(sys.executable)
        drive, tail = os.path.splitdrive(str(executable))
        if not re.fullmatch(r"[A-Za-z]:", drive):
            raise RuntimeError("verified Python does not have a local Windows drive path")
        msys_executable = "/%s%s" % (
            drive[0].lower(), tail.replace("\\", "/")
        )
        relay = shim_root / "run_aqg_child.py"
        relay.write_text(
            "from pathlib import Path\n"
            "import os, runpy, sys\n"
            # Windows Agent hosts commonly emit UTF-8 JSON with a BOM. AQG's
            # adapters request plain utf-8 and json.loads rejects the decoded
            # U+FEFF. Keep the compatibility shim child-local and JSON-only;
            # it changes no file and preserves every byte after the BOM.
            "original_read_text = Path.read_text\n"
            + "def read_text_compatible(path, encoding=None, errors=None):\n"
            + "    text = original_read_text(path, encoding=encoding, errors=errors)\n"
            + "    if encoding == 'utf-8' and path.suffix.lower() == '.json':\n"
            + "        return text.removeprefix('\\ufeff')\n"
            + "    return text\n"
            + "Path.read_text = read_text_compatible\n"
            + "root = Path(%r)\n" % str(root)
            + "arguments = sys.argv[1:]\n"
            + "if arguments and arguments[0].lower().endswith('.py'):\n"
            + "    script = Path(arguments[0])\n"
            + "    sys.path[:0] = [str(root), str(script.parent)]\n"
            + "    sys.argv = [str(script), *arguments[1:]]\n"
            + "    runpy.run_path(str(script), run_name='__main__')\n"
            + "else:\n"
            + "    os.execv(sys.executable, [sys.executable, *arguments])\n",
            encoding="utf-8",
            newline="\n",
        )
        relay_drive, relay_tail = os.path.splitdrive(str(relay))
        if not re.fullmatch(r"[A-Za-z]:", relay_drive):
            raise RuntimeError("AQG Python relay does not have a local Windows drive path")
        msys_relay = "/%s%s" % (
            relay_drive[0].lower(), relay_tail.replace("\\", "/")
        )
        shell_prefix = "python3() { %s %s \"$@\"; }; " % (
            shlex.quote(msys_executable),
            shlex.quote(msys_relay),
        )
        bootstrap = (
            "from pathlib import Path\n"
            "import sys\n"
            "root = Path(sys.argv[1])\n"
            "script = Path(sys.argv[2])\n"
            "prefix = sys.argv[3]\n"
            "sys.path[:0] = [str(root), str(script.parent)]\n"
            "from scripts import install_aqg_clients as wrapper\n"
            "if Path(wrapper.__file__).resolve() != script.resolve():\n"
            "    raise RuntimeError('AQG wrapper identity mismatch')\n"
            "original = wrapper._run_command\n"
            "wrapper._run_command = lambda command, env: original(prefix + command, env)\n"
            "raise SystemExit(wrapper.main(sys.argv[4:]))\n"
        )
        command = [
            sys.executable,
            "-I",
            "-c",
            bootstrap,
            str(root),
            str(script),
            shell_prefix,
            "--installed-supported",
            "--uninstall",
            "--aqg-root",
            str(root),
            "--home",
            str(inv.home),
        ]
        completed = subprocess.run(command, cwd=str(root), env=environment, check=False)
    finally:
        shutil.rmtree(shim_root, ignore_errors=True)
    if completed.returncode != 0:
        raise RuntimeError("AQG official user-scope uninstaller failed: exit %d" % completed.returncode)


def apply_inventory(inv: Inventory) -> Path:
    backup = create_backup_root(inv)
    manifest = Manifest(backup, inv.home, inv.scope)
    manifest.write()
    if inv.scope in ("aqg", "both"):
        invoke_aqg_uninstaller(inv)

    effective_actions: list[Action] = []
    if inv.scope in ("de", "both"):
        fresh = Inventory(inv.home, "de")
        fresh.inspect()
        fresh.blockers = [
            item for item in fresh.blockers
            if not any(item.startswith(prefix) for prefix in GUIDABLE_PROCESS_BLOCKER_PREFIXES)
        ]
        if fresh.blockers:
            raise RuntimeError("DE state changed after AQG uninstall: " + "; ".join(fresh.blockers))
        effective_actions.extend(fresh.actions)
    if inv.scope in ("aqg", "both"):
        fresh = Inventory(inv.home, "aqg")
        fresh.inspect()
        if fresh.blockers:
            raise RuntimeError("AQG state changed after official uninstall: " + "; ".join(fresh.blockers))
        effective_actions.extend(fresh.actions)

    deduplicated: list[Action] = []
    seen: set[tuple[str, str, str | None]] = set()
    for action in effective_actions:
        key = (action.kind, path_key(action.path), action.client)
        if key not in seen:
            seen.add(key)
            deduplicated.append(action)
    effective_actions = deduplicated

    config_actions = [a for a in effective_actions if a.kind in {"edit-json", "edit-toml"}]
    route_actions = [a for a in effective_actions if a.kind == "remove-skill-route"]
    record_actions = [a for a in effective_actions if a.kind == "quarantine-record"]
    aux_actions = [
        a for a in effective_actions
        if a.kind in {"quarantine-aux", "quarantine-aqg-version"}
    ]
    root_actions = [
        a for a in effective_actions
        if a.kind in {"quarantine-root", "quarantine-root-link"}
    ]
    empty_dir_actions = [a for a in effective_actions if a.kind == "remove-empty-dir"]

    for index, action in enumerate(config_actions, 1):
        apply_config(action, inv.de_root, backup, manifest, index)
        manifest.write()
    for action in route_actions:
        remove_skill_route(action, manifest)
        manifest.write()
    for index, action in enumerate(record_actions, 1):
        quarantine_path(action, backup, manifest, index)
        manifest.write()
    for index, action in enumerate(aux_actions, 1):
        quarantine_path(action, backup, manifest, index)
        manifest.write()
    for index, action in enumerate(root_actions, 1):
        quarantine_path(action, backup, manifest, index)
        manifest.write()
    for action in sorted(empty_dir_actions, key=lambda item: len(item.path.parts), reverse=True):
        if not lexists(action.path):
            continue
        if is_reparse(action.path) or not action.path.is_dir() or any(action.path.iterdir()):
            raise RuntimeError("managed state directory did not become safely empty: %s" % action.path)
        action.path.rmdir()
        manifest.add(action.path, None, "remove-empty-dir")
        manifest.write()

    prune_uninstall_backups(inv.home, inv.dp, backup)
    manifest.write()
    return backup


def verify_after_apply(home: Path, scope: str) -> None:
    dp = home / ".deeppattern"
    remaining = []
    if scope in ("de", "both") and lexists(dp / "decision-engine"):
        remaining.append(str(dp / "decision-engine"))
    if scope in ("aqg", "both") and lexists(dp / "agent-quality-gates"):
        remaining.append(str(dp / "agent-quality-gates"))
    if remaining:
        raise RuntimeError("post-uninstall verification found remaining roots: " + ", ".join(remaining))
    check = Inventory(home, scope)
    check.inspect()
    remaining_actions = [
        action for action in check.actions if action.kind != "backup-retention"
    ]
    if check.blockers:
        raise RuntimeError("post-uninstall verification blocked: " + "; ".join(check.blockers))
    if remaining_actions:
        raise RuntimeError(
            "post-uninstall verification found remaining in-scope state: "
            + ", ".join(str(action.path) for action in remaining_actions[:8])
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dp-uninstall")
    parser.add_argument("--scope", choices=("de", "aqg", "both"), required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--git", type=Path)
    parser.add_argument("--process-inventory", type=Path, required=True)
    parser.add_argument("--process-inventory-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    global GIT_EXE, PROCESS_INVENTORY_PATH, PROCESS_INVENTORY_SHA256
    args = parse_args(argv)
    if os.name != "nt" or sys.platform != "win32":
        print("unsupported platform: native Windows is required", file=sys.stderr)
        return EXIT_USAGE
    try:
        process_inventory_path = args.process_inventory.resolve(strict=True)
        git_path = args.git.resolve(strict=True) if args.git is not None else None
    except OSError as exc:
        print("verified tool path is unavailable: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    if (
        not process_inventory_path.is_file()
        or (git_path is not None and not git_path.is_file())
    ):
        print("verified tool path is not a regular file", file=sys.stderr)
        return EXIT_USAGE
    if re.fullmatch(r"[0-9a-f]{64}", args.process_inventory_sha256) is None:
        print("process inventory digest is invalid", file=sys.stderr)
        return EXIT_USAGE
    GIT_EXE = str(git_path) if git_path is not None else ""
    PROCESS_INVENTORY_PATH = process_inventory_path
    PROCESS_INVENTORY_SHA256 = args.process_inventory_sha256
    inv = Inventory(args.home, args.scope)
    inv.inspect()
    if inv.scope in ("aqg", "both") and inv.aqg_uninstaller is not None:
        inv.actions.insert(0, Action("aqg-uninstall", inv.aqg_uninstaller, "all detected user-scope adapters; project scope is preserved"))
    print_inventory(inv, args.apply)
    if inv.blockers:
        if has_only_guidable_process_blockers(inv.blockers):
            return EXIT_PROCESS_BLOCKED
        return EXIT_BLOCKED
    if not args.apply:
        return EXIT_OK
    cleanup_actions = [
        action for action in inv.actions if action.kind != "backup-retention"
    ]
    if not cleanup_actions:
        try:
            prune_uninstall_backups(inv.home, inv.dp)
        except Exception as exc:
            print("ERROR: uninstall backup retention failed: %s" % exc, file=sys.stderr)
            return EXIT_BLOCKED
        print("PASS: uninstall verified; no in-scope installation found")
        return EXIT_OK
    try:
        backup = apply_inventory(inv)
        verify_after_apply(inv.home, inv.scope)
    except Exception as exc:
        print("ERROR: uninstall stopped without a clean verification: %s" % exc, file=sys.stderr)
        return EXIT_BLOCKED
    print("PASS: uninstall verified; quarantine=%s" % backup)
    print(
        "Restart affected Agent applications before using Deep Pattern again "
        "so they reload the updated MCP configuration."
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'@

try {
    [IO.File]::WriteAllText(
        $helperPath,
        $helperSource,
        (New-Object System.Text.UTF8Encoding($false))
    )
    function Invoke-UninstallHelper {
        $inventoryDigest = Write-ProcessInventory -Path $processInventoryPath
        $arguments = @(
            "-I", $helperPath,
            "--scope", $Scope,
            "--home", $HomePath,
            "--process-inventory", $processInventoryPath,
            "--process-inventory-sha256", $inventoryDigest
        )
        if (-not [string]::IsNullOrWhiteSpace($GitPath)) {
            $arguments += @("--git", $GitPath)
        }
        if ($Apply) {
            $arguments += "--apply"
        }
        return Invoke-Clean -FilePath $PythonPath -ArgumentList $arguments
    }

    $code = Invoke-UninstallHelper
    if ($code -eq $ExitProcessBlocked) {
        if (-not $Apply) {
            exit 3
        }
        if (-not (Resolve-ActiveManagedSessions)) {
            [Console]::Error.WriteLine(("{0}: ERROR: uninstall remains blocked by active managed processes" -f $ProgramName))
            exit 3
        }
        Write-Host "Rechecking the uninstall plan immediately after process cleanup..."
        $code = Invoke-UninstallHelper
        if ($code -eq $ExitProcessBlocked) {
            [Console]::Error.WriteLine(("{0}: ERROR: a managed process is still active after the immediate recheck" -f $ProgramName))
            exit 3
        }
    }
    exit $code
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$script:PrivateBootstrapRoot) -and
        (Test-Path -LiteralPath $script:PrivateBootstrapRoot)) {
        Remove-Item -LiteralPath $script:PrivateBootstrapRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
