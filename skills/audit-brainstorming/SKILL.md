---
name: audit-brainstorming
description: "Stress-test an idea, hypothesis, strategy, or proposal with the external thinking-partner panel (hypothesis-class analysis, not defect-finding). Surfaces strengths / risks / counter_arguments / assumptions + epistemology fields (falsifiability / null_hypothesis / base_rate / confidence_update) instead of defect findings. Use when an artifact is a HYPOTHESIS not yet a deliverable. Triggers on \"brainstorm 一下\", \"stress test\", \"thinking partner\", \"压力测试这个想法\", \"审一下这个 idea\", \"我想做 X 可行吗\", \"这个策略风险在哪\", \"/audit-brainstorming\", or when reviewing a strategy doc / product hypothesis / research proposal / pre-commit idea rather than a code PR or migration."
---

# /audit-brainstorming — Hypothesis-class panel (thinking partner)

> **Thin client routing skill** for the Decision Engine hub server.
> Orchestrates the SAME panel as /audit but with artifact_intent="hypothesis"
> forced → the server selects the thinking-partner prompt (server-side); the
> prompt body never reaches the client.

## When to use vs /audit

| Use `/audit` (prescriptive) | Use `/audit-brainstorming` (hypothesis) |
|---|---|
| Code diff / PR / migration / spec | "Should we build X?" / strategy proposal |
| Anything that SHIPS as-is if accepted | Anything still ITERATING in idea-space |
| "What's wrong with this?" | "What should I worry about / what perspectives haven't I taken?" |

Unsure? Ask the user. "Find defects" → `/audit`. "Stress-test the thinking"
→ `/audit-brainstorming`.

**Input still vague / not a formed hypothesis?** Don't stress-test a non-hypothesis
— recommend `/audit-explore` first to form it (catch this BEFORE spending the panel),
then return. Recommend + gate on user confirm; don't auto-switch.

## Calling pattern

```
mcp__decision-engine__audit_skill_submit(
    skill_name="audit-brainstorming",
    args={
        "title": "<short label>",
        "content": "<idea / hypothesis / strategy / proposal text>",
        "context": "<stage? alternatives considered? decision context?>"
                   + RECOMMENDED_FRAMING,   # Steelman + Pre-mortem, see below
        "stakes": "high",      # high → epistemology rigor required
        "mode": "fast" | "standard" | "deep",   # default standard; deep for irreversible
        "domain": "strategy",
        # NOTE: artifact_intent is FORCED to "hypothesis" server-side —
        # any value you pass is overridden.
    },
)  → returns envelope {run_id, trust_signals, payload}

audit_skill_status / _result / _events / _cancel — same as /audit
(LLM-driven; real panel runs 30-60s). Wait by POLLING (wait_audit is deprecated):
loop check_audit_status(run_id) with backoff (~10s cadence), re-render per-auditor
auditors[] each round; terminal status → audit_skill_result. Unknown/error status
(run_not_found after the run was seen, or a transport error) → surface + STOP; keep a
total timeout / stay interruptible. Full loop + fallback nuance: see /audit "Waiting for the panel".
```

### Recommended context framing (Steelman + Pre-mortem) — default ON

Append to `context` (client-side injection; does NOT change server prompt):

```text
Apply Steelman + Pre-mortem framing:
1. Steelman the strongest counter-argument first — construct it as if
   defending it would win. Then evaluate.
2. Pre-mortem: assume this idea failed 12 months from now. What's the
   most plausible cause? Trace backward.
3. Preserve strengths-first ordering; do not relabel risks as strengths.
```

Skip only if (a) artifact already has equivalent framing, or (b) goal is
intentionally positive-biased. Note the skip to the user.

## You're a voice too — self-brainstorm during the wait

You (the calling agent) are never in your own external panel; it excludes your
model family to stay independent of you. But you hold
the fullest context. So **dispatch, then brainstorm the hypothesis yourself
while the panel runs** (~30-60s; same Steelman + Pre-mortem framing above) —
forming your view during the wait keeps it un-anchored. On return, merge it as
a CO-EQUAL voice (you + the N auditors), not the final say.

