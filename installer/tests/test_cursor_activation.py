from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from installer import (
    client_host_ownership,
    cursor_activation,
    managed_install,
    mcp_config,
    install,
)


class CursorInterpreterRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "deeppattern"
        self.root = self.home / "decision-engine"
        self.config_path = self.base / ".cursor" / "mcp.json"
        self.old_python = self.base / "removed-python" / "python.exe"
        self.new_python = Path(sys.executable).resolve()
        (self.root / ".git").mkdir(parents=True)
        (self.root / "installer").mkdir()
        (self.root / "installer" / "launcher.py").write_text(
            "# synthetic launcher\n",
            encoding="utf-8",
        )
        self._managed_home = mock.patch.object(
            managed_install.config,
            "DEFAULT_DEEPPATTERN_HOME",
            self.home,
        )
        self._managed_home.start()
        self.addCleanup(self._managed_home.stop)
        self._registration_root = mock.patch.object(
            mcp_config,
            "registration_root",
            return_value=self.root,
        )
        self._registration_root.start()
        self.addCleanup(self._registration_root.stop)
        self._cursor_config = mock.patch.dict(
            os.environ,
            {"CURSOR_CONFIG": str(self.config_path)},
        )
        self._cursor_config.start()
        self.addCleanup(self._cursor_config.stop)
        self.install_id = managed_install.write_managed_identity(self.root)
        self.old_entry = mcp_config.render_entry(
            client="cursor",
            python=str(self.old_python),
            cwd=self.root,
        )["mcpServers"]["decision-engine"]
        old_with_opaque = dict(self.old_entry)
        old_with_opaque["disabled"] = False
        old_with_opaque["metadata"] = {"ownerNote": "keep"}
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "mcpServers": {
                        "decision-engine": old_with_opaque,
                        "other": {"command": "keep"},
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        record = client_host_ownership.build_record(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
            managed_entry=self.old_entry,
            skill_release_id="release-synthetic",
            skill_manifest_sha256="2" * 64,
        )
        with cursor_activation.cursor_activation_lock():
            client_host_ownership.publish_record(
                record,
                managed_root=self.root,
                expected_exists=False,
                expected_sha256=None,
            )
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
            return_value=str(self.new_python),
        )
        self._interpreter.start()
        self.addCleanup(self._interpreter.stop)
        self._active_skills = mock.patch.object(
            cursor_activation,
            "_verify_owned_active_skills",
            return_value=None,
        )
        self._active_skills_mock = self._active_skills.start()
        self.addCleanup(self._active_skills.stop)

    def _entry(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))[
            "mcpServers"
        ]["decision-engine"]

    def _record(self) -> client_host_ownership.OwnershipRecord:
        record = client_host_ownership.read_record_if_present(
            "cursor",
            managed_root=self.root,
            config_path=self.config_path,
            server_name="decision-engine",
        )
        self.assertIsNotNone(record)
        return record

    def test_doctor_skill_snapshot_validates_root_before_read_only_verify(self):
        record = self._record()

        cursor_activation.verify_owned_active_skills_for_doctor(
            record,
            managed_root=self.root,
        )

        self._active_skills_mock.assert_called_once_with(
            record,
            managed_root=self.root,
        )
        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "managed install identity is invalid",
        ):
            cursor_activation.verify_owned_active_skills_for_doctor(
                record,
                managed_root=self.base / "other",
            )
        self.assertEqual(self._active_skills_mock.call_count, 1)

    def test_interpreter_repair_commits_entry_and_record_together(self):
        result = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )

        entry = self._entry()
        record = self._record()
        self.assertEqual(result["action"], "repaired")
        self.assertEqual(entry["command"], str(self.new_python))
        self.assertFalse(entry["disabled"])
        self.assertEqual(entry["metadata"], {"ownerNote": "keep"})
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))[
                "mcpServers"
            ]["other"],
            {"command": "keep"},
        )
        self.assertEqual(
            record.managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(entry),
        )
        self.assertEqual(record.skill_release_id, "release-synthetic")
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["operation"], "interpreter_repair")
        self.assertEqual(journal["phase"], "committed")

    def test_committed_journal_does_not_block_unrelated_config_edit(self):
        cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["userOpaqueTopLevel"] = {"keep": True}
        self.config_path.write_text(
            json.dumps(data, indent=2) + "\n",
            encoding="utf-8",
        )

        repeated = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )

        self.assertEqual(repeated["action"], "unchanged")
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))[
                "userOpaqueTopLevel"
            ],
            {"keep": True},
        )

    def test_legacy_repair_then_shared_managed_write_updates_without_record_lock(self):
        cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )
        config_before = self.config_path.read_bytes()
        record_path = client_host_ownership.ownership_record_path("cursor")
        record_before = record_path.read_bytes()

        # `old_python` is deliberately a REMOVED interpreter — it stands for the legacy state
        # this flow migrates away from, not for anything worth registering. Both write-time
        # gates are stubbed for that reason: the subject here is the ownership record, while
        # the interpreter gate's own refusal of an unrunnable path is covered by
        # InterpreterGateTestCase in test_mcp_config_write.
        with mock.patch.object(
            mcp_config, "_cursor_write_version_gate", return_value=None
        ), mock.patch.object(mcp_config, "_interpreter_defect", return_value=None):
            mcp_config.write_entry(
                "cursor",
                python=str(self.old_python),
                cwd=self.root,
            )

        self.assertNotEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(record_path.read_bytes(), record_before)

    def test_crash_after_mcp_publish_resumes_record_publication_idempotently(self):
        def fail_after_mcp(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
                fault_injector=fail_after_mcp,
            )
        self.assertEqual(self._entry()["command"], str(self.new_python))
        self.assertEqual(
            self._record().managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(self.old_entry),
        )

        resumed = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )
        repeated = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )

        self.assertEqual(resumed["action"], "recovered")
        self.assertEqual(repeated["action"], "unchanged")
        self.assertEqual(
            self._record().managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(self._entry()),
        )

    def test_crash_after_intent_resumes_from_exact_pre_state(self):
        config_before = self.config_path.read_bytes()
        record_path = client_host_ownership.ownership_record_path("cursor")
        record_before = record_path.read_bytes()

        def fail_after_intent(point: str) -> None:
            if point == "after_intent":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
                fault_injector=fail_after_intent,
            )
        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(record_path.read_bytes(), record_before)

        resumed = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )

        self.assertEqual(resumed["action"], "recovered")
        self.assertEqual(self._entry()["command"], str(self.new_python))
        self.assertEqual(
            self._record().managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(self._entry()),
        )

    def test_incomplete_repair_rejects_a_different_requested_interpreter(self):
        def fail_after_intent(point: str) -> None:
            if point == "after_intent":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
                fault_injector=fail_after_intent,
            )
        config_before = self.config_path.read_bytes()
        record_path = client_host_ownership.ownership_record_path("cursor")
        record_before = record_path.read_bytes()
        with mock.patch.object(
            cursor_activation,
            "_validate_repair_interpreter",
            return_value=str(self.base / "other-python.exe"),
        ):
            with self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "conflicts with the incomplete",
            ):
                cursor_activation.repair_cursor_interpreter(
                    managed_root=self.root,
                    python=self.new_python,
                )

        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(record_path.read_bytes(), record_before)

    def test_crash_after_ownership_publish_only_needs_verify_and_commit(self):
        def fail_after_ownership(point: str) -> None:
            if point == "after_ownership_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
                fault_injector=fail_after_ownership,
            )
        self.assertEqual(
            self._record().managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(self._entry()),
        )

        resumed = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )

        self.assertEqual(resumed["action"], "recovered")
        journal = json.loads(
            cursor_activation.activation_journal_path().read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["phase"], "committed")

    def test_crash_after_mcp_apply_before_phase_update_recovers(self):
        def fail_after_apply(point: str) -> None:
            if point == "after_mcp_apply_before_phase":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
                fault_injector=fail_after_apply,
            )

        resumed = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )

        self.assertEqual(resumed["action"], "recovered")
        self.assertEqual(self._entry()["command"], str(self.new_python))
        self.assertEqual(
            self._record().managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(self._entry()),
        )

    def test_crash_after_ownership_apply_before_phase_update_recovers(self):
        def fail_after_apply(point: str) -> None:
            if point == "after_ownership_apply_before_phase":
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
                fault_injector=fail_after_apply,
            )

        resumed = cursor_activation.repair_cursor_interpreter(
            managed_root=self.root,
            python=self.new_python,
        )

        self.assertEqual(resumed["action"], "recovered")
        self.assertEqual(
            self._record().managed_fields_sha256,
            client_host_ownership.managed_entry_sha256_v1(self._entry()),
        )

    def test_third_state_after_crash_fails_closed_without_overwrite(self):
        def fail_after_mcp(point: str) -> None:
            if point == "after_mcp_published":
                raise RuntimeError("synthetic crash")

        with self.assertRaises(RuntimeError):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
                fault_injector=fail_after_mcp,
            )
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["mcpServers"]["decision-engine"]["command"] = "user-third-state"
        self.config_path.write_text(
            json.dumps(data, indent=2) + "\n",
            encoding="utf-8",
        )
        third_state = self.config_path.read_bytes()
        record_before = client_host_ownership.ownership_record_path(
            "cursor"
        ).read_bytes()

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "repair_required",
        ):
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
            )

        self.assertEqual(self.config_path.read_bytes(), third_state)
        self.assertEqual(
            client_host_ownership.ownership_record_path("cursor").read_bytes(),
            record_before,
        )

    def test_corrupt_journal_is_redacted_and_never_mutates_artifacts(self):
        config_before = self.config_path.read_bytes()
        record_path = client_host_ownership.ownership_record_path("cursor")
        record_before = record_path.read_bytes()
        state_root = cursor_activation.cursor_activation_root()
        managed_install._ensure_private_registration_directory(state_root)
        private = r"C:\private-user\journal-secret"
        cursor_activation.activation_journal_path().write_text(
            json.dumps({"schema": 1, "private": private}),
            encoding="utf-8",
        )

        with self.assertRaises(cursor_activation.CursorActivationError) as caught:
            cursor_activation.repair_cursor_interpreter(
                managed_root=self.root,
                python=self.new_python,
            )

        self.assertIn("repair_required", str(caught.exception))
        self.assertNotIn(private, str(caught.exception))
        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(record_path.read_bytes(), record_before)

    def test_install_cli_uses_shared_client_repair_entrypoint(self):
        output = io.StringIO()
        with mock.patch.object(
            install,
            "repair_client_integration",
            return_value={
                "client": "cursor",
                "mcp": {"action": "updated"},
                "routed_skills": ["audit"],
            },
        ) as repair, redirect_stdout(output):
            rc = install.main(["--repair-client", "cursor"])

        self.assertEqual(rc, 0)
        repair.assert_called_once_with(
            "cursor",
            managed_install.config.managed_component_root("decision-engine"),
        )
        self.assertIn("cursor", output.getvalue())
        self.assertIn("updated", output.getvalue())

    def test_cursor_shared_writer_does_not_depend_on_legacy_activation_lock(self):
        with cursor_activation.cursor_activation_lock():
            with mock.patch.object(mcp_config, "_cursor_write_version_gate", return_value=None):
                mcp_config.write_entry(
                    "cursor",
                    python=str(self.new_python),
                    cwd=self.root,
                )

    def test_activation_lock_blocks_a_separate_windows_process(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "from installer import cursor_activation, managed_install; "
            "managed_install.config.DEFAULT_DEEPPATTERN_HOME = Path(sys.argv[2]); "
            "\ntry:\n"
            "  with cursor_activation.cursor_activation_lock(): print('acquired')\n"
            "except cursor_activation.CursorActivationError: print('blocked')\n"
        )

        with cursor_activation.cursor_activation_lock():
            completed = cursor_activation.subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    str(repo_root),
                    str(self.home),
                ],
                stdin=cursor_activation.subprocess.DEVNULL,
                stdout=cursor_activation.subprocess.PIPE,
                stderr=cursor_activation.subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "blocked")

    def test_interpreter_validation_uses_isolated_bounded_import_probe(self):
        self._interpreter.stop()
        completed = mock.Mock(returncode=0)

        with mock.patch.object(
            cursor_activation.subprocess,
            "run",
            return_value=completed,
        ) as run:
            result = cursor_activation._validate_repair_interpreter(
                self.new_python,
                self.root,
                minimum_python=tuple(sys.version_info[:2]),
            )

        self.assertEqual(result, str(self.new_python))
        command = run.call_args.args[0]
        self.assertEqual(command[0], str(self.new_python))
        self.assertEqual(command[1:3], ["-I", "-c"])
        self.assertEqual(command[-1], str(self.root))
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertIs(
            run.call_args.kwargs["stdin"],
            cursor_activation.subprocess.DEVNULL,
        )
        self.assertIs(
            run.call_args.kwargs["stdout"],
            cursor_activation.subprocess.DEVNULL,
        )
        self.assertIs(
            run.call_args.kwargs["stderr"],
            cursor_activation.subprocess.DEVNULL,
        )

    def test_interpreter_validation_rejects_bad_candidate_floor_and_probe(self):
        self._interpreter.stop()

        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "unavailable",
        ):
            cursor_activation._validate_repair_interpreter(
                self.old_python,
                self.root,
            )
        with self.assertRaisesRegex(
            cursor_activation.CursorActivationError,
            "compatible running Python",
        ):
            cursor_activation._validate_repair_interpreter(
                self.new_python,
                self.root,
                minimum_python=(sys.version_info.major + 1, 0),
            )
        with mock.patch.object(
            cursor_activation.subprocess,
            "run",
            return_value=mock.Mock(returncode=1),
        ):
            with self.assertRaisesRegex(
                cursor_activation.CursorActivationError,
                "import probe failed",
            ):
                cursor_activation._validate_repair_interpreter(
                    self.new_python,
                    self.root,
                    minimum_python=tuple(sys.version_info[:2]),
                )


if __name__ == "__main__":
    unittest.main()
