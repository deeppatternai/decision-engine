"""Synthetic, provider-free vertical tests for server-backed GE popup chat.

The hub is a real loopback HTTP server and the client side is the production
``HttpChatSession`` wired through the production native ``PopupApi`` callback
boundary.  No Claude/Codex/Cursor executable, provider, device credential, or
external network is used.
"""

from __future__ import annotations

import contextlib
import ctypes
import gc
import io
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from client.popup import chat_backend, launcher, native_shell
from client.popup import session as popup_session
from client.popup.http_chat import HttpChatSession
from client.popup.tests.test_http_chat_ui import _run_js

_UUIDS = (
    "123e4567-e89b-42d3-a456-426614174000",
    "123e4567-e89b-42d3-a456-426614174001",
    "123e4567-e89b-42d3-a456-426614174002",
    "123e4567-e89b-42d3-a456-426614174003",
)
_FORBIDDEN_REQUEST_KEYS = frozenset({"history", "model", "provider", "prompt", "usage"})


def _runtime_token() -> str:
    # Constructed at runtime so the credential used by this test cannot itself
    # become a literal test credential in a wheel/sdist.
    return "-".join(("ge", "runtime", "credential", "7f29c1"))  # noqa: FLY002


def _privacy() -> dict:
    return {
        "version": "sha256:synthetic-e2e",
        "providers": [
            {
                "category": "approved-category",
                "data_region": "synthetic-region",
                "training_enabled": False,
                "cache_ttl_seconds": 0,
                "provider_retention_hours": 0,
                "deletion_scope": "conversation",
            }
        ],
    }


def _history_turn(turn_no: int = 1) -> dict:
    suffix = "" if turn_no == 1 else f" {turn_no}"
    return {
        "turn_no": turn_no,
        "status": "completed",
        "user_text": "Earlier question" + suffix,
        "assistant_text": "Earlier answer" + suffix,
        "error_code": None,
        "completed_at": 1_787_587_200,
        "images": [],
        "wire_only_field": "discard-me",
    }


def _contains_key(value: object, forbidden: frozenset[str]) -> bool:
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and key.lower() in forbidden)
            or _contains_key(nested, forbidden)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for synthetic GE chat state")


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _open_process_identity(
    pid: int, *, platform_name: str | None = None, pidfd_opener=None
):
    """Hold an OS identity that cannot retarget if the numeric PID is reused."""
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100001, False, pid)
        return ("windows", handle) if handle else None
    pidfd_open = (
        getattr(os, "pidfd_open", None) if pidfd_opener is None else pidfd_opener
    )
    if callable(pidfd_open):
        try:
            return ("pidfd", pidfd_open(pid, 0))
        except OSError:
            return None
    return None


def _process_identity_alive(identity) -> bool:
    kind, value = identity
    if kind == "windows":
        return ctypes.windll.kernel32.WaitForSingleObject(value, 0) == 258
    readable, _writable, _exceptional = select.select([value], [], [], 0)
    return not readable


def _close_process_identity(identity) -> None:
    if identity is None:
        return
    kind, value = identity
    if kind == "windows":
        ctypes.windll.kernel32.CloseHandle(value)
    else:
        os.close(value)


