"""Behavior locks for the progressive /audit-explore skill package."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "audit-explore"
ENTRYPOINT = SKILL_ROOT / "SKILL.md"
CONSENT = SKILL_ROOT / "references" / "consent-and-framing.md"
HOSTED = SKILL_ROOT / "references" / "hosted-exploration.md"
RESULT = SKILL_ROOT / "references" / "convergence-and-result.md"


class AuditExploreSkillContractTests(unittest.TestCase):
    def _text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _normalized(self, path: Path) -> str:
        return " ".join(self._text(path).split())

    def _submission_blocks(self) -> list[str]:
        blocks = []
        for path in (HOSTED, RESULT):
            blocks.extend(
                re.findall(r"SUBMIT_TOOL\((.*?)\) -> envelope", self._text(path), re.S)
            )
        return blocks

    def test_entrypoint_is_an_english_progressive_router(self):
        text = self._text(ENTRYPOINT)
        normalized = " ".join(text.split())
        frontmatter = text.split("---", 2)[1]
        description = json.loads(
            text.splitlines()[2].removeprefix("description: ")
        )

        self.assertLessEqual(len(text.splitlines()), 70)
        self.assertTrue(text.isascii())
        for routing_marker in (
            "vague, unformed idea",
            "clear, falsifiable hypothesis",
            "/audit-explore",
        ):
            self.assertIn(routing_marker, description)
        self.assertRegex(
            description,
            re.compile(
                r"(?:^|[.!?]\s+)develop\b[^.]{0,100}\bvague, unformed idea\b"
                r"[^.]{0,100}\bclear, falsifiable hypothesis\b",
                re.I,
            ),
        )
        self.assertRegex(
            description,
            re.compile(
                r"\bdo not trigger\b[^.]{0,100}\bisolated words\b"
                r"[^.]{0,140}\bcode or repository exploration\b",
                re.I,
            ),
        )
        for route in (
            "audit-brainstorming",
            "audit-market-research",
            "audit-writing-plans",
        ):
            self.assertIn(route, frontmatter)
        for execution_detail in (
            "Double-Diamond",
            "Klein premortem",
            "Triggers include",
            "diverge_problem",
            "goldilocks_pass",
        ):
            self.assertNotIn(execution_detail, frontmatter)
        for reference in (
            "references/consent-and-framing.md",
            "references/hosted-exploration.md",
            "references/convergence-and-result.md",
        ):
            self.assertIn(reference, text)
        for progressive_gate in (
            "At activation, read only",
            "Only on a hosted path",
            "Only on that hosted path",
            "On a client-only path, do not load",
        ):
            self.assertIn(progressive_gate, normalized)

    def test_consent_and_framing_preserves_security_and_result_fields(self):
        text = self._normalized(CONSENT)

        for required in (
            "Prohibited:",
            "Routine:",
            "Sensitive but shareable",
            "first-party",
            "another person's identifiers",
            "Regulated content is never transmitted externally",
            "Secrets and credentials are never submitted",
            "Obtain explicit authorization for that complete payload",
            "recipient categories",
            "the first matching route wins",
            "Mixed content takes the most restrictive route",
            "If uncertain between Routine and Sensitive",
            "Any change to a sensitive payload requires a new approval",
            "Pending is not a local-only decision",
            "BCP-47",
            "und-*",
            "audit_mode",
            "one question at a time",
            "When [situation], I want to [motivation], so I can [outcome]",
            "user approves the frame",
            "explicitly approves the revised frame",
            "hosted-exploration.md",
            "local-only",
            "frame",
            "candidates",
            "formed_hypothesis",
            "panel_participation",
            "next_skill_handoff",
        ):
            self.assertIn(required, text)
        self.assertNotIn("~$0", text)
        self.assertNotIn("~5-8min", text)
        self.assertNotIn("~8-12min", text)
        self.assertNotIn("all other non-public content stays client-side", text)
        self.assertNotIn("runtime-discoverable authoritative policy", text)

    def test_hosted_workflow_preserves_tools_and_selection_gates(self):
        text = self._normalized(HOSTED)

        for required in (
            'artifact_intent="explore_diverge"',
            "server injects each vendor methodology lens",
            "must not pass a methodology lens",
            "audit_skill_submit",
            "wait_audit",
            "audit_skill_status",
            "audit_skill_result",
            "audit_skill_events",
            "audit_skill_cancel",
            "user selects one problem reframe",
            "user selects one or two solution directions",
            "Before each P2 and P3 external submission",
            "Routine payloads proceed directly",
            "complete exact user-derived payload",
            "Any change requires renewed approval",
            "No model-family exclusion preflight",
            "independent client voice",
            "Never include it in panel convergence counts",
            "unknown or transport-error state",
            "stop without guessing",
            "same vague idea appears within five turns",
            "materially changed",
        ):
            self.assertIn(required, text)
        self.assertNotIn('"mode":', text)
        self.assertNotIn("Self-identify the runtime model family", text)

    def test_convergence_result_preserves_trust_fields_and_exit_ownership(self):
        text = self._normalized(RESULT)

        for required in (
            "maximum of three critique rounds",
            "canonical_sha",
            "stop before the next hosted phase",
            "user approves the revised draft",
            "latest run ID and `canonical_sha`",
            "panel_size",
            "convergent_assessment",
            "convergent_count",
            "framework_diversity_check",
            "lens_injection",
            "premortem_applied",
            "goldilocks_pass",
            "exit_ready",
            "upstream_run_id",
            "upstream_canonical_sha",
            "frame",
            "candidates",
            "formed_hypothesis",
            "panel_participation",
            "next_skill_handoff",
            "must not set `exit_ready`",
            "Show the complete envelope to the user",
            "explicit confirmation",
            "Equal-weight candidates until P4",
            "`framework_diversity_check=LOW` is a warning",
        ):
            self.assertIn(required, text)

    def test_documented_panel_submissions_use_exact_phase_schemas(self):
        blocks = self._submission_blocks()
        self.assertEqual(len(blocks), 3)
        expected = (
            (
                "audit-explore",
                {"title", "content", "phase", "domain", "audit_mode"},
            ),
            (
                "audit-explore",
                {
                    "title",
                    "content",
                    "phase",
                    "domain",
                    "audit_mode",
                    "upstream_run_id",
                    "upstream_canonical_sha",
                },
            ),
            (
                "audit-explore-converge",
                {
                    "title",
                    "content",
                    "audit_mode",
                    "premortem",
                    "upstream_run_id",
                    "upstream_canonical_sha",
                },
            ),
        )
        for block, (skill_name, expected_keys) in zip(blocks, expected, strict=True):
            with self.subTest(skill_name=skill_name):
                self.assertIn(f'skill_name="{skill_name}"', block)
                keys = set(re.findall(r'^\s*"([^"]+)"\s*:', block, re.M))
                self.assertEqual(keys, expected_keys)
                self.assertIn('"audit_mode":', block)
                self.assertNotIn('"mode":', block)

    def test_every_reference_is_present_and_ascii(self):
        for path in (CONSENT, HOSTED, RESULT):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertTrue(self._text(path).isascii())


if __name__ == "__main__":
    unittest.main()
