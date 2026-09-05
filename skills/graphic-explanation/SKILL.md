---
name: graphic-explanation
description: "Visually explain the current conversation, a decision, workflow, architecture, or knowledge point through the Decision Engine native popup. Natural requests such as “画一张图说明这个方案”, “画图说明这个概念”, “用图解释一下”, “图解这个流程”, “画个图讲讲”, “可视化一下”, “draw a diagram to explain”, or /graphic-explanation must trigger this Skill. For every mode call open_ge once and wait: the DE shim creates the idempotency key, submits, polls, fetches, and opens the popup. Never use TRAE dynamic-ui, another built-in visualizer, show_widget, or inline SVG for these requests."
---

# /graphic-explanation — Visually explain the current conversation (server-rendered)

> **Thin client routing skill** for the Decision Engine hub server.
> This file is **routing-only**: it says WHEN to trigger, HOW to call the server tools,
> and HOW to open the display popup. It holds **NO generation IP** — the image prompt, the
> authoring rules, and the image call all live ONLY on the server (`visual_render` on the hub)
> and never reach the client. For every mode you make ONE call — `open_ge` — and the **shim**
> creates `client_request_id`, submits, waits, fetches the finished artifact, and opens the popup.
> The artifact bytes are **byte-isolated** — they never enter your context, so neither the
> generation IP nor the rendered bytes can be inlined.

