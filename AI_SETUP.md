# Decision Engine - Automatic Setup with Auto-Updates (hand this to your AI agent)

English | [简体中文](AI_SETUP.zh-CN.md)

> **How to use**: give this entire file to any supported host or another coding agent that
> can operate your terminal, then say: "Install Decision Engine according to this guide."
> The agent installs or repairs AQG, runs AQG Doctor, completes AQG's own standalone configuration,
> installs the credential-free `decision-engine` MCP transport and skills, activates the device
> when owner values are available, and verifies the installation; do not hand-edit MCP configuration.
>
> The agent checks the machine, obtains trusted installer source, completes the AQG standalone flow,
> installs Decision Engine, wires the DE Lite-capable MCP transport, and runs Doctor. Permanent
> activation unlocks hosted capabilities. Decision Engine then checks for and applies signed
> `stable` releases when the agent starts.
>
> This guide is only for a **first-time installation**. If
> `~/.deeppattern/decision-engine` already exists, stop and report it. Preserve the existing path;
> migration and recovery are intentionally outside this guide.

---

## Before installation

Required:

- Python 3.12 or later;
- Git 2.45 or later;
- at least one target client from the registered-host table below;
- while the repository is private, the user's GitHub account must have repository read access and
  ordinary Git authentication must already work.

The installer accepts these exact client IDs and paths:

