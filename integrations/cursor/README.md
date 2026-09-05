# Cursor integration

Cursor uses the same managed Decision Engine installation as Claude Code,
Claude Desktop, and Codex. Its user-global MCP entry points to
`installer.launcher`, never directly to `installer.shim`. Skills are routed from
the fixed managed root with POSIX symlinks or Windows directory junctions, so a
normal managed update does not copy a Cursor-specific payload or rewrite the MCP
entry.

## Install and repair

Normal setup detects Cursor and configures it together with the other installed
hosts. To repair only Cursor from the managed checkout, run:

```powershell
powershell -NoProfile -Command 'Remove-Item Env:DE_ENDPOINT,Env:DE_ACTIVATION_SECRET -ErrorAction SilentlyContinue; Set-Location (Join-Path $HOME ".deeppattern\decision-engine"); python -m installer.install --repair-client cursor'
```

```bash
( cd "$HOME/.deeppattern/decision-engine" && \
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -m installer.install --repair-client cursor )
```

The repair path uses the shared MCP writer and managed-skill-link adapter. A
Cursor conflict is reported for Cursor only; user-owned files are preserved and
other hosts remain installed. A verified legacy Cursor owned-copy installation
is retired transactionally before shared-link routing. An ambiguous or modified
legacy copy is preserved and reported instead of being deleted.

## Diagnose

Run Doctor from the fixed managed checkout:

```powershell
powershell -NoProfile -Command 'Remove-Item Env:DE_ENDPOINT,Env:DE_ACTIVATION_SECRET -ErrorAction SilentlyContinue; Set-Location (Join-Path $HOME ".deeppattern\decision-engine"); python -m installer.doctor'
```

```bash
( cd "$HOME/.deeppattern/decision-engine" && \
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -m installer.doctor )
```

Doctor checks the Cursor MCP entry, shared skill route, project-level shadowing,
supported Cursor version, and the same optional local-display capabilities used
by the other hosts.

## Local user experience

On Windows and macOS, Cursor supports:

- Graphic Explanation and Discussion Board local windows;
- text follow-up chat in the popup through the logged-in local Cursor Agent;
- the shared audit Stop panel.

The follow-up transport resolves the official `cursor-agent` executable (and the
official Windows desktop Agent location), runs Ask mode in an isolated empty
workspace with a reduced environment, bounds stdout/stderr, and never persists
the popup context or response. On Windows, Ask mode is read-only but is not an
OS-level zero-read sandbox: the Agent can read other files available to the
current user, and previously user-configured local MCP/tools may remain callable.
The transport passes `--trust` only for the empty-workspace prompt, never passes
`--approve-mcps`, and uses no automatic-write/force flag. To disable only Cursor
follow-up for future GUI launches, run `setx GE_CURSOR_FOLLOWUP 0` on Windows or
`launchctl setenv GE_CURSOR_FOLLOWUP 0` on macOS, then fully restart Cursor. The
macOS command applies to the current login session and must be rerun after signing in again.
To re-enable follow-up, run
`[Environment]::SetEnvironmentVariable('GE_CURSOR_FOLLOWUP',$null,'User')`
on Windows or `launchctl unsetenv GE_CURSOR_FOLLOWUP` on macOS, then restart Cursor.
GE/DB display and Stop remain available.

After install, repair, or update, fully quit and reopen Cursor so it reloads MCP,
PATH, and the managed skill routes.
