"""Behavior tests for the MCP forwarding shim (installer.shim).

The shim is transport-only, so tests focus on: config resolution / failure
modes, request-vs-notification handling, verbatim forwarding, and error mapping.
No real network is used — a fake forwarder records what it was asked to send.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import pathlib
import re
import ssl
import sys
import tempfile
import threading
import types
import socket
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler

from client import runner
from client.http_safety import NoRedirect
from installer import config, mcp_config, shim
from installer.client_host_runtime import CapabilityStage, PreflightResult, TransportCapabilityGate


class SelfUpdateTestCase(unittest.TestCase):
    def test_clean_checkout_runs_ff_only_pull_with_safe_kwargs(self):
        root = Path("/some/clone")
        with mock.patch.object(shim.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"")   # clean status, then pull
            shim._self_update_once(root)
        # first call = status --porcelain, last = the ff-only pull
        self.assertIn("status", run.call_args_list[0].args[0])
        pull = run.call_args_list[-1]
        self.assertEqual(pull.args[0][:3], ["git", "-C", str(root)])
        self.assertIn("--ff-only", pull.args[0])
        self.assertIn("pull", pull.args[0])
        # load-bearing safety kwargs: timeout, silenced stdio, sanitized non-interactive env
        self.assertEqual(pull.kwargs["timeout"], 30)
        self.assertEqual(pull.kwargs["stdout"], shim.subprocess.DEVNULL)
        self.assertEqual(pull.kwargs["stdin"], shim.subprocess.DEVNULL)
        self.assertTrue(pull.kwargs["start_new_session"])
        self.assertEqual(pull.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("GIT_DIR", pull.kwargs["env"])   # inherited GIT_* stripped

    def test_dirty_checkout_is_not_pulled(self):
        with mock.patch.object(shim.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b" M installer/shim.py\n")  # dirty
            shim._self_update_once(Path("/x"))
        self.assertEqual(len(run.call_args_list), 1)   # only status; NO pull on a dirty tree
        self.assertIn("status", run.call_args_list[0].args[0])

    def test_git_env_strips_inherited_git_vars(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "/evil/.git", "GIT_WORK_TREE": "/evil"}):
            env = shim._git_env()
        self.assertNotIn("GIT_DIR", env)          # a hostile GIT_DIR can't redirect the pull
        self.assertNotIn("GIT_WORK_TREE", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_self_update_swallows_all_errors(self):
        with mock.patch.object(shim.subprocess, "run", side_effect=OSError("git missing")):
            shim._self_update_once(Path("/x"))     # must not raise — best-effort

    def test_opt_out_only_on_affirmative_value(self):
        for val, disabled in (("1", True), ("true", True), ("YES", True), ("on", True),
                              ("0", False), ("false", False), ("", False)):
            with mock.patch.dict(os.environ, {"DE_NO_AUTOUPDATE": val}):
                self.assertEqual(shim._autoupdate_disabled(), disabled, "DE_NO_AUTOUPDATE=%r" % val)

    def test_maybe_self_update_opts_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            with mock.patch.dict(os.environ, {"DE_NO_AUTOUPDATE": "1"}):
                with mock.patch.object(shim.threading, "Thread") as thread:
                    shim._maybe_self_update(Path(tmp))
            thread.assert_not_called()

    def test_maybe_self_update_skips_a_non_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:   # no .git
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DE_NO_AUTOUPDATE", None)
                with mock.patch.object(shim.threading, "Thread") as thread:
                    shim._maybe_self_update(Path(tmp))
            thread.assert_not_called()

    def test_maybe_self_update_accepts_a_dotgit_file_worktree(self):
        # a linked worktree / submodule stores .git as a FILE, not a dir — must still update
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DE_NO_AUTOUPDATE", None)
                with mock.patch.object(shim.threading, "Thread") as thread:
                    shim._maybe_self_update(Path(tmp))
            thread.assert_called_once()

    def test_maybe_self_update_spawns_a_daemon_thread_for_a_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DE_NO_AUTOUPDATE", None)
                with mock.patch.object(shim.threading, "Thread") as thread:
                    shim._maybe_self_update(Path(tmp))
            thread.assert_called_once()
            self.assertTrue(thread.call_args.kwargs.get("daemon"))   # never blocks shutdown
            thread.return_value.start.assert_called_once()


class FakeForwarder:
    def __init__(self, responses=None, raise_on=None):
        self.sent = []
        self._responses = responses or {}
        self._raise_on = raise_on or set()

    def forward(self, message):
        self.sent.append(message)
        method = message.get("method")
        if method in self._raise_on:
            raise config.ShellError("boom:%s" % method)
        return self._responses.get(method, {"jsonrpc": "2.0", "id": message.get("id"), "result": {}})


def _run(forwarder, lines, **serve_kwargs):
    stdin = io.StringIO("".join(l + "\n" for l in lines))
    stdout = io.StringIO()
    # Neutralize the launch self-update in transport tests — it would otherwise spawn a real
    # `git pull` from this checkout. Its own behavior is covered by SelfUpdateTestCase.
    with mock.patch.object(shim, "_maybe_self_update"), mock.patch.dict(
        os.environ, {"DE_SKIP_STOPPER_LAUNCH": "1"}
    ):
        shim.serve(forwarder, stdin=stdin, stdout=stdout, **serve_kwargs)
    out = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
    return out


class ShimServeTestCase(unittest.TestCase):
    class _Lease:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    def _ready_cursor_gate(self):
        gate = TransportCapabilityGate.conditional(
            followup=True, stopper=True
        )
        gate.complete_preflight(PreflightResult.ready(self._Lease()))
        return gate

    def test_activated_surface_advertises_and_handles_local_completion_without_forwarding(self):
        listing = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "audit_skill_submit", "inputSchema": {}}]},
        }
        fwd = FakeForwarder(responses={"tools/list": listing})
        listed = _run(
            fwd,
            [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})],
            client_host="codex",
        )[0]
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            [
                "audit_skill_submit", "audit_skill_complete", "open_ge_popup", "open_ge",
                "open_db_board", "db_board_result",
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "active-runs.json"
            with mock.patch.dict(os.environ, {"DE_ACTIVE_RUNS": str(registry)}):
                runner.save_local_advisory_run(
                    "local_activated", surface="de_lite", status="running"
                )
                fwd = FakeForwarder()
                response = _run(
                    fwd,
                    [json.dumps({
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "audit_skill_complete", "arguments": {
                            "local_id": "local_activated", "status": "partial"
                        }},
                    })],
                    client_host="codex",
                )[0]
                self.assertEqual(fwd.sent, [])
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(payload["status"], "partial")

    def test_lite_audit_schema_declares_the_session_locale_hint(self):
        response = shim.LiteForwarder().forward({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        })
        audit_tool = next(
            tool for tool in response["result"]["tools"]
            if tool["name"] == "audit_skill_submit"
        )
        locale = audit_tool["inputSchema"]["properties"]["args"]["properties"]
        self.assertEqual(locale["ui_locale"], {
            "type": "string",
            "enum": ["zh-CN", "en-US"],
        })

    def test_lite_process_rechecks_activation_before_the_next_request(self):
        listing = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"tools": [{"name": "audit_skill_submit", "inputSchema": {}}]},
        }
        hosted = FakeForwarder(responses={"tools/list": listing})
        hosted.endpoint = "https://hub.example.test"
        with mock.patch.object(shim.Forwarder, "from_config", return_value=hosted), \
             mock.patch.object(shim, "hub_reachable", return_value=True):
            out = _run(
                shim.LiteForwarder(),
                [json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})],
                client_host="codex",
            )

        self.assertEqual([message.get("method") for message in hosted.sent], ["tools/list"])
        self.assertIn("audit_skill_complete", [tool["name"] for tool in out[0]["result"]["tools"]])

    def _degraded_refresh(self, *, offline=True, reachable=False):
        """Drive one _refresh_degraded_forwarder call with a stubbed hub + probe."""
        current = mock.Mock()
        current.endpoint = "https://hub.example"
        probe = mock.Mock(return_value=reachable)
        with (
            mock.patch.object(shim.Forwarder, "from_config", return_value=current),
            mock.patch.object(shim, "hub_reachable", probe),
        ):
            result = shim._refresh_degraded_forwarder(shim.OfflineForwarder(), False, offline)
        return result, probe

    def test_a_degraded_process_does_not_pay_a_probe_on_every_single_message(self):
        """The refresh runs before EVERY message. On a dead network each probe costs up to
        PROBE_TIMEOUT_S, so an offline session was charging the user 3s per message to re-ask a
        question whose answer had not changed. Recovery is not latency-sensitive; the probe is."""
        with (
            mock.patch.object(shim, "_last_degraded_probe_s", None),
            mock.patch.object(shim.time, "monotonic", side_effect=[100.0, 100.5, 101.0]),
        ):
            _first, first_probe = self._degraded_refresh()
            _second, second_probe = self._degraded_refresh()
            _third, third_probe = self._degraded_refresh()

        self.assertEqual(first_probe.call_count, 1, "the first message must still probe")
        self.assertEqual(second_probe.call_count, 0, "a probe 0.5s later answers nothing new")
        self.assertEqual(third_probe.call_count, 0)

    def test_recovery_is_still_noticed_once_the_interval_has_passed(self):
        """Rate-limiting must not turn into never-recovering: past the window the probe resumes,
        and a reachable hub still promotes the process back to its normal surface."""
        with (
            mock.patch.object(shim, "_last_degraded_probe_s", None),
            mock.patch.object(
                shim.time, "monotonic",
                side_effect=[200.0, 200.0 + shim._DEGRADED_REPROBE_INTERVAL_S + 0.1],
            ),
        ):
            _first, first_probe = self._degraded_refresh()
            (forwarder, lite, offline), second_probe = self._degraded_refresh(reachable=True)

        self.assertEqual(first_probe.call_count, 1)
        self.assertEqual(second_probe.call_count, 1, "the window must expire, not latch")
        self.assertFalse(lite)
        self.assertFalse(offline, "a reachable hub must restore the normal surface")

    def test_forced_refresh_bypasses_the_degraded_probe_window(self):
        """Lifecycle and explicit audit requests must not inherit a stale Lite surface."""
        hosted = FakeForwarder()
        hosted.endpoint = "https://hub.example.test"
        with (
            mock.patch.object(shim, "_last_degraded_probe_s", 100.0),
            mock.patch.object(shim.time, "monotonic", return_value=100.5),
            mock.patch.object(shim.Forwarder, "from_config", return_value=hosted) as load,
            mock.patch.object(shim, "hub_reachable", return_value=True),
        ):
            result = shim._refresh_degraded_forwarder(
                shim.LiteForwarder(), True, False, force=True
            )

        load.assert_called_once()
        self.assertIs(result[0], hosted)
        self.assertEqual(result[1:], (False, False))

    def test_forced_refresh_does_not_replace_a_caller_owned_healthy_transport(self):
        caller_owned = FakeForwarder()
        with mock.patch.object(shim.Forwarder, "from_config") as load:
            result = shim._refresh_degraded_forwarder(
                caller_owned, False, False, force=True
            )

        load.assert_not_called()
        self.assertIs(result[0], caller_owned)

    def test_forced_refresh_keeps_an_authenticated_transport_on_transient_missing_config(self):
        """A live config-bound transport is activation evidence; a momentary empty config must
        not reroute an explicit hosted audit to the unactivated DE Lite surface."""
        hosted = shim.Forwarder("https://hub.example.test", "token")
        hosted._config_bound = True
        with mock.patch.object(
            shim.Forwarder,
            "from_config",
            side_effect=shim.ActivationRequiredError("temporarily missing config"),
        ):
            result = shim._refresh_degraded_forwarder(
                hosted, False, False, force=True
            )

        self.assertIs(result[0], hosted)
        self.assertEqual(result[1:], (False, False))

    def test_late_preflight_offline_error_enters_local_audit_only_for_explicit_submit(self):
        class FlappingForwarder(shim.Forwarder):
            def __init__(self):
                self.endpoint = "https://hub.example.test"

            def forward(self, message):
                raise shim.OfflineError(
                    "hub became unreachable before sending the request",
                    data={
                        "status": "service_unavailable",
                        "reason": "unreachable",
                        "retryable": True,
                        "request_sent": False,
                        "action": "reconnect",
                    },
                )

        request = {
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"title": "Explicit audit", "content": "artifact"},
                },
            },
        }
        with mock.patch.object(shim, "hub_reachable", return_value=True):
            response = _run(FlappingForwarder(), [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "service_unavailable")
        self.assertEqual(payload["payload"]["title"], "Explicit audit")

    def test_initialize_offline_error_fails_the_transport_capability_gate(self):
        class FlappingForwarder(shim.Forwarder):
            def __init__(self):
                self.endpoint = "https://hub.example.test"

            def forward(self, message):
                raise shim.OfflineError("hub became unreachable before sending the request")

        gate = mock.Mock()
        gate.stage = CapabilityStage.READY_FOR_INITIALIZE
        gate.display_enabled = False
        gate.followup_enabled = False
        gate.stopper_enabled = False
        request = {
            "jsonrpc": "2.0",
            "id": 92,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }

        with mock.patch.object(shim, "hub_reachable", return_value=True):
            response = _run(
                FlappingForwarder(),
                [json.dumps(request)],
                capability_gate=gate,
            )[0]

        self.assertEqual(response["error"]["code"], -32001)
        gate.fail_initialize.assert_called_once_with()

    def test_a_healthy_process_never_reaches_the_probe_at_all(self):
        """Unchanged fast path: this gate must cost a normal hosted request nothing."""
        probe = mock.Mock(return_value=True)
        with (
            mock.patch.object(shim, "_last_degraded_probe_s", None),
            mock.patch.object(shim, "hub_reachable", probe),
        ):
            sentinel = object()
            result = shim._refresh_degraded_forwarder(sentinel, False, False)

        self.assertEqual(probe.call_count, 0)
        self.assertIs(result[0], sentinel)

    def test_offline_process_rechecks_reachability_before_the_next_request(self):
        listing = {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {"tools": [{"name": "audit_skill_submit", "inputSchema": {}}]},
        }
        hosted = FakeForwarder(responses={"tools/list": listing})
        hosted.endpoint = "https://hub.example.test"
        with mock.patch.object(shim.Forwarder, "from_config", return_value=hosted), \
             mock.patch.object(shim, "hub_reachable", side_effect=[False, True]):
            out = _run(
                shim.Forwarder("https://example.invalid", "token"),
                [json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})],
                client_host="codex",
            )

        self.assertEqual([message.get("method") for message in hosted.sent], ["tools/list"])
        names = [tool["name"] for tool in out[0]["result"]["tools"]]
        self.assertNotIn("service_unavailable", names)
        self.assertIn("audit_skill_complete", names)

    def test_cursor_market_research_forwards_authoritative_client_host(self):
        call = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "de_market_research",
                "arguments": {
                    "topic": "synthetic",
                    "client_host": "spoofed-host",
                },
            },
        }
        fwd = FakeForwarder()

        _run(fwd, [json.dumps(call)], client_host="cursor")

        self.assertEqual(
            fwd.sent[0]["params"]["arguments"]["client_host"],
            "cursor",
        )

    def test_cursor_unified_market_research_forwards_authoritative_client_host(self):
        call = {
            "jsonrpc": "2.0",
            "id": 70,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit-market-research",
                    "client_host": "top-level-spoofed-host",
                    "args": {
                        "topic": "synthetic",
                        "client_host": "spoofed-host",
                    },
                },
            },
        }
        original = json.loads(json.dumps(call))
        injected = shim._with_client_host_metadata(call, "cursor")
        fwd = FakeForwarder()

        _run(fwd, [json.dumps(call)], client_host="cursor")

        self.assertEqual(call, original)
        self.assertIsNot(injected, call)
        self.assertIsNot(injected["params"], call["params"])
        self.assertIsNot(
            injected["params"]["arguments"],
            call["params"]["arguments"],
        )
        self.assertIsNot(
            injected["params"]["arguments"]["args"],
            call["params"]["arguments"]["args"],
        )
        self.assertEqual(
            injected["params"]["arguments"]["args"]["client_host"],
            "cursor",
        )
        expected = json.loads(json.dumps(original))
        expected["params"]["arguments"]["client_host"] = "cursor"
        expected["params"]["arguments"]["args"]["client_host"] = "cursor"
        self.assertEqual(injected, expected)
        self.assertEqual(fwd.sent, [expected])

    def test_cursor_unified_market_research_synthesizes_missing_args(self):
        call = {
            "jsonrpc": "2.0",
            "id": 71,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {"skill_name": "audit-market-research"},
            },
        }

        injected = shim._with_client_host_metadata(call, "cursor")

        self.assertEqual(
            injected["params"]["arguments"]["args"],
            {"client_host": "cursor"},
        )

    def test_cursor_unified_market_research_rejects_non_object_args(self):
        call = {
            "jsonrpc": "2.0",
            "id": 72,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit-market-research",
                    "args": ["not", "an", "object"],
                },
            },
        }
        fwd = FakeForwarder()

        with self.assertRaisesRegex(config.ShellError, "must be an object"):
            shim._with_client_host_metadata(call, "cursor")
        output = _run(fwd, [json.dumps(call)], client_host="cursor")

        self.assertEqual(fwd.sent, [])
        self.assertEqual(output[0]["error"]["code"], -32001)
        self.assertNotIn("not", json.dumps(output))

    def test_client_host_metadata_helper_copies_every_modified_mapping(self):
        call = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "de_market_research",
                "arguments": {"topic": "synthetic"},
            },
        }
        original = json.loads(json.dumps(call))

        injected = shim._with_client_host_metadata(call, "cursor")

        self.assertEqual(call, original)
        self.assertEqual(
            injected["params"]["arguments"]["client_host"],
            "cursor",
        )
        self.assertIsNot(injected, call)
        self.assertIsNot(injected["params"], call["params"])
        self.assertIsNot(
            injected["params"]["arguments"],
            call["params"]["arguments"],
        )

    def test_client_host_injection_is_cursor_market_research_only(self):
        market_call = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "de_market_research",
                "arguments": {"topic": "synthetic"},
            },
        }
        audit_call = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "de_audit",
                "arguments": {
                    "title": "Synthetic audit",
                    "artifact": "synthetic",
                },
            },
        }
        malformed_market_call = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "de_market_research",
                "arguments": ["not", "an", "object"],
            },
        }
        unified_audit_call = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"title": "Synthetic audit", "content": "synthetic"},
                },
            },
        }
        unified_market_call = {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit-market-research",
                    "args": {
                        "topic": "synthetic",
                        "client_host": "host-owned",
                    },
                },
            },
        }
        cases = (
            ("claude", market_call),
            ("codex", market_call),
            ("unknown-host", market_call),
            ("cursor", audit_call),
            ("cursor", malformed_market_call),
            ("cursor", unified_audit_call),
            ("claude", unified_market_call),
            ("codex", unified_market_call),
            ("unknown-host", unified_market_call),
        )
        for host, call in cases:
            with self.subTest(host=host, tool=call["params"]["name"]):
                fwd = FakeForwarder()
                expected = json.loads(json.dumps(call))

                _run(fwd, [json.dumps(call)], client_host=host)

                self.assertEqual(fwd.sent, [expected])

    def test_cursor_default_policy_uses_shared_static_local_ui_capabilities(self):
        gate = shim._capability_gate_for_host("cursor")

        self.assertTrue(gate.display_enabled)
        self.assertTrue(gate.followup_enabled)
        self.assertTrue(gate.stopper_enabled)

    def test_cursor_gate_publishes_display_before_following_tools_list(self):
        gate = self._ready_cursor_gate()
        fwd = FakeForwarder(
            responses={
                "initialize": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"serverInfo": {"name": "de"}},
                },
                "tools/list": {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": []},
                },
            }
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "Cursor", "version": "fixture"}},
        }
        tools_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        out = _run(
            fwd,
            [json.dumps(initialize), json.dumps(tools_list)],
            client_host="cursor",
            capability_gate=gate,
            initialize_matcher=lambda params: params["clientInfo"]["version"]
            == "fixture",
            volatile_display_check=lambda: shim.VolatileResult.available(),
        )

        names = {tool["name"] for tool in out[1]["result"]["tools"]}
        self.assertIn("open_ge_popup", names)
        self.assertIn("open_db_board", names)

    def test_workbuddy_identity_enables_display_without_popup_followup(self):
        fwd = FakeForwarder(
            responses={
                "initialize": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"serverInfo": {"name": "de"}},
                },
                "tools/list": {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": []},
                },
            }
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "connector:custom-mcp:decision-engine",
                    "version": "1.0.0",
                }
            },
        }
        tools_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }

        out = _run(
            fwd,
            [json.dumps(initialize), json.dumps(tools_list)],
            client_host="workbuddy",
        )

        names = {tool["name"] for tool in out[1]["result"]["tools"]}
        self.assertTrue(
            {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"}
            <= names
        )
        gate = shim._capability_gate_for_host("workbuddy")
        self.assertTrue(gate.display_enabled)
        self.assertFalse(gate.followup_enabled)
        self.assertTrue(gate.stopper_enabled)

    def test_workbuddy_unrecognised_client_name_still_enables_display(self):
        """WorkBuddy composes clientInfo.name from its own internal connector id, so a
        rename upstream must not silently strip the popup tools (covers: R1)."""
        for observed in (
            "custom-mcp:decision-engine",     # same id, no `connector:` prefix
            "workbuddy",
            "connector:custom-mcp:de",        # server registered under another key
        ):
            with self.subTest(observed=observed):
                fwd = FakeForwarder(
                    responses={
                        "initialize": {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {"serverInfo": {"name": "de"}},
                        },
                        "tools/list": {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {"tools": []},
                        },
                    }
                )
                initialize = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": observed, "version": "1.0.0"}
                    },
                }
                tools_list = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }

                out = _run(
                    fwd,
                    [json.dumps(initialize), json.dumps(tools_list)],
                    client_host="workbuddy",
                )

                names = {tool["name"] for tool in out[1]["result"]["tools"]}
                self.assertTrue(
                    {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"}
                    <= names,
                    "display tools stripped for clientInfo.name=%r" % observed,
                )

    def test_enforcing_host_still_strips_display_on_unrecognised_client_name(self):
        """Counterpart to the workbuddy case: dropping enforcement for ONE host must
        not disable the mechanism for the hosts that still declare it. Without this,
        deleting the whole `if identity_enforced` branch would keep the suite green."""
        self.assertTrue(
            mcp_config.CLIENT_SPECS["cursor"].require_observed_identity,
            "cursor must still enforce for this counterpart test to mean anything",
        )
        fwd = FakeForwarder(
            responses={
                "initialize": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"serverInfo": {"name": "de"}},
                },
                "tools/list": {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": []},
                },
            }
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "not-cursor-at-all", "version": "1.0.0"}
            },
        }
        tools_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }

        out = _run(
            fwd,
            [json.dumps(initialize), json.dumps(tools_list)],
            client_host="cursor",
        )

        names = {tool["name"] for tool in out[1]["result"]["tools"]}
        self.assertFalse(
            {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"} & names,
            "an enforcing host must not receive display tools on an identity mismatch",
        )

    def test_display_suppression_reason_names_the_failing_condition(self):
        """Each of the four conjuncts that can hide the display tools must be
        distinguishable by name, so a missing open_ge is diagnosable (covers: R3)."""
        self.assertIsNone(
            shim._display_suppression_reason(
                lite_mode=False,
                offline_mode=False,
                identity_enabled=True,
                display_enabled=True,
            )
        )
        cases = (
            ({"lite_mode": True}, "lite"),
            ({"offline_mode": True}, "offline"),
            ({"identity_enabled": False}, "identity"),
            ({"display_enabled": False}, "transport"),
        )
        for override, expected_token in cases:
            with self.subTest(**override):
                kwargs = {
                    "lite_mode": False,
                    "offline_mode": False,
                    "identity_enabled": True,
                    "display_enabled": True,
                }
                kwargs.update(override)
                reason = shim._display_suppression_reason(**kwargs)
                self.assertIsNotNone(reason)
                self.assertIn(expected_token, reason)

    def test_suppressed_display_tools_are_reported_on_stderr(self):
        """A display-capable host that receives no display tools must say why
        exactly once, instead of failing silently (covers: R3)."""
        fwd = FakeForwarder(
            responses={
                "initialize": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"serverInfo": {"name": "de"}},
                },
                "tools/list": {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": []},
                },
            }
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "definitely-not-cursor", "version": "1.0.0"}
            },
        }
        tools_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }

        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = _run(
                fwd,
                [json.dumps(initialize), json.dumps(tools_list)],
                client_host="cursor",
            )
        logged = err.getvalue()

        names = {tool["name"] for tool in out[1]["result"]["tools"]}
        self.assertNotIn("open_ge", names)
        self.assertIn("display tools withheld", logged)
        self.assertIn("identity", logged)
        self.assertEqual(logged.count("display tools withheld"), 1)

    def test_identity_mismatch_logs_the_name_the_host_actually_reported(self):
        """Adapting an alias to a renamed upstream host is impossible unless the
        rejected name itself is recoverable from the logs (covers: R4)."""
        fwd = FakeForwarder(
            responses={
                "initialize": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"serverInfo": {"name": "de"}},
                },
            }
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "custom-mcp:decision-engine",
                    "version": "1.0.0",
                }
            },
        }

        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            _run(fwd, [json.dumps(initialize)], client_host="cursor")

        self.assertIn("custom-mcp:decision-engine", err.getvalue())

    def test_qoder_identity_enables_display_without_popup_followup(self):
        fwd = FakeForwarder(
            responses={
                "initialize": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"serverInfo": {"name": "de"}},
                },
                "tools/list": {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": []},
                },
            }
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "mcphost", "version": "0.1.0"}
            },
        }
        tools_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }

        out = _run(
            fwd,
            [json.dumps(initialize), json.dumps(tools_list)],
            client_host="qoder",
        )

        names = {tool["name"] for tool in out[1]["result"]["tools"]}
        self.assertTrue(
            {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"}
            <= names
        )
        gate = shim._capability_gate_for_host("qoder")
        self.assertTrue(gate.display_enabled)
        self.assertFalse(gate.followup_enabled)
        self.assertTrue(gate.stopper_enabled)

    def test_qoder_ide_identities_advertise_all_local_display_tools(self):
        for client_host, observed_name in (
            ("qoder-ide", "Qoder"),
            ("qoder-cn-ide", "Qoder CN"),
        ):
            with self.subTest(client_host=client_host):
                fwd = FakeForwarder(
                    responses={
                        "initialize": {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {"serverInfo": {"name": "de"}},
                        },
                        "tools/list": {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {"tools": []},
                        },
                    }
                )
                initialize = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": observed_name,
                            "version": "1.28.0",
                        }
                    },
                }
                tools_list = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }

                out = _run(
                    fwd,
                    [json.dumps(initialize), json.dumps(tools_list)],
                    client_host=client_host,
                )

                names = {tool["name"] for tool in out[1]["result"]["tools"]}
                self.assertTrue(
                    {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"}
                    <= names
                )

    def test_qoder_cn_018_identity_advertises_all_local_display_tools(self):
        fwd = FakeForwarder(
            responses={
                "initialize": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"serverInfo": {"name": "de"}},
                },
                "tools/list": {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": []},
                },
            }
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "qoder-desktop-mcp-host",
                    "version": "1.0.0",
                }
            },
        }
        tools_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }

        out = _run(
            fwd,
            [json.dumps(initialize), json.dumps(tools_list)],
            client_host="qoder-cn",
        )

        names = {tool["name"] for tool in out[1]["result"]["tools"]}
        self.assertTrue(
            {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"}
            <= names
        )

    def test_qoder_cn_unidentified_initialize_keeps_lite_stopper_and_completion(self):
        class _SyncThread:
            def __init__(self, target=None, args=(), **_kwargs):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

            def is_alive(self):
                return False

            def join(self, timeout=None):
                return None

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {
                    "name": "qoder-cn-http-proxy",
                    "version": "0.1.6",
                },
            },
        }
        tools_list = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
        }
        submit = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"title": "Qoder CN audit", "content": "artifact"},
                },
            },
        }
        complete = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_complete",
                "arguments": {
                    "local_id": "local_qodercn",
                    "status": "completed",
                },
            },
        }
        second_submit = json.loads(json.dumps(submit))
        second_submit["id"] = 5
        second_submit["params"]["arguments"]["args"]["title"] = "Second Qoder CN audit"
        second_complete = json.loads(json.dumps(complete))
        second_complete["id"] = 6
        second_complete["params"]["arguments"]["local_id"] = "local_qodercn2"

        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "active-runs.json"
            stdin = io.StringIO(
                "".join(
                    json.dumps(message) + "\n"
                    for message in (
                        initialize,
                        tools_list,
                        submit,
                        complete,
                        second_submit,
                        second_complete,
                    )
                )
            )
            stdout = io.StringIO()
            with mock.patch.dict(
                os.environ, {"DE_ACTIVE_RUNS": str(registry)}, clear=False
            ), mock.patch.object(
                shim.Forwarder,
                "from_config",
                side_effect=shim.ActivationRequiredError("unactivated"),
            ), mock.patch.object(
                shim.secrets,
                "token_hex",
                side_effect=("qodercn", "qodercn2"),
            ), mock.patch.object(
                shim, "_maybe_self_update"
            ), mock.patch.object(
                shim.threading, "Thread", _SyncThread
            ), mock.patch(
                "client.runner.launch_stopper_if_available",
                side_effect=OSError("synthetic GUI launch failure"),
            ) as launch:
                os.environ.pop("DE_SKIP_STOPPER_LAUNCH", None)
                shim.serve(
                    shim.LiteForwarder(),
                    stdin=stdin,
                    stdout=stdout,
                    client_host="qoder-cn",
                )

            responses = [
                json.loads(line)
                for line in stdout.getvalue().splitlines()
                if line.strip()
            ]
            listed_names = {
                tool["name"] for tool in responses[1]["result"]["tools"]
            }
            self.assertNotIn("open_ge_popup", listed_names)
            self.assertNotIn("open_db_board", listed_names)
            self.assertNotIn("error", responses[3])
            completed = json.loads(
                responses[3]["result"]["content"][0]["text"]
            )
            self.assertEqual(completed["run_id"], "local_qodercn")
            self.assertEqual(completed["status"], "completed")
            self.assertNotIn("error", responses[4])
            second_completed = json.loads(
                responses[5]["result"]["content"][0]["text"]
            )
            self.assertEqual(second_completed["run_id"], "local_qodercn2")
            self.assertEqual(second_completed["status"], "completed")
            self.assertEqual(launch.call_count, 2)
            launch.assert_called_with(prefer_shipped=False)
            saved = json.loads(registry.read_text())["runs"]["local_qodercn"]
            self.assertEqual(saved["status"], "completed")
            second_saved = json.loads(registry.read_text())["runs"]["local_qodercn2"]
            self.assertEqual(second_saved["status"], "completed")

    def test_cursor_initialize_failure_closes_gate_and_preserves_forwarded_error(self):
        gate = self._ready_cursor_gate()
        fwd = FakeForwarder(raise_on={"initialize"})
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "Cursor", "version": "fixture"}},
        }
        out = _run(
            fwd,
            [json.dumps(initialize)],
            client_host="cursor",
            capability_gate=gate,
            initialize_matcher=lambda _params: True,
        )

        self.assertEqual(gate.reason, "initialize-failed")
        self.assertEqual(out[0]["error"]["code"], -32001)

    def test_cursor_eligible_display_enables_audit_stopper(self):
        gate = self._ready_cursor_gate()
        fwd = FakeForwarder(
            responses={
                "initialize": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {},
                },
                "tools/call": {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"run_id": "audit-1"},
                },
            }
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "Cursor", "version": "fixture"}},
        }
        audit_call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "submit_audit",
                "arguments": {"title": "Explicit audit"},
            },
        }
        with mock.patch.object(shim.threading, "Thread") as thread:
            _run(
                fwd,
                [json.dumps(initialize), json.dumps(audit_call)],
                client_host="cursor",
                capability_gate=gate,
                initialize_matcher=lambda _params: True,
            )
        stopper_threads = [
            call
            for call in thread.call_args_list
            if call.kwargs.get("target") == shim._spawn_stopper_for_audit
        ]
        self.assertEqual(len(stopper_threads), 1)

    def test_cursor_ge_uses_shared_renderer_and_cursor_followup_context(self):
        fwd = mock.Mock()
        fwd.get_ge_artifact.return_value = {
            "kind": "svg",
            "data": (
                '<svg xmlns="http://www.w3.org/2000/svg">'
                "<foreignObject>trusted server content</foreignObject></svg>"
            ),
        }
        with (
            mock.patch.object(shim, "_ge_chat_transport", return_value="legacy"),
            mock.patch(
                "client.popup.launcher.render_cursor_artifact_html",
                side_effect=AssertionError("Cursor must not use a second content review"),
                create=True,
            ) as cursor_render,
            mock.patch(
                "client.popup.launcher.render_artifact_html",
                return_value="<html>trusted server content</html>",
            ) as legacy_render,
            mock.patch(
                "client.popup.session.build_ge_context_bundle"
            ) as build_context,
            mock.patch(
                "client.popup.session.spawn",
                return_value={"status": "open", "popup_id": "p1"},
            ) as spawn,
        ):
            result = shim._handle_display_call(
                fwd,
                "open_ge_popup",
                {"run_id": "r1", "context": "private"},
                client_host="cursor",
            )

        self.assertEqual(result["status"], "open")
        cursor_render.assert_not_called()
        legacy_render.assert_called_once()
        build_context.assert_called_once()
        self.assertEqual(build_context.call_args.kwargs, {"caller": "cursor"})
        self.assertNotIn("api_profile", spawn.call_args.kwargs)

    def test_cursor_db_uses_shared_trusted_server_html_bridge(self):
        fwd = mock.Mock(endpoint="https://hub.example", token="device-token")
        board = {
            "stage": "kanban",
            "columns": [{"id": "todo", "cards": []}],
            "notes": "",
            "comments": [],
        }
        server_html = "<html><script>trustedServerBoard()</script></html>"
        with (
            mock.patch(
                "client.popup.launcher.fetch_board_html",
                return_value=server_html,
            ),
            mock.patch(
                "client.popup.session.spawn",
                return_value={"status": "open", "popup_id": "p2"},
            ) as spawn,
        ):
            result = shim._handle_display_call(
                fwd,
                "open_db_board",
                {"spec": board},
                client_host="cursor",
            )

        self.assertEqual(result["status"], "open")
        html_body = spawn.call_args.args[0]
        self.assertEqual(html_body, server_html)
        self.assertNotIn("initial_state", spawn.call_args.kwargs)
        self.assertNotIn("forbidden_token", spawn.call_args.kwargs)
        self.assertNotIn("api_profile", spawn.call_args.kwargs)

    def test_client_host_normalization_is_bounded(self):
        cases = {
            "OpenAI Codex": "codex",
            "Claude Code": "claude",
            "Anthropic": "claude",
            "Cursor": "cursor",
            "Cursor Agent": "cursor",
            "cursor_agent": "cursor",
            "claude_code": "claude",
            "openai_codex_cli": "codex",
            "my-codex-wrapper": "codex",
            "Claude Code codex-compat": None,
            "codec": None,
            "claudette": None,
            "": None,
            None: None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(shim._normalize_client_host(value), expected)

    def test_cursor_initialize_client_info_normalizes_to_host_family(self):
        self.assertEqual(
            shim._client_host_from_initialize(
                {"clientInfo": {"name": "Cursor", "version": "synthetic"}}
            ),
            "cursor",
        )

    def test_serve_never_starts_the_legacy_background_self_update(self):
        # Only installer.launcher may update, before this serve loop and under its lease.
        with mock.patch.object(shim, "_maybe_self_update") as mu:
            shim.serve(FakeForwarder(), stdin=io.StringIO(""), stdout=io.StringIO())
        mu.assert_not_called()

    def test_request_is_forwarded_verbatim_and_response_emitted(self):
        fwd = FakeForwarder(responses={"tools/call": {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}})
        req = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "de_audit", "arguments": {"title": "Explicit audit"}
        }}
        out = _run(fwd, [json.dumps(req)])
        # Forwarded exactly as received (transport-only, no mutation).
        self.assertEqual(fwd.sent, [req])
        self.assertEqual(out, [{"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}])

    def test_activated_defect_audit_without_user_topic_is_rejected_before_forward(self):
        fwd = FakeForwarder()
        request = {
            "jsonrpc": "2.0",
            "id": 71,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"content": "artifact"},
                },
            },
        }

        response = _run(fwd, [json.dumps(request)], client_host="codex")[0]

        self.assertEqual(fwd.sent, [])
        self.assertEqual(response["error"]["data"]["status"], "explicit_audit_required")

    def test_notification_forwarded_without_reply(self):
        fwd = FakeForwarder()
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        out = _run(fwd, [json.dumps(note)])
        self.assertEqual(fwd.sent, [note])
        self.assertEqual(out, [])  # no id -> no response line

    def test_initialize_client_info_routes_local_display_to_codex(self):
        fwd = FakeForwarder()
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "OpenAI Codex", "version": "1.0"},
            },
        }
        display = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "open_ge_popup", "arguments": {"run_id": "ge_1"}},
        }
        captured = []

        def handle(_forwarder, name, args, *, client_host=None):
            captured.append((name, args, client_host))
            return {"status": "open", "popup_id": "pop_1"}

        with mock.patch.object(shim, "_handle_display_call", side_effect=handle):
            _run(fwd, [json.dumps(init), json.dumps(display)])

        self.assertEqual(
            captured, [("open_ge_popup", {"run_id": "ge_1"}, "codex")]
        )

    def test_registration_host_conflict_closes_optional_display_and_logs_code(self):
        fwd = FakeForwarder()
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "OpenAI Codex", "version": "1.0"},
            },
        }
        display = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "open_ge_popup",
                "arguments": {"run_id": "ge_1", "caller": "codex"},
            },
        }
        gate = self._ready_cursor_gate()
        with (
            mock.patch.object(shim, "_handle_display_call") as handle,
            mock.patch.object(shim, "_log") as log,
        ):
            out = _run(
                fwd,
                [json.dumps(init), json.dumps(display)],
                client_host="cursor",
                capability_gate=gate,
            )

        handle.assert_not_called()
        self.assertTrue(out[-1]["result"]["isError"])
        self.assertIn(
            "capability-disabled",
            out[-1]["result"]["content"][0]["text"],
        )
        log.assert_any_call(
            "host identity status=conflicting diagnostic=host_identity_conflict "
            # reported= is the RAW name, not the normalized alias it resolved to.
            "declared=cursor observed=codex reported='OpenAI Codex'"
        )

    def test_registration_host_malformed_initialize_params_logs_malformed(self):
        fwd = FakeForwarder()
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": [],
        }
        with mock.patch.object(shim, "_log") as log:
            _run(
                fwd,
                [json.dumps(init)],
                client_host="codex",
            )

        log.assert_any_call(
            # params is a list here, so there is no clientInfo to report.
            "host identity status=malformed diagnostic=none "
            "declared=codex observed=none reported=None"
        )

    def test_cursor_nonmatched_identity_denies_optional_ui_but_forwards_core(self):
        identity_cases = (
            {},
            [],
            {"clientInfo": {"name": "Cursor Agent", "version": "1.0"}},
            {"clientInfo": {"name": "OpenAI Codex", "version": "1.0"}},
        )
        for init_params in identity_cases:
            with self.subTest(init_params=init_params):
                fwd = FakeForwarder(
                    responses={
                        "initialize": {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {"serverInfo": {"name": "de"}},
                        },
                        "tools/call": {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "result": {"run_id": "audit-1"},
                        },
                    }
                )
                init = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": init_params,
                }
                display = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "open_ge_popup",
                        "arguments": {"run_id": "ge_1"},
                    },
                }
                audit = {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "de_audit",
                        "arguments": {"title": "Explicit audit"},
                    },
                }

                out = _run(
                    fwd,
                    [json.dumps(init), json.dumps(display), json.dumps(audit)],
                    client_host="cursor",
                    capability_gate=self._ready_cursor_gate(),
                )

                self.assertEqual(fwd.sent, [init, audit])
                self.assertIn(
                    "capability-disabled",
                    out[1]["result"]["content"][0]["text"],
                )
                self.assertEqual(out[2]["result"], {"run_id": "audit-1"})

    def test_cursor_identity_match_waits_for_successful_initialize_response(self):
        fwd = FakeForwarder(raise_on={"initialize"})
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "Cursor", "version": "1.0"},
            },
        }
        display = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "open_ge_popup",
                "arguments": {"run_id": "ge_1"},
            },
        }
        gate = TransportCapabilityGate.static(
            display=True,
            followup=False,
            stopper=False,
        )
        with mock.patch.object(shim, "_handle_display_call") as handle:
            out = _run(
                fwd,
                [json.dumps(init), json.dumps(display)],
                client_host="cursor",
                capability_gate=gate,
            )

        handle.assert_not_called()
        self.assertEqual(out[0]["error"]["code"], -32001)
        self.assertIn(
            "capability-disabled",
            out[1]["result"]["content"][0]["text"],
        )

    def test_matching_registration_retains_existing_display_behavior(self):
        fwd = FakeForwarder()
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "OpenAI Codex", "version": "1.0"},
            },
        }
        display = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "open_ge_popup",
                "arguments": {"run_id": "ge_1", "caller": "claude"},
            },
        }
        captured = []

        def handle(_forwarder, name, args, *, client_host=None):
            captured.append((name, args, client_host))
            return {"status": "open", "popup_id": "pop_1"}

        with mock.patch.object(shim, "_handle_display_call", side_effect=handle):
            _run(
                fwd,
                [json.dumps(init), json.dumps(display)],
                client_host="codex",
            )

        self.assertEqual(captured, [(
            "open_ge_popup",
            {"run_id": "ge_1", "caller": "claude"},
            "codex",
        )])

    def test_unknown_explicit_registration_defaults_local_display_capability_off(self):
        fwd = FakeForwarder()
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "OpenAI Codex", "version": "1.0"},
            },
        }
        display = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "open_ge_popup", "arguments": {"run_id": "ge_1"}},
        }
        with mock.patch.object(shim, "_handle_display_call") as handle:
            out = _run(
                fwd,
                [json.dumps(init), json.dumps(display)],
                client_host="unknown-host",
            )

        handle.assert_not_called()
        self.assertTrue(out[-1]["result"]["isError"])
        self.assertIn(
            "capability-disabled",
            out[-1]["result"]["content"][0]["text"],
        )

    def test_unknown_hosts_cannot_advertise_or_invoke_local_display_tools(self):
        listing = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "de_audit", "inputSchema": {}}]},
        }
        fwd = FakeForwarder(responses={"tools/list": listing})
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        out = _run(fwd, [json.dumps(req)], client_host="unknown-host")
        names = [tool["name"] for tool in out[0]["result"]["tools"]]
        self.assertEqual(names, ["de_audit"])

        display = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "open_ge_popup",
                "arguments": {"run_id": "ge_1"},
            },
        }
        with mock.patch.object(shim, "_handle_display_call") as handle:
            denied = _run(
                fwd, [json.dumps(display)], client_host="unknown-host"
            )
        handle.assert_not_called()
        self.assertTrue(denied[0]["result"]["isError"])
        self.assertIn(
            "capability-disabled",
            denied[0]["result"]["content"][0]["text"],
        )

    def test_null_id_treated_as_notification(self):
        fwd = FakeForwarder()
        note = {"jsonrpc": "2.0", "id": None, "method": "ping"}
        out = _run(fwd, [json.dumps(note)])
        self.assertEqual(out, [])

    def test_unparseable_line_is_dropped_not_answered(self):
        # An unparseable line has no recoverable id; answering with an id:null JSON-RPC error would
        # poison strict MCP clients (TS SDK rejects id:null and drops the connection), so the shim
        # logs and skips it — no reply emitted, nothing forwarded.
        fwd = FakeForwarder()
        out = _run(fwd, ["{not json"])
        self.assertEqual(out, [])
        self.assertEqual(fwd.sent, [])

    def test_non_object_json_line_is_dropped(self):
        # Valid JSON that is not an object (e.g. a bare array) is likewise dropped, not answered with
        # an id:null invalid-request error.
        fwd = FakeForwarder()
        out = _run(fwd, ["[1, 2, 3]"])
        self.assertEqual(out, [])
        self.assertEqual(fwd.sent, [])

    def test_bom_prefixed_line_is_parsed(self):
        # A UTF-8 BOM (Windows can prepend one to the first line) must be stripped so the line parses
        # instead of tripping the drop path — the original "Could not attach" root cause.
        fwd = FakeForwarder(responses={"tools/call": {"jsonrpc": "2.0", "id": 5, "result": {"ok": 1}}})
        req = "\ufeff" + json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                     "params": {"name": "de_audit", "arguments": {
                                         "title": "Explicit audit"}}})
        out = _run(fwd, [req])
        self.assertEqual(out, [{"jsonrpc": "2.0", "id": 5, "result": {"ok": 1}}])

    def test_forward_error_mapped_to_jsonrpc_error_for_request(self):
        fwd = FakeForwarder(raise_on={"tools/call"})
        req = {"jsonrpc": "2.0", "id": 3, "method": "tools/call"}
        out = _run(fwd, [json.dumps(req)])
        self.assertEqual(out[0]["id"], 3)
        self.assertEqual(out[0]["error"]["code"], -32001)
        self.assertIn("boom", out[0]["error"]["message"])

    def test_forward_error_swallowed_for_notification(self):
        fwd = FakeForwarder(raise_on={"ping"})
        note = {"jsonrpc": "2.0", "method": "ping"}
        out = _run(fwd, [json.dumps(note)])
        self.assertEqual(out, [])

    def test_offline_error_data_is_surfaced_on_the_jsonrpc_error(self):
        # An OfflineError carrying an advisory marker must reach the caller as error.data so an
        # AQG-aware caller can offer a local advisory read — while STILL being a -32001 error
        # (fail-safe: a skill-less caller just sees the message and stops). The advisory never
        # closes an audit gate, so this soft marker introduces no fail-open risk.
        class OfflineForwarder:
            def forward(self, message):
                raise shim.OfflineError(
                    "hub down", data={"reason": "unreachable", "local_advisory_available": True})

        req = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
            "name": "de_audit", "arguments": {"title": "Explicit audit"}}}
        out = _run(OfflineForwarder(), [json.dumps(req)])
        self.assertEqual(out[0]["error"]["code"], -32001)
        self.assertEqual(out[0]["error"]["data"]["local_advisory_available"], True)

    def test_plain_forward_error_has_no_data_key(self):
        # A non-offline ShellError must NOT grow a data key — existing error mapping is unchanged.
        fwd = FakeForwarder(raise_on={"tools/call"})
        req = {"jsonrpc": "2.0", "id": 4, "method": "tools/call"}
        out = _run(fwd, [json.dumps(req)])
        self.assertNotIn("data", out[0]["error"])

    def test_blank_lines_ignored(self):
        fwd = FakeForwarder()
        out = _run(fwd, ["", "   "])
        self.assertEqual(fwd.sent, [])
        self.assertEqual(out, [])


class LocalAdvisoryOfferTestCase(unittest.TestCase):
    """The raw ``de_audit`` compatibility marker remains opt-in and never starts a run.

    The high-level ``audit_skill_submit`` path has a separate contract: an explicit interactive
    audit request is already one consent and enters the local advisory directly. These tests cover
    only the legacy marker's intent/tool scoping; the high-level path is covered by the Lite and
    OfflineAttach cases below.
    """

    def _msg(self, name, arguments=None):
        params = {"name": name}
        if arguments is not None:
            arguments = dict(arguments) if isinstance(arguments, dict) else arguments
            if name in {"de_audit", "submit_audit"} and isinstance(arguments, dict):
                arguments.setdefault("title", "Explicit audit topic")
            params["arguments"] = arguments
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}

    def test_de_audit_default_intent_offers_local_advisory(self):
        offer = shim._local_advisory_offer(self._msg("de_audit", {"accept_degrade": True}))
        self.assertIsNotNone(offer)
        self.assertEqual(offer["reason"], "unreachable")
        self.assertIs(offer["local_advisory_available"], True)

    def test_de_audit_missing_arguments_gets_no_offer(self):
        # The raw compatibility tool still requires its historical marker; no arguments means the
        # caller keeps the plain offline hard stop even though the intent would have qualified.
        self.assertIsNone(shim._local_advisory_offer(self._msg("de_audit")))

    def test_de_audit_prescriptive_intent_offers_local_advisory(self):
        self.assertIsNotNone(shim._local_advisory_offer(
            self._msg("de_audit", {"accept_degrade": True, "artifact_intent": "prescriptive"})))

    def test_de_audit_hypothesis_intent_gets_no_offer(self):
        # Hypothesis (brainstorming) is not on the AQG critical path (§11) -> no local fallback.
        self.assertIsNone(shim._local_advisory_offer(
            self._msg("de_audit", {"accept_degrade": True, "artifact_intent": "hypothesis"})))

    def test_other_tools_get_no_offer(self):
        self.assertIsNone(shim._local_advisory_offer(
            self._msg("de_market_research", {"accept_degrade": True})))
        self.assertIsNone(shim._local_advisory_offer(
            self._msg("ge_render", {"accept_degrade": True})))

    def test_non_tools_call_message_gets_no_offer(self):
        self.assertIsNone(shim._local_advisory_offer({"method": "tools/list", "params": {}}))

    def test_de_audit_name_on_a_non_tools_call_method_gets_no_offer(self):
        # audit a8251d8a: the guard must key on method=="tools/call", not just params.name —
        # a non-tools/call envelope carrying name=de_audit must NOT be offered the marker.
        msg = {"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
               "params": {"name": "de_audit", "arguments": {}}}
        self.assertIsNone(shim._local_advisory_offer(msg))

    def test_unhashable_intent_returns_none_without_raising(self):
        # audit a8251d8a (gemini/codex): an unhashable artifact_intent must fail CLOSED, not
        # raise TypeError from the frozenset membership test (which would crash the serve loop).
        self.assertIsNone(shim._local_advisory_offer(
            self._msg("de_audit", {"accept_degrade": True, "artifact_intent": []})))
        self.assertIsNone(shim._local_advisory_offer(
            self._msg("de_audit", {"accept_degrade": True, "artifact_intent": {}})))

    def test_non_dict_arguments_gets_no_offer(self):
        # audit a8251d8a (codex/grok): a mis-shaped (non-dict) arguments block must not be
        # collapsed to "absent intent" and over-offer — it fails closed to the plain error.
        self.assertIsNone(shim._local_advisory_offer(self._msg("de_audit", "oops")))


class OfflineAttachTestCase(unittest.TestCase):
    def _request(self, method, msg_id=1, params=None):
        request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            request["params"] = params
        return request

    def test_unreachable_activated_session_attaches_without_hub_and_exposes_local_audit(self):
        forwarder = shim.Forwarder("https://hub.example", "tok")
        requests = [
            self._request("initialize", params={"protocolVersion": "2025-11-25"}),
            self._request("tools/list"),
        ]
        with mock.patch.object(shim, "hub_reachable", return_value=False), \
                mock.patch.object(shim, "build_opener") as opener, \
                mock.patch("client.popup.session.sweep") as sweep:
            responses = _run(
                forwarder,
                [json.dumps(request) for request in requests],
                client_host="codex",
            )

        opener.assert_not_called()
        sweep.assert_not_called()
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"],
                         "decision-engine-shell")
        self.assertEqual(
            [tool["name"] for tool in responses[1]["result"]["tools"]],
            ["service_unavailable", "audit_skill_submit", "audit_skill_complete"],
        )

    def test_unreachable_tool_calls_return_service_unavailable_without_hub_body(self):
        forwarder = shim.Forwarder("https://hub.example", "tok")
        for name in ("de_market_research", "de_forecast", "ge_render", "open_db_board",
                     "unknown_tool"):
            with self.subTest(name=name):
                request = self._request(
                    "tools/call", params={"name": name, "arguments": {}}
                )
                with mock.patch.object(shim, "hub_reachable", return_value=False):
                    response = _run(forwarder, [json.dumps(request)])[0]
                self.assertEqual(response["error"]["code"], -32001)
                self.assertEqual(response["error"]["data"], {
                    "status": "service_unavailable",
                    "reason": "unreachable",
                    "retryable": True,
                    "request_sent": False,
                    "action": "reconnect",
                })
                self.assertNotIn("hub.example", json.dumps(response))
                self.assertNotIn("tok", json.dumps(response))

    def test_service_unavailable_tool_reports_no_request_sent(self):
        forwarder = shim.Forwarder("https://hub.example", "tok")
        request = self._request(
            "tools/call", params={"name": "service_unavailable", "arguments": {}}
        )
        with mock.patch.object(shim, "hub_reachable", return_value=False):
            response = _run(forwarder, [json.dumps(request)])[0]
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload, {
            "status": "service_unavailable",
            "reason": "unreachable",
            "retryable": True,
            "request_sent": False,
            "action": "reconnect",
        })

    def test_opted_in_de_audit_retains_advisory_marker_on_service_error(self):
        forwarder = shim.Forwarder("https://hub.example", "tok")
        request = self._request(
            "tools/call",
            params={"name": "de_audit", "arguments": {
                "title": "Explicit audit", "accept_degrade": True
            }},
        )
        with mock.patch.object(shim, "hub_reachable", return_value=False):
            response = _run(forwarder, [json.dumps(request)], client_host="claude")[0]
        self.assertEqual(response["error"]["code"], -32001)
        self.assertEqual(response["error"]["data"]["status"], "service_unavailable")
        self.assertEqual(response["error"]["data"]["local_advisory_available"], True)
        self.assertEqual(response["error"]["data"]["action"], "reconnect")

    def test_unreachable_surface_lists_local_audit_and_returns_a_local_run(self):
        forwarder = shim.Forwarder("https://hub.example", "tok")
        requests = [
            self._request("tools/list"),
            self._request(
                "tools/call",
                msg_id=2,
                params={
                    "name": "audit_skill_submit",
                    "arguments": {
                        "skill_name": "audit",
                        "args": {"title": "Offline review", "content": "artifact"},
                    },
                },
            ),
        ]
        with mock.patch.object(shim, "hub_reachable", return_value=False):
            responses = _run(
                forwarder, [json.dumps(request) for request in requests], client_host="codex"
            )

        self.assertEqual(
            [tool["name"] for tool in responses[0]["result"]["tools"]],
            ["service_unavailable", "audit_skill_submit", "audit_skill_complete"],
        )
        payload = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertTrue(payload["payload"]["local"])
        self.assertEqual(payload["payload"]["title"], "Offline review")
        self.assertEqual(payload["payload"]["local_surface"], "de_lite")
        self.assertEqual(payload["payload"]["degrade_reason"], "service_unavailable")
        self.assertIsNone(payload["payload"]["audit_id"])

    def test_unreachable_local_audit_preserves_the_session_locale(self):
        forwarder = shim.Forwarder("https://hub.example", "tok")
        request = self._request(
            "tools/call",
            params={
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {
                        "title": "Offline review",
                        "content": "artifact",
                        "ui_locale": "en-US",
                    },
                },
            },
        )
        with mock.patch.object(shim, "hub_reachable", return_value=False):
            response = _run(
                forwarder, [json.dumps(request)], client_host="codex"
            )[0]
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["ui_locale"], "en-US")

    def test_unreachable_audit_automatically_arms_the_local_stopper(self):
        forwarder = shim.Forwarder("https://hub.example", "tok")
        request = self._request(
            "tools/call",
            params={
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"title": "Offline review", "content": "artifact"},
                },
            },
        )
        with mock.patch.object(shim, "hub_reachable", return_value=False), \
                mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            _run(forwarder, [json.dumps(request)], client_host="codex")
        spawn.assert_called_once()


class LiteForwarderTestCase(unittest.TestCase):
    def test_initialize_identifies_decision_engine_lite(self):
        response = shim.LiteForwarder().forward({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        })

        self.assertEqual(response["result"]["serverInfo"]["name"], "decision-engine-lite")
        self.assertEqual(response["result"]["serverInfo"]["version"], "unactivated")
        self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")

    def test_tools_list_exposes_activation_and_local_audit_only(self):
        response = shim.LiteForwarder().forward({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"
        })

        self.assertEqual(
            [tool["name"] for tool in response["result"]["tools"]],
            ["activation_required", "audit_skill_submit", "audit_skill_complete"],
        )

    def test_local_completion_updates_existing_lite_run_without_forwarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "active-runs.json"
            with mock.patch.dict(os.environ, {"DE_ACTIVE_RUNS": str(registry)}):
                with mock.patch.object(shim.secrets, "token_hex", return_value="complete"):
                    submit = shim.LiteForwarder().forward({
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "audit_skill_submit", "arguments": {
                            "skill_name": "audit", "args": {
                                "title": "Completion review", "content": "artifact"
                            }
                        }},
                    })
                with mock.patch("client.runner.launch_stopper_if_available"):
                    shim._spawn_stopper_for_audit(submit)
                complete = _run(
                    shim.LiteForwarder(), [json.dumps({
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "audit_skill_complete", "arguments": {
                            "local_id": "local_complete", "status": "completed"
                        }},
                    })],
                    client_host="codex",
                )[0]

                self.assertEqual(complete["result"]["content"][0]["type"], "text")
                payload = json.loads(complete["result"]["content"][0]["text"])
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(payload["run_id"], "local_complete")
                self.assertTrue(payload["advisory_only"])
                self.assertIsNone(payload["audit_id"])
                entry = json.loads(registry.read_text())["runs"]["local_complete"]
                self.assertEqual(entry["status"], "completed")
                self.assertIn("hidden_after", entry)

    def test_local_completion_rejects_unknown_id_and_running_status(self):
        for arguments in (
            {"local_id": "local_missing", "status": "completed"},
            {"local_id": "local_missing", "status": "running"},
        ):
            with self.subTest(arguments=arguments):
                with tempfile.TemporaryDirectory() as tmp:
                    with mock.patch.dict(
                        os.environ, {"DE_ACTIVE_RUNS": str(Path(tmp) / "active-runs.json")}
                    ):
                        response = _run(
                            shim.LiteForwarder(), [json.dumps({
                                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "audit_skill_complete", "arguments": arguments},
                            })],
                            client_host="codex",
                        )[0]
                self.assertEqual(response["error"]["data"]["status"],
                                 "local_completion_rejected")

    def test_local_completion_import_failure_is_redacted_and_keeps_transport_alive(self):
        real_import = __import__

        def fail_client_import(name, *args, **kwargs):
            if name == "client":
                raise ModuleNotFoundError("synthetic missing client package")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fail_client_import):
            response = shim._local_audit_completion_response(
                4, {"local_id": "local_missing_client", "status": "failed"}
            )

        self.assertEqual(response["error"]["code"], -32001)
        self.assertEqual(response["error"]["data"], {
            "status": "local_completion_rejected",
            "reason": "state_unavailable",
        })
        self.assertNotIn("synthetic missing client package", json.dumps(response))

    def test_explicit_local_audit_returns_advisory_run_without_echoing_content(self):
        with mock.patch.object(shim.secrets, "token_hex", return_value="abc123"):
            response = shim.LiteForwarder().forward({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "audit_skill_submit",
                    "arguments": {
                        "skill_name": "audit",
                        "args": {
                            "title": "Lite review",
                            "content": "sensitive artifact text",
                            "artifact_intent": "prescriptive",
                            "accept_degrade": True,
                            "ui_locale": "en-US",
                        },
                    },
                },
            })

        payload = json.loads(response["result"]["content"][0]["text"])
        run = payload["payload"]
        self.assertEqual(payload["run_id"], "local_abc123")
        self.assertEqual(run["run_id"], "local_abc123")
        self.assertEqual(run["status"], "running")
        self.assertIs(run["local"], True)
        self.assertEqual(run["local_surface"], "de_lite")
        self.assertEqual(run["fallback_mode"], "session-llm")
        self.assertIsNone(run["audit_id"])
        self.assertIs(run["advisory_only"], True)
        self.assertEqual(run["ui_locale"], "en-US")
        self.assertNotIn("sensitive artifact text", json.dumps(payload))

    def test_local_audit_without_user_topic_creates_no_run(self):
        response = shim.LiteForwarder().forward({
            "jsonrpc": "2.0",
            "id": 81,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"content": "artifact"},
                },
            },
        })

        self.assertEqual(response["error"]["data"]["status"], "explicit_audit_required")

    def test_local_audit_fails_closed_without_content_or_defect_review(self):
        cases = (
            ({"skill_name": "audit", "args": {
                "title": "Invalid audit", "content": ""}}, "activation_required"),
            ({"skill_name": "audit", "args": {
                "title": "Invalid audit", "content": "x",
                "accept_degrade": "true"}}, "activation_required"),
            ({"skill_name": "audit", "args": ["not", "an", "object"]},
             "explicit_audit_required"),
            ({"skill_name": "audit", "args": {
                "title": "Invalid audit", "content": "x", "accept_degrade": True,
                "artifact_intent": "hypothesis"}}, "activation_required"),
            ({"skill_name": "audit-market-research", "args": {
                "content": "x", "accept_degrade": True}}, "activation_required"),
        )
        for arguments, expected_status in cases:
            with self.subTest(arguments=arguments):
                response = shim.LiteForwarder().forward({
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {"name": "audit_skill_submit", "arguments": arguments},
                })
                self.assertEqual(response["error"]["code"], -32001)
                self.assertEqual(response["error"]["data"]["status"], expected_status)

    def test_activation_required_tool_returns_structured_result(self):
        response = shim.LiteForwarder().forward({
            "jsonrpc": "2.0",
            "id": "activation_required",
            "method": "tools/call",
            "params": {"name": "activation_required", "arguments": {}},
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "activation_required")

    def test_unavailable_tool_calls_return_activation_required_error(self):
        for name in (
            "hosted_tool",
            "unknown_tool",
            "de_audit",
            "ge_render",
            "open_db_board",
        ):
            with self.subTest(name=name):
                response = shim.LiteForwarder().forward({
                    "jsonrpc": "2.0",
                    "id": name,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                })
                self.assertEqual(response["error"]["code"], -32001)
                self.assertEqual(
                    response["error"]["data"],
                    {"status": "activation_required", "tool": name},
                )
                self.assertIn("activation_required", response["error"]["message"])

    def test_explicit_audit_is_local_without_a_second_degrade_confirmation(self):
        response = shim.LiteForwarder().forward({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Explicit audit", "content": "artifact"
                }
            }},
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "unactivated")
        self.assertTrue(payload["payload"]["advisory_only"])

    def test_serve_does_not_merge_or_dispatch_display_tools_in_lite_mode(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "open_db_board", "arguments": {}},
            },
        ]
        forwarder = shim.LiteForwarder()
        with (
            mock.patch.object(shim, "_handle_display_call") as display,
            mock.patch.object(
                shim,
                "_refresh_degraded_forwarder",
                return_value=(forwarder, True, False),
            ),
            mock.patch("client.popup.session.sweep") as sweep,
        ):
            responses = _run(
                forwarder, [json.dumps(request) for request in requests],
                client_host="codex",
            )

        display.assert_not_called()
        sweep.assert_not_called()
        self.assertEqual(
            [tool["name"] for tool in responses[0]["result"]["tools"]],
            ["activation_required", "audit_skill_submit", "audit_skill_complete"],
        )
        self.assertEqual(responses[1]["error"]["code"], -32001)
        self.assertEqual(responses[1]["error"]["data"]["tool"], "open_db_board")

    def test_lite_audit_automatically_arms_the_local_stopper(self):
        request = {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Explicit audit", "content": "artifact"
                }
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            _run(shim.LiteForwarder(), [json.dumps(request)], client_host="codex")
        spawn.assert_called_once()


    def test_main_falls_back_to_lite_only_for_activation_required(self):
        with (
            mock.patch.object(
                shim.Forwarder,
                "from_config",
                side_effect=shim.ActivationRequiredError("activate"),
            ),
            mock.patch.object(shim, "serve", return_value=0) as serve,
        ):
            result = shim.main([])

        self.assertEqual(result, 0)
        self.assertIsInstance(serve.call_args.args[0], shim.LiteForwarder)

    def test_main_preserves_non_activation_configuration_error(self):
        with (
            mock.patch.object(
                shim.Forwarder,
                "from_config",
                side_effect=config.ShellError("invalid config"),
            ),
            mock.patch.object(shim, "serve") as serve,
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            result = shim.main([])

        self.assertEqual(result, 1)
        serve.assert_not_called()


class LocalEntitlementResponseTestCase(unittest.TestCase):
    def test_insufficient_balance_from_async_audit_id_converts_the_pending_audit(self):
        queued = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps({
                "schema_version": "1.0",
                "skill": "audit",
                "audit_id": "aud_async_audit_id",
                "payload": {
                    "audit_id": "aud_async_audit_id",
                    "status": "queued",
                    "title": "Async audit id balance audit",
                },
            })}]},
        }
        failed = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "insufficient_balance"}]},
        }

        class SequencedForwarder:
            def __init__(self):
                self.sent = []

            def forward(self, message):
                self.sent.append(message)
                return queued if len(self.sent) == 1 else failed

        class SyncThread:
            def __init__(self, target=None, args=(), **kwargs):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        submit = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Async audit id balance audit", "content": "artifact",
                }
            }},
        }
        wait = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "audit_skill_status", "arguments": {
                "audit_id": "aud_async_audit_id",
            }},
        }
        forwarder = SequencedForwarder()
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, \
                mock.patch.object(shim.threading, "Thread", SyncThread), \
                mock.patch("client.runner.forget_active_run") as forget:
            out = _run(
                forwarder,
                [json.dumps(submit), json.dumps(wait)],
                client_host="codex",
            )

        payload = json.loads(out[1]["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["title"], "Async audit id balance audit")
        self.assertEqual(len(forwarder.sent), 2)
        self.assertEqual(spawn.call_count, 2)
        forget.assert_called_once_with("aud_async_audit_id")

    def test_insufficient_balance_from_wait_audit_converts_the_pending_audit(self):
        queued = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps({
                "schema_version": "1.0",
                "skill": "audit",
                "run_id": "aud_async_balance",
                "payload": {
                    "run_id": "aud_async_balance",
                    "status": "queued",
                    "title": "Async balance audit",
                    "profile": "deep",
                },
            })}]},
        }
        failed = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": json.dumps({
                "run_id": "aud_async_balance",
                "status": "failed",
                "error": "credit: insufficient_balance",
            })}]},
        }

        class SequencedForwarder:
            def __init__(self):
                self.sent = []

            def forward(self, message):
                self.sent.append(message)
                return queued if len(self.sent) == 1 else failed

        class SyncThread:
            def __init__(self, target=None, args=(), **kwargs):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        submit = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Async balance audit", "content": "artifact",
                    "ui_locale": "en-US",
                }
            }},
        }
        wait = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "wait_audit", "arguments": {
                "run_id": "aud_async_balance",
            }},
        }
        forwarder = SequencedForwarder()
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, \
                mock.patch.object(shim.threading, "Thread", SyncThread), \
                mock.patch("client.runner.forget_active_run") as forget:
            out = _run(
                forwarder,
                [json.dumps(submit), json.dumps(wait)],
                client_host="codex",
            )

        payload = json.loads(out[1]["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertEqual(payload["payload"]["title"], "Async balance audit")
        self.assertEqual(payload["payload"]["ui_locale"], "en-US")
        self.assertEqual(len(forwarder.sent), 2)
        self.assertEqual(spawn.call_count, 2)
        forget.assert_called_once_with("aud_async_balance")

    def test_insufficient_balance_from_wait_audit_without_process_context_enters_local_advisory(self):
        # A hosted MCP process may be recreated between submit and wait. The marker itself must
        # still select the local fallback; pending_audits is only a title/locale cache.
        failed = {
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps({
                "run_id": "aud_context_lost",
                "status": "failed",
                "title": "Context lost balance audit",
                "error": "insufficient_balance",
            })}]},
        }
        wait = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "wait_audit", "arguments": {
                "run_id": "aud_context_lost",
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, \
                mock.patch.object(runner, "load_active_runs_registry", return_value={"runs": {}}), \
                mock.patch.object(runner, "forget_active_run") as forget:
            out = _run(
                FakeForwarder(responses={"tools/call": failed}),
                [json.dumps(wait)],
                client_host="codex",
            )[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertTrue(payload["payload"]["local"])
        self.assertEqual(payload["payload"]["title"], "Context lost balance audit")
        spawn.assert_called_once()
        forget.assert_called_once_with("aud_context_lost")

    def test_insufficient_balance_marker_alone_from_wait_audit_enters_local_advisory(self):
        # Routing must not depend on title, language, run status, or any server prose. The run id
        # only scopes this follow-up to an audit and supplies cleanup correlation.
        failed = {
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "insufficient_balance"}]},
        }
        wait = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "wait_audit", "arguments": {
                "run_id": "aud_marker_only",
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, \
                mock.patch.object(runner, "load_active_runs_registry", return_value={"runs": {}}), \
                mock.patch.object(runner, "forget_active_run") as forget:
            out = _run(
                FakeForwarder(responses={"tools/call": failed}),
                [json.dumps(wait)],
                client_host="codex",
            )[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertTrue(payload["payload"]["local"])
        self.assertEqual(payload["payload"]["title"], "DE Lite local audit")
        spawn.assert_called_once()
        forget.assert_called_once_with("aud_marker_only")

    def test_insufficient_balance_marker_on_non_audit_tool_does_not_start_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps({
                "title": "Unrelated tool response",
                "error": "insufficient_balance",
            })}]},
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "unrelated_tool", "arguments": {}},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(
                FakeForwarder(responses={"tools/call": response}),
                [json.dumps(request)],
                client_host="codex",
            )[0]

        self.assertEqual(out["result"], response["result"])
        spawn.assert_not_called()

    def test_http_insufficient_balance_from_wait_audit_enters_local_advisory(self):
        class BalanceBlockedForwarder:
            def forward(self, _message):
                raise shim.EntitlementBlockedError(shim._credits_exhausted_action())

        wait = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "wait_audit", "arguments": {
                "run_id": "aud_http_marker",
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, \
                mock.patch.object(runner, "load_active_runs_registry", return_value={"runs": {}}), \
                mock.patch.object(runner, "forget_active_run") as forget:
            out = _run(
                BalanceBlockedForwarder(),
                [json.dumps(wait)],
                client_host="codex",
            )[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertTrue(payload["payload"]["local"])
        spawn.assert_called_once()
        forget.assert_called_once_with("aud_http_marker")

    def test_mcp_tool_wrapped_insufficient_balance_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": "深度审计未能执行：外部审计面板返回 credit: insufficient_balance，5 个审计声部均未开始。",
                }],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Wrapped insufficient balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        self.assertNotEqual(out["result"], response["result"])
        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertIsNone(payload["payload"]["audit_id"])
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_insufficient_balance_enters_local_advisory_once(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "credit: insufficient_balance"}],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Insufficient balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertIsNone(payload["payload"]["audit_id"])
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_wrapped_insufficient_balance_without_is_error_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": "深度审计未能执行：外部审计面板返回 credit: insufficient_balance，5 个审计声部均未开始。",
                }],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Missing isError audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_multiline_insufficient_balance_wrapper_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "深度审计未能完成。\n"
                        "• 主题：软件供应链依赖混淆攻击防护方案\n"
                        "• 模式：deep\n"
                        "• 运行 ID：aud_mYciHaIrG99NX_zP\n"
                        "• 面板：5 个审计声部均未启动\n"
                        "• 失败原因：credit: insufficient_balance\n\n"
                        "因此没有可靠的审计结论，也未进行本地替代审计。"
                    ),
                }],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Multiline insufficient balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertIsNone(payload["payload"]["audit_id"])
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_real_screenshot_insufficient_balance_wrapper_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "深度审计未能执行：审计服务返回 `credit: insufficient_balance`，5 个审计声音均未开始运行。\n"
                        "审计对象为“支持云同步与账户恢复的密码管理器安全设计”，运行 ID： aud_KkMwH0RNwcQ0_714 。"
                        "由于这是高风险安全设计，我不会采用未经授权的本地审查替代外部深度审计。"
                    ),
                }],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Screenshot insufficient balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertIsNone(payload["payload"]["audit_id"])
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_real_english_screenshot_insufficient_balance_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "Deep audit failed before any auditors started because the audit service reported `credit: "
                        "insufficient_balance`.\n\n"
                        "Topic selected: event-driven payment webhook processor\n"
                        "Run ID: aud_kEmifG8VQjCMZeFN"
                    ),
                }],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "English screenshot insufficient balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertIsNone(payload["payload"]["audit_id"])
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_real_decision_engine_screenshot_insufficient_balance_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "外部深度审计未能启动：Decision Engine 返回 `credit: insufficient_balance`，5 个审计声音均未开始。\n\n"
                        "运行编号： aud_ymgP3wq1lyxwlf1z\n\n"
                        "因此不能伪造或者代审计结论。待额度恢复后，可使用同一主题重新提交深度审计。"
                    ),
                }],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Decision Engine screenshot insufficient balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertIsNone(payload["payload"]["audit_id"])
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_real_english_pending_screenshot_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "The deep audit could not run because Decision Engine returned `credit: insufficient_balance`. "
                        "All five auditors remained pending.\n\n"
                        "Run ID: aud_ob6Fgtf2GX-8P1-y\n\n"
                        "No audit findings were generated."
                    ),
                }],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "English pending screenshot insufficient balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertIsNone(payload["payload"]["audit_id"])
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_real_english_external_panel_screenshot_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "The deep audit was submitted, but the external panel could not run because the audit service "
                        "reported `credit: insufficient_balance`.\n"
                        "Run ID: `aud_4hcB0zjLzPfO52Dc`\n"
                        "No independent verdict or findings are available. The memo’s main weakness is its universal "
                        "“70% conversion” target: roundabouts are context-dependent and require city-specific analysis "
                        "of pedestrians, cyclists, transit, freight, emergency vehicles, accessibility, right-of-way, "
                        "and crash data."
                    ),
                }],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "English external panel screenshot insufficient balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertTrue(payload["payload"]["local"])
        self.assertIsNone(payload["payload"]["audit_id"])
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_namespaced_audit_tool_marker_enters_local_advisory(self):
        # Some hosts pass their fully-qualified callable name through the MCP request instead of
        # stripping the server prefix. The response marker is still authoritative for this audit.
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "insufficient_balance"}],
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "mcp__decision-engine__audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Namespaced balance audit", "content": "artifact"
                }
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(
                FakeForwarder(responses={"tools/call": response}),
                [json.dumps(request)], client_host="codex",
            )[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertTrue(payload["payload"]["local"])
        self.assertEqual(payload["payload"]["title"], "Namespaced balance audit")
        spawn.assert_called_once()

    def test_mcp_tool_failed_payload_with_pending_auditors_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps({
                "run_id": "aud_synthetic_balance_failure",
                "status": "failed",
                "title": "Synthetic balance audit",
                "profile": "deep",
                "auditors": [
                    {"index": index, "status": "pending"}
                    for index in range(5)
                ],
                "error": "credit: insufficient_balance",
            })}]},
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Synthetic balance audit", "content": "artifact"
                }
            }},
        }
        forwarder = FakeForwarder(responses={"tools/call": response})
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(forwarder, [json.dumps(request)], client_host="codex")[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
        self.assertEqual(len(forwarder.sent), 1)
        spawn.assert_called_once()

    def test_mcp_tool_insufficient_balance_marker_is_sufficient_inside_text_envelope(self):
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Marker-only balance audit", "content": "artifact"
                }
            }},
        }
        accepted_texts = (
            "credit: insufficient_balance; retry later",
            "The panel could not run because insufficient_balance was reported.",
            "深度审计结果：insufficient_balance。服务端摘要格式可变化。",
        )
        for text in accepted_texts:
            with self.subTest(text=text):
                response = {
                    "jsonrpc": "2.0", "id": 1,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": text}],
                    },
                }
                with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
                    out = _run(
                        FakeForwarder(responses={"tools/call": response}),
                        [json.dumps(request)], client_host="codex",
                    )[0]
                payload = json.loads(out["result"]["content"][0]["text"])
                self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
                self.assertEqual(payload["payload"]["degrade_action"], "add_credits")
                self.assertTrue(payload["payload"]["local"])
                self.assertIsNone(payload["payload"]["audit_id"])
                spawn.assert_called_once()

    def test_mcp_insufficient_balance_marker_wins_over_response_envelope_shape(self):
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Invalid balance envelope audit", "content": "artifact"
                }
            }},
        }
        marker_responses = (
            {"jsonrpc": "2.0", "id": 1, "result": {
                "isError": False,
                "content": [{"type": "text", "text": "credit: insufficient_balance"}],
            }},
            {"jsonrpc": "2.0", "id": 1, "result": {
                "isError": None,
                "content": [{"type": "text", "text": "credit: insufficient_balance"}],
            }},
            {"jsonrpc": "2.0", "id": 1, "result": {
                "isError": True,
                "content": [
                    {"type": "text", "text": "credit: insufficient_balance"},
                    {"type": "text", "text": "additional server text"},
                ],
            }},
            {"jsonrpc": "2.0", "id": 1,
             "error": {"code": -32000, "message": "malformed response"},
             "result": {
                 "isError": True,
                 "content": [{"type": "text", "text": "credit: insufficient_balance"}],
             }},
        )
        for response in marker_responses:
            with self.subTest(response=response), mock.patch.object(
                shim, "_spawn_stopper_for_audit"
            ) as spawn:
                out = _run(
                    FakeForwarder(responses={"tools/call": response}),
                    [json.dumps(request)], client_host="codex",
                )[0]
            payload = json.loads(out["result"]["content"][0]["text"])
            self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
            self.assertTrue(payload["payload"]["local"])
            spawn.assert_called_once()

        no_marker_responses = (
            {"jsonrpc": "2.0", "id": 1, "result": {
                "isError": True,
                "content": [{"type": "text", "text": "service_unavailable"}],
            }},
            {"jsonrpc": "2.0", "id": 1, "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": "The panel failed without an entitlement marker.",
                }],
            }},
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({
                "run_id": "aud_synthetic_running",
                "status": "failed",
                "auditors": [{"status": "running"}],
                "error": "service_unavailable",
            })}] }},
        )
        for no_marker in no_marker_responses:
            with self.subTest(response=no_marker), mock.patch.object(
                shim, "_spawn_stopper_for_audit"
            ) as spawn:
                out = _run(
                    FakeForwarder(responses={"tools/call": no_marker}),
                    [json.dumps(request)], client_host="codex",
                )[0]
            self.assertEqual(out["result"], no_marker["result"])
            spawn.assert_called_once()

    def test_jsonrpc_account_action_error_enters_local_advisory(self):
        response = {
            "jsonrpc": "2.0", "id": 1,
            "error": {
                "code": -32002,
                "message": "account action required",
                "data": {
                    "status": "account_action_required",
                    "reason": "subscription_expired",
                    "retryable": False,
                    "action": "renew_subscription",
                    "local_advisory_available": True,
                },
            },
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Entitlement audit", "content": "artifact"
                }
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(
                FakeForwarder(responses={"tools/call": response}),
                [json.dumps(request)], client_host="codex",
            )[0]
        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "subscription_expired")
        spawn.assert_called_once()


class ForwarderConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.json"
        self._saved = os.environ.get("DE_CONFIG_PATH")
        os.environ["DE_CONFIG_PATH"] = str(self.cfg_path)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DE_CONFIG_PATH", None)
        else:
            os.environ["DE_CONFIG_PATH"] = self._saved
        self.tmp.cleanup()

    def _write(self, payload):
        config.atomic_write_json(self.cfg_path, payload)

    def test_missing_config_requires_activation(self):
        with self.assertRaises(shim.ActivationRequiredError):
            shim.Forwarder.from_config()

    def test_empty_config_requires_activation(self):
        self._write({})
        with self.assertRaises(shim.ActivationRequiredError):
            shim.Forwarder.from_config()

    def test_endpoint_without_token_requires_activation(self):
        self._write({"server_endpoint": "https://hub.example.com"})
        with self.assertRaises(shim.ActivationRequiredError):
            shim.Forwarder.from_config()

    def test_token_without_endpoint_remains_configuration_error(self):
        self._write({"access_token": "tok"})
        with self.assertRaises(config.ShellError) as raised:
            shim.Forwarder.from_config()
        self.assertNotIsInstance(raised.exception, shim.ActivationRequiredError)

    def test_invalid_json_remains_configuration_error(self):
        self.cfg_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(config.ShellError) as raised:
            shim.Forwarder.from_config()
        self.assertNotIsInstance(raised.exception, shim.ActivationRequiredError)

    def test_resolves_endpoint_and_token(self):
        self._write({"server_endpoint": "hub.example.com", "access_token": "tok_abc"})
        fwd = shim.Forwarder.from_config()
        self.assertEqual(fwd.endpoint, "https://hub.example.com")
        self.assertEqual(fwd.token, "tok_abc")

    def test_rejects_plaintext_http_with_token(self):
        with self.assertRaises(config.ShellError):
            shim.Forwarder("http://hub.example.com", "tok_abc")

    def test_allows_plaintext_http_to_localhost(self):
        fwd = shim.Forwarder("http://localhost:8787", "tok_abc")
        self.assertEqual(fwd.endpoint, "http://localhost:8787")


class ShimEmptyResponseTestCase(unittest.TestCase):
    def test_empty_server_response_to_request_emits_error(self):
        class EmptyForwarder:
            def forward(self, message):
                return {}  # server sent an empty body

        req = {"jsonrpc": "2.0", "id": 9, "method": "tools/call"}
        out = _run(EmptyForwarder(), [json.dumps(req)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 9)
        self.assertEqual(out[0]["error"]["code"], -32603)

    def test_empty_server_response_to_notification_stays_silent(self):
        class EmptyForwarder:
            def forward(self, message):
                return {}

        note = {"jsonrpc": "2.0", "method": "ping"}
        self.assertEqual(_run(EmptyForwarder(), [json.dumps(note)]), [])


def _submit_result(run_id="run_abc", status="queued"):
    """The MCP result envelope `audit_skill_submit` returns (run payload as JSON text)."""
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text",
                       "text": json.dumps({"run_id": run_id, "status": status})}]}}


class AuditStopPanelTestCase(unittest.TestCase):
    """The agent starts a hub audit through the shim's MCP path -- via `audit_skill_submit` (the skill
    workflow) OR the raw `submit_audit` hub tool -- not the `audit` CLI. The shim must mirror the CLI:
    on a successful submit, record the active run + launch the stop panel (off-thread, best-effort) so
    an agent-driven audit gets the same panel the CLI does, whichever submit tool it reached the hub by."""

    def test_audit_run_payload_parses_and_rejects_bad_shapes(self):
        self.assertEqual(shim._audit_run_payload(_submit_result(run_id="r9"))["run_id"], "r9")
        for bad in ({}, {"result": {}}, {"result": {"content": []}},
                    {"result": {"content": [{"type": "text", "text": "not-json"}]}},
                    {"result": {"content": [{"type": "text", "text": "[1, 2]"}]}},
                    {"result": {"content": [{"text": 123}]}}):
            self.assertIsNone(shim._audit_run_payload(bad))

    def test_audit_run_payload_unwraps_current_skill_envelope(self):
        run = {"run_id": "r9", "status": "queued", "title": "Specific audit",
               "profile": "standard"}
        envelope = {"schema_version": "1.0", "skill": "audit", "run_id": "r9",
                    "payload": dict(run)}
        response = {"result": {"content": [{"type": "text", "text": json.dumps(envelope)}]}}
        self.assertEqual(shim._audit_run_payload(response), run)

        envelope["payload"].pop("run_id")
        response["result"]["content"][0]["text"] = json.dumps(envelope)
        self.assertEqual(shim._audit_run_payload(response), run)

        envelope.pop("run_id")
        response["result"]["content"][0]["text"] = json.dumps(envelope)
        self.assertIsNone(shim._audit_run_payload(response))

    def test_spawn_launches_only_on_string_run_id(self):
        with mock.patch("client.runner.save_active_run") as save, \
             mock.patch("client.runner.launch_stopper_if_available") as launch:
            shim._spawn_stopper_for_audit(_submit_result(run_id="r1"))
        save.assert_called_once()
        launch.assert_called_once()
        # no run_id, and a truthy-but-non-string run_id, must both no-op (no active-run write, no panel)
        for bad in ({"result": {"content": [{"type": "text", "text": json.dumps({"status": "failed"})}]}},
                    {"result": {"content": [{"type": "text",
                     "text": json.dumps({"run_id": 123, "status": "queued"})}]}}):
            with mock.patch("client.runner.save_active_run") as save, \
                 mock.patch("client.runner.launch_stopper_if_available") as launch:
                shim._spawn_stopper_for_audit(bad)
            save.assert_not_called()
            launch.assert_not_called()

    def test_activated_hosted_submit_never_creates_a_de_lite_row(self):
        response = {
            "result": {"content": [{"type": "text", "text": json.dumps({
                "run_id": "hosted_abc",
                "status": "queued",
                "title": "Release contract audit",
                "profile": "standard",
            })}]},
        }
        with mock.patch("client.runner.save_active_run") as hosted_save, \
             mock.patch("client.runner.save_local_advisory_run") as local_save, \
             mock.patch("client.runner.launch_stopper_if_available") as launch:
            shim._spawn_stopper_for_audit(response)

        hosted_save.assert_called_once()
        local_save.assert_not_called()
        launch.assert_called_once_with(prefer_shipped=False)

    def test_spawn_ignores_synchronous_workflow_result_without_active_status(self):
        envelope = {
            "schema_version": "1.0",
            "skill": "audit-adjudication",
            "run_id": "adj-sync-result",
            "payload": {
                "run_id": "adj-sync-result",
                "title": "Synchronous adjudication",
                "rows": [{"decision": "accepted"}],
            },
        }
        response = {
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(envelope)}
                ]
            }
        }

        with mock.patch("client.runner.save_active_run") as save, \
             mock.patch("client.runner.launch_stopper_if_available") as launch:
            shim._spawn_stopper_for_audit(response)

        save.assert_not_called()
        launch.assert_not_called()

    def test_spawn_ignores_terminal_submit_payloads(self):
        for status in ("completed", "failed", "cancelled"):
            with self.subTest(status=status), \
                 mock.patch("client.runner.save_active_run") as save, \
                 mock.patch("client.runner.launch_stopper_if_available") as launch:
                shim._spawn_stopper_for_audit(
                    _submit_result(run_id="terminal-run", status=status)
                )
                save.assert_not_called()
                launch.assert_not_called()

    def test_spawn_accepts_any_nonterminal_submit_status(self):
        for status in ("queued", "running", "pending", "initializing", " Running "):
            with self.subTest(status=status), \
                 mock.patch("client.runner.save_active_run") as save, \
                 mock.patch("client.runner.launch_stopper_if_available") as launch:
                shim._spawn_stopper_for_audit(
                    _submit_result(run_id="active-run", status=status)
                )
                save.assert_called_once()
                launch.assert_called_once_with(prefer_shipped=False)

    def test_spawn_seeds_de_lite_run_through_local_registry(self):
        response = {
            "result": {"content": [{"type": "text", "text": json.dumps({
                "schema_version": "1.0",
                "skill": "audit",
                "run_id": "local_abc",
                "payload": {
                    "run_id": "local_abc",
                    "status": "running",
                    "title": "Lite review",
                    "local": True,
                    "local_surface": "de_lite",
                    "fallback_mode": "session-llm",
                },
            })}]},
        }
        with mock.patch("client.runner.save_active_run") as hosted_save, \
             mock.patch("client.runner.save_local_advisory_run") as local_save, \
             mock.patch("client.runner.launch_stopper_if_available") as launch:
            shim._spawn_stopper_for_audit(response)

        hosted_save.assert_not_called()
        local_save.assert_called_once_with(
            "local_abc", title="Lite review", status="running", surface="de_lite"
        )
        launch.assert_called_once_with(prefer_shipped=False)

    def test_skip_stopper_launch_prevents_registry_and_gui_side_effects(self):
        with mock.patch.dict(os.environ, {"DE_SKIP_STOPPER_LAUNCH": "1"}), \
             mock.patch("client.runner.save_active_run") as hosted_save, \
             mock.patch("client.runner.save_local_advisory_run") as local_save, \
             mock.patch("client.runner.launch_stopper_if_available") as launch:
            shim._spawn_stopper_for_audit(_submit_result(run_id="must-not-persist"))

        hosted_save.assert_not_called()
        local_save.assert_not_called()
        launch.assert_not_called()

    def test_spawn_swallows_errors(self):
        # a failure in the panel launch must never propagate (the transport thread is upstream)
        with mock.patch("client.runner.save_active_run", side_effect=OSError("boom")), \
             mock.patch("client.runner.launch_stopper_if_available"):
            shim._spawn_stopper_for_audit(_submit_result())  # must not raise

    def test_gate_set_contains_both_submit_tools(self):
        # Both the skill-workflow path and the raw hub tool must arm the stop panel — an agent may
        # reach the hub through either. Locking the contract guards against a silent narrowing.
        self.assertEqual(shim._AUDIT_SUBMIT_TOOLS,
                         frozenset({"audit_skill_submit", "submit_audit"}))

    def test_audit_run_payload_handles_both_envelope_shapes(self):
        # Flat envelope (the raw `submit_audit` return): run_id at the top level.
        self.assertEqual(shim._audit_run_payload(_submit_result(run_id="flat1"))["run_id"], "flat1")
        # Skill-workflow envelope (`audit_skill_submit`) carries extra fields alongside a top-level
        # run_id; the shared parser must still surface it (this is what arms the panel on both paths).
        skill_text = json.dumps({"skill": "audit", "run_id": "skill9", "status": "queued",
                                 "schema_version": 1})
        skill = {"jsonrpc": "2.0", "id": 1,
                 "result": {"content": [{"type": "text", "text": skill_text}]}}
        self.assertEqual(shim._audit_run_payload(skill)["run_id"], "skill9")

    def test_serve_gate_spawns_panel_for_both_submit_tools_and_nothing_else(self):
        # The serve() gate is the actual behavior under change: a tools/call named audit_skill_submit
        # OR submit_audit must fire the stop-panel spawn; any other tool must not. Run the spawn
        # synchronously (fake Thread) so the assertion never races the daemon thread.
        class _SyncThread:
            def __init__(self, target=None, args=(), **kw):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

        def spawn_calls_for(name):
            resp = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text",
                    "text": json.dumps({"run_id": "r1", "status": "queued"})}]}}
            fwd = FakeForwarder(responses={"tools/call": resp})
            arguments = {}
            if name == "audit_skill_submit":
                arguments = {"skill_name": "audit", "args": {
                    "title": "Explicit audit", "content": "artifact"}}
            elif name in {"submit_audit", "de_audit"}:
                arguments = {"title": "Explicit audit"}
            req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
            with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, \
                 mock.patch.object(shim.threading, "Thread", _SyncThread):
                _run(fwd, [json.dumps(req)], client_host="claude")
            return spawn.call_count

        for name in ("audit_skill_submit", "submit_audit"):
            self.assertEqual(spawn_calls_for(name), 1, "expected a panel spawn for %s" % name)
        for name in ("de_audit", "wait_audit", "list_running_audits"):
            self.assertEqual(spawn_calls_for(name), 0, "unexpected panel spawn for %s" % name)

    def test_trae_hosts_enable_display_without_popup_followup_and_spawn_stop_panel(self):
        """TRAE opens shared popups without enabling the deferred follow-up chat."""

        class _SyncThread:
            def __init__(self, target=None, args=(), **kw):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

            def join(self, timeout=None):
                return None

        for client_host in ("trae", "trae-work", "trae-cn", "trae-work-cn"):
            with self.subTest(client_host=client_host):
                listed = _run(
                    FakeForwarder(responses={"tools/list": {"jsonrpc": "2.0", "id": 1,
                        "result": {"tools": []}}}),
                    [json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list", "params": {}})],
                    client_host=client_host,
                )
                names = {tool["name"] for tool in listed[0]["result"]["tools"]}
                self.assertTrue(
                    {"open_ge", "open_ge_popup", "open_db_board", "db_board_result"}
                    <= names
                )

                display = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "open_ge_popup",
                                      "arguments": {"run_id": "ge-run"}}}
                captured = []

                def handle(_forwarder, name, args, **kwargs):
                    captured.append((name, args, kwargs))
                    return {"status": "open", "popup_id": "trae-popup"}

                with mock.patch.object(shim, "_handle_display_call", side_effect=handle), \
                     mock.patch.object(shim.threading, "Thread", _SyncThread):
                    _run(FakeForwarder(), [json.dumps(display)], client_host=client_host)
                self.assertEqual(captured, [(
                    "open_ge_popup",
                    {"run_id": "ge-run"},
                    {"client_host": client_host, "popup_followup": False,
                     "popup_api_profile": "legacy"},
                )])

                response = _submit_result(run_id="trae-run")
                request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "audit_skill_submit", "arguments": {
                               "skill_name": "audit", "args": {
                                   "title": "Explicit audit", "content": "artifact"}}}}
                initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"clientInfo": {"name": "Trae",
                                                         "version": "1.107.1"}}}
                with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn, \
                     mock.patch.object(shim.threading, "Thread", _SyncThread):
                    _run(
                        FakeForwarder(responses={
                            "initialize": {"jsonrpc": "2.0", "id": 1, "result": {}},
                            "tools/call": response,
                        }),
                        [json.dumps(initialize), json.dumps(request)],
                        client_host=client_host,
                    )
                spawn.assert_called_once_with(response)

                for non_submit in ("de_audit", "wait_audit", "list_running_audits"):
                    request["params"]["name"] = non_submit
                    with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
                        _run(
                            FakeForwarder(responses={"tools/call": response}),
                            [json.dumps(request)],
                            client_host=client_host,
                        )
                    spawn.assert_not_called()

    def _thread_targets(self, tool_name, result):
        fwd = FakeForwarder(responses={"tools/call": result})
        arguments = {}
        if tool_name == "audit_skill_submit":
            arguments = {"skill_name": "audit", "args": {
                "title": "Explicit audit", "content": "artifact"}}
        elif tool_name in {"submit_audit", "de_audit"}:
            arguments = {"title": "Explicit audit"}
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}}
        with mock.patch.object(shim.threading, "Thread") as T:   # capture, don't run (deterministic)
            _run(fwd, [json.dumps(call)], client_host="claude")
        return [c.kwargs.get("target") for c in T.call_args_list]

    def test_only_audit_submit_spawns_the_panel(self):
        self.assertIn(shim._spawn_stopper_for_audit,
                      self._thread_targets("audit_skill_submit", _submit_result(run_id="r1")))
        self.assertNotIn(shim._spawn_stopper_for_audit,
                         self._thread_targets("de_audit", _submit_result(run_id="r1")))

    def test_panel_spawn_is_a_daemon(self):
        fwd = FakeForwarder(responses={"tools/call": _submit_result(run_id="r1")})
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "audit_skill_submit", "arguments": {
                    "skill_name": "audit", "args": {
                        "title": "Explicit audit", "content": "artifact"}}}}
        with mock.patch.object(shim.threading, "Thread") as T:
            _run(fwd, [json.dumps(call)], client_host="claude")
        spawn = [c for c in T.call_args_list if c.kwargs.get("target") == shim._spawn_stopper_for_audit]
        self.assertEqual(len(spawn), 1)
        self.assertTrue(spawn[0].kwargs.get("daemon"))

    def test_thread_start_failure_does_not_break_transport(self):
        # Thread.start() runs on the transport thread and CAN raise (RuntimeError under thread
        # exhaustion) — a failed panel spawn must NOT stop the JSON-RPC reply from being emitted.
        class _BoomThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        fwd = FakeForwarder(responses={"tools/call": _submit_result(run_id="r1")})
        call = {"jsonrpc": "2.0", "id": 42, "method": "tools/call",
                "params": {"name": "audit_skill_submit", "arguments": {
                    "skill_name": "audit", "args": {
                        "title": "Explicit audit", "content": "artifact"}}}}
        with mock.patch.object(shim.threading, "Thread", _BoomThread):
            out = _run(fwd, [json.dumps(call)], client_host="claude")   # must not raise
        self.assertEqual(len(out), 1)
        self.assertNotIn("error", out[0])         # the submit response is still delivered
        self.assertIn("content", out[0]["result"])


def _status_result(status):
    """The MCP result envelope `visual_status` returns — status lives under `payload`."""
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text",
                       "text": json.dumps({"payload": {"status": status}})}]}}


class _GeFakeForwarder:
    """Records sent /mcp messages; answers visual submit + status polls from
    canned envelopes so open_ge's submit→poll→fetch orchestration is exercised with no network."""

    def __init__(self, *, statuses, run_id="vis_run_1", submit_run_id="vis_run_1",
                 raise_on_submit=False, raise_on_status=False, raise_on_fetch=False):
        self.endpoint = "https://hub.example"
        self.token = "synthetic-device-token"
        self.sent = []
        self._statuses = list(statuses)
        self._run_id = run_id
        self._submit_run_id = submit_run_id
        self._raise_on_submit = raise_on_submit
        self._raise_on_status = raise_on_status
        self._raise_on_fetch = raise_on_fetch
        self.fetched = []

    def forward(self, message, *, timeout_s=None):
        self.sent.append(message)
        name = (message.get("params") or {}).get("name")
        if name == "visual_render":
            if self._raise_on_submit:
                raise config.ShellError("boom: visual_render submit")
            if not self._submit_run_id:   # submit returns an envelope with NO run_id
                return {"jsonrpc": "2.0", "id": 1,
                        "result": {"content": [{"type": "text", "text": json.dumps({"status": "queued"})}]}}
            return _submit_result(run_id=self._submit_run_id)
        if name == "visual_status":
            if self._raise_on_status:
                raise config.ShellError("boom: visual_status")
            status = self._statuses.pop(0) if self._statuses else "pending"
            return _status_result(status)
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}

    def get_ge_artifact(self, run_id, *, timeout_s=None):
        if self._raise_on_fetch:
            raise config.ShellError("boom: get_ge_artifact")
        self.fetched.append(run_id)
        return {"kind": "svg", "data": "<svg/>"}


