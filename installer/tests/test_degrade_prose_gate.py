"""Tests for the /audit degrade-branch routing-prose-only gate (PR5b, v5 §15).

A best-effort tripwire asserting the audit skill's canonical degrade-routing reference carries
no hub-proprietary orchestration IP. Mirrors the AQG-side leak_gate (PR4); the
canary proves the tripwire is alive, the sanctioned-negation test proves it does
not false-positive on prose that only POINTS at where the IP lives.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from installer import leak_scan


_SANCTIONED = (
    "### Degraded / hub-unreachable\n"
    "The role prompts, the depth roster, and the adjudication logic live in that "
    "skill — never restate them here. On the user's yes, invoke aqg-multi-review "
    "new and validate its filled ledger; advisory only, never closes a gate.\n"
)

_CANARY = (
    "### Degraded / hub-unreachable\n"
    "Roster = gpt-5.5 + o3 + deepseek + qwen. Emit reply_prefix then weigh "
    "convergent vs divergent per adjudication_hint.\n"
)


class DegradeProseGateTests(unittest.TestCase):
    def test_canary_trips_the_gate(self):
        findings = leak_scan.scan_degrade_prose_text(_CANARY, path="AGENTS.md")
        self.assertTrue(findings, "canary must trip the degrade-prose gate")

    def test_sanctioned_negation_is_clean(self):
        findings = leak_scan.scan_degrade_prose_text(_SANCTIONED, path="AGENTS.md")
        self.assertEqual(findings, [], findings)

    def test_case_and_separator_variants_caught(self):
        for variant in ("Reply_Prefix", "adjudicationHint", "clean_Claude",
                        "gpt5.5", "o-3"):
            text = "### Degraded / hub-unreachable\n" + variant + "\n"
            with self.subTest(variant=variant):
                self.assertTrue(
                    leak_scan.scan_degrade_prose_text(text, path="AGENTS.md"),
                    variant,
                )

    def test_finding_excerpt_is_category_only(self):
        findings = leak_scan.scan_degrade_prose_text(
            "### Degraded / hub-unreachable\npoll adjudication_hint\n", path="AGENTS.md")
        self.assertTrue(findings)
        for f in findings:
            self.assertNotIn("adjudication_hint", f.excerpt)
            self.assertNotIn("adjudication_hint", f.kind)

    def test_no_degrade_section_is_clean(self):
        # A doc without the degrade header contributes nothing (scoped scan).
        self.assertEqual(
            leak_scan.scan_degrade_prose_text("# Some other doc\nhello\n", path="X.md"),
            [],
        )

    def test_current_repo_degrade_prose_is_clean(self):
        # The shipped audit routing reference's degrade section must scan clean.
        from pathlib import Path
        root = Path(leak_scan.__file__).resolve().parents[1]
        problems = []
        for rel in ("skills/audit/references/de-lite-routing.md",):
            p = root / rel
            if p.exists():
                problems += leak_scan.scan_degrade_prose_text(
                    p.read_text(encoding="utf-8"), path=rel)
        self.assertEqual(problems, [], problems)

    def test_local_contract_uses_aqg_skeleton_without_cross_llm_dispatch(self):
        root = Path(leak_scan.__file__).resolve().parents[1]
        for rel in ("skills/audit/references/de-lite-routing.md",):
            text = (root / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertIn("five-dimension focus prompts and ledger skeleton", text)
                self.assertIn("do not call `de_audit`", text)
                self.assertIn("needs-cross-llm-rerun", text)
                self.assertIn("must not trigger Hub access", text)
                self.assertIn("audit_skill_complete", text)
                self.assertNotIn("Seed the stop panel with", text)

    def test_audit_skill_does_not_advertise_the_removed_agent_cancel_tool(self):
        root = Path(leak_scan.__file__).resolve().parents[1]
        audit_root = root / "skills" / "audit"
        package = "\n".join(
            path.read_text(encoding="utf-8")
            for path in audit_root.rglob("*.md")
        )
        text = (audit_root / "references" / "hosted-workflow.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("audit_skill_cancel", package)
        self.assertIn("Agent instructions deliberately expose no cancellation call", text)
        self.assertIn("The Stop panel and CLI", " ".join(text.split()))

    def test_balance_marker_has_explicit_post_mcp_precedence(self):
        root = Path(leak_scan.__file__).resolve().parents[1]
        text = (root / "skills" / "audit" / "references" / "de-lite-routing.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "literal `insufficient_balance` marker is sufficient evidence",
            text,
        )
        self.assertIn(
            "one local advisory conversion",
            text,
        )
        self.assertIn(
            "never submits Hosted again",
            text,
        )
        self.assertNotIn(
            "After that\ncall returns, inspect the received MCP result",
            text,
        )


class DegradeProseHardeningTests(unittest.TestCase):
    """PR5b hardening (audit e16131d4, 4/4 convergent): close fail-open, DeepSeek
    camelCase, markdown/glue evasion, code-fence + empty-heading region bugs, and
    the decoy-section bypass."""

    def _region(self, body):
        return "### Degraded / hub-unreachable\n" + body + "\n"

    def test_deepseek_official_capitalization_is_caught(self):
        for v in ("DeepSeek", "deep-seek", "deep_seek", "deepseek"):
            with self.subTest(v=v):
                self.assertTrue(leak_scan.scan_degrade_prose_text(self._region(v)), v)

    def test_markdown_and_glue_formatting_is_caught(self):
        for v in ("adjudication **hint**", "gpt**5**", "reply`prefix`",
                  "replyprefix", "convergent-vs-divergent"):
            with self.subTest(v=v):
                self.assertTrue(leak_scan.scan_degrade_prose_text(self._region(v)), v)

    def test_code_fence_hash_comment_does_not_truncate_region(self):
        body = "```bash\n# run this\n```\nroster gpt-5.5\n"
        self.assertTrue(leak_scan.scan_degrade_prose_text(self._region(body)))

    def test_empty_atx_heading_terminates_region(self):
        # hub-IP AFTER a bare `##` (no space) heading is OUTSIDE the degrade region.
        text = ("### Degraded / hub-unreachable\nclean routing prose only\n"
                "##\nreply_prefix lives here but outside the degrade region\n")
        self.assertEqual(leak_scan.scan_degrade_prose_text(text), [])

    def test_decoy_first_section_does_not_hide_later_one(self):
        text = ("### Degraded / hub-unreachable\nclean prose\n"
                "## Other\nstuff\n"
                "### Degraded / hub-unreachable\nroster o3 deepseek\n")
        self.assertTrue(leak_scan.scan_degrade_prose_text(text))

    def test_similar_heading_is_not_selected(self):
        # 'hub-unreachable-notes' is a different heading — no region, no scan.
        text = "### Degraded / hub-unreachable-notes\nreply_prefix\n"
        self.assertEqual(leak_scan.scan_degrade_prose_text(text), [])

    def test_repo_gate_fails_closed_on_missing_doc(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            findings = leak_scan.scan_degrade_prose_repo(Path(d))
            self.assertTrue(findings)
            self.assertTrue(any("missing" in f.kind for f in findings), findings)

    def test_repo_gate_fails_closed_on_missing_section(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for rel in ("skills/audit/references/de-lite-routing.md",):
                p = Path(d) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# doc with no degrade section\n", encoding="utf-8")
            findings = leak_scan.scan_degrade_prose_repo(Path(d))
            self.assertTrue(any("section-missing" in f.kind for f in findings), findings)

    def test_repo_gate_clean_on_real_repo(self):
        repo_root = Path(leak_scan.__file__).resolve().parents[1]
        self.assertEqual(leak_scan.scan_degrade_prose_repo(repo_root), [])


if __name__ == "__main__":
    unittest.main()