def _reap_process(pid: int, identity, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = (
            _process_identity_alive(identity)
            if identity is not None
            else _process_alive(pid)
        )
        if not alive:
            return True
        time.sleep(0.01)
    if identity is None:
        return not _process_alive(pid)
    kind, value = identity
    if kind == "windows":
        kernel32 = ctypes.windll.kernel32
        kernel32.TerminateProcess(value, 1)
        kernel32.WaitForSingleObject(value, 2000)
    else:
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if not callable(pidfd_send_signal):
            return False
        try:
            pidfd_send_signal(value, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 2
        while _process_identity_alive(identity) and time.monotonic() < deadline:
            time.sleep(0.01)
        if _process_identity_alive(identity):
            pidfd_send_signal(value, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while _process_identity_alive(identity) and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _process_identity_alive(identity)


@contextlib.contextmanager
def _only_connections_to(*allowed_addresses: tuple[str, int]):
    allowed = {(host, int(port)) for host, port in allowed_addresses}
    create_connection = socket.create_connection
    socket_connect = socket.socket.connect
    socket_connect_ex = socket.socket.connect_ex
    socketpair = socket.socketpair
    socketpair_state = threading.local()
    attempts: list[tuple[str, tuple[str, int]]] = []

    def checked_destination(surface: str, address) -> tuple[str, int]:
        destination = (address[0], int(address[1]))
        internal_socketpair = bool(getattr(socketpair_state, "active", False))
        attempts.append(
            ("socketpair_connect" if internal_socketpair else surface, destination)
        )
        if destination not in allowed and not internal_socketpair:
            raise AssertionError(
                f"external network destination forbidden: {destination!r}"
            )
        return destination

    def checked_create_connection(address, *args, **kwargs):
        checked_destination("create_connection", address)
        return create_connection(address, *args, **kwargs)

    def checked_socket_connect(instance, address):
        checked_destination("connect", address)
        return socket_connect(instance, address)

    def checked_socket_connect_ex(instance, address):
        checked_destination("connect_ex", address)
        return socket_connect_ex(instance, address)

    def checked_socketpair(*args, **kwargs):
        socketpair_state.active = True
        try:
            return socketpair(*args, **kwargs)
        finally:
            socketpair_state.active = False

    with (
        mock.patch.object(
            socket, "create_connection", side_effect=checked_create_connection
        ),
        mock.patch.object(socket.socket, "connect", new=checked_socket_connect),
        mock.patch.object(socket.socket, "connect_ex", new=checked_socket_connect_ex),
        mock.patch.object(socket, "socketpair", new=checked_socketpair),
    ):
        yield attempts


class _HubState:
    def __init__(self, token: str) -> None:
        self.token = token
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.log_lines: list[str] = []
        self.submit_attempts: dict[str, int] = {}
        self.turn_text: dict[str, str] = {}
        self.polls: dict[str, int] = {}
        self.cancelled: set[str] = set()

    def record(self, request: dict) -> int:
        key = request["headers"].get("Idempotency-Key")
        with self.lock:
            self.requests.append(request)
            if key is None:
                return 0
            self.submit_attempts[key] = self.submit_attempts.get(key, 0) + 1
            return self.submit_attempts[key]

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.requests)


class _SyntheticHubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _HubState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep a local redacted log surface so the test can prove the credential
        # never appears in ordinary HTTP diagnostics.
        rendered = fmt % args
        with self.state.lock:
            self.state.log_lines.append(rendered)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _record(self, body: bytes) -> tuple[dict | None, int]:
        parsed = None
        if body:
            parsed = json.loads(body.decode("utf-8"))
        request = {
            "method": self.command,
            "path": self.path,
            "headers": {key: value for key, value in self.headers.items()},
            "body": parsed,
        }
        return parsed, self.state.record(request)

    def _json(self, status: int, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == "Bearer " + self.state.token:
            return True
        self._json(401, {"error_code": "auth_required"})
        return False

    def do_GET(self) -> None:
        if not self._authorized():
            return
        self._record(b"")
        parsed = urlsplit(self.path)
        if parsed.path == "/v1/ge/chat/capabilities":
            self._json(
                200,
                {
                    "ge_chat_v1": True,
                    "ge_chat_privacy_disclosure_summary": {
                        "version": _privacy()["version"],
                        "provider_categories": ["approved-category"],
                    },
                },
            )
            return
        if parsed.path == "/v1/ge/chat/conversations/conv_e2e/turns":
            cursor = int(parse_qs(parsed.query).get("after_turn_no", ["0"])[0])
            if cursor == 0:
                page = {
                    "turns": [_history_turn(1)],
                    "has_more": True,
                    "next_after_turn_no": 1,
                }
            elif cursor == 1:
                page = {"turns": [_history_turn(2)], "has_more": False}
            else:
                page = {"turns": [], "has_more": False}
            self._json(
                200,
                page,
            )
            return
        match = re.fullmatch(r"/v1/ge/chat/turns/(turn_[a-z0-9_]+)", parsed.path)
        if match is None:
            self._json(404, {"error_code": "not_found"})
            return
        turn_code = match.group(1)
        with self.state.lock:
            self.state.polls[turn_code] = self.state.polls.get(turn_code, 0) + 1
            poll_no = self.state.polls[turn_code]
            text = self.state.turn_text[turn_code]
            cancelled = turn_code in self.state.cancelled
        if cancelled:
            self._json(200, {"status": "cancelled", "error_code": "cancelled"})
        elif text == "candidate failure":
            if poll_no == 1:
                self._json(
                    200,
                    {
                        "status": "running",
                        "phase": "calling_model",
                        "poll_after_ms": 1,
                        "candidate": "candidate-two",
                        "provider": "internal-provider-marker",
                        "model": "internal-model-marker",
                    },
                )
            else:
                self._json(
                    200,
                    {
                        "status": "failed",
                        "error_code": "model_unavailable",
                        "message": "all internal candidates failed",
                        "provider": "internal-provider-marker",
                    },
                )
        elif text == "server cancelled":
            if poll_no == 1:
                self._json(
                    200,
                    {"status": "running", "phase": "compacting", "poll_after_ms": 1},
                )
            else:
                self._json(200, {"status": "cancelled", "error_code": "cancelled"})
        elif poll_no == 1:
            self._json(
                200,
                {
                    "status": "running",
                    "phase": "compacting",
                    "poll_after_ms": 1,
                    "candidate": "candidate-one",
                    "provider": "internal-provider-marker",
                },
            )
        elif poll_no == 2:
            self._json(
                200,
                {
                    "status": "running",
                    "phase": "calling_model",
                    "poll_after_ms": 1,
                    "candidate": "candidate-two",
                    "model": "internal-model-marker",
                },
            )
        else:
            self._json(
                200,
                {
                    "status": "completed",
                    "text": "answer for " + text,
                    "turn_count": 2 + len(self.state.turn_text),
                    "remaining_turns": 35,
                    "usage": {"internal": 999},
                },
            )

    def do_POST(self) -> None:
        if not self._authorized():
            return
        body = self._read_body()
        parsed, attempt = self._record(body)
        if self.path == "/v1/ge/chat/conversations":
            self._json(
                200,
                {
                    "conversation_code": "conv_e2e",
                    "status": "active",
                    "turn_count": 1,
                    "max_turns": 40,
                    "expires_at": 1_787_587_200,
                    "privacy_disclosure": _privacy(),
                    "balance_credits": 0,
                },
            )
            return
        submit = re.fullmatch(r"/v1/ge/chat/conversations/conv_e2e/turns", self.path)
        if submit is not None:
            assert isinstance(parsed, dict)
            key = self.headers["Idempotency-Key"]
            text = parsed["text"]
            turn_code = "turn_" + key[-4:]
            with self.state.lock:
                self.state.turn_text[turn_code] = text
            if text == "replay zero" and attempt == 1:
                # Unknown response: the server accepted the key/body but the socket
                # disappeared before status bytes reached the client.
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            price = 9 if text == "positive price" else 0
            self._json(
                200,
                {
                    "turn_code": turn_code,
                    "status": "queued",
                    "phase": "queued",
                    "price_credits": price,
                    "poll_after_ms": 1,
                    "balance_credits": 0,
                },
            )
            return
        cancel = re.fullmatch(r"/v1/ge/chat/turns/(turn_[a-z0-9_]+)/cancel", self.path)
        if cancel is not None:
            with self.state.lock:
                self.state.cancelled.add(cancel.group(1))
            self._json(202, {"accepted": True, "provider": "not-for-client"})
            return
        self._json(404, {"error_code": "not_found"})


class _SyntheticHub:
    def __init__(self, token: str) -> None:
        self.state = _HubState(token)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SyntheticHubHandler)
        self.server.daemon_threads = True
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _ProxyTrapHandler(BaseHTTPRequestHandler):
    def do_CONNECT(self) -> None:
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        self.send_error(502)

    def do_GET(self) -> None:
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        self.send_error(502)

    def do_POST(self) -> None:
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        self.send_error(502)

    def log_message(self, _fmt: str, *_args: object) -> None:
        return None


class _ProxyTrap:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyTrapHandler)
        self.server.daemon_threads = True
        self.server.hits = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    @property
    def hits(self) -> list[str]:
        return list(self.server.hits)  # type: ignore[attr-defined]

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _decode_js_call(script: str) -> tuple[str, list] | None:
    prefix = "window.geChat&&window.geChat."
    if not script.startswith(prefix) or not script.endswith(")"):
        return None
    name, sep, rest = script[len(prefix) :].partition("(")
    if not sep:
        return None
    return name, json.loads("[" + rest[:-1] + "]")


class _RecordingWindow:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.scripts: list[str] = []
        self.destroyed = threading.Event()

    def evaluate_js(self, script: str) -> None:
        with self.lock:
            self.scripts.append(script)

    def destroy(self) -> None:
        self.destroyed.set()

    def calls(self) -> list[tuple[str, list]]:
        with self.lock:
            decoded = [_decode_js_call(script) for script in self.scripts]
        return [item for item in decoded if item is not None]


class _TerminalBarrierWindow(_RecordingWindow):
    def __init__(self, terminal_name: str = "done") -> None:
        super().__init__()
        self.terminal_name = terminal_name
        self.terminal_entered = threading.Event()
        self.terminal_release = threading.Event()
        self._blocked_once = False

    def evaluate_js(self, script: str) -> None:
        super().evaluate_js(script)
        decoded = _decode_js_call(script)
        if (
            decoded is not None
            and decoded[0] == self.terminal_name
            and not self._blocked_once
        ):
            self._blocked_once = True
            self.terminal_entered.set()
            self.terminal_release.wait(timeout=5)


class _HistoryAckBarrierWindow(_RecordingWindow):
    def __init__(self) -> None:
        super().__init__()
        self.ack_entered = threading.Event()
        self.ack_release = threading.Event()

    def evaluate_js(self, script: str) -> bool:
        super().evaluate_js(script)
        decoded = _decode_js_call(script)
        if (
            decoded is not None
            and decoded[0] == "bootstrap"
            and len(decoded[1]) == 2
            and decoded[1][1] is True
        ):
            self.ack_entered.set()
            self.ack_release.wait(timeout=5)
        return True


class _ImmediateTerminalChat:
    def __init__(self) -> None:
        self.turns: list[str] = []

    def bootstrap(self) -> dict:
        return {
            "ok": True,
            "backend": "server",
            "state": "ready",
            "conversation": {
                "status": "active",
                "turn_count": 0,
                "max_turns": 40,
                "expires_at": 1_787_587_200,
            },
            "privacy_disclosure": _privacy(),
            "history": [],
        }

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_error, on_action, client_turn_id, privacy_version
        self.turns.append(text)
        on_state("completed", {"turn_count": len(self.turns), "remaining_turns": 39})
        on_delta("answer")
        on_done("answer")

    def close(self) -> None:
        return None


class _InvalidStateTerminalChat(_ImmediateTerminalChat):
    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_action, client_turn_id, privacy_version
        self.turns.append(text)
        if len(self.turns) == 1:
            on_state("completed", {"turn_count": "invalid"})
            on_state("recovering", {})
            on_state("completed", {"turn_count": 1, "remaining_turns": 39})
            on_delta("contradictory delta")
            on_done("contradictory done")
            on_error("model_unavailable")
            return
        on_state("completed", {"turn_count": 1, "remaining_turns": 39})
        on_delta("answer")
        on_done("answer")


class _RecoveringTerminalCallbackChat(_ImmediateTerminalChat):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_action, client_turn_id, privacy_version
        self.turns.append(text)
        if len(self.turns) == 1:
            on_state("recovering", {})
            if self.mode == "done":
                on_done("recovered answer")
            elif self.mode == "error":
                on_error("model_unavailable")
            else:
                raise RuntimeError("synthetic worker failure")
            return
        on_state("completed", {"turn_count": 1, "remaining_turns": 39})
        on_delta("answer")
        on_done("answer")


class _RecoveringGenerationChangeChat(_ImmediateTerminalChat):
    def __init__(self) -> None:
        super().__init__()
        self.recovering_entered = threading.Event()
        self.recovering_release = threading.Event()

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_error, on_action, client_turn_id, privacy_version
        self.turns.append(text)
        if len(self.turns) == 1:
            on_state("recovering", {})
            self.recovering_entered.set()
            self.recovering_release.wait(timeout=5)
            on_done("stale answer")
            return
        on_state("completed", {"turn_count": 1, "remaining_turns": 39})
        on_delta("answer")
        on_done("answer")


class _QueuedTerminalGenerationChangeChat(_ImmediateTerminalChat):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_queued = threading.Event()
        self.return_release = threading.Event()

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_error, on_action, client_turn_id, privacy_version
        self.turns.append(text)
        on_state("completed", {"turn_count": 1, "remaining_turns": 39})
        on_delta("answer")
        on_done("answer")
        if len(self.turns) == 1:
            self.terminal_queued.set()
            self.return_release.wait(timeout=5)


class _DuplicateTerminalChat(_ImmediateTerminalChat):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_action, client_turn_id, privacy_version
        self.turns.append(text)
        on_state("completed", {"turn_count": 1, "remaining_turns": 39})
        on_delta("answer")
        on_done("answer")
        if self.mode == "error":
            on_error("model_unavailable")
        else:
            raise RuntimeError("synthetic exception after done")


class _ExcessTerminalDeltaChat(_ImmediateTerminalChat):
    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_error, on_action, client_turn_id, privacy_version
        self.turns.append(text)
        on_state("completed", {"turn_count": 1, "remaining_turns": 39})
        on_delta("first")
        on_delta("second")
        on_delta("third")
        on_done("answer")


class _NonSuccessTerminalDeltaChat(_ImmediateTerminalChat):
    def __init__(self, state: str, payload: dict, error_code: str) -> None:
        super().__init__()
        self.state = state
        self.payload = payload
        self.error_code = error_code

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_done, on_action, client_turn_id, privacy_version
        self.turns.append(text)
        on_state(self.state, self.payload)
        on_delta("must be suppressed")
        on_error(self.error_code)


class _ContradictoryTerminalChat(_ImmediateTerminalChat):
    def __init__(
        self, state: str, payload: dict, callback: str, *, callback_first: bool
    ) -> None:
        super().__init__()
        self.state = state
        self.payload = payload
        self.callback = callback
        self.callback_first = callback_first

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_delta, on_action, client_turn_id, privacy_version
        self.turns.append(text)

        def emit_page_terminal() -> None:
            if self.callback == "done":
                on_done("contradictory answer")
            else:
                on_error("model_unavailable")

        if self.callback_first:
            emit_page_terminal()
            on_state(self.state, self.payload)
        else:
            on_state(self.state, self.payload)
            emit_page_terminal()


class _CloseActionChat(_ImmediateTerminalChat):
    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state,
    ) -> None:
        del images, on_error, client_turn_id, privacy_version
        self.turns.append(text)
        on_state("completed", {"turn_count": 1, "remaining_turns": 39})
        on_delta("answer")
        on_done("answer")
        on_action("close")


