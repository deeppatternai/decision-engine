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
Git or Python prerequisites can be installed through WinGet after explicit
confirmation. Repeated installs can identify and offer to terminate only
verified current-user DE MCP launcher processes before one immediate retry.

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
    [Console]::Error.WriteLine(("{0}: ERROR: {1}" -f $ProgramName, $Message))
    exit $Code
}

function Test-NativeWindows {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function Confirm-UserAction {
    param([Parameter(Mandatory = $true)][string]$Message)

    try {
        $answer = Read-Host ("{0} [y/N]" -f $Message)
    }
    catch {
        return $false
    }
    return $answer -match "^(?i:y|yes)$"
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
    $remove = @(
        "DE_ENDPOINT", "DE_ACTIVATION_SECRET", "PYTHONPATH", "PROJECT_ROOT",
        "AQG_ROOT", "AQG_STATE_ROOT",
        "CLAUDE_DESKTOP_CONFIG", "WORKBUDDY_APP_ROOT", "WORKBUDDY_CONFIG",
        "WORKBUDDY_SKILLS_DIR", "BASH_ENV", "ENV"
    )
    $saved = @{}
    foreach ($name in ($remove + @($effectiveEnvironment.Keys) | Select-Object -Unique)) {
        $saved[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
        [System.Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    foreach ($name in $effectiveEnvironment.Keys) {
        [System.Environment]::SetEnvironmentVariable(
            [string]$name,
            [string]$effectiveEnvironment[$name],
            "Process"
        )
    }

    try {
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
            [System.Environment]::SetEnvironmentVariable(
                [string]$name,
                $saved[$name],
                "Process"
            )
        }
    }
}

function Invoke-PythonScript {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ScriptText,
        [Parameter()][string[]]$ScriptArguments = @(),
        [string]$WorkingDirectory,
        [switch]$Capture
    )

    $scriptPath = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) ("dp-install-python-" + [Guid]::NewGuid().ToString("N") + ".py")
    $source = @"
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
$ScriptText
"@
    [IO.File]::WriteAllText($scriptPath, $source, $script:Utf8NoBom)
    try {
        [string[]]$arguments = @($scriptPath) + @($ScriptArguments)
        if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
            Push-Location -LiteralPath $WorkingDirectory
        }
        try {
            return Invoke-WithCleanEnvironment `
                -FilePath $PythonPath `
                -ArgumentList $arguments `
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
    $command = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return $null
    }
    return (Get-Item -LiteralPath $command.Source).FullName
}

function Get-GitCandidates {
    $candidates = New-Object System.Collections.Generic.List[string]
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        $candidates.Add($command.Source)
    }
    foreach ($root in @(
        [Environment]::GetEnvironmentVariable("ProgramFiles"),
        [Environment]::GetEnvironmentVariable("ProgramFiles(x86)"),
        $env:LOCALAPPDATA
    )) {
        if ([string]::IsNullOrWhiteSpace($root)) {
            continue
        }
        $candidates.Add((Join-Path $root "Git\cmd\git.exe"))
    }
    return @($candidates | Select-Object -Unique)
}

function Find-Git {
    foreach ($candidate in (Get-GitCandidates)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $result = Invoke-WithCleanEnvironment -FilePath $candidate -ArgumentList @("--version") -Capture
        $joined = ($result.Output -join "`n")
        if ($result.ExitCode -ne 0 -or $joined -notmatch "git version ([0-9]+)\.([0-9]+)") {
            continue
        }
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -gt 2 -or ($major -eq 2 -and $minor -ge 45)) {
            return (Get-Item -LiteralPath $candidate).FullName
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
    Write-Host "Git for Windows 2.45 or newer is missing."
    Install-WinGetPackage -PackageId "Git.Git" -DisplayName "Git for Windows 2.45 or newer"
    $git = Find-Git
    if ([string]::IsNullOrWhiteSpace($git)) {
        Stop-Install "WinGet finished, but Git for Windows 2.45 or newer could not be verified. Open a new PowerShell window and retry."
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

function Find-Python {
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
    foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
        if ([string]::IsNullOrWhiteSpace($root)) {
            continue
        }
        foreach ($relative in @(
            "Programs\Python\Python314\python.exe",
            "Programs\Python\Python313\python.exe",
            "Programs\Python\Python312\python.exe",
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
                "-c", "import os, sys; print(os.path.abspath(sys.executable))"
            ) -Capture
            if ($identity.ExitCode -eq 0 -and $identity.Output.Count -gt 0) {
                return ([string]$identity.Output[$identity.Output.Count - 1]).Trim()
            }
        }
    }
    return $null
}

function Resolve-Python {
    $python = Find-Python
    if (-not [string]::IsNullOrWhiteSpace($python)) {
        return $python
    }
    Write-Host "Python 3.12 or newer with ssl, venv, pip, and tkinter is missing."
    Install-WinGetPackage -PackageId "Python.Python.3.13" -DisplayName "Python 3.13"
    $python = Find-Python
    if ([string]::IsNullOrWhiteSpace($python)) {
        Stop-Install "WinGet finished, but Python 3.12 or newer with ssl, venv, pip, and tkinter could not be verified. Open a new PowerShell window and retry."
    }
    Write-Host ("Python prerequisite ready: {0}" -f $python)
    return $python
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
    or re.fullmatch(r"[0-9a-f]{40}", target.name) is None
    or not target.is_dir()
):
    raise SystemExit(1)
print(target)
'@
        $resolved = Invoke-PythonScript `
            -PythonPath $script:PythonPath `
            -ScriptText $resolveScript `
            -ScriptArguments @($AqgRoot, $versionsRoot) `
            -Capture
        if ($resolved.ExitCode -ne 0 -or $resolved.Output.Count -ne 1) {
            Stop-Install "$AqgRoot is not an AQG-managed versions\<commit> junction; preserve it and stop." $ExitBlocked
        }
        $target = ([string]$resolved.Output[0]).Trim()
        $layout = "managed"
    }

    foreach ($relativePath in @(
        ".git",
        "AI_SETUP.md",
        "scripts\install_aqg_clients.py",
        "scripts\aqg_doctor.py",
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
    if ($layout -eq "managed" -and
        -not [string]::Equals((Split-Path -Leaf $target), $head, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Install "$AqgRoot target name does not match its checked-out commit; it was preserved." $ExitBlocked
    }
    return [pscustomobject]@{ Layout = $layout; Target = $target; Head = $head }
}

function Sync-AqgCheckout {
    param([Parameter(Mandatory = $true)]$LayoutInfo)

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
    if ($LayoutInfo.Layout -eq "managed") {
        if ($LayoutInfo.Head -ne $targetSha) {
            Stop-Install "AQG managed install is at $($LayoutInfo.Head) but $AqgRef is $targetSha; preserve the versioned layout and use AQG's transactional multi-host update flow." $ExitBlocked
        }
        Write-Host ("AQG managed checkout already matches {0} at {1}; preserving the versioned layout." -f $AqgRef, $targetSha)
        return
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
wrapper_path = Path(sys.argv[2]).resolve()
arguments = sys.argv[3:]
if wrapper_path.parent != root / "scripts":
    raise RuntimeError("AQG wrapper is outside the verified checkout")

os.environ["AQG_ROOT"] = str(root)
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
        -WorkingDirectory $AqgRoot `
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
blocker_pids = ",".join(
    str(blocker.pid) for blocker in result.blockers if blocker.pid is not None
)
print(f"{result.status}\t{blocker_pids}")
if result.status not in accepted_statuses:
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

function Test-ManagedLeasePid {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $script = @'
from pathlib import Path
import sys
from installer import update_coordination

root = Path(sys.argv[1])
expected = int(sys.argv[2])
live = update_coordination.live_shim_sessions(root)
raise SystemExit(0 if any(item.pid == expected for item in live) else 1)
'@
    $probe = Invoke-PythonScript `
        -PythonPath $script:PythonPath `
        -ScriptText $script `
        -ScriptArguments @($ManagedRoot, [string]$ProcessId) `
        -WorkingDirectory $ManagedRoot `
        -Capture
    return $probe.ExitCode -eq 0
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
            "^(?i:codebuddy.*\.exe)$" { return "CodeBuddy" }
            "^(?i:codex.*\.exe)$" { return "Codex" }
            "^(?i:cursor.*\.exe)$" { return "Cursor" }
            "^(?i:qoder.*\.exe)$" { return "Qoder" }
            "^(?i:trae.*\.exe)$" { return "TRAE" }
            "^(?i:workbuddy.*\.exe)$" { return "WorkBuddy" }
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

function Resolve-ActiveManagedSessions {
    param([Parameter(Mandatory = $true)][int[]]$ProcessIds)

    Write-Host "Decision Engine is still in use by the following active MCP session(s):"
    foreach ($processId in $ProcessIds) {
        $snapshot = Get-ProcessSnapshot -ProcessId $processId
        $parent = if ($null -eq $snapshot) { "unknown" } else { [string]$snapshot.ParentProcessId }
        $hostName = Get-LikelyHostForProcess -ProcessId $processId
        Write-Host ("  PID={0} likely-host={1} parent-pid={2}" -f $processId, $hostName, $parent)
    }
    Write-Host "Completely quit the listed Agent host applications; closing only their windows is not sufficient."
    try {
        $answer = Read-Host "After quitting them, press Enter to recheck immediately (or type N to stop)"
    }
    catch {
        return $false
    }
    if ($answer -match "^(?i:n|no|q|quit)$") {
        return $false
    }

    $remaining = @($ProcessIds | Where-Object { Test-ManagedLeasePid -ProcessId $_ })
    if ($remaining.Count -eq 0) {
        return $true
    }

    $frozen = New-Object System.Collections.Generic.List[object]
    foreach ($processId in $remaining) {
        $snapshot = Get-ProcessSnapshot -ProcessId $processId
        if ($null -ne $snapshot -and
            (Test-ManagedLauncherSnapshot -Snapshot $snapshot) -and
            (Test-ManagedLeasePid -ProcessId $processId)) {
            $frozen.Add($snapshot)
        }
    }
    if ($frozen.Count -ne $remaining.Count) {
        Write-Host "The remaining process identity is not safe to terminate automatically. Quit the Agent host and rerun the installer."
        return $false
    }

    Write-Host "The remaining process(es) are current-user managed DE MCP launchers:"
    foreach ($snapshot in $frozen) {
        Write-Host ("  PID={0} likely-host={1}" -f $snapshot.ProcessId, (Get-LikelyHostForProcess -ProcessId $snapshot.ProcessId))
    }
    Write-Host "Stop-Process will target only these DE MCP launcher processes, not the Agent applications."
    if (-not (Confirm-UserAction -Message "Terminate these verified managed DE sessions and retry?")) {
        return $false
    }

    foreach ($snapshot in $frozen) {
        $current = Get-ProcessSnapshot -ProcessId $snapshot.ProcessId
        if ($null -eq $current -or
            -not (Test-SameProcessSnapshot -Expected $snapshot -Actual $current) -or
            -not (Test-ManagedLauncherSnapshot -Snapshot $current) -or
            -not (Test-ManagedLeasePid -ProcessId $snapshot.ProcessId)) {
            Write-Host ("Termination refused for PID={0} because its identity changed or ownership could not be re-proven." -f $snapshot.ProcessId)
            return $false
        }
        try {
            Stop-Process -Id $snapshot.ProcessId -Force -ErrorAction Stop
            Write-Host ("Terminated verified managed DE session PID={0}." -f $snapshot.ProcessId)
        }
        catch {
            Write-Host ("Termination failed for PID={0}: {1}" -f $snapshot.ProcessId, $_.Exception.GetType().Name)
            return $false
        }
    }
    return $true
}

function Invoke-ManagedStableUpdateWithRetry {
    $result = Invoke-ManagedStableUpdate
    if ($result.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($result.Status)) {
        return $result
    }
    if ($result.Status -eq "deferred_active_session" -and $result.BlockerPids.Count -gt 0) {
        if (Resolve-ActiveManagedSessions -ProcessIds $result.BlockerPids) {
            Write-Host "Retrying the signed stable update once..."
            return Invoke-ManagedStableUpdate
        }
    }
    return $result
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
        Write-Host "Found a complete signed and activated Decision Engine install; updating signed stable before host repair without reopening activation."
    }
    else {
        Write-Host "Found a complete signed Decision Engine stable install with activation pending; updating signed stable before resuming setup."
    }
    Write-Host "Checking and applying the newest signed Decision Engine stable release before continuing..."
    $managedUpdate = Invoke-ManagedStableUpdateWithRetry
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
            Stop-Install "Signed stable update status $failedStatus; the existing release and activation state were verified and preserved. The guided close-and-retry flow did not clear every active session." $ExitFailure
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
        $aqgVerifyCode = Invoke-AqgClientWrapper -ArgumentList $aqgVerifyArguments
        if ($aqgVerifyCode -ne 0 -and $aqgVerifyCode -ne 3) {
            Stop-Install "AQG host adapter verify failed; Decision Engine was not installed." $ExitBlocked
        }
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
    # The public install.sh performs its own newline-delimited host loop. Native
    # Windows can surface CRLF client IDs through that Bash boundary, so keep the
    # signed bootstrap in Python and let the validated JSON phase below own all
    # host wiring.
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

from installer import config, install, mcp_config
from installer.config import ShellError

clients = sys.argv[1:]
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
        -ScriptArguments ([string[]]$configuredClients.ToArray()) `
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

    Write-Host ("{0}: source={1}" -f $ProgramName, $sourceSha)
    Write-Host ("{0}: activation={1}" -f $ProgramName, $activationState)
    Write-Host ("{0}: restart every configured host before runtime verification" -f $ProgramName)
    if ($unsupported.Count -gt 0 -or $skippedClients.Count -gt 0 -or $aqgVerifyFailed -or $doctorVerifyFailed) {
        if ($unsupported.Count -gt 0) {
            Write-Host "Unsupported or unconfigured installed products:"
            foreach ($item in $unsupported) {
                Write-Host $item
            }
        }
        [Console]::Error.WriteLine(("{0}: PARTIAL: supported components were installed, but one or more detected hosts are unsupported, unconfigured, or unverifiable." -f $ProgramName))
        exit $ExitPartial
    }
    Write-Host ("{0}: PASS: installation completed; runtime verification requires host restart" -f $ProgramName)
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
