"""Behavior locks for the progressive /audit-writing-plans skill package."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "audit-writing-plans"
ENTRYPOINT = SKILL_ROOT / "SKILL.md"
AUTHORING = SKILL_ROOT / "references" / "plan-authoring.md"
VALIDATION = SKILL_ROOT / "references" / "validation-and-adjudication.md"
COMPANION = SKILL_ROOT / "references" / "agent-companion.md"
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class AuditWritingPlansSkillContractTests(unittest.TestCase):
    def _text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _normalized(self, path: Path) -> str:
        return " ".join(self._text(path).split())

    def _package(self) -> str:
        return "\n".join(
            self._text(path)
            for path in (ENTRYPOINT, AUTHORING, VALIDATION, COMPANION)
        )

    def test_entrypoint_is_a_compact_english_intent_router(self):
        text = self._text(ENTRYPOINT)
        description = json.loads(
            text.splitlines()[2].removeprefix("description: ")
        )

        self.assertLessEqual(len(text.splitlines()), 85)
        self.assertLessEqual(len(description), 700)
        self.assertNotRegex(description, HAN_RE)
        self.assertNotIn("/audit-", description)
        self.assertNotIn("superpowers", description.lower())
        for neighbor in (
            "audit-brainstorming",
            "audit-market-research",
            "audit-adjudication",
        ):
            self.assertNotIn(neighbor, description)
        for phrase in (
            "externally reviewed or explicitly confirmed by the Owner",
            "traceable implementation plan",
            "external cross-vendor panel",
            "Owner-confirmed frozen decision set",
            "human-readable primary plan",
            "only on explicit request",
            "no settled reviewed or Owner-confirmed input set exists",
        ):
            self.assertIn(phrase, description)

    def test_entrypoint_routes_the_three_existing_logic_blocks(self):
        text = self._text(ENTRYPOINT)

        for reference in (
            "references/plan-authoring.md",
            "references/validation-and-adjudication.md",
            "references/agent-companion.md",
        ):
            self.assertIn(reference, text)
        self.assertIn("ordinary host planning", text)
        self.assertIn("docs/plans/", text)
        self.assertIn("Markdown", text)
        self.assertNotIn("DOCX report table style", text)
        self.assertFalse((SKILL_ROOT / "scripts").exists())

    def test_entrypoint_locks_always_on_safety_and_cost_boundaries(self):
        text = self._normalized(ENTRYPOINT)

        for phrase in (
            "Do not overwrite an existing plan without confirmation",
            "within five turns with no material change",
            "do not create another metered validation run",
            "Do not auto-chain to another workflow",
            "failed, cancelled, timed-out, or unresolved validation never produces an accepted plan",
        ):
            self.assertIn(phrase, text)

    def test_authoring_preserves_provenance_traceability_and_primary_plan(self):
        text = self._normalized(AUTHORING)

        for phrase in (
            "externally-reviewed",
            "owner-confirmed",
            "unverified",
            "no upstream convergence signal",
            "accepted",
            "rejected",
            "needs-user-decision",
            "[author-added]",
            "traces_to",
            "docs/plans/YYYY-MM-DD-<slug>-doc.md",
            "user-specified path",
            "Upstream Conclusions",
            "Cross-Cutting",
            "Implementation",
            "Open Questions",
            "risk_class",
            "estimated_effort",
        ):
            self.assertIn(phrase, text)

    def test_validation_is_authorized_async_and_adjudicated(self):
        text = self._normalized(VALIDATION)

        for phrase in (
            "explicit authorization",
            'skill_name="audit-writing-plans"',
            '"content": "<full primary document>"',
            '"stakes": "medium"',
            '"mode": "standard"',
            "artifact_intent",
            "prescriptive",
            "pre-mortem",
            "audit_skill_status",
            "audit_skill_result",
            "audit_skill_events",
            "total wait limit",
            "Prior authorization for an upstream audit is not authorization to send a newly drafted plan",
            "submitted_document_sha256",
            "exact UTF-8 bytes",
            "every returned finding ID",
            "every finding has a recorded disposition",
            "Set `status: accepted`",
            "total wait limit is exhausted",
            "omit `validated_by`",
            "accepted",
            "rejected",
            "needs-user-decision",
            "blocking_findings_count",
            "pre_mortem_applied",
            "convergent_count",
            "canonical_sha",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("No Owner override", text)
        self.assertNotIn("convergent_verdict >= majority", text)
        self.assertIn("does not prove the Markdown file's content hash", text)

    def test_companion_is_optional_mechanical_and_status_safe(self):
        text = self._normalized(COMPANION)

        for phrase in (
            "explicit confirmation",
            "-tasks.md",
            "Do not overwrite an existing companion file without confirmation",
            "mechanical restructure",
            "net-new content",
            "linked_doc_validated_by",
            "submitted_document_sha256",
            "require it to equal",
            "non-executable",
            "inputs",
            "outputs",
            "verification",
            "acceptance",
            "dependencies",
            "traces_to",
        ):
            self.assertIn(phrase, text)
        for state in (
            "`accepted` and unchanged",
            "`review`",
            "`draft`",
            "Validation missing, failed, cancelled, timed out, or uncertain",
        ):
            self.assertIn(state, text)

    def test_shipped_instruction_package_is_english_only_and_markdown_first(self):
        package = self._package()

        self.assertNotRegex(package, HAN_RE)
        for stale in (
            "superpowers",
            "w:vAlign",
            "ShadingType.CLEAR",
            "LiSong Pro",
            "report_style.json",
        ):
            self.assertNotIn(stale, package)
        self.assertIn("Markdown", package)
        self.assertIn("explicitly requests another format", package)


if __name__ == "__main__":
    unittest.main()
