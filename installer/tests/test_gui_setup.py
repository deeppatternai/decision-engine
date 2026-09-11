"""Behavior tests for best-effort GUI preparation during installation."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from client.popup import backend as popup_backend
from installer import gui_setup


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _bash_executable() -> str:
    resolved = shutil.which("bash")
    if resolved:
        return resolved
    git = shutil.which("git")
    if git:
        bundled = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if bundled.is_file():
            return str(bundled)
    raise unittest.SkipTest("Bash is required for installer GUI behavior tests")


class GuiSetupTests(unittest.TestCase):
    def test_unix_installer_prepares_gui_before_activation_without_blocking_fallback(
        self,
    ):
        source = (Path(__file__).resolve().parents[2] / "dp-install.sh").read_text()
        start = source.index(
            "  activation_status=0\n", source.index("activated_repair_mode")
        )
        end = source.index("\n\n  case \"$activation_status\"", start)
        activation = source[start:end]
        harness = f'''set -u
run_managed_python() {{
  printf 'prepare:%s\\n' "$*"
  if IFS= read -r _; then printf 'stdin:data\\n'; else printf 'stdin:eof\\n'; fi
  return "${{PREPARE_RC:-0}}"
}}
run_selected_permanent_setup() {{
  printf 'activate\\n'
  return "${{ACTIVATION_RC:-0}}"
}}
tty_print() {{ printf 'warning:%s\\n' "$1"; }}
{activation}
printf 'status:%s\\n' "$activation_status"
'''
        warning = (
            "warning:Warning: optional activation UI preparation failed; "
            "continuing with the existing activation fallback."
        )

        for prepare_rc, activation_rc, expected_tail in (
            ("0", "0", ["activate", "status:0"]),
            ("9", "17", [warning, "activate", "status:17"]),
        ):
            with self.subTest(prepare_rc=prepare_rc):
                result = subprocess.run(
                    [_bash_executable(), "-c", harness],
                    input="untrusted pipe remainder\n",
                    text=True,
                    capture_output=True,
                    env={
                        **os.environ,
                        "PREPARE_RC": prepare_rc,
                        "ACTIVATION_RC": activation_rc,
                    },
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(
                    lines[0],
                    "prepare:-c from installer import gui_setup; import sys; "
                    "gui_setup.prepare_gui_environment(sys.executable)",
                )
                self.assertEqual(lines[1], "stdin:eof")
                self.assertEqual(lines[2:], expected_tail)

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
