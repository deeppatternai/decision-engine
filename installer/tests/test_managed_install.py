"""Behavior locks for the managed-install identity authorization contract."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import managed_install


class ManagedInstallIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "deeppattern"
        self.root = self.home / "decision-engine"
        (self.root / ".git").mkdir(parents=True)
        self.managed_home = mock.patch.object(
            managed_install.config, "DEFAULT_DEEPPATTERN_HOME", self.home
        )
        self.managed_home.start()
        self.env = mock.patch.dict(os.environ, {"DEEPPATTERN_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.managed_home.stop)
        self.addCleanup(self.tmp.cleanup)

    def _remotes(self):
        return dict(managed_install.OFFICIAL_REMOTE_URLS)

    def test_paired_marker_and_external_registration_authorize_the_exact_root(self):
        install_id = managed_install.write_managed_identity(self.root)

        identity = managed_install.validate_managed_identity(self.root, self._remotes())

        self.assertEqual(identity.install_id, install_id)
        self.assertEqual(identity.canonical_root, self.root.resolve())
        self.assertEqual(identity.repository_id, managed_install.REPOSITORY_ID)

    def test_repeated_identity_write_is_idempotent(self):
        first = managed_install.write_managed_identity(self.root)
        second = managed_install.write_managed_identity(self.root)
        self.assertEqual(second, first)

    def test_managed_authority_ignores_environment_redirects(self):
        managed_install.write_managed_identity(self.root)
        redirected = Path(self.tmp.name) / "attacker-controlled-home"
        with mock.patch.dict(os.environ, {"DEEPPATTERN_HOME": str(redirected)}, clear=False):
            self.assertEqual(managed_install.registration_path().parent.parent, self.home)
            identity = managed_install.validate_managed_identity(self.root, self._remotes())
        self.assertEqual(identity.canonical_root, self.root.resolve())

    def test_default_managed_home_is_not_derived_from_deeppattern_home_override(self):
        redirected = Path(self.tmp.name) / "hostile-import-home"
        environment = dict(os.environ)
        environment["DEEPPATTERN_HOME"] = str(redirected)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from installer import config; "
                "print(config.DEFAULT_DEEPPATTERN_HOME); print(config.deeppattern_home())",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        default_home, redirected_home = result.stdout.splitlines()
        self.assertEqual(Path(default_home), Path.home() / ".deeppattern")
        self.assertEqual(Path(redirected_home), redirected)

    def test_write_and_validate_refuse_a_developer_checkout(self):
        dev = Path(self.tmp.name) / "developer-checkout"
        (dev / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(managed_install.ManagedInstallError, "canonical install root"):
            managed_install.write_managed_identity(dev)

        managed_install.write_managed_identity(self.root)
        (dev / managed_install.MARKER_FILENAME).write_bytes(
            (self.root / managed_install.MARKER_FILENAME).read_bytes()
        )
        with self.assertRaisesRegex(managed_install.ManagedInstallError, "canonical install root"):
            managed_install.validate_managed_identity(dev, self._remotes())

    def test_partial_identity_is_not_silently_replaced(self):
        managed_install.write_managed_identity(self.root)
        (self.root / managed_install.MARKER_FILENAME).unlink()
        with self.assertRaisesRegex(managed_install.ManagedInstallError, "partial"):
            managed_install.write_managed_identity(self.root)

    def test_identity_lock_contention_fails_without_changing_the_pair(self):
        with managed_install._managed_identity_lock():
            with self.assertRaisesRegex(managed_install.ManagedInstallError, "in progress"):
                managed_install.write_managed_identity(self.root)
        install_id = managed_install.write_managed_identity(self.root)
        self.assertEqual(
            managed_install.validate_managed_identity(self.root, self._remotes()).install_id,
            install_id,
        )

    def test_identity_lock_serializes_a_second_process(self):
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from installer import config, managed_install\n"
            "config.DEFAULT_DEEPPATTERN_HOME = Path(sys.argv[1])\n"
            "with managed_install._managed_identity_lock():\n"
            "    print('locked', flush=True)\n"
            "    sys.stdin.read(1)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.home)],
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(managed_install.ManagedInstallError, "in progress"):
                managed_install.write_managed_identity(self.root)
        finally:
            if child.stdin:
                child.stdin.write("x")
                child.stdin.flush()
                child.stdin.close()
            child.wait(timeout=10)
            stdout = child.stdout.read() if child.stdout else ""
            stderr = child.stderr.read() if child.stderr else ""
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()
            self.assertEqual((child.returncode, stdout, stderr), (0, "", ""))
        managed_install.write_managed_identity(self.root)

    def test_marker_alone_never_authorizes_a_copied_developer_checkout(self):
        managed_install.write_managed_identity(self.root)
        dev = Path(self.tmp.name) / "developer-checkout"
        (dev / ".git").mkdir(parents=True)
        (dev / managed_install.MARKER_FILENAME).write_bytes(
            (self.root / managed_install.MARKER_FILENAME).read_bytes()
        )

        with self.assertRaisesRegex(managed_install.ManagedInstallError, "canonical.*root"):
            managed_install.validate_managed_identity(dev, self._remotes())

    def test_mismatched_install_ids_fail_closed(self):
        managed_install.write_managed_identity(self.root)
        marker_path = self.root / managed_install.MARKER_FILENAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["install_id"] = "11111111-1111-4111-8111-111111111111"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        with self.assertRaisesRegex(managed_install.ManagedInstallError, "install ID"):
            managed_install.validate_managed_identity(self.root, self._remotes())

    def test_unknown_marker_fields_and_schema_are_rejected(self):
        managed_install.write_managed_identity(self.root)
        marker_path = self.root / managed_install.MARKER_FILENAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        for name, value in (("unexpected", True), ("schema", 2)):
            with self.subTest(name=name):
                changed = dict(marker)
                changed[name] = value
                marker_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(managed_install.ManagedInstallError):
                    managed_install.validate_managed_identity(self.root, self._remotes())

    def test_duplicate_identity_json_keys_are_rejected(self):
        managed_install.write_managed_identity(self.root)
        marker_path = self.root / managed_install.MARKER_FILENAME
        rendered = marker_path.read_text(encoding="utf-8")
        duplicate = rendered.replace('"schema": 1', '"schema": 1,\n  "schema": 1', 1)
        marker_path.write_text(duplicate, encoding="utf-8")

        with self.assertRaisesRegex(managed_install.ManagedInstallError, "duplicate"):
            managed_install.validate_managed_identity(self.root, self._remotes())

    def test_registration_requires_an_absolute_canonical_root(self):
        managed_install.write_managed_identity(self.root)
        registration = managed_install.registration_path()
        payload = json.loads(registration.read_text(encoding="utf-8"))
        payload["canonical_root"] = "decision-engine"
        registration.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(managed_install.ManagedInstallError, "absolute"):
            managed_install.validate_managed_identity(self.root, self._remotes())

    def test_registration_directory_must_not_be_a_link(self):
        managed_install.write_managed_identity(self.root)
        registration = managed_install.registration_path()
        payload = registration.read_bytes()
        registration.unlink()
        (registration.parent / "decision-engine.identity.lock").unlink()
        registration.parent.rmdir()
        redirected = Path(self.tmp.name) / "redirected-registration"
        redirected.mkdir()
        (redirected / "decision-engine.json").write_bytes(payload)
        try:
            registration.parent.symlink_to(redirected, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable on this host")
        with self.assertRaisesRegex(managed_install.ManagedInstallError, "symlink or reparse"):
            managed_install.validate_managed_identity(self.root, self._remotes())

    def test_unofficial_missing_or_rewritten_remote_is_rejected(self):
        managed_install.write_managed_identity(self.root)
        cases = (
            {"github": "https://example.invalid/attacker/repo.git", "gitee": self._remotes()["gitee"]},
            {"github": self._remotes()["github"]},
            {**self._remotes(), "extra": "https://example.invalid/repo.git"},
        )
        for remotes in cases:
            with self.subTest(remotes=remotes):
                with self.assertRaisesRegex(managed_install.ManagedInstallError, "official remote"):
                    managed_install.validate_managed_identity(self.root, remotes)

    def test_linked_worktree_git_file_is_rejected(self):
        managed_install.write_managed_identity(self.root)
        git_dir = self.root / ".git"
        git_dir.rmdir()
        git_dir.write_text("gitdir: elsewhere", encoding="utf-8")

        with self.assertRaisesRegex(managed_install.ManagedInstallError, "linked worktree"):
            managed_install.validate_managed_identity(self.root, self._remotes())

    def test_symlink_alias_is_rejected_even_when_it_resolves_to_registered_root(self):
        managed_install.write_managed_identity(self.root)
        alias = Path(self.tmp.name) / "managed-alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable on this host")

        with self.assertRaisesRegex(managed_install.ManagedInstallError, "symlink or reparse"):
            managed_install.validate_managed_identity(alias, self._remotes())

    def test_network_root_is_rejected_before_identity_files_are_read(self):
        with mock.patch.object(managed_install, "_is_network_path", return_value=True):
            with self.assertRaisesRegex(managed_install.ManagedInstallError, "network"):
                managed_install.validate_managed_identity(self.root, self._remotes())

    def test_windows_network_detection_errors_fail_closed(self):
        if os.name != "nt":
            self.skipTest("Windows drive type probing is Windows-specific")
        with mock.patch.object(
            managed_install, "_windows_drive_type", side_effect=OSError("probe failed")
        ):
            with self.assertRaisesRegex(managed_install.ManagedInstallError, "determine"):
                managed_install.validate_managed_identity(self.root, self._remotes())

    def test_windows_unknown_missing_and_remote_drive_types_fail_closed(self):
        if os.name != "nt":
            self.skipTest("Windows drive type probing is Windows-specific")
        managed_install.write_managed_identity(self.root)
        for drive_type in (0, 1, 4, 99):
            with self.subTest(drive_type=drive_type):
                with mock.patch.object(
                    managed_install, "_windows_drive_type", return_value=drive_type
                ):
                    with self.assertRaises(managed_install.ManagedInstallError):
                        managed_install.validate_managed_identity(self.root, self._remotes())

        for drive_type in (2, 3, 5, 6):
            with self.subTest(local_drive_type=drive_type):
                with mock.patch.object(
                    managed_install, "_windows_drive_type", return_value=drive_type
                ):
                    identity = managed_install.validate_managed_identity(
                        self.root, self._remotes()
                    )
                self.assertEqual(identity.canonical_root, self.root.resolve())

    def test_registration_and_marker_are_private_where_modes_are_enforced(self):
        managed_install.write_managed_identity(self.root)
        if os.name == "nt":
            self.skipTest("Windows ACLs are not represented by POSIX mode bits")
        registration = managed_install.registration_path()
        self.assertEqual(stat.S_IMODE(registration.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(registration.parent.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.root / managed_install.MARKER_FILENAME).stat().st_mode), 0o600
        )

    def test_registration_parent_must_remain_private_on_posix(self):
        managed_install.write_managed_identity(self.root)
        if os.name == "nt":
            self.skipTest("Windows uses inherited ACLs rather than POSIX mode bits")
        managed_install.registration_path().parent.chmod(0o777)
        with self.assertRaisesRegex(managed_install.ManagedInstallError, "directory permissions"):
            managed_install.validate_managed_identity(self.root, self._remotes())

    def test_overly_permissive_registration_is_rejected_on_posix(self):
        managed_install.write_managed_identity(self.root)
        if os.name == "nt":
            self.skipTest("Windows uses inherited ACLs rather than POSIX mode bits")
        registration = managed_install.registration_path()
        registration.chmod(0o644)
        with self.assertRaisesRegex(managed_install.ManagedInstallError, "permissions"):
            managed_install.validate_managed_identity(self.root, self._remotes())

    def test_link_probe_os_errors_are_wrapped(self):
        with mock.patch("installer.managed_install.os.lstat", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(managed_install.ManagedInstallError, "inspect path"):
                managed_install.validate_managed_identity(self.root, self._remotes())


if __name__ == "__main__":
    unittest.main()
