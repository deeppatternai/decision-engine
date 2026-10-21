"""Behavior locks for Linux WebView activation and audit surfaces."""

from __future__ import annotations

import io
import json
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from installer import permanent_setup


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self


def _audit_webview_app(webview_panel):
    events = SimpleNamespace(
        closed=_FakeEvent(),
        closing=_FakeEvent(),
        minimized=_FakeEvent(),
        restored=_FakeEvent(),
        shown=_FakeEvent(),
    )
    window = SimpleNamespace(
        destroy=mock.Mock(),
        evaluate_js=mock.Mock(),
        events=events,
    )
    app = webview_panel.LinuxWebViewStopPanelApp(
        SimpleNamespace(create_window=lambda *_args, **_kwargs: window)
    )
    return app, window, events


class LinuxActivationWebViewTests(unittest.TestCase):
    def test_result_host_escapes_copy_and_keeps_accepted_dimensions(self):
        from installer import linux_gui_message

        html = linux_gui_message._render(
            {
                "title": "激活 <完成>",
                "message": "设备 & Agent 已连接",
                "button": "确定",
                "lang": "zh-Hans",
                "error": False,
            }
        )

        self.assertIn("激活 &lt;完成&gt;", html)
        self.assertIn("设备 &amp; Agent 已连接", html)
        self.assertNotIn("激活 <完成>", html)

        created = {}

        class FakeEvent:
            def __iadd__(self, _callback):
                return self

        window = SimpleNamespace(destroy=mock.Mock(), events=SimpleNamespace(closed=FakeEvent()))

        def create_window(*args, **kwargs):
            created.update(kwargs)
            return window

        def start(*_args, **_kwargs):
            bridge = threading.Thread(target=created["js_api"].dismiss)
            bridge.start()
            bridge.join()

        fake_webview = SimpleNamespace(create_window=create_window, start=start)
        payload = json.dumps(
            {
                "title": "激活完成",
                "message": "设备已连接",
                "button": "确定",
                "lang": "zh-Hans",
                "error": False,
            }
        )
        with (
            mock.patch.object(linux_gui_message.backend, "ensure_webview", return_value=True),
            mock.patch.dict(sys.modules, {"webview": fake_webview}),
            mock.patch.object(linux_gui_message.sys, "stdin", io.StringIO(payload)),
        ):
            self.assertEqual(linux_gui_message.main(), 0)

        self.assertEqual((created["width"], created["height"]), (520, 300))
        self.assertFalse(created["resizable"])

    def test_parent_treats_zero_host_exit_as_open_without_diagnostics(self):
        completed = SimpleNamespace(returncode=0, stderr="ignored renderer noise")
        with (
            mock.patch("installer.permanent_setup.subprocess.run", return_value=completed),
            mock.patch(
                "client.popup.backend.credential_free_environment", return_value={}
            ),
            mock.patch(
                "client.popup.backend._linux_user_manager_environment",
                return_value={"DISPLAY": ":0"},
            ),
            mock.patch.object(
                permanent_setup, "_setup_strings", return_value={"html_lang": "zh-Hans"}
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            opened = permanent_setup._show_linux_webview_message(
                "激活完成", "设备已连接", error=False
            )

        self.assertTrue(opened)
        self.assertEqual(stderr.getvalue(), "")

    def test_parent_reports_nonzero_host_exit_and_uses_fallback(self):
        completed = SimpleNamespace(returncode=4, stderr="renderer failed")
        with (
            mock.patch.object(permanent_setup.sys, "platform", "linux"),
            mock.patch("installer.permanent_setup.subprocess.run", return_value=completed),
            mock.patch(
                "client.popup.backend.credential_free_environment", return_value={}
            ),
            mock.patch(
                "client.popup.backend._linux_user_manager_environment",
                return_value={"DISPLAY": ":0"},
            ),
            mock.patch.object(permanent_setup, "_setup_strings", return_value={"html_lang": "zh-Hans"}),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            opened = permanent_setup._show_linux_webview_message(
                "激活完成", "设备已连接", error=False
            )

        self.assertFalse(opened)
        self.assertIn("using the Tk fallback", stderr.getvalue())


class LinuxAuditWebViewTests(unittest.TestCase):
    def test_panel_uses_safe_text_rendering_and_accepted_dimensions(self):
        from client.stopper import webview_panel

        self.assertIn("textContent=text", webview_panel._HTML)
        self.assertNotIn("innerHTML", webview_panel._HTML)
        created = {}
        window = SimpleNamespace(
            events=SimpleNamespace(
                closed=_FakeEvent(),
                closing=_FakeEvent(),
                minimized=_FakeEvent(),
                restored=_FakeEvent(),
                shown=_FakeEvent(),
            )
        )

        def create_window(*args, **kwargs):
            created.update(kwargs)
            return window

        app = webview_panel.LinuxWebViewStopPanelApp(
            SimpleNamespace(create_window=create_window)
        )

        self.assertEqual((created["width"], created["height"]), (560, 220))
        self.assertEqual(created["min_size"], (420, 150))
        self.assertTrue(created["resizable"])
        self.assertIs(app.window, window)

        app.runs["aud_中文测试"] = {
            "run_id": "untrusted-payload-id",
            "title": "中文审计",
            "profile": "fast",
            "ui_locale": "zh-CN",
            "status": "queued",
            "started_at": 1.0,
            "_sort_at": 1.0,
        }
        app._server_verified.add("aud_中文测试")
        payload = app._payload()
        self.assertEqual(payload["rows"][0]["title"], "中文审计")
        self.assertEqual(payload["rows"][0]["run_id"], "aud_中文测试")
        self.assertTrue(payload["rows"][0]["action"]["enabled"])

    def test_running_details_keep_shimmer_during_connectivity_interruption(self):
        from client.stopper import webview_panel

        app, _window, _events = _audit_webview_app(webview_panel)

        def shimmers(run_id, run, *, verified=False, poll_failure=False, gone=False):
            app.runs = {run_id: run}
            app._server_verified = {run_id} if verified else set()
            app._poll_failures = {run_id: 1} if poll_failure else {}
            app._not_found_polls = {run_id: 1} if gone else {}
            with mock.patch.object(webview_panel.time, "time", return_value=100.0):
                return app._payload()["rows"][0]["detail"]["shimmer"]

        running = {
            "run_id": "hosted",
            "status": "running",
            "profile": "standard",
            "started_at": 80.0,
            "last_ok_at": 99.0,
        }
        lite = {
            **running,
            "run_id": "lite",
            "local": True,
            "local_surface": "de_lite",
        }
        self.assertTrue(shimmers("lite", lite))
        self.assertTrue(shimmers("hosted", running, verified=True))
        self.assertFalse(shimmers("hosted", running))
        self.assertTrue(shimmers("hosted", running, verified=True, poll_failure=True))
        self.assertFalse(shimmers("hosted", running, verified=True, gone=True))
        self.assertTrue(
            shimmers("hosted", {**running, "last_ok_at": 1.0}, verified=True)
        )

        for status in (
            "completed",
            "partial",
            "failed",
            "cancelled",
            "timeout",
            "stale",
            "cancelling",
            "queued",
        ):
            with self.subTest(status=status):
                self.assertFalse(
                    shimmers(
                        "hosted",
                        {**running, "status": status, "auditors": [{"status": "running"}]},
                        verified=True,
                    )
                )

    def test_payload_uses_shared_shimmer_eligibility_predicate(self):
        from client.stopper import panel, webview_panel

        app, _window, _events = _audit_webview_app(webview_panel)
        app.runs = {
            "hosted": {
                "run_id": "untrusted-payload-id",
                "status": "running",
                "started_at": 80.0,
                "last_ok_at": 99.0,
            }
        }
        app._server_verified = {"hosted"}
        with mock.patch.object(panel, "should_shimmer_depth_line", return_value=True) as eligible, \
             mock.patch.object(webview_panel.time, "time", return_value=100.0):
            payload = app._payload()

        self.assertTrue(payload["rows"][0]["detail"]["shimmer"])
        eligible.assert_called_once()
        _args, kwargs = eligible.call_args
        self.assertEqual(kwargs["run_id"], "hosted")
        self.assertEqual(kwargs["server_verified"], {"hosted"})

    def test_disconnected_payload_keeps_running_action_and_normal_layout(self):
        from client.stopper import panel, webview_panel

        app, _window, _events = _audit_webview_app(webview_panel)
        app.runs = {
            "hosted": {
                "run_id": "hosted", "status": "running", "profile": "standard",
                "ui_locale": "en-US",
                "started_at": 1.0, "last_ok_at": 1.0,
                "auditors": [{"status": "running"}],
            }
        }
        app._server_verified = {"hosted"}
        app._poll_failures = {"hosted": 1}
        with mock.patch.object(webview_panel.time, "time", return_value=100.0):
            row = app._payload()["rows"][0]

        self.assertEqual(row["detail"]["text"], "Standard · Auditing · 1m 39s ⏱")
        self.assertEqual(row["detail"]["color"], panel.FG_MUTED)
        self.assertTrue(row["detail"]["shimmer"])
        self.assertEqual(row["action"]["text"], "Running")
        self.assertEqual(row["action"]["background"], panel.FG_RED)
        self.assertFalse(row["action"]["enabled"])

    def test_missing_optional_native_window_events_do_not_block_startup(self):
        from client.stopper import webview_panel

        closed = _FakeEvent()
        window = SimpleNamespace(
            destroy=mock.Mock(),
            evaluate_js=mock.Mock(),
            events=SimpleNamespace(closed=closed),
        )
        app = webview_panel.LinuxWebViewStopPanelApp(
            SimpleNamespace(create_window=lambda *_args, **_kwargs: window)
        )

        self.assertIs(app.window, window)
        self.assertEqual(len(closed.handlers), 1)

    def test_shimmer_is_detail_only_and_has_shared_accessible_lifecycle(self):
        from client.stopper import webview_panel

        html = webview_panel._HTML
        self.assertIn("item.detail.shimmer", html)
        self.assertIn("detail-shimmer", html)
        self.assertIn("performance.now()", html)
        self.assertIn("animationDelay", html)
        self.assertIn("prefers-reduced-motion:reduce", html)
        self.assertIn("matchMedia('(prefers-reduced-motion: reduce)')", html)
        self.assertIn("visibilitychange", html)
        self.assertIn("pagehide", html)
        self.assertIn("beforeunload", html)
        self.assertIn("window.setPanelWindowActive", html)
        self.assertIn("const row=node('section','row');", html)
        self.assertIn("const button=node('button','action'", html)
        self.assertIn("content.append(node('h1','title'", html)

    def test_native_window_events_suspend_and_resume_without_closing_handler(self):
        from client.stopper import webview_panel

        _app, window, events = _audit_webview_app(webview_panel)

        self.assertEqual(len(events.minimized.handlers), 1)
        self.assertEqual(len(events.restored.handlers), 1)
        self.assertEqual(len(events.shown.handlers), 1)
        self.assertEqual(len(events.closing.handlers), 0)

        events.minimized.handlers[0]()
        window.evaluate_js.assert_called_with("window.setPanelWindowActive(false)")
        events.restored.handlers[0]()
        window.evaluate_js.assert_called_with("window.setPanelWindowActive(true)")
        events.shown.handlers[0]()
        window.evaluate_js.assert_called_with("window.setPanelWindowActive(true)")
        self.assertEqual(window.evaluate_js.call_count, 3)


class LinuxTkFontFallbackTests(unittest.TestCase):
    def test_prefers_an_enumerated_cjk_family(self):
        from client import tk_fonts

        named = mock.Mock()
        named.actual.return_value = "fixed"
        tkfont = SimpleNamespace(
            families=lambda _root: ("DejaVu Sans", "Noto Sans CJK SC"),
            nametofont=lambda _name: named,
        )

        ui, mono = tk_fonts.resolve_linux_tk_fonts(object(), tkfont)

        self.assertEqual(ui, "Noto Sans CJK SC")
        self.assertEqual(mono, "fixed")


if __name__ == "__main__":
    unittest.main()
