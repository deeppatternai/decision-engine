"""Behavior lock for per-transport host capabilities.

The pure state machine is intentionally OS-free. Windows probes produce a
``PreflightResult``; this module proves publication, revocation, cleanup, and
call-local denial without pretending cross-platform CI executed Win32 APIs.
"""

from __future__ import annotations

import unittest
import threading
import time
from dataclasses import replace
from datetime import date
from unittest import mock

from installer.client_host_runtime import (
    CapabilityStage,
    PreflightResult,
    TransportCapabilityGate,
    VolatileResult,
    CursorProcessEvidence,
    CursorWindowsEvidence,
    PRODUCTION_CURSOR_WINDOWS_FIXTURE,
    evaluate_cursor_windows_preflight,
    load_cursor_windows_fixture,
    cursor_initialize_matches_fixture,
    run_cursor_windows_preflight,
)


class _Lease:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class TransportCapabilityGateTestCase(unittest.TestCase):
    def test_successful_preflight_and_initialize_publish_eligibility_once(self):
        lease = _Lease()
        gate = TransportCapabilityGate.conditional(followup=False, stopper=False)

        self.assertTrue(gate.complete_preflight(PreflightResult.ready(lease)))
        self.assertEqual(gate.stage, CapabilityStage.READY_FOR_INITIALIZE)
        self.assertFalse(gate.display_enabled)
        self.assertTrue(gate.finalize_initialize(matches=True))
        self.assertEqual(gate.stage, CapabilityStage.ELIGIBLE)
        self.assertTrue(gate.display_enabled)
        self.assertFalse(gate.followup_enabled)
        self.assertFalse(gate.stopper_enabled)
        self.assertEqual(lease.close_count, 0)

        self.assertFalse(gate.finalize_initialize(matches=False))
        self.assertEqual(gate.stage, CapabilityStage.ELIGIBLE)

    def test_preflight_denial_is_final_and_late_success_closes_its_lease(self):
        gate = TransportCapabilityGate.conditional(followup=False, stopper=False)
        self.assertTrue(
            gate.complete_preflight(PreflightResult.denied("fixture-disabled"))
        )
        late = _Lease()
        self.assertFalse(gate.complete_preflight(PreflightResult.ready(late)))
        self.assertEqual(gate.stage, CapabilityStage.PREFLIGHT_DENIED)
        self.assertEqual(gate.reason, "fixture-disabled")
        self.assertEqual(late.close_count, 1)

    def test_initialize_mismatch_and_failure_close_the_transferred_lease(self):
        for operation, reason in (
            (lambda gate: gate.finalize_initialize(matches=False), "initialize-mismatch"),
            (lambda gate: gate.fail_initialize(), "initialize-failed"),
        ):
            with self.subTest(reason=reason):
                lease = _Lease()
                gate = TransportCapabilityGate.conditional(
                    followup=False, stopper=False
                )
                gate.complete_preflight(PreflightResult.ready(lease))
                self.assertTrue(operation(gate))
                self.assertEqual(gate.stage, CapabilityStage.DENIED)
                self.assertEqual(gate.reason, reason)
                self.assertEqual(lease.close_count, 1)

    def test_window_absence_denies_only_that_call_and_can_recover(self):
        gate = self._eligible_gate()
        denied = gate.check_display(
            lambda: VolatileResult.unavailable("display-context-unavailable")
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "display-context-unavailable")
        self.assertEqual(gate.stage, CapabilityStage.ELIGIBLE)

        restored = gate.check_display(VolatileResult.available)
        self.assertTrue(restored.allowed)
        self.assertEqual(gate.stage, CapabilityStage.ELIGIBLE)

    def test_process_session_or_expiry_failure_irreversibly_revokes(self):
        for reason in ("terminal-exit", "session-changed", "fixture-expired"):
            with self.subTest(reason=reason):
                lease = _Lease()
                gate = self._eligible_gate(lease)
                result = gate.check_display(lambda: VolatileResult.revoked(reason))
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, reason)
                self.assertEqual(gate.stage, CapabilityStage.REVOKED)
                self.assertEqual(lease.close_count, 1)
                self.assertFalse(
                    gate.check_display(VolatileResult.available).allowed
                )

    def test_teardown_is_idempotent_and_late_callbacks_cannot_publish(self):
        lease = _Lease()
        gate = TransportCapabilityGate.conditional(followup=False, stopper=False)
        gate.complete_preflight(PreflightResult.ready(lease))
        gate.close()
        gate.close()
        self.assertEqual(gate.stage, CapabilityStage.CLOSED)
        self.assertEqual(lease.close_count, 1)

        late = _Lease()
        self.assertFalse(gate.complete_preflight(PreflightResult.ready(late)))
        self.assertEqual(late.close_count, 1)

    def test_static_hosts_keep_independent_display_followup_and_stopper_bits(self):
        gate = TransportCapabilityGate.static(
            display=True, followup=True, stopper=True
        )
        self.assertEqual(gate.stage, CapabilityStage.ELIGIBLE)
        self.assertTrue(gate.display_enabled)
        self.assertTrue(gate.followup_enabled)
        self.assertTrue(gate.stopper_enabled)
        self.assertTrue(gate.check_display(VolatileResult.available).allowed)

    def _eligible_gate(self, lease: _Lease | None = None) -> TransportCapabilityGate:
        lease = lease or _Lease()
        gate = TransportCapabilityGate.conditional(followup=False, stopper=False)
        gate.complete_preflight(PreflightResult.ready(lease))
        gate.finalize_initialize(matches=True)
        return gate


