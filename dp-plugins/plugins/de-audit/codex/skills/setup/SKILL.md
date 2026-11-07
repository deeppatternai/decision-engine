---
name: setup
description: Connect Deep Pattern Audit through the host OAuth flow with an owner-issued activation code.
---

# Set up Deep Pattern Audit

Codex installations declare `ON_INSTALL` authentication and the MCP manifest
declares a stable OAuth resource with CIMD client mode. The host decides whether
installation reuses a stored credential or starts OAuth again; the plugin cannot
inspect that state or conditionally change the install policy. The plugin never reads host credential files,
copies tokens, or performs a login call merely to test authentication state.
When setup is requested later, ask the host to authenticate or reconnect the
`audit` MCP server; do not make a connector-panel button the only setup path.

Claude Code uses transport-level OAuth discovery for this plugin and does not
provide the Codex `ON_INSTALL` authentication hook. Installation alone may not
open a browser. Do not call an Audit tool merely to manufacture an authentication
error. In Claude Desktop Code, authenticate from the same conversation that owns
the pending audit request: open that conversation's Connectors section or
`/mcp`, select the plugin-provided `plugin:audit:audit` server, and choose
Authenticate. Do not use Connect from global Plugin settings while another Code
conversation is focused as proof that the pending conversation has refreshed.
The outer-Terminal fallback is
`claude mcp login plugin:audit:audit`. A headless `claude -p` or Agent SDK run
cannot complete this browser flow and must be authenticated first from an
interactive session.

After the browser callback, let the same conversation reconnect or wait once.
If that conversation still reports Needs authentication while outer-Terminal
`claude mcp list` reports `plugin:audit:audit` as Connected, the host retained a
pre-authentication MCP snapshot. Start a new Claude Code conversation and issue
the original audit request once. Do not activate again, create another
idempotency key, or submit a replacement task merely to refresh the old
conversation.

Claude Cowork has a separate plugin installation, conversation runtime, and
OAuth store from Claude Code. Install the dedicated `cowork/` plugin entry and
start a new Cowork conversation. Its fixed, non-secret transport-auth header
requests HTTP OAuth discovery for that Cowork runtime. Authenticate or reconnect
from the active Cowork conversation's Connector. Do not use the global Plugins
Connect action, the Code `/mcp` Authenticate action, or
`claude mcp login plugin:audit:audit` as proof that Cowork is connected. After
the browser callback, let that Cowork conversation reconnect and continue the
original request rather than submitting a replacement.

Never run `codex mcp login audit` inside a model-executed Shell: that process
can be sandboxed and unable to bind the host callback or open the browser. For
Codex CLI, tell the user to run `codex mcp login audit` in the outer Terminal,
outside the agent turn. Do not run `claude mcp login plugin:audit:audit` inside a
model-executed Shell either. An already-open chat may retain the previous skill
snapshot; after an update, use a new chat for acceptance. Do not run a
plugin-local executable or ask the user to install Python, Git, Node, or a
native runtime.

The host opens A's browser activation page. The page shows Endpoint as
`https://endpoint.deeppattern.ai` and one masked activation-code field. Enter
the owner-issued activation code and confirm once. A exchanges it inside the
isolated OAuth flow and redirects to the host callback. The host stores access
and refresh tokens; the plugin never receives or persists them itself.

If the host cannot start OAuth from the current context, report that host
authentication did not start and offer its available Authenticate or Reconnect
action when one is actually exposed; do not invent a connector-panel location
or claim the service is unavailable. Do not paste an activation code, access
token, refresh token, or legacy device token into chat, source control, logs,
command history, or an MCP manifest.

Cancellation cancels only the current authorization attempt. A later explicit
setup request may start a new host OAuth attempt. Do not treat cancellation as
a service outage, and do not retry automatically in the same turn. If the host
reports an OAuth error, preserve the error category and stop; never fall back
to `/mcp`, `/mcp/v1/audit`, or a local bridge.
