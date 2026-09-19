# Analysis, Synthetic Customers, and Synthesis

Read this file only after a Deep or Premium P2 result is available. It owns P3 Multi-Lens Analysis,
conditional P4 Synthetic Customer, P5 Synthesis, trust interpretation, and final presentation.

The server uses a fixed full market-research roster for P3 and a fixed synthesis profile for P5.
Caller identity may be recorded as telemetry, but it does not filter the current P3 roster or change
the current P5 primary and backup order. Do not promise runtime-family exclusion that the server does
not enforce.

## P3 Multi-Lens Analysis

Submit one request with the approved scope and P2 pointers:

```text
SUBMIT_TOOL(
    skill_name="audit-market-research",
    args={
        "phase": "multi_lens",
        "mode": "deep" | "premium",
        "title": "<research title>",
        "research_type": "<same scope value>",
        "research_method": "<same scope value>",
        "language": "<same BCP-47 value>",
        "question": "<same question>",
        "topic": "<same topic>",
        "framework_hints": "<same scope value>",
        "upstream_p2_run_id": "<P2 run_id>",
        "upstream_p2_artifact_sha": "<P2 artifact_sha>",
        "stakes": "<current supported value>",
        "contrarian": false,
        "vendor_self_interest_topic": false
    }
)
```

Do not provide downstream `content`. The server fetches and verifies the P2 artifact by pointer,
checking terminal state, phase, device, scope, mode, and digest.

Do not pass frameworks or methodology lenses to P3. `framework_hints` remains unchanged scope
metadata; it is not permission to inject a panel method. The panel self-declares its frameworks, and
the server computes `framework_diversity_check` afterward.

Set high stakes or `contrarian=true` only when the user scope warrants it. Treat
`trust_signals.contrarian_requested` as a request signal, not proof that a dedicated contrarian voice ran.
Describe execution as unconfirmed unless a separate execution signal confirms it. Set
`vendor_self_interest_topic=true` when the topic creates model-provider commercial self-interest.

### P3 response decision table

Apply this gate only to the response for this run's submitted `skill_name=audit-market-research`,
`phase=multi_lens`, and matching P2 pointer. A conflicting `artifact_intent` in the envelope is a
STOP, even when analysis fields look plausible; never infer intent from field names alone. In the
returned P3 payload, exactly one of `audits` or `per_vendor_analysis` must be present as a nonempty
array. Every member must be an object. A missing container, both containers, an empty or non-array
container, or a non-object member is a panel failure, not an empty successful panel.

For each member, top-level defect-audit markers `overall_verdict`, `findings`, or `dimension_status`
take precedence regardless of their values; `blocking` belongs to `findings[i].blocking`, not the
top level. Generation-shaped analysis needs nonempty `insights[]` containing at least one nonempty
analysis object. Its `risks`, `assumptions`, and alternative framings are optional context, not
required shape keys. Hypothesis-shaped analysis needs a nonblank `overall_assessment` and at least
one nonempty array among `strengths[]`, `risks[]`, `counter_arguments[]`, or `assumptions[]`, containing
at least one nonempty analysis object. Field presence or an empty list alone never qualifies. If
both `insights` and `overall_assessment` appear in one member, both shapes must satisfy their rules;
a malformed present shape is not rescued by a valid other shape. Members may use different valid
shapes within the same panel.

| Case | Required observation | Decision |
|---|---|---|
| missing, dual, empty, or non-array container | Not exactly one nonempty analysis array | STOP |
| non-object member | Any member is not an object | STOP |
| defect-audit markers in any member | Any top-level marker above, even alongside valid analysis | STOP |
| blank or empty generation analysis | Present `insights` is empty or lacks a substantive object | STOP |
| blank or empty hypothesis analysis | Present `overall_assessment` is blank or all named analysis arrays empty | STOP |
| unknown member shape | Neither complete supported shape | STOP |
| valid generation member | Substantive insights, no defect markers | continue |
| valid hypothesis member | Assessment and substantive named analysis, no defect markers | continue |
| mixed valid generation and hypothesis members | Every member passes one supported shape independently | continue |

STOP if any member fails. Only after every member passes and the P3 artifact hash and dependency
chain verify may the client continue to P4/P5 using verified P3 pointers. Never replace a stopped
P3 with caller-solo synthesis. These are caller checks, not a claim that the server enforces the
same shape requirements.

Retain the P3 `run_id`, `artifact_sha`, `scope_sha`, framework diversity, convergence signals, and
all degraded reasons.

## P4 Synthetic Customer

P4 is conditional and optional. Submit the same scope plus both verified upstream artifacts:

```text
SUBMIT_TOOL(
    skill_name="audit-market-research",
    args={
        "phase": "synthetic_customer",
        "mode": "deep" | "premium",
        "upstream_p2_run_id": "<P2 run_id>",
        "upstream_p2_artifact_sha": "<P2 artifact_sha>",
        "upstream_p3_run_id": "<P3 run_id>",
        "upstream_p3_artifact_sha": "<P3 artifact_sha>",
        "calibration_present": true | false,
        "calibration_ref": "<authoritative calibration reference when present>",
        "<scope fields>": "<same values>"
    }
)
```

