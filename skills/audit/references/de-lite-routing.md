# DE Lite routing contract

Read this file only for an authorized interactive defect-review when hosted Decision Engine tools
are absent before any request, an explicit server-start failure occurs before dispatch, or the
server returns a supported local-advisory contract. The hosted MCP path remains primary.

## Authorization and no-replay gate

An explicit interactive user request or a verified AQG audit-before-commit gate is sufficient
authorization for one audit. For an AQG gate, derive the topic from the staged diff when available,
otherwise the working-tree diff or named artifact, plus task intent, acceptance criteria, and
verification results. Continue without asking for a second confirmation.

After authorization, inspect the current task tool list before reading or selecting any generic
review skill. Missing MCP tools are conclusive pre-call evidence only for the current audit attempt.
Local fallback is strictly pre-dispatch. If a request may already have been emitted, timed out, or
returned `request_outcome_unknown`, you must not call the bridge: retain its run ID and reconcile the
hosted request until its outcome is known. Do not infer an account, authentication, entitlement,
transport, TLS, or redirect state from tool absence. Do not use `.system/audit`,
`.system/review-agent`, brainstorming, or a generic review fallback.

## MCP-unavailable host bridge

This bridge is Codex-only unless another host explicitly installs an equivalent routing contract.
Use it only in an interactive session after establishing tool absence before any MCP invocation.
If the caller is headless, do not start the bridge. The same applies in CI, cron, or detached
sessions: surface the unavailable hosted path and leave the audit gate open.

Set `AUDIT_TITLE` from the user's topic or the AQG-derived topic and set `UI_LOCALE` from the current
conversation (`zh-CN` or `en-US`). Run:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/audit/scripts/de_lite_local_bridge.py" begin \
  --title "$AUDIT_TITLE" --ui-locale "$UI_LOCALE"
```

Do not emit any review text before the bridge returns a `local_host_*` ID.

`begin` accepts no artifact, endpoint, token, account state, or arbitrary path. It returns one
`local_host_*` ID with `degrade_reason=mcp_unavailable`, `audit_id:null`,
`fallback_mode=session-llm`, and `advisory_only:true`. The native Stopper keeps the authorized audit title
and displays `DE Lite · 本地审核中 · MCP 不可用`. The bridge never contacts the Hub.

Complete the local five-dimension advisory and then use the exact returned ID. First use installed
AQG `aqg-multi-review new` only for its five-dimension focus prompts and ledger skeleton. Do not
dispatch cross-LLM work or call `de_audit`. The current session model fills all five dimensions with
`audit_id:null` and `needs-cross-llm-rerun`, then runs `validate` without triggering Hub access. Use
`completed` only after ledger validation succeeds; use `partial` only for genuinely incomplete
review work and `failed` for execution or validation failure.

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/audit/scripts/de_lite_local_bridge.py" complete \
  --local-id "$LOCAL_ID" --status completed
```

If AQG or the bridge is unavailable, fail an already-started run when possible and hard stop; never
replace it with an untracked text review.

## DE Lite / unactivated authorized audit

The unactivated interactive session's local `audit_skill_submit` path applies only to this defect
review skill and never contacts the Hub.

- When the submit schema accepts only `audit` with `artifact_intent="prescriptive"`, submit that
  contract directly. Do not call `activation_required`; it does not unlock or repair DE Lite.
- Submit `skill_name="audit"`, the artifact in `args.content`, `artifact_intent="prescriptive"`, and
  the current-session `ui_locale`. `accept_degrade=true` may remain for compatibility but is not
  required. Do not ask for another confirmation. Never use this path for reminders, CI, cron,
  detached agents, or non-audit intents.
- The returned `local_*` envelope has `local_surface="de_lite"`, `audit_id=null`,
  `fallback_mode="session-llm"`, and `advisory_only=true`. It opens the normal stopper, does not run a
  hidden model, and does not echo artifact bytes.