class OpenGeDisplayTestCase(unittest.TestCase):
    """`open_ge` collapses visual_render → visual_status → fetch → spawn into ONE model-facing call,
    reusing the metered async submit path (never a direct render), and fails closed on bad input /
    bad shapes / a still-pending render."""

    def setUp(self):
        # Stub the lazy popup deps so `_handle_display_call`'s top-level
        # `from client.popup import launcher, session` resolves without a GUI.
        self.spawned = []

        launcher = types.ModuleType("client.popup.launcher")

        class PopupSpec:
            def __init__(self, kind=None, title=None, artifact=None):
                self.kind, self.title, self.artifact = kind, title, artifact

        launcher.PopupSpec = PopupSpec
        launcher.render_artifact_html = lambda spec: "<html>%s</html>" % (spec.artifact or {}).get("kind")
        launcher.render_cursor_artifact_html = lambda _spec: "<html>cursor-reviewed</html>"
        launcher.BoardFetchError = type("BoardFetchError", (Exception,), {})
        launcher.fetch_board_html = lambda *a, **k: "<html/>"

        notice = types.ModuleType("client.popup.notice")
        notice.render_ge_terminal_notice = (
            lambda status, run_id: "<notice status=%s run=%s>" % (status, run_id)
        )

        session = types.ModuleType("client.popup.session")
        self.context_calls = []
        self.spawn_kwargs = []

        def build_context(*args, **kwargs):
            self.context_calls.append((args, kwargs))
            return {"ctx": True}

        session.build_ge_context_bundle = build_context

        def _spawn(html, title, chat_context=None, **kwargs):
            self.spawned.append((html, title, chat_context))
            self.spawn_kwargs.append(kwargs)
            return {"status": "opened", "popup_id": "pop_1"}

        session.spawn = _spawn
        session.poll = lambda *a, **k: {"status": "open"}

        popup_pkg = types.ModuleType("client.popup")
        popup_pkg.launcher = launcher
        popup_pkg.notice = notice
        popup_pkg.session = session

        self._mods = {
            "client.popup": popup_pkg,
            "client.popup.launcher": launcher,
            "client.popup.notice": notice,
            "client.popup.session": session,
        }
        self._saved = {k: sys.modules.get(k) for k in self._mods}
        sys.modules.update(self._mods)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_open_ge_foreground_budget_is_ten_minutes(self):
        self.assertEqual(shim._GE_OPEN_TOTAL_BUDGET_S, 10 * 60.0)
        self.assertGreaterEqual(shim._GE_RENDER_POLL_DEADLINE_S, 9 * 60.0)
        self.assertLessEqual(
            shim._GE_RENDER_POLL_DEADLINE_S,
            shim._GE_OPEN_TOTAL_BUDGET_S - shim._GE_MIN_FETCH_RESERVE_S,
        )

    def test_open_ge_schema_covers_every_mode_and_bounds_the_retry_key(self):
        # STRUCTURE lives on the base constant (enum / bounds); DESCRIPTION prose is overlaid per
        # locale at assembly, so assert it on the en-US localized tool (see McpToolLocalizationTests).
        base = next(
            item for item in shim._DISPLAY_TOOL_SCHEMAS if item["name"] == "open_ge"
        )
        request_id = base["inputSchema"]["properties"]["client_request_id"]
        self.assertEqual(request_id["minLength"], 1)
        self.assertEqual(request_id["maxLength"], 128)
        self.assertEqual(base["inputSchema"]["properties"]["mode"]["enum"],
                         ["comic", "infographic", "diagram"])
        schema = shim._localize_tool(base, "en-US")
        self.assertIn("comic, infographic, or diagram", schema["description"])
        mode_description = schema["inputSchema"]["properties"]["mode"]["description"]
        self.assertIn("every mode", mode_description)
        self.assertIn("request_outcome_unknown", schema["description"])
        self.assertIn(
            "reuse",
            schema["inputSchema"]["properties"]["client_request_id"]["description"].lower())

    def test_open_ge_happy_path_submits_polls_and_spawns(self):
        fwd = _GeFakeForwarder(statuses=["pending", "completed"], submit_run_id="vis_run_1")
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(
                fwd, "open_ge", {"mode": "diagram", "spec": {"title": "T"}, "title": "T"})
        self.assertEqual(out.get("popup_id"), "pop_1")
        names = [(m.get("params") or {}).get("name") for m in fwd.sent]
        self.assertIn("visual_render", names)        # metered submit path used, not a direct render
        self.assertIn("visual_status", names)        # polled to terminal
        self.assertEqual(fwd.fetched, ["vis_run_1"])  # artifact fetched by run_id (byte-isolated)
        self.assertEqual(len(self.spawned), 1)

    def test_open_ge_uses_minimal_visual_contract_and_server_run_id(self):
        fwd = _GeFakeForwarder(
            statuses=["completed"], submit_run_id="vis_987654321",
        )
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(
                fwd, "open_ge", {"mode": "diagram", "spec": {"title": "T"}},
            )
        calls = [message["params"] for message in fwd.sent]
        self.assertEqual(
            [call["name"] for call in calls],
            ["visual_render", "visual_status"],
        )
        self.assertRegex(
            calls[0]["arguments"]["client_request_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertEqual(calls[1], {
            "name": "visual_status", "arguments": {"run_id": "vis_987654321"},
        })
        self.assertEqual(fwd.fetched, ["vis_987654321"])
        self.assertEqual(out.get("popup_id"), "pop_1")

    def test_open_ge_forwards_an_explicit_request_id_unchanged(self):
        fwd = _GeFakeForwarder(statuses=["completed"], submit_run_id="vis_explicit")
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            shim._handle_display_call(
                fwd,
                "open_ge",
                {
                    "mode": "comic",
                    "spec": {"comic_spec_version": 2, "panels": []},
                    "client_request_id": "retry-key-1",
                },
            )
        self.assertEqual(
            fwd.sent[0]["params"]["arguments"]["client_request_id"],
            "retry-key-1",
        )

    def test_open_ge_rejects_invalid_request_ids_without_submitting(self):
        for value in ("", "   ", "x" * 129, 7):
            with self.subTest(value=value):
                fwd = _GeFakeForwarder(statuses=[])
                out = shim._handle_display_call(
                    fwd,
                    "open_ge",
                    {"mode": "diagram", "spec": {}, "client_request_id": value},
                )
                self.assertEqual(out, {
                    "status": "failed", "reason": "invalid-client-request-id",
                })
                self.assertEqual(fwd.sent, [])

    def test_open_ge_accepts_request_id_boundary_and_normalizes_whitespace(self):
        for supplied, forwarded in (("x" * 128, "x" * 128), (" retry-key-1 ", "retry-key-1")):
            with self.subTest(supplied=supplied):
                fwd = _GeFakeForwarder(statuses=["completed"], submit_run_id="vis_boundary")
                with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                        mock.patch.object(shim.time, "sleep"):
                    shim._handle_display_call(
                        fwd,
                        "open_ge",
                        {"mode": "diagram", "spec": {}, "client_request_id": supplied},
                    )
                self.assertEqual(
                    fwd.sent[0]["params"]["arguments"]["client_request_id"], forwarded,
                )

    def test_cursor_open_ge_uses_shared_renderer_and_followup(self):
        fwd = _GeFakeForwarder(
            statuses=["completed"], submit_run_id="ge_run_cursor"
        )
        spawn = mock.Mock(
            return_value={"status": "opened", "popup_id": "pop_cursor"}
        )
        sys.modules["client.popup.session"].spawn = spawn

        with mock.patch.object(shim, "_ge_chat_transport", return_value="legacy"), \
                mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(
                fwd,
                "open_ge",
                {"mode": "diagram", "spec": {"title": "T"}},
                client_host="cursor",
            )

        self.assertEqual(out.get("popup_id"), "pop_cursor")
        self.assertEqual(spawn.call_args.args[0], "<html>svg</html>")
        self.assertEqual(self.context_calls[-1][1], {"caller": "cursor"})
        self.assertNotIn("api_profile", spawn.call_args.kwargs)

    def test_codex_client_host_is_added_to_followup_context(self):
        fwd = _GeFakeForwarder(statuses=["completed"], submit_run_id="ge_run_codex")
        with mock.patch.object(shim, "_ge_chat_transport", return_value="legacy"), \
                mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(
                fwd,
                "open_ge",
                {"mode": "diagram", "spec": {"title": "T"}},
                client_host="codex",
            )

        self.assertEqual(out.get("popup_id"), "pop_1")
        self.assertEqual(self.context_calls[-1][1], {"caller": "codex"})

    def test_codex_client_host_wins_over_spoofed_caller_on_popup_path(self):
        fwd = _GeFakeForwarder(statuses=[])
        with mock.patch.object(shim, "_ge_chat_transport", return_value="legacy"):
            out = shim._handle_display_call(
                fwd,
                "open_ge_popup",
                {"run_id": "ge_existing", "caller": "claude"},
                client_host="codex",
            )

        self.assertEqual(out.get("popup_id"), "pop_1")
        self.assertEqual(fwd.fetched, ["ge_existing"])
        self.assertEqual(self.context_calls[-1][1], {"caller": "codex"})

    def test_followup_false_skips_context_for_both_ge_display_paths(self):
        popup_fwd = _GeFakeForwarder(statuses=[])
        popup = shim._handle_display_call(
            popup_fwd,
            "open_ge_popup",
            {"run_id": "ge_existing", "context": "must-not-be-routed"},
            client_host="trae-work",
            popup_followup=False,
        )

        open_fwd = _GeFakeForwarder(
            statuses=["completed"], submit_run_id="ge_new"
        )
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            opened = shim._handle_display_call(
                open_fwd,
                "open_ge",
                {"mode": "diagram", "spec": {"title": "T"},
                 "context": "must-not-be-routed"},
                client_host="trae-work",
                popup_followup=False,
            )

        self.assertEqual(popup.get("popup_id"), "pop_1")
        self.assertEqual(opened.get("popup_id"), "pop_1")
        self.assertEqual(self.context_calls, [])
        self.assertEqual(
            [chat_context for _html, _title, chat_context in self.spawned],
            [None, None],
        )

    def test_open_ge_pending_after_deadline_returns_run_id_without_fetch(self):
        fwd = _GeFakeForwarder(statuses=["pending", "pending"], submit_run_id="ge_run_2")
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 0.02), \
                mock.patch.object(shim, "_schedule_ge_auto_open", return_value=True) as schedule, \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(
                fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}, "context": "ctx"},
                client_host="trae-work", popup_followup=False)
        self.assertEqual(out.get("status"), "scheduled")
        self.assertEqual(out.get("run_id"), "ge_run_2")
        schedule.assert_called_once_with(
            fwd,
            "ge_run_2",
            {"context": "ctx", "title": "Decision Engine"},
            client_host="trae-work",
            popup_followup=False,
            popup_api_profile="legacy",
        )
        self.assertEqual(fwd.fetched, [])            # no fetch on the pending path
        self.assertEqual(self.spawned, [])           # no popup

    def test_scheduled_ge_auto_open_waits_then_opens_without_another_tool_call(self):
        fwd = _GeFakeForwarder(statuses=["pending", "completed"])

        class _SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, **_ignored):
                self._target = target
                self._args = args
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        with mock.patch.object(shim.threading, "Thread", _SyncThread), \
                mock.patch.object(shim.time, "sleep"):
            scheduled = shim._schedule_ge_auto_open(
                fwd,
                "ge_slow",
                {"title": "Slow diagram", "context": "must-not-be-routed"},
                client_host="trae-work",
                popup_followup=False,
                popup_api_profile="legacy",
            )

        self.assertTrue(scheduled)
        self.assertEqual(fwd.fetched, ["ge_slow"])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][1:], ("Slow diagram", None))
        self.assertEqual(self.context_calls, [])

    def test_scheduled_ge_failure_opens_native_notice_instead_of_going_silent(self):
        class _SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, **_ignored):
                self._target = target
                self._args = args
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        for client_host in ("trae-work", "qoder"):
            with self.subTest(client_host=client_host):
                fwd = _GeFakeForwarder(statuses=["failed"])
                self.spawned.clear()
                with mock.patch.object(shim.threading, "Thread", _SyncThread), \
                        mock.patch.object(shim.time, "sleep"):
                    scheduled = shim._schedule_ge_auto_open(
                        fwd,
                        "ge_failed",
                        {"title": "Failed diagram"},
                        client_host=client_host,
                        popup_followup=False,
                        popup_api_profile="legacy",
                    )

                self.assertTrue(scheduled)
                self.assertEqual(fwd.fetched, [])
                self.assertEqual(
                    self.spawned,
                    [("<notice status=failed run=ge_failed>", "Failed diagram", None)],
                )

    def test_ge_auto_open_real_threads_are_daemon_capped_and_release_slots(self):
        release = threading.Event()
        two_started = threading.Event()
        all_done = threading.Event()
        state = {"started": 0, "done": 0, "threads": []}
        state_lock = threading.Lock()

        def blocking_worker(_forwarder, run_id, _popup_args, cancel_event, **_kwargs):
            with state_lock:
                state["started"] += 1
                state["threads"].append((threading.current_thread().daemon,
                                         threading.current_thread().name))
                if state["started"] == 2:
                    two_started.set()
            release.wait(2)
            with shim._GE_AUTO_OPEN_LOCK:
                if shim._GE_AUTO_OPEN_CANCEL.get(run_id) is cancel_event:
                    shim._GE_AUTO_OPEN_CANCEL.pop(run_id, None)
            shim._GE_AUTO_OPEN_SLOTS.release()
            with state_lock:
                state["done"] += 1
                if state["done"] == 3:
                    all_done.set()

        isolated_slots = threading.BoundedSemaphore(2)
        with mock.patch.object(shim, "_GE_AUTO_OPEN_SLOTS", isolated_slots), \
                mock.patch.object(shim, "_GE_AUTO_OPEN_CANCEL", {}), \
                mock.patch.object(shim, "_run_ge_auto_open", side_effect=blocking_worker):
            for run_id in ("ge_cap_1", "ge_cap_2"):
                self.assertTrue(shim._schedule_ge_auto_open(
                    object(), run_id, {}, client_host="trae-work",
                    popup_followup=False, popup_api_profile="legacy"))
            self.assertTrue(two_started.wait(1))
            self.assertFalse(shim._schedule_ge_auto_open(
                object(), "ge_cap_3", {}, client_host="trae-work",
                popup_followup=False, popup_api_profile="legacy"))
            release.set()
            deadline = time.monotonic() + 1
            while state["done"] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(state["done"], 2)
            self.assertTrue(shim._schedule_ge_auto_open(
                object(), "ge_cap_4", {}, client_host="trae-work",
                popup_followup=False, popup_api_profile="legacy"))
            self.assertTrue(all_done.wait(1))

        self.assertEqual(state["threads"], [(True, "de-ge-auto-open")] * 3)

    def test_explicit_popup_claim_supersedes_background_and_blocks_concurrent_duplicate(self):
        cancel = threading.Event()
        with shim._GE_AUTO_OPEN_LOCK:
            shim._GE_AUTO_OPEN_CANCEL["ge_claim"] = cancel
        try:
            self.assertTrue(shim._claim_ge_popup("ge_claim", cancel_background=True))
            self.assertTrue(cancel.is_set())
            self.assertFalse(shim._claim_ge_popup("ge_claim", cancel_background=False))
        finally:
            shim._release_ge_popup("ge_claim")
            with shim._GE_AUTO_OPEN_LOCK:
                shim._GE_AUTO_OPEN_CANCEL.pop("ge_claim", None)

    def test_open_ge_auto_open_capacity_failure_preserves_manual_fallback(self):
        fwd = _GeFakeForwarder(statuses=["pending", "pending"], submit_run_id="ge_busy")
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 0.02), \
                mock.patch.object(shim, "_schedule_ge_auto_open", return_value=False), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(
                fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})

        self.assertEqual(out.get("status"), "pending")
        self.assertIn("auto-open unavailable", out.get("hint", ""))
        self.assertEqual(fwd.fetched, [])
        self.assertEqual(self.spawned, [])

    def test_open_ge_failed_render_returns_failed_without_spawn(self):
        fwd = _GeFakeForwarder(statuses=["failed"], submit_run_id="ge_run_3")
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(
                fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})
        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(out.get("run_id"), "ge_run_3")
        self.assertEqual(self.spawned, [])

    def test_open_ge_bad_input_fails_closed_without_submitting(self):
        fwd = _GeFakeForwarder(statuses=[])
        out_mode = shim._handle_display_call(fwd, "open_ge", {"mode": "bogus", "spec": {}})
        out_spec = shim._handle_display_call(fwd, "open_ge", {"mode": "diagram", "spec": "nope"})
        self.assertEqual(out_mode.get("status"), "failed")
        self.assertEqual(out_spec.get("status"), "failed")
        self.assertEqual(fwd.sent, [])               # nothing submitted for invalid input

    def test_open_ge_submit_error_fails_closed(self):
        fwd = _GeFakeForwarder(statuses=[], raise_on_submit=True)
        out = shim._handle_display_call(
            fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})
        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(self.spawned, [])

    def test_open_ge_submit_outcome_unknown_returns_same_key_for_safe_retry(self):
        fwd = _GeFakeForwarder(statuses=[])
        fwd.forward = mock.Mock(
            side_effect=shim.OutcomeUnknownError("read_timeout", "b" * 32)
        )

        out = shim._handle_display_call(
            fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}}
        )

        self.assertEqual(out, {
            "status": "request_outcome_unknown",
            "reason": "read_timeout",
            "request_id": "b" * 32,
            "request_sent": True,
            "retryable": True,
            "action": "retry",
            "client_request_id": mock.ANY,
        })
        self.assertRegex(out["client_request_id"], r"^[0-9a-f-]{36}$")
        fwd.forward.assert_called_once()
        self.assertEqual(self.spawned, [])

        retry = _GeFakeForwarder(statuses=["completed"], submit_run_id="vis_retry")
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            retried = shim._handle_display_call(
                retry,
                "open_ge",
                {
                    "mode": "diagram",
                    "spec": {"x": 1},
                    "client_request_id": out["client_request_id"],
                },
            )
        sent_id = retry.sent[0]["params"]["arguments"]["client_request_id"]
        self.assertEqual(sent_id, out["client_request_id"])
        self.assertEqual(retried.get("popup_id"), "pop_1")

    def test_open_ge_submit_without_run_id_is_rejected(self):
        fwd = _GeFakeForwarder(statuses=[], submit_run_id=None)   # submit envelope carries no run_id
        out = shim._handle_display_call(
            fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})
        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(out.get("reason"), "submit-rejected")
        self.assertEqual(fwd.fetched, [])
        self.assertEqual(self.spawned, [])

    def test_open_ge_cancelled_status_fails_closed(self):
        fwd = _GeFakeForwarder(statuses=["cancelled"], submit_run_id="ge_run_c")
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})
        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(out.get("reason"), "render-cancelled")
        self.assertEqual(out.get("run_id"), "ge_run_c")
        self.assertEqual(self.spawned, [])

    def test_open_ge_artifact_fetch_failure_fails_closed(self):
        fwd = _GeFakeForwarder(statuses=["completed"], submit_run_id="ge_run_f", raise_on_fetch=True)
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})
        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(out.get("reason"), "artifact-fetch-failed")
        self.assertEqual(out.get("run_id"), "ge_run_f")
        self.assertEqual(self.spawned, [])

    def test_open_ge_popup_fetch_failure_keeps_supplied_run_id(self):
        fwd = _GeFakeForwarder(statuses=[], raise_on_fetch=True)

        out = shim._handle_display_call(
            fwd, "open_ge_popup", {"run_id": "ge_popup_f"}
        )

        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(out.get("reason"), "artifact-fetch-failed")
        self.assertEqual(out.get("run_id"), "ge_popup_f")
        self.assertEqual(self.spawned, [])

    def test_open_ge_render_failure_fails_closed(self):
        fwd = _GeFakeForwarder(statuses=["completed"], submit_run_id="ge_run_r")

        def _boom(_spec):
            raise ValueError("bad artifact")

        sys.modules["client.popup.launcher"].render_artifact_html = _boom
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})
        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(out.get("reason"), "render-failed")
        self.assertEqual(out.get("run_id"), "ge_run_r")
        self.assertEqual(self.spawned, [])

    def test_open_ge_spawn_failure_keeps_run_id_for_user_recovery(self):
        fwd = _GeFakeForwarder(statuses=["completed"], submit_run_id="ge_run_spawn")
        sys.modules["client.popup.session"].spawn = (
            lambda *_args, **_kwargs: {"status": "failed", "reason": "launch-failed"}
        )
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(
                fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}}
            )

        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(out.get("reason"), "launch-failed")
        self.assertEqual(out.get("run_id"), "ge_run_spawn")

    def test_open_ge_transient_poll_error_retries_then_completes(self):
        # A momentary poll blip must NOT abort the fast path — the next poll completes it.
        fwd = _GeFakeForwarder(statuses=["completed"], submit_run_id="ge_run_t")
        original_forward = fwd.forward
        state = {"first_status": True}

        def flaky_forward(message, *, timeout_s=None):
            name = (message.get("params") or {}).get("name")
            if name == "visual_status" and state["first_status"]:
                state["first_status"] = False
                fwd.sent.append(message)
                raise config.ShellError("transient blip")
            return original_forward(message, timeout_s=timeout_s)

        fwd.forward = flaky_forward
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 5.0), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})
        self.assertEqual(out.get("popup_id"), "pop_1")   # retried past the blip and opened
        self.assertEqual(fwd.fetched, ["ge_run_t"])

    def test_open_ge_persistent_poll_error_returns_pending_at_deadline(self):
        fwd = _GeFakeForwarder(statuses=[], submit_run_id="ge_run_p", raise_on_status=True)
        with mock.patch.object(shim, "_GE_RENDER_POLL_DEADLINE_S", 0.02), \
                mock.patch.object(shim, "_schedule_ge_auto_open", return_value=False), \
                mock.patch.object(shim, "_log"), \
                mock.patch.object(shim.time, "sleep"):
            out = shim._handle_display_call(fwd, "open_ge", {"mode": "diagram", "spec": {"x": 1}})
        self.assertEqual(out.get("status"), "pending")   # never crashes; hands off to open_ge_popup
        self.assertEqual(out.get("run_id"), "ge_run_p")
        self.assertEqual(fwd.fetched, [])
        self.assertEqual(self.spawned, [])

    def test_open_ge_non_string_mode_fails_closed_without_submitting(self):
        fwd = _GeFakeForwarder(statuses=[])
        out = shim._handle_display_call(fwd, "open_ge", {"mode": 1, "spec": {"x": 1}})
        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(fwd.sent, [])   # a non-string mode must not reach a submit / crash the worker


class McpRequestDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "decision-engine" / "config.json"
        self.env = mock.patch.dict(
            os.environ,
            {"DE_CONFIG_PATH": str(self.config_path)},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def _successful_opener():
        fake = mock.MagicMock()
        response = fake.__enter__.return_value
        response.read1.side_effect = [b'{"result":{"ok":true}}', b""]
        response.chunked = False
        response.length = 0
        opener = mock.MagicMock()
        opener.open.return_value = fake
        return opener

    def test_log_path_is_below_the_managed_runtime_logs_directory(self):
        self.assertEqual(
            shim.mcp_request_log_path(),
            self.config_path.parent / ".runtime" / "logs" / "mcp-requests.jsonl",
        )

    def test_forward_writes_privacy_safe_unicode_diagnostics_before_http(self):
        secret = "PRIVATE-CONTENT-MUST-NOT-ENTER-THE-LOG"
        message = {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                "name": "visual_render",
                "arguments": {
                    "mode": "comic",
                    "title": "乱码排查",
                    "spec": "第一格：中文正常？ ASCII ? 替换符\ufffd %s" % secret,
                },
            },
        }
        serialized = json.dumps(message).encode("utf-8")
        opener = self._successful_opener()

        def assert_log_exists_before_http(*_args, **_kwargs):
            self.assertTrue(shim.mcp_request_log_path().is_file())
            return opener

        with mock.patch.object(shim, "hub_reachable", return_value=True), \
             mock.patch.object(shim, "_request_outcome_id", return_value="a" * 32), \
             mock.patch.object(shim, "build_opener", side_effect=assert_log_exists_before_http):
            result = shim.Forwarder("https://hub.example.com", "DEVICE-TOKEN-NOT-IN-LOG").forward(
                message
            )

        self.assertEqual(result, {"result": {"ok": True}})
        raw_log = shim.mcp_request_log_path().read_text(encoding="utf-8")
        self.assertNotIn(secret, raw_log)
        self.assertNotIn("乱码排查", raw_log)
        self.assertNotIn("第一格", raw_log)
        self.assertNotIn("DEVICE-TOKEN-NOT-IN-LOG", raw_log)
        self.assertNotIn("hub.example.com", raw_log)
        entry = json.loads(raw_log)
        self.assertEqual(entry["event"], "mcp_request_preflight")
        self.assertEqual(entry["rpc_id"], 23)
        self.assertEqual(entry["method"], "tools/call")
        self.assertEqual(entry["tool_name"], "visual_render")
        self.assertEqual(entry["payload_bytes"], len(serialized))
        self.assertEqual(entry["payload_sha256"], hashlib.sha256(serialized).hexdigest())
        self.assertEqual(entry["request_id"], "a" * 32)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-request-id"), "a" * 32)
        self.assertEqual(entry["fields"]["spec"]["ascii_question_marks"], 1)
        self.assertEqual(entry["fields"]["spec"]["replacement_characters"], 1)
        self.assertEqual(entry["fields"]["spec"]["cjk_characters"], 10)
        self.assertEqual(entry["fields"]["title"]["cjk_characters"], 4)
        self.assertEqual(entry["arguments"]["replacement_characters"], 1)
        self.assertGreater(entry["arguments"]["cjk_characters"], 0)
        self.assertNotIn("sha256", entry["arguments"])
        self.assertNotIn("sha256", entry["fields"]["title"])

    def test_arbitrary_protocol_metadata_is_not_written_raw(self):
        sentinel = "PRIVATE-METADATA-MUST-NOT-ENTER-THE-LOG"
        message = {
            "jsonrpc": "2.0",
            "id": sentinel,
            "method": sentinel,
            "params": {"name": sentinel, "arguments": {}},
        }
        shim._append_mcp_request_diagnostic(message, json.dumps(message).encode("utf-8"))

        raw_log = shim.mcp_request_log_path().read_text(encoding="utf-8")
        self.assertNotIn(sentinel, raw_log)
        entry = json.loads(raw_log)
        self.assertEqual(entry["rpc_id"], "<string>")
        self.assertEqual(entry["method"], "<other>")
        self.assertEqual(entry["tool_name"], "<other>")

    def test_large_diagnostic_log_is_reset_before_the_next_record(self):
        first = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        second = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        with mock.patch.object(shim, "_MAX_MCP_REQUEST_LOG_BYTES", 1):
            shim._append_mcp_request_diagnostic(first, json.dumps(first).encode("utf-8"))
            shim._append_mcp_request_diagnostic(second, json.dumps(second).encode("utf-8"))

        lines = shim.mcp_request_log_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["rpc_id"], 2)

    def test_diagnostic_scan_is_capped_and_marks_truncation(self):
        with mock.patch.object(shim, "_MAX_MCP_DIAGNOSTIC_SCAN_CHARS", 3):
            summary = shim._diagnostic_text_summary("中文中?")
        self.assertEqual(summary["characters"], 4)
        self.assertEqual(summary["scanned_characters"], 3)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["cjk_characters"], 3)
        self.assertEqual(summary["ascii_question_marks"], 0)

    def test_linked_log_is_refused_without_blocking_the_mcp_request(self):
        from client import runner

        path = shim.mcp_request_log_path()
        runner._ensure_private_dir(path.parent)
        outside = Path(self.tmp.name) / "outside.log"
        outside.write_text("untouched", encoding="utf-8")
        try:
            path.symlink_to(outside)
        except OSError as exc:
            self.skipTest("symlink creation unavailable: %s" % exc)
        opener = self._successful_opener()
        message = {"jsonrpc": "2.0", "id": 8, "method": "ping"}
        with mock.patch.object(shim, "hub_reachable", return_value=True), \
             mock.patch.object(shim, "_log") as diagnostic, \
             mock.patch.object(shim, "build_opener", return_value=opener):
            result = shim.Forwarder("https://hub.example.com", "token").forward(message)

        self.assertEqual(result, {"result": {"ok": True}})
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")
        diagnostic.assert_called_once()

    def test_logging_failure_never_blocks_the_mcp_request(self):
        opener = self._successful_opener()
        message = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "ge_render", "arguments": {"spec": "中文"}},
        }
        with mock.patch.object(shim, "hub_reachable", return_value=True), \
             mock.patch.object(shim, "_append_mcp_request_diagnostic", side_effect=OSError("full")), \
             mock.patch.object(shim, "_log"), \
             mock.patch.object(shim, "build_opener", return_value=opener):
            result = shim.Forwarder("https://hub.example.com", "token").forward(message)

        self.assertEqual(result, {"result": {"ok": True}})
        opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class OfflineFastFailTests(unittest.TestCase):
    """A dead network used to park forward() for DEFAULT_TIMEOUT_S=120s, on the transport thread —
    so the user's whole MCP surface froze for two minutes per call, then failed with a bare timeout
    that read like the audit was lost. Owner: fail immediately and say the result is recoverable."""

    def _forwarder(self, endpoint="https://hub.invalid:8443"):
        return shim.Forwarder(endpoint, "tok")

    def test_probe_reports_a_dead_endpoint(self):
        # A port we bound and released: the connect is REFUSED, deterministically and instantly.
        # (Not a doc-range address like 192.0.2.1 — plenty of networks, VPNs and captive portals
        # answer those on the user's behalf, which would make this assert pass or fail by venue.)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        self.assertFalse(shim.hub_reachable("http://127.0.0.1:%d" % dead_port, timeout_s=0.5))

    def test_probe_reports_a_live_endpoint(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            self.assertTrue(shim.hub_reachable("http://127.0.0.1:%d" % srv.getsockname()[1]))
        finally:
            srv.close()

    def test_probe_fails_open_on_an_unparseable_endpoint(self):
        # Never invent an outage we didn't observe — let the real request decide.
        self.assertTrue(shim.hub_reachable("not-a-url"))
        self.assertTrue(shim.hub_reachable(""))

    def test_forward_fails_fast_instead_of_waiting_out_the_request_budget(self):
        fwd = self._forwarder()
        started = time.monotonic()
        with mock.patch.object(shim, "hub_reachable", return_value=False), \
             mock.patch.object(shim, "build_opener",
                               side_effect=AssertionError("must not be called")):
            with self.assertRaises(shim.ShellError) as caught:
                fwd.forward({"method": "tools/call", "params": {"name": "de_wait_audit"}})
        # The point of the fix: no request is sent and the caller is released immediately.
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("unreachable", str(caught.exception))

    def test_offline_message_says_a_submitted_audit_survives_and_how_to_get_it(self):
        msg = shim.offline_message({"method": "tools/call", "params": {"name": "de_wait_audit"}})
        self.assertIn("keeps running", msg)
        self.assertIn("NOT lost", msg)
        self.assertIn("de_wait_audit(audit_id)", msg)

    def test_offline_message_for_a_submit_says_it_never_started(self):
        # The opposite claim: promising a recoverable audit that was never accepted would be a lie.
        msg = shim.offline_message({"method": "tools/call", "params": {"name": "de_audit"}})
        self.assertIn("NOT submitted", msg)
        self.assertNotIn("keeps running", msg)

    def test_reachable_hub_still_forwards_on_the_full_budget(self):
        # A slow-but-alive hub must NOT be fast-failed: de_wait_audit legitimately blocks for
        # minutes while the panel of voices runs.
        fwd = self._forwarder()
        fake = mock.MagicMock()
        # The shared read helper drains via read1() in chunks until EOF (b""), so stub that,
        # not read(): the body in one chunk, then end-of-stream. It also inspects `chunked`
        # (must be non-chunked to be read at all) and `length` (0 = the declared body was fully
        # received, so no truncation) — a bare MagicMock would read truthy for both and trip
        # the guards, so pin them to a well-behaved complete response.
        resp = fake.__enter__.return_value
        resp.read1.side_effect = [b'{"result": {"ok": true}}', b""]
        resp.chunked = False
        resp.length = 0
        opener = mock.MagicMock()
        opener.open.return_value = fake
        sentinel = ssl.create_default_context()
        with mock.patch.object(shim, "hub_reachable", return_value=True) as probe, \
             mock.patch.object(shim, "_https_context", return_value=sentinel), \
             mock.patch.object(shim, "build_opener", return_value=opener) as built:
            out = fwd.forward({"method": "tools/call", "params": {"name": "de_wait_audit"}})
        self.assertEqual(out, {"result": {"ok": True}})
        probe.assert_called_once()
        self.assertEqual(opener.open.call_args.kwargs["timeout"], shim.DEFAULT_TIMEOUT_S)
        # Mocking build_opener mocks out the security-relevant construction, so assert what
        # was handed to it: without this, a regression dropping NoRedirect (or the TLS
        # context) from the opener would leave this test green. The real-socket
        # ForwardRedirectTests cover the guard end-to-end; only http:// is reachable there,
        # so this is the one place the HTTPS context wiring is pinned at all.
        self.assertIn(NoRedirect, built.call_args.args)
        handler = next(a for a in built.call_args.args if isinstance(a, HTTPSHandler))
        self.assertIs(handler._context, sentinel)


class _MockArtifactHandler(BaseHTTPRequestHandler):
    """Records every request it is given, then answers per the server's config: a
    ``redirect_to`` emits a 302 + Location, otherwise the ``body`` dict as JSON."""

    def _respond(self):
        self.server.requests.append((self.path, dict(self.headers)))  # type: ignore[attr-defined]
        pre_header_delay = getattr(self.server, "pre_header_delay", None)
        if pre_header_delay is not None:
            time.sleep(pre_header_delay)
        redirect_to = getattr(self.server, "redirect_to", None)
        if redirect_to:
            # `redirect_status` picks the 3xx code; /mcp is a POST, where 301/302/303 (which
            # urllib rewrites to a GET) and 307/308 (which it refuses outright) differ.
            self.send_response(getattr(self.server, "redirect_status", None) or 302)
            self.send_header("Location", redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        drip = getattr(self.server, "drip", None)
        if drip is not None:
            # A slowloris body: declare a length far larger than we will ever send, then
            # dribble one byte per `delay` forever so the client never reaches EOF and never
            # trips the per-op socket timeout (a byte keeps arriving). Only a transfer-deadline
            # can bound this — the read-time cap the shared helper adds. Stops when the client
            # disconnects (its deadline fired) → wfile.write raises, which we swallow.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(drip.get("content_length", 64 * 1024 * 1024)))
            self.end_headers()
            try:
                deadline = time.monotonic() + drip.get("max_s", 30.0)   # hard stop so a bug can't wedge the suite
                while time.monotonic() < deadline:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(drip["delay"])
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if getattr(self.server, "chunked", False):
            # A raw HTTP/1.1 chunked response (bypasses send_header so no Content-Length). The
            # body is well-formed and small — the point is that the read helper REFUSES chunked
            # outright (its framing is read by a multi-recv read1 the deadline can't interrupt),
            # not that this particular body is hostile.
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Transfer-Encoding: chunked\r\n\r\n"
                             b"b\r\n" + b'{"ok":true}' + b"\r\n0\r\n\r\n")
            return
        raw = getattr(self.server, "raw", None)
        if raw is not None:
            # A hostile body: `raw` goes on the wire verbatim, and `declared_length` (when it
            # overstates len(raw)) hangs up early to force a truncated read.
            declared = getattr(self.server, "declared_length", None) or len(raw)
            # `status` (default 200) lets `raw` ride a 4xx/5xx so the client's except-HTTPError
            # error-body read is exercised; urllib raises HTTPError for a non-2xx and the arm reads
            # exc.read() — which must be capped the same way the success read is.
            self.send_response(getattr(self.server, "status", None) or 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(declared))
            self.end_headers()
            self.wfile.write(raw)
            if declared > len(raw):
                self.close_connection = True
            return
        payload = json.dumps(self.server.body).encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _respond    # noqa: N815 (stdlib naming) — /v1/ge/artifact

    def do_POST(self):  # noqa: N802 — /mcp
        # Drain the JSON-RPC request before replying. On Windows, closing a test connection
        # with unread inbound bytes can produce WSAECONNABORTED on the client and make these
        # response-body tests fail before they reach the behavior they intend to exercise.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._respond()

    def log_message(self, *_args):  # silence
        pass


class _MockHub:
    """A real in-process hub on 127.0.0.1 (real socket, real urllib — same shape as
    test_activate._MockServer). ``redirect_to`` turns it into a hostile redirector."""

    def __init__(self, body=None, redirect_to=None, raw=None, declared_length=None,
                 redirect_status=None, status=None, drip=None, chunked=False,
                 pre_header_delay=None):
        self.httpd = HTTPServer(("127.0.0.1", 0), _MockArtifactHandler)
        self.httpd.body = body or {"kind": "svg", "data": "<svg/>"}  # type: ignore[attr-defined]
        self.httpd.redirect_to = redirect_to  # type: ignore[attr-defined]
        self.httpd.redirect_status = redirect_status  # type: ignore[attr-defined]
        self.httpd.raw = raw  # type: ignore[attr-defined]
        self.httpd.declared_length = declared_length  # type: ignore[attr-defined]
        self.httpd.status = status  # type: ignore[attr-defined]  # non-200 → drives the raw body onto a 4xx/5xx
        self.httpd.drip = drip  # type: ignore[attr-defined]  # {"delay": s, "content_length"?, "max_s"?}
        self.httpd.chunked = chunked  # type: ignore[attr-defined]
        self.httpd.pre_header_delay = pre_header_delay  # type: ignore[attr-defined]
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_port

    @property
    def requests(self):
        return self.httpd.requests  # type: ignore[attr-defined]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class GeArtifactRedirectTests(unittest.TestCase):
    """`get_ge_artifact` sends the device token as an `Authorization: Bearer` header.
    urllib's DEFAULT opener follows 3xx and its HTTPRedirectHandler rebuilds the request
    with the original headers — so a hostile/compromised redirect handed the device token
    to whatever host it named. The GE fetch must refuse redirects outright, the same way
    the /db/render fetch already does (client/popup/launcher.NoRedirect)."""

    def test_refused_redirect_never_hands_the_token_to_the_target(self):
        attacker = _MockHub()
        self.addCleanup(attacker.close)
        hub = _MockHub(redirect_to=attacker.base + "/v1/ge/artifact/r1")
        self.addCleanup(hub.close)
        fwd = shim.Forwarder(hub.base, "tok_secret")   # 127.0.0.1 → the plaintext guard allows it

        with self.assertRaises(config.ShellError):
            fwd.get_ge_artifact("r1", timeout_s=5)

        # The point of the fix: the redirect was NOT followed, so the token never reached
        # the target. (Before the fix the attacker recorded a request carrying the Bearer.)
        self.assertEqual(attacker.requests, [], "the device token was sent to the redirect target")
        self.assertEqual(len(hub.requests), 1)

    def test_refused_redirect_reports_no_server_detail_to_the_model(self):
        # The failure contract: ShellError, and the message carries neither the token nor
        # any server-supplied detail (a handler result reaches the model — a raw detail
        # must not). The target is a real local hub, so this asserts the REFUSAL rather
        # than an incidental DNS failure, and needs no network.
        attacker = _MockHub()
        self.addCleanup(attacker.close)
        hub = _MockHub(redirect_to=attacker.base + "/v1/ge/artifact/r1")
        self.addCleanup(hub.close)
        fwd = shim.Forwarder(hub.base, "tok_secret")
        with self.assertRaises(config.ShellError) as caught:
            fwd.get_ge_artifact("r1", timeout_s=5)
        message = str(caught.exception)
        self.assertIn("/v1/visual/artifacts", message)
        self.assertNotIn("tok_secret", message)
        self.assertNotIn(attacker.base, message)

    def test_non_redirected_fetch_still_returns_the_artifact(self):
        # The guard must not break the working path: a plain 200 still parses.
        hub = _MockHub(body={"kind": "svg", "data": "<svg/>"})
        self.addCleanup(hub.close)
        fwd = shim.Forwarder(hub.base, "tok_secret")
        self.assertEqual(fwd.get_ge_artifact("r1", timeout_s=5),
                         {"kind": "svg", "data": "<svg/>"})
        path, headers = hub.requests[0]
        self.assertEqual(path, "/v1/visual/artifacts/r1")
        self.assertEqual(headers["Authorization"], "Bearer tok_secret")

    def test_the_guard_is_the_one_shared_implementation(self):
        # Not a copy that can drift: all three authenticated fetches (this one, /db/render,
        # and the activation POST) must resolve to the same class.
        from client import http_safety
        from client.popup import launcher

        self.assertIs(shim.NoRedirect, http_safety.NoRedirect)
        self.assertIs(launcher.NoRedirect, http_safety.NoRedirect)

    def test_bad_response_shape_still_fails_closed(self):
        hub = _MockHub(body={"kind": "svg"})   # no data
        self.addCleanup(hub.close)
        fwd = shim.Forwarder(hub.base, "tok_secret")
        with self.assertRaises(config.ShellError):
            fwd.get_ge_artifact("r1", timeout_s=5)


class ForwardRedirectTests(unittest.TestCase):
    """/mcp is the shim's MAIN transport — every JSON-RPC message rides it under the same
    `Authorization: Bearer <device token>`, so it needs the guard at least as much as the
    artifact fetch did. Measured before the fix: on a 301/302/303 the redirect target
    received the Bearer AND its forged JSON-RPC result came back as the hub's answer.
    (307/308 already failed — CPython won't redirect a POST carrying a body.)"""

    def _hub(self, **kwargs):
        hub = _MockHub(**kwargs)
        self.addCleanup(hub.close)
        return hub

    def test_no_redirect_code_is_followed_and_none_reaches_the_target(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                attacker = self._hub()
                hub = self._hub(redirect_status=code, redirect_to=attacker.base + "/mcp")
                fwd = shim.Forwarder(hub.base, "tok_secret")
                with self.assertRaises(config.ShellError):
                    fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(attacker.requests, [],
                                 "the device token was sent to the redirect target")

    def test_a_refused_redirect_never_returns_the_targets_answer(self):
        # The second half of the leak: the forged result must not reach the caller as if the
        # hub had sent it. A ShellError is the only acceptable outcome.
        attacker = self._hub(body={"jsonrpc": "2.0", "id": 1, "result": "attacker-chosen"})
        hub = self._hub(redirect_to=attacker.base + "/mcp")
        fwd = shim.Forwarder(hub.base, "tok_secret")
        with self.assertRaises(config.ShellError) as caught:
            fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        message = str(caught.exception)
        self.assertNotIn("attacker-chosen", message)
        self.assertNotIn("tok_secret", message)
        self.assertNotIn(attacker.base, message)

    def test_a_plain_200_still_forwards_and_parses(self):
        # The guard must not break the working path.
        hub = self._hub(body={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
        fwd = shim.Forwarder(hub.base, "tok_secret")
        self.assertEqual(fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                         {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
        path, headers = hub.requests[0]
        self.assertEqual(path, "/mcp")
        self.assertEqual(headers["Authorization"], "Bearer tok_secret")

    def test_configured_fake_token_receiving_401_never_falls_back_to_lite(self):
        hub = self._hub(raw=b'{"detail":"unauthorized"}', status=401)
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config.atomic_write_json(
                config_path,
                {"server_endpoint": hub.base, "access_token": "fake-token"},
            )
            with (
                mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(config_path)}),
                mock.patch.object(shim, "LiteForwarder") as lite,
            ):
                forwarder = shim.Forwarder.from_config()
                with self.assertRaisesRegex(config.ShellError, "HTTP 401"):
                    forwarder.forward(
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                    )

        lite.assert_not_called()

    def test_token_present_401_with_entitlement_shape_never_falls_back_to_lite(self):
        hub = self._hub(
            raw=json.dumps({
                "status": "account_action_required",
                "reason": "subscription_expired",
                "retryable": False,
                "action": "renew_subscription",
                "local_advisory_available": True,
            }).encode("utf-8"),
            status=401,
        )
        request = {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "audit_skill_submit", "arguments": {
                    "skill_name": "audit", "args": {
                        "title": "Explicit audit", "content": "artifact"
                    }
                }},
            }

        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(shim.Forwarder(hub.base, "secret"), [json.dumps(request)],
                       client_host="codex")[0]

        self.assertEqual(out["error"]["code"], -32001)
        self.assertNotIn("result", out)
        self.assertNotIn("data", out["error"])
        spawn.assert_not_called()

    def test_entitlement_contract_routes_subscription_and_credits_to_local_advisory(self):
        for reason, action in (
            ("subscription_expired", "renew_subscription"),
            ("credits_exhausted", "add_credits"),
        ):
            with self.subTest(reason=reason):
                hub = self._hub(
                    raw=json.dumps({
                        "status": "account_action_required",
                        "reason": reason,
                        "retryable": False,
                        "action": action,
                        "local_advisory_available": True,
                    }).encode("utf-8"),
                    status=402,
                )
                request = {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "audit_skill_submit", "arguments": {
                        "skill_name": "audit", "args": {
                            "title": "Entitlement audit", "content": "artifact"
                        }
                    }},
                }
                with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
                    out = _run(shim.Forwarder(hub.base, "secret"), [json.dumps(request)],
                               client_host="codex")[0]
                payload = json.loads(out["result"]["content"][0]["text"])
                self.assertEqual(payload["payload"]["degrade_reason"], reason)
                self.assertEqual(payload["payload"]["degrade_action"], action)
                spawn.assert_called_once()

    def test_plain_success_body_with_insufficient_balance_marker_enters_local_advisory(self):
        hub = self._hub(raw=b"insufficient_balance", status=200)
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Plain balance marker audit", "content": "artifact",
                }
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(
                shim.Forwarder(hub.base, "secret"),
                [json.dumps(request)],
                client_host="codex",
            )[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        spawn.assert_called_once()

    def test_http_402_insufficient_balance_marker_enters_local_advisory(self):
        hub = self._hub(raw=b"credit: insufficient_balance", status=402)
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "HTTP balance marker audit", "content": "artifact",
                }
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(
                shim.Forwarder(hub.base, "secret"),
                [json.dumps(request)],
                client_host="codex",
            )[0]

        payload = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(payload["payload"]["degrade_reason"], "credits_exhausted")
        spawn.assert_called_once()

    def test_unknown_entitlement_reason_fails_closed_without_local_advisory(self):
        hub = self._hub(
            raw=b'{"status":"account_action_required","reason":"mystery"}',
            status=402,
        )
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_skill_submit", "arguments": {
                "skill_name": "audit", "args": {
                    "title": "Unknown entitlement audit", "content": "artifact"
                }
            }},
        }
        with mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(shim.Forwarder(hub.base, "secret"), [json.dumps(request)],
                       client_host="codex")[0]
        self.assertEqual(out["error"]["code"], -32001)
        self.assertNotIn("local_advisory", json.dumps(out))
        spawn.assert_not_called()

    def test_a_hostile_body_fails_closed_without_echoing_the_bytes(self):
        # Same contract hole get_ge_artifact closed: UnicodeDecodeError / IncompleteRead
        # subclass neither URLError nor OSError, and their str() carries server bytes.
        for label, kwargs in (("non-utf8", {"raw": b'{"result":"\xff\xfe"}'}),
                              ("truncated", {"raw": b"{", "declared_length": 5000})):
            with self.subTest(body=label):
                fwd = shim.Forwarder(self._hub(**kwargs).base, "tok_secret")
                with self.assertRaises(config.ShellError) as caught:
                    fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertNotIn("0xff", str(caught.exception))


