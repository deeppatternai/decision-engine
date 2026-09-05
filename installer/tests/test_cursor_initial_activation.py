"""Behavior locks for journaled first Cursor activation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from installer import (
    client_host_ownership,
    client_host_lifecycle,
    cursor_activation,
    cursor_host_lifecycle,
    cursor_skill_payload,
    managed_install,
    mcp_config,
    release_contract,
    update_transaction,
    updater,
    windows_security,
)
from installer.config import ShellError


def _blob_id(raw: bytes) -> str:
    return hashlib.sha256(b"activation-test:" + raw).hexdigest()[:40]


class CursorInitialActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "deeppattern"
        self.root = self.home / "decision-engine"
        self.config_path = self.base / ".cursor" / "mcp.json"
        self.skills_path = self.base / ".cursor" / "skills"
        (self.root / ".git").mkdir(parents=True)
        (self.root / "installer").mkdir()
        (self.root / "installer" / "launcher.py").write_text(
            "# synthetic launcher\n",
            encoding="utf-8",
        )
        self.install_id = None
        self._managed_home = mock.patch.object(
            managed_install.config,
            "DEFAULT_DEEPPATTERN_HOME",
            self.home,
        )
        self._managed_home.start()
        self.addCleanup(self._managed_home.stop)
        self.install_id = managed_install.write_managed_identity(self.root)
        self._registration_root = mock.patch.object(
            mcp_config,
            "registration_root",
            return_value=self.root,
        )
        self._registration_root.start()
        self.addCleanup(self._registration_root.stop)
        self._environment = mock.patch.dict(
            os.environ,
            {
                "CURSOR_CONFIG": str(self.config_path),
                "CURSOR_SKILLS_DIR": str(self.skills_path),
            },
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self._version = mock.patch.object(
            mcp_config,
            "_cursor_write_version_gate",
            return_value=None,
        )
        self._version.start()
        self.addCleanup(self._version.stop)
        self._interpreter = mock.patch.object(
            cursor_activation,
            "_validate_repair_interpreter",
            side_effect=lambda requested, _root, **_kwargs: str(
                Path(requested).resolve()
            ),
        )
        self._interpreter.start()
        self.addCleanup(self._interpreter.stop)
        manifest = release_contract.ReleaseManifest(
            schema=1,
            repository_id="deeppatternai/decision-engine",
            channel="stable",
            release_sequence=19,
            version="0.19.0",
            tag="v0.19.0",
            commit="9" * 40,
            min_python="3.12",
            published_at="2026-07-30T00:00:00Z",
            key_id="production",
        )
        self.verified = release_contract.VerifiedRelease(
            manifest=manifest,
            key_id="production",
        )
        self.blobs = {}
        inventory = {}
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            raw = ("# %s\n" % skill).encode("utf-8")
            object_id = _blob_id(raw)
            self.blobs[object_id] = raw
            inventory[skill] = (("SKILL.md", object_id),)
        self.signed = cursor_skill_payload.SignedInventory(
            reader=mock.sentinel.reader,
            skills=inventory,
        )
        self._inventory = mock.patch.object(
            cursor_skill_payload,
            "_verified_source_inventory",
            side_effect=lambda *_args, **_kwargs: self.signed,
        )
        self._inventory.start()
        self.addCleanup(self._inventory.stop)
        self._blobs = mock.patch.object(
            cursor_skill_payload,
            "_read_verified_blob",
            side_effect=lambda _reader, object_id: self.blobs[object_id],
        )
        self._blobs.start()
        self.addCleanup(self._blobs.stop)

    def _activate(self, *, fault_injector=None):
        return cursor_activation.activate_cursor_initial(
            managed_root=self.root,
            verified_release=self.verified,
            python=Path(sys.executable).resolve(),
            installed_at="2026-07-30T13:30:00Z",
            fault_injector=fault_injector,
        )

    def _target_release(self):
        manifest = replace(
            self.verified.manifest,
            release_sequence=20,
            version="0.20.0",
            tag="v0.20.0",
            commit="8" * 40,
            published_at="2026-07-31T00:00:00Z",
        )
        inventory = {}
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            raw = ("# next %s\n" % skill).encode("utf-8")
            object_id = _blob_id(raw)
            self.blobs[object_id] = raw
            inventory[skill] = (("SKILL.md", object_id),)
        self.signed = cursor_skill_payload.SignedInventory(
            reader=mock.sentinel.target_reader,
            skills=inventory,
        )
        return release_contract.VerifiedRelease(
            manifest=manifest,
            key_id="production",
        )

    def _entry(self):
        return mcp_config.read_entry("cursor")

    def _record(self):
        return client_host_ownership.read_record_if_present(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
        )

    def _assert_target(self) -> None:
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            self.assertEqual(
                (self.skills_path / skill / "SKILL.md").read_text(
                    encoding="utf-8"
                ),
                "# %s\n" % skill,
            )
        entry = self._entry()
        self.assertIsInstance(entry, dict)
        self.assertTrue(
            any("installer.launcher" in argument for argument in entry["args"])
        )
        self.assertFalse(
            any("installer.shim" in argument for argument in entry["args"])
        )
        self.assertEqual(entry["env"]["DE_MCP_CLIENT_HOST"], "cursor")
        record = self._record()
        self.assertIsNotNone(record)
        self.assertEqual(
            record.managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(entry),
        )

    def _assert_pre(self) -> None:
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            self.assertFalse((self.skills_path / skill).exists())
        self.assertIsNone(self._entry())
        self.assertIsNone(self._record())

    def _owned_snapshot(self):
        journal = cursor_activation.activation_journal_path()
        record = client_host_ownership.ownership_record_path("cursor")
        return {
            "config": self.config_path.read_bytes(),
            "record": record.read_bytes(),
            "journal": journal.read_bytes(),
            "skills": {
                skill: (self.skills_path / skill / "SKILL.md").read_bytes()
                for skill in cursor_skill_payload.CURSOR_M2_SKILLS
            },
        }

    def test_first_activation_commits_skills_mcp_and_record_in_one_journal(self):
        result = self._activate()

        self.assertEqual(result, {"action": "installed", "phase": "committed"})
        self._assert_target()
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["schema"], 2)
        self.assertEqual(journal["operation"], "install")
        self.assertEqual(journal["phase"], "committed")
        self.assertEqual(
            set(journal["per_skill_publish_state"]),
            set(cursor_skill_payload.CURSOR_M2_SKILLS),
        )
        self.assertTrue(
            all(
                state["state"] == "target_published"
                for state in journal["per_skill_publish_state"].values()
            )
        )
        self.assertTrue(
            all(
                len(state["object_identity"]) == 3
                for state in journal["per_skill_publish_state"].values()
            )
        )
        self.assertFalse(Path(journal["staging_root"]).parent.exists())
        phases = cursor_activation._INSTALL_PHASES
        self.assertLess(
            tuple(phases).index("skills_published"),
            tuple(phases).index("mcp_publishing"),
        )

    def test_owned_update_changes_only_owned_skills_and_record(self):
        self._activate()
        config_before = self.config_path.read_bytes()
        target = self._target_release()

        with mock.patch.object(
            cursor_activation,
            "_verified_current_release",
            return_value=target,
        ):
            result = cursor_host_lifecycle._execute(
                "update",
                {
                    "managed_root": self.root,
                    "verified_release": target,
                },
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(self.config_path.read_bytes(), config_before)
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            self.assertEqual(
                (self.skills_path / skill / "SKILL.md").read_text(
                    encoding="utf-8"
                ),
                "# next %s\n" % skill,
            )
        record = self._record()
        self.assertEqual(record.skill_release_id, "20-" + "8" * 40)
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["schema"], 3)
        self.assertEqual(journal["operation"], "update")
        self.assertEqual(journal["phase"], "committed")

    def test_owned_update_retains_only_pre_and_target_committed_caches(self):
        self._activate()
        payloads = cursor_activation.cursor_activation_root() / "skill-payloads"
        pre_cache = payloads / ("19-" + "9" * 40)
        obsolete = payloads / ("18-" + "7" * 40)
        shutil.copytree(pre_cache, obsolete)
        target = self._target_release()

        with mock.patch.object(
            cursor_activation,
            "_verified_current_release",
            return_value=target,
        ):
            cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=target,
            )

        self.assertFalse(obsolete.exists())
        self.assertTrue(pre_cache.exists())
        self.assertTrue((payloads / ("20-" + "8" * 40)).exists())

    def test_prejournal_prepared_cache_is_reconciled_on_retry(self):
        self._activate()
        payloads = cursor_activation.cursor_activation_root() / "skill-payloads"
        target = self._target_release()
        journal_before = cursor_activation.activation_journal_path().read_bytes()

        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=target,
            ),
            mock.patch.object(
                cursor_skill_payload,
                "stage_initial_publication",
                side_effect=cursor_skill_payload.CursorSkillPayloadError(
                    "staging failed"
                ),
            ),
            self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "staging failed",
            ),
        ):
            cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=target,
            )

        self.assertEqual(
            {path.name for path in payloads.iterdir()},
            {"19-" + "9" * 40, "20-" + "8" * 40},
        )
        self.assertEqual(
            cursor_activation.activation_journal_path().read_bytes(),
            journal_before,
        )

        with mock.patch.object(
            cursor_activation,
            "_verified_current_release",
            return_value=target,
        ):
            result = cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=target,
            )

        self.assertEqual(result, {"action": "updated", "phase": "committed"})
        self.assertEqual(
            {path.name for path in payloads.iterdir()},
            {"19-" + "9" * 40, "20-" + "8" * 40},
        )

    def test_owned_update_rejects_non_monotonic_release_before_writes(self):
        self._activate()
        config_before = self.config_path.read_bytes()
        record_before = client_host_ownership.record_file_sha256_if_present(
            "cursor"
        )
        skills_before = {
            skill: (self.skills_path / skill / "SKILL.md").read_bytes()
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS
        }

        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=self.verified,
            ),
            self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "newer",
            ),
        ):
            cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=self.verified,
            )

        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(
            client_host_ownership.record_file_sha256_if_present("cursor"),
            record_before,
        )
        self.assertEqual(
            {
                skill: (self.skills_path / skill / "SKILL.md").read_bytes()
                for skill in cursor_skill_payload.CURSOR_M2_SKILLS
            },
            skills_before,
        )

    def test_owned_update_rejects_nonprotected_release_without_mutation(self):
        self._activate()
        requested = self._target_release()
        before = self._owned_snapshot()

        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=self.verified,
            ),
            self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "protected state",
            ),
        ):
            cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=requested,
            )

        self.assertEqual(self._owned_snapshot(), before)

    def test_registered_update_reconstructs_equal_protected_release(self):
        self._activate()
        target = self._target_release()
        independently_loaded = release_contract.VerifiedRelease(
            manifest=target.manifest,
            key_id=target.key_id,
        )
        self.assertIsNot(target, independently_loaded)
        self.assertEqual(target, independently_loaded)

        with mock.patch.object(
            cursor_activation,
            "_verified_current_release",
            side_effect=(target, independently_loaded),
        ) as protected:
            result = cursor_host_lifecycle._execute(
                "update",
                {"managed_root": self.root},
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(protected.call_count, 2)
        self.assertEqual(self._record().skill_release_id, "20-" + "8" * 40)

    def test_owned_uninstall_removes_only_managed_cursor_artifacts(self):
        self._activate()
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        payload["ownerNote"] = "keep"
        payload["mcpServers"]["other"] = {"command": "keep"}
        self.config_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

        result = cursor_host_lifecycle._execute(
            "uninstall",
            {"managed_root": self.root},
        )

        self.assertEqual(result.status, "completed")
        after = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("decision-engine", after["mcpServers"])
        self.assertEqual(after["mcpServers"]["other"], {"command": "keep"})
        self.assertEqual(after["ownerNote"], "keep")
        self.assertIsNone(self._record())
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            self.assertFalse((self.skills_path / skill).exists())
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["schema"], 3)
        self.assertEqual(journal["operation"], "uninstall")
        self.assertEqual(journal["phase"], "committed")
        self.assertFalse(journal["target_present"])

    def test_owned_uninstall_removes_verified_payload_caches(self):
        self._activate()
        payloads = cursor_activation.cursor_activation_root() / "skill-payloads"
        self.assertTrue(any(payloads.iterdir()))

        result = cursor_activation.uninstall_cursor_owned(
            managed_root=self.root
        )

        self.assertEqual(result, {"action": "uninstalled", "phase": "committed"})
        self.assertEqual(tuple(payloads.iterdir()), ())

    def test_owned_uninstall_then_first_activation_round_trips(self):
        self._activate()
        cursor_activation.uninstall_cursor_owned(managed_root=self.root)

        result = self._activate()

        self.assertEqual(result, {"action": "installed", "phase": "committed"})
        self._assert_target()

    def test_owned_uninstall_retains_opaque_fields_on_the_managed_entry(self):
        self._activate()
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        payload["mcpServers"]["decision-engine"]["metadata"] = {
            "ownerNote": "keep"
        }
        self.config_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

        cursor_activation.uninstall_cursor_owned(managed_root=self.root)

        after = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            after["mcpServers"]["decision-engine"],
            {"metadata": {"ownerNote": "keep"}},
        )

    def test_owned_update_refuses_a_user_modified_skill_before_intent(self):
        self._activate()
        target = self._target_release()
        changed = self.skills_path / "audit" / "SKILL.md"
        changed.write_text("# user content\n", encoding="utf-8")
        journal_before = cursor_activation.activation_journal_path().read_bytes()
        record_before = client_host_ownership.record_file_sha256_if_present(
            "cursor"
        )

        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=target,
            ),
            self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "user_modified",
            ),
        ):
            cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=target,
            )

        self.assertEqual(changed.read_text(encoding="utf-8"), "# user content\n")
        self.assertEqual(
            cursor_activation.activation_journal_path().read_bytes(),
            journal_before,
        )
        self.assertEqual(
            client_host_ownership.record_file_sha256_if_present("cursor"),
            record_before,
        )

    def test_owned_uninstall_preserves_user_modified_skill_and_every_sibling(self):
        self._activate()
        changed = self.skills_path / "audit" / "SKILL.md"
        changed.write_text("# user content\n", encoding="utf-8")
        config_before = self.config_path.read_bytes()
        record_before = client_host_ownership.record_file_sha256_if_present(
            "cursor"
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "user_modified",
        ):
            cursor_activation.uninstall_cursor_owned(managed_root=self.root)

        self.assertEqual(changed.read_text(encoding="utf-8"), "# user content\n")
        self.assertTrue(
            all(
                (self.skills_path / skill / "SKILL.md").exists()
                for skill in cursor_skill_payload.CURSOR_M2_SKILLS
            )
        )
        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(
            client_host_ownership.record_file_sha256_if_present("cursor"),
            record_before,
        )

    def test_update_crash_after_target_publish_repairs_to_complete_pre_generation(self):
        self._activate()
        target = self._target_release()

        def crash(point):
            if point == "after_skill_publish:audit":
                raise RuntimeError("simulated update crash")

        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=target,
            ),
            self.assertRaisesRegex(RuntimeError, "simulated update crash"),
        ):
            cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=target,
                fault_injector=crash,
            )

        result = cursor_activation.repair_cursor_activation(
            managed_root=self.root
        )

        self.assertEqual(result, {"action": "rolled_back", "phase": "pre"})
        self._assert_target()
        self.assertEqual(self._record().skill_release_id, "19-" + "9" * 40)

    def test_uninstall_crash_after_ownership_remove_repairs_installed_generation(self):
        self._activate()

        def crash(point):
            if point == "after_ownership_apply_before_phase":
                raise RuntimeError("simulated uninstall crash")

        with self.assertRaisesRegex(RuntimeError, "simulated uninstall crash"):
            cursor_activation.uninstall_cursor_owned(
                managed_root=self.root,
                fault_injector=crash,
            )

        result = cursor_activation.repair_cursor_activation(
            managed_root=self.root
        )

        self.assertEqual(result, {"action": "rolled_back", "phase": "pre"})
        self._assert_target()
        self.assertEqual(self._record().skill_release_id, "19-" + "9" * 40)

    def test_update_crash_after_ownership_publish_repairs_pre_generation(self):
        self._activate()
        target = self._target_release()

        def crash(point):
            if point == "after_ownership_apply_before_phase":
                raise RuntimeError("simulated ownership crash")

        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=target,
            ),
            self.assertRaisesRegex(RuntimeError, "simulated ownership crash"),
        ):
            cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=target,
                fault_injector=crash,
            )

        result = cursor_activation.repair_cursor_activation(
            managed_root=self.root
        )

        self.assertEqual(result, {"action": "rolled_back", "phase": "pre"})
        self._assert_target()
        self.assertEqual(self._record().skill_release_id, "19-" + "9" * 40)

    def test_update_cleanup_failure_is_nonfatal_and_repair_retries_it(self):
        self._activate()
        target = self._target_release()
        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=target,
            ),
            mock.patch.object(
                cursor_activation,
                "_cleanup_lifecycle_backups",
                side_effect=cursor_skill_payload.CursorSkillPayloadError(
                    "cleanup failed"
                ),
            ),
        ):
            result = cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=target,
            )

        self.assertEqual(
            result,
            {
                "action": "updated",
                "phase": "committed",
                "cleanup_pending": True,
            },
        )
        self.assertEqual(self._record().skill_release_id, "20-" + "8" * 40)
        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root
        )
        self.assertEqual(repaired, {"action": "unchanged", "phase": "committed"})

    def test_uninstall_cleanup_failure_is_nonfatal_and_repair_retries_it(self):
        self._activate()
        payloads = cursor_activation.cursor_activation_root() / "skill-payloads"
        with mock.patch.object(
            cursor_activation,
            "_cleanup_lifecycle_backups",
            side_effect=cursor_skill_payload.CursorSkillPayloadError(
                "cleanup failed"
            ),
        ):
            result = cursor_activation.uninstall_cursor_owned(
                managed_root=self.root
            )

        self.assertEqual(result["phase"], "committed")
        self.assertTrue(result["cleanup_pending"])
        self.assertIsNone(self._record())
        self.assertTrue(any(payloads.iterdir()))
        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root
        )
        self.assertEqual(repaired, {"action": "unchanged", "phase": "committed"})
        self.assertEqual(tuple(payloads.iterdir()), ())

    def test_owned_lifecycle_real_lock_contention_has_zero_mutation(self):
        self._activate()
        target = self._target_release()
        before = self._owned_snapshot()

        with cursor_activation.cursor_activation_lock():
            with self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "already in progress",
            ):
                cursor_activation.update_cursor_owned(
                    managed_root=self.root,
                    verified_release=target,
                )
            with self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "already in progress",
            ):
                cursor_activation.uninstall_cursor_owned(
                    managed_root=self.root
                )

        self.assertEqual(self._owned_snapshot(), before)

    def test_update_each_skill_boundary_repairs_to_exact_pre_generation(self):
        self._activate()
        target = self._target_release()
        points = [
            "%s:%s" % (prefix, skill)
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS
            for prefix in ("after_skill_backup", "after_skill_publish")
        ] + ["after_mcp_published", "after_ownership_published"]

        for point in points:
            with self.subTest(point=point):
                def crash(observed, *, expected=point):
                    if observed == expected:
                        raise RuntimeError("simulated boundary crash")

                with (
                    mock.patch.object(
                        cursor_activation,
                        "_verified_current_release",
                        return_value=target,
                    ),
                    self.assertRaisesRegex(RuntimeError, "boundary crash"),
                ):
                    cursor_activation.update_cursor_owned(
                        managed_root=self.root,
                        verified_release=target,
                        fault_injector=crash,
                    )
                self.assertEqual(
                    cursor_activation.repair_cursor_activation(
                        managed_root=self.root
                    ),
                    {"action": "rolled_back", "phase": "pre"},
                )
                self._assert_target()
                self.assertEqual(
                    self._record().skill_release_id,
                    "19-" + "9" * 40,
                )

    def test_uninstall_each_boundary_repairs_to_exact_pre_generation(self):
        self._activate()
        points = [
            "after_skill_backup:%s" % skill
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS
        ] + [
            "after_mcp_apply_before_phase",
            "after_mcp_published",
            "after_ownership_published",
        ]

        for point in points:
            with self.subTest(point=point):
                def crash(observed, *, expected=point):
                    if observed == expected:
                        raise RuntimeError("simulated boundary crash")

                with self.assertRaisesRegex(RuntimeError, "boundary crash"):
                    cursor_activation.uninstall_cursor_owned(
                        managed_root=self.root,
                        fault_injector=crash,
                    )
                self.assertEqual(
                    cursor_activation.repair_cursor_activation(
                        managed_root=self.root
                    ),
                    {"action": "rolled_back", "phase": "pre"},
                )
                self._assert_target()
                self.assertEqual(
                    self._record().skill_release_id,
                    "19-" + "9" * 40,
                )

    def test_committed_owned_uninstall_is_idempotent(self):
        self._activate()
        first = cursor_activation.uninstall_cursor_owned(managed_root=self.root)
        second = cursor_activation.uninstall_cursor_owned(managed_root=self.root)

        self.assertEqual(first, {"action": "uninstalled", "phase": "committed"})
        self.assertEqual(second, {"action": "unchanged", "phase": "committed"})

    def test_future_lifecycle_journal_is_retained_for_newer_installer(self):
        self._activate()
        path = cursor_activation.activation_journal_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema"] = 99
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        journal_before = path.read_bytes()
        config_before = self.config_path.read_bytes()
        record_before = client_host_ownership.record_file_sha256_if_present(
            "cursor"
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "requires_newer_installer",
        ):
            cursor_activation.repair_cursor_activation(managed_root=self.root)

        self.assertEqual(path.read_bytes(), journal_before)
        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(
            client_host_ownership.record_file_sha256_if_present("cursor"),
            record_before,
        )

    def test_lifecycle_repair_rejects_journal_path_substitution_without_deletion(self):
        self._activate()
        target = self._target_release()

        def crash(point):
            if point == "after_intent":
                raise RuntimeError("simulated intent crash")

        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=target,
            ),
            self.assertRaisesRegex(RuntimeError, "simulated intent crash"),
        ):
            cursor_activation.update_cursor_owned(
                managed_root=self.root,
                verified_release=target,
                fault_injector=crash,
            )
        path = cursor_activation.activation_journal_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["pre_ownership_record"]["config_path"] = str(
            self.base / "outside" / "victim.json"
        )
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        config_before = self.config_path.read_bytes()

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "ownership binding|ownership target",
        ):
            cursor_activation.repair_cursor_activation(managed_root=self.root)

        self.assertEqual(self.config_path.read_bytes(), config_before)
        self._assert_target()

    def test_initial_journal_rejects_equal_mcp_pre_and_post_hashes(self):
        self._activate()
        journal_path = cursor_activation.activation_journal_path()
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["pre_mcp_file_hash"] = journal["post_mcp_file_hash"]
        journal_path.write_text(
            json.dumps(journal, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "initial pre-state is invalid",
        ):
            cursor_activation.repair_cursor_activation(
                managed_root=self.root,
            )

    def test_initial_journal_rejects_noncanonical_skill_release_id(self):
        self._activate()
        journal_path = cursor_activation.activation_journal_path()
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["target_skill_release_id"] = "../../outside"
        journal_path.write_text(
            json.dumps(journal, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "journal release is invalid",
        ):
            cursor_activation.repair_cursor_activation(
                managed_root=self.root,
            )

    def test_crash_during_skill_publication_repairs_to_exact_pre_state(self):
        def crash(point: str) -> None:
            if point == "after_skill_publish:audit":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        self.assertTrue((self.skills_path / "audit").is_dir())
        self.assertIsNone(self._entry())

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self._assert_pre()
        self.assertFalse(cursor_activation.activation_journal_path().exists())

    def test_missing_private_cache_after_crash_rebuilds_only_for_rollback(self):
        def crash(point: str) -> None:
            if point == "after_skill_publish:audit":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        cache_root = (
            cursor_activation.cursor_activation_root()
            / "skill-payloads"
            / journal["target_skill_release_id"]
        )
        shutil.rmtree(cache_root)

        with mock.patch.object(
            cursor_activation,
            "_verified_current_release",
            return_value=self.verified,
        ):
            repaired = cursor_activation.repair_cursor_activation(
                managed_root=self.root,
            )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self._assert_pre()
        self.assertRegex(
            cursor_skill_payload.verify_cached_payload(cache_root),
            r"^[0-9a-f]{64}$",
        )
        self.assertFalse(cursor_activation.activation_journal_path().exists())

    def test_rebuilt_cache_is_journaled_before_a_second_rollback_crash(self):
        def crash(point: str) -> None:
            if point == "after_skill_publish:audit":
                raise RuntimeError("synthetic activation crash")

        with self.assertRaisesRegex(RuntimeError, "activation crash"):
            self._activate(fault_injector=crash)
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        shutil.rmtree(
            cursor_activation.cursor_activation_root()
            / "skill-payloads"
            / journal["target_skill_release_id"]
        )
        native_remove = cursor_skill_payload.remove_initial_skill
        crashed = False

        def remove_then_crash(*args, **kwargs):
            nonlocal crashed
            native_remove(*args, **kwargs)
            if not crashed:
                crashed = True
                raise RuntimeError("synthetic rollback crash")

        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=self.verified,
            ),
            mock.patch.object(
                cursor_skill_payload,
                "remove_initial_skill",
                side_effect=remove_then_crash,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "rollback crash"):
                cursor_activation.repair_cursor_activation(
                    managed_root=self.root,
                )

        with mock.patch.object(
            cursor_activation,
            "_verified_current_release",
            return_value=self.verified,
        ):
            repaired = cursor_activation.repair_cursor_activation(
                managed_root=self.root,
            )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self._assert_pre()

    def test_crash_after_intent_before_staging_repairs_without_orphan(self):
        def crash(point: str) -> None:
            if point == "after_intent_before_staging":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["phase"], "intent")
        self.assertEqual(journal["per_skill_publish_state"], {})

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self._assert_pre()
        self.assertFalse(Path(journal["staging_root"]).parent.exists())
        self.assertFalse(cursor_activation.activation_journal_path().exists())

    def test_preexisting_cursor_config_is_restored_byte_exact_after_mcp(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        before = (
            b'{\r\n  "userSetting": {"keep": true},\r\n'
            b'  "mcpServers": {"other": {"command": "keep"}}\r\n}\r\n'
        )
        self.config_path.write_bytes(before)

        def crash(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        self.assertNotEqual(self.config_path.read_bytes(), before)
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        shutil.rmtree(self.skills_path / "audit")
        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self._entry(), None)
        self.assertIsNone(self._record())
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            self.assertFalse((self.skills_path / skill).exists())
        self.assertFalse(Path(journal["staging_root"]).parent.exists())

    def test_preimage_write_failure_leaves_discoverable_intent_for_repair(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_bytes(
            b'{"mcpServers":{"other":{"command":"keep"}}}\r\n'
        )
        with mock.patch.object(
            cursor_activation,
            "_write_preimage",
            side_effect=OSError("synthetic preimage failure"),
        ):
            with self.assertRaisesRegex(
                OSError,
                "synthetic preimage failure",
            ):
                self._activate()

        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["phase"], "intent")
        self.assertEqual(
            cursor_activation.repair_cursor_activation(managed_root=self.root),
            {"action": "rolled_back", "phase": "pre"},
        )
        self.assertFalse(cursor_activation.activation_journal_path().exists())

    def test_repair_retries_first_activation_after_protocol_was_published(self):
        protocol = self.root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
        protocol.parent.mkdir(parents=True, exist_ok=True)
        protocol.write_text("{}\n", encoding="utf-8")
        expected = {"action": "installed", "phase": "committed"}
        with (
            mock.patch.object(
                cursor_activation,
                "_verified_current_release",
                return_value=self.verified,
            ) as verify,
            mock.patch.object(
                cursor_activation,
                "activate_cursor_initial",
                return_value=expected,
            ) as activate,
        ):
            result = cursor_activation.repair_cursor_activation(
                managed_root=self.root,
            )

        self.assertEqual(result, expected)
        verify.assert_called_once_with(self.root)
        activate.assert_called_once_with(
            managed_root=self.root,
            verified_release=self.verified,
            python=Path(sys.executable),
            fault_injector=None,
        )

    def test_retry_authority_is_bound_to_protected_state_and_head(self):
        state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=self.verified.manifest.release_sequence,
            last_release_commit=self.verified.manifest.commit,
            last_manifest_sha256=release_contract.manifest_sha256(
                self.verified.manifest
            ),
            last_version=self.verified.manifest.version,
        )
        reader = mock.Mock()
        reader.run.side_effect = [
            (0, (self.verified.manifest.commit + "\n").encode("ascii")),
            (0, b"tree"),
        ]
        entries = (
            updater._GitTreeEntry(
                "100644",
                "1" * 40,
                "release/manifest.json",
            ),
            updater._GitTreeEntry(
                "100644",
                "2" * 40,
                "release/manifest.sig.json",
            ),
        )
        with (
            mock.patch.object(updater, "_read_update_state", return_value=state),
            mock.patch.object(updater, "_GitReader", return_value=reader),
            mock.patch.object(
                updater,
                "_single_commit",
                return_value=self.verified.manifest.commit,
            ),
            mock.patch.object(updater, "_parse_tree_entries", return_value=entries),
            mock.patch.object(
                cursor_skill_payload,
                "_read_verified_blob",
                side_effect=(b"manifest", b"signature"),
            ),
            mock.patch.object(
                release_contract,
                "parse_release_manifest",
                return_value=self.verified.manifest,
            ),
            mock.patch.object(
                release_contract,
                "parse_release_signature",
                return_value=mock.sentinel.signature,
            ),
            mock.patch.object(
                cursor_activation.release_acquisition,
                "load_trusted_release_keys",
                return_value={"production": mock.sentinel.key},
            ),
            mock.patch.object(
                release_contract,
                "verify_release_signature",
                return_value=self.verified,
            ),
        ):
            actual = cursor_activation._verified_current_release(self.root)

        self.assertEqual(actual, self.verified)

    def test_retry_authority_rejects_nonstable_protected_channel_before_git(self):
        state = updater.UpdateState(
            schema=1,
            channel="preview",
            last_release_sequence=self.verified.manifest.release_sequence,
            last_release_commit=self.verified.manifest.commit,
            last_manifest_sha256=release_contract.manifest_sha256(
                self.verified.manifest
            ),
            last_version=self.verified.manifest.version,
        )
        with (
            mock.patch.object(updater, "_read_update_state", return_value=state),
            mock.patch.object(
                updater,
                "_GitReader",
                side_effect=AssertionError("Git must not be read"),
            ),
        ):
            with self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "protected release channel",
            ):
                cursor_activation._verified_current_release(self.root)

    def test_crash_after_mcp_apply_resumes_forward_to_target(self):
        def crash(point: str) -> None:
            if point == "after_mcp_apply_before_phase":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        self.assertIsInstance(self._entry(), dict)
        self.assertIsNone(self._record())

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "recovered", "phase": "committed"})
        self._assert_target()

    def test_unrelated_config_edit_after_mcp_apply_resumes_to_target(self):
        def crash(point: str) -> None:
            if point == "after_mcp_apply_before_phase":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        payload["userSetting"] = {"keep": True}
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "recovered", "phase": "committed"})
        self._assert_target()
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))[
                "userSetting"
            ],
            {"keep": True},
        )

    def test_unrelated_config_edit_after_mcp_apply_survives_rollback(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "userSetting": {"before": True},
                    "mcpServers": {"other": {"command": "keep"}},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        def crash(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        payload["userSetting"] = {"after": True}
        payload["unrelated"] = ["preserve", 2]
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(self.skills_path / "audit")

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["userSetting"], {"after": True})
        self.assertEqual(persisted["unrelated"], ["preserve", 2])
        self.assertEqual(
            persisted["mcpServers"],
            {"other": {"command": "keep"}},
        )
        self._assert_pre()

    def test_unrelated_config_edit_before_mcp_apply_rolls_back_without_clobber(self):
        def crash(point: str) -> None:
            if point == "before_mcp_apply":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"userSetting": {"keep": True}}, indent=2) + "\n",
            encoding="utf-8",
        )

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8")),
            {"userSetting": {"keep": True}},
        )
        for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
            self.assertFalse((self.skills_path / skill).exists())
        self.assertIsNone(self._record())

    def test_crash_after_ownership_apply_is_idempotently_committed(self):
        def crash(point: str) -> None:
            if point == "after_ownership_apply_before_phase":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        self.assertIsNotNone(self._record())

        first = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )
        second = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(first, {"action": "recovered", "phase": "committed"})
        self.assertEqual(second, {"action": "unchanged", "phase": "committed"})
        self._assert_target()

    def test_committed_receipt_allows_unrelated_config_edit_and_cache_cleanup(self):
        self._activate()
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        payload["userSetting"] = {"keep": True}
        payload["mcpServers"]["other"] = {"command": "keep"}
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        shutil.rmtree(
            cursor_activation.cursor_activation_root()
            / "skill-payloads"
            / journal["target_skill_release_id"]
        )

        with mock.patch.object(
            cursor_activation,
            "_verified_current_release",
            return_value=self.verified,
        ):
            result = cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=Path(sys.executable).resolve(),
            )

        self.assertEqual(result, {"action": "unchanged", "phase": "committed"})
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["userSetting"], {"keep": True})
        self.assertEqual(
            persisted["mcpServers"]["other"],
            {"command": "keep"},
        )

    def test_committed_install_repair_rejects_tampered_active_skill(self):
        self._activate()
        (self.skills_path / "audit" / "SKILL.md").write_text(
            "# tampered\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "active skill integrity",
        ):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=Path(sys.executable).resolve(),
            )

    def test_committed_interpreter_repair_rejects_tampered_active_skill(self):
        self._activate()
        repaired_python = self.base / "python-repaired.exe"
        cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=repaired_python,
        )
        (self.skills_path / "audit" / "SKILL.md").write_text(
            "# tampered\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "active skill integrity",
        ):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=repaired_python,
            )

    def test_incomplete_interpreter_repair_rejects_tampered_active_skill(self):
        self._activate()
        repaired_python = self.base / "python-repaired.exe"

        def crash(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic repair crash")

        with self.assertRaisesRegex(RuntimeError, "repair crash"):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=repaired_python,
                fault_injector=crash,
            )
        (self.skills_path / "audit" / "SKILL.md").write_text(
            "# tampered\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "active skill integrity",
        ):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=repaired_python,
            )

    def test_activation_is_idempotent_after_interpreter_repair_replaces_receipt(self):
        self._activate()
        repaired_python = self.base / "python-repaired.exe"
        result = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=repaired_python,
        )
        self.assertEqual(result, {"action": "repaired", "phase": "committed"})

        repeated = self._activate()

        self.assertEqual(repeated, {"action": "unchanged", "phase": "committed"})
        self.assertEqual(self._entry()["command"], str(repaired_python.resolve()))
        self.assertEqual(
            json.loads(
                cursor_activation.activation_journal_path().read_text(
                    encoding="utf-8"
                )
            )["operation"],
            "interpreter_repair",
        )

    def test_committed_install_receipt_does_not_hide_unsupported_release(self):
        self._activate()
        newer_manifest = release_contract.ReleaseManifest(
            schema=1,
            repository_id=self.verified.manifest.repository_id,
            channel=self.verified.manifest.channel,
            release_sequence=self.verified.manifest.release_sequence + 1,
            version="0.20.0",
            tag="v0.20.0",
            commit="8" * 40,
            min_python=self.verified.manifest.min_python,
            published_at="2026-07-31T00:00:00Z",
            key_id=self.verified.manifest.key_id,
        )
        newer = release_contract.VerifiedRelease(
            manifest=newer_manifest,
            key_id=self.verified.key_id,
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "same_name_owned",
        ):
            cursor_activation.activate_cursor_initial(
                managed_root=self.root,
                verified_release=newer,
                python=Path(sys.executable).resolve(),
                installed_at="2026-07-31T13:30:00Z",
            )

    def test_committed_install_receipt_rejects_tampered_active_skill(self):
        self._activate()
        (self.skills_path / "audit" / "SKILL.md").write_text(
            "# tampered\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "active skill integrity",
        ):
            self._activate()

    def test_reactivation_after_interpreter_repair_rejects_tampered_active_skill(self):
        self._activate()
        repaired_python = self.base / "python-repaired.exe"
        cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=repaired_python,
        )
        (self.skills_path / "audit" / "SKILL.md").write_text(
            "# tampered\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "active skill integrity",
        ):
            self._activate()

        self.assertEqual(self._entry()["command"], str(repaired_python.resolve()))

    def test_exact_content_collision_is_not_adopted_or_partially_rolled_back(self):
        protected = self.skills_path / "layer-check"

        def collide(point: str) -> None:
            if point == "before_skill_apply:layer-check":
                protected.mkdir(parents=True)
                (protected / "SKILL.md").write_text(
                    "# layer-check\n",
                    encoding="utf-8",
                )

        with self.assertRaisesRegex(
            cursor_skill_payload.CursorSkillPayloadError,
            "pre-existing Cursor skill",
        ):
            self._activate(fault_injector=collide)
        before = (protected / "SKILL.md").read_bytes()

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "repair_required",
        ):
            cursor_activation.repair_cursor_activation(
                managed_root=self.root,
            )

        self.assertEqual((protected / "SKILL.md").read_bytes(), before)
        self.assertTrue((self.skills_path / "audit").exists())
        self.assertIsNone(self._entry())
        self.assertIsNone(self._record())
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            journal["per_skill_publish_state"]["layer-check"]["state"],
            "publishing",
        )
        self.assertTrue(Path(journal["staging_root"]).parent.exists())

    def test_same_name_unowned_mcp_entry_never_creates_a_journal(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "decision-engine": {
                            "command": "foreign",
                            "args": [],
                        }
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ShellError, "same_name_unowned"):
            self._activate()

        self.assertFalse(cursor_activation.activation_journal_path().exists())
        self.assertFalse(self.skills_path.exists())
        self.assertIsNone(self._record())

    def test_mcp_quarantine_preserves_last_moment_foreign_replacement(self):
        before = b'{"mcpServers":{"other":{"command":"keep"}}}\r\n'
        foreign = b'{"mcpServers":{"foreign":{"command":"preserve"}}}\r\n'
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_bytes(before)

        def crash(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        shutil.rmtree(self.skills_path / "audit")
        native_move = windows_security.move_write_through

        def replace_then_move(source, destination, *, replace_existing):
            if Path(source) == self.config_path:
                self.config_path.write_bytes(foreign)
            return native_move(
                source,
                destination,
                replace_existing=replace_existing,
            )

        with mock.patch.object(
            windows_security,
            "move_write_through",
            side_effect=replace_then_move,
        ):
            with self.assertRaisesRegex(
                ShellError,
                "repair_required",
            ):
                cursor_activation.repair_cursor_activation(
                    managed_root=self.root,
                )
        self.assertEqual(self.config_path.read_bytes(), foreign)

    def test_record_quarantine_preserves_last_moment_foreign_replacement(self):
        def crash(point: str) -> None:
            if point == "after_ownership_apply_before_phase":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        shutil.rmtree(self.skills_path / "audit")
        record_path = client_host_ownership.ownership_record_path("cursor")
        foreign = b'{"foreign":true}\n'
        native_move = windows_security.move_write_through

        def replace_then_move(source, destination, *, replace_existing):
            if Path(source) == record_path:
                record_path.write_bytes(foreign)
            return native_move(
                source,
                destination,
                replace_existing=replace_existing,
            )

        with mock.patch.object(
            windows_security,
            "move_write_through",
            side_effect=replace_then_move,
        ):
            with self.assertRaisesRegex(
                ShellError,
                "repair_required",
            ):
                cursor_activation.repair_cursor_activation(
                    managed_root=self.root,
                )
        self.assertEqual(record_path.read_bytes(), foreign)

    def test_mcp_race_is_classified_before_ownership_rollback(self):
        def crash(point: str) -> None:
            if point == "after_ownership_apply_before_phase":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        shutil.rmtree(self.skills_path / "audit")
        with mock.patch.object(
            cursor_activation,
            "_observed_initial_mcp_state",
            side_effect=(("target", True), ("third", False)),
        ):
            with self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "MCP configuration is a third state",
            ):
                cursor_activation.repair_cursor_activation(
                    managed_root=self.root,
                )

        self.assertIsNotNone(self._record())
        self.assertIsInstance(self._entry(), dict)

    def test_skill_recreation_after_quarantine_preserves_journal(self):
        def crash(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        shutil.rmtree(self.skills_path / "audit")
        protected = self.skills_path / "layer-check"
        native_rmtree = shutil.rmtree

        def remove_then_recreate(path, *args, **kwargs):
            result = native_rmtree(path, *args, **kwargs)
            if Path(path).name == "skill-layer-check.rollback":
                protected.mkdir(parents=True)
                (protected / "SKILL.md").write_text(
                    "# foreign recreation\n",
                    encoding="utf-8",
                )
            return result

        with mock.patch.object(
            cursor_skill_payload.shutil,
            "rmtree",
            side_effect=remove_then_recreate,
        ):
            with self.assertRaisesRegex(
                ShellError,
                "reappeared during rollback",
            ):
                cursor_activation.repair_cursor_activation(
                    managed_root=self.root,
                )

        self.assertEqual(
            (protected / "SKILL.md").read_text(encoding="utf-8"),
            "# foreign recreation\n",
        )
        self.assertTrue(cursor_activation.activation_journal_path().exists())

    def test_existing_ownership_quarantine_resumes_rollback(self):
        def crash(point: str) -> None:
            if point == "after_ownership_apply_before_phase":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        shutil.rmtree(self.skills_path / "audit")
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        windows_security.move_write_through(
            client_host_ownership.ownership_record_path("cursor"),
            cursor_activation._ownership_quarantine_path(journal),
            replace_existing=False,
        )

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self._assert_pre()

    def test_existing_mcp_quarantine_resumes_rollback(self):
        def crash(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        shutil.rmtree(self.skills_path / "audit")
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        windows_security.move_write_through(
            self.config_path,
            cursor_activation._mcp_quarantine_path(journal),
            replace_existing=False,
        )

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self._assert_pre()

    def test_existing_skill_quarantine_resumes_rollback(self):
        def crash(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        windows_security.move_write_through(
            self.skills_path / "audit",
            cursor_activation._skill_quarantine_path(journal, "audit"),
            replace_existing=False,
        )

        repaired = cursor_activation.repair_cursor_activation(
            managed_root=self.root,
        )

        self.assertEqual(repaired, {"action": "rolled_back", "phase": "pre"})
        self._assert_pre()

    @unittest.skipUnless(os.name == "nt", "Windows alternate streams only")
    def test_alternate_stream_on_managed_skill_fails_closed(self):
        def crash(point: str) -> None:
            if point == "after_skill_publish:audit":
                with open(
                    str(self.skills_path / "audit" / "SKILL.md") + ":owner",
                    "wb",
                ) as stream:
                    stream.write(b"user")
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "repair_required",
        ):
            cursor_activation.repair_cursor_activation(managed_root=self.root)
        self.assertTrue((self.skills_path / "audit" / "SKILL.md").exists())

    @unittest.skipUnless(os.name == "nt", "Windows alternate streams only")
    def test_alternate_stream_on_managed_skill_directory_fails_closed(self):
        def crash(point: str) -> None:
            if point == "after_skill_publish:audit":
                with open(
                    str(self.skills_path / "audit") + ":owner",
                    "wb",
                ) as stream:
                    stream.write(b"user")
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self._activate(fault_injector=crash)

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "repair_required",
        ):
            cursor_activation.repair_cursor_activation(managed_root=self.root)
        self.assertTrue((self.skills_path / "audit").exists())

    @unittest.skipUnless(os.name == "nt", "Windows alternate streams only")
    def test_unsupported_windows_stream_enumeration_fails_closed(self):
        import ctypes
        from ctypes import wintypes

        kernel32 = mock.Mock()
        kernel32.FindFirstStreamW.return_value = wintypes.HANDLE(-1).value
        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32),
            mock.patch.object(ctypes, "get_last_error", return_value=87),
        ):
            with self.assertRaisesRegex(
                cursor_skill_payload.CursorSkillPayloadError,
                "could not be inspected",
            ):
                cursor_skill_payload._has_nondefault_windows_stream(
                    self.skills_path,
                )


if __name__ == "__main__":
    unittest.main()
