"""Behavior tests for the DB board fetch's bounded read (launcher.fetch_board_html).

fetch_board_html POSTs the board spec to ``/db/render`` under the device token and reads
the rendered HTML back. Like the two shim reads, it caps the body it pulls into memory AND
bounds the wall-clock the read may take, via the shared helper (http_safety.read_within_budget)
— urllib's per-op ``timeout`` is not a transfer deadline, so a hub that dribbles bytes would
otherwise hold the read for a very long time under the byte cap. Every failure normalizes to
BoardFetchError (the fail-closed contract open_board_popup depends on).

Driven against a REAL local socket (same shape as test_shim._MockHub) so the actual urllib
stack the helper runs on is exercised, not a mocked opener.
"""

from __future__ import annotations

import threading
import time
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from client.popup import launcher


class _BoardHandler(BaseHTTPRequestHandler):
    """Answers /db/render with fixed, dripped, or deliberately malformed chunked bodies."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)   # drain the posted spec so the socket is clean
        chunked = getattr(self.server, "chunked", None)
        if chunked is not None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            config = chunked if isinstance(chunked, dict) else {"parts": chunked}
            try:
                for part in config.get("parts", []):
                    self.wfile.write(f"{len(part):X}\r\n".encode("ascii"))
                    self.wfile.write(part)
                    self.wfile.write(b"\r\n")
                mode = config.get("mode", "complete")
                if mode == "truncated":
                    return
                if mode == "slow-size":
                    self.wfile.write(b"1")
                    self.wfile.flush()
                    self._dribble(config)
                    return
                if mode == "slow-trailer":
                    self.wfile.write(b"0\r\nX-Test:")
                    self.wfile.flush()
                    self._dribble(config)
                    return
                if mode == "malformed-size":
                    self.wfile.write(b"SENSITIVE-NOT-HEX\r\n")
                    self.wfile.flush()
                    return
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            return
        drip = getattr(self.server, "drip", None)
        if drip is not None:
            self.send_response(getattr(self.server, "status", 200))
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(64 * 1024 * 1024))
            self.end_headers()
            try:
                deadline = time.monotonic() + drip.get("max_s", 30.0)
                while time.monotonic() < deadline:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(drip["delay"])
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        body = getattr(self.server, "body", b"<html><body>board</body></html>")
        self.send_response(getattr(self.server, "status", 200))
        self.send_header("Content-Type", getattr(self.server, "content_type", "text/html"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dribble(self, config):
        deadline = time.monotonic() + config.get("max_s", 3.0)
        while time.monotonic() < deadline:
            self.wfile.write(b" ")
            self.wfile.flush()
            time.sleep(config.get("delay", 0.02))

    def log_message(self, *_args):  # silence
        pass


class _MockBoardHub:
    def __init__(self, *, body=None, status=200, content_type="text/html", drip=None, chunked=None):
        self.httpd = HTTPServer(("127.0.0.1", 0), _BoardHandler)
        self.httpd.body = body if body is not None else b"<html><body>board</body></html>"  # type: ignore[attr-defined]
        self.httpd.status = status              # type: ignore[attr-defined]
        self.httpd.content_type = content_type  # type: ignore[attr-defined]
        self.httpd.drip = drip                  # type: ignore[attr-defined]
        self.httpd.chunked = chunked            # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class BoardFetchWorkingPathTests(unittest.TestCase):
    def test_a_plain_200_returns_the_rendered_html(self):
        hub = _MockBoardHub(body=b"<html><body>hi</body></html>")
        self.addCleanup(hub.close)
        html = launcher.fetch_board_html({"stage": "x"}, base_url=hub.base, token="tok", timeout_s=5)
        self.assertEqual(html, "<html><body>hi</body></html>")

    def test_a_chunked_200_returns_the_rendered_html(self):
        hub = _MockBoardHub(chunked=[b"<html><body>", b"chunked</body></html>"])
        self.addCleanup(hub.close)
        html = launcher.fetch_board_html({"stage": "x"}, base_url=hub.base, token="tok", timeout_s=5)
        self.assertEqual(html, "<html><body>chunked</body></html>")


class OpenBoardPopupTitleTests(unittest.TestCase):
    def test_default_title_uses_localized_discussion_board_surface_name(self):
        seen = []

        def fake_open(spec, **_kwargs):
            seen.append(spec.title)
            return {"ok": True, "outcome": "committed", "result": {}}

        with mock.patch.object(launcher, "fetch_board_html", return_value="<html/>"), \
                mock.patch.object(launcher, "open_popup", side_effect=fake_open), \
                mock.patch.dict("os.environ", {"DE_UI_LOCALE": "zh-CN"}):
            out = launcher.open_board_popup({}, base_url="https://hub.example", token="tok")

        self.assertTrue(out["ok"])
        self.assertEqual(seen, ["Decision Engine - \u8ba8\u8bba\u677f"])

    def test_explicit_title_gets_product_prefix(self):
        seen = []

        def fake_open(spec, **_kwargs):
            seen.append(spec.title)
            return {"ok": True, "outcome": "committed", "result": {}}

        with mock.patch.object(launcher, "fetch_board_html", return_value="<html/>"), \
                mock.patch.object(launcher, "open_popup", side_effect=fake_open), \
                mock.patch.dict("os.environ", {"DE_UI_LOCALE": "zh-CN"}):
            launcher.open_board_popup(
                {}, base_url="https://hub.example", token="tok", title="Board title"
            )

        self.assertEqual(seen, ["Decision Engine - Board title"])

    def test_prefixed_title_is_not_prefixed_twice(self):
        seen = []

        def fake_open(spec, **_kwargs):
            seen.append(spec.title)
            return {"ok": True, "outcome": "committed", "result": {}}

        with mock.patch.object(launcher, "fetch_board_html", return_value="<html/>"), \
                mock.patch.object(launcher, "open_popup", side_effect=fake_open):
            launcher.open_board_popup(
                {}, base_url="https://hub.example", token="tok", title="Decision Engine - Board"
            )

        self.assertEqual(seen, ["Decision Engine - Board"])


class BoardFetchByteCapTests(unittest.TestCase):
    def test_an_oversized_body_is_detected_not_truncated(self):
        # A body one byte past the cap must FAIL, not return a truncated prefix that renders clean.
        legal = b"<html>" + b"x" * 40 + b"</html>"
        hub = _MockBoardHub(body=legal + b" ")
        self.addCleanup(hub.close)
        with mock.patch.object(launcher, "_MAX_BOARD_BYTES", len(legal)):
            with self.assertRaises(launcher.BoardFetchError) as caught:
                launcher.fetch_board_html({"stage": "x"}, base_url=hub.base, token="tok", timeout_s=5)
        message = str(caught.exception)
        self.assertIn("exceeds", message)
        self.assertIn("/db/render", message)
        self.assertNotIn("xxx", message)   # no server body reaches the model

    def test_an_oversized_chunked_body_is_detected_not_truncated(self):
        legal = b"<html>" + b"x" * 40 + b"</html>"
        hub = _MockBoardHub(chunked=[legal, b"SENSITIVE-TAIL"])
        self.addCleanup(hub.close)
        with mock.patch.object(launcher, "_MAX_BOARD_BYTES", len(legal)):
            with self.assertRaises(launcher.BoardFetchError) as caught:
                launcher.fetch_board_html({"stage": "x"}, base_url=hub.base, token="tok", timeout_s=5)
        message = str(caught.exception)
        self.assertIn("exceeds", message)
        self.assertNotIn("SENSITIVE", message)


class BoardFetchDeadlineTests(unittest.TestCase):
    def test_a_slow_stream_is_bounded_by_the_transfer_deadline(self):
        # The drip would run 30s; the deadline is 1s. Without the wall-clock bound the read
        # would hang for the full drip (bytes keep arriving, so the socket never times out and
        # the cap is never reached).
        hub = _MockBoardHub(drip={"delay": 0.02})
        self.addCleanup(hub.close)
        started = time.monotonic()
        with self.assertRaises(launcher.BoardFetchError) as caught:
            launcher.fetch_board_html({"stage": "x"}, base_url=hub.base, token="tok", timeout_s=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8.0, "the slow-stream read was not bounded by the deadline")
        self.assertIn("budget", str(caught.exception))
        self.assertNotIn(" " * 5, str(caught.exception))

    def test_slow_chunk_framing_is_interrupted_by_the_transfer_deadline(self):
        for mode in ("slow-size", "slow-trailer"):
            with self.subTest(mode=mode):
                hub = _MockBoardHub(
                    chunked={"mode": mode, "delay": 0.02, "max_s": 10.0})
                self.addCleanup(hub.close)
                started = time.monotonic()
                with self.assertRaises(launcher.BoardFetchError) as caught:
                    launcher.fetch_board_html(
                        {"stage": "x"}, base_url=hub.base, token="tok", timeout_s=0.25)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 1.0, "chunk framing outlived the total deadline")
                self.assertIn("budget", str(caught.exception))

    def test_a_slow_http_error_body_is_not_consumed(self):
        hub = _MockBoardHub(status=503, drip={"delay": 0.02, "max_s": 10.0})
        self.addCleanup(hub.close)
        started = time.monotonic()
        with self.assertRaises(launcher.BoardFetchError) as caught:
            launcher.fetch_board_html(
                {"stage": "x"}, base_url=hub.base, token="tok", timeout_s=0.25)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("HTTP 503", str(caught.exception))


class BoardFetchCompletenessTests(unittest.TestCase):
    def test_a_chunked_body_without_terminal_zero_fails_closed(self):
        hub = _MockBoardHub(chunked={"mode": "truncated", "parts": [b"<html>partial"]})
        self.addCleanup(hub.close)
        started = time.monotonic()
        with self.assertRaises(launcher.BoardFetchError) as caught:
            launcher.fetch_board_html(
                {"stage": "x"}, base_url=hub.base, token="tok", timeout_s=0.25)
        self.assertLess(time.monotonic() - started, 1.0)
        message = str(caught.exception)
        self.assertTrue(
            "invalid framing" in message or "budget" in message,
            "truncated chunked body did not fail through a fixed bounded-read error")
        self.assertNotIn("partial", message)

    def test_malformed_chunk_size_is_redacted_with_its_exception_chain(self):
        hub = _MockBoardHub(chunked={"mode": "malformed-size"})
        self.addCleanup(hub.close)
        with self.assertRaises(launcher.BoardFetchError) as caught:
            launcher.fetch_board_html(
                {"stage": "x"}, base_url=hub.base, token="tok", timeout_s=1)
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertIn("invalid framing", rendered)
        self.assertNotIn("SENSITIVE-NOT-HEX", rendered)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)


class BoardFetchErrorBodyTests(unittest.TestCase):
    def test_a_small_http_error_body_is_not_echoed(self):
        hub = _MockBoardHub(status=400, body=b"SENSITIVE-INSTRUCTION")
        self.addCleanup(hub.close)
        with self.assertRaises(launcher.BoardFetchError) as caught:
            launcher.fetch_board_html(
                {"stage": "x"}, base_url=hub.base, token="tok", timeout_s=5)
        self.assertEqual(str(caught.exception), "HTTP 400 from /db/render")
        self.assertIsNone(caught.exception.__context__)

    def test_an_oversized_http_error_body_is_discarded(self):
        hub = _MockBoardHub(status=400, body=b"SENSITIVE-ERROR-BYTES" * 30)
        self.addCleanup(hub.close)
        with self.assertRaises(launcher.BoardFetchError) as caught:
            launcher.fetch_board_html(
                {"stage": "x"}, base_url=hub.base, token="tok", timeout_s=5)
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertNotIn("SENSITIVE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
