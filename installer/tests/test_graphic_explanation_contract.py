from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "graphic-explanation" / "SKILL.md"


class GraphicExplanationContractTests(unittest.TestCase):
    def test_visual_flow_uses_direct_call_not_audit_provider_probe(self):
        skill_text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Start directly with `open_ge`", skill_text)
        self.assertNotIn("check_provider_health", skill_text)

    def test_comic_v2_contract_is_explicit_and_routing_only(self):
        text = SKILL.read_text(encoding="utf-8")
        for required in (
            "emit `comic_spec_version: 2`",
            "1–8 ordered `panels`",
            "Each panel has an `id`",
            "optional `title`, a `visual` object",
            "`required_visuals`, `forbidden_visuals`",
            "`dialogue` as ordered",
            "`{speaker?, text}` objects",
            "server accepts legacy flat panel fields",
            "shape is documented on the `visual_render` tool's own `inputSchema`",
            "ONE `open_ge` call for every mode",
            "retry `open_ge` with the exact",
            "`client_request_id` returned by that response",
            "`status:\"request_outcome_unknown\"`",
        ):
            self.assertIn(required, text)
        self.assertNotIn("mcp__decision-engine__ge_render", text)
        self.assertNotIn("mcp__decision-engine__audit_skill_result", text)
        self.assertNotIn("NO readable text", text)
        self.assertNotIn("fonts-noto-cjk", text)

    def test_slow_diagram_auto_open_is_best_effort_and_recoverable(self):
        text = SKILL.read_text(encoding="utf-8")
        for required in (
            "`status:\"scheduled\"`",
            "best-effort, not durable",
            "retain the `run_id`",
            "If the user later reports that no popup appeared",
            "background failure",
            "native status notice",
            "waits for up to 10 minutes",
            "failed results retain the `run_id`",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
