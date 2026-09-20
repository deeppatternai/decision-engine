# Consent And Framing

Read this file when `/audit-explore` activates. It owns P0, P1, and the complete client-only route.

## P0 Route And Sensitive-Content Consent

1. Inspect the current host for a Decision Engine submit tool. Do not probe a provider directly or
   require a separate policy, pricing, or panel-composition query. The MCP tool handles device
   activation; if it reports an activation error, explain it rather than presenting client-generated
   work as a panel.
2. Detect the conversation language as a BCP-47 tag. If it is `und-*` or genuinely ambiguous, ask
   which language the user wants before continuing.
3. Classify every outgoing argument that contains user-derived information, including `title`,
   `content`, `context`, and `domain`. Apply these routes in order; the first matching route wins:
   - **Prohibited:** Secrets and credentials are never submitted. Regulated content is never
     transmitted externally. Remove medical or biometric records, another person's identifiers
     without their permission, and material under a confidentiality duty. A combination of details
     that identifies a third party without their permission also needs redaction. If safe redaction
     would lose the point, stay local. Ask the user to approve the edited frame before sending it.
   - **Sensitive but shareable:** Non-public plans, precise business figures, and first-party
     identifiers (such as the user's name, phone number, or exact address) that are not in the
     prohibited class. Also include contextual detail that could expose a user or third party
     without identifying an unconsenting third party. Display the complete exact user-derived
     payload to be sent,
     including repeated frame material and selected options, and the recipient categories (Decision
     Engine and its external panel providers). Obtain explicit authorization for that complete
     payload. Any change to a sensitive payload requires a new approval. State known handling terms;
     mark unknown retention, reuse, region, protection, cost, or duration as unknown rather than
     inventing them. Pending is not a local-only decision: wait while doing safe work.
   - **Routine:** Public or generic ideas and routine personal planning context that does not match
     either route above. An approximate budget and broad location without identifying details can
     be routine. Do not treat an idea as sensitive solely because it is personal; classify by whether
     the content could expose the user or a third party. Ordinary use proceeds without a separate
     consent prompt when the user requests this exploration.
   Mixed content takes the most restrictive route. If uncertain between Routine and Sensitive,
   treat it as Sensitive and wait for approval. If it may be Prohibited, remove the suspected content
   or keep it local; uncertainty never authorizes submission or silently chooses local-only. Model
   classification is a judgment, not a guarantee.

   Examples: "about 150,000 to take over a neighborhood cafe" is Routine; exact turnover and rent
   of an unnamed business is Sensitive; the user's own exact address is Sensitive and requires
   exact-payload approval; passwords, patient records, and an unconsenting third party's phone
   number are Prohibited.
4. Use `standard` for routine exploration and `deep` for high-stakes or irreversible decisions.
   Use `fast` only when the user explicitly asks for a quick pass and the host supports it. Honor an
   explicit supported choice and pass it as `audit_mode` in P2, P3, and P4. Before the first hosted
   submission, state the selected mode and any known metering or cost. If unknown, say so once; do
   not invent fixed estimates or require a new reply for Routine content. If activation was inferred
   from the user's vague-idea request rather than an explicit `/audit-explore`, state once that the
   panel is external before submitting. Do not require a server query for these disclosures.
5. Honor an explicit client-only choice. If sensitive content is declined, or prohibited content
   cannot be safely redacted, explain the reason and offer the local-only route.

The route has three decision states: ready for hosted submission, sensitive approval pending, and
client-only by user choice or a concrete blocker. Only the last state enters the local-only route.

## P1 Frame

Conduct a 5-Whys and Jobs-to-be-Done interview one question at a time. Establish:

- the problem or opportunity;
- why it matters now;
- who experiences it and in what situation;
- the desired observable outcome;
- the cost of inaction and important constraints.

Express the core job as: `When [situation], I want to [motivation], so I can [outcome]`.

Build a frame artifact containing the original idea, problem statement, target user, context,
desired outcome, constraints, assumptions already stated by the user, and the JTBD sentence. Show it
to the user. If the user supplies corrections, revise and show the frame again. Continue only after
the user explicitly approves the revised frame.

After the user approves the frame, classify that exact frame. A routine frame proceeds to
[hosted-exploration.md](hosted-exploration.md) when the submit tool is available. For a sensitive
frame, get approval of the complete exact payload first. A pending approval stays pending; it must not
fall through to the Client-Only Route below.

## Client-Only Route

When the user chooses client-only, a submit tool is unavailable, or the payload is prohibited and
cannot be safely redacted, preserve the same interaction gates without making a hosted call:

1. Run client-only problem divergence and generate 9 HMW reframes plus 3 PO provocations. Label the
   candidates as client-generated and ask the user to choose one.
2. Run client-only solution divergence for the selected reframe and generate 9 solution directions.
   Ask the user to choose one or two.
3. Run client-only convergence and falsification. Draft the hypothesis, apply a Klein premortem, and
   check for at least three falsification criteria, three assumptions, a null hypothesis or default,
   and a relevant base rate or reference class.
4. Produce the `frame`, `candidates`, `formed_hypothesis`, `panel_participation`, and
   `next_skill_handoff` exit fields, and label the result `local-only`. Set `panel_participation` to
   an explicit no-panel value. Do not fabricate panel convergence, server trust signals,
   `goldilocks_pass`, or server-authored `exit_ready`.
5. Show the result to the user and require the same explicit confirmation before any downstream
   skill handoff.

The local route is a useful structured exploration, not a substitute claim that an external panel
participated. It is self-contained: do not read `hosted-exploration.md` or
`convergence-and-result.md` while following this route.
