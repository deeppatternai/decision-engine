---
name: layer-check
description: "Prevent category errors in comparative, competitive, commercial, and market analysis when products from different layers are treated as direct substitutes. Use before or during comparisons involving platforms, infrastructure, applications, services, agents, models, protocols, or marketplaces; trigger on “是不是同一层”, “能直接比较吗”, “竞品层级”, “替代关系”, “layer check”, or when an audit/market-research result may conflate complements with competitors. Identify each subject’s layer, buyer, job, integration boundary, and actual substitution relationship."
---

# /layer-check — Prevent category errors in comparative analysis

> **Local reasoning discipline — no server call.** Unlike the `/audit-*` and
> `/graphic-explanation` skills, layer-check has **no server backend, no workflow
> handler, and produces no artifact**. It runs entirely inside the calling agent's
> reasoning: a checklist you apply to a comparative artifact *before* you submit it
> for audit, and to the audit result *before* you integrate any finding. Nothing here
> routes to the hub.

## Why this exists — the failure mode

Comparative analysis fails in a specific, repeatable way: **a product at one layer of
the stack gets treated as a substitute for (or a commoditizer of) a product at a
completely different layer.** The two do different jobs, sit at different points in the
value chain, and often one is a potential *dependency* of the other — yet the analysis
reads them as competitors.

### An illustrative example (abstract, so it teaches the method not a specific case)

Imagine an **application-layer workflow SaaS** — call it *Product-A* — that takes user
intent, orchestrates a multi-step workflow across several foundation models, adjudicates
the results, and returns a value-added artifact with signed evidence. Now imagine a
comparative analysis that says:

> "Product-A's edge is being commoditized by the multi-vendor **API gateways / token
> aggregators** — they already route requests across every model behind one interface."

This is a **category error**. The API gateway is *transport-layer plumbing*: it routes
requests and bills multi-vendor usage. Product-A runs an end-user *workflow* on top of
whatever transport it uses. The gateway cannot orchestrate the workflow, adjudicate the
outputs, or produce the signed artifact — it is infrastructure Product-A could optionally
consume *as* transport. Saying the gateway "commoditizes" Product-A is the same shape of
error as saying:

- "A raw foundation-model API commoditizes an AI coding IDE"
- "Stripe commoditizes Shopify"
- "Twilio commoditizes Slack"
- "AWS EC2 commoditizes a deployment platform"

In every case an **input / dependency at a lower layer** is being mistaken for a
**substitute at the product's own layer**.

### Why a multi-LLM panel does not automatically catch it — and why the adjudicator must

There are two distinct guards this skill enforces. Keep them separate; they fail
differently.

