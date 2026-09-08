"""MCP-over-HTTP forwarding shim (public shell — transport only).

This is the entire "brain-less" client surface: a local stdio MCP server that an
agent (Claude Code / Codex / Cursor) connects to, which forwards every JSON-RPC message
verbatim to the hosted Decision Engine server's ``/mcp`` endpoint over HTTPS,
attaching the device's bearer token from ``config.json``.

Deliberately carries **no** intelligence (release design §10 / §14):

- It does not know the tool catalog — ``tools/list`` is *forwarded*, so the
  server (which holds the IP) is the single source of truth for what tools
  exist. The shell hardcodes no tool schemas, prompts, voice roster, layout
  definitions, or orchestration.
- It does not synthesize, adjudicate, or transform payloads — it moves bytes.
- Voice-privacy (server returns ``Voice 1..N`` unless debug-authorized) is a
  server property; the shim passes results through untouched.

Framing: newline-delimited JSON-RPC 2.0 messages on stdin/stdout (one JSON
object per line), matching the MCP stdio transport. Requests (with ``id``) get a
forwarded response; notifications (no ``id``) are forwarded fire-and-forget.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from http.client import HTTPConnection, HTTPException, HTTPSConnection, RemoteDisconnected
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, Request, build_opener

from client import i18n
from client.http_safety import NoRedirect, read_within_budget  # shared hub-read guards (stdlib leaf)

from . import mcp_config
from .client_host_runtime import (
    CapabilityStage,
    DisplayCheckResult,
    PRODUCTION_CURSOR_WINDOWS_FIXTURE,
    TransportCapabilityGate,
    VolatileResult,
    cursor_initialize_matches_fixture,
    run_cursor_windows_preflight,
)
from .config import (
    ShellError,
    de_config_path,
    load_json,
    managed_component_root,
    normalize_endpoint,
)

SHIM_USER_AGENT = "decision-engine-shell/0.1.0"
DEFAULT_TIMEOUT_S = 120
# How long the pre-flight connect probe waits before calling the hub unreachable. Generous enough
# for a slow-but-alive link (a healthy connect is milliseconds; this tolerates ~100x that), small
# enough that an offline user gets an answer immediately instead of a frozen session.
PROBE_TIMEOUT_S = 3.0


def hub_reachable(endpoint: str, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """Can we open a TCP connection to the hub right now?

    urllib applies ONE timeout to connect+read, so there is no way to say "give up fast if the host
    is down, but keep waiting if it's just thinking". Without this probe a dead network parks every
    forwarded call for the full request budget (DEFAULT_TIMEOUT_S = 120s) — and forward() runs on
    the transport thread, so the whole MCP surface freezes with it: the user's session stops
    responding for two minutes per call. A short connect probe splits the two cases: unreachable →
    fail now; reachable-but-slow → proceed on the normal budget, so a de_wait_audit long-poll may
    still legitimately take minutes.

    Fails OPEN: anything we can't parse or probe cleanly returns True and lets the real request
    decide — this must never invent an outage that isn't there.
    """
    split = urlsplit(endpoint)
    host = split.hostname
    if not host:
        return True
    try:
        port = split.port or (443 if split.scheme == "https" else 80)
    except ValueError:
        return True                       # unparseable port → not our call to make
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False                      # refused / unreachable / DNS failure / timed out


def offline_message(message: Dict[str, Any]) -> str:
    """What the caller is told when the probe says we're offline. The audit engine is server-side,
    so an outage on this machine does NOT kill work already accepted by the hub — say so, and say
    how to get it back, instead of a bare timeout that reads like the work was lost."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    name = params.get("name") if isinstance(params, dict) else None
    if name == "de_audit":
        tail = ("This audit was NOT submitted — nothing is running for it. Re-issue it once the "
                "connection is back.")
    else:
        tail = ("Any audit already accepted by the hub keeps running there and is NOT lost — once "
                "the connection is back, collect it with de_wait_audit(audit_id).")
    return ("Decision Engine hub unreachable — no request was sent (checked in %.0fs; the network "
            "is down or the hub is not answering). %s" % (PROBE_TIMEOUT_S, tail))


# de_audit intents that map to the /audit defect-review path — the ONLY intent on the AQG
# critical gate, hence the only one offered a local advisory fallback when the hub is offline
# (see internal design notes, local-degraded-audit §11). Absent artifact_intent defaults to
# prescriptive server-side, so a de_audit call with no intent counts as defect review too.
_DEFECT_REVIEW_INTENTS = frozenset({"prescriptive"})
_DEFAULT_INTENT = "prescriptive"

_MCP_TOOL_NAME_PREFIXES = (
    "mcp__decision-engine__",
    "mcp__decision_engine__",
)


def _logical_mcp_tool_name(value: Any) -> Any:
    """Normalize host-qualified MCP names while leaving unrelated names untouched."""
    if not isinstance(value, str):
        return value
    for prefix in _MCP_TOOL_NAME_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


class ActivationRequiredError(ShellError):
    """Config state requiring device activation before hosted forwarding."""

    def __init__(self, message: str, *, reason_code: str = "activation_required"):
        super().__init__(message)
        self.reason_code = reason_code


class OfflineError(ShellError):
    """Hub-unreachable ShellError that also carries structured JSON-RPC error ``data``.

    A plain offline ShellError stays a hard stop (fail-safe: a skill-less caller sees the
    message and gives up). When the offline call is an opted-in de_audit defect review, ``data`` also
    carries a small ``{reason, local_advisory_available}`` marker so an AQG-aware caller can
    offer a LOCAL advisory sanity read. The advisory never closes an audit gate, so surfacing
    this soft marker on an error opens no fail-open path — the call is still a -32001 error."""

    def __init__(self, message: str, data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.data = data


# One sentence for every "the hub may be holding work we cannot account for" answer, so the
# transport-level unknown and the session-level in-flight guard cannot drift apart.
_OUTCOME_UNKNOWN_MESSAGE = "Decision Engine request outcome unknown — reconcile before retrying"


class OutcomeUnknownError(ShellError):
    """The request crossed the wire, but no trustworthy final response was received."""

    def __init__(self, reason: str, request_id: str):
        super().__init__(_OUTCOME_UNKNOWN_MESSAGE)
        self.data = {
            "status": "request_outcome_unknown",
            "reason": reason,
            "request_id": request_id,
            "request_sent": True,
            "retryable": False,
            "action": "reconcile",
        }


def _local_advisory_offer(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Structured error ``data`` marking that a local advisory read is available for THIS call.

    Returns the marker only for a de_audit defect-review ``tools/call`` (``artifact_intent``
    prescriptive or absent) whose caller OPTED IN with ``accept_degrade=true``; every other
    method / tool / intent, and every call without the opt-in, gets ``None`` and today's plain
    offline error (§11). ``local_advisory_available`` is a presence flag — it means "an offer is
    permitted for this intent", NOT that a local executor is confirmed installed on the caller.
    Fails CLOSED on any malformed shape and NEVER raises (a raise here would escape the serve
    loop's ``except ShellError`` and crash the transport). Status enum only — red-line clean."""
    if message.get("method") != "tools/call":            # scope to real tool calls, not any name-bearer
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if params.get("name") != "de_audit":
        return None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return None                                      # no/mis-shaped arguments -> no opt-in to read
    title = arguments.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    # The opt-in the routing prose promises callers: WITHOUT accept_degrade=true an unreachable
    # hub is the plain offline hard stop, so a caller that does not understand degradation gets
    # the safe behaviour. Identity against True, not truthiness: JSON-RPC has a real boolean, so
    # "true" / 1 / ["yes"] is a caller bug and must fail closed rather than be guessed at.
    if arguments.get("accept_degrade") is not True:
        return None
    # Absent key -> the server-side default (prescriptive, i.e. defect review). A key that is
    # PRESENT but null is not absence: it is a malformed value, and per the fail-closed rule it
    # gets no marker (audit d6d63118 f1). str-guard BEFORE the frozenset membership: an
    # unhashable intent (list/dict) would else raise TypeError inside the serve loop.
    intent = arguments.get("artifact_intent", _DEFAULT_INTENT)
    if not isinstance(intent, str) or intent not in _DEFECT_REVIEW_INTENTS:
        return None
    return {"reason": "unreachable", "local_advisory_available": True}

# Hosts for which plaintext http:// is tolerated (local dev only).
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_local_host(endpoint: str) -> bool:
    host = urlsplit(endpoint).hostname or ""
    return host in _LOCAL_HOSTS


def _https_context() -> Optional[ssl.SSLContext]:
    ca_file = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    try:
        import certifi  # type: ignore
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


# Hard ceiling on the GE artifact body we will read into memory — a compromised or buggy hub must
# not OOM the client. This is an OOM guard, NOT a content policy: the authoritative render limit is
# server-side and unknown to this shell, so the ceiling sits far above any plausible artifact (a
# comic/infographic is a few MB of base64) rather than trying to describe one. Same number and same
# threat model as launcher._MAX_BOARD_BYTES, the other authenticated hub fetch; a test pins the two
# together so they cannot drift apart.
_MAX_GE_ARTIFACT_BYTES = 64 * 1024 * 1024

# Hard ceiling on the /mcp JSON-RPC response body — the same OOM guard as the artifact cap above, but
# sized INDEPENDENTLY (PR#49 closeout flagged this: a /mcp body is consumed by the MODEL, so its ceiling
# is a separate decision, not an inherited 64 MB). This body never carries the raw popup bytes the GE
# fetch does — image bytes go out-of-band via /v1/visual/artifacts, board HTML via /db/render — it is only a
# JSON-RPC RESULT destined for model context. The largest such result is an audit-panel return (de_audit
# / de_wait_audit); the client-side skill docs in THIS repo put an audit INPUT at ≤ ~480 KB
# (skills/audit/references/hosted-workflow.md) and an adjudication payload at ≤ 2 MB
# (skills/audit-adjudication/SKILL.md).
# The hub's own authoritative response limit is server-side and NOT in this repo, so this is a DEFENSIVE
# ceiling chosen with margin over those indicative figures — comfortably above any plausible result, an
# eighth of the raw-artifact cap — not a hub-enforced number. It bounds memory; the transfer-TIME bound
# (a slow-stream hang shared with the sibling reads) is added by the shared read_within_budget helper.
# A test pins this strictly below the artifact cap so the two can't be silently equated.
_MAX_MCP_RESPONSE_BYTES = 8 * 1024 * 1024
# Diagnostics stay useful without becoming a second payload-processing pipeline or an unbounded
# runtime artifact. Counts beyond the scan cap are deliberately unknown and marked truncated.
_MAX_MCP_DIAGNOSTIC_SCAN_CHARS = 256 * 1024
_MAX_MCP_REQUEST_LOG_BYTES = 5 * 1024 * 1024
_CJK_CHARACTER_RE = re.compile(
    r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"
    r"\U00020000-\U0002FA1F\U00030000-\U000323AF]"
)
_SAFE_MCP_METHOD_LABELS = frozenset({
    "initialize",
    "notifications/initialized",
    "ping",
    "tools/call",
    "tools/list",
})
_SAFE_MCP_TOOL_LABELS = frozenset({
    "audit_skill_result",
    "audit_skill_status",
    "visual_render",
    "visual_status",
    "open_ge_popup",
})


def mcp_request_log_path() -> Path:
    """Privacy-safe client breadcrumbs written immediately before ``/mcp`` requests."""
    from client import runner

    return runner.runtime_logs_dir() / "mcp-requests.jsonl"


def mcp_request_log_lock_path() -> Path:
    from client import runner

    return runner.runtime_locks_dir() / "mcp-requests.lock"


def _diagnostic_text_summary(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        text = value
        kind = "string"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        kind = type(value).__name__
    scanned = text[:_MAX_MCP_DIAGNOSTIC_SCAN_CHARS]
    return {
        "kind": kind,
        "characters": len(text),
        "utf8_bytes": len(text.encode("utf-8")),
        "scanned_characters": len(scanned),
        "truncated": len(scanned) != len(text),
        "cjk_characters": len(_CJK_CHARACTER_RE.findall(scanned)),
        "ascii_question_marks": scanned.count("?"),
        "replacement_characters": scanned.count("\ufffd"),
    }


def _safe_rpc_id(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return "<string>"
    return "<%s>" % type(value).__name__


def _request_outcome_id() -> str:
    return secrets.token_hex(16)


class _RequestSendState:
    def __init__(self):
        self.sent = False


def _tracked_connection_factory(connection_type, send_state: _RequestSendState):
    def factory(host, **kwargs):
        connection = connection_type(host, **kwargs)
        original_endheaders = connection.endheaders

        def endheaders(message_body=None, *, encode_chunked=False):
            original_endheaders(message_body, encode_chunked=encode_chunked)
            send_state.sent = True

        connection.endheaders = endheaders
        return connection

    return factory


class _SendTrackingHTTPHandler(HTTPHandler):
    def __init__(self, send_state: _RequestSendState):
        super().__init__()
        self._send_state = send_state

    def http_open(self, request):
        return self.do_open(
            _tracked_connection_factory(HTTPConnection, self._send_state), request
        )


class _SendTrackingHTTPSHandler(HTTPSHandler):
    def __init__(self, send_state: _RequestSendState, *, context):
        super().__init__(context=context)
        self._send_state = send_state

    def https_open(self, request):
        return self.do_open(
            _tracked_connection_factory(HTTPSConnection, self._send_state),
            request,
            context=self._context,
        )


def _safe_protocol_label(value: Any, allowed: frozenset) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value if value in allowed else "<other>"


def _mcp_request_diagnostic(
    message: Dict[str, Any], data: bytes, request_id: Optional[str] = None
) -> Dict[str, Any]:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else None
    fields = {}
    if arguments is not None:
        for field_name in ("title", "spec"):
            if field_name in arguments:
                fields[field_name] = _diagnostic_text_summary(arguments[field_name])
    now = time.time()
    entry = {
        "schema_version": 1,
        "timestamp": (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
            + ".%03dZ" % int((now % 1) * 1000)
        ),
        "event": "mcp_request_preflight",
        "pid": os.getpid(),
        "rpc_id": _safe_rpc_id(message.get("id")),
        "method": _safe_protocol_label(message.get("method"), _SAFE_MCP_METHOD_LABELS),
        "tool_name": _safe_protocol_label(params.get("name"), _SAFE_MCP_TOOL_LABELS),
        "payload_bytes": len(data),
        "payload_sha256": hashlib.sha256(data).hexdigest(),
        "arguments": (
            _diagnostic_text_summary(arguments) if arguments is not None else None
        ),
        "fields": fields,
    }
    if request_id is not None:
        entry["request_id"] = request_id
    return entry


def _append_mcp_request_diagnostic(
    message: Dict[str, Any], data: bytes, request_id: Optional[str] = None
) -> None:
    # Reuse the runtime-state guards: they reject linked paths, create mode-0700 directories,
    # and open the append-only file as mode 0600 without following the final path.
    from client import runner

    path = mcp_request_log_path()
    runner._ensure_private_dir(path.parent)
    entry = _mcp_request_diagnostic(message, data, request_id=request_id)
    rendered = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
    with runner._exclusive_path_locks((mcp_request_log_lock_path(),)):
        with runner._open_append_log(path) as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() + len(rendered.encode("utf-8")) > _MAX_MCP_REQUEST_LOG_BYTES:
                handle.seek(0)
                handle.truncate(0)
            handle.write(rendered)


# NOTE: these tool schemas carry NO `description` — it is overlaid per-locale from
# client.i18n.mcp_tools() at tools/list assembly (see _localize_tool). Keep name + inputSchema
# (types / enum / required) here as the wire STRUCTURE; user-facing prose lives in the locale tables.
_ACTIVATION_REQUIRED_TOOL = {
    "name": "activation_required",
    "inputSchema": {"type": "object", "properties": {}},
}
_LITE_AUDIT_TOOL = {
    "name": "audit_skill_submit",
    "inputSchema": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "enum": ["audit"]},
            "args": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "context": {"type": "string"},
                    "artifact_intent": {
                        "type": "string",
                        "enum": ["prescriptive"],
                    },
                    "accept_degrade": {"type": "boolean", "enum": [True]},
                    "ui_locale": {
                        "type": "string",
                        "enum": ["zh-CN", "en-US"],
                    },
                },
                "required": ["title", "content"],
            },
        },
        "required": ["skill_name", "args"],
    },
}
_LOCAL_AUDIT_COMPLETE_STATUSES = ("completed", "partial", "failed")
_LOCAL_AUDIT_COMPLETE_TOOL = {
    "name": "audit_skill_complete",
    "inputSchema": {
        "type": "object",
        "properties": {
            "local_id": {"type": "string", "pattern": "^local_.+"},
            "status": {
                "type": "string",
                "enum": list(_LOCAL_AUDIT_COMPLETE_STATUSES),
            },
        },
        "required": ["local_id", "status"],
    },
}


