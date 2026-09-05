"""Server-backed GE popup chat session.

This module is the native-only HTTP boundary.  It deliberately has no GUI or
JavaScript dependency; the device token is retained only on the session object
and is added to authenticated requests here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit

from client import i18n
from client.http_safety import NoRedirect, read_within_budget
from client.version import USER_AGENT

_C0 = frozenset(chr(value) for value in range(32)) | {chr(127)}
_RESPONSE_BUDGET_S = 15.0
_SMALL_RESPONSE_BYTES = 256 * 1024
_PRIVACY_VERSION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DATA_URL = re.compile(
    r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})$"
)
_IMAGE_MEDIA = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_DATA_URL_CHARS = 5_592_500
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_IMAGE_TOTAL_BYTES = 12 * 1024 * 1024
_MAX_SUBMIT_JSON_BYTES = 20 * 1024 * 1024
_MAX_HISTORY_BYTES = 32 * 1024 * 1024
_MAX_TERMINAL_BYTES = 16 * 1024 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_TURN_RESPONSE_BYTES = 2 * 1024 * 1024
_WHOLE_TURN_S = 330.0
_CLOSE_BUDGET_S = 3.0
_SUBMIT_ATTEMPT_BUDGET_S = 120.0 + _RESPONSE_BUDGET_S
_POLL_ATTEMPT_BUDGET_S = 10.0 + _RESPONSE_BUDGET_S
_MAX_CANCEL_ATTEMPTS = 3
_ANSWER_FILL_MAX_STEPS = 24
_ANSWER_FILL_INTERVAL_S = 0.016
_STABLE_CODES = frozenset(
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
_ACTIVE_STATUS_PHASES = {
    "queued": frozenset({"queued"}),
    "running": frozenset({"compacting", "calling_model", "finalizing"}),
}

_MESSAGES = {
    "auth_required": "Authentication is required. Reopen this chat from the host.",
    "bad_response": "The chat service returned an invalid response.",
    "capability_disabled": "Server chat is not available.",
    "chat_unavailable": "Chat is unavailable.",
    "client_unsupported": "This client does not support popup chat.",
    "conversation_busy": "The conversation is busy.",
    "conversation_expired": "The conversation has expired.",
    "insufficient_credits": "There are not enough credits for this turn.",
    "input_too_large": "The chat input is too large.",
    "invalid_request": "The chat request is invalid.",
    "model_unavailable": "The chat model is unavailable.",
    "network_error": "The chat service could not be reached.",
    "not_found": "The chat resource was not found.",
    "privacy_confirmation_required": "Review and confirm the current privacy disclosure.",
    "rate_limited": "Too many chat requests were made.",
    "server_unsupported": "This server does not support popup chat.",
    "timeout": "The chat request timed out.",
    "turn_in_flight": "Another turn is already running.",
    "cancelled": "The chat turn was cancelled.",
}


def _error(
    code: str,
    *,
    retryable: bool = False,
    http_status: int | None = None,
) -> ChatClientError:
    return ChatClientError(
        code,
        _MESSAGES.get(code, _MESSAGES["chat_unavailable"]),
        retryable=retryable,
        http_status=http_status,
    )


def _has_control(value: str) -> bool:
    return any(char in _C0 for char in value)


def _bounded_string(value: object, *, max_bytes: int) -> str:
    if not isinstance(value, str) or not value or _has_control(value):
        raise _error("invalid_request")
    encode_failed = False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encode_failed = True
        encoded = b""
    if encode_failed:
        raise _error("invalid_request")
    if len(encoded) > max_bytes:
        raise _error("invalid_request")
    return value


def _wire_string(
    value: object,
    *,
    max_chars: int,
    max_bytes: int,
    allow_empty: bool = False,
    allow_controls: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or (not allow_controls and _has_control(value))
    ):
        raise _error("bad_response")
    if len(value) > max_chars:
        raise _error("bad_response")
    encode_failed = False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encode_failed = True
        encoded = b""
    if encode_failed:
        raise _error("bad_response")
    if len(encoded) > max_bytes:
        raise _error("bad_response")
    return value


def _nonnegative_int(value: object, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error("bad_response")
    if maximum is not None and value > maximum:
        raise _error("bad_response")
    return value


def _path_segment(value: object) -> str:
    clean = _wire_string(value, max_chars=256, max_bytes=1024)
    if clean in {".", ".."} or any(char in clean for char in "/\\%?#"):
        raise _error("bad_response")
    return quote(clean, safe="-._~")


def _validate_privacy(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "providers"}:
        raise _error("bad_response")
    version = value["version"]
    providers = value["providers"]
    if not isinstance(version, str) or not _PRIVACY_VERSION.fullmatch(version):
        raise _error("bad_response")
    if not isinstance(providers, list) or not 1 <= len(providers) <= 16:
        raise _error("bad_response")
    clean = []
    required = {
        "category",
        "data_region",
        "training_enabled",
        "cache_ttl_seconds",
        "provider_retention_hours",
        "deletion_scope",
    }
    for provider in providers:
        if not isinstance(provider, dict) or not required.issubset(provider):
            raise _error("bad_response")
        training = provider["training_enabled"]
        if training is not False and training is not None:
            raise _error("bad_response")
        clean.append(
            {
                "category": _wire_string(
                    provider["category"], max_chars=256, max_bytes=1024
                ),
                "data_region": _wire_string(
                    provider["data_region"], max_chars=256, max_bytes=1024
                ),
                "training_enabled": training,
                "cache_ttl_seconds": _nonnegative_int(
                    provider["cache_ttl_seconds"], maximum=2_147_483_647
                ),
                "provider_retention_hours": _nonnegative_int(
                    provider["provider_retention_hours"], maximum=2_147_483_647
                ),
                "deletion_scope": _wire_string(
                    provider["deletion_scope"], max_chars=256, max_bytes=1024
                ),
            }
        )
    return {"version": version, "providers": clean}


def _validate_history_turn(value: object, *, previous_turn_no: int) -> dict:
    required = {
        "turn_no",
        "status",
        "user_text",
        "completed_at",
        "images",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise _error("bad_response")
    turn_no = _nonnegative_int(value["turn_no"], maximum=_MAX_SAFE_INTEGER)
    if turn_no < 1 or turn_no <= previous_turn_no:
        raise _error("bad_response")
    status = value["status"]
    if status not in {"completed", "failed", "cancelled"}:
        raise _error("bad_response")
    user_text = _wire_string(
        value["user_text"], max_chars=20_000, max_bytes=80_000, allow_controls=True
    )
    assistant_text = value.get("assistant_text")
    error_code = value.get("error_code")
    if status == "completed":
        assistant_text = _wire_string(
            assistant_text,
            max_chars=200_000,
            max_bytes=800_000,
            allow_controls=True,
        )
        if error_code is not None:
            raise _error("bad_response")
    else:
        if assistant_text is not None:
            raise _error("bad_response")
        if not isinstance(error_code, str) or error_code not in _STABLE_CODES:
            raise _error("bad_response")
        if (status == "cancelled") != (error_code == "cancelled"):
            raise _error("bad_response")
    completed_at = _nonnegative_int(
        value["completed_at"], maximum=_MAX_SAFE_INTEGER
    )
    images = value["images"]
    if not isinstance(images, list) or len(images) > 5:
        raise _error("bad_response")
    clean_images = []
    image_required = {"image_index", "media_type", "size_bytes", "sha256"}
    for expected_index, image in enumerate(images):
        if not isinstance(image, dict) or not image_required.issubset(image):
            raise _error("bad_response")
        image_index = _nonnegative_int(image["image_index"])
        if image_index != expected_index:
            raise _error("bad_response")
        media_type = image["media_type"]
        digest = image["sha256"]
        if media_type not in _IMAGE_MEDIA:
            raise _error("bad_response")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise _error("bad_response")
        clean_images.append(
            {
                "image_index": expected_index,
                "media_type": media_type,
                "size_bytes": _nonnegative_int(
                    image["size_bytes"], maximum=_MAX_SAFE_INTEGER
                ),
                "sha256": digest,
            }
        )
    return {
        "turn_no": turn_no,
        "status": status,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "error_code": error_code,
        "completed_at": completed_at,
        "images": clean_images,
    }


def _valid_magic(media_type: str, payload: bytes) -> bool:
    if media_type == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return payload.startswith(b"\xff\xd8\xff")
    return (
        media_type == "image/webp"
        and len(payload) >= 12
        and payload[:4] == b"RIFF"
        and payload[8:12] == b"WEBP"
    )


def _validate_turn_input(text: object, images: object, key: object) -> tuple[str, list[str], str]:
    if not isinstance(key, str) or not _UUID4.fullmatch(key):
        raise _error("invalid_request")
    if not isinstance(text, str) or not 1 <= len(text) <= 20_000:
        raise _error("invalid_request")
    invalid_text = False
    try:
        text_bytes = text.encode("utf-8")
    except UnicodeEncodeError:
        invalid_text = True
        text_bytes = b""
    if invalid_text:
        raise _error("invalid_request")
    if len(text_bytes) > 80_000:
        raise _error("input_too_large")
    if not isinstance(images, list) or len(images) > 5:
        raise _error("invalid_request" if not isinstance(images, list) else "input_too_large")
    total = 0
    clean = []
    seen = set()
    duplicate = False
    for data_url in images:
        if not isinstance(data_url, str) or len(data_url) > _MAX_DATA_URL_CHARS:
            raise _error("input_too_large" if isinstance(data_url, str) else "invalid_request")
        invalid_ascii = False
        try:
            data_url.encode("ascii")
        except UnicodeEncodeError:
            invalid_ascii = True
        if invalid_ascii:
            raise _error("invalid_request")
        match = _DATA_URL.fullmatch(data_url)
        if not match:
            raise _error("invalid_request")
        invalid_base64 = False
        try:
            payload = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error):
            invalid_base64 = True
            payload = b""
        if invalid_base64:
            raise _error("invalid_request")
        if len(payload) > _MAX_IMAGE_BYTES:
            raise _error("input_too_large")
        total += len(payload)
        if total > _MAX_IMAGE_TOTAL_BYTES:
            raise _error("input_too_large")
        if not _valid_magic(match.group(1), payload):
            raise _error("invalid_request")
        identity = hashlib.sha256(payload).digest()
        if identity in seen:
            duplicate = True
        else:
            seen.add(identity)
        clean.append(
            f"data:{match.group(1)};base64,"
            + base64.b64encode(payload).decode("ascii")
        )
    invalid_json = False
    try:
        request_size = len(
            json.dumps(
                {"text": text, "images": clean},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (UnicodeEncodeError, ValueError):
        invalid_json = True
        request_size = 0
    if invalid_json:
        raise _error("invalid_request")
    if request_size > _MAX_SUBMIT_JSON_BYTES:
        raise _error("input_too_large")
    if duplicate:
        raise _error("invalid_request")
    return text, clean, key


def _payload_digest(text: str, images: list[str]) -> bytes:
    digest = hashlib.sha256()
    for value in (text, *images):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def _history_turn_size(turn: dict) -> int:
    size = 256
    for key in ("user_text", "assistant_text", "error_code"):
        value = turn[key]
        if isinstance(value, str):
            size += len(value.encode("utf-8"))
    for image in turn["images"]:
        size += 96
        size += len(image["media_type"].encode("utf-8"))
        size += len(image["sha256"])
    return size


def _terminal_size(terminal: dict) -> int:
    size = 256
    for value in terminal.values():
        if isinstance(value, str):
            size += len(value.encode("utf-8"))
    return size


@dataclass
class _TurnRecord:
    key: str
    text: str
    images: list[str]
    payload_digest: bytes
    started_at: float
    turn_code: str | None = None
    price_credits: int | None = None
    pending_cancel: bool = False
    cancel_sent: bool = False
    cancel_in_flight: bool = False
    cancel_attempts: int = 0
    last_cancel_error: str | None = None
    terminal_size: int = 0
    running: bool = True
    poll_after_ms: int = 250
    orphan_recovery_started: bool = False
    orphan_history_delivered: bool = False
    terminal: dict | None = None
    attempt_done: threading.Event = field(default_factory=threading.Event)
    handle_ready: threading.Event = field(default_factory=threading.Event)
    cancel_attempt_done: threading.Event = field(default_factory=threading.Event)


def _validated_endpoint(endpoint: object) -> str:
    value = _bounded_string(endpoint, max_bytes=2048)
    if "?" in value or "#" in value:
        raise _error("invalid_request")
    parse_failed = False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        parse_failed = True
        parsed = None
        port = None
    if parse_failed:
        raise _error("invalid_request")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise _error("invalid_request")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"}:
        raise _error("invalid_request")
    if port is not None and not 1 <= port <= 65535:
        raise _error("invalid_request")
    return value.rstrip("/")


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    body: bytes


class _TransportFailure(Exception):
    def __init__(self, code: str, *, retryable: bool, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


class _BodyReadFailure(Exception):
    def __init__(self, reason: str):
        super().__init__("bounded response read failed")
        self.timed_out = "budget" in reason


class _DirectTransport:
    """Authenticated urllib transport with no ambient proxies and no redirects."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
        )

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_s: float,
        max_response_bytes: int,
        response_budget_s: float,
    ) -> _HttpResponse:
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        response = None
        try:
            try:
                response = self._opener.open(request, timeout=timeout_s)
            except urllib.error.HTTPError as exc:
                response = exc
            except TimeoutError:
                raise _TransportFailure("timeout", retryable=True) from None
            except (OSError, urllib.error.URLError, ValueError):
                raise _TransportFailure("network_error", retryable=True) from None
            status = response.getcode()
            try:
                payload = read_within_budget(
                    response,
                    max_bytes=max_response_bytes,
                    budget_s=response_budget_s,
                    error=_BodyReadFailure,
                    allow_chunked=True,
                )
            except _BodyReadFailure as exc:
                code = "timeout" if exc.timed_out else "bad_response"
                raise _TransportFailure(
                    code,
                    retryable=exc.timed_out,
                    http_status=status,
                ) from None
            except (OSError, ValueError):
                raise _TransportFailure(
                    "network_error", retryable=True, http_status=status
                ) from None
            return _HttpResponse(status=status, body=payload)
        finally:
            if response is not None:
                response.close()

    def close(self) -> None:
        return None

