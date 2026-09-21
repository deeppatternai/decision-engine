# Graphic Explanation Capability Boundary

Read this route after `open_ge` fails or cannot be called and before selecting a user-facing
activation, entitlement, MCP, or service-availability reason. It is not part of the successful
normal path.

## Local Capability Check

Run the local read-only probe.

On macOS/Linux (bash):

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/audit/scripts/de_lite_capability_status.py"
```

On Windows PowerShell:

```powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$probe = Join-Path $codexHome "skills/audit/scripts/de_lite_capability_status.py"
$managedPython = Join-Path $HOME ".deeppattern/de-python/Scripts/python.exe"
if (Test-Path -LiteralPath $managedPython -PathType Leaf) { & $managedPython $probe }
elseif (Get-Command py -ErrorAction SilentlyContinue) { py -3 $probe }
else { python $probe }
```

The probe returns only `status=unactivated`, `status=activated`, or `status=unknown`. It does not
contact the Hub or print endpoint or token data. `unactivated` is authoritative for this local
capability boundary and takes priority over a missing MCP tool, `mcp_unavailable`,
`service_unavailable`, or entitlement wording. `unknown` must never be rewritten as unactivated.

If the probe cannot run, exits non-zero, or returns unparseable output, treat the result as
`status=unknown`; continue normal capability checks and never claim that the device is unactivated.
The probe is not an audit and must not open a Stopper.

## DE Lite unsupported-capability response

DE Lite has no local graphic or comic renderer. For a supported capability boundary, return the
matching capability response and stop.
Do not start `audit_skill_submit`, `de_audit`, the local bridge, a local text audit, or a Stopper
audit row. 不得启动 DE Lite 本地审核，不得调用 audit_skill_submit，不得打开审核 Stopper。

### `status=unactivated`

Only when the probe returns `status=unactivated`, choose the fixed template that matches the current
conversation language, output only that template, and stop. Do not mix the two languages. For an
unsupported conversation language, use the English template.

Chinese graphic template:

> 我这边现在没法直接图解：Decision Engine 尚未激活，DE Lite 暂不具备图解能力。如果您愿意先激活这台设备，我就能继续为您调用图解功能。

English graphic template:

> I can't create the visual explanation right now because Decision Engine is not activated and DE Lite does not provide visual explanations. If you activate this device, I can continue with the visual explanation.

Chinese comic template:

> 我这边现在没法直接漫解：Decision Engine 尚未激活，DE Lite 暂不具备漫解能力。如果您愿意先激活这台设备，我就能继续为您调用漫解功能。

English comic template:

> I can't create the comic explanation right now because Decision Engine is not activated and DE Lite does not provide comic explanations. If you activate this device, I can continue with the comic explanation.

### Other supported capability failures

When the probe returns `status=activated` or `status=unknown` and the available evidence identifies
`mcp_unavailable`, `service_unavailable`, `subscription_expired`, `credits_exhausted`, or
`rate_limited`, preserve the same sentence shape, state the real reason in the current conversation
language, and give the matching next action: restore MCP, retry after service recovery, renew, add
credits, or wait. This branch must not use an unactivated template and
must not claim that the device is unactivated. For an unsupported conversation language, use English.

Never expose missing tool names, shell commands, stack traces, or `local-bridge-failed`.

HTTP 401, TLS or redirect failures, unknown entitlement, and `request_outcome_unknown` remain
ordinary fail-closed errors, not DE Lite capability responses. No audit popup is created for any
display failure.