- Use installed AQG `aqg-multi-review new` only for its five-dimension focus prompts and ledger skeleton.
  Do not follow cross-LLM dispatch and do not call `de_audit`. The current session model
  fills all dimensions. Every finding uses `audit_id:null`; the ledger decision is
  `needs-cross-llm-rerun` with a reason that the local advisory cannot close the gate. Run `validate`;
  its rerun/judgement signal must not trigger Hub access.
- When all five review dimensions are complete and ledger `validate` succeeds, call
  `audit_skill_complete(local_id=<the returned local_* id>, status=completed)` and must use
  `completed`; output sanitization, the single-model capability limit, reference-only treatment, and
  the fact that it cannot close the gate are not reasons to use `partial`. `partial` is allowed only
  when review work, one or more dimensions, or the ledger is actually incomplete. Use `failed` for
  execution or validation failure. The completion call is local-only and cannot reopen a terminal
  run; the watchdog is abandoned-run cleanup, not success.
- Apply the user-response boundary in `localized-responses.md`. An unactivated result cannot claim a
  cross-vendor PASS/clean verdict or close an AQG gate. Activated routing and token-present auth
  failures do not enter this branch.

## Degraded / hub-unreachable and entitlement-blocked (advisory-only)

`/audit` defect review is the only skill with a local fallback, because it is on the AQG critical
path. This section describes routing shape only; role prompts and adjudication logic stay in AQG.

- **Authorized interactive audit is sufficient consent.** A pre-dispatch unreachable Hub, or a
  server-authoritative `subscription_expired`, `credits_exhausted`, or `rate_limited` contract,
  enters an advisory-only local run after an explicit user request or verified AQG gate without a
  second confirmation. The low-level `de_audit` compatibility tool still requires
  `accept_degrade=true`; WITHOUT that flag it returns an `isError` hard stop and never starts a local
  run. The local response includes the downgrade reason and never a hosted `audit_id`. Post-dispatch
  timeout, unknown outcome, or transport ambiguity never enters this branch; retain and reconcile
  the hosted run ID instead.
- **Entitlement downgrade is contract-based.** Require
  `status=account_action_required`, `local_advisory_available=true`, and one allow-listed pair:
  `subscription_expired/renew_subscription`, `credits_exhausted/add_credits`, or
  `rate_limited/wait_or_retry`. Unknown or malformed reasons remain hard errors.
- **Received authenticated Hosted responses use the literal balance marker.** For any response
  received from an authenticated Hosted MCP submit or a wait/status/result/events follow-up, the
  literal `insufficient_balance` marker is sufficient evidence to enter the existing local advisory
  with `credits_exhausted/add_credits` exactly once. The client performs one local advisory conversion
  and never submits Hosted again. Do not match language, wrapper shape, run IDs, or
  `isError`. A `timeout`, `reset`, `5xx`, `401`, `TLS/redirect`, or otherwise unknown outcome
  remains fail-closed/no-replay because it is not a received server result.
- **Headless: never offer or run the local fallback.** In cron, CI, detached, or non-interactive
  sessions, do not offer and do not run a local read; surface the plain offline error or account
  error. A verified AQG gate authorizes the hosted path, not an interactive local read.
- **For an authorized interactive request**, use AQG only for its five-dimension focus prompts and
  ledger skeleton; the current session fills them. Do not dispatch cross-LLM work and do not call
  `de_audit`. Keep `audit_id:null`, `fallback_mode:session-llm`, and a
  `needs-cross-llm-rerun` decision stating that the local advisory cannot close the gate. Validate
  without triggering Hub access, then complete the returned local ID as `completed`, `partial`, or
  `failed` according to actual completeness. If the request began with the low-level `de_audit`
  degraded marker, explain the real reason and obtain explicit consent before calling the high-level
  `audit_skill_submit`, unless a verified AQG gate already authorized the run; then continue without
  another prompt. Never seed a local row directly.
- **Advisory only.** A local single-model read is not a cross-vendor audit: it never closes a gate
  and never carries a `clean` or `pass` verdict. A native Stopper green `✓` means execution completion only, never
  audit approval or permission to commit or merge. Label it "not audited by a real panel — local
  advisory"; the human or platform retains the commit/merge decision.
