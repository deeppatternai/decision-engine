"""Cursor popup bridge profiles expose no legacy chat/share surface."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from client.popup import launcher, native_shell, session


def _public_methods(value):
    return {
        name
        for name in dir(value)
        if not name.startswith("_") and callable(getattr(value, name))
    }


def _popup_test_root(value) -> Path:
    """Place the managed root below a private parent on shared-temp POSIX hosts."""
    parent = Path(value)
    if os.name == "posix":
        os.chmod(parent, 0o700)
    return parent / "popup-sessions"


class CursorApiProfileTestCase(unittest.TestCase):
    def test_cursor_ge_profile_exposes_only_window_capture_and_chat(self):
        api = native_shell._api_for_profile(
            native_shell.PopupApi("unused"), "cursor-ge"
        )
        methods = _public_methods(api)

        self.assertEqual(
            methods,
            {
                "ask",
                "chat_ready",
                "close",
                "copy_visual_image",
                "copy_visual_image_hires",
                "hide",
                "minimize",
                "move_window",
                "resize_window",
                "retry_chat",
                "snapshot_region",
                "toggle_maximize",
                "window_state",
            },
        )
        for forbidden in ("commit", "initial_state", "layout", "share_visual_image"):
            self.assertNotIn(forbidden, methods)
        self.assertFalse(hasattr(api, "delete_chat"))
        self.assertFalse(hasattr(native_shell.PopupApi("unused"), "delete_chat"))
        self.assertFalse(hasattr(api, "cancel_chat"))
        self.assertFalse(hasattr(native_shell.PopupApi("unused"), "cancel_chat"))
        self.assertFalse(hasattr(api, "device_token"))
        self.assertFalse(hasattr(api, "endpoint"))

    def test_cursor_ge_profile_forwards_exact_legacy_and_server_ask_signatures(self):
        core = mock.create_autospec(native_shell.PopupApi, instance=True)
        core.ask.return_value = {"ok": True, "route": "unexpected"}
        api = native_shell._CursorGeApi(core)

        invalid = {"ok": False, "error_code": "invalid_request"}
        self.assertEqual(api.ask(1, "missing images"), invalid)
        self.assertEqual(api.ask(1, "partial server", [], "turn-key"), invalid)
        self.assertEqual(
            api.ask(1, "extra server argument", [], "turn-key", "privacy-v1", "extra"),
            invalid,
        )
        core.ask.assert_not_called()

        core.ask.side_effect = [
            {"ok": True, "route": "legacy"},
            {"ok": True, "route": "server"},
        ]

        self.assertEqual(
            api.ask(1, "legacy question", []),
            {"ok": True, "route": "legacy"},
        )
        self.assertEqual(
            api.ask(2, "server question", [], "turn-key", "privacy-v1"),
            {"ok": True, "route": "server"},
        )
        self.assertEqual(
            core.ask.call_args_list,
            [
                mock.call(1, "legacy question", []),
                mock.call(2, "server question", [], "turn-key", "privacy-v1"),
            ],
        )

    def test_cursor_ge_profile_forwards_chat_and_region_calls(self):
        core = mock.create_autospec(native_shell.PopupApi, instance=True)
        api = native_shell._CursorGeApi(core)
        cases = (
            ("snapshot_region", ([1, 2, 3, 4],)),
            ("chat_ready", ()),
            ("retry_chat", ("turn-key",)),
        )
        for method, args in cases:
            with self.subTest(method=method):
                expected = {"method": method}
                getattr(core, method).return_value = expected
                self.assertEqual(getattr(api, method)(*args), expected)
                getattr(core, method).assert_called_once_with(*args)

    def test_window_chrome_prefers_template_button_group(self):
        chrome_js = native_shell._WINDOW_CHROME_JS
        self.assertIn("document.getElementById('window-actions')", chrome_js)
        self.assertIn("controlHost.insertBefore(maximizeBtn, closeBtn)", chrome_js)
        self.assertIn("var(--win-btn,30px)", chrome_js)

    def test_image_preview_z_index_strictly_exceeds_native_chrome(self):
        rendered = launcher.render_artifact_html(
            launcher.PopupSpec(
                kind="ge",
                title="GE chat",
                artifact={
                    "kind": "svg",
                    "data": '<svg xmlns="http://www.w3.org/2000/svg"/>',
                },
            )
        )

        def z_index(source, selector):
            matches = re.findall(
                re.escape(selector) + r"\s*\{[^}]*z-index:\s*(\d+)", source
            )
            self.assertEqual(len(matches), 1, selector)
            return int(matches[0])

        preview = z_index(rendered, ".ge-image-preview")
        controls = z_index(native_shell._WINDOW_CHROME_JS, ".de-window-control")
        resize_handles = z_index(native_shell._WINDOW_CHROME_JS, ".de-resize-handle")

        self.assertGreater(preview, controls)
        self.assertGreater(preview, resize_handles)
        self.assertGreater(controls, resize_handles)
        self.assertLessEqual(preview, 2_147_483_647)

    def test_cursor_db_profile_has_commit_layout_but_no_chat_or_share(self):
        api = native_shell._api_for_profile(
            native_shell.PopupApi("unused"), "cursor-db"
        )
        methods = _public_methods(api)

        self.assertEqual(
            methods,
            {
                "close",
                "commit",
                "copy_visual_image",
                "copy_visual_image_hires",
                "dismiss",
                "hide",
                "initial_state",
                "layout",
                "minimize",
                "move_window",
                "resize_window",
                "toggle_maximize",
                "window_state",
            },
        )

    def test_cursor_db_bridge_rejects_token_reflection_before_result_write(self):
        board = {"stage": "kanban", "columns": [], "notes": "", "comments": []}
        with tempfile.TemporaryDirectory() as root:
            result_path = Path(root) / "result.json"
            api = native_shell._api_for_profile(
                native_shell.PopupApi(
                    str(result_path),
                    initial_state=board,
                    forbidden_token="synthetic-device-token",
                ),
                "cursor-db",
            )

            self.assertEqual(api.initial_state(), board)
            outcome = api.commit(
                {
                    **board,
                    "notes": "reflected synthetic-device-token value",
                }
            )

            self.assertEqual(outcome, {"ok": False})
            self.assertFalse(result_path.exists())

    def test_cursor_db_bridge_is_closed_until_owned_document_is_ready(self):
        board = {"stage": "kanban", "columns": [], "notes": "", "comments": []}
        with tempfile.TemporaryDirectory() as root:
            result_path = Path(root) / "result.json"
            core = native_shell.PopupApi(str(result_path), initial_state=board)
            core._cursor_guard_required = True
            core._cursor_document_ready.clear()
            api = native_shell._api_for_profile(core, "cursor-db")

            with self.assertRaises(RuntimeError):
                api.initial_state()
            self.assertEqual(api.commit(board), {"ok": False})
            with (
                mock.patch.object(launcher, "fetch_board_layout") as fetch,
                self.assertRaises(RuntimeError),
            ):
                api.layout({"graph": {"nodes": [], "edges": []}})
            fetch.assert_not_called()
            self.assertFalse(result_path.exists())

            core._cursor_document_ready.set()
            self.assertEqual(api.initial_state(), board)

    def test_shell_command_carries_explicit_api_profile(self):
        cmd = launcher.build_shell_command(
            sys.executable,
            "popup.html",
            "Cursor popup",
            "result.json",
            api_profile="cursor-ge",
        )
        self.assertEqual(cmd[-2:], ["--api-profile", "cursor-ge"])

    def test_cursor_db_state_and_token_use_owned_stdin_pipe_not_files_or_argv(self):
        board = {"stage": "kanban", "columns": [], "notes": "", "comments": []}
        pipe = mock.Mock()
        feeder_closed = threading.Event()
        pipe.close.side_effect = feeder_closed.set
        process = SimpleNamespace(stdin=pipe, poll=mock.Mock(return_value=None))
        with tempfile.TemporaryDirectory() as root:
            popup_root = _popup_test_root(root)
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(
                    session, "_validate_windows_private_data_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_a1"),
                mock.patch.object(
                    session.subprocess, "Popen", return_value=process
                ) as popen,
            ):
                result = session.spawn(
                    "<html><body>trusted board</body></html>",
                    "Cursor board",
                    api_profile="cursor-db",
                    initial_state=board,
                    forbidden_token="synthetic-device-token",
                )
                files = sorted(
                    path.name for path in (popup_root / "pop_a1").iterdir()
                )

        self.assertEqual(result, {"status": "open", "popup_id": "pop_a1"})
        self.assertEqual(files, ["popup.html"])
        command = popen.call_args.args[0]
        self.assertIn("--bridge-state-stdin", command)
        self.assertNotIn("synthetic-device-token", " ".join(command))
        self.assertNotIn(json.dumps(board), " ".join(command))
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(
            popen.call_args.kwargs["env"]["WEBVIEW2_USER_DATA_FOLDER"],
            str(popup_root / "pop_a1" / "webview2-data"),
        )
        self.assertTrue(feeder_closed.wait(1))
        sent = pipe.write.call_args.args[0]
        self.assertEqual(
            json.loads(sent.decode("utf-8")),
            {"initial_state": board, "forbidden_token": "synthetic-device-token"},
        )
        pipe.close.assert_called_once()

    def test_spawn_does_not_report_open_when_native_shell_exits_immediately(self):
        process = SimpleNamespace(stdin=None, poll=mock.Mock(return_value=134))
        with tempfile.TemporaryDirectory() as root:
            popup_root = _popup_test_root(root)
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(
                    session, "_validate_windows_private_data_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_crash"),
                mock.patch.object(session.subprocess, "Popen", return_value=process),
            ):
                result = session.spawn(
                    "<html><body>trusted board</body></html>",
                    "Crashy popup",
                )
            self.assertFalse((popup_root / "pop_crash").exists())

        self.assertEqual(
            result,
            {
                "status": "failed",
                "reason": "native-shell-exited",
                "returncode": 134,
            },
        )

    def test_cursor_bridge_missing_pipe_terminates_child_and_removes_workdir(self):
        board = {"stage": "kanban", "columns": [], "notes": "", "comments": []}
        process = SimpleNamespace(
            stdin=None,
            poll=mock.Mock(return_value=None),
            terminate=mock.Mock(),
            kill=mock.Mock(),
            wait=mock.Mock(return_value=0),
        )
        with tempfile.TemporaryDirectory() as root:
            popup_root = _popup_test_root(root)
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(
                    session, "_validate_windows_private_data_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_no_pipe"),
                mock.patch.object(session.subprocess, "Popen", return_value=process),
            ):
                result = session.spawn(
                    "<html><body>trusted board</body></html>",
                    "Cursor board",
                    api_profile="cursor-db",
                    initial_state=board,
                    forbidden_token="synthetic-device-token",
                )

            self.assertFalse((popup_root / "pop_no_pipe").exists())

        self.assertEqual(result, {"status": "failed", "reason": "bridge-handoff-failed"})
        process.terminate.assert_called_once()

    def test_cursor_feeder_start_failure_terminates_child_and_removes_workdir(self):
        board = {"stage": "kanban", "columns": [], "notes": "", "comments": []}
        process = SimpleNamespace(
            stdin=mock.Mock(),
            poll=mock.Mock(return_value=None),
            terminate=mock.Mock(),
            kill=mock.Mock(),
            wait=mock.Mock(return_value=0),
        )
        with tempfile.TemporaryDirectory() as root:
            popup_root = _popup_test_root(root)
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(
                    session, "_validate_windows_private_data_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_no_thread"),
                mock.patch.object(session.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    session,
                    "_new_cursor_bridge_thread",
                    return_value=SimpleNamespace(
                        start=mock.Mock(
                            side_effect=RuntimeError("synthetic thread start failure")
                        )
                    ),
                ),
            ):
                result = session.spawn(
                    "<html><body>trusted board</body></html>",
                    "Cursor board",
                    api_profile="cursor-db",
                    initial_state=board,
                    forbidden_token="synthetic-device-token",
                )

            self.assertFalse((popup_root / "pop_no_thread").exists())

        self.assertEqual(result, {"status": "failed", "reason": "bridge-handoff-failed"})
        process.terminate.assert_called_once()

    def test_cursor_db_large_pipe_write_cannot_block_spawn_quick_return(self):
        board = {"stage": "kanban", "columns": [], "notes": "", "comments": []}
        write_started = threading.Event()
        release_write = threading.Event()

        class BlockingPipe:
            def write(self, _payload):
                write_started.set()
                release_write.wait(2)

            def close(self):
                pass

        process = SimpleNamespace(
            stdin=BlockingPipe(),
            poll=mock.Mock(return_value=None),
            terminate=mock.Mock(),
        )
        with tempfile.TemporaryDirectory() as root:
            popup_root = _popup_test_root(root)
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(
                    session, "_validate_windows_private_data_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_a2"),
                mock.patch.object(session.subprocess, "Popen", return_value=process),
                mock.patch.object(session, "_NATIVE_SHELL_EXIT_GRACE_S", 0.0),
            ):
                started = time.monotonic()
                try:
                    result = session.spawn(
                        "<html><body>trusted board</body></html>",
                        "Cursor board",
                        api_profile="cursor-db",
                        initial_state=board,
                        forbidden_token="synthetic-device-token",
                    )
                    elapsed = time.monotonic() - started
                    self.assertTrue(write_started.wait(1))
                finally:
                    release_write.set()

        self.assertEqual(result, {"status": "open", "popup_id": "pop_a2"})
        self.assertLess(elapsed, 0.5)

    def test_native_shell_reads_cursor_db_bridge_state_from_owned_stdin(self):
        board = {"stage": "kanban", "columns": [], "notes": "", "comments": []}
        payload = json.dumps(
            {
                "initial_state": board,
                "forbidden_token": "synthetic-device-token",
            }
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as root:
            html_path = Path(root) / "popup.html"
            html_path.write_text(
                "<html><body>trusted board</body></html>", encoding="utf-8"
            )
            with (
                mock.patch.object(
                    native_shell.sys,
                    "stdin",
                    SimpleNamespace(buffer=io.BytesIO(payload)),
                ),
                mock.patch.object(native_shell, "open_window") as open_window,
            ):
                rc = native_shell.main(
                    [
                        "--html",
                        str(html_path),
                        "--title",
                        "Cursor board",
                        "--result-path",
                        str(Path(root) / "result.json"),
                        "--api-profile",
                        "cursor-db",
                        "--bridge-state-stdin",
                    ]
                )

        self.assertEqual(rc, 0)
        self.assertEqual(open_window.call_args.kwargs["initial_state"], board)
        self.assertEqual(
            open_window.call_args.kwargs["forbidden_token"],
            "synthetic-device-token",
        )


if __name__ == "__main__":
    unittest.main()