_ENTITLEMENT_ACTIONS = {
    "subscription_expired": "renew_subscription",
    "credits_exhausted": "add_credits",
    "rate_limited": "wait_or_retry",
}
_MCP_INSUFFICIENT_BALANCE_MARKER = "insufficient_balance"


def _is_mcp_insufficient_balance_text(text: str) -> bool:
    """Recognize the server's balance marker without coupling to response prose."""
    return isinstance(text, str) and _MCP_INSUFFICIENT_BALANCE_MARKER in text


def _contains_mcp_insufficient_balance_marker(value: Any) -> bool:
    """Find the server-owned balance marker anywhere in a received MCP response."""
    if isinstance(value, str):
        return _is_mcp_insufficient_balance_text(value)
    if isinstance(value, dict):
        return any(
            _contains_mcp_insufficient_balance_marker(item)
            for item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_mcp_insufficient_balance_marker(item) for item in value)
    return False


def _credits_exhausted_action() -> Dict[str, Any]:
    """Return the one server-authoritative action used for a received balance marker."""
    return {
        "status": "account_action_required",
        "reason": "credits_exhausted",
        "retryable": False,
        "action": "add_credits",
        "local_advisory_available": True,
    }


def _local_audit_args(arguments: Any) -> Optional[Dict[str, Any]]:
    """Validate the explicit interactive local defect-review request shape."""
    if not isinstance(arguments, dict) or arguments.get("skill_name") != "audit":
        return None
    args = arguments.get("args")
    if not isinstance(args, dict):
        return None
    # Keep compatibility with older routing prose, but do not require a second confirmation for
    # an explicitly user-requested audit. False and non-boolean values still fail closed.
    if "accept_degrade" in args and args.get("accept_degrade") is not True:
        return None
    intent = args.get("artifact_intent", _DEFAULT_INTENT)
    title = args.get("title")
    content = args.get("content")
    if (
        intent != _DEFAULT_INTENT
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(content, str)
        or not content.strip()
    ):
        return None
    return args


def _is_defect_audit_submission(message: Dict[str, Any]) -> bool:
    """Whether this is the high-level defect-review submission owned by this client."""
    if message.get("method") != "tools/call":
        return False
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    tool_name = _logical_mcp_tool_name(params.get("name"))
    if tool_name in {"de_audit", "submit_audit"}:
        return True
    if tool_name != "audit_skill_submit":
        return False
    arguments = params.get("arguments")
    return isinstance(arguments, dict) and arguments.get("skill_name") == "audit"


def _explicit_user_audit_topic(message: Dict[str, Any]) -> Optional[str]:
    """Return the nonempty topic carried by a high-level defect-review submission."""
    if not _is_defect_audit_submission(message):
        return None
    params = message["params"]
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return None
    workflow_args = (
        arguments.get("args")
        if _logical_mcp_tool_name(params.get("name")) == "audit_skill_submit"
        else arguments
    )
    if not isinstance(workflow_args, dict):
        return None
    title = workflow_args.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    return title.strip()


def _audit_ui_locale(message: Dict[str, Any]) -> Optional[str]:
    """Read the narrow display-locale hint without forwarding or logging user content."""
    if not _is_defect_audit_submission(message):
        return None
    params = message.get("params")
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if not isinstance(arguments, dict):
        return None
    workflow_args = (
        arguments.get("args")
        if _logical_mcp_tool_name(params.get("name")) == "audit_skill_submit"
        else arguments
    )
    if not isinstance(workflow_args, dict):
        return None
    locale = workflow_args.get("ui_locale")
    return locale if isinstance(locale, str) else None


def _explicit_audit_required_response(rid: Any) -> Dict[str, Any]:
    return _jsonrpc_error(
        rid,
        -32001,
        "explicit audit topic required",
        {
            "status": "explicit_audit_required",
            "reason": "missing_user_audit_topic",
            "request_sent": False,
        },
    )


def _local_audit_completion_args(arguments: Any) -> Optional[Dict[str, str]]:
    """Validate the narrow, terminal-only local advisory completion contract."""
    if not isinstance(arguments, dict):
        return None
    local_id = arguments.get("local_id")
    status = arguments.get("status")
    if (
        not isinstance(local_id, str)
        or not local_id.startswith("local_")
        or not local_id[6:]
        or status not in _LOCAL_AUDIT_COMPLETE_STATUSES
    ):
        return None
    return {"local_id": local_id, "status": status}


