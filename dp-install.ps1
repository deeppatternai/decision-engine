#Requires -Version 5.1
<#
.SYNOPSIS
Installs Deep Pattern (Decision Engine plus Agent Quality Gates) on native Windows.

.DESCRIPTION
This is a thin Windows bootstrapper. It acquires the installer only from the
Deep Pattern product repositories, verifies the expected main/stable channel
shape, and then delegates host MCP, skill, hook, junction, and ACL work to the
shipped DE/AQG Python adapters. Native Windows installation uses Git for
Windows Bash as required by AI_SETUP.md; WSL is deliberately rejected. Missing
Git can be installed through WinGet after explicit confirmation. Python runs
from a verified Deep Pattern private runtime and managed environment, with an
existing trusted Python or WinGet used only as a fallback. Active Agent MCP
sessions defer a signed update without blocking activation or host repair.

The script accepts no product options. The existing masked activation window is
the only interactive product input surface.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $script:Utf8NoBom
$OutputEncoding = $script:Utf8NoBom

$ProgramName = "dp-install"
$DecisionEngineRepository = "https://github.com/deeppatternai/decision-engine.git"
$AqgRepository = "https://github.com/deeppatternai/agent-quality-gates.git"
$AqgRef = "main"
$DeepPatternRoot = Join-Path $HOME ".deeppattern"
$ManagedRoot = Join-Path $HOME ".deeppattern\decision-engine"
$AqgRoot = Join-Path $HOME ".deeppattern\agent-quality-gates"
$ManagedPythonRoot = Join-Path $DeepPatternRoot "de-python"
$ManagedPythonPath = Join-Path $ManagedPythonRoot "Scripts\python.exe"
$ManagedPythonMarker = Join-Path $ManagedPythonRoot ".deeppattern-python-env"
$PrivateRuntimeRoot = Join-Path $DeepPatternRoot "runtimes"
$PrivateRuntimeBackupRoot = Join-Path $DeepPatternRoot "runtime-backups"
$PrivatePythonVersion = "3.13.15"
$PrivatePythonBuild = "20260901"
$PrivatePythonRelease = "20260901"
$PrivatePythonBaseUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$PrivatePythonRelease"
$PrivateRuntimeMarkerName = ".deeppattern-python-runtime"
$ExitFailure = 1
$ExitUsage = 2
$ExitBlocked = 3
$ExitPartial = 4
$script:AqgUpdatePending = $false
$script:UpdateDeferred = $false
$script:DeferredSessionPids = @()

function Stop-Install {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [int]$Code = $ExitUsage
    )
    [Console]::Error.WriteLine(("{0}: ERROR: {1}" -f $ProgramName, $Message))
    exit $Code
}

function Test-NativeWindows {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
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

function Invoke-WithCleanEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter()][hashtable]$Environment = @{},
        [switch]$Capture
    )

    $effectiveEnvironment = @{
        "PYTHONDONTWRITEBYTECODE" = "1"
        "PYTHONUTF8" = "1"
        "PYTHONIOENCODING" = "utf-8"
    }
    foreach ($name in $Environment.Keys) {
        $effectiveEnvironment[[string]$name] = [string]$Environment[$name]
    }
    $effectiveEnvironment["AQG_BACKUP_DIR"] = Join-Path $HOME ".deeppattern\aqg-backups"
    $remove = @(
        "DE_ENDPOINT", "DE_ACTIVATION_SECRET", "PYTHONPATH", "PROJECT_ROOT",
        "AQG_ROOT", "AQG_STATE_ROOT",
        "CLAUDE_DESKTOP_CONFIG", "WORKBUDDY_APP_ROOT", "WORKBUDDY_CONFIG",
        "WORKBUDDY_SKILLS_DIR", "BASH_ENV", "ENV"
    )
    $remove += @(
        [System.Environment]::GetEnvironmentVariables("Process").Keys |
            ForEach-Object { [string]$_ } |
            Where-Object { $_ -match "^(?i:GIT_)" }
    )
    $saved = @{}
    foreach ($name in ($remove + @($effectiveEnvironment.Keys) | Select-Object -Unique)) {
        $saved[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
    }

    try {
        # A null .NET string can become an empty value on newer runtimes.
        # Git distinguishes an absent variable from a present empty one.
        foreach ($name in $saved.Keys) {
            if (Test-Path -LiteralPath "Env:$name") {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction Stop
            }
        }
        foreach ($name in $effectiveEnvironment.Keys) {
            [System.Environment]::SetEnvironmentVariable(
                [string]$name, [string]$effectiveEnvironment[$name], "Process"
            )
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
                [System.Environment]::SetEnvironmentVariable([string]$name, $saved[$name], "Process")
            }
        }
    }
}

function Invoke-PythonScript {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ScriptText,
        [Parameter()][string[]]$ScriptArguments = @(),
        [Parameter()][hashtable]$Environment = @{},
        [string]$WorkingDirectory,
        [switch]$Capture
    )

    $scriptPath = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) ("dp-install-python-" + [Guid]::NewGuid().ToString("N") + ".py")
    [IO.File]::WriteAllText($scriptPath, $ScriptText, $script:Utf8NoBom)
    try {
        [string[]]$arguments = @("-I", $scriptPath) + @($ScriptArguments)
        if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
            Push-Location -LiteralPath $WorkingDirectory
        }
        try {
            return Invoke-WithCleanEnvironment `
                -FilePath $PythonPath `
                -ArgumentList $arguments `
                -Environment $Environment `
                -Capture:$Capture
        }
        finally {
            if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
                Pop-Location
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-WinGet {
    $candidates = New-Object System.Collections.Generic.List[string]
    try {
        Get-AppxPackage -Name "Microsoft.DesktopAppInstaller" -ErrorAction Stop |
            Sort-Object -Property Version -Descending |
            ForEach-Object {
                if (-not [string]::IsNullOrWhiteSpace([string]$_.InstallLocation)) {
                    $candidates.Add((Join-Path ([string]$_.InstallLocation) "winget.exe"))
                }
            }
    }
    catch {
        # The App Installer package may be absent on a clean Windows image.
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\winget.exe"))
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        $verified = Get-VerifiedAuthenticodePath `
            -Path $candidate `
            -PublisherPattern "(?i:Microsoft Corporation|Microsoft Windows)"
        if ($null -ne $verified) {
            return $verified
        }
    }
    return $null
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

function Find-Git {
    foreach ($candidate in (Get-GitCandidates)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $verified = Get-VerifiedAuthenticodePath `
            -Path $candidate `
            -PublisherPattern "(?i:Johannes Schindelin|Git for Windows)"
        if ($null -eq $verified) {
            continue
        }
        $result = Invoke-WithCleanEnvironment -FilePath $verified -ArgumentList @("--version") -Capture
        $joined = ($result.Output -join "`n")
        if ($result.ExitCode -ne 0 -or $joined -notmatch "git version ([0-9]+)\.([0-9]+)") {
            continue
        }
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -gt 2 -or ($major -eq 2 -and $minor -ge 36)) {
            return $verified
        }
    }
    return $null
}

function Install-WinGetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$PackageId,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )

    $winget = Resolve-WinGet
    if ([string]::IsNullOrWhiteSpace($winget)) {
        Stop-Install "$DisplayName is missing and WinGet is unavailable. Install Microsoft App Installer, then retry."
    }
    if (-not (Confirm-UserAction -Message ("Install or upgrade {0} with WinGet now?" -f $DisplayName))) {
        Stop-Install "$DisplayName installation was declined; install it, then retry."
    }
    $code = Invoke-WithCleanEnvironment -FilePath $winget -ArgumentList @(
        "install", "--id", $PackageId, "--exact", "--source", "winget",
        "--accept-source-agreements", "--accept-package-agreements"
    )
    if ($code -ne 0) {
        Stop-Install "WinGet could not install $DisplayName; correct the reported WinGet error, then retry."
    }
}

function Resolve-Git {
    $git = Find-Git
    if (-not [string]::IsNullOrWhiteSpace($git)) {
        return $git
    }
    Write-Host "Git for Windows 2.36 or newer is missing."
    Install-WinGetPackage -PackageId "Git.Git" -DisplayName "Git for Windows 2.36 or newer"
    $git = Find-Git
    if ([string]::IsNullOrWhiteSpace($git)) {
        Stop-Install "WinGet finished, but Git for Windows 2.36 or newer could not be verified. Open a new PowerShell window and retry."
    }
    Write-Host ("Git prerequisite ready: {0}" -f $git)
    return $git
}

