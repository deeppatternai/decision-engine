# Plan Authoring

Read this file when reviewed or Owner-confirmed conclusions need to become an implementation plan.
It owns input qualification, provenance, traceability, the primary document, and file placement.

## Qualify the Input

Classify the input before drafting:

| Status | Evidence | Planning behavior |
|---|---|---|
| `externally-reviewed` | A terminal, retrievable audit or research result | Extract candidate conclusions, but still determine which ones were accepted |
| `owner-confirmed` | The user explicitly approves a frozen set of conclusions | Draft from that set and disclose that there is no upstream convergence signal |
| `unverified` | Plain text with no retrievable review and no explicit approval | Ask the user to confirm the proposed input set or use ordinary host planning |

Accepted, reviewed, and trusted are different properties. A high-trust research conclusion may
still be rejected by the user. A user-approved conclusion may be planned without external upstream
convergence, but its provenance must remain `owner-confirmed`.

For retrievable upstream identifiers, verify terminal state and collect the result's accepted
decisions, open questions, convergence signal, and divergent items. If an identifier is expired or
unavailable, treat the supplied material as manual input rather than claiming verified provenance.

When an adjudication is present:

- `accepted` items may enter the frozen input set;
- `rejected` items must not become requirements or tasks;
- `needs-user-decision` items remain in Open Questions until the named decision is made;
- material divergence remains visible and cannot be smoothed into consensus.

When no adjudication exists, show the candidate input set to the user and obtain confirmation before
drafting. Record manual additions as `[author-added]`.

## Language and Profile

Detect the user's input language and record it as BCP-47. If language or locale is genuinely
ambiguous and changes the deliverable, ask rather than silently defaulting.

Choose the closest profile:

- `software`
- `ops`
- `research`
- `content`
- `ml`
- `mixed`

The profile adapts terminology, not the traceability or validation requirements. For example,
software may use Architecture while research may use Approach or Method.

## File Placement

Use the user-specified path when one is provided. Otherwise, in a repository, write:

```text
docs/plans/YYYY-MM-DD-<slug>-doc.md
```

Do not overwrite an existing file without confirmation. After writing, report the exact path to the
user. The primary plan is Markdown unless the user explicitly requests another format.

## Primary Frontmatter

Use English keys and stable values. At minimum record:

```yaml
---
status: draft
language: <BCP-47>
profile: software | ops | research | content | ml | mixed
upstream_status: externally-reviewed | owner-confirmed
upstream_refs: [<retrievable ids or manual references>]
upstream_convergence: <signal or unavailable>
---
```

Do not add `validated_by` until terminal external validation has been adjudicated. A document based
only on user confirmation must not imply upstream external review.

## Required Plan Content

Write headings and body in the user's language. Preserve these logical sections, adapting the
visible heading for the selected profile when needed:

1. **Context** - why the work exists and what evidence led here.
2. **Goals and Non-Goals** - stable `g<N>` identifiers and explicit exclusions.
3. **Upstream Conclusions** - the frozen accepted input set with `traces_to`.
4. **Architecture or Approach** - the proposed structure appropriate to the profile.
5. **Decisions** - stable `d<N>` identifiers, rationale, tradeoffs, and `traces_to`.
6. **Cross-Cutting Concerns** - security, privacy, observability, cost, dependencies, and
   reversibility. Use `N/A - <reason>` when a slot genuinely does not apply.
7. **Implementation** - ordered tasks with complete execution and verification fields.
8. **Risks** - material and tail risks, triggers, mitigations, and owners when known.
9. **Open Questions** - unresolved decisions and the person or evidence needed to close them.

Unknowns belong in Open Questions; do not hide them behind empty headings or placeholder prose.
Required implementation fields must be complete before external validation.

## Implementation Task Shape

Each task contains:

| Field | Meaning |
|---|---|
| `id` | Stable task identifier |
| `description` | Concrete work to perform |
| `inputs` | Required artifacts, decisions, or dependencies |
| `outputs` | Observable artifacts or state produced |
| `verification` | Command, check, inspection, or evidence that proves completion |
| `acceptance` | User-visible or system-visible completion condition |
| `dependencies` | Other task identifiers or external prerequisites |
| `traces_to` | Upstream conclusion, Goal, Decision, or `[author-added]` source |
| `risk_class` | Relative execution or change risk |
| `estimated_effort` | Practical effort estimate with uncertainty when needed |

Do not convert rejected findings into tasks. A task may resolve an open question, but it must say
that it is a validation task rather than presenting the unknown as settled truth.
