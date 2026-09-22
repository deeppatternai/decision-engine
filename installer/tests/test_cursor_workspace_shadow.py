from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from installer import client_host_ownership, doctor, mcp_config, windows_security
from installer.config import ShellError


class CursorWorkspaceShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.workspace = self.base / "workspace with 空格"
        self.project_config = self.workspace / ".cursor" / "mcp.json"
        self.global_config = self.base / "global" / "mcp.json"
        self.env = mock.patch.dict(
            os.environ,
            {"CURSOR_CONFIG": str(self.global_config)},
        )
        self.root = mock.patch.object(
            mcp_config,
            "registration_root",
            return_value=self.base / "managed",
        )
        self.ownership = mock.patch.object(
            client_host_ownership,
            "read_record_if_present",
            return_value=None,
        )
        self.env.start()
        self.root.start()
        self.ownership.start()
        self.addCleanup(self.ownership.stop)
        self.addCleanup(self.root.stop)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _managed_entry(self) -> dict:
        return mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]

    def _write(self, path: Path, entry: dict, **extra) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mcpServers": {
                "decision-engine": entry,
                "other": {"command": "keep-private-sibling"},
            },
            **extra,
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        path.write_bytes(raw)
        return raw

    def _write_skill(self, relative_root: str, name: str, secret: str) -> Path:
        skill = self.workspace / relative_root / name
        skill.mkdir(parents=True, exist_ok=True)
        sentinel = skill / "SKILL.md"
        sentinel.write_text(secret, encoding="utf-8")
        return sentinel

    def test_matching_project_entry_is_still_unowned_conflict_without_writes(self):
        entry = self._managed_entry()
        self._write(self.global_config, entry)
        original = self._write(
            self.project_config,
            entry,
            private="project-secret-value",
        )
        before = sorted(path.relative_to(self.workspace) for path in self.workspace.rglob("*"))

        shadow = mcp_config.cursor_workspace_shadow(self.workspace)

        self.assertIsNotNone(shadow)
        self.assertEqual(shadow.global_source, "launcher-shape")
        self.assertEqual(shadow.project_source, "launcher-shape")
        self.assertTrue(shadow.same_name_conflict)
        self.assertEqual(self.project_config.read_bytes(), original)
        self.assertEqual(
            sorted(path.relative_to(self.workspace) for path in self.workspace.rglob("*")),
            before,
        )

    def test_foreign_project_entry_is_a_same_name_conflict(self):
        self._write(self.global_config, self._managed_entry())
        self._write(
            self.project_config,
            {
                "command": "foreign-command",
                "args": ["--foreign"],
                "env": {"PRIVATE": "do-not-render"},
            },
        )

        shadow = mcp_config.cursor_workspace_shadow(self.workspace)

        self.assertEqual(shadow.global_source, "launcher-shape")
        self.assertEqual(shadow.project_source, "foreign")
        self.assertTrue(shadow.same_name_conflict)

    def test_matching_foreign_global_and_project_entries_remain_a_conflict(self):
        foreign = {
            "command": "foreign-command",
            "args": ["--foreign"],
            "env": {"PRIVATE": "do-not-render"},
        }
        self._write(self.global_config, foreign)
        self._write(self.project_config, foreign)

        shadow = mcp_config.cursor_workspace_shadow(self.workspace)

        self.assertEqual(shadow.global_source, "foreign")
        self.assertEqual(shadow.project_source, "foreign")
        self.assertTrue(shadow.same_name_conflict)

    def test_legacy_project_entry_has_fixed_source_summary(self):
        legacy = dict(
            self._managed_entry(),
            args=["-m", "installer.shim"],
        )
        self._write(self.project_config, legacy)

        shadow = mcp_config.cursor_workspace_shadow(self.workspace)

        self.assertEqual(shadow.global_source, "absent")
        self.assertEqual(shadow.project_source, "legacy-shim-shape")
        self.assertTrue(shadow.same_name_conflict)

    def test_launcher_pair_embedded_in_unrelated_args_is_foreign(self):
        self._write(
            self.project_config,
            {
                "command": str(self.base / "python.exe"),
                "args": ["--unrelated", "-m", "installer.launcher"],
            },
        )

        shadow = mcp_config.cursor_workspace_shadow(self.workspace)

        self.assertEqual(shadow.project_source, "foreign")
        self.assertTrue(shadow.same_name_conflict)

    def test_project_without_decision_engine_entry_has_no_shadow(self):
        self.project_config.parent.mkdir(parents=True)
        self.project_config.write_text(
            json.dumps({"mcpServers": {"other": {"command": "keep"}}}),
            encoding="utf-8",
        )

        self.assertIsNone(mcp_config.cursor_workspace_shadow(self.workspace))

    def test_malformed_and_oversized_project_config_fail_without_echo(self):
        secret = "project-secret-body"
        cases = {
            "malformed": ("{" + secret).encode("utf-8"),
            "oversized": b"{" + b"x" * mcp_config.MAX_AGENT_CONFIG_BYTES,
            "deeply-nested": (
                b'{"mcpServers":'
                + b"[" * 1100
                + b"]" * 1100
                + b"}"
            ),
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                self.project_config.parent.mkdir(parents=True, exist_ok=True)
                self.project_config.write_bytes(raw)
                with self.assertRaises(ShellError) as caught:
                    mcp_config.cursor_workspace_shadow(self.workspace)
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(str(self.workspace), str(caught.exception))

    def test_explicit_null_collection_or_entry_is_invalid(self):
        cases = (
            {"mcpServers": None},
            {"mcpServers": {"decision-engine": None}},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.project_config.parent.mkdir(parents=True, exist_ok=True)
                self.project_config.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaises(ShellError):
                    mcp_config.cursor_workspace_shadow(self.workspace)

    def test_link_and_special_file_project_config_are_rejected(self):
        self.project_config.parent.mkdir(parents=True, exist_ok=True)
        target = self.base / "outside-secret.json"
        target.write_text(
            json.dumps({"mcpServers": {"decision-engine": self._managed_entry()}}),
            encoding="utf-8",
        )
        try:
            self.project_config.symlink_to(target)
        except OSError as exc:
            self.skipTest("file symlinks unavailable: %s" % type(exc).__name__)

        with self.assertRaises(ShellError):
            mcp_config.cursor_workspace_shadow(self.workspace)

        self.project_config.unlink()
        self.project_config.mkdir()
        with self.assertRaises(ShellError):
            mcp_config.cursor_workspace_shadow(self.workspace)

        self.project_config.rmdir()
        self.project_config.parent.rmdir()
        outside_cursor = self.base / "outside-cursor"
        self._write(outside_cursor / "mcp.json", self._managed_entry())
        self.project_config.parent.symlink_to(
            outside_cursor,
            target_is_directory=True,
        )
        with self.assertRaises(ShellError):
            mcp_config.cursor_workspace_shadow(self.workspace)

    def test_invalid_global_config_error_does_not_echo_path_or_content(self):
        secret = "global-private-body"
        self._write(self.project_config, self._managed_entry())
        self.global_config.parent.mkdir(parents=True, exist_ok=True)
        self.global_config.write_text("{" + secret, encoding="utf-8")

        with self.assertRaises(ShellError) as caught:
            mcp_config.cursor_workspace_shadow(self.workspace)

        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(str(self.global_config), str(caught.exception))

    def test_invalid_workspace_is_warn_not_false_pass(self):
        missing = self.base / "missing-workspace"
        regular_file = self.base / "not-a-workspace"
        regular_file.write_text("private-workspace-value", encoding="utf-8")

        for workspace in (missing, regular_file):
            with self.subTest(workspace=workspace):
                result = doctor.check_cursor_workspace(workspace)
                self.assertEqual(result.status, "WARN")
                self.assertEqual(result.name, "cursor-workspace")
                self.assertNotIn(str(workspace), result.detail)
                self.assertNotIn("private-workspace-value", result.detail)

    def test_doctor_reports_malformed_and_oversized_as_redacted_warn(self):
        secret = "project-secret-body"
        cases = (
            ("{" + secret).encode("utf-8"),
            b"{" + b"x" * mcp_config.MAX_AGENT_CONFIG_BYTES,
        )
        for raw in cases:
            with self.subTest(size=len(raw)):
                self.project_config.parent.mkdir(parents=True, exist_ok=True)
                self.project_config.write_bytes(raw)

                result = doctor.check_cursor_workspace(self.workspace)

                self.assertEqual(result.status, "WARN")
                rendered = "%s %s" % (result.detail, result.fix)
                self.assertNotIn(secret, rendered)
                self.assertNotIn(str(self.workspace), rendered)

    def test_doctor_reports_no_project_entry_as_pass(self):
        self.project_config.parent.mkdir(parents=True)
        self.project_config.write_text(
            json.dumps({"mcpServers": {"other": {"command": "keep"}}}),
            encoding="utf-8",
        )

        result = doctor.check_cursor_workspace(self.workspace)

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            result.detail,
            "no project-level Decision Engine MCP or known skill shadow",
        )

    def test_doctor_reports_only_redacted_source_categories(self):
        self._write(self.global_config, self._managed_entry())
        project_raw = self._write(
            self.project_config,
            {
                "command": "private-command-value",
                "args": ["private-argument-value"],
            },
        )

        result = doctor.check_cursor_workspace(self.workspace)

        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.name, "cursor-workspace")
        self.assertIn("project_shadow_detected", result.detail)
        self.assertIn("precedence=unverified", result.detail)
        self.assertIn("global=launcher-shape", result.detail)
        self.assertIn("project=foreign", result.detail)
        self.assertIn("same_name_unowned", result.detail)
        self.assertNotIn("private-command-value", result.detail)
        self.assertNotIn(str(self.workspace), result.detail)
        self.assertEqual(self.project_config.read_bytes(), project_raw)

    def test_doctor_matching_project_entry_still_reports_unowned_conflict(self):
        entry = self._managed_entry()
        self._write(self.global_config, entry)
        self._write(self.project_config, entry)

        result = doctor.check_cursor_workspace(self.workspace)

        self.assertEqual(result.status, "WARN")
        self.assertIn("global=launcher-shape", result.detail)
        self.assertIn("project=launcher-shape", result.detail)
        self.assertIn("conflict=same_name_unowned", result.detail)

    def test_workspace_check_failure_cannot_abort_run_all(self):
        with (
            mock.patch.object(doctor, "CHECKS", ()),
            mock.patch.object(
                doctor,
                "check_cursor_workspace",
                side_effect=RuntimeError("private failure"),
            ),
        ):
            results = doctor.run_all(workspace=self.workspace)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "FAIL")
        self.assertEqual(results[0].name, "cursor-workspace")
        self.assertNotIn("private failure", results[0].detail)

    def test_project_skill_shadows_report_fixed_known_names_without_writes(self):
        cursor_secret = "cursor-private-skill-body"
        agents_secret = "agents-private-skill-body"
        cursor_sentinel = self._write_skill(
            ".cursor/skills",
            "audit",
            cursor_secret,
        )
        agents_sentinel = self._write_skill(
            ".agents/skills",
            "discussion-board",
            agents_secret,
        )
        self._write_skill(".cursor/skills", "unrelated-private-skill", "ignore")
        before = sorted(path.relative_to(self.workspace) for path in self.workspace.rglob("*"))

        shadow = mcp_config.cursor_workspace_skill_shadow(
            self.workspace,
            doctor.DE_SKILLS,
        )

        self.assertIsNotNone(shadow)
        self.assertEqual(shadow.cursor_skills, ("audit",))
        self.assertEqual(shadow.agents_skills, ("discussion-board",))
        self.assertEqual(cursor_sentinel.read_text(encoding="utf-8"), cursor_secret)
        self.assertEqual(agents_sentinel.read_text(encoding="utf-8"), agents_secret)
        self.assertEqual(
            sorted(path.relative_to(self.workspace) for path in self.workspace.rglob("*")),
            before,
        )

    def test_unknown_project_skill_names_are_not_enumerated(self):
        self._write_skill(".cursor/skills", "private-one", "private-body")
        self._write_skill(".agents/skills", "private-two", "private-body")

        shadow = mcp_config.cursor_workspace_skill_shadow(
            self.workspace,
            doctor.DE_SKILLS,
        )

        self.assertIsNone(shadow)

    def test_unsafe_requested_skill_name_is_rejected_before_workspace_access(self):
        missing = self.base / "missing-workspace"

        for name in (
            "../private",
            "nested/private",
            "..",
            "..\\private",
            "",
            ".",
            ".hidden",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ShellError) as caught:
                    mcp_config.cursor_workspace_skill_shadow(
                        missing,
                        (name,),
                    )
                if name:
                    self.assertNotIn(name, str(caught.exception))
                self.assertNotIn(str(missing), str(caught.exception))

    def test_linked_or_non_directory_skill_scope_is_rejected(self):
        self.workspace.mkdir(parents=True)
        outside = self.base / "outside-skills"
        self._write_skill("../outside-skills", "audit", "outside-private-body")
        cursor_root = self.workspace / ".cursor"
        cursor_root.mkdir()
        try:
            (cursor_root / "skills").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest("directory symlinks unavailable: %s" % type(exc).__name__)

        with self.assertRaises(ShellError):
            mcp_config.cursor_workspace_skill_shadow(
                self.workspace,
                doctor.DE_SKILLS,
            )

        (cursor_root / "skills").unlink()
        agents_root = self.workspace / ".agents"
        agents_root.write_text("private-parent-value", encoding="utf-8")
        with self.assertRaises(ShellError) as caught:
            mcp_config.cursor_workspace_skill_shadow(
                self.workspace,
                doctor.DE_SKILLS,
            )
        self.assertNotIn("private-parent-value", str(caught.exception))
        self.assertNotIn(str(self.workspace), str(caught.exception))

    def test_regular_file_is_not_a_skill_but_link_leaf_is_conservative_shadow(self):
        leaf = self.workspace / ".cursor" / "skills" / "audit"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("not-a-skill-directory", encoding="utf-8")

        self.assertIsNone(
            mcp_config.cursor_workspace_skill_shadow(
                self.workspace,
                doctor.DE_SKILLS,
            )
        )

        leaf.unlink()
        target = self.base / "outside-audit-skill"
        target.mkdir()
        try:
            leaf.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest("directory symlinks unavailable: %s" % type(exc).__name__)
        shadow = mcp_config.cursor_workspace_skill_shadow(
            self.workspace,
            doctor.DE_SKILLS,
        )
        self.assertEqual(shadow.cursor_skills, ("audit",))

    def test_skill_probe_does_not_open_bodies_or_enumerate_directories(self):
        self._write_skill(".cursor/skills", "audit", "private-body")
        self._write_skill(".cursor/skills", "private-unknown", "private-body")

        with (
            mock.patch.object(Path, "open", side_effect=AssertionError("body opened")),
            mock.patch.object(Path, "iterdir", side_effect=AssertionError("enumerated")),
            mock.patch.object(Path, "glob", side_effect=AssertionError("globbed")),
            mock.patch.object(Path, "rglob", side_effect=AssertionError("rglobbed")),
            mock.patch.object(
                mcp_config.os,
                "listdir",
                side_effect=AssertionError("listed"),
            ),
            mock.patch.object(
                mcp_config.os,
                "scandir",
                side_effect=AssertionError("scanned"),
            ),
        ):
            shadow = mcp_config.cursor_workspace_skill_shadow(
                self.workspace,
                doctor.DE_SKILLS,
            )

        self.assertEqual(shadow.cursor_skills, ("audit",))
        self.assertEqual(shadow.agents_skills, ())

    @unittest.skipUnless(os.name == "nt", "Windows directory pinning is target-specific")
    def test_windows_skill_probe_pins_and_revalidates_every_ancestor(self):
        self._write_skill(".cursor/skills", "audit", "private-body")
        entered = []
        validated = []

        class RecordingPin:
            def __init__(self, path):
                self.path = Path(path)

            def __enter__(self):
                entered.append(self.path)
                return self

            def validate(self):
                validated.append(self.path)

            def __exit__(self, _type, _value, _traceback):
                return None

        with mock.patch(
            "installer.windows_security.PinnedWindowsDirectory",
            RecordingPin,
        ):
            shadow = mcp_config.cursor_workspace_skill_shadow(
                self.workspace,
                doctor.DE_SKILLS,
            )

        expected = {
            self.workspace,
            self.workspace / ".cursor",
            self.workspace / ".cursor" / "skills",
        }
        self.assertEqual(shadow.cursor_skills, ("audit",))
        self.assertTrue(expected.issubset(set(entered)))
        self.assertTrue(expected.issubset(set(validated)))

    @unittest.skipUnless(os.name == "nt", "Windows directory pinning is target-specific")
    def test_windows_skill_probe_maps_pinned_scope_change_to_shell_error(self):
        self._write_skill(".cursor/skills", "audit", "private-body")

        class TamperedPin:
            def __init__(self, path):
                self.path = Path(path)

            def __enter__(self):
                return self

            def validate(self):
                if self.path.name == "skills":
                    raise windows_security.WindowsSecurityError(
                        "private tamper detail"
                    )

            def __exit__(self, _type, _value, _traceback):
                return None

        with mock.patch(
            "installer.windows_security.PinnedWindowsDirectory",
            TamperedPin,
        ):
            with self.assertRaises(ShellError) as caught:
                mcp_config.cursor_workspace_skill_shadow(
                    self.workspace,
                    doctor.DE_SKILLS,
                )

        self.assertIn("changed during diagnosis", str(caught.exception))
        self.assertNotIn("private tamper detail", str(caught.exception))

    def test_doctor_warns_for_skill_shadow_without_project_mcp_entry(self):
        secret = "private-skill-body"
        sentinel = self._write_skill(".agents/skills", "graphic-explanation", secret)

        result = doctor.check_cursor_workspace(self.workspace)

        self.assertEqual(result.status, "WARN")
        self.assertIn("project_skill_shadow_detected", result.detail)
        self.assertIn("cursor_skills=absent", result.detail)
        self.assertIn("agents_skills=graphic-explanation", result.detail)
        rendered = "%s %s" % (result.detail, result.fix)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(str(self.workspace), rendered)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), secret)

    def test_doctor_combines_mcp_and_skill_shadow_diagnostics(self):
        self._write(self.project_config, self._managed_entry())
        self._write_skill(".cursor/skills", "discussion-board", "private-body")

        result = doctor.check_cursor_workspace(self.workspace)

        self.assertEqual(result.status, "WARN")
        self.assertIn("project_shadow_detected", result.detail)
        self.assertIn("conflict=same_name_unowned", result.detail)
        self.assertIn("project_skill_shadow_detected", result.detail)
        self.assertIn("cursor_skills=discussion-board", result.detail)
        self.assertIn("agents_skills=absent", result.detail)

    def test_doctor_keeps_mcp_shadow_when_skill_diagnosis_fails(self):
        self._write(self.project_config, self._managed_entry())
        agents_root = self.workspace / ".agents"
        agents_root.write_text("private-invalid-scope", encoding="utf-8")

        result = doctor.check_cursor_workspace(self.workspace)

        self.assertEqual(result.status, "WARN")
        self.assertIn("project_shadow_detected", result.detail)
        self.assertIn("conflict=same_name_unowned", result.detail)
        self.assertIn("skill_diagnosis=unreadable_or_invalid", result.detail)
        self.assertNotIn("private-invalid-scope", result.detail)
        self.assertNotIn(str(self.workspace), result.detail)

    def test_doctor_keeps_skill_shadow_when_mcp_diagnosis_fails(self):
        self.project_config.parent.mkdir(parents=True)
        self.project_config.write_text("{private-invalid-mcp", encoding="utf-8")
        self._write_skill(".agents/skills", "audit", "private-skill-body")

        result = doctor.check_cursor_workspace(self.workspace)

        self.assertEqual(result.status, "WARN")
        self.assertIn("mcp_diagnosis=unreadable_or_invalid", result.detail)
        self.assertIn("project_skill_shadow_detected", result.detail)
        self.assertIn("agents_skills=audit", result.detail)
        self.assertNotIn("private-invalid-mcp", result.detail)
        self.assertNotIn(str(self.workspace), result.detail)

    def test_doctor_cli_workspace_json_appends_shadow_check(self):
        self._write(self.project_config, self._managed_entry())
        output = io.StringIO()

        with mock.patch.object(doctor, "CHECKS", ()), redirect_stdout(output):
            rc = doctor.main(
                ["--json", "--workspace", str(self.workspace)]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["name"], "cursor-workspace")
        self.assertEqual(payload["results"][0]["status"], "WARN")


if __name__ == "__main__":
    unittest.main()
