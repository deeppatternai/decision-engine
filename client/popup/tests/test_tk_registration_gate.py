"""Regression: Tk must never be constructed in a process another framework registered.

Tk 9.0's macOS backend resolves system colours through ``[NSApp macOSVersion]`` — a
selector that exists ONLY on Tk's own ``TKApplication`` subclass of ``NSApplication``.
Where pyobjc (via pywebview's cocoa backend) got there first, ``NSApp`` is a plain
``NSApplication``, the selector is unrecognised, and Tk aborts the process inside
``Tkapp_New`` rather than raising:

    -[NSApplication macOSVersion]: unrecognized selector sent to instance …
    Tkapp_New -> Tcl_AppInit -> TkCreateFrame -> Tk_GetColor -> TkpGetColor -> GetRGBA
    (crash report Python-2026-08-26-213325.ips, SIGABRT, libtcl9tk9.0.dylib)

``except TclError`` cannot catch an abort, so the only defence is to ASK BEFORE
constructing. Unlike its neighbour ``gui_registration_blocked_reason`` this gate is
FAIL-CLOSED: a false negative kills the process, while a false positive only costs a
degraded dialog on a path that is already a fallback.

Run:  python3 -m unittest client.popup.tests.test_tk_registration_gate
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from client.popup import backend


def _appkit_double(app):
    """A stand-in for the pyobjc ``AppKit`` module holding ``app`` as the running NSApp."""
    return SimpleNamespace(NSApp=lambda: app)


class _ForeignApp:
    """What pyobjc leaves behind: a plain NSApplication, no Tk selectors."""

    def __repr__(self) -> str:  # pragma: no cover — only for assertion messages
        return "<NSApplication foreign>"


class _TkApp:
    """What Tk itself installs; its real pyobjc class is named TKApplication."""


_TkApp.__name__ = "TKApplication"


class TkCreationGateTests(unittest.TestCase):
    def test_gate_is_inert_off_darwin(self):
        """Only the macOS Tk backend reaches for an NSApplication selector."""
        with (
            mock.patch.object(backend.sys, "platform", "win32"),
            mock.patch.dict(sys.modules, {"AppKit": _appkit_double(_ForeignApp())}),
        ):
            self.assertIsNone(backend.tk_creation_blocked_reason())

    def test_an_unloaded_appkit_is_never_imported_to_answer(self):
        """Importing AppKit to run the check would CAUSE the very registration it looks for."""
        with (
            mock.patch.object(backend.sys, "platform", "darwin"),
            mock.patch.dict(sys.modules, {"AppKit": None}),
        ):
            sys.modules.pop("AppKit")
            self.assertIsNone(backend.tk_creation_blocked_reason())
            self.assertNotIn("AppKit", sys.modules, "the gate must not import AppKit itself")

    def test_appkit_present_but_no_running_application_is_clear(self):
        """pywebview imported is harmless; only `initialize()` installs the NSApp."""
        with (
            mock.patch.object(backend.sys, "platform", "darwin"),
            mock.patch.dict(sys.modules, {"AppKit": _appkit_double(None)}),
        ):
            self.assertIsNone(backend.tk_creation_blocked_reason())

    def test_a_foreign_nsapplication_blocks_tk(self):
        with (
            mock.patch.object(backend.sys, "platform", "darwin"),
            mock.patch.dict(sys.modules, {"AppKit": _appkit_double(_ForeignApp())}),
        ):
            reason = backend.tk_creation_blocked_reason()

        self.assertIsNotNone(reason)
        self.assertIn("NSApplication", reason)

    def test_tks_own_application_is_not_a_conflict(self):
        """A second Tk() in a Tk-owned process is exactly the case that has always worked."""
        with (
            mock.patch.object(backend.sys, "platform", "darwin"),
            mock.patch.dict(sys.modules, {"AppKit": _appkit_double(_TkApp())}),
        ):
            self.assertIsNone(backend.tk_creation_blocked_reason())

    def test_an_unreadable_nsapp_fails_closed(self):
        """Opposite polarity to gui_registration_blocked_reason: no verdict means DO NOT build."""

        def explode():
            raise RuntimeError("bridge unavailable")

        with (
            mock.patch.object(backend.sys, "platform", "darwin"),
            mock.patch.dict(sys.modules, {"AppKit": SimpleNamespace(NSApp=explode)}),
        ):
            self.assertIsNotNone(backend.tk_creation_blocked_reason())


@unittest.skipUnless(sys.platform == "darwin", "the gate only has an effect on macOS")
class RealProcessStateTests(unittest.TestCase):
    def test_this_unregistered_test_process_is_not_blocked(self):
        """The anti-false-positive lock: an ordinary process must still get its window."""
        self.assertIsNone(backend.tk_creation_blocked_reason())


if __name__ == "__main__":
    unittest.main()
