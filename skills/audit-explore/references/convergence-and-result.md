# Convergence And Result

Read this file only on an authorized external path when entering P4, interpreting its terminal
result, or rendering the hosted final envelope. The client-only route remains entirely in
`consent-and-framing.md` and must not follow submission or polling instructions here.

## P4 Converge And Falsify

Draft a hypothesis from the approved frame, selected HMW reframe, and selected solution directions.
Submit it once with the P3 trust-chain pointers:

```text
SUBMIT_TOOL(
    skill_name="audit-explore-converge",
    args={
        "title": "<idea slug> - converge and falsify",
        "content": "<hypothesis draft with approved frame and selections>",
        "premortem": true,
        "upstream_run_id": "<P3 run_id>",
        "upstream_canonical_sha": "<P3 envelope canonical_sha>",
    },
) -> envelope {run_id, status="queued", ...}
```

Apply the hosted waiting and observation rules from `hosted-exploration.md`. The server performs the
critique, Klein premortem, and Goldilocks gate. It returns the formed hypothesis together with
`goldilocks_pass` and `exit_ready`.

The Goldilocks gate expects at least three falsification criteria, at least three explicit
assumptions, a null hypothesis or default outcome, and a base rate or reference class. If the gate
fails, show the critique and revise the draft with the user. Only after the user approves the revised
draft may the client resubmit to `skill_name="audit-explore-converge"`, using the latest run ID and
`canonical_sha` as `upstream_run_id` and `upstream_canonical_sha`. Count the initial call as round one
and allow a maximum of three critique rounds in total. After the cap, stop with `exit_ready:false`
and explain what remains missing; do not loop indefinitely.

The client must not set `exit_ready`, `goldilocks_pass`, or other server-owned trust fields. A local
client-only result must omit them or clearly mark them unavailable.

## Trust Signals

| Signal | Meaning |
|---|---|
| `canonical_sha` | SHA-256 over the canonical payload; always present |
| `panel_size` | Number of external panel voices |
| `convergent_assessment` / `convergent_count` | Most common assessment and external agreement count |
| `framework_diversity_check` | `HIGH`, `MEDIUM`, or `LOW` server assessment of distinct lenses |
| `lens_injection` | Confirms server-side dynamic methodology injection |
| `phase` / `premortem_applied` | Echoed phase and whether the premortem ran |
| `goldilocks_pass` / `exit_ready` | Whether the hypothesis satisfies the server exit gate |
| `upstream_run_id` / `upstream_canonical_sha` | Cross-phase provenance chain |

Validate that the returned phase and upstream pointers match the request chain. If `canonical_sha`
is missing or malformed, surface the contract mismatch and stop before the next hosted phase or
handoff. Preserve other unknown or missing trust signals as unavailable rather than manufacturing
values.

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

Include `goldilocks_pass`, `exit_ready`, trust signals, and the cross-phase chain only as returned by
the server. `panel_participation` audit IDs may be re-queried for seven days when the service retains
them.

Show the complete envelope to the user before taking another action. Require explicit confirmation
before invoking any downstream skill:

- `/audit-brainstorming` to challenge the formed hypothesis;
- `/audit-market-research` to acquire or validate external evidence;
- `/audit-writing-plans` to turn accepted conclusions into implementation documentation;
- `/audit` to inspect a resulting deliverable for defects.

The panel is one input to the user's decision, not the final authority.
