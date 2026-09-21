---
name: graphic-explanation
description: "Open a Decision Engine native popup that visually explains the current conversation, a decision, workflow, architecture, or concept as a comic, infographic, or diagram. Use this skill for /graphic-explanation and when the user wants a generated visual explanation to view, including equivalent intent in any language. Do not use for an interactive artifact the user must edit and submit, for editing an existing image, or for data-analysis charts. Do not substitute an inline visual or another renderer."
---

# Graphic Explanation

Use this thin-client routing skill to turn conversation content into a server-rendered visual shown
in the Decision Engine native popup. The client selects a mode and supplies user content; all
generation prompts, authoring rules, model calls, and rendered artifact bytes remain on the server.

## Boundaries

- Use this skill when the user wants a comic, infographic, or diagram to look at.
- Use `discussion-board` when the user must rearrange, edit, annotate, or submit the artifact.
- Use `audit` when the primary intent is defect review of an existing artifact.
- Use `audit-market-research` when the primary intent is commercial or market insight.
- Use an image-editing capability when the request changes an existing image.
- Use a charting or visualization capability when the primary task is data analysis.
- Use the Decision Engine native popup only. Never use a built-in visualizer,
  `show_widget`, TRAE dynamic UI, an inline visual, hand-authored SVG, or a browser fallback.

For the same unchanged content repeated within a few turns, do not render a duplicate. If the user
explicitly asks to reopen it and a prior `run_id` is known, MUST read and follow the dedup route in
`references/recovery.md`.

## Progressive Disclosure

- Normal generation and opening: continue in this file.
- `scheduled`, `pending`, `request_outcome_unknown`, a dedup reopen, or a later report that no popup
  appeared: MUST read and follow [references/recovery.md](references/recovery.md) before acting.
- A missing tool, activation/entitlement problem, or unavailable service: MUST read and follow
  [references/capability-boundary.md](references/capability-boundary.md) before choosing the
  user-facing reason.

## Workflow

1. Pick exactly one mode from the user's intent:
   - `comic`: narrative, intuition, or a visual story.
   - `infographic`: structured explanation, comparison, or summary.
   - `diagram`: precise flow, sequence, state machine, relationship, or architecture.
2. Assemble `spec` from user and conversation content. This is content, not generation IP. Its
   shape is documented on the `visual_render` tool's own `inputSchema`, the single source of truth.
   - Comic: emit `comic_spec_version: 2` and 1–8 ordered `panels`. Each panel has an `id`,
     optional `title`, a `visual` object with `scene`, `subject`, `action`, `setting`, `camera`,
     `required_visuals`, `forbidden_visuals`, and `dialogue` as ordered `{speaker?, text}` objects.
     Put readable copy in `title` or `dialogue`, and only visible scene facts in `visual`;
     the server accepts legacy flat panel fields during rollout, but new clients must emit v2.
   - Infographic: use `{main_title, sections:[...], style, layout}`.
   - Diagram: follow the exact diagram schema in `visual_render` `inputSchema`; do not invent keys.
3. Make ONE `open_ge` call for every mode and wait for its result. The shim creates the
   `client_request_id`, submits, polls, fetches the artifact, validates it, and opens the popup.

```text
open_ge(
    mode="diagram",        # comic | infographic | diagram
    spec={...},             # user content only
    title="<short label>", # optional
    context="<plain-text conversation or decision>", # optional
)
```

`context` supplies ground truth to the popup's follow-up chat. It is capped and secret-redacted
client-side. Omit it when there is no useful context to carry.

## Result Routing

- `{status:"open", popup_id}`: the popup is open; stop.
- `{status:"scheduled", run_id, hint}`: read `references/recovery.md` and retain `run_id`.
- `{status:"pending", run_id, hint}`: read `references/recovery.md` and recover in this turn.
- `{status:"request_outcome_unknown", client_request_id, ...}`: read `references/recovery.md`.
- `{status:"failed", reason, run_id?}` or an uncallable tool: MUST read and follow
  `references/capability-boundary.md` before selecting any user-facing reason.

## Pre-flight And Safety

- Start directly with `open_ge`; it is the end-to-end hub and device-token check. Do not perform a
  separate provider-health probe or submit/poll `visual_render` directly on the normal path.
- Artifact bytes are byte-isolated and never enter model context. Never fetch, inline, reconstruct,
  or locally generate the artifact.
- The shim alone holds the device token and launches the popup. Never call `client.popup.*`.
- For `request_outcome_unknown`, reuse the exact returned `client_request_id`.
  Never invent a second ID for the same user action; follow `references/recovery.md`.
- Never place image prompts, rendering algorithms, provider details, or other generation IP in this
  client skill.
- A failed display request is not an audit request. Never open an audit or Stopper as fallback.
