"""Cursor WebView2 containment around the trusted local popup document."""

from __future__ import annotations

import types
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client.popup import native_shell


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, args):
        for handler in list(self.handlers):
            handler(None, args)


class _Core:
    def __init__(self):
        self.Source = None
        self.Settings = types.SimpleNamespace(
            AreDevToolsEnabled=True,
            AreDefaultContextMenusEnabled=True,
            AreBrowserAcceleratorKeysEnabled=True,
            IsStatusBarEnabled=True,
        )
        for name in native_shell._CURSOR_WEBVIEW2_EVENTS:
            setattr(self, name, _Event())
        self.filters = []

    def AddWebResourceRequestedFilter(self, pattern, context):
        self.filters.append((pattern, context))


class CursorWebView2PolicyTests(unittest.TestCase):
    def test_guard_matrix_denies_every_non_owned_navigation_and_capability(self):
        core = _Core()
        closed = []
        ready = []
        canonical = "http://127.0.0.1:43123/popup.html"

        retained = native_shell.install_cursor_webview2_policy(
            core,
            canonical,
            lambda: closed.append(True),
            all_context="ALL",
            document_ready=lambda: ready.append(True),
        )

        self.assertTrue(retained)
        self.assertEqual(
            vars(core.Settings),
            {
                "AreDevToolsEnabled": False,
                "AreDefaultContextMenusEnabled": False,
                "AreBrowserAcceleratorKeysEnabled": False,
                "IsStatusBarEnabled": False,
            },
        )
        self.assertEqual(core.filters, [("*", "ALL")])
        self.assertTrue(
            all(
                len(getattr(core, name).handlers) == 1
                for name in native_shell._CURSOR_WEBVIEW2_EVENTS
            )
        )

        first = types.SimpleNamespace(
            Uri=canonical, NavigationId=17, Cancel=False
        )
        core.NavigationStarting.fire(first)
        self.assertFalse(first.Cancel)
        core.Source = canonical
        core.NavigationCompleted.fire(
            types.SimpleNamespace(IsSuccess=True, NavigationId=17)
        )
        self.assertEqual(ready, [True])
        second = types.SimpleNamespace(
            Uri=canonical, NavigationId=18, Cancel=False
        )
        core.NavigationStarting.fire(second)
        self.assertTrue(second.Cancel)

        frame = types.SimpleNamespace(Cancel=False)
        core.FrameNavigationStarting.fire(frame)
        self.assertTrue(frame.Cancel)
        new_window = types.SimpleNamespace(Handled=False)
        core.NewWindowRequested.fire(new_window)
        self.assertTrue(new_window.Handled)
        download = types.SimpleNamespace(Cancel=False)
        core.DownloadStarting.fire(download)
        self.assertTrue(download.Cancel)
        permission = types.SimpleNamespace(State=None, Handled=False)
        core.PermissionRequested.fire(permission)
        self.assertEqual(permission.State, "deny")
        self.assertTrue(permission.Handled)
        certificate = types.SimpleNamespace(Handled=False)
        core.ClientCertificateRequested.fire(certificate)
        self.assertTrue(certificate.Handled)

        allowed = types.SimpleNamespace(
            Request=types.SimpleNamespace(Uri="data:image/png;base64,AAAA"),
            Response=None,
        )
        core.WebResourceRequested.fire(allowed)
        self.assertIsNone(allowed.Response)
        blocked = types.SimpleNamespace(
            Request=types.SimpleNamespace(Uri="https://attacker.invalid/x"),
            Response=None,
        )
        core.WebResourceRequested.fire(blocked)
        self.assertIsNotNone(blocked.Response)

        core.ProcessFailed.fire(types.SimpleNamespace())
        self.assertEqual(closed, [True])

        failed_core = _Core()
        failed_closed = []
        native_shell.install_cursor_webview2_policy(
            failed_core,
            canonical,
            lambda: failed_closed.append(True),
            all_context="ALL",
        )
        failed_core.NavigationCompleted.fire(
            types.SimpleNamespace(IsSuccess=False, NavigationId=99)
        )
        self.assertEqual(failed_closed, [True])

    def test_completion_must_match_started_navigation_and_canonical_source(self):
        canonical = "http://127.0.0.1:43123/popup.html"
        for completion_id, source in (
            (8, canonical),
            (7, "http://127.0.0.1:43123/other.html"),
        ):
            with self.subTest(completion_id=completion_id, source=source):
                core = _Core()
                closed = []
                ready = []
                native_shell.install_cursor_webview2_policy(
                    core,
                    canonical,
                    lambda: closed.append(True),
                    all_context="ALL",
                    document_ready=lambda: ready.append(True),
                )
                core.NavigationStarting.fire(
                    types.SimpleNamespace(
                        Uri=canonical, NavigationId=7, Cancel=False
                    )
                )
                core.Source = source
                core.NavigationCompleted.fire(
                    types.SimpleNamespace(
                        IsSuccess=True, NavigationId=completion_id
                    )
                )
                self.assertEqual(closed, [True])
                self.assertEqual(ready, [])

    def test_loopback_url_validation_rejects_every_unpinned_shape(self):
        invalid = (
            "file:///C:/private/popup.html",
            "https://127.0.0.1:43123/popup.html",
            "http://localhost:43123/popup.html",
            "http://attacker.invalid:43123/popup.html",
            "http://127.0.0.1/popup.html",
            "http://user:pw@127.0.0.1:43123/popup.html",
            "http://127.0.0.1:43123/popup.html?x=1",
            "http://127.0.0.1:43123/popup.html#fragment",
            "about:blank",
        )
        for canonical in invalid:
            with self.subTest(canonical=canonical), self.assertRaises(ValueError):
                native_shell.install_cursor_webview2_policy(
                    _Core(),
                    canonical,
                    lambda: None,
                    all_context="ALL",
                )

    def test_owned_bootstrap_is_hardened_before_target_navigation(self):
        core = _Core()
        bootstrap_url = "http://127.0.0.1:43123/cursor-bootstrap.html"
        target_url = "http://127.0.0.1:43123/popup.html"
        core.Source = bootstrap_url
        events = []
        original_add = core.AddWebResourceRequestedFilter

        def add_filter(pattern, context):
            events.append("policy")
            original_add(pattern, context)

        core.AddWebResourceRequestedFilter = add_filter
        api = types.SimpleNamespace(
            close=lambda: events.append("close"),
            _core=types.SimpleNamespace(
                _cursor_document_ready=native_shell.threading.Event(),
                _cursor_ready_timer=mock.Mock(),
            ),
        )
        win = types.SimpleNamespace(
            native=types.SimpleNamespace(
                webview=types.SimpleNamespace(CoreWebView2=core)
            ),
            _resolve_url=lambda path: (
                bootstrap_url
                if Path(path).name == "cursor-bootstrap.html"
                else target_url
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            document = Path(root) / "popup.html"
            bootstrap = Path(root) / "cursor-bootstrap.html"
            document.write_text("<html></html>", encoding="utf-8")
            bootstrap.write_text("<html></html>", encoding="utf-8")
            configured_target = native_shell.configure_cursor_webview2_window(
                win,
                str(document),
                str(bootstrap),
                api,
                all_context="ALL",
                deny_state="deny",
                response_factory=lambda: "blocked",
            )

        self.assertEqual(configured_target, target_url)
        self.assertEqual(events, ["policy"])
        self.assertFalse(api._core._cursor_document_ready.is_set())
        navigation = types.SimpleNamespace(
            Uri=target_url, NavigationId=18, Cancel=False
        )
        core.NavigationStarting.fire(navigation)
        self.assertFalse(navigation.Cancel)
        core.Source = target_url
        core.NavigationCompleted.fire(
            types.SimpleNamespace(IsSuccess=True, NavigationId=18)
        )
        self.assertTrue(api._core._cursor_document_ready.is_set())
        api._core._cursor_ready_timer.cancel.assert_called_once_with()

    def test_bootstrap_identity_mismatch_closes_without_readying_bridge(self):
        core = _Core()
        core.Source = "http://127.0.0.1:43123/not-owned.html"
        closed = []
        api = types.SimpleNamespace(
            close=lambda: closed.append(True),
            _core=types.SimpleNamespace(
                _cursor_document_ready=native_shell.threading.Event(),
                _cursor_ready_timer=None,
            ),
        )
        win = types.SimpleNamespace(
            native=types.SimpleNamespace(
                webview=types.SimpleNamespace(CoreWebView2=core)
            ),
            _resolve_url=lambda path: (
                "http://127.0.0.1:43123/cursor-bootstrap.html"
                if Path(path).name == "cursor-bootstrap.html"
                else "http://127.0.0.1:43123/popup.html"
            ),
        )
        configured = native_shell.configure_cursor_webview2_window(
            win,
            "C:/owned/popup.html",
            "C:/owned/cursor-bootstrap.html",
            api,
            all_context="ALL",
            deny_state="deny",
            response_factory=lambda: "blocked",
        )

        self.assertIsNone(configured)
        self.assertEqual(closed, [True])
        self.assertFalse(api._core._cursor_document_ready.is_set())

    def test_partial_handler_install_failure_closes_without_loading(self):
        core = _Core()
        core.Source = "http://127.0.0.1:43123/cursor-bootstrap.html"
        del core.DownloadStarting
        events = []
        api = types.SimpleNamespace(
            close=lambda: events.append("close"),
            _core=types.SimpleNamespace(
                _cursor_document_ready=native_shell.threading.Event()
            ),
        )
        win = types.SimpleNamespace(
            native=types.SimpleNamespace(
                webview=types.SimpleNamespace(CoreWebView2=core)
            ),
            _resolve_url=lambda path: (
                core.Source
                if Path(path).name == "cursor-bootstrap.html"
                else "http://127.0.0.1:43123/popup.html"
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            document = Path(root) / "popup.html"
            bootstrap = Path(root) / "cursor-bootstrap.html"
            document.write_text("<html></html>", encoding="utf-8")
            bootstrap.write_text("<html></html>", encoding="utf-8")
            ok = native_shell.configure_cursor_webview2_window(
                win,
                str(document),
                str(bootstrap),
                api,
                all_context="ALL",
                deny_state="deny",
                response_factory=lambda: "blocked",
            )

        self.assertIsNone(ok)
        self.assertEqual(events, ["close"])

    def test_policy_install_is_marshaled_to_winforms_ui_thread(self):
        core = _Core()
        core.Source = "http://127.0.0.1:43123/cursor-bootstrap.html"
        events = []

        class NativeControl:
            InvokeRequired = True
            webview = types.SimpleNamespace(CoreWebView2=core)

            def Invoke(self, action):
                events.append("invoke")
                action()

        api = types.SimpleNamespace(
            close=lambda: events.append("close"),
            _core=types.SimpleNamespace(
                _cursor_document_ready=native_shell.threading.Event()
            ),
        )
        win = types.SimpleNamespace(
            native=NativeControl(),
            _resolve_url=lambda path: (
                core.Source
                if Path(path).name == "cursor-bootstrap.html"
                else "http://127.0.0.1:43123/popup.html"
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            document = Path(root) / "popup.html"
            bootstrap = Path(root) / "cursor-bootstrap.html"
            document.write_text("<html></html>", encoding="utf-8")
            bootstrap.write_text("<html></html>", encoding="utf-8")
            with mock.patch.dict(
                sys.modules,
                {"System": types.SimpleNamespace(Action=lambda action: action)},
            ):
                ok = native_shell.configure_cursor_webview2_window(
                    win,
                    str(document),
                    str(bootstrap),
                    api,
                    all_context="ALL",
                    deny_state="deny",
                    response_factory=lambda: "blocked",
                )

        self.assertEqual(ok, "http://127.0.0.1:43123/popup.html")
        self.assertEqual(events, ["invoke"])
        self.assertNotIn("close", events)

    def test_cursor_window_loads_inert_bootstrap_then_starts_hardened_target(self):
        loaded = _Event()
        fake_window = types.SimpleNamespace(
            events=types.SimpleNamespace(loaded=loaded),
        )
        created = {}

        def create_window(**kwargs):
            created.update(kwargs)
            return fake_window

        fake_webview = types.SimpleNamespace(
            create_window=create_window,
            start=mock.Mock(side_effect=lambda: loaded.fire(types.SimpleNamespace())),
        )
        with tempfile.TemporaryDirectory() as root:
            html_path = Path(root) / "popup.html"
            html_path.write_text("<html>trusted</html>", encoding="utf-8")
            with (
                mock.patch.dict(sys.modules, {"webview": fake_webview}),
                mock.patch.object(native_shell, "_IS_WINDOWS", True),
                mock.patch.object(native_shell, "_wire_window_chrome"),
                mock.patch.object(native_shell, "_win_after_show"),
                mock.patch.object(native_shell, "_install_windows_chrome"),
                mock.patch.object(
                    native_shell,
                    "configure_cursor_webview2_window",
                    return_value="http://127.0.0.1:43123/popup.html",
                ) as configure,
                mock.patch.object(
                    native_shell, "_start_cursor_target_navigation"
                ) as start_target,
                mock.patch.object(
                    native_shell, "_arm_cursor_ready_watchdog"
                ) as watchdog,
                mock.patch.object(native_shell.os, "_exit"),
            ):
                native_shell.open_window(
                    str(html_path),
                    "Cursor popup",
                    str(Path(root) / "result.json"),
                    api_profile="cursor-ge",
                )

            bootstrap_path = Path(created["url"])
            self.assertEqual(
                bootstrap_path.name, native_shell._CURSOR_BOOTSTRAP_NAME
            )
            self.assertNotEqual(bootstrap_path, html_path)
            configure.assert_called_once()
            self.assertEqual(
                Path(configure.call_args.args[2]), bootstrap_path
            )
            start_target.assert_called_once_with(
                fake_window,
                str(html_path),
                "http://127.0.0.1:43123/popup.html",
                mock.ANY,
            )
            watchdog.assert_called_once()

        fake_webview.start.assert_called_once_with()

    def test_ready_watchdog_closes_only_when_target_never_becomes_ready(self):
        timers = []

        class Timer:
            def __init__(self, timeout, callback):
                self.timeout = timeout
                self.callback = callback
                self.daemon = False
                self.cancelled = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        core = types.SimpleNamespace(
            _cursor_document_ready=native_shell.threading.Event(),
            _cursor_ready_timer=None,
        )
        closed = []
        with mock.patch.object(native_shell.threading, "Timer", Timer):
            native_shell._arm_cursor_ready_watchdog(
                core, lambda: closed.append(True)
            )
        timers[0].callback()
        self.assertEqual(closed, [True])

        core._cursor_document_ready.set()
        timers[0].callback()
        self.assertEqual(closed, [True])

    def test_target_navigation_closes_if_resolved_mapping_changes(self):
        closed = native_shell.threading.Event()

        class Window:
            real_url = None

            def load_url(self, _path):
                self.real_url = "http://127.0.0.1:43123/unexpected.html"

        native_shell._start_cursor_target_navigation(
            Window(),
            "C:/owned/popup.html",
            "http://127.0.0.1:43123/popup.html",
            closed.set,
        )

        self.assertTrue(closed.wait(1))


if __name__ == "__main__":
    unittest.main()
