"""Native popup shell — the child process that owns the DB/GE window + result.

Run (must be a Python whose venv has pywebview — see ``backend.ensure_webview``):

    python -m client.popup.native_shell --html POPUP.html --result-path RESULT.json \
        [--context BUNDLE.json] [--on-close-dismiss]

The page's Submit/Dismiss buttons call ``window.pywebview.api.commit(state)`` /
``window.pywebview.api.dismiss()``; each writes ``result-path`` ATOMICALLY (so the
launcher never reads a half-written file) and closes the window.

Two behaviours the detached (session.spawn) path adds on top of the blocking launcher:

* ``--on-close-dismiss`` — when the window is closed via the OS chrome (X / WM_CLOSE)
  with neither Submit nor Dismiss fired, the shell records a terminal ``dismissed`` so a
  detached ``session.poll`` returns a terminal state instead of hanging ``open`` until the
  TTL sweep (design §4.2). The blocking ``launcher.open_popup`` path does NOT pass this
  flag, so its OS-close still maps to ``closed`` — unchanged.
* ``--context BUNDLE.json`` — a GE follow-up-chat context bundle
  ({title, mode, caller?, source_text?, strings?, …}). When present (and the chat backend
  imports), the popup's right column becomes a live follow-up chat: ``chat_ready()`` unlocks
  the composer, ``ask(turn_id, text, images)`` runs ONE turn against the ChatSession on a
  worker thread and streams deltas back to the page via ``window.geChat``. The follow-up LLM
  is a process-isolated consumer of the rendered image (design §4.1) — outside the G1
  byte-isolation boundary by design. The bundle is deleted from disk right after it is read
  (it can hold the user's ``source_text`` — a privacy surface if left in a temp dir).

``--self-check`` exercises the exact commit → result-file plumbing WITHOUT opening a
window, so the handoff can be verified headlessly (CI / no display).
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import base64
import ctypes
import hashlib
import json
import math
import os
import re
import struct
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

try:
    # GE follow-up chat backend (pywebview-free core). Optional: a missing backend degrades the popup
    # to display-only (chat_ready() → False, the composer stays disabled), never a hard failure.
    from client.popup import chat_backend
except Exception:  # aqg: import boundary — chat is optional
    chat_backend = None

from client.popup import launcher, session as popup_session
from client import i18n


# The window-shell locale. The popup HTML that launcher renders already carries the resolver's
# chosen language as <html lang="zh|en">; native_shell reads that back (the resolver's OUTPUT, not
# a re-sniff of content) so the frameless-header control tooltips, the macOS tray tooltip, and the
# chat error fallback all match the language the page is already showing. Defaults to the resolver's
# own answer if a loaded page carries no lang (e.g. a bare DB board body).
_SHELL_LOCALE = i18n.DEFAULT_LOCALE


def _read_html_lang(html_path: str) -> str:
    """Resolve the shell locale from a loaded popup's <html lang=...>; fall back to the resolver."""
    try:
        head = Path(html_path).read_text(encoding="utf-8", errors="replace")[:4096]
        m = re.search(r"<html[^>]*\blang\s*=\s*[\"']([^\"']+)[\"']", head, re.IGNORECASE)
        if m:
            return i18n.resolve_locale(m.group(1))
    except Exception:  # aqg: boundary — a locale read must never block opening the window
        pass
    return i18n.resolve_locale(None)

# Cap the lazily-lifted diagram source so a huge inlined SVG never crosses the JS bridge whole (the
# slice also happens IN the JS — bound the IPC payload at the source), mirroring the chat preheat cap.
_MAX_VISUAL_CHARS = 8000
_MAX_CAPTURE_IMAGE_B64 = getattr(chat_backend, "_MAX_IMG_B64", 12 * 1024 * 1024)
_MAX_WINDOWS_CAPTURE_PIXELS = 40_000_000
_MAX_CAPTURE_SCALE_FACTOR = 3

_IS_MAC = sys.platform == "darwin"
_IS_WINDOWS = sys.platform == "win32"
# Menu-bar (status-bar) icon file: the client ships it under desktop/macos/bin/. This file lives at
# client/popup/native_shell.py, so the repo root is parent.parent.parent. If it's missing on an installed
# client, _apply_mac_chrome falls back to a ◧ glyph — the icon is polish, the toggle works regardless.
#
# In bin/ rather than macos/ because the Swift stop panel reads the SAME file through
# Bundle.main.url(forResource:), and for a bare Mach-O executable Bundle.main is the directory
# holding the executable, searched no further. One shared copy, placed where the stricter reader
# looks; a test pins it beside the binary.
_ICON_FILE = (Path(__file__).resolve().parent.parent.parent
              / "desktop" / "macos" / "bin" / "stopper_icon.png")
# Keep the NSStatusItem + its ObjC click target alive for the process lifetime (else GC drops the menu-bar
# icon + its action). Module-global, mirroring the reference A-repo shell.
_STATUS_KEEP: list = []

# Shared frameless-window controls. CSS draws the maximize glyph so Windows and
# macOS use the same crisp control, and transparent edge/corner handles bridge
# resize gestures to pywebview without relying on platform-native window borders.
_WINDOW_CHROME_JS = r"""
(function () {
  var header = document.querySelector('header.pywebview-drag-region');
  var closeBtn = document.getElementById('close-btn');
  var hideBtn = document.getElementById('hide-btn');
  if (!header || !closeBtn || !hideBtn || !header.contains(closeBtn) || !header.contains(hideBtn)) {
    return 'missing-chrome';
  }

  if (!document.getElementById('de-window-chrome-style')) {
    var style = document.createElement('style');
    style.id = 'de-window-chrome-style';
    style.textContent =
      '.de-window-control{width:var(--win-btn,30px)!important;height:var(--win-btn,30px)!important;min-width:var(--win-btn,30px)!important;' +
      'border:0!important;border-radius:var(--win-btn-radius,8px)!important;padding:0!important;margin-left:0!important;' +
      'display:inline-flex!important;align-items:center!important;justify-content:center!important;' +
      'box-shadow:0 1px 3px rgba(0,0,0,.14)!important;position:relative!important;box-sizing:border-box!important;' +
      'z-index:2147483646!important;}' +
      '.maximize-btn{background:#22c55e!important;color:#fff!important;}' +
      '.maximize-btn:hover{background:#16a34a!important;}' +
      '.maximize-btn::before{content:"";width:calc(var(--win-btn-icon,16px) - 4px);height:calc(var(--win-btn-icon,16px) - 4px);border:2px solid currentColor;' +
      'border-radius:2px;box-sizing:border-box;}' +
      '.maximize-btn.is-maximized::before{box-shadow:-3px 3px 0 -1px #22c55e,-3px 3px 0 0 currentColor;}' +
      '.de-resize-handle{position:fixed;z-index:2147483645;background:transparent;user-select:none;}' +
      '.de-resize-n,.de-resize-s{left:11px;right:11px;height:7px;cursor:ns-resize;}' +
      '.de-resize-n{top:0}.de-resize-s{bottom:0}' +
      '.de-resize-e,.de-resize-w{top:11px;bottom:11px;width:7px;cursor:ew-resize;}' +
      '.de-resize-e{right:0}.de-resize-w{left:0}' +
      '.de-resize-ne,.de-resize-se,.de-resize-sw,.de-resize-nw{width:11px;height:11px;}' +
      '.de-resize-ne{right:0;top:0;cursor:nesw-resize}' +
      '.de-resize-se{right:0;bottom:0;cursor:nwse-resize}' +
      '.de-resize-sw{left:0;bottom:0;cursor:nesw-resize}' +
      '.de-resize-nw{left:0;top:0;cursor:nwse-resize}';
    document.head.appendChild(style);
  }
  hideBtn.classList.add('de-window-control');
  closeBtn.classList.add('de-window-control');
  var controlHost = document.getElementById('window-actions');
  if (!controlHost || !controlHost.contains(closeBtn) || !controlHost.contains(hideBtn)) {
    controlHost = header;
  }

  if (!document.getElementById('maximize-btn')) {
    var maximizeBtn = document.createElement('button');
    maximizeBtn.type = 'button';
    maximizeBtn.id = 'maximize-btn';
    maximizeBtn.className = 'tool-btn maximize-btn';
    maximizeBtn.classList.add('de-window-control');
    var _CHROME_STRINGS = __SHELL_STRINGS__;
    function renderMaximized(maximized) {
      maximizeBtn.classList.toggle('is-maximized', maximized === true);
      maximizeBtn.title = maximized ? _CHROME_STRINGS.restore : _CHROME_STRINGS.maximize;
      maximizeBtn.setAttribute('aria-label', maximizeBtn.title);
    }
    window.deWindowChrome = {setMaximized: renderMaximized};
    renderMaximized(false);
    var stateApi = window.pywebview && window.pywebview.api;
    if (stateApi && stateApi.window_state) {
      Promise.resolve()
        .then(function () { return stateApi.window_state(); })
        .then(function (state) {
          if (state && state.ok) renderMaximized(state.maximized === true);
        })
        .catch(function () {});
    }
    maximizeBtn.onclick = function () {
      var api = window.pywebview && window.pywebview.api;
      if (api && api.toggle_maximize) {
        Promise.resolve()
          .then(function () { return api.toggle_maximize(); })
          .then(function (result) {
            if (result && result.ok) renderMaximized(result.maximized === true);
          })
          .catch(function () {});
      }
    };
    controlHost.insertBefore(maximizeBtn, closeBtn);
  }

  if (!document.querySelector('.de-resize-handle')) {
    var EDGES = ['n', 'e', 's', 'w', 'ne', 'se', 'sw', 'nw'];
    var resizeSeq = 0;
    var resize = null, latestGesture = null, resizeInFlight = false;
    function stopResize() {
      resize = null;
      latestGesture = null;
      window.removeEventListener('mousemove', moveResize);
      window.removeEventListener('mouseup', stopResize);
    }
    function flushResize() {
      if (resizeInFlight || !latestGesture) return;
      var gesture = latestGesture;
      latestGesture = null;
      var api = window.pywebview && window.pywebview.api;
      if (!api || !api.resize_window) return;
      resizeInFlight = true;
      Promise.resolve()
        .then(function () {
          return api.resize_window(gesture.id, gesture.edge, gesture.dx, gesture.dy);
        })
        .then(function (result) {
          if (result && result.dragging === false) stopResize();
        })
        .catch(function () {})
        .finally(function () {
          resizeInFlight = false;
          flushResize();
        });
    }
    function moveResize(event) {
      if (!resize) return;
      if (typeof event.buttons === 'number' && (event.buttons & 1) === 0) {
        stopResize();
        return;
      }
      latestGesture = {
        id: resize.id,
        edge: resize.edge,
        dx: event.screenX - resize.pointerX,
        dy: event.screenY - resize.pointerY
      };
      flushResize();
    }
    EDGES.forEach(function (edge) {
      var handle = document.createElement('div');
      handle.className = 'de-resize-handle de-resize-' + edge;
      handle.dataset.edge = edge;
      handle.addEventListener('mousedown', function (event) {
        if (event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();
        resize = {
          id: ++resizeSeq,
          edge: edge,
          pointerX: event.screenX,
          pointerY: event.screenY
        };
        window.addEventListener('mousemove', moveResize);
        window.addEventListener('mouseup', stopResize);
      });
      document.body.appendChild(handle);
    });
    window.addEventListener('blur', stopResize);
  }
  return 'enhanced';
})();
"""

# The server-rendered graph script runs during HTML parsing, before pywebview
# injects window.pywebview.api. Its same-origin fallback cannot work from a
# local file, so a first-load failure banner can precede an otherwise healthy
# native bridge. Once `loaded` proves the bridge exists, retry the page's own
# reset/relayout contract. The observer covers the inverse race where the
# fallback failure lands just after `loaded`; retries are deliberately bounded.
_DIAGRAM_LAYOUT_RECOVERY_JS = r"""
(function () {
  var api = window.pywebview && window.pywebview.api;
  var resetBtn = document.getElementById('reset-view');
  var boardArea = document.getElementById('board-area');
  if (!api || !api.layout || !resetBtn || !boardArea) return 'not-applicable';

  var retries = 0;
  var scheduled = false;
  function retryIfBannered() {
    if (scheduled || retries >= 2 || !document.querySelector('.gv-error')) return;
    scheduled = true;
    var timer = setTimeout(function () {
      scheduled = false;
      if (!document.querySelector('.gv-error')) return;
      retries += 1;
      resetBtn.click();
    }, 0);
    if (timer && timer.unref) timer.unref();
  }

  var observer = null;
  if (typeof MutationObserver === 'function') {
    observer = new MutationObserver(retryIfBannered);
    observer.observe(boardArea, {childList: true, subtree: true});
  }
  retryIfBannered();
  var secondCheck = setTimeout(retryIfBannered, 250);
  if (secondCheck && secondCheck.unref) secondCheck.unref();
  if (observer) {
    var cleanup = setTimeout(function () { observer.disconnect(); }, 5000);
    if (cleanup && cleanup.unref) cleanup.unref();
  }
  return 'armed';
})();
"""

# Windows pywebview 6.2.1's document-level class drag path is ineffective for this detached frameless
# WebView2 window even when the page carries the documented class. The loaded-page shim below keeps
# the stable HTML chrome contract but uses an explicit, bounded page→Window.move bridge instead.
_WINDOWS_DRAG_JS = r"""
(function () {
  var header = document.querySelector('header.pywebview-drag-region');
  var closeBtn = document.getElementById('close-btn');
  var hideBtn = document.getElementById('hide-btn');
  if (!header || !closeBtn || !header.contains(closeBtn)) return 'missing-chrome';

  var INTERACTIVE = 'button, input, a, select, textarea, label, [role="button"], [contenteditable]';
  if (header.dataset.deWindowsDrag !== '1') {
    header.dataset.deWindowsDrag = '1';
    var dragOffset = null;
    var latestMove = null;
    var moveInFlight = false;
    function stopDrag() {
      dragOffset = null;
      latestMove = null;
      window.removeEventListener('mousemove', moveDrag);
      window.removeEventListener('mouseup', stopDrag);
    }
    function flushMove() {
      if (moveInFlight || !latestMove) return;
      var point = latestMove;
      latestMove = null;
      var api = window.pywebview && window.pywebview.api;
      if (!api || !api.move_window) return;
      moveInFlight = true;
      Promise.resolve()
        .then(function () { return api.move_window(point.x, point.y); })
        .then(function (result) {
          if (result && result.dragging === false) stopDrag();
        })
        .catch(function () {})
        .finally(function () {
          moveInFlight = false;
          flushMove();
        });
    }
    function moveDrag(event) {
      if (!dragOffset) return;
      latestMove = {x: event.screenX - dragOffset.x, y: event.screenY - dragOffset.y};
      flushMove();
    }
    header.addEventListener('mousedown', function (event) {
      if (event.button !== 0) return;
      if (event.target.closest && event.target.closest(INTERACTIVE)) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      dragOffset = {x: event.clientX, y: event.clientY};
      window.addEventListener('mousemove', moveDrag);
      window.addEventListener('mouseup', stopDrag);
    });
    window.addEventListener('blur', stopDrag);
  }

  return 'enhanced';
})();
"""


def _write_json_atomic(path_value: str, payload: Dict[str, Any]) -> None:
    """Atomically write a small JSON handoff file so readers never see a partial file."""
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".%d.tmp" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_result(result_path: str, payload: Dict[str, Any]) -> None:
    """Atomically write the popup result so the launcher never sees a partial file."""
    _write_json_atomic(result_path, payload)


# The only geChat functions the shell ever calls. `fn` is interpolated raw into executable JS, so it is
# whitelisted here (defense-in-depth: even a future non-literal `fn` can never become arbitrary bridge JS).
_GECHAT_FNS = frozenset({"bootstrap", "state", "delta", "done", "error", "willClose"})
_CHAT_ERROR_CODES = frozenset(
    {
        "capability_disabled",
        "server_unsupported",
        "client_unsupported",
        "auth_required",
        "privacy_confirmation_required",
        "invalid_request",
        "input_too_large",
        "turn_in_flight",
        "conversation_busy",
        "conversation_expired",
        "rate_limited",
        "insufficient_credits",
        "model_unavailable",
        "timeout",
        "cancelled",
        "network_error",
        "bad_response",
        "not_found",
        "chat_unavailable",
    }
)
_CHAT_STATES_EMPTY = frozenset(
    {
        "capability_checking",
        "creating_conversation",
        "restoring_history",
        "ready",
        "cancelled",
        "recovering",
    }
)
_CHAT_STATES_PRICE = frozenset({"submitting", "queued", "running", "calling_model"})
_CLIENT_TURN_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PRIVACY_VERSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_DISPLAY_TURN_ID = (1 << 53) - 1
_MAX_CHAT_TEXT_CHARS = 20_000
_MAX_CHAT_TEXT_BYTES = 80_000
_MAX_CHAT_OUTPUT_CHARS = 200_000
_MAX_CHAT_DATA_URL_CHARS = 5_592_500
_MAX_CHAT_REQUEST_BYTES = 20 * 1024 * 1024
_MAX_CHAT_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_CHAT_IMAGE_TOTAL_BYTES = 12 * 1024 * 1024
_CHAT_CLOSE_BUDGET_S = 3.0
_FRONTEND_SECRET_KEYS = frozenset(
    {"token", "device_token", "access_token", "endpoint", "run_id", "conversation_code", "turn_code"}
)


def _contains_frontend_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in _FRONTEND_SECRET_KEYS:
                return True
            if _contains_frontend_secret_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_frontend_secret_key(item) for item in value)
    return False


def _safe_disclosure_text(value: Any) -> Optional[str]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        return value if len(value.encode("utf-8")) <= 1024 else None
    except UnicodeEncodeError:
        return None


def _safe_error_code(value: Any, default: str = "chat_unavailable") -> str:
    return value if isinstance(value, str) and value in _CHAT_ERROR_CODES else default


def _safe_state_payload(state: Any, payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(state, str) or not isinstance(payload, dict):
        return None
    if state == "recovering":
        if not payload:
            return {}
        if set(payload) != {"error_code"}:
            return None
        code = _safe_error_code(payload.get("error_code"), default="")
        return {"error_code": code} if code else {}
    if state in _CHAT_STATES_EMPTY:
        return {} if not payload else None
    if state in _CHAT_STATES_PRICE:
        if not payload:
            return {}
        if set(payload) != {"price_credits"}:
            return None
        price = payload.get("price_credits")
        if (
            isinstance(price, bool)
            or not isinstance(price, int)
            or not 0 <= price <= _MAX_DISPLAY_TURN_ID
        ):
            return None
        return {"price_credits": price}
    if state == "completed":
        if set(payload) != {"turn_count", "remaining_turns"}:
            return None
        values = (payload.get("turn_count"), payload.get("remaining_turns"))
        if any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= _MAX_DISPLAY_TURN_ID
            for item in values
        ):
            return None
        return {"turn_count": values[0], "remaining_turns": values[1]}
    if state in {"failed", "unavailable"}:
        if set(payload) != {"error_code"}:
            return None
        code = _safe_error_code(payload.get("error_code"), default="")
        return {"error_code": code} if code else None
    return None


