"""Client-facing entry to open a DB/GE popup and hand its result back.

Flow (the launch handoff):

    caller ──open_popup(spec)──▶ ensure pywebview ──▶ spawn native_shell (child)
      ▲                                                        │
      └────────────── result dict ◀── read result.json ◀──── user Submit/Dismiss

The popup BODY comes from the server in one of two shapes; either way this client only
DISPLAYS server-delivered output and the generation IP stays on the server:
  - GE artifact (``render_artifact_html``): the caller hands a finished
    ``{"kind": "svg"|"image", "data": ...}`` in on ``spec.artifact`` and this client wraps it
    in a display chrome.
  - DB board (``fetch_board_html`` → ``open_board_popup``): a FULL interactive HTML document,
    server-rendered from the diagram-stripped template, displayed verbatim on
    ``spec.html_body``. It is not a ``{kind,data}`` artifact, so it takes its own path — not
    the GE svg/image branch.
Swapping which body source is used changes nothing in the ``open_popup → spawn shell → read
result`` handoff.

Never opens a system browser: when the native backend is unavailable the call returns
a structured ``no-webview-backend`` outcome rather than degrading (Owner constraint).
"""

from __future__ import annotations

import argparse
import http.client
import html
import json
import math
import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from client import i18n
from client.http_safety import NoRedirect, read_within_budget  # shared hub-read guards (stdlib leaf)

from . import backend

NATIVE_SHELL_MODULE = "client.popup.native_shell"


def _default_surface_title(surface: str, locale_hint: Any = None,
                           fallback: str = "Decision Engine") -> str:
    """Localized fallback for OS-visible popup titles."""
    try:
        titles = i18n.shell(i18n.resolve_locale(locale_hint)).get("surface_title", {})
        title = titles.get(surface) if isinstance(titles, dict) else None
        return title if isinstance(title, str) and title else fallback
    except Exception:  # aqg: top-level boundary — title i18n must not block popup launch
        return fallback


def _prefixed_popup_title(title: str) -> str:
    stripped = title.strip()
    if stripped.startswith("Decision Engine - "):
        return stripped
    return "Decision Engine - " + stripped


def _json_for_script(obj: Any) -> str:
    """json.dumps that is safe to drop verbatim inside a <script> block.

    Plain ``json.dumps`` is NOT: a ``</script>`` (or a lone ``<`` / U+2028) inside a
    string value would close the block / break the parser. Escape the HTML/JS-significant
    characters to their ``\\uXXXX`` form — still valid JSON, no breakout surface."""
    text = json.dumps(obj, ensure_ascii=False)
    # Needles written as escape sequences (not literal separators) so the source
    # stays ASCII-visible — a raw U+2028/U+2029 in source is invisible + corruptible.
    for needle, escape in (
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("&", "\\u0026"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(needle, escape)
    return text


@dataclass
class PopupSpec:
    """What to show. ``kind`` selects the popup variant.

    Two mutually-exclusive body sources:
      - ``artifact`` — the GE path: a server-delivered ``{"kind": "svg"|"image",
        "data": ...}`` finished artifact that ``render_artifact_html`` wraps in a
        display chrome. ``None`` for callers that do not deliver a GE artifact.
      - ``html_body`` — the DB path: a FULL, self-contained interactive HTML
        document (the server-rendered discussion board), displayed VERBATIM. The
        DB board is not a ``{kind,data}`` artifact, so it bypasses the GE
        svg/image branch entirely (Owner 2026-07-08). When set, ``open_popup``
        writes it directly and ``artifact`` is ignored."""

    kind: str = "db"  # server-selected popup kind (e.g. "db" | "ge")
    title: str = "Decision Engine"
    note: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    artifact: Optional[Dict[str, str]] = None
    html_body: Optional[str] = None


class BoardFetchError(Exception):
    """The server ``/db/render`` fetch failed (unreachable / timeout / non-200 /
    empty body / oversized / wrong content-type / unexpected redirect). Every
    failure mode in ``fetch_board_html`` is normalized to THIS type so
    ``open_board_popup`` can map it to a structured ``render-fetch-failed``
    outcome — NEVER a browser fallback and never a local render (the template
    stays server-held; a fetch failure surfaces rather than degrading — the
    Owner "never opens a system browser" constraint)."""


# The /db/render opener is built PER CALL inside fetch_board_html (not module-level)
# so it can fold in https_context() — evaluated at call time to honor SSL_CERT_FILE /
# certifi, matching the request_json() path — while NoRedirect still refuses the
# token-leaking redirect.

# Hard ceiling on the board HTML we will read into memory — a compromised or
# buggy hub must not OOM the client. The server template is ~210 KB plus the
# data: URIs the client itself just sent, so 64 MB is generous headroom for a
# large multi-page board.
_MAX_BOARD_BYTES = 64 * 1024 * 1024
_MAX_LAYOUT_BYTES = 2 * 1024 * 1024
_MAX_LAYOUT_NODES = 512
_MAX_LAYOUT_EDGES = 2048
_MAX_LAYOUT_ITEMS = 30000
_MAX_LAYOUT_TEXT_CHARS = 1_000_000
_MAX_LAYOUT_DEPTH = 10
_MAX_CURSOR_BOARD_JSON_BYTES = 16 * 1024 * 1024
_MAX_CURSOR_BOARD_ITEMS = 30_000
_MAX_CURSOR_BOARD_TEXT_CHARS = 12 * 1024 * 1024
_MAX_CURSOR_BOARD_DEPTH = 12

def _validate_cursor_board_value(value: Any, *, token: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise BoardFetchError("Cursor board data must be an object")
    stage = value.get("stage")
    if stage not in ("kanban", "image", "document"):
        raise BoardFetchError("Cursor board data has an unsupported stage")
    allowed_fields = {
        "title",
        "stage",
        "columns",
        "image",
        "pages",
        "notes",
        "comments",
        "annotations",
    }
    if set(value) - allowed_fields:
        raise BoardFetchError("Cursor board data contains unknown fields")
    if stage == "kanban" and not isinstance(value.get("columns"), list):
        raise BoardFetchError("Cursor kanban board requires columns")
    if stage == "image" and not isinstance(value.get("image"), (str, dict)):
        raise BoardFetchError("Cursor image board requires image data")
    if stage == "document" and not isinstance(value.get("pages"), list):
        raise BoardFetchError("Cursor document board requires pages")
    if "title" in value and not isinstance(value["title"], str):
        raise BoardFetchError("Cursor board title must be text")
    if "notes" in value and not isinstance(value["notes"], str):
        raise BoardFetchError("Cursor board notes must be text")
    for field in ("comments", "annotations"):
        if field in value and not isinstance(value[field], list):
            raise BoardFetchError("Cursor board %s must be a list" % field)
    if stage == "kanban":
        for column in value["columns"]:
            if not isinstance(column, dict) or not isinstance(
                column.get("cards"), list
            ):
                raise BoardFetchError(
                    "Cursor kanban columns require object cards lists"
                )
            for field in ("title", "text"):
                if field in column and not isinstance(column[field], str):
                    raise BoardFetchError("Cursor kanban column text is invalid")
            if "id" in column and not isinstance(column["id"], (str, int)):
                raise BoardFetchError("Cursor kanban column id is invalid")
            for card in column["cards"]:
                if not isinstance(card, dict):
                    raise BoardFetchError("Cursor kanban cards must be objects")
                for field in ("title", "text"):
                    if field in card and not isinstance(card[field], str):
                        raise BoardFetchError("Cursor kanban card text is invalid")
                if "id" in card and not isinstance(card["id"], (str, int)):
                    raise BoardFetchError("Cursor kanban card id is invalid")

    stack = [(value, 0)]
    item_count = 0
    text_chars = 0
    while stack:
        current, depth = stack.pop()
        item_count += 1
        if item_count > _MAX_CURSOR_BOARD_ITEMS or depth > _MAX_CURSOR_BOARD_DEPTH:
            raise BoardFetchError("Cursor board data is too complex")
        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, str):
            text_chars += len(current)
            if token and token in current:
                raise BoardFetchError("Cursor board data contains protected credentials")
            if text_chars > _MAX_CURSOR_BOARD_TEXT_CHARS:
                raise BoardFetchError("Cursor board data contains too much text")
            continue
        if isinstance(current, (int, float)):
            if not _is_finite_number(current):
                raise BoardFetchError("Cursor board data contains a non-finite number")
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise BoardFetchError("Cursor board data contains an invalid key")
                text_chars += len(key)
                if token and token in key:
                    raise BoardFetchError("Cursor board data contains protected credentials")
                stack.append((item, depth + 1))
            if text_chars > _MAX_CURSOR_BOARD_TEXT_CHARS:
                raise BoardFetchError("Cursor board data contains too much text")
            continue
        raise BoardFetchError("Cursor board data contains a non-JSON value")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        raise BoardFetchError("Cursor board data is not valid JSON") from None
    if len(encoded) > _MAX_CURSOR_BOARD_JSON_BYTES:
        raise BoardFetchError("Cursor board data exceeds the size limit")
    return value


def _reject_cursor_token_in_value(value: Any, *, token: str) -> None:
    if not token:
        return
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if token in current:
                raise BoardFetchError("Cursor bridge data contains protected credentials")
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, dict):
            for key, item in current.items():
                if isinstance(key, str) and token in key:
                    raise BoardFetchError("Cursor bridge data contains protected credentials")
                stack.append(item)


class BoardLayoutError(Exception):
    """The authenticated ``/db/layout`` bridge failed.

    The native popup is loaded from a local file, so its page cannot safely call
    the remote endpoint itself.  This error deliberately carries only stable,
    local diagnostics; hub response bodies and the device token never cross the
    page bridge.
    """


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_layout_structure(value: Any) -> None:
    """Bound a JSON-like value before making the second, serialized copy."""
    stack = [(value, 0)]
    item_count = 0
    text_chars = 0
    while stack:
        current, depth = stack.pop()
        item_count += 1
        if item_count > _MAX_LAYOUT_ITEMS or depth > _MAX_LAYOUT_DEPTH:
            raise BoardLayoutError("layout request was too complex")
        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, str):
            text_chars += len(current)
            if text_chars > _MAX_LAYOUT_TEXT_CHARS:
                raise BoardLayoutError("layout request contained too much text")
            continue
        if isinstance(current, (int, float)):
            if not _is_finite_number(current):
                raise BoardLayoutError("layout request contained a non-finite number")
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise BoardLayoutError("layout request contained an invalid key")
                text_chars += len(key)
                stack.append((item, depth + 1))
            if text_chars > _MAX_LAYOUT_TEXT_CHARS:
                raise BoardLayoutError("layout request contained too much text")
            continue
        raise BoardLayoutError("layout request contained a non-JSON value")


def _validate_layout_request(layout_request: Any) -> Dict[str, Any]:
    """Validate the exact request envelope emitted by the server-rendered board."""
    if not isinstance(layout_request, dict):
        raise BoardLayoutError("layout request must be an object")
    if set(layout_request) - {"layout", "graph", "axes", "measure"}:
        raise BoardLayoutError("layout request contained unknown fields")
    layout = layout_request.get("layout")
    graph = layout_request.get("graph")
    if not isinstance(layout, str) or not layout.strip() or len(layout) > 64:
        raise BoardLayoutError("layout request contained an invalid layout")
    if not isinstance(graph, dict) or set(graph) != {"nodes", "edges"}:
        raise BoardLayoutError("layout request contained an invalid graph")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes, list) or len(nodes) > _MAX_LAYOUT_NODES:
        raise BoardLayoutError("layout request contained invalid nodes")
    if not isinstance(edges, list) or len(edges) > _MAX_LAYOUT_EDGES:
        raise BoardLayoutError("layout request contained invalid edges")
    for node in nodes:
        if not isinstance(node, dict):
            raise BoardLayoutError("layout request contained an invalid node")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id or len(node_id) > 256:
            raise BoardLayoutError("layout request contained an invalid node id")
    for edge in edges:
        if not isinstance(edge, dict) or "from" not in edge or "to" not in edge:
            raise BoardLayoutError("layout request contained an invalid edge")
        for endpoint in (edge["from"], edge["to"]):
            if (
                isinstance(endpoint, bool)
                or not isinstance(endpoint, (str, int))
                or len(str(endpoint)) > 256
            ):
                raise BoardLayoutError("layout request contained an invalid edge endpoint")
    for optional in ("axes", "measure"):
        if optional in layout_request and not isinstance(layout_request[optional], dict):
            raise BoardLayoutError("layout request contained invalid optional data")
    _validate_layout_structure(layout_request)
    return layout_request


def fetch_board_html(
    board_spec: Dict[str, Any],
    *,
    base_url: str,
    token: str,
    timeout_s: float = 30.0,
) -> str:
    """Fetch the finished DB board HTML from the server ``/db/render`` endpoint.

    POSTs the caller-built board spec (``{title?, stage, columns?/image?/pages?,
    notes?, comments?}``) with the Bearer device token and returns the
    server-rendered HTML body — a full interactive document, NOT a ``{kind,data}``
    artifact. The diagram-layout IP that the template omits stays server-held;
    only the finished, stripped board reaches the client.

    EVERY failure path raises ``BoardFetchError`` (the fail-closed contract):
    non-serializable spec, non-200, unreachable, timeout, an unexpected redirect,
    an oversized body, a non-``text/html`` body, an empty body, or a
    read/decode/protocol error. ``open_board_popup`` turns that into
    ``render-fetch-failed`` — the client never falls back to a local render or a
    system browser."""
    url = base_url.rstrip("/") + "/db/render"
    # Lazy cross-module import: keeps launcher import light + cycle-free (runner
    # imports launcher only lazily). USER_AGENT / https_context are the two things
    # the WORKING request_json() path sets that this path historically did not.
    # Guarded so an import failure still honors the "EVERY failure → BoardFetchError"
    # contract instead of escaping as a raw ImportError.
    try:
        from client.runner import USER_AGENT, https_context
    except ImportError as exc:
        raise BoardFetchError("client.runner unavailable for /db/render: %s" % (exc,))
    try:
        data = json.dumps(board_spec).encode("utf-8")
    except (TypeError, ValueError) as exc:
        # a non-JSON-serializable board spec must not escape as a raw TypeError
        raise BoardFetchError("board spec is not JSON-serializable: %s" % (exc,))
    request_failure = None
    try:
        request = urllib.request.Request(
            url,  # a malformed base_url raises ValueError HERE — keep it inside the guard
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/html",
                "Accept-Language": i18n.accept_language(),
                "Authorization": "Bearer " + token,
                # Match request_json(): without a real User-Agent urllib sends the
                # default "Python-urllib/x.y", which Cloudflare's edge blocks with a
                # 403 "Access denied" page. That asymmetry is exactly why /db/render
                # 403'd while /v1/devices/activate (which sets this UA) passed.
                "User-Agent": USER_AGENT,
            },
        )
        # Build the opener at call time so https_context() picks up SSL_CERT_FILE /
        # certifi (same TLS trust as request_json); NoRedirect still refuses the
        # token-leaking redirect. For http:// (tests) the default HTTPHandler serves.
        opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPSHandler(context=https_context()))
        with opener.open(request, timeout=timeout_s) as response:
            ctype = response.headers.get_content_type()
            if ctype != "text/html":
                raise BoardFetchError("/db/render returned %r, expected text/html" % (ctype,))
            # Bounded read (shared helper): DETECTS an over-limit body (reads cap+1, never a
            # truncated prefix) AND bounds TOTAL transfer wall-clock — urllib's per-op `timeout`
            # is not a transfer deadline, so a hub dribbling ~1 byte per socket-window would hold
            # this read for a very long time under the byte cap. BoardFetchError is caught by the
            # `except BoardFetchError: raise` arm below and passed through unwrapped.
            raw = read_within_budget(
                response, max_bytes=_MAX_BOARD_BYTES, budget_s=timeout_s,
                error=lambda reason: BoardFetchError("/db/render " + reason),
                allow_chunked=True)  # observed Cloudflare /db/render may reframe as chunked
            body = raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx + refused redirect — incl. fail-closed 400s
        code = exc.code
        if getattr(exc, "fp", None) is not None:
            try:
                exc.close()
            except (OSError, ValueError, AttributeError):
                pass
        # The body is hub-controlled and this exception is agent-facing. Do not read or echo
        # even a bounded prefix: a short body is still enough for prompt/control injection.
        request_failure = "HTTP %s from /db/render" % code
    except urllib.error.URLError:  # unreachable / DNS / TLS / bad scheme
        request_failure = "/db/render unreachable"
    except BoardFetchError:
        raise  # already normalized (content-type / oversize) — don't rewrap as generic
    except (TimeoutError, OSError, http.client.HTTPException, ValueError):
        # socket timeout / low-level I/O / IncompleteRead / malformed URL or decode
        request_failure = "/db/render request failed"
    if request_failure is not None:
        # Raise after the handler so urllib's remote-influenced exception is not retained in
        # __context__, even if a consumer introspects beyond the formatted traceback.
        raise BoardFetchError(request_failure)
    if not body.strip():
        raise BoardFetchError("/db/render returned an empty body")
    return body


