"""Contract tests for the layer-check skill's progressive disclosure."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "layer-check"


class LayerCheckSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entry = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    def test_entrypoint_routes_to_both_comparison_modes(self):
        direct_route = (
            "For a direct comparison or when constructing a competitor set, read and follow\n"
            "  [references/classification-framework.md](references/classification-framework.md)."
        )
        review_route = (
            "When reviewing an existing market report, strategy document, comparison, or "
            "multi-review result"
        )
        self.assertIn(direct_route, self.entry)
        self.assertIn(review_route, self.entry)
        self.assertIn("Do not load `comparative-review.md` for a simple direct comparison.", self.entry)

    def test_entrypoint_has_route_specific_output_contracts(self):
        normalized = " ".join(self.entry.split())
        self.assertIn("use the claim-based `Review Output`", normalized)
        self.assertIn("For a direct comparison, report one row per compared pair", normalized)

    def test_entrypoint_preserves_local_only_boundary(self):
        self.assertIn("does not call a server", self.entry)
        self.assertIn("does not submit an audit", self.entry)

    def test_entrypoint_defines_stable_relationship_verdicts(self):
        for verdict in (
            "direct substitute",
            "partial substitute",
            "complement or dependency",
            "vertical overlap",
            "comparable but non-substitutable",
            "not meaningfully comparable",
            "insufficient evidence",
        ):
            with self.subTest(verdict=verdict):
                self.assertIn(verdict, self.entry)

    def test_references_are_present_and_discoverable(self):
        for name in ("classification-framework.md", "comparative-review.md"):
            with self.subTest(reference=name):
                path = SKILL_ROOT / "references" / name
                self.assertTrue(path.is_file())

    def test_entrypoint_stays_thin(self):
        self.assertLessEqual(len(self.entry.splitlines()), 120)

    def test_references_and_contract_test_are_in_public_release_inventory(self):
        allowlist = (ROOT / "internal/public_release/allowlist.txt").read_text(
            encoding="utf-8"
        )
        manifest = (ROOT / "internal/public_release/content-manifest.sha256").read_text(
            encoding="utf-8"
        )
        digests = dict(
            (path, digest)
            for digest, path in (
                line.split("  ", 1)
                for line in manifest.splitlines()
                if line and not line.startswith("#")
            )
        )
        for path in (
            "skills/layer-check/SKILL.md",
            "skills/layer-check/references/classification-framework.md",
            "skills/layer-check/references/comparative-review.md",
            "installer/tests/test_layer_check_skill_contract.py",
        ):
            with self.subTest(path=path):
                self.assertIn(path, allowlist.splitlines())
                canonical_bytes = (ROOT / path).read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(digests[path], hashlib.sha256(canonical_bytes).hexdigest())


if __name__ == "__main__":
    unittest.main()
