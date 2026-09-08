# Hosted audit workflow

Read this file only after the current task exposes a Decision Engine audit submit tool.

## Pre-flight

- Confirm the device token is configured through the normal Decision Engine login flow.
- Use `deep` for high-stakes or irreversible work; use `standard` for the normal defect panel and
  `fast` for an explicitly requested surface check.
- Expect size pre-flight around 480 KB for `fast`/`standard` or 240 KB for `deep`, bounded by the
  smallest reasoner's context window.

## Optional diagnostics

For explicitly requested authentication diagnostics, use the `fast_smoke` profile.

## Submit

Bind the exact exposed callable names to the placeholders below. The placeholders are explanatory;
never invoke them literally.

```text
SUBMIT_TOOL(
    skill_name="audit",
    args={
        "title": "<short stopper-panel label>",
        "content": "<full artifact text>",
        "context": "<2-4 sentence briefing>",
        "mode": "fast" | "standard" | "deep",
        "stakes": "low" | "medium" | "high" | "irreversible",
        "artifact_intent": "prescriptive",
        "ui_locale": "zh-CN" | "en-US",
        "focus": "logic" | "security" | "performance" | "all" | "<freeform>",
        "domain": "<single>" | ["<multi>"],
    },
) -> returns envelope {run_id, status="queued", ...}

STATUS_TOOL(skill_name="audit", run_id=...)
RESULT_TOOL(skill_name="audit", run_id=...)
EVENTS_TOOL(skill_name="audit", run_id=...)
```

The explicit user language wins; otherwise derive `ui_locale` from the current conversation, never
from the artifact alone. Keep findings, headings, labels, explanations, and the final note in the
same locale. Supported values are `zh-CN` and `en-US`; host configuration and system UI provide
fallbacks, then English. `DE Lite` and `Decision Engine` remain unchanged product names.

## Wait in the same turn

Submission returns immediately while the panel runs off-thread. Poll rather than holding a server
thread. Do not send a final response or yield while status is `queued` or `running`. Between polls,
continue other unfinished work and complete the independent self-review below.

1. Poll status with backoff; about ten seconds is normally sufficient. Deep audits can take 15-30
   minutes. Never infer failure from elapsed time.
2. Re-render each auditor's `pending`, `running`, `completed`, or `failed` progress, redacted as
   `Voice 1..N`.
3. On `completed`, fetch the final result. On `failed` or `cancelled`, surface the reason and run ID;
   do not present an envelope as a clean result.
4. Treat unknown/error observation responses as unknown, not running or terminal. Retry transient
   failures with capped backoff. Give an immediate `run_not_found` brief grace, then reconcile with
   events and result calls. Preserve the run ID if observation remains unknown.

Keep a foreground recovery checkpoint sized to the run class; a deep audit can run 15-30 minutes,
so do not use a generic short timeout and stay interruptible. At the checkpoint, preserve the run ID,
retry reconciliation, and either continue foreground polling or use the registered-waiter handoff.
There is no agent-controlled `STOP` while a run is non-terminal or unobservable.

If an agent-controlled handoff is unavoidable, first register a host-native background waiter that
will wake the parent on a terminal state. A detached shell process is not enough. If the host ends
before waiter registration succeeds, retain the run ID in durable conversation state, resume polling
on the next invocation, and never claim automatic wake-up was armed.

Agent instructions deliberately expose no cancellation call. The Stop panel and CLI may request
cancellation only while a run is queued. Once execution starts, keep polling to
a terminal state.

## Modes

- `fast`: quick cross-vendor defect sanity check at low effort.
- `standard`: default cross-vendor reasoning panel for normal reviews.
- `deep`: strictest panel for production migrations, security-sensitive work, architecture, or
  irreversible decisions.

## Independent self-review

If you did not author the artifact, review it independently while the panel runs and merge your view
as a separate, co-equal voice. Do not inflate the external panel's `X/N` convergence count. If you
authored the artifact, remain adjudicator-only and do not grade your own work.