class ChatClientError(RuntimeError):
    """Stable, UI-safe failure from the GE chat client."""

    def __init__(
        self,
        code: str,
        user_message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.retryable = retryable
        self.http_status = http_status


class HttpChatSession:
    """One long-lived GE chat conversation over the Decision Engine API."""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        run_id: str,
        language: str | None = None,
        transport=None,
        clock=None,
        sleep=None,
        rng=None,
    ) -> None:
        self._endpoint = _validated_endpoint(endpoint)
        self._token = _bounded_string(token, max_bytes=8192)
        self._run_id = _bounded_string(run_id, max_bytes=256)
        self._language = language
        self._transport = transport if transport is not None else _DirectTransport()
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._rng = rng if rng is not None else random.Random()
        self._lock = threading.Lock()
        self._conversation_code: str | None = None
        self._history: list[dict] = []
        self._history_bytes = 0
        self._privacy_disclosure: dict | None = None
        self._active: _TurnRecord | None = None
        self._terminal_records: dict[str, _TurnRecord] = {}
        self._terminal_record_bytes = 0
        self._closed = False
        self._closing = False
        self._unavailable_code: str | None = None

    def _coerce_response(self, value: object, *, max_bytes: int) -> _HttpResponse:
        if isinstance(value, _HttpResponse):
            response = value
        elif isinstance(value, tuple) and len(value) == 2:
            response = _HttpResponse(status=value[0], body=value[1])
        else:
            invalid_response = False
            try:
                response = _HttpResponse(
                    status=value.status,
                    body=value.body,
                )
            except (AttributeError, TypeError):
                invalid_response = True
                response = _HttpResponse(status=0, body=b"")
            if invalid_response:
                raise _error("bad_response")
        if isinstance(response.status, bool) or not isinstance(response.status, int):
            raise _error("bad_response")
        if not isinstance(response.body, bytes) or len(response.body) > max_bytes:
            raise _error("bad_response")
        return response

    def _perform(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotency_key: str | None = None,
        timeout_s: float = 10.0,
        max_response_bytes: int = _SMALL_RESPONSE_BYTES,
    ) -> _HttpResponse:
        if self._closed:
            raise _error("chat_unavailable")
        encoded = None
        headers = {
            "Accept": "application/json",
            "Accept-Language": i18n.accept_language(),
            "Authorization": "Bearer " + self._token,
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            invalid_body = False
            try:
                encoded = json.dumps(
                    body,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError):
                invalid_body = True
            if invalid_body:
                raise _error("invalid_request")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request_method = getattr(self._transport, "request", None)
        if not callable(request_method):
            raise _error("chat_unavailable")
        transport_error = None
        try:
            response = request_method(
                method=method,
                url=self._endpoint + path,
                headers=headers,
                body=encoded,
                timeout_s=timeout_s,
                max_response_bytes=max_response_bytes,
                response_budget_s=_RESPONSE_BUDGET_S,
            )
        except _TransportFailure as exc:
            transport_error = _error(
                exc.code,
                retryable=exc.retryable,
                http_status=exc.http_status,
            )
        except TimeoutError:
            transport_error = _error("timeout", retryable=True)
        except Exception:  # noqa: BLE001  # aqg: top-level boundary — normalize untrusted transport failures
            transport_error = _error("network_error", retryable=True)
        if transport_error is not None:
            raise transport_error
        return self._coerce_response(response, max_bytes=max_response_bytes)

    @staticmethod
    def _http_error(response: _HttpResponse, *, capability: bool = False) -> ChatClientError:
        status = response.status
        if 300 <= status < 400:
            return _error("bad_response", http_status=status)
        if capability and status == 404:
            return _error("server_unsupported", http_status=status)
        try:
            error_value = json.loads(response.body.decode("utf-8"))
            server_code = error_value.get("error_code") if isinstance(error_value, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            server_code = None
        if isinstance(server_code, str) and server_code in _STABLE_CODES:
            return _error(
                server_code,
                retryable=server_code in {"network_error", "rate_limited", "timeout"},
                http_status=status,
            )
        if status in {401, 403}:
            return _error("auth_required", http_status=status)
        if status == 404:
            code = "server_unsupported" if capability else "not_found"
            return _error(code, http_status=status)
        if status == 402:
            return _error("insufficient_credits", http_status=status)
        if status == 408 or status == 504:
            return _error("timeout", retryable=True, http_status=status)
        if status == 429:
            return _error("rate_limited", retryable=True, http_status=status)
        if 500 <= status <= 599:
            return _error("network_error", retryable=True, http_status=status)
        return _error("bad_response", http_status=status)

    def _response_error(
        self, response: _HttpResponse, *, capability: bool = False
    ) -> ChatClientError:
        error = self._http_error(response, capability=capability)
        if error.code == "auth_required":
            with self._lock:
                self._unavailable_code = error.code
        return error

    @staticmethod
    def _decode_object(response: _HttpResponse) -> dict:
        invalid_json = False
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            invalid_json = True
            value = None
        if invalid_json:
            raise _error("bad_response")
        if not isinstance(value, dict):
            raise _error("bad_response")
        return value

    def get_capabilities(self) -> dict:
        def operation():
            response = self._perform("GET", "/v1/ge/chat/capabilities")
            if not 200 <= response.status < 300:
                raise self._response_error(response, capability=True)
            return self._decode_object(response)

        value = self._retry_unknown(operation, attempts=4)
        if "ge_chat_v1" not in value:
            raise _error("server_unsupported")
        enabled = value["ge_chat_v1"]
        if not isinstance(enabled, bool):
            raise _error("bad_response")
        return {"ge_chat_v1": enabled}

    def _request_object(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotency_key: str | None = None,
        timeout_s: float = 10.0,
        max_response_bytes: int = _SMALL_RESPONSE_BYTES,
    ) -> dict:
        response = self._perform(
            method,
            path,
            body=body,
            idempotency_key=idempotency_key,
            timeout_s=timeout_s,
            max_response_bytes=max_response_bytes,
        )
        if not 200 <= response.status < 300:
            raise self._response_error(response)
        return self._decode_object(response)

    def _request_success(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
    ) -> None:
        response = self._perform(method, path, body=body)
        if not 200 <= response.status < 300:
            raise self._response_error(response)

    def _retry_unknown(
        self,
        operation,
        *,
        attempts: int,
        deadline: float | None = None,
        retry_attempt_budget_s: float = 0.0,
        enforce_initial_budget: bool = False,
    ):
        last_error = None
        for attempt in range(attempts):
            if (
                deadline is not None
                and (attempt > 0 or enforce_initial_budget)
                and self._clock() + retry_attempt_budget_s > deadline
            ):
                raise _error("timeout", retryable=True)
            try:
                return operation()
            except ChatClientError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
            if attempt + 1 < attempts:
                delay = 0.5 * (2**attempt)
                if (
                    deadline is not None
                    and self._clock() + delay + retry_attempt_budget_s > deadline
                ):
                    raise _error("timeout", retryable=True)
                self._sleep(delay)
        raise last_error

    def create_or_restore_conversation(self) -> dict:
        value = self._retry_unknown(
            lambda: self._request_object(
                "POST",
                "/v1/ge/chat/conversations",
                body={"run_id": self._run_id},
            ),
            attempts=3,
        )
        required = {
            "conversation_code",
            "status",
            "turn_count",
            "max_turns",
            "expires_at",
            "privacy_disclosure",
        }
        if not required.issubset(value):
            raise _error("bad_response")
        code = _wire_string(
            value["conversation_code"], max_chars=256, max_bytes=1024
        )
        status = value["status"]
        if status != "active":
            raise _error("bad_response")
        turn_count = _nonnegative_int(
            value["turn_count"], maximum=_MAX_SAFE_INTEGER
        )
        max_turns = _nonnegative_int(value["max_turns"], maximum=_MAX_SAFE_INTEGER)
        if max_turns < 1 or turn_count > max_turns:
            raise _error("bad_response")
        expires_at = _nonnegative_int(value["expires_at"], maximum=_MAX_SAFE_INTEGER)
        privacy = _validate_privacy(value["privacy_disclosure"])
        result = {
            "conversation_code": code,
            "status": status,
            "turn_count": turn_count,
            "max_turns": max_turns,
            "expires_at": expires_at,
            "privacy_disclosure": privacy,
        }
        with self._lock:
            if self._closed:
                raise _error("chat_unavailable")
            self._conversation_code = code
            self._privacy_disclosure = privacy
        return result

    def _conversation_path(self, suffix: str = "") -> str:
        with self._lock:
            code = self._conversation_code
        if code is None:
            raise _error("chat_unavailable")
        return "/v1/ge/chat/conversations/" + _path_segment(code) + suffix

    def get_history(self, after_turn_no: int = 0, limit: int = 50) -> list[dict]:
        return self._get_history(after_turn_no=after_turn_no, limit=limit, deadline=None)

    def _get_history(
        self,
        *,
        after_turn_no: int,
        limit: int,
        deadline: float | None,
    ) -> list[dict]:
        if (
            isinstance(after_turn_no, bool)
            or not isinstance(after_turn_no, int)
            or after_turn_no < 0
            or after_turn_no > _MAX_SAFE_INTEGER
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise _error("invalid_request")
        cursor = after_turn_no
        history = []
        history_bytes = 0
        for _page_number in range(100):
            query = urlencode({"after_turn_no": cursor, "limit": limit})
            value = self._retry_unknown(
                lambda query=query: self._request_object(
                    "GET",
                    self._conversation_path("/turns") + "?" + query,
                    max_response_bytes=8 * 1024 * 1024,
                ),
                attempts=4,
                deadline=deadline,
                retry_attempt_budget_s=_POLL_ATTEMPT_BUDGET_S,
                enforce_initial_budget=deadline is not None,
            )
            has_more = value.get("has_more")
            if type(has_more) is not bool:
                raise _error("bad_response")
            expected_fields = {"turns", "has_more"}
            if has_more:
                expected_fields.add("next_after_turn_no")
            if set(value) != expected_fields or not isinstance(value["turns"], list):
                raise _error("bad_response")
            page = value["turns"]
            if len(page) > limit:
                raise _error("bad_response")
            for raw_turn in page:
                turn = _validate_history_turn(raw_turn, previous_turn_no=cursor)
                cursor = turn["turn_no"]
                history_bytes += _history_turn_size(turn)
                if history_bytes > _MAX_HISTORY_BYTES:
                    raise _error("bad_response")
                history.append(turn)
                if len(history) > 10_000:
                    raise _error("bad_response")
            if not has_more:
                return history
            next_cursor = value["next_after_turn_no"]
            if (
                isinstance(next_cursor, bool)
                or not isinstance(next_cursor, int)
                or not page
                or next_cursor != cursor
            ):
                raise _error("bad_response")
        raise _error("bad_response")

    def bootstrap(self) -> dict:
        capability = self.get_capabilities()
        if not capability["ge_chat_v1"]:
            raise _error("capability_disabled")
        conversation = self.create_or_restore_conversation()
        history = self.get_history()
        self._replace_history(history)
        return {
            "ok": True,
            "backend": "server",
            "state": "ready",
            "conversation": {
                "status": conversation["status"],
                "turn_count": conversation["turn_count"],
                "max_turns": conversation["max_turns"],
                "expires_at": conversation["expires_at"],
            },
            "privacy_disclosure": conversation["privacy_disclosure"],
            "history": history,
        }

    @staticmethod
    def _copy_history(history: list[dict]) -> list[dict]:
        return [
            {
                **turn,
                "images": [dict(image) for image in turn["images"]],
            }
            for turn in history
        ]

    def _replace_history(self, history: list[dict]) -> None:
        history_bytes = sum(_history_turn_size(turn) for turn in history)
        if len(history) > 10_000 or history_bytes > _MAX_HISTORY_BYTES:
            raise _error("bad_response")
        with self._lock:
            if self._closed:
                raise _error("chat_unavailable")
            self._history = self._copy_history(history)
            self._history_bytes = history_bytes

    def _prepare_history_append(
        self, delta: list[dict]
    ) -> tuple[int, list[dict], int]:
        with self._lock:
            previous_turn_no = self._history[-1]["turn_no"] if self._history else 0
            expected_cursor = previous_turn_no
            added_bytes = 0
            for turn in delta:
                if turn["turn_no"] <= previous_turn_no:
                    raise _error("bad_response")
                previous_turn_no = turn["turn_no"]
                added_bytes += _history_turn_size(turn)
            if (
                len(self._history) + len(delta) > 10_000
                or self._history_bytes + added_bytes > _MAX_HISTORY_BYTES
            ):
                raise _error("bad_response")
            snapshot = self._copy_history(self._history)
            snapshot.extend(self._copy_history(delta))
            return expected_cursor, snapshot, self._history_bytes + added_bytes

    def _commit_history_snapshot(
        self,
        *,
        expected_cursor: int,
        snapshot: list[dict],
        history_bytes: int,
    ) -> None:
        with self._lock:
            current_cursor = self._history[-1]["turn_no"] if self._history else 0
            if self._closed:
                raise _error("chat_unavailable")
            if current_cursor != expected_cursor:
                raise _error("bad_response")
            self._history = self._copy_history(snapshot)
            self._history_bytes = history_bytes

    def _history_cursor(self) -> int:
        with self._lock:
            return self._history[-1]["turn_no"] if self._history else 0

    def _recover_orphan_submit(
        self,
        record: _TurnRecord,
        *,
        deadline: float,
        on_action,
        emit_state,
    ) -> dict:
        """Refresh server history before retrying a fenced submit with the same key."""
        delay_index = 0
        while True:
            emit_state("recovering", {})
            delay = (1.0, 2.0, 4.0, 8.0)[delay_index] if delay_index < 4 else 10.0
            delay_index += 1
            if self._clock() + delay >= deadline:
                raise _error("timeout", retryable=True)
            self._sleep(delay)
            if self._clock() >= deadline:
                raise _error("timeout", retryable=True)

            cursor = self._history_cursor()
            try:
                delta = self._get_history(
                    after_turn_no=cursor,
                    limit=50,
                    deadline=deadline,
                )
            except ChatClientError as exc:
                if exc.retryable:
                    continue
                raise
            if delta:
                expected_cursor, snapshot, history_bytes = self._prepare_history_append(
                    delta
                )
                delivered = self._notify_ack(
                    on_action,
                    {"type": "history_snapshot", "history": snapshot},
                )
                if not delivered:
                    raise _error("bad_response")
                self._commit_history_snapshot(
                    expected_cursor=expected_cursor,
                    snapshot=snapshot,
                    history_bytes=history_bytes,
                )
                with self._lock:
                    record.orphan_history_delivered = True
            if not record.orphan_history_delivered:
                continue
            try:
                submitted = self._submit_turn(
                    record.text,
                    record.images,
                    record.key,
                    deadline=deadline,
                    enforce_initial_budget=True,
                )
            except ChatClientError as exc:
                if exc.code in {"turn_in_flight", "conversation_busy"}:
                    with self._lock:
                        record.orphan_history_delivered = False
                    continue
                raise
            with self._lock:
                record.orphan_recovery_started = False
                record.orphan_history_delivered = False
            return submitted

    def _submit_turn(
        self,
        text,
        images,
        idempotency_key: str,
        *,
        deadline: float | None,
        enforce_initial_budget: bool = False,
    ) -> dict:
        clean_text, clean_images, key = _validate_turn_input(text, images, idempotency_key)
        value = self._retry_unknown(
            lambda: self._request_object(
                "POST",
                self._conversation_path("/turns"),
                body={"text": clean_text, "images": clean_images},
                idempotency_key=key,
                timeout_s=120.0,
                max_response_bytes=_TURN_RESPONSE_BYTES,
            ),
            attempts=3,
            deadline=deadline,
            retry_attempt_budget_s=_SUBMIT_ATTEMPT_BUDGET_S,
            enforce_initial_budget=enforce_initial_budget,
        )
        required = {"turn_code", "status", "phase", "price_credits"}
        if not required.issubset(value):
            raise _error("bad_response")
        turn_code = _wire_string(value["turn_code"], max_chars=256, max_bytes=1024)
        status = value["status"]
        phase = value["phase"]
        if not isinstance(status, str) or not isinstance(phase, str):
            raise _error("bad_response")
        price = _nonnegative_int(value["price_credits"], maximum=_MAX_SAFE_INTEGER)
        common = {
            "turn_code": turn_code,
            "status": status,
            "phase": phase,
            "price_credits": price,
        }
        if status in _ACTIVE_STATUS_PHASES:
            if phase not in _ACTIVE_STATUS_PHASES[status]:
                raise _error("bad_response")
            common["poll_after_ms"] = _nonnegative_int(
                value.get("poll_after_ms"), maximum=_MAX_SAFE_INTEGER
            )
            return common
        if status == "completed":
            if phase != "finalizing" or value.get("error_code") is not None:
                raise _error("bad_response")
            if "completed_at" not in value or value.get("poll_after_ms") is not None:
                raise _error("bad_response")
            _nonnegative_int(value["completed_at"], maximum=_MAX_SAFE_INTEGER)
            common.update(
                {
                    "text": _wire_string(
                        value.get("assistant_text"),
                        max_chars=200_000,
                        max_bytes=800_000,
                        allow_controls=True,
                    ),
                    "turn_count": _nonnegative_int(
                        value.get("turn_count"), maximum=_MAX_SAFE_INTEGER
                    ),
                    "remaining_turns": _nonnegative_int(
                        value.get("remaining_turns"), maximum=_MAX_SAFE_INTEGER
                    ),
                }
            )
            return common
        if status in {"failed", "cancelled"}:
            if phase not in {"queued", "compacting", "calling_model", "finalizing"}:
                raise _error("bad_response")
            if value.get("assistant_text") is not None:
                raise _error("bad_response")
            if "completed_at" not in value or value.get("poll_after_ms") is not None:
                raise _error("bad_response")
            _nonnegative_int(value["completed_at"], maximum=_MAX_SAFE_INTEGER)
            error_code = value.get("error_code")
            if not isinstance(error_code, str) or error_code not in _STABLE_CODES:
                raise _error("bad_response")
            if (status == "cancelled") != (error_code == "cancelled"):
                raise _error("bad_response")
            common["error_code"] = error_code
            return common
        raise _error("bad_response")

    def submit_turn(self, text, images, idempotency_key: str) -> dict:
        return self._submit_turn(text, images, idempotency_key, deadline=None)

    def _poll_turn(self, turn_code: str, *, deadline: float | None) -> dict:
        code = _path_segment(turn_code)
        value = self._retry_unknown(
            lambda: self._request_object(
                "GET",
                "/v1/ge/chat/turns/" + code,
                max_response_bytes=_TURN_RESPONSE_BYTES,
            ),
            attempts=4,
            deadline=deadline,
            retry_attempt_budget_s=_POLL_ATTEMPT_BUDGET_S,
            enforce_initial_budget=deadline is not None,
        )
        status = value.get("status")
        if not isinstance(status, str):
            raise _error("bad_response")
        if status in _ACTIVE_STATUS_PHASES:
            phase = value.get("phase")
            if not isinstance(phase, str) or phase not in _ACTIVE_STATUS_PHASES[status]:
                raise _error("bad_response")
            return {
                "status": status,
                "phase": phase,
                "poll_after_ms": _nonnegative_int(
                    value.get("poll_after_ms"), maximum=_MAX_SAFE_INTEGER
                ),
            }
        if status == "completed":
            return {
                "status": status,
                "text": _wire_string(
                    value.get("text"),
                    max_chars=200_000,
                    max_bytes=800_000,
                    allow_controls=True,
                ),
                "turn_count": _nonnegative_int(
                    value.get("turn_count"), maximum=_MAX_SAFE_INTEGER
                ),
                "remaining_turns": _nonnegative_int(
                    value.get("remaining_turns"), maximum=_MAX_SAFE_INTEGER
                ),
            }
        if status in {"failed", "cancelled"}:
            code = value.get("error_code")
            if status == "cancelled" and code is None:
                code = "cancelled"
            if not isinstance(code, str) or code not in _STABLE_CODES:
                raise _error("bad_response")
            if status == "cancelled" and code != "cancelled":
                raise _error("bad_response")
            return {"status": status, "error_code": code}
        raise _error("bad_response")

    def poll_turn(self, turn_code: str) -> dict:
        return self._poll_turn(turn_code, deadline=None)

    def _poll_delay(self, poll_after_ms: int) -> float:
        clamped_ms = min(10_000, max(250, poll_after_ms))
        try:
            random_value = self._rng.random() if hasattr(self._rng, "random") else self._rng()
        except Exception:  # noqa: BLE001  # aqg: top-level boundary — RNG injection cannot break polling
            random_value = 0.5
        if (
            isinstance(random_value, bool)
            or not isinstance(random_value, (int, float))
            or not 0 <= random_value <= 1
        ):
            random_value = 0.5
        return (clamped_ms / 1000.0) * (1.0 + 0.2 * random_value)

    @staticmethod
    def _state_for_phase(phase: str) -> str:
        if phase == "calling_model":
            return "calling_model"
        if phase in {"compacting", "finalizing"}:
            return "running"
        return phase

    def _clear_active(self, record: _TurnRecord, terminal: dict) -> None:
        with self._lock:
            record.running = False
            record.text = ""
            record.images = []
            record.attempt_done.set()
            record.handle_ready.set()
            if self._closing or self._closed:
                record.terminal = None
                record.terminal_size = 0
            else:
                record.terminal = terminal
                record.terminal_size = _terminal_size(terminal)
                previous = self._terminal_records.pop(record.key, None)
                if previous is not None:
                    self._terminal_record_bytes -= previous.terminal_size
                self._terminal_records[record.key] = record
                self._terminal_record_bytes += record.terminal_size
                while (
                    len(self._terminal_records) > 64
                    or self._terminal_record_bytes > _MAX_TERMINAL_BYTES
                ):
                    evicted = self._terminal_records.pop(next(iter(self._terminal_records)))
                    self._terminal_record_bytes -= evicted.terminal_size
            if self._active is record:
                self._active = None

    def _release_active(self, record: _TurnRecord) -> None:
        """Release local ownership without inventing a server terminal result."""
        with self._lock:
            record.running = False
            record.text = ""
            record.images = []
            record.attempt_done.set()
            record.handle_ready.set()
            if self._active is record:
                self._active = None

    @staticmethod
    def _notify(callback, *args) -> bool:
        if not callable(callback):
            return False
        try:
            return callback(*args) is not False
        except Exception:  # noqa: BLE001  # aqg: top-level boundary — isolate UI callbacks
            return False

    @staticmethod
    def _notify_ack(callback, *args) -> bool:
        """Require an explicit acknowledgement without changing ordinary callback semantics."""
        if not callable(callback):
            return False
        try:
            return callback(*args) is True
        except Exception:  # noqa: BLE001  # aqg: top-level boundary — isolate UI callbacks
            return False

    def _emit_answer_deltas(self, text: str, on_delta) -> None:
        if not callable(on_delta) or not text:
            self._notify(on_delta, text)
            return
        chunk_size = max(
            1,
            (len(text) + _ANSWER_FILL_MAX_STEPS - 1) // _ANSWER_FILL_MAX_STEPS,
        )
        for end in range(chunk_size, len(text), chunk_size):
            if not self._notify(on_delta, text[:end]):
                return
            self._sleep(_ANSWER_FILL_INTERVAL_S)
        self._notify(on_delta, text)

    def _claim_cancel(self, record: _TurnRecord, *, force: bool = False) -> str | None:
        with self._lock:
            if (
                not record.pending_cancel
                or record.turn_code is None
                or record.cancel_sent
                or record.cancel_in_flight
                or (record.cancel_attempts >= _MAX_CANCEL_ATTEMPTS and not force)
            ):
                return None
            record.cancel_in_flight = True
            record.cancel_attempts += 1
            record.cancel_attempt_done.clear()
            return record.turn_code

    def _send_claimed_cancel(
        self, record: _TurnRecord, turn_code: str
    ) -> ChatClientError | None:
        try:
            self._cancel_turn(turn_code)
        except ChatClientError as exc:
            with self._lock:
                record.cancel_in_flight = False
                record.last_cancel_error = exc.code
                record.cancel_attempt_done.set()
            return exc
        with self._lock:
            record.cancel_in_flight = False
            record.cancel_sent = True
            record.last_cancel_error = None
            record.cancel_attempt_done.set()
        return None

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id: str,
        privacy_version: str,
        on_state=None,
    ) -> None:
        emit_state = lambda state, payload: self._notify(on_state, state, payload)
        try:
            clean_text, clean_images, key = _validate_turn_input(text, images, client_turn_id)
        except ChatClientError as exc:
            self._notify(on_error, exc.code)
            return
        payload_digest = _payload_digest(clean_text, clean_images)
        admission_error = None
        follower = None
        with self._lock:
            privacy = self._privacy_disclosure
            if self._unavailable_code is not None:
                mismatch = False
                admission_error = self._unavailable_code
                record = None
            elif self._closed or self._closing:
                mismatch = False
                admission_error = "chat_unavailable"
                record = None
            elif (
                privacy is None
                or not isinstance(privacy_version, str)
                or privacy_version != privacy["version"]
            ):
                mismatch = True
                record = None
            else:
                mismatch = False
                active = self._active
                if active is not None:
                    if active.key != key:
                        admission_error = "turn_in_flight"
                        record = None
                    elif active.payload_digest != payload_digest:
                        admission_error = "invalid_request"
                        record = None
                    elif active.running:
                        follower = active
                        record = None
                    else:
                        active.running = True
                        active.started_at = self._clock()
                        active.attempt_done.clear()
                        record = active
                else:
                    completed = self._terminal_records.get(key)
                    if completed is not None:
                        if completed.payload_digest != payload_digest:
                            admission_error = "invalid_request"
                            record = None
                        else:
                            follower = completed
                            record = None
                    else:
                        record = _TurnRecord(
                            key=key,
                            text=clean_text,
                            images=clean_images,
                            payload_digest=payload_digest,
                            started_at=self._clock(),
                        )
                        self._active = record
        if mismatch:
            self._notify(on_error, "privacy_confirmation_required")
            return
        if admission_error is not None:
            if admission_error == "auth_required":
                emit_state("unavailable", {"error_code": admission_error})
                self._notify(on_error, admission_error)
            elif admission_error in {"turn_in_flight", "conversation_busy"}:
                emit_state("recovering", {})
                if not callable(on_state):
                    self._notify(on_error, admission_error)
            else:
                self._notify(on_error, admission_error)
            return
        if follower is not None:
            remaining = max(0.0, follower.started_at + _WHOLE_TURN_S - self._clock())
            if not follower.attempt_done.wait(remaining):
                emit_state("recovering", {})
                return
            terminal = follower.terminal
            if terminal is None:
                emit_state("recovering", {})
                return
            if terminal["status"] == "completed":
                self._emit_answer_deltas(terminal["text"], on_delta)
                emit_state(
                    "completed",
                    {
                        "turn_count": terminal["turn_count"],
                        "remaining_turns": terminal["remaining_turns"],
                    },
                )
                self._notify(on_done, terminal["text"])
            else:
                status = terminal["status"]
                emit_state(
                    status,
                    {} if status == "cancelled" else {"error_code": terminal["error_code"]},
                )
                self._notify(on_error, terminal["error_code"])
            return
        try:
            if record.turn_code is None:
                deadline = record.started_at + _WHOLE_TURN_S
                if record.orphan_recovery_started:
                    # A committed snapshot was already page-acknowledged; retain that fact because
                    # the cursor cannot redeliver the same delta on an explicit retry invocation.
                    submitted = self._recover_orphan_submit(
                        record,
                        deadline=deadline,
                        on_action=on_action,
                        emit_state=emit_state,
                    )
                else:
                    emit_state("submitting", {})
                    try:
                        submitted = self._submit_turn(
                            record.text,
                            record.images,
                            record.key,
                            deadline=deadline,
                        )
                    except ChatClientError as exc:
                        if exc.code not in {"turn_in_flight", "conversation_busy"}:
                            raise
                        with self._lock:
                            record.orphan_recovery_started = True
                            record.orphan_history_delivered = False
                        submitted = self._recover_orphan_submit(
                            record,
                            deadline=deadline,
                            on_action=on_action,
                            emit_state=emit_state,
                        )
                with self._lock:
                    record.turn_code = submitted["turn_code"]
                    record.price_credits = submitted["price_credits"]
                    if submitted["status"] in {"queued", "running"}:
                        record.poll_after_ms = submitted["poll_after_ms"]
                        record.text = ""
                        record.images = []
                        record.handle_ready.set()
                if submitted["status"] == "completed":
                    terminal = {
                        "status": "completed",
                        "text": submitted["text"],
                        "turn_count": submitted["turn_count"],
                        "remaining_turns": submitted["remaining_turns"],
                    }
                    self._clear_active(record, terminal)
                    self._emit_answer_deltas(submitted["text"], on_delta)
                    emit_state(
                        "completed",
                        {
                            "turn_count": submitted["turn_count"],
                            "remaining_turns": submitted["remaining_turns"],
                        },
                    )
                    self._notify(on_done, submitted["text"])
                    return
                if submitted["status"] in {"failed", "cancelled"}:
                    terminal = {
                        "status": submitted["status"],
                        "error_code": submitted["error_code"],
                    }
                    self._clear_active(record, terminal)
                    emit_state(
                        submitted["status"],
                        (
                            {}
                            if submitted["status"] == "cancelled"
                            else {"error_code": submitted["error_code"]}
                        ),
                    )
                    self._notify(on_error, submitted["error_code"])
                    return
                state = self._state_for_phase(submitted["phase"])
                emit_state(state, {"price_credits": record.price_credits})
            else:
                emit_state("recovering", {})
            claimed_cancel = self._claim_cancel(record)
            if claimed_cancel is not None:
                emit_state("cancelling", {})
                self._send_claimed_cancel(record, claimed_cancel)
            poll_after_ms = record.poll_after_ms
            while True:
                deadline = record.started_at + _WHOLE_TURN_S
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise _error("timeout", retryable=True)
                delay = self._poll_delay(poll_after_ms)
                if delay >= remaining:
                    self._sleep(max(0.0, remaining))
                    raise _error("timeout", retryable=True)
                self._sleep(delay)
                if self._clock() >= deadline:
                    raise _error("timeout", retryable=True)
                result = self._poll_turn(record.turn_code, deadline=deadline)
                status = result["status"]
                if status in {"queued", "running"}:
                    if record.pending_cancel:
                        emit_state("cancelling", {})
                        claimed_cancel = self._claim_cancel(record)
                        if claimed_cancel is not None:
                            self._send_claimed_cancel(record, claimed_cancel)
                    else:
                        emit_state(
                            self._state_for_phase(result["phase"]),
                            {"price_credits": record.price_credits},
                        )
                    poll_after_ms = result["poll_after_ms"]
                    with self._lock:
                        record.poll_after_ms = poll_after_ms
                    continue
                if status == "completed":
                    terminal = dict(result)
                    self._clear_active(record, terminal)
                    self._emit_answer_deltas(result["text"], on_delta)
                    emit_state(
                        "completed",
                        {
                            "turn_count": result["turn_count"],
                            "remaining_turns": result["remaining_turns"],
                        },
                    )
                    self._notify(on_done, result["text"])
                    return
                terminal = dict(result)
                self._clear_active(record, terminal)
                emit_state(status, {} if status == "cancelled" else {"error_code": result["error_code"]})
                self._notify(on_error, result["error_code"])
                return
        except ChatClientError as exc:
            if exc.code == "auth_required":
                with self._lock:
                    self._unavailable_code = exc.code
                self._release_active(record)
                emit_state("unavailable", {"error_code": exc.code})
                self._notify(on_error, exc.code)
            elif (
                record.turn_code is None
                and exc.http_status is not None
                and not exc.retryable
                and exc.code not in {"turn_in_flight", "conversation_busy"}
            ):
                self._release_active(record)
                emit_state("failed", {"error_code": exc.code})
                self._notify(on_error, exc.code)
            else:
                with self._lock:
                    record.running = False
                    record.attempt_done.set()
                emit_state("recovering", {})
                if not callable(on_state):
                    self._notify(on_error, exc.code)
            return
        except Exception:  # noqa: BLE001  # aqg: top-level boundary — normalize worker seam failures
            with self._lock:
                record.running = False
                record.attempt_done.set()
            emit_state("recovering", {})
            if not callable(on_state):
                self._notify(on_error, "chat_unavailable")
            return

    def _cancel_turn(self, turn_code: str) -> dict:
        code = _path_segment(turn_code)
        self._request_success(
            "POST",
            "/v1/ge/chat/turns/" + code + "/cancel",
        )
        return {"ok": True, "status": "cancelling"}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closing = True
            record = self._active
            if record is not None:
                record.pending_cancel = True
        deadline = time.monotonic() + _CLOSE_BUDGET_S
        try:
            if record is not None and record.turn_code is None:
                record.handle_ready.wait(max(0.0, deadline - time.monotonic()))
            claimed_code = (
                None if record is None else self._claim_cancel(record, force=True)
            )
            wait_for_cancel = False
            cancel_worker = None
            if record is not None and claimed_code is None:
                with self._lock:
                    wait_for_cancel = record.cancel_in_flight
            elif claimed_code is not None and time.monotonic() < deadline:
                cancel_worker = threading.Thread(
                    target=self._send_claimed_cancel,
                    args=(record, claimed_code),
                    daemon=True,
                )
                try:
                    cancel_worker.start()
                except RuntimeError:
                    with self._lock:
                        record.cancel_in_flight = False
                        record.last_cancel_error = "chat_unavailable"
                        record.cancel_attempt_done.set()
                else:
                    wait_for_cancel = True
            if wait_for_cancel and record is not None:
                record.cancel_attempt_done.wait(
                    max(0.0, deadline - time.monotonic())
                )
            if cancel_worker is not None:
                try:
                    cancel_worker.join(0)
                except RuntimeError:
                    pass
        except Exception:  # noqa: BLE001,S110  # aqg: top-level boundary — bounded best-effort cancel
            pass
        finally:
            with self._lock:
                self._token = ""
                if self._active is not None:
                    self._active.running = False
                    self._active.text = ""
                    self._active.images = []
                    self._active.attempt_done.set()
                    self._active.handle_ready.set()
                self._closed = True
                self._closing = False
                self._conversation_code = None
                self._privacy_disclosure = None
                self._active = None
                self._terminal_records.clear()
                self._terminal_record_bytes = 0
                self._history = []
                self._history_bytes = 0
                self._unavailable_code = None
        try:
            close_transport = getattr(self._transport, "close", None)
        except Exception:  # noqa: BLE001  # aqg: top-level boundary — untrusted transport seam
            close_transport = None
        if callable(close_transport):
            close_done = threading.Event()

            def close_transport_bounded() -> None:
                try:
                    close_transport()
                except Exception:  # noqa: BLE001,S110  # aqg: top-level boundary — redacted best-effort close
                    pass
                finally:
                    close_done.set()

            close_worker = threading.Thread(
                target=close_transport_bounded,
                daemon=True,
            )
            try:
                close_worker.start()
            except RuntimeError:
                return
            close_done.wait(max(0.0, deadline - time.monotonic()))
            try:
                close_worker.join(0)
            except RuntimeError:
                pass