class GeChatSyntheticVerticalTests(unittest.TestCase):
    def _new_session(self, hub: _SyntheticHub, token: str) -> HttpChatSession:
        return HttpChatSession(
            endpoint=hub.endpoint,
            token=token,
            run_id="run_synthetic_e2e",
            sleep=lambda seconds: time.sleep(min(seconds, 0.01)),
            rng=lambda: 0.0,
        )

    def _wait_call(self, window: _RecordingWindow, name: str, predicate=None) -> list:
        found: list = []

        def locate() -> bool:
            for call_name, args in window.calls():
                if call_name == name and (predicate is None or predicate(args)):
                    found[:] = args
                    return True
            return False

        _wait_until(locate)
        return found

    def test_literal_true_history_ack_owns_the_lifecycle_epoch(self):
        chat = _ImmediateTerminalChat()
        window = _HistoryAckBarrierWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        _wait_until(lambda: api._server_bootstrap is not None)
        generation = api._callback_generation
        acknowledgement: list[bool] = []

        def publish_history() -> None:
            acknowledgement.append(
                api._emit_chat(
                    generation,
                    "bootstrap",
                    api._server_bootstrap,
                    True,
                    require_true=True,
                )
            )

        publish_thread = threading.Thread(target=publish_history, daemon=True)
        publish_thread.start()
        self.assertTrue(window.ack_entered.wait(2))
        acquired = api._lifecycle_lock.acquire(blocking=False)
        if acquired:
            api._lifecycle_lock.release()
        try:
            self.assertFalse(
                acquired,
                "literal-True history acknowledgement released its lifecycle epoch",
            )
        finally:
            window.ack_release.set()
        publish_thread.join(timeout=2)
        self.assertFalse(publish_thread.is_alive())
        self.assertEqual(acknowledgement, [True])

    def test_terminal_callback_batch_is_atomic_with_next_admission(self):
        chat = _ImmediateTerminalChat()
        window = _TerminalBarrierWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")
        version = _privacy()["version"]

        self.assertEqual(api.ask(1, "first", [], _UUIDS[0], version), {"ok": True})
        self.assertTrue(window.terminal_entered.wait(2))
        next_result: list[dict] = []
        next_entered = threading.Event()
        next_finished = threading.Event()

        def ask_next() -> None:
            next_entered.set()
            next_result.append(api.ask(2, "immediate second", [], _UUIDS[1], version))
            next_finished.set()

        next_thread = threading.Thread(target=ask_next, daemon=True)
        next_thread.start()
        try:
            self.assertTrue(next_entered.wait(2))
            self.assertTrue(
                next_finished.wait(2),
                "terminal flush held the lifecycle lock across a renderer callback",
            )
            self.assertEqual(
                next_result,
                [{"ok": False, "error_code": "turn_in_flight"}],
            )
        finally:
            window.terminal_release.set()
        next_thread.join(timeout=2)
        self._wait_call(window, "done", lambda args: args[0] == 1)
        _wait_until(lambda: not api._terminal_flush_in_progress)
        self.assertEqual(
            api.ask(2, "immediate second", [], _UUIDS[1], version),
            {"ok": True},
        )
        self._wait_call(window, "done", lambda args: args[0] == 2)
        self.assertEqual(chat.turns, ["first", "immediate second"])

    def test_invalid_terminal_error_flush_is_atomic_with_next_admission(self):
        chat = _InvalidStateTerminalChat()
        window = _TerminalBarrierWindow("error")
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")
        version = _privacy()["version"]

        self.assertEqual(api.ask(1, "first", [], _UUIDS[0], version), {"ok": True})
        self.assertTrue(window.terminal_entered.wait(2))
        next_result: list[dict] = []
        next_entered = threading.Event()
        next_finished = threading.Event()

        def ask_next() -> None:
            next_entered.set()
            next_result.append(api.ask(2, "immediate second", [], _UUIDS[1], version))
            next_finished.set()

        next_thread = threading.Thread(target=ask_next, daemon=True)
        next_thread.start()
        try:
            self.assertTrue(next_entered.wait(2))
            self.assertTrue(
                next_finished.wait(2),
                "fail-closed flush held the lifecycle lock across a renderer callback",
            )
            self.assertEqual(
                next_result,
                [{"ok": False, "error_code": "turn_in_flight"}],
            )
        finally:
            window.terminal_release.set()
        next_thread.join(timeout=2)
        self._wait_call(
            window,
            "error",
            lambda args: args == [1, "bad_response"],
        )
        _wait_until(lambda: not api._terminal_flush_in_progress)
        self.assertEqual(
            api.ask(2, "immediate second", [], _UUIDS[1], version),
            {"ok": True},
        )
        self._wait_call(window, "done", lambda args: args[0] == 2)
        self.assertEqual(
            [(name, args) for name, args in window.calls() if args and args[0] == 1],
            [("error", [1, "bad_response"])],
        )
        self.assertEqual(chat.turns, ["first", "immediate second"])

    def test_close_action_keeps_admission_blocked_through_will_close(self):
        chat = _CloseActionChat()
        window = _TerminalBarrierWindow("willClose")
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")
        version = _privacy()["version"]
        self.assertEqual(api.ask(1, "close", [], _UUIDS[0], version), {"ok": True})
        self.assertTrue(window.terminal_entered.wait(2))
        next_result: list[dict] = []
        next_finished = threading.Event()

        def ask_next() -> None:
            next_result.append(api.ask(2, "too late", [], _UUIDS[1], version))
            next_finished.set()

        next_thread = threading.Thread(target=ask_next, daemon=True)
        next_thread.start()
        try:
            self.assertTrue(next_finished.wait(2))
            self.assertEqual(
                next_result,
                [{"ok": False, "error_code": "turn_in_flight"}],
            )
        finally:
            window.terminal_release.set()
        next_thread.join(timeout=2)
        self.assertTrue(window.destroyed.wait(2))
        self.assertEqual(chat.turns, ["close"])

    def test_close_drains_admitted_renderer_callback_without_blocking_admission(self):
        chat = _ImmediateTerminalChat()
        window = _TerminalBarrierWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")
        version = _privacy()["version"]

        self.assertEqual(api.ask(1, "first", [], _UUIDS[0], version), {"ok": True})
        self.assertTrue(window.terminal_entered.wait(2))
        close_finished = threading.Event()

        def close_window() -> None:
            api._close_window()
            close_finished.set()

        close_thread = threading.Thread(target=close_window, daemon=True)
        close_thread.start()
        _wait_until(lambda: api._closed)
        self.assertFalse(window.destroyed.is_set())
        self.assertEqual(
            api.ask(2, "too late", [], _UUIDS[1], version),
            {"ok": False, "error_code": "chat_unavailable"},
        )
        self.assertFalse(close_finished.is_set())
        window.terminal_release.set()
        close_thread.join(timeout=2)
        self.assertTrue(close_finished.is_set())
        self.assertTrue(window.destroyed.is_set())

    def test_close_destroys_after_callback_drain_budget_expires(self):
        chat = _ImmediateTerminalChat()
        window = _TerminalBarrierWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")

        self.assertEqual(
            api.ask(1, "first", [], _UUIDS[0], _privacy()["version"]),
            {"ok": True},
        )
        self.assertTrue(window.terminal_entered.wait(2))
        close_started = time.monotonic()
        close_thread = threading.Thread(target=api._close_window, daemon=True)
        with mock.patch.object(native_shell, "_CHAT_CLOSE_BUDGET_S", 0.1):
            close_thread.start()
            _wait_until(lambda: api._closed)
            self.assertTrue(window.destroyed.wait(1))
            close_thread.join(timeout=1)
        close_elapsed = time.monotonic() - close_started
        self.assertFalse(close_thread.is_alive())
        self.assertGreaterEqual(close_elapsed, 0.08)
        self.assertLess(close_elapsed, 1)
        self.assertFalse(window.terminal_release.is_set())
        window.terminal_release.set()
        _wait_until(lambda: not api._terminal_flush_in_progress)

    def test_retry_rejects_during_terminal_flush(self):
        chat = _ImmediateTerminalChat()
        window = _TerminalBarrierWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")
        version = _privacy()["version"]

        self.assertEqual(api.ask(1, "first", [], _UUIDS[0], version), {"ok": True})
        self.assertTrue(window.terminal_entered.wait(2))
        with api._lifecycle_lock, api._busy_lock:
            api._recovering = True
            api._active_client_turn_id = _UUIDS[0]
            api._active_display_turn_id = 1
            api._last_server_request = (
                1,
                "first",
                [],
                _UUIDS[0],
                version,
            )
        try:
            self.assertEqual(
                api.retry_chat(_UUIDS[0]),
                {"ok": False, "status": "not_found"},
            )
            self.assertFalse(hasattr(api, "delete_chat"))
            self.assertEqual(chat.turns, ["first"])
        finally:
            window.terminal_release.set()
        _wait_until(lambda: not api._terminal_flush_in_progress)

    def test_terminal_batch_is_bounded_to_state_delta_and_one_page_terminal(self):
        chat = _ExcessTerminalDeltaChat()
        window = _RecordingWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")

        self.assertEqual(
            api.ask(1, "bounded", [], _UUIDS[0], _privacy()["version"]),
            {"ok": True},
        )
        self._wait_call(window, "done", lambda args: args == [1, "answer"])
        self.assertEqual(
            [(name, args) for name, args in window.calls() if args and args[0] == 1],
            [
                ("state", [1, "completed", {"turn_count": 1, "remaining_turns": 39}]),
                ("delta", [1, "first"]),
                ("done", [1, "answer"]),
            ],
        )

    def test_non_success_terminal_states_suppress_late_delta(self):
        for state, payload, error_code in (
            ("failed", {"error_code": "model_unavailable"}, "model_unavailable"),
            ("cancelled", {}, "cancelled"),
            ("unavailable", {"error_code": "chat_unavailable"}, "chat_unavailable"),
        ):
            with self.subTest(state=state):
                chat = _NonSuccessTerminalDeltaChat(state, payload, error_code)
                window = _RecordingWindow()
                api = native_shell.PopupApi("", chat=chat, chat_route="server")
                api._win = window
                self.assertTrue(api.chat_ready()["ok"])
                self._wait_call(window, "bootstrap")

                self.assertEqual(
                    api.ask(1, state, [], _UUIDS[0], _privacy()["version"]),
                    {"ok": True},
                )
                self._wait_call(
                    window,
                    "error",
                    lambda args, expected=error_code: args == [1, expected],
                )
                self.assertEqual(
                    [
                        (name, args)
                        for name, args in window.calls()
                        if args and args[0] == 1
                    ],
                    [
                        ("state", [1, state, payload]),
                        ("error", [1, error_code]),
                    ],
                )

    def test_contradictory_terminal_state_and_page_terminal_fail_closed(self):
        for state, payload, callback, callback_first in (
            (
                "completed",
                {"turn_count": 1, "remaining_turns": 39},
                "error",
                False,
            ),
            ("failed", {"error_code": "model_unavailable"}, "done", False),
            (
                "completed",
                {"turn_count": 1, "remaining_turns": 39},
                "error",
                True,
            ),
            ("failed", {"error_code": "model_unavailable"}, "done", True),
        ):
            with self.subTest(
                state=state, callback=callback, callback_first=callback_first
            ):
                chat = _ContradictoryTerminalChat(
                    state, payload, callback, callback_first=callback_first
                )
                window = _RecordingWindow()
                api = native_shell.PopupApi("", chat=chat, chat_route="server")
                api._win = window
                self.assertTrue(api.chat_ready()["ok"])
                self._wait_call(window, "bootstrap")

                self.assertEqual(
                    api.ask(1, state, [], _UUIDS[0], _privacy()["version"]),
                    {"ok": True},
                )
                self._wait_call(
                    window,
                    "error",
                    lambda args: args == [1, "bad_response"],
                )
                self.assertEqual(
                    [
                        (name, args)
                        for name, args in window.calls()
                        if args and args[0] == 1
                    ],
                    [("error", [1, "bad_response"])],
                )

    def test_matching_page_terminal_before_state_preserves_state_payload(self):
        for state, payload, callback, expected_terminal in (
            (
                "completed",
                {"turn_count": 1, "remaining_turns": 39},
                "done",
                ("done", [1, "contradictory answer"]),
            ),
            (
                "failed",
                {"error_code": "model_unavailable"},
                "error",
                ("error", [1, "model_unavailable"]),
            ),
        ):
            with self.subTest(state=state):
                chat = _ContradictoryTerminalChat(
                    state, payload, callback, callback_first=True
                )
                window = _RecordingWindow()
                api = native_shell.PopupApi("", chat=chat, chat_route="server")
                api._win = window
                self.assertTrue(api.chat_ready()["ok"])
                self._wait_call(window, "bootstrap")

                self.assertEqual(
                    api.ask(1, state, [], _UUIDS[0], _privacy()["version"]),
                    {"ok": True},
                )
                _wait_until(lambda instance=api: not instance._busy)
                self.assertEqual(
                    [
                        (name, args)
                        for name, args in window.calls()
                        if args and args[0] == 1
                    ],
                    [("state", [1, state, payload]), expected_terminal],
                )

    def test_malformed_state_after_page_terminal_fails_closed(self):
        chat = _ContradictoryTerminalChat(
            "completed", {"turn_count": "invalid"}, "done", callback_first=True
        )
        window = _RecordingWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")

        self.assertEqual(
            api.ask(1, "malformed", [], _UUIDS[0], _privacy()["version"]),
            {"ok": True},
        )
        _wait_until(lambda: not api._busy)
        self.assertEqual(
            [(name, args) for name, args in window.calls() if args and args[0] == 1],
            [("error", [1, "bad_response"])],
        )

    def test_valid_duplicate_terminal_callbacks_emit_exactly_one_terminal(self):
        for mode in ("error", "exception"):
            with self.subTest(mode=mode):
                chat = _DuplicateTerminalChat(mode)
                window = _RecordingWindow()
                api = native_shell.PopupApi("", chat=chat, chat_route="server")
                api._win = window
                self.assertTrue(api.chat_ready()["ok"])
                self._wait_call(window, "bootstrap")

                self.assertEqual(
                    api.ask(1, "duplicate", [], _UUIDS[0], _privacy()["version"]),
                    {"ok": True},
                )
                self._wait_call(window, "done", lambda args: args == [1, "answer"])
                turn_calls = [
                    (name, args)
                    for name, args in window.calls()
                    if args and args[0] == 1
                ]
                self.assertEqual(
                    turn_calls,
                    [
                        (
                            "state",
                            [
                                1,
                                "completed",
                                {"turn_count": 1, "remaining_turns": 39},
                            ],
                        ),
                        ("delta", [1, "answer"]),
                        ("done", [1, "answer"]),
                    ],
                )
                self.assertEqual(
                    sum(name in {"done", "error"} for name, _args in turn_calls),
                    1,
                )

    def test_terminal_callback_without_terminal_state_releases_recovery_ownership(self):
        for mode, terminal_name, terminal_value in (
            ("done", "done", "recovered answer"),
            ("error", "error", "model_unavailable"),
            ("exception", "error", "chat_unavailable"),
        ):
            with self.subTest(mode=mode):
                chat = _RecoveringTerminalCallbackChat(mode)
                window = _RecordingWindow()
                api = native_shell.PopupApi("", chat=chat, chat_route="server")
                api._win = window
                self.assertTrue(api.chat_ready()["ok"])
                self._wait_call(window, "bootstrap")
                version = _privacy()["version"]

                self.assertEqual(
                    api.ask(1, "first", [], _UUIDS[0], version), {"ok": True}
                )
                self._wait_call(
                    window,
                    terminal_name,
                    lambda args, expected=terminal_value: args == [1, expected],
                )
                _wait_until(lambda admitted_api=api: not admitted_api._busy)
                self.assertFalse(api._recovering)
                self.assertIsNone(api._active_client_turn_id)
                self.assertIsNone(api._active_display_turn_id)
                self.assertIsNone(api._last_server_request)

                self.assertEqual(
                    api.ask(2, "next", [], _UUIDS[1], version), {"ok": True}
                )
                self._wait_call(window, "done", lambda args: args == [2, "answer"])
                self.assertEqual(chat.turns, ["first", "next"])

    def test_generation_change_during_recovery_still_releases_ownership(self):
        chat = _RecoveringGenerationChangeChat()
        window = _RecordingWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")
        version = _privacy()["version"]

        self.assertEqual(api.ask(1, "first", [], _UUIDS[0], version), {"ok": True})
        self.assertTrue(chat.recovering_entered.wait(2))
        with api._lifecycle_lock:
            api._callback_generation += 1
        chat.recovering_release.set()
        _wait_until(lambda: not api._busy)
        self.assertFalse(api._recovering)
        self.assertIsNone(api._active_client_turn_id)
        self.assertIsNone(api._active_display_turn_id)
        self.assertIsNone(api._last_server_request)

        self.assertEqual(api.ask(2, "next", [], _UUIDS[1], version), {"ok": True})
        self._wait_call(window, "done", lambda args: args == [2, "answer"])
        self.assertEqual(chat.turns, ["first", "next"])

    def test_generation_change_suppresses_already_queued_terminal_batch(self):
        chat = _QueuedTerminalGenerationChangeChat()
        window = _RecordingWindow()
        api = native_shell.PopupApi("", chat=chat, chat_route="server")
        api._win = window
        self.assertTrue(api.chat_ready()["ok"])
        self._wait_call(window, "bootstrap")
        version = _privacy()["version"]

        self.assertEqual(api.ask(1, "first", [], _UUIDS[0], version), {"ok": True})
        self.assertTrue(chat.terminal_queued.wait(2))
        with api._lifecycle_lock:
            api._callback_generation += 1
        chat.return_release.set()
        _wait_until(lambda: not api._busy)
        self.assertEqual(
            [(name, args) for name, args in window.calls() if args and args[0] == 1],
            [],
        )

        self.assertEqual(api.ask(2, "next", [], _UUIDS[1], version), {"ok": True})
        self._wait_call(window, "done", lambda args: args == [2, "answer"])
        self.assertEqual(chat.turns, ["first", "next"])

    def test_real_http_session_native_and_ui_complete_failure_and_server_cancelled_flow(self):
        token = _runtime_token()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            _SyntheticHub(token) as hub,
            _ProxyTrap() as proxy,
            _only_connections_to(hub.server.server_address) as socket_attempts,
            mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": proxy.url,
                    "HTTPS_PROXY": proxy.url,
                    "ALL_PROXY": proxy.url,
                    "NO_PROXY": "",
                },
                clear=False,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                chat_backend,
                "_spawn_claude",
                side_effect=AssertionError("CLI forbidden"),
            ) as claude_spawn,
            mock.patch.object(
                chat_backend,
                "_spawn_codex",
                side_effect=AssertionError("CLI forbidden"),
            ) as codex_spawn,
            mock.patch.object(
                chat_backend,
                "_spawn_cursor",
                side_effect=AssertionError("CLI forbidden"),
            ) as cursor_spawn,
        ):
            with socket.socket() as loopback_probe:
                self.assertEqual(
                    loopback_probe.connect_ex(hub.server.server_address),
                    0,
                    "the direct connect_ex loopback proof failed",
                )
            chat = self._new_session(hub, token)
            window = _RecordingWindow()
            api = native_shell.PopupApi("", chat=chat, chat_route="server")
            api._win = window

            self.assertEqual(
                api.chat_ready(), {"ok": True, "state": "capability_checking"}
            )
            bootstrap_args = self._wait_call(
                window,
                "bootstrap",
                lambda args: args[0].get("ok") is True,
            )
            bootstrap = bootstrap_args[0]
            self.assertEqual(
                bootstrap["history"],
                [
                    {
                        key: value
                        for key, value in _history_turn(turn_no).items()
                        if key != "wire_only_field"
                    }
                    for turn_no in (1, 2)
                ],
            )
            self.assertNotIn("balance_credits", bootstrap["conversation"])
            privacy_version = bootstrap["privacy_disclosure"]["version"]

            self.assertEqual(
                api.ask(1, "replay zero", [], _UUIDS[0], privacy_version),
                {"ok": True},
            )
            self._wait_call(window, "done", lambda args: args[0] == 1)

            self.assertEqual(
                api.ask(2, "positive price", [], _UUIDS[1], privacy_version),
                {"ok": True},
            )
            self._wait_call(window, "done", lambda args: args[0] == 2)

            self.assertEqual(
                api.ask(3, "candidate failure", [], _UUIDS[2], privacy_version),
                {"ok": True},
            )
            self._wait_call(
                window,
                "error",
                lambda args: args == [3, "model_unavailable"],
            )

            self.assertEqual(
                api.ask(4, "server cancelled", [], _UUIDS[3], privacy_version),
                {"ok": True},
            )
            self._wait_call(
                window,
                "state",
                lambda args: args[0] == 4 and args[1] == "running",
            )
            self._wait_call(
                window,
                "error",
                lambda args: args == [4, "cancelled"],
            )

            self.assertFalse(hasattr(api, "delete_chat"))
            self.assertEqual(api.chat_closing(), {"ok": True})

            calls = window.calls()
            states = [args[1] for name, args in calls if name == "state"]
            for expected in (
                "ready",
                "submitting",
                "queued",
                "running",
                "calling_model",
                "completed",
                "failed",
                "cancelled",
            ):
                self.assertIn(expected, states)
            turn_states = {
                turn_id: [
                    args[1]
                    for name, args in calls
                    if name == "state" and args[0] == turn_id
                ]
                for turn_id in range(1, 5)
            }
            self.assertEqual(
                {turn_id: turn_states[turn_id] for turn_id in range(1, 4)},
                {
                    1: [
                        "submitting",
                        "queued",
                        "running",
                        "calling_model",
                        "completed",
                    ],
                    2: [
                        "submitting",
                        "queued",
                        "running",
                        "calling_model",
                        "completed",
                    ],
                    3: ["submitting", "queued", "calling_model", "failed"],
                },
            )
            self.assertEqual(turn_states[4][:3], ["submitting", "queued", "running"])
            self.assertEqual(turn_states[4][-1], "cancelled")
            self.assertEqual(turn_states[4], ["submitting", "queued", "running", "cancelled"])
            turn_calls = {
                turn_id: [
                    (name, args) for name, args in calls if args and args[0] == turn_id
                ]
                for turn_id in range(1, 5)
            }

            def assert_completed_fill(turn_id, price, turn_count, answer):
                events = turn_calls[turn_id]
                self.assertEqual(
                    events[:4],
                    [
                        ("state", [turn_id, "submitting", {}]),
                        ("state", [turn_id, "queued", {"price_credits": price}]),
                        ("state", [turn_id, "running", {"price_credits": price}]),
                        ("state", [turn_id, "calling_model", {"price_credits": price}]),
                    ],
                )
                self.assertEqual(
                    events[-2:],
                    [
                        (
                            "state",
                            [
                                turn_id,
                                "completed",
                                {"turn_count": turn_count, "remaining_turns": 35},
                            ],
                        ),
                        ("done", [turn_id, answer]),
                    ],
                )
                deltas = [args[1] for name, args in events[4:-2] if name == "delta"]
                self.assertEqual(len(deltas), len(events[4:-2]))
                self.assertGreater(len(deltas), 1)
                self.assertLessEqual(len(deltas), 24)
                self.assertEqual(deltas[-1], answer)
                for index in range(1, len(deltas)):
                    previous, current = deltas[index - 1], deltas[index]
                    self.assertTrue(current.startswith(previous))
                    self.assertGreater(len(current), len(previous))

            assert_completed_fill(1, 0, 3, "answer for replay zero")
            assert_completed_fill(2, 9, 4, "answer for positive price")
            self.assertEqual(
                turn_calls[3],
                [
                    ("state", [3, "submitting", {}]),
                    ("state", [3, "queued", {"price_credits": 0}]),
                    ("state", [3, "calling_model", {"price_credits": 0}]),
                    (
                        "state",
                        [3, "failed", {"error_code": "model_unavailable"}],
                    ),
                    ("error", [3, "model_unavailable"]),
                ],
            )
            self.assertEqual(
                turn_calls[4][:3],
                [
                    ("state", [4, "submitting", {}]),
                    ("state", [4, "queued", {"price_credits": 0}]),
                    ("state", [4, "running", {"price_credits": 0}]),
                ],
            )
            self.assertEqual(
                turn_calls[4][-2:],
                [
                    ("state", [4, "cancelled", {}]),
                    ("error", [4, "cancelled"]),
                ],
            )
            prices = [
                args[2]["price_credits"]
                for name, args in calls
                if name == "state"
                and args[0] is not None
                and "price_credits" in args[2]
            ]
            self.assertIn(
                0, prices, "zero price must pass even when server balance is zero"
            )
            self.assertIn(9, prices)

            replay_requests = [
                request
                for request in hub.state.snapshot()
                if request["headers"].get("Idempotency-Key") == _UUIDS[0]
            ]
            self.assertEqual(len(replay_requests), 2)
            self.assertEqual(
                [
                    (item["headers"]["Idempotency-Key"], item["body"])
                    for item in replay_requests
                ],
                [(_UUIDS[0], {"text": "replay zero", "images": []})] * 2,
            )

            requests = hub.state.snapshot()
            self.assertTrue(requests)
            self.assertNotIn("DELETE", {request["method"] for request in requests})
            self.assertFalse(
                any(request["path"].endswith("/cancel") for request in requests),
                "no user-initiated cancel request should be sent",
            )
            history_cursors = [
                parse_qs(urlsplit(request["path"]).query)["after_turn_no"]
                for request in requests
                if urlsplit(request["path"]).path
                == "/v1/ge/chat/conversations/conv_e2e/turns"
                and request["method"] == "GET"
            ]
            self.assertEqual(history_cursors, [["0"], ["1"]])
            for request in requests:
                self.assertTrue(
                    request["headers"].get("Authorization") == "Bearer " + token,
                    "synthetic request authorization did not match",
                )
                non_authorization_headers = {
                    key: value
                    for key, value in request["headers"].items()
                    if key.lower() != "authorization"
                }
                self.assertFalse(
                    token in json.dumps(non_authorization_headers, sort_keys=True),
                    "credential leaked to a non-Authorization request header",
                )
                self.assertFalse(
                    token in request["path"],
                    "credential leaked to a synthetic request path",
                )
                self.assertFalse(
                    _contains_key(request["body"], _FORBIDDEN_REQUEST_KEYS)
                )
                if request["body"] is not None:
                    self.assertFalse(
                        token in json.dumps(request["body"], sort_keys=True),
                        "credential leaked to a synthetic request body",
                    )
            create_bodies = [
                item["body"]
                for item in requests
                if item["method"] == "POST"
                and item["path"] == "/v1/ge/chat/conversations"
            ]
            self.assertEqual(create_bodies, [{"run_id": "run_synthetic_e2e"}])
            submit_bodies = [
                item["body"]
                for item in requests
                if item["method"] == "POST" and item["path"].endswith("/turns")
            ]
            self.assertTrue(
                all(set(body) == {"text", "images"} for body in submit_bodies)
            )

            rendered = launcher.render_artifact_html(
                launcher.PopupSpec(
                    kind="ge",
                    title="Synthetic GE",
                    artifact={
                        "kind": "svg",
                        "data": '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
                    },
                )
            )
            ui_scenario = """
                window.localStorage = {length: 0}; window.sessionStorage = {length: 0};
                const boot = __BOOTSTRAP__;
                const sent = window.geChat.bootstrap(boot) &&
                  window.geChat.submit('zero-priced UI turn', [], null);
                window.geChat.state(1, 'queued', {price_credits: 0});
                window.geChat.state(1, 'running', {price_credits: 0});
                window.geChat.state(1, 'calling_model', {price_credits: 0});
                window.geChat.state(1, 'completed', {turn_count: 2, remaining_turns: 38});
                window.geChat.done(1, 'UI answer');
                finish({sent, calls, dom: Object.values(nodes).map((node) => node.textContent).join('|'),
                  localStorageLength: window.localStorage.length,
                  sessionStorageLength: window.sessionStorage.length});
                """
            ui = _run_js(ui_scenario.replace("__BOOTSTRAP__", json.dumps(bootstrap)))
            self.assertTrue(ui["sent"])
            self.assertIn("Free", ui["dom"])
            self.assertEqual(ui["localStorageLength"], 0)
            self.assertEqual(ui["sessionStorageLength"], 0)

            credential_surfaces = {
                "rendered HTML": rendered,
                "chat JavaScript": launcher._GE_CHAT_JS,
                "bootstrap": json.dumps(bootstrap, sort_keys=True),
                "native scripts": json.dumps(window.scripts, sort_keys=True),
                "decoded callbacks": json.dumps(calls, sort_keys=True),
                "UI result": json.dumps(ui, sort_keys=True),
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
                "HTTP log": "\n".join(hub.state.log_lines),
                "process argv": "\n".join(sys.argv),
            }
            leaking_surfaces = [
                label
                for label, surface in credential_surfaces.items()
                if token in surface
            ]
            if any(token in key or token in value for key, value in os.environ.items()):
                leaking_surfaces.append("environment")
            self.assertEqual(
                leaking_surfaces,
                [],
                "credential leaked to named surface(s): " + ", ".join(leaking_surfaces),
            )
            callbacks_json = json.dumps(calls)
            self.assertFalse(
                "internal-provider-marker" in callbacks_json,
                "internal provider marker leaked to native callbacks",
            )
            self.assertFalse(
                "internal-model-marker" in callbacks_json,
                "internal model marker leaked to native callbacks",
            )
            expected_loopback = (
                hub.server.server_address[0],
                int(hub.server.server_address[1]),
            )
            for surface in ("create_connection", "connect", "connect_ex"):
                self.assertIn((surface, expected_loopback), socket_attempts)
            self.assertEqual(
                [
                    (surface, destination)
                    for surface, destination in socket_attempts
                    if surface != "socketpair_connect"
                    and destination != expected_loopback
                ],
                [],
                "a non-runtime socket attempted an unexpected loopback destination",
            )
            self.assertEqual(proxy.hits, [])
            claude_spawn.assert_not_called()
            codex_spawn.assert_not_called()
            cursor_spawn.assert_not_called()

    def test_shim_side_parent_exits_and_detached_child_continues_from_stdin_only(self):
        token = _runtime_token()
        with _SyntheticHub(token) as hub, tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            child_result = temp_root / "child-result.json"
            child_pid_path = temp_root / "child.pid"
            child_ack_path = temp_root / "child-identity-claimed"
            popup_root = temp_root / "popups"
            child_code = r"""
import asyncio, ctypes, json, os, sys, threading, time
from pathlib import Path
from client.popup import session as popup_session
from client.popup.http_chat import HttpChatSession

pid_path = Path(sys.argv[3])
pid_temp = pid_path.with_suffix('.tmp')
pid_temp.write_text(str(os.getpid()), encoding='ascii')
os.replace(pid_temp, pid_path)

def watchdog():
    time.sleep(15)
    os._exit(124)

threading.Thread(target=watchdog, daemon=True).start()
ack_path = Path(sys.argv[4])
ack_deadline = time.monotonic() + 5
while not ack_path.exists() and time.monotonic() < ack_deadline:
    time.sleep(0.01)
if not ack_path.exists():
    raise RuntimeError('process identity was not claimed')

def parent_alive(pid):
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

raw = sys.stdin.buffer.read(popup_session._MAX_CHAT_BRIDGE_BYTES + 1)
envelope = popup_session._validate_chat_bridge_envelope(json.loads(raw.decode("utf-8")))
token = envelope["device_token"]
parent_pid = int(sys.argv[2])
deadline = time.monotonic() + 5
while parent_alive(parent_pid) and time.monotonic() < deadline:
    time.sleep(0.01)
parent_exited = not parent_alive(parent_pid)
chat = HttpChatSession(
    endpoint=envelope["endpoint"], token=token, run_id=envelope["run_id"],
    sleep=lambda seconds: time.sleep(min(seconds, 0.01)), rng=lambda: 0.0,
)
bootstrap = chat.bootstrap()
states, done, errors = [], [], []
asyncio.run(chat.run_turn(
    "detached continuation", [], lambda value: None, done.append, errors.append, lambda value: None,
    client_turn_id="123e4567-e89b-42d3-a456-426614174000",
    privacy_version=bootstrap["privacy_disclosure"]["version"],
    on_state=lambda state, payload: states.append([state, payload]),
))
chat.close()
result_path = Path(sys.argv[1])
temp_result = result_path.with_suffix(".tmp")
temp_result.write_text(json.dumps({
    "bootstrap": bootstrap, "states": states, "done": done, "errors": errors,
    "parent_exited_before_bootstrap": parent_exited,
    "token_in_argv": any(token in value for value in sys.argv),
    "token_in_env": any(token in value for value in os.environ.values()),
}), encoding="utf-8")
os.replace(temp_result, result_path)
"""
            child_command = [
                sys.executable,
                "-c",
                child_code,
                str(child_result),
            ]
            parent_code = "\n".join(
                (
                    "import json,os,sys",
                    "from pathlib import Path",
                    "from unittest import mock",
                    "from client.popup import backend,launcher,session",
                    f"session._POPUP_ROOT=Path({str(popup_root)!r})",
                    "token=sys.stdin.read()",
                    f"child_command={child_command!r}",
                    "child_command.append(str(os.getpid()))",
                    f"child_command.append({str(child_pid_path)!r})",
                    f"child_command.append({str(child_ack_path)!r})",
                    "with mock.patch.object(backend,'ensure_webview',return_value=True), mock.patch.object(launcher,'build_shell_command',return_value=child_command):",
                    "    outcome=session.spawn('<html>safe</html>','Synthetic',chat_bootstrap={'schema_version':1,'kind':'ge-chat','route':'server','endpoint':sys.argv[1],'device_token':token,'run_id':'run_detached'})",
                    "print(json.dumps(outcome))",
                )
            )
            parent_command = [sys.executable, "-c", parent_code, hub.endpoint]
            self.assertFalse(
                token in "\n".join(parent_command),
                "credential leaked to detached parent argv",
            )
            self.assertFalse(
                token.rsplit("-", 1)[-1] in "\n".join(parent_command),
                "credential marker leaked to detached parent argv",
            )
            self.assertFalse(
                token in "\n".join(child_command),
                "credential leaked to detached child argv",
            )
            self.assertFalse(
                token.rsplit("-", 1)[-1] in "\n".join(child_command),
                "credential marker leaked to detached child argv",
            )
            completed = subprocess.run(
                parent_command,
                cwd=Path(__file__).resolve().parents[2],
                input=token,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, "detached parent process failed")
            self.assertFalse(
                token in completed.stdout,
                "credential leaked to detached parent stdout",
            )
            self.assertFalse(
                token in completed.stderr,
                "credential leaked to detached parent stderr",
            )
            self.assertEqual(json.loads(completed.stdout)["status"], "open")
            del completed
            gc.collect()
            _wait_until(child_pid_path.exists, timeout=5)
            child_pid = int(child_pid_path.read_text(encoding="ascii"))
            self.assertGreater(child_pid, 0)
            self.assertNotEqual(child_pid, os.getpid())
            child_identity = _open_process_identity(child_pid)
            if os.name == "nt" or callable(getattr(os, "pidfd_open", None)):
                self.assertIsNotNone(
                    child_identity,
                    "detached child identity could not be held before acknowledgement",
                )
            ack_temp = child_ack_path.with_suffix(".tmp")
            ack_temp.write_text("claimed", encoding="ascii")
            os.replace(ack_temp, child_ack_path)
            try:
                _wait_until(child_result.exists, timeout=10)
            finally:
                try:
                    child_exited = _reap_process(child_pid, child_identity)
                finally:
                    _close_process_identity(child_identity)
            self.assertTrue(child_exited, "detached child could not be reaped")
            child = json.loads(child_result.read_text(encoding="utf-8"))
            self.assertTrue(child["bootstrap"]["ok"])
            self.assertTrue(child["parent_exited_before_bootstrap"])
            self.assertIn("completed", [state for state, _payload in child["states"]])
            self.assertEqual(child["done"], ["answer for detached continuation"])
            self.assertEqual(child["errors"], [])
            self.assertFalse(child["token_in_argv"])
            self.assertFalse(child["token_in_env"])
            for path in temp_root.rglob("*"):
                if path.is_file():
                    self.assertFalse(
                        token.encode() in path.read_bytes(),
                        "credential leaked to a detached-flow output file",
                    )

    def test_detached_cleanup_never_terminates_by_unverified_numeric_pid(self):
        with mock.patch(
            __name__ + "._process_alive", return_value=True
        ) as process_alive:
            self.assertFalse(_reap_process(321, None, timeout=0))
        process_alive.assert_called_once_with(321)

        pidfd_open = mock.Mock(return_value=41)
        self.assertEqual(
            _open_process_identity(321, platform_name="posix", pidfd_opener=pidfd_open),
            ("pidfd", 41),
        )
        pidfd_open.assert_called_once_with(321, 0)

    def test_platform_detach_conditionals_cover_windows_and_simulated_posix(self):
        windows_os = mock.Mock(name="windows_os")
        windows_os.name = "nt"
        with mock.patch.dict(
            popup_session._detach_kwargs.__globals__, {"os": windows_os}
        ):
            self.assertEqual(
                popup_session._detach_kwargs(),
                {"creationflags": popup_session._WIN_DETACHED},
            )
        posix_os = mock.Mock(name="posix_os")
        posix_os.name = "posix"
        with mock.patch.dict(
            popup_session._detach_kwargs.__globals__, {"os": posix_os}
        ):
            self.assertEqual(
                popup_session._detach_kwargs(), {"start_new_session": True}
            )


if __name__ == "__main__":
    unittest.main()
