"""Compatibility tests for the GE server-chat stdin/native bridge."""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from client import windows_security
from client.popup import native_shell, session

_TURN_ID = "123e4567-e89b-42d3-a456-426614174000"
_PRIVACY_VERSION = "sha256:test-v1"


def _private_test_root(value) -> Path:
    """Make an explicitly test-owned root satisfy the production trust boundary."""
    root = Path(value)
    if os.name == "posix":
        os.chmod(root, 0o700)
    if os.name == "nt":
        windows_security.harden_private_data_acl(root)
    return root


def _popup_test_root(value) -> Path:
    """Place the managed root below a private parent on shared-temp POSIX hosts."""
    return _private_test_root(value) / "popup-sessions"


def _bridge(token: str = "sentinel-device-token") -> dict:
    return {
        "schema_version": 1,
        "kind": "ge-chat",
        "route": "server",
        "endpoint": "https://hub.example",
        "device_token": token,
        "run_id": "ge_run_1",
    }


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    if not predicate():
        raise AssertionError("condition did not become true")


class _RecordingPipe:
    def __init__(self, *, write_result=None, error=None, block=None, close_error=None):
        self.events = []
        self.payload = b""
        self.write_result = write_result
        self.error = error
        self.block = block
        self.close_error = close_error

    def write(self, payload):
        self.events.append("write")
        if self.block is not None:
            self.block.wait(2)
        if self.error is not None:
            raise self.error
        self.payload = bytes(payload)
        return len(payload) if self.write_result is None else self.write_result

    def flush(self):
        self.events.append("flush")

    def close(self):
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error


class _Process:
    def __init__(self, pipe):
        self.stdin = pipe
        self.poll = mock.Mock(return_value=None)
        self.terminate = mock.Mock()
        self.kill = mock.Mock()
        self.wait = mock.Mock(return_value=0)


