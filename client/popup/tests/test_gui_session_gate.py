"""Regression: a DIAGNOSTIC probe must never register the process as a macOS GUI app.

pywebview's cocoa backend runs ``NSApplication.sharedApplication()`` in its class body
(``webview/platforms/cocoa.py``), so ``webview.initialize()`` registers the CHILD process
with the WindowServer/Dock at import time. Where that registration is refused — observed
from an agent's seatbelt shell — macOS does not raise: it aborts the child inside
``HIServices _RegisterApplication`` (SIGABRT) and shows a "Python quit unexpectedly"
dialog. Doctor runs that probe at the end of ``./install.sh``, so an agent-driven install
popped a system crash dialog at the user.

Two properties are locked here, and the second matters as much as the first:

1. A caller that only wants a diagnosis (``avoid_gui_registration=True``) skips the
   registering probe in a session that cannot register, and reports ``NO_GUI_SESSION``.
2. A caller that is preparing a REAL window is unaffected — the session signal is an
   over-approximation (confinement != refused registration), so it must never be allowed
   to silently disable a popup the user asked for.

Run:  python3 -m unittest client.popup.tests.test_gui_session_gate
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from client.popup import backend

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
_REASON_SNIPPET = (
    "import sys; sys.path.insert(0, %r)\n"
    "from client.popup import backend\n"
    "print(backend.gui_registration_blocked_reason())\n" % _PROJECT_ROOT
)


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class GuiRegistrationGateTests(unittest.TestCase):
    def test_gate_is_inert_off_darwin(self):
        """Windows/Linux have no WindowServer registration step — nothing to gate."""
        with (
            mock.patch.object(backend.sys, "platform", "win32"),
            mock.patch.object(backend, "_process_is_sandboxed", return_value=True),
            mock.patch.dict(os.environ, {"SSH_CONNECTION": "10.0.0.1 1 10.0.0.2 22"}, clear=False),
        ):
            self.assertIsNone(backend.gui_registration_blocked_reason())

    def test_remote_shell_on_macos_is_reported(self):
        with (
            mock.patch.object(backend.sys, "platform", "darwin"),
            mock.patch.dict(os.environ, {"SSH_CONNECTION": "10.0.0.1 1 10.0.0.2 22"}, clear=False),
        ):
            self.assertIsNotNone(backend.gui_registration_blocked_reason())

    def test_undetectable_environment_falls_open_to_the_pre_gate_behaviour(self):
        """A missing or failing sandbox_check must degrade to "probe as before", not "blocked"."""
        with (
            mock.patch.object(backend.sys, "platform", "darwin"),
            mock.patch.dict(os.environ, {"SSH_CONNECTION": "", "SSH_TTY": ""}, clear=False),
            mock.patch.object(backend, "_process_is_sandboxed", return_value=None),
        ):
            self.assertIsNone(backend.gui_registration_blocked_reason())


@unittest.skipUnless(sys.platform == "darwin", "the gate only has an effect on macOS")
@unittest.skipUnless(shutil.which("sandbox-exec"), "needs sandbox-exec to build a real sandbox")
class RealSessionDetectionTests(unittest.TestCase):
    """Exercise the live ctypes detection instead of asserting it merely does not raise."""

    def _reason_in(self, argv_prefix):
        proc = subprocess.run(
            list(argv_prefix) + [sys.executable, "-c", _REASON_SNIPPET],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_a_real_seatbelt_child_is_detected(self):
        reason = self._reason_in(["sandbox-exec", "-p", "(version 1)(allow default)"])
        self.assertNotEqual(reason, "None", "a sandboxed child must be reported as GUI-less")
        self.assertIn("sandbox", reason)

    @unittest.skipIf(
        backend._process_is_sandboxed(), "this test process is itself sandboxed; child would inherit"
    )
    def test_an_ordinary_child_of_this_session_is_not_detected(self):
        # The anti-false-positive lock: on a normal desktop the gate must stay out of the way.
        self.assertEqual(self._reason_in([]), "None")


class DiagnosticProbeTests(unittest.TestCase):
    """avoid_gui_registration=True — Doctor's path: diagnose, never register."""

    def test_blocked_session_never_spawns_the_registering_probe(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(" ".join(command))
            return _Completed(0)

        with (
            mock.patch.object(backend, "gui_registration_blocked_reason", return_value="sandboxed"),
            mock.patch.object(backend.subprocess, "run", side_effect=fake_run),
        ):
            result = backend.inspect_webview("mcp-python", avoid_gui_registration=True)

        self.assertEqual(result.state, backend.WebviewState.NO_GUI_SESSION)
        self.assertFalse(result.ready)
        self.assertTrue(commands, "the registration-free `import webview` probe should still run")
        self.assertFalse(
            [c for c in commands if "initialize()" in c],
            "a diagnostic caller must never run the GUI-registering probe: %r" % commands,
        )

    def test_blocked_session_still_reports_a_missing_package(self):
        """Classification stays useful in a sandbox — absent is absent, so pip can still fix it."""
        with (
            mock.patch.object(backend, "gui_registration_blocked_reason", return_value="sandboxed"),
            mock.patch.object(backend.subprocess, "run", return_value=_Completed(1)),
        ):
            result = backend.inspect_webview("mcp-python", avoid_gui_registration=True)

        self.assertEqual(result.state, backend.WebviewState.MISSING)

    def test_a_desktop_session_probes_exactly_as_before(self):
        with (
            mock.patch.object(backend, "gui_registration_blocked_reason", return_value=None),
            mock.patch.object(backend.subprocess, "run", return_value=_Completed(0)) as run,
        ):
            result = backend.inspect_webview("mcp-python", avoid_gui_registration=True)

        self.assertEqual(result.state, backend.WebviewState.READY)
        self.assertTrue([c for c in run.call_args_list if "initialize()" in " ".join(c.args[0])])


class RealWindowPathTests(unittest.TestCase):
    """The session signal is an over-approximation, so it must not reach the product path."""

    def test_preparing_a_real_window_still_probes_in_a_blocked_session(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(" ".join(command))
            return _Completed(0)

        with (
            mock.patch.object(backend, "gui_registration_blocked_reason", return_value="sandboxed"),
            mock.patch.object(backend.subprocess, "run", side_effect=fake_run),
        ):
            self.assertTrue(backend.ensure_webview("mcp-python", "gate-test"))

        self.assertTrue(
            [c for c in commands if "initialize()" in c],
            "a popup the user asked for must still be probed for real: %r" % commands,
        )

    def test_default_inspection_is_unchanged_by_the_gate(self):
        with (
            mock.patch.object(backend, "gui_registration_blocked_reason", return_value="sandboxed"),
            mock.patch.object(backend.subprocess, "run", return_value=_Completed(0)),
        ):
            self.assertEqual(
                backend.inspect_webview("mcp-python").state, backend.WebviewState.READY
            )


if __name__ == "__main__":
    unittest.main()
