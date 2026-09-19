# Hosted Hypothesis Workflow

Read this file only after `/audit-brainstorming` has been selected and the current task exposes a
Decision Engine audit submit tool.

## Pre-flight

- Confirm the artifact is a formed hypothesis, strategy, or proposal rather than a vague idea or a
  deliverable awaiting defect review.
- Select `standard` by default, `fast` only for an explicitly requested surface pass, and `deep`
  for high-stakes or irreversible decisions.
- Select `stakes` from `low`, `medium`, `high`, or `irreversible`. High and irreversible stakes
  require substantive epistemology fields in the result.
- Do not submit prohibited or unredacted sensitive content.

## Context Framing

Append the following framing to `context` unless the artifact already contains equivalent framing
or the user explicitly requests a positive-only exercise. If skipped, tell the user why.

```text
Apply Steelman + Pre-mortem framing:
1. Steelman the strongest counterargument first. Construct it as if defending it would win, then
   evaluate it.
2. Pre-mortem: assume this idea failed 12 months from now. Identify the most plausible cause and
   trace backward to the decisions or assumptions that enabled it.
3. Preserve strengths-first ordering and do not relabel risks as strengths.
```

## Submit

Bind the exact callable names exposed by the host to these explanatory placeholders. Never invoke
the placeholders literally.

```text
SUBMIT_TOOL(
    skill_name="audit-brainstorming",
    args={
        "title": "<short label>",
        "content": "<formed hypothesis, strategy, or proposal>",
        "context": "<decision stage, alternatives, and constraints>" + RECOMMENDED_FRAMING,
        "stakes": "low" | "medium" | "high" | "irreversible",
        "mode": "fast" | "standard" | "deep",
        "domain": "<single domain>" | ["<multiple domains>"],
    },
) -> envelope {run_id, status="queued", ...}

STATUS_TOOL(skill_name="audit-brainstorming", run_id=...)
RESULT_TOOL(skill_name="audit-brainstorming", run_id=...)
EVENTS_TOOL(skill_name="audit-brainstorming", run_id=...)
```

`artifact_intent` is forced to `hypothesis` server-side. Do not pass a competing value or infer a
different contract from the artifact's format.

## Wait In The Same Turn

Submission is asynchronous. Poll status with capped backoff, using roughly ten-second intervals
for ordinary runs. Deep runs can take substantially longer; elapsed time alone is not failure.

1. Show each auditor as `Voice 1..N` with its `pending`, `running`, `completed`, or `failed` state.
2. On `completed`, fetch the terminal result. On `failed` or `cancelled`, surface the reason and run
   ID without presenting the envelope as a clean analysis.
3. Treat transport errors and unknown observations as unknown, not as running or terminal. Retry
   transient observations with capped backoff and preserve the run ID.
4. If a request may have been accepted but its state remains unobservable, stop rather than
   submitting a duplicate.
5. Stay interruptible and use a recovery checkpoint appropriate to the selected mode.

## Independent Client View

While the panel runs, analyze the hypothesis independently with the same Steelman and pre-mortem
framing. Form that view before reading panel conclusions. When results arrive, present the client
view as a separate, co-equal perspective and never add it to the panel's convergence count.

After a terminal result is fetched, read [result-contract.md](result-contract.md) before presenting
or acting on it.
