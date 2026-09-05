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
