---
name: audit-forecast
description: "Use when the user asks what existing forecasters or prediction markets currently expect for one specific, time-bound outcome with an external resolution rule, including equivalent intent in any language. Aggregate retrieved estimates into a sourced consensus, explain disagreement and what could change the outlook, and abstain when matching sources are unavailable. Never generate a new probability or use this for broad market research or open-ended speculation."
---

# /audit-forecast — Prediction-consensus scanner (aggregate, never produce)

> **Thin client routing skill** for the Decision Engine hub server.
> You (the client) build the verifiable `proposition`, call the server, and present
> the aggregated read in the user's language. The SERVER holds ALL the IP: the
> proposition INPUT gate + the anti-self-production OUTPUT gate
> and the server-side aggregation logic — none of that lives here.
> This file is the CALLER CONTRACT only.

## ⚠️ Read FIRST

- **Anti-self-production is the whole point.** The server AGGREGATES existing market /
  model predictions; it must NEVER emit "the AI's own forecast" (LLMs are unreliable on
  out-of-distribution future events). A fail-closed server OUTPUT GATE ties each retrieved
  source figure to a verbatim source excerpt; consensus is DERIVED only from the listed
  source figures. If configured sources find no usable match, return an honest `no_market`
  abstention with NO figures. A violation FAILS the run — it never reaches the user.
  **Present what the sources predict, never a number you or the model invented.**
- **Retrieval-only, FREE sources.** The server pools keyless free sources — **real-money
  prediction markets** + **model/analyst picks**. There is NO paid odds API and
  NO LLM panel producing numbers. (More sources land as the Owner provisions them.)
- **Proposition gate (GIGO defense).** The server HARD-rejects an inadmissible proposition
  BEFORE any work with a client-facing error. That rejection IS the answer — surface it as
  a clean "not a forecastable proposition" and do NOT retry with a reworded open-ended claim.

## When to use vs NOT

| Use /audit-forecast | Use instead |
|---|---|
| Determinate outcome + deadline + external resolver → aggregate existing market/model predictions | Open-ended commercial / GTM / outlook question, no resolver → `/audit-market-research` |
| Match / election / earnings beat-miss / price threshold / launch success | Stress-test the user's OWN already-formed idea → `/audit-brainstorming` |
| The user wants OTHERS' odds/markets pooled, not a new opinion | Vague idea not yet a hypothesis → `/audit-explore` |

Dedup: same proposition scanned within 5 turns + no material change → re-render, don't re-run.

## The `proposition` contract (you build this; the server hard-gates it)

An admissible proposition is a dict requiring **ALL** of:

Build it from user-provided or verified event details. If the specific event, future deadline,
outcomes, or external resolver cannot be established, ask for the missing information; do not
invent missing event details. For a current forecast, choose a future UTC resolution deadline
from those details; this is client preparation, not an additional server gate claim.

**Admissibility fields** — the server INPUT gate HARD-rejects the proposition unless ALL are present:

| field | rule |
|---|---|
| `statement` | non-empty string — the determinate, moderated claim (this text is what the gate inspects) |
| `proposition_type` | one of `match_result` / `score_line` / `election` / `earnings_beat_miss` / `price_threshold` / `launch_success` / `other` |
| `event_id` | non-empty string binding the specific event instance (prevents cross-edition mixups) |
| `horizon_utc` | ISO-8601 timestamp — the resolution deadline (a forecast is time-limited) |
| `outcome_set` | list of **≥2** mutually-exclusive outcomes (determinate → falsifiable) |
| `resolution_authority` **or** `resolution_url` | at least one — who/what externally resolves it |

Missing/invalid any of these → the server returns a `forecast proposition inadmissible: ...`
error naming the offending field. Fix and resubmit only if the proposition is genuinely
determinate; if it is inherently open-ended, tell the user it is not forecastable.

**Retrieval fields** — the input gate does NOT check these, but the free sources need them to
actually FIND a matching prediction. Treat `subject` as a client pre-flight requirement rather
than knowingly submitting a request that will return `no_market`:

| field | rule |
|---|---|
| `subject` | **Client-required for any data**, though not hard-gated server-side — the specific outcome/entity the sources match on (e.g. `"Team A"`, `"Argentina"`, a ticker). Identify it from verified event details or ask the user. Without it the currently described pooled sources return no usable match (`no_market`). |
| `source_hints` | Optional per-source locator hints — a dict of hub-provided source keys → `{"event_slug": "..."}` **or** `{"title_keyword": "..."}`. Supply only known locator values; never guess source-specific slugs. Without a hint the market-locator source abstains (other sources still try via `subject`). |

## Calling pattern

