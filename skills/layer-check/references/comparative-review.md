# Reviewing Existing Comparative Material

Read this reference only when checking an existing report, strategy document, market map,
comparison, or multi-review result. Apply the classification framework before using this route.

## Review Procedure

1. Extract every claim that treats one offering as a competitor, substitute, comparable,
   dependency, commoditizer, or threat to another.
2. Replace company-level names with the exact product surfaces meant by the claim. If the artifact
   does not identify a surface, mark the claim ambiguous.
3. Build the classification table from `classification-framework.md` for the subject and every
   referenced offering.
4. Trace each conclusion to its supporting reference, product fact, and reasoning path. Separate
   repeated citations from genuinely independent evidence.
5. Assign a relationship verdict to each comparison and state whether the original claim is valid,
   partially valid, invalid, or unresolved.
6. Remove or narrow invalid comparisons, then re-evaluate every downstream moat, market-size,
   revenue, positioning, and timing conclusion that depended on them.
7. Quarantine any affected component: state exactly what is commoditized or replaced, restate the
   thesis without that component, and retest the remaining moat, revenue, and timing claims.
8. Preserve conclusions that have independent same-job evidence. Do not reverse an entire thesis
   merely because one supporting comparison fails.

## Evidence Independence

Several reviewers reaching the same conclusion does not automatically create independent support.
Check whether they:

- cite distinct product facts or merely repeat the same reference;
- reason from separate customer, replacement, pricing, or integration evidence;
- name a product surface that actually performs the claimed job; and
- reach the conclusion after the questionable comparison is removed.

Repeated reliance on one cross-category reference is a correlated-evidence warning, not proof that
the reviewers share a particular training bias. Lower the confidence until the claim has independent
same-job or replacement evidence.

## Common Failure Signatures

- A vendor, API, payment rail, hosting platform, or framework is called a competitor even though the
  subject consumes it as an input.
- A claim uses "commoditize" without naming a product that replaces the customer's complete job.
- A market-size or revenue comparison uses businesses with different buyers and value-capture
  models without explaining the shared economic mechanism.
- A company name stands in for several unrelated product surfaces.
- Same-category placement is treated as proof of direct competition.
- A valid threat to one component is expanded into a claim about the whole product, moat, revenue
  ceiling, or strategic window.
- Several reviewers repeat one reference and the artifact treats the repetition as independent
  confirmation.

## Downstream Re-evaluation

For every invalid or narrowed comparison, identify which conclusions depend on it:

| Downstream claim | Review question |
|---|---|
| Competitor set | Does the offering still compete for the same job and customer decision? |
| Moat | Which exact component is affected, and which sources of value remain? |
| Market size | Was the reference used to define buyers, spend, adoption, or a valuation multiple? |
| Revenue | Do both offerings monetize the same unit of value? |
| Timing | Does the cited product actually accelerate replacement, or only lower an input cost? |
| Positioning | Can the claim be expressed using specific product capabilities rather than broad labels? |

Recalculate or retract only the portions whose support fails. Preserve independently supported
portions and label unresolved ones.

## Over-Extrapolation Quarantine

When a comparison identifies a real threat to one component, isolate that component before changing
the broader thesis:

1. Name the exact surface, workflow, or source of value affected.
2. Remove only that component from the original thesis.
3. Re-evaluate the remaining customer job, workflow, data, distribution, service, and switching
   claims using independent evidence.
4. Narrow the moat, revenue, or timing conclusion only to the portion whose support fails.

Do not treat repeated cross-category references as independent votes. If every supporting reference
depends on the same questionable comparison, lower the claim's weight until same-job replacement
evidence is available.

## Review Output

Add one row per claim:

| Original claim | Compared surfaces | Primary verdict | Secondary relationships | Evidence quality | Decision | Required correction |
|---|---|---|---|---|---|---|

Use these decision values:

- `accept`: the comparison and its scope are supported.
- `narrow`: a limited comparison is supported but the original claim overreaches.
- `reject`: the comparison is a category error or lacks a meaningful comparison purpose.
- `unresolved`: material current facts or customer evidence are missing.

After the table, summarize:

1. which downstream conclusions remain unchanged;
2. which conclusions must be narrowed, recalculated, or removed; and
3. which missing evidence would change the result.

## Privacy And Attribution

Preserve source labels and redaction already present in the supplied material. If reviewers are
identified only as `Voice 1`, `Voice 2`, and so on, keep those labels and do not infer vendors or
models. This skill adds no new source-identity policy; it carries the input's existing privacy
boundary through the review.

## Completion Checklist

- The subject and every reference name a specific offering rather than only a company.
- Every offering has buyer, user, job, budget, value-capture, and integration notes.
- Every comparison has one relationship verdict and an explicit scope.
- Vertical integration is evaluated at the overlapping product surface.
- Repeated references are not counted as independent evidence without distinct reasoning.
- Invalid comparisons are traced to downstream conclusions.
- Component-level threats are not expanded into unsupported whole-thesis conclusions.
- Affected components are quarantined and the remaining thesis is retested independently.
- Missing current product facts produce an unresolved result rather than a guessed classification.