The server verifies that P3 consumed the same P2 submitted here.

- Deep uses the deterministic mock persona path and must return
  `degraded_mode=mock_synthetic_customer` when that path runs.
- Premium uses the real fixed-provider persona panel for GO/PIVOT/KILL evaluation. If the provider
  or parsing fails, the server falls back to the mock and must expose the degradation.
- Caveat domains without calibration may skip with `p4_skipped_reason=regulated_no_calibration`.
- `p4_skipped_reason=mode_skip` is valid only when this run's mode actually omits P4. Quick has no
  P4 response because it stops after P2. Deep may conditionally run deterministic mock P4, so Deep
  alone does not justify `mode_skip`.
- `not_consumer_purchase` is not a valid skip reason. Institutional, B2B, capital-flow, supply-chain,
  or regulatory topics should be reframed to appropriate institutional personas. Surface that skip
  value as a server contract defect.

Retain the P4 pointers only when P4 ran or returned a valid skip artifact.

## P5 Synthesis

Submit the same scope, the verified P2 and P3 pointers, and P4 pointers when available:

```text
SUBMIT_TOOL(
    skill_name="audit-market-research",
    args={
        "phase": "synthesize",
        "mode": "deep" | "premium",
        "upstream_p2_run_id": "<P2 run_id>",
        "upstream_p2_artifact_sha": "<P2 artifact_sha>",
        "upstream_p3_run_id": "<P3 run_id>",
        "upstream_p3_artifact_sha": "<P3 artifact_sha>",
        "upstream_p4_run_id": "<P4 run_id, optional>",
        "upstream_p4_artifact_sha": "<P4 artifact_sha, optional>",
        "<scope fields>": "<same values>"
    }
)
```

The server fetches and verifies every dependency, including the transitive P2-to-P3 relationship.
P5 uses a synthesis panel and deterministic enforcement. The final machine-readable report is in
`payload.synthesis`.

Verify that the returned synthesizer is the server-designated non-caller synthesis model. If the P3
shape is wrong or synthesis-of-record is absent, stop and surface the failure; do not silently write
a caller-only replacement.

## Trust Signals

Preserve and explain the signals the server actually returns:

| Signal | Meaning |
|---|---|
| `artifact_sha` | Binding digest for the current phase artifact |
| `scope_sha` | Digest binding every phase to one approved scope |
| `consumed_input_run_ids` | Verified upstream run chain |
| `degraded_mode` / `degraded_reasons[]` | Every known quality reduction, without cosmetic suppression |
| `sources_failed[]` | Source failures that reduce retrieval coverage |
| `framework_diversity_check` | HIGH, MEDIUM, or LOW diversity of declared analytical frameworks |
| `convergent_*` | Returned convergence signals; agreement is not proof |
| `p4_skipped_reason` | Why synthetic-customer evaluation did not run |
| `final_round_quality` | Server assessment of the mandatory final-round quality |

Common degradation labels include `mock_ground_truth`, `ground_truth_partial`,
`mock_synthetic_customer`, `panel_failure`, `cost_cap_hit`, `p5_enforcement_partial`, and
`language_compliance_partial`. Treat the returned list as authoritative; the examples are not an
exhaustive client-side enum.

Each `payload.synthesis.insights[]` item may include a server-computed `trust_tier` of HIGH, MEDIUM,
LOW, or VERY_LOW. It may also include `q_u_failed`, `quantified_threshold_missing`, and
`tension_analysis_missing`. The client cannot override these values.

On Premium, any non-empty `sources_failed[]` means the result no longer achieved full Premium
quality. State the impact prominently without naming providers or reporting raw voice counts.

## Presentation Discipline

Render the final report in `scope.language`. Explain specialized jargon once and do not paste raw
schema as prose.

Open with a short quality preamble containing only available signals:

- retrieval status, source coverage, and coverage gaps;
- framework diversity and its independence impact;
- final-round quality and anchored-fact status when present;
- the per-conclusion trust distribution and an explicit note when it skews low.

Do not narrate the entire internal pipeline in the preamble. Do not expose provider names or raw
voice counts. A missing voice is already reflected through returned diversity, source-failure, and
per-insight trust signals.

Always show the mandatory final-round answers about falsification, missing perspectives, and base
rates, even when convergence is high. Preserve high-information disagreements rather than smoothing
them into consensus.

After delivery, ask whether the user wants a DOCX report. Load `report-generation.md` only after an
affirmative answer.

Recommend any downstream route as a question. Do not auto-chain. Strong, actionable conclusions may
be offered for implementation documentation; unresolved evidence or medium-trust reasoning may be
offered for hypothesis review. The panel remains one input, not a verdict.