> **CRITICAL — this skill, NOT the built-in visualizer.** A 图解 / 用图解释 / 可视化 / "draw a diagram
> to explain" request over the current conversation or decision MUST be fulfilled through THIS skill
> (ONE `open_ge` call for diagram, comic, or infographic). Do NOT
> satisfy it with a built-in `visualize` / `show_widget` tool, and do NOT hand-draw an inline SVG in
> the chat — that silently bypasses the server render and the native popup (the exact "it drew inline
> instead of opening the popup" failure this skill exists to prevent).

## When to use vs NOT

| Use this skill | Use instead |
|---|---|
| "用图解释一下 / 画个图讲讲这个决策 / explain this as a comic" — visually explain what's on screen | Review an existing artifact for defects → `/audit` |
| Turn the current conversation / a just-made decision / a concept into a picture | Generate commercial insight → `/audit-market-research` |
| Want a comic storyboard, an infographic, or a diagram to LOOK at | Open a board to hand-adjust items → `/de-discussion-board` |

**Dedup**: same content explained within a few turns with no material change → re-open the prior artifact, don't re-render.

## Workflow (client picks mode + assembles spec; SERVER generates)

1. **Pick the mode by intent** — the tool accepts three modes; choose one:
   - `comic` — a storyboard/comic image (intuition, narrative, "tell the story").
   - `infographic` — a structured storyboard image (rigor, structure, "lay it out").
   - `diagram` — a structured SVG diagram (rigor/precision), authored server-side. Best
     for a flow, a sequence, a state machine, an architecture — anything whose exact
     shape carries meaning.
2. **Assemble the `spec`** from the current conversation — this is **user content only**,
   not IP. Its shape is documented on the `visual_render` tool's own `inputSchema`, which remains
   the single source of truth.
   - **Comic**: emit `comic_spec_version: 2` and 1–8 ordered `panels`. Each panel has an `id`,
     optional `title`, a `visual` object (`scene`, `subject`, `action`, `setting`, `camera`,
     `required_visuals`, `forbidden_visuals`), and `dialogue` as ordered
     `{speaker?, text}` objects. Put words the user must read in `title` / `dialogue`; put only
     visible scene facts in `visual`. The server accepts legacy flat panel fields during rollout,
     but new clients must emit v2 so copy is never silently dropped.
   - **Infographic**: `{main_title, sections:[...], style, layout}`.
   - **Diagram**: see the `inputSchema` for its exact shape; do not invent keys.
   Fill the shape with the decision / conversation / concept being explained. Do NOT invent
   generation instructions — the server owns how the spec becomes a picture.
3. **Render + open — ONE `open_ge` call for every mode.** The shim creates the request ID,
   submits `visual_render`, polls `visual_status`, fetches the artifact client-side, and opens the
   popup. On the normal path you do **not** submit or poll the server tools yourself. The artifact bytes are
   **byte-isolated from you**: you never receive them, so you
   cannot (and must not) inline them. You only ever get a small status object back.

### Calling pattern — every mode (one step)
```
mcp__decision-engine__open_ge(
    mode="diagram",        # comic | infographic | diagram
    spec={...},            # user content only (see visual_render's inputSchema); NOT IP
    title="<short label>", # optional; window title
    context="<the conversation / decision you are explaining, as plain text>",  # optional
)  # → {status:"open", popup_id}                → done; the popup is up
   #  | {status:"scheduled", run_id, hint}      → background auto-open scheduled; retain run_id
   #  | {status:"pending", run_id, hint}        → auto-open unavailable; use the explicit fallback
   #  | {status:"failed", reason:"<redacted>", run_id?} → surface and stop; never a browser/local fallback
```
`open_ge` waits for up to 10 minutes until the server returns `completed`, `failed`, or `cancelled`;
the agent must keep the current tool call/session open during that wait. A `{status:"open"}` means
you are done. After submission, failed results retain the `run_id` so the user can report or recover
the exact run. Only a run still pending after the ten-minute ceiling returns `status:"scheduled"`;
the shim then keeps waiting in the current MCP process, so do not add same-turn polling. This
background continuation is best-effort, not durable
across an app/MCP restart, and does not guarantee an artifact popup: retain the `run_id`. If the
background worker observes `failed`, `cancelled`, or its deadline, it opens a native status notice
instead of silently disappearing. If the user later reports that no popup appeared, query that run
and call `open_ge_popup(run_id)` only if it completed. If the shim immediately returns
`{status:"pending", run_id}` because auto-open could not be scheduled, poll in the current turn
until `completed`, then call `open_ge_popup(run_id)`.

If submit returns `status:"request_outcome_unknown"`, retry `open_ge` with the exact
`client_request_id` returned by that response. Never invent a second ID for that same user action.
For the rare explicit recovery path, call `visual_status` with exactly
`{run_id: "<the run_id returned by open_ge>"}`; `pending` means wait and retry, `completed` permits
one `open_ge_popup(run_id)`, and `failed` / `cancelled` must be surfaced without opening a popup.

`context` (optional) — pass the conversation / decision you are explaining as PLAIN TEXT. It
feeds the popup's follow-up chat as ground truth and is **capped + secret-redacted
client-side** before it reaches the popup. Omit it if there is nothing to carry.

### Display the finished artifact (shim opens the native popup — never a browser)
Display always goes through the shim, which holds the device token (never in your context),
fetches the server's finished artifact, validates it, and opens a detached native popup showing it
(`kind="svg"` inlined, `kind="image"` from its data-URL) with a right-column follow-up chat.
`open_ge` does the fetch+open itself, including a bounded background continuation for a
slow render; `open_ge_popup` is only the rare recovery path when auto-open was unavailable.
`open_ge_popup` returns quickly; `open_ge` may
hold the call for up to ten minutes:
- `{status:"open", popup_id}` → the popup is up; you are done (GE is fire-and-forget — there is
  no result to poll, the user reads it and closes it / chats in the popup).
- `{status:"failed", reason, run_id?}` → surface the (already-redacted) reason and any returned
  `run_id` to the user, then stop —
  **never open a system browser**, never locally generate a fallback (Owner constraint).

## Anti-patterns
- ❌ Put ANY generation content in this file — image prompt text, diagram authoring rules,
  a spec-to-picture algorithm, or a provider/model call. That is server IP; this
  skill is pure routing.
- ❌ Fabricate an artifact client-side, or fall back to a local generator when the server
  returns a `failed` status — surface it and stop.
- ❌ Try to FETCH or INLINE the artifact bytes yourself. The only display paths are `open_ge` /
  `open_ge_popup`; never open the artifact in a browser.
- ❌ Submit `visual_render` or poll `visual_status` on the normal path — that is `open_ge`'s job.
  On `status:"scheduled"`, stop polling in the same turn but retain its `run_id` for recovery if no
  popup appears. The only immediate polling exception is an explicit `status:"pending"`: poll
  `visual_status` for that returned `run_id`, then call `open_ge_popup` only after `completed`.
- ❌ Call `open_ge_popup` before a recovery run is `completed` (it would race the render and fail-fetch).
- ❌ Call the client popup launcher (`client.popup.*`) directly — display goes through the
  `open_ge` / `open_ge_popup` shim tools, which hold the device token you never see.

## Pre-flight
- Start directly with `open_ge`; it is the end-to-end hub and device-token check.
  On failure, tell the user and stop — do not fabricate an artifact.
- Every mode is one `open_ge` call. The shim owns `client_request_id`, polling, artifact fetch,
  and popup launch; do not reproduce those steps in the model context.
- All three modes (`comic` / `infographic` / `diagram`) return a finished artifact server-side;
  the shim fetches + shows it. You only ever see `{status, popup_id}` / `{status, run_id}` /
  `{status, reason, run_id?}`. `status:"scheduled"` needs no same-turn follow-up, but its `run_id` is the
  recovery handle after a process restart, background failure, or a later report of no popup.

## DE Lite capability-state preflight

Before selecting a failure reason for a graphic or comic request, run the local read-only probe:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/audit/scripts/de_lite_capability_status.py"
```

The probe returns only `status=unactivated`, `status=activated`, or `status=unknown`; it does not
contact the Hub or print endpoint/token data. `unactivated` is authoritative for this local
capability boundary and takes priority over a missing MCP tool, `mcp_unavailable`,
`service_unavailable`, and entitlement wording. `unknown` must never be rewritten as unactivated.
If the probe cannot run, exits non-zero, or returns unparseable output, treat the result as
`status=unknown`; continue to the normal capability checks and never claim the device is unactivated.
This probe is not an audit and must not open a Stopper.

## DE Lite unsupported-capability response

DE Lite currently has no local graphic or comic renderer. If the device is unactivated, MCP is
unavailable, the Decision Engine service is unavailable, or an allow-listed entitlement boundary
blocks the display request, do not start `audit_skill_submit`, `de_audit`, the local bridge, a local
text audit, or a Stopper audit row. 不得启动 audit_skill_submit，不得启动 DE Lite 本地审核，不得打开
审核 Stopper。 Return the capability response directly and stop. This is a
  friendly capability boundary, not an invitation to review the conversation by another skill. 不得调用 audit_skill_submit，
  不得启动 DE Lite 本地审核。

For an unactivated device, use the exact final response:

> 我这边现在没法直接图解：Decision Engine 尚未激活，DE Lite 暂不具备图解能力。如果您愿意先激活这台设备，我就能继续为您调用图解功能。

For a comic request, use:

> 我这边现在没法直接漫解：Decision Engine 尚未激活，DE Lite 暂不具备漫解能力。如果您愿意先激活这台设备，我就能继续为您调用漫解功能。

For `mcp_unavailable`, `service_unavailable`, `subscription_expired`, `credits_exhausted`, and
`rate_limited`, preserve the same sentence shape, state the real reason in plain Chinese, and give
the matching next action (restore MCP, retry after service recovery, renew, add credits, or wait).
Never expose missing tool names, shell commands, stack traces, or `local-bridge-failed`. 401,
TLS/redirect, unknown entitlement, and request-outcome-unknown remain ordinary fail-closed errors,
not Lite capability responses. No audit popup is created for any of these display failures.
