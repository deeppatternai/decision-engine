from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
RESOURCE = "https://endpoint.deeppattern.ai/plugin/mcp/v1/audit"
VERSION = "0.2.13-remote"
CLAUDE_AUTH_HEADER = {"X-DeepPattern-MCP-Auth-Mode": "transport"}
OPENAI_OAUTH_EXTENSION = {
    "com.openai": {
        "auth": {
            "type": "oauth",
            "client": {"mode": "cimd"},
            "resource": RESOURCE,
            "baseScopes": ["audit:submit", "audit:read", "audit:events"],
        }
    }
}


def read_json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class RemotePluginContractTests(unittest.TestCase):
    def test_host_manifests_use_the_same_resource_with_host_specific_oauth(self):
        codex = read_json("codex/.mcp.json")["mcpServers"]["audit"]
        self.assertEqual(
            codex,
            {
                "type": "http",
                "url": RESOURCE,
                "extensions": OPENAI_OAUTH_EXTENSION,
            },
        )
        for relative in ("mcp.json", "cursor.mcp.json"):
            with self.subTest(relative=relative):
                server = read_json(relative)["mcpServers"]["audit"]
                self.assertEqual(server, {"type": "http", "url": RESOURCE})
        claude = read_json(".mcp.json")["mcpServers"]["audit"]
        self.assertEqual(
            claude,
            {"type": "http", "url": RESOURCE, "headers": CLAUDE_AUTH_HEADER},
        )
        cowork = read_json("cowork/.mcp.json")["mcpServers"]["audit"]
        self.assertEqual(
            cowork,
            {"type": "http", "url": RESOURCE, "headers": CLAUDE_AUTH_HEADER},
        )

    def test_codex_manifest_declares_stable_cimd_oauth_identity(self):
        portable_plugin = read_json("plugin.json")
        self.assertEqual(portable_plugin["mcpServers"], "./mcp.json")
        self.assertEqual(portable_plugin["skills"], "./skills/")
        codex_plugin = read_json("codex/.codex-plugin/plugin.json")
        self.assertEqual(codex_plugin["mcpServers"], "./.mcp.json")
        self.assertEqual(codex_plugin["skills"], "./skills/")
        server = read_json("codex/.mcp.json")["mcpServers"]["audit"]
        self.assertEqual(server["extensions"], OPENAI_OAUTH_EXTENSION)
        self.assertNotIn("clientId", json.dumps(server))
        self.assertNotIn("clientSecret", json.dumps(server))

    def test_manifests_contain_no_local_command_or_credentials(self):
        serialized = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8").lower()
            for relative in (
                "mcp.json",
                ".mcp.json",
                "cursor.mcp.json",
                "codex/.mcp.json",
                "cowork/.mcp.json",
            )
        )
        for forbidden in (
            "command",
            "access_token",
            "refresh_token",
            "device_token",
            "activation_secret",
            "client_secret",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_contract_is_oauth_isolated_and_runtime_free(self):
        contract = read_json("contracts/audit-endpoint-contract.json")
        self.assertEqual(contract["endpoint"]["resource"], RESOURCE)
        self.assertEqual(contract["authorization"]["mode"], "oauth_2_1_pkce")
        self.assertEqual(contract["authorization"]["credential_owner"], "host_oauth_store")
        self.assertEqual(
            contract["authorization"]["manifest_authentication"],
            {
                "hosts": ["codex"],
                "type": "oauth",
                "client_mode": "cimd",
                "resource": RESOURCE,
                "base_scopes": ["audit:submit", "audit:read", "audit:events"],
            },
        )
        self.assertEqual(
            contract["authorization"]["installation_credential_policy"],
            "host_managed_on_install_no_plugin_credential_introspection",
        )
        self.assertFalse(contract["authorization"]["plugin_contains_runtime"])
        self.assertFalse(contract["authorization"]["legacy_device_token_fallback"])
        self.assertFalse(contract["distribution"]["runtime_required"])
        self.assertEqual(
            contract["authorization"]["unauthenticated_discovery_methods"],
            [
                "initialize",
                "tools/list",
                "resources/list",
                "resources/read",
                "ping",
                "notifications/initialized",
            ],
        )
        self.assertEqual(
            contract["authorization"]["tool_authentication_challenge"],
            "mcp/www_authenticate",
        )
        self.assertEqual(
            contract["authorization"]["transport_authentication"],
            {
                "hosts": ["claude_code", "claude_cowork"],
                "request_header": {
                    "name": "X-DeepPattern-MCP-Auth-Mode",
                    "value": "transport",
                },
                "challenge": "http_401_www_authenticate",
                "authenticated_dispatch": "shared_oauth_principal_and_scope_checks",
            },
        )
        self.assertEqual(
            contract["authorization"]["oauth_continuation"],
            "resume_original_tool_call_with_same_idempotency_key",
        )
        self.assertEqual(
            contract["policies"]["legacy_routes_unchanged"],
            ["/v1/devices/activate", "/mcp", "/mcp/v1/audit"],
        )

    def test_contract_declares_cross_platform_web_progress_resource(self):
        contract = read_json("contracts/audit-endpoint-contract.json")
        presentation = contract["presentation"]
        self.assertEqual(presentation["mode"], "remote_https_webui")
        self.assertEqual(presentation["schema_version"], "audit-progress.v1")
        self.assertEqual(presentation["platforms"], ["macos", "linux", "windows"])
        self.assertEqual(
            presentation["delivery"],
            "mcp_app_with_resource_link_browser_fallback",
        )
        self.assertEqual(
            presentation["mcp_app_resource_uri"],
            "ui://deeppattern/audit-progress-v1.html",
        )
        self.assertEqual(
            presentation["mcp_app_mime_type"], "text/html;profile=mcp-app"
        )
        self.assertEqual(
            presentation["mcp_app_rendering_tools"],
            ["audit_skill_submit", "audit_skill_status"],
        )
        self.assertEqual(
            presentation["mcp_app_callable_tools"],
            ["audit_skill_status", "audit_skill_result"],
        )
        self.assertTrue(presentation["ticket_is_single_use"])
        self.assertEqual(
            presentation["ticket_transport"], "url_fragment_not_sent_on_get"
        )
        self.assertEqual(
            presentation["ticket_exchange_method"], "same_origin_post"
        )
        self.assertTrue(presentation["browser_close_keeps_audit_running"])
        self.assertEqual(presentation["stop_policy"], "owned_unclaimed_queued_only")
        self.assertFalse(presentation["local_runtime_required"])
        self.assertTrue(
            presentation["mcp_app_metadata_available_before_authentication"]
        )
        self.assertEqual(
            presentation["model_polling_after_non_terminal_submit"],
            "claude_desktop_code_and_cowork_bounded_wait_only",
        )
        self.assertEqual(presentation["mcp_app_polling_owner"], "embedded_app")
        self.assertEqual(
            presentation["terminal_result_delivery"],
            "app_calls_result_once_updates_model_context_and_sends_followup",
        )
        self.assertEqual(
            presentation["reviewer_identity_policy"],
            "model_name_only_for_server_debug_authorized_subject",
        )
        self.assertEqual(
            presentation["non_mcp_apps_delivery"],
            "host_specific_with_exact_resource_link_fallback",
        )
        self.assertNotIn("stop-panel", contract["policies"]["forbidden_capabilities"])

    def test_deepseek_harness_uses_native_public_pkce_without_changing_other_hosts(self):
        contract = read_json("contracts/audit-endpoint-contract.json")
        harness = contract["authorization"]["deepseek_harness_native_authentication"]
        self.assertEqual(harness["client_id"], "deep-pattern-audit-harness")
        self.assertEqual(harness["client_type"], "preconfigured_public_pkce")
        self.assertEqual(
            harness["registered_redirect_uri"],
            "http://127.0.0.1/deeppattern/oauth/callback",
        )
        self.assertEqual(harness["actual_redirect_port"], "ephemeral_loopback")
        self.assertEqual(harness["credential_owner"], "harness_opaque_grant_store")
        self.assertEqual(
            harness["credential_storage_claim"],
            "private_user_store_not_os_keychain",
        )
        self.assertEqual(
            harness["request_header"],
            {"name": "X-DeepPattern-MCP-Auth-Mode", "value": "transport"},
        )
        self.assertFalse(harness["dcr_required"])
        self.assertEqual(
            contract["distribution"]["deepseek_harness_entry"],
            "deepseek-harness/",
        )
        self.assertEqual(
            contract["distribution"]["deepseek_harness_artifact"],
            "de-audit-harness-0.2.13-remote.11.tgz",
        )
        self.assertEqual(
            contract["presentation"]["deepseek_harness_delivery"],
            "resource_link_then_bounded_status_wait_then_terminal_result",
        )
        self.assertEqual(
            contract["presentation"]["deepseek_harness_continuation"],
            "plugin_deferred_context_after_submit_and_status_until_single_terminal_result",
        )
        self.assertEqual(
            contract["presentation"]["deepseek_harness_native_tool_cards"],
            "replay_safe_per_tool_call_hcar_1",
        )
        self.assertFalse(
            contract["presentation"]["deepseek_harness_inline_mcp_app_required"]
        )

        package = read_json("deepseek-harness/package.json")
        self.assertEqual(package["version"], "0.2.13-remote.11")
        self.assertIs(package["dsh"]["client"]["immediately"], True)
        self.assertEqual(package["dsh"]["client"]["platform"], "web")
        self.assertEqual(
            package["dsh"]["client"]["inject"],
            [
                "@deepseek-ai/dsh-client-ui-renderer",
                "@deepseek-ai/dsh-client-ui-tool",
            ],
        )
        self.assertEqual(
            package["engines"]["dsh"],
            "0.2.0-rc.2 || 0.2.1-rc.5 || 0.2.2-rc.1",
        )
        self.assertEqual(package["dependencies"]["@modelcontextprotocol/client"], "2.2.0")
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
        self.assertNotIn("postinstall", package["scripts"])
        self.assertNotIn("preinstall", package["scripts"])
        patch = (ROOT / "deepseek-harness/cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("providerName: deeppattern-audit", patch)
        harness_audit = (ROOT / "deepseek-harness/skills/audit/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Immediately after submit succeeds", harness_audit)
        self.assertIn("before\nthe first status call", harness_audit)
        self.assertIn("never defer the link until the terminal result", harness_audit)
        self.assertIn("deep-pattern-audit` continuation message", harness_audit)

    def test_claude_desktop_hosts_wait_for_terminal_result_without_changing_codex_app_flow(self):
        contract = read_json("contracts/audit-endpoint-contract.json")
        presentation = contract["presentation"]
        self.assertEqual(
            presentation["model_polling_after_non_terminal_submit"],
            "claude_desktop_code_and_cowork_bounded_wait_only",
        )
        self.assertEqual(presentation["bounded_status_wait_seconds"], 20)
        self.assertEqual(presentation["bounded_status_wait_max_seconds"], 25)
        self.assertEqual(
            presentation["claude_desktop_code_delivery"],
            "resource_link_then_bounded_status_wait_then_terminal_result",
        )
        self.assertEqual(
            presentation["claude_cowork_delivery"],
            "resource_link_then_bounded_status_wait_then_terminal_result",
        )
        self.assertEqual(
            presentation["mcp_app_host_delivery"],
            "submit_then_app_owned_polling_and_followup",
        )

        skill = (ROOT / "skills" / "audit" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        codex_skill = (ROOT / "codex" / "skills" / "audit" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        cowork_skill = (ROOT / "cowork" / "skills" / "audit" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(skill, codex_skill)
        self.assertEqual(skill, cowork_skill)
        self.assertIn("Claude Desktop Code", skill)
        self.assertIn("Claude Cowork", skill)
        self.assertIn("wait_seconds=20", skill)
        self.assertIn("include_view_resource=false", skill)
        self.assertIn("audit_skill_result", skill)
        self.assertIn("Codex", skill)
        self.assertIn("embedded App owns polling", skill)

    def test_plugin_source_contains_no_runtime_or_local_bridge(self):
        for relative in (
            "runtime",
            "activation_ui.py",
            "audit_progress.py",
            "de_audit_runtime.py",
            "launcher.py",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_versions_and_repository_marketplace_entry_are_consistent(self):
        self.assertEqual(read_json("plugin.json")["version"], VERSION)
        self.assertEqual(read_json("codex/.codex-plugin/plugin.json")["version"], VERSION)
        self.assertEqual(read_json(".claude-plugin/plugin.json")["version"], VERSION)
        self.assertEqual(
            read_json("cowork/.claude-plugin/plugin.json")["version"], VERSION
        )
        self.assertEqual(read_json(".cursor-plugin/plugin.json")["version"], VERSION)
        self.assertEqual(
            read_json(".claude-plugin/marketplace.json")["plugins"][0]["version"],
            VERSION,
        )
        root_marketplace = json.loads(
            (REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        entry = root_marketplace["plugins"][0]
        self.assertEqual(entry["source"], "./dp-plugins/plugins/de-audit")
        self.assertEqual(entry["version"], VERSION)
        cursor_marketplace = json.loads(
            (REPO / ".cursor-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        cursor_entry = cursor_marketplace["plugins"][0]
        self.assertEqual(cursor_entry["source"], "dp-plugins/plugins/de-audit")
        self.assertEqual(cursor_entry["version"], VERSION)
        self.assertIn("browser OAuth activation", cursor_entry["description"])
        root_codex_marketplace = json.loads(
            (REPO / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(root_codex_marketplace["name"], "deeppattern-plugins")
        root_codex_entry = root_codex_marketplace["plugins"][0]
        self.assertEqual(
            root_codex_entry["source"],
            {
                "source": "local",
                "path": "./dp-plugins/plugins/de-audit/codex",
            },
        )
        self.assertEqual(
            root_codex_entry["policy"],
            {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
        )
        nested_marketplace = json.loads(
            (REPO / "dp-plugins" / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        nested_entry = nested_marketplace["plugins"][0]
        self.assertEqual(nested_entry["source"], "./plugins/de-audit")
        self.assertEqual(nested_entry["version"], VERSION)
        codex_marketplace = json.loads(
            (REPO / "dp-plugins" / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            codex_marketplace["plugins"][0]["source"]["path"],
            "./plugins/de-audit/codex",
        )
        self.assertEqual(
            codex_marketplace["plugins"][0]["policy"]["authentication"],
            "ON_INSTALL",
        )

    def test_public_metadata_declares_mit_and_public_repository(self):
        self.assertEqual(read_json("plugin.json")["license"], "MIT")
        self.assertEqual(read_json("codex/.codex-plugin/plugin.json")["license"], "MIT")
        self.assertEqual(read_json("deepseek-harness/package.json")["license"], "MIT")
        self.assertEqual(
            read_json(".cursor-plugin/plugin.json")["repository"],
            "https://github.com/deeppatternai/decision-engine",
        )
        license_text = (REPO / "LICENSE.md").read_text(encoding="utf-8").splitlines()
        for host in ("codex", "cowork"):
            with self.subTest(host=host):
                self.assertEqual(
                    (ROOT / host / "LICENSE.md").read_text(encoding="utf-8").splitlines(),
                    license_text,
                )

    def test_host_adapter_skills_match_canonical_skills(self):
        for name in ("audit", "setup"):
            canonical = (ROOT / "skills" / name / "SKILL.md").read_bytes()
            for host in ("codex", "cowork"):
                with self.subTest(name=name, host=host):
                    self.assertEqual(
                        (ROOT / host / "skills" / name / "SKILL.md").read_bytes(),
                        canonical,
                    )

    def test_marketplace_boundary_describes_remote_distribution(self):
        boundary = (REPO / "dp-plugins" / "README.md").read_text(encoding="utf-8")
        self.assertNotIn(".github/workflows/", boundary)
        self.assertIn("plugins/de-visual", boundary)
        self.assertIn("platform-independent package", boundary)
        self.assertIn("repository-root `.agents/plugins/marketplace.json`", boundary)
        self.assertIn("`ON_INSTALL` authentication", boundary)
        self.assertIn("separate plugin runtime and OAuth store", boundary)
        self.assertIn("plugins/de-audit/cowork", boundary)
        self.assertNotIn("dp-plugins-native-runtime.yml", boundary)
        self.assertNotIn("Each supported platform must be built", boundary)

    def test_public_readme_does_not_reference_private_rollback_record(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("rollback", readme.lower())

    def test_setup_and_audit_skills_require_host_oauth_without_fallback(self):
        setup = (ROOT / "skills/setup/SKILL.md").read_text(encoding="utf-8")
        audit = (ROOT / "skills/audit/SKILL.md").read_text(encoding="utf-8")
        setup_flat = " ".join(setup.split())
        audit_flat = " ".join(audit.split())
        self.assertIn("`ON_INSTALL` authentication", setup_flat)
        self.assertIn("codex mcp login audit", setup_flat)
        self.assertIn(
            "Never run `codex mcp login audit` inside a model-executed Shell",
            setup_flat,
        )
        self.assertIn("Authenticate or Reconnect action", setup_flat)
        self.assertIn("do not invent a connector-panel location", setup_flat)
        self.assertIn("already-open chat may retain", setup_flat)
        self.assertIn("use a new chat for acceptance", setup_flat)
        self.assertIn("Claude Code", setup_flat)
        self.assertIn("Claude Cowork", setup_flat)
        self.assertIn("separate plugin installation", setup_flat)
        self.assertIn("fixed, non-secret transport-auth header", setup_flat)
        self.assertIn("active Cowork conversation's Connector", setup_flat)
        self.assertIn("global Plugins Connect action", setup_flat)
        self.assertIn("claude mcp login plugin:audit:audit", setup_flat)
        self.assertNotIn("claude mcp login audit", setup_flat)
        self.assertIn("same conversation that owns the pending audit request", setup_flat)
        self.assertIn("pre-authentication MCP snapshot", setup_flat)
        self.assertIn("Start a new Claude Code conversation", setup_flat)
        self.assertIn("do not activate again", setup_flat.lower())
        self.assertIn(
            "do not call an audit tool merely to manufacture an authentication error",
            setup_flat.lower(),
        )
        self.assertIn("never fall back", setup_flat.lower())
        self.assertIn("Never fall back", audit_flat)
        self.assertIn(
            "Before reporting that the audit service is unavailable", audit_flat
        )
        self.assertIn(
            "Never execute `codex mcp login audit` inside a model-executed Shell",
            audit_flat,
        )
        self.assertIn("marketplace declares `ON_INSTALL`", audit_flat)
        self.assertIn("stable OAuth resource and CIMD client mode", audit_flat)
        self.assertIn("host alone decides", audit_flat)
        self.assertIn("must not read credential files", audit_flat)
        self.assertIn("invent a connector-panel location", audit_flat)
        self.assertIn("use `standard` without asking", audit_flat)
        self.assertIn("call `audit_skill_submit` immediately", audit_flat)
        self.assertIn(
            "continue the original request in the same turn",
            audit_flat.lower(),
        )
        self.assertIn("reuse that exact idempotency key", audit_flat)
        self.assertIn("Never create a second key", audit_flat)
        self.assertIn("progress ResourceLink", audit_flat)
        self.assertIn("inline audit card", audit_flat)
        self.assertIn("MCP Apps", audit_flat)
        self.assertIn("Immediately after a successful submit", audit_flat)
        self.assertIn("embedded App owns polling", audit_flat)
        self.assertIn("Claude Desktop Code", audit_flat)
        self.assertIn("Claude Cowork", audit_flat)
        self.assertIn("plugin:audit:audit", audit_flat)
        self.assertIn("current conversation's `/mcp`", audit_flat)
        self.assertIn("start a new Claude Code conversation", audit_flat)
        self.assertIn("dedicated `cowork/` plugin entry", audit_flat)
        self.assertIn("`include_view_resource=false`", audit_flat)
        self.assertIn("`wait_seconds=20`", audit_flat)
        self.assertIn("repeat that bounded status call", audit_flat)
        self.assertIn("Do not call events, do not submit again", audit_flat)
        self.assertIn("return the complete result in the same conversation", audit_flat)
        self.assertIn("calls `audit_skill_result` once", audit_flat)
        self.assertIn("updates model context", audit_flat)
        self.assertIn("then end the turn without model polling", audit_flat)
        self.assertIn("Closing the inline card", audit_flat)
        self.assertIn("does not cancel", audit_flat)
        self.assertNotIn("Ask for the audit tier", audit_flat)


if __name__ == "__main__":
    unittest.main()
