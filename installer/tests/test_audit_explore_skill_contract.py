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

    def test_entrypoint_is_an_english_progressive_router(self):
        text = self._text(ENTRYPOINT)
        normalized_entrypoint = " ".join(text.split())
        frontmatter = text.split("---", 2)[1]
        description = json.loads(
            text.splitlines()[2].removeprefix("description: ")
        )

        self.assertLessEqual(len(text.splitlines()), 70)
        self.assertTrue(text.isascii())
        self.assertIn("vague, unformed idea", description)
        self.assertIn("clear, falsifiable hypothesis", description)
        self.assertIn("external panel by default", description)
        self.assertIn("equivalent intent in any language", description)
        self.assertIn("/audit-explore", description)
        self.assertIn("Do not trigger on isolated words", description)
        self.assertIn("code or repository exploration", description)
        for route in (
            "audit-brainstorming",
            "audit-market-research",
            "audit-writing-plans",
            "audit for deliverable defects",
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
        self.assertIn(
            "vague software, product, feature, or API idea", normalized_entrypoint
        )
        self.assertIn("existing code or repository", normalized_entrypoint)
        self.assertIn("Only on a hosted path", normalized_entrypoint)

    def test_consent_and_framing_preserves_safety_and_local_path(self):
        text = self._normalized(CONSENT)

        for required in (
            "P0 Route And Sensitive-Content Consent",
            "Routine:",
            "routine personal planning context",
            "Ordinary use proceeds without a separate consent prompt",
            "Sensitive but shareable",
            "first-party",
            "another person's identifiers",
            "Pending is not a local-only decision",
            "Regulated content is never transmitted externally",
            "Secrets and credentials are never submitted",
            "Obtain explicit authorization for that complete payload",
            "recipient categories",
            "the first matching route wins",
            "Mixed content takes the most restrictive route",
            "If uncertain between Routine and Sensitive",
            "Any change to a sensitive payload requires a new approval",
            "BCP-47",
            "und-*",
            "one question at a time",
            "When [situation], I want to [motivation], so I can [outcome]",
            "user approves the frame",
            "explicitly approves the revised frame",
            "If unknown, say so once",
            "hosted-exploration.md",
            "client-only problem divergence",
            "client-only solution divergence",
            "client-only convergence and falsification",
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

    def test_hosted_workflow_preserves_phase_calls_and_selection_gates(self):
        text = self._normalized(HOSTED)

        for required in (
            'skill_name="audit-explore"',
            '"phase": "diverge_problem"',
            '"phase": "diverge_solution"',
            '"domain"',
            '"audit_mode"',
            '"upstream_run_id"',
            '"upstream_canonical_sha"',
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
            "No model-family exclusion preflight",
            "Routine payloads proceed directly",
            "complete exact user-derived payload",
            "Any change requires renewed approval",
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

    def test_convergence_result_preserves_exit_and_trust_contracts(self):
        text = self._normalized(RESULT)

        for required in (
            'skill_name="audit-explore-converge"',
            '"premortem": true',
            '"audit_mode"',
            "Klein premortem",
            "Goldilocks gate",
            "maximum of three critique rounds",
            "canonical_sha",
            "always present",
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

    def test_documented_panel_submissions_use_audit_mode(self):
        blocks = []
        for path in (HOSTED, RESULT):
            blocks.extend(
                re.findall(r"SUBMIT_TOOL\((.*?)\) -> envelope", self._text(path), re.S)
            )
        self.assertEqual(len(blocks), 3)
        for index, block in enumerate(blocks):
            phase = re.search(r'"phase": "([^"]+)"', block)
            with self.subTest(phase=phase.group(1) if phase else f"P4-{index}"):
                self.assertIn('"audit_mode":', block)
                self.assertNotIn('"mode":', block)

    def test_every_reference_is_ascii_and_phase_routed(self):
        entrypoint = self._normalized(ENTRYPOINT)
        for path in (CONSENT, HOSTED, RESULT):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertTrue(self._text(path).isascii())
        self.assertIn("At activation, read only", entrypoint)
        self.assertIn("Only on a hosted path", entrypoint)
        self.assertIn("Only on that hosted path", entrypoint)


if __name__ == "__main__":
    unittest.main()
