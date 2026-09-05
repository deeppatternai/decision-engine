"""Structural and safety contracts for the cross-agent /audit skill package."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from installer import leak_scan


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "audit"
ENTRYPOINT = SKILL_ROOT / "SKILL.md"
REFERENCE_NAMES = (
    "hosted-workflow.md",
    "result-contract.md",
    "de-lite-routing.md",
    "localized-responses.md",
    "delegated-topic-samples.md",
)
HOSTED = SKILL_ROOT / "references" / "hosted-workflow.md"
RESULT = SKILL_ROOT / "references" / "result-contract.md"
ROUTING = SKILL_ROOT / "references" / "de-lite-routing.md"
RESPONSES = SKILL_ROOT / "references" / "localized-responses.md"
DELEGATED_SAMPLES = SKILL_ROOT / "references" / "delegated-topic-samples.md"


class AuditSkillProgressiveDisclosureTests(unittest.TestCase):
    def test_entrypoint_is_focused_and_routes_every_reference(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 90)
        for name in REFERENCE_NAMES:
            with self.subTest(reference=name):
                self.assertIn(f"references/{name}", text)

    def test_discovery_description_contains_selection_not_execution_details(self):
        lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        description = json.loads(lines[2].removeprefix("description: "))

        for required in (
            "EXTERNAL, cross-vendor auditors",
            "code, documents, plans, migrations, or designs",
            "defects, risks, vulnerabilities, or omissions",
            "explicit interactive user request",
            "independent defect review",
            "another model or panel identify problems",
            "equivalent requests in any language",
            "verified AQG audit-before-commit gate",
            "invoke this skill as the first action",
            "before inspecting, creating, or modifying files",
            "invoke this skill with the user's exact request unchanged",
            "/audit-brainstorming",
        ):
            self.assertIn(required, description)
        self.assertTrue(description.isascii())
        for execution_detail in (
            "MCP tools are absent",
            "DE Lite local bridge",
            "generic review fallback",
        ):
            self.assertNotIn(execution_detail, description)

    def test_references_exist_inside_the_routed_skill_tree(self):
        for name in REFERENCE_NAMES:
            with self.subTest(reference=name):
                path = SKILL_ROOT / "references" / name
                self.assertTrue(path.is_file(), path)

    def test_entrypoint_owns_selection_authorization_and_every_stop_gate(self):
        normalized = " ".join(ENTRYPOINT.read_text(encoding="utf-8").split()).lower()
        for required in (
            "explicit interactive user request",
            "verified aqg audit-before-commit gate",
            "sufficient authorization",
            "without asking the user for confirmation",
            "request_outcome_unknown",
            "cannot close the aqg gate",
            "local_surface",
            "account_action_required",
            "before producing any result",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized)

    def test_delegated_random_topic_stays_on_the_prescriptive_audit_route(self):
        entrypoint = " ".join(ENTRYPOINT.read_text(encoding="utf-8").split()).lower()
        routing = " ".join(ROUTING.read_text(encoding="utf-8").split()).lower()

        for required in (
            "random or delegated topic",
            "must stay in this `/audit` defect-review route",
            "must not reinterpret it as hypothesis brainstorming",
            "prescriptive sample artifact",
            "secret-like literals",
            "visible progress text",
        ):
            with self.subTest(required=required):
                self.assertIn(required, entrypoint)
        for required in (
            "do not call `activation_required`",
            "does not unlock or repair de lite",
        ):
            with self.subTest(required=required):
                self.assertIn(required, routing)

    def test_delegated_topic_uses_a_classifier_safe_single_attempt_sample(self):
        entrypoint = " ".join(ENTRYPOINT.read_text(encoding="utf-8").split()).lower()
        samples = " ".join(
            DELEGATED_SAMPLES.read_text(encoding="utf-8").split()
        ).lower()

        self.assertIn("before constructing or submitting", entrypoint)
        self.assertIn("references/delegated-topic-samples.md", entrypoint)
        for required in (
            "call `audit_skill_submit` exactly once",
            "must not expand or rewrite the selected sample",
            "host permission or classifier rejection",
            "stop the audit attempt immediately",
            "do not retry",
            "do not call `activation_required`",
            "图书列表分页规则",
            "book catalog pagination rules",
        ):
            with self.subTest(required=required):
                self.assertIn(required, samples)

    def test_a_preselected_delegated_topic_is_not_replaced_after_skill_load(self):
        samples = " ".join(
            DELEGATED_SAMPLES.read_text(encoding="utf-8").split()
        ).lower()

        for required in (
            "skill invocation arguments already name a specific topic",
            "that selected topic is authoritative",
            "must not replace it with an unrelated sample-bank topic",
            "preserve the selected subject",
            "minimal prescriptive proposal",
            "待审查提案：<selected topic>",
            "proposal for review: <selected topic>",
        ):
            with self.subTest(required=required):
                self.assertIn(required, samples)

    def test_conditional_contracts_live_in_the_reference_that_owns_them(self):
        hosted = " ".join(HOSTED.read_text(encoding="utf-8").split())
        result = " ".join(RESULT.read_text(encoding="utf-8").split())
        routing = " ".join(ROUTING.read_text(encoding="utf-8").split())
        responses = " ".join(RESPONSES.read_text(encoding="utf-8").split())

        for required in (
            'returns envelope {run_id, status="queued", ...}',
            "foreground recovery checkpoint sized to the run class",
            "stay interruptible",
            "no agent-controlled `STOP`",
        ):
            self.assertIn(required, hosted)
        self.assertIn("must not append", result)
        self.assertIn("不得追加 Lite", result)
        for required in (
            "If the caller is headless",
            "must not call the bridge",
            "never contacts the Hub",
            "leave the audit gate open",
            "before the bridge returns a `local_host_*` ID",
            "must not trigger Hub access",
            "obtain explicit consent",
        ):
            self.assertIn(required, routing)
        self.assertIn("限流", responses)

    def test_degrade_leak_gate_tracks_the_canonical_reference(self):
        self.assertEqual(
            leak_scan._DEGRADE_DOCS,
            ("skills/audit/references/de-lite-routing.md",),
        )


if __name__ == "__main__":
    unittest.main()
