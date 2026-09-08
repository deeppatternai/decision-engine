"""Focused behavior tests for the server-backed GE popup HTTP session."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import threading
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from client import runner
from client.popup import http_chat
from client.popup.http_chat import ChatClientError, HttpChatSession


class _ScriptedTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **request):
        self.calls.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _ResponseWithHeaders:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        drip = getattr(self.server, "drip", None)
        if drip is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(64 * 1024 * 1024))
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    if drip.wait(0.02):
                        return
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        location = getattr(self.server, "location", None)
        if location:
            body = b"redirect-body-must-not-escape"
            self.send_response(302)
            self.send_header("Location", location)
        else:
            body = json.dumps({"ge_chat_v1": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _Server:
    def __init__(self, *, location=None, drip=None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.location = location
        self.httpd.drip = drip
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class HttpChatPublicContractTests(unittest.TestCase):
    def test_http_chat_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("client.popup.http_chat"))

    def test_every_stable_error_has_a_specific_user_message(self):
        self.assertEqual(set(http_chat._MESSAGES), set(http_chat._STABLE_CODES))

    def test_client_exposes_no_cancel_api(self):
        self.assertFalse(hasattr(HttpChatSession, "cancel_turn"))
        self.assertFalse(hasattr(HttpChatSession, "cancel_active"))

    def test_capability_request_uses_shared_nonempty_user_agent(self):
        self.assertIsInstance(runner.USER_AGENT, str)
        self.assertTrue(runner.USER_AGENT.strip())
        self.assertTrue(runner.USER_AGENT.isascii())
        self.assertTrue(http_chat._C0.isdisjoint(runner.USER_AGENT))
        self.assertEqual(
            runner.USER_AGENT,
            "decision-engine-client/" + runner.CLIENT_VERSION,
        )
        transport = _ScriptedTransport((200, b'{"ge_chat_v1":true}'))
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=transport,
        )

        self.assertEqual(session.get_capabilities(), {"ge_chat_v1": True})
        self.assertEqual(transport.calls[0]["headers"]["User-Agent"], runner.USER_AGENT)


class HttpChatTransportSafetyTests(unittest.TestCase):
    def test_plain_http_is_limited_to_literal_loopback(self):
        invalid = (
            "http://localhost:8080",
            "http://2130706433:8080",
            "http://example.test",
            "ftp://127.0.0.1",
            "https://user@example.test",
            "https://example.test/chat",
            "https://example.test?",
            "https://example.test?token=x",
            "https://example.test#",
            "https://example.test/#frag",
        )
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ChatClientError) as caught:
                    HttpChatSession(endpoint=endpoint, token="token", run_id="run")
                self.assertEqual(caught.exception.code, "invalid_request")

        for endpoint in ("http://127.0.0.1:8080", "http://[::1]:8080", "https://example.test"):
            with self.subTest(endpoint=endpoint):
                HttpChatSession(
                    endpoint=endpoint,
                    token="token",
                    run_id="run",
                    transport=_ScriptedTransport(),
                )

    def test_proxy_environment_is_bypassed_and_redirect_is_rejected(self):
        token = "TOKEN-SENTINEL-NO-REDIRECT"
        target = _Server()
        self.addCleanup(target.close)
        source = _Server(location=target.endpoint + "/stolen")
        self.addCleanup(source.close)
        proxy = _Server()
        self.addCleanup(proxy.close)

        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": proxy.endpoint,
                "HTTPS_PROXY": proxy.endpoint,
                "ALL_PROXY": proxy.endpoint,
                "NO_PROXY": "",
            },
            clear=False,
        ):
            session = HttpChatSession(endpoint=source.endpoint, token=token, run_id="run")
            with self.assertRaises(ChatClientError) as caught:
                session.get_capabilities()

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertEqual(caught.exception.code, "bad_response")
        self.assertNotIn(token, rendered)
        self.assertNotIn("redirect-body", rendered)
        self.assertEqual(len(source.httpd.requests), 1)
        self.assertEqual(proxy.httpd.requests, [])
        self.assertEqual(target.httpd.requests, [])

    def test_redirect_body_cannot_reclassify_3xx_as_retryable_server_error(self):
        transport = _ScriptedTransport((302, b'{"error_code":"rate_limited"}'))
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        with self.assertRaises(ChatClientError) as caught:
            session.get_capabilities()

        self.assertEqual(caught.exception.code, "bad_response")
        self.assertEqual(len(transport.calls), 1)

    def test_bad_json_and_response_cap_are_stable_errors_without_raw_body(self):
        sentinel = "RAW-SERVER-BODY-SENTINEL"
        cases = (
            (200, (sentinel + "{").encode("utf-8")),
            (200, b"{}" * (128 * 1024 + 1)),
        )
        for response in cases:
            with self.subTest(size=len(response[1])):
                session = HttpChatSession(
                    endpoint="https://example.test",
                    token="TOKEN-SENTINEL",
                    run_id="run",
                    transport=_ScriptedTransport(response),
                )
                with self.assertRaises(ChatClientError) as caught:
                    session.get_capabilities()
                rendered = "".join(traceback.format_exception(caught.exception))
                self.assertEqual(caught.exception.code, "bad_response")
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn("TOKEN-SENTINEL", rendered)
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)

    def test_slow_response_is_stopped_by_total_body_budget(self):
        stop = threading.Event()
        server = _Server(drip=stop)
        self.addCleanup(server.close)
        self.addCleanup(stop.set)
        session = HttpChatSession(endpoint=server.endpoint, token="token", run_id="run")

        started = http_chat.time.monotonic()
        with (
            mock.patch.object(http_chat, "_RESPONSE_BUDGET_S", 0.2),
            self.assertRaises(ChatClientError) as caught,
        ):
            session.get_capabilities()

        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(len(server.httpd.requests), 4)
        self.assertLess(http_chat.time.monotonic() - started, 6.0)

    def test_json_integer_limit_errors_are_normalized_for_success_and_error_bodies(self):
        huge_integer = b"9" * 5_000
        cases = (
            (200, b'{"ge_chat_v1":' + huge_integer + b"}"),
            (400, b'{"error_code":' + huge_integer + b"}"),
        )
        for response in cases:
            with self.subTest(status=response[0]):
                session = HttpChatSession(
                    endpoint="https://example.test",
                    token="token",
                    run_id="run",
                    transport=_ScriptedTransport(response),
                )
                with self.assertRaises(ChatClientError) as caught:
                    session.get_capabilities()
                self.assertEqual(caught.exception.code, "bad_response")

    def test_capability_404_status_overrides_body_error_code(self):
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=_ScriptedTransport((404, b'{"error_code":"not_found"}')),
        )

        with self.assertRaises(ChatClientError) as caught:
            session.get_capabilities()

        self.assertEqual(caught.exception.code, "server_unsupported")

    def test_capability_missing_field_is_unsupported_but_wrong_type_is_bad_response(self):
        cases = (({}, "server_unsupported"), ({"ge_chat_v1": 1}, "bad_response"))
        for body, expected in cases:
            with self.subTest(body=body):
                session = HttpChatSession(
                    endpoint="https://example.test",
                    token="token",
                    run_id="run",
                    transport=_ScriptedTransport((200, json.dumps(body).encode("utf-8"))),
                )
                with self.assertRaises(ChatClientError) as caught:
                    session.get_capabilities()
                self.assertEqual(caught.exception.code, expected)

    def test_auth_failure_from_any_http_method_latches_session_unavailable(self):
        transport = _ScriptedTransport((401, b'{"error_code":"auth_required"}'))
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=transport,
        )
        with self.assertRaises(ChatClientError) as caught:
            session.get_capabilities()
        self.assertEqual(caught.exception.code, "auth_required")

        states = []
        errors = []
        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual(states, [("unavailable", {"error_code": "auth_required"})])
        self.assertEqual(errors, ["auth_required"])
        self.assertEqual(len(transport.calls), 1)


def _privacy(version="sha256:test"):
    return {
        "version": version,
        "providers": [
            {
                "category": "synthetic-category",
                "data_region": "test-region",
                "training_enabled": False,
                "cache_ttl_seconds": 0,
                "provider_retention_hours": 0,
                "deletion_scope": "test-contract",
            }
        ],
    }


def _conversation_response():
    return {
        "conversation_code": "conv-test",
        "status": "active",
        "turn_count": 0,
        "max_turns": 40,
        "expires_at": 1_787_587_200,
        "privacy_disclosure": _privacy(),
    }


class HttpChatConversationTests(unittest.TestCase):
    def test_create_unknown_response_retries_same_run_id_without_token_leak(self):
        token = "TOKEN-SENTINEL-CREATE"
        transport = _ScriptedTransport(
            OSError("untrusted failure carrying " + token),
            TimeoutError("untrusted timeout carrying " + token),
            (200, json.dumps(_conversation_response()).encode("utf-8")),
        )
        session = HttpChatSession(
            endpoint="https://example.test",
            token=token,
            run_id="source-run",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        result = session.create_or_restore_conversation()

        self.assertEqual(result, _conversation_response())
        self.assertEqual(len(transport.calls), 3)
        for call in transport.calls:
            self.assertEqual(call["method"], "POST")
            self.assertEqual(call["url"], "https://example.test/v1/ge/chat/conversations")
            self.assertEqual(json.loads(call["body"]), {"run_id": "source-run"})
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + token)
            self.assertEqual(call["headers"]["User-Agent"], runner.USER_AGENT)
            self.assertNotIn(token, call["url"])
        rendered = repr(result) + repr(session)
        self.assertNotIn(token, rendered)

    def test_create_rejects_malformed_privacy_disclosure(self):
        malformed = _conversation_response()
        malformed["privacy_disclosure"] = {"version": "bad version", "providers": []}
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=_ScriptedTransport((200, json.dumps(malformed).encode("utf-8"))),
        )

        with self.assertRaises(ChatClientError) as caught:
            session.create_or_restore_conversation()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_create_accepts_unknown_training_policy_and_rejects_enabled(self):
        unknown = _conversation_response()
        unknown["privacy_disclosure"]["providers"][0]["training_enabled"] = None
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=_ScriptedTransport(
                (200, json.dumps(unknown).encode("utf-8"))
            ),
        )

        result = session.create_or_restore_conversation()

        self.assertIsNone(
            result["privacy_disclosure"]["providers"][0]["training_enabled"]
        )

        enabled = _conversation_response()
        enabled["privacy_disclosure"]["providers"][0]["training_enabled"] = True
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=_ScriptedTransport(
                (200, json.dumps(enabled).encode("utf-8"))
            ),
        )

        with self.assertRaises(ChatClientError) as caught:
            session.create_or_restore_conversation()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_create_rejects_js_unsafe_conversation_integers(self):
        for field in ("turn_count", "max_turns", "expires_at"):
            with self.subTest(field=field):
                response = _conversation_response()
                response[field] = (1 << 53)
                if field == "turn_count":
                    response["max_turns"] = 1 << 53
                session = HttpChatSession(
                    endpoint="https://example.test",
                    token="token",
                    run_id="run",
                    transport=_ScriptedTransport(
                        (200, json.dumps(response).encode("utf-8"))
                    ),
                )

                with self.assertRaises(ChatClientError) as caught:
                    session.create_or_restore_conversation()

                self.assertEqual(caught.exception.code, "bad_response")

    def test_transport_exception_chain_is_detached_from_public_error(self):
        token = "TOKEN-" + "SENTINEL-TRANSPORT-CONTEXT"
        transport = _ScriptedTransport(
            *(OSError("raw transport detail " + token) for _attempt in range(3))
        )
        session = HttpChatSession(
            endpoint="https://example.test",
            token=token,
            run_id="run",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        with self.assertRaises(ChatClientError) as caught:
            session.create_or_restore_conversation()

        self.assertEqual(caught.exception.code, "network_error")
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(token, repr(caught.exception))


def _history_turn(turn_no, *, status="completed"):
    value = {
        "turn_no": turn_no,
        "turn_code": f"must-not-reach-ui-{turn_no}",
        "status": status,
        "user_text": f"question {turn_no}",
        "assistant_text": f"answer {turn_no}" if status == "completed" else None,
        "error_code": None if status == "completed" else status,
        "completed_at": 1_787_587_200 + turn_no,
        "images": [
            {
                "image_index": 0,
                "media_type": "image/png",
                "size_bytes": 12,
                "sha256": "a" * 64,
            }
        ],
        "provider": "must-not-reach-ui",
    }
    if status == "failed":
        value["error_code"] = "model_unavailable"
    return value


def _history_response(*turns, has_more=False, next_after_turn_no=None):
    payload = {"turns": list(turns), "has_more": has_more}
    if has_more:
        if next_after_turn_no is None:
            next_after_turn_no = turns[-1]["turn_no"]
        payload["next_after_turn_no"] = next_after_turn_no
    return 200, json.dumps(payload).encode("utf-8")


class HttpChatHistoryTests(unittest.TestCase):
    def test_bootstrap_follows_has_more_cursor_and_stops_on_terminal_page(self):
        transport = _ScriptedTransport(
            (200, b'{"ge_chat_v1":true}'),
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "turns": [_history_turn(1)],
                        "has_more": True,
                        "next_after_turn_no": 1,
                    }
                ).encode("utf-8"),
            ),
            (
                200,
                json.dumps(
                    {"turns": [_history_turn(2)], "has_more": False}
                ).encode("utf-8"),
            ),
        )
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=transport,
        )

        bootstrap = session.bootstrap()

        self.assertEqual(bootstrap["backend"], "server")
        self.assertEqual([item["turn_no"] for item in bootstrap["history"]], [1, 2])
        self.assertNotIn("turn_code", bootstrap["history"][0])
        self.assertNotIn("provider", bootstrap["history"][0])
        history_calls = transport.calls[2:]
        self.assertEqual(len(history_calls), 2)
        self.assertEqual(
            [parse_qs(urlsplit(call["url"]).query)["after_turn_no"] for call in history_calls],
            [["0"], ["1"]],
        )
        self.assertTrue(
            all(call["headers"]["User-Agent"] == runner.USER_AGENT for call in history_calls)
        )

    def test_history_accepts_empty_terminal_page(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, b'{"turns":[],"has_more":false}'),
        )
        session = HttpChatTurnTests._session(transport)

        self.assertEqual(session.get_history(), [])
        self.assertEqual(len(transport.calls), 2)

    def test_history_accepts_omitted_nullable_terminal_fields(self):
        cases = []
        completed = _history_turn(1)
        completed.pop("error_code")
        cases.append((completed, "assistant_text", "answer 1", "error_code", None))
        for status in ("failed", "cancelled"):
            turn = _history_turn(1, status=status)
            turn.pop("assistant_text")
            cases.append((turn, "assistant_text", None, "error_code", turn["error_code"]))

        for turn, first_key, first_value, second_key, second_value in cases:
            with self.subTest(status=turn["status"]):
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    _history_response(turn),
                )
                session = HttpChatTurnTests._session(transport)

                history = session.get_history()

                self.assertEqual(history[0][first_key], first_value)
                self.assertEqual(history[0][second_key], second_value)

    def test_history_still_rejects_missing_status_required_terminal_fields(self):
        invalid_turns = []
        completed = _history_turn(1)
        completed.pop("assistant_text")
        invalid_turns.append(completed)
        for status in ("failed", "cancelled"):
            turn = _history_turn(1, status=status)
            turn.pop("error_code")
            invalid_turns.append(turn)
        failed = _history_turn(1, status="failed")
        failed["error_code"] = "provider_internal_detail"
        invalid_turns.append(failed)
        failed_as_cancelled = _history_turn(1, status="failed")
        failed_as_cancelled["error_code"] = "cancelled"
        invalid_turns.append(failed_as_cancelled)

        for turn in invalid_turns:
            with self.subTest(status=turn["status"], keys=sorted(turn)):
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    _history_response(turn),
                )
                session = HttpChatTurnTests._session(transport)

                with self.assertRaises(ChatClientError) as caught:
                    session.get_history()

                self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_has_more_without_cursor(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {"turns": [_history_turn(1)], "has_more": True}
                ).encode("utf-8"),
            ),
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.get_history()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_non_boolean_or_missing_has_more(self):
        invalid_pages = (
            {"turns": [], "has_more": "true"},
            {"turns": [], "has_more": 1},
            {"turns": [], "has_more": None},
            {"turns": []},
        )
        for page in invalid_pages:
            with self.subTest(page=page):
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (200, json.dumps(page).encode("utf-8")),
                )
                session = HttpChatTurnTests._session(transport)

                with self.assertRaises(ChatClientError) as caught:
                    session.get_history()

                self.assertEqual(caught.exception.code, "bad_response")

    def test_history_normalizes_non_object_json_to_bad_response(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, b"[]"),
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.get_history()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_empty_nonterminal_page(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, b'{"turns":[],"has_more":true,"next_after_turn_no":0}'),
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.get_history()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_boolean_cursor(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "turns": [_history_turn(1)],
                        "has_more": True,
                        "next_after_turn_no": True,
                    }
                ).encode("utf-8"),
            ),
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.get_history()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_terminal_page_with_cursor(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "turns": [_history_turn(1)],
                        "has_more": False,
                        "next_after_turn_no": 1,
                    }
                ).encode("utf-8"),
            ),
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.get_history()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_cursor_that_differs_from_last_turn(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "turns": [_history_turn(1)],
                        "has_more": True,
                        "next_after_turn_no": 2,
                    }
                ).encode("utf-8"),
            ),
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.get_history()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_unknown_envelope_field(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, b'{"turns":[],"has_more":false,"unexpected":null}'),
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.get_history()

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_page_larger_than_requested_limit(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            _history_response(_history_turn(1), _history_turn(2)),
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.get_history(limit=1)

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_duplicate_turn_number_without_returning_partial_history(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            _history_response(_history_turn(1), has_more=True),
            _history_response(_history_turn(1)),
        )
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=transport,
        )
        session.create_or_restore_conversation()

        with self.assertRaises(ChatClientError) as caught:
            session.get_history(limit=1)

        self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_js_unsafe_integer_fields(self):
        cases = (
            ("turn_no", None),
            ("completed_at", None),
            ("images", "size_bytes"),
        )
        for field, nested_field in cases:
            with self.subTest(field=field, nested_field=nested_field):
                turn = _history_turn(1)
                if nested_field is None:
                    turn[field] = 1 << 53
                else:
                    turn[field][0][nested_field] = 1 << 53
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    _history_response(turn),
                )
                session = HttpChatTurnTests._session(transport)

                with self.assertRaises(ChatClientError) as caught:
                    session.get_history()

                self.assertEqual(caught.exception.code, "bad_response")

    def test_history_rejects_100_nonterminal_pages_at_hard_limit(self):
        responses = [
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            *(
                _history_response(_history_turn(number), has_more=True)
                for number in range(1, 101)
            ),
        ]
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=_ScriptedTransport(*responses),
        )
        session.create_or_restore_conversation()

        with self.assertRaises(ChatClientError) as caught:
            session.get_history(limit=1)

        self.assertEqual(caught.exception.code, "bad_response")

    def test_disabled_capability_stops_bootstrap_before_create(self):
        transport = _ScriptedTransport((200, b'{"ge_chat_v1":false}'))
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=transport,
        )

        with self.assertRaises(ChatClientError) as caught:
            session.bootstrap()

        self.assertEqual(caught.exception.code, "capability_disabled")
        self.assertEqual(len(transport.calls), 1)

    def test_history_has_an_aggregate_memory_budget(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            _history_response(_history_turn(1)),
        )
        session = HttpChatTurnTests._session(transport)

        with (
            mock.patch.object(http_chat, "_MAX_HISTORY_BYTES", 64, create=True),
            self.assertRaises(ChatClientError) as caught,
        ):
            session.get_history()

        self.assertEqual(caught.exception.code, "bad_response")
        self.assertEqual(len(transport.calls), 2)


_TURN_KEY = "123e4567-e89b-42d3-a456-426614174000"
_TURN_KEY_2 = "123e4567-e89b-42d3-a456-426614174001"


def _data_url(media_type, payload):
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _submit_response(*, price=0, poll_after_ms=250):
    return {
        "turn_code": "turn-test",
        "status": "queued",
        "phase": "queued",
        "price_credits": price,
        "poll_after_ms": poll_after_ms,
    }


def _terminal_submit_response(status):
    value = {
        "turn_code": "turn-test",
        "turn_no": 1,
        "status": status,
        "phase": "finalizing" if status == "completed" else "calling_model",
        "price_credits": 0,
        "user_text": "question",
        "completed_at": 1_787_587_201,
        "images": [],
        "turn_count": 1,
        "remaining_turns": 39,
    }
    if status == "completed":
        value["assistant_text"] = "answer"
    else:
        value["error_code"] = "cancelled" if status == "cancelled" else "model_unavailable"
    return value


class _FixedRng:
    @staticmethod
    def random():
        return 0.5


class _ManualClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class HttpChatTurnTests(unittest.TestCase):
    def assert_cumulative_fill(self, deltas, expected):
        self.assertGreaterEqual(len(deltas), 1)
        self.assertLessEqual(len(deltas), http_chat._ANSWER_FILL_MAX_STEPS)
        self.assertEqual(deltas[-1], expected)
        for index in range(1, len(deltas)):
            previous, current = deltas[index - 1], deltas[index]
            self.assertTrue(current.startswith(previous))
            self.assertGreater(len(current), len(previous))

    @staticmethod
    def _session(transport, **kwargs):
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=transport,
            **kwargs,
        )
        session.create_or_restore_conversation()
        return session

    def test_conversation_create_429_rate_limit_is_not_auto_retried(self):
        transport = _ScriptedTransport(
            _ResponseWithHeaders(
                429,
                b'{"error_code":"server_busy"}',
                {"Retry-After": "7"},
            ),
            (200, json.dumps(_conversation_response()).encode("utf-8")),
        )
        session = HttpChatSession(
            endpoint="https://example.test",
            token="token",
            run_id="run",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        with self.assertRaises(ChatClientError) as caught:
            session.create_or_restore_conversation()

        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(transport.calls), 1)

    def test_submit_unknown_response_reuses_identical_idempotency_key_and_accepts_zero_price(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            OSError("unknown response"),
            (200, json.dumps(_submit_response(price=0)).encode("utf-8")),
        )
        session = self._session(transport, sleep=lambda _seconds: None)

        result = session.submit_turn("question", [], _TURN_KEY)

        self.assertEqual(result["price_credits"], 0)
        submit_calls = transport.calls[1:]
        self.assertEqual(len(submit_calls), 2)
        for call in submit_calls:
            self.assertEqual(call["method"], "POST")
            self.assertEqual(call["headers"]["Idempotency-Key"], _TURN_KEY)
            self.assertEqual(call["headers"]["User-Agent"], runner.USER_AGENT)
            self.assertEqual(json.loads(call["body"]), {"text": "question", "images": []})
            self.assertEqual(call["timeout_s"], 120.0)

    def test_submit_429_rate_limit_error_code_is_retryable(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            _ResponseWithHeaders(
                429,
                b'{"error_code":"server_busy"}',
                {"Retry-After": "7"},
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None)

        with self.assertRaises(ChatClientError) as caught:
            session.submit_turn("question", [], _TURN_KEY)

        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.http_status, 429)
        submit_calls = [
            call for call in transport.calls
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_calls), 1)

    def test_submit_503_remains_temporary_service_failure(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (503, b'{"error_code":"rate_limited"}'),
            (503, b'{"error_code":"rate_limited"}'),
            (503, b'{"error_code":"rate_limited"}'),
        )
        session = self._session(transport, sleep=lambda _seconds: None)

        with self.assertRaises(ChatClientError) as caught:
            session.submit_turn("question", [], _TURN_KEY)

        self.assertEqual(caught.exception.code, "network_error")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.http_status, 503)

    def test_submit_accepts_strict_terminal_idempotency_replays(self):
        expected = {
            "completed": {
                "turn_code": "turn-test",
                "status": "completed",
                "phase": "finalizing",
                "price_credits": 0,
                "text": "answer",
                "turn_count": 1,
                "remaining_turns": 39,
            },
            "failed": {
                "turn_code": "turn-test",
                "status": "failed",
                "phase": "calling_model",
                "price_credits": 0,
                "error_code": "model_unavailable",
            },
            "cancelled": {
                "turn_code": "turn-test",
                "status": "cancelled",
                "phase": "calling_model",
                "price_credits": 0,
                "error_code": "cancelled",
            },
        }
        for status, expected_result in expected.items():
            with self.subTest(status=status):
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (200, json.dumps(_terminal_submit_response(status)).encode("utf-8")),
                )
                session = self._session(transport)

                result = session.submit_turn("question", [], _TURN_KEY)

                self.assertEqual(result, expected_result)

    def test_submit_terminal_replay_rejects_wrong_phase_or_missing_required_fields(self):
        invalid = []
        wrong_phase = _terminal_submit_response("completed")
        wrong_phase["phase"] = "calling_model"
        invalid.append(wrong_phase)
        for status, field in (
            ("completed", "assistant_text"),
            ("failed", "error_code"),
            ("cancelled", "error_code"),
        ):
            value = _terminal_submit_response(status)
            value.pop(field)
            invalid.append(value)
        invalid_error = _terminal_submit_response("failed")
        invalid_error["error_code"] = "provider_internal_detail"
        invalid.append(invalid_error)
        failed_as_cancelled = _terminal_submit_response("failed")
        failed_as_cancelled["error_code"] = "cancelled"
        invalid.append(failed_as_cancelled)
        cancelled_as_failed = _terminal_submit_response("cancelled")
        cancelled_as_failed["error_code"] = "model_unavailable"
        invalid.append(cancelled_as_failed)

        for value in invalid:
            with self.subTest(status=value["status"], keys=sorted(value)):
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (200, json.dumps(value).encode("utf-8")),
                )
                session = self._session(transport)

                with self.assertRaises(ChatClientError) as caught:
                    session.submit_turn("question", [], _TURN_KEY)

                self.assertEqual(caught.exception.code, "bad_response")

    def test_submit_and_poll_do_not_guess_completed_answer_field(self):
        submit_value = _terminal_submit_response("completed")
        submit_value["text"] = submit_value.pop("assistant_text")
        poll_value = {
            "status": "completed",
            "assistant_text": "answer",
            "turn_count": 1,
            "remaining_turns": 39,
        }
        for operation, value in (("submit", submit_value), ("poll", poll_value)):
            with self.subTest(operation=operation):
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (200, json.dumps(value).encode("utf-8")),
                )
                session = self._session(transport)

                with self.assertRaises(ChatClientError) as caught:
                    if operation == "submit":
                        session.submit_turn("question", [], _TURN_KEY)
                    else:
                        session.poll_turn("turn-test")

                self.assertEqual(caught.exception.code, "bad_response")
                self.assertEqual(len(transport.calls), 2)

    def test_submit_normalizes_unhashable_status_and_phase_to_bad_response(self):
        for field, malformed in (("status", []), ("phase", {})):
            with self.subTest(field=field):
                value = _terminal_submit_response("failed")
                value[field] = malformed
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (200, json.dumps(value).encode("utf-8")),
                )
                session = self._session(transport)

                with self.assertRaises(ChatClientError) as caught:
                    session.submit_turn("question", [], _TURN_KEY)

                self.assertEqual(caught.exception.code, "bad_response")

    def test_poll_normalizes_unhashable_status_and_phase_to_bad_response(self):
        for field, malformed in (("status", []), ("phase", {})):
            with self.subTest(field=field):
                value = {
                    "status": "running",
                    "phase": "calling_model",
                    "poll_after_ms": 250,
                }
                value[field] = malformed
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (200, json.dumps(value).encode("utf-8")),
                )
                session = self._session(transport)

                with self.assertRaises(ChatClientError) as caught:
                    session.poll_turn("turn-test")

                self.assertEqual(caught.exception.code, "bad_response")

    def test_submit_and_poll_reject_status_phase_mismatches(self):
        malformed_pairs = (
            ("queued", "compacting"),
            ("queued", "running"),
            ("running", "queued"),
            ("running", "running"),
        )
        for operation in ("submit", "poll"):
            for status, phase in malformed_pairs:
                with self.subTest(operation=operation, status=status, phase=phase):
                    value = {
                        "status": status,
                        "phase": phase,
                        "poll_after_ms": 250,
                    }
                    if operation == "submit":
                        value.update({"turn_code": "turn-test", "price_credits": 0})
                    transport = _ScriptedTransport(
                        (200, json.dumps(_conversation_response()).encode("utf-8")),
                        (200, json.dumps(value).encode("utf-8")),
                    )
                    session = self._session(transport)

                    with self.assertRaises(ChatClientError) as caught:
                        if operation == "submit":
                            session.submit_turn("question", [], _TURN_KEY)
                        else:
                            session.poll_turn("turn-test")

                    self.assertEqual(caught.exception.code, "bad_response")

    def test_submit_and_poll_accept_formal_status_phase_pairs(self):
        valid_pairs = (
            ("queued", "queued"),
            ("running", "compacting"),
            ("running", "calling_model"),
            ("running", "finalizing"),
        )
        for operation in ("submit", "poll"):
            for status, phase in valid_pairs:
                with self.subTest(operation=operation, status=status, phase=phase):
                    value = {
                        "status": status,
                        "phase": phase,
                        "poll_after_ms": 250,
                    }
                    if operation == "submit":
                        value.update({"turn_code": "turn-test", "price_credits": 0})
                    transport = _ScriptedTransport(
                        (200, json.dumps(_conversation_response()).encode("utf-8")),
                        (200, json.dumps(value).encode("utf-8")),
                    )
                    session = self._session(transport)

                    if operation == "submit":
                        result = session.submit_turn("question", [], _TURN_KEY)
                    else:
                        result = session.poll_turn("turn-test")

                    self.assertEqual(result["status"], status)
                    self.assertEqual(result["phase"], phase)
                    self.assertEqual(result["poll_after_ms"], 250)
                    self.assertEqual(len(transport.calls), 2)
                    if operation == "submit":
                        self.assertEqual(result["turn_code"], "turn-test")
                        self.assertEqual(result["price_credits"], 0)

    def test_submit_and_completed_poll_reject_js_unsafe_integers(self):
        submit = _submit_response(price=1 << 53)
        session = self._session(
            _ScriptedTransport(
                (200, json.dumps(_conversation_response()).encode("utf-8")),
                (200, json.dumps(submit).encode("utf-8")),
            )
        )
        with self.assertRaises(ChatClientError) as caught:
            session.submit_turn("question", [], _TURN_KEY)
        self.assertEqual(caught.exception.code, "bad_response")

        submit = _submit_response(poll_after_ms=1 << 53)
        session = self._session(
            _ScriptedTransport(
                (200, json.dumps(_conversation_response()).encode("utf-8")),
                (200, json.dumps(submit).encode("utf-8")),
            )
        )
        with self.assertRaises(ChatClientError) as caught:
            session.submit_turn("question", [], _TURN_KEY)
        self.assertEqual(caught.exception.code, "bad_response")

        running = {
            "status": "running",
            "phase": "calling_model",
            "poll_after_ms": 1 << 53,
        }
        session = self._session(
            _ScriptedTransport(
                (200, json.dumps(_conversation_response()).encode("utf-8")),
                (200, json.dumps(running).encode("utf-8")),
            )
        )
        with self.assertRaises(ChatClientError) as caught:
            session.poll_turn("turn-test")
        self.assertEqual(caught.exception.code, "bad_response")

        for field in ("turn_count", "remaining_turns"):
            with self.subTest(field=field):
                completed = {
                    "status": "completed",
                    "text": "answer",
                    "turn_count": 1,
                    "remaining_turns": 39,
                }
                completed[field] = 1 << 53
                session = self._session(
                    _ScriptedTransport(
                        (200, json.dumps(_conversation_response()).encode("utf-8")),
                        (200, json.dumps(completed).encode("utf-8")),
                    )
                )
                with self.assertRaises(ChatClientError) as caught:
                    session.poll_turn("turn-test")
                self.assertEqual(caught.exception.code, "bad_response")

    def test_bad_idempotency_key_is_rejected_before_network(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8"))
        )
        session = self._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.submit_turn("question", [], _TURN_KEY + "\r\nInjected: yes")

        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(len(transport.calls), 1)

    def test_image_allowlist_magic_and_aggregate_caps_are_enforced_before_network(self):
        png = _data_url("image/png", b"\x89PNG\r\n\x1a\n")
        jpeg = _data_url("image/jpeg", b"\xff\xd8\xff")
        webp = _data_url("image/webp", b"RIFF\x00\x00\x00\x00WEBP")
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            *(
                (200, json.dumps(_submit_response()).encode("utf-8"))
                for _index in range(4)
            ),
        )
        session = self._session(transport)

        for image in (png, jpeg, webp):
            with self.subTest(valid=image[:24]):
                session.submit_turn("question", [image], _TURN_KEY)

        canonical = "data:image/png;base64,iVBORw0KGgoBAg=="
        alternate = "data:image/png;base64,iVBORw0KGgoBAh=="
        session.submit_turn("question", [alternate], _TURN_KEY)
        self.assertEqual(
            json.loads(transport.calls[-1]["body"])["images"],
            [canonical],
        )

        invalid_cases = (
            ([_data_url("image/png", b"not-a-png")], "invalid_request"),
            (["data:image/png;base64,%%%%"], "invalid_request"),
            (["data:image/png;base64,é"], "invalid_request"),
            (
                [
                    "data:image/png;base64,iVBORw0KGgoBAg==",
                    "data:image/png;base64,iVBORw0KGgoBAh==",
                ],
                "invalid_request",
            ),
            ([png] * 6, "input_too_large"),
            (
                [_data_url("image/png", b"\x89PNG\r\n\x1a\n" + b"x" * (4 * 1024 * 1024))],
                "input_too_large",
            ),
            (
                [
                    _data_url(
                        "image/png",
                        b"\x89PNG\r\n\x1a\n" + b"x" * (3 * 1024 * 1024),
                    )
                ]
                * 4,
                "input_too_large",
            ),
        )
        network_calls = len(transport.calls)
        for images, expected in invalid_cases:
            with self.subTest(expected=expected, count=len(images)):
                with self.assertRaises(ChatClientError) as caught:
                    session.submit_turn("question", images, _TURN_KEY)
                self.assertEqual(caught.exception.code, expected)
        with (
            mock.patch.object(http_chat, "_MAX_SUBMIT_JSON_BYTES", 32),
            self.assertRaises(ChatClientError) as caught,
        ):
            session.submit_turn("question", [png], _TURN_KEY)
        self.assertEqual(caught.exception.code, "input_too_large")
        with (
            mock.patch.object(http_chat, "_MAX_SUBMIT_JSON_BYTES", 64),
            self.assertRaises(ChatClientError) as caught,
        ):
            session.submit_turn("question", [canonical, alternate], _TURN_KEY)
        self.assertEqual(caught.exception.code, "input_too_large")
        self.assertEqual(len(transport.calls), network_calls)

    def test_privacy_mismatch_does_not_call_network(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8"))
        )
        session = self._session(transport)
        errors = []

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:stale",
            )
        )

        self.assertEqual(errors, ["privacy_confirmation_required"])
        self.assertEqual(len(transport.calls), 1)

    def test_run_turn_clamps_and_jitters_poll_delays_without_poll_idempotency_header(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response(price=0, poll_after_ms=0)).encode("utf-8")),
            (
                200,
                json.dumps(
                    {"status": "running", "phase": "calling_model", "poll_after_ms": 20_000}
                ).encode("utf-8"),
            ),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "answer",
                        "turn_count": 1,
                        "remaining_turns": 39,
                    }
                ).encode("utf-8"),
            ),
        )
        sleeps = []
        session = self._session(transport, sleep=sleeps.append, rng=_FixedRng())
        states = []
        deltas = []
        done = []
        errors = []

        asyncio.run(
            session.run_turn(
                "question",
                [],
                deltas.append,
                done.append,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assert_cumulative_fill(deltas, "answer")
        self.assertEqual(done, ["answer"])
        self.assertEqual(errors, [])
        self.assertEqual(sleeps[:2], [0.275, 11.0])
        self.assertEqual(len(sleeps[2:]), len(deltas) - 1)
        self.assertTrue(all(0 < delay <= 0.02 for delay in sleeps[2:]))
        self.assertEqual([state for state, _payload in states], [
            "submitting", "queued", "calling_model", "completed"
        ])
        poll_calls = transport.calls[2:]
        self.assertTrue(all("Idempotency-Key" not in call["headers"] for call in poll_calls))

    def test_completed_answer_uses_bounded_cumulative_delta_fill_before_done(self):
        answer = (
            "# Comparison\n\n"
            "百度与 Google 的差异需要从搜索、广告和生态三个层面分析。\n\n"
            "- **Search:** retrieval quality\n"
            "- **Ecosystem:** products and distribution"
        )
        completed = _terminal_submit_response("completed")
        completed["assistant_text"] = answer
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(completed).encode("utf-8")),
        )
        sleeps = []
        events = []
        deltas = []
        done = []
        session = self._session(transport, sleep=sleeps.append, rng=_FixedRng())

        def on_state(state, _payload):
            events.append(("state", state))

        def on_delta(value):
            deltas.append(value)
            events.append(("delta", value))

        def on_done(value):
            done.append(value)
            events.append(("done", value))

        asyncio.run(
            session.run_turn(
                "compare",
                [],
                on_delta,
                on_done,
                lambda _code: None,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=on_state,
            )
        )

        self.assertGreater(len(deltas), 1)
        self.assert_cumulative_fill(deltas, answer)
        self.assertEqual(done, [answer])
        self.assertEqual(len(sleeps), len(deltas) - 1)
        self.assertTrue(all(0 < delay <= 0.02 for delay in sleeps))
        self.assertLessEqual(sum(sleeps), 0.5)
        completed_index = events.index(("state", "completed"))
        self.assertTrue(all(event[0] == "delta" for event in events[1:completed_index]))
        self.assertEqual(events[completed_index + 1], ("done", answer))

    def test_single_character_answer_has_no_fill_delay(self):
        completed = _terminal_submit_response("completed")
        completed["assistant_text"] = "\u597d"
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(completed).encode("utf-8")),
        )
        sleeps = []
        deltas = []
        done = []
        session = self._session(transport, sleep=sleeps.append, rng=_FixedRng())

        asyncio.run(
            session.run_turn(
                "short",
                [],
                deltas.append,
                done.append,
                lambda _code: None,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
            )
        )

        self.assertEqual(deltas, ["\u597d"])
        self.assertEqual(done, ["\u597d"])
        self.assertEqual(sleeps, [])

    def test_delta_fill_abort_preserves_authoritative_done_and_releases_ownership(self):
        answer = "A completed answer long enough to produce several cumulative deltas."
        for failure_mode in ("reject", "raise"):
            with self.subTest(failure_mode=failure_mode):
                completed = _terminal_submit_response("completed")
                completed["assistant_text"] = answer
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (200, json.dumps(completed).encode("utf-8")),
                )
                sleeps = []
                deltas = []
                done = []
                session = self._session(
                    transport, sleep=sleeps.append, rng=_FixedRng()
                )

                def on_delta(value):
                    deltas.append(value)
                    if failure_mode == "raise":
                        raise RuntimeError("renderer unavailable")
                    return False

                asyncio.run(
                    session.run_turn(
                        "question",
                        [],
                        on_delta,
                        done.append,
                        lambda _code: None,
                        lambda _action: None,
                        client_turn_id=_TURN_KEY,
                        privacy_version="sha256:test",
                    )
                )

                self.assertEqual(len(deltas), 1)
                self.assertTrue(answer.startswith(deltas[0]))
                self.assertEqual(done, [answer])
                self.assertEqual(sleeps, [])
                self.assertIsNone(session._active)

    def test_delta_fill_handles_empty_invalid_and_large_inputs_with_existing_contract(self):
        session = self._session(
            _ScriptedTransport(
                (200, json.dumps(_conversation_response()).encode("utf-8"))
            ),
            sleep=lambda _seconds: None,
            rng=_FixedRng(),
        )
        empty_deltas = []

        session._emit_answer_deltas("", empty_deltas.append)
        session._emit_answer_deltas("ignored", None)

        self.assertEqual(empty_deltas, [""])

        large_answer = "x" * (1024 * 1024)
        large_deltas = []
        session._emit_answer_deltas(large_answer, large_deltas.append)

        self.assert_cumulative_fill(large_deltas, large_answer)
        self.assertEqual(len(large_deltas), http_chat._ANSWER_FILL_MAX_STEPS)

    def test_run_turn_publishes_terminal_submit_replay_without_polling(self):
        for status in ("completed", "failed", "cancelled"):
            with self.subTest(status=status):
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (200, json.dumps(_terminal_submit_response(status)).encode("utf-8")),
                )
                sleeps = []
                session = self._session(transport, sleep=sleeps.append, rng=_FixedRng())
                states = []
                deltas = []
                done = []
                errors = []

                asyncio.run(
                    session.run_turn(
                        "question",
                        [],
                        deltas.append,
                        done.append,
                        errors.append,
                        lambda _action: None,
                        client_turn_id=_TURN_KEY,
                        privacy_version="sha256:test",
                        on_state=lambda state, payload, _states=states: _states.append(
                            (state, payload)
                        ),
                    )
                )

                self.assertEqual(len(transport.calls), 2)
                self.assertEqual([state for state, _payload in states], ["submitting", status])
                if status == "completed":
                    self.assert_cumulative_fill(deltas, "answer")
                    self.assertEqual(done, ["answer"])
                    self.assertEqual(errors, [])
                    self.assertEqual(len(sleeps), len(deltas) - 1)
                else:
                    self.assertEqual(sleeps, [])
                    expected_error = "cancelled" if status == "cancelled" else "model_unavailable"
                    self.assertEqual(deltas, [])
                    self.assertEqual(done, [])
                    self.assertEqual(errors, [expected_error])

                record = session._terminal_records[_TURN_KEY]
                self.assertIsNone(session._active)
                self.assertFalse(record.running)
                self.assertTrue(record.attempt_done.is_set())
                self.assertTrue(record.handle_ready.is_set())
                self.assertEqual(record.text, "")
                self.assertEqual(record.images, [])

                replay_states = []
                replay_deltas = []
                replay_done = []
                replay_errors = []
                asyncio.run(
                    session.run_turn(
                        "question",
                        [],
                        replay_deltas.append,
                        replay_done.append,
                        replay_errors.append,
                        lambda _action: None,
                        client_turn_id=_TURN_KEY,
                        privacy_version="sha256:test",
                        on_state=lambda state, payload, _states=replay_states: _states.append(
                            (state, payload)
                        ),
                    )
                )

                self.assertEqual(len(transport.calls), 2)
                self.assertIs(session._terminal_records[_TURN_KEY], record)
                self.assertEqual(
                    [state for state, _payload in replay_states], [status]
                )
                if status == "completed":
                    self.assert_cumulative_fill(replay_deltas, "answer")
                    self.assertEqual(replay_done, ["answer"])
                    self.assertEqual(replay_errors, [])
                else:
                    self.assertEqual(replay_deltas, [])
                    self.assertEqual(replay_done, [])
                    self.assertEqual(replay_errors, [expected_error])

    def test_timeout_enters_recovery_and_same_key_replays_submit(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            TimeoutError("unknown submit response"),
            TimeoutError("unknown submit response"),
            TimeoutError("unknown submit response"),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "recovered answer",
                        "turn_count": 1,
                        "remaining_turns": 39,
                    }
                ).encode("utf-8"),
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        done = []
        kwargs = {
            "text": "question",
            "images": [],
            "on_delta": lambda _text: None,
            "on_done": done.append,
            "on_error": lambda _code: None,
            "on_action": lambda _action: None,
            "client_turn_id": _TURN_KEY,
            "privacy_version": "sha256:test",
            "on_state": lambda state, payload: states.append((state, payload)),
        }

        asyncio.run(session.run_turn(**kwargs))
        self.assertEqual(states[-1][0], "recovering")
        asyncio.run(session.run_turn(**kwargs))

        self.assertEqual(done, ["recovered answer"])
        submit_calls = [call for call in transport.calls if call["method"] == "POST" and call["url"].endswith("/turns")]
        self.assertEqual(len(submit_calls), 4)
        self.assertEqual({call["headers"]["Idempotency-Key"] for call in submit_calls}, {_TURN_KEY})

    def test_persistent_submit_500_enters_recovery_after_bounded_same_key_attempts(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (500, b'{"error_code":"internal_error"}'),
            (500, b'{"error_code":"internal_error"}'),
            (500, b'{"error_code":"internal_error"}'),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        errors = []

        asyncio.run(
            session.run_turn(
                "synthetic staging failure",
                [],
                lambda _text: None,
                lambda _text: None,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual([state for state, _payload in states], ["submitting", "recovering"])
        self.assertEqual(errors, [])
        submit_calls = [
            call
            for call in transport.calls
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_calls), 3)
        self.assertEqual(
            {call["headers"]["Idempotency-Key"] for call in submit_calls},
            {_TURN_KEY},
        )
        self.assertTrue(all(call["headers"]["User-Agent"] == runner.USER_AGENT for call in submit_calls))

    def test_definitive_submit_rejection_fails_and_releases_key_without_terminal_cache(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (402, b'{"error_code":"insufficient_credits"}'),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "new key accepted",
                        "turn_count": 1,
                        "remaining_turns": 39,
                    }
                ).encode("utf-8"),
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        errors = []

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual(states[-1], ("failed", {"error_code": "insufficient_credits"}))
        self.assertNotIn("recovering", [state for state, _payload in states])
        self.assertEqual(errors, ["insufficient_credits"])
        self.assertIsNone(session._active)
        self.assertNotIn(_TURN_KEY, session._terminal_records)

        done = []
        asyncio.run(
            session.run_turn(
                "next question",
                [],
                lambda _text: None,
                done.append,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY_2,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )
        self.assertEqual(done, ["new key accepted"])

    def test_auth_required_makes_session_unavailable_and_releases_active_key(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (401, b'{"error_code":"auth_required"}'),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        errors = []

        def run(key):
            asyncio.run(
                session.run_turn(
                    "question",
                    [],
                    lambda _text: None,
                    lambda _text: None,
                    errors.append,
                    lambda _action: None,
                    client_turn_id=key,
                    privacy_version="sha256:test",
                    on_state=lambda state, payload: states.append((state, payload)),
                )
            )

        run(_TURN_KEY)
        calls_after_auth_failure = len(transport.calls)
        run(_TURN_KEY_2)

        self.assertEqual(
            [item for item in states if item[0] == "unavailable"],
            [
                ("unavailable", {"error_code": "auth_required"}),
                ("unavailable", {"error_code": "auth_required"}),
            ],
        )
        self.assertEqual(errors, ["auth_required", "auth_required"])
        self.assertIsNone(session._active)
        self.assertEqual(len(transport.calls), calls_after_auth_failure)

    def test_auth_required_after_handle_is_still_global_unavailable_terminal(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (401, b'{"error_code":"auth_required"}'),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        errors = []

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual(states[-1], ("unavailable", {"error_code": "auth_required"}))
        self.assertEqual(errors, ["auth_required"])
        self.assertIsNone(session._active)
        self.assertNotIn(_TURN_KEY, session._terminal_records)

    def test_unknown_submit_shape_recovers_without_terminal_error_callback(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, b"{}"),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "same key recovered",
                        "turn_count": 1,
                        "remaining_turns": 39,
                    }
                ).encode("utf-8"),
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        errors = []
        done = []
        kwargs = {
            "text": "question",
            "images": [],
            "on_delta": lambda _text: None,
            "on_done": done.append,
            "on_error": errors.append,
            "on_action": lambda _action: None,
            "client_turn_id": _TURN_KEY,
            "privacy_version": "sha256:test",
            "on_state": lambda state, payload: states.append((state, payload)),
        }

        asyncio.run(session.run_turn(**kwargs))
        self.assertEqual(states[-1], ("recovering", {}))
        self.assertEqual(errors, [])
        asyncio.run(session.run_turn(**kwargs))

        self.assertEqual(done, ["same key recovered"])
        self.assertEqual(errors, [])
        submit_calls = [
            call for call in transport.calls
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_calls), 2)
        self.assertEqual(
            {call["headers"]["Idempotency-Key"] for call in submit_calls}, {_TURN_KEY}
        )

    def test_whole_turn_deadline_is_bounded_and_same_key_can_resume(self):
        running = {
            "status": "running",
            "phase": "calling_model",
            "poll_after_ms": 250,
        }
        completed = {
            "status": "completed",
            "text": "deadline recovery",
            "turn_count": 1,
            "remaining_turns": 39,
        }
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response(poll_after_ms=250)).encode("utf-8")),
            *((200, json.dumps(running).encode("utf-8")) for _index in range(3)),
            (200, json.dumps(completed).encode("utf-8")),
        )
        clock = _ManualClock()
        session = self._session(
            transport,
            clock=clock,
            sleep=clock.sleep,
            rng=_FixedRng(),
        )
        states = []
        done = []
        kwargs = {
            "text": "question",
            "images": [],
            "on_delta": lambda _text: None,
            "on_done": done.append,
            "on_error": lambda _code: None,
            "on_action": lambda _action: None,
            "client_turn_id": _TURN_KEY,
            "privacy_version": "sha256:test",
            "on_state": lambda state, payload: states.append((state, payload)),
        }

        with (
            mock.patch.object(http_chat, "_WHOLE_TURN_S", 1.0),
            mock.patch.object(http_chat, "_POLL_ATTEMPT_BUDGET_S", 0.1),
        ):
            asyncio.run(session.run_turn(**kwargs))
            self.assertEqual(states[-1][0], "recovering")
            self.assertEqual(done, [])
            asyncio.run(session.run_turn(**kwargs))

        self.assertEqual(done, ["deadline recovery"])
        submit_calls = [
            call for call in transport.calls
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_calls), 1)

    def test_submit_retries_do_not_start_when_whole_turn_budget_cannot_fit_them(self):
        clock = _ManualClock()

        class SlowUnknownSubmit(_ScriptedTransport):
            def request(inner_self, **request):
                if request["url"].endswith("/turns"):
                    inner_self.calls.append(request)
                    clock.value += 250.0
                    raise TimeoutError("unknown submit response")
                return super(SlowUnknownSubmit, inner_self).request(**request)

        transport = SlowUnknownSubmit(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
        )
        session = self._session(
            transport,
            clock=clock,
            sleep=clock.sleep,
            rng=_FixedRng(),
        )
        states = []

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                lambda _code: None,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual(states[-1][0], "recovering")
        submit_calls = [call for call in transport.calls if call["url"].endswith("/turns")]
        self.assertEqual(len(submit_calls), 1)

    def test_poll_retries_do_not_start_when_whole_turn_budget_cannot_fit_them(self):
        clock = _ManualClock()

        class SlowUnknownPoll(_ScriptedTransport):
            def request(inner_self, **request):
                if request["method"] == "GET" and "/v1/ge/chat/turns/" in request["url"]:
                    inner_self.calls.append(request)
                    clock.value += 310.0
                    raise TimeoutError("poll response unknown")
                return super(SlowUnknownPoll, inner_self).request(**request)

        transport = SlowUnknownPoll(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response()).encode("utf-8")),
        )
        session = self._session(
            transport,
            clock=clock,
            sleep=clock.sleep,
            rng=_FixedRng(),
        )
        states = []

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                lambda _code: None,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual(states[-1][0], "recovering")
        poll_calls = [
            call for call in transport.calls
            if call["method"] == "GET" and "/v1/ge/chat/turns/" in call["url"]
        ]
        self.assertEqual(len(poll_calls), 1)
        self.assertLessEqual(clock.value, http_chat._WHOLE_TURN_S)

    def test_poll_timeout_recovery_uses_known_handle_without_resubmit(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            TimeoutError("poll timeout"),
            TimeoutError("poll timeout"),
            TimeoutError("poll timeout"),
            TimeoutError("poll timeout"),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "known handle recovered",
                        "turn_count": 1,
                        "remaining_turns": 39,
                    }
                ).encode("utf-8"),
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        done = []
        args = (
            "question", [], lambda _text: None, done.append, lambda _code: None,
            lambda _action: None,
        )
        kwargs = {
            "client_turn_id": _TURN_KEY,
            "privacy_version": "sha256:test",
        }

        asyncio.run(session.run_turn(*args, **kwargs))
        asyncio.run(session.run_turn(*args, **kwargs))

        self.assertEqual(done, ["known handle recovered"])
        submit_calls = [
            call for call in transport.calls
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_calls), 1)

    def test_nonterminal_poll_protocol_error_does_not_fabricate_cached_failure(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (200, b"{"),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "server remained authoritative",
                        "turn_count": 1,
                        "remaining_turns": 39,
                    }
                ).encode("utf-8"),
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        done = []
        errors = []
        kwargs = {
            "text": "question",
            "images": [],
            "on_delta": lambda _text: None,
            "on_done": done.append,
            "on_error": errors.append,
            "on_action": lambda _action: None,
            "client_turn_id": _TURN_KEY,
            "privacy_version": "sha256:test",
            "on_state": lambda state, payload: states.append((state, payload)),
        }

        asyncio.run(session.run_turn(**kwargs))
        self.assertEqual(states[-1][0], "recovering")
        asyncio.run(session.run_turn(**kwargs))

        self.assertEqual(done, ["server remained authoritative"])
        self.assertNotIn("failed", [state for state, _payload in states])
        self.assertEqual(errors, [])
        submit_calls = [
            call for call in transport.calls
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_calls), 1)

    def test_retryable_failure_without_on_state_still_notifies_error_callback(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            *(TimeoutError("poll timeout") for _index in range(4)),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        errors = []

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
            )
        )

        self.assertEqual(errors, ["timeout"])

    def test_submit_429_rate_limit_enters_retryable_state_and_later_retry_succeeds(self):
        completed = _terminal_submit_response("completed")
        completed["assistant_text"] = "answer after waiting"
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            _ResponseWithHeaders(
                429,
                b'{"error_code":"server_busy"}',
                {"Retry-After": "7"},
            ),
            (200, json.dumps(completed).encode("utf-8")),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        done = []
        errors = []
        kwargs = {
            "text": "question",
            "images": [],
            "on_delta": lambda _text: None,
            "on_done": done.append,
            "on_error": errors.append,
            "on_action": lambda _action: None,
            "client_turn_id": _TURN_KEY,
            "privacy_version": "sha256:test",
            "on_state": lambda state, payload: states.append((state, payload)),
        }

        asyncio.run(session.run_turn(**kwargs))
        self.assertEqual(states, [
            ("submitting", {}),
            ("recovering", {"error_code": "rate_limited"}),
        ])
        self.assertEqual(done, [])
        self.assertEqual(errors, [])

        asyncio.run(session.run_turn(**kwargs))

        self.assertEqual(done, ["answer after waiting"])
        self.assertEqual(errors, [])

    def test_submit_429_without_state_callback_releases_turn_and_reports_error(self):
        first_completed = _terminal_submit_response("completed")
        first_completed["assistant_text"] = "fresh turn answer"
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            _ResponseWithHeaders(
                429,
                b'{"error_code":"server_busy"}',
                {"Retry-After": "7"},
            ),
            (200, json.dumps(first_completed).encode("utf-8")),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        errors = []

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=None,
            )
        )

        self.assertEqual(errors, ["rate_limited"])
        self.assertIsNone(session._active)

        done = []
        asyncio.run(
            session.run_turn(
                "fresh question",
                [],
                lambda _text: None,
                done.append,
                errors.append,
                lambda _action: None,
                client_turn_id=_TURN_KEY_2,
                privacy_version="sha256:test",
                on_state=None,
            )
        )

        self.assertEqual(done, ["fresh turn answer"])
        self.assertEqual(errors, ["rate_limited"])

    def test_callback_failure_isolated_and_terminal_cache_drops_payload_bytes(self):
        png = _data_url("image/png", b"\x89PNG\r\n\x1a\n")
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "callback-safe",
                        "turn_count": 1,
                        "remaining_turns": 39,
                    }
                ).encode("utf-8"),
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        done = []

        def broken_state(_state, _payload):
            raise RuntimeError("ui callback unavailable")

        asyncio.run(
            session.run_turn(
                "question",
                [png],
                lambda _text: None,
                done.append,
                lambda _code: None,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=broken_state,
            )
        )

        self.assertEqual(done, ["callback-safe"])
        record = session._terminal_records[_TURN_KEY]
        self.assertEqual(record.text, "")
        self.assertEqual(record.images, [])
        replayed = []
        asyncio.run(
            session.run_turn(
                "question",
                [png],
                lambda _text: None,
                replayed.append,
                lambda _code: None,
                lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
            )
        )
        self.assertEqual(replayed, ["callback-safe"])

    def test_terminal_replay_cache_has_an_aggregate_byte_budget(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "first-result" * 10,
                        "turn_count": 1,
                        "remaining_turns": 39,
                    }
                ).encode("utf-8"),
            ),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "second-result" * 10,
                        "turn_count": 2,
                        "remaining_turns": 38,
                    }
                ).encode("utf-8"),
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())

        with mock.patch.object(http_chat, "_MAX_TERMINAL_BYTES", 500, create=True):
            for key in (_TURN_KEY, _TURN_KEY_2):
                asyncio.run(
                    session.run_turn(
                        "question",
                        [],
                        lambda _text: None,
                        lambda _text: None,
                        lambda _code: None,
                        lambda _action: None,
                        client_turn_id=key,
                        privacy_version="sha256:test",
                    )
                )

        self.assertNotIn(_TURN_KEY, session._terminal_records)
        self.assertIn(_TURN_KEY_2, session._terminal_records)

    def test_orphan_busy_auto_refreshes_authoritative_history_before_same_key_submit(self):
        orphan_history = _history_turn(1)
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (409, b'{"error_code":"turn_in_flight"}'),
            _history_response(orphan_history, has_more=True),
            _history_response(),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (
                200,
                json.dumps(
                    {
                        "status": "completed",
                        "text": "orphan cleared",
                        "turn_count": 2,
                        "remaining_turns": 38,
                    }
                ).encode("utf-8"),
            ),
        )
        session = self._session(transport, sleep=lambda _seconds: None, rng=_FixedRng())
        states = []
        done = []
        actions = []

        def accept_history(action):
            actions.append(action)
            return True

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                done.append,
                lambda _code: None,
                accept_history,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual(done, ["orphan cleared"])
        self.assertIn("recovering", [state for state, _payload in states])
        self.assertEqual(
            actions,
            [{"type": "history_snapshot", "history": [{
                key: value for key, value in orphan_history.items()
                if key not in {"turn_code", "provider"}
            }]}],
        )
        submit_calls = [
            call for call in transport.calls
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_calls), 2)
        self.assertEqual({call["headers"]["Idempotency-Key"] for call in submit_calls}, {_TURN_KEY})
        history_index = next(
            index for index, call in enumerate(transport.calls)
            if "after_turn_no=" in call["url"]
        )
        resumed_submit_index = [
            index for index, call in enumerate(transport.calls)
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ][1]
        self.assertLess(history_index, resumed_submit_index)

    def test_orphan_recovery_accepts_terminal_submit_replay_without_polling(self):
        for status in ("completed", "failed", "cancelled"):
            with self.subTest(status=status):
                orphan_history = _history_turn(1)
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (409, b'{"error_code":"turn_in_flight"}'),
                    _history_response(orphan_history, has_more=True),
                    _history_response(),
                    (
                        200,
                        json.dumps(_terminal_submit_response(status)).encode("utf-8"),
                    ),
                )
                session = self._session(
                    transport, sleep=lambda _seconds: None, rng=_FixedRng()
                )
                deltas = []
                states = []
                done = []
                errors = []

                asyncio.run(
                    session.run_turn(
                        "question",
                        [],
                        deltas.append,
                        done.append,
                        errors.append,
                        lambda _action: True,
                        client_turn_id=_TURN_KEY,
                        privacy_version="sha256:test",
                        on_state=lambda state, payload, _states=states: _states.append(
                            (state, payload)
                        ),
                    )
                )

                if status == "completed":
                    self.assert_cumulative_fill(deltas, "answer")
                    self.assertEqual(done, ["answer"])
                    self.assertEqual(errors, [])
                else:
                    expected_error = (
                        "cancelled" if status == "cancelled" else "model_unavailable"
                    )
                    self.assertEqual(deltas, [])
                    self.assertEqual(done, [])
                    self.assertEqual(errors, [expected_error])
                self.assertEqual(
                    [state for state, _payload in states],
                    ["submitting", "recovering", status],
                )
                submit_calls = [
                    call
                    for call in transport.calls
                    if call["method"] == "POST" and call["url"].endswith("/turns")
                ]
                self.assertEqual(len(submit_calls), 2)
                self.assertEqual(
                    {call["headers"]["Idempotency-Key"] for call in submit_calls},
                    {_TURN_KEY},
                )
                self.assertFalse(
                    any(
                        call["method"] == "GET"
                        and "/v1/ge/chat/turns/" in call["url"]
                        for call in transport.calls
                    )
                )
                record = session._terminal_records[_TURN_KEY]
                self.assertIsNone(session._active)
                self.assertTrue(record.handle_ready.is_set())
                self.assertTrue(record.attempt_done.is_set())
                self.assertEqual(record.turn_code, "turn-test")

    def test_orphan_history_delivery_failure_keeps_recovering_without_resubmit(self):
        for callback in (
            None,
            lambda _snapshot: None,
            lambda _snapshot: False,
            mock.Mock(side_effect=RuntimeError("page gone")),
        ):
            with self.subTest(callback=callback):
                transport = _ScriptedTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (409, b'{"error_code":"conversation_busy"}'),
                    _history_response(_history_turn(1), has_more=True),
                    _history_response(),
                )
                session = self._session(
                    transport, sleep=lambda _seconds: None, rng=_FixedRng()
                )
                states = []
                errors = []

                asyncio.run(
                    session.run_turn(
                        "question",
                        [],
                        lambda _text: None,
                        lambda _text: None,
                        errors.append,
                        callback,
                        client_turn_id=_TURN_KEY,
                        privacy_version="sha256:test",
                        on_state=lambda state, payload, _states=states: _states.append(
                            (state, payload)
                        ),
                    )
                )

                self.assertEqual(states[-1][0], "recovering")
                submit_calls = [
                    call for call in transport.calls
                    if call["method"] == "POST" and call["url"].endswith("/turns")
                ]
                self.assertEqual(len(submit_calls), 1)
                self.assertFalse(session._active.orphan_history_delivered)
                self.assertFalse(session._active.running)
                self.assertEqual(session._history, [])
                self.assertEqual(errors, [])

    def test_orphan_recovery_phase_survives_ack_failure_and_second_invocation(self):
        orphan_history = _history_turn(1)

        class RetryTransport:
            def __init__(self):
                self.calls = []
                self.submit_count = 0

            def request(self, **request):
                self.calls.append(request)
                url = request["url"]
                if url.endswith("/v1/ge/chat/conversations"):
                    return 200, json.dumps(_conversation_response()).encode("utf-8")
                if url.endswith("/turns") and request["method"] == "POST":
                    self.submit_count += 1
                    if self.submit_count == 1:
                        return 409, b'{"error_code":"turn_in_flight"}'
                    return 200, json.dumps(_submit_response()).encode("utf-8")
                if "after_turn_no=0" in url:
                    return _history_response(orphan_history, has_more=True)
                if "after_turn_no=1" in url:
                    return _history_response()
                if "/v1/ge/chat/turns/" in url:
                    return 200, json.dumps(
                        {
                            "status": "completed",
                            "text": "recovered answer",
                            "turn_count": 2,
                            "remaining_turns": 38,
                        }
                    ).encode("utf-8")
                raise AssertionError(f"unexpected request: {request!r}")

        transport = RetryTransport()
        session = self._session(
            transport, sleep=lambda _seconds: None, rng=_FixedRng()
        )
        first_states = []
        first_actions = []

        def reject_history(action):
            first_actions.append(action)
            return False

        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                lambda _text: None,
                lambda _code: None,
                reject_history,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda state, payload: first_states.append((state, payload)),
            )
        )

        self.assertEqual(first_states[-1][0], "recovering")
        self.assertEqual(len(first_actions), 1)
        self.assertEqual(transport.submit_count, 1)
        self.assertFalse(session._active.running)
        first_attempt_call_count = len(transport.calls)

        second_actions = []
        done = []
        asyncio.run(
            session.run_turn(
                "question",
                [],
                lambda _text: None,
                done.append,
                lambda _code: None,
                lambda action: second_actions.append(action) or True,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
                on_state=lambda _state, _payload: None,
            )
        )

        self.assertEqual(done, ["recovered answer"])
        self.assertEqual(
            second_actions,
            [{"type": "history_snapshot", "history": [{
                key: value for key, value in orphan_history.items()
                if key not in {"turn_code", "provider"}
            }]}],
        )
        submit_indices = [
            index
            for index, call in enumerate(transport.calls)
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_indices), 2)
        second_history_index = next(
            index
            for index, call in enumerate(transport.calls)
            if index >= first_attempt_call_count and "after_turn_no=0" in call["url"]
        )
        self.assertLess(second_history_index, submit_indices[1])
        self.assertEqual(
            {transport.calls[index]["headers"]["Idempotency-Key"] for index in submit_indices},
            {_TURN_KEY},
        )

    def test_orphan_history_request_does_not_start_without_attempt_budget(self):
        clock = _ManualClock()

        class DeadlineTransport:
            def __init__(self):
                self.calls = []

            def request(self, **request):
                self.calls.append(request)
                if request["url"].endswith("/v1/ge/chat/conversations"):
                    return 200, json.dumps(_conversation_response()).encode("utf-8")
                if request["url"].endswith("/turns") and request["method"] == "POST":
                    clock.value = http_chat._WHOLE_TURN_S - 10.0
                    return 409, b'{"error_code":"turn_in_flight"}'
                raise AssertionError("orphan history request exceeded the whole-turn budget")

        transport = DeadlineTransport()
        session = self._session(
            transport, clock=clock, sleep=clock.sleep, rng=_FixedRng()
        )
        states = []

        asyncio.run(
            session.run_turn(
                "question", [], lambda _text: None, lambda _text: None,
                lambda _code: None, lambda _action: True,
                client_turn_id=_TURN_KEY, privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual(states[-1][0], "recovering")
        self.assertEqual(len(transport.calls), 2)
        self.assertLessEqual(clock.value, http_chat._WHOLE_TURN_S)

    def test_orphan_resubmit_does_not_start_without_submit_attempt_budget(self):
        clock = _ManualClock()

        class DeadlineTransport:
            def __init__(self):
                self.calls = []
                self.submit_count = 0
                self.history_page = 0

            def request(self, **request):
                self.calls.append(request)
                if request["url"].endswith("/v1/ge/chat/conversations"):
                    return 200, json.dumps(_conversation_response()).encode("utf-8")
                if request["url"].endswith("/turns") and request["method"] == "POST":
                    self.submit_count += 1
                    if self.submit_count == 1:
                        return 409, b'{"error_code":"turn_in_flight"}'
                    return 200, json.dumps(_submit_response()).encode("utf-8")
                if "after_turn_no=" in request["url"]:
                    self.history_page += 1
                    if self.history_page == 1:
                        clock.value = (
                            http_chat._WHOLE_TURN_S
                            - http_chat._SUBMIT_ATTEMPT_BUDGET_S
                            + 1.0
                        )
                        return _history_response(_history_turn(1), has_more=True)
                    return _history_response()
                if "/v1/ge/chat/turns/" in request["url"]:
                    return 200, json.dumps(
                        {
                            "status": "completed",
                            "text": "retry completed",
                            "turn_count": 2,
                            "remaining_turns": 38,
                        }
                    ).encode("utf-8")
                raise AssertionError("unexpected request")

        transport = DeadlineTransport()
        session = self._session(
            transport, clock=clock, sleep=clock.sleep, rng=_FixedRng()
        )
        states = []

        asyncio.run(
            session.run_turn(
                "question", [], lambda _text: None, lambda _text: None,
                lambda _code: None, lambda _action: True,
                client_turn_id=_TURN_KEY, privacy_version="sha256:test",
                on_state=lambda state, payload: states.append((state, payload)),
            )
        )

        self.assertEqual(states[-1][0], "recovering")
        self.assertEqual(transport.submit_count, 1)
        self.assertTrue(session._active.orphan_history_delivered)
        self.assertEqual(session._history[-1]["turn_no"], 1)

        first_attempt_call_count = len(transport.calls)
        done = []
        asyncio.run(
            session.run_turn(
                "question", [], lambda _text: None, done.append,
                lambda _code: None, lambda _action: True,
                client_turn_id=_TURN_KEY, privacy_version="sha256:test",
                on_state=lambda _state, _payload: None,
            )
        )

        self.assertEqual(done, ["retry completed"])
        self.assertEqual(transport.submit_count, 2)
        retry_calls = transport.calls[first_attempt_call_count:]
        retry_history_index = next(
            index for index, call in enumerate(retry_calls)
            if "after_turn_no=1" in call["url"]
        )
        retry_submit_index = next(
            index for index, call in enumerate(retry_calls)
            if call["method"] == "POST" and call["url"].endswith("/turns")
        )
        self.assertLess(retry_history_index, retry_submit_index)

    def test_orphan_empty_history_does_not_resubmit_before_acknowledged_delta(self):
        orphan_history = _history_turn(1)
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (409, b'{"error_code":"conversation_busy"}'),
            _history_response(),
            _history_response(),
            _history_response(orphan_history, has_more=True),
            _history_response(),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (200, json.dumps({
                "status": "completed",
                "text": "new turn completed",
                "turn_count": 2,
                "remaining_turns": 38,
            }).encode("utf-8")),
        )
        clock = _ManualClock()
        session = self._session(
            transport, clock=clock, sleep=clock.sleep, rng=_FixedRng()
        )
        actions = []

        def accept_history(action):
            actions.append(action)
            return True

        asyncio.run(
            session.run_turn(
                "new question", [], lambda _text: None, lambda _text: None,
                lambda _code: None, accept_history,
                client_turn_id=_TURN_KEY, privacy_version="sha256:test",
            )
        )

        submit_indices = [
            index for index, call in enumerate(transport.calls)
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        history_indices = [
            index for index, call in enumerate(transport.calls)
            if "after_turn_no=" in call["url"]
        ]
        self.assertEqual(len(submit_indices), 2)
        self.assertEqual(len(history_indices), 4)
        self.assertTrue(all(index < submit_indices[1] for index in history_indices))
        self.assertEqual(
            {transport.calls[index]["headers"]["Idempotency-Key"]
             for index in submit_indices},
            {_TURN_KEY},
        )
        self.assertEqual(len(actions), 1)

    def test_new_busy_fence_requires_a_fresh_acknowledged_history_delta(self):
        first_orphan = _history_turn(1)
        second_orphan = _history_turn(2)
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (409, b'{"error_code":"conversation_busy"}'),
            _history_response(first_orphan, has_more=True),
            _history_response(),
            (409, b'{"error_code":"turn_in_flight"}'),
            _history_response(),
            _history_response(second_orphan, has_more=True),
            _history_response(),
            (200, json.dumps(_submit_response()).encode("utf-8")),
            (200, json.dumps({
                "status": "completed",
                "text": "fresh fence cleared",
                "turn_count": 3,
                "remaining_turns": 37,
            }).encode("utf-8")),
        )
        session = self._session(
            transport, sleep=lambda _seconds: None, rng=_FixedRng()
        )
        actions = []
        done = []

        asyncio.run(
            session.run_turn(
                "new question", [], lambda _text: None, done.append,
                lambda _code: None, lambda action: actions.append(action) or True,
                client_turn_id=_TURN_KEY, privacy_version="sha256:test",
            )
        )

        self.assertEqual(done, ["fresh fence cleared"])
        self.assertEqual(
            [[turn["turn_no"] for turn in action["history"]] for action in actions],
            [[1], [1, 2]],
        )
        submit_indices = [
            index for index, call in enumerate(transport.calls)
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_indices), 3)
        self.assertEqual(
            {transport.calls[index]["headers"]["Idempotency-Key"] for index in submit_indices},
            {_TURN_KEY},
        )
        post_fence_cursor_one_requests = [
            index for index, call in enumerate(transport.calls)
            if "after_turn_no=1" in call["url"] and index > submit_indices[1]
        ]
        self.assertEqual(len(post_fence_cursor_one_requests), 2)
        second_delta_index = post_fence_cursor_one_requests[1]
        self.assertLess(second_delta_index, submit_indices[2])


class _BlockingPollTransport:
    def __init__(self):
        self.calls = []
        self.poll_started = threading.Event()
        self.release_poll = threading.Event()
        self.closed = False

    def request(self, **request):
        self.calls.append(request)
        url = request["url"]
        if url.endswith("/v1/ge/chat/conversations"):
            return 200, json.dumps(_conversation_response()).encode("utf-8")
        if url.endswith("/turns") and request["method"] == "POST":
            return 200, json.dumps(_submit_response()).encode("utf-8")
        if url.endswith("/cancel"):
            return 202, b""
        if "/v1/ge/chat/turns/" in url:
            self.poll_started.set()
            if not self.release_poll.wait(5):
                raise TimeoutError("test did not release poll")
            return 200, json.dumps(
                {
                    "status": "completed",
                    "text": "coalesced answer",
                    "turn_count": 1,
                    "remaining_turns": 39,
                }
            ).encode("utf-8")
        raise AssertionError(f"unexpected request: {request!r}")

    def close(self):
        self.closed = True


class _PendingCancelTransport:
    def __init__(self):
        self.calls = []
        self.submit_started = threading.Event()
        self.release_submit = threading.Event()

    def request(self, **request):
        self.calls.append(request)
        url = request["url"]
        if url.endswith("/v1/ge/chat/conversations"):
            return 200, json.dumps(_conversation_response()).encode("utf-8")
        if url.endswith("/turns") and request["method"] == "POST":
            self.submit_started.set()
            if not self.release_submit.wait(5):
                raise TimeoutError("test did not release submit")
            return 200, json.dumps(_submit_response()).encode("utf-8")
        if url.endswith("/cancel"):
            return 202, b""
        if "/v1/ge/chat/turns/" in url:
            return 200, json.dumps(
                {
                    "status": "completed",
                    "text": "server won cancel race",
                    "turn_count": 1,
                    "remaining_turns": 39,
                }
            ).encode("utf-8")
        raise AssertionError(f"unexpected request: {request!r}")


class _BlockingCancelCloseTransport(_BlockingPollTransport):
    def __init__(self):
        super().__init__()
        self.cancel_started = threading.Event()
        self.release_cancel = threading.Event()
        self.events = []

    def request(self, **request):
        if request["url"].endswith("/cancel"):
            self.calls.append(request)
            self.events.append("cancel_started")
            self.cancel_started.set()
            if not self.release_cancel.wait(5):
                raise TimeoutError("test did not release cancel")
            self.events.append("cancel_done")
            return 202, b""
        return super().request(**request)

    def close(self):
        self.events.append("transport_close")
        self.closed = True


class _CloseRecordingTransport(_ScriptedTransport):
    def __init__(self, *responses, close_error=None):
        super().__init__(*responses)
        self.close_error = close_error
        self.closed = False

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _BlockingTransportClose(_ScriptedTransport):
    def __init__(self, *responses):
        super().__init__(*responses)
        self.close_started = threading.Event()
        self.release_close = threading.Event()

    def close(self):
        self.close_started.set()
        self.release_close.wait(5)


class HttpChatCloseCancellationTests(unittest.TestCase):
    def test_close_before_handle_sends_cancel_once_after_handle(self):
        transport = _PendingCancelTransport()
        session = HttpChatTurnTests._session(
            transport, sleep=lambda _seconds: None, rng=_FixedRng()
        )
        done = []

        def run():
            asyncio.run(
                session.run_turn(
                    "question",
                    [],
                    lambda _text: None,
                    done.append,
                    lambda _code: None,
                    lambda _action: None,
                    client_turn_id=_TURN_KEY,
                    privacy_version="sha256:test",
                )
            )

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(transport.submit_started.wait(2))

        close_returned = threading.Event()
        closer = threading.Thread(target=lambda: (session.close(), close_returned.set()))
        closer.start()
        transport.release_submit.set()
        worker.join(timeout=5)
        closer.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertTrue(close_returned.is_set())
        cancel_calls = [call for call in transport.calls if call["url"].endswith("/cancel")]
        self.assertEqual(len(cancel_calls), 1)
        self.assertIsNone(cancel_calls[0]["body"])
        self.assertNotIn("Content-Type", cancel_calls[0]["headers"])

    def test_path_delimiter_in_server_handle_fails_closed_before_network(self):
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8"))
        )
        session = HttpChatTurnTests._session(transport)

        with self.assertRaises(ChatClientError) as caught:
            session.poll_turn("../turn/code")

        self.assertEqual(caught.exception.code, "bad_response")
        self.assertEqual(len(transport.calls), 1)

    def test_raw_error_body_and_token_do_not_escape_public_boundary(self):
        token = "TOKEN-" + "SENTINEL-BOUNDARY"
        body = b'{"error_code":"conversation_busy","message":"RAW-PROVIDER-SENTINEL"}'
        transport = _ScriptedTransport(
            (200, json.dumps(_conversation_response()).encode("utf-8")),
            (409, body),
        )
        before_argv = list(sys.argv)
        before_env = dict(os.environ)
        session = HttpChatSession(
            endpoint="https://example.test",
            token=token,
            run_id="run",
            transport=transport,
        )
        session.create_or_restore_conversation()

        with self.assertRaises(ChatClientError) as caught:
            session.submit_turn("question", [], _TURN_KEY)

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertEqual(caught.exception.code, "conversation_busy")
        self.assertNotIn(token, rendered)
        self.assertNotIn("RAW-PROVIDER-SENTINEL", rendered)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(sys.argv, before_argv)
        self.assertEqual(dict(os.environ), before_env)
        self.assertNotIn(token, Path(__file__).read_text(encoding="utf-8"))
        self.assertTrue(all(token not in call["url"] for call in transport.calls))

    def test_same_key_concurrent_call_coalesces_without_second_submit(self):
        transport = _BlockingPollTransport()
        session = HttpChatTurnTests._session(
            transport, sleep=lambda _seconds: None, rng=_FixedRng()
        )
        done = [[], []]

        def run(index):
            asyncio.run(
                session.run_turn(
                    "question", [], lambda _text: None, done[index].append,
                    lambda _code: None, lambda _action: None,
                    client_turn_id=_TURN_KEY,
                    privacy_version="sha256:test",
                )
            )

        first = threading.Thread(target=run, args=(0,))
        first.start()
        self.assertTrue(transport.poll_started.wait(2))
        second_started = threading.Event()

        def run_second():
            second_started.set()
            run(1)

        second = threading.Thread(target=run_second)
        second.start()
        self.assertTrue(second_started.wait(1))
        second.join(timeout=0.5)
        self.assertTrue(second.is_alive(), "same-key follower did not coalesce with the active owner")
        poll_calls = [
            call for call in transport.calls
            if call["method"] == "GET" and "/v1/ge/chat/turns/" in call["url"]
        ]
        self.assertEqual(len(poll_calls), 1, "reconnect must not create a second poll owner")
        transport.release_poll.set()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertEqual(done, [["coalesced answer"], ["coalesced answer"]])
        replayed = []
        asyncio.run(
            session.run_turn(
                "question", [], lambda _text: None, replayed.append,
                lambda _code: None, lambda _action: None,
                client_turn_id=_TURN_KEY,
                privacy_version="sha256:test",
            )
        )
        self.assertEqual(replayed, ["coalesced answer"])
        submit_calls = [
            call for call in transport.calls
            if call["method"] == "POST" and call["url"].endswith("/turns")
        ]
        self.assertEqual(len(submit_calls), 1)
        poll_calls = [
            call for call in transport.calls
            if call["method"] == "GET" and "/v1/ge/chat/turns/" in call["url"]
        ]
        self.assertEqual(len(poll_calls), 1)

    def test_close_cancels_known_handle_before_transport_close(self):
        transport = _BlockingPollTransport()
        session = HttpChatTurnTests._session(
            transport, sleep=lambda _seconds: None, rng=_FixedRng()
        )
        worker = threading.Thread(
            target=lambda: asyncio.run(
                session.run_turn(
                    "question", [], lambda _text: None, lambda _text: None,
                    lambda _code: None, lambda _action: None,
                    client_turn_id=_TURN_KEY,
                    privacy_version="sha256:test",
                )
            )
        )
        worker.start()
        self.assertTrue(transport.poll_started.wait(2))

        session.close()

        self.assertTrue(transport.closed)
        self.assertEqual(session._token, "")
        self.assertTrue(any(call["url"].endswith("/cancel") for call in transport.calls))
        transport.release_poll.set()
        worker.join(timeout=5)

    def test_close_waits_for_already_in_flight_cancel_before_transport_close(self):
        transport = _BlockingCancelCloseTransport()
        session = HttpChatTurnTests._session(
            transport, sleep=lambda _seconds: None, rng=_FixedRng()
        )
        worker = threading.Thread(
            target=lambda: asyncio.run(
                session.run_turn(
                    "question", [], lambda _text: None, lambda _text: None,
                    lambda _code: None, lambda _action: None,
                    client_turn_id=_TURN_KEY,
                    privacy_version="sha256:test",
                )
            )
        )
        worker.start()
        self.assertTrue(transport.poll_started.wait(2))

        close_returned = threading.Event()
        closer = threading.Thread(target=lambda: (session.close(), close_returned.set()))
        closer.start()
        self.assertTrue(transport.cancel_started.wait(2))
        try:
            self.assertFalse(
                close_returned.wait(0.1),
                "close returned before an already in-flight cancel completed",
            )
            self.assertFalse(transport.closed)
        finally:
            transport.release_cancel.set()
        closer.join(timeout=1)

        self.assertFalse(closer.is_alive())
        self.assertEqual(transport.events[-2:], ["cancel_done", "transport_close"])
        self.assertEqual(session._token, "")
        transport.release_poll.set()
        worker.join(timeout=5)
        self.assertEqual(session._terminal_records, {})

    def test_close_cancel_wait_is_bounded_by_single_total_budget(self):
        transport = _BlockingCancelCloseTransport()
        session = HttpChatTurnTests._session(
            transport, sleep=lambda _seconds: None, rng=_FixedRng()
        )
        worker = threading.Thread(
            target=lambda: asyncio.run(
                session.run_turn(
                    "question", [], lambda _text: None, lambda _text: None,
                    lambda _code: None, lambda _action: None,
                    client_turn_id=_TURN_KEY,
                    privacy_version="sha256:test",
                )
            )
        )
        worker.start()
        self.assertTrue(transport.poll_started.wait(2))

        started = http_chat.time.monotonic()
        with mock.patch.object(http_chat, "_CLOSE_BUDGET_S", 0.05):
            closer = threading.Thread(target=session.close)
            closer.start()
            self.assertTrue(transport.cancel_started.wait(2))
            closer.join(timeout=1)
        elapsed = http_chat.time.monotonic() - started

        self.assertFalse(closer.is_alive())
        self.assertLess(elapsed, 0.5)
        self.assertTrue(transport.closed)
        self.assertEqual(session._token, "")
        transport.release_cancel.set()
        transport.release_poll.set()
        worker.join(timeout=5)

    def test_close_zeroizes_after_thread_or_transport_close_failures(self):
        cases = ("start", "join", "transport")
        for failure in cases:
            with self.subTest(failure=failure):
                close_error = RuntimeError("close failed") if failure == "transport" else None
                transport = _CloseRecordingTransport(
                    (200, json.dumps(_conversation_response()).encode("utf-8")),
                    (202, b""),
                    close_error=close_error,
                )
                session = HttpChatTurnTests._session(transport)
                session._active = http_chat._TurnRecord(
                    key=_TURN_KEY,
                    text="sensitive question",
                    images=[],
                    payload_digest=b"digest",
                    started_at=0.0,
                    turn_code="turn-test",
                )
                if failure == "start":
                    original_start = http_chat.threading.Thread.start
                    failed_once = False

                    def fail_first_start(worker, _original_start=original_start):
                        nonlocal failed_once
                        if not failed_once:
                            failed_once = True
                            raise RuntimeError("start failed")
                        return _original_start(worker)

                    patcher = mock.patch.object(
                        http_chat.threading.Thread,
                        "start",
                        autospec=True,
                        side_effect=fail_first_start,
                    )
                elif failure == "join":
                    patcher = mock.patch.object(
                        http_chat.threading.Thread,
                        "join",
                        side_effect=RuntimeError("join failed"),
                    )
                else:
                    patcher = mock.patch.object(http_chat, "_CLOSE_BUDGET_S", 3.0)

                with patcher:
                    session.close()

                self.assertTrue(transport.closed)
                self.assertEqual(session._token, "")
                self.assertIsNone(session._active)
                self.assertEqual(session._terminal_records, {})

    def test_close_remains_bounded_when_transport_close_stalls(self):
        transport = _BlockingTransportClose(
            (200, json.dumps(_conversation_response()).encode("utf-8"))
        )
        session = HttpChatTurnTests._session(transport)
        returned = threading.Event()

        def close_session():
            with mock.patch.object(http_chat, "_CLOSE_BUDGET_S", 0.05):
                session.close()
            returned.set()

        closer = threading.Thread(target=close_session)
        closer.start()
        self.assertTrue(transport.close_started.wait(1))
        try:
            self.assertTrue(
                returned.wait(0.5), "transport.close exceeded the total close budget"
            )
            self.assertEqual(session._token, "")
            self.assertIsNone(session._active)
        finally:
            transport.release_close.set()
        closer.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
