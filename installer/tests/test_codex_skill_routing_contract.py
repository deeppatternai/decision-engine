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
            "Decision Engine native popup",
            "/graphic-explanation",
            "Use this skill",
            "generated visual explanation to view",
            "equivalent intent in any language",
            "interactive artifact the user must edit and submit",
            "inline visual",
            "another renderer",
        ):
            self.assertIn(phrase, frontmatter)
        self.assertTrue(frontmatter.isascii())
        self.assertIn("native popup only", text)
        self.assertIn("Never use a built-in visualizer", text)
        self.assertIn("Use `audit` when the primary intent is defect review", text)
        self.assertIn("Use `audit-market-research` when the primary intent is commercial", text)

    def test_board_skill_owns_interactive_adjustment_and_never_falls_back(self):
        text = self._skill("discussion-board")
        frontmatter = text.split("---", 2)[1]
        self.assertIn("interactive discussion board", frontmatter)
        self.assertIn("by hand and submit", frontmatter)
        self.assertNotIn("audit-", frontmatter)
        self.assertIn("Decision Engine board popup only", frontmatter)
        self.assertIn("do not substitute an inline board", frontmatter)
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

    def test_forecast_skill_preserves_result_and_abstention_contract(self):
        text = self._skill("audit-forecast")
        normalized = " ".join(text.split())
        for phrase in (
            "`subject`",
            "`source_hints`",
            "`no_market`",
            "audit_skill_result",
            "debug_authorized",
            "Voice N",
            "Source N",
            "preserve the `run_id`",
            "do not invent missing event details",
        ):
            self.assertIn(phrase, normalized)
        sections = {}
        for heading in (
            "The `proposition` contract (you build this; the server hard-gates it)",
            "Calling pattern",
            "Presenting the result",
            "Voice / source-name privacy (apply client-side)",
            "Anti-patterns",
        ):
            sections[heading] = " ".join(
                text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0].split()
            )
        proposition = sections["The `proposition` contract (you build this; the server hard-gates it)"]
        self.assertIn("`horizon_utc` | ISO-8601 timestamp", proposition)
        self.assertIn("future UTC", proposition)
        calling = sections["Calling pattern"]
        for phrase in (
            "All tool names in this section are illustrative",
            "total observation checkpoint",
            "unknown submission outcome",
            "preserve the `run_id`",
            "submit a duplicate run",
        ):
            self.assertIn(phrase, calling)
        result = sections["Presenting the result"]
        self.assertIn("configured sources found no usable match", result)
        self.assertIn("without a probability or a fabricated zero", result)
        privacy = sections["Voice / source-name privacy (apply client-side)"]
        self.assertIn("Unless `debug_authorized` is explicitly `true`", privacy)
        self.assertIn("missing or malformed", privacy)
        self.assertIn("`Voice N` / `Source N`", privacy)
        self.assertIn("never route around", sections["Anti-patterns"])
        self.assertNotIn("2026-07-19T20:00:00Z", normalized)
        self.assertNotIn("wait_audit is deprecated", normalized)

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

    def test_audit_brainstorming_is_an_english_progressive_router(self):
        skill_root = ROOT / "skills" / "audit-brainstorming"
        entrypoint = self._skill("audit-brainstorming")
        hosted = (skill_root / "references" / "hosted-workflow.md").read_text(
            encoding="utf-8"
        )
        result = (skill_root / "references" / "result-contract.md").read_text(
            encoding="utf-8"
        )
        frontmatter = entrypoint.split("---", 2)[1]
        normalized_entrypoint = " ".join(entrypoint.split())
        normalized_hosted = " ".join(hosted.split())

        self.assertLessEqual(len(entrypoint.splitlines()), 70)
        self.assertTrue(entrypoint.isascii())
        self.assertTrue(hosted.isascii())
        self.assertTrue(result.isascii())
        for required in (
            "formed hypothesis",
            "audit-explore",
            "audit-market-research",
            "audit-writing-plans",
            "audit for defects in deliverables",
        ):
            with self.subTest(frontmatter=required):
                self.assertIn(required, frontmatter)
        for execution_detail in (
            "Triggers on",
            "counter_arguments",
            "falsifiability_criteria",
            "epistemology_fields_filled",
        ):
            with self.subTest(execution_detail=execution_detail):
                self.assertNotIn(execution_detail, frontmatter)

        for required in (
            "Identifying what evidence is missing belongs here",
            "acquiring or validating that evidence belongs",
            "explicit invocation prevents silent rerouting",
            "confidential, restricted, or proprietary material",
            "references/hosted-workflow.md",
            "references/result-contract.md",
        ):
            with self.subTest(entrypoint=required):
                self.assertIn(required, normalized_entrypoint)

        for required in (
            'skill_name="audit-brainstorming"',
            '"title"',
            '"content"',
            '"context"',
            '"stakes"',
            '"mode"',
            '"domain"',
            "Steelman + Pre-mortem framing",
            "Never invoke the placeholders literally",
            "submitting a duplicate",
        ):
            with self.subTest(hosted=required):
                self.assertIn(required, normalized_hosted)

        for required in (
            "overall_assessment",
            "strengths[]",
            "risks[]",
            "counter_arguments[]",
            "assumptions[]",
            "falsifiability_criteria",
            "null_hypothesis_or_default",
            "base_rate_or_reference_class",
            "confidence_update_needed",
            "recommended_evidence_order[]",
            "epistemology_fields_filled",
            "convergent_assessment",
            "accepted_as_residual",
            "converted_to_experiment",
        ):
            with self.subTest(result=required):
                self.assertIn(required, result)

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