class SessionBridgeHandoffTests(unittest.TestCase):
    def _spawn(self, process, root, **kwargs):
        with (
            mock.patch.object(session.backend, "ensure_webview", return_value=True),
            mock.patch.object(session, "_POPUP_ROOT", _popup_test_root(root)),
            mock.patch.object(
                session, "_validate_windows_private_mutation_acl", return_value=True
            ),
            mock.patch.object(session, "_new_popup_id", return_value="pop_bridge"),
            mock.patch.object(
                session.launcher,
                "build_shell_command",
                return_value=[sys.executable, "-m", "client.popup.native_shell"],
            ) as build,
            mock.patch.object(session.subprocess, "Popen", return_value=process) as popen,
        ):
            result = session.spawn(
                "<html><body>artifact</body></html>",
                "GE",
                chat_bootstrap=_bridge(),
                **kwargs,
            )
        return result, build, popen

    def test_private_popup_root_rejects_windows_reparse_point(self):
        info = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_file_attributes=0x400,
        )
        with (
            mock.patch.object(Path, "mkdir"),
            mock.patch.object(Path, "lstat", return_value=info),
        ):
            self.assertFalse(session._ensure_private_popup_root())

    def test_private_popup_root_rejects_permissive_posix_directory(self):
        info = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o777,
            st_uid=123,
            st_file_attributes=0,
        )
        with (
            mock.patch.object(session, "_posix_uid", return_value=123),
            mock.patch.object(Path, "mkdir"),
            mock.patch.object(Path, "lstat", return_value=info),
        ):
            self.assertFalse(session._ensure_private_popup_root())

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_new_popup_root_rejects_untrusted_posix_parent(self):
        with tempfile.TemporaryDirectory() as parent:
            base = Path(parent)
            os.chmod(base, 0o777)
            popup_root = base / "popup-sessions"
            with mock.patch.object(session, "_POPUP_ROOT", popup_root):
                self.assertFalse(session._ensure_private_popup_root())
            self.assertFalse(popup_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_new_popup_root_rejects_untrusted_posix_ancestor(self):
        with tempfile.TemporaryDirectory() as parent:
            unsafe_home = Path(parent)
            os.chmod(unsafe_home, 0o777)
            private_parent = unsafe_home / ".deeppattern"
            private_parent.mkdir(mode=0o700)
            popup_root = private_parent / "popup-sessions"

            with mock.patch.object(session, "_POPUP_ROOT", popup_root):
                self.assertFalse(session._ensure_private_popup_root())

            self.assertFalse(popup_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_new_popup_root_accepts_trusted_symlink_ancestor(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            real_home = base / "real-home"
            private_parent = real_home / ".deeppattern"
            real_home.mkdir(mode=0o700)
            private_parent.mkdir(mode=0o700)
            linked_home = base / "linked-home"
            linked_home.symlink_to(real_home, target_is_directory=True)
            popup_root = linked_home / ".deeppattern" / "popup-sessions"

            with mock.patch.object(session, "_POPUP_ROOT", popup_root):
                self.assertTrue(session._ensure_private_popup_root())

            self.assertTrue((private_parent / "popup-sessions").is_dir())
            self.assertEqual(
                session._trusted_private_popup_root(popup_root),
                private_parent / "popup-sessions",
            )

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_new_popup_root_rejects_untrusted_canonical_symlink_ancestor(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            unsafe_home = base / "unsafe-home"
            private_parent = unsafe_home / ".deeppattern"
            private_parent.mkdir(parents=True, mode=0o700)
            os.chmod(unsafe_home, 0o777)
            linked_home = base / "linked-home"
            linked_home.symlink_to(unsafe_home, target_is_directory=True)
            popup_root = linked_home / ".deeppattern" / "popup-sessions"

            with mock.patch.object(session, "_POPUP_ROOT", popup_root):
                self.assertFalse(session._ensure_private_popup_root())

            self.assertFalse((private_parent / "popup-sessions").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_popup_operations_keep_the_validated_canonical_root(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            safe_home = base / "safe-home"
            private_parent = safe_home / ".deeppattern"
            safe_home.mkdir(mode=0o700)
            private_parent.mkdir(mode=0o700)
            attacker_parent = base / "attacker-parent"
            attacker_parent.mkdir(mode=0o700)
            os.chmod(attacker_parent, 0o777)
            attacker_hop = attacker_parent / "hop"
            attacker_hop.symlink_to(safe_home, target_is_directory=True)
            linked_home = base / "linked-home"
            linked_home.symlink_to(attacker_hop, target_is_directory=True)
            popup_root = linked_home / ".deeppattern" / "popup-sessions"

            with mock.patch.object(session, "_POPUP_ROOT", popup_root):
                self.assertTrue(session._ensure_private_popup_root())
                operational_root = session._trusted_private_popup_root(popup_root)

            self.assertEqual(
                operational_root, private_parent / "popup-sessions"
            )
            attacker_hop.unlink()
            attacker_hop.symlink_to(base / "nowhere", target_is_directory=True)
            self.assertTrue(operational_root.is_dir())

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_new_popup_root_rejects_unmodeled_access_acl(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            private_parent = base / ".deeppattern"
            private_parent.mkdir(mode=0o700)
            popup_root = private_parent / "popup-sessions"

            def listxattr(path, *, follow_symlinks=True):
                del follow_symlinks
                if Path(path) == private_parent:
                    return ["system.posix_acl_access"]
                return []

            with (
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(session.sys, "platform", "linux"),
                mock.patch.object(session.os, "listxattr", side_effect=listxattr),
            ):
                self.assertFalse(session._ensure_private_popup_root())

            self.assertFalse(popup_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_existing_popup_root_rejects_unmodeled_access_acl(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            private_parent = base / ".deeppattern"
            popup_root = private_parent / "popup-sessions"
            popup_root.mkdir(parents=True, mode=0o700)

            def listxattr(path, *, follow_symlinks=True):
                del follow_symlinks
                if Path(path) == popup_root:
                    return ["system.posix_acl_access"]
                return []

            with (
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(session.sys, "platform", "linux"),
                mock.patch.object(session.os, "listxattr", side_effect=listxattr),
            ):
                self.assertFalse(session._ensure_private_popup_root())

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_popup_leaf_rejects_unmodeled_access_acl(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            popup_root = base / ".deeppattern" / "popup-sessions"
            popup = popup_root / "pop_acl"
            popup.mkdir(parents=True, mode=0o700)
            os.utime(popup, (0, 0))

            def listxattr(path, *, follow_symlinks=True):
                del follow_symlinks
                if Path(path) == popup:
                    return ["system.posix_acl_access"]
                return []

            with (
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(session.sys, "platform", "linux"),
                mock.patch.object(session.os, "listxattr", side_effect=listxattr),
            ):
                self.assertIsNone(session._find_private_popup_workdir("pop_acl"))
                self.assertEqual(session.sweep(now=session._RESULT_TTL_S + 1), 0)

            self.assertTrue(popup.is_dir())

    def test_darwin_acl_probe_accepts_deny_but_rejects_allow_entries(self):
        deny_probe = SimpleNamespace(
            returncode=0,
            stdout="drwx------+ 1 owner staff 32 Jan 1 00:00 path\n"
            " 0: group:everyone deny delete\n",
        )
        allow_probe = SimpleNamespace(
            returncode=0,
            stdout="drwx------+ 1 owner staff 32 Jan 1 00:00 path\n"
            " 0: user:other allow add_file,delete_child\n",
        )
        with (
            mock.patch.object(session.sys, "platform", "darwin"),
            mock.patch.object(session.subprocess, "run", return_value=deny_probe),
        ):
            self.assertFalse(session._has_unmodeled_posix_acl(Path("/tmp/path")))
        with (
            mock.patch.object(session.sys, "platform", "darwin"),
            mock.patch.object(session.subprocess, "run", return_value=allow_probe),
        ):
            self.assertTrue(session._has_unmodeled_posix_acl(Path("/tmp/path")))

    @unittest.skipUnless(sys.platform == "darwin", "real Darwin ACL probe")
    def test_darwin_acl_probe_rejects_a_real_allow_entry(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            self.assertFalse(session._has_unmodeled_posix_acl(path))
            subprocess.run(
                [
                    "/bin/chmod",
                    "+a",
                    "group:everyone allow list,search",
                    os.fspath(path),
                ],
                check=True,
                capture_output=True,
            )
            try:
                self.assertTrue(session._has_unmodeled_posix_acl(path))
            finally:
                subprocess.run(
                    ["/bin/chmod", "-N", os.fspath(path)],
                    check=True,
                    capture_output=True,
                )

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_first_popup_creation_builds_then_validates_missing_parent(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            popup_root = base / ".deeppattern" / "popup-sessions"

            with mock.patch.object(session, "_POPUP_ROOT", popup_root):
                self.assertTrue(session._ensure_private_popup_root())

            self.assertTrue(popup_root.is_dir())

    @unittest.skipUnless(os.name == "posix", "POSIX namespace boundary")
    def test_safe_legacy_root_under_sticky_shared_parent_is_readable(self):
        with tempfile.TemporaryDirectory() as parent:
            base = Path(parent)
            os.chmod(base, 0o1777)
            legacy = base / "de-popups"
            legacy.mkdir(mode=0o700)
            os.chmod(legacy, 0o700)
            with mock.patch.object(session, "_LEGACY_POPUP_ROOT", legacy):
                self.assertTrue(
                    session._ensure_private_popup_root(create=False, root=legacy)
                )

    def test_existing_legacy_root_is_read_only_compatible_under_mutable_parent(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            legacy = base / "de-popups"
            legacy.mkdir(mode=0o700)
            os.chmod(legacy, 0o700)
            if os.name == "nt":
                windows_security.harden_private_data_acl(legacy)
            with (
                mock.patch.object(session, "_LEGACY_POPUP_ROOT", legacy),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=False
                ),
            ):
                self.assertTrue(
                    session._ensure_private_popup_root(create=False, root=legacy)
                )
                self.assertFalse(session._ensure_private_popup_root(create=True, root=legacy))

    def test_concurrent_popup_root_creation_revalidates_racing_winner(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            popup_root = base / "popup-sessions"
            original_mkdir = Path.mkdir
            raced = False

            def racing_mkdir(path, *args, **kwargs):
                nonlocal raced
                if Path(path) == popup_root and not raced:
                    raced = True
                    original_mkdir(path, mode=0o700)
                    os.chmod(path, 0o700)
                    raise FileExistsError(str(path))
                return original_mkdir(path, *args, **kwargs)

            with (
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(Path, "mkdir", racing_mkdir),
                mock.patch.object(
                    session, "_harden_windows_private_data_acl", return_value=True
                ) as harden,
            ):
                self.assertTrue(session._ensure_private_popup_root())

            harden.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows DACL integration")
    def test_private_popup_root_hardens_inherited_reader_acl(self):
        with tempfile.TemporaryDirectory() as parent:
            popup_root = _private_test_root(parent) / "popup-sessions"
            with mock.patch.object(session, "_POPUP_ROOT", popup_root):
                self.assertTrue(session._ensure_private_popup_root())
            windows_security.validate_private_data_acl(popup_root)

    @unittest.skipUnless(os.name == "nt", "Windows DACL integration")
    def test_real_windows_spawn_uses_shipped_acl_validator(self):
        process = _Process(None)
        with tempfile.TemporaryDirectory(prefix="de-popup-spawn-", dir=Path.home()) as parent:
            popup_root = _private_test_root(parent) / "popup-sessions"
            popup = popup_root / "pop_real_acl"
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(session, "_new_popup_id", return_value="pop_real_acl"),
                mock.patch.object(
                    session.launcher,
                    "build_shell_command",
                    return_value=[sys.executable, "-m", "client.popup.native_shell"],
                ),
                mock.patch.object(session.subprocess, "Popen", return_value=process),
                mock.patch.object(session, "_early_native_shell_returncode", return_value=None),
            ):
                result = session.spawn("<html><body>private</body></html>", "GE")

            self.assertEqual(result, {"status": "open", "popup_id": "pop_real_acl"})
            windows_security.validate_private_data_acl(popup_root)
            windows_security.validate_private_data_acl(popup)
            windows_security.validate_private_data_acl(popup / "popup.html")

    def test_existing_unsafe_popup_root_is_never_repaired_by_path(self):
        with tempfile.TemporaryDirectory() as parent:
            popup_root = Path(parent) / "popup-sessions"
            popup_root.mkdir(mode=0o700)
            with (
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_is_private_popup_directory", return_value=False
                ),
                mock.patch.object(
                    session, "_harden_windows_private_data_acl", return_value=True
                ) as harden,
            ):
                self.assertFalse(session._ensure_private_popup_root())

            harden.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows DACL integration")
    def test_new_popup_root_rejects_unsafe_parent_namespace(self):
        with tempfile.TemporaryDirectory() as parent:
            popup_root = Path(parent) / "popup-sessions"
            with (
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    windows_security,
                    "validate_private_mutation_acl",
                    side_effect=windows_security.WindowsSecurityError("unsafe parent"),
                ),
                mock.patch.object(
                    windows_security, "harden_private_data_acl"
                ) as harden,
            ):
                self.assertFalse(session._ensure_private_popup_root())

            harden.assert_not_called()

    def test_poll_and_sweep_fail_closed_when_popup_root_is_unsafe(self):
        with (
            mock.patch.object(session, "_ensure_private_popup_root", return_value=False),
            mock.patch.object(session.shutil, "rmtree") as rmtree,
        ):
            self.assertEqual(session.poll("pop_safe", wait_s=0), {"status": "unknown"})
            self.assertEqual(session.sweep(), 0)
        rmtree.assert_not_called()

    def test_poll_rejects_popup_id_path_traversal_before_root_access(self):
        for popup_id in (
            "../../.ssh",
            "pop_../private",
            "pop_a/b",
            "pop_a\\b",
            "pop_",
            "not-a-popup",
        ):
            with self.subTest(popup_id=popup_id):
                with mock.patch.object(
                    session, "_ensure_private_popup_root"
                ) as ensure_root:
                    self.assertEqual(
                        session.poll(popup_id, wait_s=0), {"status": "unknown"}
                    )
                ensure_root.assert_not_called()

    def test_poll_reads_terminal_result_from_safe_legacy_root(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            current = base / "current"
            legacy = base / "legacy"
            popup = legacy / "pop_legacy"
            legacy.mkdir(mode=0o700)
            os.chmod(legacy, 0o700)
            popup.mkdir(mode=0o700)
            (popup / "result.json").write_text(
                json.dumps({"outcome": "committed", "result": {"answer": 42}}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(session, "_POPUP_ROOT", current),
                mock.patch.object(session, "_LEGACY_POPUP_ROOT", legacy),
            ):
                result = session.poll("pop_legacy", wait_s=0)

        self.assertEqual(result, {"status": "done", "result": {"answer": 42}})

    def test_sweep_never_mutates_stale_safe_legacy_leaf(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            current = base / "current"
            legacy = base / "legacy"
            popup = legacy / "pop_stale"
            legacy.mkdir(mode=0o700)
            os.chmod(legacy, 0o700)
            popup.mkdir(mode=0o700)
            (popup / "popup.html").write_text("<html/>", encoding="utf-8")
            (popup / "result.json").write_text(
                json.dumps({"outcome": "dismissed"}), encoding="utf-8"
            )
            os.utime(popup, (0, 0))
            real_rmtree = session.shutil.rmtree
            with (
                mock.patch.object(session, "_POPUP_ROOT", current),
                mock.patch.object(session, "_LEGACY_POPUP_ROOT", legacy),
                mock.patch.object(session.shutil, "rmtree", wraps=real_rmtree) as rmtree,
            ):
                removed = session.sweep(now=session._RESULT_TTL_S + 1)
                self.assertTrue(popup.exists())

            rmtree.assert_not_called()

        self.assertEqual(removed, 0)

    def test_sweep_counts_only_confirmed_current_leaf_removals(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            current = base / "current"
            popup = current / "pop_pinned"
            current.mkdir(mode=0o700)
            popup.mkdir(mode=0o700)
            os.utime(popup, (0, 0))

            with (
                mock.patch.object(session, "_POPUP_ROOT", current),
                mock.patch.object(
                    session, "_remove_private_workdir", return_value=False
                ) as remove,
            ):
                blocked_removed = session.sweep(now=session._RESULT_TTL_S + 1)

            self.assertTrue(popup.exists())
            self.assertEqual(blocked_removed, 0)
            remove.assert_called_once_with(popup)

            with mock.patch.object(session, "_POPUP_ROOT", current):
                removed = session.sweep(now=session._RESULT_TTL_S + 1)

            self.assertFalse(popup.exists())
            self.assertEqual(removed, 1)

    def test_remove_private_workdir_reports_a_surviving_leaf(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            popup = base / "pop_blocked"
            popup.mkdir(mode=0o700)

            def blocked_rmtree(path, *, ignore_errors=False):
                self.assertEqual(path, popup)
                self.assertTrue(ignore_errors)

            with mock.patch.object(
                session.shutil, "rmtree", side_effect=blocked_rmtree
            ) as rmtree:
                self.assertFalse(session._remove_private_workdir(popup))

            self.assertTrue(popup.is_dir())
            rmtree.assert_called_once_with(popup, ignore_errors=True)

    def test_new_unsafe_empty_workdir_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            popup = Path(root) / "pop_unsafe"
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", _private_test_root(root)),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_unsafe"),
                mock.patch.object(
                    session, "_ensure_private_popup_root", return_value=True
                ),
                mock.patch.object(
                    session, "_is_private_popup_directory", return_value=False
                ),
                mock.patch.object(session.subprocess, "Popen") as popen,
            ):
                result = session.spawn("<html>private</html>", "GE")

            self.assertFalse(popup.exists())

        self.assertEqual(result, {"status": "failed", "reason": "workdir-failed"})
        popen.assert_not_called()

    def test_spawn_pins_root_and_leaf_while_writing_private_files(self):
        process = _Process(None)
        active = set()
        pin_calls = []
        real_write_text = Path.write_text

        @contextmanager
        def recording_pin(*paths):
            normalized = tuple(Path(path) for path in paths)
            pin_calls.append(normalized)
            active.update(normalized)
            try:
                yield
            finally:
                for path in normalized:
                    active.discard(path)

        def guarded_write(path, *args, **kwargs):
            path = Path(path)
            if path.name in {"popup.html", "chat_context.json"}:
                self.assertIn(path.parent, active)
                self.assertIn(path.parent.parent, active)
            return real_write_text(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            popup_root = _private_test_root(root)
            popup = popup_root / "pop_pinned_write"
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(session, "_ensure_private_popup_root", return_value=True),
                mock.patch.object(session, "_new_popup_id", return_value="pop_pinned_write"),
                mock.patch.object(
                    session, "_pin_windows_popup_directories", side_effect=recording_pin
                ),
                mock.patch.object(Path, "write_text", guarded_write),
                mock.patch.object(
                    session.launcher,
                    "build_shell_command",
                    return_value=[sys.executable, "-m", "client.popup.native_shell"],
                ),
                mock.patch.object(session.subprocess, "Popen", return_value=process),
                mock.patch.object(session, "_early_native_shell_returncode", return_value=None),
            ):
                result = session.spawn(
                    "<html><body>private</body></html>",
                    "GE",
                    chat_context={"source_text": "private context"},
                )

            self.assertEqual(result, {"status": "open", "popup_id": "pop_pinned_write"})
            self.assertEqual(pin_calls, [(popup_root,), (popup,)])

    def test_legacy_lookup_never_hardens_a_preexisting_root(self):
        with tempfile.TemporaryDirectory() as parent:
            base = _private_test_root(parent)
            current = base / "current"
            legacy = base / "legacy"
            popup = legacy / "pop_legacy"
            legacy.mkdir(mode=0o700)
            os.chmod(legacy, 0o700)
            popup.mkdir(mode=0o700)
            with (
                mock.patch.object(session, "_POPUP_ROOT", current),
                mock.patch.object(session, "_LEGACY_POPUP_ROOT", legacy),
                mock.patch.object(session, "_is_private_popup_directory", return_value=True),
                mock.patch.object(
                    session, "_harden_windows_private_data_acl", return_value=True
                ) as harden,
            ):
                self.assertEqual(
                    session._find_private_popup_workdir("pop_legacy"), popup
                )

            harden.assert_not_called()

    def test_missing_legacy_lookup_never_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as parent:
            missing_parent = Path(parent) / "missing"
            legacy = missing_parent / "legacy"
            self.assertFalse(
                session._ensure_private_popup_root(create=False, root=legacy)
            )
            self.assertFalse(missing_parent.exists())

    def test_server_bridge_handoff_is_synchronous_and_complete(self):
        pipe = _RecordingPipe()
        process = _Process(pipe)
        with tempfile.TemporaryDirectory() as root:
            result, build, popen = self._spawn(process, root)
            files = sorted(
                path.name for path in (_popup_test_root(root) / "pop_bridge").iterdir()
            )

        self.assertEqual(result, {"status": "open", "popup_id": "pop_bridge"})
        self.assertEqual(pipe.events, ["write", "flush", "close"])
        self.assertEqual(json.loads(pipe.payload.decode("utf-8")), _bridge())
        self.assertEqual(files, ["popup.html"])
        self.assertTrue(build.call_args.kwargs["chat_bridge_stdin"])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertNotIn(_bridge()["device_token"], " ".join(popen.call_args.args[0]))
        self.assertNotIn(_bridge()["device_token"], json.dumps(popen.call_args.kwargs, default=str))

    def test_server_bridge_reports_exit_before_handoff(self):
        pipe = _RecordingPipe()
        process = _Process(pipe)
        process.poll.return_value = 23
        with tempfile.TemporaryDirectory() as root:
            result, _build, _popen = self._spawn(process, root)
            self.assertFalse((_popup_test_root(root) / "pop_bridge").exists())

        self.assertEqual(
            result,
            {
                "status": "failed",
                "reason": "native-shell-exited",
                "returncode": 23,
            },
        )
        self.assertEqual(pipe.events, [])

    def test_server_bridge_reports_exit_during_handoff(self):
        class ExitPipe(_RecordingPipe):
            started = False

            def write(self, payload):
                self.started = True
                raise BrokenPipeError("synthetic child exit")

        pipe = ExitPipe()
        process = _Process(pipe)
        reap_checks = 0

        def delayed_returncode():
            nonlocal reap_checks
            if not pipe.started:
                return None
            reap_checks += 1
            return 31 if reap_checks >= 3 else None

        process.poll.side_effect = delayed_returncode
        with tempfile.TemporaryDirectory() as root:
            result, _build, _popen = self._spawn(process, root)
            self.assertFalse((_popup_test_root(root) / "pop_bridge").exists())

        self.assertEqual(
            result,
            {
                "status": "failed",
                "reason": "native-shell-exited",
                "returncode": 31,
            },
        )
        self.assertGreaterEqual(reap_checks, 3)

    def test_server_bridge_reports_real_child_exit_after_handoff(self):
        child = (
            "import sys,time; "
            "sys.stdin.buffer.read(); "
            "time.sleep(0.08); "
            "raise SystemExit(31)"
        )
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", _popup_test_root(root)),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_real_exit"),
                mock.patch.object(
                    session.launcher,
                    "build_shell_command",
                    return_value=[sys.executable, "-c", child],
                ),
                mock.patch.object(session, "_detach_kwargs", return_value={}),
                mock.patch.object(session, "_NATIVE_SHELL_EXIT_GRACE_S", 5.0),
            ):
                result = session.spawn(
                    "<html><body>artifact</body></html>",
                    "GE",
                    chat_bootstrap=_bridge(),
                )
            self.assertFalse((_popup_test_root(root) / "pop_real_exit").exists())

        self.assertEqual(
            result,
            {
                "status": "failed",
                "reason": "native-shell-exited",
                "returncode": 31,
            },
        )

    def test_delayed_real_child_startup_exit_is_not_reported_open(self):
        child = "import time; time.sleep(0.12); raise SystemExit(23)"
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", _popup_test_root(root)),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_delayed_exit"),
                mock.patch.object(
                    session.launcher,
                    "build_shell_command",
                    return_value=[sys.executable, "-c", child],
                ),
                mock.patch.object(session, "_detach_kwargs", return_value={}),
                mock.patch.object(session, "_NATIVE_SHELL_EXIT_GRACE_S", 5.0),
            ):
                result = session.spawn("<html><body>artifact</body></html>", "GE")
            self.assertFalse((_popup_test_root(root) / "pop_delayed_exit").exists())

        self.assertEqual(
            result,
            {
                "status": "failed",
                "reason": "native-shell-exited",
                "returncode": 23,
            },
        )

    def test_nonserializable_chat_context_removes_private_workdir(self):
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", _popup_test_root(root)),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_bad_context"),
            ):
                result = session.spawn(
                    "<html><body>artifact</body></html>",
                    "GE",
                    chat_context={"not_json": object()},
                )
            self.assertFalse((_popup_test_root(root) / "pop_bad_context").exists())

        self.assertEqual(result, {"status": "failed", "reason": "workdir-failed"})

    def test_workdir_collision_does_not_delete_existing_popup(self):
        with tempfile.TemporaryDirectory() as root:
            popup_root = _popup_test_root(root)
            popup_root.mkdir(mode=0o700)
            existing = popup_root / "pop_collision"
            existing.mkdir()
            sentinel = existing / "keep.txt"
            sentinel.write_text("owned by another popup", encoding="utf-8")
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_collision"),
            ):
                result = session.spawn("<html><body>artifact</body></html>", "GE")

            self.assertTrue(sentinel.is_file())
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "owned by another popup"
            )

        self.assertEqual(result, {"status": "failed", "reason": "workdir-failed"})

    def test_symlinked_popup_root_is_rejected_before_private_writes(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            target = root_path / "attacker-controlled"
            target.mkdir()
            popup_root = root_path / "de-popups"
            try:
                popup_root.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            process = _Process(None)
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(session, "_new_popup_id", return_value="pop_redirected"),
                mock.patch.object(session.subprocess, "Popen", return_value=process) as popen,
            ):
                result = session.spawn("<html><body>private</body></html>", "GE")

            self.assertFalse((target / "pop_redirected").exists())

        self.assertEqual(result, {"status": "failed", "reason": "workdir-failed"})
        popen.assert_not_called()

    def test_zero_exit_preserves_an_already_written_terminal_result(self):
        process = _Process(None)
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as root:
            popup_root = _popup_test_root(root)
            popup_dir = popup_root / "pop_terminal"

            def finish_before_return(*_args, **_kwargs):
                (popup_dir / "result.json").write_text(
                    json.dumps({"outcome": "dismissed"}), encoding="utf-8"
                )
                return process

            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_terminal"),
                mock.patch.object(
                    session.subprocess, "Popen", side_effect=finish_before_return
                ),
            ):
                result = session.spawn("<html><body>artifact</body></html>", "GE")

            self.assertTrue((popup_dir / "result.json").is_file())

        self.assertEqual(result, {"status": "open", "popup_id": "pop_terminal"})

    def test_nonzero_exit_preserves_an_already_written_terminal_result(self):
        with tempfile.TemporaryDirectory() as root:
            popup_dir = Path(root) / "pop_terminal_nonzero"
            popup_dir.mkdir(mode=0o700)
            (popup_dir / "result.json").write_text(
                json.dumps({"outcome": "committed", "result": {"answer": 42}}),
                encoding="utf-8",
            )
            result = session._native_shell_exit_response(
                popup_dir, "pop_terminal_nonzero", 9
            )
            self.assertTrue((popup_dir / "result.json").is_file())

        self.assertEqual(
            result, {"status": "open", "popup_id": "pop_terminal_nonzero"}
        )

    def test_handoff_exit_preserves_an_already_written_terminal_result(self):
        pipe = _RecordingPipe(error=BrokenPipeError("synthetic child exit"))
        process = _Process(pipe)
        process.poll.side_effect = [None, 0]
        with tempfile.TemporaryDirectory() as root:
            popup_root = _popup_test_root(root)
            popup_dir = popup_root / "pop_bridge"

            def finish_before_return(*_args, **_kwargs):
                (popup_dir / "result.json").write_text(
                    json.dumps({"outcome": "dismissed"}), encoding="utf-8"
                )
                return process

            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", popup_root),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_bridge"),
                mock.patch.object(
                    session.subprocess, "Popen", side_effect=finish_before_return
                ),
            ):
                result = session.spawn(
                    "<html><body>artifact</body></html>",
                    "GE",
                    chat_bootstrap=_bridge(),
                )
            self.assertTrue((popup_dir / "result.json").is_file())

        self.assertEqual(result, {"status": "open", "popup_id": "pop_bridge"})

    def test_short_write_terminates_child_and_removes_workdir(self):
        pipe = _RecordingPipe(write_result=1)
        process = _Process(pipe)
        with tempfile.TemporaryDirectory() as root:
            result, _build, _popen = self._spawn(process, root)
            self.assertFalse((_popup_test_root(root) / "pop_bridge").exists())

        self.assertEqual(result, {"status": "failed", "reason": "bridge-handoff-failed"})
        process.terminate.assert_called_once()

    def test_broken_pipe_terminates_child_and_removes_workdir(self):
        pipe = _RecordingPipe(error=BrokenPipeError("synthetic"))
        process = _Process(pipe)
        with tempfile.TemporaryDirectory() as root:
            result, _build, _popen = self._spawn(process, root)
            self.assertFalse((_popup_test_root(root) / "pop_bridge").exists())

        self.assertEqual(result["reason"], "bridge-handoff-failed")
        process.terminate.assert_called_once()

    def test_close_failure_terminates_child_and_removes_workdir(self):
        pipe = _RecordingPipe(close_error=OSError("synthetic close failure"))
        process = _Process(pipe)
        with tempfile.TemporaryDirectory() as root:
            result, _build, _popen = self._spawn(process, root)
            self.assertFalse((_popup_test_root(root) / "pop_bridge").exists())

        self.assertEqual(result, {"status": "failed", "reason": "bridge-handoff-failed"})
        process.terminate.assert_called_once()

    def test_missing_bridge_pipe_terminates_child_and_removes_workdir(self):
        process = _Process(None)
        with tempfile.TemporaryDirectory() as root:
            result, _build, _popen = self._spawn(process, root)
            self.assertFalse((_popup_test_root(root) / "pop_bridge").exists())

        self.assertEqual(result, {"status": "failed", "reason": "bridge-handoff-failed"})
        process.terminate.assert_called_once()

    def test_handoff_timeout_terminates_child_and_removes_workdir(self):
        release = threading.Event()
        pipe = _RecordingPipe(block=release)
        process = _Process(pipe)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            session, "_CHAT_BRIDGE_HANDOFF_TIMEOUT_S", 0.01
        ):
            try:
                result, _build, _popen = self._spawn(process, root)
                self.assertFalse((_popup_test_root(root) / "pop_bridge").exists())
            finally:
                release.set()

        self.assertEqual(result["reason"], "bridge-handoff-failed")
        process.terminate.assert_called_once()

    def test_invalid_or_oversized_envelope_never_spawns(self):
        cases = [
            {**_bridge(), "route": "legacy"},
            {**_bridge(), "extra": "field"},
            {**_bridge(), "device_token": "x" * 8193},
            {**_bridge(), "endpoint": "https://hub.example\nleak"},
        ]
        for envelope in cases:
            with self.subTest(keys=sorted(envelope)):
                with (
                    tempfile.TemporaryDirectory() as root,
                    mock.patch.object(session.backend, "ensure_webview", return_value=True),
                    mock.patch.object(session, "_POPUP_ROOT", _popup_test_root(root)),
                    mock.patch.object(
                        session,
                        "_validate_windows_private_mutation_acl",
                        return_value=True,
                    ),
                    mock.patch.object(session.subprocess, "Popen") as popen,
                ):
                    result = session.spawn("<html/>", "GE", chat_bootstrap=envelope)
                self.assertEqual(result, {"status": "failed", "reason": "bridge-state-invalid"})
                popen.assert_not_called()

    def test_platform_detach_flags_are_preserved_for_server_bridge(self):
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW — the last one keeps
        # a black console window from flashing beside the FRAMELESS popup on the python.exe
        # fallback (pythonw.exe is the primary suppressor, this is belt-and-braces).
        _DETACHED_PROCESS = 0x00000008
        _CREATE_NEW_PROCESS_GROUP = 0x00000200
        _CREATE_NO_WINDOW = 0x08000000
        for name, bit in (
            ("DETACHED_PROCESS", _DETACHED_PROCESS),
            ("CREATE_NEW_PROCESS_GROUP", _CREATE_NEW_PROCESS_GROUP),
            ("CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
        ):
            with self.subTest(flag=name):
                self.assertTrue(session._WIN_DETACHED & bit, f"{name} missing from _WIN_DETACHED")
        with mock.patch.object(session.os, "name", "nt"):
            self.assertEqual(
                session._detach_kwargs(),
                {"creationflags": session._WIN_DETACHED},
            )
        for platform_name in ("darwin", "linux"):
            with (
                self.subTest(platform=platform_name),
                mock.patch.object(session.os, "name", "posix"),
                mock.patch.object(session.sys, "platform", platform_name),
            ):
                self.assertEqual(session._detach_kwargs(), {"start_new_session": True})

    def test_native_shell_spawns_under_the_console_less_interpreter(self):
        # The call site must route the interpreter through launcher._popup_python so the
        # detached native shell is never carried by a console python.exe on Windows.
        process = _Process(_RecordingPipe())
        sentinel = "C:\\venv\\Scripts\\pythonw.exe"
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(session.backend, "ensure_webview", return_value=True),
                mock.patch.object(session, "_POPUP_ROOT", _popup_test_root(root)),
                mock.patch.object(
                    session, "_validate_windows_private_mutation_acl", return_value=True
                ),
                mock.patch.object(session, "_new_popup_id", return_value="pop_bridge"),
                mock.patch.object(
                    session.launcher, "_popup_python", return_value=sentinel
                ) as popup_python,
                mock.patch.object(
                    session.launcher,
                    "build_shell_command",
                    return_value=[sentinel, "-m", "client.popup.native_shell"],
                ) as build,
                mock.patch.object(session.subprocess, "Popen", return_value=process),
            ):
                result = session.spawn(
                    "<html><body>artifact</body></html>",
                    "GE",
                    python="C:\\venv\\Scripts\\python.exe",
                    chat_bootstrap=_bridge(),
                )
        self.assertEqual(result, {"status": "open", "popup_id": "pop_bridge"})
        popup_python.assert_called_once_with("C:\\venv\\Scripts\\python.exe")
        self.assertEqual(build.call_args.args[0], sentinel)


class PopupInterpreterSelectionTests(unittest.TestCase):
    """launcher._popup_python: prefer console-less pythonw.exe on Windows, else fall back."""

    def test_prefers_pythonw_beside_interpreter_on_windows(self):
        from client.popup import launcher

        interpreter = mock.Mock()
        interpreter.is_absolute.return_value = True
        candidate = mock.Mock()
        candidate.is_file.return_value = True
        candidate.__str__ = mock.Mock(
            return_value="C:\\venv\\Scripts\\pythonw.exe"
        )
        interpreter.with_name.return_value = candidate
        with (
            mock.patch.object(launcher.os, "name", "nt"),
            mock.patch.object(launcher, "Path", return_value=interpreter),
        ):
            chosen = launcher._popup_python("C:\\venv\\Scripts\\python.exe")
        self.assertEqual(chosen, "C:\\venv\\Scripts\\pythonw.exe")

    def test_falls_back_to_python_when_pythonw_absent_on_windows(self):
        from client.popup import launcher

        interpreter = mock.Mock()
        interpreter.is_absolute.return_value = True
        candidate = mock.Mock()
        candidate.is_file.return_value = False
        interpreter.with_name.return_value = candidate
        with (
            mock.patch.object(launcher.os, "name", "nt"),
            mock.patch.object(launcher, "Path", return_value=interpreter),
        ):
            chosen = launcher._popup_python("C:\\venv\\Scripts\\python.exe")
        self.assertEqual(chosen, "C:\\venv\\Scripts\\python.exe")

    def test_unusable_interpreter_path_falls_back_on_windows(self):
        from client.popup import launcher

        with (
            mock.patch.object(launcher.os, "name", "nt"),
            mock.patch.object(launcher, "Path", side_effect=ValueError("bad path")),
        ):
            self.assertEqual(launcher._popup_python(""), "")

    def test_relative_interpreter_never_selects_pythonw_from_working_directory(self):
        from client.popup import launcher

        interpreter = mock.Mock()
        interpreter.is_absolute.return_value = False
        with (
            mock.patch.object(launcher.os, "name", "nt"),
            mock.patch.object(launcher, "Path", return_value=interpreter),
        ):
            self.assertEqual(launcher._popup_python("python.exe"), "python.exe")
        interpreter.with_name.assert_not_called()

    def test_interpreter_unchanged_on_posix(self):
        from client.popup import launcher

        with mock.patch.object(launcher.os, "name", "posix"):
            # No pythonw split on POSIX and no filesystem probe — returned verbatim.
            with mock.patch.object(launcher.Path, "exists", return_value=True) as exists:
                chosen = launcher._popup_python("/usr/bin/python3")
            exists.assert_not_called()
        self.assertEqual(chosen, "/usr/bin/python3")


class NativeBridgeParsingTests(unittest.TestCase):
    def _run_main(self, raw: bytes):
        created = []
        http_module = types.ModuleType("client.popup.http_chat")

        class HttpChatSession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                created.append(self)

        http_module.HttpChatSession = HttpChatSession
        with tempfile.TemporaryDirectory() as root:
            html = Path(root) / "popup.html"
            html.write_text("<html/>", encoding="utf-8")
            stderr = io.StringIO()
            with (
                mock.patch.dict(sys.modules, {"client.popup.http_chat": http_module}),
                mock.patch.object(
                    native_shell.sys,
                    "stdin",
                    SimpleNamespace(buffer=io.BytesIO(raw)),
                ),
                mock.patch.object(native_shell.sys, "stderr", stderr),
                mock.patch.object(native_shell, "open_window") as open_window,
            ):
                rc = native_shell.main(
                    [
                        "--html",
                        str(html),
                        "--result-path",
                        str(Path(root) / "result.json"),
                        "--chat-bridge-stdin",
                    ]
                )
        return rc, created, open_window, stderr.getvalue()

    def test_valid_bridge_creates_http_session_without_legacy_cli(self):
        sentinel = "sentinel-device-token"
        raw = json.dumps(_bridge(sentinel), separators=(",", ":")).encode("utf-8")
        legacy = mock.Mock(side_effect=AssertionError("legacy CLI must not be constructed"))
        with mock.patch.object(native_shell.chat_backend, "ChatSession", legacy):
            rc, created, open_window, stderr = self._run_main(raw)

        self.assertEqual(rc, 0)
        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0].kwargs,
            {
                "endpoint": "https://hub.example",
                "token": sentinel,
                "run_id": "ge_run_1",
            },
        )
        self.assertEqual(open_window.call_args.kwargs["chat_route"], "server")
        self.assertIs(open_window.call_args.kwargs["chat"], created[0])
        self.assertNotIn(sentinel, stderr)
        legacy.assert_not_called()

    def test_bad_bridge_inputs_open_server_chat_unavailable_without_echo(self):
        sentinel = "sentinel-device-token"
        cases = (
            b"",
            b"\xff",
            b"{not-json",
            json.dumps({**_bridge(sentinel), "route": "legacy"}).encode("utf-8"),
            json.dumps({**_bridge(sentinel), "extra": 1}).encode("utf-8"),
            b"x" * (64 * 1024 + 1),
        )
        for raw in cases:
            with self.subTest(size=len(raw)):
                rc, created, open_window, stderr = self._run_main(raw)
                self.assertEqual(rc, 0)
                self.assertEqual(created, [])
                self.assertEqual(open_window.call_args.kwargs["chat_route"], "server")
                self.assertEqual(
                    open_window.call_args.kwargs["chat_error_code"],
                    "chat_unavailable",
                )
                self.assertNotIn(sentinel, stderr)


