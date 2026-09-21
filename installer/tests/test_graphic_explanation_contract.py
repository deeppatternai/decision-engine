from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "graphic-explanation" / "SKILL.md"
RECOVERY = ROOT / "skills" / "graphic-explanation" / "references" / "recovery.md"
CAPABILITY = (
    ROOT / "skills" / "graphic-explanation" / "references" / "capability-boundary.md"
)


class GraphicExplanationContractTests(unittest.TestCase):
    def test_visual_flow_uses_direct_call_not_audit_provider_probe(self):
        skill_text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Start directly with `open_ge`", skill_text)
        self.assertNotIn("check_provider_health", skill_text)

    def test_entry_routes_exceptional_states_to_focused_references(self):
        skill_text = SKILL.read_text(encoding="utf-8")
        self.assertIn("references/recovery.md", skill_text)
        self.assertIn("references/capability-boundary.md", skill_text)
        self.assertGreaterEqual(skill_text.count("MUST read and follow"), 2)
        self.assertIn("Never invent a second ID", skill_text)
        self.assertIn("Never open an audit or Stopper as fallback", skill_text)
        self.assertLessEqual(len(skill_text.splitlines()), 115)

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
        ):
            self.assertIn(required, text)
        self.assertNotIn("mcp__decision-engine__ge_render", text)
        self.assertNotIn("mcp__decision-engine__audit_skill_result", text)
        self.assertNotIn("NO readable text", text)
        self.assertNotIn("fonts-noto-cjk", text)

    def test_slow_diagram_auto_open_is_best_effort_and_recoverable(self):
        text = RECOVERY.read_text(encoding="utf-8")
        for required in (
            "`status:\"scheduled\"`",
            "best-effort, not durable",
            "retain the `run_id`",
            "If the user later reports that no popup appeared",
            "background failure",
            "native status notice",
            "waits for up to 10 minutes",
            "failed results retain the `run_id`",
            "retry `open_ge` with the exact",
            "`client_request_id` returned by that response",
            "`status:\"request_outcome_unknown\"`",
            'client_request_id="<returned client_request_id>"',
            "## Reopen An Unchanged Completed Artifact",
            "known prior `run_id`",
        ):
            self.assertIn(required, text)

    def test_capability_boundary_preserves_fail_closed_local_routing(self):
        text = CAPABILITY.read_text(encoding="utf-8")
        for required in (
            "de_lite_capability_status.py",
            "status=unactivated",
            "status=activated",
            "status=unknown",
            "Windows PowerShell",
            "DE Lite unsupported-capability response",
            "不得启动 DE Lite 本地审核",
            "Do not mix the two languages",
            "English graphic template",
            "English comic template",
            "unsupported conversation language",
        ):
            self.assertIn(required, text)

    def test_capability_reasons_are_split_from_unactivated_templates(self):
        text = CAPABILITY.read_text(encoding="utf-8")
        unactivated = text.split("### `status=unactivated`", 1)[1].split(
            "### Other supported capability failures", 1
        )[0]
        other_failures = text.split("### Other supported capability failures", 1)[1]

        self.assertIn("Decision Engine is not activated", unactivated)
        self.assertIn("Decision Engine 尚未激活", unactivated)
        self.assertNotIn("`mcp_unavailable`", unactivated)
        self.assertIn("`mcp_unavailable`", other_failures)
        self.assertIn("must not use an unactivated template", other_failures)
        self.assertIn("must not claim that the device is unactivated", other_failures)
        self.assertNotIn(
            "I can't create the visual explanation right now because Decision Engine is not activated",
            other_failures,
        )


if __name__ == "__main__":
    unittest.main()