def fetch_board_layout(
    layout_request: Dict[str, Any],
    *,
    timeout_s: float = 15.0,
) -> Dict[str, Any]:
    """Resolve diagram geometry through the installed client's authenticated hub session.

    The detached page receives only layout geometry (coordinates, edge paths,
    and optional layout chrome). Endpoint configuration and the bearer token
    remain in Python memory and are never serialized into the board HTML,
    result file, or JavaScript context.
    """
    layout_request = _validate_layout_request(layout_request)
    try:
        from client.runner import (
            AuditError,
            USER_AGENT,
            config_endpoint,
            has_valid_device_token,
            https_context,
            load_config,
            normalize_server_url,
        )
    except ImportError:
        raise BoardLayoutError("layout client unavailable") from None

    try:
        config = load_config()
        if not has_valid_device_token(config):
            raise BoardLayoutError("device token unavailable")
        token = str(config["access_token"]).strip()
        base_url = normalize_server_url(config_endpoint(config))
        data = json.dumps(layout_request, allow_nan=False).encode("utf-8")
        if len(data) > _MAX_LAYOUT_BYTES:
            raise BoardLayoutError("layout request was too large")
        request = urllib.request.Request(
            base_url.rstrip("/") + "/db/layout",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Language": i18n.accept_language(),
                "Authorization": "Bearer " + token,
                "User-Agent": USER_AGENT,
            },
        )
        opener = urllib.request.build_opener(
            NoRedirect,
            urllib.request.HTTPSHandler(context=https_context()),
        )
        with opener.open(request, timeout=timeout_s) as response:
            ctype = response.headers.get_content_type()
            if ctype != "application/json":
                raise BoardLayoutError("layout response was not JSON")
            raw = read_within_budget(
                response,
                max_bytes=_MAX_LAYOUT_BYTES,
                budget_s=timeout_s,
                error=lambda reason: BoardLayoutError("layout response " + reason),
                allow_chunked=True,
            )
        payload = json.loads(raw.decode("utf-8"))
    except BoardLayoutError:
        raise
    except urllib.error.HTTPError as exc:
        if getattr(exc, "fp", None) is not None:
            try:
                exc.close()
            except (OSError, ValueError, AttributeError):
                pass
        raise BoardLayoutError("layout request failed") from None
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        AuditError,
    ):
        raise BoardLayoutError("layout request failed") from None

    if not isinstance(payload, dict):
        raise BoardLayoutError("layout response was invalid")
    coords = payload.get("coords")
    edge_paths = payload.get("edgePaths")
    if not isinstance(coords, list) or len(coords) > _MAX_LAYOUT_NODES:
        raise BoardLayoutError("layout response was invalid")
    if not isinstance(edge_paths, list) or len(edge_paths) > _MAX_LAYOUT_EDGES:
        raise BoardLayoutError("layout response was invalid")
    if "chrome" in payload and not isinstance(payload["chrome"], dict):
        raise BoardLayoutError("layout response was invalid")
    for coord in coords:
        if (
            not isinstance(coord, dict)
            or not isinstance(coord.get("id"), str)
            or not _is_finite_number(coord.get("x"))
            or not _is_finite_number(coord.get("y"))
        ):
            raise BoardLayoutError("layout response was invalid")
    if any(not isinstance(edge_path, dict) for edge_path in edge_paths):
        raise BoardLayoutError("layout response was invalid")
    _validate_layout_structure(payload)
    result = {"coords": coords, "edgePaths": edge_paths}
    if "chrome" in payload:
        result["chrome"] = payload["chrome"]
    return result


# GE follow-up chat — default conversational chrome (the CHAT REPLIES themselves are localized by the
# ChatSession's strings; these are just the page welcome + composer placeholder). The language is the
# single resolver's (i18n.resolve_locale: explicit ui_locale → $DE_UI_LOCALE → system → en-US), NOT a
# sniff of the title for Han characters.
_GE_WELCOME_EN = "Ask a follow-up about this — I'll help you understand it."
_GE_PLACEHOLDER_EN = "Ask about this…  (paste a screenshot to include it)"
_GE_WELCOME_ZH = "对这张图有什么想深入了解的?问我就好。"
_GE_PLACEHOLDER_ZH = "就这张图追问…（可粘贴截图一起发）"


_GE_STATUS_CHECKING_EN = "Checking availability"
_GE_STATUS_CHECKING_ZH = "\u6b63\u5728\u68c0\u67e5\u53ef\u7528\u6027"
_GE_SEND_EN = "Send"
_GE_SEND_ZH = "\u53d1\u9001"
_GE_RETRY_EN = "Check again"
_GE_RETRY_ZH = "\u91cd\u8bd5"




