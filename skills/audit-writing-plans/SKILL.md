---
name: audit-writing-plans
description: "Turn settled conclusions that were externally reviewed or explicitly confirmed by the Owner into a traceable implementation plan, then validate it with an external cross-vendor panel before marking it implementable. Use for accepted findings, adjudicated decisions, evidence-backed conclusions, or an Owner-confirmed frozen decision set that must become execution documentation. Produce a human-readable primary plan in the user's language and, only on explicit request, a mechanically derived agent task companion. Do not use when no settled reviewed or Owner-confirmed input set exists, to review an existing artifact for defects, or to explore an unsettled idea."
---

# Writing Plans From Reviewed Conclusions

Transform settled conclusions into a traceable primary implementation plan, validate the draft,
and optionally derive an agent task companion. The client authors and adjudicates the plan. The
Decision Engine server supplies independent plan-quality review with pre-mortem framing.

## Route the Request

| Primary state and intent | Route |
|---|---|
| Reviewed or Owner-confirmed conclusions need implementation structure | Use this skill |
| Unreviewed requirements need an ordinary plan | Use ordinary host planning |
| The input is still disputed or exploratory | Resolve the decisions before authoring |
| An existing artifact needs defect review rather than production | Use the host's review capability |

An upstream audit proves that material was reviewed; it does not prove that every conclusion was
accepted. Freeze the accepted input set with the user before drafting unless a validated
adjudication already supplies that decision state.

## Workflow

1. **Ingest:** classify provenance, language, profile, accepted conclusions, rejected conclusions,
   divergent items, and open decisions.
2. **Draft:** write the human-readable primary Markdown plan. Use a user-specified path when given;
   otherwise default to `docs/plans/YYYY-MM-DD-<slug>-doc.md` in the current repository.
3. **Authorize and validate:** before sending the full draft externally, obtain explicit
   authorization, submit one plan-quality validation run, and wait for a terminal result.
4. **Adjudicate and present:** decide each finding, revise when needed, report validation honestly,
   and tell the user the primary document path.
5. **Offer the companion:** ask whether to derive an agent task document. Do not create it without
   explicit confirmation.

## Progressive References

- At activation, read [references/plan-authoring.md](references/plan-authoring.md). It owns input
  qualification, provenance, traceability, the primary-plan schema, and file placement.
- After a complete primary draft exists, read
  [references/validation-and-adjudication.md](references/validation-and-adjudication.md). It owns
  external authorization, submission, waiting, trust signals, adjudication, and validation status.
- Only after the user explicitly requests an agent task companion, read
  [references/agent-companion.md](references/agent-companion.md). It owns mechanical extraction,
  status inheritance, and executable-state rules.

## Always-On Invariants

- Preserve the user's language for headings and prose. Keep machine-readable frontmatter keys and
  identifiers in English.
- Distinguish `externally-reviewed`, `owner-confirmed`, and `unverified` input. Never invent an
  audit identifier, convergence signal, acceptance decision, or validation state.
- Only accepted or Owner-confirmed conclusions become fixed plan inputs. Keep unresolved matters in
  Open Questions or as explicit validation tasks.
- Every Goal, Decision, and implementation task traces to an upstream item or is marked
  `[author-added]`.
- External review is evidence, not authority. Adjudicate every material finding before promotion.
- A failed, cancelled, timed-out, or unresolved validation never produces an accepted plan.
- Markdown is the default output. If the user explicitly requests another format, use an available
  document capability without inventing unsupported local styling or tooling.
- Do not overwrite an existing plan without confirmation.
- Same upstream set and intent within five turns with no material change: re-render or point to the
  existing document; do not create another metered validation run.
- Do not auto-chain to another workflow.
