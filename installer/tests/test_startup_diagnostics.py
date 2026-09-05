from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from installer import launcher, shim, startup_diagnostics


class StartupDiagnosticLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "decision-engine"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.log_path = self.root / startup_diagnostics.STARTUP_LOG_RELATIVE_PATH

    def _entries(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_log_contains_only_bounded_classification_fields(self) -> None:
        secret = "PRIVATE-ENDPOINT-OR-TOKEN"
        startup_diagnostics.append_event(
            self.root,
            "launcher_failed",
            "launcher",
            client_host=secret,
            outcome="error",
            reason_code=secret,
            error_type=secret,
        )

        raw = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        entry = json.loads(raw)
        self.assertEqual(entry["client_host"], "provided")
        self.assertEqual(entry["reason_code"], "unknown")
        self.assertEqual(entry["error_type"], "other")
        self.assertNotIn("endpoint", entry)
        self.assertNotIn("token", entry)
        self.assertNotIn("message", entry)

    def test_log_is_reset_before_crossing_its_size_bound(self) -> None:
        with mock.patch.object(startup_diagnostics, "MAX_STARTUP_LOG_BYTES", 1):
            startup_diagnostics.append_event(
                self.root, "launcher_started", "launcher", outcome="started"
            )
            startup_diagnostics.append_event(
                self.root, "launcher_exited", "launcher", outcome="clean_exit"
            )

        entries = self._entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "launcher_exited")

    def test_concurrent_writers_never_interleave_json_lines(self) -> None:
        threads = [
            threading.Thread(
                target=startup_diagnostics.append_event,
                args=(self.root, "launcher_started", "launcher"),
                kwargs={"attempt": number + 1, "outcome": "started"},
            )
            for number in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        entries = self._entries()
        self.assertGreaterEqual(len(entries), 1)
        self.assertTrue(all(entry["event"] == "launcher_started" for entry in entries))


class StartupFaultInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "decision-engine"
        self.root.mkdir()
        (self.root / ".git").mkdir()

    def test_pre_initialize_failure_then_retry_success_is_distinguishable(self) -> None:
        class SuccessfulForwarder(shim.Forwarder):
            def __init__(self) -> None:
                super().__init__("https://hub.example.com", "not-logged")

            def forward(self, message, *, timeout_s=None):
                return {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "fixture", "version": "1"},
                        "capabilities": {},
                    },
                }

        @contextlib.contextmanager
        def lease(*_args, **_kwargs):
            yield mock.sentinel.lease

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "codex-mcp-client", "version": "1"},
            },
        }
        stdout = io.StringIO()
        stdin = io.StringIO(json.dumps(initialize) + "\n")
        with (
            mock.patch.object(launcher.managed_install, "_require_fixed_managed_root"),
            mock.patch.object(
                launcher.config,
                "harden_existing_windows_config",
                side_effect=[
                    launcher.ShellError("injected Windows ACL hardening failure"),
                    True,
                ],
            ),
            mock.patch.object(launcher, "_head_commit", return_value="1" * 40),
            mock.patch.object(launcher, "_version", return_value="0.2.3"),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(launcher, "_repair_codex_skill_routes", return_value=False),
            mock.patch.object(launcher, "_load_candidate_shim", return_value=shim),
            mock.patch.object(
                launcher.update_coordination, "shim_session_lease", side_effect=lease
            ),
            mock.patch.object(launcher.update_transaction, "mark_running_release"),
            mock.patch.object(
                shim.Forwarder,
                "from_config",
                return_value=SuccessfulForwarder(),
            ),
            mock.patch.object(shim, "hub_reachable", return_value=True),
            mock.patch.object(shim.sys, "stdin", stdin),
            mock.patch.object(shim.sys, "stdout", stdout),
        ):
            first = launcher.main(
                ["--managed-root", str(self.root), "--serve-only", "--client-host", "codex"]
            )
            second = launcher.main(
                ["--managed-root", str(self.root), "--serve-only", "--client-host", "codex"]
            )

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        entries = [
            json.loads(line)
            for line in (
                self.root / startup_diagnostics.STARTUP_LOG_RELATIVE_PATH
            ).read_text(encoding="utf-8").splitlines()
        ]
        events = [entry["event"] for entry in entries]
        failed_index = events.index("launcher_failed")
        self.assertNotIn("initialize_received", events[:failed_index])
        self.assertEqual(entries[failed_index]["phase"], "config")
        self.assertEqual(entries[failed_index]["reason_code"], "shell_error")
        self.assertIn("initialize_received", events[failed_index + 1 :])
        self.assertIn("initialize_response_emitted", events[failed_index + 1 :])
        response_event = next(
            entry
            for entry in entries[failed_index + 1 :]
            if entry["event"] == "initialize_response_emitted"
        )
        self.assertEqual(response_event["outcome"], "success")
        self.assertNotIn("hub.example.com", self.root.joinpath(
            startup_diagnostics.STARTUP_LOG_RELATIVE_PATH
        ).read_text(encoding="utf-8"))
        self.assertIn('"result"', stdout.getvalue())

    def test_hub_unreachable_still_completes_initialize_locally(self) -> None:
        events = []
        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"},
                }
            )
            + "\n"
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(shim, "hub_reachable", return_value=False),
            mock.patch.object(
                shim.Forwarder,
                "from_config",
                side_effect=shim.ActivationRequiredError("fixture"),
            ),
        ):
            result = shim.serve(
                shim.Forwarder("https://hub.example.com", "not-logged"),
                stdin=stdin,
                stdout=stdout,
                on_startup_event=lambda event, phase, **fields: events.append(
                    {"event": event, "phase": phase, **fields}
                ),
            )

        self.assertEqual(result, 0)
        response = json.loads(stdout.getvalue())
        self.assertIn("result", response)
        emitted = next(
            event for event in events if event["event"] == "initialize_response_emitted"
        )
        self.assertEqual(emitted["forwarder"], "offline")
        self.assertEqual(emitted["outcome"], "success")

    def test_activation_reason_distinguishes_missing_config_from_missing_token(self) -> None:
        config_path = self.root / "config.json"
        with mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(config_path)}, clear=False):
            with self.assertRaises(shim.ActivationRequiredError) as missing:
                shim.Forwarder.from_config()
            self.assertEqual(missing.exception.reason_code, "config_missing")

            config_path.write_text(
                json.dumps({"server_endpoint": "https://hub.example.com"}),
                encoding="utf-8",
            )
            with self.assertRaises(shim.ActivationRequiredError) as token:
                shim.Forwarder.from_config()
            self.assertEqual(token.exception.reason_code, "missing_access_token")


if __name__ == "__main__":
    unittest.main()
