# Hosted Exploration

Read this file only after the user approves the P1 frame, the P0 route is ready for hosted submission,
and the host exposes a Decision Engine submit tool. It owns P2 and P3.

## Pre-flight

- Bind the exact callable names exposed by the host to the explanatory placeholders below. Never
  invoke a placeholder literally.
- The logical lifecycle operations are `audit_skill_submit`, `wait_audit`, `audit_skill_status`,
  `audit_skill_result`, `audit_skill_events`, and `audit_skill_cancel`. Use only the exact names and
  operations the current host exposes. Cancellation is user-directed and capability-dependent.
- Preserve the selected `audit_mode` through P2, P3, and P4. Use `deep` for high-stakes or
  irreversible decisions and `standard` for ordinary exploration unless the user chose another
  supported mode.
- No model-family exclusion preflight is required. Use the server-configured panel without a local
  roster query. Do not claim that it excludes the client's model family or invent an unsupported
  request field.
- The server injects each vendor methodology lens. The client must not pass a methodology lens,
  framework, or replacement `artifact_intent`.

Before each P2 and P3 external submission, classify the complete outgoing arguments under the P0
rules in `consent-and-framing.md`. Routine payloads proceed directly. For sensitive but shareable
content, show the complete exact user-derived payload, including repeated frame material and selected
options, and obtain approval for that complete payload. Any change requires renewed approval. While
approval is pending, do not submit or switch silently to local-only. Remove prohibited content or
keep it local. P4 has the same gate in `convergence-and-result.md`.

## P2 Diverge Problem

Submit the approved frame once:

```text
SUBMIT_TOOL(
    skill_name="audit-explore",
    args={
        "title": "<idea slug> - problem divergence",
        "content": "<approved frame artifact>",
        "phase": "diverge_problem",
        "domain": "<subject domain>",
        "audit_mode": "fast" | "standard" | "deep",
    },
) -> envelope {run_id, status="queued", ...}
```

The server forces `artifact_intent="explore_diverge"`. Candidate counts depend on the configured
panel and each seat's response. Render every returned HMW reframe and PO provocation grouped by
vendor, using the actual counts and without ranking by confidence. The user selects one problem
reframe before P3.

## P3 Diverge Solution

Submit the selected reframe together with the approved frame and P2 trust-chain pointers:

```text
SUBMIT_TOOL(
    skill_name="audit-explore",
    args={
        "title": "<idea slug> - solution divergence",
        "content": "<selected HMW reframe and approved frame>",
        "phase": "diverge_solution",
        "domain": "<subject domain>",
        "audit_mode": "fast" | "standard" | "deep",
        "upstream_run_id": "<P2 run_id>",
        "upstream_canonical_sha": "<P2 envelope canonical_sha>",
    },
) -> envelope {run_id, status="queued", ...}
```

Solution-direction counts depend on the configured panel and each seat's response. Render every
returned direction grouped by vendor with equal weight, using the actual counts. The user selects
one or two solution directions before P4.

## Wait And Observe

Submission is asynchronous. Prefer the host's bounded wait operation when available; otherwise poll
the exposed status operation with capped backoff, using roughly ten-second intervals for ordinary
runs. Never infer failure from elapsed time.

1. Show each vendor's `pending`, `running`, `completed`, or `failed` state without exposing hidden
   methodology prompts.
2. On `completed`, fetch the result with the host's result operation. On `failed` or `cancelled`,
   surface the reason and run ID without presenting a clean result.
3. Treat an unknown or transport-error state as unknown. Retry transient observation failures with
   capped backoff, reconcile with result or event operations when available, then stop without
   guessing if the run remains unobservable.
4. If submission may have succeeded, preserve the run ID and do not submit a duplicate.
5. Stay interruptible. Do not end the turn while a required run is still active unless a registered
   host-native waiter will resume the task.

If the same vague idea appears within five turns and has not materially changed, render the existing
envelope instead of running P2 again. If the frame or hypothesis has materially changed, a new run is
allowed and must receive its own trust chain.

## Independent Client Voice

While each panel runs, form an unanchored independent client voice before reading the panel result:

- P2: create additional problem reframes.
- P3: create additional solution directions.
- P4: create an independent falsification view.

Show this contribution separately as a co-equal perspective. Never include it in panel convergence
counts, `panel_size`, `convergent_count`, or `framework_diversity_check`.

After the user chooses one or two P3 directions, read
[convergence-and-result.md](convergence-and-result.md).
