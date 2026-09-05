from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import client_host_ownership, managed_install, windows_security


class ClientHostOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "deeppattern"
        self.root = self.home / "decision-engine"
        self.config_path = Path(self.tmp.name).resolve() / "cursor" / "mcp.json"
        (self.root / ".git").mkdir(parents=True)
        self.managed_home = mock.patch.object(
            managed_install.config,
            "DEFAULT_DEEPPATTERN_HOME",
            self.home,
        )
        self.managed_home.start()
        self.addCleanup(self.managed_home.stop)
        self.addCleanup(self.tmp.cleanup)
        self.install_id = managed_install.write_managed_identity(self.root)

    def _payload(self) -> dict:
        return {
            "schema": 1,
            "install_id": self.install_id,
            "client": "cursor",
            "config_path": str(self.config_path),
            "server_name": "decision-engine",
            "managed_entry_schema": 1,
            "managed_fields_sha256": "1" * 64,
            "skill_release_id": "release-synthetic",
            "skill_manifest_sha256": "2" * 64,
        }

    def _write_record(self, payload: dict) -> Path:
        path = client_host_ownership.ownership_record_path("cursor")
        managed_install._atomic_write_private_json(
            path,
            payload,
            manage_parent=True,
        )
        return path

    def test_managed_fields_hash_is_canonical_and_ignores_opaque_fields(self):
        fields = ("command", "args", "env")
        first = {
            "command": "python",
            "args": ["-m", "installer.launcher"],
            "env": {"UNICODE": "值", "A": "1"},
            "opaque": {"ignored": True},
        }
        second = {
            "env": {"A": "1", "UNICODE": "值"},
            "args": ["-m", "installer.launcher"],
            "command": "python",
            "another_opaque": False,
        }

        self.assertEqual(
            client_host_ownership.managed_fields_sha256(first, fields),
            client_host_ownership.managed_fields_sha256(second, fields),
        )

    def test_schema_v1_hash_has_stable_vector_and_covers_every_managed_field(self):
        entry = {
            "command": "python",
            "args": ["-m", "installer.launcher"],
            "env": {"UNICODE": "值", "A": "1"},
            "opaque": "ignored",
        }
        expected = (
            "0fb74f0acfdd6ccdf12dd6dbc66838ae6"
            "0806044c7b64e4c6a62f7e8177fd3db"
        )

        self.assertEqual(
            client_host_ownership.managed_entry_sha256_v1(entry),
            expected,
        )
        opaque_change = dict(entry, opaque="changed")
        self.assertEqual(
            client_host_ownership.managed_entry_sha256_v1(opaque_change),
            expected,
        )
        mutations = {
            "command": dict(entry, command="other"),
            "args": dict(entry, args=["--other"]),
            "type": dict(entry, type="stdio"),
            "cwd": dict(entry, cwd="C:\\managed"),
            "env": dict(entry, env={"A": "2"}),
            "missing-command": {
                key: value for key, value in entry.items() if key != "command"
            },
            "missing-env": {
                key: value for key, value in entry.items() if key != "env"
            },
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                self.assertNotEqual(
                    client_host_ownership.managed_entry_sha256_v1(mutation),
                    expected,
                )

    def test_managed_fields_hash_rejects_nonfinite_json_numbers(self):
        with self.assertRaises(client_host_ownership.OwnershipError):
            client_host_ownership.managed_fields_sha256(
                {"env": {"INVALID": float("nan")}},
                ("env",),
            )

    def test_managed_fields_hash_rejects_invalid_inputs(self):
        cases = (
            ("not-an-entry", ("env",)),
            ({}, ("",)),
            ({}, (1,)),
            ({"env": {}}, ("env", "env")),
        )
        for entry, fields in cases:
            with self.subTest(entry=entry, fields=fields):
                with self.assertRaises(client_host_ownership.OwnershipError):
                    client_host_ownership.managed_fields_sha256(entry, fields)

    def test_ownership_record_path_rejects_unsafe_host_family(self):
        for host_family in (
            "",
            "Cursor",
            "../cursor",
            "cursor/other",
            "cursor\\other",
            "x" * 65,
            1,
        ):
            with self.subTest(host_family=host_family):
                with self.assertRaises(client_host_ownership.OwnershipError):
                    client_host_ownership.ownership_record_path(host_family)

    def test_missing_record_is_absent(self):
        self.assertIsNone(
            client_host_ownership.read_record_if_present(
                "cursor",
                managed_root=self.root,
                config_path=self.config_path,
                server_name="decision-engine",
            )
        )

    def test_valid_record_is_bound_to_paired_managed_install(self):
        self._write_record(self._payload())

        record = client_host_ownership.read_record_if_present(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.install_id, self.install_id)
        self.assertEqual(record.managed_fields_sha256, "1" * 64)
        self.assertEqual(record.skill_manifest_sha256, "2" * 64)

    def test_record_schema_and_identity_mismatches_fail_closed(self):
        base = self._payload()
        cases = {
            "missing-key": {
                key: value for key, value in base.items() if key != "skill_release_id"
            },
            "unknown-key": {**base, "unexpected": True},
            "wrong-schema": {**base, "schema": 2},
            "wrong-install-id": {
                **base,
                "install_id": "11111111-1111-4111-8111-111111111111",
            },
            "wrong-client": {**base, "client": "codex"},
            "relative-config": {**base, "config_path": "mcp.json"},
            "wrong-config": {
                **base,
                "config_path": str(self.config_path.with_name("other.json")),
            },
            "wrong-server": {**base, "server_name": "other"},
            "bad-entry-hash": {**base, "managed_fields_sha256": "not-a-hash"},
            "bad-skill-hash": {**base, "skill_manifest_sha256": "not-a-hash"},
            "bad-entry-schema": {**base, "managed_entry_schema": 2},
            "empty-release": {**base, "skill_release_id": ""},
            "oversized-release": {**base, "skill_release_id": "x" * 257},
            "control-release": {**base, "skill_release_id": "bad\nrelease"},
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                self._write_record(payload)
                with self.assertRaises(client_host_ownership.OwnershipError):
                    client_host_ownership.read_record_if_present(
                        "cursor",
                        managed_root=self.root,
                        config_path=self.config_path,
                        server_name="decision-engine",
                    )

    def test_duplicate_record_keys_fail_closed(self):
        path = self._write_record(self._payload())
        rendered = path.read_text(encoding="utf-8")
        path.write_text(
            rendered.replace('"schema": 1', '"schema": 1,\n  "schema": 1', 1),
            encoding="utf-8",
        )

        with self.assertRaises(client_host_ownership.OwnershipError):
            client_host_ownership.read_record_if_present(
                "cursor",
                managed_root=self.root,
                config_path=self.config_path,
                server_name="decision-engine",
            )

    def test_build_and_publish_record_binds_managed_entry_and_identity(self):
        entry = {
            "command": r"C:\Python313\python.exe",
            "args": ["-m", "installer.launcher"],
            "env": {
                "PYTHONPATH": str(self.root),
                "DE_AGENT_HOST": "cursor",
                "DE_ENABLE_LOCAL_UI": "0",
            },
            "opaque": {"preserved": True},
        }
        record = client_host_ownership.build_record(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
            managed_entry=entry,
            skill_release_id="release-synthetic",
            skill_manifest_sha256="2" * 64,
        )

        with client_host_ownership._activation_publication_scope():
            published_hash = client_host_ownership.publish_record(
                record,
                managed_root=self.root,
                expected_exists=False,
                expected_sha256=None,
            )

        path = client_host_ownership.ownership_record_path("cursor")
        self.assertEqual(
            published_hash,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        loaded = client_host_ownership.read_record_if_present(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
        )
        self.assertEqual(loaded, record)
        self.assertEqual(
            record.managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(entry),
        )

    def test_publish_record_requires_activation_lock_scope(self):
        record = client_host_ownership.build_record(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
            managed_entry={"command": "python", "args": [], "env": {}},
            skill_release_id="release-synthetic",
            skill_manifest_sha256="2" * 64,
        )

        with self.assertRaisesRegex(
            client_host_ownership.OwnershipError,
            "requires the host activation lock",
        ):
            client_host_ownership.publish_record(
                record,
                managed_root=self.root,
                expected_exists=False,
                expected_sha256=None,
            )

        self.assertFalse(
            client_host_ownership.ownership_record_path("cursor").exists()
        )

    def test_publish_record_cas_refuses_concurrent_record_change(self):
        record = client_host_ownership.build_record(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
            managed_entry={"command": "old", "args": [], "env": {}},
            skill_release_id="release-synthetic",
            skill_manifest_sha256="2" * 64,
        )
        with client_host_ownership._activation_publication_scope():
            expected_hash = client_host_ownership.publish_record(
                record,
                managed_root=self.root,
                expected_exists=False,
                expected_sha256=None,
            )
        path = client_host_ownership.ownership_record_path("cursor")
        concurrent = self._payload()
        concurrent["skill_release_id"] = "user-concurrent"
        self._write_record(concurrent)
        concurrent_bytes = path.read_bytes()
        replacement = client_host_ownership.build_record(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
            managed_entry={"command": "new", "args": [], "env": {}},
            skill_release_id="release-new",
            skill_manifest_sha256="3" * 64,
        )

        with self.assertRaisesRegex(
            client_host_ownership.OwnershipError,
            "changed while being published",
        ):
            with client_host_ownership._activation_publication_scope():
                client_host_ownership.publish_record(
                    replacement,
                    managed_root=self.root,
                    expected_exists=True,
                    expected_sha256=expected_hash,
                )

        self.assertEqual(path.read_bytes(), concurrent_bytes)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_publication_and_removal_primitives_are_host_neutral(self):
        trae_config = self.config_path.parent / "trae-mcp.json"
        record = client_host_ownership.build_record(
            "trae",
            managed_root=self.root,
            config_path=trae_config,
            server_name="decision-engine",
            managed_entry={"command": "python", "args": [], "env": {}},
            skill_release_id="release-synthetic",
            skill_manifest_sha256="2" * 64,
        )

        with client_host_ownership._activation_publication_scope():
            record_hash = client_host_ownership.publish_record(
                record,
                managed_root=self.root,
                expected_exists=False,
                expected_sha256=None,
            )

        path = client_host_ownership.ownership_record_path("trae")
        self.assertTrue(path.is_file())
        self.assertEqual(
            client_host_ownership.read_record_if_present(
                "trae",
                managed_root=self.root,
                config_path=trae_config,
                server_name="decision-engine",
            ),
            record,
        )

        with client_host_ownership._activation_publication_scope():
            client_host_ownership.remove_record(
                "trae",
                managed_root=self.root,
                expected_sha256=record_hash,
                quarantine_path=path.with_name("trae.json.quarantine"),
            )

        self.assertFalse(path.exists())
        self.assertFalse(path.with_name("trae.json.quarantine").exists())

    def test_remove_record_refuses_changed_bytes_before_move(self):
        record = client_host_ownership.build_record(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
            managed_entry={"command": "python", "args": [], "env": {}},
            skill_release_id="release-synthetic",
            skill_manifest_sha256="2" * 64,
        )
        with client_host_ownership._activation_publication_scope():
            expected_hash = client_host_ownership.publish_record(
                record,
                managed_root=self.root,
                expected_exists=False,
                expected_sha256=None,
            )
        path = client_host_ownership.ownership_record_path("cursor")
        changed = self._payload()
        changed["skill_release_id"] = "user-change"
        self._write_record(changed)
        changed_bytes = path.read_bytes()

        with (
            mock.patch.object(windows_security, "move_write_through") as move,
            client_host_ownership._activation_publication_scope(),
            self.assertRaisesRegex(
                client_host_ownership.OwnershipError,
                "user changes",
            ),
        ):
            client_host_ownership.remove_record(
                "cursor",
                managed_root=self.root,
                expected_sha256=expected_hash,
                quarantine_path=path.with_name("cursor.json.quarantine"),
            )

        move.assert_not_called()
        self.assertEqual(path.read_bytes(), changed_bytes)

    def test_remove_record_preserves_same_bytes_object_substitution(self):
        record = client_host_ownership.build_record(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
            managed_entry={"command": "python", "args": [], "env": {}},
            skill_release_id="release-synthetic",
            skill_manifest_sha256="2" * 64,
        )
        with client_host_ownership._activation_publication_scope():
            expected_hash = client_host_ownership.publish_record(
                record,
                managed_root=self.root,
                expected_exists=False,
                expected_sha256=None,
            )
        path = client_host_ownership.ownership_record_path("cursor")
        original = path.read_bytes()
        quarantine = path.with_name("cursor.json.quarantine")
        real_move = windows_security.move_write_through

        def substitute_then_move(source, destination, *, replace_existing):
            if Path(source) == path:
                replacement = path.with_name("replacement.json")
                replacement.write_bytes(original)
                os.replace(replacement, source)
            real_move(
                source,
                destination,
                replace_existing=replace_existing,
            )

        with (
            mock.patch.object(
                windows_security,
                "move_write_through",
                side_effect=substitute_then_move,
            ),
            client_host_ownership._activation_publication_scope(),
            self.assertRaisesRegex(
                client_host_ownership.OwnershipError,
                "changed during removal",
            ),
        ):
            client_host_ownership.remove_record(
                "cursor",
                managed_root=self.root,
                expected_sha256=expected_hash,
                quarantine_path=quarantine,
            )

        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(quarantine.exists())

    def test_build_record_rejects_untrusted_skill_metadata_without_write(self):
        path = client_host_ownership.ownership_record_path("cursor")
        for release_id, manifest_hash in (
            ("", "2" * 64),
            ("bad\nrelease", "2" * 64),
            ("release", "not-a-hash"),
        ):
            with self.subTest(release_id=release_id, manifest_hash=manifest_hash):
                with self.assertRaises(client_host_ownership.OwnershipError):
                    client_host_ownership.build_record(
                        "cursor",
                        managed_root=self.root,
                        config_path=self.config_path,
                        server_name="decision-engine",
                        managed_entry={"command": "python", "args": [], "env": {}},
                        skill_release_id=release_id,
                        skill_manifest_sha256=manifest_hash,
                    )
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
