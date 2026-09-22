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

        class FakeEvent:
            def __iadd__(self, _callback):
                return self

        window = SimpleNamespace(events=SimpleNamespace(closed=FakeEvent()))

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