def _inject_js_strings(js: str, sentinel: str, strings: dict) -> str:
    """Replace `sentinel` in a JS block with `strings` as a JSON object literal.

    The page language is decided ONCE in Python (i18n.resolve_locale) and the chosen dict is baked
    in here; the JS no longer reads document.documentElement.lang to pick a language. `<` / `>` / `&`
    are \\u-escaped so the JSON can never terminate the enclosing <script> or open a tag, and U+2028 /
    U+2029 are escaped because they are raw newlines in a JS string literal.
    """
    literal = json.dumps(strings, ensure_ascii=True)
    literal = (literal.replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return js.replace(sentinel, literal)


def ge_chat_js(locale: str) -> str:
    """`_GE_CHAT_JS` with the resolved locale's string dict baked in (see _inject_js_strings)."""
    return _inject_js_strings(_GE_CHAT_JS, "__GE_CHAT_STRINGS__", i18n.ge_chat(locale))


def ge_chrome_js(locale: str) -> str:
    """`_GE_CHROME_JS` with the resolved locale's string dict baked in (see _inject_js_strings)."""
    return _inject_js_strings(_GE_CHROME_JS, "__GE_CHROME_STRINGS__", i18n.ge_chrome(locale))


# The page-side follow-up-chat controller. host → page via window.geChat.{delta,done,error,willClose};
# page → host via window.pywebview.api.{chat_ready,ask}. User/error strings stay on textContent, while
# assistant Markdown is rendered by constructing a small allowlist of DOM nodes (NEVER innerHTML).
# Ported from the A-repo template (point-to-ask / snapshot region flow dropped — not in scope).
# The `S` string dict is injected by ge_chat_js()/ge_chrome_js() at render time (single resolved locale).
_GE_CHAT_JS = """
(function () {
  var scroll = document.getElementById('ge-chat-scroll');
  var composer = document.getElementById('ge-composer');
  var input = document.getElementById('composer-input');
  var sendBtn = document.getElementById('composer-send');
  var imgchips = document.getElementById('ge-imgchips');
  var statusLine = document.getElementById('ge-chat-status');
  var statusText = document.getElementById('ge-chat-status-text');
  var retryBtn = document.getElementById('ge-retry');
  var inputError = document.getElementById('ge-input-error');
  var imagePreview = document.getElementById('ge-image-preview');
  var imagePreviewImage = document.getElementById('ge-image-preview-image');
  var imagePreviewClose = document.getElementById('ge-image-preview-close');
  if (!scroll || !composer || !input || !sendBtn || !statusLine || !statusText) return;
  if (statusText.parentNode !== statusLine) statusLine.appendChild(statusText);
  if (retryBtn && retryBtn.parentNode !== statusLine) statusLine.appendChild(retryBtn);

  var MAX_IMAGES = 5, MAX_IMAGE_BYTES = 4 * 1024 * 1024, MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024;
  var MAX_DATA_URL_CHARS = 5592500, MAX_TEXT_POINTS = 20000, MAX_TEXT_BYTES = 80000;
  var MAX_COMPACT_REQUEST_BYTES = 20 * 1024 * 1024;
  var TURN_WATCHDOG_MS = 350000;             // after the server route's 330s whole-turn budget
  var busy = false, turnSeq = 0, pendingImgs = [], bubbles = {}, watchdogs = {};
  var markdownFrames = {}, pendingMarkdown = {}, renderedMarkdown = {};
  var backendMode = null, pageState = 'capability_checking';
  var BOOT_TIMEOUT_MS = 20000, bootWatchdog = null, bootTimedOut = false;
  var privacyVersion = null, unavailable = false;
  var activeTurn = null, prices = {}, terminalStates = {}, issuedClientIds = {};
  var UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

  var S = __GE_CHAT_STRINGS__;
  sendBtn.textContent = S.send;
  if (retryBtn) {
    retryBtn.setAttribute('aria-label', S.retry);
    retryBtn.title = S.retry;
  }

  var ERROR_TEXT = S.error;
  var STATE_TEXT = S.state;
  var WAITING_STATES = {
    capability_checking: true, creating_conversation: true, restoring_history: true,
    submitting: true, queued: true, running: true, calling_model: true
  };

  function setStatus(state, override) {
    if (!statusLine || !statusText) return;
    var value = typeof override === 'string' ? override : STATE_TEXT[state];
    if (typeof value !== 'string') return;
    if (bootTimedOut || pageState === 'recovering') statusLine.classList.add('is-warning');
    else statusLine.classList.remove('is-warning');
    statusText.textContent = value;
    if (typeof override === 'string' || !WAITING_STATES[state]) return;
    var dots = document.createElement('span'); dots.className = 'status-dots';
    dots.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < 3; i++) {
      var dot = document.createElement('span'); dot.className = 'status-dot';
      dot.textContent = '.'; dots.appendChild(dot);
    }
    statusText.appendChild(dots);
  }
  setStatus(pageState);

  function localError(code) {
    return Object.prototype.hasOwnProperty.call(ERROR_TEXT, code) ? ERROR_TEXT[code] : ERROR_TEXT.chat_unavailable;
  }
  function showInputError(code) { if (inputError) inputError.textContent = localError(code); }

  function clientTurnId() {
    var cryptoApi = window.crypto;
    if (!cryptoApi) return null;
    try {
      if (typeof cryptoApi.randomUUID === 'function') {
        var direct = String(cryptoApi.randomUUID()).toLowerCase();
        if (UUID_V4.test(direct)) {
          if (issuedClientIds[direct]) return null;
          issuedClientIds[direct] = true; return direct;
        }
      }
      if (typeof cryptoApi.getRandomValues === 'function') {
        var bytes = new Uint8Array(16); cryptoApi.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
        var hex = [];
        for (var i = 0; i < bytes.length; i++) hex.push(bytes[i].toString(16).padStart(2, '0'));
        var fallback = hex.slice(0, 4).join('') + '-' + hex.slice(4, 6).join('') + '-' +
          hex.slice(6, 8).join('') + '-' + hex.slice(8, 10).join('') + '-' + hex.slice(10).join('');
        if (UUID_V4.test(fallback) && !issuedClientIds[fallback]) {
          issuedClientIds[fallback] = true; return fallback;
        }
      }
    } catch (e) { return null; }
    return null;
  }

  function clearWatch(turnId) { if (watchdogs[turnId]) { clearTimeout(watchdogs[turnId]); delete watchdogs[turnId]; } }

  function safeMarkdownLink(value) {
    var raw = String(value == null ? '' : value);
    if (!raw || raw.length > 2048 || /[\\u0000-\\u0020\\u007f]/.test(raw)) return null;
    try {
      var parsed = new URL(raw);
      if (parsed.username || parsed.password) return null;
      return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
    } catch (e) { return null; }
  }
  function appendText(parent, value) {
    if (value) parent.appendChild(document.createTextNode(value));
  }
  function markdownOverBudget(value, markerLimit, lineLimit, maxUnits) {
    if (value.length > maxUnits) return true;
    var markers = 0, lines = 1;
    for (var budgetIndex = 0; budgetIndex < value.length; budgetIndex++) {
      var budgetChar = value.charAt(budgetIndex);
      if (budgetChar.charCodeAt(0) === 10 && ++lines > lineLimit) return true;
      if ('`*_[]#>'.indexOf(budgetChar) !== -1 && ++markers > markerLimit) return true;
    }
    return false;
  }
  function appendMarkdownInline(parent, value, depth) {
    var source = String(value == null ? '' : value);
    if (depth > 8) { appendText(parent, source); return; }
    if (markdownOverBudget(source, 512, 2, 50000)) { appendText(parent, source); return; }
    var index = 0, textStart = 0;
    function flush(end) { appendText(parent, source.slice(textStart, end)); }
    while (index < source.length) {
      var marker = source.charAt(index);
      var escaped = source.charAt(index + 1);
      if (marker.charCodeAt(0) === 92 && index + 1 < source.length &&
          (escaped.charCodeAt(0) === 92 || '`*_[]()#!>'.indexOf(escaped) !== -1)) {
        flush(index); appendText(parent, escaped);
        index += 2; textStart = index; continue;
      }
      if (marker === '`') {
        var codeEnd = source.indexOf('`', index + 1);
        if (codeEnd > index + 1) {
          flush(index);
          var inlineCode = document.createElement('code');
          inlineCode.textContent = source.slice(index + 1, codeEnd); parent.appendChild(inlineCode);
          index = codeEnd + 1; textStart = index; continue;
        }
      }
      var strongMarker = source.slice(index, index + 2);
      if ((strongMarker === '**' || strongMarker === '__') && depth < 8) {
        var strongEnd = source.indexOf(strongMarker, index + 2);
        if (strongEnd > index + 2) {
          flush(index);
          var strong = document.createElement('strong');
          appendMarkdownInline(strong, source.slice(index + 2, strongEnd), depth + 1);
          parent.appendChild(strong); index = strongEnd + 2; textStart = index; continue;
        }
      }
      if ((marker === '*' || marker === '_') && depth < 8) {
        var emphasisEnd = source.indexOf(marker, index + 1);
        if (emphasisEnd > index + 1) {
          flush(index);
          var emphasis = document.createElement('em');
          appendMarkdownInline(emphasis, source.slice(index + 1, emphasisEnd), depth + 1);
          parent.appendChild(emphasis); index = emphasisEnd + 1; textStart = index; continue;
        }
      }
      if (marker === '[' || (marker === '!' && source.charAt(index + 1) === '[')) {
        var image = marker === '!';
        var labelStart = index + (image ? 2 : 1);
        var labelEnd = source.indexOf('](', labelStart);
        var linkEnd = labelEnd < 0 ? -1 : source.indexOf(')', labelEnd + 2);
        if (labelEnd >= labelStart && linkEnd > labelEnd + 2) {
          if (image) {
            flush(index); appendText(parent, source.slice(index, linkEnd + 1));
            index = linkEnd + 1; textStart = index; continue;
          }
          var href = safeMarkdownLink(source.slice(labelEnd + 2, linkEnd));
          if (href) {
            flush(index);
            var link = document.createElement('a');
            link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer';
            link.referrerPolicy = 'no-referrer';
            appendMarkdownInline(link, source.slice(labelStart, labelEnd), depth + 1);
            parent.appendChild(link); index = linkEnd + 1; textStart = index; continue;
          }
          flush(index); appendText(parent, source.slice(index, linkEnd + 1));
          index = linkEnd + 1; textStart = index; continue;
        }
      }
      index += 1;
    }
    flush(source.length);
  }
  function markdownBlockStart(line) {
    return /^ {0,3}(#{1,6})\\s+/.test(line) || /^ {0,3}(?:[-+*]\\s+|[1-9]\\d{0,8}[.)]\\s+|>\\s?)/.test(line) ||
      /^ {0,3}(?:`{3,}|~{3,})/.test(line);
  }
  function renderAssistantMarkdown(target, value) {
    var rawSource = String(value == null ? '' : value);
    var source = rawSource.replace(/\\r\\n?/g, '\\n');
    target.textContent = '';
    if (markdownOverBudget(source, 4096, 4096, 400000)) {
      var fallback = document.createElement('p'); fallback.className = 'markdown-fallback';
      fallback.textContent = rawSource; target.appendChild(fallback); return;
    }
    var lines = source.split('\\n');
    var lineIndex = 0;
    while (lineIndex < lines.length) {
      var line = lines[lineIndex];
      if (!line.trim()) { lineIndex += 1; continue; }
      var fence = /^ {0,3}(`{3,}|~{3,})/.exec(line);
      if (fence) {
        var fenceChar = fence[1].charAt(0), fenceLength = fence[1].length;
        var codeLines = []; lineIndex += 1;
        while (lineIndex < lines.length) {
          var close = lines[lineIndex].trim();
          if (close.length >= fenceLength && close.split('').every(function (ch) { return ch === fenceChar; })) {
            lineIndex += 1; break;
          }
          codeLines.push(lines[lineIndex]); lineIndex += 1;
        }
        var pre = document.createElement('pre'), code = document.createElement('code');
        code.textContent = codeLines.join('\\n'); pre.appendChild(code); target.appendChild(pre); continue;
      }
      var heading = /^ {0,3}(#{1,6})\\s+(.+?)\\s*$/.exec(line);
      if (heading) {
        var headingText = heading[2].replace(/[ \\t]+#+[ \\t]*$/, '');
        var title = document.createElement('h' + heading[1].length);
        appendMarkdownInline(title, headingText, 0); target.appendChild(title); lineIndex += 1; continue;
      }
      var unordered = /^ {0,3}[-+*]\\s+(.+)$/.exec(line);
      var ordered = /^ {0,3}([1-9]\\d{0,8})[.)]\\s+(.+)$/.exec(line);
      if (unordered || ordered) {
        var list = document.createElement(unordered ? 'ul' : 'ol');
        if (ordered) list.start = Number(ordered[1]);
        while (lineIndex < lines.length) {
          var item = (unordered ? /^ {0,3}[-+*]\\s+(.+)$/ : /^ {0,3}([1-9]\\d{0,8})[.)]\\s+(.+)$/).exec(lines[lineIndex]);
          if (!item) break;
          var listItem = document.createElement('li'); appendMarkdownInline(listItem, item[ordered ? 2 : 1], 0);
          list.appendChild(listItem); lineIndex += 1;
        }
        target.appendChild(list); continue;
      }
      var quote = /^ {0,3}>\\s?(.*)$/.exec(line);
      if (quote) {
        var blockquote = document.createElement('blockquote'), quoteLine = 0;
        while (lineIndex < lines.length && (quote = /^ {0,3}>\\s?(.*)$/.exec(lines[lineIndex]))) {
          if (quoteLine++) blockquote.appendChild(document.createElement('br'));
          appendMarkdownInline(blockquote, quote[1], 0); lineIndex += 1;
        }
        target.appendChild(blockquote); continue;
      }
      var paragraph = document.createElement('p'), paragraphLine = 0;
      while (lineIndex < lines.length && lines[lineIndex].trim() && !markdownBlockStart(lines[lineIndex])) {
        if (paragraphLine++) paragraph.appendChild(document.createElement('br'));
        appendMarkdownInline(paragraph, lines[lineIndex], 0); lineIndex += 1;
      }
      if (!paragraphLine) { appendMarkdownInline(paragraph, line, 0); lineIndex += 1; }
      target.appendChild(paragraph);
    }
  }

  function addMsg(cls, text, markdown) {
    var d = document.createElement('div');
    d.className = 'msg ' + cls;
    if (text != null) {
      if (markdown === true) renderAssistantMarkdown(d, text);
      else d.textContent = text;
    }
    scroll.appendChild(d); scroll.scrollTop = scroll.scrollHeight; return d;
  }
  function addBot(turnId) {
    var d = addMsg('bot', null);
    var t = document.createElement('span'); t.className = 'typing';
    t.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < 3; i++) {
      var dot = document.createElement('span'); dot.className = 'typing-dot';
      dot.setAttribute('aria-hidden', 'true'); dot.textContent = '.'; t.appendChild(dot);
    }
    d.appendChild(t); bubbles[turnId] = d;
  }
  function renderBubbleMarkdown(turnId, text) {
    var bubble = bubbles[turnId]; if (!bubble) return;
    var value = String(text == null ? '' : text);
    renderAssistantMarkdown(bubble, value); renderedMarkdown[turnId] = value;
    if (prices[turnId]) bubble.appendChild(prices[turnId]);
  }
  function cancelBubbleMarkdown(turnId) {
    delete pendingMarkdown[turnId];
    var scheduled = markdownFrames[turnId]; if (!scheduled) return;
    delete markdownFrames[turnId];
    if (scheduled.animation) {
      if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(scheduled.handle);
    } else clearTimeout(scheduled.handle);
  }
  function setBubbleText(turnId, text, final) {
    var value = String(text == null ? '' : text);
    if (final === true) {
      cancelBubbleMarkdown(turnId);
      renderBubbleMarkdown(turnId, value); delete renderedMarkdown[turnId]; return;
    }
    pendingMarkdown[turnId] = value;
    if (markdownFrames[turnId]) return;
    var animation = typeof requestAnimationFrame === 'function';
    var callback = function () {
      delete markdownFrames[turnId];
      var pending = pendingMarkdown[turnId]; delete pendingMarkdown[turnId];
      if (typeof pending === 'string') renderBubbleMarkdown(turnId, pending);
    };
    var handle = animation ? requestAnimationFrame(callback) : setTimeout(callback, 16);
    markdownFrames[turnId] = {handle: handle, animation: animation};
    return false;
  }
  function setPrice(turnId, payload) {
    if (!payload || !Number.isSafeInteger(payload.price_credits) || payload.price_credits < 0) return;
    if (prices[turnId]) return;              // price is frozen from the first (submit) state payload
    var free = payload.price_credits === 0;
    var price = document.createElement('span'); price.className = 'ge-price' + (free ? ' is-free' : '');
    prices[turnId] = price;
    price.textContent = free ? S.free : String(payload.price_credits) + ' ' + S.credits;
    var bubble = bubbles[turnId]; if (bubble) bubble.appendChild(price);
  }
  function refreshComposer() {
    var privacyReady = backendMode === 'legacy' || (backendMode === 'server' && privacyVersion);
    var enabled = pageState === 'ready' && privacyReady && !busy && !unavailable &&
      !fileReadBusy && !fileReadQueue.length;
    composer.classList[enabled ? 'remove' : 'add']('is-disabled');
    input.disabled = !enabled; sendBtn.disabled = !enabled;
    var rb = document.getElementById('region-btn'); if (rb) rb.disabled = !enabled;
    if (retryBtn) retryBtn.hidden = !((backendMode === 'server' && pageState === 'recovering') || bootTimedOut);
  }
  function setBusy(b) {
    busy = b; refreshComposer();
    if (!b) { try { input.focus(); } catch (e) {} }
  }
  function renderHistory(history, validateOnly) {
    if (!Array.isArray(history)) return false;
    for (var historyIndex = 0; historyIndex < history.length; historyIndex++) {
      if (!history[historyIndex] || !Number.isSafeInteger(history[historyIndex].turn_no)) return false;
    }
    var ordered = history.slice().sort(function (a, b) { return a.turn_no - b.turn_no; });
    var rendered = [], lastTurn = 0;
    for (var i = 0; i < ordered.length; i++) {
      var item = ordered[i];
      if (!item || !Number.isSafeInteger(item.turn_no) || item.turn_no <= lastTurn ||
          typeof item.user_text !== 'string' || Array.from(item.user_text).length < 1 ||
          Array.from(item.user_text).length > MAX_TEXT_POINTS || typeof TextEncoder !== 'function' ||
          new TextEncoder().encode(item.user_text).length > MAX_TEXT_BYTES ||
          !Number.isSafeInteger(item.completed_at) || item.completed_at < 0 || !Array.isArray(item.images) ||
          item.images.length > MAX_IMAGES) return false;
      for (var imageIndex = 0; imageIndex < item.images.length; imageIndex++) {
        var meta = item.images[imageIndex];
        if (!meta || meta.image_index !== imageIndex ||
            (meta.media_type !== 'image/png' && meta.media_type !== 'image/jpeg' && meta.media_type !== 'image/webp') ||
            !Number.isSafeInteger(meta.size_bytes) || meta.size_bytes < 0 ||
            typeof meta.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(meta.sha256)) return false;
      }
      if (item.status === 'completed') {
        if (typeof item.assistant_text !== 'string' || !item.assistant_text ||
            Array.from(item.assistant_text).length > 200000 || item.error_code !== null) return false;
        rendered.push({user: item.user_text, images: item.images.length, cls: 'bot', answer: item.assistant_text});
      } else if (item.status === 'failed' || item.status === 'cancelled') {
        if (item.assistant_text !== null || typeof item.error_code !== 'string' ||
            !Object.prototype.hasOwnProperty.call(ERROR_TEXT, item.error_code) ||
            (item.status === 'cancelled' && item.error_code !== 'cancelled')) return false;
        rendered.push({user: item.user_text, images: item.images.length, cls: 'err', answer: localError(item.error_code)});
      } else return false;
      lastTurn = item.turn_no;
    }
    if (validateOnly === true) return true;
    scroll.textContent = '';
    if (!rendered.length) {
      var welcomeI18n = document.getElementById('ge-i18n');
      var welcome = document.createElement('div'); welcome.className = 'ge-welcome';
      welcome.textContent = welcomeI18n && welcomeI18n.dataset.welcome ? welcomeI18n.dataset.welcome : '';
      scroll.appendChild(welcome);
    }
    for (var rowIndex = 0; rowIndex < rendered.length; rowIndex++) {
      var userBubble = addMsg('user', rendered[rowIndex].user);
      if (rendered[rowIndex].images) {
        var imageTag = document.createElement('div'); imageTag.className = 'imgtag';
        imageTag.textContent = '📎 ' + rendered[rowIndex].images; userBubble.appendChild(imageTag);
      }
      addMsg(rendered[rowIndex].cls, rendered[rowIndex].answer, rendered[rowIndex].cls === 'bot');
    }
    return true;
  }
  function restorePendingTurn() {
    if (!activeTurn) return;
    var userBubble = addMsg('user', activeTurn.text || '');
    if (activeTurn.images && activeTurn.images.length) {
      appendMessageImages(userBubble, activeTurn.images);
    } else if (activeTurn.badge) {
      var imageTag = document.createElement('div'); imageTag.className = 'imgtag';
      imageTag.textContent = activeTurn.badge; userBubble.appendChild(imageTag);
    }
    addBot(activeTurn.displayId);
    if (prices[activeTurn.displayId]) bubbles[activeTurn.displayId].appendChild(prices[activeTurn.displayId]);
  }
  function validateDisclosure(disclosure, validateOnly) {
    if (!disclosure || typeof disclosure.version !== 'string' ||
        !/^[A-Za-z0-9._:-]{1,128}$/.test(disclosure.version) || !Array.isArray(disclosure.providers) ||
        disclosure.providers.length < 1 || disclosure.providers.length > 16) return false;
    var encoder = typeof TextEncoder === 'function' ? new TextEncoder() : null;
    function boundedText(value) {
      return typeof value === 'string' && Array.from(value).length >= 1 && Array.from(value).length <= 256 &&
        encoder && encoder.encode(value).length <= 1024;
    }
    function boundedInt(value) { return Number.isInteger(value) && value >= 0 && value <= 2147483647; }
    for (var i = 0; i < disclosure.providers.length; i++) {
      var provider = disclosure.providers[i];
      if (!provider || !boundedText(provider.category) || !boundedText(provider.data_region) ||
          !boundedText(provider.deletion_scope) ||
          (provider.training_enabled !== false && provider.training_enabled !== null) ||
          !boundedInt(provider.cache_ttl_seconds) || !boundedInt(provider.provider_retention_hours)) return false;
    }
    if (validateOnly === true) return true;
    privacyVersion = disclosure.version;
    return true;
  }
  var imagePreviewReturnFocus = null, imagePreviewScale = 1, imagePreviewWheelDelta = 0;
  function setImagePreviewScale(scale) {
    if (!imagePreviewImage || !imagePreviewImage.style) return;
    imagePreviewScale = Math.max(0.5, Math.min(5, Math.round(scale * 10) / 10));
    imagePreviewImage.style.transform = 'scale(' + imagePreviewScale + ')';
  }
  function resetImagePreviewView() {
    imagePreviewWheelDelta = 0;
    if (imagePreviewImage && imagePreviewImage.style) imagePreviewImage.style.transformOrigin = 'center center';
    setImagePreviewScale(1);
  }
  function setImagePreviewOrigin(event) {
    if (!imagePreviewImage || !imagePreviewImage.style || !event ||
        typeof event.clientX !== 'number' || !isFinite(event.clientX) ||
        typeof event.clientY !== 'number' || !isFinite(event.clientY) ||
        typeof imagePreviewImage.getBoundingClientRect !== 'function') return;
    var rect = imagePreviewImage.getBoundingClientRect();
    if (!rect || !(rect.width > 0) || !(rect.height > 0)) return;
    var x = Math.max(0, Math.min(100, (event.clientX - rect.left) * 100 / rect.width));
    var y = Math.max(0, Math.min(100, (event.clientY - rect.top) * 100 / rect.height));
    imagePreviewImage.style.transformOrigin =
      (Math.round(x * 10) / 10) + '% ' + (Math.round(y * 10) / 10) + '%';
  }
  function closeImagePreview(restoreFocus) {
    if (!imagePreview || !imagePreviewImage) return;
    imagePreview.hidden = true;
    resetImagePreviewView();
    imagePreviewImage.removeAttribute('src');
    imagePreviewImage.alt = '';
    var returnFocus = imagePreviewReturnFocus;
    imagePreviewReturnFocus = null;
    if (returnFocus && returnFocus.isConnected === false) returnFocus = input;
    if (restoreFocus !== false && returnFocus && typeof returnFocus.focus === 'function') {
      try { returnFocus.focus(); } catch (e) {}
    }
  }
  function openImagePreview(imageInfo, imageIndex, opener, imageAlt) {
    if (!imagePreview || !imagePreviewImage || !imagePreviewClose || !imageInfo) return;
    imagePreviewReturnFocus = opener || null;
    resetImagePreviewView();
    imagePreview.setAttribute('aria-label', S.image.dialog);
    imagePreviewClose.setAttribute('aria-label', S.image.close);
    imagePreviewClose.title = S.image.close;
    imagePreviewImage.src = imageInfo.url;
    imagePreviewImage.alt = imageAlt ||
      (S.image.selectedPrefix + (imageIndex + 1) + S.image.selectedSuffix);
    imagePreview.hidden = false;
    try { imagePreviewClose.focus(); } catch (e) {}
  }
  if (imagePreviewClose) imagePreviewClose.addEventListener('click', function () { closeImagePreview(true); });
  if (imagePreview) imagePreview.addEventListener('click', function (event) {
    if (event && event.target === imagePreview) closeImagePreview(true);
  });
  if (imagePreview) imagePreview.addEventListener('wheel', function (event) {
    if (!event || imagePreview.hidden || typeof event.deltaY !== 'number' || !isFinite(event.deltaY) ||
        event.deltaY === 0) return;
    if (event.preventDefault) event.preventDefault();
    var normalizedDelta = event.deltaY;
    if (event.deltaMode === 1) normalizedDelta *= 100 / 3;
    else if (event.deltaMode === 2) normalizedDelta *= 100;
    if ((imagePreviewWheelDelta < 0 && normalizedDelta > 0) ||
        (imagePreviewWheelDelta > 0 && normalizedDelta < 0)) imagePreviewWheelDelta = 0;
    imagePreviewWheelDelta += normalizedDelta;
    var wheelSteps = imagePreviewWheelDelta < 0
      ? Math.ceil(imagePreviewWheelDelta / 100)
      : Math.floor(imagePreviewWheelDelta / 100);
    if (!wheelSteps) return;
    imagePreviewWheelDelta -= wheelSteps * 100;
    var nextScale = imagePreviewScale - wheelSteps * 0.1;
    if (imagePreviewScale <= 1 && nextScale > 1) setImagePreviewOrigin(event);
    setImagePreviewScale(nextScale);
    if ((imagePreviewScale === 5 && event.deltaY < 0) ||
        (imagePreviewScale === 0.5 && event.deltaY > 0)) imagePreviewWheelDelta = 0;
  }, {passive: false});
  document.addEventListener('keydown', function (event) {
    if (!imagePreview || imagePreview.hidden) return;
    var unmodified = !event.ctrlKey && !event.metaKey && !event.altKey;
    var zoomInKey = unmodified && (event.key === '+' || event.key === '=' || event.key === 'Add');
    var zoomOutKey = unmodified && (event.key === '-' || event.key === '_' || event.key === 'Subtract');
    var resetKey = unmodified && event.key === '0';
    if (event.key !== 'Escape' && event.key !== 'Tab' && !zoomInKey && !zoomOutKey && !resetKey) return;
    if (event.preventDefault) event.preventDefault();
    if (event.stopImmediatePropagation) event.stopImmediatePropagation();
    if (event.key === 'Escape') closeImagePreview(true);
    else if (event.key === 'Tab' && imagePreviewClose) imagePreviewClose.focus();
    else if (resetKey) resetImagePreviewView();
    else {
      imagePreviewWheelDelta = 0;
      setImagePreviewScale(imagePreviewScale + (zoomInKey ? 0.1 : -0.1));
    }
  });
  function createImageThumbnail(imageInfo, imageIndex, onRemove) {
    var chip = document.createElement('span'); chip.className = 'imgchip';
    var preview = document.createElement('img'); preview.className = 'imgchip-preview';
    var isSent = typeof onRemove !== 'function';
    var imageAlt = isSent
      ? S.image.sentPrefix + (imageIndex + 1) + S.image.sentSuffix
      : S.image.selectedPrefix + (imageIndex + 1) + S.image.selectedSuffix;
    preview.alt = imageAlt; preview.loading = 'lazy'; preview.decoding = 'async';
    preview.src = imageInfo.url;
    var open = document.createElement('button'); open.className = 'imgchip-open'; open.type = 'button';
    var viewLabel = isSent
      ? S.image.sentViewPrefix + (imageIndex + 1) + S.image.sentViewSuffix
      : S.image.viewPrefix + (imageIndex + 1) + S.image.viewSuffix;
    open.title = viewLabel; open.setAttribute('aria-label', viewLabel);
    open.addEventListener('click', function () { openImagePreview(imageInfo, imageIndex, open, imageAlt); });
    open.appendChild(preview); chip.appendChild(open);
    if (typeof onRemove === 'function') {
      var label = document.createElement('span'); label.textContent = '📎 ' + (imageIndex + 1);
      var x = document.createElement('button'); x.className = 'imgchip-remove'; x.type = 'button'; x.textContent = '✕';
      var removeLabel = S.image.removePrefix + (imageIndex + 1) + S.image.removeSuffix;
      x.title = removeLabel; x.setAttribute('aria-label', removeLabel);
      x.addEventListener('click', onRemove);
      chip.appendChild(label); chip.appendChild(x);
    }
    return chip;
  }
  function appendMessageImages(userBubble, imageInfos) {
    if (!userBubble || !Array.isArray(imageInfos) || !imageInfos.length) return;
    var imageGroup = document.createElement('div'); imageGroup.className = 'msg-images';
    imageInfos.forEach(function (imageInfo, i) {
      imageGroup.appendChild(createImageThumbnail(imageInfo, i, null));
    });
    userBubble.appendChild(imageGroup);
  }
  function renderChips() {
    if (!imgchips) return;
    imgchips.textContent = '';               // clear via textContent (never innerHTML)
    pendingImgs.forEach(function (imageInfo, i) {
      imgchips.appendChild(createImageThumbnail(imageInfo, i, function () {
        closeImagePreview(false); pendingImgs.splice(i, 1); renderChips();
        var nextIndex = Math.min(i, pendingImgs.length - 1);
        var nextChip = nextIndex >= 0 && imgchips && imgchips.children ? imgchips.children[nextIndex] : null;
        var focusTarget = nextChip && nextChip.children ? nextChip.children[0] : input;
        if (focusTarget && typeof focusTarget.focus === 'function') {
          try { focusTarget.focus(); } catch (e) {}
        }
      }));
    });
  }
  function decodeImage(durl) {
    if (typeof durl !== 'string' || durl.length > MAX_DATA_URL_CHARS) return {error: durl && durl.length > MAX_DATA_URL_CHARS ? 'input_too_large' : 'invalid_request'};
    var match = /^data:image\\/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})$/.exec(durl);
    if (!match || match[2].length % 4 !== 0) return {error: 'invalid_request'};
    var binary;
    try { binary = atob(match[2]); } catch (e) { return {error: 'invalid_request'}; }
    if (binary.length > MAX_IMAGE_BYTES) return {error: 'input_too_large'};
    function byte(index) { return index < binary.length ? binary.charCodeAt(index) : -1; }
    var mime = match[1];
    var magicOk = mime === 'png'
      ? byte(0) === 137 && byte(1) === 80 && byte(2) === 78 && byte(3) === 71 && byte(4) === 13 && byte(5) === 10 && byte(6) === 26 && byte(7) === 10
      : mime === 'jpeg'
        ? byte(0) === 255 && byte(1) === 216 && byte(2) === 255
        : byte(0) === 82 && byte(1) === 73 && byte(2) === 70 && byte(3) === 70 &&
          byte(8) === 87 && byte(9) === 69 && byte(10) === 66 && byte(11) === 80;
    // Re-encode decoded bytes so alternate-but-valid base64 pad bits cannot make the same
    // server image look distinct.  Legacy keeps its exact accepted URL and string-identity
    // behavior; server mode reuses the canonical URL for preview, upload, and identity.
    if (!magicOk) return {error: 'invalid_request'};
    var canonical = 'data:image/' + mime + ';base64,' + btoa(binary);
    return {url: backendMode === 'server' ? canonical : durl, size: binary.length};
  }
  function addImg(durl) {
    if (pendingImgs.length >= MAX_IMAGES) { showInputError('input_too_large'); return false; }
    var info = decodeImage(durl);
    if (info.error) { showInputError(info.error); return false; }
    if (pendingImgs.some(function (existing) { return existing.url === info.url; })) { showInputError('invalid_request'); return false; }
    var total = pendingImgs.reduce(function (sum, existing) { return sum + existing.size; }, 0);
    if (total + info.size > MAX_TOTAL_IMAGE_BYTES) { showInputError('input_too_large'); return false; }
    pendingImgs.push(info); if (inputError) inputError.textContent = ''; renderChips(); return true;
  }
  function clearImgs() { closeImagePreview(false); pendingImgs = []; renderChips(); }
  function preflightAsk(modelText, images) {
    if (typeof modelText !== 'string' || !Array.isArray(images)) return {error: 'invalid_request'};
    var text = modelText.trim();
    if (!text || Array.from(text).length > MAX_TEXT_POINTS) return {error: text ? 'input_too_large' : 'invalid_request'};
    if (typeof TextEncoder !== 'function') return {error: 'invalid_request'};
    var encoder = new TextEncoder();
    if (encoder.encode(text).length > MAX_TEXT_BYTES || images.length > MAX_IMAGES) return {error: 'input_too_large'};
    var clean = [], total = 0;
    for (var i = 0; i < images.length; i++) {
      var info = decodeImage(images[i]); if (info.error) return info;
      if (clean.some(function (item) { return item.url === info.url; })) return {error: 'invalid_request'};
      total += info.size; if (total > MAX_TOTAL_IMAGE_BYTES) return {error: 'input_too_large'};
      clean.push(info);
    }
    var urls = clean.map(function (item) { return item.url; });
    if (encoder.encode(JSON.stringify({text: text, images: urls})).length > MAX_COMPACT_REQUEST_BYTES) return {error: 'input_too_large'};
    return {text: text, images: urls, imageInfos: clean};
  }

  window.geChat = {
    bootstrap: function (envelope, historyRefresh) {
      if (bootWatchdog) { try { clearTimeout(bootWatchdog); } catch (e) {} bootWatchdog = null; }
      bootTimedOut = false;
      var historyRefreshRequested = arguments.length > 1;
      if (arguments.length > 2 || (historyRefreshRequested && historyRefresh !== true)) return false;
      if (historyRefreshRequested) {
        var refreshingHistory = backendMode === 'server' && activeTurn && busy &&
          (pageState === 'submitting' || pageState === 'recovering');
        if (!refreshingHistory) return false;
        if (!envelope || envelope.ok !== true || envelope.backend !== 'server' ||
            !envelope.privacy_disclosure || envelope.privacy_disclosure.version !== privacyVersion ||
            !validateDisclosure(envelope.privacy_disclosure, true) || !renderHistory(envelope.history, true)) {
          unavailable = false; busy = true; pageState = 'recovering';
          setStatus('recovering');
          refreshComposer(); return false;
        }
        renderHistory(envelope.history);
        validateDisclosure(envelope.privacy_disclosure);
        restorePendingTurn();
        pageState = 'recovering'; busy = true; unavailable = false;
        setStatus('recovering');
        refreshComposer(); return true;
      }
      if (!envelope || envelope.ok !== true || (envelope.backend !== 'server' && envelope.backend !== 'legacy')) {
        unavailable = true; pageState = 'unavailable';
        setStatus(null, localError(envelope && envelope.error_code));
        refreshComposer();
        return false;
      }
      if (backendMode === 'server' && envelope.backend === 'legacy') {
        unavailable = true; pageState = 'unavailable';
        setStatus(null, localError('chat_unavailable'));
        refreshComposer();
        return false;
      }
      backendMode = envelope.backend;
      unavailable = false; privacyVersion = null;
      fileReadEpoch++; fileReadQueue = []; fileReadBusy = false;
      fileReadReservedCount = 0; fileReadReservedBytes = 0;
      if (backendMode === 'server') {
        if (!renderHistory(envelope.history) || !validateDisclosure(envelope.privacy_disclosure)) {
          unavailable = true; pageState = 'unavailable';
          scroll.textContent = '';
          setStatus(null, localError('bad_response'));
          refreshComposer();
          return false;
        }
      }
      pageState = envelope.state === 'ready' ? 'ready' : 'unavailable';
      unavailable = pageState !== 'ready';
      setStatus(pageState);
      if (pageState === 'ready') enableChat(); else refreshComposer();
      return true;
    },
    state: function (_turnId, state, payload) {
      if (!Object.prototype.hasOwnProperty.call(STATE_TEXT, state)) return false;
      if (_turnId == null && state === 'failed') {
        // No null-turn operation remains; ignore stale failures from an older bridge.
        return true;
      }
      if (_turnId != null && (state === 'failed' || state === 'cancelled')) {
        window.geChat.error(_turnId, state === 'cancelled' ? 'cancelled' : (payload && payload.error_code));
        return true;
      }
      if (state === 'unavailable') {
        var unavailableCode = payload && payload.error_code;
        if (activeTurn) window.geChat.error(activeTurn.displayId, unavailableCode);
        unavailable = true; busy = false; activeTurn = null; pageState = 'unavailable';
        setStatus(null, localError(unavailableCode));
        refreshComposer(); return true;
      }
      if (_turnId != null) {
        if (!activeTurn || activeTurn.displayId !== _turnId) return true;
        if (terminalStates[_turnId] && !(state === 'completed' && bubbles[_turnId])) return true;
      }
      pageState = state;
      if (state === 'submitting' || state === 'queued' || state === 'running' || state === 'calling_model' ||
          state === 'recovering') busy = true;
      if (_turnId != null && (state === 'submitting' || state === 'queued' || state === 'running' || state === 'calling_model')) {
        setPrice(_turnId, payload);
      }
      if (_turnId != null && state === 'completed') terminalStates[_turnId] = 'completed';
      if (state === 'ready') bootTimedOut = false;
      var recoverError = state === 'recovering' && payload && payload.error_code;
      setStatus(state, recoverError ? localError(recoverError) : null);
      refreshComposer();
      return true;
    },
    delta: function (turnId, text) { if (!bubbles[turnId]) return; setBubbleText(turnId, text, false); scroll.scrollTop = scroll.scrollHeight; },
    done: function (turnId, text) {
      clearWatch(turnId);
      if (!activeTurn || activeTurn.displayId !== turnId ||
          (terminalStates[turnId] && !bubbles[turnId]) ||
          (terminalStates[turnId] && terminalStates[turnId] !== 'completed')) return;
      if (bubbles[turnId]) setBubbleText(turnId, text, true);
      terminalStates[turnId] = 'completed';
      delete bubbles[turnId];
      activeTurn = null;
      bootTimedOut = false; pageState = 'ready'; setBusy(false);
      setStatus('completed');
      scroll.scrollTop = scroll.scrollHeight;
    },
    error: function (turnId, msg) {
      if (terminalStates[turnId] === 'completed' || (terminalStates[turnId] && !bubbles[turnId]) ||
          !activeTurn || activeTurn.displayId !== turnId) return;
      clearWatch(turnId);
      cancelBubbleMarkdown(turnId); delete renderedMarkdown[turnId];
      var shown = backendMode === 'server' ? localError(msg) : String(msg || ERROR_TEXT.chat_unavailable);
      var b = bubbles[turnId]; if (b) { b.className = 'msg err'; b.textContent = shown; } else { addMsg('err', shown); }
      terminalStates[turnId] = msg === 'cancelled' ? 'cancelled' : 'failed'; delete bubbles[turnId];
      activeTurn = null; bootTimedOut = false; pageState = 'ready'; setBusy(false);
      setStatus(null, shown);
    },
    willClose: function () { /* host destroys the window next; nothing to do */ },
    isBusy: function () { return busy; },
    submit: submit,
    addImg: addImg
  };

  if (retryBtn) retryBtn.addEventListener('click', function () {
    if (bootTimedOut) { startBoot(); return; }
    if (backendMode !== 'server' || !activeTurn || pageState !== 'recovering' || unavailable) return;
    try { Promise.resolve(window.pywebview.api.retry_chat(activeTurn.clientId)); } catch (e) {}
  });
  function recoverUncertainSubmit(turnId) {
    if (backendMode !== 'server' || unavailable || !activeTurn || activeTurn.displayId !== turnId ||
        terminalStates[turnId]) return;
    pageState = 'recovering'; busy = true;
    setStatus('recovering');
    refreshComposer();
  }

  function submit(modelText, images, badge) {
    if (busy || unavailable || pageState !== 'ready') return false;
    var checked = preflightAsk(modelText, images || []);
    if (checked.error) { showInputError(checked.error); return false; }
    var imgs = checked.images, imageInfos = checked.imageInfos; modelText = checked.text;
    var clientId = null;
    if (backendMode === 'server') {
      if (!privacyVersion) return false;
      clientId = clientTurnId();
      if (!clientId) {
        setStatus(null, localError('chat_unavailable'));
        return false;
      }
    }
    var turnId = ++turnSeq;
    var u = addMsg('user', modelText || '');
    if (imageInfos.length) appendMessageImages(u, imageInfos);
    else if (badge) { var tag = document.createElement('div'); tag.className = 'imgtag'; tag.textContent = badge; u.appendChild(tag); }
    if (inputError) inputError.textContent = '';
    activeTurn = {displayId: turnId, clientId: clientId, text: modelText || '',
      images: imageInfos.slice(), badge: imageInfos.length ? null : (badge || null)};
    pageState = 'submitting';
    setStatus('submitting');
    setBusy(true); addBot(turnId);
    // watchdog: the host guarantees a terminal done/error (chat_backend caps a turn at ~300s), but a hard
    // bridge crash could drop it — un-wedge the composer after a generous ceiling (audit bcbb0346 grok f1).
    watchdogs[turnId] = setTimeout(function () {
      delete watchdogs[turnId];
      if (!bubbles[turnId]) return;
      if (backendMode === 'server' && activeTurn && activeTurn.displayId === turnId) {
        pageState = 'recovering'; busy = true;
        setStatus(null, localError('timeout'));
        refreshComposer();
      } else {
        window.geChat.error(turnId, localError('timeout'));
      }
    }, TURN_WATCHDOG_MS);
    try {
      var request = backendMode === 'server'
        ? window.pywebview.api.ask(turnId, modelText, imgs, clientId, privacyVersion)
        : window.pywebview.api.ask(turnId, modelText, imgs);
      Promise.resolve(request).then(function (r) {
        if (r && r.ok === false) {
          var code = r.error_code || 'chat_unavailable';
          var activePrivacyRejection = backendMode === 'server' && code === 'privacy_confirmation_required' &&
            activeTurn && activeTurn.displayId === turnId;
          if (activePrivacyRejection) {
            // error() resets pageState to ready; unavailable keeps the composer disabled during that callback.
            unavailable = true;
          }
          window.geChat.error(turnId, code);
          if (activePrivacyRejection) {
            pageState = 'unavailable'; refreshComposer();
          }
        }
      }).catch(function () {
        if (backendMode === 'server') recoverUncertainSubmit(turnId);
        else window.geChat.error(turnId, localError('chat_unavailable'));
      });
    } catch (e) {
      if (backendMode === 'server') recoverUncertainSubmit(turnId);
      else window.geChat.error(turnId, localError('chat_unavailable'));
    }
    return true;
  }
  // Seam for the header region-ask (in the chrome IIFE): a captured region crop is pushed into THIS
  // composer's pending-image list (same chip UI as a paste), so region-ask reuses the audited send path.

  function send() {
    if (busy) return;
    var text = (input.value || '').trim();
    var imgs = pendingImgs.map(function (item) { return item.url; });
    if (!text && !imgs.length) return;
    if (submit(text, imgs, null)) { input.value = ''; clearImgs(); }
  }
  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', function (e) {
    // !isComposing: a CJK IME presses Enter to pick a candidate — don't submit mid-composition
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  var fileReadQueue = [], fileReadBusy = false, fileReadEpoch = 0;
  var fileReadReservedCount = 0, fileReadReservedBytes = 0;
  function pumpImageFiles() {
    if (fileReadBusy || !fileReadQueue.length) return;
    fileReadBusy = true;
    var file = fileReadQueue.shift(), reader = new FileReader(), epoch = fileReadEpoch;
    refreshComposer();
    var next = function () {
      if (epoch !== fileReadEpoch) return;
      fileReadReservedCount = Math.max(0, fileReadReservedCount - 1);
      fileReadReservedBytes = Math.max(0, fileReadReservedBytes - file.size);
      fileReadBusy = false; refreshComposer(); pumpImageFiles();
    };
    reader.onload = function () { if (epoch === fileReadEpoch) addImg(String(reader.result)); next(); };
    reader.onerror = next; reader.onabort = next;
    try { reader.readAsDataURL(file); } catch (e) { next(); }
  }
  function queueImageFile(file) {
    if (!backendMode || unavailable || pageState !== 'ready') return false;
    if (!file || (file.type !== 'image/png' && file.type !== 'image/jpeg' && file.type !== 'image/webp')) {
      showInputError('invalid_request'); return false;
    }
    if (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > MAX_IMAGE_BYTES) {
      showInputError(file && file.size > MAX_IMAGE_BYTES ? 'input_too_large' : 'invalid_request'); return false;
    }
    var selectedBytes = pendingImgs.reduce(function (sum, existing) { return sum + existing.size; }, 0);
    if (pendingImgs.length + fileReadReservedCount >= MAX_IMAGES ||
        selectedBytes + fileReadReservedBytes + file.size > MAX_TOTAL_IMAGE_BYTES) {
      showInputError('input_too_large'); return false;
    }
    fileReadReservedCount++; fileReadReservedBytes += file.size;
    fileReadQueue.push(file); refreshComposer(); pumpImageFiles(); return true;
  }
  input.addEventListener('paste', function (e) {
    var items = (e.clipboardData && e.clipboardData.items) || [], took = false;
    for (var i = 0; i < items.length; i++) {
      if (items[i].type === 'image/png' || items[i].type === 'image/jpeg' || items[i].type === 'image/webp') {
        var file = items[i].getAsFile();
        if (queueImageFile(file)) took = true;
      }
    }
    if (took) e.preventDefault();
  });
  composer.addEventListener('dragover', function (e) { if (e && e.preventDefault) e.preventDefault(); });
  composer.addEventListener('drop', function (e) {
    if (e && e.preventDefault) e.preventDefault();
    var files = (e && e.dataTransfer && e.dataTransfer.files) || [];
    for (var i = 0; i < files.length; i++) {
      queueImageFile(files[i]);
    }
  });

  function enableChat() {
    bootTimedOut = false; pageState = 'ready'; unavailable = false; refreshComposer();
    var i18n = document.getElementById('ge-i18n');
    if (i18n && i18n.dataset.placeholder) input.placeholder = i18n.dataset.placeholder;
    var w = document.getElementById('ge-welcome');
    if (w && i18n && i18n.dataset.welcome) w.textContent = i18n.dataset.welcome;  // textContent, never innerHTML
    try { input.focus(); } catch (e) {}
    setTimeout(function () { try { input.focus(); } catch (e) {} }, 120);
  }
  function armBootWatchdog() {
    if (bootWatchdog) { try { clearTimeout(bootWatchdog); } catch (e) {} }
    bootWatchdog = setTimeout(function () {
      bootWatchdog = null;
      if (pageState !== 'capability_checking') return;
      bootTimedOut = true; unavailable = true; pageState = 'unavailable';
      setStatus(null, localError('init_timeout'));
      refreshComposer();
    }, BOOT_TIMEOUT_MS);
  }
  function ensureSetupBridge() {
    if (setup()) return true;
    if (!_setupBridgeArmed) {
      _setupBridgeArmed = true;
      window.addEventListener('pywebviewready', setup);
    }
    var tries = 0, poll = setInterval(function () { if (setup() || ++tries >= 60) clearInterval(poll); }, 100);
    return false;
  }
  function startBoot() {
    bootTimedOut = false; unavailable = false; pageState = 'capability_checking';
    _setupDone = false;
    setStatus('capability_checking'); refreshComposer();
    armBootWatchdog();
    ensureSetupBridge();
  }
  var _setupDone = false, _setupBridgeArmed = false;
  function setup() {
    if (_setupDone) return true;
    try {
      var api = window.pywebview && window.pywebview.api;
      if (!api || !api.chat_ready) return false;   // bridge not ready / display-only popup
      _setupDone = true;
      // chat_ready is an immediate acknowledgement. When it resolves ok, the asynchronous bootstrap
      // callback owns readiness. When it resolves a terminal {ok:false} (a display-only popup with no
      // chat bridge, or the server declared unavailable) — or rejects — enter the terminal unavailable
      // state NOW and disarm the boot watchdog, instead of sitting in capability_checking until it
      // fires init_timeout. Guard on capability_checking so a bootstrap that already won is not undone.
      function chatUnavailable(code) {
        if (bootWatchdog) { try { clearTimeout(bootWatchdog); } catch (e) {} bootWatchdog = null; }
        if (pageState !== 'capability_checking') return;
        // Same terminal-boot-failure transition the watchdog makes (armBootWatchdog): retry then
        // restarts boot (retry-click: `if (bootTimedOut) startBoot()`), which re-runs chat_ready.
        bootTimedOut = true; unavailable = true; pageState = 'unavailable';
        setStatus(null, localError(code));
        refreshComposer();
      }
      Promise.resolve(api.chat_ready()).then(function (r) {
        if (!r || r.ok !== true) chatUnavailable(r && r.error_code);
      }, function () { chatUnavailable('chat_unavailable'); });
      return true;
    } catch (e) { return false; }
  }
  // pywebview injects the api asynchronously and fires pywebviewready ONCE (may fire before this
  // listener attaches) — so also poll for a bounded window. setup() is guarded → asked once.
  startBoot();
})();
"""


# The frameless-window chrome controller (ported/adapted from the A-repo GE template.html — the window has
# no OS title bar, so the HTML header IS the window's control surface). host ← page via
# window.pywebview.api.{close,hide,copy_visual_image,share_visual_image,snapshot_region}. Region-ask is the
# simplified client flow: drag a box on the .artifact → snapshot_region → the crop is ATTACHED to the
# existing follow-up composer (window.geChat.addImg) rather than A's separate floating mini-composer — same
# audited send path, one less surface. A's zoom/pan/live-view/freeze are intentionally NOT ported.
_GE_CHROME_JS = """
(function () {
  function api() { return (window.pywebview && window.pywebview.api) || null; }
  var art = document.querySelector('.artifact');
  var box = document.getElementById('ge-region-box');
  var imagePreview = document.getElementById('ge-image-preview');
  var S = __GE_CHROME_STRINGS__;
  var visualCaptureSupported = null;
  var visualCaptureProbe = null;

  function probeVisualCapture() {
    if (visualCaptureSupported !== null) return Promise.resolve(visualCaptureSupported);
    var a = api();
    if (!a || !a.visual_capture_capabilities) { visualCaptureSupported = true; return Promise.resolve(true); }
    if (visualCaptureProbe) return visualCaptureProbe;
    try {
      visualCaptureProbe = Promise.resolve(a.visual_capture_capabilities())
        .then(function (res) {
          visualCaptureSupported = !(res && res.supported === false);
          visualCaptureProbe = null;
          return visualCaptureSupported;
        })
        .catch(function () {
          visualCaptureSupported = true;
          visualCaptureProbe = null;
          return true;
        });
    } catch (e) {
      visualCaptureSupported = true;
      visualCaptureProbe = null;
      return Promise.resolve(true);
    }
    return visualCaptureProbe;
  }

  function rectLeft(r) { return Number(r && r.left != null ? r.left : r && r.x) || 0; }
  function rectTop(r) { return Number(r && r.top != null ? r.top : r && r.y) || 0; }
  function rectWidth(r) { return Math.max(0, Number(r && r.width != null ? r.width : r && r.w) || 0); }
  function rectHeight(r) { return Math.max(0, Number(r && r.height != null ? r.height : r && r.h) || 0); }
  function captureRect(x, y, w, h) { return { x: x, y: y, w: w, h: h, left: x, top: y, width: w, height: h, right: x + w, bottom: y + h }; }

  function pageCaptureSubject() {
    if (!art || !art.querySelector) return null;
    return art.querySelector('.ge-vis svg') || art.querySelector('.ge-vis img');
  }

  function pageVisualCaptureSupported() {
    var subject = pageCaptureSubject();
    if (!subject || typeof document.createElement !== 'function' || typeof Image !== 'function') return false;
    var tag = String(subject.tagName || '').toLowerCase();
    if (tag !== 'svg' && tag !== 'img') return false;
    if (tag === 'svg' && typeof XMLSerializer !== 'function') return false;
    try {
      var c = document.createElement('canvas');
      return !!(c && c.getContext && c.toDataURL && c.getContext('2d'));
    } catch (e) { return false; }
  }

  function subjectImageSource(subject, rect, scale) {
    var tag = String(subject && subject.tagName || '').toLowerCase();
    if (tag === 'img') return subject.currentSrc || subject.src || null;
    if (tag !== 'svg' || typeof XMLSerializer !== 'function') return null;
    try {
      var clone = subject.cloneNode(true);
      clone.setAttribute('width', String(Math.max(1, Math.round(rectWidth(rect) * scale))));
      clone.setAttribute('height', String(Math.max(1, Math.round(rectHeight(rect) * scale))));
      if (!clone.getAttribute('xmlns')) clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
      return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(clone));
    } catch (e) { return null; }
  }

  function scaledFont(font, scale) {
    return String(font || '13px sans-serif').replace(/(\\d+(?:\\.\\d+)?)px/, function (_, n) {
      return String(Math.max(1, Number(n) * scale)) + 'px';
    });
  }

  function drawSignature(ctx, r, scale) {
    if (!art || !art.querySelector || !ctx) return;
    var sig = art.querySelector('.ge-signature');
    if (!sig || !sig.getBoundingClientRect) return;
    var text = sig.textContent || '';
    if (!text) return;
    var sr = sig.getBoundingClientRect();
    var rx = rectLeft(r), ry = rectTop(r), rw = rectWidth(r), rh = rectHeight(r);
    var sx = rectLeft(sr), sy = rectTop(sr), sw = rectWidth(sr), sh = rectHeight(sr);
    var left = Math.max(rx, sx), top = Math.max(ry, sy);
    var right = Math.min(rx + rw, sx + sw), bottom = Math.min(ry + rh, sy + sh);
    if (right <= left || bottom <= top) return;
    var style = window.getComputedStyle ? window.getComputedStyle(sig) : null;
    ctx.fillStyle = (style && style.color) || '#b0a99c';
    ctx.font = scaledFont((style && style.font) || '13px sans-serif', scale);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(
      text,
      (sx + sw / 2 - rx) * scale,
      (sy + sh / 2 - ry) * scale,
      Math.max(1, sw * scale)
    );
  }

  function captureVisualFromPage(r) {
    return new Promise(function (resolve) {
      var subject = pageCaptureSubject();
      if (!subject || !subject.getBoundingClientRect) { resolve({ok: false, reason: 'unsupported'}); return; }
      var subjectRect = subject.getBoundingClientRect();
      var scale = Math.max(1, Math.min(2, Number(window.devicePixelRatio) || 1));
      var rx = rectLeft(r), ry = rectTop(r), rw = rectWidth(r), rh = rectHeight(r);
      var sx = rectLeft(subjectRect), sy = rectTop(subjectRect), sw = rectWidth(subjectRect), sh = rectHeight(subjectRect);
      var width = Math.max(1, Math.ceil(rw * scale)), height = Math.max(1, Math.ceil(rh * scale));
      if (width > 8192 || height > 8192 || width * height > 16777216) {
        resolve({ok: false});
        return;
      }
      var canvas, ctx;
      try {
        canvas = document.createElement('canvas');
        canvas.width = width; canvas.height = height;
        ctx = canvas.getContext && canvas.getContext('2d');
      } catch (e) { ctx = null; }
      if (!canvas || !ctx || !canvas.toDataURL) { resolve({ok: false, reason: 'unsupported'}); return; }
      var src = subjectImageSource(subject, subjectRect, scale);
      if (!src) { resolve({ok: false, reason: 'unsupported'}); return; }
      var img = new Image();
      var settled = false;
      var timer = setTimeout(function () { finish({ok: false, reason: 'unsupported'}); }, 8000);
      function finish(res) {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        img.onload = img.onerror = null;
        resolve(res);
      }
      img.onload = function () {
        try {
          ctx.fillStyle = '#fff';
          ctx.fillRect(0, 0, width, height);
          ctx.drawImage(
            img,
            (sx - rx) * scale,
            (sy - ry) * scale,
            sw * scale,
            sh * scale
          );
          drawSignature(ctx, r, scale);
          var image = canvas.toDataURL('image/png');
          finish(String(image).indexOf('data:image/png;base64,') === 0 ? {ok: true, image: image} : {ok: false});
        } catch (e) { finish({ok: false}); }
      };
      img.onerror = function () { finish({ok: false, reason: 'unsupported'}); };
      try { img.src = src; } catch (e) { finish({ok: false, reason: 'unsupported'}); }
    });
  }

  // ---- toast: brief bottom-center confirmation ----
  var _toastTimer = null;
  function showToast(msg) {
    var t = document.getElementById('ge-toast');
    if (!t) return;
    t.textContent = msg; t.hidden = false;                 // textContent, never innerHTML
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { t.hidden = true; }, 1800);
  }
  function hideToast() {   // drop any live toast NOW (so it isn't baked into a capture)
    if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }
    var t = document.getElementById('ge-toast'); if (t) t.hidden = true;
  }

  // ---- close / hide: a frameless window has no OS chrome, so the injected js_api owns both ----
  function geClose() { var a = api(); if (a && a.close) { a.close(); return; } try { window.close(); } catch (e) {} }
  function geHide() { var a = api(); if (a && a.hide) a.hide(); }
  var closeBtn = document.getElementById('close-btn'); if (closeBtn) closeBtn.onclick = geClose;
  var hideBtn = document.getElementById('hide-btn'); if (hideBtn) hideBtn.onclick = geHide;
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (imagePreview && !imagePreview.hidden) return;
    if (regionMode) { exitRegion(); return; }          // Esc disarms region-select first
    // Esc MINIMIZES, it never closes (Owner, 2026-09-16). close() is the ONLY terminal action for a
    // fire-and-forget popup — there is no reopening it — so binding it to a single keystroke meant one
    // stray Esc destroyed the artifact plus any half-typed follow-up. hide() only puts the window away
    // (real minimize on Windows/Linux, orderOut + Dock recall on macOS); the header ✕ stays the one way
    // to destroy. Esc is still a routine keystroke inside a text field (clear it / dismiss an IME
    // candidate list), so don't even minimize while composing or focused in an editable control.
    if (e.isComposing) return;
    var t = e.target;
    if (t && t.closest && t.closest('input, textarea, [contenteditable]')) return;
    geHide();
  });

  // ---- header controls must NOT drag the window: pywebview's drag-region walks up from the mousedown
  //      target, so a press on any control would start a window MOVE. Delegate one mousedown and stop
  //      propagation only when the press lands on an interactive control (empty header still drags). ----
  document.querySelectorAll('.pywebview-drag-region').forEach(function (region) {
    region.addEventListener('mousedown', function (e) {
      if (e.target.closest && e.target.closest('button, input, a, select, textarea, label, [role=\"button\"], [contenteditable]')) e.stopPropagation();
    });
  });

  // ---- Screenshot / Share: capture the .artifact rect (the visual + baked-in .ge-signature). 2×rAF so any
  //      just-changed layout has painted before the native snapshot fires. Native shell only; an honest
  //      toast when the host reports {ok:false, reason:'unsupported'} (a platform that can't capture). ----
  function captureArtifact(fn, naMsg, errMsg, unsMsg, onOk, extra) {
    if (!art) return;
    function runPageCapture() {
      var a = api();
      var dataFn = fn === 'copy_visual_image' ? 'copy_visual_image_data_url'
        : fn === 'share_visual_image' ? 'share_visual_image_data_url' : null;
      if (!dataFn || !pageVisualCaptureSupported()) { showToast(unsMsg); return; }
      if (!a || !a[dataFn]) { showToast(naMsg); return; }
      hideToast();
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        var r = art.getBoundingClientRect();
        captureVisualFromPage(captureRect(rectLeft(r), rectTop(r), rectWidth(r), rectHeight(r))).then(function (res) {
          if (!res || res.ok === false || !res.image) { showToast(res && res.reason === 'unsupported' ? unsMsg : errMsg); return; }
          var args = [res.image];
          if (extra) args.push(extra);
          try {
            Promise.resolve(a[dataFn].apply(a, args))
              .then(function (outcome) {
                if (outcome && outcome.ok === false) showToast(outcome.reason === 'unsupported' ? unsMsg : errMsg);
                else if (onOk) onOk();
              })
              .catch(function () { showToast(errMsg); });
          } catch (e) { showToast(errMsg); }
        }).catch(function () { showToast(errMsg); });
      }); });
    }
    if (visualCaptureSupported === false) { runPageCapture(); return; }
    probeVisualCapture().then(function (supported) {
      var a = api();
      if (!supported) { runPageCapture(); return; }
      if (!a || !a[fn]) { showToast(naMsg); return; }
      hideToast();   // a lingering toast sits over the .artifact — drop it so it isn't captured into the shot
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        var r = art.getBoundingClientRect();
        var args = [[rectLeft(r), rectTop(r), rectWidth(r), rectHeight(r), window.innerWidth, window.innerHeight]];
        if (extra) args.push(extra);
        try {
          Promise.resolve(a[fn].apply(a, args))
            .then(function (res) {
              if (res && res.ok === false) showToast(res.reason === 'unsupported' ? unsMsg : errMsg);
              else if (onOk) onOk();
            })
            .catch(function () { showToast(errMsg); });
        } catch (e) { showToast(errMsg); }
      }); });
    }).catch(function () { showToast(errMsg); });
  }
  var shotBtn = document.getElementById('shot-btn');
  if (shotBtn) shotBtn.onclick = function () {
    captureArtifact('copy_visual_image', S.shot_na, S.shot_err, S.shot_uns, function () { showToast(S.shot_ok); });
  };
  var shareBtn = document.getElementById('share-btn');
  if (shareBtn) shareBtn.onclick = function () {
    var b = shareBtn.getBoundingClientRect();               // anchor the native share menu under the button
    captureArtifact('share_visual_image', S.share_na, S.share_err, S.share_uns, null, [b.x, b.y, b.width, b.height]);
  };

  // ---- region-ask: arm → drag a box on the visual → snapshot_region → ATTACH the crop to the composer.
  //      Box + rects are viewport (position:fixed) coords so they map 1:1 to the native snapshot rect; the
  //      pane is frozen (overflow:hidden via .regioning) while selecting. snapGen orphans a stale in-flight
  //      capture; clampRect keeps the crop inside .artifact (fail-closed — never grab the chrome/chat). ----
  var regionBtn = document.getElementById('region-btn');
  var regionMode = false, rDragging = false, sX = 0, sY = 0, capturing = false, snapGen = 0;
  var CLICK_THRESH = 8;                                     // < this on both axes = a click → a neighborhood box
  function setBox(x, y, w, h) { box.style.left = x + 'px'; box.style.top = y + 'px'; box.style.width = w + 'px'; box.style.height = h + 'px'; }
  function clampRect(r) {                                   // intersect with the visual pane; too-small → null
    var v = art.getBoundingClientRect();
    var x1 = Math.max(r.x, v.left), y1 = Math.max(r.y, v.top);
    var x2 = Math.min(r.x + r.w, v.right), y2 = Math.min(r.y + r.h, v.bottom);
    var w = x2 - x1, h = y2 - y1;
    return (w >= 8 && h >= 8) ? { x: x1, y: y1, w: w, h: h } : null;
  }
  function neighborhoodBox(cx, cy) {                        // a click → a square around the point, shifted fully inside the pane
    var v = art.getBoundingClientRect();
    var side = Math.min(240, v.width, v.height);
    if (side < 8) return null;
    var x = Math.min(Math.max(cx - side / 2, v.left), v.right - side);
    var y = Math.min(Math.max(cy - side / 2, v.top), v.bottom - side);
    return { x: x, y: y, w: side, h: side };
  }
  function enterRegionNow() { regionMode = true; regionBtn.classList.add('is-active'); if (art) art.classList.add('regioning'); showToast(S.arm); }
  function enterRegion() {
    if (!regionBtn || regionBtn.disabled) return;
    if (visualCaptureSupported === false) {
      if (pageVisualCaptureSupported()) enterRegionNow();
      else showToast(S.reg_uns);
      return;
    }
    probeVisualCapture().then(function (supported) {
      if (!supported && !pageVisualCaptureSupported()) { showToast(S.reg_uns); return; }
      if (!regionBtn || regionBtn.disabled || regionMode) return;
      enterRegionNow();
    }).catch(function () { if (!regionBtn || regionBtn.disabled || regionMode) return; enterRegionNow(); });
  }
  function exitRegion() { snapGen++; regionMode = false; rDragging = false; if (regionBtn) regionBtn.classList.remove('is-active'); if (art) art.classList.remove('regioning'); if (box) box.hidden = true; }
  if (regionBtn) regionBtn.addEventListener('click', function () { regionMode ? exitRegion() : enterRegion(); });

  if (art) art.addEventListener('mousedown', function (e) {
    if (!regionMode || e.button !== 0 || capturing) return;  // a capture is resolving — don't start a box we'd have to drop
    snapGen++;                                             // starting a fresh selection drops any prior in-flight capture
    rDragging = true; sX = e.clientX; sY = e.clientY;
    setBox(sX, sY, 0, 0); box.hidden = false; e.preventDefault();
  });
  window.addEventListener('mousemove', function (e) {
    if (!rDragging) return;
    setBox(Math.min(e.clientX, sX), Math.min(e.clientY, sY), Math.abs(e.clientX - sX), Math.abs(e.clientY - sY));
  });
  window.addEventListener('mouseup', function (e) {
    if (!rDragging) return;
    rDragging = false;
    var dx = Math.abs(e.clientX - sX), dy = Math.abs(e.clientY - sY);
    var r = (dx < CLICK_THRESH && dy < CLICK_THRESH)
      ? neighborhoodBox(sX, sY)
      : clampRect({ x: Math.min(e.clientX, sX), y: Math.min(e.clientY, sY), w: dx, h: dy });
    if (!r) { box.hidden = true; return; }
    captureRegion(r);
  });
  // button released OUTSIDE the popup → the window 'mouseup' may never arrive; end the drag on blur so
  // rDragging can't get stuck true and keep reshaping the box on the next stray mousemove.
  window.addEventListener('blur', function () { if (rDragging) { rDragging = false; box.hidden = true; } });

  function captureRegion(r) {
    if (capturing) return;                                 // reentrancy guard: no parallel captures
    var a = api();
    var pageCapture = visualCaptureSupported === false && pageVisualCaptureSupported();
    if (!pageCapture && (!a || !a.snapshot_region)) { showToast(S.reg_na); exitRegion(); return; }
    capturing = true;
    var myGen = ++snapGen;
    var stale = function () { return myGen !== snapGen; };  // cancel/reselect during the async capture
    box.hidden = true;                                     // don't capture the selection box INTO the crop
    hideToast();                                           // ...nor the arming toast
    var fail = function (reason) { capturing = false; if (stale()) return; showToast(reason === 'unsupported' ? S.reg_uns : S.reg_err); };
    requestAnimationFrame(function () { requestAnimationFrame(function () {
      var p;
      try {
        p = pageCapture
          ? captureVisualFromPage(r)
          : Promise.resolve(a.snapshot_region([r.x, r.y, r.w, r.h, window.innerWidth, window.innerHeight]));
      }
      catch (e) { fail(); return; }
      p.then(function (res) {
        if (stale()) { capturing = false; return; }        // superseded → drop the result, never attach
        if (!res || res.ok === false || !res.image) { fail(res && res.reason); return; }
        capturing = false;
        // honor addImg's result: it returns false at the composer image cap (or if the chat seam is gone),
        // in which case the crop was NOT attached — don't claim success (audit f-addimg-return, 4/4 panel).
        var added = (window.geChat && window.geChat.addImg) ? window.geChat.addImg(res.image) : false;
        exitRegion();
        if (added) {
          showToast(S.reg_add);
          var inp = document.getElementById('composer-input');
          if (inp && !inp.disabled) { try { inp.focus(); } catch (e) {} }
        } else {
          showToast(S.reg_reject);
        }
      }).catch(function () { fail(); });
    }); });
  }
})();
"""


def _ge_chat_pane(welcome: str, placeholder: str, *, zh: bool) -> str:
    """The right-column follow-up-chat pane. Starts DISABLED (``is-disabled``); the JS enables it only
    when ``pywebview.api.chat_ready()`` resolves true (a GE popup with a context bundle). ``welcome`` /
    ``placeholder`` are already html.escape'd and ride into a data-* holder the JS reads via textContent."""
    return (
        "<div class=\"ge-chat\">"
        "<div id=\"ge-i18n\" data-welcome=\"%(welcome)s\" data-placeholder=\"%(placeholder)s\"></div>"
        "<div id=\"ge-chat-status\" class=\"ge-chat-status\" role=\"status\">"
        "<span id=\"ge-chat-status-text\" class=\"ge-chat-status-text\">%(status)s"
        "<span class=\"status-dots\" aria-hidden=\"true\">"
        "<span class=\"status-dot\">.</span><span class=\"status-dot\">.</span>"
        "<span class=\"status-dot\">.</span></span></span>"
        "<button id=\"ge-retry\" class=\"ge-retry\" type=\"button\" hidden aria-label=\"%(retry)s\" title=\"%(retry)s\">"
        "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
        "<path d=\"M20 11a8 8 0 1 0 2.3 5.7\"/><polyline points=\"20 4 20 11 13 11\"/></svg>"
        "</button></div>"
        "<div id=\"ge-chat-scroll\" class=\"ge-chat-scroll\">"
        "<div id=\"ge-welcome\" class=\"ge-welcome\">%(welcome)s</div>"
        "</div>"
        "<div id=\"ge-composer\" class=\"ge-composer is-disabled\">"
        "<div id=\"ge-imgchips\" class=\"ge-imgchips\"></div>"
        "<div class=\"ge-composer-row\">"
        "<textarea id=\"composer-input\" rows=\"3\" placeholder=\"%(placeholder)s\" disabled></textarea>"
        "<button id=\"composer-send\" type=\"button\" disabled>%(send)s</button>"
        "</div>"
        "<div id=\"ge-input-error\" class=\"ge-input-error\" role=\"alert\"></div></div>"
        "<div id=\"ge-image-preview\" class=\"ge-image-preview\" hidden role=\"dialog\" "
        "aria-modal=\"true\" aria-label=\"%(preview_label)s\">"
        "<button id=\"ge-image-preview-close\" class=\"ge-image-preview-close\" type=\"button\" "
        "aria-label=\"%(preview_close_label)s\">✕</button>"
        "<img id=\"ge-image-preview-image\" class=\"ge-image-preview-image\" alt=\"\">"
        "</div></div>"
    ) % {
        "welcome": welcome,
        "placeholder": placeholder,
        "status": _GE_STATUS_CHECKING_ZH if zh else _GE_STATUS_CHECKING_EN,
        "send": _GE_SEND_ZH if zh else _GE_SEND_EN,
        "retry": _GE_RETRY_ZH if zh else _GE_RETRY_EN,
        "preview_label": "\u6240\u9009\u56fe\u7247\u9884\u89c8" if zh else "Selected image preview",
        "preview_close_label": "\u5173\u95ed\u56fe\u7247\u9884\u89c8" if zh else "Close image preview",
    }


def render_artifact_html(spec: PopupSpec) -> str:
    """Render the server-delivered FINISHED artifact into a self-contained popup body.

    ``spec.artifact`` is exactly what the server render seam returned —
    ``{"kind": "svg"|"image", "data": "<SVG markup | data:image/... URL>"}``, the finished
    product ONLY. This client just DISPLAYS it: ``kind="svg"`` inlines the markup (crisp,
    scalable); ``kind="image"`` shows it as an ``<img>`` sourced from the data-URL. The
    generation IP that authored the artifact stays on the server and never reaches here.

    The window is FRAMELESS (native_shell opens it with no OS title bar), so the HTML ``<header>``
    IS the window's control surface — 5 controls wired to the injected js_api: region-ask (drag a box
    on the visual → ``snapshot_region`` → the crop is attached to the follow-up composer), screenshot
    (``copy_visual_image`` → clipboard), share (``share_visual_image`` → native share sheet), hide
    (``hide``) and close (``close``; also Esc). GE is FIRE-AND-FORGET — there is no Submit/commit round
    trip (the popup produces no decision to return); closing the window is the only terminal action. A
    ``.ge-signature`` wordmark sits under the visual so every screenshot/share carries it. The caller
    text is HTML-escaped (non-str metadata is coerced to its default first, so the fail-closed contract
    holds) before it reaches any attribute/text context.

    Raises ``ValueError`` when the artifact is missing or malformed — ``open_popup`` maps that
    to a structured ``render-failed`` outcome (an undelivered artifact is never shown raw)."""
    artifact = spec.artifact
    if not isinstance(artifact, dict):
        raise ValueError("spec.artifact is required to render the popup body")
    art_kind = artifact.get("kind")
    data = artifact.get("data")
    if not isinstance(data, str) or not data:
        raise ValueError("artifact.data must be a non-empty string")

    # Coerce metadata to str up front: html.escape() on a non-str raises AttributeError,
    # which open_popup's (TypeError, ValueError) catch would NOT convert to render-failed.
    # Non-str / empty falls back to the display default (also what the commit payload sends,
    # so the submitted value matches what is shown).
    kind_text = spec.kind if isinstance(spec.kind, str) and spec.kind else "db"
    title_text = spec.title if isinstance(spec.title, str) and spec.title else "Decision Engine"
    note_text = spec.note if isinstance(spec.note, str) else ""
    title = html.escape(title_text, quote=True)
    kind = html.escape(kind_text, quote=True)
    note = html.escape(note_text, quote=True) if note_text else ""

    if art_kind == "svg":
        # The SVG is the server's finished, IP-free output; inline it verbatim so it stays
        # crisp and scalable. `data` is the substituted VALUE (not the format template), so its
        # own '%' chars are never re-interpreted. A minimal shape check rejects a stray
        # data-URL / error string accidentally routed to the svg branch.
        if "<svg" not in data:
            raise ValueError("artifact.data for kind=svg must contain <svg> markup")
        artifact_block = (
            "<div class=\"artifact\"><div class=\"ge-vis\">" + data + "</div>"
            "<div class=\"ge-signature\">—— DeepPattern · Decision Engine · Graphic Explanation ——</div></div>"
        )
    elif art_kind == "image":
        # A finished base64 data-URL from the server; require a non-empty payload after the
        # comma (rejects a bare `data:image/…,`) and escape it for the src attribute context.
        head, _, payload = data.partition(",")
        if not (head.startswith("data:image/") and payload):
            raise ValueError("artifact.data for kind=image must be a data:image/ URL with a payload")
        artifact_block = (
            "<div class=\"artifact\"><div class=\"ge-vis\"><img alt=\"%s\" src=\"%s\"></div>"
            "<div class=\"ge-signature\">—— DeepPattern · Decision Engine · Graphic Explanation ——</div></div>"
            % (title, html.escape(data, quote=True))
        )
    else:
        raise ValueError("artifact.kind must be 'svg' or 'image', got %r" % (art_kind,))

    note_block = "<p class=\"note\">%s</p>" % note if note else ""
    # Follow-up-chat chrome language comes from the single resolver (explicit ui_locale in payload →
    # $DE_UI_LOCALE → system UI language → en-US), NOT from sniffing the title for Han characters.
    # welcome + placeholder are html.escape'd, then ride into a data-* holder the chat JS reads via
    # textContent.
    _locale = i18n.resolve_locale(spec.payload.get("ui_locale"))
    _zh = i18n.is_zh(_locale)
    welcome = html.escape(_GE_WELCOME_ZH if _zh else _GE_WELCOME_EN, quote=True)
    placeholder = html.escape(_GE_PLACEHOLDER_ZH if _zh else _GE_PLACEHOLDER_EN, quote=True)
    chat_pane = _ge_chat_pane(welcome, placeholder, zh=_zh)
    # Frameless-header control tooltips + the <html lang> value (html.escape'd into title="" attrs).
    # The page JS no longer branches on <html lang>: its string dict is injected below for this one
    # resolved locale, so the whole page renders a single resolver-chosen language. <html lang> stays
    # for accessibility / font selection only.
    lang = "zh" if _zh else "en"
    if _zh:
        t_region, t_shot, t_share = "框选提问（拖一个框，就框住的那块图追问）", "截图到剪贴板（带署名）", "分享…（系统分享菜单）"
        t_hide, t_close = "隐藏（菜单栏图标可再显示）", "关闭（Esc）"
    else:
        t_region, t_shot, t_share = "Ask about a region (drag a box)", "Screenshot to clipboard", "Share… (system share menu)"
        t_hide, t_close = "Hide (menu-bar icon toggles show/hide)", "Close (Esc)"
    t_region = html.escape(t_region, quote=True)
    t_shot = html.escape(t_shot, quote=True)
    t_share = html.escape(t_share, quote=True)
    t_hide = html.escape(t_hide, quote=True)
    t_close = html.escape(t_close, quote=True)
    return (
        "<!doctype html><html lang=\"%(lang)s\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>%(title)s</title>"
        "<style>"
        "html,body{height:100%%}"
        "body{font:15px -apple-system,system-ui,sans-serif;margin:0;padding:0;"
        "box-sizing:border-box;display:flex;flex-direction:column;"
        "background:#faf6ec;color:#1c1c1c}"
        # frameless-window header = the window's control surface (there is no OS title bar)
        "header.pywebview-drag-region{flex:0 0 auto;display:flex;align-items:center;gap:12px;"
        "padding:12px 20px;border-bottom:1px solid #e6ddc4;background:#fff}"
        "header .title{font-size:17px;font-weight:700;color:#1c1c1c}"
        "header .mode{font-size:12px;color:#8a7a55;border:1px solid #e6ddc4;background:#f1ebde;"
        "border-radius:999px;padding:3px 11px;text-transform:uppercase;letter-spacing:.05em}"
        "header .spacer{flex:1}"
        "header .tool-btn{width:30px;height:30px;border:0;border-radius:8px;"
        "background:#efe9dc;color:#6b6456;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.10);"
        "display:flex;align-items:center;justify-content:center;box-sizing:border-box;padding:0}"
        "header .tool-btn:hover{background:#e4dcc9;color:#43412f}"
        "header .tool-btn:active{transform:translateY(.5px)}"
        "header .tool-btn:disabled{opacity:.4;cursor:not-allowed}"
        "header .tool-btn.is-active{background:#0e7490;color:#fff}"
        "header .tool-btn.is-active:hover{background:#0c647a;color:#fff}"
        "header .tool-btn svg{width:16px;height:16px}"
        "header .hide-btn{width:30px;height:30px;border:0;border-radius:8px;padding:0;"
        "background:#febc2e;font-size:0;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.14);"
        "display:flex;align-items:center;justify-content:center;box-sizing:border-box}"
        "header .hide-btn::before{content:\"\";width:13px;height:4px;background:#fff;border-radius:2px}"
        "header .hide-btn:hover{background:#f0ad1d}"
        "header .close-btn{width:30px;height:30px;border:0;border-radius:8px;padding:0;box-sizing:border-box;"
        "background:#e5484d;color:#fff;font-size:17px;font-weight:900;cursor:pointer;line-height:1;"
        "box-shadow:0 1px 3px rgba(0,0,0,.14);display:flex;align-items:center;justify-content:center}"
        "header .close-btn:hover{background:#c93c41}"
        # bottom-center toast + the fixed-coord region selection box
        ".ge-toast{position:fixed;left:50%%;bottom:30px;transform:translateX(-50%%);"
        "background:rgba(31,41,55,.93);color:#fff;padding:9px 16px;border-radius:10px;font-size:14px;"
        "z-index:60;box-shadow:0 4px 16px rgba(0,0,0,.22);pointer-events:none}"
        ".ge-toast[hidden]{display:none}"
        ".ge-region-box{position:fixed;z-index:50;border:2px dashed #0e7490;"
        "background:rgba(14,116,144,.10);border-radius:6px;pointer-events:none;"
        "box-shadow:0 0 0 9999px rgba(43,38,32,.04)}"
        ".ge-region-box[hidden]{display:none}"
        # content area below the header
        ".ge-body{flex:1;min-height:0;display:flex;flex-direction:column;padding:16px 22px 20px;box-sizing:border-box}"
        "h1{font-size:20px;margin:.1em 0 .35em}"
        ".note{color:#444;margin:.1em 0 .6em}"
        ".ge-main{flex:1;min-height:0;display:flex;gap:16px}"
        # left visual pane: column so the .ge-signature wordmark sits under the visual and rides into every capture
        ".artifact{flex:1;min-height:0;display:flex;flex-direction:column;"
        "background:#fff;border:1px solid #e6ddc4;border-radius:8px;padding:12px}"
        ".artifact.regioning{cursor:crosshair;user-select:none}"
        # freeze the scroll container (.ge-vis, not .artifact) during selection so the fixed box maps 1:1 to the crop
        ".artifact.regioning .ge-vis{overflow:hidden}"
        ".ge-vis{flex:1;min-height:0;width:100%%;display:flex;align-items:center;justify-content:center;overflow:auto}"
        ".ge-vis svg{display:block;width:100%%;height:100%%;max-width:100%%;max-height:100%%}"
        ".ge-vis img{display:block;max-width:100%%;max-height:100%%;height:auto}"
        ".ge-signature{flex:none;text-align:center;color:#b0a99c;font-size:13px;letter-spacing:.04em;margin-top:6px}"
        # right-column follow-up chat
        ".ge-chat{flex:0 0 340px;min-height:0;display:flex;flex-direction:column;"
        "background:#fff;border:1px solid #e6ddc4;border-radius:8px;overflow:hidden}"
        ".ge-chat-status{padding:6px 10px 6px 12px;border-bottom:1px solid #eee3c8;background:#fbf8ef;"
        "color:#6b5e3c;font-size:12px;min-height:17px;display:flex;align-items:center;gap:10px}"
        ".ge-chat-status.is-warning{color:#c2410c;background:#fff6ed}"
        ".ge-chat-status-text{flex:1;min-width:0}"
        ".ge-retry{flex:none;display:inline-flex;align-items:center;justify-content:center;"
        "padding:0;border:0;background:transparent;color:inherit;cursor:pointer;line-height:0;overflow:visible}"
        ".ge-retry:focus-visible{outline:2px solid #f97316;outline-offset:2px}"
        ".ge-retry svg{display:block;width:16px;height:16px}"
        ".ge-chat-scroll{flex:1;min-height:0;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:10px}"
        ".ge-welcome{color:#6b5e3c;font-size:14px}"
        ".msg{max-width:100%%;white-space:pre-wrap;word-break:break-word;line-height:1.45;"
        "padding:8px 11px;border-radius:10px;font-size:14px}"
        ".msg.user{align-self:flex-end;background:#eef4ef;border:1px solid #d6e5d9}"
        ".msg.bot{align-self:flex-start;background:#f7f3e8;border:1px solid #ece2c8;white-space:normal}"
        ".msg.bot p,.msg.bot h1,.msg.bot h2,.msg.bot h3,.msg.bot h4,.msg.bot h5,.msg.bot h6{margin:0 0 .55em}"
        ".msg.bot p:last-child,.msg.bot h1:last-child,.msg.bot h2:last-child,.msg.bot h3:last-child,"
        ".msg.bot h4:last-child,.msg.bot h5:last-child,.msg.bot h6:last-child{margin-bottom:0}"
        ".msg.bot h1{font-size:1.35em}.msg.bot h2{font-size:1.25em}.msg.bot h3{font-size:1.15em}"
        ".msg.bot h4,.msg.bot h5,.msg.bot h6{font-size:1em}"
        ".msg.bot ul,.msg.bot ol{margin:.35em 0 .55em;padding-left:1.5em}"
        ".msg.bot p,.msg.bot li,.msg.bot blockquote{white-space:pre-wrap}"
        ".msg.bot blockquote{margin:.45em 0;padding-left:.75em;border-left:3px solid #c9bd97;color:#5d5545}"
        ".msg.bot code{font-family:Consolas,'SFMono-Regular',monospace;background:#eee8d8;border-radius:4px;padding:.08em .3em}"
        ".msg.bot pre{margin:.45em 0;max-width:100%%;overflow:auto;white-space:pre-wrap;background:#eee8d8;border-radius:6px;padding:8px}"
        ".msg.bot pre code{background:transparent;padding:0}.msg.bot a{color:#0e7490;text-decoration:underline}"
        ".msg.bot .markdown-fallback{white-space:pre-wrap}"
        ".msg.err{align-self:flex-start;background:#fbeeec;border:1px solid #e6c9c3;color:#8a2b1c}"
        ".imgtag{margin-top:4px;font-size:12px;color:#6b5e3c}"
        ".msg-images{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}"
        ".msg-images .imgchip{padding:2px;border-radius:6px}"
        ".msg-images .imgchip-preview{width:44px;height:44px;object-fit:contain}"
        ".typing{display:inline-flex;align-items:center;color:#b3a684}"
        ".typing-dot{display:inline-block;animation:ge-typing-dot 1.2s ease-in-out infinite}"
        ".typing-dot:nth-child(2){animation-delay:.15s}.typing-dot:nth-child(3){animation-delay:.3s}"
        "@keyframes ge-typing-dot{0%%,80%%,100%%{opacity:.3;transform:translateY(0)}"
        "40%%{opacity:1;transform:translateY(-2px)}}"
        ".status-dots{display:inline-flex}.status-dot{display:inline-block;"
        "animation:ge-status-dot 1.2s ease-in-out infinite}"
        ".status-dot:nth-child(2){animation-delay:.15s}.status-dot:nth-child(3){animation-delay:.3s}"
        "@keyframes ge-status-dot{0%%,80%%,100%%{opacity:.3;transform:translateY(0)}"
        "40%%{opacity:1;transform:translateY(-2px)}}"
        "@media (prefers-reduced-motion:reduce){.typing-dot,.status-dot{animation:none;opacity:1;transform:none}}"
        ".ge-price{display:block;margin-top:5px;color:#6b5e3c;font-size:11px;font-weight:600}"
        ".ge-price.is-free{color:#15803d}"
        ".ge-composer{border-top:1px solid #eee3c8;padding:10px}"
        ".ge-composer.is-disabled{opacity:.55}"
        ".ge-imgchips{display:flex;flex-wrap:wrap;gap:6px}"
        ".ge-imgchips:not(:empty){margin-bottom:8px}"
        ".imgchip{display:inline-flex;align-items:center;gap:4px;font-size:12px;background:#eef4ef;"
        "border:1px solid #d6e5d9;border-radius:12px;padding:2px 6px}"
        ".imgchip-preview{width:26px;height:26px;object-fit:cover;border-radius:5px;background:#fff}"
        ".imgchip button{border:0;background:none;cursor:pointer;font:inherit;padding:0;color:#6b5e3c}"
        ".imgchip-open{display:flex;align-items:center;border-radius:5px!important;cursor:zoom-in!important}"
        ".imgchip-open:focus-visible,.imgchip-remove:focus-visible{outline:2px solid #0e7490;outline-offset:2px}"
        ".ge-image-preview{position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;"
        "box-sizing:border-box;overflow:hidden;padding:5vh 5vw;background:rgba(20,18,14,.82)}"
        ".ge-image-preview[hidden]{display:none}"
        ".ge-image-preview-image{display:block;max-width:90vw;max-height:86vh;object-fit:contain;background:#fff;"
        "transform-origin:center center;border-radius:10px;box-shadow:0 12px 36px rgba(0,0,0,.42)}"
        ".ge-image-preview-close{position:absolute;z-index:1;top:16px;right:16px;width:36px;height:36px;padding:0;"
        "border:1px solid rgba(255,255,255,.72);border-radius:50%%;background:rgba(28,28,28,.82);color:#fff;"
        "font-size:18px;line-height:1;cursor:pointer}"
        ".ge-image-preview-close:focus-visible{outline:3px solid #fff;outline-offset:3px}"
        # One rounded Composer box: textarea + send button are flex siblings so the button sits
        # BELOW the textarea (not overlapping it) — this lets the textarea have symmetric
        # padding (12px on all sides, no right/bottom safe-zone needed) and keeps the button
        # bottom-right aligned. The textarea stays the scrollable surface (overflow-y:auto,
        # max-height 220px) while the button always pins to the row's bottom-right edge without
        # any chance of covering the user's last line.
        ".ge-composer-row{display:flex;flex-direction:column;border:1px solid #c9bd97;"
        "border-radius:12px;background:#fff}"
        ".ge-composer-row:focus-within{border-color:#0e7490;box-shadow:0 0 0 2px rgba(14,116,144,.15)}"
        "#composer-input{flex:1;display:block;box-sizing:border-box;width:100%%;font:inherit;"
        "color:inherit;resize:none;border:0;outline:none;background:transparent;line-height:1.5;"
        "min-height:130px;max-height:220px;overflow-y:auto;padding:12px 12px 28px 12px;scrollbar-width:none}"
        "#composer-input::-webkit-scrollbar{display:none;width:0;height:0}"
        "#composer-send{align-self:flex-end;flex:none;height:28px;padding:0 12px;"
        "font-size:12px;line-height:1;border:1px solid #0e7490;border-radius:6px;"
        "background:#0e7490;color:#fff;margin:4px 6px 6px}"
        "#composer-send:disabled{cursor:default;opacity:.6}"
        ".ge-input-error{min-height:0;margin-top:4px;color:#8a2b1c;font-size:12px}"
        "button{font:inherit;padding:9px 18px;border-radius:8px;border:1px solid #c9bd97;cursor:pointer}"
        "</style></head><body>"
        # Frameless header IS the window control surface. Left: product label + mode badge. Right: the 5
        # controls — region-ask (disabled until the chat backend arms it) / screenshot / share / hide / close.
        "<header class=\"pywebview-drag-region\">"
        "<span class=\"title\">Decision Engine</span>"
        "<span class=\"mode\">%(kind)s</span>"
        "<span class=\"spacer\"></span>"
        "<button class=\"tool-btn\" id=\"region-btn\" title=\"%(t_region)s\" aria-label=\"%(t_region)s\" disabled>"
        "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"2.5\" y=\"2.5\" width=\"19\" height=\"19\" rx=\"2\" fill=\"none\" stroke-width=\"1.5\" stroke-dasharray=\"3 2.4\"/><path d=\"M3 3 L3 16 L6.4 12.8 L8.9 18.9 L10.8 18.1 L8.3 12.1 L13 11.9 Z\" fill=\"currentColor\" stroke=\"currentColor\" stroke-width=\"0.7\" transform=\"rotate(-10 3 3) translate(3 3) scale(0.94) translate(-3 -3)\"/><text x=\"17.1\" y=\"12.6\" text-anchor=\"middle\" font-size=\"12\" font-weight=\"600\" fill=\"currentColor\" stroke=\"none\" font-family=\"'Helvetica Neue',Helvetica,Arial\">?</text></svg>"
        "</button>"
        "<button class=\"tool-btn\" id=\"shot-btn\" title=\"%(t_shot)s\" aria-label=\"%(t_shot)s\">"
        "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z\"/><circle cx=\"12\" cy=\"13\" r=\"4\"/></svg>"
        "</button>"
        "<button class=\"tool-btn\" id=\"share-btn\" title=\"%(t_share)s\" aria-label=\"%(t_share)s\">"
        "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"18\" cy=\"5\" r=\"3\"/><circle cx=\"6\" cy=\"12\" r=\"3\"/><circle cx=\"18\" cy=\"19\" r=\"3\"/><line x1=\"8.59\" y1=\"13.51\" x2=\"15.42\" y2=\"17.49\"/><line x1=\"15.41\" y1=\"6.51\" x2=\"8.59\" y2=\"10.49\"/></svg>"
        "</button>"
        "<button class=\"hide-btn\" id=\"hide-btn\" title=\"%(t_hide)s\" aria-label=\"%(t_hide)s\">-</button>"
        "<button class=\"close-btn\" id=\"close-btn\" title=\"%(t_close)s\" aria-label=\"%(t_close)s\">✖</button>"
        "</header>"
        "<div id=\"ge-toast\" class=\"ge-toast\" hidden></div>"
        "<div id=\"ge-region-box\" class=\"ge-region-box\" hidden></div>"
        "<div class=\"ge-body\">"
        "<h1>%(title)s</h1>"
        "%(note_block)s"
        "<div class=\"ge-main\">%(artifact_block)s%(chat_pane)s</div>"
        "</div>"
        # chat controller first (defines window.geChat incl. addImg), then the header/region chrome that uses it
        "<script>%(chat_js)s</script>"
        "<script>%(chrome_js)s</script>"
        "</body></html>"
    ) % {
        "title": title,
        "kind": kind,
        "lang": lang,
        "note_block": note_block,
        "artifact_block": artifact_block,
        "chat_pane": chat_pane,
        "t_region": t_region,
        "t_shot": t_shot,
        "t_share": t_share,
        "t_hide": t_hide,
        "t_close": t_close,
        "chat_js": ge_chat_js(_locale),
        "chrome_js": ge_chrome_js(_locale),
    }


def render_activation_html(spec: PopupSpec) -> str:
    """Render the first-use device-activation FORM into a self-contained popup body.

    Unlike ``render_artifact_html`` (which DISPLAYS a server artifact), this COLLECTS input:
    an activation-secret field + an optional device-name field. Submit gathers the field
    VALUES in-page and calls ``window.pywebview.api.commit({"activation_secret": ...,
    "device_name": ...})``; Cancel calls ``dismiss()``. The typed values leave only through
    the ``result.json`` handshake and are NEVER re-rendered into the page, so user input has
    no injection surface. Only caller-controlled ``title`` / ``note`` and the prefill
    ``device_name_default`` reach the DOM, all ``html.escape``-d.

    ``spec.payload`` may carry ``{"device_name_default": str, "error": str}``: the first
    prefills the name field; the second shows a retry notice (the caller passes ONLY generic,
    self-authored text here — never a server raw error or the secret). Namespaced element ids
    (``de-act-*``) keep the controls isolated. The secret field is ``type=password`` so it is
    not shoulder-surfed or captured by a screen grab; it is never echoed back into markup."""
    title_text = spec.title if isinstance(spec.title, str) and spec.title else "Decision Engine"
    note_text = spec.note if isinstance(spec.note, str) else ""
    payload = spec.payload if isinstance(spec.payload, dict) else {}
    name_default = payload.get("device_name_default")
    name_default = name_default if isinstance(name_default, str) else ""
    error_text = payload.get("error")
    error_text = error_text if isinstance(error_text, str) else ""

    # Single resolver-chosen language (explicit ui_locale in payload → $DE_UI_LOCALE → system → en-US).
    resolved = i18n.resolve_locale(payload.get("ui_locale"))
    labels = i18n.activation(resolved)

    title = html.escape(title_text, quote=True)
    note = html.escape(note_text, quote=True) if note_text else ""
    name_value = html.escape(name_default, quote=True)
    error = html.escape(error_text, quote=True) if error_text else ""

    note_block = "<p class=\"note\">%s</p>" % note if note else ""
    # Fixed error slot: html.escape(error) is already safe; the value is caller-authored
    # generic text (see docstring), never server output.
    error_block = "<p class=\"error\" role=\"alert\">%s</p>" % error if error else ""
    return (
        "<!doctype html><html lang=\"%(lang)s\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>%(title)s</title>"
        "<style>"
        "html,body{height:100%%}"
        "body{font:15px -apple-system,system-ui,sans-serif;margin:0;padding:24px;"
        "box-sizing:border-box;display:flex;flex-direction:column;"
        "background:#faf6ec;color:#1c1c1c}"
        "h1{font-size:20px;margin:.2em 0 .4em}"
        ".note{color:#444;margin:.2em 0 .8em}"
        ".error{color:#b0281a;margin:.2em 0 .8em;font-weight:600}"
        "label{display:block;font-size:13px;color:#5a4e30;margin:.8em 0 .3em}"
        "input{font:inherit;width:100%%;box-sizing:border-box;padding:9px 11px;"
        "border:1px solid #c9bd97;border-radius:8px;background:#fff}"
        "input:focus{outline:2px solid #2f6f4f;outline-offset:1px}"
        ".actions{margin-top:22px;display:flex;gap:12px}"
        "button{font:inherit;padding:9px 18px;border-radius:8px;"
        "border:1px solid #c9bd97;cursor:pointer}"
        "#de-act-submit{background:#2f6f4f;color:#fff;border-color:#2f6f4f}"
        "#de-act-cancel{background:#fff}"
        "</style></head><body>"
        "<h1>%(title)s</h1>"
        "%(note_block)s"
        "%(error_block)s"
        "<label for=\"de-act-secret\">%(secret_label)s</label>"
        "<input id=\"de-act-secret\" type=\"password\" autocomplete=\"off\" "
        "autocapitalize=\"off\" autocorrect=\"off\" spellcheck=\"false\" autofocus>"
        "<label for=\"de-act-name\">%(name_label)s</label>"
        "<input id=\"de-act-name\" type=\"text\" autocomplete=\"off\" value=\"%(name_value)s\">"
        "<div class=\"actions\">"
        "<button id=\"de-act-submit\">%(submit_label)s</button>"
        "<button id=\"de-act-cancel\">%(cancel_label)s</button>"
        "</div>"
        "<script>"
        "function api(){return (window.pywebview&&window.pywebview.api)||null;}"
        "function de_submit(){var a=api();if(!a)return;"
        "var s=document.getElementById('de-act-secret').value;"
        "var n=document.getElementById('de-act-name').value;"
        "a.commit({activation_secret:s,device_name:n});}"
        "document.getElementById('de-act-submit').onclick=de_submit;"
        "document.getElementById('de-act-cancel').onclick="
        "function(){var a=api();if(a)a.dismiss();};"
        "document.getElementById('de-act-secret').addEventListener('keydown',"
        "function(e){if(e.key==='Enter'){e.preventDefault();de_submit();}});"
        "</script>"
        "</body></html>"
    ) % {
        "lang": resolved,
        "title": title,
        "note_block": note_block,
        "error_block": error_block,
        "name_value": name_value,
        "secret_label": html.escape(labels["secret_label"], quote=True),
        "name_label": html.escape(labels["name_label"], quote=True),
        "submit_label": html.escape(labels["submit"], quote=True),
        "cancel_label": html.escape(labels["cancel"], quote=True),
    }


_MAC_POPUP_APP_NAME = "Decision Engine Popup"
_MAC_POPUP_BUNDLE_ID = "com.deeppattern.decisionengine.popup"


def _deeppattern_state_root() -> Path:
    config = os.getenv("DE_CONFIG_PATH")
    if config:
        return Path(config).expanduser().parent
    return Path.home() / ".deeppattern"


def _mac_popup_app_path() -> Path:
    override = os.getenv("DE_POPUP_APP")
    if override:
        return Path(override).expanduser()
    return _deeppattern_state_root() / "app" / f"{_MAC_POPUP_APP_NAME}.app"


def _mac_popup_executable_path(app_path: Optional[Path] = None) -> Path:
    root = app_path or _mac_popup_app_path()
    return root / "Contents" / "MacOS" / _MAC_POPUP_APP_NAME


def _write_macos_popup_wrapper(source: Path, target: Path) -> None:
    """Write a tiny app-bundle executable without relocating the Python binary."""
    script = (
        "#!/bin/sh\n"
        f"exec {shlex.quote(str(source))} \"$@\"\n"
    )
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(script, encoding="utf-8", newline="\n")
    tmp.chmod(0o755)
    os.replace(tmp, target)


def _ensure_macos_popup_app(python: str) -> Optional[Path]:
    """Create the real macOS app bundle that owns popup Dock identity."""
    if sys.platform != "darwin":
        return None
    try:
        source = Path(python).expanduser()
        if not source.is_file():
            return None
        if any(char in str(source) for char in ("\n", "\r")):
            return None
        app_path = _mac_popup_app_path()
        contents = app_path / "Contents"
        macos_dir = contents / "MacOS"
        resources = contents / "Resources"
        macos_dir.mkdir(parents=True, exist_ok=True)
        resources.mkdir(parents=True, exist_ok=True)
        info = {
            "CFBundleDevelopmentRegion": "en",
            "CFBundleDisplayName": _MAC_POPUP_APP_NAME,
            "CFBundleExecutable": _MAC_POPUP_APP_NAME,
            "CFBundleIconFile": "AppIcon",
            "CFBundleIdentifier": _MAC_POPUP_BUNDLE_ID,
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": _MAC_POPUP_APP_NAME,
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "10.15",
            "NSHighResolutionCapable": True,
        }
        with open(contents / "Info.plist", "wb") as handle:
            plistlib.dump(info, handle, sort_keys=True)
        icon = Path(__file__).resolve().parents[2] / "desktop" / "icons" / "AppIcon.icns"
        if icon.is_file():
            import shutil

            shutil.copy2(icon, resources / "AppIcon.icns")
        executable = _mac_popup_executable_path(app_path)
        _write_macos_popup_wrapper(source, executable)
        return executable
    except OSError:
        return None


def _popup_python(python: str) -> str:
    """The console-less interpreter for the native popup shell.

    On Windows a console-subsystem ``python.exe`` pops a black console window next to the
    FRAMELESS popup even when the child is spawned DETACHED. Prefer the GUI-subsystem
    ``pythonw.exe`` beside the given interpreter (same venv, same site-packages) so no console
    is ever allocated; fall back to the original path if it is absent or ``python`` is not a
    usable path. POSIX has no such split, so the interpreter is returned unchanged. Mirrors
    ``client.runner._gui_python`` so both detached-child spawns behave identically."""
    if os.name != "nt":
        return python
    try:
        interpreter = Path(python)
        if not interpreter.is_absolute():
            return python
        candidate = interpreter.with_name("pythonw.exe")
        return str(candidate) if candidate.is_file() else python
    except (TypeError, ValueError, OSError):
        return python


def build_shell_command(python: str, html_path: str, title: str, result_path: str,
                        chat_context_path: Optional[str] = None,
                        on_close_dismiss: bool = False,
                        api_profile: str = "legacy",
                        bridge_state_stdin: bool = False,
                        chat_bridge_stdin: bool = False,
                        ready_path: Optional[str] = None) -> List[str]:
    """The argv that launches the native shell child in ``python``'s venv.

    ``chat_context_path`` (GE follow-up chat) appends ``--context <path>`` so the popup wires a
    ChatSession from that bundle. The bootstrap pins the child to THIS launcher's source tree before
    importing the shell, so an embedded Python ``.pth`` cannot silently route it to another checkout.
    ``chat_bridge_stdin`` appends only the non-sensitive ``--chat-bridge-stdin`` switch; the actual
    server bootstrap envelope is delivered through the child pipe by ``session.spawn``.
    ``ready_path`` appends the child-to-parent loaded+DOM-ready marker path used by detached spawn.
    ``on_close_dismiss`` (the detached ``session.spawn`` poll path) appends ``--on-close-dismiss`` so a
    bare OS-chrome close records a terminal ``dismissed``; the blocking ``open_popup`` path leaves it
    off, so its OS-close still maps to ``closed`` (unchanged)."""
    source_root = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        "import runpy,sys;"
        f"sys.path.insert(0,{source_root!r});"
        f"runpy.run_module({NATIVE_SHELL_MODULE!r},run_name='__main__',alter_sys=True)"
    )
    executable = _ensure_macos_popup_app(python)
    if executable is not None:
        python = str(executable)
    cmd = [
        python,
        "-c",
        bootstrap,
        "--html",
        html_path,
        "--title",
        title,
        "--result-path",
        result_path,
    ]
    if chat_context_path:
        cmd += ["--context", chat_context_path]
    if on_close_dismiss:
        cmd += ["--on-close-dismiss"]
    if api_profile != "legacy":
        cmd += ["--api-profile", api_profile]
    if bridge_state_stdin:
        cmd += ["--bridge-state-stdin"]
    if chat_bridge_stdin:
        cmd += ["--chat-bridge-stdin"]
    if ready_path:
        cmd += ["--ready-path", ready_path]
    return cmd


