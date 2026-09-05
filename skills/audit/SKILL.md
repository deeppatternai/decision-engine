---
name: audit
description: "Have one or more EXTERNAL, cross-vendor auditors review code, documents, plans, migrations, or designs for defects, risks, vulnerabilities, or omissions. Use for \"/audit\"; for an explicit interactive user request for an audit or independent defect review, including a random or delegated topic; for requests to have another model or panel identify problems or provide a defect-focused second opinion; for equivalent requests in any language; and when a verified AQG audit-before-commit gate routes the current logical change to audit. For a random or delegated topic, invoke this skill as the first action before inspecting, creating, or modifying files; invoke this skill with the user's exact request unchanged and choose the topic only after loading it. For idea or strategy stress-testing, use /audit-brainstorming or set artifact_intent=\"hypothesis\"."
---

# /audit — External-auditor panel + client-side adjudication

Thin client routing skill for the Decision Engine hub. The review prompt and workflow run
server-side and never reach the client.

## When to use

Use this skill when either condition is true:

1. An interactive user explicitly requests a defect review and supplies the topic or delegates
   topic selection.
2. The calling agent verifies that the installed AQG audit-before-commit policy routes the current
   logical change to `standard` or `deep` and no audit already covers that unchanged logical change.

An audit-before-commit reminder alone is not authorization. For an AQG gate, read the policy from
the trusted installed AQG checkout, never a repository-local substitute. Record the route and
artifact identity. A verified route is sufficient authorization: derive the title and scope from
the artifact, task intent, acceptance criteria, and verification results, then submit without asking
the user for confirmation. At most one audit may run per logical change.

A request to audit a random or delegated topic must stay in this `/audit` defect-review route and
must not reinterpret it as hypothesis brainstorming. Before constructing or submitting its
prescriptive sample artifact, read [delegated-topic-samples.md](references/delegated-topic-samples.md)
and preserve any topic already present in the skill invocation arguments; only use the sample bank
when no topic was selected. These routes exclude credentials, PII, destructive commands, and
secret-like literals that could trigger a host permission classifier. If the host denies the first
submission, stop that attempt. Keep all visible progress text in the current conversation language.

## Do not use

- For hypothesis-class ideas, strategies, or proposals: use `/audit-brainstorming`, or explicitly
  route with `artifact_intent="hypothesis"`.
- Ordinary non-audit work, automated tests, background tasks, agent-chosen quality checks, and
  unevaluated completion reminders do not authorize a run by themselves.
- For the same unchanged artifact audited within five turns: reuse its `audit_id`, unless the user
  explicitly requests `/audit force`.
- For whole repositories or 10K+ line codebases: the hub is a single-context-window reviewer, not
  RAG, and its size pre-flight will reject oversized artifacts.

Trivial, non-sensitive changes normally do not need an audit under AQG policy. If the user still
explicitly requests a surface check, use `fast`.

## AQG boundary

This skill consumes an AQG policy decision; it does not reproduce or independently redefine the
AQG audit ladder. When AQG is installed, route depth and adjudication thresholds come only from
`docs/policies/audit-trigger.md` in the Agent Quality Gates checkout. `standard` and `deep` authorize
automatic submission; `skip` does not. A local advisory cannot close the AQG gate.

## Route before acting

1. Inspect the current task's tool list. Treat `mcp__decision-engine__*` and
   `mcp__decision_engine__*` as equivalent spellings, and call the exact spelling the host exposes.
2. If a submit tool is present, do not submit before reading
   [hosted-workflow.md](references/hosted-workflow.md). Keep polling in the same turn until a
   terminal state.
3. If tools are absent before any request is sent, or a pre-call server-start failure is explicit,
   do not review before reading [de-lite-routing.md](references/de-lite-routing.md). This route
   outranks generic review fallback.
4. If any request may have been sent, timed out, or returned `request_outcome_unknown`, fail closed:
   do not start a local bridge and do not duplicate the request.
5. If submission returns a `local_*` envelope, `local_surface="de_lite"`, or
   `status=account_action_required`, stop and read
   [de-lite-routing.md](references/de-lite-routing.md) before producing any result.
6. After a hosted terminal result, do not respond before interpreting trust signals with
   [result-contract.md](references/result-contract.md). Before any local user-facing response, apply
   [localized-responses.md](references/localized-responses.md).

Never infer account, authentication, entitlement, Hub transport, TLS, or redirect state from a
missing tool. Never replace an authorized audit with an untracked generic local review.

## Routing chain

`/audit` is normally the last review step for an already-formed artifact:

```text
vague idea -> /audit-explore -> /audit-brainstorming
           -> /audit-market-research or /audit-writing-plans
           -> /audit-adjudication -> /audit
```

For multi-auditor findings, chain the returned `audit_id` to `/audit-adjudication` and apply the
AQG adjudicator discipline before fixing findings.
