---
name: visual
description: Create hosted Deep Pattern diagrams, infographics, and comics through the isolated remote Visual MCP server.
---

# Deep Pattern Visual

Use the Visual MCP server for requests to create a diagram, infographic, or
comic. Choose exactly one mode:

- `diagram` for systems, flows, comparisons, maps, timelines, and structured explanations.
- `infographic` for a polished single-page visual summary.
- `comic` for a sequence of illustrated panels that explains a topic.

Call `visual_submit` once with a stable idempotency key and retain the returned
task ID. Never create a second key or submit a replacement while the original
task is queued or running. The server owns entitlement, pricing, generation,
storage, and task ownership. Never request or expose artifact bytes in model
text.

After submit, present the exact returned viewer ResourceLink immediately. On a
host that mounts the MCP App, let the card own live polling. On Claude Desktop
Code, Cowork, or another host that does not mount the App, call
`visual_status` with the same task ID, `include_view_resource=false`, and
`wait_seconds=20`. Repeat only while the same task is non-terminal. When it is
terminal, call `visual_result` once. Return the final status and preserve the
viewer link so the user can zoom, fit, download, copy, and continue in the
protected browser UI.

The only public tools are `visual_submit`, `visual_status`,
`visual_result`, `visual_cancel`, and `visual_open_view`. Do not call
Audit tools, legacy `/mcp` routes, or local runtime commands. Closing a card,
browser tab, or host panel does not cancel generation. Cancel only when the
user explicitly asks and reuse the retained task ID.

If authentication is required, use the setup flow and then continue the
original request with the same idempotency key. The host stores OAuth
credentials. Never ask the user to paste an activation code, access token,
refresh token, device token, or client secret into chat or a manifest.