class _Window:
    def __init__(self):
        self.evaluated = []
        self.destroy = mock.Mock()

    def evaluate_js(self, script):
        self.evaluated.append(script)
        return True


class _ServerChat:
    def __init__(self):
        self.bootstrap_started = threading.Event()
        self.bootstrap_release = threading.Event()
        self.turn_started = threading.Event()
        self.turn_release = threading.Event()
        self.calls = []
        self.closed = False
        self.bootstrap_thread_id = None

    def bootstrap(self):
        self.bootstrap_thread_id = threading.get_ident()
        self.bootstrap_started.set()
        self.bootstrap_release.wait(2)
        return {
            "ok": True,
            "backend": "server",
            "state": "ready",
            "conversation": {
                "status": "active",
                "turn_count": 0,
                "max_turns": 40,
                "expires_at": 1,
            },
            "privacy_disclosure": {
                "version": _PRIVACY_VERSION,
                "providers": [
                    {
                        "category": "safe-category",
                        "data_region": "safe-region",
                        "training_enabled": False,
                        "cache_ttl_seconds": 0,
                        "provider_retention_hours": 0,
                        "deletion_scope": "safe-scope",
                    }
                ],
            },
            "history": [],
        }

    async def run_turn(
        self,
        text,
        images,
        on_delta,
        on_done,
        on_error,
        on_action,
        *,
        client_turn_id,
        privacy_version,
        on_state=None,
    ):
        self.calls.append((text, images, client_turn_id, privacy_version))
        self.turn_started.set()
        if on_state is not None:
            on_state("submitting", {"price_credits": 0})
        self.turn_release.wait(2)
        on_delta("answer")
        on_done("answer")

    def close(self):
        self.closed = True


