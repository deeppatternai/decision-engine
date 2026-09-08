"""Behavior locks for routing that moved from global AGENTS.md into Codex skills."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CodexSkillRoutingContractTests(unittest.TestCase):
    def _skill(self, name: str) -> str:
        return (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")

    def _audit_package(self) -> str:
        skill_root = ROOT / "skills" / "audit"
        paths = (skill_root / "SKILL.md", *sorted((skill_root / "references").glob("*.md")))
        return "\n".join(path.read_text(encoding="utf-8") for path in paths)

    def test_graphic_skill_owns_the_native_popup_and_no_builtin_fallback_rule(self):
        text = self._skill("graphic-explanation")
        frontmatter = text.split("---", 2)[1]
        for phrase in (
            "must trigger this Skill",
            "open_ge once",
            "built-in visualizer",
            "show_widget",
            "inline SVG",
        ):
            self.assertIn(phrase, frontmatter)
        self.assertIn("Do NOT\n> satisfy it with a built-in", text)

    def test_board_skill_owns_interactive_adjustment_and_never_falls_back(self):
        text = self._skill("discussion-board")
        frontmatter = text.split("---", 2)[1]
        self.assertIn("interactive board", frontmatter)
        self.assertIn("Decision Engine board popup only", frontmatter)
        self.assertIn("Do not substitute an inline board", frontmatter)
        self.assertIn("Fail-closed, never degrade", text)
        self.assertIn("Never a browser fallback", text)

    def test_audit_skill_owns_consent_headless_and_policy_routing(self):
        text = self._skill("audit")
        package = self._audit_package()
        frontmatter = text.split("---", 2)[1]
        normalized = " ".join(package.split())
        self.assertIn("explicit interactive user request", frontmatter)
        self.assertIn("verified AQG audit-before-commit gate", frontmatter)
        self.assertIn("audit-brainstorming", frontmatter)
        self.assertIn("Inspect the current task's tool list", text)
        self.assertIn("sufficient authorization", text)
        self.assertIn("without asking the user for confirmation", normalized)
        self.assertIn("staged diff when available", package)
        self.assertIn("working-tree diff or named artifact", normalized)
        self.assertIn("task intent, acceptance criteria, and verification results", package)
        self.assertIn("trusted installed AQG checkout", text)
        self.assertIn("never a repository-local substitute", normalized)
        self.assertIn("At most one audit may run per logical change", normalized)
        self.assertIn("reuse its `audit_id`", normalized)
        self.assertIn("CI, cron, or detached", normalized)
        self.assertIn("A verified AQG gate authorizes the hosted path", normalized)
        self.assertIn("If the caller is headless, do not start the bridge", normalized)
        self.assertIn("leave the audit gate open", normalized)
        self.assertIn("/audit-adjudication", text)
        self.assertIn("AQG adjudicator discipline", text)
        self.assertEqual(package.count("docs/policies/audit-trigger.md"), 1)
        self.assertNotIn(
            "implicit completion gate, audit-before-commit reminder", package
        )
        for stale_ladder_phrase in (
            "do not audit",
            "codex-config/audit-routing",
            "a small single-file edit",
        ):
            self.assertNotIn(stale_ladder_phrase, text.lower())

    def test_audit_skill_accepts_both_mcp_server_prefix_spellings(self):
        text = self._skill("audit")
        package = self._audit_package()
        self.assertIn("mcp__decision-engine__*", text)
        self.assertIn("mcp__decision_engine__*", text)
        self.assertIn("call the exact spelling the host exposes", text)
        self.assertIn("never invoke them literally", package)

    def test_audit_adjudication_never_puts_activation_secret_in_argv(self):
        text = self._skill("audit-adjudication")
        self.assertNotIn("--activation-secret", text)
        self.assertIn("installer.permanent_setup", text)

    def test_audit_workflows_do_not_require_a_provider_probe_or_skip_preparation(self):
        texts = {"audit": self._audit_package()}
        for name in (
            "audit-adjudication",
            "audit-brainstorming",
            "audit-explore",
            "audit-forecast",
            "audit-market-research",
            "audit-writing-plans",
        ):
            texts[name] = self._skill(name)
        for name, text in texts.items():
            with self.subTest(skill=name):
                self.assertNotIn("check_provider_health", text)
                self.assertNotIn(
                    "directly after the required authorization checks",
                    " ".join(text.split()),
                )

    def test_audit_keeps_authentication_diagnostics_explicitly_requested(self):
        self.assertIn(
            "For explicitly requested authentication diagnostics, use the `fast_smoke` profile.",
            self._audit_package(),
        )

    def test_replacement_skills_keep_the_four_frontmatter_descriptions(self):
        for name in (
            "graphic-explanation",
            "discussion-board",
            "audit",
            "audit-brainstorming",
        ):
            with self.subTest(name=name):
                frontmatter = self._skill(name).split("---", 2)[1]
                self.assertIn("description:", frontmatter)
                self.assertGreater(len(frontmatter), 120)


if __name__ == "__main__":
    unittest.main()
