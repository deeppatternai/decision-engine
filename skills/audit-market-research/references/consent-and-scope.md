# Consent, Scope, and Client-Only Path

Read this file when the market-research skill activates. It owns P0 Consent, P1 Scope, tier
selection, and the full regulated client-only path.

## P0 Consent

1. Inspect the host for the exact market-research lifecycle tools it exposes. A configured device
   token is a normal masked login or setup state; do not probe model providers directly.
2. Detect the user's language as a BCP-47 tag. If the result is `und-*` or ambiguous, ask instead of
   silently choosing a language.
3. Classify the proposed payload as one of four classes: public, internal, confidential, and
   regulated. Treat personal, proprietary, restricted, and credential-bearing material as
   non-public.
4. Secrets and credentials are never submitted. Regulated content remains client-only. Other
   non-public content stays local unless a runtime-discoverable authoritative policy covers the
   exact data class and the user gives explicit authorization.
5. Before authorization, explain the local and hosted paths. For a hosted path, disclose the bounded
   data categories, recipient category, and any authoritative retention, reuse, regional, or
   protection terms available at runtime. If required terms are unavailable, offer only the local
   path for non-public content.
6. Report current cost, metered status, and expected duration only from current host or server data.
   If any value is unknown, say so and require consent to that uncertainty. Do not hard-code prices
   or duration estimates.
7. If the topic concerns an AI vendor, model provider, or another subject where panel members may
   have commercial self-interest, disclose that convergence can amplify shared incentives.
8. Before hosted authorization, explain that the run will use the comparison path even for other
   research topics. A source selected only by another research class may be omitted, and synthesis
   may include a comparison view.
9. Get an explicit path and tier choice. Silence, prior use, or enthusiasm is not authorization.

## Tier Choice

Use only modes the current host exposes.

| Choice | Meaning |
|---|---|
| Regulated local | No server retrieval or panel call; produce a generic degraded brief locally |
| Quick | Mock P2 fact pack only; stop after the evidence brief |
| Deep | Real mainstream retrieval, P3 analysis, conditional mock P4, and P5 synthesis |
| Premium | Expanded real retrieval, P3 analysis, conditional real P4, and P5 synthesis |

Deep is real retrieval. Quick is the only tier whose P2 fact pack is intentionally mock. Premium
widens the source set and may use paid, professional, regional, or agentic retrieval sources.

## P1 Scope

Accept one of these starting forms:

- direct text;
- a prior audit or exploration identifier;
- a URL;
- a topic or research question.

Clarify only what is needed to create one stable research envelope. Ask one question at a time when
the user has not supplied enough information.

The scope fields are:

| Field | Meaning |
|---|---|
| `research_type` | Set the hosted P1 scope to the fixed literal `comparison` and reuse it on P2-P5; never use a localized description or another category value. The regulated local path does not submit this field |
| `research_method` | The intended research method or evidence approach |
| `language` | The output and research language as BCP-47 |
| `question` | The decision-relevant question and, when relevant, the actual research category the report must address |
| `topic` | The market, actual research category, customer group, geography, or commercial subject |
| `framework_hints` | User-supplied scope metadata; never convert it into P3 methodology-lens injection |
| `mode` | Quick, Deep, or Premium for hosted runs |

Pass the same scope fields on every server submission. Preserve explicit unset values rather than
silently inventing defaults. The server computes `scope_sha` and rejects downstream artifacts whose
device, mode, phase, scope, run state, or digest does not match.

The server's dedicated `consumer_voc` source route is no longer forced by this field. Put the
actual research category in `question` or `topic` and include it in the P2 retrieval query.
Keyword-based routing may select relevant sources, but does not guarantee the dedicated route.

If an older run chain used a different `research_type`, do not change the value partway through that
chain. Before starting a new P2 run with `research_type="comparison"`, repeat the applicable P0
cost and path disclosure and obtain approval for the new P1 scope. Use only the new chain's pointers
for P3-P5.

Show the scope to the user and obtain approval before P2. A material change to the question, topic,
language, method, framework hints, or mode creates a new scope and new run chain.

## Regulated Client-Only Route

For regulated content, do not call the server at any phase.

1. Keep the user's sensitive details local.
2. Produce a generic degraded brief using only permitted local reasoning and user-provided facts.
3. Separate known facts, assumptions, open questions, possible evidence sources, risks, and next
   validation steps.
4. Label the result `regulated_local` and state that no hosted retrieval, panel analysis, artifact
   binding, or server trust tier was performed.
5. Do not invent citations, panel participation, convergence, `artifact_sha`, `scope_sha`, or a
   server-authored trust tier.
6. Present the brief in `scope.language` and ask before any downstream action.

This is a useful planning brief, not a validated market-research result.

## Deduplication

If the same question and scope appeared within the last five turns without material change, reuse
and re-render the existing result. Do not create another metered run merely to restate it.
