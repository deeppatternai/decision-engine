"""Cross-client contract tests for shipped Decision Engine Skill metadata."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from installer.doctor import DE_SKILLS


ROOT = Path(__file__).resolve().parents[2]


class SkillFrontmatterCompatibilityTests(unittest.TestCase):
    def test_all_descriptions_are_json_quoted_yaml_scalars(self):
        """TRAE's strict YAML parser rejects plain scalars containing `: ` sequences."""

        for name in DE_SKILLS:
            with self.subTest(skill=name):
                lines = (ROOT / "skills" / name / "SKILL.md").read_text(
                    encoding="utf-8"
                ).splitlines()
                self.assertGreaterEqual(len(lines), 4)
                self.assertEqual(lines[0], "---")
                self.assertEqual(lines[1], f"name: {name}")
                self.assertTrue(lines[2].startswith("description: "))
                encoded = lines[2].removeprefix("description: ")
                description = json.loads(encoded)
                self.assertIsInstance(description, str)
                self.assertTrue(description.strip())
                self.assertEqual(lines[3], "---")

    def test_semantically_rewritten_descriptions_keep_canonical_routing_phrases(self):
        expected_phrases = {
            "audit-adjudication": (
                "multiple completed audit results",
                "same or different reviewers, rounds, or sessions",
                "single result",
                "needs-user-decision",
            ),
            "audit-explore": (
                "vague, unformed idea",
                "equivalent intent in any language",
                "/audit-explore",
            ),
            "audit-forecast": (
                "existing forecasters or prediction markets",
                "specific, time-bound outcome",
                "equivalent intent in any language",
                "abstain when matching sources are unavailable",
                "Never generate a new probability",
            ),
            "audit-market-research": (
                "retrieving and citing external evidence",
                "TAM/SAM/SOM",
            ),
            "discussion-board": ("interactive discussion board", "board-based rearrangement"),
            "graphic-explanation": (
                "/graphic-explanation",
                "Use this skill",
                "generated visual explanation to view",
                "equivalent intent in any language",
                "interactive artifact the user must edit and submit",
            ),
            "layer-check": ("竞品层级", "layer check"),
        }
        for name, phrases in expected_phrases.items():
            with self.subTest(skill=name):
                line = (ROOT / "skills" / name / "SKILL.md").read_text(
                    encoding="utf-8"
                ).splitlines()[2]
                description = json.loads(line.removeprefix("description: "))
                for phrase in phrases:
                    self.assertIn(phrase, description)

    def test_audit_forecast_description_is_english_only(self):
        line = (ROOT / "skills" / "audit-forecast" / "SKILL.md").read_text(
            encoding="utf-8"
        ).splitlines()[2]
        description = json.loads(line.removeprefix("description: "))
        self.assertTrue(description.isascii())

    def test_audit_adjudication_description_is_english_and_multi_result_first(self):
        line = (ROOT / "skills" / "audit-adjudication" / "SKILL.md").read_text(
            encoding="utf-8"
        ).splitlines()[2]
        description = json.loads(line.removeprefix("description: "))
        self.assertTrue(description.isascii())
        self.assertTrue(description.startswith("Consolidate multiple completed audit results"))

    def test_graphic_explanation_description_is_english_only(self):
        line = (ROOT / "skills" / "graphic-explanation" / "SKILL.md").read_text(
            encoding="utf-8"
        ).splitlines()[2]
        description = json.loads(line.removeprefix("description: "))
        self.assertTrue(description.isascii())

    def test_descriptions_fit_qoder_desktop_metadata_limit(self):
        """Qoder Desktop 1.106.3 disables Skills above 1024 characters."""

        for name in DE_SKILLS:
            with self.subTest(skill=name):
                line = (ROOT / "skills" / name / "SKILL.md").read_text(
                    encoding="utf-8"
                ).splitlines()[2]
                description = json.loads(line.removeprefix("description: "))
                # Qoder 1.106.3 trims the description and disables it only
                # when the resulting JavaScript string length exceeds 1024.
                self.assertLessEqual(len(description.strip()), 1024)


if __name__ == "__main__":
    unittest.main()