class ForwardOutcomeUnknownTests(unittest.TestCase):
    @staticmethod
    def _message():
        return {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "de_audit", "arguments": {
                    "title": "Explicit audit"}}}

    @staticmethod
    def _response(*, read1_side_effect):
        response = mock.MagicMock()
        response.read1.side_effect = read1_side_effect
        response.chunked = False
        response.length = 0
        context = mock.MagicMock()
        context.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = context
        return opener

    def _assert_unknown(self, opener, *, reason):
        message = self._message()
        with mock.patch.object(shim, "hub_reachable", return_value=True), \
             mock.patch.object(shim, "build_opener", return_value=opener):
            out = _run(shim.Forwarder("https://hub.example.com", "secret"),
                       [json.dumps(message)])[0]

        self.assertEqual(out["error"]["code"], -32001)
        data = out["error"]["data"]
        self.assertEqual(data["status"], "request_outcome_unknown")
        self.assertEqual(data["reason"], reason)
        self.assertTrue(data["request_sent"])
        self.assertFalse(data["retryable"])
        self.assertEqual(data["action"], "reconcile")
        self.assertRegex(data["request_id"], r"^[0-9a-f]{32}$")
        rendered = json.dumps(out)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("hub.example.com", rendered)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-request-id"), data["request_id"])

    def _assert_not_claimed_sent(self, reason):
        opener = mock.Mock()
        opener.open.side_effect = URLError(reason)
        with mock.patch.object(shim, "hub_reachable", return_value=True), \
             mock.patch.object(shim, "build_opener", return_value=opener):
            out = _run(shim.Forwarder("https://hub.example.com", "secret"),
                       [json.dumps(self._message())])[0]

        self.assertEqual(out["error"]["code"], -32001)
        self.assertNotIn("data", out["error"])
        self.assertNotIn("request outcome unknown", out["error"]["message"])
        opener.open.assert_called_once()

    def _assert_pre_header_unknown(self, exception, *, reason):
        opener = mock.Mock()

        def build(*handlers):
            tracker = next(
                handler._send_state for handler in handlers
                if hasattr(handler, "_send_state")
            )

            def fail_after_send(*_args, **_kwargs):
                tracker.sent = True
                raise exception

            opener.open.side_effect = fail_after_send
            return opener

        with mock.patch.object(shim, "hub_reachable", return_value=True), \
             mock.patch.object(shim, "build_opener", side_effect=build):
            out = _run(shim.Forwarder("https://hub.example.com", "secret"),
                       [json.dumps(self._message())])[0]

        data = out["error"]["data"]
        self.assertEqual(data["status"], "request_outcome_unknown")
        self.assertEqual(data["reason"], reason)
        self.assertTrue(data["request_sent"])
        self.assertFalse(data["retryable"])
        opener.open.assert_called_once()

    def test_connect_timeout_is_not_claimed_as_request_sent(self):
        self._assert_not_claimed_sent(TimeoutError())

    def test_connect_reset_is_not_claimed_as_request_sent(self):
        self._assert_not_claimed_sent(ConnectionResetError())

    def test_request_outcome_unknown_never_arms_a_local_advisory(self):
        opener = self._response(read1_side_effect=ConnectionResetError())
        message = self._message()
        with mock.patch.object(shim, "hub_reachable", return_value=True), \
             mock.patch.object(shim, "build_opener", return_value=opener), \
             mock.patch.object(shim, "_spawn_stopper_for_audit") as spawn:
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [json.dumps(message)],
            )[0]
        self.assertEqual(out["error"]["data"]["status"], "request_outcome_unknown")
        spawn.assert_not_called()

    def test_pre_header_timeout_after_send_is_unknown(self):
        self._assert_pre_header_unknown(TimeoutError(), reason="read_timeout")

    def test_pre_header_reset_after_send_is_unknown(self):
        self._assert_pre_header_unknown(ConnectionResetError(), reason="response_reset")

    def test_real_pre_header_timeout_is_unknown_and_not_replayed(self):
        hub = _MockHub(pre_header_delay=0.3)
        self.addCleanup(hub.close)
        forwarder = shim.Forwarder(hub.base, "secret", timeout_s=0.05)

        with self.assertRaises(shim.OutcomeUnknownError) as caught:
            forwarder.forward(self._message())

        self.assertEqual(caught.exception.data["reason"], "read_timeout")
        self.assertTrue(caught.exception.data["request_sent"])
        self.assertEqual(len(hub.requests), 1)
        self.assertEqual(
            hub.requests[0][1]["X-Request-Id"],
            caught.exception.data["request_id"],
        )

    def test_response_reset_after_send_is_unknown_and_not_replayed(self):
        self._assert_unknown(
            self._response(read1_side_effect=ConnectionResetError()),
            reason="response_reset",
        )

    def test_read_timeout_after_send_is_unknown_and_not_replayed(self):
        self._assert_unknown(
            self._response(read1_side_effect=TimeoutError()),
            reason="read_timeout",
        )

    def test_total_read_budget_after_response_is_unknown(self):
        opener = self._response(read1_side_effect=[b"{"])

        def expire_budget(_response, *, error, **_kwargs):
            raise error("transfer exceeded its 1s budget")

        with mock.patch.object(shim, "read_within_budget", side_effect=expire_budget):
            self._assert_unknown(opener, reason="read_timeout")

    def test_truncated_response_after_headers_is_unknown(self):
        opener = self._response(read1_side_effect=[b""])
        response = opener.open.return_value.__enter__.return_value
        response.length = 10
        self._assert_unknown(opener, reason="response_reset")

    def test_http_5xx_is_unknown_without_echoing_hub_body(self):
        body = io.BytesIO(b"hub secret and server details")
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(
            "https://hub.example.com/mcp", 503, "service unavailable", {}, body
        )
        self._assert_unknown(opener, reason="http_5xx")
        self.assertTrue(body.closed)


