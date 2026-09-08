---
name: audit-explore
description: "Develop a vague, unformed idea into a falsifiable hypothesis through a cross-vendor LLM panel, Double-Diamond framing, and a Klein premortem. Use when the user wants to shape a rough concept before stress-testing, commercial validation, or implementation planning. Triggers include \"帮我把这个模糊想法理清楚\", \"我有个朦胧的想法\", \"这个 idea 还不成型\", \"帮我探索这个 problem space\", \"help me explore this vague idea\", \"develop this rough concept into a hypothesis\", equivalent producer-direction phrases in any language, and /audit-explore. Use /audit-brainstorming for an already formed hypothesis and /audit for defects in a shipping artifact. Do not auto-trigger on standalone words such as idea, explore, 想法, or 探索 without vague-development intent."
---

# /audit-explore — Vague idea → formed hypothesis (multi-LLM divergence)

> **Thin client routing skill** for the Decision Engine hub server.
> You (the client) run the public Double-Diamond skeleton + 5-Whys interview +
> user picks + exit-envelope assembly. The SERVER runs the divergence/convergence
> panels and holds the deep methodology IP — none of that lives in this file.

## When to use vs NOT

| Use this skill | Use instead |
|---|---|
| 1-5 sentence vague / fuzzy / "不成型" idea → develop into a testable hypothesis | Hypothesis already formed → `/audit-brainstorming` |
| About to hit `/audit-brainstorming` but the idea isn't hypothesis-class yet | Defect review of a shipping artifact → `/audit` |
| Cross-domain (software / research / ops / content / ml / business / gtm) | Commercial validation of a formed hypothesis → `/audit-market-research` |

Dedup: same vague idea explored within 5 turns + no material change → re-render the envelope, don't re-run.

## Workflow (5 phases — client orchestrates, server runs the panels)

- **[P0 CONSENT]** (client, MANDATORY before any panel call): classify data sensitivity (public / internal / confidential / regulated) AND show cost+time (`mode=standard` ~$0/~5-8min, `mode=deep` +metered/~8-12min, or no-panel client-only ideation). User must explicitly say yes + depth. `regulated`/declined → offer no-panel client-only. Detect BCP-47 language (und-* → ask).
- **[P1 FRAME]** (client, no panel): 5-Whys + JTBD interview, one question at a time (problem? why now? who? success? cost of inaction? → "When [situation] I want to [motivation] so I can [outcome]"). Build the frame artifact; user approves.
- **[P2 DIVERGE PROBLEM]** (server panel): submit the frame; server runs the panel with each vendor on its own methodology lens (server-side, hidden) → 9 HMW reframes + 3 PO provocations. **You** show them with vendor attribution; user picks 1.
- **[P3 DIVERGE SOLUTION]** (server panel): submit chosen HMW → 9 solution-directions. User picks 1-2.
- **[P4 CONVERGE+FALSIFY]** (server panel, premortem on): submit the draft → critique + Klein premortem + Goldilocks gate → hypothesis + `goldilocks_pass`/`exit_ready`.
- **[EXIT]** (client): assemble the typed envelope (frame, candidates, formed_hypothesis, panel_participation, next_skill_handoff). Show user before any downstream chaining.

### Calling pattern
```
# P2 / P3 — divergence panels (server injects each vendor's lens; do NOT pass lenses)
mcp__decision-engine__audit_skill_submit(
    skill_name="audit-explore",
    args={
        "title": "<idea slug> — <phase>",
        "content": "<frame artifact | chosen HMW + frame>",
        "phase": "diverge_problem",        # or "diverge_solution"
        "domain": "software",              # selects the server-side domain profile
        "mode": "standard",                # fast | standard | deep (default standard)
        # artifact_intent FORCED "explore_diverge" server-side (GENERATION contract → divergent
        #   options; NOT "hypothesis", which would degrade divergence to a strengths/risks review);
        #   methodology lens injected SERVER-side.
        "upstream_run_id": "<prior phase run_id>",          # P3+ : stateless linkage
        "upstream_canonical_sha": "<prior envelope canonical_sha>",
    },
)
# P4 — converge
mcp__decision-engine__audit_skill_submit(
    skill_name="audit-explore-converge",
    args={"title": "...", "content": "<hypothesis draft>", "premortem": True,
          "upstream_run_id": "...", "upstream_canonical_sha": "..."},
)
# Wait by POLLING (wait_audit is deprecated): loop check_audit_status(run_id) with backoff
# (~10s cadence), re-render per-auditor auditors[] each round; on terminal status fetch the
# payload with audit_skill_result. Unknown/error status (run_not_found after the run was seen,
# or a transport error) → surface + STOP; keep a total timeout / stay interruptible. Full loop
# + fallback nuance: see /audit "Waiting for the panel".
audit_skill_status / _result / _events / _cancel  (LLM-driven 30-60s; poll check_audit_status)
```

### You're a voice too — diverge alongside the panel
The panel excludes your own model family for independence, so you're never in it — don't double-count (keep `convergent_count` / `framework_diversity_check` external). But you hold the fullest context: while the panel runs, generate your OWN reframes (P2) / solution-directions (P3) / falsification (P4) — un-anchored — then fold them in as a co-equal voice shown separately. You also compose the panel: self-identify your runtime + pick `mode` so the server composes a complementary cross-vendor panel that excludes your own model family; if you can't, ASK — never silently default.

## Trust signals (envelope.trust_signals)
| Signal | Meaning |
|---|---|
| `canonical_sha` | sha256 over payload (always present) |
| `panel_size` / `convergent_assessment` / `convergent_count` | multi-審: auditor count + convergence (X/N) |
| `framework_diversity_check` | HIGH / MEDIUM / LOW — did the panel actually apply distinct lenses (server-computed) |
| `lens_injection` | `server-side-dynamic` — confirms per-vendor lenses were applied |
| `phase` / `premortem_applied` | (echoed) diverge phase; whether converge ran the Klein premortem |
| `goldilocks_pass` / `exit_ready` | (converge only) hypothesis met the exit gate (≥3 falsification criteria + ≥3 assumptions + null + base rate) |
| `upstream_run_id` / `upstream_canonical_sha` | (echoed) cross-phase trust chain |

## Convergence-quality discipline (apply client-side)
- **Convergence is a WARNING, not confidence** — `framework_diversity_check=LOW` = panel collapsed onto one frame (shared bias); surface it, don't celebrate.
- **Equal-weight candidates until P4** — no confidence ranking in the problem- and solution-divergence phases (early-confident-wrong locks in).
- **Critique loop ≤3 rounds** (hard cap); if Goldilocks still fails → `exit_ready:false` + what's missing; don't loop the panel.
- **Klein premortem always in P4** (`premortem=true`) — cheap insurance.

## Anti-patterns
- ❌ Skip P0 consent / data-safety before any panel call
- ❌ Auto-converge or auto-chain downstream without user picks + showing the envelope
- ❌ Pass your own methodology lenses — the server injects them
- ❌ Mark `exit_ready` yourself — it's the server Goldilocks gate

## Routing (chain)
```
vague idea → /audit-explore (this skill) → /audit-brainstorming (stress-test)
          → /audit-market-research (commercial) OR /audit-writing-plans (impl)
```
Gate every transition on user confirmation; the panel is one input, not a verdict.

## Pre-flight
- Device token configured
- P0 consent gate is mandatory — never bypass, even if the user seems eager
- High-stakes/irreversible → `mode="deep"`; `panel_participation` audit_ids enable 7-day re-query
