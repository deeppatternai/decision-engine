# Deep Pattern Audit for DeepSeek Harness

This is the runtime-free Harness adapter for the hosted Deep Pattern Audit MCP
server. It uses Harness's Node runtime, credentials service, and tool registry;
it does not bundle Python, Node executables, a local audit server, or an
activation secret.

## Compatibility

- DeepSeek Harness `0.2.0-rc.2`, `0.2.1-rc.5`, or `0.2.2-rc.1`
- Node.js `>=20` supplied by Harness
- macOS, Linux, and Windows share the same package
- Interactive desktop sessions with a system browser and loopback callback

Remote/headless sessions are not an interactive OAuth target in this first
slice. The hosted progress page works in any browser after the task is created,
but authorization must be completed on the machine that owns the Harness
profile and loopback callback.

## Install a local candidate

Build first:

```sh
pnpm install --ignore-scripts
pnpm run build
pnpm run pack:local
```

Install the resulting `.tgz` into an isolated profile with the Harness CLI:

```sh
dsh plugin --profile <profile> add /absolute/path/to/de-audit-harness-0.2.13-remote.11.tgz
```

After installing or updating the package, fully quit every Harness window and
reopen Harness before starting the acceptance conversation. Harness can
hot-load the MCP server while leaving the already-running renderer unchanged;
reload alone does not reliably activate a newly installed Client Module, so
native audit cards will otherwise be absent even though the audit and hosted
progress page work.

The package patch loads this adapter and its bundled `audit` and `setup` skills.
No existing Codex, Claude, Cursor, Marketplace, or traditional installer entry
is replaced.

The production authorization server must preconfigure public client
`deep-pattern-audit-harness` with registered redirect
`http://127.0.0.1/deeppattern/oauth/callback`. Actual callbacks use a temporary
loopback port. DCR remains disabled.

## User flow

1. Ask Harness to connect Deep Pattern Audit or request an audit.
2. The local control tool opens the browser only when no reusable authorization
   exists.
3. Complete owner-code activation in the browser.
4. The same Harness profile stores and refreshes the OAuth grant, registers the
   four allowlisted audit tools, and continues the request.
5. The hosted WebUI is the persistent progress surface. Harness waits with
   bounded status calls and returns the terminal result in the conversation.
   On Harness versions that expose `ToolRunContext.deferContext`, the plugin
   injects a task-bound continuation after submit and each status response so
   this sequence does not depend only on model interpretation of the skill.
6. Harness's web client renders replay-safe native cards for submit, status,
   result, and events calls. Each invocation owns its own card and persists only
   a bounded presentation projection: task state, server-authorized reviewer
   labels, elapsed time, the trusted progress URL, and a terminal summary. The
   hosted WebUI remains the continuous live view.

## Card display in Harness

Audit cards respect the user's Harness **Work details** preference. The plugin
registers keyed atomic tool views with the official Client Module API and does
not force a display mode or manipulate Harness's private conversation DOM.

- **Standard / Compact** groups tool calls. Expand **Tools called** (or its
  localized equivalent) to see the audit cards.
- **Detailed** shows cards directly while the current turn is running; Harness
  may fold the completed historical turn back into a tool group.
- **Verbose** keeps tool cards directly visible.

Therefore, seeing the live progress link and a collapsed tool group in Standard
mode does not mean the Client Module failed to load. Use Detailed for direct
live-card acceptance, or expand the tool group in Standard. A full Harness
restart is still required after installing or updating the Client Module.

`mcp__audit__connect` supports `connect`, `status`, and explicit `logout`.
Remote tools are registered only after successful OAuth and are removed on
logout or plugin disposal.

## Credential boundary

Tokens, PKCE verifier, and OAuth state are stored as a versioned opaque grant by
`ctx.credentials`. They are never written to this package, Cordis YAML, model
context, or logs. Harness's default credentials backend is private to the local
user, but it is not documented here as OS Keychain-backed and should not be
treated as isolated from every process running as that same user.
