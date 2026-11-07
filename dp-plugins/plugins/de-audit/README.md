# Deep Pattern Audit remote candidate

`audit@deeppattern` is a portable, runtime-free plugin for the isolated Deep
Pattern Audit MCP resource:

```text
https://endpoint.deeppattern.ai/plugin/mcp/v1/audit
```

The plugin manifests contain only the remote URL and skills. They contain no
Python runtime, local launcher, activation code, OAuth token, legacy device
token, client secret, or host API key. Codex, Claude, Cursor, and compatible
Agent Plugin hosts use their standard Streamable HTTP MCP and OAuth support.

Codex's host-specific manifest declares the remote resource plus public OAuth metadata using
CIMD client mode. Codex still discovers A's protected-resource and
authorization-server metadata. The marketplace uses `ON_INSTALL`, so Codex owns
whether installation reuses a host credential or starts OAuth; the plugin has no
API for inspecting that credential state or dynamically changing the install
policy. The plugin never reads the host store or contains credentials. The deterministic manual
entry points remain the connector Authenticate action and `codex mcp login
audit`. After updating the plugin, acceptance starts in a new chat because an
already-open chat may retain its previous skill snapshot. The browser page
shows the fixed Endpoint and one masked activation-code field. After the user
confirms the owner-issued code, A redirects to the host callback and the host
stores OAuth credentials.

Claude Code's plugin-specific `.mcp.json` adds a fixed, non-secret transport
authentication mode header. A responds to an unauthenticated connection with
HTTP 401 and `WWW-Authenticate`, so Claude Code can mark `audit` for sign-in
in `/mcp`. Portable Agent Plugin hosts, Cursor, and Hermes retain their URL-only
manifests. The header selects challenge delivery only;
authenticated requests use the same OAuth principal, scopes, ownership, and
billing checks.

DeepSeek Harness uses the independent native package under
`deepseek-harness/`. The verified Harness `0.2.0-rc.2`, `0.2.1-rc.5`, and `0.2.2-rc.1`
releases do not provide a complete host OAuth flow, so this small Cordis adapter
drives MCP SDK PKCE against the same
remote resource and stores its versioned grant through Harness credentials. It
registers only the four `mcp__audit__audit_skill_*` tools after authorization,
plus the local `mcp__audit__connect` control tool. It bundles no executable
runtime. The A deployment must preconfigure public client
`deep-pattern-audit-harness` and loopback redirect
`http://127.0.0.1/deeppattern/oauth/callback`; DCR remains disabled. This does
not change Codex, Claude, Cursor, Hermes, or legacy installation manifests.

Claude Cowork currently has a separate plugin directory, conversation runtime,
and OAuth store from Claude Code. It must be installed separately from the
`cowork/` subdirectory. That entry uses the same fixed, non-secret transport
authentication declaration as Claude Code so the Cowork runtime owns its OAuth
transaction. Authenticate or reconnect from the active Cowork conversation;
the global Plugins Connect action can be routed to a focused Code conversation
instead. Authenticating the Code connector does not authenticate Cowork. This host-specific entry is
still runtime-free and uses the same remote resource, scopes, ownership, and
billing enforcement.

Audit requests call `audit_skill_submit` immediately. An unauthenticated first
call returns the standard tool-level OAuth challenge on hosts using anonymous
discovery, so the host can open
activation and then continue the original tool call with the same idempotency
key. It must not create a second audit. Requests that omit a tier use `standard`.
Explicit `fast`, `standard`, and `deep` requests remain unchanged.

Tool and App metadata are discoverable before authentication. Successful submit
and status calls expose an MCP Apps audit card plus a
short-lived audit progress ResourceLink. Compatible hosts render the responsive
card directly in the conversation; other hosts use the HTTPS browser fallback.
After a non-terminal submit on MCP Apps hosts, the model ends its turn without
calling status, result, or events; the embedded App owns polling so the running
card remains mounted. Claude Desktop Code receives the exact browser
ResourceLink immediately, then uses the existing status tool's bounded wait
option until the task is terminal and reads the final result into the same
conversation. Claude Cowork uses the same bounded wait and terminal-result
flow. DeepSeek Harness also uses bounded status waits and the exact ResourceLink,
then reads the terminal result in the original conversation; this first slice
does not claim an inline MCP Apps card. Other non-MCP-Apps hosts retain the
exact ResourceLink fallback.
Both presentations are server-hosted and platform-independent across macOS,
Linux, and Windows. They use no local runtime, and closing either presentation
does not cancel the audit. A browser stop action is available only while the
owned task remains queued and unclaimed.

The remote resource exposes only:

- `audit_skill_submit`
- `audit_skill_status`
- `audit_skill_result`
- `audit_skill_events`

Authentication, entitlement, subject binding, idempotency, usage, billing,
settlement, refund, refresh rotation, and revocation remain server-enforced.
There is no fallback to the original `/mcp` or `/mcp/v1/audit` device-token
routes. The original DE installation and those routes remain unchanged.

Run the deterministic candidate checks with:

```text
python3 -m unittest discover -s dp-plugins/plugins/de-audit/tests -v
python3 dp-plugins/plugins/de-audit/packaging/build_package.py --output-dir /tmp/de-audit-remote
cd dp-plugins/plugins/de-audit/deepseek-harness && pnpm install --ignore-scripts && pnpm test
python3 dp-plugins/plugins/de-audit/packaging/build_harness_package.py --output-dir /tmp/de-audit-harness
```

The generated zip is platform-independent. A clean package check rejects any
`runtime/` directory, executable, Python bridge, or credential-bearing manifest.
Human acceptance still requires a deployed OAuth-enabled A endpoint and a real
host browser callback. Progress acceptance additionally requires the server's
default-off audit Web UI feature and isolated progress schema to be enabled.