def _safe_bootstrap(value: Any, *, backend: str) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or _contains_frontend_secret_key(value):
        return None
    if backend == "legacy":
        expected = {
            "ok": True,
            "backend": "legacy",
            "state": "ready",
            "conversation": None,
            "privacy_disclosure": None,
            "history": [],
        }
        return dict(expected) if value == expected else None
    if value.get("ok") is False:
        if set(value) != {"ok", "backend", "state", "error_code"}:
            return None
        code = _safe_error_code(value.get("error_code"), default="")
        if value.get("backend") != "server" or value.get("state") != "unavailable" or not code:
            return None
        return {
            "ok": False,
            "backend": "server",
            "state": "unavailable",
            "error_code": code,
        }
    if set(value) != {
        "ok",
        "backend",
        "state",
        "conversation",
        "privacy_disclosure",
        "history",
    }:
        return None
    if value.get("ok") is not True or value.get("backend") != "server" or value.get("state") != "ready":
        return None
    disclosure = value.get("privacy_disclosure")
    version = disclosure.get("version") if isinstance(disclosure, dict) else None
    if not isinstance(version, str) or _PRIVACY_VERSION_RE.fullmatch(version) is None:
        return None
    providers = disclosure.get("providers")
    provider_fields = (
        "category",
        "data_region",
        "training_enabled",
        "cache_ttl_seconds",
        "provider_retention_hours",
        "deletion_scope",
    )
    if not isinstance(providers, list) or not 1 <= len(providers) <= 16:
        return None
    safe_providers = []
    for provider in providers:
        if not isinstance(provider, dict) or not all(
            field in provider for field in provider_fields
        ):
            return None
        category = _safe_disclosure_text(provider["category"])
        data_region = _safe_disclosure_text(provider["data_region"])
        deletion_scope = _safe_disclosure_text(provider["deletion_scope"])
        training_enabled = provider["training_enabled"]
        cache_ttl_seconds = provider["cache_ttl_seconds"]
        provider_retention_hours = provider["provider_retention_hours"]
        if (
            category is None
            or data_region is None
            or deletion_scope is None
            or (training_enabled is not False and training_enabled is not None)
            or isinstance(cache_ttl_seconds, bool)
            or not isinstance(cache_ttl_seconds, int)
            or not 0 <= cache_ttl_seconds <= 2_147_483_647
            or isinstance(provider_retention_hours, bool)
            or not isinstance(provider_retention_hours, int)
            or not 0 <= provider_retention_hours <= 2_147_483_647
        ):
            return None
        safe_providers.append(
            {
                "category": category,
                "data_region": data_region,
                "training_enabled": training_enabled,
                "cache_ttl_seconds": cache_ttl_seconds,
                "provider_retention_hours": provider_retention_hours,
                "deletion_scope": deletion_scope,
            }
        )
    safe_value = dict(value)
    safe_value["privacy_disclosure"] = {
        "version": version,
        "providers": safe_providers,
    }
    # The HTTP session owns the detailed DTO validation.  This bridge adds a second trust-boundary
    # projection so only frozen disclosure fields reach the page, even with a nonstandard chat
    # implementation, then checks that the complete page payload is JSON-safe.
    try:
        json.dumps(safe_value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return safe_value


def _valid_display_turn_id(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= _MAX_DISPLAY_TURN_ID
    )


def _validate_server_images(images: list[str]) -> tuple[Optional[str], bool]:
    total = 0
    seen = set()
    duplicate = False
    prefixes = {
        "data:image/png;base64,": "png",
        "data:image/jpeg;base64,": "jpeg",
        "data:image/webp;base64,": "webp",
    }
    for item in images:
        prefix = next((candidate for candidate in prefixes if item.startswith(candidate)), None)
        if prefix is None:
            return "invalid_request", False
        encoded = item[len(prefix):]
        if not encoded or not encoded.isascii():
            return "invalid_request", False
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            return "invalid_request", False
        if len(raw) > _MAX_CHAT_IMAGE_BYTES:
            return "input_too_large", False
        total += len(raw)
        if total > _MAX_CHAT_IMAGE_TOTAL_BYTES:
            return "input_too_large", False
        kind = prefixes[prefix]
        matches = (
            (kind == "png" and raw.startswith(b"\x89PNG\r\n\x1a\n"))
            or (kind == "jpeg" and raw.startswith(b"\xff\xd8\xff"))
            or (kind == "webp" and len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
        )
        if not matches:
            return "invalid_request", False
        identity = hashlib.sha256(raw).digest()
        if identity in seen:
            duplicate = True
        else:
            seen.add(identity)
    return None, duplicate


def _server_admission_error(
    display_turn_id: Any,
    text: Any,
    images: Any,
    client_turn_id: Any,
    privacy_version: Any,
    expected_privacy_version: Optional[str],
) -> Optional[str]:
    if not _valid_display_turn_id(display_turn_id):
        return "invalid_request"
    if not isinstance(text, str) or not text:
        return "invalid_request"
    if len(text) > _MAX_CHAT_TEXT_CHARS:
        return "input_too_large"
    try:
        if len(text.encode("utf-8")) > _MAX_CHAT_TEXT_BYTES:
            return "input_too_large"
    except UnicodeError:
        return "invalid_request"
    if not isinstance(client_turn_id, str) or _CLIENT_TURN_RE.fullmatch(client_turn_id) is None:
        return "invalid_request"
    if not isinstance(privacy_version, str) or _PRIVACY_VERSION_RE.fullmatch(privacy_version) is None:
        return "invalid_request"
    if expected_privacy_version is None or privacy_version != expected_privacy_version:
        return "privacy_confirmation_required"
    if not isinstance(images, list) or len(images) > 5:
        return "invalid_request"
    for item in images:
        if not isinstance(item, str):
            return "invalid_request"
        if len(item) > _MAX_CHAT_DATA_URL_CHARS:
            return "input_too_large"
    try:
        compact = json.dumps(
            {"text": text, "images": images},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return "invalid_request"
    if len(compact) > _MAX_CHAT_REQUEST_BYTES:
        return "input_too_large"
    image_error, duplicate_image = _validate_server_images(images)
    if image_error is not None:
        return image_error
    return "invalid_request" if duplicate_image else None


def _js_call(fn: str, *args: Any) -> str:
    """Build a ``window.geChat.<fn>(...)`` JS call with every arg JSON-encoded (ensure_ascii=True → pure
    ASCII, so a raw U+2028/U+2029 can't break the JS string). ``evaluate_js`` runs JS, not HTML, and the
    page-side ``geChat`` handler assigns values via ``textContent`` (never ``innerHTML``) — so a streamed
    delta carries no markup-injection surface. Returns "" for a non-whitelisted ``fn`` (a no-op the caller
    skips) so an unexpected function name can never be interpolated into executable bridge JS."""
    if fn not in _GECHAT_FNS:
        return ""
    payload = ",".join(json.dumps(a, ensure_ascii=True) for a in args)
    return f"window.geChat&&window.geChat.{fn}({payload})"


class PopupApi:
    """Exposed to the page as ``window.pywebview.api``.

    ``commit`` records the user's confirmed result; ``dismiss`` records an explicit cancel. Both are
    serialized + one-shot so a rapid double-click (pywebview runs js_api calls on parallel worker threads)
    can't write twice or double-close.

    When a ``chat`` (ChatSession) is wired (a GE popup with a context bundle), ``chat_ready`` unlocks the
    page composer and ``ask`` runs a follow-up turn on a worker thread, streaming deltas back via
    ``window.geChat`` — so the js_api bridge thread and the window controls stay responsive during a long
    turn. A separate ``_turn_lock`` serializes turns (the GUI also disables Send while a turn is in flight).
    """

    def __init__(
        self,
        result_path: str,
        chat: Any = None,
        chat_route: Optional[str] = None,
        chat_error_code: Optional[str] = None,
        initial_state: Optional[Dict[str, Any]] = None,
        forbidden_token: Optional[str] = None,
    ) -> None:
        self._result_path = result_path
        self._lock = threading.Lock()       # one-shot result-write guard
        self._done = False
        self._win = None  # set by open_window once the window exists
        self._chat = chat                   # a chat_backend.ChatSession, or None for a display-only popup
        self._chat_route = chat_route or ("legacy" if chat is not None else None)
        self._chat_error_code = _safe_error_code(chat_error_code) if chat_error_code else None
        self._turn_lock = threading.Lock()  # serialize follow-up turns
        self._closed = False                # a queued turn must not run for a window that's closing
        self._busy = False                  # admission control: at most one follow-up turn in flight
        self._busy_lock = threading.Lock()  # guards _busy (set in ask, cleared in the worker's finally)
        self._terminal_flush_in_progress = False
        self._lifecycle_lock = threading.RLock()
        self._callback_publication_condition = threading.Condition()
        self._active_callback_publications = 0
        self._chat_close_lock = threading.Lock()
        self._chat_shutdown = False
        self._callback_generation = 0
        self._bootstrap_lock = threading.Lock()
        self._bootstrap_started = False
        self._privacy_version = None
        self._server_bootstrap = None
        self._active_client_turn_id = None
        self._active_display_turn_id = None
        self._last_server_request = None
        self._recovering = False
        self._maximized = False
        self._window_state_lock = threading.Lock()
        self._window_action_lock = threading.Lock()
        self._layout_slots = threading.BoundedSemaphore(value=2)
        self._resize_gesture_id = None
        self._resize_edge = None
        self._resize_base = None
        self._cursor_initial = initial_state
        self._cursor_forbidden_token = forbidden_token
        self._cursor_guard_required = False
        self._cursor_document_ready = threading.Event()
        self._cursor_document_ready.set()
        self._cursor_ready_timer = None

    def _finish(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._shutdown_chat()
        with self._lock:
            if self._done:
                return {"ok": False}
            try:
                write_result(self._result_path, payload)
            except Exception:  # aqg: top-level boundary — a write error must not crash the popup
                return {"ok": False}
            self._done = True
            self._closed = True   # so a queued follow-up turn skips a window that's going away
        if self._win is not None:
            _cleanup_windows_share_at_exit()
            try:
                self._win.destroy()
            except Exception:  # aqg: top-level boundary — page may already be gone
                pass
        return {"ok": True}

    def commit(self, state: Any) -> Dict[str, Any]:
        return self._finish({"outcome": "committed", "result": state})

    def dismiss(self) -> Dict[str, Any]:
        return self._finish({"outcome": "dismissed", "result": None})

    def close(self) -> Dict[str, Any]:
        """The header's ✕ button. A close without Submit is a dismissal — record the terminal
        outcome + tear the window down (frameless: there is NO OS titlebar ✕ to fall back on)."""
        return self.dismiss()

    def minimize(self) -> Dict[str, Any]:
        """The header's — button. Frameless windows have no OS minimize control, so the page calls this.
        On macOS the popup is a frameless floating window with no OS minimize, so HIDE it (orderOut); the
        Dock icon (primary) or the menu-bar icon (backup) recalls it. Other platforms: a real OS minimize.
        Best-effort: a backend without hide/minimize must not crash the popup."""
        if self._win is not None:
            try:
                if _IS_MAC:
                    self._win.hide()          # hide; the Dock icon / menu-bar icon is the way back
                else:
                    self._win.minimize()
            except Exception:  # aqg: top-level boundary — hide/minimize is cosmetic, never fatal
                pass
        return {"ok": True}

    # the designed board header calls `minimize`; keep `hide` as an alias so either name works
    def hide(self) -> Dict[str, Any]:
        return self.minimize()

    def window_state(self) -> Dict[str, Any]:
        """Return only the state needed to initialize the page-drawn maximize control."""
        if self._win is None:
            return {"ok": False, "maximized": False}
        return {"ok": True, "maximized": self._window_is_maximized()}

    def layout(self, request: Any) -> Dict[str, Any]:
        """Serve the `/db/render` page's `api.layout(req)` seam without exposing credentials."""
        if not self._layout_slots.acquire(blocking=False):
            raise RuntimeError("layout unavailable")
        try:
            try:
                return launcher.fetch_board_layout(request)
            except Exception:  # aqg: top-level boundary — expose neither token nor remote response details
                raise RuntimeError("layout unavailable") from None
        finally:
            self._layout_slots.release()

    def _cursor_initial_state(self) -> Dict[str, Any]:
        if (
            self._cursor_guard_required
            and not self._cursor_document_ready.is_set()
        ) or not isinstance(self._cursor_initial, dict):
            raise RuntimeError("board unavailable")
        return self._cursor_initial

    def _cursor_commit(self, state: Any) -> Dict[str, Any]:
        if self._cursor_guard_required and not self._cursor_document_ready.is_set():
            return {"ok": False}
        try:
            checked = launcher._validate_cursor_board_value(
                state, token=self._cursor_forbidden_token or ""
            )
        except (TypeError, ValueError, launcher.BoardFetchError):
            return {"ok": False}
        return self._finish({"outcome": "committed", "result": checked})

    def _cursor_layout(self, request: Any) -> Dict[str, Any]:
        if self._cursor_guard_required and not self._cursor_document_ready.is_set():
            raise RuntimeError("layout unavailable")
        token = self._cursor_forbidden_token or ""
        try:
            checked = launcher._validate_layout_request(request)
            launcher._reject_cursor_token_in_value(checked, token=token)
        except (TypeError, ValueError, launcher.BoardLayoutError, launcher.BoardFetchError):
            raise RuntimeError("layout unavailable") from None
        if not self._layout_slots.acquire(blocking=False):
            raise RuntimeError("layout unavailable")
        try:
            try:
                result = launcher.fetch_board_layout(checked)
                launcher._reject_cursor_token_in_value(result, token=token)
                launcher._validate_layout_structure(result)
                return result
            except Exception:  # aqg: top-level boundary — no remote/token detail crosses the Cursor bridge
                raise RuntimeError("layout unavailable") from None
        finally:
            self._layout_slots.release()

    def _on_window_maximized(self, *_args: Any) -> None:
        """Synchronize button behavior after any native maximize path (button, Win+Up, snap, taskbar)."""
        with self._window_state_lock:
            self._maximized = True
        self._sync_window_chrome_state(True)

    def _on_window_restored(self, *_args: Any) -> None:
        """Synchronize button behavior after any native restore path."""
        with self._window_state_lock:
            self._maximized = False
        self._sync_window_chrome_state(False)

    def _sync_window_chrome_state(self, maximized: bool) -> None:
        if self._win is None:
            return
        try:
            literal = "true" if maximized else "false"
            self._win.evaluate_js(
                "window.deWindowChrome&&window.deWindowChrome.setMaximized(%s)" % literal
            )
        except Exception:  # aqg: top-level boundary — native state remains authoritative
            pass

    def _window_is_maximized(self) -> bool:
        """Prefer current Win32 state, falling back to synchronized pywebview events."""
        native_state = _windows_window_maximized(self._win)
        if native_state is not None:
            with self._window_state_lock:
                self._maximized = native_state
            return native_state
        with self._window_state_lock:
            return self._maximized

    def toggle_maximize(self) -> Dict[str, Any]:
        """Serialize a maximize/restore transition against the current native window state."""
        if self._win is None:
            return {"ok": False}
        with self._window_action_lock:
            maximize = not self._window_is_maximized()
            self._resize_gesture_id = None
            self._resize_edge = None
            self._resize_base = None
            try:
                if maximize:
                    self._win.maximize()
                else:
                    self._win.restore()
            except Exception:  # aqg: top-level boundary — window chrome is cosmetic, never fatal
                return {"ok": False}
            # Event delivery is backend-driven; update immediately too so the next serialized click
            # behaves correctly even before the native resize callback reaches Python.
            with self._window_state_lock:
                self._maximized = maximize
        return {"ok": True, "maximized": maximize}

    def move_window(self, x: Any, y: Any) -> Dict[str, Any]:
        """Move the Windows frameless window to bounded logical screen coordinates."""
        if not _IS_WINDOWS or self._win is None or isinstance(x, bool) or isinstance(y, bool):
            return {"ok": False}
        if not _windows_primary_button_down():
            return {"ok": False, "dragging": False}
        try:
            left, top = float(x), float(y)
        except (TypeError, ValueError):
            return {"ok": False}
        if not math.isfinite(left) or not math.isfinite(top):
            return {"ok": False}
        left = max(-100000, min(100000, round(left)))
        top = max(-100000, min(100000, round(top)))
        # Window.move accepts logical pixels and pywebview's WinForms backend applies the current
        # per-monitor DPI scale. Moving a zoomed form has backend-specific results, so make that
        # combined gesture explicit: restore with the button first, then drag.
        with self._window_action_lock:
            if self._window_is_maximized():
                return {"ok": False, "dragging": False}
            try:
                self._win.move(left, top)
            except Exception:  # aqg: top-level boundary — dragging is cosmetic, never fatal
                return {"ok": False, "dragging": False}
        return {"ok": True}

    def resize_window(
        self,
        gesture_id: Any,
        edge: Any,
        delta_x: Any,
        delta_y: Any,
    ) -> Dict[str, Any]:
        """Apply gesture deltas to one native frame on Windows or macOS.

        Capturing ``win.x/y/width/height`` in Python keeps the anchor and the
        subsequent move/resize calls in pywebview's native coordinate system;
        the page supplies only pointer deltas.
        """
        if self._win is None:
            return {"ok": False, "dragging": False}
        if (
            isinstance(gesture_id, bool)
            or not isinstance(gesture_id, (int, float))
            or not isinstance(edge, str)
            or edge not in {"n", "e", "s", "w", "ne", "se", "sw", "nw"}
            or isinstance(delta_x, bool)
            or isinstance(delta_y, bool)
        ):
            return {"ok": False, "dragging": False}
        if _IS_WINDOWS and not _windows_primary_button_down():
            return {"ok": False, "dragging": False}
        try:
            gesture_number = float(gesture_id)
            dx, dy = float(delta_x), float(delta_y)
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "dragging": False}
        if (
            not math.isfinite(gesture_number)
            or not gesture_number.is_integer()
            or gesture_number < 0
            or gesture_number > 1_000_000_000
            or not all(math.isfinite(value) for value in (dx, dy))
        ):
            return {"ok": False, "dragging": False}
        gesture = int(gesture_number)
        dx = max(-100000, min(100000, dx))
        dy = max(-100000, min(100000, dy))
        with self._window_action_lock:
            if self._window_is_maximized():
                self._resize_gesture_id = None
                self._resize_edge = None
                self._resize_base = None
                return {"ok": False, "dragging": False}
            try:
                if gesture != self._resize_gesture_id:
                    frame = tuple(
                        float(getattr(self._win, name))
                        for name in ("x", "y", "width", "height")
                    )
                    if (
                        not all(math.isfinite(value) for value in frame)
                        or frame[2] <= 0
                        or frame[3] <= 0
                    ):
                        raise ValueError("invalid native frame")
                    self._resize_gesture_id = gesture
                    self._resize_edge = edge
                    self._resize_base = frame
                elif edge != self._resize_edge:
                    raise ValueError("resize edge changed")
                left, top, base_width, base_height = self._resize_base
                target_width, target_height = base_width, base_height
                if "e" in edge:
                    target_width = max(480, base_width + dx)
                if "s" in edge:
                    target_height = max(360, base_height + dy)
                if "w" in edge:
                    target_width = max(480, base_width - dx)
                    left += base_width - target_width
                if "n" in edge:
                    target_height = max(360, base_height - dy)
                    top += base_height - target_height
                left = max(-100000, min(100000, round(left)))
                top = max(-100000, min(100000, round(top)))
                target_width = max(480, min(100000, round(target_width)))
                target_height = max(360, min(100000, round(target_height)))
                if "w" in edge or "n" in edge:
                    self._win.move(left, top)
                self._win.resize(target_width, target_height)
            except Exception:  # aqg: top-level boundary — window resizing is cosmetic, never fatal
                self._resize_gesture_id = None
                self._resize_edge = None
                self._resize_base = None
                return {"ok": False, "dragging": False}
        return {"ok": True, "dragging": True}

    def copy_visual_image(self, rect: Any = None) -> Dict[str, Any]:
        """Screenshot the LEFT visual region → PNG → clipboard AS AN IMAGE. Windows requires the strict
        artifact rect + viewport; macOS preserves its existing optional full-view fallback. Other platforms
        return ``reason='unsupported'`` so the page shows an honest toast."""
        if self._closed:
            return {"ok": False}
        if not _visual_capture_supported():
            return {"ok": False, "reason": "unsupported"}
        if _IS_WINDOWS:
            request = _norm_windows_capture_request(rect)
            if request is None:
                return {"ok": False}
            png = _snapshot_windows_png(
                self._win, request, self._window_action_lock, artifact_only=True)
            if png is None:
                return {"ok": False}
            return {"ok": _copy_windows_png_to_clipboard(self._win, png)}
        png = _snapshot_png_data(_main_webview, rect=_norm_rect(rect))
        if png is None:
            return {"ok": False}
        return {"ok": bool(_copy_png_to_clipboard(png))}

    def copy_visual_image_hires(self, rect: Any = None, factor: Any = 2) -> Dict[str, Any]:
        """Windows-only supersampled visual screenshot, written only to the local image clipboard."""
        if self._closed:
            return {"ok": False}
        if not _visual_capture_supported():
            return {"ok": False, "reason": "unsupported"}
        if not _IS_WINDOWS:
            return {"ok": False, "reason": "unsupported"}
        try:
            scale_factor = int(factor)
        except (TypeError, ValueError):
            return {"ok": False}
        if not 2 <= scale_factor <= _MAX_CAPTURE_SCALE_FACTOR:
            return {"ok": False}
        request = _norm_windows_capture_request(rect)
        if request is None:
            return {"ok": False}
        png = _snapshot_windows_hires_png(
            self._win, request, self._window_action_lock, scale_factor)
        if png is None:
            return {"ok": False}
        return {"ok": _copy_windows_png_to_clipboard(self._win, png)}

    def share_visual_image(self, rect: Any = None, anchor: Any = None) -> Dict[str, Any]:
        """Screenshot the LEFT visual region → PNG → the platform-native share experience. Windows requires
        the strict artifact rect + viewport; macOS also uses ``anchor`` for its existing menu placement."""
        if self._closed:
            return {"ok": False}
        if not _visual_capture_supported():
            return {"ok": False, "reason": "unsupported"}
        if _IS_WINDOWS:
            request = _norm_windows_capture_request(rect)
            if request is None:
                return {"ok": False}
            png = _snapshot_windows_png(
                self._win, request, self._window_action_lock, artifact_only=True)
            if png is None:
                return {"ok": False}
            return {"ok": _present_windows_share(self._win, png)}
        png = _snapshot_png_data(_main_webview, rect=_norm_rect(rect))
        if png is None:
            return {"ok": False}
        return {"ok": _present_share_menu(png, anchor)}

    def snapshot_region(self, rect: Any = None) -> Dict[str, Any]:
        """Point-to-ask: screenshot the user-FRAMED region → PNG → a ``data:image/png;base64,…`` URL the PAGE
        feeds the follow-up chat as an image block. ``rect`` = [x,y,w,h] CSS px (the selection box). Fail-CLOSED:
        a missing/malformed rect returns {ok:False} with NO image (never a full-popup shot that would leak the
        chat column / chrome the user didn't frame) — unlike copy/share, which may fall back to a full shot."""
        if self._closed:
            return {"ok": False}
        if not _visual_capture_supported():   # unsupported platforms get an honest toast, like copy/share
            return {"ok": False, "reason": "unsupported"}
        if _IS_WINDOWS:
            request = _norm_windows_capture_request(rect)
            if request is None:
                return {"ok": False}
            png = _snapshot_windows_region_png(
                self._win, request, self._window_action_lock
            )
        else:
            norm = _norm_rect(rect)
            if norm is None:             # region capture demands a real rect — do NOT degrade to a full-view shot
                return {"ok": False}
            png = _snapshot_png_data(_main_webview, rect=norm, strict_rect=True)
        if png is None:
            return {"ok": False}
        try:
            if _IS_WINDOWS:
                if len(png) > (_MAX_CAPTURE_IMAGE_B64 * 3 // 4):
                    return {"ok": False}
                b64 = base64.b64encode(png).decode("ascii")
                if len(b64) > _MAX_CAPTURE_IMAGE_B64:
                    return {"ok": False}
            else:
                b64 = str(png.base64EncodedStringWithOptions_(0))   # NSData → base64 NSString → str
        except Exception:  # aqg: top-level boundary — encode failure → fail-closed (page keeps the selection)
            return {"ok": False}
        return {"ok": True, "image": "data:image/png;base64," + b64}

    def on_window_closed(self) -> None:
        """Called after the window closes when ``--on-close-dismiss`` is set: record a terminal
        ``dismissed`` UNLESS a commit/dismiss already fired. Idempotent via the one-shot ``_done`` guard, so
        an in-page Submit/Dismiss (which already wrote its result) is never overwritten — only a bare
        OS-chrome close lands a fresh ``dismissed`` (design §4.2, so a detached poll doesn't hang to TTL)."""
        self._finish({"outcome": "dismissed", "result": None})

    # -- GE follow-up chat --------------------------------------------------------------------------
    def _callback_allowed(self, generation: int) -> bool:
        with self._lifecycle_lock:
            return not self._closed and generation == self._callback_generation

    def _emit_chat(
        self,
        generation: int,
        fn: str,
        *args: Any,
        require_true: bool = False,
    ) -> bool:
        """Validate one native→page callback and suppress it after close/destroy."""
        if not self._callback_allowed(generation) or self._win is None:
            return False
        checked_args = None
        if fn == "bootstrap" and len(args) in {1, 2}:
            checked = _safe_bootstrap(args[0], backend=self._chat_route or "server")
            if checked is not None:
                if len(args) == 1:
                    checked_args = (checked,)
                elif args[1] is True:
                    checked_args = (checked, True)
        elif fn == "state" and len(args) == 3:
            display_turn_id, state, payload = args
            if display_turn_id is not None and not _valid_display_turn_id(display_turn_id):
                return False
            checked_payload = _safe_state_payload(state, payload)
            checked_args = (
                (display_turn_id, state, checked_payload)
                if checked_payload is not None
                else None
            )
        elif fn in {"delta", "done"} and len(args) == 2:
            display_turn_id, text = args
            if _valid_display_turn_id(display_turn_id) and isinstance(text, str) and len(text) <= _MAX_CHAT_OUTPUT_CHARS:
                checked_args = (display_turn_id, text)
        elif fn == "error" and len(args) == 2:
            display_turn_id, code = args
            if (display_turn_id is None or _valid_display_turn_id(display_turn_id)) and code in _CHAT_ERROR_CODES:
                checked_args = (display_turn_id, code)
        elif fn == "willClose" and len(args) == 1:
            display_turn_id = args[0]
            if display_turn_id is None or _valid_display_turn_id(display_turn_id):
                checked_args = (display_turn_id,)
        if checked_args is None:
            return False
        call = _js_call(fn, *checked_args)
        if not call:
            return False
        # History recovery is the synchronous acknowledgement exception: keep its renderer
        # result and the active generation in one lifecycle epoch. Ordinary callbacks admit
        # publication atomically, then release the lifecycle lock before renderer code.
        if require_true:
            with self._lifecycle_lock:
                if (
                    self._closed
                    or generation != self._callback_generation
                    or self._win is None
                ):
                    return False
                window = self._win
                with self._callback_publication_condition:
                    self._active_callback_publications += 1
                try:
                    result = window.evaluate_js(call)
                except Exception:  # aqg: top-level boundary — the page may disappear during teardown
                    return False
                finally:
                    with self._callback_publication_condition:
                        self._active_callback_publications -= 1
                        self._callback_publication_condition.notify_all()
                return result is True

        with self._lifecycle_lock:
            if self._closed or generation != self._callback_generation or self._win is None:
                return False
            window = self._win
            with self._callback_publication_condition:
                self._active_callback_publications += 1
        try:
            result = window.evaluate_js(call)
        except Exception:  # aqg: top-level boundary — the page may disappear during teardown
            return False
        finally:
                with self._callback_publication_condition:
                    self._active_callback_publications -= 1
                    self._callback_publication_condition.notify_all()
        return True

    def _start_chat_worker(self, *, target, args=(), name: str) -> bool:
        try:
            threading.Thread(target=target, args=args, daemon=True, name=name).start()
            return True
        except (RuntimeError, OSError):
            return False

    def _release_server_turn_ownership_locked(self) -> None:
        """Release retry/admission state while the caller holds ``_busy_lock``."""
        self._recovering = False
        self._active_client_turn_id = None
        self._active_display_turn_id = None
        self._last_server_request = None

    def chat_ready(self) -> Dict[str, Any]:
        """Acknowledge immediately and deliver bootstrap on a worker, never on the bridge thread."""
        if self._chat_route not in {"server", "legacy"}:
            return {"ok": False, "state": "unavailable"}
        with self._lifecycle_lock:
            if self._closed:
                return {"ok": False, "state": "unavailable"}
            generation = self._callback_generation
            with self._bootstrap_lock:
                if not self._bootstrap_started:
                    self._bootstrap_started = True
                    if not self._start_chat_worker(
                        target=self._bootstrap_chat,
                        args=(generation,),
                        name="de-ge-chat-bootstrap",
                    ):
                        self._chat_error_code = "chat_unavailable"
                        self._bootstrap_started = False
                        return {"ok": False, "state": "unavailable"}
        return {"ok": True, "state": "capability_checking"}

    def _bootstrap_chat(self, generation: int) -> None:
        if not self._callback_allowed(generation):
            return
        if self._chat_route == "legacy":
            payload = {
                "ok": True,
                "backend": "legacy",
                "state": "ready",
                "conversation": None,
                "privacy_disclosure": None,
                "history": [],
            }
            self._emit_chat(generation, "bootstrap", payload)
            self._emit_chat(generation, "state", None, "ready", {})
            return
        if self._chat is None:
            code = _safe_error_code(self._chat_error_code)
            self._emit_chat(
                generation,
                "bootstrap",
                {"ok": False, "backend": "server", "state": "unavailable", "error_code": code},
            )
            self._emit_chat(generation, "state", None, "unavailable", {"error_code": code})
            return
        try:
            payload = _safe_bootstrap(self._chat.bootstrap(), backend="server")
            if payload is None or payload.get("ok") is not True:
                raise ValueError("bad bootstrap")
            self._privacy_version = payload["privacy_disclosure"]["version"]
            self._server_bootstrap = payload
        except BaseException as exc:  # aqg: top-level boundary — only a stable code reaches the page
            code = _safe_error_code(getattr(exc, "code", None))
            self._emit_chat(
                generation,
                "bootstrap",
                {"ok": False, "backend": "server", "state": "unavailable", "error_code": code},
            )
            self._emit_chat(generation, "state", None, "unavailable", {"error_code": code})
            return
        self._emit_chat(generation, "bootstrap", payload)
        self._emit_chat(generation, "state", None, "ready", {})

    def ask(
        self,
        turn_id: Any,
        text: Any,
        images: Any = None,
        client_turn_id: Any = None,
        privacy_version: Any = None,
    ) -> Dict[str, Any]:
        """Admit one legacy or server turn and return before its worker performs transport I/O."""
        if self._chat_route == "legacy":
            return self._ask_legacy(turn_id, text, images)
        if self._chat_route != "server" or self._chat is None:
            return {"ok": False, "error_code": _safe_error_code(self._chat_error_code)}
        error = _server_admission_error(
            turn_id,
            text,
            images,
            client_turn_id,
            privacy_version,
            self._privacy_version,
        )
        if error is not None:
            return {"ok": False, "error_code": error}
        # Re-check lifecycle only after the potentially expensive image validation, then
        # admit under the same lock ordering used by shutdown (lifecycle -> busy).  Closing
        # during validation therefore wins and no worker starts against a closed transport.
        with self._lifecycle_lock:
            if self._closed:
                return {"ok": False, "error_code": "chat_unavailable"}
            generation = self._callback_generation
            with self._busy_lock:
                if self._terminal_flush_in_progress:
                    return {"ok": False, "error_code": "turn_in_flight"}
                if self._busy:
                    if client_turn_id == self._active_client_turn_id:
                        return {"ok": True, "status": "coalesced"}
                    return {"ok": False, "error_code": "turn_in_flight"}
                if self._recovering:
                    if client_turn_id == self._active_client_turn_id:
                        return {"ok": True, "status": "recovering"}
                    return {"ok": False, "error_code": "turn_in_flight"}
                self._busy = True
                self._active_client_turn_id = client_turn_id
                self._active_display_turn_id = turn_id
                self._last_server_request = (turn_id, text, list(images), client_turn_id, privacy_version)
            if not self._start_chat_worker(
                target=self._run_server_turn_thread,
                args=(turn_id, text, images, client_turn_id, privacy_version, generation),
                name="de-ge-chat-turn",
            ):
                with self._busy_lock:
                    self._busy = False
                    self._release_server_turn_ownership_locked()
                return {"ok": False, "error_code": "chat_unavailable"}
        return {"ok": True}

    def _ask_legacy(self, turn_id: Any, text: Any, images: Any) -> Dict[str, Any]:
        # Preserve the legacy three-argument admission and ChatSession.run_turn call exactly.
        if self._chat is None or self._closed or not (text or images):
            return {"ok": False}
        with self._busy_lock:
            if self._busy:
                return {"ok": False}
            self._busy = True
        if not self._start_chat_worker(
            target=self._run_legacy_turn_thread,
            args=(turn_id, text, images),
            name="de-ge-legacy-chat-turn",
        ):
            with self._busy_lock:
                self._busy = False
            return {"ok": False}
        return {"ok": True}

    def _error_string(self) -> str:
        """The chat's localized error string, hardened so the ERROR path itself can never raise (a
        missing / non-mapping ``strings`` must not escape the worker and leave the page without a
        terminal event)."""
        try:
            return self._chat.strings.get("error", i18n.chat_defaults(_SHELL_LOCALE)["error"])
        except Exception:  # aqg: top-level boundary — the error path must always deliver a string
            return i18n.chat_defaults(_SHELL_LOCALE)["error"]

    def _run_legacy_turn_thread(self, turn_id: Any, text: Any, images: Any) -> None:
        win = self._win

        def js(fn: str, *a: Any) -> None:
            if win is None:
                return
            call = _js_call(fn, *a)
            if not call:              # non-whitelisted fn → nothing to run
                return
            try:
                win.evaluate_js(call)
            except Exception:  # aqg: top-level boundary — the page may be gone mid-stream
                pass

        want_close = {"v": False}

        def on_action(a: str) -> None:
            if a == "close":
                want_close["v"] = True

        try:
            with self._turn_lock:                       # one turn at a time (the UI also disables Send)
                if self._closed:                        # window closed while this turn was queued → skip,
                    js("error", turn_id, self._error_string())  # but always deliver a terminal event so
                    return                              # the composer never wedges waiting on a reply
                # LAZY: only when the user actually asks do we lift the visual out of the already-rendered
                # page — nothing heavy is prepared/fed before a follow-up. INSIDE the lock so a rapid
                # double-send can't run two concurrent extracts.
                if self._chat.session_id is None and not self._chat.context.get("_visual_loaded"):
                    self._load_visual_from_page(win)
                try:
                    asyncio.run(self._chat.run_turn(
                        text, images,
                        lambda s: js("delta", turn_id, s),
                        lambda s: js("done", turn_id, s),
                        lambda m: js("error", turn_id, m),
                        on_action))
                except BaseException:  # aqg: top-level boundary — ANY worker crash (incl. CancelledError)
                    # must re-enable the UI; keep the detail OUT of the bubble (it can name a path/vendor).
                    js("error", turn_id, self._error_string())
                if want_close["v"]:
                    # set INSIDE the lock so a second turn already queued on _turn_lock can't pass the
                    # `if self._closed` guard before the close takes effect (audit ef71deb2 claude f1).
                    self._closed = True
            if want_close["v"]:                          # user asked to exit → host closes the window
                js("willClose", turn_id)
                self._close_window()
        finally:
            with self._busy_lock:
                self._busy = False                       # admission gate always released, even on crash

    def _run_server_turn_thread(
        self,
        turn_id: int,
        text: str,
        images: list,
        client_turn_id: str,
        privacy_version: str,
        generation: int,
    ) -> None:
        want_close = {"value": False}
        terminal_pending = {"value": False}
        terminal_state = {"value": None}
        page_terminal_pending = {"value": False}
        page_terminal_callback = {"value": None}
        terminal_faulted = {"value": False}
        terminal_delta_pending = {"value": False}
        terminal_callbacks: list[tuple[str, tuple[Any, ...]]] = []
        # HttpChatSession invokes callbacks synchronously on this worker via
        # ``_notify``.  The lifecycle -> busy lock order still makes every terminal
        # flag/ownership transition atomic with ask/close admission.

        def queue_page_terminal(callback: str, callback_args: tuple[Any, ...]) -> None:
            with self._lifecycle_lock:
                if self._closed or generation != self._callback_generation:
                    return
                with self._busy_lock:
                    if page_terminal_pending["value"]:
                        return
                    if terminal_state["value"] is not None:
                        expected = (
                            "done" if terminal_state["value"] == "completed" else "error"
                        )
                        if callback != expected:
                            terminal_callbacks[:] = [
                                ("error", (turn_id, "bad_response"))
                            ]
                            terminal_faulted["value"] = True
                            page_terminal_pending["value"] = True
                            page_terminal_callback["value"] = "error"
                            return
                    if not terminal_pending["value"]:
                        self._release_server_turn_ownership_locked()
                        terminal_pending["value"] = True
                    page_terminal_pending["value"] = True
                    page_terminal_callback["value"] = callback
                    terminal_callbacks.append((callback, callback_args))

        def on_state(state: Any, payload: Any) -> None:
            checked = _safe_state_payload(state, payload)
            with self._lifecycle_lock:
                if self._closed or generation != self._callback_generation:
                    return
                with self._busy_lock:
                    if terminal_pending["value"]:
                        if checked is None:
                            terminal_callbacks[:] = [
                                ("error", (turn_id, "bad_response"))
                            ]
                            terminal_faulted["value"] = True
                            page_terminal_pending["value"] = True
                            page_terminal_callback["value"] = "error"
                            return
                        if (
                            terminal_state["value"] is None
                            and page_terminal_pending["value"]
                            and state
                            in {"completed", "failed", "cancelled", "unavailable"}
                        ):
                            expected = "done" if state == "completed" else "error"
                            terminal_state["value"] = state
                            if page_terminal_callback["value"] != expected:
                                terminal_callbacks[:] = [
                                    ("error", (turn_id, "bad_response"))
                                ]
                                terminal_faulted["value"] = True
                                page_terminal_callback["value"] = "error"
                            else:
                                terminal_callbacks.insert(
                                    0, ("state", (turn_id, state, checked))
                                )
                        return
                    if checked is None:
                        self._release_server_turn_ownership_locked()
                        terminal_pending["value"] = True
                        page_terminal_pending["value"] = True
                        terminal_faulted["value"] = True
                        terminal_callbacks.append(
                            ("error", (turn_id, "bad_response"))
                        )
                        return
                    if state == "recovering":
                        self._recovering = True
                    elif state in {"completed", "failed", "cancelled", "unavailable"}:
                        self._release_server_turn_ownership_locked()
                        terminal_pending["value"] = True
                        terminal_state["value"] = state
                        terminal_callbacks.append(
                            ("state", (turn_id, state, checked))
                        )
                        return
            self._emit_chat(generation, "state", turn_id, state, checked)

        def on_delta(value: Any) -> None:
            with self._lifecycle_lock:
                if self._closed or generation != self._callback_generation:
                    return
                with self._busy_lock:
                    if terminal_faulted["value"] or page_terminal_pending["value"]:
                        return
                    if terminal_pending["value"]:
                        if (
                            terminal_state["value"] != "completed"
                            or terminal_delta_pending["value"]
                        ):
                            return
                        terminal_delta_pending["value"] = True
                        terminal_callbacks.append(("delta", (turn_id, value)))
                        return
            self._emit_chat(generation, "delta", turn_id, value)

        def on_done(value: Any) -> None:
            queue_page_terminal("done", (turn_id, value))

        def on_error(code: Any) -> None:
            queue_page_terminal(
                "error",
                (turn_id, _safe_error_code(code, default="bad_response")),
            )

        def on_action(action: Any) -> bool:
            if action == "close":
                want_close["value"] = True
                return True
            if (
                isinstance(action, dict)
                and set(action) == {"type", "history"}
                and action.get("type") == "history_snapshot"
                and isinstance(action.get("history"), list)
            ):
                with self._lifecycle_lock:
                    if self._closed or generation != self._callback_generation:
                        return False
                    cached = self._server_bootstrap
                    if not isinstance(cached, dict):
                        return False
                    snapshot = dict(cached)
                    snapshot["history"] = action["history"]
                    checked = _safe_bootstrap(snapshot, backend="server")
                    if checked is None:
                        return False
                    if not self._emit_chat(
                        generation,
                        "bootstrap",
                        checked,
                        True,
                        require_true=True,
                    ):
                        return False
                    self._server_bootstrap = checked
                return True
            return False

        try:
            if not self._callback_allowed(generation):
                return
            asyncio.run(
                self._chat.run_turn(
                    text,
                    images,
                    on_delta,
                    on_done,
                    on_error,
                    on_action,
                    client_turn_id=client_turn_id,
                    privacy_version=privacy_version,
                    on_state=on_state,
                )
            )
        except BaseException as exc:  # aqg: top-level boundary — raw HTTP/provider detail stays in Python
            queue_page_terminal(
                "error",
                (
                    turn_id,
                    _safe_error_code(getattr(exc, "code", None)),
                ),
            )
        finally:
            # Release native ownership, then publish without holding a Python lock
            # across renderer code.  The explicit flush gate prevents another turn
            # from acquiring ownership between this turn's state, delta, and terminal.
            flush_admission = False
            with self._lifecycle_lock:
                with self._busy_lock:
                    self._busy = False
                    if not terminal_pending["value"] and (
                        self._closed
                        or generation != self._callback_generation
                        or not self._recovering
                    ):
                        self._release_server_turn_ownership_locked()
                    if terminal_callbacks or want_close["value"]:
                        self._terminal_flush_in_progress = True
                        flush_admission = True
            try:
                for callback, callback_args in terminal_callbacks:
                    self._emit_chat(generation, callback, *callback_args)
                if want_close["value"] and self._callback_allowed(generation):
                    self._emit_chat(generation, "willClose", turn_id)
                    self._close_window()
            finally:
                if flush_admission:
                    with self._lifecycle_lock:
                        with self._busy_lock:
                            self._terminal_flush_in_progress = False

    def retry_chat(self, client_turn_id: Any) -> Dict[str, Any]:
        if self._chat_route != "server":
            return {"ok": False, "status": "client_unsupported"}
        with self._lifecycle_lock:
            if self._closed:
                return {"ok": False, "status": "chat_unavailable"}
            generation = self._callback_generation
            with self._busy_lock:
                request = self._last_server_request
                if (
                    self._busy
                    or self._terminal_flush_in_progress
                    or not self._recovering
                    or request is None
                    or client_turn_id != self._active_client_turn_id
                ):
                    return {"ok": False, "status": "not_found"}
                self._busy = True
            if not self._start_chat_worker(
                target=self._run_server_turn_thread,
                args=(*request, generation),
                name="de-ge-chat-retry",
            ):
                with self._busy_lock:
                    self._busy = False
                return {"ok": False, "status": "chat_unavailable"}
        return {"ok": True, "status": "recovering"}

    def _shutdown_chat(self) -> None:
        with self._chat_close_lock:
            if self._chat_shutdown:
                return
            self._chat_shutdown = True
            close_deadline = time.monotonic() + _CHAT_CLOSE_BUDGET_S
            with self._lifecycle_lock:
                self._closed = True
                self._callback_generation += 1
                self._server_bootstrap = None
            with self._callback_publication_condition:
                while self._active_callback_publications:
                    remaining = close_deadline - time.monotonic()
                    if remaining <= 0:
                        # Window teardown is the final boundary when renderer publication
                        # does not return inside the existing total close budget.
                        break
                    self._callback_publication_condition.wait(timeout=remaining)
            if self._chat_route != "server" or self._chat is None:
                return
            with self._busy_lock:
                self._release_server_turn_ownership_locked()
            finished = threading.Event()

            def teardown() -> None:
                try:
                    self._chat.close()
                except BaseException:  # aqg: top-level boundary — teardown is idempotent/best-effort
                    pass
                finally:
                    finished.set()

            # HttpChatSession.close owns best-effort active-turn cancellation and transport teardown.
            # The bridge thread waits only for the existing total close budget; the detached popup's
            # process-level exit policy remains the final boundary if teardown stalls.
            if self._start_chat_worker(
                target=teardown,
                name="de-ge-chat-close",
            ):
                finished.wait(timeout=max(0.0, close_deadline - time.monotonic()))

    def chat_closing(self) -> Dict[str, Any]:
        self._shutdown_chat()
        return {"ok": True}

    def _close_window(self) -> None:
        """Close the window WITHOUT writing a result (a chat 'close' is not a board Submit/Dismiss). The
        detached ``--on-close-dismiss`` hook then records the terminal ``dismissed`` when start() returns."""
        self._shutdown_chat()
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:  # aqg: top-level boundary — page may already be gone
                pass

    def _load_visual_from_page(self, win: Any) -> None:
        """Lazily lift the rendered visual out of the ALREADY-RENDERED page on the FIRST ask: an inlined
        ``.artifact svg`` → its markup as ``visual_text`` (lossless labels), else a ``.artifact img`` → its
        data: URL as the follow-up image. The query + slice run INSIDE the JS so a huge artifact never
        crosses the bridge before Python truncates it. Best-effort — any failure runs the chat text-only."""
        ctx = self._chat.context
        try:
            svg = win.evaluate_js(
                "(function(){var d=document.querySelector('.artifact svg');"
                "return d?d.outerHTML.slice(0," + str(_MAX_VISUAL_CHARS) + "):''})()")
            if isinstance(svg, str) and svg.strip():
                ctx["visual_text"] = svg[:_MAX_VISUAL_CHARS]
            else:
                cap = chat_backend._MAX_IMG_BLOCKS if chat_backend else 5
                # BYTE-cap each data URL (not just the COUNT) — mirror the SVG's payload budget so a single
                # multi-MB in-page data URL can't cross the bridge whole and stress IPC/memory (audit
                # ef71deb2 f1). The filter runs IN the JS (bound at the source) and is re-enforced in Python.
                b64cap = (chat_backend._MAX_IMG_B64 if chat_backend else 12 * 1024 * 1024) + 64
                urls = win.evaluate_js(
                    "(function(){return Array.from(document.querySelectorAll('.artifact img'))"
                    ".map(function(a){return a.src||''})"
                    ".filter(function(s){return s.indexOf('data:')===0&&s.length<=" + str(b64cap) + "})"
                    ".slice(0," + str(cap) + ")})()")
                if isinstance(urls, list):
                    ctx["images"] = [u for u in urls
                                     if isinstance(u, str) and u.startswith("data:") and len(u) <= b64cap][:cap]
        except Exception:  # aqg: top-level boundary — degrade to text-only if the extract fails
            pass
        ctx["_visual_loaded"] = True


class _CursorWindowApi:
    """Minimal window/clipboard surface shared by Cursor popup profiles."""

    def __init__(self, core: PopupApi) -> None:
        self._core = core

    def close(self) -> Dict[str, Any]:
        return self._core.close()

    def minimize(self) -> Dict[str, Any]:
        return self._core.minimize()

    def hide(self) -> Dict[str, Any]:
        return self._core.hide()

    def window_state(self) -> Dict[str, Any]:
        return self._core.window_state()

    def toggle_maximize(self) -> Dict[str, Any]:
        return self._core.toggle_maximize()

    def move_window(self, x: Any, y: Any) -> Dict[str, Any]:
        return self._core.move_window(x, y)

    def resize_window(
        self,
        gesture_id: Any,
        edge: Any,
        delta_x: Any,
        delta_y: Any,
    ) -> Dict[str, Any]:
        return self._core.resize_window(
            gesture_id, edge, delta_x, delta_y
        )

    def copy_visual_image(self, rect: Any = None) -> Dict[str, Any]:
        return self._core.copy_visual_image(rect)

    def copy_visual_image_hires(self, rect: Any = None, factor: Any = 2) -> Dict[str, Any]:
        return self._core.copy_visual_image_hires(rect, factor)


_CURSOR_GE_ARG_MISSING = object()


class _CursorGeApi(_CursorWindowApi):
    """GE page surface: window/capture plus the frozen follow-up chat bridge."""

    def snapshot_region(self, rect: Any = None) -> Dict[str, Any]:
        return self._core.snapshot_region(rect)

    def chat_ready(self) -> Dict[str, Any]:
        return self._core.chat_ready()

    def ask(
        self,
        turn_id: Any,
        text: Any,
        images: Any = _CURSOR_GE_ARG_MISSING,
        client_turn_id: Any = _CURSOR_GE_ARG_MISSING,
        privacy_version: Any = _CURSOR_GE_ARG_MISSING,
        *extra: Any,
    ) -> Dict[str, Any]:
        if extra or images is _CURSOR_GE_ARG_MISSING or (
            (client_turn_id is _CURSOR_GE_ARG_MISSING)
            != (privacy_version is _CURSOR_GE_ARG_MISSING)
        ):
            return {"ok": False, "error_code": "invalid_request"}
        if client_turn_id is _CURSOR_GE_ARG_MISSING:
            return self._core.ask(turn_id, text, images)
        return self._core.ask(turn_id, text, images, client_turn_id, privacy_version)

    def retry_chat(self, client_turn_id: Any) -> Dict[str, Any]:
        return self._core.retry_chat(client_turn_id)


class _CursorDbApi(_CursorWindowApi):
    def initial_state(self) -> Dict[str, Any]:
        return self._core._cursor_initial_state()

    def commit(self, state: Any) -> Dict[str, Any]:
        return self._core._cursor_commit(state)

    def dismiss(self) -> Dict[str, Any]:
        return self._core.dismiss()

    def layout(self, request: Any) -> Dict[str, Any]:
        return self._core._cursor_layout(request)


def _api_for_profile(core: PopupApi, profile: str) -> Any:
    if profile == "legacy":
        return core
    if profile == "cursor-ge":
        return _CursorGeApi(core)
    if profile == "cursor-db":
        return _CursorDbApi(core)
    raise ValueError("unknown popup API profile")


_CURSOR_WEBVIEW2_EVENTS = (
    "NavigationStarting",
    "NavigationCompleted",
    "FrameNavigationStarting",
    "NewWindowRequested",
    "DownloadStarting",
    "PermissionRequested",
    "ClientCertificateRequested",
    "WebResourceRequested",
    "ProcessFailed",
    "ScriptDialogOpening",
    "WindowCloseRequested",
)
_CURSOR_DATA_IMAGE_PREFIXES = (
    "data:image/svg+xml;base64,",
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/gif;base64,",
    "data:image/webp;base64,",
)
_CURSOR_BOOTSTRAP_NAME = "cursor-bootstrap.html"
_CURSOR_BOOTSTRAP_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<title>Decision Engine</title></head><body></body></html>"
)
_CURSOR_READY_TIMEOUT_S = 5.0


def _validated_cursor_loopback_url(value: str) -> tuple[str, str, int]:
    if not isinstance(value, str):
        raise ValueError("canonical Cursor document URI required")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("canonical Cursor document URI required") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("canonical Cursor document URI required")
    return parsed.scheme, parsed.hostname, port


def install_cursor_webview2_policy(
    core: Any,
    canonical_document: str,
    close_popup,
    *,
    all_context: Any,
    deny_state: Any = "deny",
    response_factory=None,
    document_ready=None,
) -> list[Any]:
    """Install the fail-closed Cursor WebView2 matrix around one owned document."""
    _validated_cursor_loopback_url(canonical_document)
    settings = core.Settings
    settings.AreDevToolsEnabled = False
    settings.AreDefaultContextMenusEnabled = False
    settings.AreBrowserAcceleratorKeysEnabled = False
    settings.IsStatusBarEnabled = False
    core.AddWebResourceRequestedFilter("*", all_context)
    if response_factory is None:
        response_factory = lambda: object()

    retained = []
    state = {
        "top_level_seen": False,
        "navigation_id": None,
    }

    def guarded(handler):
        def invoke(_sender, args):
            try:
                handler(args)
            except BaseException:  # aqg: top-level boundary — any WebView2 handler fault closes Cursor
                close_popup()

        retained.append(invoke)
        return invoke

    def navigation_starting(args):
        uri = getattr(args, "Uri", None)
        navigation_id = getattr(args, "NavigationId", None)
        if (
            not state["top_level_seen"]
            and uri == canonical_document
            and navigation_id is not None
        ):
            state["top_level_seen"] = True
            state["navigation_id"] = navigation_id
            return
        args.Cancel = True

    def navigation_completed(args):
        if (
            not state["top_level_seen"]
            or getattr(args, "NavigationId", None) != state["navigation_id"]
            or not getattr(args, "IsSuccess", False)
            or getattr(core, "Source", None) != canonical_document
        ):
            close_popup()
            return
        if document_ready is not None:
            document_ready()

    def cancel(args):
        args.Cancel = True

    def handle_new_window(args):
        args.Handled = True

    def deny_permission(args):
        args.State = deny_state
        args.Handled = True

    def cancel_certificate(args):
        args.Handled = True

    def filter_resource(args):
        request = getattr(args, "Request", None)
        uri = getattr(request, "Uri", None)
        if uri == canonical_document or (
            isinstance(uri, str) and uri.startswith(_CURSOR_DATA_IMAGE_PREFIXES)
        ):
            return
        args.Response = response_factory()

    def close(_args):
        close_popup()

    def suppress_dialog(args):
        accept = getattr(args, "Accept", None)
        if callable(accept):
            accept()
        close_popup()

    handlers = {
        "NavigationStarting": navigation_starting,
        "NavigationCompleted": navigation_completed,
        "FrameNavigationStarting": cancel,
        "NewWindowRequested": handle_new_window,
        "DownloadStarting": cancel,
        "PermissionRequested": deny_permission,
        "ClientCertificateRequested": cancel_certificate,
        "WebResourceRequested": filter_resource,
        "ProcessFailed": close,
        "ScriptDialogOpening": suppress_dialog,
        "WindowCloseRequested": close,
    }
    for event_name in _CURSOR_WEBVIEW2_EVENTS:
        event = getattr(core, event_name)
        event += guarded(handlers[event_name])
    return retained


def configure_cursor_webview2_window(
    win: Any,
    html_path: str,
    bootstrap_path: str,
    api: Any,
    *,
    all_context: Any = None,
    deny_state: Any = None,
    response_factory=None,
    ready_path: Optional[str] = None,
) -> Optional[str]:
    """Harden an inert owned bootstrap before navigating to trusted content."""
    outcome = {"target": None}

    def configure():
        try:
            core = win.native.webview.CoreWebView2
            if core is None:
                raise ValueError("Cursor WebView2 is unavailable")
            active_context = all_context
            active_deny_state = deny_state
            if active_context is None or active_deny_state is None:
                from Microsoft.Web.WebView2.Core import (
                    CoreWebView2PermissionState,
                    CoreWebView2WebResourceContext,
                )

                active_context = CoreWebView2WebResourceContext.All
                active_deny_state = CoreWebView2PermissionState.Deny
            active_response_factory = response_factory
            if active_response_factory is None:
                active_response_factory = (
                    lambda: core.Environment.CreateWebResourceResponse(
                        None, 403, "Blocked", ""
                    )
                )
            resolver = getattr(win, "_resolve_url", None)
            if not callable(resolver):
                raise ValueError("Cursor loopback resolver unavailable")
            bootstrap_url = str(resolver(bootstrap_path))
            canonical = str(resolver(html_path))
            bootstrap_origin = _validated_cursor_loopback_url(bootstrap_url)
            target_origin = _validated_cursor_loopback_url(canonical)
            if (
                bootstrap_origin != target_origin
                or bootstrap_url == canonical
                or str(core.Source) != bootstrap_url
            ):
                raise ValueError("loaded Cursor bootstrap identity mismatch")

            def document_ready():
                timer = getattr(api._core, "_cursor_ready_timer", None)
                if timer is not None:
                    timer.cancel()
                api._core._cursor_document_ready.set()
                _mark_popup_ready(win, ready_path)

            retained = install_cursor_webview2_policy(
                core,
                canonical,
                api.close,
                all_context=active_context,
                deny_state=active_deny_state,
                response_factory=active_response_factory,
                document_ready=document_ready,
            )
            api._cursor_policy_handlers = retained
            outcome["target"] = canonical
        except BaseException:  # aqg: top-level boundary — partial hardening never opens Cursor
            try:
                api.close()
            except BaseException:  # aqg: top-level boundary — close failure cannot enable navigation
                pass

    try:
        native = win.native
        if getattr(native, "InvokeRequired", False):
            from System import Action

            native.Invoke(Action(configure))
        else:
            configure()
        return outcome["target"]
    except BaseException:  # aqg: top-level boundary — failed UI marshalling closes Cursor
        try:
            api.close()
        except BaseException:  # aqg: top-level boundary — close failure cannot enable navigation
            pass
        return None


def _arm_cursor_ready_watchdog(
    core_api: PopupApi,
    close_popup,
    *,
    timeout_s: float = _CURSOR_READY_TIMEOUT_S,
) -> None:
    """Close a Cursor popup unless the exact hardened target becomes ready."""

    def expire():
        if core_api._cursor_document_ready.is_set():
            return
        try:
            close_popup()
        except BaseException:  # aqg: top-level boundary
            pass

    timer = threading.Timer(timeout_s, expire)
    timer.daemon = True
    core_api._cursor_ready_timer = timer
    timer.start()


def _start_cursor_target_navigation(
    win: Any,
    html_path: str,
    expected_target: str,
    close_popup,
) -> None:
    """Navigate off the WinForms callback only after containment is installed."""

    def navigate():
        try:
            win.load_url(html_path)
            if str(getattr(win, "real_url", "")) != expected_target:
                raise ValueError("Cursor target mapping changed")
        except BaseException:  # aqg: top-level boundary
            try:
                close_popup()
            except BaseException:  # aqg: top-level boundary
                pass

    try:
        threading.Thread(
            target=navigate,
            name="de-cursor-popup-navigation",
            daemon=True,
        ).start()
    except BaseException:  # aqg: top-level boundary
        try:
            close_popup()
        except BaseException:  # aqg: top-level boundary
            pass


def _mac_visible_frame():
    """macOS main screen's VISIBLE frame (excludes the menu bar / Dock) as a top-left (x, y, w, h) so the
    window fills the usable area. NSScreen uses a bottom-left origin; pywebview x/y is top-left, so flip y.
    Returns None if AppKit/NSScreen is unavailable (→ the caller keeps a default size)."""
    try:
        from AppKit import NSScreen
        scr = NSScreen.mainScreen()
        full, vf = scr.frame(), scr.visibleFrame()
        x = int(vf.origin.x)
        y = int(full.size.height - (vf.origin.y + vf.size.height))   # menu-bar height, from the top
        return x, y, int(vf.size.width), int(vf.size.height)
    except Exception:  # aqg: top-level boundary — fall back to a default size if NSScreen is unavailable
        return None


def _apply_mac_chrome(title: str):
    """macOS only: a menu-bar NSStatusItem (a SECONDARY show/hide affordance). Returns the item so the
    caller can wire its click action once the window exists.

    We deliberately NO LONGER force Accessory (no-Dock) policy: pywebview sets
    NSApplicationActivationPolicyRegular, and fighting it back to Accessory raced in the field — users
    ended up as a Regular app anyway (the Python rocket in the Dock) but with the window hidden and NO
    working way back (the Dock icon had no reopen handler, the menu-bar glyph was easy to miss). So we
    now embrace a normal Dock app: the Dock icon is the PRIMARY way back (see _install_dock_reopen) and
    this menu-bar icon is the backup. Best-effort (ported from the reference A-repo shell)."""
    from AppKit import NSImage, NSStatusBar, NSVariableStatusItemLength
    from Foundation import NSSize

    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    img = NSImage.alloc().initWithContentsOfFile_(str(_ICON_FILE)) if _ICON_FILE.exists() else None
    if img is not None:
        # Height is what a menu bar rations; width follows the image's own aspect, so the framed
        # mark is not squashed into a square. Mirrors the Swift panel, which reads the same file.
        native = img.size()
        ratio = (native.width / native.height) if native.height > 0 else 1.0
        img.setSize_(NSSize(18 * ratio, 18))
        img.setTemplate_(True)               # auto-invert for dark/light menu bars
        item.button().setImage_(img)
    else:
        item.button().setTitle_("◧")          # glyph fallback if the icon file is missing
    item.button().setToolTip_(i18n.shell(_SHELL_LOCALE)["tray_toggle"] % title)
    _STATUS_KEEP.append(item)
    return item


def _content_nswindow():
    """The popup's real NSWindow (pywebview's WindowHost), skipping the status item's own window."""
    from AppKit import NSApp
    for w in NSApp.windows():
        if type(w).__name__ == "NSStatusBarWindow":
            continue
        if w.canBecomeKeyWindow():
            return w
    return None


# ---- GE visual capture: Screenshot / Share / region-ask. macOS keeps the existing WKWebView/AppKit path;
# Windows uses WebView2 compositor capture (not PrintWindow, which can return blank Chromium frames),
# WinForms image clipboard, and HWND-bound WinRT sharing. Other platforms degrade honestly to unsupported. ----
def _visual_capture_supported() -> bool:
    """True when the platform has the native WebView stack used by its capture implementation."""
    if _IS_WINDOWS:
        try:
            import webview  # noqa: F401
            return True
        except Exception:  # aqg: top-level boundary — no pywebview means no WinForms/WebView2 capture
            return False
    if not _IS_MAC:
        return False
    try:
        import WebKit  # noqa: F401
        return True
    except Exception:  # aqg: top-level boundary — no WebKit → no native capture
        return False


def _norm_rect(rect):
    """A page-supplied [x,y,w,h] → a validated (x,y,w,h) float tuple, or None (→ full-view snapshot).
    Guards a malformed / non-positive rect from the JS bridge so a bad value degrades to a full capture."""
    try:
        x, y, w, h = float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _norm_windows_capture_request(rect):
    """Validate ``[x,y,w,h,viewport_w,viewport_h]`` from the page for a strict Windows crop.

    Windows captures the WebView2 compositor's whole viewport, then crops the PNG. Viewport CSS size is
    part of the request so the scale comes from the captured pixels rather than an assumed monitor DPI.
    Every capture is strict: screenshot/share must contain only ``.artifact`` and region-ask must never
    fall back to the full WebView (which includes chrome/chat).
    """
    try:
        values = tuple(float(rect[i]) for i in range(6))
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    x, y, width, height, viewport_width, viewport_height = values
    if width < 8.0 or height < 8.0 or viewport_width <= 0.0 or viewport_height <= 0.0:
        return None
    if x < 0.0 or y < 0.0 or x + width > viewport_width or y + height > viewport_height:
        return None
    return ((x, y, width, height), (viewport_width, viewport_height))


def _windows_crop_box(rect, viewport, png_size):
    """Map a strict CSS rect to an inward-rounded PNG box that cannot include neighboring pixels."""
    try:
        x, y, width, height = (float(value) for value in rect)
        viewport_width, viewport_height = (float(value) for value in viewport)
        png_width, png_height = (int(value) for value in png_size)
    except (TypeError, ValueError):
        return None
    values = (x, y, width, height, viewport_width, viewport_height)
    if not all(math.isfinite(value) for value in values):
        return None
    if (x < 0.0 or y < 0.0 or width <= 0.0 or height <= 0.0 or
            viewport_width <= 0.0 or viewport_height <= 0.0 or png_width <= 0 or png_height <= 0 or
            x + width > viewport_width or y + height > viewport_height):
        return None
    scale_x, scale_y = png_width / viewport_width, png_height / viewport_height
    if abs(scale_x - scale_y) > max(2.0 / viewport_width, 2.0 / viewport_height):
        return None
    left = max(0, math.ceil(x * scale_x))
    top = max(0, math.ceil(y * scale_y))
    right = min(png_width, math.floor((x + width) * scale_x))
    bottom = min(png_height, math.floor((y + height) * scale_y))
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _png_dimensions(png):
    """Return trustworthy PNG IHDR dimensions, rejecting malformed or unreasonable payloads."""
    if not isinstance(png, (bytes, bytearray)) or len(png) < 24:
        return None
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        return None
    try:
        width, height = struct.unpack(">II", png[16:24])
    except struct.error:
        return None
    if (not (1 <= width <= 32768 and 1 <= height <= 32768) or
            width * height > _MAX_WINDOWS_CAPTURE_PIXELS):
        return None
    return (width, height)


_WINDOWS_CAPTURE_LOCK = threading.Lock()
_WINDOWS_CAPTURE_STATE_LOCK = threading.Lock()
_WINDOWS_CAPTURE_POISONED = False
_WINDOWS_EVAL_STATE_LOCK = threading.Lock()
_WINDOWS_EVAL_POISONED = False


def _evaluate_windows_js_bounded(win, script, timeout=2.0):
    """Bound pywebview's unbounded WinForms evaluate_js wait to one poisoned generation."""
    global _WINDOWS_EVAL_POISONED
    with _WINDOWS_EVAL_STATE_LOCK:
        if _WINDOWS_EVAL_POISONED:
            return None
    done = threading.Event()
    state = {"result": None}

    def _evaluate():
        global _WINDOWS_EVAL_POISONED
        try:
            state["result"] = win.evaluate_js(script)
        except BaseException:  # aqg: top-level boundary — renderer/thread failures remain fail-closed
            state["result"] = None
        finally:
            with _WINDOWS_EVAL_STATE_LOCK:
                _WINDOWS_EVAL_POISONED = False
                done.set()

    worker = threading.Thread(target=_evaluate, name="de-ge-webview-eval", daemon=True)
    worker.start()
    if done.wait(timeout=timeout):
        return state["result"]
    with _WINDOWS_EVAL_STATE_LOCK:
        if done.is_set():
            return state["result"]
        _WINDOWS_EVAL_POISONED = True
    return None


def _windows_ui_invoke(win, fn):
    """Run ``fn`` synchronously on the WinForms UI thread and report whether it completed."""
    if not _IS_WINDOWS or win is None or not callable(fn):
        return False
    done = threading.Event()
    state = {"ok": False}

    def _run() -> None:
        try:
            state["ok"] = fn() is not False
        except BaseException:  # aqg: top-level boundary - UI-thread work must fail closed
            state["ok"] = False
        finally:
            done.set()

    try:
        native = win.native
        invoke_required = getattr(native, "InvokeRequired", None)
        if invoke_required is None:
            return False
        if bool(invoke_required):
            from System import Action

            native.Invoke(Action(_run))
        else:
            _run()
    except BaseException:  # aqg: top-level boundary - disposed WinForms controls reject dispatch
        return False
    return bool(done.is_set() and state["ok"])


def _grow_windows_webview(win, width, height):
    """Temporarily enlarge the WebView2 surface without resizing or moving the popup window."""
    try:
        width, height = int(math.ceil(float(width))), int(math.ceil(float(height)))
    except (TypeError, ValueError, OverflowError):
        return False
    if width <= 0 or height <= 0 or width * height > _MAX_WINDOWS_CAPTURE_PIXELS:
        return False

    def _grow():
        native = win.native
        control = getattr(native, "webview", None)
        if control is None:
            return False
        from System.Drawing import Point, Size
        from System.Windows.Forms import DockStyle

        if getattr(win, "_de_capture_webview_restore", None) is None:
            win._de_capture_webview_restore = {
                "control": control,
                "dock": getattr(control, "Dock", None),
                "location": getattr(control, "Location", None),
                "size": getattr(control, "Size", None),
            }
        setattr(control, "Dock", getattr(DockStyle, "None"))
        control.Location = Point(0, 0)
        control.Size = Size(width, height)
        return True

    return _windows_ui_invoke(win, _grow)


def _restore_windows_webview(win):
    """Undo ``_grow_windows_webview`` and return the control to its prior Dock/Size state."""
    if not _IS_WINDOWS or win is None:
        return False
    state = getattr(win, "_de_capture_webview_restore", None)
    if not isinstance(state, dict):
        return False

    def _restore():
        control = state.get("control")
        if control is None:
            return False
        if state.get("location") is not None:
            control.Location = state["location"]
        if state.get("size") is not None:
            control.Size = state["size"]
        if state.get("dock") is not None:
            control.Dock = state["dock"]
        try:
            delattr(win, "_de_capture_webview_restore")
        except AttributeError:
            pass
        return True

    return _windows_ui_invoke(win, _restore)


def _windows_page_viewport(win):
    """Read the current page viewport and DPR from the WebView document."""
    try:
        current = _evaluate_windows_js_bounded(
            win,
            "(function(){return [window.innerWidth,window.innerHeight,"
            "window.devicePixelRatio||1]})()",
        )
        values = tuple(float(current[index]) for index in range(3))
    except Exception:  # aqg: top-level boundary - a closing renderer makes capture fail closed
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    width, height, ratio = values
    if width <= 0.0 or height <= 0.0 or ratio <= 0.0:
        return None
    return (width, height, ratio)


def _await_windows_viewport(win, min_width, min_height, timeout=2.0):
    """Wait until the page has observed the enlarged WebView2 viewport."""
    try:
        min_width, min_height = float(min_width), float(min_height)
    except (TypeError, ValueError):
        return None
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        viewport = _windows_page_viewport(win)
        if (
            viewport is not None
            and viewport[0] + 0.5 >= min_width
            and viewport[1] + 0.5 >= min_height
        ):
            return viewport
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.025)


def _begin_page_capture_scale(win):
    """Ask the template to suppress resize-driven camera fitting before the native surface grows."""
    return _evaluate_windows_js_bounded(
        win,
        "(function(){try{var f=window.__deCaptureBegin;"
        "if(typeof f!=='function')return false;return f()!==false;}catch(e){return false;}})()",
    ) is True


def _drive_page_capture_scale(win, factor, timeout=1.5):
    """Apply template-side supersampling scale and wait for its readiness flag."""
    try:
        factor = int(factor)
    except (TypeError, ValueError):
        return False
    started = _evaluate_windows_js_bounded(
        win,
        "(function(k){try{if(typeof window.__deCaptureScale!=='function')return false;"
        "window.__deCaptureScale(k);return true;}catch(e){return false;}})(%d)" % factor,
    ) is True
    if not started:
        return False
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        ready = _evaluate_windows_js_bounded(
            win,
            "(function(){try{var r=window.__deCaptureScaleReady;"
            "if(typeof r==='function')return r()===true;return r===true;}"
            "catch(e){return false;}})()",
            timeout=0.5,
        ) is True
        if ready:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.025)


def _restore_page_capture_scale(win):
    """Restore the template camera while the privacy mask is still covering the viewport."""
    return _evaluate_windows_js_bounded(
        win,
        "(function(){try{var f=window.__deCaptureScaleRestore;"
        "if(typeof f!=='function')return false;return f()!==false;}catch(e){return false;}})()",
    ) is True


def _release_page_capture_scale(win):
    """Release the template resize gate after the native WebView has been restored."""
    return _evaluate_windows_js_bounded(
        win,
        "(function(){try{var f=window.__deCaptureScaleRelease;"
        "if(typeof f!=='function')return false;return f()!==false;}catch(e){return false;}})()",
    ) is True


def _capture_windows_webview_png(win, timeout=4.0):
    """Capture WebView2's compositor output to PNG bytes from a pywebview js_api worker thread."""
    global _WINDOWS_CAPTURE_POISONED
    with _WINDOWS_CAPTURE_STATE_LOCK:
        if _WINDOWS_CAPTURE_POISONED:
            return None
    if not _IS_WINDOWS or win is None or not _WINDOWS_CAPTURE_LOCK.acquire(timeout=timeout):
        return None
    release_guard = threading.Lock()
    released = {"value": False}

    def _release_capture_lock() -> None:
        global _WINDOWS_CAPTURE_POISONED
        with release_guard:
            if released["value"]:
                return
            released["value"] = True
            with _WINDOWS_CAPTURE_STATE_LOCK:
                _WINDOWS_CAPTURE_POISONED = False
            _WINDOWS_CAPTURE_LOCK.release()

    try:
        native = win.native
        if not bool(native.InvokeRequired):
            _release_capture_lock()
            return None  # waiting from the WinForms UI thread would deadlock
        done = threading.Event()
        state = {"png": None, "stream": None, "delegates": []}
        try:
            from System import Action
            from System.IO import MemoryStream
            from System.Threading.Tasks import Task
            from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat

            def _start_capture() -> None:
                stream = None
                try:
                    stream = MemoryStream()
                    state["stream"] = stream
                    task = native.webview.CoreWebView2.CapturePreviewAsync(
                        CoreWebView2CapturePreviewImageFormat.Png, stream)

                    def _complete(completed) -> None:
                        try:
                            if not completed.IsCanceled and not completed.IsFaulted:
                                state["png"] = bytes(stream.ToArray())
                        except BaseException:  # aqg: top-level boundary — nothing escapes a .NET task callback
                            state["png"] = None
                        finally:
                            try:
                                stream.Dispose()
                            except BaseException:  # aqg: top-level boundary — Dispose cannot escape .NET callback
                                pass
                            state["stream"] = None
                            try:
                                done.set()
                            except BaseException:  # aqg: top-level boundary — signaling cannot escape .NET callback
                                pass
                            _release_capture_lock()

                    callback = Action[Task](_complete)
                    state["delegates"].append(callback)
                    task.ContinueWith(callback)
                except BaseException:  # aqg: top-level boundary — nothing escapes the WinForms Action
                    if stream is not None:
                        try:
                            stream.Dispose()
                        except BaseException:  # aqg: top-level boundary — failed-start cleanup stays contained
                            pass
                        state["stream"] = None
                    done.set()
                    _release_capture_lock()

            starter = Action(_start_capture)
            state["delegates"].append(starter)
            native.BeginInvoke(starter)
        except Exception:  # aqg: top-level boundary — missing backend/.NET types or dispatch failure
            _release_capture_lock()
            return None
        if not done.wait(timeout=timeout):
            with _WINDOWS_CAPTURE_STATE_LOCK:
                if done.is_set():
                    png = state["png"]
                    return png if _png_dimensions(png) is not None and len(png) <= 64 * 1024 * 1024 else None
                _WINDOWS_CAPTURE_POISONED = True
            return None
        png = state["png"]
        return png if _png_dimensions(png) is not None and len(png) <= 64 * 1024 * 1024 else None
    except BaseException:  # aqg: top-level boundary — capture API must fail closed without wedging the lock
        _release_capture_lock()
        return None


def _crop_windows_png(png, box):
    """Crop PNG bytes with System.Drawing and require at least one non-transparent output pixel."""
    try:
        from System import Array, Byte
        from System.Drawing import Bitmap, Graphics, GraphicsUnit, Rectangle
        from System.Drawing.Imaging import ImageFormat, ImageLockMode, PixelFormat
        from System.IO import MemoryStream
        from System.Runtime.InteropServices import Marshal

        left, top, right, bottom = box
        width, height = right - left, bottom - top
        source_stream = MemoryStream(png)
        source = Bitmap(source_stream)
        target = Bitmap(width, height, PixelFormat.Format32bppArgb)
        graphics = Graphics.FromImage(target)
        try:
            graphics.DrawImage(source, Rectangle(0, 0, width, height),
                               Rectangle(left, top, width, height), GraphicsUnit.Pixel)
            bits = target.LockBits(Rectangle(0, 0, width, height), ImageLockMode.ReadOnly,
                                   PixelFormat.Format32bppArgb)
            try:
                stride = abs(int(bits.Stride))
                raw = Array.CreateInstance(Byte, stride * height)
                Marshal.Copy(bits.Scan0, raw, 0, len(raw))
                # Format32bppArgb rows are naturally 4-byte aligned; slicing the copied byte buffer
                # avoids millions of Python/.NET indexer calls on a transparent failure frame.
                visible = any(bytes(raw)[3::4])
            finally:
                target.UnlockBits(bits)
            if not visible:
                return None
            output = MemoryStream()
            try:
                target.Save(output, ImageFormat.Png)
                result = bytes(output.ToArray())
            finally:
                output.Dispose()
            return result if _png_dimensions(result) is not None else None
        finally:
            graphics.Dispose()
            target.Dispose()
            source.Dispose()
            source_stream.Dispose()
    except Exception:  # aqg: top-level boundary — invalid image/crop must fail closed
        return None


def _windows_page_capture_matches(win, request, artifact_only):
    """Bind a requested crop to the live artifact and viewport before/after native capture."""
    try:
        current = _evaluate_windows_js_bounded(win,
            "(function(){var a=document.querySelector('.artifact');"
            "if(!a)return null;var r=a.getBoundingClientRect();"
            "return [r.x,r.y,r.width,r.height,window.innerWidth,window.innerHeight]})()")
        live_rect = tuple(float(current[index]) for index in range(4))
        live_viewport = tuple(float(current[index]) for index in range(4, 6))
        rect, viewport = request
        rect = tuple(float(value) for value in rect)
        viewport = tuple(float(value) for value in viewport)
    except Exception:  # aqg: top-level boundary — a closing WebView makes the capture fail closed
        return False
    values = live_rect + live_viewport + rect + viewport
    if not all(math.isfinite(value) for value in values):
        return False
    if any(abs(live_viewport[index] - viewport[index]) > 0.5 for index in range(2)):
        return False
    if artifact_only:
        return all(abs(live_rect[index] - rect[index]) <= 0.5 for index in range(4))
    live_x, live_y, live_width, live_height = live_rect
    x, y, width, height = rect
    tolerance = 0.5
    return (x >= live_x - tolerance and y >= live_y - tolerance and
            x + width <= live_x + live_width + tolerance and
            y + height <= live_y + live_height + tolerance)


def _windows_region_artifact_request(win, crop_request):
    """Resolve artifact and actually-visible boxes separately from the requested crop."""
    try:
        current = _evaluate_windows_js_bounded(win,
            "(function(){try{var a=document.querySelector('.artifact');if(!a)return null;"
            "var r=a.getBoundingClientRect(),left=Math.max(0,r.left),top=Math.max(0,r.top),"
            "right=Math.min(window.innerWidth,r.right),bottom=Math.min(window.innerHeight,r.bottom);"
            "for(var p=a.parentElement;p;p=p.parentElement){var cs=getComputedStyle(p),pr=p.getBoundingClientRect();"
            "if(cs.overflowX!=='visible'){left=Math.max(left,pr.left);right=Math.min(right,pr.right);}"
            "if(cs.overflowY!=='visible'){top=Math.max(top,pr.top);bottom=Math.min(bottom,pr.bottom);}}"
            "return [r.x,r.y,r.width,r.height,left,top,Math.max(0,right-left),Math.max(0,bottom-top),"
            "window.innerWidth,window.innerHeight];}catch(e){return null;}})()")
        artifact_rect = tuple(float(current[index]) for index in range(4))
        visible_rect = tuple(float(current[index]) for index in range(4, 8))
        live_viewport = tuple(float(current[index]) for index in range(8, 10))
        crop_rect, viewport = crop_request
        crop_rect = tuple(float(value) for value in crop_rect)
        viewport = tuple(float(value) for value in viewport)
    except Exception:  # aqg: top-level boundary — a closing WebView makes the region fail closed
        return None
    values = artifact_rect + visible_rect + live_viewport + crop_rect + viewport
    if not all(math.isfinite(value) for value in values):
        return None
    if any(abs(live_viewport[index] - viewport[index]) > 0.5 for index in range(2)):
        return None
    _artifact_x, _artifact_y, artifact_width, artifact_height = artifact_rect
    visible_x, visible_y, visible_width, visible_height = visible_rect
    crop_x, crop_y, crop_width, crop_height = crop_rect
    viewport_width, viewport_height = viewport
    tolerance = 0.5
    if not (artifact_width > 0.0 and artifact_height > 0.0 and
            crop_width > 0.0 and crop_height > 0.0 and
            crop_x >= -tolerance and crop_y >= -tolerance and
            crop_x + crop_width <= viewport_width + tolerance and
            crop_y + crop_height <= viewport_height + tolerance and
            visible_width > 0.0 and visible_height > 0.0 and
            crop_x >= visible_x - tolerance and crop_y >= visible_y - tolerance and
            crop_x + crop_width <= visible_x + visible_width + tolerance and
            crop_y + crop_height <= visible_y + visible_height + tolerance):
        return None
    return (artifact_rect, visible_rect, viewport)


def _windows_region_capture_matches(win, artifact_request, crop_request):
    """Revalidate live region geometry, top-layer state, and mask survival around CapturePreview."""
    artifact_rect, visible_rect, viewport = artifact_request
    crop_rect, crop_viewport = crop_request
    expected = [*artifact_rect, *visible_rect, *viewport]
    script = """(function(v){try{
var a=document.querySelector('.artifact'),key='__deWindowsRegionCapturePrivacy',s=window[key];
if(!a)return null;var r=a.getBoundingClientRect(),left=Math.max(0,r.left),top=Math.max(0,r.top),
right=Math.min(window.innerWidth,r.right),bottom=Math.min(window.innerHeight,r.bottom);
for(var p=a.parentElement;p;p=p.parentElement){var ps=getComputedStyle(p),pr=p.getBoundingClientRect();
if(ps.overflowX!=='visible'){left=Math.max(left,pr.left);right=Math.min(right,pr.right);}
if(ps.overflowY!=='visible'){top=Math.max(top,pr.top);bottom=Math.min(bottom,pr.bottom);}}
var root=s&&s.root,active=!!(
s&&root&&root.isConnected&&root===document.getElementById('de-windows-region-capture-privacy'));
if(active){var cs=getComputedStyle(root);active=cs.position==='fixed'&&cs.display!=='none'&&
cs.visibility!=='hidden'&&parseFloat(cs.opacity||'1')>0&&Array.isArray(s.values)&&s.values.length===10;}
if(active)for(var j=0;j<10;j++)if(!Number.isFinite(s.values[j])||Math.abs(s.values[j]-v[j])>0.01){active=false;break;}
if(document.fullscreenElement||document.querySelector('dialog[open]'))active=false;
var popovers=document.querySelectorAll('[popover]');
for(var i=0;i<popovers.length;i++){try{if(popovers[i].matches(':popover-open'))active=false;}catch(e){active=false;}}
return [r.x,r.y,r.width,r.height,left,top,Math.max(0,right-left),Math.max(0,bottom-top),
window.innerWidth,window.innerHeight,active];
}catch(e){return null;}})(%s)""" % json.dumps(expected, separators=(",", ":"))
    try:
        current = _evaluate_windows_js_bounded(win, script)
        live_rect = tuple(float(current[index]) for index in range(4))
        live_visible = tuple(float(current[index]) for index in range(4, 8))
        live_viewport = tuple(float(current[index]) for index in range(8, 10))
        active = current[10] is True
        artifact_rect = tuple(float(value) for value in artifact_rect)
        visible_rect = tuple(float(value) for value in visible_rect)
        viewport = tuple(float(value) for value in viewport)
        crop_rect = tuple(float(value) for value in crop_rect)
        crop_viewport = tuple(float(value) for value in crop_viewport)
    except Exception:  # aqg: top-level boundary — any uncertain region state fails closed
        return False
    values = (live_rect + live_visible + live_viewport + artifact_rect + visible_rect +
              viewport + crop_rect + crop_viewport)
    if not active or not all(math.isfinite(value) for value in values):
        return False
    if any(abs(live_viewport[index] - viewport[index]) > 0.5 or
           abs(crop_viewport[index] - viewport[index]) > 0.5 for index in range(2)):
        return False
    if any(abs(live_rect[index] - artifact_rect[index]) > 0.01 for index in range(4)):
        return False
    if any(abs(live_visible[index] - visible_rect[index]) > 0.01 for index in range(4)):
        return False
    _live_x, _live_y, live_width, live_height = live_rect
    visible_x, visible_y, visible_width, visible_height = live_visible
    crop_x, crop_y, crop_width, crop_height = crop_rect
    viewport_width, viewport_height = viewport
    tolerance = 0.01
    return (live_width > 0.0 and live_height > 0.0 and
            visible_width > 0.0 and visible_height > 0.0 and
            crop_width > 0.0 and crop_height > 0.0 and
            crop_x >= -tolerance and crop_y >= -tolerance and
            crop_x + crop_width <= viewport_width + tolerance and
            crop_y + crop_height <= viewport_height + tolerance and
            crop_x >= visible_x - tolerance and crop_y >= visible_y - tolerance and
            crop_x + crop_width <= visible_x + visible_width + tolerance and
            crop_y + crop_height <= visible_y + visible_height + tolerance)


def _install_windows_region_capture_privacy_mask(win, artifact_request):
    """Cover everything outside the visible artifact without moving or restyling it."""
    artifact_rect, visible_rect, viewport = artifact_request
    values = [*artifact_rect, *visible_rect, *viewport]
    script = """(function(v){
var key='__deWindowsRegionCapturePrivacy',old=window[key];if(old)return false;
var a=document.querySelector('.artifact');if(!a||!a.parentNode)return false;
if(document.fullscreenElement||document.querySelector('dialog[open]'))return false;
var popovers=document.querySelectorAll('[popover]');
for(var i=0;i<popovers.length;i++){try{if(popovers[i].matches(':popover-open'))return false;}catch(e){return false;}}
var root=document.createElement('div'),state={root:root,values:v.slice(0)};window[key]=state;
function neutralize(){try{root.style.setProperty('display','none','important');}catch(e){}}
function destroy(){neutralize();var ok=true;try{if(root.parentNode)root.parentNode.removeChild(root);}catch(first){
try{root.remove();}catch(second){ok=false;}}if(root.parentNode)ok=false;
if(ok&&window[key]===state)delete window[key];return ok;}
state.timer=setTimeout(destroy,12000);
function cover(left,top,width,height){if(width<=0||height<=0)return;
var c=document.createElement('div');c.style.cssText='position:absolute;background:#fff;pointer-events:none';
c.style.left=left+'px';c.style.top=top+'px';c.style.width=width+'px';c.style.height=height+'px';root.appendChild(c);}
  try{var x=v[4],y=v[5],w=v[6],h=v[7],vw=v[8],vh=v[9],right=x+w,bottom=y+h;
root.id='de-windows-region-capture-privacy';
root.style.cssText='position:fixed;inset:0;background:transparent;z-index:2147483647;overflow:hidden;pointer-events:none';
cover(0,0,vw,y);cover(0,y,x,h);cover(right,y,vw-right,h);cover(0,bottom,vw,vh-bottom);
document.documentElement.appendChild(root);return true;
}catch(e){neutralize();var ok=true;try{if(root.parentNode)root.parentNode.removeChild(root);}catch(first){
try{root.remove();}catch(second){ok=false;}}if(root.parentNode)ok=false;if(ok){try{clearTimeout(state.timer);}catch(timer){}
delete window[key];}return false;}
})(%s)""" % json.dumps(values, separators=(",", ":"))
    return _evaluate_windows_js_bounded(win, script) is True


def _remove_windows_region_capture_privacy_mask(win):
    script = """(function(){
var key='__deWindowsRegionCapturePrivacy',s=window[key];if(!s)return false;
var root=s.root,ok=true;
try{if(root)root.style.setProperty('display','none','important');}catch(hidden){}
try{if(root&&root.parentNode)root.parentNode.removeChild(root);}catch(first){
try{if(root)root.remove();}catch(second){ok=false;}}
if(root&&root.parentNode)ok=false;if(ok){try{if(s.timer)clearTimeout(s.timer);}catch(timer){}
delete window[key];}return ok;
})()"""
    return _evaluate_windows_js_bounded(win, script) is True


def _cleanup_windows_region_capture_privacy_mask(win):
    """Best-effort region-mask removal; ordinary evaluator failures never cross the bridge."""
    for _attempt in range(2):
        try:
            if _remove_windows_region_capture_privacy_mask(win):
                return True
        except Exception:  # aqg: top-level boundary — a closing WebView must still fail closed
            continue
    return False


def _install_windows_capture_privacy_mask(win, request):
    """Move the live artifact into an opaque fixed overlay so geometry races cannot reveal adjacent UI."""
    rect, viewport = request
    values = [*rect, *viewport]
    script = """(function(v){
var key='__deWindowsCapturePrivacy',old=window[key];if(old)return false;
var a=document.querySelector('.artifact');if(!a||!a.parentNode)return false;
var p=document.createComment('de-capture-placeholder'),root=document.createElement('div');
var original=a.getAttribute('style');a.parentNode.insertBefore(p,a);
root.id='de-windows-capture-privacy';
root.style.cssText='position:fixed;inset:0;background:#fff;z-index:2147483647;overflow:hidden;pointer-events:none';
a.style.position='absolute';a.style.left=v[0]+'px';a.style.top=v[1]+'px';
a.style.boxSizing='border-box';a.style.width=v[2]+'px';a.style.height=v[3]+'px';a.style.margin='0';a.style.transform='none';
root.appendChild(a);document.documentElement.appendChild(root);
window[key]={artifact:a,placeholder:p,root:root,style:original};return true;
})(%s)""" % json.dumps(values, separators=(",", ":"))
    return _evaluate_windows_js_bounded(win, script) is True


def _remove_windows_capture_privacy_mask(win):
    script = """(function(){
var key='__deWindowsCapturePrivacy',s=window[key];if(!s)return false;
try{if(s.placeholder&&s.placeholder.parentNode){s.placeholder.parentNode.insertBefore(s.artifact,s.placeholder);s.placeholder.remove();}
if(s.style===null)s.artifact.removeAttribute('style');else s.artifact.setAttribute('style',s.style);
if(s.root&&s.root.parentNode)s.root.remove();return true;}finally{delete window[key];}
})()"""
    return _evaluate_windows_js_bounded(win, script) is True


def _snapshot_windows_hires_png(win, request, window_action_lock, factor):
    """Capture a supersampled artifact by parking the enlarged viewport just below the visible window."""
    if not _IS_WINDOWS or request is None or window_action_lock is None:
        return None
    try:
        factor = int(factor)
    except (TypeError, ValueError):
        return None
    if not 2 <= factor <= _MAX_CAPTURE_SCALE_FACTOR:
        return None
    rect, _viewport = request
    try:
        _x, _y, width, height = (float(value) for value in rect)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (width, height)) or width <= 0 or height <= 0:
        return None
    with window_action_lock:
        if not _windows_page_capture_matches(win, request, True):
            return None
        measured = _windows_page_viewport(win)
        if measured is None:
            return None
        view_width, view_height, ratio = measured
        need_width = max(view_width, width * factor)
        need_height = view_height + height * factor
        control_width = int(math.ceil(need_width * ratio))
        control_height = int(math.ceil(need_height * ratio))
        if control_width <= 0 or control_height <= 0:
            return None
        if control_width * control_height > _MAX_WINDOWS_CAPTURE_PIXELS:
            return None
        if not _begin_page_capture_scale(win):
            return None
        result = None
        cleanup_ok = False
        try:
            scaled_rect = (0.0, view_height, width * factor, height * factor)
            if not _install_windows_capture_privacy_mask(
                win, (scaled_rect, (need_width, need_height))
            ):
                return None
            if not _grow_windows_webview(win, control_width, control_height):
                return None
            grown = _await_windows_viewport(win, need_width, need_height)
            if grown is None:
                return None
            parked = (scaled_rect, (grown[0], grown[1]))
            if not _drive_page_capture_scale(win, factor):
                return None
            if not _windows_page_capture_matches(win, parked, True):
                return None
            full_png = _capture_windows_webview_png(win)
            dimensions = _png_dimensions(full_png)
            if dimensions is None:
                return None
            box = _windows_crop_box(parked[0], parked[1], dimensions)
            result = _crop_windows_png(full_png, box) if box is not None else None
        finally:
            camera_ok = _restore_page_capture_scale(win)
            mask_ok = _remove_windows_capture_privacy_mask(win)
            webview_ok = _restore_windows_webview(win)
            release_ok = _release_page_capture_scale(win)
            cleanup_ok = camera_ok and mask_ok and webview_ok and release_ok
        return result if cleanup_ok else None