**1. Adjudicator over-extrapolation (the primary, higher-frequency failure).** Even when
the panel gets the layers *right* — correctly scoping a commoditization signal to the one
lower layer it actually touches — the adjudicator can take that TRUE, narrow signal ("the
transport-layer piece of the moat will be commoditized") and **over-extrapolate** it into
a whole-thesis down-revision (gut the overall moat, shrink the window, slash the revenue
ceiling), collapsing a "one-slice-is-commoditizable" observation into "the whole product's
moat is gone" — ignoring that the durable, higher-layer moats are untouched. **The primary
discipline is adjudicator-side: do NOT extrapolate a single-layer commoditization signal
into a whole-thesis / revenue / window down-revision. Quarantine the commoditized layer,
re-state the thesis without it, and check whether it still holds.**

**2. Auditor correlated bias (a real but secondary risk).** Multi-LLM convergence is not
automatic proof of a true signal. Trigger keywords ("unified cross-model interface / token
pool / uptime SLA") reliably *associate* to transport-layer aggregators in the models'
shared pretraining corpus (Common Crawl, GitHub, AI-tooling blogs). Because the panel
shares that corpus, for keyword-triggered references their "independence" is not
statistical independence — several voices can converge on the *same wrong reference* not
because it is a real threat but because they learned the same association. So convergence
on a cross-layer reference is **evidence of shared training-distribution bias, not
evidence of a real threat.**

Both guards live with the **adjudicator (the calling agent)**, because neither is
something an individual auditor can self-correct: the bias is in the shared reference and
in the extrapolation step, not in any single voice's local reasoning.

## When to run this skill

**MANDATORY** when the artifact contains ANY of:

- Comparative claims ("X commoditizes Y", "替代", "competes with", "alternative to <product>")
- Moat / 护城河 discussion
- Competitive analysis / reference class / comp-set
- TAM / SAM / SOM estimation citing comparable companies
- "Why us not them" positioning
- Anything where the adjudication trades on "is product A in the same market as product B?"

**SKIP** for:

- Pure code-defect audit (no cross-product comparison)
- Pure logic / mathematical verification
- Internal-only style / readability review

## Voice-name privacy (applies to the layer-check verdict you report)

Audit returns are redacted to `Voice 1..N` for normal (non-debug) users. When you report
which auditors cited or converged on a cross-layer reference, refer to them ONLY by their
`Voice N` labels (`Voice 1`, `Voice 2`, …), NEVER by a model / vendor name — even from
memory. (Full rule: `/audit` → "Voice-name privacy".)

## The layer taxonomy

Pre-audit, every product mentioned in the artifact should be tagged with **one** of these
layers. Comparing across layers requires explicit justification.

| Layer | What it is | Examples |
|---|---|---|
| **L1 Metal** | Hardware, chips, datacenter | GPUs / accelerators, datacenter fiber |
| **L2 Foundation Model** | Pretrained base + alignment | The large frontier model families |
| **L3 Training / Eval Infrastructure** | Pipelines feeding L2 | RLHF tooling, eval harnesses, dataset platforms |
| **L4 Transport / API Gateway** | Routes requests to L2, no business logic | Multi-vendor API gateways / token aggregators, managed model-hosting APIs, a raw foundation-model API |
| **L5 Orchestration Framework** | Multi-LLM workflow primitives, no end-user product | Agent / chain / graph frameworks |
| **L6 Developer Tooling Platform** | Code-side infra + ops | Vector databases, managed compute / inference-hosting platforms |
| **L7 Application / Workflow SaaS** | End-user product with workflow, UX, business logic | AI coding IDEs, code-review products, search assistants, vertical copilots |
| **L8 Brand / Distribution Surface** | User attention, default workflow | Consumer chat destinations, code-host marketplaces |

A product can span layers — a single company can ship an L2 foundation model **and** an L4
API **and** an L7 app **and** an L8 distribution surface. Tag the product's **primary
monetized layer** for the artifact's purposes.

**Always cite a specific product or surface, not a company name.** A big AI company
occupies L2 (its model family) + L4 (its raw API) + L8 (its consumer destination)
simultaneously. "Company X competes with Product-A" is ambiguous and recreates exactly the
confusion this skill prevents. "Company X's *consumer app* competes with Product-A" or
"Company X's *raw API* competes with Product-A" is testable — one may be true and the
other a category error.

## The cross-layer comparison rule

> **Heuristic, not a hard law: layer distance is a fast *screen*, not the verdict — the
> real test is same job-to-be-done.** A product at Lx is almost never substituted by one
> at Ly when |x − y| ≥ 2. But adjacency (|x − y| ≤ 1) does NOT make two products
> substitutes: when their jobs differ they are still a category error.

| Comparison | Same category? | Why |
|---|---|---|
| AI coding IDE (L7) vs code-review SaaS (L7) | ✅ Yes | Both application-layer dev-workflow products |
| Workflow SaaS (L7) vs API gateway (L4) | ❌ **No, 3 layers apart** | Gateway routes calls; the SaaS runs workflows on top of routed calls |
| API gateway (L4) vs agent framework (L5) | 🟡 Adjacent | Both dev plumbing; some overlap, different jobs |
| A company's raw API (L4) vs its consumer app (L8) | ❌ No | Same company, different products at different layers |
| No-code app builder (L7) vs managed-compute platform (L6) | ❌ No | One ships an app for an end user; the other rents compute |

**Adjacent layers (|x − y| ≤ 1):** distance alone does NOT clear the comparison. Confirm
both products do the **same job-to-be-done**; if the jobs differ, an adjacent pair is
still a category error ("ship an app for an end user" ≠ "rent compute / containers").
Valid only with explicit same-job justification.

**Skipping layers (|x − y| ≥ 2):** comparison is almost certainly a category error. Demand
evidence.

**Same layer (|x − y| = 0):** passes the category gate — but that is necessary, not
sufficient. Same layer ≠ same ICP ≠ same job-to-be-done, so still run a normal market-fit
check (see "What this skill does NOT do").

**Exception — vertical integration / product extension.** A company can occupy multiple
layers via different products. If product A (Lx) cites product B as a substitute and B
sits ≥ 2 layers away *by company-level naming*, the comparison is still valid IF B's
company **actually ships a same-layer product** taking the same job-to-be-done. Cite the
specific product, not the company. "A search assistant (L7) competes with a consumer chat
destination (L8 + L7 workflow)" is valid because that destination itself offers
L7-equivalent workflow. "A search assistant (L7) competes with a raw model API (L4)" is
NOT valid — different layer, different job. The exception narrows *when*, not *whether*,
the layer rule applies.

## How to apply — two integration points

### A. Pre-audit (artifact author / calling session)

**Before submitting a comparative / moat / TAM artifact for audit, do this in the same
turn:**

1. **Tag the subject product's layer.** "Product-A is L7 (application + workflow SaaS)."
2. **Tag every cited reference's layer.** "References: an AI coding IDE (L7), a code-review
   SaaS (L7), a vertical copilot (L7)."
3. **Drop any cross-layer reference that snuck in.** If you find yourself reaching for "and
   we're like the token aggregator in unifying access" — STOP. That aggregator is L4.
   Either drop the reference or explain the layer transition explicitly.
4. **Replace layer-confusing keywords** in the moat / value-prop description. The left
   column reliably triggers a transport-layer reference in any auditor's training
   distribution; the right column anchors to application-layer concepts:

   | Replace this (auditor trigger keyword) | With this (layer-specific) |
   |---|---|
   | "unified cross-model interface" / "one API for all models" | "cross-model adjudication logic" / "convergent-vs-divergent analysis" |
   | "cross-model token pool" / "credit aggregation" | "cross-product wallet" / "unified workflow billing" |
   | "cross-model uptime SLA" | "workflow-level SLA" / "task-completion guarantee" |
   | "routing" / "failover" | "orchestration" / "multi-LLM panel coordination" |

5. **Inject layer context into the audit `context` field:**

   ```text
   context: "Subject product is at L7 (application + workflow SaaS layer).
   Comparable products: <same-layer L7 products>. DO NOT compare to L4
   transport gateways / token aggregators — those are potential dependencies,
   not substitutes. DO NOT compare to L2 foundation models — those are inputs,
   not competitors."
   ```

### B. Post-audit (adjudicator / calling session, MANDATORY)

**Before integrating ANY convergent finding from a comparative audit, run this check:**

1. **List every product reference each auditor used.** Which fields carry references
   depends on the audit's schema:
   - **DEFECT audits** (`/audit` default — returns `findings[]`): scan each finding's
     `claim` + `evidence`.
   - **HYPOTHESIS audits** (`/audit-brainstorming`): scan `risks` / `counter_arguments` /
     `competitor` / `base_rate_or_reference_class`.
2. **For each reference, tag its layer** using the taxonomy above.
3. **Flag any reference at distance ≥ 2 from the subject product's layer.**
4. **Downgrade convergent signals that all reference flagged products** — this is
   correlated bias, not independent judgment.

   If multiple auditors converge on the same cross-layer reference, **that is evidence of
   shared training-distribution bias, NOT evidence of a real threat.**

5. **Re-state the affected findings without the bad reference** — does the underlying
   claim still hold? Often the claim collapses without the (wrong) reference.

#### Adjudicator script for the post-audit check

```text
For each convergent finding F in audits[*]:
  # F's reference-bearing fields depend on the audit schema:
  #   DEFECT (findings[], from /audit):        F.claim + F.evidence
  #   HYPOTHESIS (from /audit-brainstorming):  F.risks + F.counter_arguments
  #                                            + F.competitor + F.base_rate_or_reference_class
  refs = extract all product names mentioned in F's reference-bearing fields
  for r in refs:
    L_r = classify_layer(r)          # use taxonomy table
    L_subject = layer of artifact's subject
    distance = |L_r - L_subject|
    if distance >= 2:
      mark r as CROSS_LAYER
  if all refs in F are CROSS_LAYER:
    F.signal_quality = "correlated_bias_suspected"
    F.weight = "discuss but do not down-revise core thesis"
  elif majority refs are CROSS_LAYER:
    F.signal_quality = "mixed"
    F.weight = "examine same-layer refs only"
  else:
    F.signal_quality = "real"
    F.weight = "full integration"
```

## Failure-mode signatures to look for

These patterns in the audit result tell the adjudicator to suspect layer confusion:

1. **Multiple auditors converge on the same set of lower-layer infra references when the
   subject is an application-layer product.** Especially well-known infra (API gateways,
   payment rails, comms APIs, cloud compute). This overlap may be keyword-association rather
   than a real threat.

2. **The "commoditize" verb applied across layers.** Transport-layer products do not
   commoditize application-layer products. They are inputs.

3. **References that are also potential vendors / dependencies of the subject product.** If
   "competitor X" is something the subject could literally consume as plumbing, X is not a
   competitor.

4. **A reference class for revenue / market-sizing drawn from a different layer.** Comparing
   an application-layer product to a transport-layer library for revenue trajectory is
   meaningless — they monetize fundamentally different value.

5. **A "the edge is being commoditized" claim with no same-layer substitute named.** If the
   audit says "X is being commoditized" but cannot name a *same-layer* product doing X, the
   claim is suspect.

## Common cross-layer category errors (reference table)

| Subject (correct layer) | Common wrong reference | Why it's wrong | Correct same-layer reference |
|---|---|---|---|
| Cross-model workflow SaaS (L7) | Multi-vendor API gateway / token aggregator (L4) | The gateway routes; the SaaS runs workflows on top | Same-layer application-workflow products |
| AI coding IDE (L7) | A raw foundation-model API (L4) | The API is an input; the IDE is the product | Other AI-assisted IDEs / editor copilots |
| No-code app builder (L7) | Managed container / compute infra (L6) | Different value prop | Other app builders |
| Search assistant (L7) | A web-search API (L4) | The API is an input | Other search-assistant apps |
| Commerce platform (L7) | Payment processor (L4) | Payment is one feature | Other commerce platforms |
| Workspace + AI (L7) | A raw foundation-model API (L4) | The API is an input | Other AI workspace products |
| Vertical (legal / medical) copilot (L7) | A raw foundation-model API (L4) | The API is an input | Other vertical copilots in the same domain |

## What this skill does NOT do

- Does not detect within-layer competitive errors (e.g. claiming product A competes with B
  when they target different ICPs at the same layer). Use normal market-fit reasoning.
- Does not validate moat strength — only catches the **wrong-layer reference** failure
  mode. An edge may still be weak for legitimate same-layer reasons.
- Does not replace independent judgment about whether a strategy is sound. Layer-check is
  necessary but not sufficient.
- Does not apply to single-auditor audits **for the convergence-downgrade step only** (B.4
  / the `all refs CROSS_LAYER → correlated_bias` branch) — with one auditor there is no
  cross-voice convergence to weigh. Everything else still applies regardless of auditor
  count: pre-audit layer tagging stays mandatory, and a single auditor can still make a
  layer error, so the reviewer must sanity-check its references against the taxonomy.

## Relationship to other skills

| Skill | Relationship |
|---|---|
| `/audit` (defect review) | layer-check applies when `/audit` is comparing products. For pure code / spec defect review, skip. |
| `/audit-brainstorming` (hypothesis stress-test) | **Strong overlap** — hypothesis-class artifacts often carry comparative claims. Run layer-check by default for market / strategy / business-model brainstorming. |
| `/audit-market-research` (commercial insight) | **Strong overlap** — competitor / TAM / SAM / comp-set claims are its core output. Run layer-check by default on any comparative finding it produces. |
| `/audit-explore` (vague idea → hypothesis) | If the formed hypothesis carries competitive / market positioning, run layer-check before handing it downstream. |
| `/audit-forecast` (prediction-consensus) | Usually N/A. Applies only if the forecast reasoning leans on a cross-product reference class / comp-set. |
| `/audit-writing-plans` (impl-doc producer) | Layer-check should already have run upstream; if the drafted plan re-introduces comparative / market claims, re-check before promoting it. |

## Operational checklist (post-audit, for adjudicator)

Before writing the synthesis reply to the user:

- [ ] Identified the subject product's primary layer (L1–L8)
- [ ] Listed every product reference in each auditor's output
- [ ] Classified each reference's layer
- [ ] Computed |layer distance| for each reference vs the subject
- [ ] Flagged all refs at distance ≥ 2
- [ ] Re-examined convergent findings that depend only on flagged refs — downgraded weight or removed
- [ ] If any core down-revision (window narrowing, moat reduction, scenario reweighting) traces to a flagged reference — reversed **only the portion** that depends on the cross-layer reference; kept any portion still supported by same-layer evidence
- [ ] **Over-extrapolation guard:** even when a commoditization signal is TRUE and same-layer, did NOT extrapolate it from the affected layer into a whole-thesis / revenue / window down-revision — quarantined the commoditized layer, re-stated the thesis WITHOUT it, and confirmed the remaining durable-layer moats still hold
- [ ] Verbally acknowledged that layer-check ran (so the user knows it was applied)

## Final note — when convergence is real

Convergence IS real signal when:

- Multiple auditors cite **same-layer** competitors with **distinct** reasoning paths
- The convergent claim survives translation into layer-specific keywords
- The convergent claim does not require the cross-layer reference to hold

This skill is not anti-convergence. It is anti-*spurious*-convergence. The whole point of a
multi-LLM panel is to extract real cross-checked signal; layer-check is the quality gate
that distinguishes real signal from training-distribution echo.
