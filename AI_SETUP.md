# Decision Engine - AI-assisted local setup

English | [简体中文](AI_SETUP.zh-CN.md)

## User prompt

Give this file to an AI Agent on the target device, then say only:

> Install Decision Engine according to the attached `AI_SETUP.md`.

You may attach the official installer for the current operating system in the same message. When no
installer is attached, the Agent uses the official GitHub command in this guide.

## Scope

This guide applies only to a local Agent session running on the same device and as the same desktop
user that will use Decision Engine. Remote, container, cloud, VM, WSL, and cross-device installation
are out of scope.

The installer is authoritative for environment checks, Agent detection and selection, dependencies,
AQG, MCP, Skills, Hooks, activation, Doctor, updates, and recovery diagnostics. The Agent must not
read the script and reconstruct those decisions itself.

## Instructions for the AI Agent

### 1. Select exactly one source

- macOS or Linux: `dp-install.sh`
- native Windows: `dp-install.ps1`

Apply this priority exactly:

1. If the current message explicitly attaches one matching installer and it is accessible, use its
   exact absolute attachment path.
2. If no matching installer is attached or it is inaccessible, use the official GitHub command in
   section 2.
3. If multiple matching installers are attached, stop with `INSTALLER_SOURCE_AMBIGUOUS`.

Only files explicitly listed as attachments to the current message count as uploaded. Do not search
workspaces, projects, home directories, recent files, or the guide's directory for an installer. Do
not read, analyze, or modify the installer in preparation for setup.

### 2. Run the installation command

Run exactly one command matching the current system and selected source.

#### macOS

Before any macOS installation command, complete the permission and terminal-capability decision in
section 3. Except for a known unsupported host explicitly allowed to enter manual fallback there,
do not execute or hand back a command until the user explicitly confirms Full Access.

Run the selected command through the Agent host's normal local command runner. **Do not first use a
host action that runs it in a visible terminal.** `--agent-terminal` opens and manages the one
Terminal.app window itself. The original Agent command waits, receives the complete output and real
exit status, and then closes the independent installer terminal window.

With an uploaded installer:

```bash
bash "<exact absolute path of the dp-install.sh attachment>" --agent-terminal
```

Without an uploaded installer:

```bash
p="$(mktemp /tmp/dp-install.XXXXXX)" && curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh -o "$p" && bash "$p" --agent-terminal
```

`--agent-terminal` only hands the existing no-argument interactive installer to Terminal.app. It
does not make installation decisions for the user. The user completes Agent selection, dependency
approval, and other ordinary prompts there. Activation secrets are entered only in the native
masked window. If the Agent incorrectly invokes this option inside Terminal.app, the installer
returns `BLOCKED` before installation to prevent duplicate windows or an Agent session that ends
before the installation does.

#### Linux desktop

Use the host's action for running a command in a visible local terminal. With an uploaded installer:

```bash
bash "<exact absolute attachment path>/dp-install.sh"
```

Without an uploaded installer:

```bash
curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh | bash
```

#### Native Windows

Before any native Windows installation command, complete the Windows permission decision in
section 3. Do not execute or hand back a command until the user explicitly confirms Full Access.

Run the selected command through the Agent host's normal local command runner. Do not first use a
host action that runs it in a visible terminal. `-AgentTerminal` opens one visible Windows
PowerShell window, runs the unchanged no-argument interactive installer there, waits for its real
exit status, and lets that child window close when the installer exits.