| Host | `--client` ID | User MCP config | Managed Skills | Platform scope |
|---|---|---|---|---|
| Claude Code | `claude-code` | `~/.claude.json` | `~/.claude/skills` | supported platforms |
| Claude Desktop | `claude-desktop` | Windows: `%APPDATA%/Claude/claude_desktop_config.json`<br>macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`<br>Linux: `~/.config/Claude/claude_desktop_config.json` | none | supported platforms |
| Claude third-party provider profile | `claude-desktop-3p` | `~/Library/Application Support/Claude-3p/claude_desktop_config.json` | none | macOS only; independent from Claude Desktop |
| Tencent CodeBuddy Agent CLI | `codebuddy` | `~/.codebuddy/mcp.json` | `~/.codebuddy/skills` | independent Agent CLI only; CodeBuddy Studio is a separate unsupported product |
| Codex | `codex` | `~/.codex/config.toml` | `~/.codex/skills` | supported platforms on builds that load global Skills |
| Cursor | `cursor` | `~/.cursor/mcp.json` | `~/.cursor/skills` | supported platforms |
| Alibaba Qoder Desktop | `qoder` | `~/.qoder/mcp.json` (Windows); `~/.qoder/settings.json` (macOS) | `~/.qoder/skills` | Windows Desktop 1.106.3+; macOS Qoder.app 0.1.3+ |
| Alibaba Qoder CN Desktop | `qoder-cn` | `~/.qoder-cn/settings.json` | `~/.qoder-cn/skills` | macOS Qoder CN.app 0.1.4 only |
| Alibaba Qoder IDE | `qoder-ide` | `~/.qoder/mcp.json` | `~/.qoder/skills` | macOS Qoder IDE.app 1.106.3+; shares family Skills and hook with Qoder Desktop |
| Alibaba Qoder CN IDE | `qoder-cn-ide` | `~/.qoder-cn/mcp.json` | `~/.qoder-cn/skills` | macOS Qoder CN IDE.app 1.106.3+; shares family Skills and hook with Qoder CN Desktop |
| TRAE Desktop | `trae` | `~/Library/Application Support/Trae/User/mcp.json` | `~/.trae/skills` | macOS Trae.app 3.5.81 |
| TRAE Work | `trae-work` | Windows: `%APPDATA%/TRAE SOLO/User/mcp.json`<br>macOS: `~/Library/Application Support/TRAE SOLO/User/mcp.json` | `~/.trae/skills` | Windows and macOS Desktop 0.1.48+ |
| TRAE CN Desktop | `trae-cn` | Windows: `%APPDATA%/Trae CN/User/mcp.json`<br>macOS: `~/Library/Application Support/Trae CN/User/mcp.json` | `~/.trae-cn/skills` | Windows Trae Code CN 3.3.98+; macOS Trae CN.app 3.3.95 |
| TRAE Work CN | `trae-work-cn` | Windows: `%APPDATA%/TRAE SOLO CN/User/mcp.json`<br>macOS: `~/Library/Application Support/TRAE SOLO CN/User/mcp.json` | `~/.trae-cn/skills` | Windows and macOS Desktop 0.1.48+ |
| Tencent WorkBuddy Desktop | `workbuddy` | `~/.workbuddy/mcp.json` | `~/.workbuddy/skills` | Windows and macOS Desktop |
| Tencent WorkBuddy AI Desktop | `workbuddy-ai` | `~/.workbuddy-ai/mcp.json` | `~/.workbuddy-ai/skills` | macOS WorkBuddy AI.app 5.5.2+ only |

Graphic Explanation popup follow-up is provided through the API and does not require a
host CLI. The shared native Graphic Explanation / Discussion Board windows and audit Stop Panel
remain available for supported hosts.

Decision Engine is owner-hosted. Audit, graphics, boards, and other hosted features require two
values issued by the service owner:

- `DE_ENDPOINT`: the owner's HTTPS service endpoint;
- `DE_ACTIVATION_SECRET`: the owner's device activation key.

These values are **optional for the core installation**. The installer may register the local,
credential-free MCP transport before activation so DE Lite is reachable; hosted features remain
unavailable until permanent activation succeeds. Never ask the user to paste either real value into
chat, a command line, or a normal file.

If both values already exist in the agent's current process environment, installation adopts them
without a dialog and without putting the secret in command arguments. Successful activation stores
the endpoint and server-issued device credentials permanently; it never stores the original
activation secret. If either value is unavailable, AQG is installed or repaired, its standalone
configuration is completed, and AQG Doctor passes before the Decision Engine core may be installed.
The local MCP entry may be present, but it carries no endpoint or credential and exposes only the
allowed DE Lite path until activation. The agent resumes at step 5 after the values are obtained;
reinstallation is unnecessary.

Windows User environment variables written from PowerShell are inherited only by newly started
processes. A stale agent process may not see them; this does not block installation and step 5 can
complete permanent setup afterward.

---

## Instructions for the AI agent

> AI: execute and verify each step. Before cloning, running installer code, or writing agent
> configuration, briefly state what you are about to do. The user's request to follow this guide
> authorizes the normal installation steps below.
>
> A core installation, Git authentication, signature verification, fixed-path, or MCP wiring
> failure must stop the flow and be reported accurately. Optional GUI preparation or device
> activation failure must also be reported, but must not roll back a completed core installation.
>
> Never print, log, commit, or copy into your context a real endpoint, activation key, device token,
> Git credential, or complete configuration file. Never ask the user to send a secret in chat.
>
> A dirty or diverged installer checkout does not block the whole installation: preserve it and
> clone a clean checkout into a new directory. An existing or abnormal fixed installation path does
> block this first-install guide: preserve it, stop, and report it. Never reset, stash, clean,
> delete, move, or overwrite either path.

### 0. Check the environment and target clients

If owner values already exist in the agent process, treat them as restricted input. Every child
process must run with both values removed except the install commands in steps 3-4 and the explicit
`permanent_setup --from-env` command in step 5. The Bash commands below use `env -u` for that reason.
Do not read or copy either value while sanitizing the child environment.

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git --version
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 --version
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -c "import sys, ssl, venv; print(sys.executable)"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -m pip --version
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -c "import tkinter; print('tkinter: OK')"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -c "import webview; print('pywebview: OK')" || echo "pywebview: not installed yet (optional)"
```

- Do not treat the first `python3` on `PATH` as the complete machine check. Enumerate all
  `python3.*`, `python3`, and `python` candidates (for example, `type -a python3 python3.12 python`)
  and run the version and module probes against each candidate. macOS may put Apple's Python 3.9
  before a compatible Homebrew or user-managed Python; a failing first candidate is not evidence
  that Python must be installed.
  Select the first candidate that passes every required probe, record its exact absolute path, and
  set `DE_PYTHON` to that path before continuing.
