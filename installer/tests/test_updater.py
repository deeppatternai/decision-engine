"""Behavior locks for the read-only managed Git update inspection core."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from installer import managed_install, release_contract, updater


@unittest.skipUnless(shutil.which("git"), "Git is required for updater inspection tests")
class UpdateInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "deeppattern"
        self.root = self.home / "decision-engine"
        self.root.mkdir(parents=True)
        self.git = shutil.which("git")
        self.git_resolver = mock.patch.object(
            updater, "_trusted_git_candidates", return_value=(Path(self.git),)
        )
        self.git_resolver.start()
        self.addCleanup(self.git_resolver.stop)
        self._git("init", "-q")
        self._git("config", "user.name", "Updater Test")
        self._git("config", "user.email", "updater@example.invalid")
        self._git("config", "core.autocrlf", "false")
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
        self.marker_bytes = (self.root / managed_install.MARKER_FILENAME).read_bytes()
        self.state_digest = "1" * 64
        self._write_state()
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
        self.trusted_keys = {"test-key": mock.sentinel.public_key}
        self.verified = release_contract.VerifiedRelease(self.manifest, "test-key")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.git, "-C", str(self.root), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @property
    def state_path(self) -> Path:
        return self.root / ".runtime" / "update-state.json"

    def _write_state(self, **changes) -> None:
        payload = {
            "schema": 1,
            "channel": "stable",
            "last_release_sequence": 22,
            "last_release_commit": self.old_commit,
            "last_manifest_sha256": self.state_digest,
            "last_version": "0.2.2",
        }
        payload.update(changes)
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        if os.name != "nt":
            self.state_path.parent.chmod(0o700)
            self.state_path.chmod(0o600)

    def _inspect(self):
        with mock.patch.object(
            updater.release_contract, "authorize_release", return_value=self.verified
        ) as authorize:
            result = updater.inspect_update(
                self.root,
                self.manifest,
                self.signature,
                self.trusted_keys,
            )
        return result, authorize

    def _tree_digest(self, root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*"), key=lambda item: str(item)):
            relative = path.relative_to(root).as_posix().encode("utf-8")
            info = path.lstat()
            digest.update(relative + b"\0" + str(stat.S_IFMT(info.st_mode)).encode() + b"\0")
            digest.update(str(stat.S_IMODE(info.st_mode)).encode() + b"\0")
            if path.is_symlink():
                digest.update(os.readlink(path).encode("utf-8") + b"\0")
            elif path.is_file():
                body = path.read_bytes()
                digest.update(str(len(body)).encode() + b"\0" + body)
        return digest.hexdigest()

    def test_inspection_authorizes_with_prior_values_loaded_from_private_state(self):
        result, authorize = self._inspect()

        self.assertEqual(result.status, "update_available")
        self.assertEqual(result.current_commit, self.old_commit)
        self.assertEqual(result.target_commit, self.new_commit)
        self.assertFalse(result.tracked_dirty)
        self.assertEqual(result.collision_paths, ())
        self.assertEqual(authorize.call_count, 1)
        args, kwargs = authorize.call_args
        self.assertIs(args[0], self.manifest)
        self.assertIs(args[1], self.signature)
        self.assertIs(args[2], self.trusted_keys)
        self.assertEqual(kwargs["last_sequence"], 22)
        self.assertEqual(kwargs["last_commit"], self.old_commit)
        self.assertEqual(kwargs["last_manifest_sha256"], self.state_digest)
        self.assertEqual(kwargs["expected_repository_id"], managed_install.REPOSITORY_ID)
        self.assertEqual(kwargs["expected_channel"], "stable")

    def test_state_reader_uses_path_fallback_when_safe_dir_fd_is_unavailable(self):
        native_open = os.open
        native_lstat = os.lstat
        with (
            mock.patch.object(updater, "_supports_safe_dir_fd", return_value=False) as support,
            mock.patch.object(updater.os, "open", wraps=native_open) as opened,
            mock.patch.object(updater.os, "lstat", wraps=native_lstat) as inspected,
        ):
            state = updater._read_update_state(self.root)
        self.assertEqual(state.last_release_commit, self.old_commit)
        support.assert_called_once_with()
        self.assertTrue(opened.call_args_list)
        self.assertTrue(all("dir_fd" not in call.kwargs for call in opened.call_args_list))
        inspected_paths = {Path(call.args[0]) for call in inspected.call_args_list}
        self.assertTrue({self.root, self.state_path.parent, self.state_path} <= inspected_paths)

    @unittest.skipUnless(
        updater._supports_safe_dir_fd(), "safe dir_fd traversal requires POSIX support"
    )
    def test_state_reader_uses_nofollow_dir_fd_when_supported(self):
        native_open = os.open
        with mock.patch.object(updater.os, "open", wraps=native_open) as opened:
            state = updater._read_update_state(self.root)
        self.assertEqual(state.last_release_commit, self.old_commit)
        self.assertTrue(any("dir_fd" in call.kwargs for call in opened.call_args_list))
        directory_flags = os.O_NOFOLLOW | os.O_DIRECTORY
        self.assertTrue(
            any(call.args[1] & directory_flags == directory_flags for call in opened.call_args_list)
        )

    def test_inspection_api_has_no_caller_supplied_prior_or_running_python_parameters(self):
        parameters = inspect.signature(updater.inspect_update).parameters
        self.assertNotIn("last_sequence", parameters)
        self.assertNotIn("last_commit", parameters)
        self.assertNotIn("last_manifest_sha256", parameters)
        self.assertNotIn("running_python", parameters)
        self.assertNotIn("git_executable", parameters)
        self.assertTrue(all(
            parameter.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
            for parameter in parameters.values()
        ))

    def test_missing_corrupt_duplicate_or_zero_sequence_state_fails_closed(self):
        valid = {
            "schema": 1,
            "channel": "stable",
            "last_release_sequence": 22,
            "last_release_commit": self.old_commit,
            "last_manifest_sha256": self.state_digest,
            "last_version": "0.2.2",
        }
        duplicate = json.dumps(valid, sort_keys=True).replace(
            '"last_release_sequence": 22',
            '"last_release_sequence": 22, "last_release_sequence": 99',
        )
        cases = (
            ("missing", None, "missing|unreadable"),
            ("malformed", "not-json", "invalid|unreadable"),
            ("duplicate", duplicate, "duplicate"),
            ("zero", json.dumps({
                "schema": 1,
                "channel": "stable",
                "last_release_sequence": 0,
                "last_release_commit": self.old_commit,
                "last_manifest_sha256": self.state_digest,
                "last_version": "0.2.2",
            }), "sequence"),
        )
        for label, payload, message in cases:
            with self.subTest(label=label):
                if payload is None:
                    self.state_path.unlink(missing_ok=True)
                else:
                    self.state_path.write_text(payload, encoding="utf-8")
                    if os.name != "nt":
                        self.state_path.chmod(0o600)
                with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
                    with self.assertRaisesRegex(updater.UpdateInspectionError, message):
                        updater.inspect_update(
                            self.root, self.manifest, self.signature, {}
                        )
                authorize.assert_not_called()
                self._write_state()

    def test_oversized_or_recursively_nested_state_fails_closed(self):
        cases = (
            (b"x" * (updater._MAX_STATE_BYTES + 1), "size limit"),
            (("[" * 2000 + "0" + "]" * 2000).encode("ascii"), "invalid|unreadable|schema"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                self.state_path.write_bytes(payload)
                if os.name != "nt":
                    self.state_path.chmod(0o600)
                with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
                    with self.assertRaisesRegex(updater.UpdateInspectionError, message):
                        updater.inspect_update(self.root, self.manifest, self.signature, {})
                authorize.assert_not_called()
                self._write_state()

    def test_state_file_link_is_rejected(self):
        target = self.root / "elsewhere.json"
        target.write_bytes(self.state_path.read_bytes())
        self.state_path.unlink()
        try:
            self.state_path.symlink_to(target)
        except OSError:
            self.skipTest("file symlinks are unavailable on this host")
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "link"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_state_permissions_fail_closed_on_posix(self):
        if os.name == "nt":
            self.skipTest("PR3 cannot validate Windows DACLs; PR4 must before mutation")
        self.state_path.chmod(0o644)
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "permissions"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_state_parent_permissions_fail_closed_on_posix(self):
        if os.name == "nt":
            self.skipTest("PR3 cannot validate Windows DACLs; PR4 must before mutation")
        self.state_path.parent.chmod(0o777)
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "permissions"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_multiply_linked_state_file_fails_closed(self):
        alias = self.root / "state-hardlink.json"
        try:
            os.link(self.state_path, alias)
        except OSError:
            self.skipTest("hardlinks are unavailable on this host")
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "link"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_runtime_state_parent_link_fails_closed(self):
        real = self.root / "real-runtime"
        self.state_path.parent.replace(real)
        try:
            self.state_path.parent.symlink_to(real, target_is_directory=True)
        except OSError:
            real.replace(self.state_path.parent)
            self.skipTest("directory symlinks are unavailable on this host")
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "link"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_state_replacement_during_read_fails_closed_on_posix(self):
        if os.name == "nt":
            self.skipTest("Windows does not permit replacing this open test file")
        original = updater._read_bounded_state

        def replace_after_read(handle):
            raw = original(handle)
            replacement = self.state_path.with_suffix(".replacement")
            replacement.write_bytes(raw)
            replacement.chmod(0o600)
            os.replace(replacement, self.state_path)
            return raw

        with mock.patch.object(updater, "_read_bounded_state", side_effect=replace_after_read):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "changed"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})

    def test_live_remote_map_not_a_caller_value_controls_identity(self):
        self._git("remote", "set-url", "github", "https://example.invalid/attacker.git")
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "identity"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_extra_remote_fails_before_authorization(self):
        self._git("remote", "add", "extra", "https://example.invalid/extra.git")
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "identity"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_missing_remote_set_fails_as_an_identity_error(self):
        self._git("remote", "remove", "github")
        self._git("remote", "remove", "gitee")
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "identity"):
                updater.inspect_update(self.root, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_execution_capable_local_git_config_fails_before_authorization(self):
        hostile = (
            ("filter.hostile.process", "hostile-command"),
            ("filter.hostile.clean", "hostile-command"),
            ("filter.hostile.smudge", "hostile-command"),
            ("include.path", str(self.root / "attacker.cfg")),
            ("core.fsmonitor", "hostile-command"),
            ("core.hooksPath", str(self.root / "hooks")),
            ("core.sshCommand", "hostile-command"),
            ("diff.hostile.textconv", "hostile-command"),
            ("URL.hostile.insteadOf", "https://github.com/"),
            ("credential.helper", "hostile-command"),
            ("core.pager", "hostile-command"),
            ("core.editor", "hostile-command"),
            ("core.alternateRefsCommand", "hostile-command"),
            ("core.gitProxy", "hostile-command"),
            ("core.askPass", "hostile-command"),
            ("core.ExcludesFile", str(self.root / "global-ignore")),
            ("extensions.worktreeConfig", "true"),
            ("uploadpack.packObjectsHook", "hostile-command"),
            ("protocol.file.allow", "always"),
            ("alias.hostile", "!hostile-command"),
            ("submodule.hostile.update", "!hostile-command"),
            ("diff.external", "hostile-command"),
            ("interactive.diffFilter", "hostile-command"),
            ("difftool.hostile.cmd", "hostile-command"),
            ("mergetool.hostile.cmd", "hostile-command"),
            ("gpg.program", "hostile-command"),
            ("sequence.editor", "hostile-command"),
            ("receivepack.packObjectsHook", "hostile-command"),
            ("trace2.eventTarget", str(self.root / "trace2-event.json")),
        )
        for key, value in hostile:
            with self.subTest(key=key):
                self._git("config", key, value)
                with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
                    with self.assertRaisesRegex(updater.UpdateInspectionError, "execution-capable"):
                        updater.inspect_update(self.root, self.manifest, self.signature, {})
                authorize.assert_not_called()
                if key.casefold().startswith("trace2."):
                    self.assertFalse((self.root / "trace2-event.json").exists())
                self._git("config", "--unset-all", key)

    def test_subdirectory_is_not_accepted_as_the_managed_root(self):
        subdirectory = self.root / "nested"
        subdirectory.mkdir()
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "top-level"):
                updater.inspect_update(subdirectory, self.manifest, self.signature, {})
        authorize.assert_not_called()

    def test_head_must_still_equal_last_known_good_commit(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        with mock.patch.object(updater.release_contract, "authorize_release") as authorize:
            with self.assertRaisesRegex(updater.UpdateInspectionError, "last-known-good"):
                updater.inspect_update(
                    self.root, self.manifest, self.signature, {}
                )
        authorize.assert_not_called()

    def test_tag_must_resolve_to_the_signed_commit(self):
        wrong = release_contract.ReleaseManifest(
            **{**self.manifest.__dict__, "commit": self.old_commit}
        )
        verified = release_contract.VerifiedRelease(wrong, "test-key")
        with mock.patch.object(
            updater.release_contract, "authorize_release", return_value=verified
        ):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "tag.*commit"):
                updater.inspect_update(
                    self.root, wrong, self.signature, {}
                )

    def test_same_named_branch_cannot_substitute_for_a_missing_release_tag(self):
        self._git("branch", "v0.2.6", self.new_commit)
        candidate = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "version": "0.2.6",
                "tag": "v0.2.6",
                "release_sequence": 26,
            }
        )
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        with mock.patch.object(
            updater.release_contract, "authorize_release", return_value=verified
        ):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "operation failed: target"):
                updater.inspect_update(self.root, candidate, self.signature, {})

    def test_real_signature_authorization_reaches_update_available(self):
        from installer.tests.test_release_contract import _TEST_E, _TEST_N, _sign

        candidate = release_contract.ReleaseManifest(
            **{**self.manifest.__dict__, "key_id": "test-release-key"}
        )
        keys = {
            "test-release-key": release_contract.RsaPublicKey(
                key_id="test-release-key", modulus=_TEST_N, exponent=_TEST_E
            )
        }
        result = updater.inspect_update(self.root, candidate, _sign(candidate), keys)
        self.assertEqual(result.status, "update_available")
        self.assertEqual(result.target_commit, self.new_commit)

    def test_real_signature_authorization_rejects_a_tampered_signature(self):
        from installer.tests.test_release_contract import _TEST_E, _TEST_N, _sign

        candidate = release_contract.ReleaseManifest(
            **{**self.manifest.__dict__, "key_id": "test-release-key"}
        )
        signature = _sign(candidate)
        replacement = "A" if signature.signature[0] != "A" else "B"
        tampered = release_contract.ReleaseSignature(
            **{**signature.__dict__, "signature": replacement + signature.signature[1:]}
        )
        keys = {
            "test-release-key": release_contract.RsaPublicKey(
                key_id="test-release-key", modulus=_TEST_N, exponent=_TEST_E
            )
        }
        with self.assertRaises(release_contract.ReleaseContractError):
            updater.inspect_update(self.root, candidate, tampered, keys)

    def test_authorizer_cannot_return_a_different_manifest_than_the_input(self):
        different = release_contract.ReleaseManifest(
            **{**self.manifest.__dict__, "release_sequence": 24}
        )
        with mock.patch.object(
            updater.release_contract,
            "authorize_release",
            return_value=release_contract.VerifiedRelease(different, "test-key"),
        ):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "does not match"):
                updater.inspect_update(
                    self.root, self.manifest, self.signature, self.trusted_keys
                )

    def test_signed_target_may_not_track_runtime_config_or_marker_paths(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        for relative in ("config.json", ".runtime/state.json", ".managed-install.json"):
            with self.subTest(relative=relative):
                self._git("reset", "--hard", "-q", self.new_commit)
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("tracked\n", encoding="utf-8")
                self._git("add", "-f", relative)
                self._git("commit", "-qm", "protected path")
                commit = self._git("rev-parse", "HEAD").stdout.strip()
                tag = "v0.2.%d" % (4 + len(relative))
                self._git("tag", tag)
                candidate = release_contract.ReleaseManifest(
                    **{
                        **self.manifest.__dict__,
                        "version": tag[1:],
                        "tag": tag,
                        "commit": commit,
                        "release_sequence": 24 + len(relative),
                    }
                )
                verified = release_contract.VerifiedRelease(candidate, "test-key")
                self._git("reset", "--hard", "-q", self.old_commit)
                marker = self.root / managed_install.MARKER_FILENAME
                if not marker.exists():
                    marker.write_bytes(self.marker_bytes)
                with mock.patch.object(
                    updater.release_contract, "authorize_release", return_value=verified
                ):
                    with self.assertRaisesRegex(updater.UpdateInspectionError, "protected"):
                        updater.inspect_update(
                            self.root, candidate, self.signature, {}
                        )

    def test_signed_target_may_not_contain_a_gitlink(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            "160000,%s,vendor/submodule" % self.old_commit,
        )
        self._git("commit", "-qm", "target with gitlink")
        commit = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.30")
        candidate = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "version": "0.2.30",
                "tag": "v0.2.30",
                "commit": commit,
                "release_sequence": 30,
            }
        )
        self._git("reset", "--hard", "-q", self.old_commit)
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        with mock.patch.object(
            updater.release_contract, "authorize_release", return_value=verified
        ):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "gitlink"):
                updater.inspect_update(self.root, candidate, self.signature, {})

    def test_replacement_refs_do_not_hide_a_protected_target_path(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        protected = self.root / "config.json"
        protected.write_text("tracked\n", encoding="utf-8")
        self._git("add", "-f", "config.json")
        self._git("commit", "-qm", "protected path")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.4")
        self._git("replace", target, self.old_commit)
        candidate = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "version": "0.2.4",
                "tag": "v0.2.4",
                "commit": target,
                "release_sequence": 24,
            }
        )
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        self._git("reset", "--hard", "-q", self.old_commit)
        with mock.patch.object(updater.release_contract, "authorize_release", return_value=verified):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "protected"):
                updater.inspect_update(
                    self.root, candidate, self.signature, {}
                )

    def test_tracked_drift_is_classified_without_changing_the_file(self):
        changed = self.root / "app.txt"
        changed.write_text("local work\n", encoding="utf-8")
        result, _authorize = self._inspect()
        self.assertTrue(result.tracked_dirty)
        self.assertEqual(changed.read_text(encoding="utf-8"), "local work\n")

    def test_missing_tracked_file_is_classified_dirty(self):
        (self.root / "app.txt").unlink()
        result, _authorize = self._inspect()
        self.assertTrue(result.tracked_dirty)

    def test_clean_tracked_symlink_with_empty_eol_fields_is_supported(self):
        link = self.root / "app-link"
        try:
            link.symlink_to("app.txt")
        except OSError:
            self.skipTest("tracked symlinks are unavailable")
        self._git("add", "app-link")
        self._git("commit", "-qm", "tracked symlink")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        self._write_state(last_release_commit=base)
        result, _authorize = self._inspect()
        self.assertFalse(result.tracked_dirty)

    def test_tracked_symlink_replaced_by_same_body_regular_file_is_dirty(self):
        link = self.root / "app-link"
        try:
            link.symlink_to("app.txt")
        except OSError:
            self.skipTest("tracked symlinks are unavailable")
        self._git("add", "app-link")
        self._git("commit", "-qm", "tracked symlink")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        self._write_state(last_release_commit=base)
        self._git("config", "core.symlinks", "true")
        link.unlink()
        link.write_bytes(b"app.txt")
        result, _authorize = self._inspect()
        self.assertTrue(result.tracked_dirty)

    def test_clean_autocrlf_checkout_is_not_reported_dirty(self):
        (self.root / "app.txt").write_bytes(b"lf-only-base\n")
        self._git("add", "app.txt")
        self._git("commit", "-qm", "LF base")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        self._write_state(last_release_commit=base)
        self._git("config", "core.autocrlf", "true")
        (self.root / "app.txt").unlink()
        self._git("checkout", "--", "app.txt")
        self.assertIn(b"\r\n", (self.root / "app.txt").read_bytes())
        result, _authorize = self._inspect()
        self.assertFalse(result.tracked_dirty)

    def test_manual_crlf_change_without_conversion_policy_is_dirty(self):
        self.assertEqual(
            self._git("config", "--get", "core.autocrlf").stdout.strip(),
            "false",
        )
        (self.root / "app.txt").write_bytes(b"lf-only-base\n")
        self._git("add", "app.txt")
        self._git("commit", "-qm", "LF base")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        self._write_state(last_release_commit=base)
        (self.root / "app.txt").write_bytes(b"lf-only-base\r\n")
        result, _authorize = self._inspect()
        self.assertTrue(
            result.tracked_dirty,
            self._git("ls-files", "--eol", "--", "app.txt").stdout,
        )

    def test_explicit_crlf_attribute_authorizes_clean_checkout(self):
        self.assertEqual(
            self._git("config", "--get", "core.autocrlf").stdout.strip(),
            "false",
        )
        (self.root / ".gitattributes").write_bytes(b"app.txt text eol=crlf\n")
        (self.root / "app.txt").write_bytes(b"attribute-base\n")
        self._git("add", ".gitattributes", "app.txt")
        self._git("commit", "-qm", "attribute CRLF base")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        self._write_state(last_release_commit=base)
        (self.root / "app.txt").unlink()
        self._git("checkout", "--", "app.txt")
        self.assertIn(b"\r\n", (self.root / "app.txt").read_bytes())
        self.assertIn(
            "attr/text eol=crlf",
            self._git("ls-files", "--eol", "--", "app.txt").stdout,
        )
        result, _authorize = self._inspect()
        self.assertFalse(result.tracked_dirty)

    def test_minus_text_overrides_autocrlf_and_keeps_manual_crlf_dirty(self):
        (self.root / ".gitattributes").write_bytes(b"app.txt -text\n")
        (self.root / "app.txt").write_bytes(b"binary-base\n")
        self._git("add", ".gitattributes", "app.txt")
        self._git("commit", "-qm", "binary attribute base")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        self._write_state(last_release_commit=base)
        self._git("config", "core.autocrlf", "true")
        (self.root / "app.txt").write_bytes(b"binary-base\r\n")
        self.assertIn(
            "attr/-text",
            self._git("ls-files", "--eol", "--", "app.txt").stdout,
        )
        result, _authorize = self._inspect()
        self.assertTrue(result.tracked_dirty)

    def test_assume_unchanged_and_skip_worktree_flags_are_classified_as_dirty(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self._git("update-index", flag, "app.txt")
                changed = self.root / "app.txt"
                changed.write_text("hidden local work\n", encoding="utf-8")
                result, _authorize = self._inspect()
                self.assertTrue(result.tracked_dirty)
                self.assertEqual(changed.read_text(encoding="utf-8"), "hidden local work\n")
                inverse = "--no-" + flag[2:]
                self._git("update-index", inverse, "app.txt")
                self._git("checkout", "--", "app.txt")

    def test_ignored_collision_is_reported_without_changing_it(self):
        self._git("reset", "--hard", "-q", self.old_commit)
        (self.root / ".gitignore").write_text("collision.txt\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-qm", "ignore collision")
        base = self._git("rev-parse", "HEAD").stdout.strip()
        self._write_state(last_release_commit=base)
        (self.root / "collision.txt").write_text("target\n", encoding="utf-8")
        self._git("add", "-f", "collision.txt")
        self._git("commit", "-qm", "track collision")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.5")
        candidate = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "version": "0.2.5",
                "tag": "v0.2.5",
                "commit": target,
                "release_sequence": 25,
            }
        )
        self._git("reset", "--hard", "-q", base)
        local = self.root / "collision.txt"
        local.write_text("keep me\n", encoding="utf-8")
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        with mock.patch.object(updater.release_contract, "authorize_release", return_value=verified):
            result = updater.inspect_update(
                self.root, candidate, self.signature, {}
            )
        self.assertEqual(result.collision_paths, ("collision.txt",))
        self.assertEqual(local.read_text(encoding="utf-8"), "keep me\n")

    def test_untracked_collision_is_reported_without_changing_it(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        future = self.root / "future.txt"
        future.write_text("target\n", encoding="utf-8")
        self._git("add", "future.txt")
        self._git("commit", "-qm", "future target")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.7")
        candidate = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "version": "0.2.7",
                "tag": "v0.2.7",
                "commit": target,
                "release_sequence": 27,
            }
        )
        self._git("reset", "--hard", "-q", self.old_commit)
        future.write_text("keep me\n", encoding="utf-8")
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        with mock.patch.object(updater.release_contract, "authorize_release", return_value=verified):
            result = updater.inspect_update(self.root, candidate, self.signature, {})
        self.assertEqual(result.collision_paths, ("future.txt",))
        self.assertEqual(future.read_text(encoding="utf-8"), "keep me\n")

    def test_filesystem_only_unknown_reaches_collision_result(self):
        self._git("reset", "--hard", "-q", self.new_commit)
        future = self.root / "filesystem-only.txt"
        future.write_text("target\n", encoding="utf-8")
        self._git("add", "filesystem-only.txt")
        self._git("commit", "-qm", "filesystem-only target")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v0.2.8")
        candidate = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "version": "0.2.8",
                "tag": "v0.2.8",
                "commit": target,
                "release_sequence": 28,
            }
        )
        self._git("reset", "--hard", "-q", self.old_commit)
        future.write_text("keep me\n", encoding="utf-8")
        verified = release_contract.VerifiedRelease(candidate, "test-key")
        with (
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=verified
            ),
            mock.patch.object(updater, "_parse_nul_paths", return_value=()),
        ):
            result = updater.inspect_update(self.root, candidate, self.signature, {})
        self.assertEqual(result.collision_paths, ("filesystem-only.txt",))
        self.assertEqual(future.read_text(encoding="utf-8"), "keep me\n")

    def test_success_and_failure_paths_leave_git_and_state_bytes_unchanged(self):
        before_home = self._tree_digest(self.home)
        before_state = self.state_path.read_bytes()
        self._inspect()
        self.assertEqual(self._tree_digest(self.home), before_home)
        self.assertEqual(self.state_path.read_bytes(), before_state)

        with mock.patch.object(
            updater.release_contract,
            "authorize_release",
            side_effect=release_contract.ReleaseContractError("bad signature"),
        ):
            with self.assertRaises(release_contract.ReleaseContractError):
                updater.inspect_update(
                    self.root, self.manifest, self.signature, {}
                )
        self.assertEqual(self._tree_digest(self.home), before_home)
        self.assertEqual(self.state_path.read_bytes(), before_state)

    def test_concurrent_read_only_inspections_return_consistent_diagnostics(self):
        with mock.patch.object(
            updater.release_contract, "authorize_release", return_value=self.verified
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _item: updater.inspect_update(
                        self.root, self.manifest, self.signature, self.trusted_keys
                    ),
                    range(2),
                ))
        self.assertEqual(results[0], results[1])
        self.assertFalse(any(
            thread.name == "updater-git-output" and thread.is_alive()
            for thread in threading.enumerate()
        ))

    def test_changed_generation_on_final_recheck_fails_closed(self):
        original_run = updater._GitReader.run
        head_reads = 0

        def unstable_run(reader, operation, **kwargs):
            nonlocal head_reads
            result = original_run(reader, operation, **kwargs)
            if operation == "head":
                head_reads += 1
                if head_reads == 2:
                    return 0, ("f" * 40 + "\n").encode("ascii")
            return result

        with mock.patch.object(updater._GitReader, "run", new=unstable_run):
            with mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ):
                with self.assertRaisesRegex(updater.UpdateInspectionError, "changed"):
                    updater.inspect_update(
                        self.root, self.manifest, self.signature, self.trusted_keys
                    )

    def test_final_generation_rehashes_tracked_content(self):
        with (
            mock.patch.object(
                updater,
                "_tracked_worktree_is_dirty",
                wraps=updater._tracked_worktree_is_dirty,
            ) as tracked,
            mock.patch.object(
                updater.release_contract, "authorize_release", return_value=self.verified
            ),
        ):
            updater.inspect_update(
                self.root, self.manifest, self.signature, self.trusted_keys
            )
        self.assertEqual(tracked.call_count, 2)

    def test_live_inspection_routes_every_git_call_through_registered_operations(self):
        with mock.patch.object(
            updater, "_git_arguments", wraps=updater._git_arguments
        ) as arguments:
            self._inspect()
        operations = {call.args[0] for call in arguments.call_args_list}
        self.assertTrue(operations)
        self.assertTrue(operations.issubset(updater._GIT_OPERATION_TEMPLATES))

    def test_git_executable_candidates_are_resolved_once_per_inspection(self):
        with mock.patch.object(
            updater, "_resolve_git_executables", wraps=updater._resolve_git_executables
        ) as resolver:
            self._inspect()
        self.assertEqual(resolver.call_count, 1)

    def test_live_inspection_passes_the_scrubbed_environment_to_every_git_process(self):
        with mock.patch.object(
            updater, "_run_bounded_git", wraps=updater._run_bounded_git
        ) as run:
            self._inspect()
        self.assertGreater(run.call_count, 0)
        expected = updater._git_environment(str(Path(self.git).resolve()))
        for call in run.call_args_list:
            self.assertEqual(call.args[1], expected)

    def test_equal_signed_release_is_reported_up_to_date(self):
        current = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "release_sequence": 22,
                "version": "0.2.2",
                "tag": "v0.2.2",
                "commit": self.old_commit,
            }
        )
        self._write_state(last_manifest_sha256=release_contract.manifest_sha256(current))
        with mock.patch.object(
            updater.release_contract,
            "authorize_release",
            return_value=release_contract.VerifiedRelease(current, "test-key"),
        ):
            result = updater.inspect_update(
                self.root, current, self.signature, self.trusted_keys
            )
        self.assertEqual(result.status, "up_to_date")

    def test_currency_status_requires_the_complete_last_known_good_tuple(self):
        current = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "release_sequence": 22,
                "version": "0.2.2",
                "tag": "v0.2.2",
                "commit": self.old_commit,
            }
        )
        for state_change in (
            {"last_manifest_sha256": "f" * 64},
            {"last_version": "0.2.1"},
        ):
            with self.subTest(state_change=state_change):
                changes = {
                    "last_manifest_sha256": release_contract.manifest_sha256(current)
                }
                changes.update(state_change)
                self._write_state(**changes)
                with mock.patch.object(
                    updater.release_contract,
                    "authorize_release",
                    return_value=release_contract.VerifiedRelease(current, "test-key"),
                ):
                    with self.assertRaisesRegex(
                        updater.UpdateInspectionError, "conflicts"
                    ):
                        updater.inspect_update(
                            self.root, current, self.signature, self.trusted_keys
                        )

    def test_lower_sequence_is_rejected_even_if_an_authorizer_regresses(self):
        older = release_contract.ReleaseManifest(
            **{
                **self.manifest.__dict__,
                "release_sequence": 21,
                "version": "0.2.2",
                "tag": "v0.2.2",
                "commit": self.old_commit,
            }
        )
        with mock.patch.object(
            updater.release_contract,
            "authorize_release",
            return_value=release_contract.VerifiedRelease(older, "test-key"),
        ):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "older"):
                updater.inspect_update(self.root, older, self.signature, self.trusted_keys)

    def test_result_is_frozen_diagnostic_data_not_an_apply_capability(self):
        result, _authorize = self._inspect()
        with self.assertRaises((AttributeError, TypeError)):
            result.status = "authorized"  # type: ignore[misc]
        for name in ("apply", "authorize", "reset", "serialize", "write"):
            self.assertFalse(hasattr(result, name))


class CollisionClassificationTests(unittest.TestCase):
    def test_collision_check_uses_path_fallback_when_safe_dir_fd_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            collision = root / "collision.txt"
            collision.write_bytes(b"keep")
            native_lstat = os.lstat
            with (
                mock.patch.object(
                    updater, "_supports_safe_dir_fd", return_value=False
                ) as support,
                mock.patch.object(updater.os, "lstat", wraps=native_lstat) as inspected,
            ):
                updater._reject_collision_links(root, ("collision.txt",))
        support.assert_called_once_with()
        inspected.assert_called_once_with(collision)

    def test_collision_path_fallback_rejects_links_reparse_and_hardlinks(self):
        cases = (
            (
                mock.Mock(st_mode=stat.S_IFLNK | 0o777, st_nlink=1, st_file_attributes=0),
                "link or reparse point",
            ),
            (
                mock.Mock(st_mode=stat.S_IFDIR | 0o755, st_nlink=1, st_file_attributes=0x400),
                "link or reparse point",
            ),
            (
                mock.Mock(st_mode=stat.S_IFREG | 0o644, st_nlink=2, st_file_attributes=0),
                "multiply linked",
            ),
        )
        for info, message in cases:
            with self.subTest(message=message, mode=info.st_mode):
                with (
                    mock.patch.object(updater, "_supports_safe_dir_fd", return_value=False),
                    mock.patch.object(updater.os, "lstat", return_value=info),
                    mock.patch.object(
                        updater.stat,
                        "FILE_ATTRIBUTE_REPARSE_POINT",
                        0x400,
                        create=True,
                    ),
                ):
                    with self.assertRaisesRegex(updater.UpdateInspectionError, message):
                        updater._reject_collision_links(Path("root"), ("collision",))

    def test_collision_path_fallback_stops_at_non_directory_ancestor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ancestor").write_bytes(b"file")
            with mock.patch.object(updater, "_supports_safe_dir_fd", return_value=False):
                updater._reject_collision_links(root, ("ancestor/child.txt",))

    @unittest.skipUnless(
        updater._supports_safe_dir_fd(), "safe dir_fd traversal requires POSIX support"
    )
    def test_collision_check_uses_nofollow_dir_fd_when_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "parent").mkdir()
            (root / "parent" / "collision.txt").write_bytes(b"keep")
            native_open = os.open
            with mock.patch.object(updater.os, "open", wraps=native_open) as opened:
                updater._reject_collision_links(root, ("parent/collision.txt",))
        self.assertTrue(any("dir_fd" in call.kwargs for call in opened.call_args_list))
        directory_flags = os.O_NOFOLLOW | os.O_DIRECTORY
        self.assertTrue(
            any(call.args[1] & directory_flags == directory_flags for call in opened.call_args_list)
        )

    def test_protected_targets_are_derived_from_runtime_contract_constants(self):
        self.assertEqual(updater._CONFIG_FILENAME, "config.json")
        self.assertIn(managed_install.MARKER_FILENAME.casefold(), updater._PROTECTED_TARGETS)
        self.assertIn(
            updater.UPDATE_STATE_RELATIVE_PATH.parts[0].casefold(),
            updater._PROTECTED_TARGETS,
        )

    def test_exact_ancestry_case_unicode_and_ignored_collisions(self):
        target = ("exact.txt", "parent", "deep/child.txt", "case.txt", "caf\u00e9.txt")
        unknown = ("exact.txt", "parent/file.txt", "deep", "CASE.TXT", "cafe\u0301.txt")
        collisions = updater._classify_collisions(
            target, unknown, fold_case=True, normalize_unicode=True
        )
        self.assertEqual(
            collisions,
            ("CASE.TXT", "cafe\u0301.txt", "deep", "exact.txt", "parent/file.txt"),
        )

    def test_case_variants_are_distinct_on_case_sensitive_filesystems(self):
        self.assertEqual(
            updater._classify_collisions(
                ("case.txt",), ("CASE.TXT",), fold_case=False, normalize_unicode=False
            ),
            (),
        )

    def test_case_folding_and_unicode_normalization_are_independent(self):
        decomposed = "cafe\u0301.txt"
        composed = "caf\u00e9.txt"
        self.assertEqual(
            updater._classify_collisions(
                (composed,), (decomposed,), fold_case=False, normalize_unicode=True
            ),
            (decomposed,),
        )
        self.assertEqual(
            updater._classify_collisions(
                (composed,), (decomposed,), fold_case=True, normalize_unicode=False
            ),
            (),
        )

    def test_hostile_git_paths_fail_closed_before_filesystem_use(self):
        for path in (
            "", ".", "../outside", "/absolute", "C:/absolute", ".git/config",
            "trailing.", "trailing ", "NUL.txt", "dir/COM1", "stream:name",
            "line\nbreak", "carriage\rreturn", "escape\x1bname",
            "dir/.git/config", ".g\u200cit/config", "CONFIG~1.JSO",
            "COM\u00b9.txt", "LPT\u00b2.txt",
        ):
            with self.subTest(path=path):
                with self.assertRaises(updater.UpdateInspectionError):
                    updater._validate_relative_git_path(path)

    def test_legal_posix_only_local_names_are_not_treated_as_target_paths(self):
        for path in (
            "NUL.txt", "notes:draft.txt", "back\\slash.txt", "trailing.",
            "line\nbreak",
        ):
            with self.subTest(path=path):
                self.assertTrue(updater._validate_relative_git_path(path, portable=False))
                with self.assertRaises(updater.UpdateInspectionError):
                    updater._validate_relative_git_path(path, portable=True)

    def test_target_identity_collisions_fail_closed(self):
        for paths in (
            ("Case.txt", "case.txt"),
            ("caf\u00e9.txt", "cafe\u0301.txt"),
            ("ab.txt", "a\u200cb.txt"),
            ("parent", "parent/child.txt"),
        ):
            with self.subTest(paths=paths):
                with self.assertRaises(updater.UpdateInspectionError):
                    updater._validate_target_identities(paths)

    def test_hfs_ignorable_unknown_name_collides_with_target(self):
        self.assertEqual(
            updater._classify_collisions(
                ("ab.txt",),
                ("a\u200cb.txt",),
                fold_case=True,
                normalize_unicode=True,
            ),
            ("a\u200cb.txt",),
        )

    def test_filesystem_unknown_walk_sees_case_alias_omitted_by_index_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Foo").write_bytes(b"tracked")
            (root / "foo").write_bytes(b"unknown")
            if os.path.samefile(root / "Foo", root / "foo"):
                self.skipTest("case-distinct filesystem entries are unavailable")
            index = (
                updater._GitIndexEntry("H", "100644", "0" * 40, 0, "Foo"),
            )
            self.assertEqual(
                updater._filesystem_unknown_paths(root, index),
                ("foo",),
            )

    def test_local_filesystem_path_key_matches_platform_case_semantics(self):
        left = updater._local_filesystem_path_key("Folder/File.txt")
        right = updater._local_filesystem_path_key("folder/file.TXT")
        if os.name == "nt":
            self.assertEqual(left, right)
        else:
            self.assertNotEqual(left, right)
        with mock.patch.object(updater.os, "name", "posix"), mock.patch.object(
            updater.sys, "platform", "darwin"
        ):
            self.assertNotEqual(
                updater._local_filesystem_path_key("Folder/File.txt"),
                updater._local_filesystem_path_key("folder/file.TXT"),
            )
            self.assertNotEqual(
                updater._local_filesystem_path_key("caf\u00e9.txt"),
                updater._local_filesystem_path_key("cafe\u0301.txt"),
            )

    def test_sorted_tracked_paths_find_only_real_descendants(self):
        paths = tuple(
            sorted(
                updater._local_filesystem_path_key(path)
                for path in ("deep/child.txt", "deeply/other.txt", "root.txt")
            )
        )
        self.assertTrue(
            updater._sorted_paths_have_descendant(
                paths, updater._local_filesystem_path_key("deep")
            )
        )
        self.assertFalse(
            updater._sorted_paths_have_descendant(
                paths, updater._local_filesystem_path_key("dee")
            )
        )
        self.assertFalse(
            updater._sorted_paths_have_descendant(
                paths, updater._local_filesystem_path_key("root.txt")
            )
        )
        with mock.patch.object(updater.os, "name", "nt"):
            self.assertTrue(
                updater._sorted_paths_have_descendant(
                    (r"deep\child.txt", r"deeply\other.txt"), "deep"
                )
            )
            self.assertFalse(
                updater._sorted_paths_have_descendant(
                    (r"deeply\other.txt",), "deep"
                )
            )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO entries require POSIX")
    def test_filesystem_unknown_walk_sees_special_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            os.mkfifo(root / "pipe")
            self.assertEqual(
                updater._filesystem_unknown_paths(root, ()),
                ("pipe",),
            )

    def test_filesystem_unknown_walk_descends_into_tracked_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nested = root / "deep"
            nested.mkdir()
            (nested / "child.txt").write_bytes(b"tracked")
            (nested / "unknown.txt").write_bytes(b"unknown")
            index = (
                updater._GitIndexEntry(
                    "H", "100644", "0" * 40, 0, "deep/child.txt"
                ),
            )
            self.assertEqual(
                updater._filesystem_unknown_paths(root, index),
                ("deep/unknown.txt",),
            )

    def test_case_distinct_dot_git_name_follows_local_filesystem_semantics(self):
        payload = b".GIT/local.txt\x00"
        if updater._local_filesystem_path_key(".GIT") == updater._local_filesystem_path_key(
            ".git"
        ):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "protected"):
                updater._parse_nul_paths(payload, portable=False)
        else:
            self.assertEqual(
                updater._parse_nul_paths(payload, portable=False),
                (".GIT/local.txt",),
            )

    def test_regular_file_symlink_checkout_requires_explicit_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            body = b"nested/target.txt"
            (root / "link").write_bytes(body)
            object_id = hashlib.sha1(b"blob %d\x00" % len(body) + body).hexdigest()
            entry = updater._GitIndexEntry("H", "120000", object_id, 0, "link")
            self.assertEqual(
                updater._working_tree_blob_ids(
                    root,
                    entry,
                    allow_regular_symlink_file=True,
                    remaining_bytes=1024,
                    deadline=time.monotonic() + 5,
                )[0],
                (object_id,),
            )
            self.assertFalse(
                updater._working_tree_blob_ids(
                    root,
                    entry,
                    allow_regular_symlink_file=False,
                    remaining_bytes=1024,
                    deadline=time.monotonic() + 5,
                )[2]
            )

    def test_windows_symlink_target_bytes_use_git_separators(self):
        with mock.patch.object(updater.os, "name", "nt"):
            self.assertEqual(
                updater._git_symlink_target_bytes(r"nested\target.txt"),
                b"nested/target.txt",
            )

    def test_nul_path_and_index_parsers_reject_truncation_and_duplicates(self):
        for payload in (b"unterminated", b"a\x00a\x00", b"\x00"):
            with self.subTest(payload=payload):
                with self.assertRaises(updater.UpdateInspectionError):
                    updater._parse_nul_paths(payload, portable=False)
        for payload in (
            b"unterminated",
            b"H path\x00",
            b"H 100644 short 0\tpath\x00",
        ):
            with self.subTest(index_payload=payload):
                with self.assertRaises(updater.UpdateInspectionError):
                    updater._parse_index_entries(payload)
        record = b"H 100644 " + (b"a" * 40) + b" 0\tpath\x00"
        with self.assertRaisesRegex(updater.UpdateInspectionError, "duplicate"):
            updater._parse_index_entries(record + record)
        conflict = (
            b"H 100644 " + (b"a" * 40) + b" 1\tpath\x00"
            b"H 100644 " + (b"b" * 40) + b" 2\tpath\x00"
        )
        self.assertEqual(len(updater._parse_index_entries(conflict)), 2)
        mixed = record + b"H 100644 " + (b"b" * 40) + b" 1\tpath\x00"
        with self.assertRaisesRegex(updater.UpdateInspectionError, "stages"):
            updater._parse_index_entries(mixed)

    def test_tracked_file_replaced_by_directory_is_an_unknown_collision_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            replaced = root / "tracked.txt"
            replaced.mkdir()
            (replaced / "nested.txt").write_bytes(b"unknown")
            index = (
                updater._GitIndexEntry(
                    "H", "100644", "0" * 40, 0, "tracked.txt"
                ),
            )
            self.assertEqual(
                updater._filesystem_unknown_paths(root, index),
                ("tracked.txt",),
            )

    def test_path_complexity_limits_fail_closed(self):
        deep = "/".join("d" for _ in range(updater._MAX_PATH_COMPONENTS + 1))
        long_name = "x" * (updater._MAX_PATH_CHARACTERS + 1)
        for path in (deep, long_name):
            with self.subTest(path_length=len(path)):
                with self.assertRaisesRegex(updater.UpdateInspectionError, "complexity"):
                    updater._validate_relative_git_path(path)

    def test_collision_paths_reject_links_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "target.txt"
            target.write_text("data", encoding="utf-8")
            link = root / "link.txt"
            try:
                link.symlink_to(target)
            except OSError:
                link = None
            if link is not None:
                with self.assertRaisesRegex(updater.UpdateInspectionError, "link|reparse"):
                    updater._reject_collision_links(root, ("link.txt",))

            alias = root / "alias.txt"
            try:
                os.link(target, alias)
            except OSError:
                alias = None
            if alias is not None:
                with self.assertRaisesRegex(updater.UpdateInspectionError, "multiply linked"):
                    updater._reject_collision_links(root, ("target.txt",))

    def test_crlf_change_is_dirty_without_eol_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            body = b"first\nsecond\n"
            (root / "text.txt").write_bytes(body.replace(b"\n", b"\r\n"))
            object_id = hashlib.sha1(b"blob %d\x00" % len(body) + body).hexdigest()
            entry = updater._GitIndexEntry("H", "100644", object_id, 0, "text.txt")
            digests, consumed, stable = updater._working_tree_blob_ids(
                root,
                entry,
                remaining_bytes=1024,
                deadline=time.monotonic() + 5,
            )
        self.assertTrue(stable)
        self.assertGreater(consumed, 0)
        self.assertNotIn(object_id, digests)

    def test_crlf_checkout_matches_lf_blob_only_with_eol_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            body = b"first\nsecond\n"
            path = root / "text.txt"
            path.write_bytes(body.replace(b"\n", b"\r\n"))
            info = path.stat()
            object_id = hashlib.sha1(b"blob %d\x00" % len(body) + body).hexdigest()
            entry = updater._GitIndexEntry("H", "100644", object_id, 0, "text.txt")
            digests, consumed, stable = updater._working_tree_blob_ids(
                root,
                entry,
                allow_crlf_normalization=True,
                remaining_bytes=1024,
                deadline=time.monotonic() + 5,
            )
        self.assertTrue(stable)
        self.assertEqual(consumed, info.st_size * 2)
        self.assertIn(object_id, digests)

    def test_tracked_hash_reads_only_the_approved_file_size(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "text.txt"
            body = b"content"
            path.write_bytes(body)
            object_id = hashlib.sha1(b"blob 7\x00content").hexdigest()
            entry = updater._GitIndexEntry("H", "100644", object_id, 0, "text.txt")
            original_fdopen = os.fdopen

            class ExactReadHandle:
                def __init__(self, handle):
                    self.handle = handle
                    self.reads = 0

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.handle.close()

                def fileno(self):
                    return self.handle.fileno()

                def read(self, size):
                    self.reads += 1
                    if self.reads > 1:
                        raise AssertionError("hashing read beyond the approved file size")
                    return self.handle.read(size)

                def seek(self, *args):
                    return self.handle.seek(*args)

            def exact_fdopen(fd, mode):
                return ExactReadHandle(original_fdopen(fd, mode))

            with mock.patch.object(updater.os, "fdopen", side_effect=exact_fdopen):
                digests, consumed, stable = updater._working_tree_blob_ids(
                    root,
                    entry,
                    remaining_bytes=1024,
                    deadline=time.monotonic() + 5,
                )
        self.assertTrue(stable)
        self.assertEqual(consumed, len(body))
        self.assertIn(object_id, digests)

    def test_tracked_hash_uses_nonblocking_open_when_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "text.txt").write_bytes(b"content")
            object_id = hashlib.sha1(b"blob 7\x00content").hexdigest()
            entry = updater._GitIndexEntry("H", "100644", object_id, 0, "text.txt")
            original_open = os.open
            observed_flags = []

            def recording_open(path, flags, *args, **kwargs):
                observed_flags.append(flags)
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(updater.os, "open", side_effect=recording_open):
                updater._working_tree_blob_ids(
                    root,
                    entry,
                    remaining_bytes=1024,
                    deadline=time.monotonic() + 5,
                )
        self.assertTrue(observed_flags)
        nonblocking = getattr(os, "O_NONBLOCK", 0)
        if nonblocking:
            self.assertTrue(observed_flags[0] & nonblocking)

    def test_index_eol_parser_is_strict_and_preserves_conversion_authority(self):
        output = b"i/lf    w/crlf  attr/                  \ttext.txt\x00"
        self.assertEqual(
            updater._parse_index_eol(output),
            {
                "text.txt": updater._GitEolInfo(
                    index="lf", worktree="crlf", attribute=""
                )
            },
        )
        self.assertEqual(
            updater._parse_index_eol(
                b"i/      w/      attr/                  \tlink.txt\x00"
            ),
            {
                "link.txt": updater._GitEolInfo(
                    index="", worktree="", attribute=""
                )
            },
        )
        self.assertFalse(
            updater._crlf_conversion_authorized(
                updater._GitEolInfo(
                    index="crlf", worktree="crlf", attribute=""
                ),
                "true",
                "unset",
            )
        )
        self.assertFalse(
            updater._crlf_conversion_authorized(
                updater._GitEolInfo(index="lf", worktree="mixed", attribute="text"),
                "false",
                "unset",
            )
        )
        text_crlf = updater._GitEolInfo(
            index="lf", worktree="crlf", attribute="text"
        )
        self.assertFalse(
            updater._crlf_conversion_authorized(text_crlf, "false", "lf")
        )
        self.assertTrue(
            updater._crlf_conversion_authorized(text_crlf, "true", "lf")
        )
        self.assertTrue(
            updater._crlf_conversion_authorized(text_crlf, "false", "crlf")
        )
        self.assertFalse(
            updater._crlf_conversion_authorized(text_crlf, "input", "crlf")
        )
        self.assertTrue(
            updater._crlf_conversion_authorized(
                updater._GitEolInfo(
                    index="lf", worktree="crlf", attribute="text eol=crlf"
                ),
                "input",
                "lf",
            )
        )
        for attribute in ("-text", "text eol=lf"):
            with self.subTest(attribute=attribute):
                self.assertFalse(
                    updater._crlf_conversion_authorized(
                        updater._GitEolInfo(
                            index="lf", worktree="crlf", attribute=attribute
                        ),
                        "true",
                        "crlf",
                    )
                )
        no_attribute = updater._GitEolInfo(
            index="lf", worktree="crlf", attribute=""
        )
        self.assertTrue(
            updater._crlf_conversion_authorized(no_attribute, "true", "lf")
        )
        self.assertFalse(
            updater._crlf_conversion_authorized(no_attribute, "false", "crlf")
        )
        with mock.patch.object(updater.os, "name", "nt"):
            self.assertTrue(
                updater._crlf_conversion_authorized(text_crlf, "false", "native")
            )
            self.assertTrue(
                updater._crlf_conversion_authorized(text_crlf, "false", "unset")
            )
        with mock.patch.object(updater.os, "name", "posix"):
            self.assertFalse(
                updater._crlf_conversion_authorized(text_crlf, "false", "unset")
            )
        with self.assertRaisesRegex(updater.UpdateInspectionError, "index-eol"):
            updater._parse_index_eol(output.replace(b"w/crlf", b"broken"))

    def test_tracked_file_size_budget_is_conservatively_dirty(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "large.txt"
            path.write_bytes(b"content")
            object_id = hashlib.sha1(b"blob 7\x00content").hexdigest()
            entry = updater._GitIndexEntry("H", "100644", object_id, 0, "large.txt")
            with mock.patch.object(updater, "_MAX_TRACKED_FILE_BYTES", 1):
                self.assertFalse(
                    updater._working_tree_blob_ids(
                        root, entry, remaining_bytes=1024, deadline=time.monotonic() + 5
                    )[2]
                )
            with self.assertRaisesRegex(updater.UpdateInspectionError, "time budget"):
                updater._working_tree_blob_ids(
                    root, entry, remaining_bytes=1024, deadline=time.monotonic() - 1
                )

    def test_tracked_file_hash_aggregate_budget_is_conservatively_dirty(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entries = []
            eol_info = {}
            for name in ("first.txt", "second.txt"):
                body = b"content"
                (root / name).write_bytes(body)
                object_id = hashlib.sha1(b"blob 7\x00content").hexdigest()
                entries.append(
                    updater._GitIndexEntry("H", "100644", object_id, 0, name)
                )
                eol_info[name] = updater._GitEolInfo("lf", "lf", "")
            tree_entries = tuple(
                updater._GitTreeEntry(entry.mode, entry.object_id, entry.path)
                for entry in entries
            )
            with mock.patch.object(updater, "_MAX_TRACKED_TOTAL_BYTES", 10):
                self.assertTrue(
                    updater._tracked_worktree_is_dirty(
                        root,
                        tree_entries,
                        tuple(entries),
                        eol_info,
                        "false",
                        "unset",
                        "false",
                        deadline=time.monotonic() + 5,
                    )
                )

    def test_large_collection_helpers_honor_an_expired_overall_deadline(self):
        expired = time.monotonic() - 1
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checks = (
                lambda: updater._tracked_files_generation(
                    root, (), deadline=expired
                ),
                lambda: updater._filesystem_unknown_paths(
                    root, (), deadline=expired
                ),
                lambda: updater._tracked_worktree_is_dirty(
                    root, (), (), {}, "false", "unset", "false", deadline=expired
                ),
                lambda: updater._classify_collisions(
                    (),
                    (),
                    fold_case=True,
                    normalize_unicode=True,
                    deadline=expired,
                ),
                lambda: updater._target_has_protected_path((), deadline=expired),
                lambda: updater._validate_target_identities((), deadline=expired),
                lambda: updater._reject_collision_links(root, (), deadline=expired),
            )
            for check in checks:
                with self.subTest(check=check):
                    with self.assertRaisesRegex(
                        updater.UpdateInspectionError, "overall deadline"
                    ):
                        check()

    def test_large_collection_helpers_check_deadline_between_entries(self):
        first = updater._GitIndexEntry(
            "H", "100644", "a" * 40, 0, "first/tracked.txt"
        )
        second = updater._GitIndexEntry(
            "H", "100644", "b" * 40, 0, "second/tracked.txt"
        )
        entries = (first, second)
        tree = tuple(
            updater._GitTreeEntry(entry.mode, entry.object_id, entry.path)
            for entry in entries
        )
        eol_info = {
            entry.path: updater._GitEolInfo("lf", "lf", "")
            for entry in entries
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            generation_clock = {"expired": False}

            def finish_first_stat(_path):
                generation_clock["expired"] = True
                return os.stat_result(
                    (stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 0, 0, 0, 0)
                )

            with mock.patch.object(
                updater.time,
                "monotonic",
                side_effect=lambda: 2.0 if generation_clock["expired"] else 0.0,
            ), mock.patch.object(
                updater.os, "lstat", side_effect=finish_first_stat
            ) as lstat_path:
                with self.assertRaisesRegex(
                    updater.UpdateInspectionError, "overall deadline"
                ):
                    updater._tracked_files_generation(
                        root, entries, deadline=1.0
                    )
            self.assertEqual(lstat_path.call_count, 1)

            hash_clock = {"expired": False}

            def finish_first_hash(_root, entry, **_kwargs):
                hash_clock["expired"] = True
                return ((entry.object_id,), 0, True)

            with mock.patch.object(
                updater.time,
                "monotonic",
                side_effect=lambda: 2.0 if hash_clock["expired"] else 0.0,
            ), mock.patch.object(
                updater,
                "_working_tree_blob_ids",
                side_effect=finish_first_hash,
            ) as hash_blob:
                with self.assertRaisesRegex(
                    updater.UpdateInspectionError, "overall deadline"
                ):
                    updater._tracked_worktree_is_dirty(
                        root,
                        tree,
                        entries,
                        eol_info,
                        "false",
                        "unset",
                        "false",
                        deadline=1.0,
                    )
            self.assertEqual(hash_blob.call_count, 1)

    def test_large_collection_helpers_check_deadline_while_collecting_iterables(self):
        first = updater._GitIndexEntry(
            "H", "100644", "a" * 40, 0, "first.txt"
        )

        class ExplodingEntry:
            def __getattribute__(self, name):
                raise AssertionError(f"expired entry field was accessed: {name}")

        def expire_before_second(clock):
            yield first
            clock["expired"] = True
            yield ExplodingEntry()

        helpers = (
            lambda root, entries: updater._tracked_files_generation(
                root, entries, deadline=1.0
            ),
            lambda root, entries: updater._filesystem_unknown_paths(
                root, entries, deadline=1.0
            ),
            lambda root, entries: updater._tracked_worktree_is_dirty(
                root, entries, (), {}, "false", "unset", "false", deadline=1.0
            ),
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for helper in helpers:
                clock = {"expired": False}
                with self.subTest(helper=helper), mock.patch.object(
                    updater.time,
                    "monotonic",
                    side_effect=lambda: 2.0 if clock["expired"] else 0.0,
                ):
                    with self.assertRaisesRegex(
                        updater.UpdateInspectionError, "overall deadline"
                    ):
                        helper(root, expire_before_second(clock))

            index_clock = {"expired": False}
            tree = (
                updater._GitTreeEntry(first.mode, first.object_id, first.path),
                updater._GitTreeEntry("100644", "b" * 40, "second.txt"),
            )
            with mock.patch.object(
                updater.time,
                "monotonic",
                side_effect=lambda: 2.0 if index_clock["expired"] else 0.0,
            ):
                with self.assertRaisesRegex(
                    updater.UpdateInspectionError, "overall deadline"
                ):
                    updater._tracked_worktree_is_dirty(
                        root,
                        tree,
                        expire_before_second(index_clock),
                        {},
                        "false",
                        "unset",
                        "false",
                        deadline=1.0,
                    )

    def test_filesystem_walk_checks_deadline_between_directory_entries(self):
        clock = {"expired": False}
        first = mock.Mock()
        first.name = "first.txt"
        first.path = "first.txt"
        first.stat.return_value = os.stat_result(
            (stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 0, 0, 0, 0)
        )

        class ExplodingDirectoryEntry:
            def __getattribute__(self, name):
                raise AssertionError(f"expired directory entry was accessed: {name}")

        def directory_entries():
            yield first
            clock["expired"] = True
            yield ExplodingDirectoryEntry()

        scan = mock.MagicMock()
        scan.__enter__.return_value = directory_entries()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            updater.time,
            "monotonic",
            side_effect=lambda: 2.0 if clock["expired"] else 0.0,
        ), mock.patch.object(updater.os, "scandir", return_value=scan):
            with self.assertRaisesRegex(
                updater.UpdateInspectionError, "overall deadline"
            ):
                updater._filesystem_unknown_paths(
                    Path(folder), (), deadline=1.0
                )


class GitCommandPolicyTests(unittest.TestCase):
    def test_dir_fd_path_requires_open_and_stat_support(self):
        both = {updater.os.open, updater.os.stat}
        cases = (
            ("nt", both, {updater.os.stat}, 1, 2, False),
            ("posix", set(), {updater.os.stat}, 1, 2, False),
            ("posix", {updater.os.open}, {updater.os.stat}, 1, 2, False),
            ("posix", {updater.os.stat}, {updater.os.stat}, 1, 2, False),
            ("posix", both, set(), 1, 2, False),
            ("posix", both, {updater.os.stat}, 0, 2, False),
            ("posix", both, {updater.os.stat}, 1, 0, False),
            ("posix", both, {updater.os.stat}, 1, 2, True),
        )
        for name, dir_fd, follow_symlinks, nofollow, directory, expected in cases:
            with self.subTest(
                name=name,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
                nofollow=nofollow,
                directory=directory,
            ):
                with (
                    mock.patch.object(updater.os, "name", name),
                    mock.patch.object(updater.os, "supports_dir_fd", dir_fd),
                    mock.patch.object(
                        updater.os, "supports_follow_symlinks", follow_symlinks
                    ),
                    mock.patch.object(updater.os, "O_NOFOLLOW", nofollow, create=True),
                    mock.patch.object(updater.os, "O_DIRECTORY", directory, create=True),
                ):
                    self.assertEqual(updater._supports_safe_dir_fd(), expected)

    def test_only_literal_read_only_git_operations_are_registered(self):
        self.assertEqual(updater._GIT_OPERATION_TEMPLATES, {
            "autocrlf": (
                "config", "--local", "--no-includes", "--get", "core.autocrlf"
            ),
            "eol": ("config", "--local", "--no-includes", "--get", "core.eol"),
            "head": ("rev-parse", "--verify", "HEAD^{commit}"),
            "head_tree": ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
            "index": ("ls-files", "--stage", "-v", "-z"),
            "index_eol": ("ls-files", "--eol", "-z"),
            "remotes": (
                "config", "-z", "--local", "--no-includes", "--get-regexp",
                r"^remote\..*\.url$",
            ),
            "target": ("rev-parse", "--verify", "refs/tags/{tag}^{commit}"),
            "target_tree": ("ls-tree", "-r", "-z", "--full-tree", "{commit}"),
            "trust_store": ("show", "{commit}:installer/release-trust.json"),
            "top_level": ("rev-parse", "--show-toplevel"),
            "symlinks": (
                "config", "--local", "--no-includes", "--get", "core.symlinks"
            ),
            "unsafe_config": (
                "config", "-z", "--local", "--no-includes", "--name-only", "--list",
            ),
            "unknown": (
                "ls-files", "-z", "--others", "--directory", "--no-empty-directory"
            ),
            "version": ("--version",),
        })

    def test_every_git_command_disables_lazy_fetch_and_optional_writes(self):
        argv = updater._git_arguments(
            "target",
            Path("C:/managed-root"),
            git_executable="C:/Program Files/Git/cmd/git.exe",
            tag="v1.2.3",
        )
        self.assertIn("--no-lazy-fetch", argv)
        self.assertIn("--no-optional-locks", argv)
        self.assertIn("--no-replace-objects", argv)
        self.assertIn("--no-pager", argv)
        self.assertEqual(
            argv[-3:], ("rev-parse", "--verify", "refs/tags/v1.2.3^{commit}")
        )
        top_level = updater._git_arguments(
            "top_level",
            Path("C:/managed-root"),
            git_executable="C:/Program Files/Git/cmd/git.exe",
        )
        self.assertFalse(any(item.startswith("core.worktree=") for item in top_level))
        version = updater._git_arguments(
            "version",
            Path("C:/managed-root"),
            git_executable="C:/Program Files/Git/cmd/git.exe",
        )
        self.assertNotIn("--no-lazy-fetch", version)

    def test_git_environment_scrubs_redirects_and_disables_optional_writes(self):
        poisoned = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "C:/attacker/alternate",
            "GIT_CEILING_DIRECTORIES": "C:/attacker",
            "GIT_COMMON_DIR": "C:/attacker/common",
            "GIT_CONFIG": "C:/attacker/config",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_GLOBAL": "C:/attacker/global",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_SYSTEM": "C:/attacker/system",
            "GIT_CONFIG_VALUE_0": "hostile-command",
            "GIT_DIR": "C:/attacker/repo",
            "GIT_EXEC_PATH": "C:/attacker/bin",
            "GIT_EXTERNAL_DIFF": "hostile-command",
            "GIT_INDEX_FILE": "C:/attacker/index",
            "GIT_NAMESPACE": "attacker",
            "GIT_OBJECT_DIRECTORY": "C:/attacker/objects",
            "GIT_PAGER": "hostile-command",
            "GIT_SSH": "hostile-command",
            "GIT_SSH_COMMAND": "hostile-command",
            "GIT_WORK_TREE": "C:/attacker/tree",
            "HOME": "C:/attacker/home",
            "PATH": "C:/attacker/bin",
            "SYSTEMROOT": "C:/attacker/windows",
            "WINDIR": "C:/attacker/windows",
        }
        with mock.patch.dict(
            os.environ,
            poisoned,
            clear=False,
        ):
            environment = updater._git_environment()
        allowed_git = {
            "GIT_ATTR_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
            "GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS", "GIT_OPTIONAL_LOCKS", "GIT_PAGER",
            "GIT_TERMINAL_PROMPT",
        }
        self.assertTrue({key for key in environment if key.startswith("GIT_")} <= allowed_git)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_ambient_git_environment_preserves_credential_resolution(self):
        # The exact inverse property from _git_environment above: this variant
        # exists specifically so a real HTTPS credential helper / SSH agent
        # CAN be found (confirmed live against github.com during development
        # — the hardened _git_environment's stripped HOME/GIT_CONFIG_GLOBAL
        # made a private-repo clone fail with "could not read Username").
        # HOME/PATH/SSH_AUTH_SOCK must survive untouched; only GIT_*
        # variables are stripped (anti-injection), and it still forces
        # non-interactive, fail-fast behavior.
        ambient = {
            "HOME": "/Users/real-user",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
            "GIT_TERMINAL_PROMPT": "1",
            "GIT_SSH_COMMAND": "hostile-command",
            "SSH_ASKPASS": "some-gui-prompt",
        }
        with mock.patch.dict(os.environ, ambient, clear=False):
            environment = updater._ambient_git_environment()
        self.assertEqual(environment["HOME"], "/Users/real-user")
        self.assertEqual(environment["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertEqual(environment["SSH_AUTH_SOCK"], "/tmp/ssh-agent.sock")
        self.assertNotIn("SSH_ASKPASS", environment)
        # Every GIT_* var is stripped from the ambient copy first (anti-
        # injection) — the poisoned GIT_SSH_COMMAND above must not survive —
        # then only these are explicitly (re)set.
        self.assertEqual(
            {key for key in environment if key.startswith("GIT_")},
            {"GIT_TERMINAL_PROMPT", "GIT_NO_REPLACE_OBJECTS", "GIT_OPTIONAL_LOCKS", "GIT_PAGER"},
        )
        # Fail fast rather than hang: every caller invokes git from a Python
        # subprocess, never a terminal a human is watching.
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["SSH_ASKPASS_REQUIRE"], "never")
        self.assertNotIn("attacker", environment["PATH"].casefold())
        self.assertNotIn("attacker", environment["HOME"].casefold())
        if os.name == "nt":
            self.assertEqual(Path(environment["SYSTEMROOT"]), updater._windows_system_root())

    def test_windows_git_candidates_ignore_program_files_environment_poisoning(self):
        if os.name != "nt":
            self.skipTest("Windows registry-backed candidate resolution")
        with mock.patch.dict(
            os.environ,
            {"ProgramFiles": r"C:\attacker", "ProgramW6432": r"C:\attacker"},
            clear=False,
        ):
            candidates = updater._trusted_git_candidates()
        self.assertTrue(candidates)
        self.assertFalse(any("attacker" in str(path).casefold() for path in candidates))

    def test_git_executable_must_resolve_to_an_absolute_file(self):
        with mock.patch.object(
            updater, "_trusted_git_candidates", return_value=(Path("git"),)
        ):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "trusted system"):
                updater._resolve_git_executable()

    def test_git_executable_generation_includes_content_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "git.bin"
            executable.write_bytes(b"first")
            first_info = executable.stat()
            first = updater._git_executable_generation(str(executable))
            executable.write_bytes(b"other")
            os.utime(
                executable,
                ns=(first_info.st_atime_ns, first_info.st_mtime_ns),
            )
            second = updater._git_executable_generation(str(executable))
            self.assertNotEqual(first, second)

    def test_git_executable_generation_enforces_size_and_deadline_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "git"
            executable.write_bytes(b"git")
            with mock.patch.object(updater, "_MAX_GIT_EXECUTABLE_BYTES", 1):
                with self.assertRaisesRegex(updater.UpdateInspectionError, "size limit"):
                    updater._git_executable_generation(str(executable))
            with self.assertRaisesRegex(updater.UpdateInspectionError, "overall deadline"):
                updater._git_executable_generation(
                    str(executable), deadline=time.monotonic() - 1
                )

    def test_git_reader_rejects_executable_generation_changes_before_and_after_run(self):
        reader = updater._GitReader.__new__(updater._GitReader)
        reader.root = Path.cwd()
        reader.git_executable = "trusted-git"
        reader.git_generation = ("original",)
        reader.environment = {}
        reader.deadline = time.monotonic() + 5

        with mock.patch.object(
            updater, "_git_executable_generation", return_value=("changed",)
        ):
            with mock.patch.object(updater, "_run_bounded_git") as run:
                with self.assertRaisesRegex(updater.UpdateInspectionError, "changed"):
                    reader.run("version")
        run.assert_not_called()

        with mock.patch.object(
            updater,
            "_git_executable_generation",
            side_effect=[("original",), ("changed",)],
        ):
            with mock.patch.object(
                updater,
                "_run_bounded_git",
                return_value=(0, b"git version 2.51.2\n"),
            ):
                with self.assertRaisesRegex(updater.UpdateInspectionError, "changed"):
                    reader.run("version")

    def test_git_version_requirement_is_explicit_and_actionable(self):
        self.assertEqual(
            updater._require_supported_git_version(b"git version 2.51.2.windows.1\n"),
            (2, 51, 2),
        )
        self.assertEqual(
            updater._require_supported_git_version(
                b"git version 2.51.2 (Apple Git-154)\n"
            ),
            (2, 51, 2),
        )
        with self.assertRaisesRegex(updater.UpdateInspectionError, "2.45 or newer"):
            updater._require_supported_git_version(b"git version 2.44.4\n")

        with mock.patch.object(updater, "_trusted_git_candidates", return_value=()):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "not installed"):
                updater._resolve_git_executable()

    def test_missing_local_autocrlf_is_explicitly_unset(self):
        reader = mock.Mock()
        reader.run.return_value = (1, b"")
        self.assertEqual(updater._read_autocrlf(reader), "unset")
        reader.run.assert_called_once_with("autocrlf", allowed=(0, 1))

    def test_local_symlink_checkout_policy_is_strict(self):
        reader = mock.Mock()
        reader.run.side_effect = ((0, b"false\n"), (1, b""), (0, b"maybe\n"))
        self.assertEqual(updater._read_symlinks(reader), "false")
        self.assertEqual(updater._read_symlinks(reader), "unset")
        with self.assertRaisesRegex(updater.UpdateInspectionError, "core.symlinks"):
            updater._read_symlinks(reader)

    def test_local_eol_policy_and_windows_pin_precondition_are_strict(self):
        reader = mock.Mock()
        reader.run.side_effect = ((0, b"crlf\n"), (1, b""), (0, b"maybe\n"))
        self.assertEqual(updater._read_eol(reader), "crlf")
        self.assertEqual(updater._read_eol(reader), "unset")
        with self.assertRaisesRegex(updater.UpdateInspectionError, "core.eol"):
            updater._read_eol(reader)
        with mock.patch.object(updater.os, "name", "nt"):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "must pin"):
                updater._validate_local_checkout_policy("unset", "false")
            with self.assertRaisesRegex(updater.UpdateInspectionError, "must pin"):
                updater._validate_local_checkout_policy("false", "unset")
            updater._validate_local_checkout_policy("false", "false")

    def test_unsupported_git_version_stops_before_any_repository_operation(self):
        with mock.patch.object(updater, "_resolve_git_executables", return_value=("git",)):
            with mock.patch.object(
                updater, "_git_executable_generation", return_value=("stable",)
            ):
                with mock.patch.object(
                    updater,
                    "_run_bounded_git",
                    return_value=(0, b"git version 2.44.4\n"),
                ) as run:
                    with self.assertRaisesRegex(
                        updater.UpdateInspectionError, "2.45 or newer"
                    ):
                        updater._GitReader(Path.cwd())
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("--no-lazy-fetch", run.call_args.args[0])

    def test_supported_trusted_git_candidate_is_used_after_an_older_one(self):
        def version_for(argv, _environment, **_kwargs):
            version = b"2.44.4" if argv[0] == "old-git" else b"2.51.2"
            return 0, b"git version " + version + b"\n"

        with mock.patch.object(
            updater,
            "_resolve_git_executables",
            return_value=("old-git", "supported-git"),
        ):
            with mock.patch.object(
                updater, "_git_executable_generation", return_value=("stable",)
            ):
                with mock.patch.object(
                    updater, "_run_bounded_git", side_effect=version_for
                ) as run:
                    reader = updater._GitReader(Path.cwd())
        self.assertEqual(reader.git_executable, "supported-git")
        self.assertEqual(run.call_count, 2)

    def test_git_standard_error_never_enters_machine_output(self):
        code, output = updater._run_bounded_git(
            [
                os.sys.executable,
                "-c",
                "import sys; sys.stderr.write('warning\\n'); sys.stdout.write('machine')",
            ],
            os.environ,
        )
        self.assertEqual(code, 0)
        self.assertEqual(output, b"machine")

    def test_inherited_stdout_process_tree_refuses_without_hanging(self):
        script = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)'], "
            "stdout=sys.stdout, stderr=subprocess.DEVNULL)"
        )
        started = time.monotonic()
        with self.assertRaisesRegex(updater.UpdateInspectionError, "reader did not stop"):
            updater._run_bounded_git([os.sys.executable, "-c", script], os.environ)
        self.assertLess(time.monotonic() - started, 4.5)
        cleanup_deadline = time.monotonic() + 2
        while time.monotonic() < cleanup_deadline and any(
            thread.name == "updater-git-output" and thread.is_alive()
            for thread in threading.enumerate()
        ):
            time.sleep(0.01)
        self.assertFalse(any(
            thread.name == "updater-git-output" and thread.is_alive()
            for thread in threading.enumerate()
        ))

    @unittest.skipUnless(shutil.which("git"), "Git is required for lazy-fetch test")
    def test_missing_promisor_object_does_not_contact_the_network_or_write_objects(self):
        git = updater._resolve_git_executable()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def run(*args):
                return subprocess.run(
                    [git, "-C", str(root), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            run("init", "-q")
            run("config", "user.name", "Updater Test")
            run("config", "user.email", "updater@example.invalid")
            (root / "payload.txt").write_text("payload\n", encoding="utf-8")
            run("add", "payload.txt")
            run("commit", "-qm", "promisor target")
            commit = run("rev-parse", "HEAD").stdout.strip()
            run("tag", "v1.0.0")

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(1)
            port = listener.getsockname()[1]
            run("remote", "add", "origin", "http://127.0.0.1:%d/repo.git" % port)
            run("config", "remote.origin.promisor", "true")
            run("config", "remote.origin.partialclonefilter", "blob:none")

            object_path = root / ".git" / "objects" / commit[:2] / commit[2:]
            self.assertTrue(object_path.is_file())
            object_path.chmod(stat.S_IWRITE)
            object_path.unlink()
            before_objects = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in (root / ".git" / "objects").rglob("*")
                if path.is_file()
            }
            contacted = threading.Event()

            def accept_once():
                try:
                    connection, _address = listener.accept()
                except (OSError, socket.timeout):
                    return
                contacted.set()
                connection.close()

            server = threading.Thread(target=accept_once, daemon=True)
            server.start()
            argv = updater._git_arguments(
                "target", root, git_executable=git, tag="v1.0.0"
            )
            code, _output = updater._run_bounded_git(
                argv, updater._git_environment(git)
            )
            listener.close()
            server.join(timeout=2)
            after_objects = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in (root / ".git" / "objects").rglob("*")
                if path.is_file()
            }
        self.assertNotEqual(code, 0)
        self.assertFalse(contacted.is_set())
        self.assertEqual(after_objects, before_objects)

    def test_git_output_and_wall_clock_are_bounded(self):
        with mock.patch.object(updater, "_GIT_OUTPUT_LIMIT", 32):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "size limit"):
                updater._run_bounded_git(
                    [os.sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000)"],
                    os.environ,
                )
        with mock.patch.object(updater, "_GIT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(updater.UpdateInspectionError, "timed out"):
                updater._run_bounded_git(
                    [os.sys.executable, "-c", "import time; time.sleep(1)"],
                    os.environ,
                )


if __name__ == "__main__":
    unittest.main()