def read_popup_result(result_path: str) -> Optional[Dict[str, Any]]:
    """Read the child's committed result, or None if it wrote nothing / bad JSON."""
    path = Path(result_path)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def open_popup(
    spec: PopupSpec,
    *,
    python: Optional[str] = None,
    timeout_s: float = 1800.0,
    label: str = "de-popup",
    render: Callable[[PopupSpec], str] = render_artifact_html,
) -> Dict[str, Any]:
    """Open the popup and return the outcome. Never raises for an expected failure.

    ``render`` selects the popup BODY: the default ``render_artifact_html`` DISPLAYS a
    server artifact; ``render_activation_html`` collects the first-use activation form. Both
    share the identical spawn → read-result handoff, so swapping the renderer changes nothing
    below. A renderer that raises ``TypeError`` / ``ValueError`` maps to ``render-failed``.

    ``timeout_s`` bounds the WHOLE interaction (the child blocks on the window until
    the user acts), so the default is generous — a considered high-stakes decision may
    take many minutes — while still capping a wedged child so the caller can't hang
    forever. On timeout a result committed just before the deadline is still honored.

    Outcomes: ``committed`` (user confirmed, ``result`` carries the payload),
    ``dismissed`` (user cancelled), ``closed`` (window closed with no action),
    ``timeout`` (interaction exceeded ``timeout_s`` with no result),
    ``render-failed`` (spec.payload not JSON-serializable), ``launch-failed`` (could
    not spawn the child), ``native-shell-failed`` (child exited nonzero with no
    result), ``no-webview-backend`` (native backend unavailable — no browser fallback)."""
    python = python or sys.executable
    if not backend.ensure_webview(python, label):
        return {"ok": False, "outcome": "no-webview-backend", "result": None}

    workdir = Path(tempfile.mkdtemp(prefix="de-popup-"))
    html_path = workdir / "popup.html"
    result_path = workdir / "result.json"
    try:
        try:
            if spec.html_body is not None:
                # DB path: a full interactive HTML document, written verbatim (no
                # GE svg/image chrome). The board is the whole body.
                if not isinstance(spec.html_body, str) or not spec.html_body:
                    raise ValueError("spec.html_body must be a non-empty string")
                html_path.write_text(spec.html_body, encoding="utf-8")
            else:
                # #48 parametrized render (default render_artifact_html for GE; the
                # activation popup passes render=render_activation_html).
                html_path.write_text(render(spec), encoding="utf-8")
        except (TypeError, ValueError) as exc:
            # a missing / malformed artifact must not escape as a raw exception —
            # keep the structured-dict contract uniform across every failure mode
            return {"ok": False, "outcome": "render-failed", "result": None, "error": str(exc)}

        cmd = build_shell_command(_popup_python(python), str(html_path), spec.title or "Decision Engine", str(result_path))
        returncode: Optional[int] = None
        try:
            # stdin=DEVNULL: like the detached path (session.py:121) and the webview probes
            # (backend.py), the popup child must never inherit the caller's stdin — under a serving
            # shim that stdin is the live MCP JSON-RPC pipe, and a WebView2 grandchild inheriting it hangs.
            returncode = subprocess.run(cmd, stdin=subprocess.DEVNULL, timeout=timeout_s).returncode
        except subprocess.TimeoutExpired:
            # the child was SIGKILLed; a result written just before the deadline is
            # still valid — only report "timeout" if nothing landed
            if read_popup_result(str(result_path)) is None:
                return {"ok": False, "outcome": "timeout", "result": None}
        except OSError as exc:
            return {"ok": False, "outcome": "launch-failed", "result": None, "error": str(exc)}

        written = read_popup_result(str(result_path))
        if written is None:
            # no result file: a nonzero child exit is a crash, not a user-initiated close
            if returncode not in (0, None):
                return {"ok": False, "outcome": "native-shell-failed",
                        "result": None, "returncode": returncode}
            return {"ok": False, "outcome": "closed", "result": None}
        outcome = written.get("outcome")
        return {
            "ok": outcome == "committed",
            "outcome": outcome or "closed",
            "result": written.get("result"),
        }
    finally:
        # rmtree (not targeted unlinks) so a stray result.json.<pid>.tmp left by a
        # child killed mid-write doesn't leak the whole workdir
        shutil.rmtree(workdir, ignore_errors=True)


