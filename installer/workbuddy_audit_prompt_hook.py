"""Inject deterministic WorkBuddy AI routing context for explicit audits."""

from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO, TextIO

try:
    from installer.audit_prompt_hook_common import (
        is_explicit_audit_prompt as _is_explicit_audit_prompt,
        read_payload as _read_payload,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"installer", "installer.audit_prompt_hook_common"}:
        raise
    from audit_prompt_hook_common import (
        is_explicit_audit_prompt as _is_explicit_audit_prompt,
        read_payload as _read_payload,
    )


_ROUTING_CONTEXT = (
    "Decision Engine routing requirement: this user prompt explicitly requests an audit. "
    "Invoke Skill `audit` now as the first action and pass the user's exact request unchanged. "
    "Do not inspect the workspace, create or modify files, search the web, invoke "
    "`audit-brainstorming`, or answer with an inline review before Skill `audit` is loaded. "
    "Use the tool from connector `decision-engine` whose logical name "
    "`audit_skill_submit` is exposed by this host. Never call `activation_required`; an "
    "unactivated audit is handled by `audit_skill_submit`. Submit exactly once. After any "
    "rejected or failed submission, stop without retrying another Decision Engine tool. "
    "Do not invoke or invent an AQG MCP tool; load the installed AQG skill when the audit "
    "instructions require it. Call the Decision Engine tool with logical name "
    "`audit_skill_complete` only after submit returns a non-empty `local_id`, passing exactly "
    "that `local_id` and `status=completed`. Attempt completion exactly once. If completion "
    "is rejected or fails, stop and report it instead of retrying. Never use `run_id` in "
    "completion arguments."
)


def route_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload or payload.get("hook_event_name") != "UserPromptSubmit":
        return {}
    if not _is_explicit_audit_prompt(payload.get("prompt")):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _ROUTING_CONTEXT,
        }
    }


def main(
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    source = stdin or sys.stdin.buffer
    destination = stdout or sys.stdout
    json.dump(route_payload(_read_payload(source)), destination, ensure_ascii=True)
    destination.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
