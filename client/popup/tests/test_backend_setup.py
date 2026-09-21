"""Behavior tests for native popup dependency classification and preparation."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from client.popup import backend


class _Completed:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class BackendSetupTests(unittest.TestCase):
    def test_linux_restores_missing_graphical_environment_from_user_manager(self):
        user_environment = _Completed(
            0,
            "DISPLAY=:1\n"
            "WAYLAND_DISPLAY=wayland-0\n"
            "XDG_RUNTIME_DIR=/run/user/1000\n"
            "DE_ACTIVATION_SECRET=must-not-be-copied\n",
        )
        with (
            mock.patch.object(backend.sys, "platform", "linux"),
            mock.patch.object(backend.shutil, "which", return_value="/usr/bin/systemctl"),
            mock.patch.object(backend.subprocess, "run", return_value=user_environment),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertTrue(backend._restore_linux_graphical_session_environment())
            self.assertEqual(os.environ.get("DISPLAY"), ":1")
            self.assertEqual(os.environ.get("WAYLAND_DISPLAY"), "wayland-0")
            self.assertEqual(os.environ.get("XDG_RUNTIME_DIR"), "/run/user/1000")
            self.assertNotIn("DE_ACTIVATION_SECRET", os.environ)

    def test_linux_backend_probe_requires_a_real_display_connection(self):
        with mock.patch.object(
            backend.subprocess, "run", return_value=_Completed(1)
        ) as run:
            result = backend._ready_probe_state("mcp-python", gui="gtk")

        self.assertEqual(result, backend.WebviewState.BACKEND_UNAVAILABLE)
        probe_script = run.call_args.args[0][2]
        self.assertIn("Gdk.Display.get_default()", probe_script)

    def test_linux_ensure_selects_a_verified_backend_for_the_popup_child(self):
        ready = backend.WebviewResult(backend.WebviewState.READY)
        selected = []

        def probe(_python, *, timeout_s=30, avoid_gui_registration=False, gui=None):
            del timeout_s, avoid_gui_registration
            selected.append(gui)
            return (
                backend.WebviewState.READY
                if gui == "qt"
                else backend.WebviewState.BACKEND_UNAVAILABLE
            )

        with (
            mock.patch.object(backend.sys, "platform", "linux"),
            mock.patch.object(
                backend, "_restore_linux_graphical_session_environment"
            ),
            mock.patch.object(backend, "prepare_webview", return_value=ready),
            mock.patch.object(backend, "_ready_probe_state", side_effect=probe),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertTrue(backend.ensure_webview("mcp-python"))
            self.assertEqual(os.environ.get("PYWEBVIEW_GUI"), "qt")

        self.assertEqual(selected, ["gtk", "qt"])

    def test_linux_ensure_refuses_when_no_supported_backend_is_verified(self):
        ready = backend.WebviewResult(backend.WebviewState.READY)
        with (
            mock.patch.object(backend.sys, "platform", "linux"),
            mock.patch.object(
                backend, "_restore_linux_graphical_session_environment"
            ),
            mock.patch.object(backend, "prepare_webview", return_value=ready),
            mock.patch.object(
                backend,
                "_ready_probe_state",
                return_value=backend.WebviewState.BACKEND_UNAVAILABLE,
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(backend.ensure_webview("mcp-python"))
            self.assertNotIn("PYWEBVIEW_GUI", os.environ)

    def test_all_webview_probes_strip_owner_credentials_from_children(self):
        secret = "owner_process_value"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
                clear=False,
            ),
            mock.patch.object(
                backend.subprocess, "run", return_value=_Completed(0)
            ) as run,
        ):
            result = backend.inspect_webview("mcp-python")

        self.assertTrue(result.ready)
        self.assertGreaterEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertNotIn("DE_ENDPOINT", call.kwargs["env"])
            self.assertNotIn("DE_ACTIVATION_SECRET", call.kwargs["env"])

    def test_importable_package_with_failed_native_init_is_not_called_missing(self):
        def fake_run(command, **_kwargs):
            joined = " ".join(command)
            if "initialize()" in joined:
                return _Completed(1)
            if "import webview" in joined:
                return _Completed(0)
            raise AssertionError("unexpected command: %r" % command)

        with mock.patch.object(backend.subprocess, "run", side_effect=fake_run):
            result = backend.inspect_webview("mcp-python")

        self.assertEqual(result.state, backend.WebviewState.BACKEND_UNAVAILABLE)
        self.assertFalse(result.ready)

    def test_missing_package_reports_pip_failure_and_uses_requested_python(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            joined = " ".join(command)
            if joined.endswith("-c import webview"):
                return _Completed(1)
            if "install --upgrade pip" in joined:
                return _Completed(0)
            if "-m pip install" in joined:
                return _Completed(7, "synthetic pip failure")
            raise AssertionError("unexpected command: %r" % command)

        with mock.patch.object(backend.subprocess, "run", side_effect=fake_run):
            result = backend.prepare_webview("mcp-python", label="setup-test")

        self.assertEqual(result.state, backend.WebviewState.INSTALL_FAILED)
        self.assertEqual(result.pip_exit_code, 7)
        self.assertTrue(commands)
        self.assertTrue(all(command[0] == "mcp-python" for command in commands))


if __name__ == "__main__":
    unittest.main()
