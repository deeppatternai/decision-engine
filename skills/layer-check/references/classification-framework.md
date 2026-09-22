# Classification Framework

Read this reference for every layer check. It supplies a non-linear product map and the tests used
to distinguish substitution from adjacency, dependency, and vertical overlap.

## Start With The Offering

Classify a named product surface, not an organization. One organization can simultaneously sell a
foundation technology, an API, an application, and a distribution surface. Select the surface that
appears in the comparison claim and record any additional surface that creates vertical overlap.

Use facts from the user, supplied artifacts, or verified sources. Do not infer current capabilities
from a company name or reputation.

## Functional Map

These categories describe positions in a value chain. They are not a numerical ladder, and the
distance between categories is not a competition score.

| Category | Primary function | Typical offerings |
|---|---|---|
| Physical infrastructure | Supplies hardware, facilities, or network capacity | Accelerators, datacenters, network fabric |
| Foundation technology | Supplies a reusable core capability | Foundation models, database engines, core protocols |
| Training, data, or evaluation infrastructure | Produces, adapts, measures, or governs foundation capabilities | Training pipelines, dataset platforms, evaluation systems |
| Access and transport | Exposes, routes, hosts, or bills access to another capability | APIs, gateways, managed model endpoints |
| Orchestration framework | Gives builders primitives for composing multi-step behavior | Agent, chain, workflow, or graph frameworks |
| Developer or operations platform | Operates, stores, observes, or deploys technical workloads | Compute platforms, vector stores, observability tools |
| End-user application or workflow | Completes a user or business task through product logic and UX | Coding tools, review products, search assistants, vertical software |
| Distribution or marketplace | Controls discovery, default access, audience, or transaction flow | App stores, marketplaces, consumer destinations |

An offering may span categories. Record each material function, then identify which function is
actually being compared. Do not collapse all surfaces into a single company-level label.

## Decision Dimensions

Evaluate each dimension explicitly:

| Dimension | Question |
|---|---|
| Buyer | Does the same role or organization approve and pay for both? |
| User | Does the same person or system operate both? |
| Job-to-be-done | Are customers hiring both offerings for the same outcome? |
| Budget | Do they compete for the same spend or procurement decision? |
| Replacement | Can adopting one eliminate or materially reduce the need for the other? |
| Value capture | Do they monetize the same unit of customer value? |
| Integration | Does one consume, supply, host, distribute, or enable the other? |
| Switching behavior | Do customers evaluate them as alternatives at the same decision point? |

No single dimension is decisive. Direct substitution normally requires strong alignment on job,
replacement, and customer decision, with supporting alignment on buyer or budget.

## Primary Substitution Verdicts

### Direct Substitute

Use when a customer can choose either offering to complete substantially the same job and adopting
one normally removes the need for the other. Feature differences do not prevent this verdict.

### Partial Substitute

Use when replacement is real but bounded to a workflow, segment, use case, or product surface. Name
that boundary. Do not extend the result to the whole product or market.

### Comparable But Non-Substitutable

Use when a comparison is useful for one bounded purpose, such as pricing mechanics, distribution,
growth pattern, or operational model, while the offerings do not replace each other. State the
valid comparison dimension and prohibit broader competitor claims.

### Not Meaningfully Comparable

Use when the offerings serve different jobs, buyers, budgets, or value pools and the proposed
comparison has no stated limited purpose.

### Insufficient Evidence

Use when material facts about the offering, customer, replacement behavior, or current capability
are missing or contradictory. List the smallest fact set needed to resolve the classification.

## Secondary Relationship Annotations

### Complement Or Dependency

Use when one offering is an input, vendor, channel, host, distribution surface, or capability that
the other can consume. Integration alone does not prove non-competition, but it is strong evidence
against a claim of complete substitution.

### Vertical Overlap

Use when offerings originate at different functional layers but one has expanded into the same job
and customer decision as the other. Judge the expanded product surface, not the parent company's
original layer.

These annotations may coexist with a primary substitution verdict. Replacement evidence controls
the primary verdict: a vertically integrated same-job offering is primarily a direct or partial
substitute and secondarily a vertical overlap. Scope annotations to the relevant product surface.

## Heuristics And Exceptions

- Same category is necessary for many direct comparisons but never sufficient.
- Different categories are a prompt to inspect the relationship, not proof of a category error.
- Offerings in clearly distant functional categories carry a strong presumption against direct
  substitution. Override it only with affirmative evidence of same-job replacement or a vertically
  integrated product surface that reaches the same customer decision.
- A lower-level capability may commoditize one component of an application without replacing the
  workflow, customer relationship, proprietary data, distribution, or operational service.
- A dependency can become a competitor through vertical expansion; require evidence that it now
  performs the same job for the same customer.
- A distribution surface can compete with an application when it offers an equivalent workflow,
  but access to the same audience alone does not establish substitution.
- Reference-class and comparable-company analysis may cross categories only when the compared
  metric and economic mechanism are explicitly shared.

## Framing Hygiene

Describe the compared capability and relationship precisely. Replace vague company-level or
umbrella claims with the actual surface, job, and boundary being evaluated. For example, distinguish
"routes API requests" from "completes and adjudicates a user workflow."

Do not rewrite accurate user language merely to steer a later reviewer toward a preferred answer.
When passing the comparison into another review, include the product surfaces, their functional
categories, the job-to-be-done, and any dependency or vertical-overlap relationship as neutral
context.

## Illustrative Checks

| Comparison | Likely relationship | Reason to verify |
|---|---|---|
| End-user workflow application vs raw API it consumes | Complement or dependency | One completes the user's job; the other supplies a capability |
| Commerce platform vs payment processor | Complement or dependency | Payment is one input unless the processor ships the same commerce workflow |
| Two end-user applications for the same workflow | Direct or partial substitute | Confirm buyer, segment, and replacement behavior |
| Application vs infrastructure provider with a new application product | Vertical overlap | Evaluate the new product surface rather than the provider's historical layer |
| Two unrelated products with similar subscription pricing | Comparable but non-substitutable | Pricing form alone does not establish competition |

## Scope Guard

After classifying the relationship, rewrite the original claim at the narrowest supported scope.
For example, replace "the infrastructure provider eliminates the application's moat" with "the
provider may commoditize the application's access layer; the workflow and distribution claims
remain untested." Do not preserve or reject the broader thesis until its remaining components are
examined separately.
