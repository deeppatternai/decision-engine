#!/usr/bin/env python3
"""Behavior locks for deferred active-session updates in dp-install.sh."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "dp-install.sh"


def shell_function(source: str, name: str) -> str:
    start = re.search(rf"(?m)^{re.escape(name)}\(\) \{{\n", source)
    if start is None:
        return ""
    next_function = re.search(
        r"(?m)^[A-Za-z_][A-Za-z0-9_]*\(\) \{\n", source[start.end() :]
    )
    end = len(source) if next_function is None else start.end() + next_function.start()
    return source[start.start() : end]


class DeferredActiveSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def parse_update_result(self, value: str) -> subprocess.CompletedProcess[str]:
        function = shell_function(self.source, "parse_managed_update_result")
        self.assertTrue(function)
        harness = "".join(
            (
                "set -euo pipefail\n",
                function,
                'parse_managed_update_result "$1"\n',
                'printf "%s|%s\\n" "$managed_update_status" "$managed_update_blocker_pids"\n',
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", harness, "test", value],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def test_parses_deferred_status_and_blocker_pids(self) -> None:
        result = self.parse_update_result("deferred_active_session\t82352,84204")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "deferred_active_session|82352,84204\n")

    def test_rejects_untrusted_blocker_pid_text(self) -> None:
        result = self.parse_update_result("deferred_active_session\t82352;kill -9 1")
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_deferred_update_preserves_current_root_and_continues_setup(self) -> None:
        self.assertIn("de_update_deferred=1", self.source)
        self.assertIn('catalog_root="$MANAGED_ROOT"', self.source)
        self.assertIn(
            "setup will continue without stopping Agent applications", self.source
        )
        self.assertIn("SUCCESS_WITH_RESTART_REQUIRED", self.source)

    def test_deferred_update_requires_verified_unchanged_state(self) -> None:
        block = self.source[
            self.source.index("if ! run_managed_update_once; then") :
            self.source.index('catalog_root="$source_root"')
        ]
        self.assertIn("validate_complete_managed_root", block)
        self.assertIn("managed_activation_state", block)
        self.assertIn("managed_config_digest", block)
        self.assertIn("deferred_active_session", block)

    def test_installer_does_not_offer_to_terminate_active_sessions(self) -> None:
        self.assertNotIn("offer_term_for_managed_processes", self.source)
        self.assertNotIn("Send TERM to", self.source)
        self.assertNotIn("After quitting them, press Enter", self.source)

    def test_chatgpt_codex_process_is_identified(self) -> None:
        function = shell_function(self.source, "likely_host_for_pid")
        self.assertTrue(function)
        harness = "".join(
            (
                "set -euo pipefail\n",
                "process_field() {\n",
                "  case \"$1:$2\" in\n",
                "    50512:command) printf '%s\\n' '/Users/test/.deeppattern/de-python/bin/python3' ;;\n",
                "    50512:ppid) printf '%s\\n' '8981' ;;\n",
                "    8981:command) printf '%s\\n' '/Applications/ChatGPT.app/Contents/Resources/codex' ;;\n",
                "    8981:ppid) printf '%s\\n' '1' ;;\n",
                "    *) return 1 ;;\n",
                "  esac\n",
                "}\n",
                function,
                "likely_host_for_pid 50512\n",
            )
        )
        result = subprocess.run(
            ["/bin/bash", "-c", harness],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "Codex (ChatGPT.app)\n")


if __name__ == "__main__":
    unittest.main()