function Resolve-GitBash {
    param([Parameter(Mandatory = $true)][string]$GitPath)

    $gitDirectory = Split-Path -Parent $GitPath
    $gitRoot = Split-Path -Parent $gitDirectory
    $candidates = @(
        (Join-Path $gitRoot "bin\bash.exe"),
        (Join-Path $gitRoot "usr\bin\bash.exe")
    )
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
    $verified = Get-VerifiedAuthenticodePath `
        -Path $Candidate `
        -PublisherPattern "(?i:Python Software Foundation|Microsoft Corporation|Anaconda)"
    if ($null -eq $verified) {
        return $false
    }
    $version = Invoke-WithCleanEnvironment -FilePath $verified -ArgumentList @(
        "-I", "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)"
    ) -Capture
    if ($version.ExitCode -ne 0) {
        return $false
    }
    $modules = Invoke-WithCleanEnvironment -FilePath $verified -ArgumentList @(
        "-I", "-c", "import ssl, venv, tkinter"
    ) -Capture
    if ($modules.ExitCode -ne 0) {
        return $false
    }
    $pip = Invoke-WithCleanEnvironment -FilePath $verified -ArgumentList @(
        "-I", "-m", "pip", "--version"
    ) -Capture
    return $pip.ExitCode -eq 0
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:DE_PYTHON)) {
        $candidates.Add($env:DE_PYTHON)
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
            $probe = Invoke-WithCleanEnvironment -FilePath $py -ArgumentList @(
                $selector, "-I", "-c", "import sys; print(sys.executable)"
            ) -Capture
            if ($probe.ExitCode -eq 0 -and $probe.Output.Count -gt 0) {
                $resolved = [string]$probe.Output[$probe.Output.Count - 1]
                if (-not [string]::IsNullOrWhiteSpace($resolved)) {
                    $candidates.Add($resolved.Trim())
                }
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
        if (Test-PythonExecutable -Candidate $candidate) {
            $identity = Invoke-WithCleanEnvironment -FilePath $candidate -ArgumentList @(
                "-I", "-c", "import os, sys; print(os.path.abspath(sys.executable))"
            ) -Capture
            if ($identity.ExitCode -eq 0 -and $identity.Output.Count -gt 0) {
                return ([string]$identity.Output[$identity.Output.Count - 1]).Trim()
            }
        }
    }
    return $null
}

function Test-PrivatePythonExecutable {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    if (-not [IO.Path]::IsPathRooted($Candidate) -or
        -not (Test-Path -LiteralPath $Candidate -PathType Leaf) -or
        (Test-ReparsePoint -Path $Candidate)) {
        return $false
    }
    $probe = Invoke-WithCleanEnvironment -FilePath $Candidate -ArgumentList @(
        "-I", "-c",
        "import ssl, sys, tkinter, venv; import pip; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)"
    ) -Capture
    return $probe.ExitCode -eq 0
}

function Assert-PrivateDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Description
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or (Test-ReparsePoint -Path $Path)) {
        Stop-Install "$Path is not a regular $Description directory; preserve it and stop." $ExitBlocked
    }
    $owner = (Get-Acl -LiteralPath $Path -ErrorAction Stop).GetOwner(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
    if ($owner -ne [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value) {
        Stop-Install "$Path is not owned by the current Windows user; preserve it and stop." $ExitBlocked
    }
}

function Get-PrivateRuntimeSpec {
    $architecture = [string]$env:PROCESSOR_ARCHITEW6432
    if ([string]::IsNullOrWhiteSpace($architecture)) {
        $architecture = [string]$env:PROCESSOR_ARCHITECTURE
    }
    if ([string]::IsNullOrWhiteSpace($architecture)) {
        $architecture = [string]$env:PROCESSOR_IDENTIFIER
    }
    if ($architecture -match "^(?i:AMD64|x86_64)") {
        $runtimeArchitecture = "x86_64"
        $sha256 = "9bcc038a0bf180612ed56dec93d4977d035e80b8d9320ef51a38c287baf134b7"
        $size = 47042104L
    }
    elseif ($architecture -match "^(?i:ARM64|aarch64)") {
        $runtimeArchitecture = "aarch64"
        $sha256 = "ce87247378f43f88e0202a0fa6d3cdb5f5fb246a3bc61b2fb604bd49b7862508"
        $size = 43801216L
    }
    else {
        Stop-Install "Unsupported Windows architecture for the private Python runtime: $architecture"
    }
    $runtimeId = "cpython-$PrivatePythonVersion+$PrivatePythonBuild-$runtimeArchitecture-pc-windows-msvc"
    $asset = "$runtimeId-install_only.tar.gz"
    return [pscustomobject]@{
        Architecture = $runtimeArchitecture
        RuntimeId = $runtimeId
        Asset = $asset
        Url = "$PrivatePythonBaseUrl/$($asset.Replace('+', '%2B'))"
        Sha256 = $sha256
        Size = $size
        Directory = Join-Path $PrivateRuntimeRoot $runtimeId
    }
}

function Resolve-TrustedSystemTool {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$PublisherPattern
    )

    $candidate = Join-Path $env:WINDIR "System32\$Name"
    return Get-VerifiedAuthenticodePath -Path $candidate -PublisherPattern $PublisherPattern
}

function Test-PrivateRuntime {
    param([Parameter(Mandatory = $true)]$Spec)

    $marker = Join-Path $Spec.Directory $PrivateRuntimeMarkerName
    $python = Join-Path $Spec.Directory "python\python.exe"
    if (-not (Test-Path -LiteralPath $Spec.Directory -PathType Container) -or
        (Test-ReparsePoint -Path $Spec.Directory) -or
        -not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        (Test-ReparsePoint -Path $marker)) {
        return $false
    }
    $lines = @(Get-Content -LiteralPath $marker -ErrorAction SilentlyContinue)
    if ($lines -notcontains "schema=1" -or
        $lines -notcontains ("runtime_id={0}" -f $Spec.RuntimeId) -or
        $lines -notcontains ("sha256={0}" -f $Spec.Sha256)) {
        return $false
    }
    return Test-PrivatePythonExecutable -Candidate $python
}

function Get-RuntimeBackupPath {
    param([Parameter(Mandatory = $true)][string]$Label)

    Assert-PrivateDirectory -Path $PrivateRuntimeBackupRoot -Description "Deep Pattern runtime backup"
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $candidate = Join-Path $PrivateRuntimeBackupRoot "$stamp-$Label"
    $suffix = 0
    while (Test-Path -LiteralPath $candidate) {
        $suffix++
        $candidate = Join-Path $PrivateRuntimeBackupRoot "$stamp-$suffix-$Label"
    }
    return $candidate
}

function Move-ToRuntimeBackup {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or (Test-ReparsePoint -Path $Path)) {
        Stop-Install "$Path is not a regular Deep Pattern runtime directory; preserve it and stop." $ExitBlocked
    }
    $destination = Get-RuntimeBackupPath -Label $Label
    Move-Item -LiteralPath $Path -Destination $destination -ErrorAction Stop
    Write-Host "Preserved the previous runtime state at $destination."
}