- Python 3.12 is the hard minimum. The agent must detect the interpreter, record its exact absolute
  path, and verify the version plus `ssl`, `venv`, `pip`, and `tkinter`. Reuse any existing Python
  >=3.12 that passes those probes; a newer version is valid and must not trigger installation or
  downgrade. Report the selected version and continue without a version-choice prompt. If Python is
  absent, below 3.12, or lacks required modules, explain the finding and ask: "May I install and
  verify the latest available stable Python 3.x release that meets the 3.12+ requirement, then
  continue?" After authorization, use the current python.org installer or a user-approved package
  manager on Windows/macOS. On Linux, use the distribution package manager and install the latest
  available stable Python 3.x meeting the 3.12+ requirement plus its matching Tk package (commonly
  `python3-tk`, but use the detected distribution's package name). Verify the resulting interpreter
  before continuing. Allow the user to approve any OS elevation or installer UI, then rerun every
  check. If authorization is declined, stop. This applies to native Windows and macOS/Linux.
- On macOS/Linux/WSL and native Windows Git Bash, unconditionally bind the verified interpreter for
  all later commands, whether it was pre-existing or newly installed. Replace the placeholder with
  the exact path printed by the verified interpreter:

  ```bash
  export DE_PYTHON="<absolute path printed by step 0>"
  "$DE_PYTHON" -c "import sys, ssl, venv, tkinter; print(sys.executable)"
  ```

  Native Windows PowerShell uses the equivalent `$PythonPath` variable shown in step 5.
- Git must be 2.45+. If Git is too old, stop and ask the user to upgrade it.
- `tkinter` provides step 5's masked setup window. If it is unavailable, do not collect secrets by
  another route. Python/Tk readiness is a prerequisite for this installation flow, even when owner
  values will be supplied later.
- `pywebview` provides board, diagram, comic, and other visual windows. A missing package is not an
  installation failure: setup proactively attempts to install it in the MCP Python, first visual
  use retries, and Doctor distinguishes a missing package from an unavailable native GUI backend.
- Native Windows clients must use Git Bash (from Git for Windows) for `install.sh`; do not translate
  it into PowerShell. WSL is supported only when every selected client runs inside that same WSL
  distribution. Do not install under WSL for a native Windows Codex, Claude Code, Claude Desktop,
  Cursor, TRAE Work, TRAE Work CN, WorkBuddy, or Qoder host. Creating skill symlinks on native
  Windows also requires Developer Mode or an Administrator
  terminal. If skill linking fails specifically with `WinError 1314`, enable Developer Mode and
  rerun step 3; preserve the valid partial installation instead of deleting it.
- Detect the target clients from the MCP/product locations in the table above. On Windows this
  includes `%APPDATA%/TRAE SOLO/User/`, `%APPDATA%/TRAE SOLO CN/User/`,
  `%LOCALAPPDATA%/Programs/TRAE SOLO/`, `%LOCALAPPDATA%/Programs/TRAE SOLO CN/`,
  `%LOCALAPPDATA%/Programs/WorkBuddy/`, and `%LOCALAPPDATA%/Programs/Qoder/`; do not infer one
  product from another product's directory. Match the two TRAE directory names exactly:
  `TRAE SOLO CN` is not evidence that `TRAE SOLO` is installed. Prefer the client currently running
  this guide. On macOS, probe the exact bundles `/Applications/Qoder.app`,
  `/Applications/Qoder CN.app`, `/Applications/Trae.app`, `/Applications/Trae CN.app`,
  `/Applications/TRAE SOLO.app`, `/Applications/TRAE SOLO CN.app`, and
  `/Applications/WorkBuddy.app`; do not infer one bundle from another. Intel and Apple Silicon use
  these same paths and configuration contracts.
  The normal permanent setup wires every selected client after activation succeeds, so show the detected list
  and confirm it with the user before step 3. This confirmation is all-or-stop: if the user declines
  any detected host, stop before step 3 instead of silently selecting it. If uncertain, ask rather
  than guessing.

Before running any Decision Engine core-install command, ask only whether both owner values are
already available; never ask for the values themselves:

> Do you already have the endpoint and device activation key from the Decision Engine owner? Reply
> only "Yes" or "No". Do not send the real values in chat.

If the host supports a structured choice prompt, present `Yes` and `No` as the two selectable
options and use the selected value as the explicit answer. Otherwise, in this English-language
flow, accept only the literal answers `Yes` or `No` (case-insensitive); do not infer an answer from
machine state.

Require an explicit "Yes" or "No" answer and remember only that answer. Do not infer "No" from
missing `DE_ENDPOINT` or `DE_ACTIVATION_SECRET` environment variables, missing configuration,
an unactivated device, user silence, or any other machine state. Any response other than an explicit
"Yes" or "No" leaves the owner-value status unknown; ask again and do not begin the Decision Engine
core installation. AQG installation or repair, AQG standalone configuration, and a passing AQG
Doctor always run before Decision Engine core installation. If the answer is "No", continue the
core installation with only the credential-free DE Lite MCP transport and finish with the exact
deferred-activation wording in step 8.

While the repository is private, verify the user's ordinary GitHub access:

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git ls-remote \
  https://github.com/deeppatternai/decision-engine.git \
  refs/heads/stable refs/heads/main
```

Both refs must be returned: `main` supplies the install driver and `stable` supplies the signed
release metadata.

Git Credential Manager, macOS Keychain, or a browser may open the normal user sign-in flow. The
user must complete it. Never request a GitHub token, password, or SSH private key, and never embed a
credential in a URL or command.

### 1. Require an empty fixed installation path

```bash
DE_INSTALL_ROOT="$HOME/.deeppattern/decision-engine"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET test ! -e "$DE_INSTALL_ROOT" \
  && env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET test ! -L "$DE_INSTALL_ROOT"
```

Continue only if this succeeds. If the path already exists as a directory, file, symlink, broken
symlink, junction/reparse point, or anything ambiguous, preserve it and stop. Report that this is
not a first-time installation; do not run migration or recovery from this guide and do not use
`DE_DEV_MODE=1` to bypass the check.

### 2. Prepare a clean installer checkout (`DE_ROOT`)

`DE_ROOT` is only the trusted source directory used to run `install.sh`. It is not the final
installation path. Prefer, in order:

1. an existing `$DE_ROOT` when it is set and the directory exists;
2. a Decision Engine checkout explicitly named by the user;
3. a new clone.

When no suitable checkout exists, clone to a path separate from the fixed installation path:

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET mkdir -p "$HOME/.deeppattern"
DE_ROOT="${DE_ROOT:-$HOME/.deeppattern/decision-engine-root}"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git clone \
  --branch main \
  https://github.com/deeppatternai/decision-engine.git "$DE_ROOT"
```

`main` is the authoritative source of the trusted `install.sh` driver; step 3 independently resolves
and signature-verifies the release published through `stable` for the fixed installed copy.

For an existing candidate, inspect it without changing it. Compare the origin in memory and print
it only after it exactly matches one of the credential-free expected URLs; never echo an arbitrary
remote URL:

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" status --porcelain
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" branch --show-current
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" rev-list --left-right --count 'HEAD...@{upstream}'
DE_ORIGIN_URL="$(env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" remote get-url origin)"
case "$DE_ORIGIN_URL" in
  https://github.com/deeppatternai/decision-engine.git|git@github.com:deeppatternai/decision-engine.git|ssh://git@github.com/deeppatternai/decision-engine.git)
    printf 'origin: %s (expected)\n' "$DE_ORIGIN_URL"
    ;;
  *)
    echo "origin: unexpected; preserve this checkout and use a fresh directory"
    ;;
