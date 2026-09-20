---
name: audit-explore
description: "Develop a vague, unformed idea into a clear, falsifiable hypothesis through guided framing and an external panel by default for routine content. Use for /audit-explore and equivalent intent in any language when the user wants to clarify or develop a rough concept before evaluation. Do not trigger on isolated words such as idea or explore, or when the primary task is existing code or repository exploration. Use audit-brainstorming for formed hypotheses, audit-market-research for external evidence, audit-writing-plans for implementation documentation, and audit for deliverable defects."
---

# /audit-explore - From vague idea to falsifiable hypothesis

Use this skill to clarify an idea that is not yet specific enough to evaluate. The client owns
framing, content routing, user choices, and the final handoff. Decision Engine owns external
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

1. **P0 Route:** classify the content. Routine use takes the hosted path without a separate
   consent prompt; sensitive content may need approval or a local-only path.
2. **P1 Frame:** use 5-Whys and JTBD questions to create a frame the user approves.
3. **P2 Diverge Problem:** produce problem reframes; the user selects one.
4. **P3 Diverge Solution:** produce solution directions; the user selects one or two.
5. **P4 Converge and Falsify:** critique the draft, run the premortem and Goldilocks gate, and form
   the hypothesis.
6. **Exit:** show the typed result envelope and ask before any downstream handoff.

## Progressive References

- At activation, read only [consent-and-framing.md](references/consent-and-framing.md). It owns P0,
  P1, data handling, and the complete client-only path.
- Only on a hosted path, after an approved frame, read
  [hosted-exploration.md](references/hosted-exploration.md). It owns P2 and P3 submissions,
  polling, user selections, and the independent client voice.
- Only on that hosted path, when entering P4 or rendering the hosted final envelope,
  read
  [convergence-and-result.md](references/convergence-and-result.md). It owns convergence, trust
  signals, the exit gate, presentation, and downstream confirmation.

Do not load later references early. On a client-only path, do not load or follow hosted submission
instructions.

## Always-On Invariants

- Ordinary user-directed `/audit-explore` use proceeds through MCP without a separate external
  consent prompt. Classify the exact P2, P3, and P4 payloads; do not silently choose local-only.
- Secrets, regulated data, unconsented third-party identifiers, and material under a confidentiality
  duty must be removed or kept local. First-party identifiers can be sensitive but shareable with
  exact-payload approval. Routine personal planning context is not automatically sensitive.
- Classify Prohibited before Sensitive before Routine; uncertainty takes the stricter route.
- For sensitive but shareable content, show the complete exact user-derived payload and recipients
  and obtain explicit authorization before each changed submission. Pending is not local-only.
- The user approves the P1 frame, chooses the P2 reframe and P3 direction, and confirms every
  downstream transition. Never auto-converge or auto-chain.
- Use `audit_mode`; never supply lenses or trust fields, query policy or roster, or claim exclusion.
- Treat the external panel as one input, not a verdict. Keep the independent client contribution
  separate from panel convergence counts.
- If the same vague idea appears within five turns without material change, re-render the existing
  envelope instead of submitting a duplicate. A materially changed idea may start a new run.
