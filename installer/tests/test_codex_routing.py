"""Behavior tests for retiring the legacy Codex global AGENTS.md block."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import codex_routing
from installer.config import ShellError

ROOT = Path(__file__).resolve().parents[2]


class CodexRoutingRetirementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.agents = Path(self._tmp.name) / ".codex" / "AGENTS.md"
        self.agents.parent.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"CODEX_AGENTS_MD": str(self.agents)})
        env.start()
        self.addCleanup(env.stop)

    def test_retire_removes_only_the_managed_block_and_keeps_a_backup(self):
        self.agents.write_text(
            "user prefix\n\n%s\nlegacy DE prose\n%s\n\nuser suffix\n"
            % (codex_routing._BEGIN, codex_routing._END),
            encoding="utf-8",
        )

        result = codex_routing.retire_routing()

        self.assertEqual(result["action"], "removed")
        self.assertEqual(
            self.agents.read_text(encoding="utf-8"),
            "user prefix\n\n\n\nuser suffix\n",
        )
        self.assertTrue(Path(result["backup"]).is_file())

    def test_repository_has_no_global_routing_fragment_or_writer(self):
        self.assertFalse((ROOT / "integrations" / "codex" / "AGENTS.md").exists())
        self.assertFalse(hasattr(codex_routing, "write_routing"))
        install_text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("install the Codex routing fragment", install_text)
        self.assertNotIn("paste integrations/codex/AGENTS.md", install_text)

    def test_absent_file_is_a_noop_and_is_not_created(self):
        result = codex_routing.retire_routing()

        self.assertEqual(result["action"], "unchanged")
        self.assertIsNone(result["backup"])
        self.assertFalse(self.agents.exists())

    def test_unmarked_user_file_is_unchanged(self):
        self.agents.write_bytes(b"# user rules\r\n")

        result = codex_routing.retire_routing()

        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(self.agents.read_bytes(), b"# user rules\r\n")

    def test_crlf_outside_the_managed_block_is_preserved_byte_for_byte(self):
        self.agents.write_bytes(
            b"user prefix\r\n\r\n"
            + codex_routing._BEGIN.encode("utf-8")
            + b"\r\nlegacy\r\n"
            + codex_routing._END.encode("utf-8")
            + b"\r\n\r\nuser suffix\r\n"
        )

        codex_routing.retire_routing()

        self.assertEqual(
            self.agents.read_bytes(),
            b"user prefix\r\n\r\n\r\n\r\nuser suffix\r\n",
        )

    def test_automatic_cleanup_defers_until_all_four_replacement_skills_exist(self):
        self.agents.write_text(
            "%s\nlegacy\n%s\n" % (codex_routing._BEGIN, codex_routing._END),
            encoding="utf-8",
        )
        body = Path(self._tmp.name) / "body"

        result = codex_routing.retire_routing(require_skills_root=body)

        self.assertEqual(result["action"], "deferred (skills unavailable)")
        self.assertIn(codex_routing._BEGIN, self.agents.read_text(encoding="utf-8"))

    def test_replacement_skill_gate_accepts_the_four_exact_routes(self):
        body = Path(self._tmp.name) / "body"
        for name in codex_routing.REPLACEMENT_SKILLS:
            skill = body / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: %s\n---\n" % name,
                encoding="utf-8",
            )

        with mock.patch.object(
            codex_routing.config,
            "codex_skills_dir",
            return_value=body / "skills",
        ):
            self.assertTrue(codex_routing.replacement_skills_ready(body))

    def test_replacement_skill_gate_respects_codex_skill_support_probe(self):
        body = Path(self._tmp.name) / "body"
        for name in codex_routing.REPLACEMENT_SKILLS:
            skill = body / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("skill", encoding="utf-8")

        with (
            mock.patch.object(
                codex_routing.config,
                "codex_skills_dir",
                return_value=body / "skills",
            ),
            mock.patch.object(
                codex_routing.config,
                "codex_skills_in_use",
                return_value=False,
            ),
        ):
            self.assertFalse(codex_routing.replacement_skills_ready(body))

    def test_symlink_backed_agents_file_is_refused_without_touching_target(self):
        target = Path(self._tmp.name) / "managed" / "AGENTS.md"
        target.parent.mkdir()
        target.write_text(
            "%s\nlegacy\n%s\n" % (codex_routing._BEGIN, codex_routing._END),
            encoding="utf-8",
        )
        try:
            self.agents.symlink_to(target)
        except OSError as exc:
            self.skipTest("file symlinks unavailable: %s" % exc)
        before = target.read_bytes()

        with self.assertRaisesRegex(ShellError, "symbolic link"):
            codex_routing.retire_routing()

        self.assertTrue(self.agents.is_symlink())
        self.assertEqual(target.read_bytes(), before)

    def test_dangling_symlink_is_refused(self):
        try:
            self.agents.symlink_to(Path(self._tmp.name) / "missing-target")
        except OSError as exc:
            self.skipTest("file symlinks unavailable: %s" % exc)

        with self.assertRaisesRegex(ShellError, "symbolic link"):
            codex_routing.retire_routing()

        self.assertTrue(self.agents.is_symlink())

    def test_exact_unmarked_legacy_copy_is_detected_but_not_removed(self):
        text = "\n".join(codex_routing._LEGACY_UNMARKED_SIGNATURES)
        self.agents.write_text(text, encoding="utf-8")

        self.assertTrue(codex_routing.has_unmarked_legacy_copy(text))
        result = codex_routing.retire_routing()

        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(self.agents.read_text(encoding="utf-8"), text)

    def test_dry_run_reports_removal_without_writing_or_backup(self):
        self.agents.write_text(
            "%s\nlegacy\n%s\n" % (codex_routing._BEGIN, codex_routing._END),
            encoding="utf-8",
        )
        before = self.agents.read_bytes()

        result = codex_routing.retire_routing(dry_run=True)

        self.assertEqual(result["action"], "removed (dry-run)")
        self.assertIsNone(result["backup"])
        self.assertEqual(self.agents.read_bytes(), before)

    def test_malformed_markers_fail_closed_without_writing(self):
        self.agents.write_text(
            "%s\nlegacy without end\n" % codex_routing._BEGIN,
            encoding="utf-8",
        )
        before = self.agents.read_bytes()

        with self.assertRaisesRegex(ShellError, "malformed"):
            codex_routing.retire_routing()

        self.assertEqual(self.agents.read_bytes(), before)

    def test_concurrent_user_edit_is_not_clobbered(self):
        self.agents.write_text(
            "%s\nlegacy\n%s\n" % (codex_routing._BEGIN, codex_routing._END),
            encoding="utf-8",
        )
        real_atomic_write = codex_routing._atomic_write_text

        def racing_write(path, text, **kwargs):
            path.write_text("concurrent user edit\n", encoding="utf-8")
            return real_atomic_write(path, text, **kwargs)

        with mock.patch.object(
            codex_routing, "_atomic_write_text", side_effect=racing_write
        ):
            with self.assertRaisesRegex(ShellError, "changed while being updated"):
                codex_routing.retire_routing()

        self.assertEqual(
            self.agents.read_text(encoding="utf-8"), "concurrent user edit\n"
        )


if __name__ == "__main__":
    unittest.main()
