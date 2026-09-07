"""Behavior locks for TRAE Code CN deterministic audit routing."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO, StringIO
from pathlib import Path

from installer import trae_cn_audit_prompt_hook


class TraeCnAuditPromptHookTestCase(unittest.TestCase):
    def _run(self, payload: object) -> dict[str, object]:
        source = BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        destination = StringIO()
        self.assertEqual(trae_cn_audit_prompt_hook.main(source, destination), 0)
        return json.loads(destination.getvalue())

    def test_explicit_chinese_and_english_audits_route_to_direct_de_tools(self):
        for prompt in (
            "审计一个固定主题",
            "请深度审核这个设计",
            "audit this fixed topic",
            "Please thoroughly audit this plan",
        ):
            with self.subTest(prompt=prompt):
                output = self._run(
                    {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
                )
                context = output["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Invoke Skill `audit` now as the first action", context)
                self.assertIn("logical name `audit_skill_submit`", context)
                self.assertIn("Submit exactly once", context)
                self.assertIn("logical name `audit_skill_complete`", context)

    def test_ordinary_chat_adds_no_context(self):
        self.assertEqual(
            self._run(
                {"hook_event_name": "UserPromptSubmit", "prompt": "写一个排序函数"}
            ),
            {},
        )

    def test_hook_script_runs_from_an_unrelated_working_directory(self):
        script = Path(trae_cn_audit_prompt_hook.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, str(script)],
                cwd=tmp,
                input=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "audit this fixed topic",
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


if __name__ == "__main__":
    unittest.main()