function Install-PrivatePythonRuntime {
    $spec = Get-PrivateRuntimeSpec
    Assert-PrivateDirectory -Path $DeepPatternRoot -Description "Deep Pattern"
    Assert-PrivateDirectory -Path $PrivateRuntimeRoot -Description "Deep Pattern runtime"

    if (Test-Path -LiteralPath $spec.Directory) {
        if (Test-PrivateRuntime -Spec $spec) {
            Write-Host ("Reusing Deep Pattern private Python {0} at {1}." -f $PrivatePythonVersion, $spec.Directory)
            return (Join-Path $spec.Directory "python\python.exe")
        }
        if (-not (Confirm-UserAction "The Deep Pattern private Python runtime is incomplete or invalid. Preserve it and download a verified replacement?")) {
            Stop-Install "The invalid private Python runtime was preserved; repair was declined." $ExitBlocked
        }
        Move-ToRuntimeBackup -Path $spec.Directory -Label "private-python-runtime"
    }
    else {
        Write-Host ("Deep Pattern requires its verified private Python {0} runtime." -f $PrivatePythonVersion)
        Write-Host ("Downloading about {0:N0} MB into {1} without changing system Python." -f ($spec.Size / 1MB), $PrivateRuntimeRoot)
    }

    $curl = Resolve-TrustedSystemTool -Name "curl.exe" -PublisherPattern "(?i:Microsoft Corporation|Microsoft Windows)"
    $tar = Resolve-TrustedSystemTool -Name "tar.exe" -PublisherPattern "(?i:Microsoft Corporation|Microsoft Windows)"
    if ([string]::IsNullOrWhiteSpace($curl) -or [string]::IsNullOrWhiteSpace($tar)) {
        Write-Warning "Trusted Windows curl.exe or tar.exe is unavailable; the private Python runtime cannot be prepared."
        return $null
    }

    $stage = Join-Path $PrivateRuntimeRoot (".python-runtime-stage-" + [Guid]::NewGuid().ToString("N"))
    $archive = Join-Path $stage $spec.Asset
    $extract = Join-Path $stage "extract"
    New-Item -ItemType Directory -Path $extract -Force | Out-Null
    try {
        Write-Host ("Downloading Deep Pattern private Python {0} for {1}..." -f $PrivatePythonVersion, $spec.Architecture)
        $downloadCode = Invoke-WithCleanEnvironment -FilePath $curl -ArgumentList @(
            "--fail", "--location", "--show-error", "--progress-bar",
            "--proto", "=https", "--tlsv1.2", "--connect-timeout", "20", "--retry", "2",
            "--output", $archive, $spec.Url
        )
        if ($downloadCode -ne 0 -or -not (Test-Path -LiteralPath $archive -PathType Leaf)) {
            Write-Warning "Private Python download failed; no existing runtime was overwritten."
            return $null
        }
        $actualSize = (Get-Item -LiteralPath $archive -Force).Length
        $actualSha = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSize -ne $spec.Size -or $actualSha -ne $spec.Sha256) {
            Write-Warning "Private Python download failed its fixed size or SHA-256 check; nothing was installed."
            return $null
        }

        $listing = Invoke-WithCleanEnvironment -FilePath $tar -ArgumentList @("-tzf", $archive) -Capture
        if ($listing.ExitCode -ne 0 -or $listing.Output.Count -eq 0) {
            Write-Warning "Private Python archive could not be inspected; nothing was installed."
            return $null
        }
        foreach ($rawMember in $listing.Output) {
            $member = ([string]$rawMember).Trim().Replace("\", "/")
            $segments = @($member.Split("/") | Where-Object { $_ -ne "" })
            if ([string]::IsNullOrWhiteSpace($member) -or
                ($member -ne "python" -and -not $member.StartsWith("python/")) -or
                $member.StartsWith("/") -or
                $member -match "^[A-Za-z]:" -or
                $segments -contains "..") {
                Write-Warning "Private Python archive has an unexpected path layout; nothing was installed."
                return $null
            }
        }
        $extractCode = Invoke-WithCleanEnvironment -FilePath $tar -ArgumentList @("-xzf", $archive, "-C", $extract)
        if ($extractCode -ne 0) {
            Write-Warning "Private Python archive extraction failed; nothing was installed."
            return $null
        }
        $topLevel = @(Get-ChildItem -LiteralPath $extract -Force)
        $extractedPython = Join-Path $extract "python"
        if ($topLevel.Count -ne 1 -or $topLevel[0].Name -ne "python" -or
            -not (Test-Path -LiteralPath $extractedPython -PathType Container) -or
            (Test-ReparsePoint -Path $extractedPython) -or
            @(Get-ChildItem -LiteralPath $extractedPython -Recurse -Force | Where-Object {
                ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
            }).Count -ne 0 -or
            -not (Test-PrivatePythonExecutable -Candidate (Join-Path $extractedPython "python.exe"))) {
            Write-Warning "The verified private Python archive does not provide ssl, venv, tkinter, and pip on this Windows device."
            return $null
        }

        $stagedRuntime = Join-Path $stage "runtime"
        New-Item -ItemType Directory -Path $stagedRuntime | Out-Null
        Move-Item -LiteralPath $extractedPython -Destination (Join-Path $stagedRuntime "python")
        $markerLines = @(
            "schema=1",
            "runtime_id=$($spec.RuntimeId)",
            "python_version=$PrivatePythonVersion",
            "architecture=$($spec.Architecture)",
            "source=$($spec.Url)",
            "sha256=$($spec.Sha256)"
        )
        [IO.File]::WriteAllLines(
            (Join-Path $stagedRuntime $PrivateRuntimeMarkerName),
            $markerLines,
            $script:Utf8NoBom
        )
        if (Test-Path -LiteralPath $spec.Directory) {
            Stop-Install "$($spec.Directory) appeared during download; preserve it and retry." $ExitBlocked
        }
        Move-Item -LiteralPath $stagedRuntime -Destination $spec.Directory
        if (-not (Test-PrivateRuntime -Spec $spec)) {
            Stop-Install "$($spec.Directory) was installed but failed final verification; preserve it and stop." $ExitBlocked
        }
        Write-Host ("Deep Pattern private Python runtime ready at {0}." -f $spec.Directory)
        return (Join-Path $spec.Directory "python\python.exe")
    }
    finally {
        if (Test-Path -LiteralPath $stage) {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-ManagedPythonEnvironment {
    if (-not (Test-Path -LiteralPath $ManagedPythonRoot -PathType Container) -or
        (Test-ReparsePoint -Path $ManagedPythonRoot) -or
        -not (Test-Path -LiteralPath $ManagedPythonMarker -PathType Leaf) -or
        (Test-ReparsePoint -Path $ManagedPythonMarker)) {
        return $false
    }
    $lines = @(Get-Content -LiteralPath $ManagedPythonMarker -ErrorAction SilentlyContinue)
    return ($lines -contains "schema=1") -and
        (Test-PrivatePythonExecutable -Candidate $ManagedPythonPath)
}

function New-ManagedPythonEnvironment {
    param([Parameter(Mandatory = $true)][string]$BasePython)

    Assert-PrivateDirectory -Path $DeepPatternRoot -Description "Deep Pattern"
    $stage = Join-Path $DeepPatternRoot (".de-python-stage-" + [Guid]::NewGuid().ToString("N"))
    $stagedEnvironment = Join-Path $stage "de-python"
    New-Item -ItemType Directory -Path $stage | Out-Null
    try {
        $code = Invoke-WithCleanEnvironment -FilePath $BasePython -ArgumentList @("-I", "-m", "venv", $stagedEnvironment)
        $stagedPython = Join-Path $stagedEnvironment "Scripts\python.exe"
        if ($code -ne 0 -or -not (Test-PrivatePythonExecutable -Candidate $stagedPython)) {
            Stop-Install "Could not create a complete private Deep Pattern Python environment with $BasePython." $ExitBlocked
        }
        [IO.File]::WriteAllLines(
            (Join-Path $stagedEnvironment ".deeppattern-python-env"),
            @("schema=1", "base_python=$BasePython"),
            $script:Utf8NoBom
        )
        if (Test-Path -LiteralPath $ManagedPythonRoot) {
            Stop-Install "$ManagedPythonRoot appeared while Python was being prepared; preserve it and retry." $ExitBlocked
        }
        Move-Item -LiteralPath $stagedEnvironment -Destination $ManagedPythonRoot
    }
    finally {
        if (Test-Path -LiteralPath $stage) {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not (Test-ManagedPythonEnvironment)) {
        Stop-Install "The private Deep Pattern Python environment failed final verification." $ExitBlocked
    }
    Write-Host "Created the private Deep Pattern Python environment at $ManagedPythonRoot."
    return $ManagedPythonPath
}

function Resolve-Python {
    if (Test-ManagedPythonEnvironment) {
        Write-Host "Reusing the private Deep Pattern Python environment at $ManagedPythonRoot."
        return $ManagedPythonPath
    }
    if (Test-Path -LiteralPath $ManagedPythonRoot) {
        if (-not (Confirm-UserAction "The private Deep Pattern Python environment is unusable. Preserve it and rebuild it?")) {
            Stop-Install "$ManagedPythonRoot was preserved; repair was declined." $ExitBlocked
        }
        Move-ToRuntimeBackup -Path $ManagedPythonRoot -Label "de-python"
    }

    $basePython = $null
    if (-not [string]::IsNullOrWhiteSpace($env:DE_PYTHON)) {
        if (-not (Test-PythonExecutable -Candidate $env:DE_PYTHON)) {
            Stop-Install "DE_PYTHON does not identify an Authenticode-verified Python 3.12+ with ssl, venv, tkinter, and pip."
        }
        $basePython = (Get-Item -LiteralPath $env:DE_PYTHON -Force).FullName
        Write-Host "Using the explicitly selected DE_PYTHON only to prepare the private Deep Pattern environment."
    }
    else {
        $basePython = Install-PrivatePythonRuntime
    }
    if ([string]::IsNullOrWhiteSpace($basePython)) {
        Write-Warning "A verified private Python runtime could not be used. Checking an existing trusted Python or WinGet fallback."
        $basePython = Find-Python
    }
    if ([string]::IsNullOrWhiteSpace($basePython)) {
        Write-Host "Python 3.12 or newer with ssl, venv, pip, and tkinter is missing."
        Install-WinGetPackage -PackageId "Python.Python.3.13" -DisplayName "Python 3.13"
        $basePython = Find-Python
    }
    if ([string]::IsNullOrWhiteSpace($basePython)) {
        Stop-Install "A compatible verified Python could not be prepared. Open a new PowerShell window and retry."
    }
    return New-ManagedPythonEnvironment -BasePython $basePython
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force
    return ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
}

function Get-VerifiedAqgLayout {
    if (-not (Test-Path -LiteralPath $AqgRoot)) {
        return [pscustomobject]@{ Layout = "absent"; Target = $null; Head = $null }
    }
    if (-not (Test-Path -LiteralPath $AqgRoot -PathType Container)) {
        Stop-Install "$AqgRoot is not an AQG checkout directory; preserve it and stop." $ExitBlocked
    }

    $layout = "regular"
    $target = $AqgRoot
    if (Test-ReparsePoint -Path $AqgRoot) {
        $versionsRoot = Join-Path (Split-Path -Parent $AqgRoot) "versions"
        if (Test-ReparsePoint -Path $versionsRoot) {
            Stop-Install "The AQG versions directory is a reparse point; preserve it and stop." $ExitBlocked
        }
        $resolveScript = @'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
versions = Path(sys.argv[2])
try:
    target = root.resolve(strict=True)
    versions = versions.resolve(strict=True)
except OSError:
    raise SystemExit(1)
if (
    target.parent != versions
    or not target.is_dir()
):
    raise SystemExit(1)
if re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", target.name) is None:
    raise SystemExit(1)
print(target)
'@
        $resolved = Invoke-PythonScript `
            -PythonPath $script:PythonPath `
            -ScriptText $resolveScript `
            -ScriptArguments @($AqgRoot, $versionsRoot) `
            -Capture
        if ($resolved.ExitCode -ne 0 -or $resolved.Output.Count -ne 1) {
            Stop-Install "$AqgRoot is not an AQG-managed versions\<release-or-commit> junction; preserve it and stop." $ExitBlocked
        }
        $target = ([string]$resolved.Output[0]).Trim()
        $layout = "managed"
    }

    foreach ($relativePath in @(
        ".git",
        "AI_SETUP.md",
        "scripts\install_aqg_clients.py",
        "scripts\aqg_doctor.py",
        "VERSION",
        "requirements.txt"
    )) {
        $required = Join-Path $AqgRoot $relativePath
        if (-not (Test-Path -LiteralPath $required) -or (Test-ReparsePoint -Path $required)) {
            Stop-Install "$AqgRoot has an incomplete or link-like AQG layout; preserve it and stop." $ExitBlocked
        }
    }

    $origin = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "remote", "get-url", "origin"
    ) -Capture
    $originText = ($origin.Output -join "").Trim()
    if ($origin.ExitCode -ne 0 -or $originText -notin @(
        $AqgRepository,
        "git@github.com:deeppatternai/agent-quality-gates.git",
        "ssh://git@github.com/deeppatternai/agent-quality-gates.git"
    )) {
        Stop-Install "$AqgRoot is not owned by the approved AQG product repository; it was preserved." $ExitBlocked
    }
    $status = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "status", "--porcelain=v1", "--untracked-files=all"
    ) -Capture
    if ($status.ExitCode -ne 0 -or -not [string]::IsNullOrWhiteSpace(($status.Output -join ""))) {
        Stop-Install "$AqgRoot contains local changes; it was preserved." $ExitBlocked
    }
    $headResult = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "rev-parse", "--verify", "HEAD"
    ) -Capture
    $head = ($headResult.Output -join "").Trim()
    if ($headResult.ExitCode -ne 0 -or $head -notmatch "^[0-9a-f]{40}$") {
        Stop-Install "$AqgRoot does not resolve to a full Git commit; it was preserved." $ExitBlocked
    }
    if ($layout -eq "managed") {
        if (-not (Test-AqgManagedTargetName -Target $target -Head $head)) {
            Stop-Install "$AqgRoot target name does not match VERSION/HEAD or the official retry rule; it was preserved." $ExitBlocked
        }
    }
    return [pscustomobject]@{ Layout = $layout; Target = $target; Head = $head }
}

function Test-AqgManagedTargetName {
    param([string]$Target, [string]$Head)
    $aqgNameScript = @'
from pathlib import Path
import re
import sys

root, head = Path(sys.argv[1]), sys.argv[2]
if re.fullmatch(r"[0-9a-f]{40}", head) is None:
    raise SystemExit(1)
if re.fullmatch(r"[0-9a-f]{40}", root.name):
    raise SystemExit(0 if root.name == head else 1)
if re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", root.name) is None:
    raise SystemExit(1)
version = (root / "VERSION").read_text(encoding="utf-8").strip()
def release(value):
    return len(value) <= 40 and re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", value
    ) is not None
reissue = f"{version}-{head[:12]}"
bases = {version if release(version) else head, reissue if release(reissue) else head}
legacy = root.name == version and re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?", version)
matches = legacy or root.name in bases or any(
    re.fullmatch(re.escape(base[:23]) + r"-[0-9a-f]{16}", root.name) for base in bases
)
raise SystemExit(0 if matches else 1)
'@
    $result = Invoke-PythonScript -PythonPath $script:PythonPath -ScriptText $aqgNameScript `
        -ScriptArguments @($Target, $Head) -Capture
    return $result.ExitCode -eq 0
}

function Assert-OwnedAqgDirectory {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return }
    if (-not $item.PSIsContainer -or (Test-ReparsePoint -Path $Path)) {
        Stop-Install "AQG backup path is not a regular directory: $Path" $ExitBlocked
    }
    $managedBoundary = [IO.Path]::GetFullPath((Join-Path $HOME ".deeppattern")).TrimEnd("\")
    $fullPath = [IO.Path]::GetFullPath($item.FullName).TrimEnd("\")
    if ($fullPath -ne $managedBoundary -and
        -not $fullPath.StartsWith(($managedBoundary + "\"), [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Install "AQG backup path is outside the current user's Deep Pattern directory: $Path" $ExitBlocked
    }
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    $trustedOwners = @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
        "S-1-5-18",       # LocalSystem can own state created by an elevated installer.
        "S-1-5-32-544"   # Builtin Administrators is the common elevated owner.
    )
    if ($owner -notin $trustedOwners) {
        Stop-Install "AQG backup directory owner is not the current user or a trusted Windows installer identity: $Path" $ExitBlocked
    }
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
        # Deny use of directories writable by Everyone, Authenticated Users or Users.
        if ($rule.AccessControlType -eq "Allow" -and
            $sid -in @("S-1-1-0", "S-1-5-11", "S-1-5-32-545") -and
            (([int]$rule.FileSystemRights -band 0xD0156) -ne 0)) {
            Stop-Install "AQG backup directory grants broad write access: $Path" $ExitBlocked
        }
    }
}

function Invoke-AqgBackupResidue {
    param([string]$Mode, [string]$Identity = "", [switch]$Capture)
    $aqgBackupScript = @'
from pathlib import Path
import os
import stat
import sys
import tempfile

dp, action = Path(sys.argv[1]), sys.argv[2]
source, destination = dp / "versions/aqg-backups", dp / "aqg-backups"
def directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"not a regular non-reparse directory: {path}")
    return info
def identity():
    parts = []
    for path in (dp, source.parent, source):
        info = directory(path)
        parts.extend((info.st_dev, info.st_ino, info.st_mtime_ns))
    return ":".join(map(str, parts))
try:
    if action == "inspect" and not os.path.lexists(source):
        raise SystemExit(0)
    frozen = identity()
    if action == "inspect":
        print(frozen)
        raise SystemExit(0)
    if action != "move" or len(sys.argv) != 4 or frozen != sys.argv[3]:
        raise ValueError("backup directory identity changed after confirmation; nothing moved")
    if os.path.lexists(destination):
        directory(destination)
    else:
        destination.mkdir(mode=0o700)
    archive = Path(tempfile.mkdtemp(prefix="legacy-versions-", dir=destination))
    try:
        for path in (dp, source.parent, destination):
            directory(path)
        info = directory(source)
        if ":".join(map(str, (info.st_dev, info.st_ino, info.st_mtime_ns))) != ":".join(frozen.split(":")[-3:]):
            raise ValueError("backup directory changed before move; nothing moved")
        os.rename(source, archive / "aqg-backups")
    except BaseException:
        archive.rmdir()
        raise
    print(f"PRESERVE archived legacy AQG backups: {archive / 'aqg-backups'}")
except (OSError, ValueError) as exc:
    print(f"AQG backup relocation refused: {exc}", file=sys.stderr)
    raise SystemExit(2)
'@
    return Invoke-PythonScript -PythonPath $script:PythonPath -ScriptText $aqgBackupScript `
        -ScriptArguments @((Join-Path $HOME ".deeppattern"), $Mode, $Identity) -Capture:$Capture
}

