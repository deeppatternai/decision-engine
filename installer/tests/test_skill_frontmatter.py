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
                "audit 结果整合",
                "decide on audit results",
                "synthesize prior audits",
            ),
            "audit-explore": (
                "vague, unformed idea",
                "equivalent intent in any language",
                "/audit-explore",
            ),
            "audit-forecast": ("预测平台怎么看", "what do forecasters predict"),
            "audit-market-research": (
                "retrieving and citing external evidence",
                "TAM/SAM/SOM",
            ),
            "discussion-board": ("讨论板", "drag/reorder cards"),
            "graphic-explanation": ("用图解释一下", "draw a diagram to explain"),
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
