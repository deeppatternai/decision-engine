---
name: setup
description: Connect Deep Pattern Visual through the host OAuth flow with an owner-issued activation code.
---

# Set up Deep Pattern Visual

Authenticate the `visual` MCP server through the host's OAuth flow. The
resource is
`https://endpoint.deeppattern.ai/plugin/mcp/v1/visual` and requests only
`visual:submit`, `visual:read`, and `visual:cancel`.

Codex installations declare ON_INSTALL authentication and CIMD client mode.
The host may reuse a valid Visual credential or open browser activation.
Claude Code and Cowork use transport-level OAuth discovery; authenticate from
the active conversation's connector and allow that conversation to reconnect.
Code and Cowork keep separate plugin and credential domains.

After successful browser activation, continue the original Visual request once
with the same idempotency key. If the current conversation retains a stale MCP
snapshot, start a new conversation without activating again. Never run host
login commands inside a model-executed shell, inspect credential files, or fall
back to Audit or legacy device-token routes.