def _snapshot_windows_png(win, request, window_action_lock, artifact_only=False):
    """Capture and strictly crop while rejecting any layout/window-size race."""
    if not _IS_WINDOWS or request is None or window_action_lock is None:
        return None
    rect, viewport = request
    with window_action_lock:
        if not _windows_page_capture_matches(win, request, artifact_only):
            return None
        if not _install_windows_capture_privacy_mask(win, request):
            return None
        try:
            if not _windows_page_capture_matches(win, request, artifact_only):
                return None
            full_png = _capture_windows_webview_png(win)
            dimensions = _png_dimensions(full_png)
            if dimensions is None or not _windows_page_capture_matches(win, request, artifact_only):
                return None
            box = _windows_crop_box(rect, viewport, dimensions)
            return _crop_windows_png(full_png, box) if box is not None else None
        finally:
            _remove_windows_capture_privacy_mask(win)


def _snapshot_windows_region_png(win, crop_request, window_action_lock):
    """Capture one exact user-selected region without resizing the surrounding artifact."""
    if not _IS_WINDOWS or crop_request is None or window_action_lock is None:
        return None
    crop_rect, viewport = crop_request
    try:
        with window_action_lock:
            result = None
            cleanup_ok = False
            try:
                artifact_request = _windows_region_artifact_request(win, crop_request)
                if artifact_request is None:
                    return None
                if not _install_windows_region_capture_privacy_mask(win, artifact_request):
                    return None
                if _windows_region_capture_matches(win, artifact_request, crop_request):
                    full_png = _capture_windows_webview_png(win)
                    dimensions = _png_dimensions(full_png)
                    if (dimensions is not None and
                            _windows_region_capture_matches(
                                win, artifact_request, crop_request
                            )):
                        box = _windows_crop_box(crop_rect, viewport, dimensions)
                        result = _crop_windows_png(full_png, box) if box is not None else None
            except Exception:  # aqg: top-level boundary — region capture never escapes the bridge
                return None
            finally:
                cleanup_ok = _cleanup_windows_region_capture_privacy_mask(win)
            return result if cleanup_ok else None
    except Exception:  # aqg: top-level boundary — lock/window failures fail closed without unlocked cleanup
        return None


