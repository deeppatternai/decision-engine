"""Behavior locks for WorkBuddy AI deterministic audit routing."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO, StringIO
from pathlib import Path

from installer import workbuddy_audit_prompt_hook
from installer.client_hosts.hosts import workbuddy_ai_prompt_hook
from installer.config import ShellError


class WorkBuddyAuditPromptHookTestCase(unittest.TestCase):
    def _run(self, payload: object) -> dict[str, object]:
        source = BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        destination = StringIO()
        self.assertEqual(workbuddy_audit_prompt_hook.main(source, destination), 0)
        return json.loads(destination.getvalue())

    def test_explicit_chinese_and_english_audits_route_to_direct_de_tools(self):
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
                context = output["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Invoke Skill `audit` now as the first action", context)
                self.assertIn("exact request unchanged", context)
                self.assertIn("logical name `audit_skill_submit`", context)
                self.assertIn("Submit exactly once", context)
                self.assertIn("non-empty `local_id`", context)
                self.assertIn("logical name `audit_skill_complete`", context)
                self.assertIn("Attempt completion exactly once", context)
                self.assertIn("If completion is rejected or fails, stop", context)
                self.assertIn("Never call `activation_required`", context)
                self.assertNotIn("generic `mcp_call`", context)

    def test_non_audit_prompts_add_no_context(self):
        for payload in (
            {"hook_event_name": "UserPromptSubmit", "prompt": "写一个排序函数"},
            {"hook_event_name": "UserPromptSubmit", "prompt": "不要审计这个方案"},
            {"hook_event_name": "PreToolUse", "prompt": "audit this plan"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self._run(payload), {})

    def test_hook_script_runs_from_a_hostile_working_directory(self):
        script = Path(workbuddy_audit_prompt_hook.__file__).resolve()
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


class WorkBuddyAuditPromptHookInstallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "decision-engine"
        script = self.root / "installer" / "workbuddy_audit_prompt_hook.py"
        script.parent.mkdir(parents=True)
        script.write_text("# hook\n", encoding="utf-8")
        self.settings = Path(self.tmp.name) / ".workbuddy-ai" / "settings.json"
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
                    "command": "/usr/bin/python3 /opt/aqg/hook.py userPromptSubmit",
                }
            ],
        }

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
        workbuddy_ai_prompt_hook.install_hook(
            self.settings,
            de_root=self.root,
            python_executable="/usr/bin/python3",
        )

        result = workbuddy_ai_prompt_hook.remove_owned_hook(self.settings)

        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"]["UserPromptSubmit"], [aqg, user])
        self.assertEqual(result["action"], "removed")

    def test_same_managed_id_with_foreign_command_is_preserved_and_blocks(self):
        foreign = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        "/opt/foreign/hook --managed-id "
                        + workbuddy_ai_prompt_hook.MANAGED_ID
                    ),
                    "timeout": 30,
                }
            ],
        }
        self.settings.write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [foreign]}}),
            encoding="utf-8",
        )
        before = self.settings.read_bytes()

        with self.assertRaisesRegex(ShellError, "same_id_unowned"):
            workbuddy_ai_prompt_hook.install_hook(
                self.settings,
                de_root=self.root,
                python_executable="/usr/bin/python3",
            )

        self.assertEqual(self.settings.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