With an uploaded installer:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<exact absolute path of the dp-install.ps1 attachment>" -AgentTerminal
```

Without an uploaded installer:

```powershell
$p="$env:TEMP\dp-install.ps1"; irm "https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.ps1" | Set-Content -LiteralPath $p -Encoding UTF8; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p -AgentTerminal
```

The user completes Agent selection and ordinary prompts in the visible child window. Activation
secrets remain in the native masked window. The Agent command must remain running until it receives
the numeric child exit status; it must not treat successful window creation as successful
installation.

Do not add arguments other than the exact documented entry mode, environment overrides, wrappers,
redirects, or whole-installer `sudo`. Do not recreate the interactive flow in an Agent background
sandbox or guess user input.

### 3. Permissions and interaction

Neither this guide nor the installer can switch the host's approval mode, such as Ask, Auto Approve,
or Full Access, or automatically restore that setting afterward. A one-time macOS permission to
control Terminal grants Automation access only; it is not the host's Full Access mode.

Follow this macOS permission and terminal-handoff state machine exactly. Never return to an earlier
step on your own:

1. If the current host is WorkBuddy or WorkBuddy AI, regardless of version or current permission
   mode, immediately report `VISIBLE_TERMINAL_HANDOFF_UNSUPPORTED` and enter the manual Terminal
   fallback below. Do not request Full Access and do not run `--agent-terminal` or
   `--agent-activate`. This is a verified host-capability limit, not an installer failure.
2. For every other Agent, tell the user before installation or resumed activation to enable Full
   Access temporarily and restore it manually afterward. Until the user confirms, report
   `AGENT_FULL_ACCESS_REQUIRED` and stop without falling back early.
3. After the user says “Full Access enabled”, “已开启完全访问”, or gives equivalent explicit
   confirmation in the current conversation, treat it as valid for the current installation task
   and attempt the original automatic handoff exactly once. Never ask again, ask whether it applies
   to the current session, or require the user to repeat the confirmation.
4. If the command exits `3` or reports `dp-install: BLOCKED`, `did not permit`, Terminal.app did not
   open, startup timed out, the host sandbox denied the handoff, or permission error `-10004`,
   immediately report `VISIBLE_TERMINAL_HANDOFF_BLOCKED` and include the one applicable manual
   command below in the **same response**. Do not request permission confirmation again or ask
   whether the user wants the command.

A one-time macOS permission to control Terminal does not replace Full Access confirmation. Other
Agents must not fall back based only on their name or one unauthorized attempt, but once the user
has confirmed Full Access, never reinterpret a failed handoff as missing confirmation.

Manual Terminal fallback applies only to terminal-handoff failure. Once Terminal.app opens,
dependency, network, activation, Doctor, or installer errors are not handoff failures. Preserve and
report those errors without rerunning manually.

For a first installation with an uploaded installer, ask the user to run this in Terminal.app:

```bash
bash "<exact absolute path of the dp-install.sh attachment>"
```

For a first installation without an uploaded installer, ask the user to run:

```bash
curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh | bash
```

For resumed activation, use the manual `--activate` command in section 4. The same response that
enters fallback must contain exactly one command matching the current source and say: run it in
Terminal.app, then return the numeric exit code and final `dp-install:` line. Do not merely report
failure, request permission confirmation again, or ask whether the user wants the command. Do not
reconstruct installation steps from the script or claim success before receiving the result.

Do not request macOS Full Disk Access, change operating-system security settings, or loop retries.
After automatic completion, failure, or manual fallback, remind the user to restore the previous
permission mode manually if they changed it.

On native Windows, use the Agent host's own Full Access mode, not macOS Full Disk Access. Before
installation or resumed activation, tell the user to enable Full Access temporarily and restore the
previous permission mode manually afterward. Until the user explicitly confirms it in the current
conversation, report `AGENT_FULL_ACCESS_REQUIRED` and stop without executing or falling back. Treat
one explicit confirmation as valid for the current installation task and do not ask again.

After confirmation, attempt the documented `-AgentTerminal` or `-AgentActivate` handoff exactly
once. If it exits `3`, reports `dp-install: BLOCKED`, or no visible PowerShell window opens,
immediately report `VISIBLE_TERMINAL_HANDOFF_BLOCKED` and include exactly one applicable manual
command in the same response. For first installation, use the same uploaded or official-GitHub
command from section 2 with `-AgentTerminal` removed. For resumed activation, use the matching
`-Activate` command below. Do not ask for another confirmation before providing that command. Once
the visible window opens, later installer, network, activation, or Doctor errors are product
results, not terminal-handoff failures, and must not trigger an automatic rerun. After automatic
completion or fallback, remind the user to restore the previous Agent permission mode manually.

The user handles ordinary prompts directly in the visible terminal. Never ask for an activation
key, endpoint, device token, or another secret in chat. Those values go only into the installer's
native masked window.

### 4. Resume activation later

If the installer reports `activation=pending` and the user later asks to continue or retry
activation, do not rerun the complete installation flow from section 2. Run exactly one
activation-only command matching the original platform and source.

macOS with an uploaded installer:

```bash
bash "<exact absolute path of the dp-install.sh attachment>" --agent-activate
```

macOS without an uploaded installer:

```bash
p="$(mktemp /tmp/dp-install.XXXXXX)" && curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh -o "$p" && bash "$p" --agent-activate
```

On macOS, continue to use the host's normal local command runner, not its action for running in a
visible terminal. This entry opens one managed Terminal.app window and starts only the native
masked activation. It does not check for updates, select Agents, install AQG, or rewrite MCP/Skills.

If section 3 selected manual Terminal fallback, then with an uploaded installer ask the user to run
this in Terminal.app:

```bash
bash "<exact absolute path of the dp-install.sh attachment>" --activate
```

Without an uploaded installer, ask the user to run:

```bash
p="$(mktemp /tmp/dp-install.XXXXXX)" && curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh -o "$p" && bash "$p" --activate
```

`--activate` is only for a user who is already in a visible terminal. Never hand
`--agent-activate` back for manual execution.

Linux desktop with an uploaded installer:

```bash
bash "<exact absolute path of the dp-install.sh attachment>" --agent-activate
```

Linux desktop without an uploaded installer:

```bash
p="$(mktemp /tmp/dp-install.XXXXXX)" && curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh -o "$p" && bash "$p" --agent-activate
```

On Linux, run it through the host's visible local terminal. The activation-only entry accepts only
a complete, clean, verifiable managed installation. If the installation is missing, damaged, or
has a recovery marker, stop and report the result without falling back to a full installation or
inventing a repair.

Native Windows with an uploaded installer:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<exact absolute path of the dp-install.ps1 attachment>" -AgentActivate
```