class SessionOutcomeUnknownLatchTests(unittest.TestCase):
    """DE-026 / R-076 behaviour locks.

    ``OutcomeUnknownError`` protects the message that raised it, but the no-replay contract
    belongs to the SESSION: once a request has crossed the wire with an unknown outcome the hub
    may already own a run. A later connect-probe failure then looks exactly like a *pre*-dispatch
    outage, and the pre-dispatch branch is allowed to arm a DE Lite advisory — producing a second
    audit for one user intent while the hosted run is still live. These lock the observable side
    effects (local run creation, dispatch counts, run identity), not the return shape."""

    @staticmethod
    def _audit(rid):
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {"title": "Random topic", "content": "please audit this"},
                },
            },
        }

    @staticmethod
    def _local_runs(out):
        """Every ``local_*`` run id the session handed back to the host."""
        found = []
        for entry in out:
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            for block in result.get("content", []):
                text = block.get("text") if isinstance(block, dict) else None
                if not isinstance(text, str):
                    continue
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                run_id = payload.get("run_id")
                if isinstance(run_id, str) and run_id.startswith("local_"):
                    found.append(run_id)
        return found

    def _dispatch_then_die(self, dispatches, exc):
        """build_opener stand-in: mark the request as sent, then fail post-dispatch."""
        def build(*handlers):
            tracker = next(
                handler._send_state for handler in handlers
                if hasattr(handler, "_send_state")
            )
            opener = mock.Mock()

            def fail_after_send(*_args, **_kwargs):
                tracker.sent = True
                dispatches.append(1)
                raise exc()

            opener.open.side_effect = fail_after_send
            return opener

        return build

    def _run_two_audits(self, exc):
        """One audit that crosses the wire and dies, then a second while the probe says down."""
        dispatches = []
        # serve() probes once at startup, then _post probes per forwarded request.
        probe = mock.Mock(side_effect=[True, True, False])
        with mock.patch.object(shim, "hub_reachable", probe), \
             mock.patch.object(shim, "build_opener",
                               side_effect=self._dispatch_then_die(dispatches, exc)):
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [json.dumps(self._audit(1)), json.dumps(self._audit(2))],
            )
        return out, dispatches

    def test_post_dispatch_reset_blocks_lite_on_a_later_probe_failure(self):
        """R26-02: exactly one hosted dispatch, zero local begins."""
        out, dispatches = self._run_two_audits(ConnectionResetError)

        self.assertEqual(out[0]["error"]["data"]["status"], "request_outcome_unknown")
        self.assertEqual(
            self._local_runs(out), [],
            "a session holding an unreconciled hosted dispatch must not start DE Lite",
        )
        self.assertEqual(len(dispatches), 1, "the audit must never be re-dispatched")

    def test_post_dispatch_timeout_blocks_lite_on_a_later_probe_failure(self):
        """R26-01: the timeout variant of the same session state."""
        out, _ = self._run_two_audits(TimeoutError)

        self.assertEqual(out[0]["error"]["data"]["status"], "request_outcome_unknown")
        self.assertEqual(self._local_runs(out), [])

    def test_second_audit_after_unknown_outcome_reports_reconcile_not_unavailable(self):
        """R26-03: the host must be told to reconcile the live run, not that MCP is down."""
        out, _ = self._run_two_audits(ConnectionResetError)

        second = out[1]
        self.assertIn("error", second, "the follow-up must not be answered with a local run")
        data = second["error"]["data"]
        self.assertEqual(data["status"], "request_outcome_unknown")
        self.assertTrue(data["request_sent"])
        self.assertFalse(data["retryable"])
        self.assertEqual(data["action"], "reconcile")
        rendered = json.dumps(out)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("hub.example.com", rendered)

    def test_pre_dispatch_outage_without_any_dispatch_still_offers_lite(self):
        """R26-04: the latch must not over-block — a session that never reached the hub keeps
        today's offline DE Lite path, so this is the anti-regression side of the fix."""
        probe = mock.Mock(side_effect=[False, False])
        with mock.patch.object(shim, "hub_reachable", probe):
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [json.dumps(self._audit(1))],
            )

        self.assertEqual(len(self._local_runs(out)), 1)

    def test_hosted_traffic_still_works_after_an_unknown_outcome(self):
        """The latch closes the LITE path only; a recovered hub keeps serving hosted calls."""
        dispatches = []
        probe = mock.Mock(return_value=True)
        opener_holder = {}

        def build(*handlers):
            tracker = next(
                handler._send_state for handler in handlers
                if hasattr(handler, "_send_state")
            )
            opener = mock.Mock()

            def behave(*_args, **_kwargs):
                tracker.sent = True
                dispatches.append(1)
                if len(dispatches) == 1:
                    raise ConnectionResetError()
                response = mock.MagicMock()
                response.read1.side_effect = [
                    b'{"jsonrpc":"2.0","id":2,"result":{"ok":true}}', b""
                ]
                response.chunked = False
                response.length = 0
                context = mock.MagicMock()
                context.__enter__.return_value = response
                return context

            opener.open.side_effect = behave
            opener_holder["opener"] = opener
            return opener

        with mock.patch.object(shim, "hub_reachable", probe), \
             mock.patch.object(shim, "build_opener", side_effect=build):
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [json.dumps(self._audit(1)), json.dumps(self._audit(2))],
            )

        self.assertEqual(out[0]["error"]["data"]["status"], "request_outcome_unknown")
        self.assertEqual(out[1].get("result"), {"ok": True})
        self.assertEqual(self._local_runs(out), [])

    @staticmethod
    def _submit_accepted_opener(run_id):
        """A hosted submit that SUCCEEDS: the hub returns a run_id with a non-terminal status,
        so the session now knows a real hosted run exists."""
        envelope = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(
                {"run_id": run_id, "status": "queued", "title": "Random topic"}
            )}]},
        }).encode("utf-8")
        response = mock.MagicMock()
        response.read1.side_effect = [envelope, b""]
        response.chunked = False
        response.length = 0
        context = mock.MagicMock()
        context.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = context
        return opener

    def test_live_hosted_run_blocks_lite_when_the_probe_later_fails(self):
        """R26-03: a hosted run_id was returned, so a later pre-dispatch outage must preserve that
        identity for reconciliation rather than opening a local second opinion for the same work."""
        # serve() start, then the submit; the second audit never reaches the wire.
        probe = mock.Mock(side_effect=[True, True, False])
        opener = self._submit_accepted_opener("aud_hosted_r2603")
        with mock.patch.object(shim, "hub_reachable", probe), \
             mock.patch.object(shim, "build_opener", return_value=opener):
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [json.dumps(self._audit(1)), json.dumps(self._audit(2))],
            )

        self.assertIn("aud_hosted_r2603", json.dumps(out[0]),
                      "the hosted submit itself must still succeed")
        self.assertEqual(
            self._local_runs(out), [],
            "a live hosted run must not be shadowed by a DE Lite run",
        )
        self.assertIn("error", out[1])
        data = out[1]["error"]["data"]
        self.assertEqual(data["run_id"], "aud_hosted_r2603",
                         "the hosted identity must survive for recovery/reconciliation")
        self.assertEqual(data["action"], "reconcile")
        self.assertTrue(data["request_sent"])
        self.assertFalse(data["retryable"])
        self.assertEqual(opener.open.call_count, 1,
                         "the second audit must never be dispatched")

    def test_completed_hosted_run_stops_blocking_lite(self):
        """The guard must retire with the run: once the hosted audit reaches a terminal status the
        session is free again, so a genuine later outage still offers DE Lite."""
        terminal = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(
                {"run_id": "aud_done_r2603", "status": "completed", "title": "Random topic"}
            )}]},
        }).encode("utf-8")
        response = mock.MagicMock()
        response.read1.side_effect = [terminal, b""]
        response.chunked = False
        response.length = 0
        context = mock.MagicMock()
        context.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = context
        probe = mock.Mock(side_effect=[True, True, False])
        with mock.patch.object(shim, "hub_reachable", probe), \
             mock.patch.object(shim, "build_opener", return_value=opener):
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [json.dumps(self._audit(1)), json.dumps(self._audit(2))],
            )

        self.assertEqual(len(self._local_runs(out)), 1)

    @staticmethod
    def _body_context(rid, payload):
        """One hub response whose MCP result text carries ``payload``."""
        envelope = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }).encode("utf-8")
        response = mock.MagicMock()
        response.read1.side_effect = [envelope, b""]
        response.chunked = False
        response.length = 0
        context = mock.MagicMock()
        context.__enter__.return_value = response
        return context

    @classmethod
    def _queued_submit_context(cls, run_id):
        """One ACCEPTED hosted submit: a run_id the hub owns at a non-terminal status."""
        return cls._body_context(
            1, {"run_id": run_id, "status": "queued", "title": "Random topic"}
        )

    def _accepted_submit_then(self, calls, run_id, follow_up_error):
        """build_opener stand-in: the submit is ACCEPTED with a live run_id, then the follow-up
        on that same run crosses the wire and fails the way ``follow_up_error`` builds."""
        def build(*handlers):
            tracker = next(
                handler._send_state for handler in handlers
                if hasattr(handler, "_send_state")
            )
            opener = mock.Mock()

            def behave(*_args, **_kwargs):
                tracker.sent = True
                calls.append(1)
                if len(calls) == 1:
                    return self._queued_submit_context(run_id)
                raise follow_up_error()

            opener.open.side_effect = behave
            return opener

        return build

    @staticmethod
    def _wait(rid, run_id):
        return {
            "jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": "wait_audit", "arguments": {"run_id": run_id}},
        }

    def _wire_sequence(self, calls, *steps):
        """build_opener stand-in driving ONE scripted step per wire call: either an exception
        class to raise, or a zero-arg callable returning a response context. Running past the
        end raises IndexError, so an unexpected extra dispatch fails loudly instead of being
        silently absorbed by a permissive mock."""
        def build(*handlers):
            tracker = next(
                handler._send_state for handler in handlers
                if hasattr(handler, "_send_state")
            )
            opener = mock.Mock()

            def behave(*_args, **_kwargs):
                tracker.sent = True
                calls.append(1)
                step = steps[len(calls) - 1]
                if isinstance(step, type) and issubclass(step, BaseException):
                    raise step()
                return step()

            opener.open.side_effect = behave
            return opener

        return build

    @staticmethod
    def _healthy_then_offline(healthy_probes):
        """hub_reachable stand-in: healthy for the first N probes, then a PERSISTENT outage, so
        the second, third and later messages all meet the same down hub rather than a one-shot
        blip that would let a bypass hide behind a lucky recovery."""
        seen = {"n": 0}

        def probe(*_args, **_kwargs):
            seen["n"] += 1
            return seen["n"] <= healthy_probes

        return mock.Mock(side_effect=probe)

    @staticmethod
    def _config_bound_forwarder():
        """The PRODUCTION transport shape (F26-03).

        `Forwarder.from_config` sets `_config_bound`, and ONLY a config-bound transport lets
        `_refresh_degraded_forwarder` swap in an `OfflineForwarder` BEFORE `forward()` is ever
        called — whose `forward()` returns a local DE Lite result instead of raising
        `OfflineError`. A directly constructed `Forwarder` short-circuits that refresh, so the
        earlier tests in this class could not observe the production path at all."""
        forwarder = shim.Forwarder("https://hub.example.com", "secret")
        forwarder._config_bound = True
        return forwarder

    def _serve_config_bound(self, messages, *, healthy_probes, wire_steps):
        """Drive serve() over the production-shaped transport: config-bound, refreshed from
        config on lifecycle/audit messages, against a hub that goes down and stays down."""
        calls = []
        forwarder = self._config_bound_forwarder()
        with mock.patch.object(shim, "hub_reachable",
                               self._healthy_then_offline(healthy_probes)), \
             mock.patch.object(shim.Forwarder, "from_config", return_value=forwarder), \
             mock.patch.object(shim, "build_opener",
                               side_effect=self._wire_sequence(calls, *wire_steps)):
            out = _run(forwarder, [json.dumps(m) for m in messages])
        return out, calls

    def test_unknown_follow_up_outcome_keeps_the_known_hosted_run_id(self):
        """F26-01: the hub ACCEPTED a run, then a follow-up on it died post-dispatch. A later
        pre-dispatch outage must hand back BOTH identities — the hosted run_id is the only thing
        the caller can reconcile against, and dropping it for the dead follow-up's request_id
        leaves the accepted run unaccounted for."""
        calls = []
        # serve() start, the submit, the follow-up, then the third audit's probe says down.
        probe = mock.Mock(side_effect=[True, True, True, False])
        with mock.patch.object(shim, "hub_reachable", probe), \
             mock.patch.object(
                 shim, "build_opener",
                 side_effect=self._accepted_submit_then(
                     calls, "aud_known_r2603", ConnectionResetError)):
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [
                    json.dumps(self._audit(1)),
                    json.dumps(self._wait(2, "aud_known_r2603")),
                    json.dumps(self._audit(3)),
                ],
            )

        self.assertEqual(len(calls), 2, "the third audit must never reach the wire")
        self.assertEqual(
            self._local_runs(out), [],
            "a hosted run the hub still owns must not be shadowed by a DE Lite run",
        )
        data = out[2]["error"]["data"]
        self.assertEqual(
            data.get("run_id"), "aud_known_r2603",
            "the accepted hosted identity must survive the follow-up's unknown outcome",
        )
        self.assertEqual(
            data.get("request_id"), out[1]["error"]["data"]["request_id"],
            "the sent follow-up's request identity must survive alongside the run_id",
        )
        self.assertEqual(data["status"], "request_outcome_unknown")
        self.assertTrue(data["request_sent"])
        self.assertFalse(data["retryable"])
        self.assertEqual(data["action"], "reconcile")
        rendered = json.dumps(out)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("hub.example.com", rendered)

    def test_credits_fallback_retires_the_pending_hosted_run(self):
        """F26-02: an insufficient_balance follow-up is a DETERMINISTIC transition — the hosted
        run is over and exactly one DE Lite advisory replaces it. Leaving that run in the session's
        pending map makes it a phantom that blocks a later genuine offline audit forever."""
        calls = []
        probe = mock.Mock(side_effect=[True, True, True, False])

        def credits_block():
            return HTTPError(
                "https://hub.example.com/mcp", 402, "payment required", {},
                io.BytesIO(b"credit: insufficient_balance"),
            )

        with mock.patch.object(shim, "hub_reachable", probe), \
             mock.patch("client.runner.forget_active_run") as forget, \
             mock.patch.object(
                 shim, "build_opener",
                 side_effect=self._accepted_submit_then(
                     calls, "aud_credits_r2603", credits_block)):
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [
                    json.dumps(self._audit(1)),
                    json.dumps(self._wait(2, "aud_credits_r2603")),
                    json.dumps(self._audit(3)),
                ],
            )

        self.assertEqual(len(calls), 2, "the credits downgrade must not re-dispatch the audit")
        forget.assert_called_once_with("aud_credits_r2603")
        downgraded = json.loads(out[1]["result"]["content"][0]["text"])
        self.assertEqual(downgraded["payload"]["degrade_reason"], "credits_exhausted")
        self.assertEqual(
            len(self._local_runs(out)), 2,
            "the retired hosted run must stop blocking a later genuine offline DE Lite audit",
        )

    # --- F26-03: the same contracts over the PRODUCTION config-bound transport -----------------

    def test_config_bound_live_hosted_run_blocks_lite_when_the_probe_later_fails(self):
        """F26-03 / lock 1: the hub ACCEPTED `aud_cfg_r2603`, then went down. On the production
        transport the outage is discovered by the pre-forward config refresh, which swaps in an
        OfflineForwarder whose `forward()` RETURNS a local run rather than raising — so the
        decision has to be made before any forwarder can answer, not inside an error handler."""
        out, calls = self._serve_config_bound(
            [self._audit(1), self._audit(2)],
            healthy_probes=3,          # serve() start, the submit's refresh, the submit's send
            wire_steps=[lambda: self._queued_submit_context("aud_cfg_r2603")],
        )

        self.assertEqual(len(calls), 1, "the second audit must never reach the wire")
        self.assertEqual(
            self._local_runs(out), [],
            "a hosted run the hub still owns must not be shadowed by a DE Lite run",
        )
        data = out[1]["error"]["data"]
        self.assertEqual(data.get("run_id"), "aud_cfg_r2603")
        self.assertEqual(data["status"], "request_outcome_unknown")
        self.assertTrue(data["request_sent"])
        self.assertFalse(data["retryable"])
        self.assertEqual(data["action"], "reconcile")

    def test_config_bound_unknown_dispatch_blocks_lite_when_the_probe_later_fails(self):
        """F26-03 / lock 2: the post-dispatch latch has to survive the same production path —
        the first audit crossed the wire and died, so the next one under outage must reconcile
        against its request identity instead of opening a local second opinion."""
        out, calls = self._serve_config_bound(
            [self._audit(1), self._audit(2)],
            healthy_probes=3,
            wire_steps=[ConnectionResetError],
        )

        self.assertEqual(len(calls), 1, "the second audit must never reach the wire")
        self.assertEqual(self._local_runs(out), [])
        data = out[1]["error"]["data"]
        self.assertEqual(data["status"], "request_outcome_unknown")
        self.assertEqual(
            data.get("request_id"), out[0]["error"]["data"]["request_id"],
            "the dead dispatch's request identity is what the caller reconciles against",
        )
        self.assertTrue(data["request_sent"])
        self.assertFalse(data["retryable"])
        self.assertEqual(data["action"], "reconcile")

    def test_the_guard_holds_for_every_later_audit_not_just_the_next_one(self):
        """F26-03 / lock 3: once the session is offline the forwarder STAYS an OfflineForwarder,
        so a guard that only covers the message that discovered the outage leaks on the one
        after it. Both later audits must be refused, and neither may start a local run."""
        out, calls = self._serve_config_bound(
            [self._audit(1), self._audit(2), self._audit(3)],
            healthy_probes=3,
            wire_steps=[lambda: self._queued_submit_context("aud_repeat_r2603")],
        )

        self.assertEqual(len(calls), 1, "neither later audit may reach the wire")
        self.assertEqual(
            self._local_runs(out), [],
            "protection must not expire after one message",
        )
        for index in (1, 2):
            data = out[index]["error"]["data"]
            self.assertEqual(data.get("run_id"), "aud_repeat_r2603")
            self.assertEqual(data["action"], "reconcile")

    def test_a_lifecycle_refresh_during_outage_cannot_bypass_the_guard(self):
        """F26-03 / lock 4: `tools/list` forces its own config refresh, so the forwarder is
        already an OfflineForwarder by the time the next audit arrives — that audit never takes
        an error path at all. The refusal must survive an intervening lifecycle message."""
        out, calls = self._serve_config_bound(
            [
                self._audit(1),
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                self._audit(3),
            ],
            healthy_probes=3,
            wire_steps=[lambda: self._queued_submit_context("aud_lifecycle_r2603")],
        )

        self.assertEqual(len(calls), 1)
        self.assertIn("result", out[1], "the lifecycle call itself must still be answered")
        self.assertEqual(
            self._local_runs(out), [],
            "a tools/list refresh must not launder the outage into a DE Lite opening",
        )
        self.assertEqual(out[2]["error"]["data"].get("run_id"), "aud_lifecycle_r2603")

    def test_non_audit_unknown_outcome_still_leaves_de_lite_available(self):
        """F26-03 / lock 5, the REVERSE scoping direction: an unrelated tool dying on the wire
        leaves no hosted audit unaccounted for, so a later genuine offline audit must still get
        its DE Lite advisory. This is the over-blocking side of the guard — without the
        `_is_audit_traffic` scoping the latch would arm here and the local run would vanish."""
        chat = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "some_other_tool", "arguments": {}}}
        out, calls = self._serve_config_bound(
            [chat, self._audit(2)],
            healthy_probes=2,          # serve() start, then the chat call's own send
            wire_steps=[ConnectionResetError],
        )

        # The scoping predicate is what separates the two messages; assert it directly so the
        # behavioural expectation below is anchored to a stated rule, not a coincidence.
        self.assertFalse(shim._is_audit_traffic(chat))
        self.assertTrue(shim._is_audit_traffic(self._audit(2)))
        self.assertEqual(len(calls), 1)
        self.assertIn("error", out[0], "the dead non-audit call still reports its own failure")
        self.assertEqual(
            len(self._local_runs(out)), 1,
            "a session holding no unaccounted hosted audit keeps its offline DE Lite path",
        )

    def test_terminal_follow_up_without_a_run_id_retires_the_correlated_run(self):
        """F26-03 / lock 6: a follow-up answer can report a TERMINAL status while omitting the
        run_id — the caller already named the run in the request. Retiring only on a
        response-carried id leaves a finished run pending forever, and with the guard now
        covering the production path that phantom permanently denies DE Lite to this session."""
        out, calls = self._serve_config_bound(
            [
                self._audit(1),
                self._wait(2, "aud_term_r2603"),
                self._audit(3),
            ],
            healthy_probes=4,   # start, the submit's refresh, the submit's send, the wait's send
            wire_steps=[
                lambda: self._queued_submit_context("aud_term_r2603"),
                lambda: self._body_context(2, {"status": "completed", "title": "Random topic"}),
            ],
        )

        self.assertEqual(len(calls), 2, "the third audit must not reach the down hub")
        self.assertIn("result", out[1], "the terminal follow-up is still answered normally")
        self.assertEqual(
            len(self._local_runs(out)), 1,
            "a hosted run the hub reported terminal must stop blocking the offline DE Lite path",
        )

    def test_ordinary_traffic_after_unknown_outcome_starts_no_audit(self):
        """R26-08: a non-audit call during the same outage triggers neither hosted nor Lite."""
        dispatches = []
        probe = mock.Mock(side_effect=[True, True, False])
        chat = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "some_other_tool", "arguments": {}}}
        with mock.patch.object(shim, "hub_reachable", probe), \
             mock.patch.object(
                 shim, "build_opener",
                 side_effect=self._dispatch_then_die(dispatches, ConnectionResetError)):
            out = _run(
                shim.Forwarder("https://hub.example.com", "secret"),
                [json.dumps(self._audit(1)), json.dumps(chat)],
            )

        self.assertEqual(self._local_runs(out), [])
        self.assertIn("error", out[1])


