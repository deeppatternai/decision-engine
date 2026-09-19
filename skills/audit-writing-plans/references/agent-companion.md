# Agent Task Companion

Read this file only after the primary plan has been delivered and the user gives explicit
confirmation to derive an agent-oriented task document.

## Output

Write the companion beside the primary plan using the same slug:

```text
docs/plans/YYYY-MM-DD-<slug>-tasks.md
```

If the primary plan uses a user-specified directory, place the companion there unless the user asks
for another location. Do not overwrite an existing companion file without confirmation. Report the
exact path after writing. Markdown remains the default; if the user explicitly requests another
format, use an available document capability.

## Mechanical Extraction Only

The companion is a mechanical restructure of the validated primary plan's Implementation tasks. For
each task, preserve:

- `id`
- `description`
- `inputs`
- `outputs`
- `verification`
- `acceptance`
- `dependencies`
- `traces_to`
- `risk_class`
- `estimated_effort`

Reordering may follow the declared dependency graph. Formatting may become more execution-oriented,
but task meaning, scope, acceptance, and risk cannot change.

If extraction requires net-new content, a new decision, an unstated dependency, or a changed
acceptance condition, stop. Add the material to the primary plan, resolve its provenance, and repeat
validation when the change is substantive. Do not hide authorship by placing new work only in the
companion.

## Linkage and Status

Record the primary document path and its validation metadata under `linked_doc_validated_by`. Do not
copy unsupported trust signals or claim that the companion was independently validated when it was
only derived.

Before extraction, recompute the primary file's exact-byte SHA-256 and require it to equal
`submitted_document_sha256` in `validated_by`. A mismatch makes the source non-executable until the
changed primary plan is reviewed again.

Status inheritance is fail-closed:

| Primary state | Companion state |
|---|---|
| `accepted` and unchanged | May be marked executable |
| `review` | Must remain `review` and non-executable |
| `draft` | Must remain `draft` and non-executable |
| Validation missing, failed, cancelled, timed out, or uncertain | Must be non-executable |

Tell the user whether the companion is executable and why. The existence of a `-tasks.md` file is
not evidence that its source plan passed validation.
