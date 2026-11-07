---
name: visual
description: Create a hosted Deep Pattern diagram, infographic, or comic from DeepSeek Harness.
---

# Deep Pattern Visual for Harness

Use only `mcp__visual__visual_submit`, `mcp__visual__visual_status`,
`mcp__visual__visual_result`, `mcp__visual__visual_cancel`, and
`mcp__visual__visual_open_view`. If they are absent, call
`mcp__visual__connect` once with `action: connect`, complete browser OAuth, and
wait for connected state before resuming the original request.

Choose `diagram`, `infographic`, or `comic` from the user's requested output.
Create one stable idempotency key and submit exactly once. Never replace a
queued or running task after OAuth, reconnect, timeout, or a user follow-up.

Immediately after submit, show the exact `Open visual progress:` link and task
ID before the first status call. Harness native cards display each submit,
status, result, cancel, or viewer response, but the protected browser viewer is
the durable live surface. Do not rewrite, prefetch, exchange, or cache its URL.

Keep the same conversation alive with `visual_status`, `wait_seconds: 20`, and
`include_view_resource: false`. When terminal, call `visual_result` exactly
once and return the result. Never expose artifact bytes in model text. The
viewer owns fit, zoom, copy, download, and follow-up interaction.

Closing Harness or the viewer does not cancel generation. Call
`visual_cancel` only when the user explicitly asks. Never call Audit tools,
legacy routes, local runtime executables, or a second submit for the same user
request.