class GeArtifactHostileResponseTests(unittest.TestCase):
    """The threat model that motivates the redirect guard — a hostile or compromised hub —
    also reaches `resp.read().decode()`. These three used to escape as raw UnicodeDecodeError /
    IncompleteRead / ValueError, breaking the "every failure path raises ShellError" contract;
    and their str() carries server-chosen bytes, which must never reach model context."""

    def _forwarder(self, hub):
        self.addCleanup(hub.close)
        return shim.Forwarder(hub.base, "tok_secret")

    def test_non_utf8_body_fails_closed_without_echoing_the_bytes(self):
        fwd = self._forwarder(_MockHub(raw=b'{"kind":"svg","data":"\xff\xfe"}'))
        with self.assertRaises(config.ShellError) as caught:
            fwd.get_ge_artifact("r1", timeout_s=5)
        # UnicodeDecodeError's own message names the offending byte ("can't decode byte 0xff
        # in position 22") — the ShellError must not carry it onward to the model.
        self.assertNotIn("0xff", str(caught.exception))

    def test_truncated_body_fails_closed(self):
        # Content-Length promises 5000 bytes, the hub hangs up after 1 → IncompleteRead,
        # which subclasses http.client.HTTPException, not URLError/OSError.
        fwd = self._forwarder(_MockHub(raw=b"{", declared_length=5000))
        with self.assertRaises(config.ShellError):
            fwd.get_ge_artifact("r1", timeout_s=5)

    def test_malformed_redirect_location_fails_closed(self):
        # urllib parses Location BEFORE consulting NoRedirect, so a malformed one raises
        # ValueError("Invalid IPv6 URL") from inside the opener rather than reaching the guard.
        fwd = self._forwarder(_MockHub(redirect_to="http://["))
        with self.assertRaises(config.ShellError):
            fwd.get_ge_artifact("r1", timeout_s=5)

    def _record_read_sizes(self):
        """Record the bytes each read1() pulls from the response body.

        The bound is the whole point of the cap: a hostile oversize body must be DETECTED
        without pulling the whole thing into memory. The shared read helper (http_safety.
        read_within_budget) drains via read1() and stops the instant it has one byte past
        the cap — so the total pulled is exactly cap+1 for an oversize body, and exactly the
        body length for a legal one. The tests assert on that total, which is what separates
        a bounded read from an unbounded resp.read() that would OOM on a real hostile body."""
        sizes = []
        real_read1 = http.client.HTTPResponse.read1

        def spy(response, amt=-1):
            chunk = real_read1(response, amt)
            sizes.append(len(chunk))
            return chunk

        patcher = mock.patch.object(http.client.HTTPResponse, "read1", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sizes

    def test_oversized_body_fails_closed(self):
        # An unbounded read().decode() lets a hostile hub OOM the client — the same threat
        # /db/render caps at _MAX_BOARD_BYTES. The cap is patched down here so the test
        # asserts the ENFORCEMENT, not the ceiling's magnitude (a real 64 MB body would
        # buy nothing but a slow test); test_the_cap_matches_the_sibling_fetch pins the value.
        sizes = self._record_read_sizes()
        body = b'{"kind":"svg","data":"' + b"x" * 500 + b'"}'
        fwd = self._forwarder(_MockHub(raw=body))
        with mock.patch.object(shim, "_MAX_GE_ARTIFACT_BYTES", 64):
            with self.assertRaises(config.ShellError) as caught:
                fwd.get_ge_artifact("r1", timeout_s=5)
        # The read was BOUNDED — it pulled exactly cap+1 bytes and stopped, never the whole body.
        self.assertEqual(sum(sizes), 65)
        message = str(caught.exception)
        # Assert the cap's OWN wording: every raise site in get_ge_artifact embeds
        # "/v1/ge/artifact", so matching only that would also pass if this error were
        # swallowed and re-emitted by one of the handlers below the raise.
        self.assertIn("exceeds", message)
        self.assertIn("/v1/visual/artifacts", message)
        # No server body → model: the oversize report must not echo the payload back.
        self.assertNotIn("xxx", message)

    def test_one_byte_over_the_cap_is_detected_not_truncated(self):
        # The discriminating case for DETECT-vs-truncate: the first `cap` bytes are a
        # complete, valid artifact and the body carries ONE more. read(cap) would hand back
        # legal JSON and silently drop the tail — a hostile hub's oversize body would parse
        # clean. Reading cap+1 sees the extra byte and fails closed.
        legal = json.dumps({"kind": "svg", "data": "<svg/>"}, separators=(",", ":")).encode("utf-8")
        fwd = self._forwarder(_MockHub(raw=legal + b" "))
        with mock.patch.object(shim, "_MAX_GE_ARTIFACT_BYTES", len(legal)):
            with self.assertRaises(config.ShellError) as caught:
                fwd.get_ge_artifact("r1", timeout_s=5)
        self.assertIn("exceeds", str(caught.exception))

    def test_body_at_the_cap_still_returns_the_artifact(self):
        # The other side of that boundary: a body of exactly the cap is legal and must
        # parse, so the ceiling is exclusive — it rejects cap+1, not cap.
        sizes = self._record_read_sizes()
        data = "<svg/>"
        body = json.dumps({"kind": "svg", "data": data}, separators=(",", ":")).encode("utf-8")
        fwd = self._forwarder(_MockHub(raw=body))
        with mock.patch.object(shim, "_MAX_GE_ARTIFACT_BYTES", len(body)):
            self.assertEqual(fwd.get_ge_artifact("r1", timeout_s=5), {"kind": "svg", "data": data})
        # A legal body of exactly the cap is pulled in full and no further — cap+1 is never exceeded.
        self.assertEqual(sum(sizes), len(body))

    def test_the_cap_matches_the_sibling_fetch(self):
        # All three authenticated reads pull a hub body into client memory under the same
        # threat model, so they carry the same ceiling — this pins them together so one
        # can't drift. The hub's own render limit is server-side and NOT known here; this
        # is an OOM guard sized far above any plausible render, not a content policy.
        from client.popup import launcher

        self.assertEqual(shim._MAX_GE_ARTIFACT_BYTES, launcher._MAX_BOARD_BYTES)


class ForwardResponseSizeTests(unittest.TestCase):
    """`forward()` reads the /mcp JSON-RPC response into client memory under the SAME
    hostile-hub threat model as the artifact fetch — an unbounded `resp.read().decode()`
    lets a compromised hub stream without limit and OOM the client (the timeout is per
    socket op, not a transfer deadline, so a hub that keeps sending stays under it). It
    gets the same bounded read. The ceiling is sized INDEPENDENTLY, though: a /mcp body is
    a JSON-RPC result destined for MODEL context (an audit panel's findings), not the raw
    popup bytes the GE artifact carries, so it sits far below the 64 MB artifact ceiling."""

    def _forwarder(self, hub):
        self.addCleanup(hub.close)
        return shim.Forwarder(hub.base, "tok_secret")   # 127.0.0.1 → the plaintext guard allows it

    def _record_read_sizes(self):
        """Record the bytes every response read forward() performs pulls into memory. The
        SUCCESS body now drains through the shared helper's read1() (in chunks); the 4xx/5xx
        error body is still read by the except-arm's exc.read(cap+1). Spy BOTH primitives and
        assert on the total pulled — the bound is that a hostile body is DETECTED at cap+1,
        never read whole (an unbounded read would raise the same on a test body while OOMing a
        real one). Mirrors GeArtifactHostileResponseTests, which only needs read1."""
        sizes = []
        real_read = http.client.HTTPResponse.read
        real_read1 = http.client.HTTPResponse.read1

        def read_spy(response, amt=None):
            chunk = real_read(response, amt)
            sizes.append(len(chunk))
            return chunk

        def read1_spy(response, amt=-1):
            chunk = real_read1(response, amt)
            sizes.append(len(chunk))
            return chunk

        for name, spy in (("read", read_spy), ("read1", read1_spy)):
            patcher = mock.patch.object(http.client.HTTPResponse, name, spy)
            patcher.start()
            self.addCleanup(patcher.stop)
        return sizes

    def test_oversized_response_fails_closed_without_echoing_the_bytes(self):
        # An unbounded read().decode() lets a hostile hub OOM the client. The cap is patched
        # down here so the test asserts the ENFORCEMENT, not the ceiling's magnitude (a real
        # 8 MB body would buy nothing but a slow test); the independence test pins the value.
        sizes = self._record_read_sizes()
        body = b'{"jsonrpc":"2.0","id":1,"result":"' + b"x" * 500 + b'"}'
        fwd = self._forwarder(_MockHub(raw=body))
        with mock.patch.object(shim, "_MAX_MCP_RESPONSE_BYTES", 64):
            with self.assertRaises(config.ShellError) as caught:
                fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        # The read was BOUNDED — it pulled exactly cap+1 bytes and stopped, never the whole body.
        self.assertEqual(sum(sizes), 65)
        message = str(caught.exception)
        self.assertIn("exceeds", message)
        self.assertIn("/mcp", message)
        self.assertNotIn("xxx", message)   # no server body → model

    def test_one_byte_over_the_cap_is_detected_not_truncated(self):
        # The discriminating case for DETECT-vs-truncate: read(cap) would hand back the legal
        # prefix and silently drop the tail — a hostile oversize body would parse clean.
        # Reading cap+1 sees the extra byte and fails closed (mirrors the GE sibling).
        legal = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
                           separators=(",", ":")).encode("utf-8")
        fwd = self._forwarder(_MockHub(raw=legal + b" "))
        with mock.patch.object(shim, "_MAX_MCP_RESPONSE_BYTES", len(legal)):
            with self.assertRaises(config.ShellError) as caught:
                fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("exceeds", str(caught.exception))

    def test_body_at_the_cap_still_forwards_and_parses(self):
        # The other side of that boundary: a body of exactly the cap is legal and must parse,
        # so the ceiling is exclusive — it rejects cap+1, not cap.
        sizes = self._record_read_sizes()
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        fwd = self._forwarder(_MockHub(raw=body))
        with mock.patch.object(shim, "_MAX_MCP_RESPONSE_BYTES", len(body)):
            self.assertEqual(
                fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), payload)
        # A legal body of exactly the cap is pulled in full and no further — cap+1 is never exceeded.
        self.assertEqual(sum(sizes), len(body))

    def test_the_mcp_cap_is_sized_independently_of_the_artifact_cap(self):
        # The /mcp body is a JSON-RPC result destined for MODEL context (an audit panel's
        # findings — deep audit results are ~240 KB, the adjudication payload is server-capped
        # at 2 MB), NOT the raw popup bytes the GE artifact carries. Different sizing rationale
        # (PR#49 closeout: "那条 body 是给 model 消费的，天花板是独立决定"), so it must NOT inherit
        # the 64 MB artifact ceiling by reflex: pin it strictly below, so a copy that equates
        # the two ceilings fails HERE.
        self.assertLess(shim._MAX_MCP_RESPONSE_BYTES, shim._MAX_GE_ARTIFACT_BYTES)
        self.assertEqual(shim._MAX_MCP_RESPONSE_BYTES, 8 * 1024 * 1024)

    def test_oversized_4xx_error_body_is_bounded_and_not_echoed(self):
        # The success read is not the only authenticated read: the except-HTTPError arm reads
        # exc.read() to echo the hub's 4xx/5xx error contract. Uncapped, a hostile hub OOMs the
        # client via an error-status body, bypassing the success cap (deep audit 29486897,
        # gemini/grok). The read must be bounded and an over-cap body dropped, not echoed.
        sizes = self._record_read_sizes()
        body = b'{"error":"' + b"x" * 500 + b'"}'
        fwd = self._forwarder(_MockHub(raw=body, status=400))
        with mock.patch.object(shim, "_MAX_MCP_RESPONSE_BYTES", 64):
            with self.assertRaises(config.ShellError) as caught:
                fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        # The error read was BOUNDED to cap+1, not the whole hostile body.
        self.assertEqual(sum(sizes), 65)
        message = str(caught.exception)
        self.assertIn("HTTP 400", message)   # the error is still reported...
        self.assertNotIn("xxx", message)     # ...but the over-cap body is dropped, never echoed

    def test_small_4xx_error_body_is_still_echoed(self):
        # The cap must only trim a HOSTILE error body — a normal small one is still surfaced
        # verbatim (the hub's own error contract, PR#49). Regression guard on the bound's floor.
        fwd = self._forwarder(_MockHub(raw=b'{"detail":"nope"}', status=400))
        with self.assertRaises(config.ShellError) as caught:
            fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        message = str(caught.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("nope", message)

    def test_json_with_oversized_int_fails_closed_as_shell_error(self):
        # json.loads() raises a PLAIN ValueError (not JSONDecodeError) on an integer literal past
        # the int-string digit limit — outside the read block's (HTTPException, ValueError) arm, so
        # it escaped raw, breaking the every-failure-is-ShellError contract (deep audit 29486897,
        # codex-deep). The digit limit is lowered so the case is deterministic across runtimes.
        old_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(640)   # 640 is the stdlib floor
        self.addCleanup(sys.set_int_max_str_digits, old_limit)
        body = b'{"jsonrpc":"2.0","id":1,"result":' + b"1" * 1000 + b"}"
        fwd = self._forwarder(_MockHub(raw=body))
        with self.assertRaises(config.ShellError) as caught:
            fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        message = str(caught.exception)
        self.assertIn("invalid JSON", message)
        self.assertNotIn("1111", message)   # type only — the offending digits never reach the model


class HubReadDeadlineTests(unittest.TestCase):
    """A byte cap alone does NOT bound a hostile hub: urllib's ``timeout`` is per socket op,
    not a transfer deadline, so a hub that dribbles ~1 byte just before each socket-timeout
    window holds the read for a very long time while staying under the cap and never tripping
    the timeout. The shared read helper adds a wall-clock transfer deadline that bounds this.
    Both shim reads (``/mcp`` and ``/v1/ge/artifact``) run it against a REAL dripping socket."""

    def _forwarder(self, **drip_kwargs):
        hub = _MockHub(drip=drip_kwargs)
        self.addCleanup(hub.close)
        return shim.Forwarder(hub.base, "tok_secret")

    def test_get_ge_artifact_is_bounded_by_the_transfer_deadline(self):
        # The drip would take 30s to "finish"; the deadline is 1s. If the read were bounded
        # only by the cap and the per-op socket timeout, it would hang for the full drip.
        fwd = self._forwarder(delay=0.02)
        started = time.monotonic()
        with self.assertRaises(config.ShellError) as caught:
            fwd.get_ge_artifact("r1", timeout_s=1)
        elapsed = time.monotonic() - started
        # Bounded: it did NOT wait for the 30s drip. A generous ceiling keeps CI non-flaky
        # while still being far below the drip length, so only the deadline can explain it.
        self.assertLess(elapsed, 8.0, "the slow-stream read was not bounded by the deadline")
        # And it is specifically the DEADLINE, not the cap or an incidental parse error.
        self.assertIn("budget", str(caught.exception))
        # No server bytes reach the model — the dribble (spaces) must not be echoed.
        self.assertNotIn(" " * 5, str(caught.exception))

    def test_forward_is_bounded_by_the_transfer_deadline(self):
        # /mcp is the transport thread — a slowloris here freezes the whole MCP surface, so it
        # needs the deadline at least as much as the artifact fetch.
        fwd = self._forwarder(delay=0.02)
        started = time.monotonic()
        with self.assertRaises(shim.OutcomeUnknownError) as caught:
            fwd.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout_s=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8.0, "the slow-stream read was not bounded by the deadline")
        self.assertEqual(caught.exception.data["reason"], "read_timeout")
        self.assertTrue(caught.exception.data["request_sent"])
        self.assertFalse(caught.exception.data["retryable"])
        self.assertNotIn("tok_secret", str(caught.exception))


