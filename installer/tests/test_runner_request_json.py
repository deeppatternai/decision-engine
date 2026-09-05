"""Behavior tests for the `audit` CLI's transport (client.runner.request_json).

request_json carries the device token as `Authorization: Bearer` on every call the CLI
and the stopper panel make (submit / status / events / result / cancel / devices-me, plus
the anonymous healthz and activation POST). It is the last of the four authenticated
fetch paths to get the shared redirect guard — the other three landed in d1f4ec5 / 6784e5b.

Two contracts are pinned here, both driven against a REAL local socket rather than a
mocked opener, so they exercise the actual urllib stack the fix depends on:
  * a hostile redirect must never receive the token (the target records ZERO requests);
  * EVERY failure path raises AuditError, and no hostile path echoes server-chosen bytes.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from client import http_safety, runner


class _MockHandler(BaseHTTPRequestHandler):
    """Records every request, then answers per the server's config: a ``redirect_to``
    emits ``status`` (default 302) + Location; ``raw`` goes on the wire verbatim (with
    ``declared_length`` overstating it to force a truncated read); otherwise ``body`` as
    JSON under ``status``."""

    def _respond(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        self.server.requests.append((self.command, self.path, dict(self.headers)))  # type: ignore[attr-defined]
        status = getattr(self.server, "status", 200)
        redirect_to = getattr(self.server, "redirect_to", None)
        if redirect_to:
            self.send_response(status if status != 200 else 302)
            self.send_header("Location", redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = getattr(self.server, "raw", None)
        if raw is None:
            raw = json.dumps(self.server.body).encode("utf-8")  # type: ignore[attr-defined]
        declared = getattr(self.server, "declared_length", None) or len(raw)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(declared))
        self.end_headers()
        self.wfile.write(raw)
        if declared > len(raw):
            self.close_connection = True   # hang up early → IncompleteRead on the client

    do_GET = _respond    # noqa: N815 (stdlib naming)
    do_POST = _respond   # noqa: N815

    def log_message(self, *_args):  # silence
        pass


class _MockHub:
    """A real in-process hub on 127.0.0.1 (real socket, real urllib — same shape as
    test_shim._MockHub). ``redirect_to`` turns it into a hostile redirector."""

    def __init__(self, body=None, *, status=200, redirect_to=None, raw=None, declared_length=None):
        self.httpd = HTTPServer(("127.0.0.1", 0), _MockHandler)
        self.httpd.body = {"ok": True} if body is None else body  # type: ignore[attr-defined]
        self.httpd.status = status              # type: ignore[attr-defined]
        self.httpd.redirect_to = redirect_to    # type: ignore[attr-defined]
        self.httpd.raw = raw                    # type: ignore[attr-defined]
        self.httpd.declared_length = declared_length  # type: ignore[attr-defined]
        self.httpd.requests = []                # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_port

    @property
    def requests(self):
        return self.httpd.requests  # type: ignore[attr-defined]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class _RequestJsonTestCase(unittest.TestCase):
    """Points DE_CONFIG_PATH at an empty temp dir so load_config() reads no real config,
    and every call passes an explicit token — which keeps request_json out of the
    lazy-activation branch (`token is None`), so no test can open a popup."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(
            runner.os.environ, {"DE_CONFIG_PATH": str(Path(tmp.name) / "config.json")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def hub(self, **kwargs):
        hub = _MockHub(**kwargs)
        self.addCleanup(hub.close)
        return hub


class CancelCommandContractTests(unittest.TestCase):
    def test_stopped_true_clears_the_local_active_run(self):
        args = Namespace(run_id="r1")
        with mock.patch.object(runner, "request_json", return_value={"stopped": True}), \
             mock.patch.object(runner, "clear_active_run") as clear, \
             mock.patch.object(runner, "print_json") as output:
            exit_code = runner.cmd_cancel(args)

        self.assertEqual(exit_code, 0)
        clear.assert_called_once_with("r1")
        output.assert_called_once_with({"stopped": True})

    def test_stopped_false_keeps_the_local_active_run_and_returns_nonzero(self):
        args = Namespace(run_id="r1")
        with mock.patch.object(runner, "request_json", return_value={"stopped": False}), \
             mock.patch.object(runner, "clear_active_run") as clear, \
             mock.patch.object(runner, "print_json") as output:
            exit_code = runner.cmd_cancel(args)

        self.assertEqual(exit_code, 1)
        clear.assert_not_called()
        output.assert_called_once_with({"stopped": False})

    def test_stopped_false_persists_an_authoritative_running_status(self):
        args = Namespace(run_id="r1")
        result = {"stopped": False, "status": "running"}
        registry = {"runs": {"r1": {"run_id": "r1", "status": "queued"}}}
        with mock.patch.object(runner, "request_json", return_value=result), \
             mock.patch.object(runner, "load_active_runs_registry", return_value=registry), \
             mock.patch.object(runner, "save_active_run") as save, \
             mock.patch.object(runner, "clear_active_run") as clear, \
             mock.patch.object(runner, "print_json"):
            exit_code = runner.cmd_cancel(args)

        self.assertEqual(exit_code, 1)
        clear.assert_not_called()
        save.assert_called_once_with({"run_id": "r1", "status": "running"})


class RequestJsonRedirectTests(_RequestJsonTestCase):
    """request_json sends the device token as an `Authorization: Bearer` header through a
    bare urlopen. urllib's DEFAULT opener follows 3xx and its HTTPRedirectHandler rebuilds
    the request with the ORIGINAL headers — so a hostile/compromised redirect handed the
    device token to whatever host it named. This transport must refuse redirects outright,
    the same way the other three authenticated fetches already do."""

    def test_refused_redirect_never_hands_the_token_to_the_target(self):
        attacker = self.hub()
        hub = self.hub(redirect_to=attacker.base + "/v1/audits/r1")

        with self.assertRaises(runner.AuditError):
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)

        # The point of the fix: the redirect was NOT followed, so the token never reached
        # the target. (Before the fix the attacker recorded a request carrying the Bearer.)
        self.assertEqual(attacker.requests, [], "the device token was sent to the redirect target")
        self.assertEqual(len(hub.requests), 1)

    def test_refused_redirect_reports_no_server_detail(self):
        # The failure contract: AuditError, and the message carries neither the token nor
        # any server-supplied detail. The target is a real local hub, so this asserts the
        # REFUSAL rather than an incidental DNS failure, and needs no network.
        attacker = self.hub()
        hub = self.hub(redirect_to=attacker.base + "/v1/audits/r1")
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)
        message = str(caught.exception)
        self.assertIn("/v1/audits/r1", message)
        self.assertNotIn("tok_secret", message)
        self.assertNotIn(attacker.base, message)

    def test_every_redirect_code_is_refused_and_none_reaches_the_target(self):
        # 307/308 preserve the method, 301/302/303 rewrite it to GET — all five would carry
        # the Authorization header onward, so all five must be refused.
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                attacker = self.hub()
                hub = self.hub(status=code, redirect_to=attacker.base + "/v1/audits/r1")
                with self.assertRaises(runner.AuditError):
                    runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                        token="tok_secret", timeout_s=5)
                self.assertEqual(attacker.requests, [])

    def test_a_redirected_post_body_never_reaches_the_target_either(self):
        # The submit path POSTs the artifact under the same Bearer.
        attacker = self.hub()
        hub = self.hub(redirect_to=attacker.base + "/v1/audits")
        with self.assertRaises(runner.AuditError):
            runner.request_json("POST", "/v1/audits", server_url=hub.base, token="tok_secret",
                                body={"artifact": "secret-source"}, timeout_s=5)
        self.assertEqual(attacker.requests, [])

    def test_anonymous_healthz_refuses_redirect_too(self):
        # token="" carries no Bearer, but a followed redirect would still let the TARGET
        # answer for the configured hub — the same reasoning that made activation refuse-all.
        attacker = self.hub()
        hub = self.hub(redirect_to=attacker.base + "/healthz")
        with self.assertRaises(runner.AuditError):
            runner.request_json("GET", "/healthz", server_url=hub.base, token="", timeout_s=5)
        self.assertEqual(attacker.requests, [])

    def test_the_guard_is_the_one_shared_implementation(self):
        # Not a copy that can drift: all four authenticated fetches must resolve to the
        # same class. (installer.shim and client.popup.launcher are pinned in test_shim.)
        self.assertIs(runner.NoRedirect, http_safety.NoRedirect)


