"""Behavior locks for the progressive /audit-market-research skill package."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "audit-market-research"
ENTRYPOINT = SKILL_ROOT / "SKILL.md"
CONSENT = SKILL_ROOT / "references" / "consent-and-scope.md"
GROUND_TRUTH = SKILL_ROOT / "references" / "ground-truth.md"
ANALYSIS = SKILL_ROOT / "references" / "analysis-and-synthesis.md"
REPORT = SKILL_ROOT / "references" / "report-generation.md"
STYLE = SKILL_ROOT / "assets" / "report_style.json"
RENDERER = SKILL_ROOT / "scripts" / "render_report.py"
FONT_RESOLVER = SKILL_ROOT / "scripts" / "font_resolver.py"
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class AuditMarketResearchSkillContractTests(unittest.TestCase):
    def _text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _normalized(self, path: Path) -> str:
        return " ".join(self._text(path).split())

    def _package(self) -> str:
        paths = (ENTRYPOINT, CONSENT, GROUND_TRUTH, ANALYSIS, REPORT)
        return "\n".join(self._text(path) for path in paths)

    def test_entrypoint_is_a_compact_intent_based_router(self):
        text = self._text(ENTRYPOINT)
        description = json.loads(
            text.splitlines()[2].removeprefix("description: ")
        )

        self.assertLessEqual(len(text.splitlines()), 85)
        self.assertNotRegex(description, HAN_RE)
        self.assertNotIn("/audit-market-research", description)
        self.assertNotIn("audit-writing-plans", description)
        self.assertNotIn("audit-forecast", description)
        for phrase in (
            "retrieving and citing external evidence",
            "multiple independent perspectives",
            "Use when the user needs",
            "market sizing (TAM/SAM/SOM)",
            "go-to-market (GTM)",
            "commercial validation of a formed hypothesis",
            "risks, uncertainties, and decision implications",
            "forecast consensus about a specific time-bound outcome",
        ):
            self.assertIn(phrase, description)
        for reference in (
            "references/consent-and-scope.md",
            "references/ground-truth.md",
            "references/analysis-and-synthesis.md",
            "references/report-generation.md",
        ):
            self.assertIn(reference, text)

    def test_tier_and_phase_contract_matches_current_server(self):
        package = " ".join(self._package().split())

        for phrase in (
            "P0 Consent",
            "P1 Scope",
            "P2 Ground Truth",
            "P3 Multi-Lens Analysis",
            "P4 Synthetic Customer",
            "P5 Synthesis",
            "regulated content remains client-only",
            "Quick uses mock ground truth and stops after P2",
            "Deep uses real server-side retrieval",
            "Premium uses expanded real retrieval",
            "Deep uses the deterministic mock persona path",
            "Premium uses the real fixed-provider persona panel",
        ):
            self.assertIn(phrase, package)
        for stale_claim in (
            "deep/quick = MOCK",
            "deep/quick floor",
            "Both deep and quick use mock ground truth",
        ):
            self.assertNotIn(stale_claim.lower(), package.lower())

    def test_consent_scope_and_ground_truth_contracts_are_preserved(self):
        consent = self._normalized(CONSENT)
        ground_truth = self._normalized(GROUND_TRUTH)

        for phrase in (
            "public, internal, confidential, and regulated",
            "BCP-47",
            "und-*",
            "explicit authorization",
            "research_type",
            "research_method",
            "language",
            "question",
            "topic",
            "framework_hints",
            "same scope fields on every server submission",
            "scope_sha",
            "generic degraded brief",
        ):
            self.assertIn(phrase, consent)
        for phrase in (
            '"phase": "ground_truth"',
            '"mode": "quick" | "deep" | "premium"',
            '"content"',
            "audit_skill_submit",
            "audit_skill_status",
            "audit_skill_result",
            "audit_skill_events",
            "audit_skill_cancel",
            "artifact_sha",
            "mock_ground_truth",
            "ground_truth_partial",
            "local-language queries",
            "caller-side search",
            "one submission for P2",
            "seven days",
        ):
            self.assertIn(phrase, ground_truth)

    def test_analysis_synthesis_and_trust_contracts_are_preserved(self):
        text = self._normalized(ANALYSIS)

        for phrase in (
            '"phase": "multi_lens"',
            '"phase": "synthetic_customer"',
            '"phase": "synthesize"',
            "upstream_p2_run_id",
            "upstream_p2_artifact_sha",
            "upstream_p3_run_id",
            "upstream_p3_artifact_sha",
            "upstream_p4_run_id",
            "upstream_p4_artifact_sha",
            "server fetches and verifies",
            "framework_diversity_check",
            "convergent_",
            "p4_skipped_reason",
            "payload.synthesis",
            "trust_tier",
            "q_u_failed",
            "quantified_threshold_missing",
            "tension_analysis_missing",
            "final_round_quality",
            "sources_failed",
            "panel_failure",
            "cost_cap_hit",
            "p5_enforcement_partial",
            "language_compliance_partial",
            "consumed_input_run_ids",
            "Do not pass frameworks or methodology lenses",
            "Do not expose provider names or raw voice counts",
            "Do not auto-chain",
        ):
            self.assertIn(phrase, text)

    def test_report_resources_remain_discoverable_and_user_editable(self):
        text = self._normalized(REPORT)
        style = json.loads(self._text(STYLE))

        for phrase in (
            "offer a DOCX report",
            "scripts/render_report.py",
            "assets/report_style.json",
            "scripts/font_resolver.py",
            "scripts/requirements.txt",
            "python-docx",
            "--lang <bcp47>",
            "user-editable",
            "canonical style source",
            "Fonts are not embedded",
        ):
            self.assertIn(phrase, text)
        self.assertTrue(RENDERER.is_file())
        self.assertTrue(FONT_RESOLVER.is_file())
        self.assertNotIn("\\U0001f524", self._text(RENDERER))
        for key in ("font", "palette", "semantic", "cell"):
            self.assertIn(key, style)

    def test_shipped_market_research_package_has_no_chinese_prose(self):
        suffixes = {".md", ".json", ".py", ".txt"}
        for path in sorted(SKILL_ROOT.rglob("*")):
            if path.is_file() and path.suffix in suffixes:
                with self.subTest(path=path.relative_to(SKILL_ROOT)):
                    self.assertNotRegex(self._text(path), HAN_RE)


if __name__ == "__main__":
    unittest.main()
