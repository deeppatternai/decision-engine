from __future__ import annotations

from io import BytesIO, StringIO
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

from installer import qoder_audit_prompt_hook
from installer.client_hosts.hosts import qoder_prompt_hook
from installer.config import ShellError


class QoderAuditPromptHookTests(unittest.TestCase):
    def _run(self, payload: object) -> dict[str, object]:
        source = BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        destination = StringIO()
        self.assertEqual(qoder_audit_prompt_hook.main(source, destination), 0)
        return json.loads(destination.getvalue())

    def test_explicit_chinese_and_english_audits_route_before_other_actions(self):
        for prompt in (
            "审计一个随机主题",
            "请深度审核这个设计",
            "audit a random topic",
            "Please thoroughly audit this plan",
        ):
            with self.subTest(prompt=prompt):
                output = self._run(
                    {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
                )
                block = output["hookSpecificOutput"]
                self.assertEqual(block["hookEventName"], "UserPromptSubmit")
                context = block["additionalContext"]
                self.assertIn("Invoke Skill `audit` now as the first action", context)
                self.assertIn("exact request unchanged", context)
                self.assertIn("Do not inspect the workspace", context)
                self.assertIn("`audit-brainstorming`", context)

    def test_audit_route_injects_qoder_mcp_wrapper_contract(self):
        output = self._run(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "audit a random topic",
            }
        )

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("generic `mcp_call` tool", context)
        self.assertIn(
            '"toolName":"mcp__decision_engine__audit_skill_submit"', context
        )
        self.assertIn('"arguments":{"skill_name":"audit","args":', context)
        self.assertIn("Do not call any `mcp__decision_engine__*` name directly", context)
        self.assertIn("Never call `activation_required`", context)
        self.assertIn("After any rejected or failed submission, stop", context)
        self.assertIn("non-empty `local_id`", context)
        self.assertIn(
            '"toolName":"mcp__decision_engine__audit_skill_complete"', context
        )
        self.assertIn(
            '"arguments":{"local_id":"<returned local_* id>",'
            '"status":"completed"}',
            context,
        )
        self.assertIn("Do not invoke or invent an AQG MCP tool", context)
        self.assertIn("only permitted MCP target names", context)

    def test_non_audit_prompts_and_invalid_events_add_no_context(self):
        for payload in (
            {"hook_event_name": "UserPromptSubmit", "prompt": "写一个排序函数"},
            {"hook_event_name": "UserPromptSubmit", "prompt": "不要审计这个方案"},
            {"hook_event_name": "PreToolUse", "prompt": "audit this plan"},
            {"hook_event_name": "UserPromptSubmit", "prompt": 42},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self._run(payload), {})

    def test_malformed_or_oversized_input_fails_open_without_echoing_input(self):
        for raw in (b"not-json", b"{" + b"x" * (64 * 1024)):
            with self.subTest(size=len(raw)):
                destination = StringIO()
                self.assertEqual(
                    qoder_audit_prompt_hook.main(BytesIO(raw), destination), 0
                )
                self.assertEqual(json.loads(destination.getvalue()), {})

    def test_hook_script_runs_from_a_hostile_working_directory(self):
        script = Path(qoder_audit_prompt_hook.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, str(script)],
                cwd=tmp,
                input=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "audit this rule",
                    }
                ).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        output = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit"
        )


class QoderAuditPromptHookInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "decision-engine"
        self.script = self.root / "installer" / "qoder_audit_prompt_hook.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# hook\n", encoding="utf-8")
        self.settings = Path(self.tmp.name) / ".qoder-cn" / "settings.json"
        self.settings.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _aqg_group() -> dict[str, object]:
        return {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "/usr/bin/python3 /opt/aqg/qoder_hook_adapter.py",
                }
            ],
        }

    def _managed_groups(self, data: dict[str, object]) -> list[dict[str, object]]:
        groups = data["hooks"]["UserPromptSubmit"]
        return [
            group
            for group in groups
            if any(
                hook.get("name") == qoder_prompt_hook.MANAGED_HOOK_NAME
                for hook in group.get("hooks", [])
            )
        ]

    def test_install_preserves_aqg_and_user_hooks_and_migrates_experiment(self):
        aqg = self._aqg_group()
        user = {
            "matcher": "custom",
            "hooks": [{"type": "command", "command": "/opt/user/hook"}],
        }
        experiment = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "/usr/bin/python3",
                    "args": [str(self.script)],
                    "name": qoder_prompt_hook.LEGACY_EXPERIMENT_NAME,
                }
            ],
        }
        self.settings.write_text(
            json.dumps(
                {"hooks": {"UserPromptSubmit": [aqg, user, experiment]}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = qoder_prompt_hook.install_hook(
            self.settings,
            de_root=self.root,
            python_executable="/usr/bin/python3",
        )
        first = self.settings.read_bytes()
        second = qoder_prompt_hook.install_hook(
            self.settings,
            de_root=self.root,
            python_executable="/usr/bin/python3",
        )

        data = json.loads(first.decode("utf-8"))
        groups = data["hooks"]["UserPromptSubmit"]
        self.assertIn(aqg, groups)
        self.assertIn(user, groups)
        self.assertEqual(len(self._managed_groups(data)), 1)
        managed = self._managed_groups(data)[0]["hooks"][0]
        self.assertEqual(managed["command"], "/usr/bin/python3")
        self.assertEqual(managed["args"], [str(self.script)])
        self.assertNotIn(qoder_prompt_hook.LEGACY_EXPERIMENT_NAME, first.decode())
        self.assertEqual(result["action"], "updated")
        self.assertEqual(second["action"], "unchanged")
        self.assertEqual(self.settings.read_bytes(), first)

    def test_same_managed_name_with_foreign_command_is_preserved_and_blocks(self):
        foreign = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "/opt/foreign/hook",
                    "name": qoder_prompt_hook.MANAGED_HOOK_NAME,
                }
            ],
        }
        self.settings.write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [foreign]}}),
            encoding="utf-8",
        )
        before = self.settings.read_bytes()

        with self.assertRaisesRegex(ShellError, "same_name_unowned"):
            qoder_prompt_hook.install_hook(
                self.settings,
                de_root=self.root,
                python_executable="/usr/bin/python3",
            )

        self.assertEqual(self.settings.read_bytes(), before)

    def test_remove_owned_hook_preserves_aqg_and_user_hooks(self):
        aqg = self._aqg_group()
        user = {
            "matcher": "custom",
            "hooks": [{"type": "command", "command": "/opt/user/hook"}],
        }
        self.settings.write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [aqg, user]}}),
            encoding="utf-8",
        )
        qoder_prompt_hook.install_hook(
            self.settings,
            de_root=self.root,
            python_executable="/usr/bin/python3",
        )

        result = qoder_prompt_hook.remove_owned_hook(self.settings)

        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"]["UserPromptSubmit"], [aqg, user])
        self.assertEqual(result["action"], "removed")

    def test_dry_run_reports_update_without_mutating_settings(self):
        self.settings.write_text("{}\n", encoding="utf-8")
        before = self.settings.read_bytes()

        result = qoder_prompt_hook.install_hook(
            self.settings,
            de_root=self.root,
            python_executable="/usr/bin/python3",
            dry_run=True,
        )

        self.assertEqual(result["action"], "updated (dry-run)")
        self.assertEqual(self.settings.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
