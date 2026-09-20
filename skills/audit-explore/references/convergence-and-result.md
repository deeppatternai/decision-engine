# Convergence And Result

Read this file only on a hosted path when entering P4, interpreting its terminal
result, or rendering the hosted final envelope. The client-only route remains entirely in
`consent-and-framing.md` and must not follow submission or polling instructions here.

## P4 Converge And Falsify

Draft a hypothesis from the approved frame, selected HMW reframe, and selected solution directions.
Reclassify the complete P4 outgoing arguments under the P0 routing rules. Routine content proceeds.
For sensitive but shareable content, show the complete exact user-derived payload, including the
repeated frame and selected directions, and obtain approval for that complete payload before
submission. If approval is pending, do not submit or switch silently to local-only. Remove prohibited
content or keep it local. Submit once with the selected `audit_mode` and P3 trust-chain pointers:

```text
SUBMIT_TOOL(
    skill_name="audit-explore-converge",
    args={
        "title": "<idea slug> - converge and falsify",
        "content": "<hypothesis draft with approved frame and selections>",
        "audit_mode": "fast" | "standard" | "deep",
        "premortem": true,
        "upstream_run_id": "<P3 run_id>",
        "upstream_canonical_sha": "<P3 envelope canonical_sha>",
    },
) -> envelope {run_id, status="queued", ...}
```

Apply the hosted waiting and observation rules from `hosted-exploration.md`. The server performs the
critique, Klein premortem, and Goldilocks gate. It returns the formed hypothesis together with
`goldilocks_pass` and `exit_ready`.

The Goldilocks gate checks whether a majority of successful panel seats supplied at least three
falsification criteria, three explicit assumptions, a null hypothesis or default outcome, and a base
rate or reference class. `goldilocks_pass` and `exit_ready` report that output structure, not the
quality of the user's hypothesis. Use `convergent_assessment` for the quality decision: `speculative`
or a weaker assessment does not pass; `promising_with_concerns` passes only with the risks and
objections shown prominently; stronger assessments pass. A pass also requires `exit_ready=true`.
A missing or unknown assessment, or a missing or false `exit_ready`, does not pass.

When the draft does not pass, show the critique and revise it with the user. Only after the user
approves the revised draft may the client resubmit to `skill_name="audit-explore-converge"`, using
the same `audit_mode` and the latest run ID and `canonical_sha` as `upstream_run_id` and
`upstream_canonical_sha`. Recheck the complete revised payload under P0; any change to sensitive
content needs a new exact-payload approval before resubmission. Count the initial call as round one
and allow a maximum of three critique rounds in total. After the cap, stop and explain the unmet
quality or structure condition. If consensus remains `speculative`, say so explicitly rather than
calling it a structure failure. Report the server's actual `exit_ready` value; do not replace it.

The client must not set `exit_ready`, `goldilocks_pass`, or other server-owned trust fields. A local
client-only result must omit them or clearly mark them unavailable.

## Trust Signals

| Signal | Meaning |
|---|---|
| `canonical_sha` | SHA-256 over the canonical payload; always present |
| `panel_size` | All assigned external panel seats, including failed or unparsed seats |
| `convergent_assessment` / `convergent_count` | Most common assessment and agreement count over all assigned seats |
| `framework_diversity_check` | `HIGH`, `MEDIUM`, or `LOW` server assessment of distinct lenses |
| `lens_injection` | Confirms server-side dynamic methodology injection |
| `phase` (submission response) | Submitted phase |
| `premortem_applied` | Whether the premortem ran, when reported |
| `goldilocks_pass` / `exit_ready` | Whether a majority of successful seats supplied all four required output fields; not a hypothesis-quality judgment |
| `goldilocks_pass_count` | Passing successful seats over all successful seats |
| `upstream_run_id` / `upstream_canonical_sha` (submission response) | Cross-phase provenance chain |

`panel_size` and the denominator of `convergent_count` include failed or unparsed seats;
`goldilocks_pass_count` uses successful seats as its denominator. Different denominators are normal.
Show how many seats actually produced usable results and how many did not.

Validate `phase` and upstream pointers in the submission response against the request chain. Their
absence from the later result response is normal. If `canonical_sha` is missing or malformed,
surface the contract mismatch and stop before the next hosted phase or handoff. Preserve other
unknown or missing trust signals as unavailable rather than manufacturing values.

`framework_diversity_check=LOW` is a warning about shared framing or correlated bias, not confidence.
Surface it prominently. Convergence is agreement among the external voices, not proof.

## Quality Discipline

- Equal-weight candidates until P4. Do not rank P2 or P3 candidates by confidence.
- Klein premortem is mandatory in P4 through `"premortem": true`.
- Keep the independent client view outside `panel_size` and every convergence count.
- Preserve high-information disagreements and missing falsifiers instead of smoothing them away.

## Exit Envelope

Assemble a typed result containing:

- `frame`: the user-approved P1 artifact;
- `candidates`: P2 and P3 candidates plus the user's selections;
- `formed_hypothesis`: the P4 hypothesis and falsification material;
- `panel_participation`: phase run IDs and available panel metadata;
- `next_skill_handoff`: the proposed destination and the minimum context it needs.

Include `goldilocks_pass`, `exit_ready`, trust signals, and the cross-phase chain only as returned in
the corresponding server responses. `panel_participation` audit IDs may be re-queried for seven days
when the service retains them.

Show the complete envelope to the user before taking another action. Require explicit confirmation
before invoking any downstream skill:

- `/audit-brainstorming` to challenge the formed hypothesis;
- `/audit-market-research` to acquire or validate external evidence;
- `/audit-writing-plans` to turn accepted conclusions into implementation documentation;
- `/audit` to inspect a resulting deliverable for defects.

The panel is one input to the user's decision, not the final authority.