esac
unset DE_ORIGIN_URL
```

Use it only when it is the expected Decision Engine checkout on `main`, its upstream is exactly
`origin/main`, the worktree is clean, and it has no local-only
commits (`ahead=0`). Then update it with:

Do not repoint or reuse a checkout that still tracks the retired
`integration/managed-git-updater-p3-p5` source. Keep it as a recovery checkout and use the fresh,
explicitly pinned `main` clone below for installation.

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" pull --ff-only
```

If the checkout is dirty, has untracked files, local commits, a missing upstream, an unexpected
branch or remote, a divergence, or a failed fast-forward, preserve it and continue from a clean new
directory:

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET mkdir -p "$HOME/.deeppattern"
DE_ROOT="$HOME/.deeppattern/decision-engine-root-fresh-$(env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET date +%Y%m%d%H%M%S)"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git clone \
  --branch main \
  https://github.com/deeppatternai/decision-engine.git "$DE_ROOT"
```

Before executing code from `DE_ROOT`, report the verified expected URL from the safe check above and
the commit. Do not display an unmatched remote URL:

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" rev-parse --short HEAD
```

### 3. Verify AQG, then install the Decision Engine core

```bash
( cd "$DE_ROOT" && ./install.sh de )
```

This command, or the explicit bootstrap command in step 4, is allowed to inherit existing
`DE_ENDPOINT` and `DE_ACTIVATION_SECRET`. The installer captures them, removes them before launching
unrelated child processes, and passes them only to permanent setup.

