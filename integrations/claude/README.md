# Claude Wrapper

`audit-hub.sh` lets Claude submit artifacts to the Decision Engine hub while
selecting the Claude-use mirror profile. That means Claude's work is reviewed by
frontier models from other vendors — not by Claude itself — with additional
vendors joining the committee at higher audit depths. Submitted artifacts are
sent to those third-party model providers. The hub returns a Markdown report;
bring it back into the conversation for adjudication.

Smoke test:

```bash
integrations/claude/audit-hub.sh \
  --profile fast_smoke \
  --text "connectivity smoke"
```

Audit piped content:

```bash
git diff | integrations/claude/audit-hub.sh \
  --title "Claude requested diff audit" \
  --context "Review this patch from Claude." \
  --focus "bugs, regressions, security, tests"
```

Environment overrides:

```bash
AUDIT_HUB_AUDIT_MODE=dual integrations/claude/audit-hub.sh --text "..."
AUDIT_HUB_TIER=deep integrations/claude/audit-hub.sh --file /absolute/path
```
