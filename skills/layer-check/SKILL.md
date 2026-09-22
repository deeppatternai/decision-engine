---
name: layer-check
description: "Determine whether products, services, platforms, or technologies are true substitutes, complements, dependencies, or vertically overlapping offerings. Use when a competitor claim, market map, positioning argument, comparable-company set, moat analysis, or market-size or revenue estimate based on comparables may combine offerings that perform different jobs or operate at different product layers. Classify the specific offerings by buyer, user, job-to-be-done, value capture, and integration boundary, then state whether the comparison is valid. Do not use for ordinary feature comparisons between offerings already established as direct competitors."
---

# Layer Check

Use this local reasoning skill to prevent category errors in competitive and commercial
analysis. It determines the relationship between specific offerings before treating them as
competitors, substitutes, comparables, or evidence about an entire business thesis.

This skill does not call a server, does not submit an audit, and does not produce a hosted
artifact. It analyzes the user's material and returns a structured relationship verdict.

## Use And Boundaries

Use this skill when the user asks whether offerings:

- compete directly or can replace one another;
- belong in the same competitor set, market map, or comparable-company set;
- threaten the same moat, budget, customer workflow, or revenue pool;
- are dependencies, complements, platforms, or vertically integrated alternatives;
- support a market-size or revenue estimate through comparable products or companies; or
- have been compared in an existing report without a clear product-layer justification.

Skip it for ordinary feature comparisons after direct competition is already established, pure
code or mathematical review, and analysis that contains no cross-product claim. This skill does
not measure market size, prove moat strength, or decide whether a strategy is sound.

## Core Rules

1. Compare specific offerings or product surfaces, not company names. A company may operate at
   several layers through separate products.
2. Treat layer classification as a screen, not the verdict. Same layer does not prove
   substitutability, and different layers do not rule it out when vertical integration creates a
   genuine same-job alternative.
3. Center the verdict on actual substitution: buyer, user, job-to-be-done, budget, replacement
   behavior, value capture, and integration boundary.
4. Distinguish a competitor from an input, vendor, channel, complement, or dependency.
5. Scope conclusions to the affected layer. A commoditized component does not by itself erase the
   value, moat, revenue, or adoption case of the complete offering.
6. Treat repeated conclusions as independent support only when their evidence and reasoning paths
   are meaningfully independent. Agreement around one weak comparison is not stronger evidence.
7. Mark uncertain product facts as `insufficient evidence`; do not force a layer or relationship.

## Progressive Disclosure

- For a direct comparison or when constructing a competitor set, read and follow
  [references/classification-framework.md](references/classification-framework.md).
- When reviewing an existing market report, strategy document, comparison, or multi-review result,
  read both [references/classification-framework.md](references/classification-framework.md) and
  [references/comparative-review.md](references/comparative-review.md).
- Do not load `comparative-review.md` for a simple direct comparison.

## Workflow

1. Restate the exact comparison claim and name the specific offering from each organization.
2. Select the route above and gather only the facts needed by its decision dimensions.
3. Classify each offering's function and position in the value chain. Allow multiple layers when
   the offering genuinely spans them; identify the surface relevant to this comparison.
4. Test whether a customer can replace one with the other for the same job and budget. Separate
   full replacement from partial overlap, integration, distribution, and dependency.
5. Challenge the proposed conclusion: remove the questionable reference and check whether the
   competitive, moat, market-size, or revenue claim still holds.
6. Return the route-specific table, one primary verdict per comparison, and any secondary
   relationship annotations. State evidence gaps and the narrowest conclusion supported by the facts.

## Output Contract

For an existing report, strategy document, or multi-review result, use the claim-based
`Review Output` in `references/comparative-review.md` instead of the direct-comparison table below.

For a direct comparison, report one row per compared pair:

| Field | Required content |
|---|---|
| Compared offerings | The two specific products, services, APIs, marketplaces, or surfaces |
| Functional layers | Relevant categories for each offering |
| Buyer and user alignment | Whether the same parties pay for and use them |
| Job and budget overlap | Whether they pursue the same outcome and procurement decision |
| Replacement evidence | Whether choosing one removes or reduces the need for the other |
| Primary verdict | One substitution verdict from the list below |
| Secondary relationships | Zero or more structural annotations from the list below |
| Evidence | Facts supporting the classification; identify missing facts |

Choose exactly one primary substitution verdict:

- `direct substitute`: either offering can replace the other for substantially the same job.
- `partial substitute`: only a defined workflow, customer segment, or feature is replaceable.
- `comparable but non-substitutable`: comparison is useful for a limited dimension, not replacement.
- `not meaningfully comparable`: the claim combines different jobs, buyers, or value pools.
- `insufficient evidence`: the available facts cannot support a responsible classification.

Add zero or more secondary relationship annotations:

- `complement or dependency`: one increases, supplies, distributes, or enables the other's value.
- `vertical overlap`: offerings begin at different layers but one has expanded into the other's job.

Replacement takes precedence on the primary axis. If a vertically integrated provider now performs
the same job, use `direct substitute` or `partial substitute` as primary and `vertical overlap` as a
secondary annotation. If one surface remains an input while another overlaps, preserve both scoped
relationships rather than forcing one company-wide label.

Close with:

- **Comparison validity:** valid, partially valid, invalid, or unresolved.
- **Scope:** the exact workflow, segment, layer, or claim to which the verdict applies.
- **Implication:** what the verdict changes in the user's analysis, without extending beyond the
  supported scope.

## Safety Against Overreach

- Do not convert a layer label into a factual product claim without evidence.
- Do not use numerical layer distance as proof of competition or non-competition.
- Do not rewrite the user's positioning merely to make a comparison pass.
- Preserve any privacy labels already present in supplied material; do not infer hidden identities.
- When current product capabilities are material and not supplied, verify them or clearly leave the
  verdict unresolved.