With `WITH_AQG=1` (default), this first checks whether the existing AQG installation is healthy. A
healthy AQG checkout is reused without a forced pull or reinstall. Otherwise the installer installs
or repairs AQG, installs its declared Python dependencies (including PyYAML), and requires AQG Doctor
to pass before declaring AQG ready. Use `WITH_AQG=0` only when the user explicitly requests Decision
Engine without AQG:

```bash
( cd "$DE_ROOT" && WITH_AQG=0 ./install.sh de )
```

AQG ready is not the end of AQG setup. With the default `WITH_AQG=1`, after AQG install or repair and
the first AQG Doctor pass, the agent must continue with AQG's full standalone configuration before
continuing any Decision Engine core installation, permanent activation, MCP wiring, or Decision
Engine Doctor.

Read and follow AQG's English standalone instructions:

```bash
"${PAGER:-less}" "$AQG_ROOT/AI_SETUP.md"
```

`AQG_ROOT` must point to the AQG checkout for
`https://github.com/deeppatternai/agent-quality-gates.git`. If the AQG remote reported below does
not match that repository, preserve the checkout, stop, and ask for the correct AQG root.

Use AQG's default `installed-supported` mode. First dry-run:

```bash
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT"
```

Show the user the dry-run summary, including `AQG_ROOT`, remote, commit, detected clients, selected
clients, skipped clients, support-status, the skills, rules, hooks, and MCP configuration AQG would
write, and whether `PROJECT_ROOT` is required. If the dry-run reports skipped project-scope clients,
such as `qoder-cn requires --project-root`, treat that as expected when `PROJECT_ROOT` is absent: the
AQG wrapper is skipping those project-scope adapters and continuing with the other selected clients.
That AQG project-scope profile is separate from DE's macOS `qoder-cn` Desktop MCP adapter in the
registered-host table; do not use one profile's path or detection result as evidence for the other.
Report the skipped clients and reasons, then continue to `--apply` after the user explicitly
confirms. Only stop and ask for an existing absolute `PROJECT_ROOT` path when the user explicitly
wants those skipped project-scope clients installed, or when a later `--apply`, `--verify`,
`--uninstall`, or `--is-installed` invocation still requires `PROJECT_ROOT`. Do not guess from the
summary alone.

After the user explicitly confirms the AQG standalone changes, apply them:

```bash
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT" --apply
```

If the AQG standalone flow synchronizes Codex or Claude Code rules surfaces, follow the AQG document
exactly: backup first, update only the AQG-managed section, and do not edit content outside that AQG
section. Before installing AQG lifecycle hooks, tell the user that this enables AQG hooks and may
include blocking gates, then wait for confirmation.

After AQG standalone configuration finishes, run AQG Doctor again:

```bash
python3 "$AQG_ROOT/scripts/aqg_doctor.py"
```

If AQG Doctor reports any `FAIL`, fix AQG first and rerun Doctor. Do not claim Decision Engine or
AQG installation is complete, and do not continue the Decision Engine core, activation, MCP wiring,
Decision Engine Doctor stages until the full AQG standalone
configuration is complete and AQG Doctor passes.

AQG standalone configuration may write AQG's own skills, rules, hooks, and MCP entries. Decision
Engine MCP wiring remains separate and is written only by Decision Engine. Its credential-free
transport may be registered before activation; activation controls hosted capability access.

After this AQG gate passes, the Decision Engine install flow independently clones a
signature-verified `stable` release into `~/.deeppattern/decision-engine` (unrelated to `DE_ROOT`),
installs the core, skills, updater, and credential-free MCP transport, and attempts permanent
activation only when both owner values are available.

The installer:

