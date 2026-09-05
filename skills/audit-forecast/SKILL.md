---
name: audit-forecast
description: "Aggregate how existing external forecasters and prediction markets currently estimate one specific, verifiable, time-bound outcome. Use for prediction-consensus questions such as “市场现在认为某事发生的概率是多少”, “预测平台怎么看”, “what do forecasters predict”, or /audit-forecast. Return consensus strength, divergence, and falsifiers from retrieval-grounded sources. Do not use for broad market sizing, general research, personal guesses, or non-verifiable futures."
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
  out-of-distribution future events). A fail-closed server OUTPUT GATE re-enforces this:
  every figure carries the verbatim source excerpt it came from; no market found ⇒ an
  honest `no_market` abstention with NO figures; consensus is DERIVED from the listed
  sources. A violation FAILS the run — it never reaches the user. **Present what the
  MARKET predicts, never a number you or the model invented.**
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
actually FIND the market. Omit `subject` and the run returns an honest `no_market` with no data:

| field | rule |
|---|---|
| `subject` | **REQUIRED for any data** — the specific outcome/entity the sources match on (e.g. `"Team A"`, `"Argentina"`, a ticker). Without it BOTH pooled sources fail closed → `no_market`. |
| `source_hints` | recommended per-source locator hints — a dict of per-source keys → `{"event_slug": "..."}` **or** `{"title_keyword": "..."}` that locates the market for a market-locator source (concrete source keys are hub-provided). Omit it and the market-locator source abstains (other sources still try via `subject`). |

## Calling pattern

```
mcp__decision-engine__audit_skill_submit(
    skill_name="audit-forecast",
    args={
        "mode": "premium",              # quick | deep | premium (default premium)
        "proposition": {
            "statement": "Team A wins the 2026 Cup final vs Team B",
            "proposition_type": "match_result",
            "event_id": "cup-2026-final-teamA-teamB",
            "horizon_utc": "2026-07-19T20:00:00Z",
            "outcome_set": ["Team A wins", "Team B wins", "draw"],
            "resolution_authority": "official competition result",
            "subject": "Team A",                       # what the sources match on — REQUIRED for real data
            "source_hints": {},                        # optional per-source locator hints (concrete keys are hub-provided)
        },
    },
)
# LLM/retrieval is async — POLL, never block (wait_audit is deprecated).
# IN-LOOP poll (status + per-auditor progress ONLY; carries NO forecast payload):
mcp__decision-engine__check_audit_status(run_id="<id>")   # → {status, auditors:[...], error}
#   backoff ~10s cadence; re-render per-auditor auditors[] each round; never infer failure
#   from elapsed time. Bound the loop: unknown/error status (run_not_found once the run was
#   already seen, or a transport error) → surface + STOP; keep a total timeout / stay interruptible.
# TERMINAL fetch (ONLY this carries the forecast payload — status tools never do):
mcp__decision-engine__audit_skill_result(skill_name="audit-forecast", run_id="<id>")
# (audit_skill_status is the skill-scoped status echo; audit_skill_events / _cancel also available;
#  artifact_intent is FORCED "hypothesis" server-side.)
```

## Voice / source-name privacy (apply client-side)

Every result carries `debug_authorized`. When **`false`** (normal users) the payload is
ALREADY redacted — refer to every pooled source (`provenance[].via`,
`provenance[].source_url`) ONLY as `Voice N` / `Source N` in EVERYTHING the user sees; NEVER
name a vendor/source. When **`true`** (operator) you may use real names. The forecast SUBJECT
can legitimately be a named entity ("will Argentina win…") — keep the subject; redaction only
strips source-identity tokens. Do not surface pricing / credit cost in user-facing prose.

## Trust signals (envelope.trust_signals)

| Signal | Meaning |
|---|---|
| `canonical_sha` | sha256 over payload (always present) |
| `artifact_intent` | forced `hypothesis` (a forecast is not a defect review) |
| `stakes` | forecast stakes classification |
| `proposition_type` | echoed proposition type |
| `free_sources_planned` | the free source roster the run queried |

## Anti-patterns

- ❌ Present a probability/number that isn't carried verbatim from a listed source (the server gate blocks it; never route around it)
- ❌ Reword a gate-rejected open-ended claim to force it through — a rejection is the honest answer
- ❌ Add a paid odds API / your own model's "estimate" — free-source retrieval-only by design
- ❌ Name a pooled source when `debug_authorized=false` — use `Voice N` / `Source N`
- ❌ Emphasize cost / "free" as a selling point in user-facing output

## Pre-flight

- Hub reachable (`mcp__decision-engine__check_provider_health`); device token configured
- Build the `proposition` BEFORE calling — include `subject` (+ `source_hints`) or the run returns `no_market`; the gate rejects an inadmissible one before any work
- High-stakes by nature → keep `mode="premium"` unless the user explicitly asks for a cheap/quick pass
