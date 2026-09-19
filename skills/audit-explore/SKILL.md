---
name: audit-explore
description: "Develop a vague, unformed idea into a clear, falsifiable hypothesis through guided framing and, when authorized, an external panel. Use for /audit-explore and equivalent intent in any language when the user wants to clarify or develop a rough concept before evaluation. Do not trigger on isolated words such as idea or explore, or when the primary task is existing code or repository exploration. Use audit-brainstorming for formed hypotheses, audit-market-research for external evidence, audit-writing-plans for implementation documentation, and audit for deliverable defects."
---

# /audit-explore - From vague idea to falsifiable hypothesis

Use this skill to clarify an idea that is not yet specific enough to evaluate. The client owns
framing, consent, user choices, and the final handoff. Decision Engine owns any authorized external
divergence and convergence panels and their server-side methodology lenses.

## Routing

| Request | Route |
|---|---|
| Develop a vague idea into a testable hypothesis | `/audit-explore` |
| Challenge an already formed hypothesis or strategy | `/audit-brainstorming` |
| Acquire or verify external facts, citations, competitors, or market evidence | `/audit-market-research` |
| Turn accepted conclusions into implementation documentation | `/audit-writing-plans` |
| Find defects in a deliverable | `/audit` |

An isolated word such as `idea` or `explore` is not enough. A vague software, product, feature, or API
idea remains in scope. The exclusion applies when the primary task is inspecting existing code or
repository content. For a mixed request, frame the vague idea first, then ask before handing a formed
research question to `/audit-market-research` or starting implementation exploration. A direct
evidence-first or code-inspection request routes to the corresponding specialist immediately.

## Phase Map

1. **P0 Consent:** classify the content, explain available execution paths, and obtain the user's
   explicit choice before any external transmission.
2. **P1 Frame:** use 5-Whys and JTBD questions to create a frame the user approves.
3. **P2 Diverge Problem:** produce problem reframes; the user selects one.
4. **P3 Diverge Solution:** produce solution directions; the user selects one or two.
5. **P4 Converge and Falsify:** critique the draft, run the premortem and Goldilocks gate, and form
   the hypothesis.
6. **Exit:** show the typed result envelope and ask before any downstream handoff.

## Progressive References

- At activation, read only [consent-and-framing.md](references/consent-and-framing.md). It owns P0,
  P1, data handling, and the complete client-only path.
- Only on an authorized external path, after an approved frame, read
  [hosted-exploration.md](references/hosted-exploration.md). It owns P2 and P3 submissions,
  polling, user selections, and the independent client voice.
- Only on that authorized external path, when entering P4 or rendering the hosted final envelope,
  read
  [convergence-and-result.md](references/convergence-and-result.md). It owns convergence, trust
  signals, the exit gate, presentation, and downstream confirmation.

Do not load later references early. On a client-only path, do not load or follow hosted submission
instructions.

## Always-On Invariants

- External processing is optional. Never treat skill activation as transmission consent.
- Regulated content is never transmitted externally. Keep other non-public content client-side
  unless a runtime-discoverable authoritative policy and explicit user authorization both permit
  the exact transmission. Secrets and credentials are never submitted.
- The user approves the P1 frame, chooses the P2 reframe and P3 direction, and confirms every
  downstream transition. Never auto-converge or auto-chain.
- The client never supplies methodology lenses or authors server-owned trust and exit fields.
- Treat the external panel as one input, not a verdict. Keep the independent client contribution
  separate from panel convergence counts.
- If the same vague idea appears within five turns without material change, re-render the existing
  envelope instead of submitting a duplicate. A materially changed idea may start a new run.