- clones and signature-verifies the current `stable` release;
- installs Decision Engine at `~/.deeppattern/decision-engine`;
- installs the Decision Engine skills;
- reuses a healthy AQG installation, or installs its dependencies and verifies it;
- requires the agent to complete AQG standalone configuration and rerun AQG Doctor before any
  Decision Engine core, activation, MCP wiring, or Decision Engine Doctor stage
  continues;
- merges the credential-free `decision-engine` MCP entry into every selected registered client in
  the table above, backing up first and preserving every other MCP server;
- retires the legacy Decision Engine block from Codex's global `AGENTS.md` when present;
- enables automatic update checks on later agent starts;
- prepares tkinter and pywebview using the same Python recorded for MCP;
- runs permanent activation automatically when both owner values are available in the process;
- treats missing owner values as a successful activation deferral, preserves the core installation
  and DE Lite MCP transport, and leaves hosted capabilities disabled;
- runs Doctor.

`WITH_AQG=1` is the default and installs, repairs, verifies, and then leads into full standalone
configuration of the AQG engineering toolkit. Use `WITH_AQG=0 ./install.sh de` only when the user
explicitly wants Decision Engine without AQG.

If automatic client detection is incomplete, continue with step 4. Any Git authentication,
signature, fixed-path, AQG install, AQG standalone configuration, AQG Doctor, or core installer
failure stops this flow. MCP wiring errors must be reported, whether the transport is registered
during installation or repaired after activation. Do not report success merely because source
files were cloned.

### 4. Name clients only when automatic detection failed

```bash
( cd "$DE_ROOT" && "$DE_PYTHON" -m installer.bootstrap_managed_install install \
    --client codex )
```

Replace `codex` with any registered ID from the table above: `claude-code`, `claude-desktop`,
`codebuddy`, `cursor`, `qoder`, `qoder-cn`, `trae`, `trae-work`, `trae-cn`, `trae-work-cn`, `workbuddy`, or `workbuddy-ai`. For multiple clients, repeat the
flag in the same command, for example `--client codex --client qoder`. `DE_PYTHON` must already
name the exact interpreter verified in step 0. This uses the intended clients for this bootstrap
run. The outer installer may already have registered a credential-free MCP transport; this bootstrap
command does not receive an endpoint or activation key. Reuse the same flags in step 5 if activation
is deferred. Do not put the endpoint or activation key in the command line.

### 5. Let the agent open permanent setup when activation is incomplete

Skip this step if installation already confirmed permanent activation. Otherwise require an
explicit answer and ask:

> Do you already have the endpoint and device activation key from the Decision Engine owner? Reply
> only "Yes" or "No". Do not send the real values in chat.

Do not infer the answer from environment variables, configuration, activation status, or any other
machine state. If the response is not an explicit "Yes" or "No", ask again and do not continue this
step.
Use the structured `Yes`/`No` choice when the host provides it; otherwise require the literal
English answer as described above.

Before opening setup, read the installation output and Doctor. If either reports
`owner-guided recovery required`, `persistence is uncertain`, or `do not retry automatically`, do
not retry activation. Preserve `.runtime/activation-recovery-required.json` and report that the
Decision Engine owner or support must handle it. Only an ordinary missing-value skip or rejected
value may continue below.

If both values are already in the agent's process environment, run without exposing them in argv:

```bash
( cd "$HOME/.deeppattern/decision-engine" && "$DE_PYTHON" -m installer.permanent_setup --from-env )
```

Otherwise, when the user answers "Yes", explain that a masked desktop window will open. When the
current host is WorkBuddy, use its Bash tool to run the WorkBuddy command below with
`dangerouslyDisableSandbox: true`, regardless of whether its sandbox is currently enabled. Explain
that this requires user approval, and wait for that approval before it runs outside the sandbox. The
command-scoped marker identifies the host even if an intermediate launcher detaches. If the user
declines or WorkBuddy cannot provide approved outside-sandbox execution, show the command and ask
the user to run it in a normal desktop terminal.

On Windows, WorkBuddy's launch path cannot reliably render the WebView form (it can paint a blank
frame that takes no input even after outside-sandbox execution is approved). The marker therefore
always opens the existing, fully functional masked Tk form. This is the supported WorkBuddy
activation UI and needs no rerun. On other platforms the marker does not change backend selection;
the outside-sandbox execution remains necessary for the existing desktop-session guard.