def open_board_popup(
    board_spec: Dict[str, Any],
    *,
    base_url: str,
    token: str,
    python: Optional[str] = None,
    timeout_s: float = 1800.0,
    fetch_timeout_s: float = 30.0,
    title: Optional[str] = None,
    label: str = "de-db-popup",
) -> Dict[str, Any]:
    """Fetch a DB board from the server and show it in the native popup.

    The one seam the DB flow adds on top of PR#34's frozen handoff: fetch the
    finished, diagram-stripped board HTML from ``/db/render`` (``fetch_board_html``)
    and hand it to ``open_popup`` on the DB ``html_body`` path — the native shell /
    ``result.json`` round-trip is unchanged, so a user Submit returns the adjusted
    board (``{columns, notes, comments, pages?, annotations?, ...}``) to the caller
    verbatim.

    On a fetch failure returns a structured ``render-fetch-failed`` outcome (the
    ``BoardFetchError`` detail rides on ``error``) — the client never falls back to
    a local render or a system browser. All other outcomes are ``open_popup``'s
    (``committed`` / ``dismissed`` / ``closed`` / ``timeout`` / ``no-webview-backend`` …)."""
    candidate_title = title or (board_spec.get("title") if isinstance(board_spec, dict) else None)
    resolved_title = (
        _prefixed_popup_title(candidate_title)
        if isinstance(candidate_title, str) and candidate_title.strip()
        else _default_surface_title(
            "discussion_board",
            board_spec.get("ui_locale") if isinstance(board_spec, dict) else None,
            fallback="Discussion Board",
        )
    )
    try:
        html_body = fetch_board_html(
            board_spec, base_url=base_url, token=token, timeout_s=fetch_timeout_s
        )
    except BoardFetchError as exc:
        return {"ok": False, "outcome": "render-fetch-failed", "result": None, "error": str(exc)}
    spec = PopupSpec(kind="db", title=resolved_title, html_body=html_body)
    return open_popup(spec, python=python, timeout_s=timeout_s, label=label)


