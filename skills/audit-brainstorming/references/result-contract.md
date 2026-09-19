# Hypothesis Result Contract

Read this file after a hosted `/audit-brainstorming` run reaches a terminal state.

## Payload Shape

The hypothesis payload uses these fields:

- `overall_assessment`: `compelling`, `promising_with_concerns`, `speculative`, or `weak`.
- `strengths[]`.
- `risks[]`, including category, likelihood, impact, and mitigation when available.
- `counter_arguments[]`, including the represented perspective when available.
- `assumptions[]`, including evidence for and evidence against when available.
- `open_questions[]`.
- Epistemology fields: `falsifiability_criteria`, `null_hypothesis_or_default`,
  `base_rate_or_reference_class`, and `confidence_update_needed`.
- Decision-surface fields: `reversibility`, `blast_radius`, `second_order_effects[]`,
  `key_person_dependencies[]`, and `recommended_evidence_order[]`.

This is not a defect-finding schema. Do not convert the response into `findings`,
`dimension_status`, `blocking`, or `overall_verdict`. If those fields appear instead of the
hypothesis contract, surface a workflow mismatch and do not present it as a valid hypothesis result.

## Trust Signals

| Signal | Meaning |
|---|---|
| `canonical_sha` | SHA-256 over the canonical payload |
| `overall_assessment` | The resolved hypothesis assessment |
| `strengths_count` | Number of strengths |
| `risk_count` | Number of risks |
| `counter_argument_count` | Number of counterarguments |
| `assumption_count` | Number of assumptions |
| `open_question_count` | Number of open questions |
| `high_impact_risk_count` | Risks whose impact is high |
| `epistemology_fields_filled` | Number of populated epistemology fields out of four |
| `has_epistemology_fields` | Whether at least one epistemology field is populated |
| `panel_size` | Number of external panel voices |
| `convergent_assessment` | Most common panel assessment |
| `convergent_count` | Agreement count within the external panel |

All four epistemology fields being empty on a high-stakes run is itself a warning that the
hypothesis is not adequately grounded.

## Interpretation

- Convergence is evidence of agreement on a known signal, not proof that the hypothesis is true.
- Divergence on a key claim is a high-information gap that deserves attention rather than removal.
- Before concluding, identify what would falsify the hypothesis, which perspective is missing, and
  which base rate or reference class is relevant.
- For hypotheses about the LLM or AI commercial ecosystem, disclose that panel members may share
  incentives and training biases. Treat convergence as lower trust unless grounded independently.
- Keep the client's independent view separate from `panel_size` and `convergent_count`.

## Adjudication

Use hypothesis-specific decisions for each risk, counterargument, or assumption:

- `mitigated`
- `accepted_as_residual`
- `rebutted`
- `converted_to_experiment`
- `pending_owner`

Do not force hypothesis perspectives into defect-centric accepted or rejected findings. Use
`/audit-adjudication` when the user wants a consolidated decision table.

## Presentation And Downstream Routing

Present strengths before risks, then counterarguments, assumptions, epistemology gaps, and the
decision surface. Highlight high-impact risks and missing epistemology fields without treating the
panel as the final authority.

Recommend, but never silently invoke, the next operation:

- Use `/audit-market-research` to gather or verify external evidence identified as missing.
- Use `/audit-writing-plans` after conclusions are accepted and need implementation structure.
- Use `/audit` when a resulting artifact is ready for defect review.

Keep the response in the user's conversation language even though this skill package is written in
English.