Native Windows without an uploaded installer:

```powershell
$p="$env:TEMP\dp-install.ps1"; irm "https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.ps1" | Set-Content -LiteralPath $p -Encoding UTF8; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p -AgentActivate
```

Run these through the Agent host's normal local command runner. They open one visible PowerShell
window and run activation only; they do not check for updates, select Agents, install AQG, or
rewrite MCP/Skills. If the automatic Windows handoff is blocked, remove `-Agent` and give the user
the matching `-Activate` command to run in an already-visible PowerShell terminal. Never hand
`-AgentActivate` back for manual execution.

Do not invoke `python3 -m installer.permanent_setup` directly or invent a Python path, working
directory, or `PYTHONPATH`. That internal module command depends on the managed source directory,
can fail with `ModuleNotFoundError`, and bypasses the top-level installer's recovery checks.

Report that the activation window opened only after the native masked window is actually visible.
If the command exits nonzero, prints `ModuleNotFoundError`, or no window appears, report the failure
accurately instead of claiming success from intent.

### 5. Report the result

After the installer exits, report:

- platform and source (`uploaded` or `official-github`);
- exact command executed;
- numeric exit code;
- final `dp-install:` line;
- every `ERROR`, `BLOCKED`, or `PARTIAL` result;
- exact restart, trust, authorization, or other user action requested by the installer.

Exit code `0` means success. Exit code `4` is partial completion, not full success. Any other nonzero
exit code means failure. When an Agent restart is required, fully quit its tray or background process
before reopening it; starting a new chat is not a restart.

Preserve complete output on failure. Do not delete or rename `.deeppattern`, reset a checkout,
manually edit MCP/Skills configuration, install an alternative Python, or invent recovery steps.