function Repair-AqgBackupResidue {
    $dp = Join-Path $HOME ".deeppattern"
    $source = Join-Path $dp "versions\aqg-backups"
    $destination = Join-Path $dp "aqg-backups"
    $probe = Invoke-AqgBackupResidue -Mode inspect -Capture
    if ($probe.ExitCode -ne 0) {
        Stop-Install "Cannot safely inspect legacy AQG backups: $($probe.Output -join ' ')" $ExitBlocked
    }
    if ($probe.Output.Count -eq 0) { return }
    if ($probe.Output.Count -ne 1 -or [string]$probe.Output[0] -notmatch '^\d+(?::\d+){8}$') {
        Stop-Install "Unexpected legacy AQG backup identity; nothing moved." $ExitBlocked
    }
    foreach ($path in @($dp, (Split-Path -Parent $source), $source, $destination)) {
        Assert-OwnedAqgDirectory -Path $path
    }
    Write-Host "AQG historical backups were found at $source."
    Write-Host "They will be moved intact into a new legacy-versions archive under $destination; nothing will be deleted, merged, or overwritten."
    foreach ($path in @($dp, (Split-Path -Parent $source), $source, $destination)) {
        Assert-OwnedAqgDirectory -Path $path
    }
    $code = Invoke-AqgBackupResidue -Mode move -Identity ([string]$probe.Output[0])
    if ($code -ne 0) {
        Stop-Install "Could not relocate legacy backups. Close applications using the directory and retry; no copy/delete fallback was attempted." $ExitBlocked
    }
}