## Output shape (hypothesis-class, NOT findings)

`overall_assessment` ∈ {compelling, promising_with_concerns, speculative, weak}
+ `strengths[]` + `risks[]` (category/likelihood/impact/mitigation) +
`counter_arguments[]` (from_perspective) + `assumptions[]` (evidence_for vs
evidence_against) + `open_questions[]` + 4 epistemology fields
(`falsifiability_criteria` / `null_hypothesis_or_default` /
`base_rate_or_reference_class` / `confidence_update_needed`) + 5
decision-surface fields (`reversibility` enum / `blast_radius` /
`second_order_effects[]` / `key_person_dependencies[]` /
`recommended_evidence_order[]` — the "act-on-it" dimensions: `irreversible` +
wide `blast_radius` should raise the rigor bar before committing).

NO findings / dimension_status / blocking / overall_verdict.

## Trust signals (envelope.trust_signals)

| Signal | Meaning |
|---|---|
| `canonical_sha` | sha256 over payload (always present) |
| `overall_assessment` | compelling / promising_with_concerns / speculative / weak |
| `strengths_count` / `risk_count` / `counter_argument_count` / `assumption_count` / `open_question_count` | list sizes |
| `high_impact_risk_count` | risks with impact=high (engage these first) |
| `epistemology_fields_filled` | "X/4" — all-4-null on high stakes = the author hasn't grounded the hypothesis (itself a signal) |
| `has_epistemology_fields` | bool — true if ≥1 of the 4 epistemology fields is filled |
| `panel_size` / `convergent_assessment` / `convergent_count` | multi-審 only |

## Convergence-quality discipline (READ before integrating panel output)

These are the meta-disciplines that prevent false confidence — apply
client-side:

1. **Convergence is high-CONFIDENCE, NOT truth.** `convergent_assessment`
   means the panel agrees on a KNOWN signal. It does NOT mean the
   hypothesis is correct. Three LLMs sharing RLHF training can converge on
   a shared blind spot. Treat convergence as "worth attention", not "settled".
2. **Mandatory final round.** Before concluding, ask the panel (or yourself):
   (a) what evidence would FALSIFY this? (b) what perspective is NOT
   represented in the panel? (c) what's the base rate / reference class?
   The `epistemology_fields_filled` signal tracks whether (a)-(c) got answered.
3. **LLM/AI self-interest disclosure.** When the hypothesis is about the
   LLM/AI commercial ecosystem (e.g. "should we build an AI agent product"),
   the panel has shared bias — convergence there is LOW trust by default.
   Flag it explicitly to the user.

**Divergence on key claims = high-INFORMATION** (per 2025 LLM
stress-testing research: high-disagreement → 5-13× higher spec-violation rates).
Surface panel splits as "gap signal — needs attention", not noise.

## Adjudication semantics (hypothesis ≠ defect)

Per risk / counter-argument / assumption: `mitigated` / `accepted_as_residual`
/ `rebutted` / `converted_to_experiment` / `pending_owner`. Do NOT shoehorn
into defect-centric accepted/rejected.

## Routing (chain pattern)

```
vague idea → /audit-explore (hypothesis formation)
          → /audit-brainstorming (this skill — panel stress-test)
          → compelling? → /audit-writing-plans (operationalize) OR /audit (if becoming code)
          → /audit-adjudication (decision table)
```

Post-panel transition is RECOMMENDATION not enforcement — gate on user
confirmation; the panel is one input not a verdict (no 敷衍附和 / sycophancy).

## Pre-flight reminders

- Hub reachable (`mcp__decision-engine__check_provider_health`)
- Device token configured
- For high-stakes irreversible ideas use mode="deep" (full panel)
- Dedup: same hypothesis audited within 5 turns + unchanged → refer prior run_id
