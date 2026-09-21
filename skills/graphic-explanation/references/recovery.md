# Graphic Explanation Recovery

Read this route only when `open_ge` returns a nonterminal delivery state or the user later reports
that no popup appeared. The normal path remains one `open_ge` call.

## Status Contract

`open_ge` waits for up to 10 minutes for the server to return `completed`, `failed`, or `cancelled`.
Keep the tool call and current session open during that wait. A completed render is fetched and
opened by the shim without exposing artifact bytes to model context. After submission,
failed results retain the `run_id` so the exact run can be reported or recovered.

### `status:"scheduled"`

The render was still pending at the ten-minute ceiling and the shim scheduled a background
continuation. Retain the `run_id`, stop same-turn polling, and tell the user only what the returned
hint requires. Background continuation is best-effort, not durable across an app or MCP restart.

If the worker observes a background failure, cancellation, or deadline, it opens a native status notice
instead of silently disappearing. If the user later reports that no popup appeared, query
that `run_id` with `visual_status`; call `open_ge_popup(run_id)` exactly once only after the status is
`completed`.

### `status:"pending"`

Auto-open could not be scheduled. Poll `visual_status({run_id: "<returned run_id>"})` in the current
turn. `pending` means wait and retry. `completed` permits one `open_ge_popup(run_id)`. Surface
`failed` or `cancelled` without opening a popup.

### `status:"request_outcome_unknown"`

If this status is returned, retry `open_ge` with the exact
`client_request_id` returned by that response:

```text
open_ge(..., client_request_id="<returned client_request_id>")
```

Never invent a second ID for the same user action. This preserves idempotency when submission may
already have succeeded.

## Reopen An Unchanged Completed Artifact

When the user explicitly asks to reopen the same unchanged explanation and a known prior `run_id`
is available, query that run once with `visual_status`. If it is `completed`, call
`open_ge_popup(run_id)` exactly once. For `pending`, follow the pending route above. Surface
`failed` or `cancelled`; never re-render merely to satisfy dedup.

## Recovery Boundaries

- Use `visual_status` only for the explicit recovery and dedup-reopen cases above.
- Never call `open_ge_popup` before the run is `completed`.
- Never fetch or inline artifact bytes, invoke a local renderer, open a system browser, or call the
  popup launcher directly.
- `open_ge_popup` is a recovery or explicit dedup-reopen path only; `open_ge` owns normal display.
