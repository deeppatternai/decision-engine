---
name: audit-adjudication
description: "Combine 1-N prior audit results into a unified user-facing accept, reject, or needs-user-decision table. Supports findings, hypothesis, and mixed schemas from single or multi-auditor panels across sessions; the client decides each row while the hub validates the ledger and trust signals. Use for \"audit 结果整合\", \"把 audit 结果做 adjudication\", \"整合审计结果\", \"审计结果汇总\", \"决定接受哪些 findings\", \"decide on audit results\", \"synthesize prior audits\", \"adjudicate audit results\", \"merge audit findings\", or /audit-adjudication. This is the user-facing Decision Engine skill, not AQG internal build-session adjudication. Use /audit, /audit-brainstorming, /audit-market-research, /audit-writing-plans, or another audit-* producer to run a new audit."
---

# /audit-adjudication — Synthesize prior audits into unified accept/reject table

> **Thin client routing skill** for the Decision Engine hub server.
> Server-side validates the structured ledger schema + computes trust
> signals; reasoning (which finding accept/reject, what action) stays
> client-side.

## When to use

- Multi-审 panel returned and you want a clean accept/reject table for the user
- N audit rounds accumulated (round 1/2/3...) and you want cross-round synthesis
- Cross-session audit summary across 2-3 prior build sessions
- Mixed findings + hypothesis composite output (from `/audit-market-research`
  or `/audit-writing-plans`) — need one consolidated view
- Hand the table to a downstream agent (EAF) for execution
- Apply AQG validator fail-stop discipline to a user-facing decision artifact

## NOT to use

- **No audit results yet** — first run `/audit`, `/audit-market-research`,
  `/audit-writing-plans`, `/audit-brainstorming`, or `/audit-layer-check`
- **You want to RUN an audit** — use the audit skills above
- **Audit returned solid + 0 findings** — adjudication is trivial; skip

## Calling pattern (client-side flow)

```
1. PARSE       — accept audit_id refs, raw JSON paste, or reply-text paste
2. RETRIEVE    — for audit_ids, call `mcp__decision-engine__check_audit_status`
3. SCHEMA-DETECT — findings-schema vs hypothesis-schema vs mixed (composite)
4. CROSS-DEDUP — when N>1, find persistent rows across audits
5. CLASSIFY-AND-DECIDE — apply per-schema logic, write decisions
6. (CONDITIONAL) LAYER-CHECK — invoke /audit-layer-check if cross-product
7. SUBMIT      — call mcp__decision-engine__audit_skill_submit with the
                 structured rows; server validates schema + emits trust signals
8. RENDER      — render markdown from envelope.payload for user
```

MCP submission shape:

```
mcp__decision-engine__audit_skill_submit(
    skill_name="audit-adjudication",
    args={
        "title": "<short label>",                                # optional
        "source_audit_ids": ["audit_abc", "audit_def"],          # optional but recommended
        "rows": [
            {
                "finding": "<substantive claim>",
                "decision": "accepted" | "rejected" | "needs-user-decision",
                "action": "<fix or reason>",
                "verification": "<check command/result>",
                "convergence": "3/3" | "audit_a: 3/3 + audit_b: 2/3",  # optional
                "source": "audit_abc",                            # optional; preserves traceability
            },
            ...
        ],
        "hypothesis_perspectives": {                              # optional hypothesis-perspectives block
            "strengths": [...],
            "risks": [...],
            "counter_arguments": [...],
            "assumptions": [...],
            "epistemology": {...},
        },
        "notes": "<adjudicator notes>",                           # optional
    },
)  → returns envelope {run_id="adj-...", trust_signals, payload}
```

This skill is **synchronous** on the server: `audit_skill_submit` returns the
validated envelope immediately. `audit_skill_status` / `_result` / `_events`
raise WorkflowError ("synchronous — use submit() return value"); `_cancel`
returns an idempotent ack envelope (nothing to cancel).

## Trust signals (envelope.trust_signals)

| Signal | Type | Meaning |
|---|---|---|
| `canonical_sha` | hex64 | sha256 over payload; tamper-detection (always present) |
| `row_count` | int | total decision rows |
| `accepted_count` | int | rows with `decision: accepted` |
| `rejected_count` | int | rows with `decision: rejected` |
| `needs_user_decision_count` | int | rows with `decision: needs-user-decision` |
| `source_audit_count` | int | distinct audit_ids in the `source_audit_ids` manifest (per-row `source` cells are informational free-text, NOT counted) |
| `has_hypothesis_block` | bool | informational hypothesis-perspectives block present? |

A high `needs_user_decision_count` is a signal that the calling session
should pause and surface those decisions to the user rather than presenting
the table as fully resolved.

## Output rendering (client-side)

The server returns structured rows; the calling session renders the
markdown table from `envelope.payload.rows`. AQG validator-compatible 4-col
format (`finding` / `decision` / `action` / `verification`) plus optional
`convergence` + `source` cols.

```
## Adjudication table — <date> — sources: <audit_id list>

| finding | decision | action | verification | convergence | source |
|---|---|---|---|---|---|
| <claim> | accepted/rejected/needs-user-decision | <fix or reason> | <check> | X/N or 1/1 | <audit_id> |
```

Hypothesis-schema rows (per the hypothesis-schema convention) are
surfaced as an INFORMATIONAL block BELOW the table — NOT adjudicated per-row.

## Routing (chain pattern)

```
/audit (or any audit-* skill) → 1-N audit envelopes
                              → /audit-adjudication (this skill)
                              → unified ledger → user / downstream agent
```

## Pre-flight reminders

- Device token configured through the masked setup flow (`python3 -m installer.permanent_setup`)
- All `audit_id` references should be within the 7-day TTL window for
  retrieval via `check_audit_status`. If aged out, fall back to raw JSON
  paste or reply-text paste.
- `needs-user-decision` rows MUST name the specific decision needed and
  responsible actor (no vague "ask Owner")
- Server enforces: 4 required cols, decision enum, row count ≤ 500,
  source_audit_ids ≤ 50, notes ≤ 50 KB, total payload ≤ 2 MB
