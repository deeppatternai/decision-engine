"""Behavior tests for best-effort GUI preparation during installation."""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from client.popup import backend as popup_backend
from installer import gui_setup


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class GuiSetupTests(unittest.TestCase):
    def test_tkinter_probe_uses_target_python_without_inheriting_stdin(self):
        with (
            mock.patch.dict(
                os.environ,
                {"DE_ACTIVATION_SECRET": "owner_process_value"},
                clear=False,
            ),
            mock.patch.object(
                gui_setup.subprocess, "run", return_value=_Completed(0)
            ) as run,
        ):
            state = gui_setup.inspect_tkinter("mcp-python")

        self.assertEqual(state, gui_setup.TkinterState.READY)
        command = run.call_args.args[0]
        self.assertEqual(command, ["mcp-python", "-c", "import tkinter"])
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("DE_ACTIVATION_SECRET", run.call_args.kwargs["env"])

    def test_preparation_uses_same_python_for_both_gui_components(self):
        webview = popup_backend.WebviewResult(popup_backend.WebviewState.READY)
        with (
            mock.patch.object(
                gui_setup, "inspect_tkinter", return_value=gui_setup.TkinterState.READY
            ) as tkinter_probe,
            mock.patch.object(
                gui_setup.popup_backend, "prepare_webview", return_value=webview
            ) as prepare_webview,
        ):
            result = gui_setup.prepare_gui_environment("mcp-python")

        self.assertEqual(result.tkinter, gui_setup.TkinterState.READY)
        self.assertTrue(result.pywebview.ready)
        tkinter_probe.assert_called_once_with("mcp-python")
        prepare_webview.assert_called_once_with("mcp-python", label="de-gui-setup")


if __name__ == "__main__":
    unittest.main()
