"""Focused regressions for Stopper DEBUG model disclosure."""

from __future__ import annotations

import unittest
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from client import runner


SOURCE = (
    Path(__file__).resolve().parents[2]
    / "desktop"
    / "macos"
    / "DecisionEngineStopper.swift"
).read_text(encoding="utf-8")


class StopperDebugAuthorizedTests(unittest.TestCase):
    def test_active_run_payload_preserves_debug_authorized(self):
        payload = runner.active_run_payload(
            {
                "run_id": "r1",
                "status": "running",
                "debug_authorized": True,
                "auditors": [{"status": "running", "model_id": "gpt-5.6-sol"}],
            }
        )
        self.assertIs(payload["debug_authorized"], True)
        self.assertIn("auditors", payload)

    def test_save_active_run_persists_debug_authorized(self):
        with mock.patch.object(
            runner, "config_path", return_value=Path("config.json")
        ), mock.patch.dict(runner.os.environ, {}, clear=True), mock.patch.object(
            runner, "_write_active_runs_registry"
        ) as write_registry, mock.patch.object(
            runner, "_write_active_run_payload"
        ) as write_payload:
            runner.save_active_run(
                {
                    "run_id": "r1",
                    "status": "running",
                    "debug_authorized": True,
                    "auditors": [{"status": "running", "model_id": "gpt-5.6-sol"}],
                }
            )

        written = write_registry.call_args.args[0]["runs"]["r1"]
        self.assertIs(written["debug_authorized"], True)
        self.assertIs(write_payload.call_args.args[0]["debug_authorized"], True)

    def test_macos_merge_preserves_debug_authorized(self):
        self.assertIn(
            'updated["debug_authorized"] = payload["debug_authorized"] as? Bool',
            SOURCE,
        )


if __name__ == "__main__":
    unittest.main()
