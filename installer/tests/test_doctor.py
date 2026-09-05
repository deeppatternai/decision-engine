"""Tests for the Decision Engine doctor (installer.doctor)."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from installer import (
    client_host_ownership,
    codex_routing,
    config,
    cursor_activation,
    cursor_skill_payload,
    doctor,
    install,
    managed_install,
    mcp_config,
)
from installer import updater
from installer.config import ShellError


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._host_isolation = tempfile.TemporaryDirectory()
        self.addCleanup(self._host_isolation.cleanup)
        trae_config = (
            Path(self._host_isolation.name)
            / "trae-work-cn"
            / "User"
            / "mcp.json"
        )
        environment = mock.patch.dict(
            os.environ,
            {
                "TRAE_WORK_CONFIG": str(
                    Path(self._host_isolation.name)
                    / "trae-work"
                    / "User"
                    / "mcp.json"
                ),
                "TRAE_WORK_SKILLS_DIR": str(
                    Path(self._host_isolation.name)
                    / "no-trae-work"
                    / "skills"
                ),
                "TRAE_WORK_APP_ROOT": str(
                    Path(self._host_isolation.name)
                    / "no-trae-work"
                    / "app"
                ),
                "TRAE_WORK_CN_CONFIG": str(trae_config),
                "TRAE_WORK_CN_SKILLS_DIR": str(
                    Path(self._host_isolation.name)
                    / "no-trae-work-cn"
                    / "skills"
                ),
                "TRAE_WORK_CN_APP_ROOT": str(
                    Path(self._host_isolation.name) / "no-trae-work-cn" / "app"
                ),
                "WORKBUDDY_CONFIG": str(
                    Path(self._host_isolation.name)
                    / "no-workbuddy"
                    / "mcp.json"
                ),
                "WORKBUDDY_SKILLS_DIR": str(
                    Path(self._host_isolation.name)
                    / "no-workbuddy"
                    / "skills"
                ),
                "WORKBUDDY_APP_ROOT": str(
                    Path(self._host_isolation.name)
                    / "no-workbuddy"
                    / "app"
                ),
                "QODER_CONFIG": str(
                    Path(self._host_isolation.name) / "no-qoder" / "mcp.json"
                ),
                "QODER_SKILLS_DIR": str(
                    Path(self._host_isolation.name) / "no-qoder" / "skills"
                ),
                "QODER_APP_ROOT": str(
                    Path(self._host_isolation.name) / "no-qoder" / "app"
                ),
                "QODER_CN_CONFIG": str(
                    Path(self._host_isolation.name) / "no-qoder-cn" / "settings.json"
                ),
                "QODER_CN_SKILLS_DIR": str(
                    Path(self._host_isolation.name) / "no-qoder-cn" / "skills"
                ),
                "QODER_CN_APP_ROOT": str(
                    Path(self._host_isolation.name) / "no-qoder-cn" / "app"
                ),
                "TRAE_CONFIG": str(
                    Path(self._host_isolation.name) / "no-trae" / "mcp.json"
                ),
                "TRAE_SKILLS_DIR": str(
                    Path(self._host_isolation.name) / "no-trae" / "skills"
                ),
                "TRAE_APP_ROOT": str(
                    Path(self._host_isolation.name) / "no-trae" / "app"
                ),
                "TRAE_CN_CONFIG": str(
                    Path(self._host_isolation.name) / "no-trae-cn" / "mcp.json"
                ),
                "TRAE_CN_SKILLS_DIR": str(
                    Path(self._host_isolation.name) / "no-trae-cn" / "skills"
                ),
                "TRAE_CN_APP_ROOT": str(
                    Path(self._host_isolation.name) / "no-trae-cn" / "app"
                ),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def _build_worktree(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)
        (root / ".gitignore").write_text("/.managed-install.json\n", encoding="utf-8")
        (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(root), "add", ".gitignore", "tracked.txt"],
            check=True,
            timeout=10,
        )
        subprocess.run(
            [
                "git", "-C", str(root), "-c", "user.name=Doctor Test",
                "-c", "user.email=doctor@example.invalid", "commit", "-qm", "baseline",
            ],
            check=True,
            timeout=10,
        )

    def test_managed_update_warns_for_a_legacy_install(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            doctor.config, "managed_component_root", return_value=Path(tmp)
        ):
            result = doctor.check_managed_update()
        self.assertEqual(result.status, "WARN")
        self.assertIn("legacy", result.detail)

    @mock.patch("platform.system", return_value="Darwin")
    def test_host_stopper_agent_ready_is_a_pass(self, _system):
        with mock.patch(
            "installer.stopper_launch_agent.status",
            return_value={"status": "ready", "loaded": True},
        ):
            result = doctor.check_stopper_host_bridge()

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.name, "stopper-host")

    @mock.patch("platform.system", return_value="Darwin")
    def test_missing_host_stopper_agent_is_a_repairable_fail(self, _system):
        with mock.patch(
            "installer.stopper_launch_agent.status",
            return_value={"status": "missing"},
        ):
            result = doctor.check_stopper_host_bridge()

        self.assertEqual(result.status, "FAIL")
        self.assertIn("missing", result.detail)
        self.assertIn("installer.stopper_launch_agent install", result.fix)

    def test_journal_only_is_reported_as_partial_not_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "decision-engine"
            journal = root / ".runtime" / "update-journal.json"
            journal.parent.mkdir(parents=True)
            journal.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                doctor.config, "managed_component_root", return_value=root
            ):
                result = doctor.check_managed_update()
        self.assertEqual(result.status, "FAIL")
        self.assertIn("partially", result.detail)

    def test_managed_update_reports_installed_target_and_running(self):
        state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=2,
            last_release_commit="1" * 40,
            last_manifest_sha256="2" * 64,
            last_version="0.2.0",
            source="github",
            last_attempt_at="2026-07-22T00:00:00Z",
            last_result="updated",
            previous_commit="0" * 40,
            target_commit="1" * 40,
            running_commit="1" * 40,
            running_version="0.2.0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for relative in (
                ".managed-install.json",
                updater.UPDATE_STATE_RELATIVE_PATH,
                Path(".runtime") / "update-protocol.json",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(doctor.config, "managed_component_root", return_value=root),
                mock.patch.object(updater, "_read_update_state", return_value=state),
                mock.patch(
                    "installer.update_transaction._require_protocol_ready",
                    return_value=None,
                ),
                mock.patch(
                    "installer.release_acquisition.load_trusted_release_keys",
                    return_value={"production": mock.sentinel.key},
                ),
            ):
                result = doctor.check_managed_update()
        self.assertEqual(result.status, "PASS")
        self.assertIn("installed=0.2.0@111111111111", result.detail)
        self.assertIn("running=111111111111", result.detail)

    def test_managed_update_reports_retry_pending_as_recoverable(self):
        state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=2,
            last_release_commit="1" * 40,
            last_manifest_sha256="2" * 64,
            last_version="0.2.0",
            source="github",
            last_attempt_at="2026-07-28T00:00:00Z",
            last_result="retry_pending",
            previous_commit="1" * 40,
            target_commit="3" * 40,
            running_commit="1" * 40,
            running_version="0.2.0",
            error_code="GitMutationError",
            transaction_id="a" * 32,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for relative in (
                ".managed-install.json",
                updater.UPDATE_STATE_RELATIVE_PATH,
                Path(".runtime") / "update-protocol.json",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(doctor.config, "managed_component_root", return_value=root),
                mock.patch.object(updater, "_read_update_state", return_value=state),
                mock.patch(
                    "installer.update_transaction._require_protocol_ready",
                    return_value=None,
                ),
                mock.patch(
                    "installer.release_acquisition.load_trusted_release_keys",
                    return_value={"production": mock.sentinel.key},
                ),
            ):
                result = doctor.check_managed_update()

        self.assertEqual(result.status, "WARN")
        self.assertIn("result=retry_pending", result.detail)
        self.assertIn("next agent restart", result.fix)

    def test_dangling_control_link_is_corrupt_not_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            marker = root / ".managed-install.json"
            try:
                marker.symlink_to(root / "missing-target")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            with mock.patch.object(
                doctor.config, "managed_component_root", return_value=root
            ):
                result = doctor.check_managed_update()
        self.assertEqual(result.status, "FAIL")
        self.assertIn("partially", result.detail)
    def test_python_passes_on_current_interpreter(self):
        r = doctor.check_python()
        self.assertEqual(r.status, "PASS")  # tests require 3.12+ to run at all

    def test_libreoffice_absent_is_warn_not_fail(self):
        with mock.patch.object(doctor.office, "find_libreoffice", return_value=None):
            r = doctor.check_libreoffice()
        self.assertEqual(r.status, "WARN")
        self.assertTrue(r.fix)

    def test_libreoffice_present_passes(self):
        with mock.patch.object(doctor.office, "find_libreoffice", return_value="/usr/bin/soffice"):
            r = doctor.check_libreoffice()
        self.assertEqual(r.status, "PASS")

    def test_skills_missing_is_fail(self):
        # Skills are the core payload, not optional — a failed link is a broken install.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(doctor.config, "claude_skills_dir", return_value=base / "claude"), \
                 mock.patch.object(doctor.config, "codex_skills_dir", return_value=base / "codex"):
                r = doctor.check_skills()
        self.assertEqual(r.status, "FAIL")

    def test_skills_missing_from_codex_is_fail_even_when_claude_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            claude = base / "claude"
            codex = base / "codex"
            for skill in doctor.DE_SKILLS:
                (claude / skill).mkdir(parents=True)
            codex.mkdir()
            routes = {
                "claude": (claude, frozenset()),
                "codex": (codex, frozenset()),
            }
            with mock.patch.object(
                mcp_config, "active_skill_routes", return_value=routes
            ):
                result = doctor.check_skills()

        self.assertEqual(result.status, "FAIL")
        self.assertIn("codex", result.detail.lower())
        self.assertIn("restart Codex", result.fix)
        self.assertIn("legacy", result.fix.lower())
        self.assertIn("install.sh", result.fix)

    def test_skills_pass_when_both_client_directories_are_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            claude = base / "claude"
            codex = base / "codex"
            for directory in (claude, codex):
                for skill in doctor.DE_SKILLS:
                    (directory / skill).mkdir(parents=True)
            routes = {
                "claude": (claude, frozenset()),
                "codex": (codex, frozenset()),
            }
            with mock.patch.object(
                mcp_config, "active_skill_routes", return_value=routes
            ):
                result = doctor.check_skills()

        self.assertEqual(result.status, "PASS")
        self.assertIn("claude=", result.detail)
        self.assertIn("codex=", result.detail)

    def test_cursor_skills_are_checked_only_after_explicit_mcp_wiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            claude = base / "claude"
            for skill in doctor.DE_SKILLS:
                (claude / skill).mkdir(parents=True)
            with mock.patch.object(
                mcp_config,
                "active_skill_routes",
                return_value={"claude": (claude, frozenset())},
            ) as routes:
                result = doctor.check_skills()

        self.assertEqual(result.status, "PASS")
        self.assertNotIn("cursor=", result.detail)
        routes.assert_called_once_with(for_doctor=True)

    def test_configured_cursor_checks_only_supported_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            claude = base / "claude"
            cursor = base / "cursor"
            managed = base / "managed"
            excluded = frozenset({"discussion-board", "graphic-explanation"})
            for skill in doctor.DE_SKILLS:
                (claude / skill).mkdir(parents=True)
                if skill not in excluded:
                    source = managed / "skills" / skill
                    source.mkdir(parents=True)
                    cursor.mkdir(parents=True, exist_ok=True)
                    install._create_skill_route(cursor / skill, source)
            with (
                mock.patch.dict(
                    os.environ,
                    {"DE_CONFIG_PATH": str(managed / "config.json")},
                ),
                mock.patch.object(
                    mcp_config,
                    "active_skill_routes",
                    return_value={
                        "claude": (claude, frozenset()),
                        "cursor": (cursor, excluded),
                    },
                ),
            ):
                result = doctor.check_skills()

        self.assertEqual(result.status, "PASS")
        self.assertIn("claude=10", result.detail)
        self.assertIn("cursor=8", result.detail)

    def test_configured_cursor_rejects_user_owned_skill_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cursor = base / "cursor"
            managed = base / "managed"
            excluded = frozenset({"discussion-board", "graphic-explanation"})
            for skill in doctor.DE_SKILLS:
                if skill not in excluded:
                    (cursor / skill).mkdir(parents=True)
            with (
                mock.patch.dict(
                    os.environ,
                    {"DE_CONFIG_PATH": str(managed / "config.json")},
                ),
                mock.patch.object(
                    mcp_config,
                    "active_skill_routes",
                    return_value={"cursor": (cursor, excluded)},
                ),
            ):
                result = doctor.check_skills()

        self.assertEqual(result.status, "FAIL")
        self.assertIn("cursor", result.detail)

    def test_popup_probe_exception_is_warn_not_fail(self):
        # An optional-dep probe that RAISES must degrade to WARN, never escalate to FAIL.
        from client.popup import backend as real_backend
        with mock.patch.object(real_backend, "inspect_webview", side_effect=RuntimeError("probe blew up")):
            r = doctor.check_popup_backend()
        self.assertEqual(r.status, "WARN")

    def test_popup_reports_a_gui_less_session_without_blaming_the_package(self):
        # In an agent sandbox / remote shell the readiness probe is never run (running it
        # would abort the child with a macOS crash dialog), so Doctor must say the session
        # cannot answer — not that pywebview is missing or broken.
        from client.popup import backend as popup_backend

        inspection = popup_backend.WebviewResult(popup_backend.WebviewState.NO_GUI_SESSION)
        with mock.patch.object(popup_backend, "inspect_webview", return_value=inspection):
            result = doctor.check_popup_backend()

        self.assertEqual(result.status, "WARN")
        self.assertIn("GUI session", result.detail)
        self.assertNotIn("missing", result.detail)
        self.assertIn("Terminal", result.fix)

    def test_popup_distinguishes_installed_package_with_unavailable_backend(self):
        from client.popup import backend as popup_backend

        inspection = popup_backend.WebviewResult(
            popup_backend.WebviewState.BACKEND_UNAVAILABLE
        )
        with (
            mock.patch.object(popup_backend, "inspect_webview", return_value=inspection),
            mock.patch.object(popup_backend, "webview_backend_ready", return_value=False),
        ):
            result = doctor.check_popup_backend()

        self.assertEqual(result.status, "WARN")
        self.assertIn("installed", result.detail)
        self.assertIn("backend", result.detail)
        self.assertNotIn("not installed", result.detail)

    def test_mcp_registered_passes_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            # registration_root() now fails closed with no activated managed install (Phase 3);
            # give both the fixture-writing render_entry() call below and doctor's own internal
            # expected_entry() call the same stable root, matching EntryStatusTestCase's pattern.
            with mock.patch.object(mcp_config, "registration_root", return_value=Path(tmp)):
                cc = Path(tmp) / ".claude.json"
                cc.write_text(json.dumps(mcp_config.render_entry()))
                with mock.patch.dict(os.environ, {
                    "CLAUDE_CODE_CONFIG": str(cc),
                    "CODEX_CONFIG": str(Path(tmp) / "none" / "config.toml"),
                    "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                    "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
                }):
                    r = doctor.check_mcp_registered()
        self.assertEqual(r.status, "PASS")
        self.assertIn("claude-code", r.detail)

    def test_mcp_registered_uses_registry_doctor_capability_selection(self):
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        codex_without_mcp_probe = replace(
            mcp_config.CLIENT_SPECS["codex"],
            doctor_capabilities=frozenset({"skills"}),
        )
        with (
            mock.patch.object(
                mcp_config,
                "CLIENT_SPECS",
                {
                    "cursor": cursor,
                    "codex": codex_without_mcp_probe,
                },
            ),
            mock.patch.object(
                mcp_config,
                "detect_clients",
                return_value=["cursor", "codex"],
            ),
            mock.patch.object(
                mcp_config,
                "entry_status",
                return_value="ready",
            ) as status,
        ):
            result = doctor.check_mcp_registered()

        self.assertEqual(result.status, "PASS")
        self.assertIn("cursor", result.detail)
        self.assertNotIn("codex", result.detail)
        status.assert_called_once_with("cursor")

    def test_mcp_unregistered_is_warn_with_write_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            cc = Path(tmp) / ".claude.json"
            cc.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
            with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_CONFIG": str(cc),
                "CODEX_CONFIG": str(Path(tmp) / "none" / "config.toml"),
                "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
            }), mock.patch.object(
                mcp_config,
                "clients_with_doctor_capability",
                return_value=("claude-code", "claude-desktop", "codex"),
            ):
                r = doctor.check_mcp_registered()
        self.assertEqual(r.status, "WARN")
        self.assertIn("--write", r.fix)   # actionable: points at the auto-connect command

    def test_mcp_codex_comment_is_not_a_false_pass(self):
        # audit 6c6ba7d8 gpt f4: a commented-out header must NOT read as registered.
        with tempfile.TemporaryDirectory() as tmp:
            toml = Path(tmp) / "config.toml"
            toml.write_text("# [mcp_servers.decision-engine]\n# command = \"x\"\n")
            with mock.patch.dict(os.environ, {
                "CODEX_CONFIG": str(toml),
                "CLAUDE_CODE_CONFIG": str(Path(tmp) / "none" / ".claude.json"),
                "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
            }), mock.patch.object(
                mcp_config,
                "clients_with_doctor_capability",
                return_value=("claude-code", "claude-desktop", "codex"),
            ):
                r = doctor.check_mcp_registered()
        self.assertEqual(r.status, "WARN")   # commented block is not a live registration

    def test_mcp_stale_codex_direct_shim_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            toml = Path(tmp) / "config.toml"
            toml.write_text(
                "[mcp_servers.decision-engine]\n"
                'command = "python3"\n'
                'args = ["-m", "installer.shim"]\n',
                encoding="utf-8",
            )
            with (
                mock.patch.object(mcp_config, "registration_root", return_value=Path(tmp)),
                mock.patch.dict(os.environ, {
                    "CODEX_CONFIG": str(toml),
                    "CLAUDE_CODE_CONFIG": str(Path(tmp) / "none" / ".claude.json"),
                    "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                    "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
                }),
            ):
                r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "WARN")
        self.assertIn("stale", r.detail)
        self.assertIn("codex", r.detail)

    def test_mcp_generated_codex_launcher_entry_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            toml = Path(tmp) / "config.toml"
            toml.write_text(
                mcp_config.render_codex_toml(cwd=Path(tmp)),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, {
                    "CODEX_CONFIG": str(toml),
                    "CLAUDE_CODE_CONFIG": str(Path(tmp) / "none" / ".claude.json"),
                    "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                    "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
                }),
                mock.patch.object(mcp_config, "registration_root", return_value=Path(tmp)),
            ):
                r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "PASS")
        self.assertIn("codex", r.detail)

    def _install_routing_block(self, tmp: Path, body: str = "legacy DE prose") -> Path:
        home = tmp / ".codex"
        home.mkdir(exist_ok=True)
        agents = home / "AGENTS.md"
        text = "# my own codex rules\n\n%s\n%s\n%s\n" % (
            codex_routing._BEGIN, body.strip("\n"), codex_routing._END)
        agents.write_text(text, encoding="utf-8")
        return agents

    def test_codex_legacy_routing_block_is_reported_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = self._install_routing_block(Path(tmp))
            before = agents.read_bytes()
            with mock.patch.dict(os.environ, {"CODEX_AGENTS_MD": str(agents)}):
                r = doctor.check_codex_legacy_routing()
            after = agents.read_bytes()

        self.assertEqual(r.status, "WARN")
        self.assertIn("legacy", r.detail)
        self.assertIn("python3 -m installer.codex_routing", r.fix)
        self.assertEqual(after, before)

    def test_codex_legacy_routing_absence_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / ".codex" / "AGENTS.md"
            agents.parent.mkdir()
            agents.write_text("# my own codex rules\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_AGENTS_MD": str(agents)}):
                r = doctor.check_codex_legacy_routing()

        self.assertEqual(r.status, "PASS")
        self.assertIn("legacy routing absent", r.detail)

    def test_unmarked_legacy_copy_warns_without_claiming_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / ".codex" / "AGENTS.md"
            agents.parent.mkdir()
            agents.write_text(
                "\n".join(codex_routing._LEGACY_UNMARKED_SIGNATURES),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CODEX_AGENTS_MD": str(agents)}):
                r = doctor.check_codex_legacy_routing()

        self.assertEqual(r.status, "WARN")
        self.assertIn("unmarked legacy", r.detail)
        self.assertIn("by hand", r.fix)
        self.assertIn("project AGENTS.md", r.fix)

    def test_codex_malformed_legacy_routing_is_reported_for_manual_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / ".codex" / "AGENTS.md"
            agents.parent.mkdir()
            agents.write_text(codex_routing._BEGIN + "\nlegacy\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_AGENTS_MD": str(agents)}):
                r = doctor.check_codex_legacy_routing()

        self.assertEqual(r.status, "WARN")
        self.assertIn("malformed", r.detail)
        self.assertIn("by hand", r.fix)

    def test_codex_legacy_routing_absent_codex_is_not_in_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nowhere" / "AGENTS.md"
            with mock.patch.dict(os.environ, {"CODEX_AGENTS_MD": str(missing)}):
                r = doctor.check_codex_legacy_routing()

        self.assertEqual(r.status, "PASS")
        self.assertIn("not in use", r.detail)

    def _codex_home(self, tmp: Path, *, skills: bool) -> dict:
        """A Codex host under ``tmp`` with the requested Decision Engine skill payload, plus the env
        pinning every Codex path (config and skills) inside it so no probe can reach
        this machine's real ~/.codex."""
        home = tmp / ".codex"
        home.mkdir(exist_ok=True)
        (home / "skills").mkdir(exist_ok=True)
        payload = tmp / "managed" / "skills"
        (payload / "audit").mkdir(parents=True, exist_ok=True)
        if skills:
            (home / "skills" / "audit").mkdir(exist_ok=True)
        return {
            "CODEX_CONFIG": str(home / "config.toml"),
            "CODEX_SKILLS_DIR": str(home / "skills"),
            "DE_CONFIG_PATH": str(tmp / "managed" / "config.json"),
            # Claude Code and Cursor carry skill probes of their own, and this fixture creates a
            # skills payload for them to intersect with — pin their directories inside the temp
            # tree too, so no case here can read the developer's real ~/.claude or ~/.cursor.
            "CLAUDE_SKILLS_DIR": str(tmp / "no-claude" / "skills"),
            "CURSOR_SKILLS_DIR": str(tmp / "no-cursor" / "skills"),
        }

    def test_mcp_ready_client_with_absent_unonboarded_sibling_stays_pass(self):
        # A host that is merely PRESENT (no DE skills and no probe registered for
        # it) is a note, not a defect — the user may simply not want DE wired there.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mcp_config, "registration_root", return_value=Path(tmp)):
                cc = Path(tmp) / ".claude.json"
                cc.write_text(json.dumps(mcp_config.render_entry()), encoding="utf-8")
                desktop = Path(tmp) / "desktop" / "claude_desktop_config.json"
                desktop.parent.mkdir()
                with mock.patch.dict(os.environ, {
                    "CLAUDE_CODE_CONFIG": str(cc),
                    "CLAUDE_DESKTOP_CONFIG": str(desktop),
                    "CODEX_CONFIG": str(Path(tmp) / "none" / "config.toml"),
                    "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
                }):
                    r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "PASS")
        self.assertIn("not wired: claude-desktop", r.detail)

    def test_mcp_absent_codex_with_de_payload_is_not_masked_by_a_ready_sibling(self):
        # The shipped symptom: ~/.codex carries DE skills, so Codex is
        # told to call mcp__decision-engine__* tools its own config never registered. A ready
        # claude-code must not turn that into an overall PASS.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(mcp_config, "registration_root", return_value=tmp):
                cc = tmp / ".claude.json"
                cc.write_text(json.dumps(mcp_config.render_entry()), encoding="utf-8")
                desktop = tmp / "desktop" / "claude_desktop_config.json"
                desktop.parent.mkdir()
                desktop.write_text(json.dumps(mcp_config.render_entry()), encoding="utf-8")
                env = self._codex_home(tmp, skills=True)
                env.update({
                    "CLAUDE_CODE_CONFIG": str(cc),
                    "CLAUDE_DESKTOP_CONFIG": str(desktop),
                    "CURSOR_CONFIG": str(tmp / "none" / "cursor.json"),
                })
                with mock.patch.dict(os.environ, env):
                    r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "WARN")
        self.assertIn("ready: claude-code", r.detail)
        self.assertIn("codex skills installed but MCP not wired", r.detail)
        self.assertNotIn("not wired: codex", r.detail)   # not a passive note any more
        self.assertIn("python3 -m installer.mcp_config --write --client codex", r.fix)
        self.assertIn("RESTART", r.fix)

    def test_mcp_absent_codex_without_de_payload_does_not_claim_installed_content(self):
        # A Codex that exists but carries no DE payload is still reported — its MCP entry is
        # missing — but the detail may not claim DE skills that are not there.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(mcp_config, "registration_root", return_value=tmp):
                cc = tmp / ".claude.json"
                cc.write_text(json.dumps(mcp_config.render_entry()), encoding="utf-8")
                env = self._codex_home(tmp, skills=False)
                env.update({
                    "CLAUDE_CODE_CONFIG": str(cc),
                    "CLAUDE_DESKTOP_CONFIG": str(tmp / "none" / "desktop.json"),
                    "CURSOR_CONFIG": str(tmp / "none" / "cursor.json"),
                })
                with mock.patch.dict(os.environ, env):
                    r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "WARN")
        self.assertIn("codex in use but MCP not wired", r.detail)
        self.assertNotIn("skills installed", r.detail)

    def test_mcp_absent_entry_beside_installed_de_skills_is_reported_for_every_host(self):
        # The same contradiction Codex hit is possible for Claude Code and Cursor, whose skills
        # also live outside their MCP config: DE skills routed in, no entry registered.
        for client, config_env, skills_env, config_name in (
            ("claude-code", "CLAUDE_CODE_CONFIG", "CLAUDE_SKILLS_DIR", ".claude.json"),
            ("cursor", "CURSOR_CONFIG", "CURSOR_SKILLS_DIR", "mcp.json"),
        ):
            with self.subTest(client=client), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                with mock.patch.object(
                    mcp_config, "registration_root", return_value=tmp
                ):
                    payload = tmp / "managed" / "skills"
                    (payload / "audit").mkdir(parents=True)
                    skills = tmp / client / "skills"
                    (skills / "audit").mkdir(parents=True)
                    host_config = tmp / client / config_name
                    host_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
                    codex = tmp / ".codex"
                    codex.mkdir()
                    (codex / "config.toml").write_text(
                        mcp_config.render_codex_toml(cwd=tmp), encoding="utf-8"
                    )
                    env = {
                        config_env: str(host_config),
                        skills_env: str(skills),
                        "DE_CONFIG_PATH": str(tmp / "managed" / "config.json"),
                        "CODEX_CONFIG": str(codex / "config.toml"),
                        "CODEX_AGENTS_MD": str(codex / "AGENTS.md"),
                        "CODEX_SKILLS_DIR": str(codex / "skills"),
                        "CLAUDE_DESKTOP_CONFIG": str(tmp / "none" / "desktop.json"),
                    }
                    env.setdefault("CLAUDE_CODE_CONFIG", str(tmp / "none" / ".claude.json"))
                    env.setdefault("CURSOR_CONFIG", str(tmp / "none" / "cursor.json"))
                    env.setdefault("CLAUDE_SKILLS_DIR", str(tmp / "none" / "skills"))
                    env.setdefault("CURSOR_SKILLS_DIR", str(tmp / "none" / "skills"))
                    with mock.patch.dict(os.environ, env):
                        r = doctor.check_mcp_registered()

                self.assertEqual(r.status, "WARN")
                # No routing FILE for these hosts — the detail must say skills, not routing.
                self.assertIn("%s skills installed but MCP not wired" % client, r.detail)
                self.assertNotIn("routing", r.detail)
                self.assertIn("--client %s" % client, r.fix)

    def test_mcp_present_host_without_de_skills_stays_an_informational_note(self):
        # Cursor installed, its own skills present, none of ours, no entry — the user may
        # simply not want Decision Engine there, so this must not become a warning.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(mcp_config, "registration_root", return_value=tmp):
                payload = tmp / "managed" / "skills"
                (payload / "audit").mkdir(parents=True)
                cursor_skills = tmp / ".cursor" / "skills"
                (cursor_skills / "someone-elses-skill").mkdir(parents=True)
                (tmp / ".cursor" / "mcp.json").write_text(
                    json.dumps({"mcpServers": {}}), encoding="utf-8"
                )
                cc = tmp / ".claude.json"
                cc.write_text(json.dumps(mcp_config.render_entry()), encoding="utf-8")
                with mock.patch.dict(os.environ, {
                    "CLAUDE_CODE_CONFIG": str(cc),
                    "CLAUDE_SKILLS_DIR": str(tmp / "no-claude" / "skills"),
                    "CURSOR_CONFIG": str(tmp / ".cursor" / "mcp.json"),
                    "CURSOR_SKILLS_DIR": str(cursor_skills),
                    "DE_CONFIG_PATH": str(tmp / "managed" / "config.json"),
                    "CODEX_CONFIG": str(tmp / "none" / "config.toml"),
                    "CODEX_AGENTS_MD": str(tmp / "none" / "AGENTS.md"),
                    "CODEX_SKILLS_DIR": str(tmp / "none" / "skills"),
                    "CLAUDE_DESKTOP_CONFIG": str(tmp / "none" / "desktop.json"),
                }):
                    r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "PASS")
        self.assertIn("not wired: cursor", r.detail)

    def test_mcp_stays_pass_on_a_machine_without_codex(self):
        # No ~/.codex, no routing, no Codex skills — nothing to demand, no noise.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(mcp_config, "registration_root", return_value=tmp):
                cc = tmp / ".claude.json"
                cc.write_text(json.dumps(mcp_config.render_entry()), encoding="utf-8")
                with mock.patch.dict(os.environ, {
                    "CLAUDE_CODE_CONFIG": str(cc),
                    "CODEX_CONFIG": str(tmp / "none" / "config.toml"),
                    "CODEX_AGENTS_MD": str(tmp / "none" / "AGENTS.md"),
                    "CODEX_SKILLS_DIR": str(tmp / "none" / "skills"),
                    "CLAUDE_DESKTOP_CONFIG": str(tmp / "none" / "desktop.json"),
                    "CURSOR_CONFIG": str(tmp / "none" / "cursor.json"),
                }):
                    r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "PASS")
        self.assertNotIn("codex", r.detail)

    def test_mcp_mixed_ready_and_stale_reports_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mcp_config, "registration_root", return_value=Path(tmp)):
                cc = Path(tmp) / ".claude.json"
                cc.write_text(json.dumps(mcp_config.render_entry()), encoding="utf-8")
                codex = Path(tmp) / "config.toml"
                codex.write_text(
                    "[mcp_servers.decision-engine]\n"
                    'command = "python3"\n'
                    'args = ["-m", "installer.shim"]\n',
                    encoding="utf-8",
                )
                with mock.patch.dict(os.environ, {
                    "CLAUDE_CODE_CONFIG": str(cc),
                    "CODEX_CONFIG": str(codex),
                    "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "desktop.json"),
                    "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
                }):
                    r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "WARN")
        self.assertIn("ready: claude-code", r.detail)
        self.assertIn("repair: codex=stale", r.detail)

    def test_mcp_unreadable_config_is_redacted(self):
        secret_body = "private-config-body"
        with tempfile.TemporaryDirectory() as tmp:
            cc = Path(tmp) / ".claude.json"
            cc.write_text("{" + secret_body, encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_CONFIG": str(cc),
                "CODEX_CONFIG": str(Path(tmp) / "none" / "config.toml"),
                "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "desktop.json"),
                "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
            }):
                r = doctor.check_mcp_registered()

        self.assertEqual(r.status, "WARN")
        self.assertIn("claude-code=unreadable", r.detail)
        self.assertNotIn(secret_body, r.detail)

    def test_dev_mode_passes_when_no_client_is_in_dev_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mcp_config, "registration_root", return_value=Path(tmp)):
                cc = Path(tmp) / ".claude.json"
                cc.write_text(json.dumps(mcp_config.render_entry()), encoding="utf-8")
                with mock.patch.dict(os.environ, {
                    "CLAUDE_CODE_CONFIG": str(cc),
                    "CODEX_CONFIG": str(Path(tmp) / "none" / "config.toml"),
                    "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                    "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
                }), mock.patch.object(
                    mcp_config,
                    "detect_clients",
                    return_value=["claude-code", "claude-desktop", "codex"],
                ):
                    r = doctor.check_dev_mode()
        self.assertEqual(r.status, "PASS")

    def test_dev_mode_warns_and_names_the_client_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev_root = Path(tmp) / "checkout"
            dev_root.mkdir()
            cc = Path(tmp) / ".claude.json"
            cc.write_text(json.dumps(mcp_config.render_entry(dev_root=dev_root)), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_CONFIG": str(cc),
                "CODEX_CONFIG": str(Path(tmp) / "none" / "config.toml"),
                "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
            }):
                r = doctor.check_dev_mode()
        self.assertEqual(r.status, "WARN")
        self.assertIn("claude-code", r.detail)
        self.assertIn("DEVELOPER MODE", r.detail)

    def test_python_wiring_passes_for_the_current_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev_root = Path(tmp) / "checkout"
            dev_root.mkdir()
            cc = Path(tmp) / ".claude.json"
            cc.write_text(
                json.dumps(mcp_config.render_entry(dev_root=dev_root, python=sys.executable)),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_CONFIG": str(cc),
                "CODEX_CONFIG": str(Path(tmp) / "none" / "config.toml"),
                "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
            }):
                r = doctor.check_python_wiring()
        self.assertEqual(r.status, "PASS")

    def test_python_wiring_warns_when_recorded_interpreter_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev_root = Path(tmp) / "checkout"
            dev_root.mkdir()
            missing_python = str(Path(tmp) / "no-such-python3")
            cc = Path(tmp) / ".claude.json"
            cc.write_text(
                json.dumps(mcp_config.render_entry(dev_root=dev_root, python=missing_python)),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_CONFIG": str(cc),
                "CODEX_CONFIG": str(Path(tmp) / "none" / "config.toml"),
                "CLAUDE_DESKTOP_CONFIG": str(Path(tmp) / "none" / "cd.json"),
                "CURSOR_CONFIG": str(Path(tmp) / "none" / "cursor.json"),
            }):
                r = doctor.check_python_wiring()
        self.assertEqual(r.status, "WARN")
        self.assertIn("claude-code", r.detail)
        self.assertIn("dev-root", r.fix)   # this entry is a dev-root one — repair hint must match

    def test_activation_uses_real_keys_and_never_leaks_values(self):
        # Real activation signal = access_token + device_id (installer.activate). Values
        # assembled at run time so they can't self-flag the leak scanner reading this source.
        token = "tok-" + "fake" + "-value"
        host = "de-" + "example" + ".invalid"
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"access_token": token, "device_id": "dev1",
                                       "server_endpoint": "https://" + host}))
            with mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(cfg)}):
                r = doctor.check_activation()
        self.assertEqual(r.status, "PASS")
        self.assertNotIn(token, r.detail)   # never leak the token
        self.assertNotIn(host, r.detail)    # never leak the endpoint

    def test_activation_non_dict_config_is_warn_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps(["not", "a", "dict"]))
            with mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(cfg)}):
                r = doctor.check_activation()
        self.assertEqual(r.status, "WARN")
        self.assertIn("installer.permanent_setup", r.fix)

    def test_activation_corrupt_config_does_not_claim_permanent_setup_will_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text("{not-json", encoding="utf-8")
            with mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(cfg)}):
                r = doctor.check_activation()
        self.assertEqual(r.status, "WARN")
        self.assertIn("managed repair/support", r.fix)
        self.assertNotIn("permanent_setup", r.fix)

    def test_activation_recovery_marker_blocks_retry_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text("{}\n", encoding="utf-8")
            marker = cfg.parent / doctor.config.ACTIVATION_RECOVERY_RELATIVE_PATH
            marker.parent.mkdir(parents=True)
            marker.write_text('{"schema": 1}\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(cfg)}):
                result = doctor.check_activation()

        self.assertEqual(result.status, "WARN")
        self.assertIn("recovery required", result.detail)
        self.assertIn("do not rerun", result.fix)
        self.assertNotIn("permanent_setup", result.fix)

    @unittest.skipUnless(shutil.which("git"), "Git is required for managed-install diagnostics")
    def test_worktree_check_passes_when_only_ignored_runtime_files_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)
            source_ignore = Path(__file__).resolve().parents[2] / ".gitignore"
            (root / ".gitignore").write_text(source_ignore.read_text(encoding="utf-8"), encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True, timeout=10)
            subprocess.run(
                [
                    "git", "-C", str(root), "-c", "user.name=Doctor Test",
                    "-c", "user.email=doctor@example.invalid", "commit", "-qm", "baseline",
                ],
                check=True,
                timeout=10,
            )
            (root / "active-run.json").write_text("{}", encoding="utf-8")
            (root / ".runtime").mkdir()
            (root / ".runtime" / "active-runs.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "PASS")

    @unittest.skipUnless(shutil.which("git"), "Git is required for managed-install diagnostics")
    def test_worktree_check_names_unknown_dirt_without_reading_its_contents(self):
        secret_body = "private-body-must-not-appear"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)
            (root / "unexpected.txt").write_text(secret_body, encoding="utf-8")
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertIn("unexpected.txt", r.detail)
        self.assertNotIn(secret_body, r.detail)

    @unittest.skipUnless(shutil.which("git"), "Git is required for managed-install diagnostics")
    def test_worktree_check_fails_for_tracked_changes_in_a_managed_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._build_worktree(root)
            managed_install.marker_path(root).write_text("{}\n", encoding="utf-8")
            (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(
                     os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False,
                 ):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "FAIL")
        self.assertIn("MCP launcher", r.fix)

    @unittest.skipUnless(shutil.which("git"), "Git is required for managed-install diagnostics")
    def test_worktree_check_warns_for_only_untracked_files_in_a_managed_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._build_worktree(root)
            managed_install.marker_path(root).write_text("{}\n", encoding="utf-8")
            (root / "notes.txt").write_text("local note\n", encoding="utf-8")
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(
                     os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False,
                 ):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")

    @unittest.skipUnless(shutil.which("git"), "Git is required for managed-install diagnostics")
    def test_worktree_check_warns_for_tracked_changes_in_a_developer_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._build_worktree(root)
            (root / "tracked.txt").write_text("developer change\n", encoding="utf-8")
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(
                     os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False,
                 ):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")

    def test_worktree_check_finds_managed_tracked_changes_after_truncated_untracked_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            managed_install.marker_path(root).write_text("{}\n", encoding="utf-8")

            def fake_run(argv, **_kwargs):
                if "rev-parse" in argv:
                    return subprocess.CompletedProcess(argv, 0), False, str(root) + "\n"
                if "config" in argv:
                    return subprocess.CompletedProcess(argv, 1), False, ""
                if "--untracked-files=normal" in argv:
                    return subprocess.CompletedProcess(argv, 0), True, "?? early-note.txt\n"
                if "--untracked-files=no" in argv:
                    return subprocess.CompletedProcess(argv, 0), False, " M tracked.txt\n"
                raise AssertionError("unexpected git command: %r" % argv)

            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(
                     os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False,
                 ), \
                 mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/git"), \
                 mock.patch.object(doctor, "_run_bounded_process", side_effect=fake_run):
                r = doctor.check_worktree()

        self.assertEqual(r.status, "FAIL")
        self.assertIn("tracked.txt", r.detail)
        self.assertNotIn("early-note.txt", r.detail)

    def test_worktree_check_warns_when_git_cannot_read_the_checkout(self):
        failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="broken")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False), \
                 mock.patch.object(doctor.shutil, "which", return_value="C:/trusted/git.exe"), \
                 mock.patch.object(doctor, "_run_bounded_process", return_value=(failed, False, "")):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertIn("readable Git checkout", r.detail)

    def test_worktree_check_warns_when_status_fails_after_identity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_run(argv, **_kwargs):
                if "rev-parse" in argv:
                    return subprocess.CompletedProcess(argv, 0), False, str(root) + "\n"
                if "config" in argv:
                    return subprocess.CompletedProcess(argv, 1), False, ""
                return subprocess.CompletedProcess(argv, 128), False, ""

            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False), \
                 mock.patch.object(doctor.shutil, "which", return_value="C:/trusted/git.exe"), \
                 mock.patch.object(doctor, "_run_bounded_process", side_effect=fake_run):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertIn("readable Git checkout", r.detail)

    def test_worktree_check_scrubs_git_environment_and_terminal_controls(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0), False, str(root) + "\n"
            if "config" in argv:
                return subprocess.CompletedProcess(argv, 1), False, ""
            else:
                return subprocess.CompletedProcess(argv, 0), False, '?? bad\x1b[31m-name\n'

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(
                     os.environ,
                     {"DE_CONFIG_PATH": str(root / "config.json"), "GIT_INDEX_FILE": "C:/evil/index"},
                     clear=False,
                 ), mock.patch.object(doctor.shutil, "which", return_value="C:/trusted/git.exe"), \
                 mock.patch.object(doctor, "_run_bounded_process", side_effect=fake_run):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertNotIn("GIT_INDEX_FILE", captured["env"])
        self.assertIn("core.fsmonitor=false", captured["argv"])
        self.assertNotIn("\x1b", r.detail)
        self.assertIn("?", r.detail)

    @unittest.skipUnless(shutil.which("git"), "Git is required for managed-install diagnostics")
    def test_worktree_check_refuses_execution_capable_local_git_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "filter-ran.txt"
            subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)
            subprocess.run(
                ["git", "-C", str(root), "config", "filter.hostile.clean", "hostile-command"],
                check=True,
                timeout=10,
            )
            (root / ".gitattributes").write_text("*.txt filter=hostile\n", encoding="utf-8")
            (root / "payload.txt").write_text("payload", encoding="utf-8")
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertIn("execution-capable", r.detail)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which("git"), "Git is required for managed-install diagnostics")
    def test_worktree_check_refuses_checkout_local_config_includes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)
            subprocess.run(
                ["git", "-C", str(root), "config", "include.path", str(root / "extra.cfg")],
                check=True,
                timeout=10,
            )
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertIn("execution-capable", r.detail)

    def test_bounded_process_terminates_after_output_limit(self):
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 200000); sys.stdout.flush()",
        ]
        completed, truncated, output = doctor._run_bounded_process(
            command,
            env=os.environ.copy(),
            timeout=10,
            output_limit=4096,
        )
        self.assertTrue(truncated)
        self.assertLessEqual(len(output.encode("utf-8")), 4096)
        self.assertIsInstance(completed.returncode, int)

    @unittest.skipUnless(shutil.which("git"), "Git is required for managed-install diagnostics")
    def test_worktree_check_rejects_an_enclosing_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            subprocess.run(["git", "init", "-q", str(outer)], check=True, timeout=10)
            install = outer / "nested" / "decision-engine"
            install.mkdir(parents=True)
            (outer / "private-outer-file.txt").write_text("not install state", encoding="utf-8")
            with mock.patch.object(doctor.config, "component_root", return_value=install), \
                 mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(install / "config.json")}, clear=False):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertIn("not itself", r.detail)
        self.assertNotIn("private-outer-file", r.detail)

    def test_worktree_check_reports_a_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(doctor.config, "component_root", return_value=root), \
                 mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(root / "config.json")}, clear=False), \
                 mock.patch.object(doctor.shutil, "which", return_value="C:/trusted/git.exe"), \
                 mock.patch.object(
                     doctor,
                     "_run_bounded_process",
                     side_effect=subprocess.TimeoutExpired(["git", "status"], 10),
                 ):
                r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertIn("timed out", r.detail)

    def test_worktree_check_rejects_a_config_root_mismatch_before_git(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(doctor.config, "component_root", return_value=Path(tmp) / "managed"), \
             mock.patch.dict(
                 os.environ,
                 {"DE_CONFIG_PATH": str(Path(tmp) / "elsewhere" / "config.json")},
                 clear=False,
             ), mock.patch.object(doctor.subprocess, "run") as run:
            r = doctor.check_worktree()
        self.assertEqual(r.status, "WARN")
        self.assertIn("differ", r.detail)
        run.assert_not_called()


class CursorDoctorStatusTests(unittest.TestCase):
    def test_status_vocabulary_matches_the_master_plan(self):
        self.assertEqual(
            doctor.CURSOR_DOCTOR_STATES,
            (
                "not_selected",
                "configured_healthy",
                "configured_broken",
                "project_shadow_detected",
                "same_name_unowned",
                "skills_missing",
                "skills_user_modified",
                "version_skew_reload_required",
                "server_dependency_blocked",
                "local_ui_disabled",
                "unsupported_cursor_version",
                "cursor_version_unknown",
                "host_identity_conflict",
                "same_name_user_modified",
            ),
        )

    def test_unselected_and_healthy_states_are_mutually_exclusive(self):
        self.assertEqual(
            doctor.aggregate_cursor_doctor_states(
                doctor.CursorDoctorEvidence(selected=False)
            ),
            ("not_selected",),
        )
        self.assertEqual(
            doctor.aggregate_cursor_doctor_states(
                doctor.CursorDoctorEvidence(
                    selected=True,
                    mcp_status="ready",
                    skills_status="healthy",
                    version_status="candidate_standard_version_floor_met",
                )
            ),
            ("configured_healthy",),
        )

    def test_all_master_states_are_reachable_from_normalized_evidence(self):
        scenarios = (
            doctor.CursorDoctorEvidence(selected=False),
            doctor.CursorDoctorEvidence(
                selected=True,
                mcp_status="ready",
                skills_status="healthy",
                version_status="candidate_standard_version_floor_met",
            ),
            doctor.CursorDoctorEvidence(
                selected=True,
                mcp_status="stale",
                skills_status="missing",
                project_shadow=True,
                version_status="unsupported_cursor_version",
                release_state="reload_required",
                version_skew=True,
                server_dependency_blocked=True,
                local_ui_enabled=False,
                identity_status="conflicting",
            ),
            doctor.CursorDoctorEvidence(
                selected=True,
                mcp_status="same_name_unowned",
                skills_status="user_modified",
                version_status="cursor_version_unknown",
            ),
            doctor.CursorDoctorEvidence(
                selected=True,
                mcp_status="same_name_user_modified",
                skills_status="healthy",
            ),
        )

        observed = {
            state
            for evidence in scenarios
            for state in doctor.aggregate_cursor_doctor_states(evidence)
        }

        self.assertEqual(observed, set(doctor.CURSOR_DOCTOR_STATES))

    def test_unknown_or_inconsistent_evidence_fails_closed(self):
        invalid = (
            doctor.CursorDoctorEvidence(
                selected=True,
                mcp_status="almost-ready",
            ),
            doctor.CursorDoctorEvidence(
                selected=True,
                identity_status="cursor-vscode",
            ),
            doctor.CursorDoctorEvidence(
                selected=False,
                mcp_status="ready",
            ),
        )

        for evidence in invalid:
            with self.subTest(evidence=evidence):
                with self.assertRaises(ValueError):
                    doctor.aggregate_cursor_doctor_states(evidence)

    def test_machine_collector_short_circuits_an_unselected_cursor(self):
        with (
            mock.patch.object(
                mcp_config,
                "entry_status",
                return_value="absent",
            ),
            mock.patch.object(
                doctor,
                "_cursor_owned_skills_status",
                return_value="not_checked",
            ) as skills,
            mock.patch.object(
                doctor.cursor_version,
                "collect_cursor_version_evidence",
            ) as version,
        ):
            evidence = doctor.collect_cursor_doctor_evidence()

        self.assertEqual(
            evidence,
            doctor.CursorDoctorEvidence(selected=False),
        )
        skills.assert_called_once_with()
        version.assert_not_called()

    def test_machine_collector_connects_protected_running_release_skew(self):
        assessment = doctor.cursor_version.CursorVersionAssessment(
            state="candidate_standard_version_floor_met",
            detail="redacted",
        )
        with (
            mock.patch.object(mcp_config, "entry_status", return_value="ready"),
            mock.patch.object(
                doctor,
                "_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                doctor.cursor_version,
                "collect_cursor_version_evidence",
                return_value=object(),
            ),
            mock.patch.object(
                doctor.cursor_version,
                "assess_cursor_version",
                return_value=assessment,
            ),
            mock.patch.object(
                doctor,
                "_cursor_managed_release_state",
                return_value="reload_required",
            ),
            mock.patch.object(
                doctor,
                "_cursor_local_ui_enabled",
                return_value=False,
            ),
        ):
            evidence = doctor.collect_cursor_doctor_evidence()

        self.assertEqual(evidence.release_state, "reload_required")
        self.assertTrue(evidence.version_skew)
        self.assertIn(
            "version_skew_reload_required",
            doctor.aggregate_cursor_doctor_states(evidence),
        )

    def test_unknown_running_release_is_observable_but_not_called_skew(self):
        assessment = doctor.cursor_version.CursorVersionAssessment(
            state="candidate_standard_version_floor_met",
            detail="redacted",
        )
        with (
            mock.patch.object(mcp_config, "entry_status", return_value="ready"),
            mock.patch.object(
                doctor,
                "_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                doctor.cursor_version,
                "collect_cursor_version_evidence",
                return_value=object(),
            ),
            mock.patch.object(
                doctor.cursor_version,
                "assess_cursor_version",
                return_value=assessment,
            ),
            mock.patch.object(
                doctor,
                "_cursor_managed_release_state",
                return_value="unknown",
            ),
            mock.patch.object(
                doctor,
                "_cursor_local_ui_enabled",
                return_value=False,
            ),
        ):
            evidence = doctor.collect_cursor_doctor_evidence()

        self.assertEqual(evidence.release_state, "unknown")
        self.assertFalse(evidence.version_skew)

    def test_owned_but_missing_entry_is_broken_not_unselected(self):
        assessment = doctor.cursor_version.CursorVersionAssessment(
            state="candidate_standard_version_floor_met",
            detail="redacted",
        )
        with (
            mock.patch.object(
                mcp_config,
                "entry_status",
                return_value="absent",
            ),
            mock.patch.object(
                doctor,
                "_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                doctor.cursor_version,
                "collect_cursor_version_evidence",
                return_value=object(),
            ),
            mock.patch.object(
                doctor.cursor_version,
                "assess_cursor_version",
                return_value=assessment,
            ),
            mock.patch.object(
                doctor,
                "_cursor_local_ui_enabled",
                return_value=False,
            ),
        ):
            evidence = doctor.collect_cursor_doctor_evidence()

        self.assertEqual(evidence.mcp_status, "same_name_unowned")
        self.assertEqual(
            doctor.aggregate_cursor_doctor_states(evidence),
            (
                "configured_broken",
                "same_name_unowned",
                "local_ui_disabled",
            ),
        )

    def test_machine_collector_uses_only_normalized_probe_results(self):
        version_assessment = doctor.cursor_version.CursorVersionAssessment(
            state="cursor_version_unknown",
            detail="private detail that must not enter aggregate evidence",
        )
        with (
            mock.patch.object(
                mcp_config,
                "entry_status",
                return_value="ready",
            ),
            mock.patch.object(
                doctor,
                "_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                doctor.cursor_version,
                "collect_cursor_version_evidence",
                return_value=object(),
            ),
            mock.patch.object(
                doctor.cursor_version,
                "assess_cursor_version",
                return_value=version_assessment,
            ),
            mock.patch.object(
                doctor,
                "_cursor_local_ui_enabled",
                return_value=False,
            ),
            mock.patch.object(
                doctor,
                "_cursor_managed_release_state",
                return_value=None,
            ),
        ):
            evidence = doctor.collect_cursor_doctor_evidence(
                identity_status="conflicting",
            )

        self.assertEqual(
            evidence,
            doctor.CursorDoctorEvidence(
                selected=True,
                mcp_status="ready",
                skills_status="healthy",
                version_status="cursor_version_unknown",
                local_ui_enabled=False,
                identity_status="conflicting",
            ),
        )
        self.assertEqual(
            doctor.aggregate_cursor_doctor_states(evidence),
            (
                "configured_broken",
                "local_ui_disabled",
                "cursor_version_unknown",
                "host_identity_conflict",
            ),
        )

    def test_workspace_probe_failure_is_broken_without_private_detail(self):
        private = r"C:\private-workspace\secret"
        assessment = doctor.cursor_version.CursorVersionAssessment(
            state="candidate_standard_version_floor_met",
            detail="redacted",
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                mcp_config,
                "entry_status",
                return_value="ready",
            ),
            mock.patch.object(
                doctor,
                "_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                doctor.cursor_version,
                "collect_cursor_version_evidence",
                return_value=object(),
            ),
            mock.patch.object(
                doctor.cursor_version,
                "assess_cursor_version",
                return_value=assessment,
            ),
            mock.patch.object(
                mcp_config,
                "cursor_workspace_shadow",
                side_effect=config.ShellError(private),
            ),
            mock.patch.object(
                doctor,
                "_cursor_local_ui_enabled",
                return_value=False,
            ),
        ):
            evidence = doctor.collect_cursor_doctor_evidence(
                workspace=Path(tmp),
            )

        self.assertTrue(evidence.diagnostic_failed)
        self.assertNotIn(private, repr(evidence))
        self.assertEqual(
            doctor.aggregate_cursor_doctor_states(evidence),
            ("configured_broken", "local_ui_disabled"),
        )

    def test_workspace_skill_shadow_uses_cursor_m2_allowlist(self):
        assessment = doctor.cursor_version.CursorVersionAssessment(
            state="candidate_standard_version_floor_met",
            detail="redacted",
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                mcp_config,
                "entry_status",
                return_value="ready",
            ),
            mock.patch.object(
                doctor,
                "_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                doctor.cursor_version,
                "collect_cursor_version_evidence",
                return_value=object(),
            ),
            mock.patch.object(
                doctor.cursor_version,
                "assess_cursor_version",
                return_value=assessment,
            ),
            mock.patch.object(
                mcp_config,
                "cursor_workspace_shadow",
                return_value=None,
            ),
            mock.patch.object(
                mcp_config,
                "cursor_workspace_skill_shadow",
                return_value=None,
            ) as shadow,
            mock.patch.object(
                doctor,
                "_cursor_local_ui_enabled",
                return_value=False,
            ),
        ):
            workspace = Path(tmp)
            doctor.collect_cursor_doctor_evidence(workspace=workspace)

        shadow.assert_called_once_with(
            workspace,
            cursor_skill_payload.CURSOR_M2_SKILLS,
        )

    def test_mcp_change_during_snapshot_cannot_report_healthy(self):
        assessment = doctor.cursor_version.CursorVersionAssessment(
            state="candidate_standard_version_floor_met",
            detail="redacted",
        )
        with (
            mock.patch.object(
                mcp_config,
                "entry_status",
                side_effect=("ready", "same_name_user_modified"),
            ),
            mock.patch.object(
                doctor,
                "_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                doctor.cursor_version,
                "collect_cursor_version_evidence",
                return_value=object(),
            ),
            mock.patch.object(
                doctor.cursor_version,
                "assess_cursor_version",
                return_value=assessment,
            ),
            mock.patch.object(
                doctor,
                "_cursor_local_ui_enabled",
                return_value=False,
            ),
        ):
            evidence = doctor.collect_cursor_doctor_evidence()

        self.assertTrue(evidence.diagnostic_failed)
        self.assertEqual(evidence.mcp_status, "same_name_user_modified")
        self.assertEqual(
            doctor.aggregate_cursor_doctor_states(evidence),
            (
                "configured_broken",
                "local_ui_disabled",
                "same_name_user_modified",
            ),
        )

    def test_owned_skill_probe_requires_ownership_before_claiming_health(self):
        with (
            mock.patch.object(
                mcp_config,
                "registration_root",
                return_value=Path("managed"),
            ),
            mock.patch.object(
                client_host_ownership,
                "read_record_if_present",
                return_value=None,
            ),
        ):
            self.assertEqual(
                doctor._legacy_cursor_owned_skills_status(),
                "not_checked",
            )

    def test_owned_skill_probe_distinguishes_missing_from_cached_modification(self):
        record = mock.Mock(
            skill_release_id="1-" + "1" * 40,
            skill_manifest_sha256="a" * 64,
        )
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "active"
            state_root = root / "state"
            modified_destination = root / "modified"
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                (modified_destination / skill).mkdir(parents=True)

            def run_probe(path: Path):
                spec = replace(
                    cursor,
                    skills_path=lambda: path,
                    skills_global_path=lambda: path,
                )
                with (
                    mock.patch.dict(
                        mcp_config.CLIENT_SPECS,
                        {"cursor": spec},
                        clear=False,
                    ),
                    mock.patch.object(
                        mcp_config,
                        "registration_root",
                        return_value=root,
                    ),
                    mock.patch.object(
                        client_host_ownership,
                        "read_record_if_present",
                        return_value=record,
                    ),
                    mock.patch.object(
                        cursor_activation,
                        "cursor_activation_root",
                        return_value=state_root,
                    ),
                    mock.patch.object(
                        cursor_skill_payload,
                        "verify_cached_payload",
                        return_value=record.skill_manifest_sha256,
                    ),
                    mock.patch.object(
                        cursor_skill_payload,
                        "verify_skill_copy",
                        side_effect=cursor_skill_payload.CursorSkillPayloadError(
                            "changed"
                        ),
                    ) as verify,
                ):
                    result = doctor._legacy_cursor_owned_skills_status()
                return result, verify.call_count

            missing, missing_verifies = run_probe(destination)
            modified, modified_verifies = run_probe(modified_destination)

        self.assertEqual((missing, missing_verifies), ("missing", 0))
        self.assertEqual(modified, "user_modified")
        self.assertEqual(modified_verifies, 1)

    def test_owned_skill_probe_does_not_label_transient_read_failure_as_modified(self):
        record = mock.Mock(
            skill_release_id="1-" + "5" * 40,
            skill_manifest_sha256="f" * 64,
        )
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "active"
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                (destination / skill).mkdir(parents=True)
            spec = replace(
                cursor,
                skills_path=lambda: destination,
                skills_global_path=lambda: destination,
            )
            with (
                mock.patch.dict(
                    mcp_config.CLIENT_SPECS,
                    {"cursor": spec},
                    clear=False,
                ),
                mock.patch.object(
                    mcp_config,
                    "registration_root",
                    return_value=root,
                ),
                mock.patch.object(
                    client_host_ownership,
                    "read_record_if_present",
                    return_value=record,
                ),
                mock.patch.object(
                    cursor_activation,
                    "cursor_activation_root",
                    return_value=root / "state",
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "verify_cached_payload",
                    return_value=record.skill_manifest_sha256,
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "verify_skill_copy",
                    side_effect=(
                        cursor_skill_payload.CursorSkillPayloadUnverifiableError(
                            "changed while being read"
                        )
                    ),
                ),
            ):
                result = doctor._legacy_cursor_owned_skills_status()

        self.assertEqual(result, "unverifiable")

    def test_owned_skill_probe_rejects_changed_ownership_generation(self):
        record = mock.Mock(
            skill_release_id="1-" + "6" * 40,
            skill_manifest_sha256="1" * 64,
        )
        changed = replace(
            client_host_ownership.OwnershipRecord(
                install_id="11111111-1111-1111-1111-111111111111",
                client="cursor",
                config_path=Path("C:/cursor/mcp.json"),
                server_name=mcp_config.DEFAULT_SERVER_NAME,
                managed_fields_sha256="2" * 64,
                skill_release_id=record.skill_release_id,
                skill_manifest_sha256=record.skill_manifest_sha256,
            ),
            managed_fields_sha256="3" * 64,
        )
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "active"
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                (destination / skill).mkdir(parents=True)
            spec = replace(
                cursor,
                skills_path=lambda: destination,
                skills_global_path=lambda: destination,
            )
            with (
                mock.patch.dict(
                    mcp_config.CLIENT_SPECS,
                    {"cursor": spec},
                    clear=False,
                ),
                mock.patch.object(
                    mcp_config,
                    "registration_root",
                    return_value=root,
                ),
                mock.patch.object(
                    client_host_ownership,
                    "read_record_if_present",
                    side_effect=(record, changed),
                ),
                mock.patch.object(
                    cursor_activation,
                    "cursor_activation_root",
                    return_value=root / "state",
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "verify_cached_payload",
                    return_value=record.skill_manifest_sha256,
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "verify_skill_copy",
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "_tree_has_nondefault_windows_stream",
                    return_value=False,
                ),
            ):
                result = doctor._legacy_cursor_owned_skills_status()

        self.assertEqual(result, "unverifiable")

    def test_owned_skill_probe_uses_read_only_release_fallback(self):
        record = mock.Mock(
            skill_release_id="1-" + "2" * 40,
            skill_manifest_sha256="b" * 64,
        )
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "active"
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                (destination / skill).mkdir(parents=True)
            spec = replace(
                cursor,
                skills_path=lambda: destination,
                skills_global_path=lambda: destination,
            )
            with (
                mock.patch.dict(
                    mcp_config.CLIENT_SPECS,
                    {"cursor": spec},
                    clear=False,
                ),
                mock.patch.object(
                    mcp_config,
                    "registration_root",
                    return_value=root,
                ),
                mock.patch.object(
                    client_host_ownership,
                    "read_record_if_present",
                    return_value=record,
                ),
                mock.patch.object(
                    cursor_activation,
                    "cursor_activation_root",
                    return_value=root / "state",
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "verify_cached_payload",
                    side_effect=cursor_skill_payload.CursorSkillPayloadError(
                        "cache unavailable"
                    ),
                ),
                mock.patch.object(
                    cursor_activation,
                    "verify_owned_active_skills_for_doctor",
                ) as verify,
            ):
                result = doctor._legacy_cursor_owned_skills_status()

        self.assertEqual(result, "healthy")
        verify.assert_called_once_with(record, managed_root=root)

    def test_owned_skill_probe_rejects_link_like_roots_before_content_read(self):
        record = mock.Mock(
            skill_release_id="1-" + "3" * 40,
            skill_manifest_sha256="c" * 64,
        )
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "active"
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                (destination / skill).mkdir(parents=True)
            spec = replace(
                cursor,
                skills_path=lambda: destination,
                skills_global_path=lambda: destination,
            )
            with (
                mock.patch.dict(
                    mcp_config.CLIENT_SPECS,
                    {"cursor": spec},
                    clear=False,
                ),
                mock.patch.object(
                    mcp_config,
                    "registration_root",
                    return_value=root,
                ),
                mock.patch.object(
                    client_host_ownership,
                    "read_record_if_present",
                    return_value=record,
                ),
                mock.patch.object(
                    managed_install,
                    "_is_link_like",
                    return_value=True,
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "verify_cached_payload",
                ) as verify,
            ):
                result = doctor._legacy_cursor_owned_skills_status()

        self.assertEqual(result, "unverifiable")
        verify.assert_not_called()

    def test_unverifiable_release_fallback_does_not_accuse_user_modification(self):
        record = mock.Mock(
            skill_release_id="1-" + "4" * 40,
            skill_manifest_sha256="d" * 64,
        )
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "active"
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                (destination / skill).mkdir(parents=True)
            spec = replace(
                cursor,
                skills_path=lambda: destination,
                skills_global_path=lambda: destination,
            )
            with (
                mock.patch.dict(
                    mcp_config.CLIENT_SPECS,
                    {"cursor": spec},
                    clear=False,
                ),
                mock.patch.object(
                    mcp_config,
                    "registration_root",
                    return_value=root,
                ),
                mock.patch.object(
                    client_host_ownership,
                    "read_record_if_present",
                    return_value=record,
                ),
                mock.patch.object(
                    cursor_activation,
                    "cursor_activation_root",
                    return_value=root / "state",
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "verify_cached_payload",
                    side_effect=cursor_skill_payload.CursorSkillPayloadError(
                        "cache unavailable"
                    ),
                ),
                mock.patch.object(
                    cursor_activation,
                    "verify_owned_active_skills_for_doctor",
                    side_effect=cursor_activation.CursorActivationError(
                        "private path detail"
                    ),
                ),
            ):
                result = doctor._legacy_cursor_owned_skills_status()

        self.assertEqual(result, "unverifiable")

    def test_owned_skill_probe_rejects_noncanonical_release_id_before_cache_read(self):
        record = mock.Mock(
            skill_release_id="../outside",
            skill_manifest_sha256="e" * 64,
        )
        cursor = mcp_config.CLIENT_SPECS["cursor"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "active"
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                (destination / skill).mkdir(parents=True)
            spec = replace(
                cursor,
                skills_path=lambda: destination,
                skills_global_path=lambda: destination,
            )
            with (
                mock.patch.dict(
                    mcp_config.CLIENT_SPECS,
                    {"cursor": spec},
                    clear=False,
                ),
                mock.patch.object(
                    mcp_config,
                    "registration_root",
                    return_value=root,
                ),
                mock.patch.object(
                    client_host_ownership,
                    "read_record_if_present",
                    return_value=record,
                ),
                mock.patch.object(
                    cursor_skill_payload,
                    "verify_cached_payload",
                ) as verify,
            ):
                result = doctor._legacy_cursor_owned_skills_status()

        self.assertEqual(result, "unverifiable")
        verify.assert_not_called()

    def test_machine_state_boundary_contains_optional_probe_failure(self):
        with mock.patch.object(
            doctor,
            "collect_cursor_doctor_evidence",
            side_effect=RuntimeError("private path detail"),
        ):
            states = doctor.cursor_doctor_states_for_machine()

        self.assertEqual(states, ("configured_broken",))

    def test_cursor_local_ui_is_enabled_on_windows_and_macos(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                self.assertTrue(doctor._cursor_local_ui_enabled())


class MainTests(unittest.TestCase):
    def _run(self, argv, results):
        with (
            mock.patch.object(doctor, "run_all", return_value=results),
            mock.patch.object(
                doctor,
                "cursor_doctor_states_for_machine",
                return_value=("not_selected",),
            ),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main(argv)
        return code, buf.getvalue()

    def test_exit_zero_when_only_pass_and_warn(self):
        results = [doctor.CheckResult("PASS", "python", "3.11"),
                   doctor.CheckResult("WARN", "libreoffice", "not installed (optional)", fix="on demand")]
        code, out = self._run([], results)
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_exit_one_on_fail(self):
        results = [doctor.CheckResult("FAIL", "python", "3.11", fix="need 3.12+")]
        code, out = self._run([], results)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_json_shape(self):
        results = [doctor.CheckResult("PASS", "python", "3.11")]
        code, out = self._run(["--json"], results)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["results"][0]["name"], "python")
        self.assertEqual(payload["cursor_states"], ["not_selected"])

    def test_cursor_version_json_stays_isolated_from_status_collector(self):
        result = doctor.CheckResult("PASS", "cursor-version", "supported")
        with (
            mock.patch.object(
                doctor,
                "check_cursor_version",
                return_value=result,
            ),
            mock.patch.object(
                doctor,
                "cursor_doctor_states_for_machine",
            ) as states,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main(["--cursor-version", "--json"])

        self.assertEqual(code, 0)
        self.assertNotIn("cursor_states", json.loads(buf.getvalue()))
        states.assert_not_called()

    def test_human_output_does_not_run_cursor_status_collector(self):
        results = [doctor.CheckResult("PASS", "python", "3.11")]
        with (
            mock.patch.object(doctor, "run_all", return_value=results),
            mock.patch.object(
                doctor,
                "cursor_doctor_states_for_machine",
            ) as states,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main([])

        self.assertEqual(code, 0)
        self.assertIn("OK", buf.getvalue())
        states.assert_not_called()

    def test_stop_panel_passes_on_macos(self):
        with mock.patch("platform.system", return_value="Darwin"):
            r = doctor.check_stop_panel()
        self.assertEqual(r.status, "PASS")
        self.assertIn("native", r.detail)

    def test_setup_dialog_checks_tkinter_even_on_macos(self):
        with mock.patch.dict(sys.modules, {"tkinter": None}):
            result = doctor.check_setup_dialog()

        self.assertEqual(result.status, "WARN")
        self.assertIn("tkinter", result.detail)
        self.assertIn("--from-env", result.fix)
        self.assertIn("never argv or shell history", result.fix)

    def test_stop_panel_warns_when_tkinter_missing_off_macos(self):
        # non-macOS + no tkinter → WARN with an actionable fix, never FAIL (audits still run)
        with mock.patch("platform.system", return_value="Windows"), \
             mock.patch.dict(sys.modules, {"tkinter": None}):
            r = doctor.check_stop_panel()
        self.assertEqual(r.status, "WARN")
        self.assertTrue(r.fix)

    def test_stop_panel_passes_when_tkinter_present_off_macos(self):
        import types
        with mock.patch("platform.system", return_value="Windows"), \
             mock.patch.dict(sys.modules, {"tkinter": types.ModuleType("tkinter")}):
            r = doctor.check_stop_panel()
        self.assertEqual(r.status, "PASS")


if __name__ == "__main__":
    unittest.main()
