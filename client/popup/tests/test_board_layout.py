"""Behavior coverage for the native discussion-board layout bridge."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from client.popup import launcher, native_shell


class _LayoutHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.requests.append((self.path, dict(self.headers), body))
        redirect_to = getattr(self.server, "redirect_to", None)
        if redirect_to:
            self.send_response(302)
            self.send_header("Location", redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = getattr(
            self.server,
            "payload",
            b'{"coords":[{"id":"n1","x":10,"y":20}],"edgePaths":[]}',
        )
        self.send_response(200)
        self.send_header(
            "Content-Type",
            getattr(self.server, "content_type", "application/json"),
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _LayoutHub:
    def __init__(self, *, payload=None, redirect_to=None, content_type="application/json"):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LayoutHandler)
        self.server.requests = []
        self.server.payload = (
            b'{"coords":[{"id":"n1","x":10,"y":20}],"edgePaths":[]}'
            if payload is None
            else payload
        )
        self.server.redirect_to = redirect_to
        self.server.content_type = content_type
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        host, port = self.server.server_address
        return "http://%s:%d" % (host, port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _config(base):
    return {"server_endpoint": base, "access_token": "tok_secret"}


class BoardLayoutFetchTests(unittest.TestCase):
    def test_authenticated_exact_request_returns_geometry(self):
        request = {
            "layout": "flow",
            "graph": {
                "nodes": [{"id": "n1", "text": "A", "_w": 120, "_h": 60}],
                "edges": [],
            },
        }
        with _LayoutHub() as hub, mock.patch(
            "client.runner.load_config", return_value=_config(hub.base)
        ):
            result = launcher.fetch_board_layout(request, timeout_s=2)

        self.assertEqual(result["coords"], [{"id": "n1", "x": 10, "y": 20}])
        self.assertEqual(result["edgePaths"], [])
        path, headers, body = hub.server.requests[0]
        self.assertEqual(path, "/db/layout")
        self.assertEqual(headers["Authorization"], "Bearer tok_secret")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(json.loads(body), request)

    def test_redirect_is_refused_without_contacting_target(self):
        with _LayoutHub() as target:
            with _LayoutHub(redirect_to=target.base + "/stolen") as hub, mock.patch(
                "client.runner.load_config", return_value=_config(hub.base)
            ):
                with self.assertRaises(launcher.BoardLayoutError):
                    launcher.fetch_board_layout(
                        {"layout": "flow", "graph": {"nodes": [], "edges": []}},
                        timeout_s=2,
                    )
        self.assertEqual(target.server.requests, [])

    def test_oversized_and_malformed_responses_fail_closed(self):
        for payload in (b"x" * 65, b"{not-json"):
            with self.subTest(payload=payload[:12]):
                with _LayoutHub(payload=payload) as hub, mock.patch(
                    "client.runner.load_config", return_value=_config(hub.base)
                ), mock.patch.object(launcher, "_MAX_LAYOUT_BYTES", 64):
                    with self.assertRaises(launcher.BoardLayoutError):
                        launcher.fetch_board_layout(
                            {"layout": "flow", "graph": {"nodes": [], "edges": []}},
                            timeout_s=2,
                        )

    def test_request_schema_and_encoded_size_fail_before_network(self):
        invalid_requests = (
            [],
            {"layout": "flow", "graph": {"nodes": [], "edges": []}, "other": True},
            {"layout": "flow", "graph": {"nodes": [{"id": ""}], "edges": []}},
            {"layout": "flow", "graph": {"nodes": [], "edges": [{}]}},
        )
        with mock.patch("client.popup.launcher.urllib.request.build_opener") as opener:
            for request in invalid_requests:
                with self.subTest(request=request), self.assertRaises(
                    launcher.BoardLayoutError
                ):
                    launcher.fetch_board_layout(request)
        opener.assert_not_called()

        request = {
            "layout": "flow",
            "graph": {"nodes": [{"id": "n1", "text": "x" * 100}], "edges": []},
        }
        with mock.patch.object(launcher, "_MAX_LAYOUT_BYTES", 64), mock.patch(
            "client.runner.load_config",
            return_value=_config("https://hub.invalid"),
        ), mock.patch("client.popup.launcher.urllib.request.build_opener") as opener:
            with self.assertRaises(launcher.BoardLayoutError):
                launcher.fetch_board_layout(request)
        opener.assert_not_called()

        with mock.patch.object(launcher, "_MAX_LAYOUT_NODES", 0), self.assertRaises(
            launcher.BoardLayoutError
        ):
            launcher.fetch_board_layout(request)
        with mock.patch.object(launcher, "_MAX_LAYOUT_DEPTH", 1), self.assertRaises(
            launcher.BoardLayoutError
        ):
            launcher.fetch_board_layout(
                {"layout": "flow", "graph": {"nodes": [], "edges": []}}
            )

    def test_wrong_content_type_and_invalid_geometry_fail_closed(self):
        payloads = (
            b'{"coords":{},"edgePaths":[]}',
            b'{"coords":[{"id":"n1","x":"bad","y":0}],"edgePaths":[]}',
            b'{"coords":[],"edgePaths":[null]}',
            b'{"coords":[],"edgePaths":[],"chrome":[]}',
        )
        with _LayoutHub(content_type="text/html") as hub, mock.patch(
            "client.runner.load_config", return_value=_config(hub.base)
        ):
            with self.assertRaises(launcher.BoardLayoutError):
                launcher.fetch_board_layout(
                    {"layout": "flow", "graph": {"nodes": [], "edges": []}},
                    timeout_s=2,
                )
        for payload in payloads:
            with self.subTest(payload=payload), _LayoutHub(payload=payload) as hub, mock.patch(
                "client.runner.load_config", return_value=_config(hub.base)
            ):
                with self.assertRaises(launcher.BoardLayoutError):
                    launcher.fetch_board_layout(
                        {"layout": "flow", "graph": {"nodes": [], "edges": []}},
                        timeout_s=2,
                    )

    def test_success_drops_unexpected_top_level_fields(self):
        payload = (
            b'{"coords":[],"edgePaths":[],"chrome":{"markerR":4},'
            b'"internalDiagnostic":"do not expose"}'
        )
        with _LayoutHub(payload=payload) as hub, mock.patch(
            "client.runner.load_config", return_value=_config(hub.base)
        ):
            result = launcher.fetch_board_layout(
                {"layout": "flow", "graph": {"nodes": [], "edges": []}},
                timeout_s=2,
            )
        self.assertEqual(
            result,
            {"coords": [], "edgePaths": [], "chrome": {"markerR": 4}},
        )

    def test_missing_device_token_fails_before_network(self):
        with mock.patch(
            "client.runner.load_config",
            return_value={"server_endpoint": "https://hub.invalid"},
        ), mock.patch("client.popup.launcher.urllib.request.build_opener") as opener:
            with self.assertRaises(launcher.BoardLayoutError):
                launcher.fetch_board_layout(
                    {"layout": "flow", "graph": {"nodes": [], "edges": []}}
                )
        opener.assert_not_called()


class PopupLayoutBridgeTests(unittest.TestCase):
    def test_popup_api_delegates_layout_without_persisting_credentials(self):
        request = {"layout": "flow", "graph": {"nodes": [], "edges": []}}
        geometry = {"coords": [], "edgePaths": []}
        api = native_shell.PopupApi("unused")
        with mock.patch.object(
            native_shell.launcher, "fetch_board_layout", return_value=geometry
        ) as fetch:
            self.assertEqual(api.layout(request), geometry)
        fetch.assert_called_once_with(request)
        self.assertNotIn("token", api.__dict__)
        self.assertNotIn("endpoint", api.__dict__)

    def test_popup_api_exposes_only_stable_layout_failure(self):
        api = native_shell.PopupApi("unused")
        with mock.patch.object(
            native_shell.launcher,
            "fetch_board_layout",
            side_effect=launcher.BoardLayoutError("secret transport detail"),
        ):
            with self.assertRaisesRegex(RuntimeError, "^layout unavailable$"):
                api.layout({"layout": "flow", "graph": {"nodes": [], "edges": []}})

    def test_popup_api_rejects_excess_concurrent_layout_calls(self):
        api = native_shell.PopupApi("unused")
        release = threading.Event()
        both_entered = threading.Event()
        count_lock = threading.Lock()
        entered = 0

        def blocked_fetch(_request):
            nonlocal entered
            with count_lock:
                entered += 1
                if entered == 2:
                    both_entered.set()
            release.wait(timeout=2)
            return {"coords": [], "edgePaths": []}

        results = []
        request = {"layout": "flow", "graph": {"nodes": [], "edges": []}}
        with mock.patch.object(
            native_shell.launcher, "fetch_board_layout", side_effect=blocked_fetch
        ):
            workers = [
                threading.Thread(target=lambda: results.append(api.layout(request)))
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            self.assertTrue(both_entered.wait(timeout=1))
            with self.assertRaisesRegex(RuntimeError, "^layout unavailable$"):
                api.layout(request)
            release.set()
            for worker in workers:
                worker.join(timeout=2)
        self.assertEqual(results, [{"coords": [], "edgePaths": []}] * 2)


if __name__ == "__main__":
    unittest.main()
