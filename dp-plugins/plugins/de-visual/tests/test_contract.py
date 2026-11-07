from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESOURCE = "https://endpoint.deeppattern.ai/plugin/mcp/v1/visual"
VERSION = "0.1.0-remote.1"


def read_json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class VisualPluginContractTests(unittest.TestCase):
    def test_manifests_share_isolated_visual_resource(self):
        for relative in ("mcp.json", "cursor.mcp.json", ".mcp.json", "codex/.mcp.json", "cowork/.mcp.json"):
            with self.subTest(relative=relative):
                server = read_json(relative)["mcpServers"]["visual"]
                self.assertEqual(server["type"], "http")
                self.assertEqual(server["url"], RESOURCE)
                self.assertNotIn("command", server)
        codex = read_json("codex/.mcp.json")["mcpServers"]["visual"]
        auth = codex["extensions"]["com.openai"]["auth"]
        self.assertEqual(auth["resource"], RESOURCE)
        self.assertEqual(auth["client"], {"mode": "cimd"})
        self.assertEqual(auth["baseScopes"], ["visual:submit", "visual:read", "visual:cancel"])

    def test_contract_is_runtime_free_and_cross_platform(self):
        contract = read_json("contracts/visual-endpoint-contract.json")
        self.assertEqual(contract["endpoint"]["resource"], RESOURCE)
        self.assertFalse(contract["authorization"]["audit_scope_inheritance"])
        self.assertFalse(contract["authorization"]["plugin_contains_runtime"])
        self.assertFalse(contract["lifecycle"]["artifact_bytes_model_visible"])
        self.assertEqual(contract["presentation"]["platforms"], ["macos", "linux", "windows"])
        self.assertEqual(contract["modes"], ["diagram", "infographic", "comic"])

    def test_versions_and_skills_are_consistent(self):
        for relative in ("plugin.json", ".claude-plugin/plugin.json", "codex/.codex-plugin/plugin.json", "cowork/.claude-plugin/plugin.json", ".cursor-plugin/plugin.json"):
            self.assertEqual(read_json(relative)["version"], VERSION)
        source = (ROOT / "skills/visual/SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(source, (ROOT / "codex/skills/visual/SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(source, (ROOT / "cowork/skills/visual/SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("visual_status", source)
        self.assertIn("wait_seconds=20", source)

    def test_public_metadata_declares_mit_and_public_repository(self):
        self.assertEqual(read_json("plugin.json")["license"], "MIT")
        self.assertEqual(read_json("codex/.codex-plugin/plugin.json")["license"], "MIT")
        self.assertEqual(read_json("deepseek-harness/package.json")["license"], "MIT")
        self.assertEqual(
            read_json(".cursor-plugin/plugin.json")["repository"],
            "https://github.com/deeppatternai/decision-engine",
        )
        repo = ROOT.parents[2]
        license_text = (repo / "LICENSE.md").read_text(encoding="utf-8").splitlines()
        for host in ("codex", "cowork"):
            with self.subTest(host=host):
                self.assertEqual(
                    (ROOT / host / "LICENSE.md").read_text(encoding="utf-8").splitlines(),
                    license_text,
                )

    def test_deepseek_harness_declares_only_verified_host_versions(self):
        package = read_json("deepseek-harness/package.json")
        self.assertEqual(package["version"], "0.1.0-remote.2")
        self.assertEqual(
            package["engines"]["dsh"],
            "0.2.0-rc.2 || 0.2.1-rc.5 || 0.2.2-rc.1",
        )
        self.assertEqual(
            package["peerDependencies"]["@deepseek-ai/cordis"],
            "4.0.4 || 4.0.5 || 4.1.0",
        )
        for dependency in (
            "@deepseek-ai/dsh-credentials",
            "@deepseek-ai/dsh-llm",
            "@deepseek-ai/dsh-mcp-client",
            "@deepseek-ai/dsh-skill-filesystem",
            "@deepseek-ai/dsh-tools",
        ):
            self.assertEqual(
                package["peerDependencies"][dependency],
                "0.2.0-rc.2 || 0.2.1-rc.5 || 0.2.2-rc.1",
            )
            self.assertEqual(package["devDependencies"][dependency], "0.2.2-rc.1")

    def test_no_credentials_or_runtime_are_packaged(self):
        serialized = "\n".join((ROOT / relative).read_text(encoding="utf-8").lower() for relative in ("mcp.json", ".mcp.json", "codex/.mcp.json", "cowork/.mcp.json"))
        for forbidden in ("access_token", "refresh_token", "device_token", "client_secret", "activation_code"):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse((ROOT / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
