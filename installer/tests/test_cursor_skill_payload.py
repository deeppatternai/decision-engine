"""Behavior locks for signed Cursor owned-copy skill payloads."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from installer import (
    client_host_ownership,
    cursor_skill_payload,
    managed_install,
    release_contract,
    updater,
)


def _blob_id(raw: bytes) -> str:
    return hashlib.sha256(b"test-git-object:" + raw).hexdigest()[:40]


class CursorSkillPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "managed root 中文 😀"
        (self.root / "skills").mkdir(parents=True)
        self.state = self.base / "private state"
        self.manifest = release_contract.ReleaseManifest(
            schema=1,
            repository_id="deeppatternai/decision-engine",
            channel="stable",
            release_sequence=7,
            version="0.7.0",
            tag="v0.7.0",
            commit="7" * 40,
            min_python="3.12",
            published_at="2026-07-30T00:00:00Z",
            key_id="production",
        )
        self.verified = release_contract.VerifiedRelease(
            manifest=self.manifest,
            key_id="production",
        )
        self.blobs = {}
        self.inventory = {}
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            raw = ("# %s\n" % skill).encode("utf-8")
            object_id = _blob_id(raw)
            self.blobs[object_id] = raw
            self.inventory[skill] = (("SKILL.md", object_id),)

    def _prepare(self):
        signed = cursor_skill_payload.SignedInventory(
            reader=mock.sentinel.reader,
            skills=self.inventory,
        )
        with (
            client_host_ownership._activation_publication_scope(),
            mock.patch.object(
                cursor_skill_payload,
                "_verified_source_inventory",
                return_value=signed,
            ),
            mock.patch.object(
                cursor_skill_payload,
                "_read_verified_blob",
                side_effect=lambda _reader, object_id: self.blobs[object_id],
            ),
        ):
            return cursor_skill_payload.prepare_verified_payload(
                self.root,
                self.verified,
                transaction_id=str(uuid.UUID(int=1)),
                installed_at="2026-07-30T01:02:03Z",
                state_root=self.state,
            )

    def test_missing_file_is_classified_as_unverifiable_not_modified(self):
        with self.assertRaises(
            cursor_skill_payload.CursorSkillPayloadUnverifiableError
        ):
            cursor_skill_payload._stable_read(
                self.base / "missing",
                maximum=16,
                label="active skill",
            )

    def test_explicit_m1_and_m2_allowlists_are_stable(self):
        self.assertEqual(
            cursor_skill_payload.CURSOR_M1_SKILLS,
            (
                "audit",
                "audit-adjudication",
                "audit-brainstorming",
                "audit-explore",
                "audit-forecast",
                "audit-market-research",
                "audit-writing-plans",
                "layer-check",
            ),
        )
        self.assertEqual(
            cursor_skill_payload.CURSOR_M2_SKILLS,
            cursor_skill_payload.CURSOR_M1_SKILLS
            + ("discussion-board", "graphic-explanation"),
        )

    def test_requires_verified_release_before_reading_source(self):
        with mock.patch.object(
            cursor_skill_payload, "_verified_source_inventory"
        ) as inventory:
            with self.assertRaisesRegex(
                cursor_skill_payload.CursorSkillPayloadError,
                "verified release",
            ):
                cursor_skill_payload.prepare_verified_payload(
                    self.root,
                    self.manifest,
                    transaction_id=str(uuid.uuid4()),
                    installed_at="2026-07-30T01:02:03Z",
                    state_root=self.state,
                )
        inventory.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_requires_activation_lock_before_any_state_write(self):
        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "activation lock",
        ):
            cursor_skill_payload.prepare_verified_payload(
                self.root,
                self.verified,
                transaction_id=str(uuid.uuid4()),
                installed_at="2026-07-30T01:02:03Z",
                state_root=self.state,
            )
        self.assertFalse(self.state.exists())

    def test_prepares_exact_git_blob_copy_and_bounded_manifest(self):
        raw = b'{"safe":true}\n'
        object_id = _blob_id(raw)
        self.blobs[object_id] = raw
        self.inventory["audit-market-research"] += (
            ("assets/样式 😀.json", object_id),
        )

        prepared = self._prepare()

        copied = (
            prepared.root
            / "audit-market-research"
            / "assets"
            / "样式 😀.json"
        )
        self.assertEqual(copied.read_bytes(), raw)
        payload = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
        record = payload["skills"]["audit-market-research"]["files"][
            "assets/样式 😀.json"
        ]
        self.assertEqual(record["git_blob_sha1"], object_id)
        self.assertEqual(record["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["size"], len(raw))
        self.assertEqual(
            prepared.manifest_sha256,
            cursor_skill_payload.verify_cached_payload(
                prepared.root,
            ),
        )

    def test_inventory_must_be_exact_and_canonical(self):
        bad_values = (
            {**self.inventory, "foreign": (("SKILL.md", "1" * 40),)},
            {**self.inventory, "audit": (("../escape", "1" * 40),)},
            {**self.inventory, "audit": (("nested\\ambiguous", "1" * 40),)},
            {**self.inventory, "audit": ()},
            {**self.inventory, "audit": (("SKILL.md", "not-an-oid"),)},
        )
        for bad in bad_values:
            with self.subTest(inventory=bad):
                signed = cursor_skill_payload.SignedInventory(
                    mock.sentinel.reader,
                    bad,
                )
                with (
                    client_host_ownership._activation_publication_scope(),
                    mock.patch.object(
                        cursor_skill_payload,
                        "_verified_source_inventory",
                        return_value=signed,
                    ),
                    self.assertRaises(cursor_skill_payload.CursorSkillPayloadError),
                ):
                    cursor_skill_payload.prepare_verified_payload(
                        self.root,
                        self.verified,
                        transaction_id=str(uuid.uuid4()),
                        installed_at="2026-07-30T01:02:03Z",
                        state_root=self.state,
                    )

    def test_existing_exact_cache_reused_but_coherent_tamper_is_refused(self):
        prepared = self._prepare()
        reused = self._prepare()
        self.assertEqual(reused.root, prepared.root)
        self.assertFalse((self.state / "staging" / str(uuid.UUID(int=1))).exists())
        path = prepared.root / "audit" / "SKILL.md"
        raw = b"coherent malicious rewrite"
        path.write_bytes(raw)
        payload = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
        file_record = payload["skills"]["audit"]["files"]["SKILL.md"]
        file_record["git_blob_sha1"] = _blob_id(raw)
        file_record["sha256"] = hashlib.sha256(raw).hexdigest()
        file_record["size"] = len(raw)
        payload["skills"]["audit"]["tree_sha256"] = cursor_skill_payload._skill_tree_digest(
            payload["skills"]["audit"]["files"]
        )
        prepared.manifest_path.write_bytes(cursor_skill_payload._canonical_json(payload))

        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "existing cached payload",
        ):
            self._prepare()
        self.assertEqual(path.read_bytes(), raw)

    def test_cleanup_prunes_valid_siblings_but_preserves_invalid_cache(self):
        prepared = self._prepare()
        payloads = self.state / "skill-payloads"
        valid_old = payloads / ("6-" + "6" * 40)
        invalid_old = payloads / ("5-" + "5" * 40)
        shutil.copytree(prepared.root, valid_old)
        shutil.copytree(prepared.root, invalid_old)
        (invalid_old / "audit" / "SKILL.md").write_text(
            "tampered\n",
            encoding="utf-8",
        )

        with client_host_ownership._activation_publication_scope():
            skipped = cursor_skill_payload.cleanup_committed_payloads(
                self.state,
                keep_release_ids=frozenset({prepared.release_id}),
            )

        self.assertEqual(skipped, 1)
        self.assertFalse(valid_old.exists())
        self.assertTrue(invalid_old.exists())
        self.assertTrue(prepared.root.exists())

    def test_owned_state_rejects_link_component_without_mutation(self):
        prepared = self._prepare()
        target = self.base / "active" / "audit"
        shutil.copytree(prepared.root / "audit", target)
        identity = cursor_skill_payload._directory_identity(target)

        with mock.patch.object(
            managed_install,
            "_reject_link_components",
            side_effect=managed_install.ManagedInstallError("link"),
        ):
            state = cursor_skill_payload.owned_skill_state(
                prepared.root,
                target,
                "audit",
                expected_manifest_sha256=prepared.manifest_sha256,
                expected_object_identity=identity,
            )

        self.assertEqual(state, "third")
        self.assertTrue((target / "SKILL.md").is_file())

    def test_staging_link_is_refused_without_writing_outside_state(self):
        self.state.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        try:
            (self.state / "staging").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError:
            self.skipTest("test environment cannot create a directory symlink")
        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "state root",
        ):
            self._prepare()
        self.assertEqual(tuple(outside.iterdir()), ())

    def test_initial_preflight_refuses_collision_without_mutation(self):
        prepared = self._prepare()
        destination = self.base / "cursor skills"
        destination.mkdir()
        protected = destination / "layer-check"
        protected.mkdir()
        sentinel = protected / "user-owned.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with (
            mock.patch.object(
                cursor_skill_payload,
                "_verified_source_inventory",
                return_value=cursor_skill_payload.SignedInventory(
                    mock.sentinel.reader,
                    self.inventory,
                ),
            ),
            mock.patch.object(
                cursor_skill_payload,
                "_read_verified_blob",
                side_effect=lambda _reader, object_id: self.blobs[object_id],
            ),
            self.assertRaisesRegex(
                cursor_skill_payload.CursorSkillPayloadError,
                "pre-existing Cursor skill",
            ),
        ):
            cursor_skill_payload.preflight_initial_publication(
                prepared,
                destination,
            )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual({path.name for path in destination.iterdir()}, {"layer-check"})

    def test_manifest_rejects_extra_tree_duplicate_key_and_bad_record_shape(self):
        prepared = self._prepare()
        extra = prepared.root / "audit" / "unexpected-empty"
        extra.mkdir()
        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "unexpected",
        ):
            cursor_skill_payload.verify_cached_payload(prepared.root)
        extra.rmdir()

        original = prepared.manifest_path.read_bytes()
        prepared.manifest_path.write_bytes(b'{"schema":1,' + original[1:])
        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "duplicate key",
        ):
            cursor_skill_payload.verify_cached_payload(prepared.root)

        prepared.manifest_path.write_bytes(original)
        payload = json.loads(original)
        payload["skills"]["layer-check"] = 5
        prepared.manifest_path.write_bytes(cursor_skill_payload._canonical_json(payload))
        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "skill record",
        ):
            cursor_skill_payload.verify_cached_payload(prepared.root)

        prepared.manifest_path.write_bytes(b"[" * 2000)
        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "manifest is invalid",
        ):
            cursor_skill_payload.verify_cached_payload(prepared.root)

    def test_oversized_blob_is_refused_and_staging_is_reclaimed(self):
        raw = b"x" * (cursor_skill_payload._MAX_FILE_BYTES + 1)
        object_id = _blob_id(raw)
        self.inventory["audit"] = (("SKILL.md", object_id),)
        self.blobs[object_id] = raw
        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "size limit",
        ):
            self._prepare()
        self.assertFalse((self.state / "staging" / str(uuid.UUID(int=1))).exists())

    def test_git_inventory_refuses_head_other_than_verified_commit(self):
        reader = mock.Mock()
        reader.run.return_value = (0, b"8" * 40 + b"\n")
        with (
            mock.patch.object(
                cursor_skill_payload.updater,
                "_GitReader",
                return_value=reader,
            ),
            self.assertRaisesRegex(
                cursor_skill_payload.CursorSkillPayloadError,
                "HEAD",
            ),
        ):
            cursor_skill_payload._verified_source_inventory(
                self.root,
                self.verified,
            )
        reader.run.assert_called_once_with("head")

    def test_real_git_inventory_and_blob_reader_prepare_current_commit(self):
        repo = Path(__file__).resolve().parents[2]
        reader = updater._GitReader(repo)
        _code, output = reader.run("head")
        commit = updater._single_commit(output, "test HEAD")
        manifest = release_contract.ReleaseManifest(
            schema=1,
            repository_id="deeppatternai/decision-engine",
            channel="stable",
            release_sequence=8,
            version="0.8.0",
            tag="v0.8.0",
            commit=commit,
            min_python="3.12",
            published_at="2026-07-30T00:00:00Z",
            key_id="production",
        )
        verified = release_contract.VerifiedRelease(manifest, "production")
        with client_host_ownership._activation_publication_scope():
            prepared = cursor_skill_payload.prepare_verified_payload(
                repo,
                verified,
                transaction_id=str(uuid.uuid4()),
                installed_at="2026-07-30T01:02:03Z",
                state_root=self.base / "real git state",
            )
        self.assertEqual(
            (prepared.root / "audit" / "SKILL.md").read_bytes(),
            cursor_skill_payload._read_verified_blob(
                updater._GitReader(repo),
                dict(
                    cursor_skill_payload._verified_source_inventory(
                        repo,
                        verified,
                    ).skills["audit"]
                )["SKILL.md"],
            ),
        )


if __name__ == "__main__":
    unittest.main()