class HubReadChunkedRefusalTests(unittest.TestCase):
    """A Transfer-Encoding: chunked response is read by a multi-recv read1() the transfer
    deadline cannot interrupt (its framing dribble would hold the thread), so the shared
    helper refuses chunked outright — end-to-end through the real urllib stack here."""

    def _forwarder(self):
        hub = _MockHub(chunked=True)
        self.addCleanup(hub.close)
        return shim.Forwarder(hub.base, "tok_secret")

    def test_get_ge_artifact_refuses_a_chunked_response(self):
        with self.assertRaises(config.ShellError) as caught:
            self._forwarder().get_ge_artifact("r1", timeout_s=5)
        self.assertIn("chunked", str(caught.exception))

    def test_forward_refuses_a_chunked_response(self):
        with self.assertRaises(config.ShellError) as caught:
            self._forwarder().forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("chunked", str(caught.exception))


class HubReadTruncationTests(unittest.TestCase):
    """A Content-Length body that ends before its declared length is a truncated read. The
    dangerous case is a truncated prefix that is ITSELF valid JSON: the former unbounded
    resp.read() raised IncompleteRead on it; the shared helper must likewise fail closed
    rather than hand a silently-short body to the parser (and thence the model)."""

    def test_a_truncated_but_valid_json_body_is_not_accepted(self):
        # A complete, parseable artifact — but the hub declares far more than it sends, then
        # closes. Detect-not-accept: the read must fail, not return the short (valid) body.
        legal = json.dumps({"kind": "svg", "data": "<svg/>"}, separators=(",", ":")).encode("utf-8")
        hub = _MockHub(raw=legal, declared_length=len(legal) + 5000)
        self.addCleanup(hub.close)
        fwd = shim.Forwarder(hub.base, "tok_secret")
        with self.assertRaises(config.ShellError) as caught:
            fwd.get_ge_artifact("r1", timeout_s=5)
        # No server bytes in the message, and specifically the completeness failure.
        self.assertIn("declared length", str(caught.exception))
        self.assertNotIn("svg", str(caught.exception))