function Invoke-AqgUpdateContract {
    param([ValidateSet("migrate", "update")][string]$Mode)
    $null = Get-VerifiedAqgLayout
    Write-Host ("AQG official managed-update phase: {0}..." -f $Mode)
    $aqgUpdateScript = @'
from pathlib import Path
import os
import sys

root = Path(sys.argv[1]).resolve(strict=True)
logical = Path(sys.argv[1]).absolute()
mode, remote = sys.argv[2:4]
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
os.environ["AQG_ROOT"] = str(logical)
os.chdir(logical.parent)


def official_link_kind(path):
    try:
        from scripts.aqg_update import stage
    except ImportError:
        return "symlink" if path.is_symlink() else None
    classifier = getattr(stage, "current_link_kind", None)
    if classifier is None:
        return "symlink" if path.is_symlink() else None
    try:
        return classifier(path)
    except (OSError, RuntimeError, ValueError):
        return None


# Older AQG releases cannot safely update through a directory junction. Only
# pass one to an updater that explicitly classifies it as a supported link kind.
if getattr(logical.lstat(), "st_reparse_tag", 0) == 0xA0000003 \
        and official_link_kind(logical) != "junction":
    print("AQG status=legacy-junction: configuration can be retained, but automatic update compatibility is unverified.")
    raise SystemExit(5)
if mode == "migrate":
    from scripts.aqg_update.migrate import ensure_managed_layout
    result = ensure_managed_layout(logical)
    print(f"AQG layout: {result.reason}")
    link_kind = official_link_kind(logical)
    if not result.managed or link_kind not in {"symlink", "junction"}:
        print("AQG layout is pending. The verified checkout remains usable, but this AQG release cannot enable ordinary-user automatic updates on this Windows device.")
        raise SystemExit(4)
    print(f"AQG managed entrance ({link_kind}): {logical} -> {logical.resolve(strict=True)}")
    raise SystemExit(0)
from scripts.aqg_update.run import check
result = check(root=logical, remote=remote, channel="stable", apply=True)
print(f"AQG signed update: status={result.outcome}; {result.detail}")
if result.pending:
    print("AQG host approvals remain pending: " + ", ".join(result.pending))
    raise SystemExit(4)
if result.outcome in {"current", "applied"}:
    raise SystemExit(0)
if result.outcome in {"disabled", "too-soon"}:
    print("AQG update check skipped by official policy; latest release is not confirmed.")
    raise SystemExit(0)
raise SystemExit(4 if result.outcome in {"pending", "busy", "deferred"} else 2)
'@
    $code = Invoke-PythonScript -PythonPath $script:PythonPath -ScriptText $aqgUpdateScript `
        -ScriptArguments @($AqgRoot, $Mode, $AqgRepository) `
        -Environment @{ "PATH" = "$(Split-Path -Parent $GitPath);$env:PATH"; "AQG_BASH" = $BashPath } `
        -WorkingDirectory (Split-Path -Parent $AqgRoot)
    if ($code -eq 5) {
        $script:AqgUpdatePending = $true
        Write-Warning "Preserving the verified legacy AQG junction. Automatic update support remains pending; no version tree was replaced."
        return
    }
    if ($code -eq 4 -and $Mode -eq "migrate") {
        $pendingLayout = Get-VerifiedAqgLayout
        if ($pendingLayout.Layout -eq "regular") {
            $script:AqgUpdatePending = $true
            Write-Warning "AQG remains a verified regular checkout. Installation will continue, but AQG automatic updates are pending a junction-capable public release."
            return
        }
    }
    if ($code -ne 0) {
        $exitCode = if ($code -eq 4) { $ExitPartial } else { $ExitBlocked }
        Stop-Install "AQG $Mode did not complete; inspect the reason above. No forced checkout or replacement was attempted." $exitCode
    }
    $after = Get-VerifiedAqgLayout
    if ($after.Layout -ne "managed") {
        Stop-Install "AQG did not produce a verified managed versions directory." $ExitBlocked
    }
}

function Sync-AqgCheckout {
    param([Parameter(Mandatory = $true)]$LayoutInfo)

    if ($LayoutInfo.Layout -eq "managed") {
        Invoke-AqgUpdateContract -Mode update
        return
    }
    if ($LayoutInfo.Layout -eq "absent") {
        Write-Host ("Cloning Agent Quality Gates from {0} at {1}..." -f $AqgRepository, $AqgRef)
        $parent = Split-Path -Parent $AqgRoot
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        $clone = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
            "clone", "--config", "core.autocrlf=false", "--config", "core.eol=lf",
            "--depth", "1", "--branch", $AqgRef, "--single-branch",
            "--", $AqgRepository, $AqgRoot
        )
        if ($clone -ne 0) {
            Stop-Install "Could not clone Agent Quality Gates from the approved product repository." $ExitBlocked
        }
        $null = Get-VerifiedAqgLayout
        return
    }
    Write-Host ("Synchronizing Agent Quality Gates from {0} at {1}..." -f $AqgRepository, $AqgRef)
    if ($LayoutInfo.Layout -eq "regular") {
        foreach ($setting in @(
            @("core.autocrlf", "false"),
            @("core.eol", "lf")
        )) {
            $code = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
                "-C", $AqgRoot, "config", "--local", $setting[0], $setting[1]
            )
            if ($code -ne 0) {
                Stop-Install "Could not pin LF-safe Git configuration for the AQG checkout; it was preserved." $ExitBlocked
            }
        }
        $rewrite = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
            "-C", $AqgRoot, "checkout-index", "--all", "--force"
        )
        if ($rewrite -ne 0) {
            Stop-Install "Could not rematerialize LF-safe AQG files; the checkout was preserved." $ExitBlocked
        }
        $clean = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
            "-C", $AqgRoot, "status", "--porcelain=v1", "--untracked-files=all"
        ) -Capture
        if ($clean.ExitCode -ne 0 -or -not [string]::IsNullOrWhiteSpace(($clean.Output -join ""))) {
            Stop-Install "AQG files changed while line endings were normalized; the checkout was preserved." $ExitBlocked
        }
    }

    $fetch = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "fetch", "--depth", "1", $AqgRepository,
        ("refs/heads/{0}" -f $AqgRef)
    )
    if ($fetch -ne 0) {
        Stop-Install "Could not read the AQG product target $AqgRef; the existing checkout was preserved."
    }
    $targetResult = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "rev-parse", "--verify", "FETCH_HEAD"
    ) -Capture
    $targetSha = ($targetResult.Output -join "").Trim()
    if ($targetResult.ExitCode -ne 0 -or $targetSha -notmatch "^[0-9a-f]{40}$") {
        Stop-Install "The AQG product target did not resolve to a valid commit; the existing checkout was preserved." $ExitBlocked
    }

    $checkout = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "checkout", "--detach", $targetSha
    )
    if ($checkout -ne 0) {
        Stop-Install "Could not switch the AQG checkout to the approved target; it was preserved." $ExitBlocked
    }
    $normalize = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $AqgRoot, "remote", "set-url", "origin", $AqgRepository
    )
    if ($normalize -ne 0) {
        Stop-Install "AQG was updated, but its origin could not be normalized to the product repository." $ExitBlocked
    }
    $null = Get-VerifiedAqgLayout
}

function Install-AqgDependencies {
    $requirements = Join-Path $AqgRoot "requirements.txt"
    Write-Host ("Installing or verifying Agent Quality Gates runtime dependencies with {0}..." -f $script:PythonPath)
    $venvProbe = Invoke-WithCleanEnvironment -FilePath $script:PythonPath -ArgumentList @(
        "-c", "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)"
    ) -Capture
    $pipArguments = @("-m", "pip", "install")
    if ($venvProbe.ExitCode -ne 0) {
        $pipArguments += "--user"
    }
    $pipArguments += @("-r", $requirements)
    $pipCode = Invoke-WithCleanEnvironment -FilePath $script:PythonPath -ArgumentList $pipArguments
    if ($pipCode -ne 0) {
        Stop-Install "AQG dependency installation failed; Decision Engine was not installed." $ExitBlocked
    }
}

function Repair-AqgCodexHookEntrance {
    $null = Get-VerifiedAqgLayout
    $aqgHookScript = @'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve(strict=True)
logical = Path(sys.argv[1]).absolute()
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
from scripts import install_aqg_clients, install_aqg_codex_hooks

# Match the existing Windows client relay's BOM-tolerant JSON reads.
original_read_text = Path.read_text
def read_text_compatible(path, encoding=None, errors=None):
    text = original_read_text(path, encoding=encoding, errors=errors)
    return text.removeprefix("\ufeff") if encoding == "utf-8" and path.suffix.lower() == ".json" else text
Path.read_text = read_text_compatible
if "codex" not in install_aqg_clients.installed_supported_clients():
    raise SystemExit(0)
arguments = ["--aqg-root", str(logical)]
if install_aqg_codex_hooks.main(["--verify", *arguments]) == 0:
    raise SystemExit(0)
print("Rebinding AQG Codex hooks to the stable managed entrance...")
if install_aqg_codex_hooks.main(["--apply", *arguments]) != 0:
    raise SystemExit(2)
raise SystemExit(install_aqg_codex_hooks.main(["--verify", *arguments]))
'@
    $code = Invoke-PythonScript -PythonPath $script:PythonPath -ScriptText $aqgHookScript `
        -ScriptArguments @($AqgRoot) -WorkingDirectory (Split-Path -Parent $AqgRoot) `
        -Environment @{ "PATH" = "$(Split-Path -Parent $GitPath);$env:PATH"; "AQG_ROOT" = $AqgRoot; "AQG_BASH" = $BashPath }
    if ($code -ne 0) {
        Stop-Install "AQG Codex hooks could not be verified against the stable managed entrance." $ExitBlocked
    }
}

function Invoke-AqgClientWrapper {
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [switch]$Capture
    )

    $bridgeScript = @'
from pathlib import Path
import os
import re
import shlex
import shutil
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
logical_root = Path(sys.argv[1]).absolute()
wrapper_path = Path(sys.argv[2]).resolve()
arguments = sys.argv[3:]
if wrapper_path.parent != root / "scripts":
    raise RuntimeError("AQG wrapper is outside the verified checkout")

os.environ["AQG_ROOT"] = str(logical_root)
os.environ["PYTHONPATH"] = os.pathsep.join((str(root), str(wrapper_path.parent)))
sys.path[:0] = [str(root), str(wrapper_path.parent)]

shim_root = Path(tempfile.mkdtemp(prefix="dp-aqg-python-"))
try:
    executable = Path(sys.executable)
    drive, tail = os.path.splitdrive(str(executable))
    if re.fullmatch(r"[A-Za-z]:", drive) is None:
        raise RuntimeError("verified Python does not have a local Windows drive path")
    msys_executable = "/%s%s" % (drive[0].lower(), tail.replace("\\", "/"))

    relay = shim_root / "run_aqg_child.py"
    relay.write_text(
        "from pathlib import Path\n"
        "import os, runpy, sys\n"
        "original_read_text = Path.read_text\n"
        "def read_text_compatible(path, encoding=None, errors=None):\n"
        "    text = original_read_text(path, encoding=encoding, errors=errors)\n"
        "    if encoding == 'utf-8' and path.suffix.lower() == '.json':\n"
        "        return text.removeprefix('\\ufeff')\n"
        "    return text\n"
        "Path.read_text = read_text_compatible\n"
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
    if re.fullmatch(r"[A-Za-z]:", relay_drive) is None:
        raise RuntimeError("AQG Python relay does not have a local Windows drive path")
    msys_relay = "/%s%s" % (relay_drive[0].lower(), relay_tail.replace("\\", "/"))
    shell_prefix = "python3() { %s %s \"$@\"; }; export -f python3; " % (
        shlex.quote(msys_executable),
        shlex.quote(msys_relay),
    )

    from scripts import install_aqg_clients as wrapper

    if Path(wrapper.__file__).resolve() != wrapper_path:
        raise RuntimeError("AQG wrapper identity mismatch")
    original_run_command = wrapper._run_command
    wrapper._run_command = (
        lambda command, env: original_run_command(shell_prefix + command, env)
    )
    result = wrapper.main(arguments)
finally:
    shutil.rmtree(shim_root, ignore_errors=True)
raise SystemExit(result)
'@
    return Invoke-PythonScript `
        -PythonPath $script:PythonPath `
        -ScriptText $bridgeScript `
        -ScriptArguments (@($AqgRoot) + @($ArgumentList)) `
        -Environment @{ "PATH" = "$(Split-Path -Parent $GitPath);$env:PATH"; "AQG_BASH" = $BashPath } `
        -WorkingDirectory (Split-Path -Parent $AqgRoot) `
        -Capture:$Capture
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

function Invoke-ManagedPermanentSetup {
    if (-not (Test-Path -LiteralPath $ManagedRoot -PathType Container)) {
        Stop-Install "The managed Decision Engine root is missing before activation." $ExitBlocked
    }

    $setupScript = @'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve(strict=True)
entry = (root / "installer" / "permanent_setup.py").resolve(strict=True)
sys.path.insert(0, str(root))

from installer import permanent_setup

if Path(permanent_setup.__file__).resolve(strict=True) != entry:
    raise RuntimeError("managed permanent setup identity mismatch")

show_status_dialog = permanent_setup._show_gui_message

def show_error_dialog_only(title, message, *, error=False):
    if error:
        show_status_dialog(title, message, error=True)

permanent_setup._show_gui_message = show_error_dialog_only
raise SystemExit(permanent_setup.main([]))
'@
    return Invoke-PythonScript `
        -PythonPath $script:PythonPath `
        -ScriptText $setupScript `
        -ScriptArguments @($ManagedRoot) `
        -WorkingDirectory $ManagedRoot
}

function Test-ManagedRootGitState {
    $script:ManagedRootValidationError = $null
    $status = Invoke-WithCleanEnvironment -FilePath $GitPath -ArgumentList @(
        "-C", $ManagedRoot, "status", "--porcelain=v1", "--untracked-files=all"
    ) -Capture
    if ($status.ExitCode -ne 0) {
        $script:ManagedRootValidationError = "managed checkout Git status could not be read"
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
        $script:ManagedRootValidationError = "unexpected Git-visible change: $line"
        return $false
    }
    return $true
}

function Test-CompleteManagedRoot {
    $script:ManagedRootValidationError = $null
    if (-not (Test-Path -LiteralPath $ManagedRoot -PathType Container) -or
        (Test-ReparsePoint -Path $ManagedRoot)) {
        $script:ManagedRootValidationError = "managed root is missing, not a directory, or link-like"
        return $false
    }
    $gitDirectory = Join-Path $ManagedRoot ".git"
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container) -or
        (Test-ReparsePoint -Path $gitDirectory)) {
        $script:ManagedRootValidationError = "managed root does not contain a normal .git directory"
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
            $script:ManagedRootValidationError = "required managed file is missing or link-like: $relativePath"
            return $false
        }
    }
    if (-not (Test-ManagedRootGitState)) {
        return $false
    }

    $validationScript = @'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(root))
from installer import managed_install, update_transaction, updater

reader = updater._GitReader(root)
identity = managed_install.validate_managed_identity(
    root, updater._read_remotes(reader)
)
state = updater._read_update_state(identity.canonical_root)
update_transaction._require_protocol_ready(identity.canonical_root)
_code, head_output = reader.run("head")
head = updater._single_commit(head_output, "managed HEAD")
version = (identity.canonical_root / "VERSION").read_text(encoding="utf-8").strip()
if head != state.last_release_commit:
    print("managed HEAD differs from the protected release state", file=sys.stderr)
    raise SystemExit(1)
if version != state.last_version:
    print("managed VERSION differs from the protected release state", file=sys.stderr)
    raise SystemExit(1)
'@
    $validation = Invoke-PythonScript `
        -PythonPath $script:PythonPath `
        -ScriptText $validationScript `
        -ScriptArguments @($ManagedRoot) `
        -WorkingDirectory $ManagedRoot `
        -Capture
    if ($validation.ExitCode -ne 0) {
        $detail = (($validation.Output | ForEach-Object { [string]$_ }) -join " ").Trim()
        $script:ManagedRootValidationError = if ([string]::IsNullOrWhiteSpace($detail)) {
            "managed identity or update-state validation failed"
        }
        else {
            "managed identity or update-state validation failed: $detail"
        }
        return $false
    }
    return $true
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

root = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(root))
from installer import (
    launcher,
    update_coordination,
    update_transaction,
    updater,
)
from installer.config import ShellError
from installer.release_acquisition import load_trusted_release_keys

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
        if recovery is not None and recovery.status == "deferred_active_session":
            result = recovery
        elif recovery is not None and recovery.status in {
            "repair_required",
            "retry_pending",
            "skipped_locked",
        }:
            raise ShellError(f"managed update recovery returned {recovery.status}")
        else:
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
deferred_statuses = {"deferred_active_session"}
blocker_pids = ",".join(
    str(blocker.pid) for blocker in result.blockers if blocker.pid is not None
)
print(f"{result.status}\t{blocker_pids}")
if result.status not in accepted_statuses | deferred_statuses:
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
    $result = Invoke-PythonScript `
        -PythonPath $script:PythonPath `
        -ScriptText $updateScript `
        -ScriptArguments @($ManagedRoot) `
        -WorkingDirectory $ManagedRoot `
        -Capture
    $acceptedStatuses = @("up_to_date", "candidate_ready", "updated")
    $status = $null
    $blockerPids = @()
    foreach ($rawLine in $result.Output) {
        $line = ([string]$rawLine).TrimEnd("`r", "`n")
        if ($line -match "^([a-z_]+)\t([0-9,]*)$") {
            $status = $Matches[1]
            if (-not [string]::IsNullOrWhiteSpace($Matches[2])) {
                $blockerPids = @($Matches[2].Split(",") | ForEach-Object { [int]$_ })
            }
        }
        elseif (-not [string]::IsNullOrWhiteSpace($line)) {
            [Console]::Error.WriteLine($line)
        }
    }
    return [pscustomobject]@{
        ExitCode = $result.ExitCode
        Status = $status
        BlockerPids = $blockerPids
    }
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

if (Test-ReparsePoint -Path (Join-Path $HOME ".deeppattern")) {
    Stop-Install "$HOME\.deeppattern is a reparse point; preserve it and use an owner-guided install." $ExitBlocked
}
$aqgLayoutInfo = Get-VerifiedAqgLayout

# Repeated installs must update through the signed transaction before the
# temporary main checkout performs bootstrap repair or writes host state.
$managedRootItem = Get-Item -LiteralPath $ManagedRoot -Force -ErrorAction SilentlyContinue
$managedRootWasPresent = $null -ne $managedRootItem
if ($managedRootWasPresent) {
    if (-not (Test-CompleteManagedRoot)) {
        $rootDefect = if ([string]::IsNullOrWhiteSpace($script:ManagedRootValidationError)) {
            "validation failed for an unspecified reason"
        }
        else {
            $script:ManagedRootValidationError
        }
        Stop-Install "$ManagedRoot exists but is not a complete, clean, verified managed stable install. Reason: $rootDefect. Preserve it and use the managed uninstall or replacement flow." $ExitBlocked
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
        Write-Host "Found a complete signed and activated Decision Engine install; checking signed stable before host repair without reopening activation."
    }
    else {
        Write-Host "Found a complete signed Decision Engine stable install with activation pending; checking signed stable before resuming setup. Active MCP sessions will defer the update without blocking activation."
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
        if ($preserved -and
            $managedUpdate.Status -eq "deferred_active_session" -and
            $managedUpdate.BlockerPids.Count -gt 0) {
            $script:UpdateDeferred = $true
            $script:DeferredSessionPids = @($managedUpdate.BlockerPids)
            Write-Host "Active MCP sessions are using the current verified Decision Engine release. The signed update is deferred; setup will continue without stopping Agent applications."
        }
        elseif ($preserved) {
            $failedStatus = if ($null -eq $managedUpdate.Status) { "unknown" } else { $managedUpdate.Status }
            Stop-Install "Signed stable update status $failedStatus; the existing release and activation state were verified and preserved." $ExitFailure
        }
        elseif (-not $preserved) {
            Stop-Install "Signed stable update did not complete and the previous release could not be re-verified. Preserve the managed root and use the owner-guided recovery flow." $ExitBlocked
        }
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
    if (-not $script:UpdateDeferred) {
        Write-Host "Decision Engine signed stable update status: $($managedUpdate.Status)"
    }
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
        "clone", "--config", "core.autocrlf=false", "--config", "core.eol=lf",
        "--depth", "1", "--branch", "main", "--single-branch",
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
    foreach ($required in @(
        "install.sh",
        "AI_SETUP.md",
        "installer\managed_install.py",
        "installer\bootstrap_managed_install.py"
    )) {
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

    Repair-AqgBackupResidue
    $aqgLayoutInfo = Get-VerifiedAqgLayout
    Sync-AqgCheckout -LayoutInfo $aqgLayoutInfo
    Install-AqgDependencies

    $aqgAdapter = Join-Path $AqgRoot "scripts\install_aqg_clients.py"
    $aqgDoctor = Join-Path $AqgRoot "scripts\aqg_doctor.py"
    $aqgVerifyArguments = @(
        $aqgAdapter, "--installed-supported", "--verify",
        "--aqg-root", $AqgRoot, "--home", $HOME
    )
    $aqgApplyArguments = @(
        $aqgAdapter, "--installed-supported", "--apply",
        "--aqg-root", $AqgRoot, "--home", $HOME
    )
    $aqgLayoutAfterSync = Get-VerifiedAqgLayout
    $aqgRouteTimer = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host "Checking existing Agent Quality Gates host routes..."
    $initialAqgVerify = Invoke-AqgClientWrapper -ArgumentList $aqgVerifyArguments -Capture
    $aqgRoutesVerified = ($initialAqgVerify.ExitCode -eq 0 -or $initialAqgVerify.ExitCode -eq 3)
    $aqgManagedLayoutReady = $aqgLayoutAfterSync.Layout -eq "managed"
    if ($aqgRoutesVerified -and $aqgManagedLayoutReady) {
        Write-Host "Agent Quality Gates host routes are already verified; skipping junction recreation."
    }
    else {
        if ($initialAqgVerify.ExitCode -eq 3) {
            Write-Host "No supported Agent host was detected; installing the default Codex routes."
            $aqgApplyArguments = @(
                $aqgAdapter, "--clients", "codex", "--apply",
                "--aqg-root", $AqgRoot, "--home", $HOME
            )
            $aqgVerifyArguments = @(
                $aqgAdapter, "--clients", "codex", "--verify",
                "--aqg-root", $AqgRoot, "--home", $HOME
            )
        }
        Write-Host "Agent Quality Gates host routes need installation or repair; applying them once..."
        $aqgApplyCode = Invoke-AqgClientWrapper -ArgumentList $aqgApplyArguments
        if ($aqgApplyCode -ne 0 -and $aqgApplyCode -ne 3) {
            Stop-Install "AQG host adapter apply failed; Decision Engine was not installed." $ExitBlocked
        }
    }
    Invoke-AqgUpdateContract -Mode migrate
    Repair-AqgCodexHookEntrance
    $aqgVerifyCode = Invoke-AqgClientWrapper -ArgumentList $aqgVerifyArguments
    if ($aqgVerifyCode -ne 0 -and $aqgVerifyCode -ne 3) {
        Stop-Install "AQG host adapter verify failed; Decision Engine was not installed." $ExitBlocked
    }
    $aqgRouteTimer.Stop()
    Write-Host ("Agent Quality Gates host routing ready in {0:N1} seconds." -f $aqgRouteTimer.Elapsed.TotalSeconds)
    $aqgDoctorCode = Invoke-WithCleanEnvironment `
        -FilePath $script:PythonPath `
        -ArgumentList @($aqgDoctor, "--no-cli") `
        -Environment @{ "AQG_ROOT" = $AqgRoot }
    if ($aqgDoctorCode -ne 0) {
        Stop-Install "AQG Doctor reported an unhealthy installation; Decision Engine was not installed." $ExitBlocked
    }

    Write-Host "Installing the signed Decision Engine stable release..."
    if ($managedRootWasPresent) {
        Write-Host "Reusing the existing verified signed Decision Engine checkout without cloning or replacing it."
    }
    else {
        # The public install.sh performs its own newline-delimited host loop.
        # Keep the signed bootstrap in Python and let the validated JSON phase
        # below own all native Windows host wiring.
        Push-Location -LiteralPath $sourceRoot
        try {
            $installCode = Invoke-WithCleanEnvironment -FilePath $script:PythonPath -ArgumentList @(
                "-m", "installer.bootstrap_managed_install", "install"
            )
        }
        finally {
            Pop-Location
        }
        if ($installCode -ne 0) {
            Stop-Install "The core installer failed. Preserve its output and run dp-uninstall.ps1 -Scope both -Apply before a clean retry." $ExitBlocked
        }
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
        $setupCode = Invoke-ManagedPermanentSetup
        Write-Host "Activation window completed; continuing installation..."
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
        "-c", "import json; from installer import mcp_config; print(json.dumps({'clients': mcp_config.detect_clients()}))"
    ) -Capture
    if ($clientsResult.ExitCode -ne 0) {
        Stop-Install "Installed host detection failed." $ExitBlocked
    }
    $wiringFailed = $false
    $configuredClients = New-Object System.Collections.Generic.List[string]
    $skippedClients = New-Object System.Collections.Generic.List[object]
    $validClients = @(
        "claude-code", "claude-desktop", "claude-desktop-3p", "codebuddy",
        "codex", "cursor", "qoder", "qoder-cn", "qoder-ide", "qoder-cn-ide",
        "trae", "trae-work", "trae-cn", "trae-work-cn", "workbuddy", "workbuddy-ai"
    )
    $clients = @()
    $clientsPayload = ($clientsResult.Output | ForEach-Object { [string]$_ }) -join ""
    try {
        $decodedPayload = ConvertFrom-Json -InputObject $clientsPayload
    }
    catch {
        Stop-Install "Installed host detection returned invalid JSON." $ExitBlocked
    }
    $payloadProperties = @($decodedPayload.PSObject.Properties | ForEach-Object { $_.Name })
    if ($payloadProperties.Count -ne 1 -or $payloadProperties[0] -ne "clients") {
        Stop-Install "Installed host detection returned an invalid JSON object." $ExitBlocked
    }
    $decodedClients = $decodedPayload.clients
    if ($null -eq $decodedClients -or $decodedClients -isnot [System.Array]) {
        Stop-Install "Installed host detection did not return a clients array." $ExitBlocked
    }
    foreach ($decodedClient in $decodedClients) {
        if ($null -eq $decodedClient -or $decodedClient -isnot [string]) {
            Stop-Install "Installed host detection returned a non-string client id." $ExitBlocked
        }
        $client = [string]$decodedClient
        if ([string]::IsNullOrWhiteSpace($client) -or $client -match "[\r\n]" -or $client -ne $client.Trim()) {
            Stop-Install "Installed host detection returned an invalid client id." $ExitBlocked
        }
        if ($validClients -notcontains $client) {
            Stop-Install ("Installed host detection returned an unsupported client id: {0}" -f $client) $ExitBlocked
        }
        $clients += $client
    }
    foreach ($client in $clients) {
        $preflightArguments = @(
            "-c",
            "import json, sys; from installer import mcp_config; from installer.config import ShellError; client=sys.argv[1]; expected=client.replace('-', '_') + '_not_installed:'; payload={'client': client}; allow_unactivated=(sys.argv[2] == '1');`ntry:`n result=mcp_config.write_entry(client, allow_unactivated=allow_unactivated, dry_run=True); payload.update({'status': 'ready', 'action': result.get('action'), 'warning': result.get('warning')})`nexcept ShellError as exc:`n reason=str(exc); payload.update({'status': 'not-installed' if reason.startswith(expected) else 'error', 'reason': reason})`nprint(json.dumps(payload, ensure_ascii=True))",
            $client,
            $(if ($activationState -eq "activated") { "0" } else { "1" })
        )
        $preflightResult = Invoke-ManagedPython -Arguments $preflightArguments -Capture
        if ($preflightResult.ExitCode -ne 0) {
            [Console]::Error.WriteLine(("{0}: ERROR: MCP preflight failed for {1}" -f $ProgramName, $client))
            $wiringFailed = $true
            continue
        }
        $preflightPayload = ($preflightResult.Output | ForEach-Object { [string]$_ }) -join ""
        try {
            $preflight = ConvertFrom-Json -InputObject $preflightPayload
        }
        catch {
            [Console]::Error.WriteLine(("{0}: ERROR: MCP preflight returned invalid JSON for {1}" -f $ProgramName, $client))
            $wiringFailed = $true
            continue
        }
        $preflightProperties = @($preflight.PSObject.Properties | ForEach-Object { $_.Name })
        if (($preflightProperties -notcontains "client") -or
            ($preflightProperties -notcontains "status") -or
            $preflight.client -isnot [string] -or
            $preflight.client -ne $client -or
            $preflight.status -isnot [string]) {
            [Console]::Error.WriteLine(("{0}: ERROR: MCP preflight returned an invalid result for {1}" -f $ProgramName, $client))
            $wiringFailed = $true
            continue
        }
        if ($preflight.status -eq "not-installed") {
            if (($preflightProperties -notcontains "reason") -or
                $preflight.reason -isnot [string] -or
                -not $preflight.reason.StartsWith(($client.Replace("-", "_") + "_not_installed:"))) {
                [Console]::Error.WriteLine(("{0}: ERROR: MCP preflight returned an invalid not-installed result for {1}" -f $ProgramName, $client))
                $wiringFailed = $true
                continue
            }
            $skippedClients.Add([pscustomobject]@{
                Client = $client
                Reason = [string]$preflight.reason
            })
            Write-Warning ("Skipping {0}: {1}" -f $client, $preflight.reason)
            continue
        }
        if ($preflight.status -ne "ready") {
            $reason = if (($preflightProperties -contains "reason") -and
                $preflight.reason -is [string] -and
                -not [string]::IsNullOrWhiteSpace($preflight.reason)) {
                [string]$preflight.reason
            }
            else {
                "adapter preflight rejected the host"
            }
            [Console]::Error.WriteLine(("{0}: ERROR: MCP preflight failed for {1}: {2}" -f $ProgramName, $client, $reason))
            $wiringFailed = $true
            continue
        }
        $wireArguments = @("-m", "installer.mcp_config", "--write", "--client", $client)
        if ($activationState -ne "activated") {
            $wireArguments += "--allow-unactivated"
        }
        $wireCode = Invoke-ManagedPython -Arguments $wireArguments
        if ($wireCode -ne 0) {
            [Console]::Error.WriteLine(("{0}: ERROR: MCP wiring failed for {1}" -f $ProgramName, $client))
            $wiringFailed = $true
            continue
        }
        $configuredClients.Add($client)
    }
    $routeScript = @'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(root))
