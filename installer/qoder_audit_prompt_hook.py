"""Inject deterministic Qoder routing context for explicit audit prompts."""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from typing import Any, BinaryIO, TextIO


_MAX_INPUT_BYTES = 64 * 1024
_AUDIT_PREFIX = re.compile(
    r"^(?:(?:please|kindly)\s+)?(?:(?:deeply|thoroughly)\s+)?audit\b"
    r"|^(?:请)?(?:深度|深入)?(?:审计|审核)",
    re.IGNORECASE,
)
_ROUTING_CONTEXT = (
    "Decision Engine routing requirement: this user prompt explicitly requests an audit. "
    "Invoke Skill `audit` now as the first action and pass the user's exact request unchanged. "
    "Do not inspect the workspace, create or modify files, search the web, invoke "
    "`audit-brainstorming`, or answer with an inline review before Skill `audit` is loaded. "
    "After the skill is loaded, Qoder must invoke Decision Engine through the generic "
    "`mcp_call` tool. The submit call must have exactly this wrapper shape: "
    "{\"toolName\":\"mcp__decision_engine__audit_skill_submit\","
    "\"arguments\":{\"skill_name\":\"audit\",\"args\":{...}}}. "
    "`arguments` must be an object, not a JSON string. Do not call any "
    "`mcp__decision_engine__*` name directly. Never call `activation_required`; an "
    "unactivated audit is handled by `audit_skill_submit`. Submit exactly once. After any "
    "rejected or failed submission, stop without retrying another Decision Engine tool. "
    "The only permitted MCP target names in this workflow are "
    "`mcp__decision_engine__audit_skill_submit` and "
    "`mcp__decision_engine__audit_skill_complete`. Do not invoke or invent an AQG MCP tool; "
    "load the installed AQG skill when the audit instructions require it. Call complete only "
    "after submit returns a non-empty `local_id`, with exactly this wrapper shape: "
    "{\"toolName\":\"mcp__decision_engine__audit_skill_complete\","
    "\"arguments\":{\"local_id\":\"<returned local_* id>\","
    "\"status\":\"completed\"}}. Never use `run_id` in the completion arguments."
)


def _read_payload(stream: BinaryIO) -> dict[str, Any] | None:
    raw = stream.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_explicit_audit_prompt(prompt: object) -> bool:
    if not isinstance(prompt, str):
        return False
    normalized = unicodedata.normalize("NFKC", prompt).strip()
    return bool(_AUDIT_PREFIX.match(normalized))


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