class McpToolLocalizationTests(unittest.TestCase):
    """tools/list descriptions moved to client.i18n.mcp_tools(); the shim overlays them per host
    locale. Two invariants: (1) en-US output is byte-for-byte what shipped before i18n (baseline
    snapshot); (2) zh-CN localizes prose while preserving every protocol token AND the untouched
    wire structure (enum / required / bounds). _MCP_LOCALE is a process memo, so each test pins it."""

    _BASELINE = json.loads(
        (Path(__file__).with_name("_mcp_tools_en_baseline.json")).read_text(encoding="utf-8")
    )

    @staticmethod
    def _norm(obj):
        # ORDER-SENSITIVE (no sort_keys): the claim is byte-for-byte wire identity, and JSON key
        # order is part of the bytes. _localize_tool rebuilds {name, description, inputSchema} in the
        # pre-i18n source order; the baseline is captured from HEAD in that same insertion order.
        return json.dumps(obj, ensure_ascii=False)

    def _advertised(self, locale):
        """Every tool the shim advertises, keyed by name, with descriptions overlaid at `locale`."""
        with mock.patch.object(shim, "_MCP_LOCALE", locale):
            lite = shim.LiteForwarder().forward(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
            offline = shim.OfflineForwarder().forward(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
            # display + local-audit merges run on the ACTIVATED path over a host listing
            merged = shim._merge_display_tools({"result": {"tools": []}})["result"]["tools"]
            complete = shim._merge_local_audit_tools(
                {"result": {"tools": []}})["result"]["tools"]
        by_name = {}
        for tool in (*lite, *offline, *merged, *complete):
            by_name[tool["name"]] = tool
        return by_name

    def test_en_us_tools_list_matches_the_pre_i18n_baseline_byte_for_byte(self):
        advertised = self._advertised("en-US")
        # every non-display baseline tool + all display schemas reappear identically
        expected = {k: v for k, v in self._BASELINE.items() if k != "display_schemas"}
        for schema in self._BASELINE["display_schemas"]:
            expected[schema["name"]] = schema
        for name, exp in expected.items():
            self.assertIn(name, advertised, "%s no longer advertised" % name)
            self.assertEqual(self._norm(advertised[name]), self._norm(exp),
                             "%s en-US output drifted from the pre-i18n baseline" % name)

    def test_zh_cn_localizes_prose_but_preserves_protocol_tokens_and_structure(self):
        advertised = self._advertised("zh-CN")
        open_ge = advertised["open_ge"]
        # prose is Chinese now …
        self.assertIn("渲染", open_ge["description"])
        # … but every protocol token survives verbatim (the calling model still parses these)
        for token in ("{status:'scheduled', run_id}",
                      "{status:'request_outcome_unknown', retryable:true, client_request_id}",
                      "open_ge_popup", "comic", "infographic", "diagram"):
            self.assertIn(token, open_ge["description"],
                          "zh-CN open_ge dropped protocol token %r" % token)
        # the wire STRUCTURE is untouched by localization
        self.assertEqual(open_ge["inputSchema"]["properties"]["mode"]["enum"],
                         ["comic", "infographic", "diagram"])
        self.assertEqual(open_ge["inputSchema"]["required"], ["mode", "spec"])
        self.assertEqual(open_ge["inputSchema"]["properties"]["client_request_id"]["maxLength"], 128)
        # db_board_result keeps its exact numeric bounds token
        self.assertIn("≤55s", advertised["db_board_result"]["description"])
        # the session-locale hint enum is still advertised (unrelated to description locale)
        self.assertEqual(
            advertised["audit_skill_submit"]["inputSchema"]["properties"]["args"]["properties"][
                "ui_locale"]["enum"], ["zh-CN", "en-US"])

    def test_every_advertised_tool_has_a_locale_table_entry(self):
        # _localize_tool ships structure-only (no description) for a tool missing from the table;
        # this guards that the table and the wire schemas never drift apart.
        from client import i18n
        table = i18n.mcp_tools("en-US")
        for name, tool in self._advertised("en-US").items():
            self.assertIn(name, table, "%s advertised but absent from MCP_TOOLS table" % name)
            self.assertIn("description", tool, "%s advertised with no description overlaid" % name)

    def test_localize_tool_does_not_mutate_the_shared_base_schema(self):
        # deepcopy isolation: overlaying zh-CN must not leave prose on the module-level constants.
        shim._localize_tool(shim._DISPLAY_TOOL_SCHEMAS[1], "zh-CN")
        self.assertNotIn("description", shim._DISPLAY_TOOL_SCHEMAS[1])
        self.assertNotIn(
            "description",
            shim._DISPLAY_TOOL_SCHEMAS[1]["inputSchema"]["properties"]["mode"])
        self.assertNotIn("description", shim._ACTIVATION_REQUIRED_TOOL)

    def test_activation_required_call_message_follows_host_locale(self):
        with mock.patch.object(shim, "_MCP_LOCALE", "zh-CN"):
            resp = shim.LiteForwarder().forward({
                "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "activation_required", "arguments": {}}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "activation_required")
        self.assertEqual(payload["message"], "在使用 Decision Engine 工具前，请先激活此设备。")

    def test_no_assembled_tool_is_ever_description_less(self):
        # Descriptions carry the model's behavioral guardrails ("do not inline", "do not poll"); a
        # description-less tool is a silent contract regression. Assert none across en / zh / unknown.
        for locale in ("en-US", "zh-CN", "fr-FR"):
            for name, tool in self._advertised(locale).items():
                self.assertTrue(tool.get("description"),
                                "%s shipped without a description at %s" % (name, locale))

    def test_partial_locale_table_falls_back_to_english_prose_not_empty(self):
        # A zh table missing one entry must degrade to the en-US prose for THAT tool, never to a
        # description-less tool (the whole point of the guardrail text).
        from client import i18n
        real = i18n.mcp_tools

        def holed(locale):
            table = dict(real(locale))
            if locale == "zh-CN":
                table.pop("open_ge", None)
            return table

        with mock.patch.object(i18n, "mcp_tools", holed):
            tool = shim._localize_tool(
                next(s for s in shim._DISPLAY_TOOL_SCHEMAS if s["name"] == "open_ge"), "zh-CN")
        self.assertTrue(tool["description"].startswith("Open a server-rendered"))  # en fallback

    def test_localize_tool_never_raises_on_the_transport_thread(self):
        # tools/list previously used static literals that could not fail; localization must not turn
        # it into a crash vector. Any table/import error degrades to a structure-only copy.
        from client import i18n

        def boom(locale):
            raise RuntimeError("table blew up")

        with mock.patch.object(i18n, "mcp_tools", boom):
            tool = shim._localize_tool(shim._DISPLAY_TOOL_SCHEMAS[0], "en-US")
        self.assertEqual(tool["name"], "open_ge_popup")       # structure survives
        self.assertNotIn("description", tool)                 # prose degraded, no raise

    def test_locale_resolution_failure_falls_back_without_breaking_transport(self):
        from client import i18n

        with (
            mock.patch.object(shim, "_MCP_LOCALE", None),
            mock.patch.object(
                i18n, "resolve_locale", side_effect=RuntimeError("locale probe failed")
            ),
        ):
            self.assertEqual(shim._mcp_locale(), "en-US")

    def test_activation_message_does_not_crash_when_table_entry_missing(self):
        # f1 regression: the message site uses .get, so an absent table entry yields a null message
        # rather than a KeyError that would kill the tool-call response on the transport thread.
        from client import i18n
        real = i18n.mcp_tools

        def without_activation(locale):
            table = dict(real(locale))
            table.pop("activation_required", None)
            return table

        with mock.patch.object(i18n, "mcp_tools", without_activation), \
                mock.patch.object(shim, "_MCP_LOCALE", "en-US"):
            resp = shim.LiteForwarder().forward({
                "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "activation_required", "arguments": {}}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "activation_required")  # still answers, no crash

    def test_every_base_tool_name_is_present_in_the_en_us_table(self):
        # _localize_tool's en-US fallback + non-empty-description guarantee only hold if every wire
        # schema name exists in the en-US table. Pin that so a new tool can't ship description-less.
        from client import i18n
        table = i18n.mcp_tools("en-US")
        bases = [shim._ACTIVATION_REQUIRED_TOOL, shim._LITE_AUDIT_TOOL,
                 shim._LOCAL_AUDIT_COMPLETE_TOOL, shim._SERVICE_UNAVAILABLE_TOOL,
                 *shim._DISPLAY_TOOL_SCHEMAS]
        for base in bases:
            self.assertIn(base["name"], table,
                          "%s has no en-US MCP_TOOLS entry" % base["name"])


class McpErrorMessageLocalizationTests(unittest.TestCase):
    """The two model-facing -32001 error.message sentences follow the host locale.

    Only these two are localized. The terse machine slugs ("explicit audit topic required",
    "local audit completion rejected") stay English by design: they are paired with a structured
    ``{status, reason}`` payload and read by code, so they are protocol, not prose.
    """

    def _offline_error(self):
        return shim.OfflineForwarder().forward({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "open_ge", "arguments": {}}})["error"]

    def _lite_error(self):
        return shim.LiteForwarder().forward({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "open_ge", "arguments": {}}})["error"]

    def test_en_us_messages_are_byte_identical_to_the_pre_i18n_literals(self):
        # The English wire bytes must not shift: these are the exact pre-change literals.
        with mock.patch.object(shim, "_MCP_LOCALE", "en-US"):
            self.assertEqual(self._offline_error()["message"],
                             "Decision Engine service unavailable — reconnect and retry")
            self.assertEqual(
                self._lite_error()["message"],
                "activation_required: activate this device before using Decision Engine tools")

    def test_zh_cn_messages_are_translated_and_keep_code_and_data(self):
        with mock.patch.object(shim, "_MCP_LOCALE", "zh-CN"):
            offline, lite = self._offline_error(), self._lite_error()
        self.assertEqual(offline["message"], "Decision Engine 服务不可用 — 请重新连接后重试")
        self.assertEqual(lite["message"],
                         "activation_required: 在使用 Decision Engine 工具前，请先激活此设备")
        # error CODE and structured data are protocol, never localized.
        self.assertEqual(offline["code"], -32001)
        self.assertEqual(lite["code"], -32001)
        self.assertEqual(lite["data"], {"status": "activation_required", "tool": "open_ge"})

    def test_activation_prefix_token_is_verbatim_in_every_language(self):
        # Callers may match on the `activation_required:` token (it mirrors data.status), so it is
        # preserved untranslated even in zh-CN. Also pins the shim's own substring assertion.
        for locale in ("en-US", "zh-CN", "fr-FR"):
            with mock.patch.object(shim, "_MCP_LOCALE", locale):
                message = self._lite_error()["message"]
            self.assertTrue(message.startswith("activation_required:"), message)

    def test_unknown_locale_falls_back_to_english_prose(self):
        with mock.patch.object(shim, "_MCP_LOCALE", "fr-FR"):
            self.assertEqual(self._offline_error()["message"],
                             "Decision Engine service unavailable — reconnect and retry")

    def test_machine_slug_messages_stay_english(self):
        # Deliberate scope boundary: the code-read slugs are NOT localized.
        with mock.patch.object(shim, "_MCP_LOCALE", "zh-CN"):
            explicit = shim._explicit_audit_required_response(3)["error"]
            rejected = shim._local_audit_completion_response(4, {"bogus": True})["error"]
        self.assertEqual(explicit["message"], "explicit audit topic required")
        self.assertEqual(rejected["message"], "local audit completion rejected")

    def test_message_lookup_never_raises_on_the_transport_thread(self):
        # Same guarantee as _localize_tool: a broken table degrades to the English literal rather
        # than killing the transport thread mid-response.
        from client import i18n

        def boom(locale):
            raise RuntimeError("table blew up")

        with mock.patch.object(i18n, "mcp_errors", boom), \
                mock.patch.object(shim, "_MCP_LOCALE", "zh-CN"):
            error = self._offline_error()
        self.assertEqual(error["code"], -32001)
        self.assertEqual(error["message"],
                         "Decision Engine service unavailable — reconnect and retry")

    def test_missing_slug_falls_back_to_the_english_literal(self):
        from client import i18n
        real = i18n.mcp_errors

        def holed(locale):
            table = dict(real(locale))
            table.pop("service_unavailable", None)
            return table

        with mock.patch.object(i18n, "mcp_errors", holed), \
                mock.patch.object(shim, "_MCP_LOCALE", "zh-CN"):
            self.assertEqual(self._offline_error()["message"],
                             "Decision Engine service unavailable — reconnect and retry")

    def test_hardcoded_fallback_matches_the_en_us_table_exactly(self):
        # DRIFT GUARD: _MCP_ERROR_FALLBACK duplicates en_US.MCP_ERRORS so a dead table still emits
        # the pre-i18n literal. If someone edits the English wording in one place only, the two
        # sources disagree and the emitted message depends on whether the table loaded — catch that.
        from client import i18n
        self.assertEqual(shim._MCP_ERROR_FALLBACK, dict(i18n.mcp_errors("en-US")))

    def test_every_slug_used_in_the_shim_has_a_fallback_entry(self):
        # _mcp_error_message ends in _MCP_ERROR_FALLBACK[slug]; that final subscript is only total
        # because every call site passes a slug present in the dict. Pin the invariant by scanning
        # the source, so a new call site with an unknown slug fails here and not on the transport
        # thread as a KeyError inside an error response.
        source = pathlib.Path(shim.__file__).read_text(encoding="utf-8")
        slugs = set(re.findall(r"_mcp_error_message\(\s*[\"']([^\"']+)[\"']", source))
        self.assertTrue(slugs, "no _mcp_error_message call sites found — scan pattern went stale")
        for slug in sorted(slugs):
            self.assertIn(slug, shim._MCP_ERROR_FALLBACK,
                          "%s has no _MCP_ERROR_FALLBACK entry" % slug)
