# Validation and Adjudication

Read this file only after the primary Markdown plan is complete. It owns authorization for external
processing, plan-quality validation, result handling, adjudication, and promotion status.

## Authorization Gate

The primary plan may contain repository details, internal decisions, or other non-public material.
Before transmitting the full document, disclose the external processing path, current metering when
known, and the bounded data categories involved. Obtain explicit authorization for this document.

Never send secrets, credentials, personal or regulated data, or restricted/confidential content
that the current authoritative policy does not permit. Redact safely or keep validation local. Prior
authorization for an upstream audit is not authorization to send a newly drafted plan.

If authorization is declined, deliver the primary Markdown plan with `status: draft` or
`status: review`, explain that external validation did not run, and omit `validated_by`.

## Submit the Draft

Immediately before submission, compute `submitted_document_sha256` over the exact UTF-8 bytes of
the primary Markdown file. Keep that local value with the pending run. It identifies the revision
the client submitted; unlike the result's `canonical_sha`, it is not a server attestation.

Use the exact callable names exposed by the host:

```text
SUBMIT_TOOL(
    skill_name="audit-writing-plans",
    args={
        "title": "<project> - plan quality validation",
        "content": "<full primary document>",
        "context": "Upstream references: <ids and available convergence>",
        "stakes": "medium",
        "mode": "standard"
    }
)
```

Choose `stakes` from the supported severity hints (`low`, `medium`, `high`, or `irreversible`) based
on impact. Choose `mode` from `fast`, `standard`, or `deep` according to current audit policy; mode,
not stakes, controls panel depth.

The server forces `artifact_intent="prescriptive"` and injects plan-quality focus plus pre-mortem
framing. Client values for those fields are not authoritative and should not be used to override the
server contract.

## Wait for the Terminal Result

Preserve the returned `run_id`. Poll with `audit_skill_status` at bounded backoff while remaining
interruptible. Use a total wait limit appropriate to the selected mode. Do not submit a duplicate
while the run is pending or running.

On terminal completion, fetch `audit_skill_result`. On failure or cancellation, surface the reason
and run identifier. If a previously observed run becomes unknown or a transport error makes state
uncertain, reconcile with `audit_skill_events` and the result endpoint, then stop without guessing if
the state remains unresolved. Use cancellation only when the user requests it and the host supports
it.

If the total wait limit is exhausted without a terminal result, report the run as unresolved, keep
the plan at `draft` or `review`, and omit `validated_by`. Do not infer failure or submit a duplicate.

## Author Self-Review

The panel independently reviews a plan authored by the client. Do not add the client's opinion to
the panel's `convergent_count`. While the panel runs, inspect the draft for missing dependencies,
unsafe sequencing, unverifiable acceptance criteria, irreversible steps, and optimistic assumptions.

## Adjudicate Every Finding

External review is evidence, not authority. Record a disposition for every returned finding ID:

| Decision | Meaning | Required action |
|---|---|---|
| `accepted` | Concrete, correct, and within scope | Revise the primary plan and verify the change |
| `rejected` | False, unsupported, out of scope, or lower value than its cost | Record a specific technical reason and evidence |
| `needs-user-decision` | Depends on product semantics, architecture direction, risk acceptance, or user preference | Name the decision and responsible user or Owner |

Do not silently accept a convergent blocking finding. Do not reject a finding merely because it is
inconvenient. Low-severity findings may share one grouped action only when every included finding ID
is listed. Preserve panel splits on key claims as high-information divergence.

## Outcome Routing

| Outcome | Plan state |
|---|---|
| Validation completed successfully, the current file matches `submitted_document_sha256`, every finding has a recorded disposition, and no blocking issue remains | Set `status: accepted` and record `validated_by` |
| Accepted finding requires a plan change | Revise, verify, and validate the new substantive revision |
| Key finding remains `needs-user-decision` | Keep `status: review` and ask the named decision |
| User knowingly accepts a non-blocking residual risk | Keep the risk explicit and record the decision; use `review` when it affects implementability |
| Audit failed, was cancelled, timed out, remains uncertain, or the current file digest differs | Do not claim validation; set or keep `status: draft` or `status: review` and omit `validated_by` |

An Owner decision can resolve product semantics or accept an explicitly described residual risk. It
cannot turn a failed audit into a successful audit or erase an unresolved technical contradiction.

## Trust Signals

Preserve the signals the server actually returns:

| Signal | Meaning |
|---|---|
| `canonical_sha` | Digest of the returned validation payload |
| `overall_verdict` | `solid`, `has-gaps`, `has-serious-issues`, or `fundamentally-flawed` |
| `findings_count` | Total findings returned |
| `blocking_findings_count` | Findings marked as blocking |
| `dimension_blocking_count` | Review dimensions with blocking issues |
| `pre_mortem_applied` | Confirms server-side pre-mortem framing was applied |
| `panel_size` | Number of panel positions represented by the result |
| `convergent_verdict` | The panel's convergence label when available |
| `convergent_count` | The panel agreement signal, usually expressed as X/N |

Convergence can increase confidence but does not establish truth. Divergence on a key claim is a
result to preserve, not noise to discard.

`canonical_sha` binds the server's result payload; it does not prove the Markdown file's content
hash. Do not label it as a document digest. Before promotion, recompute the current file's exact-byte
SHA-256 and compare it with `submitted_document_sha256`. Any substantive change to Goals, Decisions,
Implementation, Risks, or Open Questions after validation requires another validation before the
plan can remain accepted. A failed re-validation demotes the plan to `review` or `draft`.

## Validation Metadata and Presentation

After successful adjudication, add a compact `validated_by` block containing only supported facts,
such as the validation `run_id`, validation time, mode, stakes, local
`submitted_document_sha256`, result `canonical_sha`, convergence, pre-mortem status, and retained
divergent items. Do not invent model names, counts, or hashes.

Tell the user:

- the exact primary document path;
- whether the plan is `accepted`, `review`, or `draft`;
- the validation run identifier;
- blocking and divergent outcomes;
- any residual risk or unavailable trust signal.

Then ask whether to derive the optional agent task companion. Do not auto-derive it.
