"""Behavior locks for user-facing installer completion guidance."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class InstallCompletionGuidanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.posix = (ROOT / "dp-install.sh").read_text(encoding="utf-8")
        cls.windows = (ROOT / "dp-install.ps1").read_text(encoding="utf-8")

    def test_success_explains_restart_and_runtime_verification(self) -> None:
        guidance = (
            "Deep Pattern installation is complete. Fully restart each configured "
            "Agent application, then open a new session and verify that the "
            "integration works."
        )
        self.assertIn(guidance, self.posix)
        self.assertIn(guidance, self.windows)
        self.assertIn(
            "Deep Pattern installation is complete and ready to use. Open a new "
            "Agent session and verify that the integration works.",
            self.windows,
        )

    def test_partial_host_result_explains_usable_and_incomplete_integrations(self) -> None:
        guidance = (
            "Deep Pattern is ready to use with each successfully configured Agent "
            "integration. Fully restart those Agent applications, then open a new "
            "session and verify that the integration works; review the items above "
            "for any integration that still needs attention."
        )
        self.assertIn(guidance, self.posix)
        self.assertIn(guidance, self.windows)

    def test_deferred_update_remains_honest(self) -> None:
        deferred = (
            "The current verified Deep Pattern installation remains available. "
            "Fully restart the configured Agent applications, then open a new "
            "session to use the available integration; rerun this installer after "
            "completing activation and the pending update."
        )
        self.assertIn(deferred, self.posix)
        self.assertIn(deferred, self.windows)

    def test_machine_readable_outcomes_are_preserved(self) -> None:
        self.assertIn("PASS: all selected supported hosts are configured on disk", self.posix)
        self.assertIn("exit \"$EXIT_PARTIAL\"", self.posix)
        self.assertIn("PASS: installation completed; runtime verification requires host restart", self.windows)
        self.assertIn("exit $ExitPartial", self.windows)


if __name__ == "__main__":
    unittest.main()
