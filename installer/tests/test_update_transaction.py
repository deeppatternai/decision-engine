"""Behavior locks for the managed updater transaction and shim admission protocol."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from installer.tests.test_release_contract import _TEST_E, _TEST_N, _sign
from installer import (
    doctor,
    launcher,
    managed_install,
    release_acquisition,
    release_contract,
    update_coordination,
    update_staging,
    update_transaction,
    updater,
    windows_security,
)


@unittest.skipUnless(os.name == "nt", "Win32 boundary tests")
class WindowsSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_pinned_directory_detects_path_rename(self):
        root = Path(self.tmp.name) / "root"
        moved = Path(self.tmp.name) / "moved"
        root.mkdir()
        with windows_security.PinnedWindowsDirectory(root) as pinned:
            pinned.validate()
            os.replace(root, moved)
            with self.assertRaises(windows_security.WindowsSecurityError):
                pinned.validate()
            os.replace(moved, root)
            pinned.validate()

    def test_private_acl_masks_cover_delete_child_and_maximum_allowed(self):
        self.assertTrue(windows_security._MUTATION_ACCESS_MASK & 0x00000040)
        self.assertTrue(windows_security._MUTATION_ACCESS_MASK & 0x02000000)
        self.assertTrue(windows_security._DATA_ACCESS_MASK & 0x02000000)

    def test_hardening_rejects_untrusted_owner_before_acl_write(self):
        kernel32 = SimpleNamespace(CloseHandle=mock.Mock())
        advapi32 = SimpleNamespace(SetSecurityInfo=mock.Mock())
        with (
            mock.patch.object(
                windows_security,
                "_open_security_handle",
                return_value=(123, True, kernel32, advapi32),
            ),
            mock.patch.object(
                windows_security,
                "_validate_trusted_owner_handle",
                side_effect=windows_security.WindowsSecurityError("untrusted owner"),
            ),
            self.assertRaisesRegex(
                windows_security.WindowsSecurityError, "untrusted owner"
            ),
        ):
            windows_security.harden_private_data_acl(Path(self.tmp.name))

        advapi32.SetSecurityInfo.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(123)

    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_acl_rejects_world_writable_directory(self):
        root = Path(self.tmp.name) / "broad"
        root.mkdir()

        def remove_world_grant():
            subprocess.run(
                ["icacls", str(root), "/remove:g", "*S-1-1-0", "/T", "/C"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        self.addCleanup(remove_world_grant)
        granted = subprocess.run(
            ["icacls", str(root), "/grant", "*S-1-1-0:(OI)(CI)M"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(granted.returncode, 0)
        with self.assertRaisesRegex(
            windows_security.WindowsSecurityError, "untrusted principal"
        ):
            windows_security.validate_private_mutation_acl(root)

    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_private_data_acl_rejects_world_read_only_directory(self):
        with tempfile.TemporaryDirectory(prefix="de-private-acl-", dir=Path.home()) as temp:
            root = Path(temp) / "readable"
            root.mkdir()
            try:
                granted = subprocess.run(
                    ["icacls", str(root), "/grant", "*S-1-1-0:(OI)(CI)RX"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.assertEqual(granted.returncode, 0)
                windows_security.validate_private_mutation_acl(root)
                with self.assertRaisesRegex(
                    windows_security.WindowsSecurityError, "untrusted principal"
                ):
                    windows_security.validate_private_data_acl(root)
            finally:
                subprocess.run(
                    ["icacls", str(root), "/remove:g", "*S-1-1-0", "/T", "/C"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_private_data_acl_hardening_removes_inherited_readers(self):
        root = Path(self.tmp.name) / "private-data"
        root.mkdir()

        granted = subprocess.run(
            ["icacls", str(root), "/grant", "*S-1-1-0:(OI)(CI)RX"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(granted.returncode, 0)

        child = root / "child"
        child.mkdir()
        secret = child / "secret.txt"
        secret.write_text("private", encoding="utf-8")

        with self.assertRaisesRegex(
            windows_security.WindowsSecurityError, "untrusted principal"
        ):
            windows_security.validate_private_data_acl(root)
        with self.assertRaisesRegex(
            windows_security.WindowsSecurityError, "untrusted principal"
        ):
            windows_security.validate_private_data_acl(secret)

        windows_security.harden_private_data_acl(root)
        windows_security.validate_private_data_acl(root)
        windows_security.validate_private_data_acl(child)
        windows_security.validate_private_data_acl(secret)

    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_private_data_fd_rejects_world_readable_file(self):
        root = Path(self.tmp.name) / "private-fd"
        root.mkdir()
        windows_security.harden_private_data_acl(root)
        secret = root / "secret.json"
        secret.write_text("{}", encoding="utf-8")
        with secret.open("rb") as handle:
            windows_security.validate_private_data_fd(handle.fileno())
        granted = subprocess.run(
            ["icacls", str(secret), "/grant", "*S-1-1-0:(R)"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(granted.returncode, 0)
        with secret.open("rb") as handle, self.assertRaisesRegex(
            windows_security.WindowsSecurityError, "untrusted principal"
        ):
            windows_security.validate_private_data_fd(handle.fileno())

    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_acl_rejects_inherit_only_world_writable_directory(self):
        root = Path(self.tmp.name) / "inherit-only-broad"
        root.mkdir()

        def remove_world_grant():
            subprocess.run(
                ["icacls", str(root), "/remove:g", "*S-1-1-0", "/C"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        self.addCleanup(remove_world_grant)
        granted = subprocess.run(
            ["icacls", str(root), "/grant", "*S-1-1-0:(OI)(CI)(IO)M"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(granted.returncode, 0)
        with self.assertRaisesRegex(
            windows_security.WindowsSecurityError, "untrusted principal"
        ):
            windows_security.validate_private_mutation_acl(root)

    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_acl_does_not_treat_a_write_deny_as_a_grant(self):
        root = Path(self.tmp.name) / "explicit-deny"
        root.mkdir()

        def remove_world_deny():
            subprocess.run(
                ["icacls", str(root), "/remove:d", "*S-1-1-0", "/C"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        self.addCleanup(remove_world_deny)
        denied = subprocess.run(
            ["icacls", str(root), "/deny", "*S-1-1-0:(WD)"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(denied.returncode, 0)
        windows_security.validate_private_mutation_acl(root)

class CrossPlatformMoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_no_replace_move_preserves_an_existing_destination(self):
        source = Path(self.tmp.name) / "source.txt"
        destination = Path(self.tmp.name) / "destination.txt"
        source.write_text("source", encoding="utf-8")
        destination.write_text("destination", encoding="utf-8")
        try:
            with self.assertRaises(FileExistsError):
                windows_security.move_write_through(
                    source, destination, replace_existing=False
                )
        except windows_security.WindowsSecurityError as exc:
            if (
                "unavailable on this platform" not in str(exc)
                or sys.platform.startswith("linux")
                or sys.platform == "darwin"
            ):
                raise
            self.skipTest(str(exc))
        self.assertEqual(source.read_text(encoding="utf-8"), "source")
        self.assertEqual(destination.read_text(encoding="utf-8"), "destination")


class _ManagedRootTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "deeppattern"
        self.root = self.home / "decision-engine"
        self.root.mkdir(parents=True)
        (self.root / ".git").mkdir()
        self.managed_home = mock.patch.object(
            managed_install.config, "DEFAULT_DEEPPATTERN_HOME", self.home
        )
        self.managed_home.start()
        self.addCleanup(self.managed_home.stop)

    def _subprocess_environment(self):
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[2])
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = source_root + (os.pathsep + existing if existing else "")
        return environment

    def _stop_process(self, process, *, kill=False):
        if process.poll() is None:
            if kill:
                process.kill()
            elif process.stdin is not None:
                process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stderr is not None:
            process.stderr.close()

    def _wait_for_file(self, path: Path, process, label: str) -> str:
        deadline = time.monotonic() + 30
        while not path.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                error = process.stderr.read() if process.stderr is not None else ""
                self.fail("%s exited early: %s" % (label, error))
            time.sleep(0.02)
        if not path.exists():
            self._stop_process(process, kill=True)
            self.fail("timed out waiting for %s" % label)
        return path.read_text(encoding="utf-8")

    def _spawn_holder(self, kind: str, *, commit: str = "1" * 40, attempt: Path = None):
        ready = self.root / ("%s-%s.ready" % (kind, time.monotonic_ns()))
        attempt_path = attempt or self.root / ("%s-%s.attempt" % (kind, time.monotonic_ns()))
        script = (
            "import pathlib,sys; "
            "from installer import update_coordination as c; "
            "root=pathlib.Path(sys.argv[1]); ready=pathlib.Path(sys.argv[2]); "
            "attempt=pathlib.Path(sys.argv[3]); kind=sys.argv[4]; commit=sys.argv[5]; "
            "attempt.write_text('attempting'); "
            "ctx=(c.install_transaction(root, timeout_seconds=20) if kind=='transaction' "
            "else c.shim_session_lease(root, commit, admission_timeout_seconds=20, heartbeat_seconds=None)); "
            "held=ctx.__enter__(); ready.write_text(str(getattr(held,'path','ready'))); "
            "sys.stdin.read(); ctx.__exit__(None,None,None)"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.root),
                str(ready),
                str(attempt_path),
                kind,
                commit,
            ],
            env=self._subprocess_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_process, process)
        self._wait_for_file(attempt_path, process, "%s holder attempt" % kind)
        return process, ready


class UpdateCoordinationTests(_ManagedRootTestCase):
    def test_second_process_cannot_enter_held_install_transaction(self):
        holder, ready = self._spawn_holder("transaction")
        self._wait_for_file(ready, holder, "transaction holder")

        with self.assertRaises(update_coordination.InstallTransactionBusy):
            with update_coordination.install_transaction(self.root, timeout_seconds=0.25):
                self.fail("contending process entered the transaction")
        self._stop_process(holder, kill=True)
        with update_coordination.install_transaction(self.root, timeout_seconds=2):
            pass

    def test_os_lock_is_authoritative_for_live_and_stale_leases(self):
        holder, ready = self._spawn_holder("lease", commit="1" * 40)
        lease_path = Path(self._wait_for_file(ready, holder, "lease holder"))
        old = time.time() - 86_400
        os.utime(lease_path, (old, old))
        self.assertLess(lease_path.stat().st_mtime, time.time() - 80_000)
        live = update_coordination.live_shim_sessions(self.root)
        self.assertEqual([item.path for item in live], [lease_path])

        self._stop_process(holder, kill=True)
        self.assertEqual(update_coordination.live_shim_sessions(self.root), ())
        self.assertFalse(lease_path.exists(), "an unlocked lease record is reclaimed")

    def test_shim_admission_waits_for_the_writer_then_holds_a_lease(self):
        with update_coordination.install_transaction(self.root, timeout_seconds=1):
            holder, ready = self._spawn_holder("lease", commit="2" * 40)
            self.assertFalse(ready.exists(), "shim cannot be admitted while writer holds lock")
        lease_path = Path(self._wait_for_file(ready, holder, "shim admission"))
        self.assertEqual(
            [item.path for item in update_coordination.live_shim_sessions(self.root)],
            [lease_path],
        )

    def test_unfinished_journal_blocks_new_shim_admission(self):
        journal = self.root / update_coordination.UPDATE_JOURNAL_RELATIVE_PATH
        journal.parent.mkdir(parents=True)
        journal.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            update_coordination.UpdateCoordinationError, "unfinished managed update"
        ):
            with update_coordination.shim_session_lease(
                self.root, "3" * 40, heartbeat_seconds=None
            ):
                self.fail("shim entered while an update journal existed")

    def test_initial_lease_write_failure_releases_lock_and_path(self):
        with mock.patch.object(
            update_coordination,
            "_write_locked_payload",
            side_effect=update_coordination.UpdateCoordinationError("disk full"),
        ):
            with self.assertRaisesRegex(
                update_coordination.UpdateCoordinationError, "disk full"
            ):
                with update_coordination.shim_session_lease(
                    self.root, "4" * 40, heartbeat_seconds=None
                ):
                    self.fail("lease context unexpectedly entered")
        self.assertEqual(update_coordination.live_shim_sessions(self.root), ())
        leases = self.root / update_coordination.LEASES_RELATIVE_PATH
        self.assertEqual(tuple(leases.glob("*.json")), ())

    def test_heartbeat_and_concurrent_scans_keep_live_payload_valid(self):
        commit = "5" * 40
        with update_coordination.shim_session_lease(
            self.root, commit, heartbeat_seconds=0.01
        ) as lease:
            for _attempt in range(20):
                live = update_coordination.live_shim_sessions(self.root)
                self.assertEqual(len(live), 1)
                self.assertEqual(live[0].path, lease.path)
                self.assertEqual(live[0].running_commit, commit)
                time.sleep(0.005)
        self.assertEqual(update_coordination.live_shim_sessions(self.root), ())

    def test_lease_scan_refuses_an_unbounded_directory(self):
        leases = self.root / update_coordination.LEASES_RELATIVE_PATH
        leases.mkdir(mode=0o700, parents=True)
        if os.name != "nt":
            leases.chmod(0o700)
        with mock.patch.object(update_coordination, "_MAX_LEASE_FILES", 2):
            for index in range(3):
                (leases / ("%d.json" % index)).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                update_coordination.UpdateCoordinationError, "entry-count"
            ):
                update_coordination.live_shim_sessions(self.root)

    def test_closed_or_forged_transaction_cannot_authorize_a_scan(self):
        with update_coordination.install_transaction(self.root) as transaction:
            forged = update_coordination.InstallTransaction(
                transaction.path,
                transaction.fileno(),
                transaction._identity,
                object(),
                transaction._released,
            )
            with self.assertRaisesRegex(
                update_coordination.UpdateCoordinationError, "capability"
            ):
                update_coordination.live_shim_sessions(
                    self.root, transaction=forged
                )
        with update_coordination.install_transaction(self.root) as replacement:
            self.assertEqual(replacement.path, transaction.path)
            with self.assertRaisesRegex(
                update_coordination.UpdateCoordinationError, "no longer active"
            ):
                update_coordination.live_shim_sessions(
                    self.root, transaction=transaction
                )


@unittest.skipUnless(shutil.which("git"), "Git is required for update transaction tests")
class UpdateTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "deeppattern"
        self.root = self.home / "decision-engine"
        self.root.mkdir(parents=True)
        self.git = shutil.which("git")
        self.git_home = Path(self.tmp.name) / "git-home"
        self.git_home.mkdir()
        self.empty_template = Path(self.tmp.name) / "empty-git-template"
        self.empty_template.mkdir()
        self.git_environment = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": str(self.git_home),
            "USERPROFILE": str(self.git_home),
        }
        self.git_resolver = mock.patch.object(
            updater, "_trusted_git_candidates", return_value=(Path(self.git),)
        )
        self.git_resolver.start()
        self.addCleanup(self.git_resolver.stop)
        self._git("init", "-q", "--template=%s" % self.empty_template)
        self._git("config", "user.name", "Transaction Test")
        self._git("config", "user.email", "transaction@example.invalid")
        self._git("config", "core.autocrlf", "false")
        self._git("config", "core.symlinks", "false" if os.name == "nt" else "true")
        self._git("remote", "add", "github", dict(managed_install.OFFICIAL_REMOTE_URLS)["github"])
        self._git("remote", "add", "gitee", dict(managed_install.OFFICIAL_REMOTE_URLS)["gitee"])
        (self.root / "VERSION").write_text("0.2.2\n", encoding="utf-8")
        (self.root / "app.txt").write_text("old\n", encoding="utf-8")
        self._git("add", "VERSION", "app.txt")
        self._git("commit", "-qm", "old")
        self.old_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.2")
        (self.root / "VERSION").write_text("0.2.3\n", encoding="utf-8")
        (self.root / "app.txt").write_text("new\n", encoding="utf-8")
        self._git("add", "VERSION", "app.txt")
        self._git("commit", "-qm", "new")
        self.new_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.3")
        self._git("reset", "--hard", "-q", self.old_commit)

        self.managed_home = mock.patch.object(
            managed_install.config, "DEFAULT_DEEPPATTERN_HOME", self.home
        )
        self.managed_home.start()
        self.addCleanup(self.managed_home.stop)
        managed_install.write_managed_identity(self.root)
        self._write_state()
        update_transaction._write_protocol_ready(self.root)

        self.manifest = release_contract.ReleaseManifest(
            schema=1,
            repository_id=managed_install.REPOSITORY_ID,
            channel="stable",
            release_sequence=23,
            version="0.2.3",
            tag="v0.2.3",
            commit=self.new_commit,
            min_python="3.12",
            published_at="2026-07-22T00:00:00Z",
            key_id="test-key",
        )
        self.signature = release_contract.ReleaseSignature(
            schema=1,
            algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-key",
            signature="AA==",
        )
        self.verified = release_contract.VerifiedRelease(self.manifest, "test-key")

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.git, "-C", str(self.root), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=10,
            env=self.git_environment,
        )

    @property
    def state_path(self) -> Path:
        return self.root / updater.UPDATE_STATE_RELATIVE_PATH

    def _write_state(self, **changes) -> None:
        payload = {
            "schema": 1,
            "channel": "stable",
            "last_release_sequence": 22,
            "last_release_commit": self.old_commit,
            "last_manifest_sha256": "1" * 64,
            "last_version": "0.2.2",
        }
        payload.update(changes)
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        if os.name != "nt":
            self.state_path.parent.chmod(0o700)
            self.state_path.chmod(0o600)

    def _spawn_lease_holder(self):
        ready = self.root / "foreign-lease.ready"
        script = (
            "import pathlib,sys; from installer import update_coordination as c; "
            "root=pathlib.Path(sys.argv[1]); ready=pathlib.Path(sys.argv[2]); "
            "ctx=c.shim_session_lease(root,sys.argv[3],admission_timeout_seconds=20,heartbeat_seconds=None); "
            "lease=ctx.__enter__(); ready.write_text(str(lease.path)); "
            "sys.stdin.read(); ctx.__exit__(None,None,None)"
        )
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[2])
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = source_root + (os.pathsep + existing if existing else "")
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root), str(ready), self.old_commit],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup():
            if process.poll() is None and process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            if process.stderr is not None:
                process.stderr.close()

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("lease holder exited early: %s" % process.stderr.read())
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "foreign lease holder did not become ready")
        return process, Path(ready.read_text(encoding="utf-8"))

    def _apply(self):
        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(update_transaction, "_run_candidate_smoke", return_value=None),
        ):
            return update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {"test-key": mock.sentinel.key}
            )

    def _prepare_dirty_journal(self, transaction_id: str):
        with mock.patch.object(
            updater.release_contract, "authorize_release", return_value=self.verified
        ):
            inspection = updater.inspect_update(
                self.root, self.manifest, self.signature, {}
            )
        recovery, _manifest = update_transaction._prepare_recovery(
            self.root, transaction_id, inspection
        )
        journal = update_transaction._journal_for(
            transaction_id,
            updater._read_update_state(self.root),
            inspection,
            phase="prepared",
        )
        update_transaction._write_journal(self.root, journal)
        return recovery, journal

    def _write_empty_journal(self, phase: str = "candidate_applied") -> dict:
        transaction_id = "a" * 32
        recovery = self.root / update_transaction.RECOVERY_RELATIVE_PATH / transaction_id
        recovery.mkdir(parents=True, exist_ok=True)
        update_transaction._atomic_write_private_json(
            recovery / "manifest.json",
            {
                "schema": 1,
                "transaction_id": transaction_id,
                "created_at": "2026-07-22T00:00:00Z",
                "tracked": [],
                "collisions": [],
                "index_size": None,
                "index_sha256": None,
            },
        )
        journal = {
            "schema": 1,
            "transaction_id": transaction_id,
            "phase": phase,
            "previous_commit": self.old_commit,
            "previous_release_sequence": 22,
            "previous_manifest_sha256": "1" * 64,
            "previous_version": "0.2.2",
            "target_commit": self.new_commit,
            "target_version": "0.2.3",
            "release_sequence": 23,
            "manifest_sha256": release_contract.manifest_sha256(self.manifest),
            "recovery_relative_path": (
                update_transaction.RECOVERY_RELATIVE_PATH / transaction_id
            ).as_posix(),
        }
        update_transaction._write_journal(self.root, journal)
        return journal

    def test_busy_transaction_is_skipped_before_lock_local_authorization(self):
        with (
            mock.patch.object(
                update_transaction.update_coordination,
                "install_transaction",
                side_effect=update_coordination.InstallTransactionBusy("busy"),
            ),
            mock.patch.object(updater, "inspect_update") as inspect_update,
        ):
            result = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
        self.assertEqual(result.status, "skipped_locked")
        inspect_update.assert_not_called()

    def test_initial_state_is_written_only_for_the_signed_head_tag_and_version(self):
        self.state_path.unlink()
        (self.root / update_transaction.PROTOCOL_READY_RELATIVE_PATH).unlink()
        self._git("reset", "--hard", "-q", self.new_commit)
        with mock.patch.object(
            release_contract,
            "verify_release_signature",
            return_value=self.verified,
        ) as verify:
            state = update_transaction.initialize_release_state(
                self.root,
                self.manifest,
                self.signature,
                {"test-key": mock.sentinel.key},
                source="github",
            )

        verify.assert_called_once()
        self.assertEqual(state.last_release_commit, self.new_commit)
        self.assertEqual(state.last_release_sequence, 23)
        self.assertEqual(state.last_version, "0.2.3")
        self.assertEqual(state.source, "github")
        self.assertIsNone(state.running_commit)
        self.assertFalse(
            (self.root / update_transaction.PROTOCOL_READY_RELATIVE_PATH).exists(),
            "state initialization must not activate mutation before MCP wiring",
        )

    def test_initial_state_refuses_a_head_that_differs_from_the_signed_release(self):
        self.state_path.unlink()
        (self.root / update_transaction.PROTOCOL_READY_RELATIVE_PATH).unlink()
        with mock.patch.object(
            release_contract,
            "verify_release_signature",
            return_value=self.verified,
        ):
            with self.assertRaisesRegex(
                update_transaction.UpdateTransactionError, "HEAD"
            ):
                update_transaction.initialize_release_state(
                    self.root,
                    self.manifest,
                    self.signature,
                    {"test-key": mock.sentinel.key},
                    source="github",
                )
        self.assertFalse(self.state_path.exists())

    def test_authorization_receives_exact_inputs_before_git_mutation(self):
        trusted = {"test-key": mock.sentinel.key}
        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ) as authorize,
            mock.patch.object(update_transaction, "_run_candidate_smoke", return_value=None),
        ):
            update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, trusted
            )
        args, kwargs = authorize.call_args
        self.assertIs(args[0], self.manifest)
        self.assertIs(args[1], self.signature)
        self.assertIs(args[2], trusted)
        self.assertEqual(kwargs["last_commit"], self.old_commit)

    def test_signature_rejection_leaves_tree_and_transaction_artifacts_unchanged(self):
        before_state = self.state_path.read_bytes()
        with mock.patch.object(
            updater.release_contract,
            "authorize_release",
            side_effect=release_contract.ReleaseContractError("bad signature"),
        ):
            with self.assertRaises(release_contract.ReleaseContractError):
                update_transaction.apply_present_update(
                    self.root, self.manifest, self.signature, {}
                )
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)
        self.assertEqual(self.state_path.read_bytes(), before_state)
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        self.assertFalse((self.root / ".runtime" / "recovery").exists())

    def test_oversized_recovery_refuses_before_journal_or_git_mutation(self):
        (self.root / "app.txt").write_text("user edit\n", encoding="utf-8")
        before_state = self.state_path.read_bytes()
        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(update_transaction, "_MAX_RECOVERY_BYTES", 1),
            mock.patch.object(update_transaction, "_reset_to_commit") as reset,
        ):
            with self.assertRaisesRegex(
                update_transaction.UpdateTransactionError, "size limit"
            ):
                update_transaction.apply_present_update(
                    self.root, self.manifest, self.signature, {}
                )
        reset.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), before_state)
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        recovery_root = self.root / update_transaction.RECOVERY_RELATIVE_PATH
        self.assertFalse(recovery_root.exists() and any(recovery_root.iterdir()))

    def test_initial_journal_failure_removes_the_unreferenced_capture(self):
        (self.root / "app.txt").write_text("user edit\n", encoding="utf-8")
        with mock.patch.object(
            update_transaction,
            "_write_journal",
            side_effect=update_transaction.UpdateTransactionError("disk full"),
        ):
            with self.assertRaisesRegex(
                update_transaction.UpdateTransactionError, "disk full"
            ):
                self._apply()

        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        recovery_root = self.root / update_transaction.RECOVERY_RELATIVE_PATH
        self.assertFalse(recovery_root.exists() and any(recovery_root.iterdir()))

    def test_tracked_snapshot_captures_only_actual_drift(self):
        pristine = self.root / "pristine.bin"
        pristine.write_bytes(b"p" * 4096)
        self._git("add", "pristine.bin")
        self._git("commit", "-qm", "add pristine file")
        (self.root / "app.txt").write_text("user edit\n", encoding="utf-8")
        recovery = self.root / update_transaction.RECOVERY_RELATIVE_PATH / ("d" * 32)
        update_transaction._ensure_private_directory(recovery)

        tracked, _total, _index_size, _index_digest = (
            update_transaction._snapshot_tracked(self.root, recovery)
        )

        self.assertEqual([item["path"] for item in tracked], ["app.txt"])
        self.assertFalse((recovery / "tracked" / "pristine.bin").exists())

    def test_content_drift_is_captured_even_when_size_and_mtime_are_preserved(self):
        path = self.root / "app.txt"
        original = path.stat()
        path.write_bytes(b"bad\n")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

        result = self._apply()

        self.assertEqual(result.status, "candidate_ready")
        manifest_path = (
            self.root
            / update_transaction.RECOVERY_RELATIVE_PATH
            / result.transaction_id
            / "manifest.json"
        )
        recovery_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [item["path"] for item in recovery_manifest["tracked"]],
            ["app.txt"],
        )

    def test_combined_recovery_path_cap_is_enforced_before_collision_move(self):
        (self.root / "app.txt").write_text("user edit\n", encoding="utf-8")
        collision = self.root / "future.txt"
        collision.write_text("user data\n", encoding="utf-8")
        inspection = mock.Mock(tracked_dirty=True, collision_paths=("future.txt",))

        with mock.patch.object(update_transaction, "_MAX_RECOVERY_PATHS", 1):
            with self.assertRaisesRegex(
                update_transaction.UpdateTransactionError, "path-count"
            ):
                update_transaction._prepare_recovery(
                    self.root, "e" * 32, inspection
                )

        self.assertEqual(collision.read_text(encoding="utf-8"), "user data\n")

    def test_recovery_directory_flush_reaches_the_managed_root(self):
        inspection = mock.Mock(tracked_dirty=False, collision_paths=())
        transaction_id = "9" * 32
        with mock.patch.object(
            update_transaction, "_fsync_directory_chain"
        ) as flush_chain:
            recovery, _manifest = update_transaction._prepare_recovery(
                self.root, transaction_id, inspection
            )
        flush_chain.assert_called_once_with(recovery, self.root)

    def test_collision_bytes_are_persisted_before_the_namespace_move(self):
        source = self.root / "future.txt"
        source.write_bytes(b"collision-bytes\x00")
        recovery = self.root / update_transaction.RECOVERY_RELATIVE_PATH / ("8" * 32)
        update_transaction._ensure_private_directory(recovery)
        native_persist = update_transaction._persist_collision_tree
        native_move = windows_security.move_write_through
        events = []

        def persist(*args, **kwargs):
            events.append("persist")
            return native_persist(*args, **kwargs)

        def move(*args, **kwargs):
            events.append("move")
            return native_move(*args, **kwargs)

        with (
            mock.patch.object(
                update_transaction, "_persist_collision_tree", side_effect=persist
            ),
            mock.patch.object(
                update_transaction.windows_security,
                "move_write_through",
                side_effect=move,
            ),
        ):
            update_transaction._move_collision(
                self.root, recovery, "future.txt"
            )

        self.assertEqual(events, ["persist", "move"])
        self.assertEqual(
            (recovery / "collisions" / "future.txt").read_bytes(),
            b"collision-bytes\x00",
        )

    def test_collision_persistence_failure_suppresses_the_namespace_move(self):
        source = self.root / "future.txt"
        source.write_bytes(b"collision-bytes\x00")
        recovery = self.root / update_transaction.RECOVERY_RELATIVE_PATH / ("7" * 32)
        update_transaction._ensure_private_directory(recovery)
        with (
            mock.patch.object(
                update_transaction,
                "_fsync_stable_regular_file",
                side_effect=update_transaction.UpdateTransactionError("fsync failed"),
            ),
            mock.patch.object(
                update_transaction.windows_security, "move_write_through"
            ) as move,
        ):
            with self.assertRaisesRegex(
                update_transaction.UpdateTransactionError, "fsync failed"
            ):
                update_transaction._move_collision(
                    self.root, recovery, "future.txt"
                )
        move.assert_not_called()
        self.assertEqual(source.read_bytes(), b"collision-bytes\x00")

    def test_reset_requests_git_fsync_for_all_repository_metadata(self):
        arguments = update_transaction._mutation_arguments(
            self.root, self.old_commit, self.git
        )
        self.assertIn("core.fsync=all", arguments)
        self.assertIn("core.fsyncMethod=fsync", arguments)

    def test_unsafe_local_git_config_refuses_before_recovery_capture(self):
        native_inspect = updater.inspect_update

        def change_config_after_inspection(*args, **kwargs):
            result = native_inspect(*args, **kwargs)
            self._git("config", "filter.unsafe.smudge", "arbitrary-command")
            return result

        with (
            mock.patch.object(
                updater, "inspect_update", side_effect=change_config_after_inspection
            ),
            mock.patch.object(update_transaction, "_prepare_recovery") as prepare,
        ):
            with self.assertRaisesRegex(
                update_transaction.GitMutationError, "execution-capable"
            ):
                self._apply()

        prepare.assert_not_called()
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        self.assertFalse((self.root / update_transaction.RECOVERY_RELATIVE_PATH).exists())

    def test_present_signed_target_is_applied_and_persisted_as_candidate_ready(self):
        native_write_journal = update_transaction._write_journal
        with mock.patch.object(
            update_transaction, "_write_journal", wraps=native_write_journal
        ) as write_journal:
            result = self._apply()

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.new_commit)
        self.assertEqual((self.root / "app.txt").read_text(encoding="utf-8"), "new\n")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_release_sequence"], 23)
        self.assertEqual(state["last_release_commit"], self.new_commit)
        self.assertEqual(state["last_result"], "candidate_ready")
        self.assertIsNone(state["running_commit"])
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        self.assertEqual(
            [call.args[1]["phase"] for call in write_journal.call_args_list],
            ["prepared", "reset_started", "candidate_applied", "smoke_started"],
        )

    def test_same_target_retry_preserves_unserved_candidate_ready(self):
        first = self._apply()
        first_state = json.loads(self.state_path.read_text(encoding="utf-8"))

        second = self._apply()

        second_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(second.status, "candidate_ready")
        self.assertEqual(second.transaction_id, first.transaction_id)
        self.assertEqual(second_state["last_result"], "candidate_ready")
        self.assertEqual(second_state["transaction_id"], first_state["transaction_id"])
        self.assertIsNone(second_state["running_commit"])

    def test_missing_protocol_ready_marker_refuses_before_git_mutation(self):
        (self.root / update_transaction.PROTOCOL_READY_RELATIVE_PATH).unlink()
        before = self._git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(update_transaction.UpdateTransactionError, "launcher protocol"):
            self._apply()
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), before)

    def test_missing_protocol_marker_does_not_block_existing_journal_recovery(self):
        self._write_empty_journal()
        self._git("reset", "--hard", "-q", self.new_commit)
        (self.root / update_transaction.PROTOCOL_READY_RELATIVE_PATH).unlink()

        result = self._apply()

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.new_commit)
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

    def test_protocol_marker_can_publish_inside_existing_migration_lock(self):
        marker = self.root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
        marker.unlink()
        with update_coordination.install_transaction(self.root) as transaction:
            update_transaction._write_protocol_ready_locked(
                self.root, transaction
            )
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8")),
            {"schema": 1, "launcher_protocol": 1},
        )

    @unittest.skipUnless(os.name == "nt", "Win32 ACL wiring test")
    def test_broad_protected_acl_refuses_before_git_mutation(self):
        native_validate = windows_security.validate_private_mutation_acl

        def reject_state(path):
            if Path(path) == self.state_path:
                raise windows_security.WindowsSecurityError("broad state ACL")
            return native_validate(path)

        with (
            mock.patch.object(
                windows_security,
                "validate_private_mutation_acl",
                side_effect=reject_state,
            ),
            mock.patch.object(update_transaction, "_reset_to_commit") as reset,
        ):
            with self.assertRaisesRegex(update_transaction.UpdateTransactionError, "ACL"):
                self._apply()
        reset.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Win32 candidate ACL wiring test")
    def test_broad_candidate_file_acl_rolls_back_before_smoke(self):
        native_validate = windows_security.validate_private_mutation_acl

        def reject_candidate(path):
            if Path(path) == self.root / "app.txt":
                raise windows_security.WindowsSecurityError("broad candidate ACL")
            return native_validate(path)

        with (
            mock.patch.object(
                windows_security,
                "validate_private_mutation_acl",
                side_effect=reject_candidate,
            ),
            mock.patch.object(update_transaction, "_run_candidate_smoke") as smoke,
        ):
            result = self._apply()
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)
        smoke.assert_not_called()

    def test_live_shim_defers_without_reset_or_lkg_advance(self):
        before = self.state_path.read_bytes()
        _holder, _lease_path = self._spawn_lease_holder()
        result = self._apply()
        self.assertEqual(result.status, "deferred_active_session")
        self.assertEqual(result.previous_commit, self.old_commit)
        self.assertIsNone(result.target_commit)
        self.assertIsNone(result.transaction_id)
        self.assertIsNone(result.recovery_path)
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_release_commit"], self.old_commit)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_current_signed_release_with_live_shim_reports_up_to_date_to_doctor(self):
        current_manifest = replace(
            self.manifest,
            release_sequence=22,
            version="0.2.2",
            tag="v0.2.2",
            commit=self.old_commit,
            key_id="test-release-key",
        )
        self._write_state(
            last_manifest_sha256=release_contract.manifest_sha256(current_manifest),
            source="github",
            last_attempt_at="2026-09-19T00:00:00Z",
            last_result="up_to_date",
            previous_commit=self.old_commit,
            target_commit=self.old_commit,
            running_commit=self.old_commit,
            running_version="0.2.2",
            error_code=None,
            transaction_id=None,
        )
        signature = _sign(current_manifest)
        acquired = release_acquisition.AcquiredRelease(
            release_acquisition.GITHUB_SOURCE, current_manifest, signature,
        )
        _holder, _lease_path = self._spawn_lease_holder()
        keys = {
            "test-release-key": release_contract.RsaPublicKey(
                key_id="test-release-key", modulus=_TEST_N, exponent=_TEST_E,
            )
        }
        with (
            mock.patch.object(release_acquisition, "discover_release", return_value=acquired),
            mock.patch.object(release_acquisition, "fetch_release_objects") as fetch,
            mock.patch.object(update_staging, "stage_release") as stage,
            mock.patch.object(doctor.config, "managed_component_root", return_value=self.root),
            mock.patch.object(release_acquisition, "load_trusted_release_keys", return_value=keys),
            mock.patch.object(launcher, "load_trusted_release_keys", return_value=keys),
            mock.patch.object(launcher, "_harden_config_with_diagnostics"),
            mock.patch.object(launcher, "_serve_shim", return_value=0),
        ):
            self.assertTrue(update_coordination.live_shim_sessions(self.root))
            self.assertEqual(launcher.launch(self.root), 0)
            attempt = update_staging.read_attempt(self.root)
            diagnosis = doctor.check_managed_update()
            invalid = release_acquisition.AcquiredRelease(
                release_acquisition.GITHUB_SOURCE,
                current_manifest,
                replace(signature, signature="AA=="),
            )
            with mock.patch.object(release_acquisition, "discover_release", return_value=invalid):
                with self.assertRaises(release_contract.ReleaseContractError):
                    launcher._attempt_update(self.root, keys, deadline=time.monotonic() + 60)
        self.assertEqual(diagnosis.status, "PASS")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt[1], "up_to_date")
        fetch.assert_not_called()
        stage.assert_not_called()

    def test_current_release_does_not_ignore_a_mismatched_live_shim(self):
        self.manifest = replace(
            self.manifest,
            release_sequence=22,
            version="0.2.2",
            tag="v0.2.2",
            commit=self.old_commit,
        )
        self.verified = release_contract.VerifiedRelease(self.manifest, "test-key")
        self._write_state(
            last_manifest_sha256=release_contract.manifest_sha256(self.manifest),
            source="github",
            last_attempt_at="2026-09-19T00:00:00Z",
            last_result="up_to_date",
            previous_commit=self.old_commit,
            target_commit=self.old_commit,
            running_commit=self.old_commit,
            running_version="0.2.2",
            error_code=None,
            transaction_id=None,
        )
        other_session = update_coordination.LiveShimSession(
            self.root / "other-lease", None, None, self.new_commit, None,
        )
        with mock.patch.object(
            update_coordination, "live_shim_sessions", return_value=(other_session,)
        ):
            result = self._apply()
        self.assertEqual(result.status, "deferred_active_session")
        self.assertEqual(result.blockers, (other_session,))

    def test_current_release_with_pending_state_still_defers_live_shim(self):
        self.manifest = replace(
            self.manifest,
            release_sequence=22,
            version="0.2.2",
            tag="v0.2.2",
            commit=self.old_commit,
        )
        self.verified = release_contract.VerifiedRelease(self.manifest, "test-key")
        _holder, _lease_path = self._spawn_lease_holder()
        base_state = {
            "last_manifest_sha256": release_contract.manifest_sha256(self.manifest),
            "source": "github",
            "last_attempt_at": "2026-09-19T00:00:00Z",
            "last_result": "up_to_date",
            "previous_commit": self.old_commit,
            "target_commit": self.old_commit,
            "running_commit": self.old_commit,
            "running_version": "0.2.2",
            "error_code": None,
            "transaction_id": None,
        }
        for pending in (
            {"transaction_id": "a" * 32},
            {"error_code": "interrupted"},
            {"last_result": "candidate_ready", "transaction_id": "a" * 32},
        ):
            with self.subTest(pending=pending):
                self._write_state(**{**base_state, **pending})
                result = self._apply()
                self.assertEqual(result.status, "deferred_active_session")

    def test_state_change_after_inspection_does_not_bypass_live_shim(self):
        self.manifest = replace(
            self.manifest,
            release_sequence=22,
            version="0.2.2",
            tag="v0.2.2",
            commit=self.old_commit,
        )
        self.verified = release_contract.VerifiedRelease(self.manifest, "test-key")
        self._write_state(
            last_manifest_sha256=release_contract.manifest_sha256(self.manifest),
            source="github",
            last_attempt_at="2026-09-19T00:00:00Z",
            last_result="up_to_date",
            previous_commit=self.old_commit,
            target_commit=self.old_commit,
            running_commit=self.old_commit,
            running_version="0.2.2",
            error_code=None,
            transaction_id=None,
        )
        _holder, _lease_path = self._spawn_lease_holder()
        inspect = updater.inspect_update

        def interrupted_inspection(*args, **kwargs):
            result = inspect(*args, **kwargs)
            self._write_state(
                last_manifest_sha256=release_contract.manifest_sha256(self.manifest),
                source="github",
                last_attempt_at="2026-09-19T00:00:00Z",
                last_result="candidate_ready",
                previous_commit=self.old_commit,
                target_commit=self.old_commit,
                running_commit=self.new_commit,
                running_version="0.2.3",
                error_code=None,
                transaction_id="a" * 32,
            )
            return result

        with mock.patch.object(updater, "inspect_update", side_effect=interrupted_inspection):
            result = self._apply()
        self.assertEqual(result.status, "deferred_active_session")

    def test_staged_release_installs_offline_after_live_session_exits(self):
        acquired = release_acquisition.AcquiredRelease(
            release_acquisition.GITHUB_SOURCE, self.manifest, self.signature,
        )
        keys = {"test-key": mock.sentinel.key}
        holder, _lease = self._spawn_lease_holder()
        with (
            mock.patch.object(release_contract, "authorize_release", return_value=self.verified),
            mock.patch.object(release_acquisition, "discover_release", return_value=acquired),
            mock.patch.object(release_acquisition, "fetch_release_objects") as fetch,
        ):
            self.assertEqual(
                launcher._attempt_update(self.root, keys, deadline=time.monotonic() + 60).status,
                "deferred_active_session",
            )
        fetch.assert_called_once()
        self.assertTrue((self.root / update_staging.STAGED_RELEASE_RELATIVE_PATH).exists())
        holder.stdin.close()
        holder.wait(timeout=10)
        with (
            mock.patch.object(launcher, "load_trusted_release_keys", return_value=keys),
            mock.patch.object(release_contract, "authorize_release", return_value=self.verified),
            mock.patch.object(update_transaction, "_run_candidate_smoke"),
            mock.patch.object(release_acquisition, "discover_release", side_effect=AssertionError("network used")) as network,
            mock.patch.object(launcher, "_handoff_to_fresh_launcher", return_value=0) as handoff,
        ):
            self.assertEqual(launcher.launch(self.root), 0)
        network.assert_not_called()
        handoff.assert_called_once()
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.new_commit)
        self.assertEqual(updater._read_update_state(self.root).last_release_commit, self.new_commit)
        self.assertFalse((self.root / update_staging.STAGED_RELEASE_RELATIVE_PATH).exists())

    def test_existing_journal_plus_live_shim_defers_then_recovers(self):
        holder, _lease = self._spawn_lease_holder()
        self._write_empty_journal(phase="prepared")
        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            result = self._apply()
        self.assertEqual(result.status, "deferred_active_session")
        self.assertEqual(result.error_code, "journal_with_live_session")
        reset.assert_not_called()
        journal = json.loads(
            (self.root / update_transaction.JOURNAL_RELATIVE_PATH).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["phase"], "prepared")
        holder.stdin.close()
        holder.wait(timeout=10)

        recovered = self._apply()
        self.assertEqual(recovered.status, "rolled_back")
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

    def test_tracked_drift_is_recoverable_before_reset(self):
        (self.root / "app.txt").write_bytes(b"local-binary\x00change")
        native_reset = update_transaction._reset_to_commit

        def assert_recovery_first(root, commit, **kwargs):
            recoveries = tuple((self.root / ".runtime" / "recovery").iterdir())
            self.assertEqual(len(recoveries), 1)
            manifest = json.loads((recoveries[0] / "manifest.json").read_text(encoding="utf-8"))
            entry = next(item for item in manifest["tracked"] if item["path"] == "app.txt")
            self.assertEqual(
                (recoveries[0] / entry["recovery_path"]).read_bytes(),
                b"local-binary\x00change",
            )
            return native_reset(root, commit, **kwargs)

        with mock.patch.object(
            update_transaction, "_reset_to_commit", side_effect=assert_recovery_first
        ):
            result = self._apply()
        recovery = Path(result.recovery_path)
        manifest = json.loads((recovery / "manifest.json").read_text(encoding="utf-8"))
        entry = next(item for item in manifest["tracked"] if item["path"] == "app.txt")
        recovered = recovery / entry["recovery_path"]
        self.assertEqual(recovered.read_bytes(), b"local-binary\x00change")
        self.assertEqual((self.root / "app.txt").read_text(encoding="utf-8"), "new\n")

    def test_recovery_manifest_has_a_separate_bounded_metadata_budget(self):
        journal = self._write_empty_journal(phase="prepared")
        recovery = self.root / journal["recovery_relative_path"]
        tracked = [
            {
                "path": "tree/%04d-%s" % (index, "x" * 80),
                "mode": "100644",
                "kind": "missing",
            }
            for index in range(2000)
        ]
        payload = {
            "schema": 1,
            "transaction_id": journal["transaction_id"],
            "created_at": "2026-07-22T00:00:00Z",
            "tracked": tracked,
            "collisions": [],
            "index_size": 0,
            "index_sha256": hashlib.sha256(b"").hexdigest(),
        }
        update_transaction._atomic_write_private_json(
            recovery / "manifest.json",
            payload,
            max_bytes=update_transaction._MAX_RECOVERY_MANIFEST_BYTES,
        )
        self.assertGreater(
            (recovery / "manifest.json").stat().st_size,
            update_transaction._MAX_CONTROL_BYTES,
        )
        _path, loaded = update_transaction._read_recovery_manifest(
            self.root, journal
        )
        self.assertEqual(len(loaded["tracked"]), 2000)

    def test_edit_after_recovery_capture_blocks_reset_and_preserves_new_bytes(self):
        native_inspect = updater.inspect_update
        calls = 0

        def edit_before_second_inspection(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.root / "app.txt").write_text(
                    "edit after recovery capture\n", encoding="utf-8"
                )
            return native_inspect(*args, **kwargs)

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(
                updater, "inspect_update", side_effect=edit_before_second_inspection
            ),
            mock.patch.object(update_transaction, "_reset_to_commit") as reset,
        ):
            result = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
        self.assertEqual(result.status, "repair_required")
        self.assertEqual(result.error_code, "recovery_state_changed")
        reset.assert_not_called()
        self.assertEqual(
            (self.root / "app.txt").read_text(encoding="utf-8"),
            "edit after recovery capture\n",
        )

    def test_second_inspection_base_error_routes_through_rollback(self):
        native_inspect = updater.inspect_update
        calls = 0

        def fail_second_inspection(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise updater.UpdateInspectionError("Git timed out")
            return native_inspect(*args, **kwargs)

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(
                updater, "inspect_update", side_effect=fail_second_inspection
            ),
        ):
            result = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)

    def test_collision_is_moved_only_after_durable_recovery(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        future = self.root / "future.txt"
        future.write_text("target\n", encoding="utf-8")
        (self.root / "VERSION").write_text("0.2.4\n", encoding="utf-8")
        self._git("add", "VERSION", "future.txt")
        self._git("commit", "-qm", "collision target")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.4")
        self._git("reset", "--hard", "-q", self.old_commit)
        future.write_bytes(b"keep-me\x00")
        candidate = replace(
            self.manifest,
            version="0.2.4",
            tag="v0.2.4",
            commit=target,
            release_sequence=24,
        )
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        native_move = update_transaction._move_collision

        def assert_collision_recovery_first(root, recovery, relative):
            manifest = json.loads((recovery / "manifest.json").read_text(encoding="utf-8"))
            entry = next(item for item in manifest["collisions"] if item["path"] == relative)
            self.assertEqual(entry["sha256"], update_transaction._sha256_path(root / relative))
            return native_move(root, recovery, relative)

        with (
            mock.patch.object(updater.release_contract, "authorize_release", return_value=verified),
            mock.patch.object(update_transaction, "_run_candidate_smoke", return_value=None),
            mock.patch.object(
                update_transaction, "_move_collision", side_effect=assert_collision_recovery_first
            ),
        ):
            result = update_transaction.apply_present_update(
                self.root, candidate, self.signature, {}
            )
        recovery = Path(result.recovery_path)
        collision = recovery / "collisions" / "future.txt"
        self.assertEqual(collision.read_bytes(), b"keep-me\x00")
        self.assertEqual(future.read_text(encoding="utf-8"), "target\n")

    def test_directory_collision_is_moved_without_clobbering_user_tree(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        target_directory = self.root / "future-dir"
        target_directory.mkdir()
        (target_directory / "target.txt").write_text("target\n", encoding="utf-8")
        (self.root / "VERSION").write_text("0.2.4\n", encoding="utf-8")
        self._git("add", "VERSION", "future-dir/target.txt")
        self._git("commit", "-qm", "directory collision target")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.4")
        self._git("reset", "--hard", "-q", self.old_commit)
        target_directory.mkdir()
        (target_directory / "user.bin").write_bytes(b"user-tree\x00")
        candidate = replace(
            self.manifest,
            version="0.2.4",
            tag="v0.2.4",
            commit=target,
            release_sequence=24,
        )
        verified = release_contract.VerifiedRelease(candidate, "test-key")

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=verified
            ),
            mock.patch.object(update_transaction, "_run_candidate_smoke", return_value=None),
        ):
            result = update_transaction.apply_present_update(
                self.root, candidate, self.signature, {}
            )

        backup = Path(result.recovery_path) / "collisions" / "future-dir"
        self.assertEqual((backup / "user.bin").read_bytes(), b"user-tree\x00")
        self.assertEqual(
            (target_directory / "target.txt").read_text(encoding="utf-8"),
            "target\n",
        )

    def test_failure_immediately_after_collision_move_restores_user_tree(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        future = self.root / "future.txt"
        future.write_text("target\n", encoding="utf-8")
        (self.root / "VERSION").write_text("0.2.4\n", encoding="utf-8")
        self._git("add", "VERSION", "future.txt")
        self._git("commit", "-qm", "collision crash target")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.4")
        self._git("reset", "--hard", "-q", self.old_commit)
        future.write_bytes(b"survive-crash\x00")
        candidate = replace(
            self.manifest,
            version="0.2.4",
            tag="v0.2.4",
            commit=target,
            release_sequence=24,
        )
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        native_move = update_transaction._move_collision

        def move_then_interrupt(root, recovery, relative):
            native_move(root, recovery, relative)
            raise KeyboardInterrupt("simulated process interruption")

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=verified
            ),
            mock.patch.object(
                update_transaction, "_move_collision", side_effect=move_then_interrupt
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                update_transaction.apply_present_update(
                    self.root, candidate, self.signature, {}
                )
        self.assertFalse(future.exists())

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=verified
            ),
            mock.patch.object(update_transaction, "_run_candidate_smoke", return_value=None),
        ):
            recovered = update_transaction.apply_present_update(
                self.root, candidate, self.signature, {}
            )
        self.assertEqual(recovered.status, "rolled_back")
        self.assertEqual(future.read_bytes(), b"survive-crash\x00")

    def test_smoke_failure_restores_colliding_user_data(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        future = self.root / "future.txt"
        future.write_text("target\n", encoding="utf-8")
        (self.root / "VERSION").write_text("0.2.4\n", encoding="utf-8")
        self._git("add", "VERSION", "future.txt")
        self._git("commit", "-qm", "collision rollback target")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.4")
        self._git("reset", "--hard", "-q", self.old_commit)
        future.write_bytes(b"user-collision\x00")
        candidate = replace(
            self.manifest,
            version="0.2.4",
            tag="v0.2.4",
            commit=target,
            release_sequence=24,
        )
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=verified
            ),
            mock.patch.object(
                update_transaction,
                "_run_candidate_smoke",
                side_effect=update_transaction.CandidateSmokeError("failed"),
            ),
        ):
            result = update_transaction.apply_present_update(
                self.root, candidate, self.signature, {}
            )
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)
        self.assertEqual(future.read_bytes(), b"user-collision\x00")
        self.assertIn("?? future.txt", self._git("status", "--porcelain").stdout)

    def test_late_unsafe_config_restores_collision_without_a_second_reset(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        future = self.root / "future.txt"
        future.write_text("target\n", encoding="utf-8")
        (self.root / "VERSION").write_text("0.2.4\n", encoding="utf-8")
        self._git("add", "VERSION", "future.txt")
        self._git("commit", "-qm", "late unsafe config target")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.4")
        self._git("reset", "--hard", "-q", self.old_commit)
        future.write_bytes(b"late-config-collision\x00")
        candidate = replace(
            self.manifest,
            version="0.2.4",
            tag="v0.2.4",
            commit=target,
            release_sequence=24,
        )
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        native_validate = update_transaction._validated_mutation_reader
        validations = 0

        def introduce_unsafe_config(root, *, anchor):
            nonlocal validations
            validations += 1
            if validations == 2:
                self._git("config", "filter.unsafe.smudge", "arbitrary-command")
            return native_validate(root, anchor=anchor)

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=verified
            ),
            mock.patch.object(
                update_transaction,
                "_validated_mutation_reader",
                side_effect=introduce_unsafe_config,
            ),
            mock.patch.object(update_transaction, "_run_candidate_smoke") as smoke,
        ):
            result = update_transaction.apply_present_update(
                self.root, candidate, self.signature, {}
            )

        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)
        self.assertEqual(future.read_bytes(), b"late-config-collision\x00")
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        smoke.assert_not_called()

    def test_smoke_failure_rolls_back_without_advancing_lkg(self):
        (self.root / "app.txt").write_text("user edit\n", encoding="utf-8")
        native_write_journal = update_transaction._write_journal
        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(
                update_transaction,
                "_run_candidate_smoke",
                side_effect=update_transaction.CandidateSmokeError("failed"),
            ),
            mock.patch.object(
                update_transaction, "_write_journal", wraps=native_write_journal
            ) as write_journal,
        ):
            result = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)
        self.assertEqual((self.root / "app.txt").read_text(encoding="utf-8"), "user edit\n")
        self.assertIn(" M app.txt", self._git("status", "--porcelain").stdout)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_release_commit"], self.old_commit)
        self.assertEqual(state["last_result"], "rolled_back")
        self.assertIsNone(state["running_commit"])
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        self.assertEqual(
            [call.args[1]["phase"] for call in write_journal.call_args_list],
            [
                "prepared",
                "reset_started",
                "candidate_applied",
                "smoke_started",
                "rollback_started",
            ],
        )

    def test_staged_tracked_deletion_survives_failed_candidate_rollback(self):
        (self.root / "app.txt").unlink()
        self._git("add", "app.txt")
        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(
                update_transaction,
                "_run_candidate_smoke",
                side_effect=update_transaction.CandidateSmokeError("failed"),
            ),
        ):
            result = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )

        self.assertEqual(result.status, "rolled_back")
        self.assertFalse((self.root / "app.txt").exists())
        self.assertIn("D  app.txt", self._git("status", "--porcelain").stdout)

    def test_candidate_persistence_failure_rolls_back_before_state_commit(self):
        native_persist = update_transaction._persist_checkout
        persistence_calls = 0

        def fail_candidate_persistence(root):
            nonlocal persistence_calls
            persistence_calls += 1
            if persistence_calls == 1:
                raise update_transaction.UpdateTransactionError("persistence failed")
            return native_persist(root)

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(update_transaction, "_run_candidate_smoke", return_value=None),
            mock.patch.object(
                update_transaction,
                "_persist_checkout",
                side_effect=fail_candidate_persistence,
            ),
        ):
            result = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )

        self.assertEqual(result.status, "rolled_back")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_release_commit"], self.old_commit)

    def test_rollback_sharing_violation_enters_retry_pending_and_preserves_first_error(self):
        native_reset = update_transaction._reset_to_commit

        def fail_rollback(root, commit, **kwargs):
            if commit == self.old_commit:
                raise update_transaction.GitMutationError("sharing violation")
            return native_reset(root, commit, **kwargs)

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(
                update_transaction,
                "_run_candidate_smoke",
                side_effect=update_transaction.CandidateSmokeError("failed"),
            ),
            mock.patch.object(update_transaction, "_reset_to_commit", side_effect=fail_rollback),
        ):
            result = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
        self.assertEqual(result.status, "retry_pending")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_release_commit"], self.old_commit)
        self.assertEqual(state["last_result"], "retry_pending")
        self.assertEqual(state["error_code"], "CandidateSmokeError")
        self.assertTrue((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        journal = json.loads(
            (self.root / update_transaction.JOURNAL_RELATIVE_PATH).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["phase"], "retry_pending")
        self.assertEqual(journal["first_error_code"], "CandidateSmokeError")
        self.assertEqual(journal["failure_phase"], "smoke_started")
        self.assertEqual(journal["retry_action"], "rollback")
        self.assertEqual(journal["retry_count"], 1)

    def test_legacy_0217_repair_state_forward_completes_clean_signed_target(self):
        journal = self._write_empty_journal(phase="repair_required")
        self._git("reset", "--hard", "-q", self.new_commit)
        self._write_state(
            source="github",
            last_attempt_at="2026-07-28T00:00:00Z",
            last_result="repair_required",
            previous_commit=self.old_commit,
            target_commit=self.new_commit,
            running_commit=None,
            running_version=None,
            error_code="repair_required",
            transaction_id=journal["transaction_id"],
        )

        with (
            mock.patch.object(update_transaction, "_run_candidate_smoke") as smoke,
            mock.patch.object(update_transaction, "_reset_to_commit") as reset,
        ):
            result = update_transaction.finalize_present_journal(self.root)

        self.assertEqual(result.status, "candidate_ready")
        reset.assert_not_called()
        smoke.assert_called_once()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_release_commit"], self.new_commit)
        self.assertEqual(state["last_version"], "0.2.3")
        self.assertEqual(state["last_result"], "candidate_ready")
        self.assertIsNone(state["error_code"])
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

    def test_retry_pending_rollback_completes_then_update_reapplies(self):
        native_reset = update_transaction._reset_to_commit

        def fail_rollback(root, commit, **kwargs):
            if commit == self.old_commit:
                raise update_transaction.GitMutationError("sharing violation")
            return native_reset(root, commit, **kwargs)

        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(
                update_transaction,
                "_run_candidate_smoke",
                side_effect=update_transaction.CandidateSmokeError("failed"),
            ),
            mock.patch.object(update_transaction, "_reset_to_commit", side_effect=fail_rollback),
        ):
            first = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
        self.assertEqual(first.status, "retry_pending")

        with mock.patch.object(update_transaction, "_run_candidate_smoke") as smoke:
            recovered = update_transaction.finalize_present_journal(self.root)

        self.assertEqual(recovered.status, "rolled_back")
        smoke.assert_not_called()
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

        reapplied = self._apply()
        self.assertEqual(reapplied.status, "candidate_ready")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.new_commit)

    def test_repeated_retry_preserves_first_error_and_increments_count(self):
        journal = self._write_empty_journal(phase="repair_required")
        self._git("reset", "--hard", "-q", self.new_commit)
        self._write_state(
            source="github",
            last_attempt_at="2026-07-28T00:00:00Z",
            last_result="repair_required",
            previous_commit=self.old_commit,
            target_commit=self.new_commit,
            running_commit=None,
            running_version=None,
            error_code="GitMutationError",
            transaction_id=journal["transaction_id"],
        )

        with mock.patch.object(
            update_transaction,
            "_run_candidate_smoke",
            side_effect=update_transaction.CandidateSmokeError("still busy"),
        ), mock.patch.object(
            updater.release_contract, "authorize_release", return_value=self.verified
        ):
            first = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
            second = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )

        self.assertEqual(first.status, "retry_pending")
        self.assertEqual(second.status, "retry_pending")
        self.assertEqual(second.error_code, "GitMutationError")
        persisted = json.loads(
            (self.root / update_transaction.JOURNAL_RELATIVE_PATH).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["first_error_code"], "GitMutationError")
        self.assertEqual(persisted["retry_action"], "forward")
        self.assertEqual(persisted["retry_count"], 2)

    def test_rollback_started_journal_retries_and_completes_rollback(self):
        self._write_empty_journal(phase="rollback_started")
        result = self._apply()
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(result.error_code, "crash_reentry")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old_commit)

    def test_unexpected_head_on_reentry_requires_repair_without_reset(self):
        self._write_empty_journal(phase="reset_started")
        (self.root / "app.txt").write_text("unrelated commit\n", encoding="utf-8")
        self._git("add", "app.txt")
        self._git("commit", "-qm", "unexpected concurrent commit")
        unexpected = self._git("rev-parse", "HEAD").stdout.strip()

        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            result = self._apply()

        self.assertEqual(result.status, "repair_required")
        reset.assert_not_called()
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), unexpected)

    def test_dangerous_phases_refuse_changed_state_without_reset(self):
        for phase in ("rollback_started", "repair_required"):
            with self.subTest(phase=phase):
                self._write_empty_journal(phase=phase)
                (self.root / "app.txt").write_text(
                    "changed after %s\n" % phase, encoding="utf-8"
                )
                with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
                    result = self._apply()
                self.assertEqual(result.status, "repair_required")
                self.assertEqual(result.error_code, "reentry_state_changed")
                reset.assert_not_called()
                (self.root / update_transaction.JOURNAL_RELATIVE_PATH).unlink()
                self._git("reset", "--hard", "-q", self.old_commit)

    def test_recovery_refuses_execution_capable_git_config(self):
        self._write_empty_journal()
        self._git("reset", "--hard", "-q", self.new_commit)
        self._git("config", "filter.unsafe.smudge", "arbitrary-command")

        result = self._apply()

        self.assertEqual(result.status, "repair_required")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.new_commit)
        self.assertEqual(result.error_code, "reentry_state_changed")

    def test_post_crash_tracked_edit_blocks_automatic_rollback(self):
        self._write_empty_journal()
        self._git("reset", "--hard", "-q", self.new_commit)
        (self.root / "app.txt").write_text("post-crash user edit\n", encoding="utf-8")
        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            result = self._apply()
        self.assertEqual(result.status, "repair_required")
        self.assertEqual(result.error_code, "reentry_state_changed")
        reset.assert_not_called()
        self.assertEqual(
            (self.root / "app.txt").read_text(encoding="utf-8"),
            "post-crash user edit\n",
        )

    def test_post_capture_edit_to_previously_clean_tracked_file_blocks_rollback(self):
        (self.root / "app.txt").write_text("captured edit\n", encoding="utf-8")
        with mock.patch.object(
            updater.release_contract, "authorize_release", return_value=self.verified
        ):
            inspection = updater.inspect_update(
                self.root, self.manifest, self.signature, {}
            )
        transaction_id = "f" * 32
        update_transaction._prepare_recovery(
            self.root, transaction_id, inspection
        )
        update_transaction._write_journal(
            self.root,
            update_transaction._journal_for(
                transaction_id,
                updater._read_update_state(self.root),
                inspection,
                phase="prepared",
            ),
        )
        (self.root / "VERSION").write_text("post-capture edit\n", encoding="utf-8")

        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            result = self._apply()

        self.assertEqual(result.status, "repair_required")
        reset.assert_not_called()
        self.assertEqual(
            (self.root / "VERSION").read_text(encoding="utf-8"),
            "post-capture edit\n",
        )

    def test_corrupt_tracked_recovery_file_blocks_reset(self):
        (self.root / "app.txt").write_text("captured edit\n", encoding="utf-8")
        recovery, _journal = self._prepare_dirty_journal("6" * 32)
        backup = recovery / "tracked" / "app.txt"
        backup.write_text("corrupt backup\n", encoding="utf-8")

        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            result = self._apply()

        self.assertEqual(result.status, "repair_required")
        reset.assert_not_called()
        self.assertEqual(
            (self.root / "app.txt").read_text(encoding="utf-8"),
            "captured edit\n",
        )

    def test_corrupt_recovery_index_blocks_reset(self):
        (self.root / "app.txt").write_text("captured edit\n", encoding="utf-8")
        recovery, _journal = self._prepare_dirty_journal("5" * 32)
        (recovery / "git-index.bin").write_bytes(b"corrupt index")

        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            result = self._apply()

        self.assertEqual(result.status, "repair_required")
        reset.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX mode-bit behavior")
    def test_post_capture_permission_change_blocks_reset(self):
        path = self.root / "app.txt"
        path.write_text("captured edit\n", encoding="utf-8")
        path.chmod(0o600)
        self._prepare_dirty_journal("4" * 32)
        path.chmod(0o640)

        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            result = self._apply()

        self.assertEqual(result.status, "repair_required")
        reset.assert_not_called()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_nonterminal_target_journal_reentry_forward_completes(self):
        transaction_id = "a" * 32
        recovery = self.root / update_transaction.RECOVERY_RELATIVE_PATH / transaction_id
        recovery.mkdir(parents=True)
        update_transaction._atomic_write_private_json(
            recovery / "manifest.json",
            {
                "schema": 1,
                "transaction_id": transaction_id,
                "created_at": "2026-07-22T00:00:00Z",
                "tracked": [],
                "collisions": [],
                "index_size": None,
                "index_sha256": None,
            },
        )
        journal = self.root / update_transaction.JOURNAL_RELATIVE_PATH
        journal.write_text(json.dumps({
            "schema": 1,
            "transaction_id": transaction_id,
            "phase": "candidate_applied",
            "previous_commit": self.old_commit,
            "previous_release_sequence": 22,
            "previous_manifest_sha256": "0" * 64,
            "previous_version": "0.1.0",
            "target_commit": self.new_commit,
            "target_version": "0.2.3",
            "release_sequence": 23,
            "manifest_sha256": release_contract.manifest_sha256(self.manifest),
            "recovery_relative_path": ".runtime/recovery/%s" % transaction_id,
        }, sort_keys=True) + "\n", encoding="utf-8")
        if os.name != "nt":
            journal.chmod(0o600)
        self._git("reset", "--hard", "-q", self.new_commit)

        result = self._apply()

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.new_commit)
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

        second = self._apply()
        self.assertEqual(second.status, "candidate_ready")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.new_commit)

    def test_committed_state_plus_journal_reentry_finalizes_without_reset(self):
        result = self._apply()
        self.assertEqual(result.status, "candidate_ready")
        update_transaction._write_journal(
            self.root,
            {
                "schema": 1,
                "transaction_id": result.transaction_id,
                "phase": "smoke_started",
                "previous_commit": self.old_commit,
                "previous_release_sequence": 22,
                "previous_manifest_sha256": "1" * 64,
                "previous_version": "0.2.2",
                "target_commit": self.new_commit,
                "target_version": "0.2.3",
                "release_sequence": 23,
                "manifest_sha256": release_contract.manifest_sha256(self.manifest),
                "recovery_relative_path": (
                    update_transaction.RECOVERY_RELATIVE_PATH / result.transaction_id
                ).as_posix(),
            },
        )
        with (
            mock.patch.object(updater, "inspect_update") as inspect_update,
            mock.patch.object(update_transaction, "_reset_to_commit") as reset,
        ):
            recovered = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
        self.assertEqual(recovered.status, "candidate_ready")
        inspect_update.assert_not_called()
        reset.assert_not_called()
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

    def test_candidate_state_commit_survives_journal_delete_failure(self):
        native_remove = update_transaction._remove_durable
        with mock.patch.object(
            update_transaction,
            "_remove_durable",
            side_effect=update_transaction.UpdateTransactionError("sharing violation"),
        ):
            result = self._apply()
        self.assertEqual(result.status, "candidate_ready")
        self.assertTrue((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_result"], "candidate_ready")

        with (
            mock.patch.object(update_transaction, "_remove_durable", wraps=native_remove),
            mock.patch.object(update_transaction, "_reset_to_commit") as reset,
        ):
            finalized = self._apply()
        self.assertEqual(finalized.status, "candidate_ready")
        reset.assert_not_called()
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

    def test_manifest_free_finalizer_clears_a_committed_journal_before_admission(self):
        with mock.patch.object(
            update_transaction,
            "_remove_durable",
            side_effect=update_transaction.UpdateTransactionError("sharing violation"),
        ):
            first = self._apply()
        self.assertEqual(first.status, "candidate_ready")
        self.assertTrue((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

        finalized = update_transaction.finalize_present_journal(self.root)

        self.assertIsNotNone(finalized)
        self.assertEqual(finalized.status, "candidate_ready")
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())
        with update_coordination.shim_session_lease(
            self.root, self.new_commit, heartbeat_seconds=None
        ) as lease:
            self.assertTrue(lease.path.exists())

    def test_rolled_back_state_survives_journal_delete_failure(self):
        native_remove = update_transaction._remove_durable
        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
            mock.patch.object(
                update_transaction,
                "_run_candidate_smoke",
                side_effect=update_transaction.CandidateSmokeError("failed"),
            ),
            mock.patch.object(
                update_transaction,
                "_remove_durable",
                side_effect=update_transaction.UpdateTransactionError("sharing violation"),
            ),
        ):
            first = update_transaction.apply_present_update(
                self.root, self.manifest, self.signature, {}
            )
        self.assertEqual(first.status, "rolled_back")
        self.assertTrue((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

        with (
            mock.patch.object(update_transaction, "_remove_durable", wraps=native_remove),
            mock.patch.object(update_transaction, "_reset_to_commit") as reset,
        ):
            finalized = self._apply()
        self.assertEqual(finalized.status, "rolled_back")
        reset.assert_not_called()
        self.assertFalse((self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists())

    def test_residual_candidate_journal_refuses_a_changed_checkout(self):
        with mock.patch.object(
            update_transaction,
            "_remove_durable",
            side_effect=update_transaction.UpdateTransactionError("sharing violation"),
        ):
            first = self._apply()
        self.assertEqual(first.status, "candidate_ready")
        (self.root / "app.txt").write_text("changed after commit\n", encoding="utf-8")

        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            recovered = self._apply()
        self.assertEqual(recovered.status, "repair_required")
        reset.assert_not_called()

    def test_candidate_smoke_discards_output_and_appends_managed_root(self):
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(
            update_transaction.subprocess, "run", return_value=completed
        ) as run:
            update_transaction._run_candidate_smoke(self.root, self.manifest)
        args, kwargs = run.call_args
        code = args[0][4]
        self.assertIn("sys.path.append", code)
        self.assertNotIn("sys.path.insert", code)
        self.assertIn("root in module.parents", code)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)

    def test_real_candidate_smoke_runs_against_the_project_checkout(self):
        project_root = Path(__file__).resolve().parents[2]
        project_version = (project_root / "VERSION").read_text(encoding="utf-8").strip()
        manifest = replace(self.manifest, version=project_version)
        update_transaction._run_candidate_smoke(project_root, manifest)

    def test_live_defer_does_not_clobber_candidate_ready_commit_point(self):
        first = self._apply()
        before = self.state_path.read_bytes()
        self._spawn_lease_holder()

        deferred = self._apply()

        self.assertEqual(deferred.status, "deferred_active_session")
        self.assertEqual(deferred.transaction_id, first.transaction_id)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_process_death_reenters_by_rollback_or_verified_forward_completion(self):
        script = r"""
