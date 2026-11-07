---
name: audit
description: Run a Hosted Deep Pattern audit from DeepSeek Harness.
---

# Deep Pattern Audit for Harness

Use only these remote tools:

- `mcp__audit__audit_skill_submit`
- `mcp__audit__audit_skill_status`
- `mcp__audit__audit_skill_result`
- `mcp__audit__audit_skill_events`

If those tools are absent, call `mcp__audit__connect` with `action: connect`.
When it reports `authorizing`, tell the user to complete the browser activation
that was opened. Then call `mcp__audit__connect` with `action: status` and
`wait_ms: 20000` until it reports `connected`, the user cancels, or the
authorization attempt fails. Do not submit while authorization is incomplete.
Do not create a second activation attempt while one is already active.

For an audit request, create one idempotency key and retain it for the complete
request. If the user does not select a tier, use `standard` without asking.
Submit exactly once with `mcp__audit__audit_skill_submit`. OAuth, reconnects,
status waits, and user follow-ups must not generate a second idempotency key or
a replacement task.

Immediately after submit succeeds, present its exact `Open audit progress:`
Markdown link and task ID in a user-visible assistant message. Do this before
the first status call; never defer the link until the terminal result. Harness
does not currently promise an inline MCP Apps card, so the hosted browser WebUI
is the live progress surface. Do not rewrite, prefetch, exchange, or cache its
URL. Closing that page does not cancel the server task.

Keep the original conversation alive with bounded calls to
`mcp__audit__audit_skill_status` for the same task ID. Use `wait_seconds: 20`
and `include_view_resource: false`. Each call must remain within the host tool
timeout. Do not call events as a polling substitute and do not submit again.
When status becomes terminal, call `mcp__audit__audit_skill_result` exactly once
and return the complete result in the conversation, repeating the same progress
link with the task ID.

The plugin may append a `deep-pattern-audit` continuation message after submit
or status. Treat it as the authoritative continuation for that existing task:
follow it before ending the turn, never turn it into a second submission, and
never replace its task ID. The continuation exists so this behavior is stable
across user languages and model choices.

If the host ends the turn before completion, retain and report the task ID and
the exact ResourceLink. A later result request resumes status/result reads for
that task; it never creates a replacement audit. Never invent results, expose
OAuth credentials, call legacy routes, or invoke local runtime executables.
