# Hosted result contract

Read this file after a hosted audit reaches a terminal state.

A hosted result must not append any DE Lite, MCP-unavailable, service-unavailable, entitlement, or
unactivated template, and must not reuse a previous local run's final note. End with the hosted
server result in the current conversation language. 服务端正常审核不得追加 Lite 友好提示，也不得复用上一轮本地审核的最终说明。

## Trust signals

| Signal | Type | Meaning |
|---|---|---|
| `canonical_sha` | hex64 | SHA-256 over payload; always present for tamper detection |
| `artifact_intent` | string | `prescriptive` or `hypothesis` |
| `stakes` | string | `low`, `medium`, `high`, or `irreversible` |
| `mode` | string | resolved `fast`, `standard`, or `deep` mode |
| `tier` | string | resolved per-auditor `fast` or `reasoning` effort |
| `overall_verdict` | string | `solid`, `has-gaps`, `has-serious-issues`, or `fundamentally-flawed` |
| `findings_count` | integer | total panel findings |
| `blocking_findings_count` | integer | findings whose `blocking` flag is true |
| `dimension_blocking_count` | integer | dimensions with `blocking-issues` status |
| `panel_size` / `convergent_verdict` / `convergent_count` | mixed | multi-auditor panels only: auditor count, most-common verdict, and `X/N` convergence |

For panels of two or more auditors, treat a defect raised by only some auditors as a high-information
panel split or gap signal, not noise.

## Adjudication

Do not apply findings blindly. Send multi-auditor results and their `audit_id` through
`/audit-adjudication`, using the installed AQG adjudication discipline when available. Keep an
independent self-review separate from the panel count. A hosted `failed`, `cancelled`, or unobservable
run has no clean verdict.