All tool names in this section are illustrative; bind the exact names exposed by the current host.
The example placeholders must be replaced with verified event details before submission.

```
mcp__decision-engine__audit_skill_submit(
    skill_name="audit-forecast",
    args={
        "mode": "premium",              # quick | deep | premium (default premium)
        "proposition": {
            "statement": "Team A wins the named final against Team B",
            "proposition_type": "match_result",
            "event_id": "<specific event instance ID>",
            "horizon_utc": "<future ISO-8601 UTC resolution deadline>",
            "outcome_set": ["Team A wins", "Team B wins", "draw"],
            "resolution_authority": "official competition result",
            "subject": "Team A",                       # what the sources match on — REQUIRED for real data
            "source_hints": {},                        # add only verified, hub-supported locator hints
        },
    },
)  # → {run_id, status="queued", ...}
```

The retrieval workflow is asynchronous. If submission returns an unknown outcome, do not retry
blindly: reconcile through a returned `run_id` or a host-provided request/idempotency ID when
available; otherwise surface the unknown submission outcome and stop. After a known submission,
preserve the `run_id`; do not start another run for the same proposition while this one may still
be active. Observe with the host-exposed `audit_skill_status` or `check_audit_status` at
bounded backoff (normally about ten seconds), and show per-auditor progress when the status
includes it. If the host exposes
`wait_audit`, bounded wait chunks are also valid; do not assume it is either universally
available or deprecated. Keep a total observation checkpoint appropriate to the selected mode;
if status remains unknown or errors persist after bounded retries and reconciliation, preserve
the `run_id`, report the uncertainty, and stop the current polling loop. Stay interruptible and
do not infer run failure from elapsed time alone.

On `completed`, fetch `audit_skill_result(skill_name="audit-forecast", run_id=...)`;
status responses carry progress, not the forecast payload. On `failed` or `cancelled`, surface
the reason and stop. For a transient observation error or a previously seen `run_id` that
becomes unknown, retry briefly and reconcile with available events and result tools. If state
remains uncertain, preserve the `run_id` and report that uncertainty; never guess a result or
submit a duplicate run. The server forces `artifact_intent="hypothesis"` for this workflow.
Use `audit_skill_events` for reconciliation when available; request `audit_skill_cancel` only
when the user asks to cancel and the host supports it.

## Presenting the result

For a completed run, present only returned source figures and server-derived consensus.
Explain agreement, divergence, and falsifiers or catalysts when the result contains them;
do not invent a missing figure, source, or explanation. `no_market` means the configured
sources found no usable match for this proposition, not that no forecast exists anywhere.
Show the abstention without a probability or a fabricated zero. A failed or cancelled run
has no usable forecast.

## Voice / source-name privacy (apply client-side)

Every result normally carries `debug_authorized`. Unless `debug_authorized` is explicitly `true`,
including when it is missing or malformed, treat the caller as unauthorized: refer to every pooled
source (`provenance[].via`, `provenance[].source_url`) ONLY as `Voice N` / `Source N` in EVERYTHING
the user sees; NEVER name a vendor/source or echo raw identity tokens even if the payload contains
them. When **`true`** (operator) you may use real names. The forecast SUBJECT
can legitimately be a named entity ("will Argentina win…") — keep the subject; redaction only
strips source-identity tokens. Do not surface pricing / credit cost in user-facing prose.

## Trust signals (envelope.trust_signals)

| Signal | Meaning |
|---|---|
| `canonical_sha` | sha256 over payload (always present); does not prove the forecast is correct |
| `artifact_intent` | forced `hypothesis` (a forecast is not a defect review) |
| `stakes` | forecast stakes classification |
| `proposition_type` | echoed proposition type |
| `free_sources_planned` | the free source roster planned for querying, not proof that each yielded data |

## Anti-patterns

- ❌ Present an unsupported probability/number that cannot be traced to listed source figures or their server-derived consensus; the server gate blocks it, so never route around that gate
- ❌ Reword a gate-rejected open-ended claim to force it through — a rejection is the honest answer
- ❌ Add a paid odds API / your own model's "estimate" — free-source retrieval-only by design
- ❌ Name a pooled source unless `debug_authorized` is explicitly `true` — use `Voice N` / `Source N`
- ❌ Emphasize cost / "free" as a selling point in user-facing output

## Pre-flight

- Device token configured
- Build the `proposition` BEFORE calling — include `subject`, plus verified `source_hints` if available; the gate rejects an inadmissible one before any work
- `mode="premium"` is the current default. Use a quick/cheap pass only when the user explicitly asks and the host supports it; do not guess an unsupported mode or assume every forecast is equally high-stakes
