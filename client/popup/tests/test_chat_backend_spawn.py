"""Regression tests for console-less GE follow-up CLI subprocesses."""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import tempfile
import traceback
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from client.popup import chat_backend


class ChatBackendSpawnTestCase(unittest.TestCase):
    @staticmethod
    def _write_codex_config(root: str, provider_lines: str = "", base_url: str = "https://proxy.example/v1") -> None:
        Path(root, "config.toml").write_text(
            'model = "gpt-test-model"\n'
            'model_provider = "custom"\n'
            '[model_providers.custom]\n'
            'name = "Test proxy"\n'
            f'base_url = "{base_url}"\n'
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n'
            + provider_lines,
            encoding="utf-8",
        )

    def test_codex_route_is_projected_into_first_and_resume_commands(self):
        with tempfile.TemporaryDirectory() as codex_home, mock.patch.dict(
            os.environ, {"CODEX_HOME": codex_home}, clear=True
        ):
            self._write_codex_config(codex_home)
            session = chat_backend.ChatSession({"caller": "codex"})
            first = session._build_codex_cmd(True, [], codex_home)
            session.session_id = "thread-test"
            resume = session._build_codex_cmd(False, [], codex_home)

        self.assertEqual(session.model, "gpt-test-model")
        self.assertEqual(session.transport, "codex")
        for override in session._codex_config_args:
            self.assertIn(override, first)
            self.assertIn(override, resume)
        for command in (first, resume):
            self.assertEqual(command.count("-c"), len(session._codex_config_args))
            self.assertEqual(command.count("--ignore-user-config"), 1)
            self.assertEqual(command[command.index("--model") + 1], "gpt-test-model")
        self.assertTrue(all(value in "\n".join(session._codex_config_args) for value in ('model_provider="custom"', 'base_url="https://proxy.example/v1"')))

    def test_codex_desktop_loopback_provider_omits_experimental_token(self):
        token = "synthetic-desktop-token"
        with tempfile.TemporaryDirectory() as codex_home, mock.patch.dict(
            os.environ, {"CODEX_HOME": codex_home}, clear=True
        ):
            self._write_codex_config(
                codex_home,
                base_url="http://127.0.0.1:15721/v1",
                provider_lines=f'experimental_bearer_token = "{token}"\n',
            )
            session = chat_backend.ChatSession({"caller": "codex"})
            first = session._build_codex_cmd(True, [], codex_home)

        projected = "\n".join(session._codex_config_args)
        command_text = "\n".join(first)
        self.assertEqual(session.transport, "codex")
        self.assertIn('base_url="http://127.0.0.1:15721/v1"', projected)
        self.assertNotIn("experimental_bearer_token", projected)
        self.assertNotIn(token, projected)
        self.assertNotIn("experimental_bearer_token", command_text)
        self.assertNotIn(token, command_text)

    def test_codex_desktop_localhost_provider_is_allowed(self):
        with tempfile.TemporaryDirectory() as codex_home, mock.patch.dict(
            os.environ, {"CODEX_HOME": codex_home}, clear=True
        ):
            self._write_codex_config(codex_home, base_url="http://localhost:15721/v1")
            session = chat_backend.ChatSession({"caller": "codex"})

        self.assertEqual(session.transport, "codex")
        self.assertIn('base_url="http://localhost:15721/v1"', "\n".join(session._codex_config_args))

    def test_remote_http_codex_route_fails_closed(self):
        with tempfile.TemporaryDirectory() as codex_home:
            self._write_codex_config(codex_home, base_url="http://proxy.example/v1")
            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}, clear=True), \
                    self.assertRaises(chat_backend.ChatRouteError):
                chat_backend.ChatSession({"caller": "codex"})

    def test_claude_route_is_projected_into_first_and_resume_commands(self):
        with tempfile.TemporaryDirectory() as claude_home, mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": claude_home}, clear=True
        ):
            settings = {"model": "claude-test-model",
                        "env": {"ANTHROPIC_BASE_URL": "https://proxy.example/api"},
                        "hooks": {"SessionStart": [{"command": "must-not-load"}]}}
            Path(claude_home, "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            session = chat_backend.ChatSession({"caller": "claude"})
            first = session._build_cmd()
            session.session_id = "session-test"
            resume = session._build_cmd()

        self.assertEqual(session.model, "claude-test-model")
        self.assertEqual(session.transport, "claude")
        for command in (first, resume):
            settings = json.loads(command[command.index("--settings") + 1])
            self.assertEqual(settings, {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example/api"}})
            self.assertNotIn("must-not-load", "\n".join(command))
            values = [command[command.index(option) + 1] for option in
                      ("--setting-sources", "--allowedTools", "--disallowedTools", "--model")]
            self.assertEqual(values, ["", "", "*", "claude-test-model"])

    def test_explicit_and_environment_models_override_configured_models(self):
        with tempfile.TemporaryDirectory() as codex_home, mock.patch.dict(
            os.environ, {"CODEX_HOME": codex_home, "GE_CHAT_MODEL": "gpt-env-model"}, clear=True
        ):
            self._write_codex_config(codex_home)
            env_model = chat_backend.ChatSession({"caller": "codex"}).model
            explicit = chat_backend.ChatSession(
                {"caller": "codex"}, model="gpt-explicit-model"
            ).model
        self.assertEqual(env_model, "gpt-env-model")
        self.assertEqual(explicit, "gpt-explicit-model")

    def test_whitespace_explicit_model_yields_to_environment_model(self):
        with (
            mock.patch.dict(
                os.environ, {"GE_CHAT_MODEL": "gpt-env-model"}, clear=True
            ),
            mock.patch.object(
                chat_backend, "_codex_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
        ):
            session = chat_backend.ChatSession(
                {"caller": "codex"}, model="   "
            )

        self.assertEqual(session.transport, "codex")
        self.assertEqual(session.model, "gpt-env-model")

    def test_whitespace_environment_model_is_treated_as_absent(self):
        with (
            mock.patch.dict(os.environ, {"GE_CHAT_MODEL": "   "}, clear=True),
            mock.patch.object(
                chat_backend, "_codex_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
        ):
            session = chat_backend.ChatSession({"caller": "codex"})
            command = session._build_codex_cmd(True, [], os.getcwd())

        self.assertEqual(session.transport, "codex")
        self.assertIsNone(session.model)
        self.assertNotIn("--model", command)

    def test_absent_configs_omit_non_cursor_model_flags(self):
        with tempfile.TemporaryDirectory() as empty, mock.patch.dict(
            os.environ, {"CODEX_HOME": empty, "CLAUDE_CONFIG_DIR": empty}, clear=True
        ), mock.patch.object(chat_backend, "_resolve_cli", return_value=None), \
                mock.patch.object(chat_backend, "_resolve_cursor_cli", return_value=None):
            codex = chat_backend.ChatSession({"caller": "codex"})
            claude = chat_backend.ChatSession({"caller": "claude"})
            cursor = chat_backend.ChatSession({"caller": "cursor"})
            codex_cmd = codex._build_codex_cmd(True, [], empty)
            claude_cmd = claude._build_cmd()

        self.assertIsNone(codex.model)
        self.assertIsNone(claude.model)
        self.assertEqual(cursor.model, "auto")
        self.assertNotIn("--model", codex_cmd)
        self.assertNotIn("--model", claude_cmd)

    def test_unknown_and_missing_callers_fail_closed(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                chat_backend, "_codex_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(
                chat_backend, "_claude_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None) as resolve_cli,
        ):
            sessions = [
                chat_backend.ChatSession({}),
                chat_backend.ChatSession({"caller": None}),
                chat_backend.ChatSession({"caller": ""}),
                chat_backend.ChatSession({"caller": "   "}),
                chat_backend.ChatSession({"caller": "glm"}),
            ]

        for session in sessions:
            self.assertIsNone(session.transport)
            self.assertIsNone(session.model)
            self.assertIsNotNone(session._no_transport_for)
        resolve_cli.assert_not_called()

    def test_unroutable_turn_reports_unavailable_without_spawning(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
        ):
            session = chat_backend.ChatSession({"caller": "glm"})

        on_delta = mock.Mock()
        on_done = mock.Mock()
        on_error = mock.Mock()
        on_action = mock.Mock()
        with (
            mock.patch.object(session, "_run_turn_codex", new_callable=mock.AsyncMock) as codex,
            mock.patch.object(session, "_run_turn_cursor", new_callable=mock.AsyncMock) as cursor,
        ):
            asyncio.run(
                session.run_turn(
                    "普通追问", [], on_delta, on_done, on_error, on_action
                )
            )

        on_error.assert_called_once_with(session.strings["unavailable"])
        on_delta.assert_not_called()
        on_done.assert_not_called()
        on_action.assert_not_called()
        codex.assert_not_awaited()
        cursor.assert_not_awaited()

    def test_known_model_can_supply_transport_when_caller_is_missing(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                chat_backend, "_codex_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
        ):
            session = chat_backend.ChatSession({}, model="gpt-5.6-sol")

        self.assertEqual(session.transport, "codex")
        self.assertEqual(session.model, "gpt-5.6-sol")
        self.assertIsNone(session._no_transport_for)

    def test_model_family_resolution_is_exact_or_hyphenated(self):
        expected = {
            "claude": "claude",
            "claude-opus-5": "claude",
            "claude-codex-v1": "claude",
            "gpt-5.6-sol": "codex",
            "gpt": "codex",
            "gpt4": None,
            "o3": "codex",
            "o1preview": None,
            "encodex-v1": None,
            "o3rdparty-model": None,
            "opuscorp-model": None,
            "glm-4-plus": None,
            "cursor-fast": None,
            "auto": None,
        }
        for model, transport in expected.items():
            with self.subTest(model=model):
                self.assertEqual(chat_backend._resolve_transport(model), transport)

    def test_unknown_explicit_model_fails_closed(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None) as resolve_cli,
        ):
            session = chat_backend.ChatSession(
                {"caller": "codex"}, model="glm-4-plus"
            )

        self.assertIsNone(session.transport)
        self.assertEqual(session.model, "glm-4-plus")
        self.assertEqual(session._no_transport_for, "model 'glm-4-plus'")
        resolve_cli.assert_not_called()

    def test_cursor_caller_uses_cursor_agent_ask_transport(self):
        with mock.patch.object(
            chat_backend,
            "_resolve_cursor_cli",
            return_value=("/usr/local/bin/agent",),
        ), mock.patch.object(chat_backend, "_cursor_sandbox_mode", return_value="enabled"):
            session = chat_backend.ChatSession({"caller": "cursor"})
            first = session._build_cursor_cmd(True, "/tmp/ge-chat")
            session.session_id = "cursor-chat-id"
            resumed = session._build_cursor_cmd(False, "/tmp/ge-chat")

        self.assertEqual(session.transport, "cursor")
        self.assertEqual(session.model, "auto")
        for command in (first, resumed):
            self.assertEqual(command[0], "/usr/local/bin/agent")
            self.assertIn("--print", command)
            self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
            self.assertEqual(command[command.index("--mode") + 1], "ask")
            self.assertEqual(command[command.index("--sandbox") + 1], "enabled")
            self.assertEqual(command[command.index("--workspace") + 1], "/tmp/ge-chat")
            self.assertIn("--trust", command)
            self.assertNotIn("--force", command)
            self.assertNotIn("--approve-mcps", command)
        self.assertNotIn("--resume", first)
        self.assertEqual(resumed[resumed.index("--resume") + 1], "cursor-chat-id")

    def test_cursor_windows_uses_read_only_ask_mode_without_force_flags(self):
        with mock.patch.object(
            chat_backend,
            "_resolve_cursor_cli",
            return_value=(r"C:\cursor-agent\agent.cmd",),
        ), mock.patch.object(chat_backend, "_cursor_sandbox_mode", return_value="disabled"):
            session = chat_backend.ChatSession({"caller": "cursor"})
            command = session._build_cursor_cmd(True, r"C:\empty-workspace")

        self.assertEqual(command[command.index("--sandbox") + 1], "disabled")
        self.assertEqual(command[command.index("--mode") + 1], "ask")
        self.assertNotIn("--force", command)
        self.assertNotIn("--yolo", command)
        self.assertNotIn("--approve-mcps", command)

    def test_cursor_model_override_stays_on_cursor_transport(self):
        with (
            mock.patch.object(
                chat_backend,
                "_resolve_cursor_cli",
                return_value=("/trusted/cursor-agent",),
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            session = chat_backend.ChatSession(
                {"caller": "cursor"}, model="gpt-5.5"
            )

        self.assertEqual(session.transport, "cursor")
        self.assertEqual(session.model, "gpt-5.5")

    def test_auto_model_fails_closed_for_non_cursor_callers(self):
        with (
            mock.patch.dict(os.environ, {"GE_CHAT_MODEL": "auto"}, clear=True),
            mock.patch.object(
                chat_backend, "_claude_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(
                chat_backend, "_codex_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
        ):
            claude = chat_backend.ChatSession({"caller": "claude"})
            codex = chat_backend.ChatSession({"caller": "codex"})

        self.assertEqual((claude.transport, claude.model), (None, "auto"))
        self.assertEqual((codex.transport, codex.model), (None, "auto"))
        self.assertIsNotNone(claude._no_transport_for)
        self.assertIsNotNone(codex._no_transport_for)

    def test_non_cursor_cursor_model_fails_closed(self):
        with mock.patch.dict(
            os.environ, {"GE_CHAT_MODEL": "cursor-fast"}, clear=True
        ), mock.patch.object(
            chat_backend, "_resolve_cli", return_value=None
        ), mock.patch.object(
            chat_backend, "_claude_route_from_user_config", return_value=(None, ())
        ):
            session = chat_backend.ChatSession({"caller": "codex"})

        self.assertIsNone(session.transport)
        self.assertEqual(session.model, "cursor-fast")
        self.assertEqual(session._no_transport_for, "model 'cursor-fast'")

    def test_cursor_followup_can_be_explicitly_disabled_without_resolving_cli(self):
        with (
            mock.patch.dict(
                os.environ, {"GE_CURSOR_FOLLOWUP": "0"}, clear=True
            ),
            mock.patch.object(chat_backend, "_resolve_cursor_cli") as resolve,
        ):
            session = chat_backend.ChatSession({"caller": "cursor"})

        self.assertTrue(session._cursor_followup_disabled)
        self.assertIsNone(session._cursor_bin)
        resolve.assert_not_called()

    def test_missing_claude_cli_guides_the_user_to_install_and_sign_in(self):
        with (
            mock.patch.object(
                chat_backend, "_claude_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
        ):
            # Language comes from the resolver now; pin en-US so the English guidance assertion is
            # deterministic regardless of the host machine's system locale.
            session = chat_backend.ChatSession({"caller": "claude", "ui_locale": "en-US"})
        errors = []
        asyncio.run(
            session.run_turn(
                "explain the label", None, lambda _x: None, lambda _x: None,
                errors.append, lambda _x: None,
            )
        )
        self.assertEqual(errors, [session.strings["missing_claude_cli"]])
        self.assertIn("Claude Code CLI", errors[0])
        self.assertIn("install and sign in", errors[0])

    def test_missing_codex_cli_uses_the_approved_localized_guidance(self):
        with (
            mock.patch.object(
                chat_backend, "_codex_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
        ):
            # Language now comes from the resolver, not the title — pin it explicitly for the
            # Chinese localization assertion below.
            session = chat_backend.ChatSession(
                {"caller": "codex", "title": "追问", "ui_locale": "zh-CN"}
            )
        errors = []
        asyncio.run(
            session.run_turn(
                "解释标签", None, lambda _x: None, lambda _x: None,
                errors.append, lambda _x: None,
            )
        )
        self.assertEqual(errors, [session.strings["missing_codex_cli"]])
        self.assertEqual(
            errors[0],
            "当前设备未安装 Codex CLI，因此无法使用右侧追问。"
            "请在终端安装并完成登录，然后重新打开此窗口。",
        )

    def test_missing_cursor_cli_guides_install_but_disabled_cursor_does_not(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(chat_backend, "_resolve_cursor_cli", return_value=None),
        ):
            # env cleared → resolver would follow the machine locale; pin en-US so the English
            # "install and sign in" assertion is deterministic on any host.
            missing = chat_backend.ChatSession({"caller": "cursor", "ui_locale": "en-US"})
        missing_errors = []
        asyncio.run(
            missing.run_turn(
                "explain", None, lambda _x: None, lambda _x: None,
                missing_errors.append, lambda _x: None,
            )
        )
        self.assertEqual(missing_errors, [missing.strings["missing_cursor_cli"]])
        self.assertIn("install and sign in", missing_errors[0])

        with mock.patch.dict(os.environ, {"GE_CURSOR_FOLLOWUP": "0"}, clear=True):
            disabled = chat_backend.ChatSession({"caller": "cursor"})
        disabled_errors = []
        asyncio.run(
            disabled.run_turn(
                "explain", None, lambda _x: None, lambda _x: None,
                disabled_errors.append, lambda _x: None,
            )
        )
        self.assertEqual(
            disabled_errors, [disabled.strings["cursor_followup_disabled"]]
        )
        self.assertNotIn("install and sign in", disabled_errors[0])

    def test_cursor_stream_parser_collects_deltas_and_requires_success_terminal(self):
        success = b"\n".join((
            json.dumps({"type": "system", "subtype": "init", "session_id": "cursor-id"}).encode(),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hello "}
            ]}}).encode(),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "world"}
            ]}}).encode(),
            json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "result": "hello world", "session_id": "cursor-id"}).encode(),
        ))
        self.assertEqual(
            chat_backend._parse_cursor_stream(success),
            ("cursor-id", "hello world", None),
        )
        invalid_session = (
            b'{"type":"system","session_id":"bad & injected"}\n'
            b'{"type":"result","subtype":"success","is_error":false,'
            b'"result":"answer"}\n'
        )
        invalid_id, _answer, invalid_error = chat_backend._parse_cursor_stream(
            invalid_session
        )
        self.assertIsNone(invalid_id)
        self.assertEqual(invalid_error, "cursor session id was invalid")
        self.assertEqual(
            chat_backend._parse_cursor_stream(
                b'{"type":"assistant","message":{"content":[]}}'
            )[1],
            None,
        )

    def test_cursor_child_uses_login_and_drops_ambient_credentials(self):
        trusted = str(Path(tempfile.gettempdir()).resolve() / "cursor-agent")
        with mock.patch.dict(os.environ, {
            "PATH": os.pathsep.join(("/untrusted/bin", "/usr/bin")),
            "HOME": "/synthetic/home",
            "CURSOR_API_KEY": "synthetic-cursor-secret",
            "OPENAI_API_KEY": "synthetic-openai-secret",
            "AWS_SECRET_ACCESS_KEY": "synthetic-aws-secret",
            "HTTPS_PROXY": "https://user:secret@proxy.example",
            "PIP_INDEX_URL": "https://token@packages.example/simple",
            "CI_JOB_JWT": "synthetic-ci-secret",
        }, clear=True):
            child = chat_backend._cursor_subscription_env_safe((trusted,))
        self.assertEqual(child["HOME"], "/synthetic/home")
        self.assertEqual(
            child["PATH"].split(os.pathsep)[0],
            str(Path(trusted).parent),
        )
        self.assertNotIn("/untrusted/bin", child["PATH"])
        self.assertNotIn("CURSOR_API_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", child)
        self.assertNotIn("HTTPS_PROXY", child)
        self.assertNotIn("PIP_INDEX_URL", child)
        self.assertNotIn("CI_JOB_JWT", child)

    def test_cursor_resolver_never_accepts_generic_agent_from_path(self):
        def resolve(command):
            return "/untrusted/agent" if command == "agent" else None

        with (
            mock.patch.object(chat_backend, "_resolve_cli", side_effect=resolve),
            mock.patch.object(chat_backend.os, "name", "posix"),
        ):
            self.assertIsNone(chat_backend._resolve_cursor_cli())

    def test_cursor_windows_resolver_uses_version_checked_vendor_candidate(self):
        with tempfile.TemporaryDirectory() as local_app_data:
            candidate = Path(local_app_data) / "cursor-agent" / "agent.cmd"
            candidate.parent.mkdir()
            candidate.write_text("@echo off\n", encoding="utf-8")
            script = candidate.with_suffix(".ps1")
            script.write_text("exit $LASTEXITCODE\n", encoding="utf-8")
            system_root = Path(local_app_data) / "Windows"
            powershell = (
                system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            powershell.parent.mkdir(parents=True)
            powershell.write_bytes(b"synthetic executable")
            with (
                mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
                mock.patch.object(chat_backend.os, "name", "nt"),
                mock.patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": local_app_data, "SYSTEMROOT": str(system_root)},
                    clear=True,
                ),
                mock.patch.object(
                    chat_backend, "_cli_version_ok", return_value=True
                ) as version_ok,
            ):
                resolved = chat_backend._resolve_cursor_cli()

        self.assertEqual(
            resolved,
            (
                str(powershell), "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(script),
            ),
        )
        version_ok.assert_called_once_with(str(candidate))

    def test_auto_model_is_cursor_scoped_and_other_callers_fail_closed(self):
        with (
            mock.patch.dict(os.environ, {"GE_CHAT_MODEL": "auto"}, clear=True),
            mock.patch.object(
                chat_backend, "_claude_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(
                chat_backend, "_codex_route_from_user_config", return_value=(None, ())
            ),
            mock.patch.object(chat_backend, "_resolve_cli", return_value=None),
            mock.patch.object(chat_backend, "_resolve_cursor_cli", return_value=None),
        ):
            transports = {
                caller: chat_backend.ChatSession({"caller": caller}).transport
                for caller in ("claude", "codex", "cursor")
            }

        self.assertEqual(
            transports,
            {"claude": None, "codex": None, "cursor": "cursor"},
        )

    def test_cursor_windows_batch_wrapper_fails_closed_without_powershell_script(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            chat_backend.os, "name", "nt"
        ), mock.patch.dict(os.environ, {"SYSTEMROOT": tmp}, clear=True):
            wrapper = Path(tmp) / "cursor-agent.cmd"
            wrapper.write_text("@echo off\n", encoding="utf-8")
            self.assertIsNone(chat_backend._cursor_cli_invocation(str(wrapper)))

    def test_cursor_turn_commits_session_only_after_success(self):
        class Proc:
            returncode = 0

            async def communicate(self, _prompt):
                return (
                    b'{"type":"system","session_id":"cursor-session"}\n'
                    b'{"type":"result","subtype":"success","is_error":false,'
                    b'"result":"safe answer"}\n',
                    b"",
                )

        session = chat_backend.ChatSession({"caller": "cursor"})
        session._cursor_bin = ("/trusted/cursor-agent",)
        deltas, done, errors = [], [], []
        with mock.patch.object(
            chat_backend, "_spawn_cursor", mock.AsyncMock(return_value=Proc())
        ) as spawn, mock.patch.object(
            chat_backend,
            "_communicate_cursor_bounded",
            mock.AsyncMock(return_value=(
                b'{"type":"system","session_id":"cursor-session"}\n'
                b'{"type":"result","subtype":"success","is_error":false,'
                b'"result":"safe answer"}\n',
                b"",
            )),
        ):
            asyncio.run(
                session._run_turn_cursor(
                    "question", None, deltas.append, done.append, errors.append, lambda _x: None
                )
            )

        self.assertEqual(session.session_id, "cursor-session")
        self.assertEqual(done, ["safe answer"])
        self.assertEqual(errors, [])
        self.assertNotIn("--stream-partial-output", spawn.await_args.args[0])

    def test_cursor_turn_reports_images_instead_of_silently_dropping_them(self):
        session = chat_backend.ChatSession({"caller": "cursor"})
        session._cursor_bin = ("/trusted/cursor-agent",)
        errors = []
        with mock.patch.object(chat_backend, "_spawn_cursor") as spawn:
            asyncio.run(
                session._run_turn_cursor(
                    "question", ["data:image/png;base64,AA=="], lambda _x: None,
                    lambda _x: None, errors.append, lambda _x: None,
                )
            )

        spawn.assert_not_called()
        self.assertEqual(errors, [session.strings["images_unsupported"]])

    def test_cursor_empty_success_result_is_reported_as_model_error(self):
        class Proc:
            returncode = 0

            async def communicate(self, _prompt):
                return (
                    b'{"type":"system","session_id":"candidate"}\n'
                    b'{"type":"result","subtype":"success","is_error":false,'
                    b'"result":""}\n',
                    b"",
                )

        session = chat_backend.ChatSession({"caller": "cursor"})
        session._cursor_bin = ("/trusted/cursor-agent",)
        done, errors = [], []
        with mock.patch.object(
            chat_backend, "_spawn_cursor", mock.AsyncMock(return_value=Proc())
        ), mock.patch.object(
            chat_backend,
            "_communicate_cursor_bounded",
            mock.AsyncMock(return_value=(
                b'{"type":"system","session_id":"candidate"}\n'
                b'{"type":"result","subtype":"success","is_error":false,'
                b'"result":""}\n',
                b"",
            )),
        ):
            asyncio.run(
                session._run_turn_cursor(
                    "question", None, lambda _x: None, done.append,
                    errors.append, lambda _x: None,
                )
            )

        self.assertIsNone(session.session_id)
        self.assertEqual(done, [])
        self.assertEqual(errors, [session.strings["model_error"]])

    def test_cursor_failure_does_not_log_child_stderr(self):
        private = "C:/private/profile reflected-secret"

        class Proc:
            returncode = 1

            async def communicate(self, _prompt):
                return b"", private.encode()

        session = chat_backend.ChatSession({"caller": "cursor"})
        session._cursor_bin = ("/trusted/cursor-agent",)
        errors, captured = [], io.StringIO()
        with (
            mock.patch.object(
                chat_backend, "_spawn_cursor", mock.AsyncMock(return_value=Proc())
            ),
            mock.patch.object(
                chat_backend,
                "_communicate_cursor_bounded",
                mock.AsyncMock(return_value=(b"", private.encode())),
            ),
            redirect_stderr(captured),
        ):
            asyncio.run(
                session._run_turn_cursor(
                    "question", None, lambda _x: None, lambda _x: None,
                    errors.append, lambda _x: None,
                )
            )

        self.assertEqual(errors, [session.strings["model_error"]])
        self.assertNotIn(private, captured.getvalue())

    def test_cursor_stream_reader_enforces_aggregate_byte_limit(self):
        async def read_over_limit():
            stream = asyncio.StreamReader()
            stream.feed_data(b"12345")
            stream.feed_eof()
            await chat_backend._read_cursor_stream_bounded(stream, 4)

        with self.assertRaises(chat_backend.CursorOutputLimitError):
            asyncio.run(read_over_limit())

    def test_cursor_stream_limit_fails_fast_when_sibling_pipe_never_closes(self):
        class Stdin:
            def write(self, _data):
                return None

            async def drain(self):
                return None

            def close(self):
                return None

        class Proc:
            stdin = Stdin()

            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.wait = mock.AsyncMock()

        async def overflow_without_stderr_eof():
            proc = Proc()
            proc.stdout.feed_data(b"12345")
            with mock.patch.object(chat_backend, "_CURSOR_STDOUT_MAX_BYTES", 4):
                with self.assertRaises(chat_backend.CursorOutputLimitError):
                    await asyncio.wait_for(
                        chat_backend._communicate_cursor_bounded(proc, b"prompt"),
                        timeout=0.2,
                    )
            proc.wait.assert_not_awaited()

        asyncio.run(overflow_without_stderr_eof())

    def test_cursor_output_limit_reports_generic_error_and_kills_once(self):
        proc = mock.Mock(returncode=None)
        session = chat_backend.ChatSession({"caller": "cursor"})
        session._cursor_bin = ("/trusted/cursor-agent",)
        errors = []
        with (
            mock.patch.object(
                chat_backend, "_spawn_cursor", mock.AsyncMock(return_value=proc)
            ),
            mock.patch.object(
                chat_backend,
                "_communicate_cursor_bounded",
                mock.AsyncMock(
                    side_effect=chat_backend.CursorOutputLimitError("too much")
                ),
            ),
            mock.patch.object(chat_backend, "_kill", mock.AsyncMock()) as kill,
        ):
            asyncio.run(
                session._run_turn_cursor(
                    "question", None, lambda _x: None, lambda _x: None,
                    errors.append, lambda _x: None,
                )
            )

        self.assertEqual(errors, [session.strings["error"]])
        kill.assert_awaited_once_with(proc)

    def test_cursor_timeout_reports_generic_error_and_kills_once(self):
        proc = mock.Mock()
        proc.communicate = mock.AsyncMock(side_effect=asyncio.TimeoutError)
        session = chat_backend.ChatSession({"caller": "cursor"})
        session._cursor_bin = ("/trusted/cursor-agent",)
        errors = []
        with (
            mock.patch.object(
                chat_backend, "_spawn_cursor", mock.AsyncMock(return_value=proc)
            ),
            mock.patch.object(
                chat_backend,
                "_communicate_cursor_bounded",
                mock.AsyncMock(side_effect=asyncio.TimeoutError),
            ),
            mock.patch.object(chat_backend, "_kill", mock.AsyncMock()) as kill,
        ):
            asyncio.run(
                session._run_turn_cursor(
                    "question", None, lambda _x: None, lambda _x: None,
                    errors.append, lambda _x: None,
                )
            )

        self.assertEqual(errors, [session.strings["timeout"]])
        kill.assert_awaited_once_with(proc)

    def test_cursor_cancellation_kills_once_and_propagates(self):
        started = asyncio.Event()
        never = asyncio.Event()

        proc = mock.Mock()

        async def communicate(_proc, _prompt):
            started.set()
            await never.wait()

        proc.communicate = mock.AsyncMock(side_effect=communicate)
        session = chat_backend.ChatSession({"caller": "cursor"})
        session._cursor_bin = ("/trusted/cursor-agent",)

        async def cancelled_turn():
            task = asyncio.create_task(
                session._run_turn_cursor(
                    "question", None, lambda _x: None, lambda _x: None,
                    lambda _x: None, lambda _x: None,
                )
            )
            await started.wait()
            task.cancel()
            await task

        with (
            mock.patch.object(
                chat_backend, "_spawn_cursor", mock.AsyncMock(return_value=proc)
            ),
            mock.patch.object(
                chat_backend,
                "_communicate_cursor_bounded",
                mock.AsyncMock(side_effect=communicate),
            ),
            mock.patch.object(chat_backend, "_kill", mock.AsyncMock()) as kill,
            self.assertRaises(asyncio.CancelledError),
        ):
            asyncio.run(cancelled_turn())

        kill.assert_awaited_once_with(proc)

    def test_parent_claude_endpoint_survives_secret_scrub_without_token(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"ANTHROPIC_BASE_URL": "https://proxy.example/api",
                         "ANTHROPIC_AUTH_TOKEN": "synthetic-secret-value",
                         "CLAUDE_CONFIG_DIR": root}, clear=True,
        ):
            session = chat_backend.ChatSession({"caller": "claude"})
            os.environ["ANTHROPIC_BASE_URL"] = "https://changed.example/api"
            command = session._build_cmd()
            child = chat_backend._subscription_env()
        self.assertEqual(json.loads(command[command.index("--settings") + 1])["env"]["ANTHROPIC_BASE_URL"], "https://proxy.example/api")
        self.assertNotIn("ANTHROPIC_BASE_URL", child)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", child)

    def test_claude_child_drops_unprojected_route_controls(self):
        route_controls = {
            "ANTHROPIC_CUSTOM_HEADERS": "X-Synthetic: value",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
        }
        with mock.patch.dict(
            os.environ, {**route_controls, "PATH": "synthetic-path"}, clear=True
        ):
            child = chat_backend._subscription_env()

        self.assertEqual(child["PATH"], "synthetic-path")
        for name in route_controls:
            self.assertNotIn(name, child)

    def test_missing_toml_parsers_fail_as_route_error(self):
        real_import = builtins.__import__

        def missing_parsers(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tomllib" or (
                name == "installer._vendor" and "tomli" in fromlist
            ):
                raise ModuleNotFoundError("synthetic missing parser")
            return real_import(name, globals, locals, fromlist, level)

        with tempfile.TemporaryDirectory() as root:
            self._write_codex_config(root)
            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": root}, clear=True),
                mock.patch("builtins.__import__", side_effect=missing_parsers),
                self.assertRaises(chat_backend.ChatRouteError),
            ):
                chat_backend.ChatSession({"caller": "codex"})

    def test_unsafe_codex_routes_fail_closed_without_echoing_values(self):
        cases = {
            "credentialed URL": 'base_url = "https://user:synthetic@proxy.example/v1"',
            "query URL": 'base_url = "https://proxy.example/v1?token=synthetic"',
            "secret provider field": 'env_key = "SYNTHETIC_PROVIDER_KEY"',
        }
        for label, replacement in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                self._write_codex_config(root)
                path = Path(root, "config.toml")
                text = path.read_text(encoding="utf-8")
                if replacement.startswith("base_url"):
                    text = text.replace('base_url = "https://proxy.example/v1"', replacement)
                else:
                    text += replacement + "\n"
                path.write_text(text, encoding="utf-8")
                with mock.patch.dict(os.environ, {"CODEX_HOME": root}, clear=True), \
                        self.assertRaises(chat_backend.ChatRouteError) as caught:
                    chat_backend.ChatSession({"caller": "codex"})
                self.assertNotIn("synthetic", "".join(traceback.format_exception(caught.exception)).lower())

    def test_unsafe_claude_routes_fail_closed_without_echoing_values(self):
        cases = [
            {"env": {"ANTHROPIC_BASE_URL": "http://proxy.example/api"}},
            {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example/api",
                     "ANTHROPIC_API_KEY": "synthetic-secret-value"}},
        ]
        for settings in cases:
            with self.subTest(settings=list(settings["env"])), tempfile.TemporaryDirectory() as root:
                Path(root, "settings.json").write_text(json.dumps(settings), encoding="utf-8")
                with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": root}, clear=True), \
                        self.assertRaises(chat_backend.ChatRouteError) as caught:
                    chat_backend.ChatSession({"caller": "claude"})
                self.assertNotIn("synthetic", "".join(traceback.format_exception(caught.exception)).lower())

    def test_malformed_route_configs_fail_closed(self):
        fixtures = (("CODEX_HOME", "config.toml", 'model = "synthetic-secret'),
                    ("CODEX_HOME", "config.toml", 'model = "bad model"'),
                    ("CODEX_HOME", "config.toml", 'profile = "work"'),
                    ("CLAUDE_CONFIG_DIR", "settings.json", "{not-json"))
        for env_name, filename, contents in fixtures:
            with self.subTest(env=env_name), tempfile.TemporaryDirectory() as root:
                Path(root, filename).write_text(contents, encoding="utf-8")
                with mock.patch.dict(os.environ, {env_name: root}, clear=True), \
                        self.assertRaises(chat_backend.ChatRouteError) as caught:
                    chat_backend.ChatSession({"caller": "codex" if env_name == "CODEX_HOME" else "claude"})
                self.assertNotIn("synthetic", "".join(traceback.format_exception(caught.exception)).lower())

    def test_cli_version_probe_requires_successful_version_command(self):
        completed = chat_backend.subprocess.CompletedProcess(
            ["/opt/bin/codex", "--version"], 0
        )
        with mock.patch.object(
            chat_backend.subprocess, "run", return_value=completed
        ) as run:
            self.assertTrue(chat_backend._cli_version_ok("/opt/bin/codex"))

        self.assertEqual(run.call_args.args[0], ["/opt/bin/codex", "--version"])
        self.assertIs(run.call_args.kwargs["stdin"], chat_backend.subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stdout"], chat_backend.subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], chat_backend.subprocess.DEVNULL)

    def test_cli_resolver_skips_known_desktop_shims(self):
        self.assertTrue(
            chat_backend._is_disallowed_cli_path(
                r"C:\Program Files\WindowsApps\codex.exe", platform="nt"
            )
        )
        self.assertTrue(
            chat_backend._is_disallowed_cli_path(
                "/Applications/Codex.app/Contents/MacOS/Codex"
            )
        )

    def test_cli_resolver_uses_later_candidate_when_first_fails(self):
        with (
            mock.patch.object(
                chat_backend,
                "_iter_cli_candidates",
                return_value=iter(["/bad/codex", "/good/codex"]),
            ),
            mock.patch.object(
                chat_backend,
                "_cli_version_ok",
                side_effect=lambda path: path == "/good/codex",
            ) as probe,
        ):
            resolved = chat_backend._resolve_cli("codex", cache={})

        self.assertEqual(resolved, "/good/codex")
        self.assertEqual(
            [call.args[0] for call in probe.call_args_list],
            ["/bad/codex", "/good/codex"],
        )

    def test_chat_session_commands_use_resolved_cli_paths(self):
        resolved = {
            "claude": "/usr/local/bin/claude",
            "codex": "/usr/local/bin/codex",
        }

        with mock.patch.object(
            chat_backend,
            "_resolve_cli",
            side_effect=lambda command: resolved[command],
        ), mock.patch.object(
            chat_backend, "_claude_route_from_user_config", return_value=(None, ())
        ), mock.patch.object(
            chat_backend, "_codex_route_from_user_config", return_value=(None, ())
        ):
            claude = chat_backend.ChatSession({"caller": "claude"})
            codex = chat_backend.ChatSession({"caller": "codex"})

        self.assertEqual(claude._build_cmd()[0], "/usr/local/bin/claude")
        self.assertEqual(
            codex._build_codex_cmd(True, [], "/tmp/ge-chat")[0],
            "/usr/local/bin/codex",
        )

    def test_platform_spawn_kwargs(self):
        windows = chat_backend._subprocess_session_kwargs("nt")
        self.assertTrue(windows.get("creationflags", 0) & 0x08000000)
        self.assertFalse(windows.get("creationflags", 0) & 0x00000200)
        self.assertNotIn("start_new_session", windows)

        posix = chat_backend._subprocess_session_kwargs("posix")
        self.assertIs(posix.get("start_new_session"), True)
        self.assertNotIn("creationflags", posix)

    def test_followup_transports_forward_platform_kwargs(self):
        async def exercise(spawn):
            captured = {}

            async def fake_create(*args, **kwargs):
                captured.update(kwargs)
                return object()

            with (
                mock.patch.object(
                    chat_backend,
                    "_subprocess_session_kwargs",
                    return_value={"creationflags": 0x08000000},
                ),
                mock.patch.object(
                    asyncio, "create_subprocess_exec", side_effect=fake_create
                ),
            ):
                await spawn(["cli"], ".", {})
            return captured

        for spawn in (
            chat_backend._spawn_claude,
            chat_backend._spawn_codex,
            chat_backend._spawn_cursor,
        ):
            with self.subTest(spawn=spawn.__name__):
                kwargs = asyncio.run(exercise(spawn))
                self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_windows_kill_terminates_process_tree_and_reaps_direct_child(self):
        class Proc:
            pid = 42

            def __init__(self):
                self.killed = False
                self.waited = False

            def kill(self):
                self.killed = True

            async def wait(self):
                self.waited = True

        killer = mock.Mock()
        killer.wait = mock.AsyncMock(return_value=0)
        proc = Proc()
        with mock.patch.object(
            asyncio,
            "create_subprocess_exec",
            mock.AsyncMock(return_value=killer),
        ) as create:
            asyncio.run(chat_backend._kill(proc, platform="nt"))

        self.assertEqual(
            create.await_args.args[:6],
            ("taskkill", "/PID", "42", "/T", "/F"),
        )
        self.assertFalse(proc.killed)
        self.assertTrue(proc.waited)

    def test_windows_kill_falls_back_to_direct_child(self):
        class Proc:
            pid = 42

            def __init__(self):
                self.killed = False

            def kill(self):
                self.killed = True

            async def wait(self):
                return 0

        proc = Proc()
        with mock.patch.object(
            asyncio,
            "create_subprocess_exec",
            mock.AsyncMock(side_effect=OSError("taskkill unavailable")),
        ):
            asyncio.run(chat_backend._kill(proc, platform="nt"))

        self.assertTrue(proc.killed)

    def test_posix_kill_targets_process_group_and_reaps(self):
        proc = mock.Mock(pid=42)
        proc.wait = mock.AsyncMock()
        with (
            mock.patch.object(
                chat_backend.os, "getpgid", return_value=42, create=True
            ),
            mock.patch.object(
                chat_backend.os, "killpg", create=True
            ) as killpg,
            mock.patch.object(
                chat_backend.signal, "SIGKILL", 9, create=True
            ),
        ):
            asyncio.run(chat_backend._kill(proc, platform="posix"))

        killpg.assert_called_once_with(42, 9)
        proc.kill.assert_not_called()
        proc.wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
