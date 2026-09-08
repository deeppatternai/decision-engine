"""Coverage for the popup shell's Windows taskbar identity (``native_shell``).

The stop panel is tkinter and gets its icon from ``tk_icon.apply_window_icon``. This popup is
pywebview in a SEPARATE process, and its Windows backend is a WinForms host that takes no ``icon``
argument at all — that parameter exists only on the GTK and Qt backends. So the icon has to be
stamped on the raw window handle, and the AppUserModelID has to be claimed by this process too:
inheriting the stop panel's is not a thing, they share no process.

This pins the wiring — the handle is found, the icon is applied once, and every failure is absorbed.
Whether the taskbar button actually changes is a Windows-only observation this cannot make.

Headless (stdlib only; no window / pywebview backend). Run from the repo root:

    python3 -m unittest client.popup.tests.test_taskbar_icon
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from client.popup import native_shell


class _Handle:
    """A pythonnet IntPtr stand-in: int() refuses it, ToInt64() answers."""

    def __init__(self, value):
        self._value = value

    def __int__(self):
        raise TypeError("cannot convert IntPtr")

    def ToInt64(self):
        return self._value


class WindowsHandle(unittest.TestCase):
    def test_a_plain_integer_handle_is_taken_as_is(self):
        win = SimpleNamespace(native=SimpleNamespace(Handle=0xBEEF))
        self.assertEqual(native_shell._windows_hwnd(win), 0xBEEF)

    def test_an_intptr_handle_goes_through_toint64(self):
        """pythonnet does not always make IntPtr int()-able, and the backend hands back an IntPtr."""
        win = SimpleNamespace(native=SimpleNamespace(Handle=_Handle(0xBEEF)))
        self.assertEqual(native_shell._windows_hwnd(win), 0xBEEF)

    def test_a_backend_without_native_answers_zero(self):
        """The pin is ``pywebview>=4.0`` with no ceiling, so ``native`` may not be there. A build
        that renames or drops it must cost the icon, not the window."""
        self.assertEqual(native_shell._windows_hwnd(SimpleNamespace()), 0)
        self.assertEqual(native_shell._windows_hwnd(SimpleNamespace(native=object())), 0)


class InstallTaskbarIcon(unittest.TestCase):
    def test_the_handle_reaches_apply_taskbar_icon(self):
        win = SimpleNamespace(native=SimpleNamespace(Handle=0xBEEF))
        with mock.patch("client.tk_icon.apply_taskbar_icon", return_value=True) as apply_icon:
            native_shell._install_windows_taskbar_icon(win)
        apply_icon.assert_called_once_with(0xBEEF)

    def test_it_runs_once_across_repeated_loads(self):
        """``loaded`` fires once per navigation and the Cursor profile navigates (bootstrap →
        target). Each LoadImageW hands back a fresh HICON that nothing frees, so an unguarded
        handler leaks a pair per hop."""
        win = SimpleNamespace(native=SimpleNamespace(Handle=0xBEEF))
        with mock.patch("client.tk_icon.apply_taskbar_icon", return_value=True) as apply_icon:
            native_shell._install_windows_taskbar_icon(win)
            native_shell._install_windows_taskbar_icon(win)
            native_shell._install_windows_taskbar_icon(win)
        self.assertEqual(apply_icon.call_count, 1)

    def test_a_failed_apply_is_retried_on_the_next_load(self):
        """Only a SUCCESS latches. A first call that lands before the handle is real must not
        spend the one attempt this window gets."""
        win = SimpleNamespace(native=SimpleNamespace(Handle=0xBEEF))
        with mock.patch("client.tk_icon.apply_taskbar_icon", return_value=False) as apply_icon:
            native_shell._install_windows_taskbar_icon(win)
            native_shell._install_windows_taskbar_icon(win)
        self.assertEqual(apply_icon.call_count, 2)

    def test_no_handle_never_calls_through(self):
        with mock.patch("client.tk_icon.apply_taskbar_icon") as apply_icon:
            native_shell._install_windows_taskbar_icon(SimpleNamespace())
        apply_icon.assert_not_called()

    def test_a_raising_apply_never_escapes(self):
        """This runs from a pywebview event handler; an exception there is the popup's problem."""
        win = SimpleNamespace(native=SimpleNamespace(Handle=0xBEEF))
        with mock.patch("client.tk_icon.apply_taskbar_icon", side_effect=OSError("no user32")):
            native_shell._install_windows_taskbar_icon(win)   # must not raise


class ClaimAppIdentity(unittest.TestCase):
    def test_the_popup_process_claims_its_own_identity(self):
        with mock.patch("client.tk_icon.claim_app_identity", return_value=True) as claim:
            native_shell._claim_app_identity()
        claim.assert_called_once_with()

    def test_a_raising_claim_never_escapes(self):
        """It is called on the line before create_window. Nothing here may stop a window opening."""
        with mock.patch("client.tk_icon.claim_app_identity", side_effect=OSError("no shell32")):
            native_shell._claim_app_identity()   # must not raise


class DockIdentity(unittest.TestCase):
    def test_the_localized_title_reaches_macos_dock_identity(self):
        with mock.patch("client.tk_icon.apply_dock_app_name", return_value=True) as app_name, \
                mock.patch("client.tk_icon.apply_dock_icon", return_value=True) as icon:
            native_shell._install_dock_icon("Decision Engine - \u56fe\u89e3")
        app_name.assert_called_once_with("Decision Engine - \u56fe\u89e3")
        icon.assert_called_once_with()

    def test_a_raising_dock_identity_never_escapes(self):
        with mock.patch("client.tk_icon.apply_dock_app_name", side_effect=OSError("no app")), \
                mock.patch("client.tk_icon.apply_dock_icon", return_value=True):
            native_shell._install_dock_icon("Decision Engine - \u56fe\u89e3")


if __name__ == "__main__":
    unittest.main()
