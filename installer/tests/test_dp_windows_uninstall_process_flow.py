#!/usr/bin/env python3
"""Behavior locks for Windows process-only uninstall guidance."""

from __future__ import annotations

import contextlib
import io
import types
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "dp-uninstall.ps1"


def load_embedded_helper() -> dict[str, object]:
    source = SCRIPT.read_text(encoding="utf-8")
    helper = source.split("$helperSource = @'\n", 1)[1].split("\n'@\n", 1)[0]
    namespace: dict[str, object] = {"__name__": __name__}
    exec(compile(helper, str(SCRIPT), "exec"), namespace)
    return namespace


class WindowsProcessGuidanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_embedded_helper()

    def test_mixed_managed_launcher_and_unknown_wrapper_are_guidable(self) -> None:
        blockers = [
            "live managed DE MCP process must be stopped before uninstall: pid=100",
            "live DE launcher process ownership is unknown: pid=99",
        ]
        self.assertTrue(
            self.module["has_only_guidable_process_blockers"](blockers)
        )

    def test_non_process_ownership_blocker_remains_fail_closed(self) -> None:
        blockers = [
            "live managed DE MCP process must be stopped before uninstall: pid=100",
            "Decision Engine root ownership cannot be proven",
        ]
        self.assertFalse(
            self.module["has_only_guidable_process_blockers"](blockers)
        )

    def test_apply_plan_announces_guidance_instead_of_stop(self) -> None:
        inventory = types.SimpleNamespace(
            scope="both",
            actions=[],
            notes=[],
            blockers=[
                "live DE launcher process ownership is unknown: pid=99",
            ],
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.module["print_inventory"](inventory, True)
        rendered = output.getvalue()
        self.assertIn("ACTION REQUIRED:", rendered)
        self.assertNotIn("STOP:", rendered)

    def test_dry_run_never_claims_that_process_cleanup_will_run(self) -> None:
        inventory = types.SimpleNamespace(
            scope="both",
            actions=[],
            notes=[],
            blockers=[
                "live DE launcher process ownership is unknown: pid=99",
            ],
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.module["print_inventory"](inventory, False)
        rendered = output.getvalue()
        self.assertIn("STOP:", rendered)
        self.assertNotIn("ACTION REQUIRED:", rendered)


if __name__ == "__main__":
    unittest.main()