def _copy_windows_png_to_clipboard(win, png, timeout=3.0):
    """Put PNG on the Windows clipboard as an image from the popup's WinForms STA thread."""
    if not _IS_WINDOWS or win is None or _png_dimensions(png) is None:
        return False
    done = threading.Event()
    state = {"ok": False, "delegate": None, "expired": False, "lock": threading.Lock()}
    try:
        native = win.native
        if not bool(native.InvokeRequired):
            return False
        from System import Action
        from System.Drawing import Bitmap
        from System.IO import MemoryStream
        from System.Windows.Forms import Clipboard

        def _copy() -> None:
            try:
                with state["lock"]:
                    if state["expired"]:
                        return
                stream = MemoryStream(png)
                source = Bitmap(stream)
                image = Bitmap(source)
                try:
                    Clipboard.SetImage(image)
                    state["ok"] = bool(Clipboard.ContainsImage())
                finally:
                    image.Dispose()
                    source.Dispose()
                    stream.Dispose()
            except BaseException:  # aqg: top-level boundary — nothing escapes the WinForms clipboard Action
                state["ok"] = False
            finally:
                done.set()

        state["delegate"] = Action(_copy)
        native.BeginInvoke(state["delegate"])
    except Exception:  # aqg: top-level boundary — missing WinForms/.NET or dispatch failure
        return False
    if done.wait(timeout=timeout):
        return state["ok"]
    with state["lock"]:
        if done.is_set():
            return state["ok"]
        state["expired"] = True
    return False


