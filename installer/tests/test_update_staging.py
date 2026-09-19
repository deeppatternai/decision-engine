"""Regression tests for cached signed releases and startup completion receipts."""

from __future__ import annotations

import tempfile
import unittest
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from unittest import mock

from installer import managed_install, release_acquisition, release_contract, update_staging, updater, windows_security
from installer.tests.test_release_contract import _TEST_E, _TEST_N, _sign


class UpdateStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "decision-engine"
        self.root.mkdir()
        self.state = updater.UpdateState(
            schema=1, channel="stable", last_release_sequence=22,
            last_release_commit="1" * 40, last_manifest_sha256="2" * 64,
            last_version="0.2.2",
        )
        self.manifest = release_contract.ReleaseManifest(
            schema=1, repository_id=managed_install.REPOSITORY_ID,
            channel="stable", release_sequence=23, version="0.2.3",
            tag="v0.2.3", commit="3" * 40, min_python="3.12",
            published_at="2026-07-22T00:00:00Z", key_id="test-key",
        )
        self.signature = release_contract.ReleaseSignature(
            schema=1, algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-key", signature="AA==",
        )
        self.acquired = release_acquisition.AcquiredRelease(
            release_acquisition.GITHUB_SOURCE, self.manifest, self.signature,
        )
        self.keys = {"test-key": mock.sentinel.key}

    def test_missing_completion_receipt_is_not_a_completed_check(self):
        self.assertIsNone(update_staging.read_completion(self.root))

    def test_interrupted_attempt_has_no_completed_receipt(self):
        attempt_id = update_staging.begin_attempt(self.root)
        self.assertEqual(update_staging.read_attempt(self.root), (attempt_id, "started"))
        self.assertIsNone(update_staging.read_completion(self.root))
        update_staging.finish_attempt(self.root, attempt_id, "up_to_date")
        self.assertEqual(
            update_staging.read_completion(self.root), (attempt_id, "up_to_date")
        )

    def test_staged_release_is_reauthorized_on_read(self):
        with mock.patch.object(
            release_contract, "authorize_release",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ) as authorize:
            update_staging.stage_release(self.root, self.acquired, self.state, self.keys)
            loaded = update_staging.load_release(self.root, self.state, self.keys)
        self.assertEqual(loaded, self.acquired)
        self.assertEqual(authorize.call_count, 2)
        path = self.root / update_staging.STAGED_RELEASE_RELATIVE_PATH
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        else:
            windows_security.validate_private_mutation_acl(path)

    def test_tampered_staged_release_is_refused(self):
        manifest = replace(self.manifest, key_id="test-release-key")
        signature = _sign(manifest)
        acquired = replace(self.acquired, manifest=manifest, signature=signature)
        keys = {"test-release-key": release_contract.RsaPublicKey(
            key_id="test-release-key", modulus=_TEST_N, exponent=_TEST_E,
        )}
        update_staging.stage_release(self.root, acquired, self.state, keys)
        self.assertEqual(update_staging.load_release(self.root, self.state, keys), acquired)
        path = self.root / update_staging.STAGED_RELEASE_RELATIVE_PATH
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace("0.2.3", "0.2.4"), encoding="utf-8")
        with self.assertRaises(update_staging.UpdateStagingError):
            update_staging.load_release(self.root, self.state, keys)

    def test_cached_signed_release_is_refused_after_state_advances(self):
        manifest = replace(self.manifest, key_id="test-release-key")
        signature = _sign(manifest)
        acquired = replace(self.acquired, manifest=manifest, signature=signature)
        keys = {"test-release-key": release_contract.RsaPublicKey(
            key_id="test-release-key", modulus=_TEST_N, exponent=_TEST_E,
        )}
        update_staging.stage_release(self.root, acquired, self.state, keys)
        newer = replace(self.state, last_release_sequence=24)
        with self.assertRaises(update_staging.UpdateStagingError):
            update_staging.load_release(self.root, newer, keys)

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions")
    def test_existing_runtime_directory_is_hardened_before_staging(self):
        runtime = self.root / ".runtime"
        runtime.mkdir(mode=0o755)
        runtime.chmod(0o755)
        with mock.patch.object(
            release_contract, "authorize_release",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ):
            update_staging.stage_release(self.root, self.acquired, self.state, self.keys)
            self.assertEqual(update_staging.load_release(self.root, self.state, self.keys), self.acquired)
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)

    def test_older_release_cannot_be_restaged_against_newer_state(self):
        newer_state = replace(self.state, last_release_sequence=24)
        with mock.patch.object(
            release_contract, "verify_release_signature",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ):
            with self.assertRaises(update_staging.UpdateStagingError):
                update_staging.stage_release(self.root, self.acquired, newer_state, self.keys)

    def test_completion_receipt_is_ignored_if_corrupted(self):
        attempt_id = update_staging.begin_attempt(self.root)
        update_staging.finish_attempt(self.root, attempt_id, "up_to_date")
        path = self.root / update_staging.COMPLETION_RELATIVE_PATH
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(update_staging.read_completion(self.root))

    def test_completion_receipt_with_unhashable_status_is_ignored(self):
        update_staging.begin_attempt(self.root)
        path = self.root / update_staging.COMPLETION_RELATIVE_PATH
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = ["up_to_date"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(update_staging.read_completion(self.root))

    def test_staged_release_with_unhashable_source_is_refused(self):
        with mock.patch.object(
            release_contract, "authorize_release",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ):
            update_staging.stage_release(self.root, self.acquired, self.state, self.keys)
        path = self.root / update_staging.STAGED_RELEASE_RELATIVE_PATH
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["source"] = ["github"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(update_staging.UpdateStagingError):
            update_staging.load_release(self.root, self.state, self.keys)

    def test_staged_release_rejects_malformed_document_shapes(self):
        with mock.patch.object(
            release_contract, "authorize_release",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ):
            update_staging.stage_release(self.root, self.acquired, self.state, self.keys)
        path = self.root / update_staging.STAGED_RELEASE_RELATIVE_PATH
        original = json.loads(path.read_text(encoding="utf-8"))
        for field in ("manifest", "signature"):
            with self.subTest(field=field):
                payload = dict(original, **{field: ["invalid"]})
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(update_staging.UpdateStagingError):
                    update_staging.load_release(self.root, self.state, self.keys)

    def test_staged_release_refuses_symlink(self):
        path = self.root / update_staging.STAGED_RELEASE_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "other.json"
        target.write_text("{}", encoding="utf-8")
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(update_staging.UpdateStagingError):
            update_staging.load_release(self.root, self.state, self.keys)

    def test_missing_stage_is_not_an_error(self):
        self.assertIsNone(update_staging.load_release(self.root, self.state, self.keys))
