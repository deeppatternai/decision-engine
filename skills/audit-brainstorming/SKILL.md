---
name: audit-brainstorming
description: "Stress-test a formed hypothesis, strategy, or proposal before commitment using an external panel. Use when the user wants to identify strengths, risks, counterarguments, assumptions, falsifiers, base-rate questions, or missing evidence. Use audit-explore for vague ideas, audit-market-research to gather or verify external evidence, audit-writing-plans to operationalize accepted conclusions, and audit for defects in deliverables."
---

# /audit-brainstorming - Hypothesis stress-test panel

Thin client routing skill for the Decision Engine hub. The server forces
`artifact_intent="hypothesis"` and selects the thinking-partner prompt; that prompt never reaches
the client.

## Routing

Use this skill when the user has a formed hypothesis, strategy, product idea, research proposal,
or decision proposal and wants its reasoning challenged before commitment or implementation.

| Request | Route |
|---|---|
| Develop a vague idea into a testable hypothesis | `/audit-explore` |
| Challenge a formed hypothesis or identify missing evidence and falsifiers | `/audit-brainstorming` |
| Gather, verify, or cite external market, competitor, or customer evidence | `/audit-market-research` |
| Turn accepted conclusions into an implementation document | `/audit-writing-plans` |
| Find defects, vulnerabilities, or omissions in a deliverable | `/audit` |

Representative requests include "stress-test this strategy," "give me the strongest case against
this proposal," "which assumptions could invalidate this hypothesis," and "run a pre-mortem on
this decision." Generic words such as "idea," "strategy," "brainstorm," or "review" are not
sufficient without hypothesis-testing intent.

Identifying what evidence is missing belongs here; acquiring or validating that evidence belongs
to `/audit-market-research`. For a request that explicitly contains both operations, separate them
and ask before chaining a second skill.

An explicit invocation prevents silent rerouting but does not override semantic or safety
constraints. If the requested operation is incompatible with hypothesis analysis, explain the
mismatch and ask before using the appropriate adjacent skill.

## Route Before Submitting

1. Inspect the current task's tool list. Treat `mcp__decision-engine__*` and
   `mcp__decision_engine__*` as equivalent spellings and call the exact spelling the host exposes.
2. Before a hosted submission, read [hosted-workflow.md](references/hosted-workflow.md) and follow
   its submission, waiting, and failure rules.
3. Keep an asynchronous run in the current turn until it reaches a terminal state or its state is
   genuinely unobservable. Never duplicate a request whose outcome is unknown.
4. Before presenting a terminal result, read
   [result-contract.md](references/result-contract.md) and apply its hypothesis-specific schema,
   trust, and adjudication rules.

## Invariants

- Never submit secrets, credentials, personal or regulated data, or confidential, restricted, or
  proprietary material. Redact it first; if safe redaction would invalidate the analysis, stop and
  explain the limitation.
- The server-forced hypothesis intent is authoritative. Do not pass or simulate a prescriptive
  defect-review intent through this skill.
- Reuse the prior run ID when the same hypothesis was processed within five turns and has not
  materially changed.
- Any transition to another skill is a recommendation that requires user confirmation.