_WINDOWS_SHARE_LOCK = threading.Lock()
_WINDOWS_SHARE_ACTIVE = None
_WINDOWS_SHARE_RETAINED = []
_WINDOWS_SHARE_PENDING_LOCK = threading.Lock()
_WINDOWS_SHARE_PENDING_UNLINKS = {}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(value):
    """Build a ctypes GUID without importing Windows-only modules at module import time."""
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _unlink_windows_share_path(path, delays=(0.0, 0.05, 0.2)):
    """Remove a private share PNG with bounded retries for a target app's short-lived file handle."""
    target = Path(path)
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            target.unlink(missing_ok=True)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if index == len(delays) - 1:
                return False
    return False


def _schedule_windows_share_unlink_retry(path, delays=(1.0, 5.0, 30.0, 120.0)):
    """Retain a failed share path and retry in the background; never silently forget it."""
    target = str(path)
    state = {"index": 0, "timer": None, "delays": tuple(delays)}
    with _WINDOWS_SHARE_PENDING_LOCK:
        if target in _WINDOWS_SHARE_PENDING_UNLINKS:
            return
        _WINDOWS_SHARE_PENDING_UNLINKS[target] = state

    def _attempt():
        if _unlink_windows_share_path(target, delays=(0.0,)):
            with _WINDOWS_SHARE_PENDING_LOCK:
                _WINDOWS_SHARE_PENDING_UNLINKS.pop(target, None)
            return
        with _WINDOWS_SHARE_PENDING_LOCK:
            current = _WINDOWS_SHARE_PENDING_UNLINKS.get(target)
            if current is not state or state["index"] >= len(state["delays"]):
                if current is state and state["index"] >= len(state["delays"]):
                    state["exhausted"] = True
                    print("native_shell: Windows share temporary cleanup exhausted", file=sys.stderr)
                return
            delay = state["delays"][state["index"]]
            state["index"] += 1
            timer = threading.Timer(delay, _attempt)
            timer.daemon = True
            state["timer"] = timer
        timer.start()

    _attempt()