On macOS, run the shell command directly in WorkBuddy's Bash tool (still with
`dangerouslyDisableSandbox: true` after user approval, as described above) or a normal desktop
terminal. Do not generate or open a `.command` wrapper through Finder. If that wrapper is launched
from a sandboxed WorkBuddy action, the Sandbox can block the handoff to Terminal before Python
starts; any resulting permission dialog is not the activation UI. Once the command starts
successfully, setup opens the masked pywebview form; it uses Tk only as a fallback when pywebview is
unavailable before native registration.

WorkBuddy on macOS, Linux, WSL, or native Windows Git Bash:

```bash
( cd "$HOME/.deeppattern/decision-engine" && \
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET \
  DE_WORKBUDDY_SETUP=1 \
  "$DE_PYTHON" -m installer.permanent_setup )
```

For every other host, proactively run the platform command as before. Do not ask the user to export
values or type them in chat.

macOS / Linux / WSL, or native Windows Git Bash:

```bash
( cd "$HOME/.deeppattern/decision-engine" && \
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "$DE_PYTHON" -m installer.permanent_setup )
```

For hosts other than WorkBuddy, native Windows PowerShell may be used only for a native Git Bash
installation, never for a WSL installation. The agent must replace `$PythonPath` below with the exact absolute interpreter path
printed in step 0; do not guess or silently select another Python:

```powershell
$Root = "$env:USERPROFILE\.deeppattern\decision-engine"
$PythonPath = "<absolute Python path printed in step 0>"
$env:DE_ENDPOINT = $null
$env:DE_ACTIVATION_SECRET = $null
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "Decision Engine installation directory is missing"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "The Python interpreter used for installation is missing"
}
Set-Location -LiteralPath $Root -ErrorAction Stop
& $PythonPath -m installer.permanent_setup
if ($LASTEXITCODE -ne 0) {
    throw "Decision Engine permanent setup failed"
}
```

Set `DE_PYTHON` to the exact interpreter path verified in step 0. The user types the endpoint and
masked secret only in that window. The program activates directly
from memory, atomically saves the endpoint and server-issued device/access/refresh credentials only
after activation succeeds, never stores the original activation secret, repairs MCP configuration
with backups, and runs Doctor. Success is permanent across terminal, agent, and machine restarts.

When step 4 required explicit client selection, append the same repeatable `--client` flags to the
permanent setup command (for example, `--client codex --client qoder`). This carries the confirmed
selection through activation; every registered host follows the same post-activation wiring rule.
When no flags are supplied, permanent setup wires all detected supported clients.

An already activated device is not prompted again and does not consume another device slot. A
cancelled window writes no credential and must be reported as cancelled. If activation succeeds but
MCP or Doctor then fails, preserve the issued device credentials, fix the named stage, and rerun
permanent setup; it must not request owner values or consume another slot again.

If no GUI can open, stop and report that the masked setup window is unavailable. Never fall back to
chat, `.bashrc`, `setx`, or a normal file.

The values cannot be generated from GitHub, Codex, Claude, or OpenAI accounts. The user obtains them
from the person who invited them, their organization's Decision Engine administrator, or the service
owner. When they obtain both values later, they only need to answer "Yes" when the agent asks the
question above; the agent returns to this step and opens the window. Reinstallation is unnecessary.

### 6. Verify the installed copy

Run Doctor locally after the core installation. Before activation, a registered MCP entry may expose
only the DE Lite path; do not treat that as hosted-service readiness. After activation, GUI apps
inherit PATH only when their process starts; then tell the user to **fully quit and reopen** each
configured host from the registered-host table and confirm:

- Ask the restarted host to call `list_auditors` and confirm it returns a list.
- If Claude Desktop was configured, its platform path from the table must contain the server.
- For every JSON/TOML check below, parse only whether the `decision-engine` server key exists; never
  print the complete host config, its environment block, or any value from it.
- If Cursor was configured, `~/.cursor/mcp.json` must contain the `decision-engine` server key.
- If TRAE Work was configured, its Windows or macOS path from the table must contain the server.
- If TRAE Work CN was configured, its Windows or macOS path from the table must contain the server.
- If WorkBuddy was configured, `~/.workbuddy/mcp.json` must contain the server.
- If Qoder was configured, use the platform path in the table; Qoder CN must use
  `~/.qoder-cn/settings.json`. Do not inspect either Qoder IDE profile.