class CursorWindowsEvidenceEvaluatorTestCase(unittest.TestCase):
    def _fixture(self):
        return load_cursor_windows_fixture(
            {
                "fixture_id": "cursor-win-synthetic-v1",
                "capture_id": "capture-a1",
                "enabled": True,
                "expires_on": "2099-12-31",
                "client_name": "Cursor",
                "client_version": "3.3.30",
                "roots_list_changed": True,
                "parent_image_basename": "node.exe",
                "required_argv_rules": [
                    {
                        "value": "--cursor-mcp",
                        "match": "exact",
                        "scope": "direct-parent",
                    }
                ],
                "forbidden_argv_rules": [
                    {
                        "value": "--print",
                        "match": "exact",
                        "scope": "any-hop",
                    }
                ],
                "max_ancestor_hops": 3,
                "terminal_hop": 2,
                "architecture": "x64",
                "terminal_image_basename": "Cursor.exe",
                "terminal_known_roots": ["LocalAppData"],
                "terminal_relative_paths": ["Programs/Cursor/Cursor.exe"],
                "terminal_product": "Cursor",
                "terminal_company": "Anysphere, Inc.",
                "terminal_file_version": "3.3.30",
                "window_class_names": ["Chrome_WidgetWin_1"],
                "release_sha256": "A" * 64,
                "release_signer_subject": "CN=Anysphere, Inc.",
                "release_capture_id": "capture-a1",
            }
        )

    def _evidence(self, *, terminal_argv=("Cursor.exe",)):
        lease = _Lease()
        return CursorWindowsEvidence(
            platform="win32",
            remote_environment=False,
            shim_session_id=1,
            active_console_session_id=1,
            current_process_creation_time=300,
            ancestors=(
                CursorProcessEvidence(
                    pid=20,
                    image_basename="node.exe",
                    argv=("node.exe", "--cursor-mcp"),
                    session_id=1,
                    creation_time=200,
                ),
                CursorProcessEvidence(
                    pid=10,
                    image_basename="Cursor.exe",
                    argv=terminal_argv,
                    session_id=1,
                    creation_time=100,
                    final_path="C:/Users/A/AppData/Local/Programs/Cursor/Cursor.exe",
                    fixed_volume=True,
                    reparse_target=False,
                    product="Cursor",
                    company="Anysphere, Inc.",
                    file_version="3.3.30",
                ),
            ),
            known_roots={"LocalAppData": "C:/Users/A/AppData/Local"},
            terminal_lease=lease,
        )

    def test_exact_synthetic_fixture_and_evidence_grant(self):
        result = evaluate_cursor_windows_preflight(
            self._fixture(), self._evidence(), today=date(2026, 7, 30)
        )

        self.assertTrue(result.eligible)
        self.assertEqual(result.reason, "preflight-ready")

    def test_forbidden_cli_token_denies_and_production_fixture_stays_disabled(self):
        denied = evaluate_cursor_windows_preflight(
            self._fixture(),
            self._evidence(terminal_argv=("Cursor.exe", "--print")),
            today=date(2026, 7, 30),
        )

        self.assertFalse(denied.eligible)
        self.assertEqual(denied.reason, "cli-mode")
        self.assertFalse(PRODUCTION_CURSOR_WINDOWS_FIXTURE.enabled)

    def test_initialize_matches_exact_fixture_roots_capability(self):
        fixture = replace(self._fixture(), roots_list_changed=False)
        matching = {
            "clientInfo": {
                "name": fixture.client_name,
                "version": fixture.client_version,
            },
            "capabilities": {"roots": {"listChanged": False}},
        }

        self.assertTrue(cursor_initialize_matches_fixture(matching, fixture))
        self.assertFalse(
            cursor_initialize_matches_fixture(
                {
                    **matching,
                    "capabilities": {"roots": {"listChanged": True}},
                },
                fixture,
            )
        )

    def test_production_fixture_records_redacted_cursor_3538_ide_capture(self):
        fixture = PRODUCTION_CURSOR_WINDOWS_FIXTURE

        self.assertFalse(fixture.enabled)
        self.assertEqual(fixture.fixture_id, "cursor-windows-local-3538-v1")
        self.assertEqual(
            fixture.capture_id,
            "cursor-3538-agent-20260730-paired",
        )
        self.assertEqual(fixture.client_name, "cursor-vscode")
        self.assertEqual(fixture.client_version, "1.0.0")
        self.assertFalse(fixture.roots_list_changed)
        self.assertEqual(fixture.parent_image_basename, "Cursor.exe")
        self.assertEqual(fixture.terminal_hop, 2)
        self.assertEqual(fixture.max_ancestor_hops, 2)
        self.assertEqual(fixture.terminal_known_roots, ("ProgramFiles",))
        self.assertEqual(
            fixture.terminal_relative_paths,
            ("cursor/Cursor.exe",),
        )
        self.assertEqual(fixture.terminal_company, "Anysphere")
        self.assertEqual(fixture.terminal_file_version, "3.5.38")
        self.assertEqual(
            fixture.release_sha256,
            (
                "703c886461176d1f9a60b79af396814f"
                "da4b48ef24d27ba06a0bf1c593874e54"
            ),
        )
        self.assertEqual(fixture.expires_on, date(2026, 8, 6))

    def test_redacted_production_ide_pair_grants_only_when_explicitly_enabled(self):
        fixture = replace(PRODUCTION_CURSOR_WINDOWS_FIXTURE, enabled=True)
        lease = _Lease()
        evidence = CursorWindowsEvidence(
            platform="win32",
            remote_environment=False,
            shim_session_id=7,
            active_console_session_id=7,
            current_process_creation_time=300,
            ancestors=(
                CursorProcessEvidence(
                    pid=20,
                    image_basename="Cursor.exe",
                    argv=("Cursor.exe", "--type=utility"),
                    session_id=7,
                    creation_time=200,
                    final_path="C:/Program Files/cursor/Cursor.exe",
                    fixed_volume=True,
                    reparse_target=False,
                    product="Cursor",
                    company="Anysphere",
                    file_version="3.5.38",
                ),
                CursorProcessEvidence(
                    pid=10,
                    image_basename="Cursor.exe",
                    argv=("Cursor.exe",),
                    session_id=7,
                    creation_time=100,
                    final_path="C:/Program Files/cursor/Cursor.exe",
                    fixed_volume=True,
                    reparse_target=False,
                    product="Cursor",
                    company="Anysphere",
                    file_version="3.5.38",
                ),
            ),
            known_roots={"ProgramFiles": "C:/Program Files"},
            terminal_lease=lease,
        )
        initialize = {
            "clientInfo": {"name": "cursor-vscode", "version": "1.0.0"},
            "capabilities": {"roots": {"listChanged": False}},
        }

        self.assertTrue(cursor_initialize_matches_fixture(initialize, fixture))
        self.assertTrue(
            evaluate_cursor_windows_preflight(
                fixture,
                evidence,
                today=date(2026, 7, 30),
            ).eligible
        )
        self.assertEqual(lease.close_count, 0)

    def test_substituted_cursor_direct_parent_denies_before_grant(self):
        fixture = replace(PRODUCTION_CURSOR_WINDOWS_FIXTURE, enabled=True)
        evidence = CursorWindowsEvidence(
            platform="win32",
            remote_environment=False,
            shim_session_id=7,
            active_console_session_id=7,
            current_process_creation_time=300,
            ancestors=(
                CursorProcessEvidence(
                    pid=20,
                    image_basename="Cursor.exe",
                    argv=("Cursor.exe", "--type=utility"),
                    session_id=7,
                    creation_time=200,
                    final_path="C:/Users/Owner/Cursor.exe",
                    fixed_volume=True,
                    reparse_target=False,
                    product="Cursor",
                    company="Anysphere",
                    file_version="3.5.38",
                ),
                CursorProcessEvidence(
                    pid=10,
                    image_basename="Cursor.exe",
                    argv=("Cursor.exe",),
                    session_id=7,
                    creation_time=100,
                    final_path="C:/Program Files/cursor/Cursor.exe",
                    fixed_volume=True,
                    reparse_target=False,
                    product="Cursor",
                    company="Anysphere",
                    file_version="3.5.38",
                ),
            ),
            known_roots={"ProgramFiles": "C:/Program Files"},
            terminal_lease=_Lease(),
        )

        denied = evaluate_cursor_windows_preflight(
            fixture,
            evidence,
            today=date(2026, 7, 30),
        )

        self.assertFalse(denied.eligible)
        self.assertEqual(denied.reason, "binary-location-mismatch")

    def test_paired_agent_cli_topology_never_grants_native_display(self):
        fixture = replace(PRODUCTION_CURSOR_WINDOWS_FIXTURE, enabled=True)
        evidence = CursorWindowsEvidence(
            platform="win32",
            remote_environment=False,
            shim_session_id=7,
            active_console_session_id=7,
            current_process_creation_time=300,
            ancestors=(
                CursorProcessEvidence(
                    pid=20,
                    image_basename="node.exe",
                    argv=("node.exe",),
                    session_id=7,
                    creation_time=200,
                ),
                CursorProcessEvidence(
                    pid=10,
                    image_basename="powershell.exe",
                    argv=("powershell.exe",),
                    session_id=7,
                    creation_time=100,
                ),
            ),
            known_roots={"ProgramFiles": "C:/Program Files"},
            terminal_lease=_Lease(),
        )
        initialize = {
            "clientInfo": {"name": "Cursor", "version": "1.0.0"},
            "capabilities": {},
        }

        self.assertFalse(cursor_initialize_matches_fixture(initialize, fixture))
        denied = evaluate_cursor_windows_preflight(
            fixture,
            evidence,
            today=date(2026, 7, 30),
        )
        self.assertFalse(denied.eligible)
        self.assertEqual(denied.reason, "process-role-mismatch")

    def test_empty_terminal_version_metadata_denies_fail_closed(self):
        for field in ("product", "company", "file_version"):
            evidence = self._evidence()
            terminal = replace(evidence.ancestors[1], **{field: ""})
            evidence = replace(
                evidence,
                ancestors=(evidence.ancestors[0], terminal),
            )

            result = evaluate_cursor_windows_preflight(
                self._fixture(),
                evidence,
                today=date(2026, 7, 30),
            )

            with self.subTest(field=field):
                self.assertEqual(result.reason, "binary-version-mismatch")
                self.assertEqual(
                    evidence.terminal_lease.close_count,
                    1,
                )

    def test_disabled_native_probe_boundary_never_calls_probe_or_matches_initialize(self):
        disabled = replace(PRODUCTION_CURSOR_WINDOWS_FIXTURE, enabled=False)
        probe = mock.Mock()
        result = run_cursor_windows_preflight(
            fixture=disabled,
            probes=probe,
            platform="win32",
        )

        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "fixture-disabled")
        probe.collect.assert_not_called()
        self.assertFalse(
            cursor_initialize_matches_fixture(
                {
                    "clientInfo": {"name": "Cursor", "version": "3.3.30"},
                    "capabilities": {"roots": {"listChanged": True}},
                },
                disabled,
            )
        )

    def test_stalled_probe_times_out_and_late_evidence_closes_its_lease(self):
        release = threading.Event()
        evidence = self._evidence()

        class Probe:
            def collect(self, _fixture, _deadline):
                release.wait(0.1)
                return evidence

        started = time.monotonic()
        result = run_cursor_windows_preflight(
            fixture=self._fixture(),
            probes=Probe(),
            platform="win32",
            timeout_s=0.03,
        )
        elapsed = time.monotonic() - started
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "preflight-timeout")
        self.assertLess(elapsed, 0.08)

        release.set()
        deadline = time.monotonic() + 1.0
        while evidence.terminal_lease.close_count == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(evidence.terminal_lease.close_count, 1)

    def test_fifth_stalled_probe_is_rejected_as_saturated(self):
        release = threading.Event()
        all_done = threading.Event()
        completion_lock = threading.Lock()
        completion_count = 0

        class Probe:
            def collect(self, _fixture, _deadline):
                nonlocal completion_count
                try:
                    release.wait(0.2)
                    return self_outer._evidence()
                finally:
                    with completion_lock:
                        completion_count += 1
                        if completion_count == 4:
                            all_done.set()

        self_outer = self
        all_workers_completed = False
        try:
            for _ in range(4):
                result = run_cursor_windows_preflight(
                    fixture=self._fixture(),
                    probes=Probe(),
                    platform="win32",
                    timeout_s=0.01,
                )
                self.assertEqual(result.reason, "preflight-timeout")
            started = time.monotonic()
            saturated = run_cursor_windows_preflight(
                fixture=self._fixture(),
                probes=Probe(),
                platform="win32",
                timeout_s=0.1,
            )
            self.assertEqual(saturated.reason, "preflight-saturated")
            self.assertLess(time.monotonic() - started, 0.05)
        finally:
            release.set()
            all_workers_completed = all_done.wait(1.0)
        self.assertTrue(all_workers_completed)

    def test_fixture_loader_rejects_unknown_fields_and_capture_mismatch(self):
        fixture = self._fixture()
        raw = {
            "fixture_id": fixture.fixture_id,
            "capture_id": fixture.capture_id,
            "enabled": fixture.enabled,
            "expires_on": fixture.expires_on.isoformat(),
            "client_name": fixture.client_name,
            "client_version": fixture.client_version,
            "roots_list_changed": fixture.roots_list_changed,
            "parent_image_basename": fixture.parent_image_basename,
            "required_argv_rules": [
                vars(rule) for rule in fixture.required_argv_rules
            ],
            "forbidden_argv_rules": [
                vars(rule) for rule in fixture.forbidden_argv_rules
            ],
            "max_ancestor_hops": fixture.max_ancestor_hops,
            "terminal_hop": fixture.terminal_hop,
            "architecture": fixture.architecture,
            "terminal_image_basename": fixture.terminal_image_basename,
            "terminal_known_roots": list(fixture.terminal_known_roots),
            "terminal_relative_paths": list(fixture.terminal_relative_paths),
            "terminal_product": fixture.terminal_product,
            "terminal_company": fixture.terminal_company,
            "terminal_file_version": fixture.terminal_file_version,
            "window_class_names": list(fixture.window_class_names),
            "release_sha256": fixture.release_sha256,
            "release_signer_subject": fixture.release_signer_subject,
            "release_capture_id": "different-capture",
            "unknown": True,
        }

        with self.assertRaises(ValueError):
            load_cursor_windows_fixture(raw)


if __name__ == "__main__":
    unittest.main()
