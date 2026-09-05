"""Tests for LibreOffice discovery + on-demand install (installer.office)."""

from __future__ import annotations

import unittest
from unittest import mock

from installer import office


class FindLibreOfficeTests(unittest.TestCase):
    def test_found_on_path(self):
        with mock.patch.object(office.shutil, "which", side_effect=lambda n: "/usr/bin/soffice" if n == "soffice" else None):
            self.assertEqual(office.find_libreoffice(), "/usr/bin/soffice")

    def test_absent_everywhere_returns_none(self):
        with mock.patch.object(office.shutil, "which", return_value=None), \
             mock.patch.object(office.sys, "platform", "linux"):
            self.assertIsNone(office.find_libreoffice())

    def test_macos_app_bundle_fallback(self):
        with mock.patch.object(office.shutil, "which", return_value=None), \
             mock.patch.object(office.sys, "platform", "darwin"), \
             mock.patch.object(office.os.path, "isfile", return_value=True), \
             mock.patch.object(office.os, "access", return_value=True):
            self.assertEqual(office.find_libreoffice(), office._MAC_APP_SOFFICE)


class InstallHintTests(unittest.TestCase):
    def test_macos_hint_is_brew_cask(self):
        with mock.patch.object(office.sys, "platform", "darwin"):
            self.assertIn("brew install --cask libreoffice", office.install_hint())

    def test_linux_hint_is_apt_or_dnf(self):
        with mock.patch.object(office.sys, "platform", "linux"):
            hint = office.install_hint()
            self.assertIn("apt-get", hint)
            self.assertIn("libreoffice", hint)


class EnsureLibreOfficeTests(unittest.TestCase):
    def test_already_present_returns_ready(self):
        with mock.patch.object(office, "find_libreoffice", return_value="/usr/bin/soffice"):
            ready, detail = office.ensure_libreoffice()
        self.assertTrue(ready)
        self.assertEqual(detail, "/usr/bin/soffice")

    def test_absent_no_brew_returns_hint_never_sudo(self):
        # Linux path: no auto-install (needs root); must return the manual command, run nothing.
        with mock.patch.object(office, "find_libreoffice", return_value=None), \
             mock.patch.object(office.sys, "platform", "linux"), \
             mock.patch.object(office.subprocess, "run") as run:
            ready, detail = office.ensure_libreoffice()
        self.assertFalse(ready)
        self.assertIn("libreoffice", detail)
        run.assert_not_called()  # NEVER a silent install on Linux (root)

    def test_macos_auto_install_via_brew_then_rediscover(self):
        calls = {"n": 0}

        def _find():
            calls["n"] += 1
            return None if calls["n"] == 1 else "/opt/homebrew/bin/soffice"

        with mock.patch.object(office, "find_libreoffice", side_effect=_find), \
             mock.patch.object(office.sys, "platform", "darwin"), \
             mock.patch.object(office.shutil, "which", return_value="/opt/homebrew/bin/brew"), \
             mock.patch.object(office.subprocess, "run") as run:
            ready, detail = office.ensure_libreoffice()
        self.assertTrue(ready)
        run.assert_called_once()
        self.assertIn("--cask", run.call_args.args[0])


class OfficeMainTests(unittest.TestCase):
    def test_main_zero_when_ready(self):
        with mock.patch.object(office, "ensure_libreoffice", return_value=(True, "/usr/bin/soffice")):
            self.assertEqual(office.main(), 0)

    def test_main_one_when_absent(self):
        with mock.patch.object(office, "ensure_libreoffice", return_value=(False, "brew install --cask libreoffice")):
            self.assertEqual(office.main(), 1)


if __name__ == "__main__":
    unittest.main()
