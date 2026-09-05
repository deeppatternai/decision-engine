"""Behavior tests for host onboarding evidence (installer.host_onboarding).

Every test pins the Codex paths (config and skills) and the managed skills payload
inside a temp directory, so no probe can read this machine's real ~/.codex.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from installer import host_onboarding, mcp_config
from installer.config import ShellError


class CodexOnboardingEvidenceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.codex = self.tmp / ".codex"
        self.payload = self.tmp / "managed" / "skills"
        (self.payload / "audit").mkdir(parents=True)
        env = mock.patch.dict(
            os.environ,
            {
                "CODEX_CONFIG": str(self.codex / "config.toml"),
                "CODEX_SKILLS_DIR": str(self.codex / "skills"),
                "DE_CONFIG_PATH": str(self.tmp / "managed" / "config.json"),
                # The sibling hosts now have skill probes too — pin theirs inside the temp
                # tree as well, or this case would read the developer's real ~/.claude/skills.
                "CLAUDE_SKILLS_DIR": str(self.tmp / "no-claude" / "skills"),
                "CURSOR_SKILLS_DIR": str(self.tmp / "no-cursor" / "skills"),
                "TRAE_WORK_CONFIG": str(
                    self.tmp / "no-trae-work" / "mcp.json"
                ),
                "TRAE_WORK_SKILLS_DIR": str(
                    self.tmp / "no-trae-work" / "skills"
                ),
                "TRAE_WORK_APP_ROOT": str(
                    self.tmp / "no-trae-work" / "app"
                ),
                "TRAE_WORK_CN_CONFIG": str(
                    self.tmp / "no-trae-work-cn" / "mcp.json"
                ),
                "TRAE_WORK_CN_SKILLS_DIR": str(
                    self.tmp / "no-trae-work-cn" / "skills"
                ),
                "TRAE_WORK_CN_APP_ROOT": str(
                    self.tmp / "no-trae-work-cn" / "app"
                ),
                "WORKBUDDY_CONFIG": str(
                    self.tmp / "no-workbuddy" / "mcp.json"
                ),
                "WORKBUDDY_SKILLS_DIR": str(
                    self.tmp / "no-workbuddy" / "skills"
                ),
                "WORKBUDDY_APP_ROOT": str(
                    self.tmp / "no-workbuddy" / "app"
                ),
                "QODER_CONFIG": str(self.tmp / "no-qoder" / "mcp.json"),
                "QODER_SKILLS_DIR": str(
                    self.tmp / "no-qoder" / "skills"
                ),
                "QODER_APP_ROOT": str(self.tmp / "no-qoder" / "app"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def _install_skill(self, name: str = "audit") -> None:
        (self.codex / "skills").mkdir(parents=True, exist_ok=True)
        (self.codex / "skills" / name).mkdir(exist_ok=True)

    def test_absent_codex_shows_no_evidence(self):
        self.assertEqual(host_onboarding.onboarding_evidence("codex"), ())
        self.assertFalse(host_onboarding.is_onboarded("codex"))
        self.assertEqual(host_onboarding.onboarded_clients(["codex"]), ())

    def test_bare_host_directory_is_presence_evidence_only(self):
        self.codex.mkdir()

        evidence = host_onboarding.onboarding_evidence("codex")

        self.assertEqual(evidence, (host_onboarding.PRESENCE_EVIDENCE,))
        self.assertFalse(host_onboarding.has_payload_evidence(evidence))

    def test_routed_de_skill_is_payload_evidence(self):
        self._install_skill()

        evidence = host_onboarding.onboarding_evidence("codex")

        self.assertIn("skill route", evidence)
        self.assertTrue(host_onboarding.has_payload_evidence(evidence))

    def test_configured_but_empty_skills_directory_is_not_payload_evidence(self):
        # CODEX_SKILLS_DIR being set makes the route "in use" (intent); only an actually routed
        # skill is evidence that a payload landed.
        (self.codex / "skills").mkdir(parents=True)

        evidence = host_onboarding.onboarding_evidence("codex")

        self.assertNotIn("skill route", evidence)

    def test_foreign_skill_in_the_host_is_not_de_payload_evidence(self):
        self._install_skill("someone-elses-skill")

        self.assertNotIn("skill route", host_onboarding.onboarding_evidence("codex"))

    def test_one_failing_probe_cannot_hide_the_others(self):
        self._install_skill()
        with mock.patch.object(
            host_onboarding,
            "ONBOARDING_PROBES",
            {
                "codex": (
                    (
                        host_onboarding.PRESENCE_EVIDENCE,
                        mock.Mock(side_effect=OSError("unreadable")),
                    ),
                    (
                        "skill route",
                        lambda: host_onboarding._skill_payload_present("codex"),
                    ),
                )
            },
        ):
            evidence = host_onboarding.onboarding_evidence("codex")

        self.assertEqual(evidence, ("skill route",))

    def test_report_covers_only_the_requested_hosts(self):
        self._install_skill()

        self.assertEqual(host_onboarding.onboarding_report(["claude-code"]), {})
        self.assertIn("codex", host_onboarding.onboarding_report(["codex"]))

    def test_host_without_probes_is_never_onboarded(self):
        # Claude Desktop takes no skills at all, so a missing entry there is a choice.
        self.assertEqual(host_onboarding.onboarding_evidence("claude-desktop"), ())


class SkillPayloadHostsTestCase(unittest.TestCase):
    """Claude Code and Cursor keep their skills outside their MCP config too, so an installed
    DE skill payload is onboarding evidence for them as well — but their mere presence is not."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.payload = self.tmp / "managed" / "skills"
        (self.payload / "audit").mkdir(parents=True)
        self.skills = {
            "claude-code": self.tmp / ".claude" / "skills",
            "cursor": self.tmp / ".cursor" / "skills",
            "trae": self.tmp / ".trae" / "skills",
            "trae-work": self.tmp / ".trae" / "skills",
            "trae-cn": self.tmp / ".trae-cn" / "skills",
            "trae-work-cn": self.tmp / ".trae-cn" / "skills",
            "workbuddy": self.tmp / ".workbuddy" / "skills",
            "qoder": self.tmp / ".qoder" / "skills",
            "qoder-cn": self.tmp / ".qoder-cn" / "skills",
        }
        env = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_SKILLS_DIR": str(self.skills["claude-code"]),
                "CURSOR_SKILLS_DIR": str(self.skills["cursor"]),
                "CLAUDE_CODE_CONFIG": str(self.tmp / ".claude.json"),
                "CURSOR_CONFIG": str(self.tmp / ".cursor" / "mcp.json"),
                "TRAE_CONFIG": str(self.tmp / "trae-profile" / "User" / "mcp.json"),
                "TRAE_SKILLS_DIR": str(self.skills["trae"]),
                "TRAE_APP_ROOT": str(self.tmp / "no-trae" / "app"),
                "TRAE_WORK_CONFIG": str(
                    self.tmp / "trae-profile" / "User" / "mcp.json"
                ),
                "TRAE_WORK_SKILLS_DIR": str(self.skills["trae-work"]),
                "TRAE_WORK_APP_ROOT": str(self.tmp / "no-trae-work" / "app"),
                "TRAE_CN_CONFIG": str(
                    self.tmp / "trae-cn-desktop-profile" / "User" / "mcp.json"
                ),
                "TRAE_CN_SKILLS_DIR": str(self.skills["trae-cn"]),
                "TRAE_CN_APP_ROOT": str(self.tmp / "no-trae-cn" / "app"),
                "TRAE_WORK_CN_CONFIG": str(
                    self.tmp / "trae-cn-profile" / "User" / "mcp.json"
                ),
                "TRAE_WORK_CN_SKILLS_DIR": str(self.skills["trae-work-cn"]),
                "TRAE_WORK_CN_APP_ROOT": str(
                    self.tmp / "no-trae-work-cn" / "app"
                ),
                "WORKBUDDY_CONFIG": str(
                    self.tmp / "workbuddy-profile" / "mcp.json"
                ),
                "WORKBUDDY_SKILLS_DIR": str(self.skills["workbuddy"]),
                "WORKBUDDY_APP_ROOT": str(
                    self.tmp / "no-workbuddy" / "app"
                ),
                "QODER_CONFIG": str(self.tmp / "qoder-profile" / "mcp.json"),
                "QODER_SKILLS_DIR": str(self.skills["qoder"]),
                "QODER_APP_ROOT": str(self.tmp / "no-qoder" / "app"),
                "QODER_CN_CONFIG": str(
                    self.tmp / "qoder-cn-profile" / "settings.json"
                ),
                "QODER_CN_SKILLS_DIR": str(self.skills["qoder-cn"]),
                "QODER_CN_APP_ROOT": str(self.tmp / "no-qoder-cn" / "app"),
                "DE_CONFIG_PATH": str(self.tmp / "managed" / "config.json"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def test_routed_de_skill_is_payload_evidence(self):
        for client, directory in self.skills.items():
            with self.subTest(client=client):
                (directory / "audit").mkdir(parents=True, exist_ok=True)

                evidence = host_onboarding.onboarding_evidence(client)

                self.assertEqual(evidence, ("skill route",))
                self.assertTrue(host_onboarding.has_payload_evidence(evidence))

    def test_present_host_without_de_skills_is_not_onboarded(self):
        # The host exists and even has its own skills — but none of ours.
        for client, directory in self.skills.items():
            with self.subTest(client=client):
                (directory / "someone-elses-skill").mkdir(
                    parents=True, exist_ok=True
                )
                (self.tmp / ".claude.json").touch()

                self.assertEqual(host_onboarding.onboarding_evidence(client), ())

    def test_cursor_evidence_does_not_depend_on_its_mcp_entry(self):
        # Cursor's Doctor-side skill predicate is gated on the MCP entry existing, which is the
        # very entry this probe reports missing; the probe must not inherit that gate.
        (self.skills["cursor"] / "audit").mkdir(parents=True)

        self.assertEqual(mcp_config.entry_status("cursor"), "absent")
        self.assertNotIn("cursor", mcp_config.active_skill_routes(for_doctor=True))
        self.assertIn("skill route", host_onboarding.onboarding_evidence("cursor"))


class ProbeRegistryValidationTestCase(unittest.TestCase):
    """Each case asserts the REASON, not just that something was refused — with several rules
    in play, a bare assertRaises would pass on the wrong one."""

    def _registry(self, **overrides):
        """The shipped registry with one host replaced, or removed via ``None``."""
        registry = dict(host_onboarding.ONBOARDING_PROBES)
        for client, entries in overrides.items():
            if entries is None:
                registry.pop(client, None)
            else:
                registry[client] = entries
        return registry

    def test_shipped_registry_is_valid(self):
        host_onboarding._validate_probes()

    def test_unknown_host_is_refused(self):
        with self.assertRaises(ShellError) as ctx:
            host_onboarding._validate_probes(
                self._registry(**{"not-a-host": ((host_onboarding.SKILL_EVIDENCE, bool),)})
            )

        self.assertIn("unknown onboarding host", str(ctx.exception))

    def test_host_without_an_mcp_entry_probe_is_refused(self):
        skills_only = replace(
            mcp_config.CLIENT_SPECS["codex"],
            doctor_capabilities=frozenset({"skills"}),
        )
        with mock.patch.object(mcp_config, "CLIENT_SPECS", {"codex": skills_only}):
            with self.assertRaises(ShellError) as ctx:
                host_onboarding._validate_probes({"codex": (("host directory", bool),)})

        self.assertIn("does not register an MCP entry", str(ctx.exception))

    def test_unknown_evidence_category_is_refused(self):
        with self.assertRaises(ShellError) as ctx:
            host_onboarding._validate_probes(self._registry(codex=(("vibes", bool),)))

        self.assertIn("unknown evidence category", str(ctx.exception))

    def test_skill_evidence_without_a_skills_path_is_refused(self):
        pathless = replace(mcp_config.CLIENT_SPECS["codex"], skills_global_path=None)
        with mock.patch.object(mcp_config, "CLIENT_SPECS", {"codex": pathless}):
            with self.assertRaises(ShellError) as ctx:
                host_onboarding._validate_probes(
                    {"codex": ((host_onboarding.SKILL_EVIDENCE, bool),)}
                )

        self.assertIn("without a skills path", str(ctx.exception))

    def test_registered_host_that_loses_its_skill_probe_is_refused(self):
        with self.assertRaises(ShellError) as ctx:
            host_onboarding._validate_probes(self._registry(cursor=None))

        self.assertIn("cursor", str(ctx.exception))
        self.assertIn("delivers skills", str(ctx.exception))

    def test_a_newly_registered_skill_host_builds_its_probe_from_metadata(self):
        # The scenario this guard exists for: a Cursor-like agent joins CLIENT_SPECS, ships DE
        # skills, and nobody adds a probe. Its skills could land with no MCP entry and no check
        # would notice — so the module refuses to import instead.
        newcomer = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            id="newcomer",
            host_family="newcomer",
        )
        with mock.patch.object(
            mcp_config,
            "CLIENT_SPECS",
            {**mcp_config.CLIENT_SPECS, "newcomer": newcomer},
        ):
            probes = host_onboarding._build_onboarding_probes(
                mcp_config.CLIENT_SPECS
            )
            host_onboarding._validate_probes(probes)

        self.assertEqual(
            tuple(label for label, _probe in probes["newcomer"]),
            (host_onboarding.SKILL_EVIDENCE,),
        )

    def test_skill_host_without_declarative_evidence_is_refused(self):
        newcomer = replace(
            mcp_config.CLIENT_SPECS["cursor"],
            id="newcomer",
            host_family="newcomer",
            onboarding_evidence=(),
        )
        with (
            mock.patch.object(mcp_config, "CLIENT_SPECS", {"newcomer": newcomer}),
            self.assertRaisesRegex(ShellError, "delivers skills"),
        ):
            host_onboarding._build_onboarding_probes(mcp_config.CLIENT_SPECS)

    def test_a_host_that_ships_no_skills_needs_no_probe(self):
        # Claude Desktop is MCP-only: a missing entry there is a choice, not a contradiction.
        self.assertEqual(
            mcp_config.CLIENT_SPECS["claude-desktop"].skill_delivery_mode, "none"
        )
        self.assertNotIn("claude-desktop", host_onboarding.ONBOARDING_PROBES)
        host_onboarding._validate_probes()


if __name__ == "__main__":
    unittest.main()