class MarketResearchP3ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        skill = ROOT / "skills/audit-market-research"
        cls.text = "\n".join(
            (skill / path).read_text(encoding="utf-8")
            for path in (
                "SKILL.md",
                "references/ground-truth.md",
                "references/analysis-and-synthesis.md",
            )
        )
        cls.normalized = " ".join(cls.text.split())

    def test_p3_contract_matrix(self):
        section = self.text.split("### P3 response decision table", 1)[1].split("\n## ", 1)[0]
        rows = {}
        for line in section.splitlines():
            if not line.startswith("| ") or line.startswith("| Case") or line.startswith("|---"):
                continue
            cells = [cell.strip() for cell in line.strip("| ").split("|")]
            if len(cells) == 3:
                rows[cells[0]] = (cells[1], cells[2])
        self.assertEqual(
            {name: verdict for name, (_, verdict) in rows.items()},
            {
                "missing, dual, empty, or non-array container": "STOP",
                "non-object member": "STOP",
                "defect-audit markers in any member": "STOP",
                "blank or empty generation analysis": "STOP",
                "blank or empty hypothesis analysis": "STOP",
                "unknown member shape": "STOP",
                "valid generation member": "continue",
                "valid hypothesis member": "continue",
                "mixed valid generation and hypothesis members": "continue",
            },
        )
        self.assertIn("exactly one", section)
        self.assertIn("`audits` or `per_vendor_analysis`", section)
        self.assertIn("every member", section)

    def test_p3_requires_provenance_and_substantive_shapes(self):
        section = self.text.split("### P3 response decision table", 1)[1].split("\n## ", 1)[0]
        normalized = " ".join(section.split())
        for phrase in (
            "`skill_name=audit-market-research`",
            "`phase=multi_lens`",
            "matching P2 pointer",
            "conflicting `artifact_intent`",
            "nonempty `insights[]`",
            "nonblank `overall_assessment`",
            "`strengths[]`, `risks[]`, `counter_arguments[]`, or `assumptions[]`",
            "`overall_verdict`, `findings`, or `dimension_status`",
            "`findings[i].blocking`",
            "optional context, not required shape keys",
            "both `insights` and `overall_assessment`",
            "verified P3 pointers",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized)
        self.assertNotIn("top-level `blocking`", section)

    def test_contrarian_is_only_a_request_signal(self):
        for phrase in (
            "`contrarian=true`",
            "`trust_signals.contrarian_requested`",
            "not proof that a dedicated contrarian voice ran",
            "unconfirmed unless a separate execution signal confirms it",
        ):
            self.assertIn(phrase, self.normalized)
        self.assertNotIn("server forces a contrarian vendor", self.text)

    def test_quick_mock_and_p4_skip_claims_do_not_conflict(self):
        self.assertIn("Quick uses mock ground truth and stops after P2", self.text)
        self.assertIn("`mock_ground_truth` (quick only)", self.text)
        self.assertIn("Deep may conditionally run deterministic mock P4", self.normalized)
        self.assertIn("Quick has no P4 response because it stops after P2", self.normalized)
        for stale in (
            "mock_ground_truth` (deep/quick)",
            "mock_ground_truth` (deep/quick floor)",
            "deep/quick = MOCK",
            "mode_skip` (Fast/Deep)",
        ):
            self.assertNotIn(stale, self.text)


if __name__ == "__main__":
    unittest.main()
