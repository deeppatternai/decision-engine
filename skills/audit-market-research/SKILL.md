---
name: audit-market-research
description: "Research a market, competitor set, customer segment, commercial opportunity, or go-to-market (GTM) question by retrieving and citing external evidence and evaluating it from multiple independent perspectives. Use when the user needs market sizing (TAM/SAM/SOM), competitor or customer insights, trend analysis, go-to-market guidance, or commercial validation of a formed hypothesis. Produce cited findings, risks, uncertainties, and decision implications. Do not use for vague idea development, deliverable defect review, implementation planning, or forecast consensus about a specific time-bound outcome."
---

# Market Research

Produce an evidence-backed commercial insight report. The client owns consent, scope, presentation,
and user decisions. The Decision Engine server owns hosted retrieval, panel analysis, artifact
binding, and synthesis enforcement.

## Route the Request

| Primary intent | Route |
|---|---|
| Research a market, competitors, customers, sizing, trends, or GTM | Use this skill |
| Develop a vague idea into a testable hypothesis | Use `audit-explore` |
| Challenge the reasoning of an already formed hypothesis | Use `audit-brainstorming` |
| Aggregate forecasts for one verifiable, time-bound outcome | Use `audit-forecast` |
| Review an existing deliverable for defects | Use `audit` |
| Turn accepted conclusions into implementation documentation | Use `audit-writing-plans` |

Do not trigger on an isolated word such as `market`, `trend`, or `competitor`. Look for a research
intent that calls for external evidence or commercial analysis. An explicit invocation remains in
scope, but the description does not rely on it for semantic routing.

## Select the Data Path and Tier

| Path | Current behavior |
|---|---|
| Regulated | All work remains client-side as a clearly labeled generic degraded brief |
| Quick | Quick uses mock ground truth and stops after P2; it is infrastructure output, not validated insight |
| Deep | Deep uses real server-side retrieval, P3 analysis, conditional deterministic-mock P4, and P5 synthesis |
| Premium | Premium uses expanded real retrieval, P3 analysis, conditional real P4 personas, and P5 synthesis |

Never claim a higher tier than the returned trust signals support. Partial source or panel failures
remain visible as degraded reasons.

## Phase Map

1. **P0 Consent (client):** classify data, disclose the external path and current metering, and get
   explicit authorization. All regulated content remains client-only.
2. **P1 Scope (client):** establish the BCP-47 language and one stable scope envelope.
3. **P2 Ground Truth (server):** retrieve or mock the fact pack according to the selected tier.
4. **P3 Multi-Lens Analysis (server, Deep/Premium):** analyze the server-owned P2 artifact without
   client-supplied frameworks.
5. **P4 Synthetic Customer (server, conditional):** use a deterministic mock in Deep or a real
   fixed-provider persona panel in Premium; otherwise return a valid skip reason.
6. **P5 Synthesis (server, Deep/Premium):** synthesize the verified upstream artifacts and apply
   deterministic trust enforcement.
7. **Present and ask (client):** render in the user's language, preserve uncertainty, ask before any
   downstream handoff, and offer a DOCX report.

## Progressive References

- At activation, read [references/consent-and-scope.md](references/consent-and-scope.md). It owns P0,
  P1, tier selection, and the complete regulated client-only path.
- After an authorized non-regulated scope is approved, read
  [references/ground-truth.md](references/ground-truth.md) for P2, retrieval coverage, and lifecycle
  handling. Quick stops there.
- Only for a Deep or Premium continuation, read
  [references/analysis-and-synthesis.md](references/analysis-and-synthesis.md) for P3-P5, trust
  signals, presentation, and downstream routing.
- Only after the user requests a DOCX, read
  [references/report-generation.md](references/report-generation.md). Do not load report styling
  during research.

## Always-On Invariants

- Skill activation is not consent to transmit data. No server call occurs before P0 authorization.
- Pass the same P1 scope fields on every server submission so the server can bind one `scope_sha`.
- Pass server artifacts between phases only by verified `run_id` and `artifact_sha` pointers. Never
  paste a server-owned fact pack or analysis back as downstream `content`.
- Do not pass frameworks or methodology lenses to P3. The server assesses declared diversity after
  independent analysis.
- Do not override `artifact_sha`, `scope_sha`, `trust_tier`, degraded reasons, or exit-quality fields.
- Present citations, falsifiers, missing perspectives, base rates, and material disagreements.
- Do not expose provider names or raw voice counts in a normal report. Present derived quality
  signals and their impact instead.
- Same question within five turns with no material scope change: re-render; do not re-run.
- Do not auto-chain. The user chooses whether to continue to another skill.