def _local_advisory_response(
    rid: Any,
    *,
    reason: str,
    action: Optional[str] = None,
    title: Optional[str] = None,
    ui_locale: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the one local-audit envelope used by all approved downgrade boundaries."""
    from client import runner

    run_id = "local_%s" % secrets.token_hex(12)
    safe_title = title.strip() if isinstance(title, str) else ""
    run: Dict[str, Any] = {
        "run_id": run_id,
        "status": "running",
        "title": safe_title or "DE Lite local audit",
        "local": True,
        "local_surface": "de_lite",
        "fallback_mode": "session-llm",
        "degrade_reason": reason,
        "audit_id": None,
        "advisory_only": True,
        "ui_locale": runner.normalize_ui_locale(ui_locale),
        "next_action": "run_aqg_multi_review_in_current_session",
    }
    if action is not None:
        run["degrade_action"] = action
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "result": _tool_content({
            "schema_version": "1.0",
            "skill": "audit",
            "run_id": run_id,
            "payload": run,
        }),
    }


def _local_audit_completion_response(
    rid: Any, arguments: Any
) -> Dict[str, Any]:
    """Complete one existing DE Lite run without exposing filesystem errors or Hub details."""
    allowed = _local_audit_completion_args(arguments)
    if allowed is None:
        return _jsonrpc_error(
            rid,
            -32001,
            "local audit completion rejected",
            {"status": "local_completion_rejected", "reason": "invalid_arguments"},
        )
    try:
        from client import runner
    except Exception:  # aqg: top-level boundary — a thin install must not kill MCP transport
        return _jsonrpc_error(
            rid,
            -32001,
            "local audit completion rejected",
            {"status": "local_completion_rejected", "reason": "state_unavailable"},
        )
    try:
        payload = runner.complete_local_advisory_run(**allowed)
    except runner.AuditError:
        return _jsonrpc_error(
            rid,
            -32001,
            "local audit completion rejected",
            {"status": "local_completion_rejected", "reason": "unknown_or_terminal_run"},
        )
    except OSError:  # aqg: local state boundary — do not leak paths or break MCP transport
        return _jsonrpc_error(
            rid,
            -32001,
            "local audit completion rejected",
            {"status": "local_completion_rejected", "reason": "state_write_failed"},
        )
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "result": _tool_content({
            "status": payload["status"],
            "run_id": payload["run_id"],
            "local": True,
            "local_surface": "de_lite",
            "advisory_only": True,
            "audit_id": None,
        }),
    }


def _account_action_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """Validate a decoded server-owned entitlement contract."""
    if not isinstance(payload, dict):
        return None
    reason = payload.get("reason")
    if (
        payload.get("status") != "account_action_required"
        or payload.get("local_advisory_available") is not True
        or type(payload.get("retryable")) is not bool
        or not isinstance(reason, str)
        or reason not in _ENTITLEMENT_ACTIONS
        or payload.get("action") != _ENTITLEMENT_ACTIONS[reason]
    ):
        return None
    return {
        "status": "account_action_required",
        "reason": reason,
        "retryable": payload["retryable"],
        "action": payload["action"],
        "local_advisory_available": True,
    }


def _account_action_contract(raw_error: str, http_status: int) -> Optional[Dict[str, Any]]:
    """Accept only the server-owned entitlement reason/action contract."""
    if http_status not in (402, 403, 429):
        return None
    try:
        payload = json.loads(raw_error)
    except (TypeError, ValueError):
        return None
    return _account_action_payload(payload)


def _account_action_response(response: Any) -> Optional[Dict[str, Any]]:
    """Read the same contract when the Hub wraps it in JSON-RPC error.data."""
    if not isinstance(response, dict):
        return None

    # ``insufficient_balance`` is the server's authoritative business marker. It may arrive in the
    # initial submit envelope, a later wait/status envelope, or JSON text nested inside either one.
    # Once bytes have been received, converting that deterministic rejection to one local advisory
    # is not a replay. Do not make the fallback depend on MCP wrapper shape or localized prose.
    if _contains_mcp_insufficient_balance_marker(response):
        return _credits_exhausted_action()

    error = response.get("error")
    data = error.get("data") if isinstance(error, dict) else None
    structured = _account_action_payload(data)
    if structured is not None:
        return structured
    return None


class EntitlementBlockedError(ShellError):
    """A server-authoritative subscription/credits/rate-limit block."""

    def __init__(self, data: Dict[str, Any]):
        super().__init__("Decision Engine account action required: %s" % data["reason"])
        self.data = data

_SERVICE_UNAVAILABLE_PAYLOAD = {
    "status": "service_unavailable",
    "reason": "unreachable",
    "retryable": True,
    "request_sent": False,
    "action": "reconnect",
}
_SERVICE_UNAVAILABLE_TOOL = {
    "name": "service_unavailable",
    "inputSchema": {"type": "object", "properties": {}},
}


def _service_unavailable_data(message: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(_SERVICE_UNAVAILABLE_PAYLOAD)
    offer = _local_advisory_offer(message)
    if offer is not None:
        data.update(offer)
    return data


class OfflineForwarder:
    """Activated MCP surface while the Hub is unreachable before a request."""

    is_lite = False

    def forward(
        self, message: Dict[str, Any], *, timeout_s: Optional[float] = None
    ) -> Dict[str, Any]:
        del timeout_s
        rid = message.get("id")
        method = message.get("method")
        if method == "initialize":
            params = message.get("params")
            protocol_version = (
                params.get("protocolVersion") if isinstance(params, dict) else None
            )
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": protocol_version or "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "decision-engine-shell", "version": "0.1.0"},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "tools": [
                        _localize_tool(_SERVICE_UNAVAILABLE_TOOL, _mcp_locale()),
                        _localize_tool(_LITE_AUDIT_TOOL, _mcp_locale()),
                        _localize_tool(_LOCAL_AUDIT_COMPLETE_TOOL, _mcp_locale()),
                    ]
                },
            }
        if method == "tools/call":
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            if params.get("name") == "audit_skill_submit":
                arguments = params.get("arguments")
                args = _local_audit_args(arguments)
                if args is not None:
                    return _local_advisory_response(
                        rid,
                        reason="service_unavailable",
                        title=args.get("title"),
                        ui_locale=args.get("ui_locale"),
                    )
                if (
                    _is_defect_audit_submission(message)
                    and _explicit_user_audit_topic(message) is None
                ):
                    return _explicit_audit_required_response(rid)
            if params.get("name") == "service_unavailable":
                return {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": _tool_content(dict(_SERVICE_UNAVAILABLE_PAYLOAD)),
                }
            return _jsonrpc_error(
                rid,
                -32001,
                _mcp_error_message("service_unavailable", _mcp_locale()),
                _service_unavailable_data(message),
            )
        return {"jsonrpc": "2.0", "id": rid, "result": {}}


class LiteForwarder:
    """Local MCP surface for installations that have not been activated."""

    is_lite = True

    def forward(
        self, message: Dict[str, Any], *, timeout_s: Optional[float] = None
    ) -> Dict[str, Any]:
        del timeout_s
        rid = message.get("id")
        method = message.get("method")
        if method == "initialize":
            params = message.get("params")
            protocol_version = (
                params.get("protocolVersion") if isinstance(params, dict) else None
            )
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": protocol_version or "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "decision-engine-lite", "version": "unactivated"},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "tools": [
                        _localize_tool(_ACTIVATION_REQUIRED_TOOL, _mcp_locale()),
                        _localize_tool(_LITE_AUDIT_TOOL, _mcp_locale()),
                        _localize_tool(_LOCAL_AUDIT_COMPLETE_TOOL, _mcp_locale()),
                    ]
                },
            }
        if method == "tools/call":
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            tool_name = params.get("name")
            if tool_name == "activation_required":
                return {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": _tool_content(
                        {
                            "status": "activation_required",
                            # same prose as the tool's advertised description, host-locale-resolved.
                            # .get (not []) so a table gap degrades to no message, never a KeyError
                            # on the transport thread — _localize_tool guarantees the key whenever
                            # activation_required is in the en-US table (pinned by a test).
                            "message": _localize_tool(
                                _ACTIVATION_REQUIRED_TOOL, _mcp_locale()
                            ).get("description"),
                        }
                    ),
                }
            if tool_name == "audit_skill_submit":
                arguments = params.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                allowed = _local_audit_args(arguments)
                if allowed is not None:
                    return _local_advisory_response(
                        rid,
                        reason="unactivated",
                        title=allowed.get("title"),
                        ui_locale=allowed.get("ui_locale"),
                    )
                if (
                    _is_defect_audit_submission(message)
                    and _explicit_user_audit_topic(message) is None
                ):
                    return _explicit_audit_required_response(rid)
            tool_name = tool_name if isinstance(tool_name, str) else "<unknown>"
            return _jsonrpc_error(
                rid,
                -32001,
                _mcp_error_message("activation_required", _mcp_locale()),
                {"status": "activation_required", "tool": tool_name},
            )
        return {"jsonrpc": "2.0", "id": rid, "result": {}}


class Forwarder:
    """Holds the resolved endpoint + token and POSTs raw JSON-RPC to ``/mcp``."""

    def __init__(self, endpoint: str, token: str, *, timeout_s: int = DEFAULT_TIMEOUT_S):
        self.endpoint = normalize_endpoint(endpoint)
        # Only instances resolved from the installed config may be refreshed during a
        # long-lived MCP session. Directly constructed transports are caller-owned fixtures.
        self._config_bound = False
        # Never transmit the bearer device token over cleartext. normalize_endpoint
        # upgrades scheme-less input to https, but an explicit http:// would slip
        # the token onto the wire — reject it (localhost dev excepted).
        if (
            token
            and self.endpoint.startswith("http://")
            and not _is_local_host(self.endpoint)
        ):
            raise ShellError(
                "refusing to send the device token over plaintext http:// endpoint "
                "%s — use https://" % self.endpoint
            )
        self.token = token
        self.timeout_s = timeout_s

    @classmethod
    def from_config(cls, timeout_s: int = DEFAULT_TIMEOUT_S) -> "Forwarder":
        config_path = de_config_path()
        config_exists = config_path.exists()
        config = load_json(config_path)
        endpoint = config.get("server_endpoint") or ""
        token = config.get("access_token") or ""
        if not endpoint:
            error_type = ShellError if token else ActivationRequiredError
            reason_code = (
                "config_missing" if not config_exists else "missing_endpoint_and_token"
            )
            error_kwargs = (
                {"reason_code": reason_code}
                if error_type is ActivationRequiredError
                else {}
            )
            raise error_type(
                "no server_endpoint in %s — run the installer with "
                "--server-endpoint, or activate this device first" % de_config_path(),
                **error_kwargs,
            )
        if not token:
            raise ActivationRequiredError(
                "device not activated (no access_token in %s) — exchange your "
                "API key for a device token first (see client docs)"
                % de_config_path(),
                reason_code="missing_access_token",
            )
        forwarder = cls(endpoint, token, timeout_s=timeout_s)
        forwarder._config_bound = True
        return forwarder

    def forward(self, message: Dict[str, Any], *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """POST one JSON-RPC message to /mcp, return the parsed JSON response.

        Like every other authenticated call, this one REFUSES redirects
        (``client.http_safety.NoRedirect``). It carries the device token, so a followed 3xx
        would hand it to whatever host the Location names and then feed that host's reply
        back as the hub's answer — measured, before the guard: on a 301/302/303 the target
        received ``Authorization: Bearer <token>`` and its forged JSON-RPC result was
        returned to the caller. (307/308 already failed, since CPython won't redirect a POST
        with a body.)

        The hub does not redirect /mcp as of this commit — but that is a fact about today's
        deployment, not a guarantee: put a proxy, a load balancer's http→https upgrade, or
        framework-level trailing-slash canonicalization in front of it and every JSON-RPC
        message hard-fails here BY DESIGN. That trade is deliberate (a token leak is worse
        than an outage), which is why the refusal message points at the config: the fix is
        the endpoint, never the guard.

        ``timeout_s`` overrides the default per-request timeout for THIS call (used by the local
        display handlers to bound a status poll well below the worker's EOF-drain budget); the
        serial transport path omits it and keeps ``self.timeout_s``."""
        if not hub_reachable(self.endpoint):
            raise OfflineError(offline_message(message), data=_local_advisory_offer(message))
        data = json.dumps(message).encode("utf-8")
        request_id = _request_outcome_id()
        try:
            _append_mcp_request_diagnostic(message, data, request_id=request_id)
        except Exception as exc:  # aqg: top-level boundary — diagnostics must never block MCP
            _log("mcp request diagnostic skipped: %s" % type(exc).__name__)
        from client import i18n  # lazy: stdlib-leaf resolver, keep transport path GUI/config-free

        headers = {
            "Accept": "application/json",
            "Accept-Language": i18n.accept_language(),
            "Content-Type": "application/json",
            "User-Agent": SHIM_USER_AGENT,
            "Authorization": "Bearer " + self.token,
            "X-Request-ID": request_id,
        }
        effective_timeout = self.timeout_s if timeout_s is None else timeout_s
        request = Request(self.endpoint + "/mcp", data=data, headers=headers, method="POST")
        send_state = _RequestSendState()
        response_started = False
        try:
            # Built per call so _https_context() is evaluated now and honours SSL_CERT_FILE /
            # certifi — mirrors the get_ge_artifact opener below.
            opener = build_opener(
                NoRedirect,
                _SendTrackingHTTPHandler(send_state),
                _SendTrackingHTTPSHandler(send_state, context=_https_context()),
            )
            with opener.open(request, timeout=effective_timeout) as resp:
                response_started = True
                # Bounded read (shared helper): PR#50 capped the BYTES here (an unbounded read()
                # OOMs the client); this routes the same cap through read_within_budget, which also
                # bounds TOTAL transfer wall-clock — urllib's per-op timeout is not a transfer
                # deadline, so a hub dribbling ~1 byte per socket-window would hold this transport-
                # thread read for a very long time under the byte cap. Over-cap still DETECTS (reads
                # cap+1, never a truncated prefix); the check is on bytes read, never the hub-supplied
                # Content-Length. ShellError is a RuntimeError, so a breach raise passes the except
                # arms below (HTTPError/OSError/...) untouched — no server body → model.
                body = read_within_budget(
                    resp, max_bytes=_MAX_MCP_RESPONSE_BYTES, budget_s=effective_timeout,
                    error=lambda reason: (
                        OutcomeUnknownError("read_timeout", request_id)
                        if reason.startswith("transfer exceeded its ")
                        else OutcomeUnknownError("response_reset", request_id)
                        if reason == "response body ended before its declared length"
                        else ShellError("/mcp " + reason)
                    ))   # helper-authored reason only; no server bytes reach the model
                raw = body.decode("utf-8")
        except HTTPError as exc:
            # exc owns an OPEN response fp; a completed read() closes it at EOF, the refused
            # redirect below never reads one — so the arm closes on its way out.
            try:
                if exc.code >= 500:
                    raise OutcomeUnknownError("http_5xx", request_id) from None
                if 300 <= exc.code < 400:
                    # The refused-redirect path (NoRedirect raises HTTPError with the 3xx code),
                    # plus any 3xx urllib declined to act on. The body is deliberately not read.
                    # The trust boundary: a 4xx/5xx body below is the TLS-authenticated hub's
                    # own error contract and IS echoed; a 3xx body is whoever answered a
                    # redirect we refused to follow, and a shim result reaches model context.
                    # `from None` keeps the Location off the exception chain.
                    raise ShellError(
                        "HTTP %s from /mcp: refused — this client never follows redirects on "
                        "an authenticated call; check that your configured endpoint is the "
                        "hub's real https:// URL" % exc.code) from None
                try:
                    # exc.read() on a 4xx/5xx is an authenticated read too, so bound it with the
                    # SAME cap: an unbounded error body OOMs the client just as a success body would.
                    # Over-cap → drop the diagnostic body and fall back to exc.reason (a normal small
                    # error body is still echoed exactly as before — this only trims a hostile one).
                    err_body = exc.read(_MAX_MCP_RESPONSE_BYTES + 1)
                    raw_error = ("" if len(err_body) > _MAX_MCP_RESPONSE_BYTES
                                 else err_body.decode("utf-8", errors="replace"))
                except (HTTPException, OSError):
                    raw_error = ""   # a truncated error body must not escape the ShellError contract
                if _is_mcp_insufficient_balance_text(raw_error):
                    raise EntitlementBlockedError(_credits_exhausted_action()) from None
                account_action = _account_action_contract(raw_error, exc.code)
                if account_action is not None:
                    raise EntitlementBlockedError(account_action) from None
                raise ShellError("HTTP %s from /mcp: %s" % (exc.code, raw_error or exc.reason))
            finally:
                try:
                    exc.close()
                except Exception:  # aqg: top-level boundary — cleanup must never replace the ShellError
                    pass
        except TimeoutError:
            if response_started or send_state.sent:
                raise OutcomeUnknownError("read_timeout", request_id) from None
            raise ShellError("/mcp request failed before a response: TimeoutError") from None
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, RemoteDisconnected):
                raise OutcomeUnknownError("response_reset", request_id) from None
            if isinstance(reason, (TimeoutError, ConnectionResetError,
                                   ConnectionAbortedError, BrokenPipeError)):
                if send_state.sent:
                    outcome_reason = (
                        "read_timeout" if isinstance(reason, TimeoutError)
                        else "response_reset"
                    )
                    raise OutcomeUnknownError(outcome_reason, request_id) from None
                raise ShellError(
                    "/mcp request failed before a response: %s" % type(reason).__name__
                ) from None
            raise ShellError("/mcp request failed: %s" % reason)
        except RemoteDisconnected:
            raise OutcomeUnknownError("response_reset", request_id) from None
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
            if response_started or send_state.sent:
                raise OutcomeUnknownError("response_reset", request_id) from None
            raise ShellError(
                "/mcp request failed before a response: %s" % type(exc).__name__
            ) from None
        except OSError as exc:
            raise ShellError("/mcp request failed: %s" % exc)
        except (HTTPException, ValueError) as exc:
            # Same gap get_ge_artifact closed: a non-UTF-8 body (UnicodeDecodeError, a
            # ValueError), a truncated read (IncompleteRead, an HTTPException), or a malformed
            # Location subclass neither URLError nor OSError, so they escaped raw — and their
            # str() echoes server-chosen bytes. Report the type only.
            raise ShellError("/mcp request failed: %s" % type(exc).__name__) from None
        if _is_mcp_insufficient_balance_text(raw):
            # A service may answer with a plain-text body rather than a JSON-RPC envelope. The
            # authenticated response is still an authoritative balance rejection; normalize it at
            # the transport boundary so the normal serve() downgrade path owns the UI transition.
            raise EntitlementBlockedError(_credits_exhausted_action()) from None
        if not raw:
            return {}
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ShellError("invalid JSON from /mcp: %s" % exc)
        except ValueError as exc:
            # json.loads can raise a plain ValueError that is NOT a JSONDecodeError — e.g. an
            # integer literal past sys.get_int_max_str_digits() — which the arm above misses; it
            # would otherwise escape raw, breaking the every-failure-is-a-ShellError contract. Type
            # only: keep server-chosen bytes (the offending digits) out of the model-facing message.
            raise ShellError("invalid JSON from /mcp: %s" % type(exc).__name__) from None
        return loaded if isinstance(loaded, dict) else {"result": loaded}

    def get_ge_artifact(self, run_id: str, *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """GET the server-rendered GE artifact bytes (`{kind, data}`) by run_id, with the device
        token. This is the G1 out-of-band path (design §3.1): the token never enters model context,
        so only this client handler can retrieve the bytes. Raises ShellError on any failure.

        The fetch REFUSES redirects (``client.http_safety.NoRedirect``, the same guard the
        /db/render and activation calls use). urllib's default opener follows 3xx and rebuilds with
        the ORIGINAL headers, so a hostile or compromised redirect would hand this ``Authorization:
        Bearer <device token>`` to whatever host it names — and return that host's bytes as the
        artifact. The hub never redirects this endpoint, so refusing costs nothing.

        The body read is capped at ``_MAX_GE_ARTIFACT_BYTES``. The timeout does NOT already bound
        it: urllib's ``timeout`` is per socket operation, not a transfer deadline, so a hub that
        keeps sending stays under it indefinitely and an unbounded read grows without limit.

        ``timeout_s`` (open_ge) caps the fetch to the worker's REMAINING EOF-drain budget so a
        completed-near-deadline render's slow fetch can't push the worker past the drain; it only
        shrinks the bound, never raises it above ``_DISPLAY_FETCH_TIMEOUT_S``."""
        from client import i18n  # lazy: stdlib-leaf resolver, keep transport path GUI/config-free

        headers = {
            "Accept": "application/json",
            "Accept-Language": i18n.accept_language(),
            "User-Agent": SHIM_USER_AGENT,
            "Authorization": "Bearer " + self.token,
        }
        url = self.endpoint + "/v1/visual/artifacts/" + quote(run_id, safe="")
        request = Request(url, headers=headers, method="GET")
        try:
            # bound < the serve() EOF drain (60s) so a slow fetch worker can't outlive the join;
            # open_ge passes its remaining budget to shrink this further on the completed path.
            bound = _DISPLAY_FETCH_TIMEOUT_S if timeout_s is None else min(_DISPLAY_FETCH_TIMEOUT_S, timeout_s)
            timeout = min(self.timeout_s, bound)
            # Built per call (not module-level) so _https_context() is evaluated now and honours
            # SSL_CERT_FILE / certifi — mirrors the /db/render opener at launcher.fetch_board_html.
            opener = build_opener(NoRedirect, HTTPSHandler(context=_https_context()))
            with opener.open(request, timeout=timeout) as resp:
                # Bounded read (shared helper): DETECTS an over-limit body (reads cap+1, never a
                # truncated prefix) AND bounds TOTAL transfer wall-clock — urllib's `timeout` is
                # per socket op, so without the deadline a hub that keeps dribbling stays under it
                # indefinitely. The check is on bytes actually read, never on Content-Length (a
                # hub-supplied header free to lie). ShellError is a RuntimeError, so a breach raise
                # passes the handlers below untouched. `budget_s=timeout`: the same remaining EOF-
                # drain budget the socket timeout uses, now also the transfer deadline.
                body = read_within_budget(
                    resp, max_bytes=_MAX_GE_ARTIFACT_BYTES, budget_s=timeout,
                    error=lambda reason: ShellError("/v1/visual/artifacts " + reason))  # no server body → model
                raw = body.decode("utf-8")
        except HTTPError as exc:
            # Also the refused-redirect path (NoRedirect raises HTTPError with the 3xx code).
            # The body is deliberately never read (no server body → model), so close the
            # response explicitly rather than leaving the socket to the GC (matches activate.py).
            try:
                exc.close()
            except Exception:  # aqg: top-level boundary — cleanup must never replace the ShellError
                pass
            # `from None`: the HTTPError carries the response headers (a refused redirect's
            # Location among them). Nothing formats __context__ today, but this result is
            # model-facing — don't leave the target host hanging off the exception chain.
            raise ShellError("HTTP %s from /v1/visual/artifacts" % exc.code) from None
        except TimeoutError:
            raise ShellError("/v1/visual/artifacts timed out after %ss" % self.timeout_s)
        except (URLError, OSError) as exc:
            raise ShellError("/v1/visual/artifacts request failed: %s" % exc)
        except (HTTPException, ValueError) as exc:
            # These carry SERVER-CONTROLLED bytes and subclass neither URLError nor OSError, so
            # they used to escape raw — breaking the every-failure-is-a-ShellError contract:
            #   UnicodeDecodeError (a ValueError) — a non-UTF-8 body; str() echoes the offending byte
            #   IncompleteRead (an HTTPException) — a body truncated mid-read
            #   ValueError — a malformed Location, raised inside urllib BEFORE NoRedirect is consulted
            # Report the exception TYPE only: interpolating these would put server bytes in front of
            # the model, which is the very thing the bare `% exc.code` above exists to avoid.
            raise ShellError("/v1/visual/artifacts request failed: %s" % type(exc).__name__) from None
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ShellError("invalid JSON from /v1/visual/artifacts: %s" % exc)
        if not (isinstance(loaded, dict) and isinstance(loaded.get("kind"), str)
                and isinstance(loaded.get("data"), str)):
            raise ShellError("unexpected /v1/visual/artifacts response shape")
        return loaded


def _jsonrpc_error(rid: Any, code: int, message: str,
                   data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:                      # optional JSON-RPC 2.0 error.data (omitted when absent)
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


# ---- local display tools (see internal design notes, ge-db-enforced-popup §3–§4) ---------------
# These three run LOCALLY in the shim (they open a native window on THIS machine); everything else
# is forwarded transport. The shim already holds base_url+token, so it does the two authenticated
# hub fetches (GE artifact by run_id; DB board /db/render) — the bytes reach the popup, never the
# model (G1). A bounded db_board_result poll runs OFF the transport thread (§C1).
# Like the four tool constants above, these carry NO `description` (top-level or per-param): the
# prose is overlaid per-locale from client.i18n.mcp_tools() at tools/list assembly (_localize_tool).
# Only the wire STRUCTURE lives here — types, enum, minLength/maxLength, required.
_DISPLAY_TOOL_SCHEMAS = [
    {
        "name": "open_ge_popup",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "title": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "open_ge",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["comic", "infographic", "diagram"]},
                "spec": {"type": "object"},
                "client_request_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "title": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["mode", "spec"],
        },
    },
    {
        "name": "open_db_board",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "object"},
                "title": {"type": "string"},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "db_board_result",
        "inputSchema": {
            "type": "object",
            "properties": {
                "popup_id": {"type": "string"},
                "wait_s": {"type": "number"},
            },
            "required": ["popup_id"],
        },
    },
]
_DISPLAY_TOOLS = {schema["name"] for schema in _DISPLAY_TOOL_SCHEMAS}


def _localized_display_title(surface: str, locale_hint: Any = None,
                             fallback: str = "Decision Engine") -> str:
    """Default OS/Dock/taskbar title for a local display surface."""
    try:
        titles = i18n.shell(i18n.resolve_locale(locale_hint)).get("surface_title", {})
        title = titles.get(surface) if isinstance(titles, dict) else None
        return title if isinstance(title, str) and title else fallback
    except Exception:  # aqg: top-level boundary — display-title i18n must not block popup launch
        return fallback


def _prefixed_display_title(title: str) -> str:
    """OS-visible title when the caller supplies business text."""
    stripped = title.strip()
    if stripped.startswith("Decision Engine - "):
        return stripped
    return "Decision Engine - " + stripped


def _display_window_title(args: Dict[str, Any], surface: str, *,
                          spec: Optional[Dict[str, Any]] = None,
                          fallback: str = "Decision Engine") -> str:
    """Resolve the native popup title using a product prefix for caller/content titles."""
    title = args.get("title")
    if isinstance(title, str) and title.strip():
        return _prefixed_display_title(title)
    if spec is not None:
        spec_title = spec.get("title")
        if isinstance(spec_title, str) and spec_title.strip():
            return _prefixed_display_title(spec_title)
    locale_hint = args.get("ui_locale")
    if locale_hint is None and spec is not None:
        locale_hint = spec.get("ui_locale")
    return _localized_display_title(surface, locale_hint, fallback=fallback)


# tools/list descriptions are read by the CALLING MODEL, so they follow the host UI language. Resolve
# it ONCE per process (this shim is long-running; the OS UI language does not change under it) and
# memoize — mirrors client/native_shell.py's _SHELL_LOCALE. `_localize_tool` takes an explicit locale
# so it stays trivially unit-testable; only the assembly call sites read the cached value.
_MCP_LOCALE: Optional[str] = None
_MCP_LOCALE_LOCK = threading.Lock()


def _mcp_locale() -> str:
    """Host UI locale for MCP tool descriptions, resolved once and cached for the process.

    Double-checked locking: the resolved value is deterministic (host OS/env locale), so a race is
    benign, but the lock keeps the 'resolved ONCE' invariant literally true and avoids a redundant
    ctypes/env probe when several transport messages arrive together at startup."""
    global _MCP_LOCALE
    if _MCP_LOCALE is None:
        with _MCP_LOCALE_LOCK:
            if _MCP_LOCALE is None:
                try:
                    from client import i18n  # lazy: keep transport path GUI/config-free

                    _MCP_LOCALE = i18n.resolve_locale()
                except Exception as exc:  # aqg: top-level boundary — locale is optional transport prose
                    _log(
                        "mcp locale resolution fell back to en-US: %s"
                        % type(exc).__name__
                    )
                    _MCP_LOCALE = "en-US"
    return _MCP_LOCALE


def _localize_tool(base: Dict[str, Any], locale: str) -> Dict[str, Any]:
    """Return a deep copy of a wire tool schema with `description` (top-level + per-param) overlaid
    from the locale table, preserving the original ``{name, description, inputSchema}`` key order so
    en-US output is byte-for-byte what shipped before i18n.

    The base carries STRUCTURE only; prose comes from client.i18n.mcp_tools(). This runs on the MCP
    transport thread where tools/list previously used static literals that could never fail, so it is
    TOTAL by construction:
      * a locale missing an entry falls back to the en-US table (a partial zh table degrades to
        English prose, never to a description-less tool — those descriptions carry the model's
        behavioral guardrails, e.g. "do not inline", "do not poll");
      * any exception (import failure, unexpected table shape) degrades to a structure-only copy and
        is logged, rather than raising and failing the whole tools/list handshake.
    A base whose name is in NEITHER table is a programming error caught by a test (every base tool
    name must be in en_US.MCP_TOOLS); at runtime it ships structure-only rather than crashing."""
    try:
        from client import i18n  # lazy: stdlib-leaf resolver, keep transport path GUI/config-free
        name = base.get("name")
        table = i18n.mcp_tools(locale)
        strings = table.get(name)
        if not strings:
            # Missing at this locale → en-US (DEFAULT_LOCALE, kept complete by the parity test).
            strings = i18n.mcp_tools(i18n.DEFAULT_LOCALE).get(name)
        if not strings:
            return copy.deepcopy(base)  # unknown tool: structure-only (test guards this can't happen)
        # Rebuild preserving key order: description sits right after name, as in the pre-i18n literals.
        tool: Dict[str, Any] = {}
        for key, value in base.items():
            tool[key] = copy.deepcopy(value)
            if key == "name":
                tool["description"] = strings["description"]
        props = tool.get("inputSchema", {}).get("properties", {})
        for pname, pdesc in strings.get("params", {}).items():
            target = props.get(pname)
            if isinstance(target, dict):
                target["description"] = pdesc  # params carried description LAST → append matches order
        return tool
    except Exception as exc:  # aqg: top-level boundary — never fail tools/list over prose
        _log("mcp tool localization fell back to structure-only: %s" % type(exc).__name__)
        return copy.deepcopy(base)


# Last-resort English for the two localized -32001 messages. These duplicate en_US.MCP_ERRORS on
# purpose: if the locale table cannot be read at all, the transport still emits the exact pre-i18n
# literal instead of an empty message. A test asserts the two stay identical to the table (drift guard).
_MCP_ERROR_FALLBACK = {
    "service_unavailable": "Decision Engine service unavailable — reconnect and retry",
    "activation_required": (
        "activation_required: activate this device before using Decision Engine tools"
    ),
}


def _mcp_error_message(slug: str, locale: str) -> str:
    """Return the localized ``-32001`` error.message for an internal slug.

    Only the human-readable sentence is localized: the error CODE and the structured ``data``
    payload stay in the caller, and ``activation_required`` keeps its ``activation_required:``
    prefix verbatim in every language because callers may match on that token.

    TOTAL by construction, for the same reason as _localize_tool — this runs on the MCP transport
    thread, where the message used to be a static literal that could never fail: a locale missing
    the slug falls back to the en-US table, and any exception degrades to _MCP_ERROR_FALLBACK.
    """
    try:
        from client import i18n  # lazy: stdlib-leaf resolver, keep transport path GUI/config-free
        message = i18n.mcp_errors(locale).get(slug)
        if not message:
            message = i18n.mcp_errors(i18n.DEFAULT_LOCALE).get(slug)
        if message:
            return message
    except Exception as exc:  # aqg: top-level boundary — never fail a response over prose
        _log("mcp error-message localization fell back to English: %s" % type(exc).__name__)
    return _MCP_ERROR_FALLBACK[slug]


_GE_CHAT_SERVER = "server"
_GE_CHAT_LEGACY = "legacy"


def _ge_chat_transport(popup_followup: bool) -> Optional[str]:
    """Resolve the immutable per-popup chat route from the user-level config only.

    Missing is the new server default.  The ``server`` route is pure hub HTTP (no local Agent,
    no local-file read) so it is available to any host that can render a GE popup, regardless of
    ``popup_followup``.  ``popup_followup`` gates ONLY the ``legacy`` route, which spawns a local
    Agent that reads the user's files: without that capability an exact ``legacy`` rollback fails
    closed rather than handing a non-Agent host the file-reading bridge.  Every other value also
    fails closed rather than becoming an accidental rollback switch.
    """
    rollback_config = managed_component_root("decision-engine") / "config.json"
    configured = load_json(rollback_config).get("ge_chat_transport")
    if configured is None or configured == _GE_CHAT_SERVER:
        return _GE_CHAT_SERVER
    if configured == _GE_CHAT_LEGACY:
        return _GE_CHAT_LEGACY if popup_followup else None
    return None

def _normalize_client_host(value: Any) -> Optional[str]:
    return mcp_config.normalize_client_host(value)


def _client_host_from_initialize(params: Dict[str, Any]) -> Optional[str]:
    info = params.get("clientInfo")
    return (
        _normalize_client_host(info.get("name"))
        if isinstance(info, dict)
        else None
    )


def _copy_tool_call_arguments(
    message: Dict[str, Any],
    params: Dict[str, Any],
    arguments: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    forwarded = dict(message)
    forwarded_params = dict(params)
    forwarded_arguments = dict(arguments)
    forwarded_params["arguments"] = forwarded_arguments
    forwarded["params"] = forwarded_params
    return forwarded, forwarded_arguments


def _with_client_host_metadata(
    message: Dict[str, Any], client_host: Optional[str]
) -> Dict[str, Any]:
    """Copy and stamp authoritative Cursor metadata on Market Research calls."""
    if client_host != "cursor" or message.get("method") != "tools/call":
        return message
    params = message.get("params")
    if not isinstance(params, dict):
        return message
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return message
    tool_name = params.get("name")
    if tool_name == "audit_skill_submit":
        if arguments.get("skill_name") != "audit-market-research":
            return message
        workflow_args = arguments.get("args", {})
        if not isinstance(workflow_args, dict):
            raise ShellError("Cursor Market Research args must be an object")
        forwarded, forwarded_arguments = _copy_tool_call_arguments(
            message, params, arguments
        )
        forwarded_workflow_args = dict(workflow_args)
        forwarded_workflow_args["client_host"] = client_host
        forwarded_arguments["args"] = forwarded_workflow_args
        if "client_host" in forwarded_arguments:
            forwarded_arguments["client_host"] = client_host
        return forwarded
    if tool_name != "de_market_research":
        return message
    forwarded, forwarded_arguments = _copy_tool_call_arguments(
        message, params, arguments
    )
    forwarded_arguments["client_host"] = client_host
    return forwarded


_MAX_DISPLAY_WORKERS = 4        # cap concurrent local display workers (backpressure vs a chatty host)
_DISPLAY_FETCH_TIMEOUT_S = 30   # bound each display hub fetch < the serve() EOF drain (60s)
# open_ge runs submit → poll → fetch → spawn on ONE display worker. Per the product's explicit
# terminal-wait policy, the foreground tool call may stay open for up to ten minutes:
#  - a total wall-clock budget for the worker (with margin for the spawn);
#  - a per-status-call timeout so one hung/slow /mcp poll can't overrun the budget;
#  - a minimum fetch reserve, so even a render that completes LATE in the poll window still has
#    bounded time to fetch — and if the budget is spent it schedules a capped background auto-open
#    instead of an unbounded fetch.
_GE_OPEN_TOTAL_BUDGET_S = 10 * 60.0  # whole open_ge foreground ceiling
_GE_MIN_FETCH_RESERVE_S = 4.0        # keep bounded time for artifact fetch + native spawn
_GE_RENDER_POLL_DEADLINE_S = _GE_OPEN_TOTAL_BUDGET_S - _GE_MIN_FETCH_RESERVE_S
_GE_RENDER_POLL_INTERVAL_S = 1.5   # status poll cadence
_GE_STATUS_CALL_TIMEOUT_S = 8.0    # per visual_status POST — bounds a hung/slow status read
_GE_SUBMIT_TIMEOUT_S = 15.0        # per visual_render submit POST
_GE_AUTO_OPEN_DEADLINE_S = 15 * 60.0  # best-effort ceiling while the MCP host process stays alive
_GE_AUTO_OPEN_POLL_INTERVAL_S = 5.0   # slower cadence for the long-lived background path
_GE_AUTO_OPEN_SLOTS = threading.BoundedSemaphore(_MAX_DISPLAY_WORKERS)
_GE_AUTO_OPEN_LOCK = threading.Lock()
_GE_AUTO_OPEN_CANCEL: Dict[str, threading.Event] = {}
_GE_POPUP_IN_FLIGHT = set()


def _git_env() -> Dict[str, str]:
    """A sanitized environment for the self-update git calls. Strips inherited ``GIT_*`` vars —
    a ``GIT_DIR`` / ``GIT_WORK_TREE`` from the launching shell would override ``-C`` and make git
    operate on the WRONG repo (audit 53a171e5 gemini CRITICAL). Forces NON-INTERACTIVE: no terminal
    / SSH host-key / credential prompt may hang the launch — a promptable git would sit until the
    timeout on every start (grok f3 / gemini)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new"
    env["GIT_ASKPASS"] = ""
    return env


def _self_update_once(root: Path) -> None:
    """Fast-forward the client checkout in place. `--ff-only` NEVER creates a merge or rewrites
    history. We ALSO skip a dirty checkout (a `git status --porcelain` pre-check) so a customer's
    hand-edit is never touched and behaviour is a true no-op unless the tree is clean — `--ff-only`
    alone can still fast-forward non-conflicting files of a dirty tree (audit 53a171e5 gpt/grok).

    The pulled code applies on the NEXT launch — the running shim keeps its already-imported modules,
    and the popup runs as a fresh subprocess that reads from disk anyway. Fully best-effort: every
    failure (offline, git absent, diverged, dirty) is swallowed + logged to stderr, never fatal.
    `start_new_session=True` puts git in its own group so a launch-time kill can't strand it."""
    env = _git_env()
    kw = dict(stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
              env=env, start_new_session=True)
    try:
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                stdout=subprocess.PIPE, **kw)
        if status.returncode != 0 or (status.stdout or b"").strip():
            _log("self-update: skipped (worktree not clean or not a git repo)")
            return
        result = subprocess.run(["git", "-C", str(root), "pull", "--ff-only", "--quiet"],
                                stdout=subprocess.DEVNULL, **kw)
        _log("self-update: git pull --ff-only rc=%d" % result.returncode)
    except Exception as exc:  # aqg: top-level boundary — auto-update is best-effort, never fatal
        _log("self-update: skipped (%s)" % type(exc).__name__)


def _autoupdate_disabled() -> bool:
    # opt-out only on an affirmative value — `DE_NO_AUTOUPDATE=0` must NOT disable updates
    # (any non-empty string is truthy in Python; audit 53a171e5 gemini).
    return os.getenv("DE_NO_AUTOUPDATE", "").strip().lower() in ("1", "true", "yes", "on")


def _maybe_self_update(root: Optional[Path] = None) -> None:
    """Kick off a background, non-blocking self-update so the customer gets fixes just by reopening
    the app — no commands, ever. Skips when this isn't a git checkout, or when DE_NO_AUTOUPDATE is
    set (air-gapped / pinned installs). Runs in a daemon thread so it can't delay the MCP handshake."""
    if _autoupdate_disabled():
        return
    root = root or Path(__file__).resolve().parent.parent   # the shell root = the client checkout
    if not (root / ".git").exists():   # `.exists()` not `.is_dir()` — a worktree/submodule .git is a FILE
        return
    try:
        threading.Thread(target=_self_update_once, args=(root,), daemon=True, name="de-self-update").start()
    except Exception:  # aqg: top-level boundary — a thread-start failure must never break transport
        pass


def _log(message: str) -> None:
    """Diagnostics to stderr — NEVER into a model-facing tool result (redaction: a handler's raw
    failure detail can carry the endpoint / server response, so it stays out of the response)."""
    try:
        print("de-shim: " + message, file=sys.stderr)
    except Exception:  # pragma: no cover - diagnostics must never raise
        pass


def _tool_content(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a payload into the MCP tool-call result shape (matches the hub's _text_result)."""
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _mcp_call_tool(forwarder: "Forwarder", name: str, arguments: Dict[str, Any],
                   *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """Invoke an MCP tool over /mcp and return its decoded JSON payload (the ``_text_result``
    ``content[0].text``). Raises ShellError on a JSON-RPC error or any unexpected result shape, so
    a caller can fail closed on one exception type. This is how a display handler reuses the SAME
    hub tools the model calls (e.g. ``visual_render``) — the metered server path runs unchanged.
    ``timeout_s`` bounds this single POST (a display handler passes a short one so a hung /mcp read
    can't overrun the worker's EOF-drain budget)."""
    message = {
        "jsonrpc": "2.0",
        "id": "shim-" + name,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = forwarder.forward(message, timeout_s=timeout_s)
    error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict):
        raise ShellError("%s: %s" % (name, error.get("message") or "tool error"))
    result = response.get("result") if isinstance(response, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if not (isinstance(content, list) and content and isinstance(content[0], dict)):
        raise ShellError("unexpected %s result shape" % name)
    text = content[0].get("text")
    if not isinstance(text, str):
        raise ShellError("unexpected %s result shape" % name)
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ShellError("invalid JSON from %s: %s" % (name, exc))
    if not isinstance(payload, dict):
        raise ShellError("unexpected %s payload" % name)
    return payload


def _poll_ge_terminal(forwarder: "Forwarder", run_id: str, *, deadline: float,
                      call_timeout_s: float,
                      interval_s: float = _GE_RENDER_POLL_INTERVAL_S,
                      stop_event: Optional[threading.Event] = None) -> str:
    """Poll ``visual_status`` for a GE run until terminal or the absolute monotonic ``deadline``.
    Returns the terminal status ("completed"/"failed"/"cancelled"), or "pending" if it is still
    running at the deadline. Returns "superseded" when an explicit popup call takes ownership from
    a background waiter. Foreground callers can offer an ``open_ge_popup`` fallback on "pending";
    background callers show a bounded status notice and retain the run_id for later recovery.
    Each status POST is bounded by ``call_timeout_s`` so one hung /mcp read can't overrun the
    worker's EOF-drain budget. A transient poll error is retried within the deadline (not treated as
    terminal), so a momentary blip does not abort the one-step fast path. Runs on a display worker
    thread, so the blocking sleep never stalls the transport."""
    while True:
        if stop_event is not None and stop_event.is_set():
            return "superseded"
        try:
            env = _mcp_call_tool(forwarder, "visual_status", {"run_id": run_id},
                                 timeout_s=call_timeout_s)
        except ShellError as exc:
            _log("open_ge status poll error (retrying until deadline): %s" % exc)
            env = None   # transient → fall through to the deadline check + retry, never crash
        if stop_event is not None and stop_event.is_set():
            return "superseded"
        if env is not None:
            payload = env.get("payload") if isinstance(env.get("payload"), dict) else env
            status = payload.get("status") if isinstance(payload, dict) else None
            if status in ("completed", "failed", "cancelled"):
                return status
        if time.monotonic() >= deadline:
            return "pending"
        time.sleep(min(interval_s, max(0.0, deadline - time.monotonic())))


def _claim_ge_popup(run_id: str, *, cancel_background: bool) -> bool:
    """Claim one in-process popup open and optionally supersede its background waiter."""
    with _GE_AUTO_OPEN_LOCK:
        if cancel_background:
            waiter = _GE_AUTO_OPEN_CANCEL.get(run_id)
            if waiter is not None:
                waiter.set()
        if run_id in _GE_POPUP_IN_FLIGHT:
            return False
        _GE_POPUP_IN_FLIGHT.add(run_id)
        return True


def _release_ge_popup(run_id: str) -> None:
    with _GE_AUTO_OPEN_LOCK:
        _GE_POPUP_IN_FLIGHT.discard(run_id)


def _run_ge_auto_open(
    forwarder: "Forwarder",
    run_id: str,
    popup_args: Dict[str, Any],
    cancel_event: threading.Event,
    *,
    client_host: Optional[str],
    popup_followup: bool,
    popup_api_profile: str,
) -> None:
    """Wait for a slow GE render, then open its artifact or a terminal status notice."""
    try:
        status = _poll_ge_terminal(
            forwarder,
            run_id,
            deadline=time.monotonic() + _GE_AUTO_OPEN_DEADLINE_S,
            call_timeout_s=_GE_STATUS_CALL_TIMEOUT_S,
            interval_s=_GE_AUTO_OPEN_POLL_INTERVAL_S,
            stop_event=cancel_event,
        )
        if status == "superseded":
            return
        if status != "completed":
            _log("GE auto-open stopped for %s with status=%s" % (run_id, status))
            if status in ("failed", "cancelled", "pending"):
                try:
                    from client.popup import notice, session
                    title = popup_args.get("title")
                    if not isinstance(title, str) or not title.strip():
                        title = _localized_display_title("diagram")
                    html = notice.render_ge_terminal_notice(status, run_id)
                    # A status-only page has none of the Cursor artifact bootstrap contract;
                    # use the shared legacy window API (close/minimize) for every host.
                    result = session.spawn(html, title)
                    if result.get("status") not in ("open", "opened"):
                        _log("GE auto-open notice failed for %s: %s" %
                             (run_id, result.get("reason", "unknown")))
                except Exception as exc:  # aqg: top-level boundary -- delayed notice is optional UX
                    _log("GE auto-open notice crashed for %s: %r" % (run_id, exc))
            return
        if cancel_event.is_set():
            return
        result = _handle_display_call(
            forwarder,
            "open_ge_popup",
            {"run_id": run_id, "_auto_open": True, **popup_args},
            client_host=client_host,
            popup_followup=popup_followup,
            popup_api_profile=popup_api_profile,
        )
        if result.get("status") not in ("open", "opened"):
            _log("GE auto-open popup failed for %s: %s" % (run_id, result.get("reason", "unknown")))
    except Exception as exc:  # aqg: top-level boundary -- optional delayed display must not kill MCP
        _log("GE auto-open crashed for %s: %r" % (run_id, exc))
    finally:
        with _GE_AUTO_OPEN_LOCK:
            if _GE_AUTO_OPEN_CANCEL.get(run_id) is cancel_event:
                _GE_AUTO_OPEN_CANCEL.pop(run_id, None)
        _GE_AUTO_OPEN_SLOTS.release()


def _schedule_ge_auto_open(
    forwarder: "Forwarder",
    run_id: str,
    popup_args: Dict[str, Any],
    *,
    client_host: Optional[str],
    popup_followup: bool,
    popup_api_profile: str,
) -> bool:
    """Start one capped daemon waiter; false preserves the explicit/manual fallback."""
    if not _GE_AUTO_OPEN_SLOTS.acquire(blocking=False):
        _log("GE auto-open capacity exhausted for %s" % run_id)
        return False
    cancel_event = threading.Event()
    with _GE_AUTO_OPEN_LOCK:
        if run_id in _GE_AUTO_OPEN_CANCEL:
            _GE_AUTO_OPEN_SLOTS.release()
            return True
        _GE_AUTO_OPEN_CANCEL[run_id] = cancel_event
    try:
        threading.Thread(
            target=_run_ge_auto_open,
            args=(forwarder, run_id, popup_args, cancel_event),
            kwargs={
                "client_host": client_host,
                "popup_followup": popup_followup,
                "popup_api_profile": popup_api_profile,
            },
            daemon=True,
            name="de-ge-auto-open",
        ).start()
    except Exception as exc:  # aqg: top-level boundary -- scheduling failure keeps manual fallback
        with _GE_AUTO_OPEN_LOCK:
            if _GE_AUTO_OPEN_CANCEL.get(run_id) is cancel_event:
                _GE_AUTO_OPEN_CANCEL.pop(run_id, None)
        _GE_AUTO_OPEN_SLOTS.release()
        _log("GE auto-open scheduling failed for %s: %r" % (run_id, exc))
        return False
    return True


def _handle_display_call(
    forwarder: "Forwarder",
    name: str,
    args: Dict[str, Any],
    *,
    client_host: Optional[str] = None,
    popup_followup: bool = True,
    popup_api_profile: str = "legacy",
) -> Dict[str, Any]:
    """Execute one local display tool; return the model-facing payload (a small status object —
    never the rendered bytes, never a raw failure detail)."""
    from client.popup import launcher, session  # lazy: keep the transport path free of popup deps

    def resolve_ge_route(run_id: Optional[str] = None) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        # Display is decoupled from chat: a route we cannot authorize (legacy without the
        # local-Agent capability, an unknown value, or an unreadable rollback config) never
        # attaches a chat bridge, but it also never fails the popup — the already-server-rendered
        # artifact still displays. Only server/legacy attach a bridge (spawn_ge); None ⇒ display-only.
        try:
            route = _ge_chat_transport(popup_followup)
        except (ShellError, OSError, UnicodeError):
            route = None
        return route, None

    def spawn_ge(
        html: str,
        title: str,
        artifact: Dict[str, Any],
        run_id: str,
        route: str,
        profile: str,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if profile != "legacy":
            kwargs["api_profile"] = profile
        if route == _GE_CHAT_SERVER:
            kwargs["chat_bootstrap"] = {
                "schema_version": 1,
                "kind": "ge-chat",
                "route": "server",
                "endpoint": forwarder.endpoint,
                "device_token": forwarder.token,
                "run_id": run_id,
            }
        elif route == _GE_CHAT_LEGACY:
            kwargs["chat_context"] = session.build_ge_context_bundle(
                args.get("context"),
                title,
                artifact.get("kind"),
                **({"caller": client_host} if client_host else {}),
            )
        return session.spawn(html, title, **kwargs)

    if name == "open_ge_popup":
        run_id = (args.get("run_id") or "").strip()
        if not run_id:
            return {"status": "failed", "reason": "run_id required"}
        route, route_failure = resolve_ge_route(run_id)
        if route_failure is not None:
            return route_failure
        if not _claim_ge_popup(run_id, cancel_background=not bool(args.get("_auto_open"))):
            return {"status": "scheduled", "run_id": run_id,
                    "hint": "popup open already in progress"}
        try:
            try:
                artifact = forwarder.get_ge_artifact(run_id)   # {kind, data} — client-side only
            except ShellError as exc:
                _log("open_ge_popup fetch failed: %s" % exc)
                return {"status": "failed", "reason": "artifact-fetch-failed", "run_id": run_id}
            title = _display_window_title(args, "diagram")
            profile = "cursor-ge" if popup_api_profile == "cursor" else popup_api_profile
            try:
                popup_spec = launcher.PopupSpec(
                    kind="ge", title=title, artifact=artifact
                )
                html = launcher.render_artifact_html(popup_spec)
            except (TypeError, ValueError) as exc:
                _log("open_ge_popup render failed: %s" % exc)
                return {"status": "failed", "reason": "render-failed", "run_id": run_id}
            result = spawn_ge(html, title, artifact, run_id, route, profile)
            if result.get("status") == "failed":
                result = dict(result)
                result.setdefault("run_id", run_id)
            return result
        finally:
            _release_ge_popup(run_id)

    if name == "open_ge":
        # One-step GE: submit visual_render → poll to terminal → fetch → spawn, so the model emits a
        # single decisive call (mirrors open_db_board). The render stays on the metered async server
        # path (submit is the same visual_render tool the model would call); only the popup is local.
        # The whole chain follows the ten-minute foreground terminal-wait policy above.
        start = time.monotonic()
        mode = args.get("mode")
        if not isinstance(mode, str) or mode.strip() not in ("comic", "infographic", "diagram"):
            return {"status": "failed", "reason": "mode must be comic|infographic|diagram"}
        mode = mode.strip()
        spec = args.get("spec")
        if not isinstance(spec, dict):
            return {"status": "failed", "reason": "spec required"}
        route, route_failure = resolve_ge_route()
        if route_failure is not None:
            return route_failure
        title = _display_window_title(args, mode, spec=spec)
        client_request_id = args.get("client_request_id")
        if client_request_id is None:
            client_request_id = str(uuid.uuid4())
        if not isinstance(client_request_id, str) or not client_request_id.strip() \
                or len(client_request_id.strip()) > 128:
            return {"status": "failed", "reason": "invalid-client-request-id"}
        client_request_id = client_request_id.strip()
        try:
            env = _mcp_call_tool(
                forwarder,
                "visual_render",
                {
                    "client_request_id": client_request_id,
                    "mode": mode,
                    "spec": spec,
                    "title": title,
                },
                timeout_s=_GE_SUBMIT_TIMEOUT_S,
            )
        except OutcomeUnknownError as exc:
            unknown = dict(exc.data)
            # Unlike a generic non-idempotent MCP call, visual_render is safe to replay with the
            # same client_request_id. Expose that exact recovery action to the calling agent.
            unknown["retryable"] = True
            unknown["action"] = "retry"
            unknown["client_request_id"] = client_request_id
            return unknown
        except ShellError as exc:
            _log("open_ge submit failed: %s" % exc)
            return {"status": "failed", "reason": "submit-failed"}
        run_id = env.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            _log("open_ge submit returned no run_id: %r" % env)   # submit-time validation error
            return {"status": "failed", "reason": "submit-rejected"}
        # Poll only until the total budget minus a fetch reserve, and never past the poll-window cap.
        poll_deadline = min(start + _GE_OPEN_TOTAL_BUDGET_S - _GE_MIN_FETCH_RESERVE_S,
                            time.monotonic() + _GE_RENDER_POLL_DEADLINE_S)
        status = _poll_ge_terminal(forwarder, run_id, deadline=poll_deadline,
                                   call_timeout_s=_GE_STATUS_CALL_TIMEOUT_S)
        if status != "completed":
            if status == "pending":
                popup_args = {"title": title}
                if args.get("context") is not None:
                    popup_args["context"] = args.get("context")
                if _schedule_ge_auto_open(
                    forwarder,
                    run_id,
                    popup_args,
                    client_host=client_host,
                    popup_followup=popup_followup,
                    popup_api_profile=popup_api_profile,
                ):
                    return {"status": "scheduled", "run_id": run_id,
                            "hint": "best-effort auto-open scheduled; artifact popup is not "
                                    "guaranteed; terminal failure opens a notice; keep run_id "
                                    "for recovery"}
                return {"status": "pending", "run_id": run_id,
                        "hint": "auto-open unavailable; poll until ready, then call open_ge_popup"}
            return {"status": "failed", "reason": "render-" + status, "run_id": run_id}
        # completed → fetch within the REMAINING budget so submit+poll+fetch stays under the drain,
        # then reuse the exact open_ge_popup tail (byte-isolated fetch + native spawn).
        remaining = _GE_OPEN_TOTAL_BUDGET_S - (time.monotonic() - start)
        if remaining < _GE_MIN_FETCH_RESERVE_S:   # budget spent: finish fetch+open off-thread
            popup_args = {"title": title}
            if args.get("context") is not None:
                popup_args["context"] = args.get("context")
            if _schedule_ge_auto_open(
                forwarder,
                run_id,
                popup_args,
                client_host=client_host,
                popup_followup=popup_followup,
                popup_api_profile=popup_api_profile,
            ):
                return {"status": "scheduled", "run_id": run_id,
                        "hint": "best-effort auto-open scheduled; artifact popup is not "
                                "guaranteed; terminal failure opens a notice; keep run_id "
                                "for recovery"}
            return {"status": "pending", "run_id": run_id,
                    "hint": "auto-open unavailable; call open_ge_popup with this run_id"}
        try:
            artifact = forwarder.get_ge_artifact(run_id, timeout_s=remaining)
        except ShellError as exc:
            _log("open_ge artifact fetch failed: %s" % exc)
            return {"status": "failed", "reason": "artifact-fetch-failed", "run_id": run_id}
        profile = "cursor-ge" if popup_api_profile == "cursor" else popup_api_profile
        try:
            popup_spec = launcher.PopupSpec(
                kind="ge", title=title, artifact=artifact
            )
            html = launcher.render_artifact_html(popup_spec)
        except (TypeError, ValueError) as exc:
            _log("open_ge render failed: %s" % exc)
            return {"status": "failed", "reason": "render-failed", "run_id": run_id}
        result = spawn_ge(html, title, artifact, run_id, route, profile)
        if result.get("status") == "failed":
            result = dict(result)
            result.setdefault("run_id", run_id)
        return result

    if name == "open_db_board":
        spec = args.get("spec")
        if not isinstance(spec, dict):
            return {"status": "failed", "reason": "spec required"}
        title = _display_window_title(
            args, "discussion_board", spec=spec, fallback="Discussion Board"
        )
        profile = "cursor-db" if popup_api_profile == "cursor" else popup_api_profile
        try:
            html = launcher.fetch_board_html(
                spec, base_url=forwarder.endpoint, token=forwarder.token,
                timeout_s=_DISPLAY_FETCH_TIMEOUT_S)
        except launcher.BoardFetchError as exc:
            _log("open_db_board /db/render failed: %s" % exc)
            return {"status": "failed", "reason": "render-fetch-failed"}
        if profile == "legacy":
            return session.spawn(html, title)
        return session.spawn(html, title, api_profile=profile)

    if name == "db_board_result":
        popup_id = (args.get("popup_id") or "").strip()
        if not popup_id:
            return {"status": "failed", "reason": "popup_id required"}
        raw_wait = args.get("wait_s")
        wait_s = float(raw_wait) if isinstance(raw_wait, (int, float)) else 50.0
        wait_s = max(1.0, min(wait_s, 55.0))   # bounded < Codex's 60s tool_timeout
        out = session.poll(popup_id, wait_s=wait_s)
        if out.get("status") == "done":
            return {"status": "done", "board": out.get("result")}   # the user's edited board
        return out   # open / dismissed / unknown

    return {"status": "failed", "reason": "unknown display tool"}   # unreachable (gated by _DISPLAY_TOOLS)


def _merge_display_tools(response: Dict[str, Any]) -> Dict[str, Any]:
    """Inject the local display tool schemas into a forwarded tools/list result so the host can
    discover + call them. The local names are RESERVED: any server entry colliding with a display
    tool is DROPPED and replaced by the local schema — since dispatch is always local for those
    names, the advertised schema must be the local one (server never shadows local behavior)."""
    result = response.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        kept = [t for t in result["tools"]
                if not (isinstance(t, dict) and t.get("name") in _DISPLAY_TOOLS)]
        kept.extend(_localize_tool(schema, _mcp_locale()) for schema in _DISPLAY_TOOL_SCHEMAS)
        result["tools"] = kept
    return response


def _merge_local_audit_tools(response: Dict[str, Any]) -> Dict[str, Any]:
    """Advertise the local-only completion callback alongside a host's server tools."""
    result = response.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        kept = [
            tool for tool in result["tools"]
            if not (isinstance(tool, dict) and tool.get("name") == "audit_skill_complete")
        ]
        kept.append(_localize_tool(_LOCAL_AUDIT_COMPLETE_TOOL, _mcp_locale()))
        result["tools"] = kept
    return response


def _display_suppression_reason(
    *,
    lite_mode: bool,
    offline_mode: bool,
    identity_enabled: bool,
    display_enabled: bool,
) -> Optional[str]:
    """Which conjunct is withholding the local display tools, or None if none is.

    The four gates at the tools/list merge are ANDed and, until this existed, failed
    IDENTICALLY from outside: open_ge simply was not in the list, with nothing said to
    the user or the calling model. Diagnosing one cost a multi-session bisect against a
    second machine. Order mirrors the merge condition so the reported cause is the one a
    reader will find first at the call site. Pure and keyword-only so the call site can
    stay a single expression and each branch is directly testable.
    """

    if lite_mode:
        return "lite session (hub not activated); local display is not served"
    if offline_mode:
        return "offline session (hub unreachable); the artifact cannot be rendered"
    if not identity_enabled:
        return (
            "host identity was enforced and did not match; the preceding "
            "'host identity status=' line carries the reported= name to adapt to"
        )
    if not display_enabled:
        return "transport capability gate has display disabled for this host"
    return None


def _display_capability_disabled() -> Dict[str, Any]:
    result = _tool_content(
        {"status": "failed", "reason": "capability-disabled"}
    )
    result["isError"] = True
    return result


def _dispatch_display(
    forwarder: "Forwarder",
    rid: Any,
    params: Dict[str, Any],
    emit,
    sem,
    client_host: Optional[str],
    popup_followup: bool,
    popup_api_profile: str,
) -> None:
    """Worker-thread body: run a local display tool and emit its response under the stdout lock,
    then release the concurrency slot. A display handler must NEVER take down the shim, so
    everything is contained here."""
    try:
        handler_kwargs = {"client_host": client_host}
        if not popup_followup or popup_api_profile != "legacy":
            handler_kwargs.update(
                popup_followup=popup_followup,
                popup_api_profile=popup_api_profile,
            )
        payload = _handle_display_call(
            forwarder,
            params.get("name"),
            params.get("arguments") or {},
            **handler_kwargs,
        )
        emit({"jsonrpc": "2.0", "id": rid, "result": _tool_content(payload)})
    except Exception as exc:  # aqg: worker boundary — contain everything, reply an error not silence
        _log("display handler crashed: %r" % exc)
        emit(_jsonrpc_error(rid, -32603, "display handler failed"))
    finally:
        sem.release()


# --- audit stop panel (client-side) --------------------------------------------------------------
# When the agent starts a hub audit -- via `audit_skill_submit` (the skill/workflow path) OR the raw
# `submit_audit` hub tool -- mirror what the standalone `audit` CLI does on POST /v1/audits: record the
# client-side active run and launch the stop panel so the user can watch + cancel it. The shim is the
# ONLY audit entry point an agent has, so without this the stop panel -- which SHIPS in the client but
# was only ever wired into the CLI -- never appears for an agent-driven audit. Both submit tools are
# gated here because an agent may reach the hub through either. Best-effort + fire-and-forget: the
# panel is a UX nicety, never a transport/correctness concern, so it runs off the transport thread and
# swallows every failure. `launch_stopper_if_available` self-gates (DE_SKIP_STOPPER_LAUNCH=1 opt-out;
# macOS launches the native binary / launch-agent / .app, Windows & Linux fall back to the
# cross-platform tkinter panel; skips when nothing is installed/shipped). No double-window risk from
# arming both tools: the panel is a cross-process singleton (client.stopper.panel.acquire_single_instance
# holds a lock file for the process lifetime), so a second launch -- CLI, or the other submit tool --
# is a no-op rather than a duplicate window.
_AUDIT_SUBMIT_TOOLS = frozenset({"audit_skill_submit", "submit_audit"})
_AUDIT_FOLLOWUP_TOOLS = frozenset({
    "audit_skill_status",
    "audit_skill_result",
    "audit_skill_events",
    "check_audit_status",
    "get_audit_result",
    "wait_audit",
})


def _is_audit_traffic(message: Dict[str, Any]) -> bool:
    """Whether this message is a hosted audit submission or a follow-up on one.

    Scopes the DE-026 no-replay latch: only audit traffic can leave a hosted run the client is
    unable to account for, so only audit traffic arms it. An unknown outcome on unrelated tool
    traffic must not close a legitimate offline DE Lite path."""
    if _is_defect_audit_submission(message):
        return True
    if message.get("method") != "tools/call":
        return False
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    name = _logical_mcp_tool_name(params.get("name"))
    return name in _AUDIT_SUBMIT_TOOLS or name in _AUDIT_FOLLOWUP_TOOLS


def _hosted_run_in_flight_error(
    run_id: str, dispatch: Optional[OutcomeUnknownError] = None
) -> Dict[str, Any]:
    """Reconcile contract for an outage that arrives while the hub still owns a live run.

    Reuses the documented ``request_outcome_unknown`` status rather than inventing a new enum a
    host would not recognise: from this client's side the OUTCOME of that run is precisely what is
    unknown while the hub is unreachable. ``run_id`` rides along so the caller can resume or
    reconcile the hosted audit — never start a second one (DE-026 / R-076, lock R26-03).

    ``dispatch`` is the session's unresolved post-dispatch audit, when there is one. Both
    identities then matter and neither substitutes for the other: ``run_id`` names the work the
    hub ACCEPTED, ``request_id`` names the follow-up that crossed the wire and never answered
    (DE-026 / R-076, F26-01). Returns a fresh dict — the exception's own ``data`` is never
    mutated, so the message that raised it keeps reporting exactly what it reported."""
    data = {
        "status": "request_outcome_unknown",
        "reason": "hosted_run_in_flight",
        "run_id": run_id,
        "request_sent": True,
        "retryable": False,
        "action": "reconcile",
    }
    if dispatch is not None:
        data["request_id"] = dispatch.data["request_id"]
    return data


def _hosted_work_blocking_local_audit(
    pending_audits: Dict[str, Dict[str, Any]],
    unreconciled_audit_dispatch: Optional[OutcomeUnknownError],
) -> Optional[Dict[str, Any]]:
    """The ONE DE-026 decision: does this session hold hosted audit work it cannot account for?

    Returns the reconcile error ``data`` when a local DE Lite second opinion must NOT be opened,
    or ``None`` when the session is free to degrade. Two very different code paths reach an
    outage and both must answer identically (DE-026 / R-076, F26-03): a config-bound transport
    discovers the outage in the pre-forward refresh and is SWAPPED for an ``OfflineForwarder``
    whose ``forward()`` returns a local run without ever raising, while a caller-owned transport
    raises ``OfflineError`` from the send preflight. Sharing this decision is what keeps the two
    from drifting — the earlier fix lived only in the error handler, so production bypassed it.

    A known hosted ``run_id`` outranks a bare unresolved dispatch because it is the identity the
    hub can actually be reconciled against; when both exist the reply carries both. Neither map
    is consulted for anything else, so a session that never reached the hub — unactivated Lite,
    or a genuine pre-dispatch outage — reads ``None`` here and keeps today's fallback."""
    in_flight_run = next(reversed(pending_audits), None)
    if in_flight_run is not None:
        return _hosted_run_in_flight_error(in_flight_run, unreconciled_audit_dispatch)
    if unreconciled_audit_dispatch is not None:
        # A copy: the emitted envelope must never alias the exception's own ``data``.
        return dict(unreconciled_audit_dispatch.data)
    return None


_TERMINAL_AUDIT_SUBMIT_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})


def _audit_run_payload(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse the ``{run_id, status, …}`` submission payload out of an `audit_skill_submit` MCP result
    (a ``_text_result``: ``{"content": [{"type": "text", "text": <json>}]}``). None on any unexpected
    shape / a failed submit that carries no run payload."""
    result = response.get("result") if isinstance(response, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if not (isinstance(content, list) and content and isinstance(content[0], dict)):
        return None
    text = content[0].get("text")
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    nested = payload.get("payload")
    if isinstance(nested, dict) and ("skill" in payload or "schema_version" in payload):
        run = dict(nested)
        nested_run_id = run.get("run_id")
        top_run_id = payload.get("run_id")
        if (not isinstance(nested_run_id, str) or not nested_run_id.strip()) \
                and isinstance(top_run_id, str) and top_run_id.strip():
            run["run_id"] = top_run_id
        if not isinstance(run.get("run_id"), str) or not run["run_id"].strip():
            return None
        return run
    return payload


def _audit_request_context(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Capture only the title/locale needed if a later audit response needs local fallback."""
    title = _explicit_user_audit_topic(message)
    if title is None:
        return None
    return {"title": title, "ui_locale": _audit_ui_locale(message)}


def _active_audit_context(run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Recover display metadata from the private client run registry after MCP restart."""
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    try:
        from client import runner

        registry = runner.load_active_runs_registry()
        runs = registry.get("runs") if isinstance(registry, dict) else None
        run = runs.get(run_id) if isinstance(runs, dict) else None
    except Exception as exc:  # aqg: top-level boundary -- registry is display metadata only
        _log("active audit context recovery skipped: %r" % exc)
        return None
    if not isinstance(run, dict) or run.get("local") is True:
        return None
    title = run.get("title")
    locale = run.get("ui_locale")
    if not isinstance(title, str):
        title = ""
    if not isinstance(locale, str):
        locale = None
    return {"title": title, "ui_locale": locale}


def _audit_response_context(response: Any) -> Optional[Dict[str, Any]]:
    """Read only structured display metadata from a received audit response."""
    def find(value: Any) -> Optional[Dict[str, Any]]:
        if isinstance(value, dict):
            title = value.get("title")
            locale = value.get("ui_locale")
            if isinstance(title, str) and title.strip():
                return {"title": title.strip(), "ui_locale": locale if isinstance(locale, str) else None}
            for nested in value.values():
                found = find(nested)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple)):
            for item in value:
                found = find(item)
                if found is not None:
                    return found
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return None
            if isinstance(decoded, (dict, list)):
                return find(decoded)
        return None

    return find(response)


def _audit_response_run_id(response: Any) -> Optional[str]:
    payload = _audit_run_payload(response) if isinstance(response, dict) else None
    if isinstance(payload, dict):
        for field in ("run_id", "audit_id"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()

    # Some hosted wrappers put the identifier beside `content`, or use `audit_id` in a nested
    # payload. Read only these two protocol fields; never scan arbitrary response prose for an ID.
    def find_identifier(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            for field in ("run_id", "audit_id"):
                candidate = value.get(field)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for nested in value.values():
                found = find_identifier(nested)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple)):
            for item in value:
                found = find_identifier(item)
                if found is not None:
                    return found
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return None
            if isinstance(decoded, (dict, list)):
                return find_identifier(decoded)
        return None

    return find_identifier(response)


def _audit_response_status(response: Any) -> Optional[str]:
    """Read an audit status from normal or JSON-text-wrapped follow-up responses."""
    payload = _audit_run_payload(response) if isinstance(response, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get("status"), str):
        return payload["status"].strip().lower()

    def find_status(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            status = value.get("status")
            if isinstance(status, str) and status.strip():
                return status.strip().lower()
            for nested in value.values():
                found = find_status(nested)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple)):
            for item in value:
                found = find_status(item)
                if found is not None:
                    return found
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return None
            if isinstance(decoded, (dict, list)):
                return find_status(decoded)
        return None

    return find_status(response)


def _audit_request_run_id(message: Dict[str, Any]) -> Optional[str]:
    """Read a follow-up run id without treating arbitrary tool arguments as audit state."""
    if message.get("method") != "tools/call":
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if _logical_mcp_tool_name(params.get("name")) not in _AUDIT_FOLLOWUP_TOOLS:
        return None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return None
    for field in ("run_id", "audit_id"):
        run_id = arguments.get(field)
        if isinstance(run_id, str) and run_id.strip():
            return run_id.strip()
    return None


def _audit_fallback_context(
    message: Dict[str, Any], response: Any = None
) -> Optional[Dict[str, Any]]:
    """Resolve title/locale without making them part of the balance decision."""
    request_context = _audit_request_context(message)
    if request_context is not None:
        return request_context

    run_id = _audit_request_run_id(message) or _audit_response_run_id(response)
    registry_context = _active_audit_context(run_id)
    if registry_context is not None:
        return registry_context

    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if _logical_mcp_tool_name(params.get("name")) in _AUDIT_FOLLOWUP_TOOLS:
        response_context = _audit_response_context(response)
        if response_context is not None:
            return response_context

    # A recognized audit follow-up plus the authoritative marker is enough to select Lite. A
    # missing title is a metadata loss, not a reason to return the server failure unchanged.
    if _logical_mcp_tool_name(params.get("name")) in _AUDIT_FOLLOWUP_TOOLS and run_id is not None:
        return {"title": "", "ui_locale": None}
    return None


def _remove_hosted_run_for_local_fallback(run_id: Optional[str]) -> None:
    """Remove the already-visible Hosted row before adding its local replacement."""
    if not run_id:
        return
    try:
        from client import runner

        runner.forget_active_run(run_id)
    except Exception as exc:  # aqg: top-level boundary -- local panel cleanup cannot break MCP
        _log("hosted row cleanup before local fallback failed: %r" % exc)


_STOPPER_COMPLETION_SLOTS = threading.BoundedSemaphore(8)


def _schedule_stopper_completion(message: Dict[str, Any], response: Dict[str, Any]) -> None:
    """Bound optional display workers; neither parsing nor disk locks may hold up MCP replies."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    name = _logical_mcp_tool_name(params.get("name"))
    if not isinstance(name, str) or name not in _AUDIT_FOLLOWUP_TOOLS:
        return
    if not _STOPPER_COMPLETION_SLOTS.acquire(blocking=False):
        _log("audit completion sync skipped: worker capacity exhausted")
        return

    def sync() -> None:
        try:
            _sync_stopper_completion(message, response)
        except Exception as exc:  # aqg: top-level boundary — optional UI parsing must not lose results
            _log("audit completion sync skipped: %s" % type(exc).__name__)
        finally:
            _STOPPER_COMPLETION_SLOTS.release()

    try:
        threading.Thread(target=sync, daemon=True, name="de-stopper-completion").start()
    except Exception as exc:  # aqg: top-level boundary — thread creation is best-effort UI work
        _STOPPER_COMPLETION_SLOTS.release()
        _log("audit completion sync skipped: %s" % type(exc).__name__)


def _sync_stopper_completion(message: Dict[str, Any], response: Dict[str, Any]) -> None:
    """A completed MCP read must expire its local row even when no GUI is running."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if _logical_mcp_tool_name(params.get("name")) not in _AUDIT_FOLLOWUP_TOOLS:
        return
    if not isinstance(response, dict):
        return
    result = response.get("result")
    if response.get("error") is not None or not isinstance(result, dict) or result.get("isError"):
        return
    view = _audit_run_payload(response)
    run_id = _audit_request_run_id(message)
    # Use the explicit run-level payload, never a nested voice's status or an ID from prose.
    if not run_id or not isinstance(view, dict) or view.get("local") is True:
        return
    if any(view.get(field, run_id) != run_id for field in ("run_id", "audit_id")):
        return
    status = view.get("status")
    if not isinstance(status, str) or status.strip().lower() not in _TERMINAL_AUDIT_SUBMIT_STATUSES:
        return
    try:
        from client import runner

        runner.sync_hosted_run_completion(run_id, {**view, "status": status.strip().lower()})
    except Exception as exc:  # aqg: top-level boundary — UI state must not break result delivery
        _log("audit completion sync skipped: %s" % type(exc).__name__)


def _spawn_stopper_for_audit(
    response: Dict[str, Any], ui_locale: Optional[str] = None
) -> None:
    """Best-effort: record the client active run + launch the native stop panel for a just-submitted
    audit. Runs on a daemon thread (never blocks the transport); every failure is swallowed. No-op
    unless the submit returned a ``run_id`` with a non-terminal status. Async audit submissions
    contractually return ``status=queued`` (skills/audit/references/hosted-workflow.md); synchronous workflow results
    omit status, and already-terminal submissions have nothing for the panel to poll."""
    if os.getenv("DE_SKIP_STOPPER_LAUNCH") == "1":
        return
    try:
        payload = _audit_run_payload(response)
        run_id = payload.get("run_id") if payload else None
        if not isinstance(run_id, str) or not run_id.strip():
            return  # failed / rate-limited / malformed submit → no active run, no panel
        status = payload.get("status")
        if not isinstance(status, str) or not status.strip() \
                or status.strip().lower() in _TERMINAL_AUDIT_SUBMIT_STATUSES:
            return  # synchronous result / terminal submit → no pollable active run
        from client import runner  # lazy: keep the transport path free of GUI/config deps
        if payload.get("local") is True:
            _save_local_audit_run(payload, ui_locale)
        else:
            hosted_payload = dict(payload)
            hosted_payload["ui_locale"] = runner.normalize_ui_locale(
                ui_locale if ui_locale is not None else payload.get("ui_locale")
            )
            runner.save_active_run(hosted_payload)
        runner.launch_stopper_if_available(prefer_shipped=False)
    except Exception as exc:  # aqg: top-level boundary — a UX panel must never break the MCP transport
        _log("audit stop panel launch failed: %r" % exc)  # diagnosable, still swallowed


def _save_local_audit_run(
    payload: Dict[str, Any], ui_locale: Optional[str] = None
) -> bool:
    """Persist a validated local run independently of the optional native panel."""
    from client import runner

    run_id = payload.get("run_id")
    status = payload.get("status")
    if (
        payload.get("local") is not True
        or not isinstance(run_id, str)
        or not run_id.strip()
        or not isinstance(status, str)
        or not status.strip()
        or status.strip().lower() in _TERMINAL_AUDIT_SUBMIT_STATUSES
    ):
        return False
    local_kwargs = {
        "title": payload.get("title"),
        "status": status.strip().lower(),
        "surface": payload.get("local_surface"),
    }
    if payload.get("degrade_reason") is not None:
        local_kwargs["degrade_reason"] = payload.get("degrade_reason")
    if payload.get("degrade_action") is not None:
        local_kwargs["degrade_action"] = payload.get("degrade_action")
    if ui_locale is not None:
        local_kwargs["ui_locale"] = ui_locale
    runner.save_local_advisory_run(run_id, **local_kwargs)
    return True


def _launch_stopper_for_saved_audit() -> None:
    """Best-effort GUI launch after local state is already durable."""
    try:
        from client import runner

        runner.launch_stopper_if_available(prefer_shipped=False)
    except Exception as exc:  # aqg: top-level boundary — optional GUI cannot undo local state
        _log("audit stop panel launch failed: %r" % exc)


def _local_audit_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return validated local-audit args for an interactive MCP tool call."""
    if message.get("method") != "tools/call":
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if _logical_mcp_tool_name(params.get("name")) != "audit_skill_submit":
        return None
    return _local_audit_args(params.get("arguments"))


def _capability_gate_for_host(
    client_host: Optional[str],
) -> TransportCapabilityGate:
    """Build a transport gate from immutable host-registry metadata."""
    adapter = mcp_config.host_adapter(client_host)
    if adapter is None:
        return TransportCapabilityGate.static(
            display=False, followup=False, stopper=False
        )
    if adapter.runtime_display_policy:
        gate = TransportCapabilityGate.conditional(
            followup=adapter.popup_followup,
            stopper=adapter.audit_stop_panel,
        )
        probes = None
        if PRODUCTION_CURSOR_WINDOWS_FIXTURE.enabled:
            try:
                # Imported HERE, not at module scope: this is the shim's only use of a
                # Win32-only module, and importing it eagerly is what let a module-scope
                # defect in it (a 24-byte `_GUID` off Windows) take the whole MCP server
                # down at startup on macOS. The struct is fixed, but the coupling should
                # not be able to do that again.
                from .cursor_windows_native import default_cursor_windows_probes

                probes = default_cursor_windows_probes()
            except Exception:  # aqg: top-level boundary -- retain M1 forwarding
                pass
        gate.complete_preflight(
            run_cursor_windows_preflight(
                fixture=PRODUCTION_CURSOR_WINDOWS_FIXTURE,
                probes=probes,
            )
        )
        return gate
    return TransportCapabilityGate.static(
        display=adapter.local_display_tools,
        followup=adapter.popup_followup,
        stopper=adapter.audit_stop_panel,
    )


# How long a degraded process waits before spending another recovery probe. The refresh below runs
# before EVERY message, and on a dead network each probe costs up to PROBE_TIMEOUT_S — so an
# offline session charged the user that much per message merely to re-ask a question whose answer
# had not changed. Recovery is not latency-sensitive (noticing a restored hub within this window is
# indistinguishable from instant to a human), while the probe very much is.
_DEGRADED_REPROBE_INTERVAL_S = 15.0
_last_degraded_probe_s: Optional[float] = None


def _degraded_reprobe_due() -> bool:
    """Whether a degraded process should spend a recovery probe on THIS message.

    Monotonic, not wall clock: a clock adjustment mid-session must not strand a process in its
    degraded surface. The first call after degradation always probes, so the existing
    recheck-before-the-next-request behaviour is preserved for the message where it matters.
    """
    global _last_degraded_probe_s

    now = time.monotonic()
    last = _last_degraded_probe_s
    if last is not None and now - last < _DEGRADED_REPROBE_INTERVAL_S:
        return False
    _last_degraded_probe_s = now
    return True


def _clear_degraded_reprobe_clock() -> None:
    """Forget the probe window so a LATER degradation probes immediately instead of inheriting
    the timestamp of the outage it just recovered from."""
    global _last_degraded_probe_s

    _last_degraded_probe_s = None


def _refresh_degraded_forwarder(
    forwarder: Any,
    lite_mode: bool,
    offline_mode: bool,
    *,
    force: bool = False,
) -> tuple[Any, bool, bool]:
    """Re-evaluate a degraded MCP surface before the next request.

    The launcher process can outlive activation and a transient Hub outage. Keeping the
    startup-selected Lite/offline object forever makes a recovered device look degraded even
    though its current config and network are healthy. Lifecycle and explicit audit requests can
    force this check; unrelated hosted traffic keeps its existing latency and transport path.
    """
    if not force and not lite_mode and not offline_mode:
        return forwarder, lite_mode, offline_mode
    if force and not lite_mode and not offline_mode and not getattr(
        forwarder, "_config_bound", False
    ):
        # A caller-owned transport may deliberately point at a fixture or proxy. Refresh only
        # transports resolved from the installed config.
        return forwarder, lite_mode, offline_mode
    if not force and not _degraded_reprobe_due():
        # Still degraded and it is not time to ask again: keep the surface the caller already has.
        # Skipped BEFORE from_config() so a rate-limited message pays neither the probe nor the
        # config read.
        return forwarder, lite_mode, offline_mode
    try:
        timeout_s = getattr(forwarder, "timeout_s", DEFAULT_TIMEOUT_S)
        current = Forwarder.from_config(timeout_s=timeout_s)
    except ActivationRequiredError:
        if lite_mode:
            return forwarder, True, False
        if offline_mode:
            # This process already proved it started activated and entered OfflineForwarder only
            # because the Hub was unreachable. A later missing token cannot rewrite that evidence.
            return forwarder, False, True
        # A config-bound hosted transport is equally strong evidence that this process started
        # activated. A transient missing/partially-written config must not silently reroute an
        # explicit cross-vendor audit to DE Lite; a fresh process will resolve the current config.
        return forwarder, False, False
    except ShellError as exc:
        _log("degraded surface refresh skipped: %s" % type(exc).__name__)
        return forwarder, lite_mode, offline_mode

    if hub_reachable(current.endpoint):
        _clear_degraded_reprobe_clock()
        return current, False, False
    return OfflineForwarder(), False, True


def serve(
    forwarder: Forwarder,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    on_ready=None,
    on_startup_event=None,
    client_host: Optional[str] = None,
    capability_gate: Optional[TransportCapabilityGate] = None,
    initialize_matcher: Optional[Callable[[Dict[str, Any]], bool]] = None,
    volatile_display_check: Optional[Callable[[], VolatileResult]] = None,
) -> int:
    """Run the stdio forwarding loop until stdin closes (EOF).

    Forwarded transport stays serial (the hub handles those in ms). Local display tool calls are
    dispatched to daemon WORKER threads so a bounded db_board_result poll (≤55s) never stalls
    forwarding of other MCP messages (design D1/§3). All stdout writes go through one lock so the
    main loop and the workers never interleave a line."""
    # The recovery-probe window belongs to a serve SESSION, not to the module: a fresh loop must
    # ask the network again rather than inherit the timestamp of some earlier process's outage.
    _clear_degraded_reprobe_clock()
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    emit_lock = threading.Lock()
    display_slots = threading.Semaphore(_MAX_DISPLAY_WORKERS)   # backpressure on local display work
    lite_mode = isinstance(forwarder, LiteForwarder)
    offline_mode = isinstance(forwarder, OfflineForwarder)
    # DE-026 / R-076: the audit request this session put on the wire whose outcome it never
    # learned, if any. OutcomeUnknownError protects the message that raised it, but the hub may
    # already own that run, so the no-replay contract has to outlive the message: while this is
    # set, a later connect-probe failure is not accepted as proof of a PRE-dispatch outage and
    # cannot open a local DE Lite second opinion for an intent the hub may already be serving.
    # Session-scoped by design — a fresh process re-reads the network instead of inheriting it.
    unreconciled_audit_dispatch: Optional[OutcomeUnknownError] = None
    if isinstance(forwarder, Forwarder) and not hub_reachable(forwarder.endpoint):
        # Probe once before MCP traffic: an activated host still completes the handshake locally,
        # but no request, popup sweep, or local display implementation is allowed during outage.
        forwarder = OfflineForwarder()
        offline_mode = True
    registration_host_supplied = client_host is not None
    resolved_client_host = _normalize_client_host(client_host)
    gate_injected = capability_gate is not None
    transport_gate = capability_gate or _capability_gate_for_host(resolved_client_host)
    registered_adapter = mcp_config.host_adapter(resolved_client_host)
    identity_enforced = bool(
        registration_host_supplied
        and registered_adapter is not None
        and registered_adapter.require_observed_identity
    )
    identity_optional_features_enabled = not identity_enforced
    unverified_lite_stopper = False
    initialize_matcher = initialize_matcher or (
        lambda params: (
            resolved_client_host == "cursor"
            and cursor_initialize_matches_fixture(
                params, PRODUCTION_CURSOR_WINDOWS_FIXTURE
            )
        )
    )
    def emit(payload: Dict[str, Any]) -> None:
        with emit_lock:
            _emit(stdout, payload)

    def startup_event(event: str, phase: str, **fields) -> None:
        if on_startup_event is None:
            return
        try:
            on_startup_event(event, phase, **fields)
        except Exception:
            # Startup evidence is best-effort and must never alter MCP behavior.
            return

    def active_forwarder_kind() -> str:
        if lite_mode:
            return "lite"
        if offline_mode:
            return "offline"
        return "hosted"

    # Lite and offline activated sessions never import or touch popup state.
    if not lite_mode and not offline_mode:
        try:
            from client.popup import session as _session
            _session.sweep()
        except Exception as exc:  # aqg: top-level boundary — cleanup must never block startup
            _log("popup sweep skipped: %r" % exc)

    if on_ready is not None:
        on_ready()
    startup_event(
        "stdio_loop_ready",
        "stdio",
        forwarder=active_forwarder_kind(),
        outcome="ready",
    )

    workers: list = []
    pending_audits: Dict[str, Dict[str, Any]] = {}
    for line in stdin:
        workers = [w for w in workers if w.is_alive()]   # prune finished workers every iteration
        line = line.strip()
        # A UTF-8 BOM (Windows pipes/editors can prepend one) is NOT whitespace, so str.strip()
        # leaves it in place and json.loads would choke on it -- strip it (U+FEFF) explicitly.
        line = line.lstrip("\ufeff").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # DELIBERATE deviation from JSON-RPC 2.0: the spec says reply with a parse error whose
            # id is null, but the MCP TS-SDK JSONRPCError schema requires id in {string, number}
            # and tears the whole connection down on id:null ("Could not attach"). An unparseable
            # line has no recoverable id to answer anyway, so we log and skip rather than emit a
            # stream-killing id:null error. Peers rely on their own request timeouts for the (rare)
            # case where a real request was the thing that got corrupted.
            _log("dropping unparseable stdin line (%d chars)" % len(line))
            continue
        if not isinstance(message, dict):
            # Same deliberate JSON-RPC 2.0 deviation as above (would be an id:null invalid-request).
            _log("dropping non-object JSON-RPC message")
            continue

        is_request = "id" in message and message.get("id") is not None
        method = message.get("method")
        raw_params = message.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        tool_name = _logical_mcp_tool_name(params.get("name"))
        identity_matches_initialize = False

        if method == "initialize":
            startup_event(
                "initialize_received",
                "initialize",
                forwarder=active_forwarder_kind(),
                outcome="started",
            )

        if method == "initialize" and registration_host_supplied:
            identity = mcp_config.resolve_host_identity(
                resolved_client_host, raw_params
            )
            if identity.status != "matched":
                reported = raw_params.get("clientInfo") if isinstance(
                    raw_params, dict
                ) else None
                reported_name = (
                    reported.get("name") if isinstance(reported, dict) else None
                )
                _log(
                    # The REPORTED name is the actionable part: an upstream host that
                    # renames its connector id can only be re-adapted by someone who can
                    # read back what it actually sent. It is a product identifier, never
                    # user content.
                    "host identity status=%s diagnostic=%s declared=%s observed=%s "
                    "reported=%r"
                    % (
                        identity.status,
                        identity.diagnostic or "none",
                        identity.declared_host or "none",
                        identity.observed_host or "none",
                        reported_name,
                    )
                )
            identity_matches_initialize = (
                not identity_enforced or identity.optional_features_enabled
            )
            unverified_lite_stopper = bool(
                lite_mode
                and registered_adapter is not None
                and registered_adapter.unverified_lite_stopper
                and registered_adapter.audit_stop_panel
                and identity.status in {"missing", "unknown"}
                and identity.observed_host is None
                and identity.diagnostic is None
            )
            if identity_enforced:
                # Publish readiness only after the forwarded initialize
                # receives a successful result.
                identity_optional_features_enabled = False
            if identity_enforced and not identity_matches_initialize:
                # An explicitly injected policy/test gate is still subordinate
                # to the evidence-backed host-identity boundary.
                transport_gate.close()
                transport_gate = TransportCapabilityGate.static(
                    display=False, followup=False, stopper=False
                )

        # Explicit registration metadata wins. Legacy registrations can still
        # resolve the host from the standard MCP initialize handshake.
        if (
            method == "initialize"
            and not registration_host_supplied
            and resolved_client_host is None
        ):
            resolved_client_host = _client_host_from_initialize(params)
            if not gate_injected:
                transport_gate.close()
                transport_gate = _capability_gate_for_host(resolved_client_host)

        # Local advisory completion is a client-owned state transition. It must never be sent to
        # the Hub, including when the current session is activated but has just entered a local
        # entitlement/offline fallback.
        if method == "tools/call" and tool_name == "audit_skill_complete":
            response = _local_audit_completion_response(
                message.get("id"), params.get("arguments")
            )
            if is_request:
                emit(response)
            continue

        # Product trigger gate: no user audit topic means no hosted request and no local run.
        # The routing contract owns intent classification; this transport-level check prevents a
        # missing-topic defect review from reaching any execution boundary.
        if (
            _is_defect_audit_submission(message)
            and _explicit_user_audit_topic(message) is None
        ):
            if is_request:
                emit(_explicit_audit_required_response(message.get("id")))
            continue

        # Re-read current activation/reachability on lifecycle requests and explicit audits. This
        # prevents a long-lived MCP process from carrying a stale surface into a new session while
        # avoiding a probe on unrelated hosted traffic.
        lifecycle_refresh = method in {"initialize", "tools/list"}
        audit_refresh = (
            method == "tools/call"
            and _is_defect_audit_submission(message)
            and _explicit_user_audit_topic(message) is not None
        )
        forwarder, lite_mode, offline_mode = _refresh_degraded_forwarder(
            forwarder,
            lite_mode,
            offline_mode,
            force=lifecycle_refresh or audit_refresh,
        )

        # Local display tool → run OFF the transport thread (a bounded poll must not block forwarding).
        if (
            not lite_mode
            and not offline_mode
            and is_request
            and method == "tools/call"
            and params.get("name") in _DISPLAY_TOOLS
        ):
            display_check = (
                transport_gate.check_display(volatile_display_check)
                if identity_optional_features_enabled
                else DisplayCheckResult(False, "display-capability-disabled")
            )
            if not display_check.allowed:
                emit({
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": _display_capability_disabled(),
                })
                continue
            if not display_slots.acquire(blocking=False):
                # saturated: reject fast rather than stacking unbounded threads/popups.
                emit({"jsonrpc": "2.0", "id": message.get("id"),
                      "result": _tool_content({"status": "failed", "reason": "busy"})})
                continue
            worker = threading.Thread(
                target=_dispatch_display,
                args=(
                    forwarder,
                    message.get("id"),
                    params,
                    emit,
                    display_slots,
                    resolved_client_host,
                    (
                        identity_optional_features_enabled
                        and transport_gate.followup_enabled
                    ),
                    (
                        mcp_config.host_adapter(resolved_client_host).popup_api_profile
                        if mcp_config.host_adapter(resolved_client_host) is not None
                        else "legacy"
                    ),
                ),
                daemon=True,
            )
            worker.start()
            workers.append(worker)
            continue

        # DE-026 / R-076, F26-03: the no-Lite decision has to be made BEFORE any forwarder can
        # answer. `_refresh_degraded_forwarder` above may already have swapped a config-bound
        # transport for an OfflineForwarder — the production shape — and that object RETURNS a
        # local DE Lite run instead of raising, so a guard living only in the error handler
        # below never sees the message that matters. Scoped to a request that would actually
        # open a local audit; ordinary traffic and hosted recovery are untouched.
        if (
            is_request
            and (lite_mode or offline_mode)
            and _local_audit_request(message) is not None
        ):
            blocked = _hosted_work_blocking_local_audit(
                pending_audits, unreconciled_audit_dispatch
            )
            if blocked is not None:
                emit(_jsonrpc_error(
                    message.get("id"), -32001, _OUTCOME_UNKNOWN_MESSAGE, blocked
                ))
                continue

        # Everything else: forward inline (fast hub round-trip).
        forced_local_fallback = False
        forced_local_panel_locale = None
        try:
            response = forwarder.forward(
                _with_client_host_metadata(message, resolved_client_host)
            )
        except EntitlementBlockedError as exc:
            fallback_context = _audit_fallback_context(message)
            if (
                is_request
                and fallback_context is not None
                and exc.data.get("local_advisory_available") is True
            ):
                response = _local_advisory_response(
                    message.get("id"),
                    reason=exc.data["reason"],
                    action=exc.data.get("action"),
                    title=fallback_context.get("title"),
                    ui_locale=fallback_context.get("ui_locale"),
                )
                forced_local_fallback = True
                forced_local_panel_locale = fallback_context.get("ui_locale")
                blocked_run_id = _audit_request_run_id(message)
                if blocked_run_id is not None:
                    # DE-026 / R-076, F26-02: this downgrade is DETERMINISTIC — the hosted run is
                    # over and one DE Lite advisory has replaced it. Retire it from the in-flight
                    # map exactly as the response-carried marker below does, or the finished run
                    # stays a phantom that blocks every later genuine offline audit this session.
                    pending_audits.pop(blocked_run_id, None)
                _remove_hosted_run_for_local_fallback(blocked_run_id)
            else:
                if is_request:
                    emit(_jsonrpc_error(message.get("id"), -32001, str(exc), exc.data))
                    if method == "initialize":
                        startup_event(
                            "initialize_response_emitted",
                            "initialize",
                            forwarder=active_forwarder_kind(),
                            outcome="error",
                            reason_code="initialize_forward_error",
                            error_type=type(exc).__name__,
                        )
                continue
        except ShellError as exc:
            # Every failed initialize must close the conditional capability gate, including the
            # pre-send OfflineError path below. Leaving READY_FOR_INITIALIZE live retains its
            # native lease and makes the failed handshake look pending for the rest of the session.
            if method == "initialize":
                transport_gate.fail_initialize()
            if isinstance(exc, OfflineError):
                # The request-side preflight proved that no authenticated request was sent. An
                # explicit high-level audit may therefore use the same local service-unavailable
                # advisory as a startup-detected outage; all other calls remain hard stops.
                forwarder = OfflineForwarder()
                lite_mode = False
                offline_mode = True
                local_args = _local_audit_request(message)
                if is_request and local_args is not None:
                    # The send preflight proved this message was NOT dispatched, but the session
                    # may still owe the hub an answer for an earlier one. Same decision as the
                    # pre-forward gate above, deliberately the same helper: an outage reached by
                    # a raise and an outage reached by a forwarder swap must refuse identically.
                    blocked = _hosted_work_blocking_local_audit(
                        pending_audits, unreconciled_audit_dispatch
                    )
                    if blocked is not None:
                        emit(_jsonrpc_error(
                            message.get("id"), -32001, _OUTCOME_UNKNOWN_MESSAGE, blocked
                        ))
                        continue
                    response = forwarder.forward(message)
                else:
                    if is_request:
                        emit(_jsonrpc_error(message.get("id"), -32001, str(exc), exc.data))
                        if method == "initialize":
                            startup_event(
                                "initialize_response_emitted",
                                "initialize",
                                forwarder=active_forwarder_kind(),
                                outcome="error",
                                reason_code="initialize_forward_error",
                                error_type=type(exc).__name__,
                            )
                    continue
            else:
                if isinstance(exc, OutcomeUnknownError) and _is_audit_traffic(message):
                    # The request reached the wire, so the hub may have accepted this audit. Latch
                    # it for the rest of the session: nothing observed from this client afterwards
                    # can prove the run does not exist, and only a reconcile can retire it.
                    unreconciled_audit_dispatch = exc
                if is_request:
                    # OfflineError may carry an advisory marker (de_audit defect review); a plain
                    # ShellError carries none, so the emitted error is byte-identical to before.
                    data = exc.data if isinstance(
                        exc, (OfflineError, OutcomeUnknownError)
                    ) else None
                    emit(_jsonrpc_error(message.get("id"), -32001, str(exc), data))
                    if method == "initialize":
                        startup_event(
                            "initialize_response_emitted",
                            "initialize",
                            forwarder=active_forwarder_kind(),
                            outcome="error",
                            reason_code="initialize_forward_error",
                            error_type=type(exc).__name__,
                        )
                continue
        local_args = _local_audit_request(message)
        account_action = _account_action_response(response)
        request_context = _audit_request_context(message)
        response_audit_payload = _audit_run_payload(response)
        response_is_local = bool(
            response_audit_payload is not None
            and response_audit_payload.get("local") is True
        )
        response_run_id = _audit_response_run_id(response)
        response_status = _audit_response_status(response)
        request_run_id = _audit_request_run_id(message)
        correlation_run_id = response_run_id or request_run_id

        # Submit is asynchronous: the balance marker can arrive in a later wait/status/result
        # response. Keep only the display metadata needed for that one in-flight audit; never keep
        # artifact bytes, credentials, or server response text in this process-local map.
        if (
            request_context is not None
            and tool_name in _AUDIT_SUBMIT_TOOLS
            and response_run_id is not None
            and not response_is_local
        ):
            submitted_status = response_status
            if (
                isinstance(submitted_status, str)
                and submitted_status.strip().lower()
                not in _TERMINAL_AUDIT_SUBMIT_STATUSES
            ):
                pending_audits[response_run_id] = request_context

        fallback_context = request_context
        if fallback_context is None and correlation_run_id is not None:
            fallback_context = pending_audits.get(correlation_run_id)
        if fallback_context is None:
            fallback_context = _audit_fallback_context(message, response)
        local_fallback = forced_local_fallback
        local_panel_locale = forced_local_panel_locale
        if (
            is_request
            and account_action is not None
            and account_action.get("local_advisory_available") is True
            and fallback_context is not None
        ):
            response = _local_advisory_response(
                message.get("id"),
                reason=account_action["reason"],
                action=account_action.get("action"),
                title=fallback_context.get("title"),
                ui_locale=fallback_context.get("ui_locale"),
            )
            local_fallback = True
            local_panel_locale = fallback_context.get("ui_locale")
            if correlation_run_id is not None:
                pending_audits.pop(correlation_run_id, None)
                _remove_hosted_run_for_local_fallback(correlation_run_id)
        elif (
            correlation_run_id is not None
            and response_status in _TERMINAL_AUDIT_SUBMIT_STATUSES
        ):
            # DE-026 / R-076, F26-03 lock 6: a follow-up answer may report the terminal status
            # while omitting the run_id the REQUEST already named. Correlate on either identity
            # so a finished run is actually retired; the status still has to be terminal, and
            # `correlation_run_id` comes from this exchange alone, so no unrelated run is touched.
            pending_audits.pop(correlation_run_id, None)
        # A request MUST get a reply (JSON-RPC 2.0) — even if the server sent an
        # empty body, emit an error rather than leaving the client hanging on
        # that id. Notifications (no id) get no reply regardless.
        if is_request:
            if response:
                if method == "tools/call":
                    _schedule_stopper_completion(message, response)
                if method == "initialize":
                    if isinstance(response.get("result"), dict):
                        transport_gate.finalize_initialize(
                            matches=bool(initialize_matcher(params))
                        )
                        if identity_enforced:
                            identity_optional_features_enabled = (
                                identity_matches_initialize
                            )
                    elif transport_gate.stage == CapabilityStage.READY_FOR_INITIALIZE:
                        transport_gate.fail_initialize()
                if method == "tools/list":
                    adapter = mcp_config.host_adapter(resolved_client_host)
                    if adapter is not None:
                        response = _merge_local_audit_tools(response)
                    withheld = _display_suppression_reason(
                        lite_mode=lite_mode,
                        offline_mode=offline_mode,
                        identity_enabled=identity_optional_features_enabled,
                        display_enabled=transport_gate.display_enabled,
                    )
                    if withheld is None:
                        response = _merge_display_tools(response)
                    elif adapter is not None and adapter.local_display_tools:
                        # Only hosts that SHOULD have had them: staying quiet here is
                        # what made a missing open_ge undiagnosable from the outside.
                        _log("display tools withheld: %s" % withheld)
                elif method == "tools/call" and (
                    tool_name in _AUDIT_SUBMIT_TOOLS or local_fallback
                ):
                    # a hub audit just started via the MCP path → pop the client stop panel (off-thread,
                    # best-effort) so an agent-driven audit gets the same panel the CLI already does.
                    # Guard the spawn itself: Thread.start() runs on THIS transport thread and can raise
                    # (e.g. RuntimeError under thread exhaustion); it must never stop emit(response).
                    local_surface = lite_mode or offline_mode
                    if (
                        local_surface
                        and unverified_lite_stopper
                        and os.getenv("DE_SKIP_STOPPER_LAUNCH") != "1"
                    ):
                        try:
                            locale = (
                                local_panel_locale
                                if local_fallback
                                else _audit_ui_locale(message)
                            )
                            payload = _audit_run_payload(response)
                            if (
                                payload is not None
                                and _save_local_audit_run(payload, locale)
                            ):
                                threading.Thread(
                                    target=_launch_stopper_for_saved_audit,
                                    daemon=True,
                                ).start()
                        except Exception as exc:  # aqg: top-level boundary -- local UX side-effect
                            _log("audit stop panel spawn failed: %r" % exc)
                    elif transport_gate.stopper_enabled and (
                        identity_optional_features_enabled or local_surface
                    ):
                        try:
                            locale = (
                                local_panel_locale
                                if local_fallback
                                else _audit_ui_locale(message)
                            )
                            threading.Thread(
                                target=_spawn_stopper_for_audit,
                                args=((response,) if locale is None else (response, locale)),
                                daemon=True,
                            ).start()
                        except Exception as exc:  # aqg: top-level boundary -- optional UX side-effect
                            _log("audit stop panel spawn failed: %r" % exc)
                emit(response)
                if method == "initialize":
                    startup_event(
                        "initialize_response_emitted",
                        "initialize",
                        forwarder=active_forwarder_kind(),
                        outcome=(
                            "success" if isinstance(response.get("result"), dict) else "error"
                        ),
                        reason_code=(
                            None
                            if isinstance(response.get("result"), dict)
                            else "initialize_forward_error"
                        ),
                    )
            else:
                emit(_jsonrpc_error(message.get("id"), -32603, "empty response from server"))
                if method == "initialize":
                    startup_event(
                        "initialize_response_emitted",
                        "initialize",
                        forwarder=active_forwarder_kind(),
                        outcome="error",
                        reason_code="empty_initialize_response",
                    )

    # Drain in-flight display workers so their responses are flushed before we exit on EOF.
    for worker in workers:
        worker.join(timeout=60)
    transport_gate.close()
    return 0


def _emit(stdout: TextIO, payload: Dict[str, Any]) -> None:
    stdout.write(json.dumps(payload) + "\n")
    stdout.flush()


def main(argv: Optional[list] = None) -> int:
    import argparse

    # Force UTF-8 on the JSON-RPC stdio transport BEFORE any read. On Windows sys.stdin/stdout
    # default to the locale codec (e.g. GBK), which mangles non-ASCII tool args (Chinese, em-dash)
    # into lone surrogates and raises UnicodeEncodeError on forward. Guarded so an injected /
    # non-reconfigurable stream (tests) is left untouched.
    for _stream in (sys.stdin, sys.stdout):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # buffered data / non-reconfigurable stream
                pass

    parser = argparse.ArgumentParser(
        prog="de-shim",
        description="Decision Engine MCP-over-HTTP forwarding shim (transport only)",
    )
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--client-host",
        default=None,
        help="MCP host family supplied by the registration",
    )
    args = parser.parse_args(argv)
    try:
        forwarder = Forwarder.from_config(timeout_s=args.timeout_s)
    except ActivationRequiredError:
        forwarder = LiteForwarder()
    except ShellError as exc:
        print("de-shim: %s" % exc, file=sys.stderr)
        return 1
    return serve(forwarder, client_host=args.client_host)


if __name__ == "__main__":
    raise SystemExit(main())