def _smoke_artifact(title: str) -> Dict[str, str]:
    """A trivial inline-SVG artifact so the CLI opens a real, visible window without a
    server round-trip. It is a launch smoke test only — NOT graphic-explanation content
    (the real artifact always arrives on ``spec.artifact`` from the server render seam)."""
    label = html.escape(title or "Decision Engine", quote=True)
    return {
        "kind": "svg",
        "data": (
            "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 480 200\">"
            "<rect width=\"480\" height=\"200\" fill=\"#fffdf6\"/>"
            "<text x=\"240\" y=\"108\" text-anchor=\"middle\" font-family=\"sans-serif\" "
            "font-size=\"20\" fill=\"#2f6f4f\">%s</text></svg>" % label
        ),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="decision-engine-popup", description="Open a DB/GE popup")
    parser.add_argument("--kind", choices=("db", "ge"), default="db")
    parser.add_argument("--title", default="Decision Engine")
    parser.add_argument("--note", default="")
    parser.add_argument("--json", action="store_true", help="print the outcome as JSON")
    args = parser.parse_args(argv)

    # The CLI is a launch smoke test: it carries a trivial local artifact so a real window
    # opens. Production callers deliver the server-rendered artifact on ``spec.artifact``.
    spec = PopupSpec(kind=args.kind, title=args.title, note=args.note,
                     artifact=_smoke_artifact(args.title))
    outcome = open_popup(spec)
    if args.json:
        print(json.dumps(outcome, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("outcome: %s" % outcome.get("outcome"))
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