def _retain_windows_share_objects(state, *objects):
    """Atomically retain callback/RCW objects unless cleanup already won the race."""
    with state["lock"]:
        if state.get("cleaned"):
            return False
        state["keep"].extend(objects)
        return True


def _retain_windows_share_subscription(state, source, event_name, bridge, handler, token):
    """Record a native event or immediately undo it if cleanup already started."""
    with state["lock"]:
        if not state.get("cleaned"):
            state["subscriptions"].append((source, event_name, token))
            state["keep"].extend((source, bridge, handler, token))
            return True
    try:
        _unsubscribe_windows_event(source, event_name, token)
    except BaseException:  # aqg: top-level boundary — late registration cleanup cannot escape WinRT
        pass
    return False


def _detach_windows_share_subscriptions(state, subscriptions):
    """Unsubscribe retained WinRT events on the popup STA, including package completion events."""
    if not subscriptions:
        return

    def _detach():
        for source, event_name, token in subscriptions:
            try:
                _unsubscribe_windows_event(source, event_name, token)
            except BaseException:  # aqg: top-level boundary — closed WinRT RCWs are best-effort cleanup
                pass

    native = state.get("native")
    try:
        if native is not None and (bool(getattr(native, "IsDisposed", False)) or
                                   bool(getattr(native, "Disposing", False))):
            return
        if native is not None and bool(native.InvokeRequired):
            from System import Action
            native.BeginInvoke(Action(_detach))
        else:
            _detach()
    except BaseException:  # aqg: top-level boundary — form teardown can reject the STA dispatch
        pass


def _cleanup_windows_share(state):
    """Idempotently release a share session and its private temporary PNG."""
    global _WINDOWS_SHARE_ACTIVE
    if not state:
        return
    lock = state.get("lock")
    if lock is None:
        return
    detach = None
    subscriptions = []
    path = None
    with lock:
        if state.get("cleaned"):
            return
        state["cleaned"] = True
        detach = state.pop("detach", None)
        subscriptions = list(state.get("subscriptions", ()))
        state["subscriptions"] = []
        path = state.get("path")
        for timer_name in ("timer", "watchdog"):
            timer = state.get(timer_name)
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
    _detach_windows_share_subscriptions(state, subscriptions)
    if callable(detach):
        try:
            detach()
        except BaseException:  # aqg: top-level boundary — event cleanup must not escape a timer callback
            pass
    if path is not None:
        if not _unlink_windows_share_path(path):
            _schedule_windows_share_unlink_retry(path)
    with lock:
        state["keep"] = []
    with _WINDOWS_SHARE_LOCK:
        if _WINDOWS_SHARE_ACTIVE is state:
            _WINDOWS_SHARE_ACTIVE = None
        _WINDOWS_SHARE_RETAINED[:] = [item for item in _WINDOWS_SHARE_RETAINED if item is not state]


def _retire_windows_share(state):
    """Free the UI-active slot while retaining a completed session's PNG for its consumer."""
    global _WINDOWS_SHARE_ACTIVE
    with state["lock"]:
        if state.get("cleaned"):
            return False
        state["phase"] = "retained"
    with _WINDOWS_SHARE_LOCK:
        if _WINDOWS_SHARE_ACTIVE is state:
            _WINDOWS_SHARE_ACTIVE = None
        if not any(item is state for item in _WINDOWS_SHARE_RETAINED):
            _WINDOWS_SHARE_RETAINED.append(state)
    return True


def _cleanup_windows_share_at_exit():
    """Remove any share artifact when the popup process exits before WinRT completion/cancel."""
    with _WINDOWS_SHARE_LOCK:
        state = _WINDOWS_SHARE_ACTIVE
        retained = list(_WINDOWS_SHARE_RETAINED)
    for current in [state, *retained]:
        if current is not None:
            _cleanup_windows_share(current)
    with _WINDOWS_SHARE_PENDING_LOCK:
        pending = list(_WINDOWS_SHARE_PENDING_UNLINKS.items())
    for path, pending_state in pending:
        timer = pending_state.get("timer")
        if timer is not None:
            timer.cancel()
        if _unlink_windows_share_path(path, delays=(0.0, 0.1, 0.4, 1.5)):
            with _WINDOWS_SHARE_PENDING_LOCK:
                _WINDOWS_SHARE_PENDING_UNLINKS.pop(path, None)


atexit.register(_cleanup_windows_share_at_exit)


def _schedule_windows_share_cleanup(state, delay=5.0):
    """Keep the source alive briefly after completion/cancel, then clean it in a daemon timer."""
    with state["lock"]:
        if state.get("cleaned") or state.get("cleanup_scheduled"):
            return
        state["cleanup_scheduled"] = True
        timer = threading.Timer(delay, _cleanup_windows_share, args=(state,))
        timer.daemon = True
        state["timer"] = timer
        timer.start()


def _finish_windows_share(state, *, cancelled):
    """Free the UI slot immediately, then retain native resources until Windows finishes dismissal."""
    if _retire_windows_share(state):
        _schedule_windows_share_cleanup(state)


def _windows_share_live(state):
    with state["lock"]:
        return not state.get("cleaned")


def _windows_event_handler(source, event_name, callback):
    """Build an exact .NET event delegate through a typed bridge and return objects to retain."""
    event = source.GetType().GetEvent(event_name)
    if event is None:
        raise AttributeError(event_name)
    from System import Action, Array, Object
    from System.Linq.Expressions import Expression, ParameterExpression

    bridge = Action[Object, Object](callback)
    invoke = event.EventHandlerType.GetMethod("Invoke")
    parameters = Array[ParameterExpression]([
        Expression.Parameter(parameter.ParameterType, parameter.Name)
        for parameter in invoke.GetParameters()
    ])
    arguments = Array[Expression]([
        Expression.Convert(parameter, Object) for parameter in parameters
    ])
    body = Expression.Call(
        Expression.Constant(bridge), bridge.GetType().GetMethod("Invoke"), arguments)
    handler = Expression.Lambda(event.EventHandlerType, body, parameters).Compile()
    return bridge, handler


def _subscribe_windows_event(source, event_name, callback):
    """Attach an exact WinRT delegate and retain its EventRegistrationToken for removal."""
    bridge, handler = _windows_event_handler(source, event_name, callback)
    from System import Array, Object
    event = source.GetType().GetEvent(event_name)
    if event is None:
        raise AttributeError(event_name)
    token = event.GetAddMethod().Invoke(source, Array[Object]([handler]))
    return bridge, handler, token


def _unsubscribe_windows_event(source, event_name, token):
    from System import Array, Object
    event = source.GetType().GetEvent(event_name)
    if event is None:
        return
    event.GetRemoveMethod().Invoke(source, Array[Object]([token]))


def _windows_share_manager(hwnd):
    """Return a HWND-bound DataTransferManager RCW plus its interop factory pointer and release callback."""
    import ctypes
    import clr
    from ctypes import wintypes

    clr.AddReference("System.Runtime.WindowsRuntime")
    from System import IntPtr
    from System.Runtime.InteropServices import Marshal

    combase = ctypes.WinDLL("combase", use_last_error=True)
    combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    combase.WindowsCreateString.restype = ctypes.c_long
    combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    combase.WindowsDeleteString.restype = ctypes.c_long
    combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GUID),
                                               ctypes.POINTER(ctypes.c_void_p)]
    combase.RoGetActivationFactory.restype = ctypes.c_long

    class_name = "Windows.ApplicationModel.DataTransfer.DataTransferManager"
    hstring = ctypes.c_void_p()
    factory = ctypes.c_void_p()
    if combase.WindowsCreateString(class_name, len(class_name), ctypes.byref(hstring)) < 0:
        return None
    try:
        interop_iid = _guid("3A3DCD6C-3EAB-43DC-BCDE-45671CE800C8")
        if combase.RoGetActivationFactory(hstring, ctypes.byref(interop_iid), ctypes.byref(factory)) < 0:
            return None
    finally:
        combase.WindowsDeleteString(hstring)
    if not factory.value:
        return None

    # Windows SDK shobjidl_core.h ``IDataTransferManagerInterop`` ABI: IUnknown slots 0..2,
    # GetForWindow at 3, ShowShareUIForWindow at 4; IID is the SDK interface UUID below.
    vtable = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    get_for_window = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p))(vtable[3])
    show_for_window = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, wintypes.HWND)(vtable[4])
    manager_iid = _guid("A5CAEE9B-8708-49D1-8D36-67D25A8DA00C")
    manager_pointer = ctypes.c_void_p()
    hr = get_for_window(factory, hwnd, ctypes.byref(manager_iid), ctypes.byref(manager_pointer))
    if hr < 0 or not manager_pointer.value:
        release(factory)
        return None
    try:
        manager = Marshal.GetObjectForIUnknown(IntPtr(manager_pointer.value))
    except Exception:  # aqg: top-level boundary — failed RCW creation must release both COM references
        release(factory)
        return None
    finally:
        Marshal.Release(IntPtr(manager_pointer.value))
    return manager, factory, release, show_for_window