class RequestJsonHostileResponseTests(_RequestJsonTestCase):
    """The threat model that motivates the redirect guard — a hostile or compromised hub —
    also reaches `response.read().decode("utf-8")`. These escaped as raw UnicodeDecodeError /
    IncompleteRead, breaking the "every failure raises AuditError" contract; and their str()
    carries server-chosen bytes, which must never reach model context."""

    def test_non_utf8_body_fails_closed_without_echoing_the_bytes(self):
        hub = self.hub(raw=b'{"ok":"\xff\xfe"}')
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)
        # UnicodeDecodeError's own message names the offending byte ("can't decode byte 0xff
        # in position 7") — the AuditError must not carry it onward to the model.
        self.assertNotIn("0xff", str(caught.exception))

    def test_truncated_body_fails_closed(self):
        # Content-Length promises 5000 bytes, the hub hangs up after 1 → IncompleteRead,
        # which subclasses http.client.HTTPException, not URLError/OSError.
        hub = self.hub(raw=b"{", declared_length=5000)
        with self.assertRaises(runner.AuditError):
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)

    def test_truncated_error_body_still_raises_audit_error(self):
        # The HTTPError arm reads the body to surface the hub's error message — that read
        # can ITSELF raise IncompleteRead, from inside the handler, escaping the try entirely.
        hub = self.hub(status=400, raw=b"{", declared_length=5000)
        with self.assertRaises(runner.AuditError):
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)

    def test_malformed_redirect_location_fails_closed(self):
        # urllib parses Location BEFORE consulting NoRedirect, so a malformed one raises
        # ValueError("Invalid IPv6 URL") from inside the opener rather than reaching the guard.
        hub = self.hub(redirect_to="http://[")
        with self.assertRaises(runner.AuditError):
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)

    def test_non_redirect_3xx_still_fails_closed_without_echoing_the_body(self):
        # A 3xx the stdlib doesn't treat as a redirect never reaches NoRedirect; it lands on
        # the 3xx arm anyway. No 3xx is part of the hub's JSON-error contract, so its body is
        # not a message worth echoing either.
        #
        # 300, not 304: CPython gives a 304 a zero-length body no matter what the server
        # sent (HTTPResponse.length = 0), so a 304 here would pass against the OLD
        # body-echoing code too and prove nothing. A 300's body IS readable.
        hub = self.hub(status=300, raw=b'{"error":"attacker-chosen"}')
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)
        self.assertNotIn("attacker-chosen", str(caught.exception))

    def test_a_malformed_location_refusal_closes_the_response(self):
        # The stdlib quotes + urlparses Location BEFORE consulting the guard, so a malformed
        # one used to raise ValueError from inside that parse — carrying no fp to close, and
        # leaving live HTTPResponses reachable off the error's __context__ (`from None`
        # suppresses display but keeps the chain). NoRedirect now refuses at the
        # http_error_30x seam, ahead of the parse, so this exits as a closeable HTTPError.
        hub = self.hub(redirect_to="http://[")
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)
        responses = self._responses_reachable_from(caught.exception)
        # Assert we actually found some: an empty walk would make the check below vacuous.
        self.assertTrue(responses, "found no response to check — the walk is not exercising the path")
        for response in responses:
            self.assertTrue(response.isclosed(), "a refused redirect left its response open")

    def _responses_reachable_from(self, exc):
        found, seen = [], set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            tb = exc.__traceback__
            while tb:
                for value in tb.tb_frame.f_locals.values():
                    if value.__class__.__name__ == "HTTPResponse":
                        found.append(value)
                tb = tb.tb_next
            exc = exc.__context__
        return found