class NativeServerLifecycleTests(unittest.TestCase):
    def _api(self, chat=None):
        chat = chat or _ServerChat()
        api = native_shell.PopupApi("unused", chat=chat, chat_route="server")
        api._win = _Window()
        return api, chat

    def test_chat_ready_bootstrap_runs_off_bridge_thread_and_uses_frozen_callbacks(self):
        api, chat = self._api()
        returned = threading.Event()
        result = {}
        caller_thread_id = None

        def call_chat_ready():
            nonlocal caller_thread_id
            caller_thread_id = threading.get_ident()
            result.update(api.chat_ready())
            returned.set()

        caller = threading.Thread(target=call_chat_ready, name="bridge-caller")
        caller.start()

        self.assertTrue(chat.bootstrap_started.wait(1))
        self.assertTrue(returned.wait(1))
        caller.join(1)
        self.assertFalse(caller.is_alive())
        self.assertEqual(result, {"ok": True, "state": "capability_checking"})
        self.assertNotEqual(caller_thread_id, chat.bootstrap_thread_id)
        self.assertEqual(api._win.evaluated, [])
        chat.bootstrap_release.set()
        _wait_until(lambda: any(".bootstrap(" in item for item in api._win.evaluated))
        rendered = "\n".join(api._win.evaluated)
        self.assertIn("window.geChat.bootstrap", rendered)
        self.assertIn('window.geChat.state(null,"ready",{})', rendered)

    def test_cumulative_deltas_reach_existing_page_callback_before_terminal_events(self):
        class CumulativeChat(_ServerChat):
            async def run_turn(
                self,
                text,
                images,
                on_delta,
                on_done,
                on_error,
                on_action,
                *,
                client_turn_id,
                privacy_version,
                on_state=None,
            ):
                self.calls.append((text, images, client_turn_id, privacy_version))
                on_state("submitting", {"price_credits": 0})
                for value in ("a", "ab", "abc"):
                    on_delta(value)
                on_state("completed", {"turn_count": 1, "remaining_turns": 39})
                on_done("abc")

        api, _chat = self._api(CumulativeChat())
        api._privacy_version = _PRIVACY_VERSION

        self.assertEqual(
            api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION),
            {"ok": True},
        )
        _wait_until(lambda: any("window.geChat.done" in item for item in api._win.evaluated))

        expected = (
            'window.geChat.delta(1,"a")',
            'window.geChat.delta(1,"ab")',
            'window.geChat.delta(1,"abc")',
            'window.geChat.state(1,"completed"',
            'window.geChat.done(1,"abc")',
        )
        positions = [
            next(index for index, script in enumerate(api._win.evaluated) if value in script)
            for value in expected
        ]
        self.assertEqual(positions, sorted(positions))

    def test_capability_or_bootstrap_failure_never_constructs_cli(self):
        api = native_shell.PopupApi(
            "unused",
            chat=None,
            chat_route="server",
            chat_error_code="server_unsupported",
        )
        api._win = _Window()
        with mock.patch.object(
            native_shell.chat_backend,
            "ChatSession",
            side_effect=AssertionError("no server-to-CLI fallback"),
        ) as legacy:
            self.assertEqual(
                api.chat_ready(),
                {"ok": True, "state": "capability_checking"},
            )
            _wait_until(lambda: bool(api._win.evaluated))

        rendered = "\n".join(api._win.evaluated)
        self.assertIn("server_unsupported", rendered)
        legacy.assert_not_called()

    def test_server_credentials_and_opaque_handles_never_enter_javascript(self):
        api, chat = self._api()
        chat.device_token = "sentinel-device-token"
        chat.endpoint = "https://secret-hub.example"
        chat.run_id = "secret-run-id"
        api.chat_ready()
        self.assertTrue(chat.bootstrap_started.wait(1))
        chat.bootstrap_release.set()
        _wait_until(lambda: any(".bootstrap(" in item for item in api._win.evaluated))

        rendered = "\n".join(api._win.evaluated)
        self.assertNotIn(chat.device_token, rendered)
        self.assertNotIn(chat.endpoint, rendered)
        self.assertNotIn(chat.run_id, rendered)

    def test_provider_fields_are_projected_before_javascript(self):
        api, _chat = self._api()
        payload = {
            "ok": True,
            "backend": "server",
            "state": "ready",
            "conversation": {
                "status": "active",
                "turn_count": 0,
                "max_turns": 40,
                "expires_at": 1,
            },
            "privacy_disclosure": {
                "version": _PRIVACY_VERSION,
                "future_metadata": "SENTINEL-DISCLOSURE-EXTRA",
                "providers": [
                    {
                        "category": "safe-category",
                        "data_region": "safe-region",
                        "training_enabled": False,
                        "cache_ttl_seconds": 0,
                        "provider_retention_hours": 0,
                        "deletion_scope": "safe-scope",
                        "model": "SENTINEL-MODEL",
                        "api_key": "SENTINEL-KEY",
                        "raw_body": "RAW-PROVIDER-SENTINEL",
                    }
                ],
            },
            "history": [],
        }

        api._emit_chat(api._callback_generation, "bootstrap", payload)

        rendered = "\n".join(api._win.evaluated)
        self.assertIn("safe-category", rendered)
        self.assertNotIn("SENTINEL-MODEL", rendered)
        self.assertNotIn("SENTINEL-KEY", rendered)
        self.assertNotIn("RAW-PROVIDER-SENTINEL", rendered)
        self.assertNotIn("SENTINEL-DISCLOSURE-EXTRA", rendered)

    def test_invalid_provider_shapes_are_not_delivered_to_javascript(self):
        base = {
            "ok": True,
            "backend": "server",
            "state": "ready",
            "conversation": {
                "status": "active",
                "turn_count": 0,
                "max_turns": 40,
                "expires_at": 1,
            },
            "privacy_disclosure": {
                "version": _PRIVACY_VERSION,
                "providers": [
                    {
                        "category": "safe-category",
                        "data_region": "safe-region",
                        "training_enabled": False,
                        "cache_ttl_seconds": 0,
                        "provider_retention_hours": 0,
                        "deletion_scope": "safe-scope",
                    }
                ],
            },
            "history": [],
        }

        def clone():
            return json.loads(json.dumps(base))

        missing = clone()
        del missing["privacy_disclosure"]["providers"][0]["cache_ttl_seconds"]
        non_list = clone()
        non_list["privacy_disclosure"]["providers"] = {}
        nested = clone()
        nested["privacy_disclosure"]["providers"][0]["category"] = {
            "metadata": "SENTINEL-NESTED"
        }
        wrong_bool = clone()
        wrong_bool["privacy_disclosure"]["providers"][0]["training_enabled"] = 1
        enabled = clone()
        enabled["privacy_disclosure"]["providers"][0]["training_enabled"] = True
        oversized_int = clone()
        oversized_int["privacy_disclosure"]["providers"][0][
            "provider_retention_hours"
        ] = 2_147_483_648

        for name, payload in (
            ("missing", missing),
            ("non-list", non_list),
            ("nested", nested),
            ("wrong-bool", wrong_bool),
            ("enabled", enabled),
            ("oversized-int", oversized_int),
        ):
            with self.subTest(name=name):
                api, _chat = self._api()
                api._emit_chat(api._callback_generation, "bootstrap", payload)
                self.assertEqual(api._win.evaluated, [])

    def test_unknown_training_policy_is_delivered_as_null(self):
        api, _chat = self._api()
        payload = {
            "ok": True,
            "backend": "server",
            "state": "ready",
            "conversation": {
                "status": "active",
                "turn_count": 0,
                "max_turns": 40,
                "expires_at": 1,
            },
            "privacy_disclosure": {
                "version": _PRIVACY_VERSION,
                "providers": [
                    {
                        "category": "safe-category",
                        "data_region": "unverified",
                        "training_enabled": None,
                        "cache_ttl_seconds": 0,
                        "provider_retention_hours": 0,
                        "deletion_scope": "unverified",
                    }
                ],
            },
            "history": [],
        }

        api._emit_chat(api._callback_generation, "bootstrap", payload)

        self.assertEqual(len(api._win.evaluated), 1)
        self.assertIn('"training_enabled": null', api._win.evaluated[0])

    def test_invalid_state_payload_is_not_delivered_to_page(self):
        api, _chat = self._api()
        api._emit_chat(
            api._callback_generation,
            "state",
            1,
            "running",
            {"price_credits": 0, "device_token": "sentinel-device-token"},
        )
        self.assertEqual(api._win.evaluated, [])

    def test_state_payload_numbers_stay_within_javascript_safe_integer_range(self):
        api, _chat = self._api()
        generation = api._callback_generation

        api._emit_chat(
            generation,
            "state",
            1,
            "running",
            {"price_credits": native_shell._MAX_DISPLAY_TURN_ID},
        )
        api._emit_chat(
            generation,
            "state",
            1,
            "completed",
            {
                "turn_count": native_shell._MAX_DISPLAY_TURN_ID,
                "remaining_turns": native_shell._MAX_DISPLAY_TURN_ID,
            },
        )
        delivered = list(api._win.evaluated)

        api._emit_chat(
            generation,
            "state",
            1,
            "running",
            {"price_credits": native_shell._MAX_DISPLAY_TURN_ID + 1},
        )
        api._emit_chat(
            generation,
            "state",
            1,
            "completed",
            {
                "turn_count": native_shell._MAX_DISPLAY_TURN_ID + 1,
                "remaining_turns": 0,
            },
        )

        self.assertEqual(len(delivered), 2)
        self.assertEqual(api._win.evaluated, delivered)

    def test_server_ask_is_keyed_and_mid_turn_failure_has_no_cli_fallback(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION
        first = api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION)
        self.assertEqual(first, {"ok": True})
        self.assertTrue(chat.turn_started.wait(1))
        self.assertEqual(
            api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION),
            {"ok": True, "status": "coalesced"},
        )
        self.assertEqual(
            api.ask(
                2,
                "other",
                [],
                "223e4567-e89b-42d3-a456-426614174000",
                _PRIVACY_VERSION,
            ),
            {"ok": False, "error_code": "turn_in_flight"},
        )
        chat.turn_release.set()
        _wait_until(lambda: not api._busy)
        self.assertEqual(len(chat.calls), 1)

    def test_invalid_server_admission_makes_zero_http_calls(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION
        cases = (
            (True, "q", [], _TURN_ID, _PRIVACY_VERSION),
            (1, "", [], _TURN_ID, _PRIVACY_VERSION),
            (1, "q", None, _TURN_ID, _PRIVACY_VERSION),
            (1, "q", [], "not-a-uuid", _PRIVACY_VERSION),
            (1, "q", [], _TURN_ID, "wrong"),
            (1, "q", ["https://example/image.png"], _TURN_ID, _PRIVACY_VERSION),
        )
        for args in cases:
            with self.subTest(args=args[:2]):
                self.assertFalse(api.ask(*args)["ok"])
        self.assertEqual(chat.calls, [])

    def test_single_alternate_base64_image_is_forwarded(self):
        api, chat = self._api()
        self.addCleanup(chat.turn_release.set)
        api._privacy_version = _PRIVACY_VERSION
        alternate = "data:image/png;base64,iVBORw0KGgoBAh=="

        result = api.ask(
            1,
            "question",
            [alternate],
            _TURN_ID,
            _PRIVACY_VERSION,
        )

        self.assertEqual(result, {"ok": True})
        self.assertTrue(chat.turn_started.wait(1))
        self.assertEqual(
            chat.calls,
            [("question", [alternate], _TURN_ID, _PRIVACY_VERSION)],
        )
        chat.turn_release.set()
        _wait_until(lambda: not api._busy)

    def test_duplicate_decoded_images_are_rejected_before_worker_or_http(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION
        canonical = "data:image/png;base64,iVBORw0KGgoBAg=="
        alternate = "data:image/png;base64,iVBORw0KGgoBAh=="

        try:
            result = api.ask(
                1,
                "question",
                [canonical, alternate],
                _TURN_ID,
                _PRIVACY_VERSION,
            )
        finally:
            chat.turn_release.set()

        self.assertEqual(result, {"ok": False, "error_code": "invalid_request"})
        self.assertEqual(chat.calls, [])

    def test_duplicate_image_does_not_mask_later_aggregate_oversize(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION
        raw = b"\x89PNG\r\n\x1a\n" + b"x" * (3 * 1024 * 1024)

        def data_url(payload):
            return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")

        first = data_url(raw)
        later_one = data_url(raw[:-1] + b"y")
        later_two = data_url(raw[:-1] + b"z")
        try:
            result = api.ask(
                1,
                "question",
                [first, first, later_one, later_two],
                _TURN_ID,
                _PRIVACY_VERSION,
            )
        finally:
            chat.turn_release.set()

        self.assertEqual(result, {"ok": False, "error_code": "input_too_large"})
        self.assertEqual(chat.calls, [])

    def test_duplicate_image_does_not_mask_compact_request_oversize(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION
        canonical = "data:image/png;base64,iVBORw0KGgoBAg=="
        alternate = "data:image/png;base64,iVBORw0KGgoBAh=="

        with mock.patch.object(native_shell, "_MAX_CHAT_REQUEST_BYTES", 1):
            result = api.ask(
                1,
                "question",
                [canonical, alternate],
                _TURN_ID,
                _PRIVACY_VERSION,
            )

        self.assertEqual(result, {"ok": False, "error_code": "input_too_large"})
        self.assertEqual(chat.calls, [])

    def test_compact_request_oversize_precedes_base64_and_magic_validation(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION

        for image in (
            "data:image/png;base64,%%%%",
            "data:image/png;base64,/9j/4A==",
        ):
            with self.subTest(image=image), mock.patch.object(
                native_shell,
                "_MAX_CHAT_REQUEST_BYTES",
                1,
            ):
                result = api.ask(
                    1,
                    "question",
                    [image],
                    _TURN_ID,
                    _PRIVACY_VERSION,
                )
                self.assertEqual(
                    result,
                    {"ok": False, "error_code": "input_too_large"},
                )

        self.assertEqual(chat.calls, [])

    def test_text_validation_uses_stable_size_and_shape_errors_without_http(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION
        cases = (
            (None, "invalid_request"),
            (123, "invalid_request"),
            ("\ud800", "invalid_request"),
            ("x" * (native_shell._MAX_CHAT_TEXT_CHARS + 1), "input_too_large"),
        )
        for text, expected in cases:
            with self.subTest(text_type=type(text).__name__, expected=expected):
                result = api.ask(1, text, [], _TURN_ID, _PRIVACY_VERSION)
                self.assertEqual(result, {"ok": False, "error_code": expected})
        self.assertEqual(chat.calls, [])

    def test_close_during_validation_wins_before_worker_admission(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION
        validating = threading.Event()
        release = threading.Event()
        result = {}

        def validate(*_args):
            validating.set()
            release.wait(2)
            return None

        with mock.patch.object(native_shell, "_server_admission_error", side_effect=validate):
            worker = threading.Thread(
                target=lambda: result.update(
                    api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION)
                )
            )
            worker.start()
            self.assertTrue(validating.wait(1))
            self.assertEqual(api.chat_closing(), {"ok": True})
            release.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, {"ok": False, "error_code": "chat_unavailable"})
        self.assertEqual(chat.calls, [])

    def test_pending_submit_close_delegates_to_session_and_releases_ownership(self):
        events = []

        class OrderedChat(_ServerChat):
            def close(self):
                events.append("close")
                super().close()

        api, chat = self._api(OrderedChat())
        api._privacy_version = _PRIVACY_VERSION
        self.assertTrue(api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION)["ok"])
        self.assertTrue(chat.turn_started.wait(1))
        self.assertEqual(api.chat_closing(), {"ok": True})
        self.assertEqual(events, ["close"])
        self.assertFalse(api._recovering)
        self.assertIsNone(api._active_client_turn_id)
        self.assertIsNone(api._active_display_turn_id)
        self.assertIsNone(api._last_server_request)
        chat.turn_release.set()

    def test_shutdown_wait_uses_only_the_remaining_close_budget(self):
        api, _chat = self._api()
        finished = mock.Mock()
        finished.wait.return_value = False
        with (
            mock.patch.object(native_shell, "_CHAT_CLOSE_BUDGET_S", 0.05),
            mock.patch.object(native_shell.threading, "Event", return_value=finished),
            mock.patch.object(api, "_start_chat_worker", return_value=True) as start,
            mock.patch.object(native_shell.time, "monotonic", side_effect=[10.0, 10.02]),
        ):
            self.assertEqual(api.chat_closing(), {"ok": True})

        start.assert_called_once()
        finished.wait.assert_called_once()
        self.assertAlmostEqual(finished.wait.call_args.kwargs["timeout"], 0.03)

    def test_shutdown_budget_bounds_a_blocked_transport_close(self):
        close_started = threading.Event()
        release_close = threading.Event()
        result = {}

        class BlockingCloseChat(_ServerChat):
            def close(self):
                close_started.set()
                release_close.wait(2)
                super().close()

        api, chat = self._api(BlockingCloseChat())
        caller = threading.Thread(
            target=lambda: result.update(api.chat_closing()),
            name="test-chat-closing",
        )
        try:
            with mock.patch.object(native_shell, "_CHAT_CLOSE_BUDGET_S", 0.05):
                caller.start()
                self.assertTrue(close_started.wait(1))
                _wait_until(lambda: not caller.is_alive())

            self.assertFalse(caller.is_alive())
            self.assertEqual(result, {"ok": True})
        finally:
            release_close.set()
            caller.join(1)
        self.assertFalse(caller.is_alive())
        _wait_until(lambda: chat.closed)
        self.assertTrue(chat.closed)

    def test_close_suppresses_late_callbacks(self):
        api, chat = self._api()
        api._privacy_version = _PRIVACY_VERSION
        self.assertTrue(api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION)["ok"])
        self.assertTrue(chat.turn_started.wait(1))
        self.assertEqual(api.chat_closing(), {"ok": True})
        before = list(api._win.evaluated)
        chat.turn_release.set()
        _wait_until(lambda: not api._busy)
        self.assertEqual(api._win.evaluated, before)

    def test_retry_reuses_the_same_client_turn_id_after_recovering(self):
        class RecoveringChat(_ServerChat):
            async def run_turn(
                self,
                text,
                images,
                on_delta,
                on_done,
                on_error,
                on_action,
                *,
                client_turn_id,
                privacy_version,
                on_state=None,
            ):
                self.calls.append((text, images, client_turn_id, privacy_version))
                if len(self.calls) == 1:
                    on_state("recovering", {})
                    return
                on_state("completed", {"turn_count": 1, "remaining_turns": 39})
                on_done("answer")

        api, chat = self._api(RecoveringChat())
        api._privacy_version = _PRIVACY_VERSION
        self.assertTrue(api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION)["ok"])
        _wait_until(lambda: not api._busy)
        self.assertEqual(
            api._last_server_request,
            (1, "question", [], _TURN_ID, _PRIVACY_VERSION),
        )

        self.assertEqual(
            api.ask(1, "changed question", [], _TURN_ID, _PRIVACY_VERSION),
            {"ok": True, "status": "recovering"},
        )
        self.assertEqual(
            api.ask(
                2,
                "different turn",
                [],
                "223e4567-e89b-42d3-a456-426614174000",
                _PRIVACY_VERSION,
            ),
            {"ok": False, "error_code": "turn_in_flight"},
        )
        self.assertEqual(len(chat.calls), 1)

        self.assertEqual(
            api.retry_chat(_TURN_ID),
            {"ok": True, "status": "recovering"},
        )
        _wait_until(lambda: len(chat.calls) == 2 and not api._busy)
        self.assertEqual([call[2] for call in chat.calls], [_TURN_ID, _TURN_ID])
        self.assertEqual([call[0] for call in chat.calls], ["question", "question"])
        self.assertFalse(api._recovering)
        self.assertIsNone(api._last_server_request)

    def test_orphan_history_snapshot_reuses_sanitized_bootstrap_callback(self):
        history = [{
            "turn_no": 1,
            "status": "completed",
            "user_text": "orphan question",
            "assistant_text": "server authoritative answer",
            "error_code": None,
            "completed_at": 1,
            "images": [],
        }]

        class OrphanHistoryChat(_ServerChat):
            async def run_turn(
                self,
                text,
                images,
                on_delta,
                on_done,
                on_error,
                on_action,
                *,
                client_turn_id,
                privacy_version,
                on_state=None,
            ):
                self.calls.append((text, images, client_turn_id, privacy_version))
                on_state("recovering", {})
                on_action({"type": "history_snapshot", "history": history})
                on_state("completed", {"turn_count": 2, "remaining_turns": 38})
                on_done("current answer")

        api, chat = self._api(OrphanHistoryChat())
        chat.bootstrap_release.set()
        self.assertEqual(
            api.chat_ready(), {"ok": True, "state": "capability_checking"}
        )
        _wait_until(lambda: api._privacy_version == _PRIVACY_VERSION)
        initial_bootstraps = sum(
            "window.geChat.bootstrap(" in script for script in api._win.evaluated
        )

        self.assertEqual(
            api.ask(1, "current question", [], _TURN_ID, _PRIVACY_VERSION),
            {"ok": True},
        )
        _wait_until(lambda: not api._busy)

        bootstrap_calls = [
            script for script in api._win.evaluated
            if "window.geChat.bootstrap(" in script
        ]
        self.assertEqual(len(bootstrap_calls), initial_bootstraps + 1)
        self.assertIn("server authoritative answer", bootstrap_calls[-1])
        self.assertNotIn("turn_code", bootstrap_calls[-1])
        self.assertTrue(bootstrap_calls[-1].endswith(",true)"))

    def test_orphan_history_snapshot_requires_literal_page_true_ack(self):
        history = [{
            "turn_no": 1,
            "status": "completed",
            "user_text": "orphan question",
            "assistant_text": "page rejects this snapshot",
            "error_code": None,
            "completed_at": 1,
            "images": [],
        }]

        class AckChat(_ServerChat):
            def __init__(self):
                super().__init__()
                self.history_ack = None

            async def run_turn(
                self, text, images, on_delta, on_done, on_error, on_action, *,
                client_turn_id, privacy_version, on_state=None,
            ):
                self.calls.append((text, images, client_turn_id, privacy_version))
                on_state("recovering", {})
                self.history_ack = on_action(
                    {"type": "history_snapshot", "history": history}
                )

        class AckWindow(_Window):
            def __init__(self, result):
                super().__init__()
                self.result = result

            def evaluate_js(self, script):
                self.evaluated.append(script)
                if "page rejects this snapshot" not in script:
                    return True
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

        for result in (False, None, 1, "true", RuntimeError("page disappeared")):
            with self.subTest(result=result):
                api, chat = self._api(AckChat())
                api._win = AckWindow(result)
                chat.bootstrap_release.set()
                self.assertEqual(
                    api.chat_ready(), {"ok": True, "state": "capability_checking"}
                )
                _wait_until(lambda: api._privacy_version == _PRIVACY_VERSION)

                self.assertEqual(
                    api.ask(1, "current question", [], _TURN_ID, _PRIVACY_VERSION),
                    {"ok": True},
                )
                _wait_until(lambda: not api._busy)

                self.assertIs(chat.history_ack, False)
                self.assertTrue(api._recovering)
                self.assertEqual(len(chat.calls), 1)

    def test_orphan_history_refresh_marker_requires_literal_true_and_exact_arity(self):
        api, chat = self._api()
        chat.bootstrap_release.set()
        self.assertEqual(
            api.chat_ready(), {"ok": True, "state": "capability_checking"}
        )
        _wait_until(lambda: api._server_bootstrap is not None)
        generation = api._callback_generation
        bootstrap = api._server_bootstrap
        evaluated_before = len(api._win.evaluated)

        for args in ((bootstrap, 1), (bootstrap, "true"), (bootstrap, True, "extra")):
            with self.subTest(args=args[1:]):
                self.assertIs(
                    api._emit_chat(generation, "bootstrap", *args),
                    False,
                )
                self.assertEqual(len(api._win.evaluated), evaluated_before)

        self.assertIs(api._emit_chat(generation, "state", 0, "ready", {}), False)
        self.assertEqual(len(api._win.evaluated), evaluated_before)

    def test_hide_and_same_key_reconnect_do_not_start_parallel_turn_worker(self):
        api, chat = self._api()
        api._win.hide = mock.Mock()
        api._win.minimize = mock.Mock()
        api._privacy_version = _PRIVACY_VERSION
        self.assertEqual(
            api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION),
            {"ok": True},
        )
        self.assertTrue(chat.turn_started.wait(1))

        self.assertEqual(api.hide(), {"ok": True})
        self.assertEqual(
            api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION),
            {"ok": True, "status": "coalesced"},
        )
        self.assertEqual(api.retry_chat(_TURN_ID), {"ok": False, "status": "not_found"})
        self.assertEqual(len(chat.calls), 1)
        expected_control = api._win.hide if native_shell._IS_MAC else api._win.minimize
        expected_control.assert_called_once_with()
        chat.turn_release.set()
        _wait_until(lambda: not api._busy)

    def test_terminal_server_states_release_recovery_and_request_ownership(self):
        terminal_payloads = {
            "completed": {"turn_count": 1, "remaining_turns": 39},
            "failed": {"error_code": "model_unavailable"},
            "cancelled": {},
            "unavailable": {"error_code": "auth_required"},
        }

        for terminal_state, terminal_payload in terminal_payloads.items():
            with self.subTest(state=terminal_state):
                class TerminalChat(_ServerChat):
                    async def run_turn(
                        self,
                        text,
                        images,
                        on_delta,
                        on_done,
                        on_error,
                        on_action,
                        *,
                        client_turn_id,
                        privacy_version,
                        on_state=None,
                    ):
                        self.calls.append(
                            (text, images, client_turn_id, privacy_version)
                        )
                        on_state("recovering", {})
                        on_state(terminal_state, terminal_payload)

                api, _chat = self._api(TerminalChat())
                api._privacy_version = _PRIVACY_VERSION

                self.assertEqual(
                    api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION),
                    {"ok": True},
                )
                _wait_until(lambda: not api._busy)

                self.assertFalse(api._recovering)
                self.assertIsNone(api._active_client_turn_id)
                self.assertIsNone(api._active_display_turn_id)
                self.assertIsNone(api._last_server_request)

    def test_recovery_unavailable_releases_ownership_and_readmits_next_turn(self):
        next_turn_id = "223e4567-e89b-42d3-a456-426614174000"
        terminal_seen = threading.Event()
        terminal_release = threading.Event()

        class RecoveryUnavailableChat(_ServerChat):
            async def run_turn(
                self,
                text,
                images,
                on_delta,
                on_done,
                on_error,
                on_action,
                *,
                client_turn_id,
                privacy_version,
                on_state=None,
            ):
                self.calls.append((text, images, client_turn_id, privacy_version))
                if len(self.calls) == 1:
                    on_state("recovering", {})
                elif len(self.calls) == 2:
                    on_state("unavailable", {"error_code": "auth_required"})
                    terminal_seen.set()
                    terminal_release.wait(2)
                else:
                    on_state(
                        "completed", {"turn_count": 2, "remaining_turns": 38}
                    )
                    on_done("next answer")

        api, chat = self._api(RecoveryUnavailableChat())
        api._privacy_version = _PRIVACY_VERSION
        self.assertEqual(
            api.ask(1, "question", [], _TURN_ID, _PRIVACY_VERSION),
            {"ok": True},
        )
        _wait_until(lambda: len(chat.calls) == 1 and not api._busy)
        self.assertTrue(api._recovering)

        try:
            self.assertEqual(
                api.retry_chat(_TURN_ID),
                {"ok": True, "status": "recovering"},
            )
            self.assertTrue(terminal_seen.wait(1))
            self.assertFalse(api._recovering)
            self.assertTrue(api._busy)
            self.assertIsNone(api._active_client_turn_id)
            self.assertIsNone(api._active_display_turn_id)
            self.assertIsNone(api._last_server_request)
        finally:
            terminal_release.set()
        _wait_until(lambda: len(chat.calls) == 2 and not api._busy)

        self.assertIsNone(api._active_client_turn_id)
        self.assertIsNone(api._active_display_turn_id)
        self.assertIsNone(api._last_server_request)
        self.assertIn(
            'window.geChat&&window.geChat.state(1,"unavailable",{"error_code": "auth_required"})',
            api._win.evaluated,
        )
        self.assertEqual(
            api.ask(2, "next", [], next_turn_id, _PRIVACY_VERSION),
            {"ok": True},
        )
        _wait_until(lambda: len(chat.calls) == 3 and not api._busy)
        self.assertEqual([call[2] for call in chat.calls], [_TURN_ID, _TURN_ID, next_turn_id])

    def test_legacy_keeps_three_argument_ask_and_rejects_server_only_methods(self):
        called = []

        class LegacyChat:
            session_id = "legacy-session"
            context = {}
            strings = {"error": "legacy error"}

            async def run_turn(self, text, images, on_delta, on_done, on_error, on_action):
                called.append((text, images))
                on_done("legacy answer")

        api = native_shell.PopupApi("unused", chat=LegacyChat(), chat_route="legacy")
        api._win = _Window()
        self.assertEqual(api.ask(1, "legacy", []), {"ok": True})
        _wait_until(lambda: not api._busy)
        self.assertEqual(called, [("legacy", [])])
        unsupported = {"ok": False, "status": "client_unsupported"}
        self.assertEqual(api.retry_chat(_TURN_ID), unsupported)


if __name__ == "__main__":
    unittest.main()
