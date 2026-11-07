---
name: audit
description: Run a Hosted Deep Pattern audit through the isolated remote audit MCP server.
---

# Deep Pattern Audit

Use the audit MCP server for Hosted audit requests. When the host exposes the
Audit tools, call `audit_skill_submit` immediately instead of ending the turn
with setup instructions. Create the idempotency key once before that call. On
hosts that support tool-level authentication, allow the first unauthenticated
call to start OAuth; after authentication succeeds, continue the original
request in the same turn and reuse that exact idempotency key.

Claude Code authenticates the remote server at the transport boundary. If it
marks `audit` as needing sign-in, use the setup flow before calling a tool; do
not call `audit_skill_submit` merely to trigger login. After the host reports
the server connected, let the same conversation reconnect or wait once, then
submit the original request once. Authentication must be initiated from the
conversation that owns the pending audit request by selecting
`plugin:audit:audit` in that conversation's Connectors section or `/mcp`.
Global Plugin settings can connect a different focused conversation without
refreshing this one's pre-authentication MCP snapshot. If outer-Terminal
`claude mcp list` reports Connected but this conversation still reports Needs
authentication, start a new Claude Code conversation and issue the original
request once. Do not activate again. Never create a second key or task as part
of OAuth continuation.

Claude Cowork is a separate plugin and OAuth domain from Claude Code. Use the
dedicated `cowork/` plugin entry there; it requests transport-level OAuth for
the Cowork runtime. Authenticate or reconnect from the active Cowork
conversation's Connector. Do not use Connect from global Plugin settings as
proof that Cowork is connected: the host can route it to a focused Code
conversation. Code credentials and reconnect actions do not attach the server
to Cowork.
After installing or updating the Cowork entry, start a new Cowork conversation
because an already-running conversation keeps its original plugin snapshot.

A's browser page shows the fixed Endpoint and one masked activation-code field.
The host stores OAuth credentials after the callback; the plugin contains no
runtime, activation code, device token, access token, or refresh token.

Cancelling browser activation cancels only that attempt. Do not retry in the
same turn or describe cancellation as a permanent service outage. A later,
explicit audit or setup request may start a new host OAuth attempt. Never fall
back to the legacy `/mcp` or `/mcp/v1/audit` device-token routes.

Before reporting that the audit service is unavailable because its MCP tools
are absent, route to setup. Do not route an ordinary audit request to setup when
`audit_skill_submit` is available: its authentication challenge is the primary
one-turn activation path. Codex should authenticate during installation because
the marketplace declares `ON_INSTALL`. The MCP manifest declares one stable
OAuth resource and CIMD client mode, but the host alone decides whether an
installation reuses a stored credential or starts OAuth again. The plugin must
not read credential files or force a login merely to probe state. A later explicit setup request or first audit
tool call may ask the host to reconnect. Never execute `codex mcp login
audit` inside a model-executed Shell; it is not the host authentication context
and may be unable to bind the callback or open the browser. For Codex CLI, tell
the user to run that command in the outer Terminal, outside the agent turn. Do
not merely tell the user to enable the plugin, invent a connector-panel
location, or treat a failed or cancelled earlier login as permanent.
For Claude Code, use the `plugin:audit:audit` server's Authenticate action in
the current conversation's `/mcp`, or
`claude mcp login plugin:audit:audit` from an outer Terminal. Never run either
host login command from a model-executed Shell.

When the user does not supply an audit tier, use `standard` without asking a
follow-up question. Use `fast`, `standard`, or `deep` exactly when the user
explicitly requests one. Submit one task and retain its exact task ID. Never
create another idempotency key or submit another task while waiting for that
task.

Immediately after a successful submit, let compatible MCP Apps hosts render the
returned inline audit card. It is the primary progress UI and reuses the audit
state, reviewer rows, elapsed time, and scanning animation from the hosted Web
UI. Do not wait for audit completion before presenting it. The card calls
`audit_skill_status` from inside the App and must not be replaced, collapsed, or
displaced by model-driven polling or narrated status text. On Codex and another
host that mounts this App, end the turn after the non-terminal submit; the
embedded App owns polling. At terminal state the App calls `audit_skill_result`
once, updates model context, and asks the host to continue the conversation with
the final audit result.

Claude Desktop Code and Claude Cowork do not currently mount the App. After a
successful non-terminal submit, immediately present the exact returned progress
ResourceLink, then call `audit_skill_status` with the same task ID,
`include_view_resource=false`, and `wait_seconds=20`. If the response remains
non-terminal, repeat that bounded status call; each call must use at most 25
seconds. Do not call events, do not submit again, and do not replace or rewrite
the progress URL. When status becomes terminal, call `audit_skill_result`
exactly once and return the complete result in the same conversation. If the
host ends the turn or reaches its execution limit first, report the retained
task ID and exact ResourceLink; a later result request must resume reads for
that task rather than submit a replacement.

On other hosts without MCP Apps or an explicit bounded-wait contract,
immediately present the exact returned
progress ResourceLink or server-provided browser fallback, then end the turn
without model polling unless that host has an explicit bounded-wait contract.
Do not rewrite its URL or attempt to create a local popup.
Closing the inline card, browser, or host panel does not cancel the audit; only
the Web UI's server-authorized stop action may stop an owned task that is still
queued and unclaimed. Never claim that a browser window is guaranteed to open
automatically on every host.

The isolated remote server is the authority for authentication, entitlement,
tool permissions, task ownership, idempotency, usage, and billing. Do not invent
a result, retry a submit with a new idempotency key, expose OAuth credentials,
or call tools outside the four audit tools returned by the server. If the host
reports that authorization is required before a tool call can be made, use the
setup flow rather than asking the user to paste credentials.