from installer import config, install, mcp_config
from installer.config import ShellError

clients = sys.argv[2:]
if (
    not isinstance(clients, list)
    or not all(isinstance(client, str) and client for client in clients)
    or len(clients) != len(set(clients))
):
    raise SystemExit("configured client list is invalid")

body_root = config.managed_component_root("decision-engine")
routed = {}
failed = {}
with install.install_lock(blocking=False):
    for client in clients:
        spec = mcp_config.CLIENT_SPECS.get(client)
        if spec is None:
            failed[client] = "host specification is unavailable"
            continue
        if not spec.repair_skills_on_setup or spec.skill_delivery_mode == "none":
            continue
        if spec.skills_global_path is None:
            failed[client] = "skill destination is unavailable"
            continue
        try:
            destination = spec.skills_global_path()
            install._preflight_skill_routes(
                "decision-engine",
                body_root / "skills",
                body_root / "skills",
                destination,
                spec.excluded_skills,
            )
            routed[client] = install._route_skills(
                "decision-engine",
                body_root,
                dest_root=destination,
                excluded_skills=spec.excluded_skills,
            )
        except (ShellError, OSError) as exc:
            failed[client] = install._skill_route_failure_reason(exc)

print(json.dumps({"routed": routed, "failed": failed}, sort_keys=True))
raise SystemExit(1 if failed else 0)
'@
    $routeCode = Invoke-PythonScript `
        -PythonPath $script:PythonPath `
        -ScriptText $routeScript `
        -ScriptArguments (@($ManagedRoot) + @([string[]]$configuredClients.ToArray())) `
        -WorkingDirectory $ManagedRoot
    if ($routeCode -ne 0) {
        [Console]::Error.WriteLine(("{0}: ERROR: managed skill routing failed" -f $ProgramName))
        $wiringFailed = $true
    }
    if ($wiringFailed) {
        Stop-Install "Core installation is complete, but one or more host integrations failed." $ExitBlocked
    }

    $aqgVerifyFailed = $false

    $doctorCode = Invoke-ManagedPython -Arguments @("-m", "installer.doctor")
    $doctorVerifyFailed = $doctorCode -ne 0
    if ($activationState -eq "activated" -and $doctorCode -ne 0) {
        Stop-Install "Decision Engine is activated, but Doctor reports a failure." $ExitBlocked
    }
    elseif ($doctorVerifyFailed) {
        Write-Warning "Decision Engine Doctor could not verify the unactivated DE Lite installation; review the Doctor output above."
    }

    Write-Host "Decision Engine host capability report:"
    foreach ($client in $configuredClients) {
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
    foreach ($skipped in $skippedClients) {
        Write-Host ("{0}: DE MCP=not-configured; runtime=not-applicable; reason={1}" -f $skipped.Client, $skipped.Reason)
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

    if ($script:UpdateDeferred) {
        Write-Host "Decision Engine deferred update report:"
        Write-Host ("Activation: {0}" -f $(if ($activationState -eq "activated") { "complete" } else { "pending" }))
        Write-Host "DE update: deferred because Agent MCP sessions are active"
        Write-Host "Active MCP sessions recorded when this update was deferred:"
        foreach ($processId in $script:DeferredSessionPids) {
            $snapshot = Get-ProcessSnapshot -ProcessId $processId
            if ($null -eq $snapshot) {
                Write-Host ("  PID={0} likely-host=session exited after deferral" -f $processId)
                continue
            }
            Write-Host ("  PID={0} likely-host={1} parent-pid={2} executable={3}" -f
                $processId,
                (Get-LikelyHostForProcess -ProcessId $processId),
                $snapshot.ParentProcessId,
                $snapshot.ExecutablePath)
        }
        Write-Host "Fully quit every listed Agent application before reopening any of them."
        Write-Host "The first new MCP session will retry the pending signed update."
    }

    Write-Host ("{0}: source={1}" -f $ProgramName, $sourceSha)
    Write-Host ("{0}: activation={1}" -f $ProgramName, $activationState)
    Write-Host ("{0}: restart every configured host before runtime verification" -f $ProgramName)
    if ($unsupported.Count -gt 0 -or $skippedClients.Count -gt 0 -or $aqgVerifyFailed -or $doctorVerifyFailed -or $script:AqgUpdatePending) {
        if ($unsupported.Count -gt 0) {
            Write-Host "Unsupported or unconfigured installed products:"
            foreach ($item in $unsupported) {
                Write-Host $item
            }
        }
        [Console]::Error.WriteLine(("{0}: PARTIAL: supported components were installed, but host verification or AQG automatic update compatibility remains incomplete." -f $ProgramName))
        exit $ExitPartial
    }
    if ($script:UpdateDeferred) {
        if ($activationState -eq "activated") {
            Write-Host ("{0}: SUCCESS_WITH_RESTART_REQUIRED: device activation and host configuration are complete; the current verified DE release was preserved and its signed update remains pending." -f $ProgramName)
            exit 0
        }
        [Console]::Error.WriteLine(("{0}: PARTIAL: device activation and the signed DE update remain pending; the current verified release and host configuration were preserved." -f $ProgramName))
        exit $ExitPartial
    }
    Write-Host ("{0}: PASS: installation completed; runtime verification requires host restart" -f $ProgramName)
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