def _show_windows_share_ui(native, storage_file, state):
    """Run on the WinForms STA: bind DataRequested, populate the bitmap, and show the system UI."""
    import clr

    clr.AddReference("System.Runtime.WindowsRuntime")
    try:
        from Windows.Storage.Streams import RandomAccessStreamReference

        if not _windows_share_live(state):
            return False
        bitmap_reference = RandomAccessStreamReference.CreateFromFile(storage_file)

        handle = native.Handle
        hwnd = handle.ToInt64() if hasattr(handle, "ToInt64") else handle.ToInt32()
        interop = _windows_share_manager(hwnd)
        if interop is None:
            return False
        manager, factory, release, show_for_window = interop
        def _on_share_completed(*_args):
            try:
                _finish_windows_share(state, cancelled=False)
            except BaseException:  # aqg: top-level boundary - nothing escapes a WinRT event callback
                pass

        def _on_share_canceled(*_args):
            try:
                _finish_windows_share(state, cancelled=True)
            except BaseException:  # aqg: top-level boundary - nothing escapes a WinRT event callback
                pass

        def _on_data_requested(_sender, args):
            try:
                with state["lock"]:
                    if state.get("cleaned"):
                        return
                data = args.Request.Data
                data.Properties.Title = "Decision Engine"
                data.Properties.Description = "Decision Engine visual"
                data.SetBitmap(bitmap_reference)
                for event_name, callback in (
                        ("ShareCompleted", _on_share_completed),
                        ("ShareCanceled", _on_share_canceled)):
                    try:
                        bridge, handler, token = _subscribe_windows_event(
                            data, event_name, callback)
                        if not _retain_windows_share_subscription(
                                state, data, event_name, bridge, handler, token):
                            return
                    except Exception:  # aqg: top-level boundary — older builds use watchdog cleanup below
                        pass
                with state["lock"]:
                    if state.get("cleaned"):
                        return
                    state["keep"].extend([data, _on_share_completed, _on_share_canceled])
            except BaseException as exc:  # aqg: top-level boundary — fail closed at the WinRT callback boundary
                try:
                    args.Request.FailWithDisplayText("Decision Engine could not prepare the image.")
                except BaseException:  # aqg: top-level boundary — older WinRT projections may lack this API
                    pass
                try:
                    _schedule_windows_share_cleanup(state)
                except BaseException:  # aqg: top-level boundary — cleanup scheduling cannot escape WinRT
                    pass
                try:
                    print("native_shell: Windows share DataRequested failed (%s)" % type(exc).__name__,
                          file=sys.stderr)
                except BaseException:  # aqg: top-level boundary — diagnostic logging cannot escape WinRT
                    pass

        try:
            data_bridge, data_handler, data_token = _subscribe_windows_event(
                manager, "DataRequested", _on_data_requested)
            if not _retain_windows_share_subscription(
                    state, manager, "DataRequested", data_bridge, data_handler, data_token):
                return False
            if not _retain_windows_share_objects(
                    state, manager, storage_file, bitmap_reference, _on_data_requested):
                return False
            with state["lock"]:
                if state.get("cleaned"):
                    return False
                hr = show_for_window(factory, hwnd)
        finally:
            release(factory)
        if hr < 0:
            return False
        watchdog = threading.Timer(300.0, _cleanup_windows_share, args=(state,))
        watchdog.daemon = True
        with state["lock"]:
            if state.get("cleaned"):
                return False
            state["watchdog"] = watchdog
            watchdog.start()
        return True
    except Exception as exc:  # aqg: top-level boundary — WinRT projection/share failures are recoverable
        try:
            print("native_shell: Windows share setup failed (%s)" % type(exc).__name__, file=sys.stderr)
        except BaseException:  # aqg: top-level boundary — diagnostic logging cannot escape WinForms
            pass
        return False


def _start_windows_share_ui(win, state):
    """Begin StorageFile projection and HWND-bound share setup without blocking the WinForms STA."""
    try:
        import clr
        clr.AddReference("System.Runtime.WindowsRuntime")
        from System import Action
        from Windows.Foundation import AsyncOperationCompletedHandler, AsyncStatus
        from Windows.Storage import StorageFile

        native = win.native
        if not bool(native.InvokeRequired):
            return False

        def _load_storage() -> None:
            try:
                operation = StorageFile.GetFileFromPathAsync(state["path"])

                def _loaded(async_operation, status) -> None:
                    try:
                        if status != AsyncStatus.Completed or not _windows_share_live(state):
                            state["shown"].set()
                            return
                        storage_file = async_operation.GetResults()

                        def _show() -> None:
                            try:
                                if _windows_share_live(state):
                                    ok = _show_windows_share_ui(native, storage_file, state)
                                    with state["lock"]:
                                        state["ok"] = ok
                                        if not state.get("cleaned") and state.get("phase") == "pending":
                                            state["phase"] = "shown" if ok else "failed"
                            except BaseException:  # aqg: top-level boundary — nothing escapes WinForms Action
                                state["ok"] = False
                            finally:
                                state["shown"].set()

                        show_delegate = Action(_show)
                        if not _retain_windows_share_objects(state, show_delegate):
                            state["shown"].set()
                            return
                        native.BeginInvoke(show_delegate)
                    except BaseException:  # aqg: top-level boundary — nothing escapes WinRT completion
                        state["shown"].set()

                completion = AsyncOperationCompletedHandler[StorageFile](_loaded)
                if not _retain_windows_share_objects(state, operation, completion):
                    state["shown"].set()
                    return
                operation.Completed = completion
            except BaseException:  # aqg: top-level boundary — nothing escapes the WinForms storage Action
                state["shown"].set()

        start_delegate = Action(_load_storage)
        if not _retain_windows_share_objects(state, start_delegate):
            return False
        native.BeginInvoke(start_delegate)
        return True
    except Exception:  # aqg: top-level boundary — missing WinRT/WinForms dispatch is a normal failure
        return False


def _reuse_active_windows_share(state, timeout):
    """Return an active request's result, or None when a closed modal was retired for replacement."""
    with state["lock"]:
        phase = state.get("phase")
        shown = state.get("shown")
        native = state.get("native")
    if phase == "pending":
        if shown is None or not shown.wait(timeout=timeout):
            return False
        with state["lock"]:
            return bool(state.get("ok"))
    if phase == "shown":
        try:
            if native is not None and not bool(native.Enabled):
                return True
        except BaseException:  # aqg: top-level boundary - disposed WinForms state is stale, not reusable
            pass
        if _retire_windows_share(state):
            _schedule_windows_share_cleanup(state)
        return None
    return None


def _present_windows_share(win, png, timeout=5.0):
    """Persist PNG privately, open Windows 10/11 share UI for this HWND, and retain it through cancel/use."""
    global _WINDOWS_SHARE_ACTIVE
    if not _IS_WINDOWS or win is None or _png_dimensions(png) is None:
        return False
    with _WINDOWS_SHARE_LOCK:
        active = _WINDOWS_SHARE_ACTIVE
    if active is not None:
        reused = _reuse_active_windows_share(active, timeout)
        if reused is not None:
            return reused
    descriptor = None
    path = None
    try:
        descriptor, path = tempfile.mkstemp(prefix="de-ge-share-", suffix=".png")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None  # fdopen owns and closes the descriptor from here onward
            handle.write(png)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:  # aqg: top-level boundary — no temp artifact means no share
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if path is not None:
            _unlink_windows_share_path(path)
        return False
    state = {
        "path": path,
        "shown": threading.Event(),
        "ok": False,
        "keep": [],
        "subscriptions": [],
        "native": getattr(win, "native", None),
        "lock": threading.RLock(),
        "cleaned": False,
        "phase": "pending",
    }
    with _WINDOWS_SHARE_LOCK:
        conflict_state = _WINDOWS_SHARE_ACTIVE
        if conflict_state is None:
            _WINDOWS_SHARE_ACTIVE = state
    if conflict_state is not None:
        _unlink_windows_share_path(path)
        reused = _reuse_active_windows_share(conflict_state, timeout)
        return _present_windows_share(win, png, timeout) if reused is None else reused
    if not _start_windows_share_ui(win, state) or not state["shown"].wait(timeout=timeout) or not state["ok"]:
        _cleanup_windows_share(state)
        return False
    return True


def _subview_frame_in_content(css_x, css_y, w, h, content_h, flipped):
    """Map a CSS (top-left origin) rect to an NSView frame in the contentView's coords, as ((x,y),(w,h)).
    A non-flipped NSView has a BOTTOM-left origin, so flip y (y = content_h - css_y - h); a flipped view
    passes y through. w/h clamp to >= 1 so a zero/negative measured rect never yields a negative frame.
    Pure (no AppKit) → unit-testable without a window."""
    w = max(1.0, float(w))
    h = max(1.0, float(h))
    x = float(css_x)
    y = float(css_y) if flipped else (float(content_h) - float(css_y) - h)
    return ((x, y), (w, h))


def _settle_objc_result(done, store, error) -> bool:
    """Body of a WKWebView completion handler as a testable unit with the crash-safety invariant baked in:
    `store` (0-arg) runs ONLY when `error` is None; the `done` Event is ALWAYS set (guarded finally); and
    NOTHING escapes — not even BaseException. A raise inside an ObjC-invoked completion handler unwinds
    through PyObjC and SIGABRTs the whole popup, so this boundary catches EVERYTHING. Returns True if
    `store` ran clean (or was skipped for an error), False if it raised (swallowed + logged)."""
    ok = True
    try:
        if error is None:
            store()
    except BaseException as exc:  # aqg: ObjC-callback boundary — nothing may reach the ObjC runtime (SIGABRT)
        ok = False
        try:
            print("native_shell: swallowed exception in WKWebView completion handler: %r" % (exc,),
                  file=sys.stderr)
        except BaseException:  # aqg: even the diagnostic print must never raise out of an ObjC callback
            pass
    finally:
        try:
            done.set()
        except BaseException:  # aqg: a missed signal (→ the caller's bounded-wait timeout) beats a crash
            pass
    return ok


def _main_webview():
    """The popup's MAIN WKWebView. pywebview wraps it in a KVO subclass, so match via isKindOfClass_(WKWebView),
    not a class-name compare. Returns None off macOS / if not found. MUST run on the main thread (walks the
    AppKit view tree)."""
    if not _IS_MAC:
        return None
    try:
        from WebKit import WKWebView
    except Exception:  # aqg: top-level boundary — no WebKit → no native screenshot
        return None
    win = _content_nswindow()
    if win is None:
        return None

    def _find(view):
        if view is None:
            return None
        try:
            if view.isKindOfClass_(WKWebView):
                return view
        except Exception:  # aqg: top-level boundary — a non-view object in the tree
            return None
        try:
            subs = list(view.subviews())
        except Exception:  # aqg: top-level boundary — a leaf without subviews()
            subs = []
        for s in subs:
            found = _find(s)
            if found is not None:
                return found
        return None

    return _find(win.contentView())


def _snapshot_png_data(get_wv, timeout=3.0, rect=None, strict_rect=False):
    """Snapshot a WKWebView to PNG NSData (or None). `get_wv` is invoked ON THE MAIN THREAD inside the
    dispatched block. WKWebView.takeSnapshot must be initiated on the main thread and its completion fires
    there too, so the whole resolve+snapshot is dispatched to the main queue and the WORKER blocks on a
    bounded threading.Event (a main-thread wait would deadlock). The encode happens on the worker AFTER the
    wait, so a completion arriving past the timeout only stashes an unread image and produces nothing.

    `strict_rect`: when a `rect` is supplied but cannot be applied to the snapshot config, FAIL CLOSED
    (produce no image) instead of degrading to a full-view shot. Region-ask passes this (a full shot would
    leak the chat column / chrome the user never framed); copy/share leave it False (a full fallback there
    is just the user's own visual)."""
    if not _IS_MAC:
        return None
    if threading.current_thread() is threading.main_thread():
        return None   # the bounded wait would deadlock; js_api callers are always a worker
    try:
        from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep
        from Foundation import NSOperationQueue
        from WebKit import WKSnapshotConfiguration
    except Exception:  # aqg: top-level boundary — missing framework → no snapshot
        return None
    done = threading.Event()
    state = {"image": None}

    def _take():
        try:
            wv = get_wv()   # resolve on the MAIN thread (view-tree walk)
            if wv is None:
                done.set()
                return
            cfg = WKSnapshotConfiguration.alloc().init()
            if rect is not None:                     # region snapshot: rect = (x,y,w,h) in webview CSS px.
                # WKWebView is a FLIPPED view (isFlipped=YES), so a top-left-origin CSS rect maps straight
                # into WKSnapshotConfiguration.rect with NO y-flip — this is why it diverges from
                # _subview_frame_in_content (which positions a bottom-left NSView frame for the share anchor).
                try:
                    from Foundation import NSMakeRect
                    cfg.setRect_(NSMakeRect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])))
                except Exception:  # aqg: the requested rect could not be applied
                    if strict_rect:
                        # region-ask is FAIL-CLOSED: never degrade to a full-view shot (chat/chrome leak).
                        done.set()
                        return
                    # copy/share: a bad rect degrades to a full shot of the user's OWN visual — acceptable

            def _handler(image, error):
                def _store() -> None:
                    if image is not None:
                        state["image"] = image
                _settle_objc_result(done, _store, error)
            wv.takeSnapshotWithConfiguration_completionHandler_(cfg, _handler)
        except Exception:  # aqg: top-level boundary — degrade to no-snapshot
            done.set()

    try:
        NSOperationQueue.mainQueue().addOperationWithBlock_(_take)
    except Exception:  # aqg: top-level boundary — can't schedule → no snapshot
        return None
    if not done.wait(timeout=timeout):
        return None   # TIMEOUT: a late _handler only stashes an unread image
    image = state["image"]
    if image is None:
        return None
    try:
        rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        return rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)   # PNG NSData
    except Exception:  # aqg: top-level boundary — encode failure → no snapshot
        return None


def _copy_png_to_clipboard(png_data) -> bool:
    """Write PNG NSData to the general pasteboard AS AN IMAGE (a paste in any app yields the picture).
    NSPasteboard mutation is dispatched to the MAIN queue (AppKit thread-affinity). Best-effort."""
    if png_data is None or not _IS_MAC:
        return False
    try:
        from AppKit import NSPasteboard, NSPasteboardTypePNG
        from Foundation import NSOperationQueue

        def _do() -> None:
            try:
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setData_forType_(png_data, NSPasteboardTypePNG)
            except Exception:  # aqg: top-level boundary — a clipboard failure is cosmetic
                pass
        NSOperationQueue.mainQueue().addOperationWithBlock_(_do)
        return True
    except Exception:  # aqg: top-level boundary — missing framework / dispatch failure → no copy
        return False


_SHARE_KEEP = []   # keep the NSSharingServicePicker alive until the user dismisses it (else GC closes it)


def _present_share_menu(png_data, anchor=None) -> bool:
    """Present the macOS native share sheet (NSSharingServicePicker) for a PNG, anchored UNDER the Share
    button. `anchor` = the button's [x,y,w,h] in CSS px (top-left origin), mapped to the contentView's
    coords via _subview_frame_in_content (flip-aware). Main queue; best-effort."""
    if png_data is None or not _IS_MAC:
        return False
    try:
        from AppKit import NSImage, NSMaxYEdge, NSMinYEdge, NSSharingServicePicker
        from Foundation import NSMakeRect, NSOperationQueue
    except Exception:  # aqg: top-level boundary — missing framework → no share
        return False

    def _do() -> None:
        try:
            img = NSImage.alloc().initWithData_(png_data)
            win = _content_nswindow()
            view = win.contentView() if win is not None else None
            if img is None or view is None:
                return
            picker = NSSharingServicePicker.alloc().initWithItems_([img])
            _SHARE_KEEP.append(picker)
            del _SHARE_KEEP[:-8]   # bound the keep-alive list (was unbounded → a per-share leak); 8 recent pickers is ample
            b = view.bounds()
            flipped = bool(view.isFlipped())
            if anchor is not None:
                (rx, ry), (rw, rh) = _subview_frame_in_content(
                    anchor[0], anchor[1], anchor[2], anchor[3], float(b.size.height), flipped)
                rect = NSMakeRect(rx, ry, rw, rh)
            else:
                rect = NSMakeRect(float(b.size.width) - 80.0,
                                  (0.0 if flipped else float(b.size.height) - 36.0), 30.0, 30.0)
            edge = NSMaxYEdge if flipped else NSMinYEdge
            picker.showRelativeToRect_ofView_preferredEdge_(rect, view, edge)
        except Exception:  # aqg: top-level boundary — a present failure must not crash the popup
            pass
    try:
        NSOperationQueue.mainQueue().addOperationWithBlock_(_do)
        return True
    except Exception:  # aqg: top-level boundary — dispatch failure → no share
        return False


def _mac_after_show(*_args) -> None:
    """`loaded` handler: activate the app + make the window key on the NEXT main-loop tick, so the popup
    opens FOCUSED. The focus step is DEFERRED via the main queue because AppKit window-ordering INSIDE this
    KVO callback crashes (the A-repo spike lesson); running it after the callback returns is safe. (We no
    longer force Accessory/no-Dock here — the popup is a normal Dock app now; see _apply_mac_chrome.)"""
    try:
        from AppKit import NSApp, NSFloatingWindowLevel
        from Foundation import NSOperationQueue

        def _focus() -> None:
            try:
                NSApp.activateIgnoringOtherApps_(True)
                w = _content_nswindow()
                if w is not None:
                    w.makeKeyAndOrderFront_(None)
                    # on_top sets NSStatusWindowLevel (25), which occludes the system IME candidate window
                    # → drop to NSFloatingWindowLevel (3): still floats (no-Dock preserved) but below the IME.
                    w.setLevel_(NSFloatingWindowLevel)
            except Exception:  # aqg: top-level boundary — focus is a nicety, never crash
                pass
        NSOperationQueue.mainQueue().addOperationWithBlock_(_focus)
    except Exception:  # aqg: top-level boundary — best-effort, a Dock icon / unfocus is cosmetic-only
        pass


def _run_on_mac_main_queue(callback) -> None:
    """Run an AppKit callback on the main queue when available, otherwise run it inline."""
    try:
        from Foundation import NSOperationQueue

        NSOperationQueue.mainQueue().addOperationWithBlock_(callback)
    except Exception:  # aqg: top-level boundary — tests/headless hosts may lack PyObjC's queue
        callback()


def _install_mac_status_item(title: str, win) -> None:
    """Create and wire the macOS status item after pywebview has created NSApplication.

    Direct NSStatusBar access before AppKit/pywebview owns a legal NSApplication can abort the
    process in CoreGraphics. Match the other macOS AppKit niceties by running from the loaded
    event, then defer once more through the main queue when PyObjC exposes it.
    """
    def _install() -> None:
        try:
            item = _apply_mac_chrome(title)
            _wire_status_toggle(item, win)
        except Exception as exc:  # aqg: top-level boundary — icon wiring is optional, never hard-fail
            print("native_shell: macOS status icon skipped (%s)" % exc, file=sys.stderr)

    _run_on_mac_main_queue(_install)


def _win_after_show(win) -> None:
    """`loaded` handler (Windows): re-issue a show on the detached frameless popup.

    A DETACHED_PROCESS child's first FRAMELESS (WS_POPUP) window can come up hidden — the reported
    symptom (a bordered window was force-shown, only the frameless popup regressed). Re-assert
    visibility in-process via pywebview's public, thread-safe ``Window.show()`` (Show()+Activate()
    on the WinForms form). Idempotent — a no-op when the window is already up, so it is harmless in
    the paths where the popup already shows. NOTE: not reproduced in an interactive-desktop spawn
    (the bug's trigger appears tied to the shim's launch window-station/desktop session), so this is
    a directionally-correct remedy, not one verified against the original failure."""
    try:
        win.show()
    except Exception as exc:  # aqg: top-level boundary — a failed re-show must never crash the popup
        print("native_shell: windows re-show skipped (%s)" % exc, file=sys.stderr)


def _install_window_chrome(win) -> None:
    """Install shared maximize and edge/corner resize controls."""
    try:
        # Bake the resolved-locale control tooltips into the chrome JS (single language, no in-JS
        # branch) — same script-safe escaping launcher uses for its page string dicts.
        chrome_js = launcher._inject_js_strings(
            _WINDOW_CHROME_JS, "__SHELL_STRINGS__", i18n.shell(_SHELL_LOCALE))
        outcome = win.evaluate_js(chrome_js)
        if outcome != "enhanced":
            print(
                "native_shell: window chrome enhancement skipped (%s)" % outcome,
                file=sys.stderr,
            )
    except Exception as exc:  # aqg: top-level boundary — markup drift/page teardown stays non-fatal
        print("native_shell: window chrome enhancement skipped (%s)" % exc, file=sys.stderr)


def _install_diagram_layout_recovery(win) -> None:
    """Retry only the server page's own relayout after the native API becomes ready."""
    try:
        outcome = win.evaluate_js(_DIAGRAM_LAYOUT_RECOVERY_JS)
        if outcome not in {"armed", "not-applicable"}:
            print(
                "native_shell: diagram layout recovery skipped (%s)" % outcome,
                file=sys.stderr,
            )
    except Exception as exc:  # aqg: top-level boundary — a recovery retry is best-effort
        print("native_shell: diagram layout recovery skipped (%s)" % exc, file=sys.stderr)


def _install_windows_chrome(win) -> None:
    """Install the Windows-only title-bar drag bridge."""
    try:
        outcome = win.evaluate_js(_WINDOWS_DRAG_JS)
        if outcome != "enhanced":
            print(
                "native_shell: windows drag enhancement skipped (%s)" % outcome,
                file=sys.stderr,
            )
    except Exception as exc:  # aqg: top-level boundary — markup drift/page teardown stays non-fatal
        print("native_shell: windows drag enhancement skipped (%s)" % exc, file=sys.stderr)


def _claim_app_identity() -> None:
    """Tell Windows who this process is, BEFORE the first window — see
    ``client.tk_icon.claim_app_identity``.

    macOS splits the equivalent in two: process name is claimed before ``create_window`` so Dock
    never snapshots ``Python``, while the tile image waits until ``loaded`` when NSApplication
    exists."""
    try:
        from client.tk_icon import claim_app_identity

        claim_app_identity()
    except Exception as exc:  # aqg: top-level boundary — an icon never blocks a window
        print("native_shell: app identity skipped (%s)" % exc, file=sys.stderr)