import os
import pathlib
import sys
from unittest import mock
from installer import managed_install, release_contract, update_transaction, updater

root = pathlib.Path(sys.argv[1])
home = pathlib.Path(sys.argv[2])
git = pathlib.Path(sys.argv[3])
old_commit = sys.argv[4]
new_commit = sys.argv[5]
crash_phase = sys.argv[6]
manifest = release_contract.ReleaseManifest(
    schema=1,
    repository_id=managed_install.REPOSITORY_ID,
    channel="stable",
    release_sequence=23,
    version="0.2.3",
    tag="v0.2.3",
    commit=new_commit,
    min_python="3.12",
    published_at="2026-07-22T00:00:00Z",
    key_id="test-key",
)
signature = release_contract.ReleaseSignature(
    schema=1,
    algorithm=release_contract.RSA_SHA256_ALGORITHM,
    key_id="test-key",
    signature="AA==",
)
verified = release_contract.VerifiedRelease(manifest, "test-key")
native_write = update_transaction._write_journal

def write_then_crash(target_root, value):
    native_write(target_root, value)
    if value["phase"] == crash_phase:
        os._exit(86)

with (
    mock.patch.object(managed_install.config, "DEFAULT_DEEPPATTERN_HOME", home),
    mock.patch.object(updater, "_trusted_git_candidates", return_value=(git,)),
    mock.patch.object(release_contract, "authorize_release", return_value=verified),
    mock.patch.object(update_transaction, "_write_journal", side_effect=write_then_crash),
):
    update_transaction.apply_present_update(root, manifest, signature, {})