```bash
( cd "$HOME/.deeppattern/decision-engine" && \
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "$DE_PYTHON" -m installer.doctor )
```

Run Doctor from the installed copy, not `DE_ROOT`. Check every line:

- `python` and `skills` should pass; after activation, `mcp` and `python-wiring` should also pass;
- `dev-mode` should report that developer mode is off;
- `setup-gui` reports tkinter readiness;
- `popup` reports pywebview and its native GUI backend; a GUI warning does not invalidate the core
  MCP installation, but must be reported;
- before owner values are provided, `activation: not activated yet` is expected; an MCP entry may
  start the local DE Lite shim, but hosted capabilities must remain unavailable;
- after step 5, `activation` must report that the device is activated even with all `DE_*`
  environment variables absent;
- `libreoffice` is optional;
- before the first full restart, `update` may still show `running=unconfirmed`; the restart in step
  7 must establish the running commit.

If any real check is marked **FAIL** or `✗`, report and follow only its explicit repair guidance.
Do not delete, reset, or overwrite the installation, and do not claim success while a real failure
remains.

### 7. After activation, fully restart and smoke-test MCP

Skip this step while activation is deferred. After successful permanent activation, fully restart
the host so its MCP process reloads the saved credentials and hosted capabilities. Tell the user to
fully quit and reopen every configured host from the registered-host table. Opening only a new task
or tab is not enough. Then ask the
restarted agent:

> Call `list_auditors` and show the result.

- If the device is activated, an auditor list confirms MCP, activation, and the service path.
- If the tool does not exist, MCP configuration was not loaded. Recheck the configured client and
  confirm the host was fully quit before restart.

Run Doctor once more after restart and confirm `update` reports a real `running=<commit>` rather
than `unconfirmed`.

### 8. Report clearly to the user

Before the full restart, say **"Installation steps completed; waiting for restart confirmation"**,
not that every feature is already verified. Report:

- installed version and commit;
- tkinter and pywebview status;
- configured clients;
- whether MCP entries were added, updated, or already present, including backup paths;
- whether permanent activation succeeded or was intentionally deferred;
- whether a full host restart is still required;
- whether a fresh driver checkout was used and where the preserved original remains.

If permanently activated, explain that the device credentials are saved and no longer depend on
Git Bash or `DE_*` environment variables. After the restart, `list_auditors` must return the
auditor list to confirm the complete path.

If owner values were not provided, report the local DE Lite transport separately from activation and
do not claim hosted capabilities are configured. Do not request a host restart solely for hosted
Decision Engine features. End with this short wording:

```text
AQG standalone configuration is complete and AQG Doctor passes. Decision Engine is not activated.
Obtain the endpoint and device activation key from the Decision Engine owner. Do not send the real
values in chat; when you have them, tell me only: "Continue installing DE."
```

Do not call deferred activation an installation failure, and do not call a real failure successful.

---

## Boundaries for the AI agent

- Perform only the installation, activation, MCP merge, and verification described here. Do not
  touch production, secrets, branch protection, or unrelated projects.
- Never manually print, record, commit, or type on the user's behalf any GitHub token, password, SSH
  private key, API key, device token, owner value, or complete configuration file. The sole
  persistence exception is `installer.permanent_setup`: after successful activation it may store
  the endpoint and server-issued device credentials; it never stores the activation secret.
- Use installer-provided backup-first MCP merge and repair commands. Never overwrite agent
  configuration manually, and preserve every unrelated MCP server.
- A bad driver checkout moves the flow to a fresh directory; an existing or abnormal fixed install
  path stops the flow. Do not confuse these two paths.
- Never delete, reset, force-checkout, stash, clean, move, or overwrite existing user files.
- Authentication and masked credential entry belong to the user. The agent opens the approved
  prompt, continues verification, and reports non-sensitive errors accurately.

---

## Distribution note

This guide ships with the complete Decision Engine client bundle and must not be separated from
`install.sh`, `installer/`, and the release-signing trust material. Install-driver clones use the
repository's authoritative `main` branch; signed runtime code still comes from `stable` metadata and
an immutable release tag. No server endpoint or secret is hardcoded here.
