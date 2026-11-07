"""Cross-client contract tests for shipped Decision Engine Skill metadata."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from installer.doctor import DE_SKILLS


ROOT = Path(__file__).resolve().parents[2]


class SkillFrontmatterCompatibilityTests(unittest.TestCase):
    def _description(self, name: str) -> str:
        line = (ROOT / "skills" / name / "SKILL.md").read_text(
            encoding="utf-8"
        ).splitlines()[2]
        return json.loads(line.removeprefix("description: "))

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

    def test_descriptions_keep_machine_stable_routing_markers(self):
        required_markers = {
            "audit-adjudication": (
                "multiple completed audit results",
                "single result",
                "needs-user-decision",
            ),
            "audit-explore": (
                "/audit-explore",
                "audit-brainstorming",
                "audit-market-research",
                "audit-writing-plans",
            ),
            "audit-forecast": (
                "existing forecasters or prediction markets",
                "time-bound outcome",
            ),
            "audit-market-research": ("TAM/SAM/SOM",),
            "graphic-explanation": ("/graphic-explanation",),
            "layer-check": ("job-to-be-done",),
        }
        for name, markers in required_markers.items():
            with self.subTest(skill=name):
                for marker in markers:
                    self.assertIn(marker, self._description(name))

        positive_patterns = {
            "audit-adjudication": (
                r"(?:^|[.!?]\s+)consolidate\b[^.]{0,80}"
                r"\bmultiple completed audit results\b",
                r"(?:^|[.!?]\s+)(?:also\s+)?use\b[^.]{0,80}"
                r"\bsingle result\b",
            ),
            "audit-forecast": (
                r"(?:^|[.!?]\s+)use when\b[^.]{0,100}"
                r"\bexisting forecasters or prediction markets\b"
                r"[^.]{0,100}\btime-bound outcome\b",
            ),
        }
        for name, patterns in positive_patterns.items():
            for pattern in patterns:
                with self.subTest(skill=name, contract="positive"):
                    self.assertRegex(
                        self._description(name), re.compile(pattern, re.I)
                    )

        exclusion_patterns = {
            "audit-adjudication": (
                r"\b(?:do not|never)\b[^.]{0,100}\bstart a new audit\b"
            ),
            "audit-forecast": (
                r"\bnever\b[^.]{0,100}\bnew probability\b"
                r"[^.]{0,100}\bbroad market research\b"
            ),
        }
        for name, pattern in exclusion_patterns.items():
            with self.subTest(skill=name, contract="exclusion"):
                self.assertRegex(self._description(name), re.compile(pattern, re.I))

    def test_cross_host_routing_descriptions_are_english_only(self):
        for name in (
            "audit-adjudication",
            "audit-forecast",
            "graphic-explanation",
            "layer-check",
        ):
            with self.subTest(skill=name):
                self.assertTrue(self._description(name).isascii())

    def test_descriptions_fit_qoder_desktop_metadata_limit(self):
        """Qoder Desktop 1.106.3 disables Skills above 1024 characters."""

        for name in DE_SKILLS:
            with self.subTest(skill=name):
                # Qoder 1.106.3 trims the description and disables it only
                # when the resulting JavaScript string length exceeds 1024.
                self.assertLessEqual(len(self._description(name).strip()), 1024)


if __name__ == "__main__":
    unittest.main()
