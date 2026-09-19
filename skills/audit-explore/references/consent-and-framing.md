# Consent And Framing

Read this file when `/audit-explore` activates. It owns P0, P1, and the complete client-only route.

## P0 Consent

Before any external call:

1. Inspect the current host capabilities and confirm that the Decision Engine device token is
   configured through the normal masked login or setup flow. Do not probe a provider directly.
2. Detect the conversation language as a BCP-47 tag. If it is `und-*` or genuinely ambiguous, ask
   which language the user wants before continuing.
3. Classify the proposed content as public, internal, confidential, and regulated. Treat personal,
   proprietary, restricted, and credential-bearing content as non-public even if those labels are
   not supplied by the user.
4. Secrets and credentials are never submitted. Regulated content is never transmitted externally;
   offer only the client-only route. By default, all other non-public content stays client-side. An
   exception requires a runtime-discoverable authoritative policy that permits the exact data class
   and names the required protections. If that policy is absent, unclear, or not applicable, remain
   client-side.
5. Before asking for authorization, explain the available paths: an external cross-vendor panel or
   a client-only exploration. For the external path, disclose the bounded content or data categories
   that will be sent, recipient categories, and any authoritative retention, reuse, regional, and
   protection terms available to the host. If required terms cannot be established, offer only the
   client-only path.
6. Show current cost and duration estimates only when the host or server exposes them. Also state the
   current metered status of every offered mode from authoritative host or server data. If cost,
   duration, or metering is unknown, say so and require consent to the uncertainty; do not invent or
   reuse fixed estimates. Recommend `deep` for high-stakes or irreversible decisions and `standard`
   for ordinary exploration. Offer only modes the host exposes.
7. Obtain explicit authorization for the external path and selected mode. Silence, prior use of the
   skill, or eagerness to continue is not authorization.

If authorization is declined or transmission is not permitted, offer the local-only route below.
Never imply that the user must use a panel to finish the exploration.

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

After the user approves the frame and external processing remains authorized, read
[hosted-exploration.md](hosted-exploration.md). Otherwise continue with the Client-Only Route below.

## Client-Only Route

When no external panel is authorized, preserve the same interaction gates without making a hosted
call:

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