class RequestJsonWorkingPathTests(_RequestJsonTestCase):
    """The guard must not break the paths that already worked."""

    def test_plain_200_still_returns_the_parsed_body(self):
        hub = self.hub(body={"run_id": "r1", "status": "completed"})
        self.assertEqual(
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5),
            {"run_id": "r1", "status": "completed"})
        method, path, headers = hub.requests[0]
        self.assertEqual((method, path), ("GET", "/v1/audits/r1"))
        self.assertEqual(headers["Authorization"], "Bearer tok_secret")
        self.assertEqual(headers["User-Agent"], runner.USER_AGENT)
        # Every hub fetch tags itself with the shell's resolved UI locale (a single supported
        # tag). Membership, not equality: the exact resolver chain is locked in test_i18n.
        self.assertIn(headers["Accept-Language"], ("en-US", "zh-CN"))

    def test_post_body_still_reaches_the_hub_over_https_context(self):
        # https_context() returns None when neither SSL_CERT_FILE nor certifi is present;
        # HTTPSHandler(context=None) must stay acceptable, since the opener is now always built.
        hub = self.hub(body={"run_id": "r1"})
        with mock.patch.object(runner, "https_context", return_value=None):
            self.assertEqual(
                runner.request_json("POST", "/v1/audits", server_url=hub.base,
                                    token="tok_secret", body={"artifact": "x"}, timeout_s=5),
                {"run_id": "r1"})
        self.assertEqual(hub.requests[0][0], "POST")

    def test_http_error_still_surfaces_the_hub_error_message(self):
        # The 4xx contract the CLI depends on: the hub's own {"error": ...} still reaches
        # the user. Only 3xx bodies are withheld.
        hub = self.hub(status=400, body={"error": "activation secret expired"})
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("POST", "/v1/devices/activate", server_url=hub.base,
                                token="", body={"activation_secret": "act_x"}, timeout_s=5)
        self.assertIn("activation secret expired", str(caught.exception))

    def test_invalid_json_body_still_reports_the_json_error(self):
        # json.JSONDecodeError is a ValueError; the new ValueError arm must not shadow it.
        hub = self.hub(raw=b"not json at all")
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)
        self.assertIn("invalid JSON response", str(caught.exception))

    def test_a_malformed_server_url_is_an_audit_error_that_still_names_the_typo(self):
        # base_url is the user's OWN config, not server bytes — so unlike the transport arms,
        # this one echoes it. A raw ValueError here would break the contract on the single
        # most likely user error in the function.
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("GET", "/v1/audits/r1", server_url="http://[", token="tok_secret")
        self.assertIn("http://[", str(caught.exception))

    def test_empty_body_still_returns_an_empty_dict(self):
        hub = self.hub(raw=b"")
        self.assertEqual(
            runner.request_json("POST", "/v1/audits/r1/cancel", server_url=hub.base,
                                token="tok_secret", body={}, timeout_s=5),
            {})

    def test_http_4xx_error_carries_the_status_code(self):
        # The stop panel needs to tell a definitive 404 (the hub purged / never registered a run)
        # apart from a transient network AuditError, so it can reap a vanished run instead of
        # polling it forever. request_json exposes the HTTP status on the raised AuditError.
        hub = self.hub(status=404, body={"error": "run_not_found"})
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("GET", "/v1/audits/r1", server_url=hub.base,
                                token="tok_secret", timeout_s=5)
        self.assertEqual(getattr(caught.exception, "status_code", None), 404)

    def test_http_json_error_carries_the_machine_readable_error_code(self):
        hub = self.hub(status=409, body={"error": "synchronous_workflow_id"})
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json(
                "GET",
                "/v1/audits/adj-sync-result",
                server_url=hub.base,
                token="tok_secret",
                timeout_s=5,
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            getattr(caught.exception, "error_code", None),
            "synchronous_workflow_id",
        )

    def test_a_non_http_audit_error_has_no_status_code(self):
        # A transport/URL failure is not an HTTP status — it must not masquerade as one, or the
        # panel would misread a network blip as "run gone" and reap a live run.
        with self.assertRaises(runner.AuditError) as caught:
            runner.request_json("GET", "/v1/audits/r1", server_url="http://[", token="tok_secret")
        self.assertIsNone(getattr(caught.exception, "status_code", None))


if __name__ == "__main__":
    unittest.main()