raise SystemExit(87)
"""
        for phase in ("prepared", "reset_started", "candidate_applied", "smoke_started"):
            with self.subTest(phase=phase):
                self._git("reset", "--hard", "-q", self.old_commit)
                self._write_state()
                (self.root / "app.txt").write_text(
                    "crash-safe %s\n" % phase, encoding="utf-8"
                )
                crashed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(self.root),
                        str(self.home),
                        str(self.git),
                        self.old_commit,
                        self.new_commit,
                        phase,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                    },
                )
                self.assertEqual(crashed.returncode, 86, crashed.stderr)
                journal = json.loads(
                    (self.root / update_transaction.JOURNAL_RELATIVE_PATH).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(journal["phase"], phase)

                recovered = self._apply()

                expected_forward = phase in {"candidate_applied", "smoke_started"}
                self.assertEqual(
                    recovered.status,
                    "candidate_ready" if expected_forward else "rolled_back",
                )
                self.assertEqual(
                    self._git("rev-parse", "HEAD").stdout.strip(),
                    self.new_commit if expected_forward else self.old_commit,
                )
                if expected_forward:
                    self.assertEqual(
                        (Path(recovered.recovery_path) / "tracked" / "app.txt").read_text(
                            encoding="utf-8"
                        ),
                        "crash-safe %s\n" % phase,
                    )
                else:
                    self.assertEqual(
                        (self.root / "app.txt").read_text(encoding="utf-8"),
                        "crash-safe %s\n" % phase,
                    )
                self.assertFalse(
                    (self.root / update_transaction.JOURNAL_RELATIVE_PATH).exists()
                )

    def test_malformed_state_refuses_before_any_mutation(self):
        self.state_path.write_bytes(b'{"schema":1')
        before = self._git("rev-parse", "HEAD").stdout.strip()
        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "invalid|unreadable"):
                self._apply()
        reset.assert_not_called()
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), before)

    def test_corrupt_journal_enters_stable_repair_without_git_mutation(self):
        journal = self.root / update_transaction.JOURNAL_RELATIVE_PATH
        journal.write_bytes(b'{"schema":1')
        if os.name != "nt":
            journal.chmod(0o600)
        before = self._git("rev-parse", "HEAD").stdout.strip()
        with mock.patch.object(update_transaction, "_reset_to_commit") as reset:
            first = self._apply()
            second = self._apply()
        self.assertEqual(first.status, "repair_required")
        self.assertEqual(second.status, "repair_required")
        self.assertEqual(first.error_code, "corrupt_journal")
        reset.assert_not_called()
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(journal.read_bytes(), b'{"schema":1')

    def test_malformed_state_does_not_block_valid_journal_recovery(self):
        self._write_empty_journal()
        self._git("reset", "--hard", "-q", self.new_commit)
        self.state_path.write_bytes(b'{"schema":1')

        result = self._apply()

        self.assertEqual(result.status, "candidate_ready")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.new_commit)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_result"], "candidate_ready")


if __name__ == "__main__":
    unittest.main()