def _claim_dock_app_name(title: str) -> None:
    """macOS: claim the Dock-visible process name before the app registers with Dock.

    Unlike the Dock icon, changing NSProcessInfo's process name does not need an NSApplication and
    should happen before ``create_window`` so Dock never snapshots the interpreter's ``Python`` name.
    It is repeated after ``loaded`` by ``_install_dock_icon`` as a harmless fallback for older
    backends, but this early call is the one that matters for the taskbar/Dock hover title.
    """
    try:
        from client.tk_icon import apply_dock_app_name

        if not apply_dock_app_name(title):
            print("native_shell: dock app name skipped", file=sys.stderr)
    except Exception as exc:  # aqg: top-level boundary — a title never blocks a window
        print("native_shell: dock app name skipped (%s)" % exc, file=sys.stderr)


def _install_dock_icon(title: str) -> None:
    """macOS: replace the interpreter's Dock identity with the product's, once pywebview's app exists.

    On ``loaded`` rather than before ``create_window`` because ``apply_dock_icon`` deliberately
    refuses to instantiate the NSApplication itself — doing so is what aborts a Tk that starts
    afterwards, and the same restraint costs nothing here since pywebview has long since made one
    by the time a page loads."""
    _claim_dock_app_name(title)
    try:
        from client.tk_icon import apply_dock_icon

        if not apply_dock_icon():
            print("native_shell: dock icon skipped (no application or no icns)", file=sys.stderr)
    except Exception as exc:  # aqg: top-level boundary — an icon never blocks a window
        print("native_shell: dock identity skipped (%s)" % exc, file=sys.stderr)


def _write_ready_diagnostic(ready_path: Optional[str], reason: str, detail: str = "") -> None:
    """Best-effort child-side startup diagnostic beside the ready marker."""
    if not ready_path:
        return
    try:
        diagnostic_path = str(Path(ready_path).with_name("ready-diagnostic.json"))
        payload = {"ok": False, "reason": reason}
        if detail:
            payload["detail"] = detail[:200]
        _write_json_atomic(diagnostic_path, payload)
    except Exception as exc:  # aqg: top-level boundary — diagnostics must never break the popup
        print("native_shell: ready diagnostic skipped (%s)" % exc, file=sys.stderr)


def _mark_popup_ready(win: Any, ready_path: Optional[str]) -> None:
    """Loaded hook: prove the page DOM is reachable, then let the parent report ``open``."""
    if not ready_path:
        return
    path = Path(ready_path)
    if path.exists():
        return
    probe = (
        "(function(){"
        "if(!document||document.readyState!=='complete'||!document.body)return false;"
        "if(document.getElementById('close-btn'))return true;"
        "if(document.querySelector('.artifact'))return true;"
        "return document.body.children&&document.body.children.length>0;"
        "})()"
    )
    try:
        if win.evaluate_js(probe) is True:
            _write_json_atomic(str(path), {"ok": True, "state": "ready"})
        else:
            _write_ready_diagnostic(ready_path, "dom-not-ready")
    except Exception as exc:  # aqg: top-level boundary — startup readiness is reported by parent
        _write_ready_diagnostic(ready_path, "dom-probe-failed", type(exc).__name__)


def _windows_hwnd(win) -> int:
    """The popup's Win32 handle. Delegates to client.tk_icon so the setup dialogs, which need the
    same lookup and must not import this module, share one implementation."""
    from client.tk_icon import native_window_handle

    return native_window_handle(win)


def _install_windows_taskbar_icon(win) -> None:
    """Stamp the product icon on the popup's taskbar button and Alt-Tab card.

    Runs on ``loaded`` because the handle does not exist until the window is realised, and guards
    against running twice: ``loaded`` fires once per navigation, and the Cursor profile navigates
    (bootstrap → target), so an unguarded handler would leak an HICON pair per hop."""
    if getattr(win, "_de_taskbar_icon_set", False):
        return
    try:
        from client.tk_icon import apply_taskbar_icon

        hwnd = _windows_hwnd(win)
        if not hwnd:
            print("native_shell: taskbar icon skipped (no window handle)", file=sys.stderr)
            return
        win._de_taskbar_icon_set = apply_taskbar_icon(hwnd)
    except Exception as exc:  # aqg: top-level boundary — a missing icon never closes the popup
        print("native_shell: taskbar icon skipped (%s)" % exc, file=sys.stderr)


def _wire_window_chrome(win, api: PopupApi) -> None:
    """Wire the shared controls for every frameless backend (including Windows and macOS)."""
    try:
        win.events.maximized += api._on_window_maximized
        win.events.restored += api._on_window_restored
    except Exception as exc:  # aqg: top-level boundary — backend event support is optional
        print("native_shell: window state wiring skipped (%s)" % exc, file=sys.stderr)
    win.events.loaded += lambda *a: _install_window_chrome(win)
    win.events.loaded += lambda *a: _install_diagram_layout_recovery(win)


def _windows_primary_button_down() -> bool:
    """Read the configured primary mouse button so missed DOM mouseup cannot leave drag armed."""
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # GetAsyncKeyState reports physical buttons. Respect Windows' left-handed button swap so
        # the DOM's logical primary button and the native safety check describe the same press.
        primary_vk = 0x02 if user32.GetSystemMetrics(23) else 0x01  # SM_SWAPBUTTON / VK_RBUTTON
        return bool(user32.GetAsyncKeyState(primary_vk) & 0x8000)
    except Exception:  # aqg: top-level boundary — fail closed: no button state means no move
        return False


def _windows_window_maximized(win) -> Optional[bool]:
    """Return Win32's live zoom state when the pywebview native handle is available."""
    if not _IS_WINDOWS or win is None:
        return None
    try:
        import ctypes
        handle = win.native.Handle
        hwnd = handle.ToInt64() if hasattr(handle, "ToInt64") else handle.ToInt32()
        return bool(ctypes.windll.user32.IsZoomed(ctypes.c_void_p(hwnd)))
    except Exception:  # aqg: top-level boundary — event-synchronized state remains the fallback
        return None


def _show_and_focus(win) -> None:
    """Bring the popup window back: show it, activate the app, make it key + FRONT at floating level."""
    from AppKit import NSApp, NSFloatingWindowLevel
    win.show()                                   # hidden → bring it back to the front, FOCUSED
    NSApp.activateIgnoringOtherApps_(True)
    w = _content_nswindow()
    if w is not None:
        w.makeKeyAndOrderFront_(None)
        w.setLevel_(NSFloatingWindowLevel)       # re-apply (show can reset to on_top's level)


def _toggle_window(win) -> None:
    """Show a hidden popup (front + focused) or hide a visible one — the SHARED show/hide toggle used by
    BOTH the menu-bar icon and the Dock-icon reopen, so the two recovery paths behave identically."""
    w = _content_nswindow()
    if w is not None and bool(w.isVisible()):
        win.hide()                               # visible → hide so the caller window shows through
    else:
        _show_and_focus(win)


def _wire_status_toggle(item, win) -> None:
    """Menu-bar icon click → TOGGLE show/hide (the SECONDARY recovery path; the Dock icon is primary).
    So the user has two ways to hide (this icon + the in-page `−` button) and two ways to recall it
    (this icon + the Dock icon). Ported from the reference A-repo shell."""
    from Foundation import NSObject

    class _Toggle(NSObject):
        def clicked_(self, _sender):
            try:
                _toggle_window(win)
            except Exception:  # aqg: top-level boundary — a status-bar click must never crash the popup
                pass

    handler = _Toggle.alloc().init()
    _STATUS_KEEP.append(handler)                 # keep the target alive for the process lifetime
    item.button().setTarget_(handler)
    item.button().setAction_("clicked:")


def _install_dock_reopen(win) -> None:
    """macOS: make a Dock-icon click TOGGLE the popup (show it when hidden, hide it when visible) — the
    PRIMARY, most-discoverable way back after the `−` button hides the window.

    The popup is a normal Dock app (pywebview forces NSApplicationActivationPolicyRegular and we no longer
    fight it), so macOS delivers a Dock-icon click to the app delegate's
    ``applicationShouldHandleReopen:hasVisibleWindows:``. pywebview's own app delegate does NOT implement
    that selector, so we ADD it to the LIVE delegate's class — non-destructive (pywebview keeps every
    method it has; the reopen selector is a pure addition, its signature inferred from AppKit metadata) —
    or install a minimal delegate if pywebview set none. Best-effort: on any failure the menu-bar icon
    stays the way back."""
    try:
        import objc
        from AppKit import NSApp
        from Foundation import NSObject

        sel_name = b"applicationShouldHandleReopen:hasVisibleWindows:"

        def _reopen(_self, _app, _has_visible):
            try:
                _toggle_window(win)
            except Exception:  # aqg: top-level boundary — a Dock click must never crash the popup
                pass
            return True

        delegate = NSApp.delegate()
        if delegate is not None:
            if not delegate.respondsToSelector_(sel_name):
                objc.classAddMethods(delegate.class__(), [objc.selector(_reopen, selector=sel_name)])
        else:
            class _DEReopenDelegate(NSObject):
                def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):
                    try:
                        _toggle_window(win)
                    except Exception:  # aqg: top-level boundary
                        pass
                    return True

            handler = _DEReopenDelegate.alloc().init()
            _STATUS_KEEP.append(handler)         # keep the delegate alive for the process lifetime
            NSApp.setDelegate_(handler)
    except Exception as exc:  # aqg: top-level boundary — dock reopen is a recovery nicety, never fatal
        print("native_shell: dock reopen wiring skipped (%s)" % exc, file=sys.stderr)


def open_window(html_path: str, title: str, result_path: str,
                context: "Optional[Dict[str, Any]]" = None,
                on_close_dismiss: bool = False,
                api_profile: str = "legacy",
                initial_state: "Optional[Dict[str, Any]]" = None,
                forbidden_token: Optional[str] = None,
                chat: Any = None,
                chat_route: Optional[str] = None,
                chat_error_code: Optional[str] = None,
                ready_path: Optional[str] = None) -> None:
    """Open the native pywebview window and block until it closes.

    ``webview`` is imported lazily so this module stays importable (and self-checkable) on a machine
    without pywebview. When ``context`` is a bundle and the chat backend imports, a ChatSession is wired so
    the GE follow-up chat is live. When ``on_close_dismiss`` is set, a bare OS-chrome close records a
    terminal ``dismissed`` after the window is gone (detached poll — design §4.2).

    macOS: the window opens FILLING the screen's visible frame as a floating window, then wires
    Dock/status-bar recovery affordances after pywebview has initialized AppKit. Other platforms keep
    the prior default-sized window.
    """
    import webview  # lazy: only the real window path needs the backend

    # Match the shell chrome (control tooltips / tray / chat error fallback) to the language the
    # page already renders: read the resolver's <html lang> back out of the popup HTML.
    global _SHELL_LOCALE
    _SHELL_LOCALE = _read_html_lang(html_path)

    if chat_route != "server" and context and chat_backend is not None:
        try:
            chat = chat_backend.ChatSession(context=context)
            chat_route = "legacy"
        except Exception as exc:  # aqg: top-level boundary — never fail the popup over chat setup
            print("native_shell: chat backend setup skipped (%s)" % exc, file=sys.stderr)

    core_api = PopupApi(
        result_path,
        chat=chat,
        chat_route=chat_route,
        chat_error_code=chat_error_code,
        initial_state=initial_state,
        forbidden_token=forbidden_token,
    )
    api = _api_for_profile(core_api, api_profile)
    cursor_profile = api_profile in ("cursor-ge", "cursor-db")
    if cursor_profile:
        core_api._cursor_guard_required = True
        core_api._cursor_document_ready.clear()
    if cursor_profile and not _IS_WINDOWS:
        core_api.dismiss()
        return
    bootstrap_path = None
    window_url = html_path
    if cursor_profile:
        bootstrap_path = str(Path(html_path).with_name(_CURSOR_BOOTSTRAP_NAME))
        try:
            Path(bootstrap_path).write_text(
                _CURSOR_BOOTSTRAP_HTML, encoding="utf-8"
            )
        except OSError:
            core_api.dismiss()
            return
        window_url = bootstrap_path
    # frameless=True → NO native title bar / traffic-light chrome: the board HTML's own header IS the
    # title bar and its top-right buttons (minimize / close / camera / share) are the window controls
    # (they call the PopupApi methods below). This matches the designed frameless window — a plain
    # create_window opens a bordered OS window, which reads as "a system browser". easy_drag=False →
    # the window is dragged only via the header's `pywebview-drag-region`, not by grabbing a card.
    kw = dict(
        title=title,
        url=window_url,
        js_api=api,
        frameless=True,
        easy_drag=False,
        resizable=True,
    )
    vf = _mac_visible_frame() if _IS_MAC else None
    if vf:
        # no-Dock Accessory windows must be FLOATING (on_top) to stay visible + clickable; size to the
        # screen's visible frame so the popup opens filling the usable area (below the menu bar / above the Dock).
        kw.update(on_top=True, x=vf[0], y=vf[1], width=vf[2], height=vf[3])
    else:
        kw.update(width=900, height=640)   # non-mac / NSScreen unavailable — keep the prior default size
    # Before create_window: Windows reads the AppUserModelID when a window registers with the shell,
    # so a later claim leaves the already-registered button showing Python's icon. This is a
    # separate process from the stop panel and inherits nothing from it — it must claim its own.
    if _IS_MAC:
        _claim_dock_app_name(title)
    _claim_app_identity()
    win = webview.create_window(**kw)
    core_api._win = win
    _wire_window_chrome(win, core_api)
    if cursor_profile:
        _arm_cursor_ready_watchdog(core_api, api.close)
    if _IS_MAC:
        win.events.loaded += lambda *a: _install_dock_icon(title)   # …and stop being python3
        win.events.loaded += lambda *a: _mac_after_show()          # focus the window after it shows
        win.events.loaded += lambda *a: _install_dock_reopen(win)  # Dock-icon click → show/hide toggle
        win.events.loaded += lambda *a: _install_mac_status_item(title, win)  # menu-bar backup toggle
    elif _IS_WINDOWS:
        win.events.loaded += lambda *a: _win_after_show(win)       # re-show the detached frameless popup
        win.events.loaded += lambda *a: _install_windows_chrome(win)
        win.events.loaded += lambda *a: _install_windows_taskbar_icon(win)   # …and stop being Python
    if ready_path and not cursor_profile:
        win.events.loaded += lambda *a: _mark_popup_ready(win, ready_path)
    if cursor_profile:
        cursor_policy_started = threading.Event()

        def configure_cursor_after_bootstrap(*_args):
            if cursor_policy_started.is_set():
                return
            cursor_policy_started.set()
            target_url = configure_cursor_webview2_window(
                win,
                html_path,
                bootstrap_path,
                api,
                ready_path=ready_path,
            )
            if target_url is None:
                api.close()
                return
            _start_cursor_target_navigation(
                win, html_path, target_url, api.close
            )

        win.events.loaded += configure_cursor_after_bootstrap
    webview.start()
    # The OS-close path does not necessarily call the page's beforeunload callback.  Finish the same
    # bounded cancel-before-transport-close sequence before the native process exits.
    core_api.chat_closing()
    # start() returns on close. Record a terminal 'dismissed' for a bare OS-chrome close (idempotent — a
    # prior commit/dismiss already set _done, so this is a no-op then) BEFORE the force-exit.
    if on_close_dismiss:
        core_api.on_window_closed()
    if bootstrap_path is not None:
        try:
            Path(bootstrap_path).unlink()
        except OSError:
            pass
    # ``os._exit`` bypasses Python atexit handlers; remove any private share PNG explicitly first.
    _cleanup_windows_share_at_exit()
    # start()'s js_api HTTP-bridge thread is non-daemon and would keep the process alive — force a full teardown.
    os._exit(0)


def _self_check() -> int:
    """Headless proof that commit → result-file works, with no window/display."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result_path = os.path.join(tmp, "result.json")
        api = PopupApi(result_path)
        sample = {"kind": "db", "selftest": True}
        ok = api.commit(sample)
        if not ok.get("ok"):
            print("native_shell self-check: commit did not report ok", file=sys.stderr)
            return 1
        try:
            written = json.loads(Path(result_path).read_text(encoding="utf-8"))
        except Exception as exc:  # aqg: top-level boundary — self-check must report, not raise
            print("native_shell self-check: result unreadable (%s)" % exc, file=sys.stderr)
            return 1
        if written != {"outcome": "committed", "result": sample}:
            print("native_shell self-check: result mismatch %r" % written, file=sys.stderr)
            return 1
        # commit is one-shot: a second call must be a no-op, not a second write.
        if api.commit({"kind": "db", "again": True}).get("ok"):
            print("native_shell self-check: commit was not one-shot", file=sys.stderr)
            return 1
    print("native_shell self-check: ok")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="native_shell", description="DB/GE native popup shell")
    parser.add_argument("--html", help="path to the popup HTML to display")
    parser.add_argument("--title", default="Decision Engine", help="window title")
    parser.add_argument("--result-path", dest="result_path", help="Submit/Dismiss write the result JSON here")
    parser.add_argument("--context", default=None,
                        help="path to a JSON follow-up-context bundle (enables the GE chat); deleted after read")
    parser.add_argument("--on-close-dismiss", action="store_true", dest="on_close_dismiss",
                        help="record a terminal 'dismissed' if the window is closed via OS chrome (detached poll)")
    parser.add_argument(
        "--api-profile",
        choices=("legacy", "cursor-ge", "cursor-db"),
        default="legacy",
        help="least-privilege page bridge profile",
    )
    parser.add_argument(
        "--bridge-state-stdin",
        action="store_true",
        help="read one bounded Cursor DB state envelope from this child's owned stdin pipe",
    )
    parser.add_argument(
        "--chat-bridge-stdin",
        action="store_true",
        help="read one bounded GE server-chat envelope from this child's owned stdin pipe",
    )
    parser.add_argument(
        "--ready-path",
        default=None,
        help="write a readiness marker after pywebview loaded and DOM probing succeeds",
    )
    parser.add_argument("--self-check", action="store_true", help="headless commit→result plumbing check; no window")
    args = parser.parse_args(argv)

    if args.self_check:
        return _self_check()
    if not args.html or not args.result_path:
        print("native_shell: --html and --result-path are required", file=sys.stderr)
        return 2
    if not Path(args.html).is_file():
        print("native_shell: --html file not found: %s" % args.html, file=sys.stderr)
        return 2

    context = None
    if args.context:
        try:
            loaded = json.loads(Path(args.context).read_text(encoding="utf-8"))
            context = loaded if isinstance(loaded, dict) else None
        except Exception as exc:  # aqg: top-level boundary — a bad bundle opens the popup WITHOUT chat
            print("native_shell: --context unreadable (%s); opening without chat" % exc, file=sys.stderr)
        finally:
            # the bundle is fully in memory now; delete the on-disk temp (it can hold the user's
            # source_text — a privacy surface if left behind). Best-effort — never block the popup.
            try:
                Path(args.context).unlink()
            except OSError:
                pass

    initial_state = None
    forbidden_token = None
    if args.bridge_state_stdin:
        if args.api_profile != "cursor-db":
            print("native_shell: bridge state requires cursor-db", file=sys.stderr)
            return 2
        try:
            raw = sys.stdin.buffer.read(launcher._MAX_CURSOR_BOARD_JSON_BYTES + 4097)
            if len(raw) > launcher._MAX_CURSOR_BOARD_JSON_BYTES + 4096:
                raise ValueError("bridge state too large")
            envelope = json.loads(raw.decode("utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {
                "initial_state",
                "forbidden_token",
            }:
                raise ValueError("bridge state shape")
            initial_state = envelope["initial_state"]
            forbidden_token = envelope["forbidden_token"]
            if not isinstance(forbidden_token, str) or not forbidden_token:
                raise ValueError("bridge token shape")
            launcher._validate_cursor_board_value(
                initial_state, token=forbidden_token
            )
        except (OSError, UnicodeError, TypeError, ValueError, launcher.BoardFetchError):
            print("native_shell: invalid bridge state", file=sys.stderr)
            return 2

    server_chat = None
    chat_route = None
    chat_error_code = None
    checked = None
    if args.chat_bridge_stdin:
        chat_route = "server"
        if args.bridge_state_stdin or args.context:
            chat_error_code = "chat_unavailable"
        else:
            raw = None
            envelope = None
            try:
                raw = sys.stdin.buffer.read(popup_session._MAX_CHAT_BRIDGE_BYTES + 1)
                if not raw or len(raw) > popup_session._MAX_CHAT_BRIDGE_BYTES:
                    raise ValueError("chat bridge size")
                envelope = json.loads(raw.decode("utf-8"))
                checked = popup_session._validate_chat_bridge_envelope(envelope)
                from client.popup.http_chat import HttpChatSession

                server_chat = HttpChatSession(
                    endpoint=checked["endpoint"],
                    token=checked["device_token"],
                    run_id=checked["run_id"],
                )
            except BaseException:  # aqg: top-level boundary — never echo the credential envelope
                chat_error_code = "chat_unavailable"
                server_chat = None
            finally:
                raw = None
                envelope = None
                checked = None
                try:
                    sys.stdin.buffer.close()
                except (OSError, ValueError):
                    pass

    open_window(args.html, args.title, args.result_path,
                context=context, on_close_dismiss=args.on_close_dismiss,
                api_profile=args.api_profile, initial_state=initial_state,
                forbidden_token=forbidden_token, chat=server_chat,
                chat_route=chat_route, chat_error_code=chat_error_code,
                ready_path=args.ready_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
