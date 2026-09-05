from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from installer import (
    client_host_lifecycle,
    config,
    managed_install,
    mcp_config,
    updater,
)


class ClientHostLifecycleContractTests(unittest.TestCase):
    def test_synthetic_adapter_dispatches_without_host_branches(self):
        calls = []

        def execute(operation, request):
            calls.append((operation, request))
            return client_host_lifecycle.LifecycleResult(
                host_family="synthetic",
                operation=operation,
                status="completed",
            )

        adapter = client_host_lifecycle.HostLifecycleAdapter(
            adapter_id="synthetic-owned-v1",
            host_family="synthetic",
            supported_operations=frozenset({"update", "uninstall"}),
            execute=execute,
        )

        result = client_host_lifecycle.dispatch_operation(
            "synthetic",
            "update",
            request={"generation": "next"},
            host_adapter_id="synthetic-owned-v1",
            adapters={"synthetic-owned-v1": adapter},
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(calls, [("update", {"generation": "next"})])

    def test_registered_host_without_adapter_is_explicitly_unsupported(self):
        for host in ("claude-code", "claude-desktop", "codex"):
            result = client_host_lifecycle.dispatch_registered_operation(
                host,
                "update",
                request={},
            )
            self.assertEqual(result.status, "unsupported_for_host")
            self.assertEqual(
                result.host_family,
                mcp_config.CLIENT_SPECS[host].host_family,
            )

    def test_adapter_cannot_claim_another_host(self):
        adapter = client_host_lifecycle.HostLifecycleAdapter(
            adapter_id="mismatch",
            host_family="cursor",
            supported_operations=frozenset({"update"}),
            execute=lambda _operation, _request: None,
        )
        with self.assertRaisesRegex(ValueError, "host family"):
            client_host_lifecycle.dispatch_operation(
                "synthetic",
                "update",
                request={},
                host_adapter_id="mismatch",
                adapters={"mismatch": adapter},
            )

    def test_release_ids_must_advance_monotonically(self):
        self.assertEqual(
            client_host_lifecycle.require_monotonic_release(
                "19-" + "1" * 40,
                "20-" + "2" * 40,
            ),
            (19, 20),
        )
        for target in ("19-" + "2" * 40, "18-" + "2" * 40):
            with self.assertRaisesRegex(ValueError, "newer"):
                client_host_lifecycle.require_monotonic_release(
                    "19-" + "1" * 40,
                    target,
                )

    def test_authorized_path_is_derived_from_root_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "skills"
            target = root / "audit"
            self.assertEqual(
                client_host_lifecycle.require_authorized_child(target, root),
                target,
            )
            with self.assertRaisesRegex(ValueError, "authorized root"):
                client_host_lifecycle.require_authorized_child(
                    root.parent / "outside",
                    root,
                )

    def test_authorized_path_rejects_an_existing_link_component(self):
        candidate = Path("C:/synthetic-root/child")
        with (
            mock.patch.object(
                managed_install,
                "_reject_link_components",
                side_effect=managed_install.ManagedInstallError("link"),
            ),
            self.assertRaisesRegex(ValueError, "link component"),
        ):
            client_host_lifecycle.require_authorized_child(
                candidate,
                candidate.parent,
            )

    def test_release_evidence_truth_table(self):
        one = "1" * 40
        two = "2" * 40
        cases = (
            ((one, one, one), "current"),
            ((two, one, one), "update_required"),
            ((two, two, one), "reload_required"),
            ((one, one, None), "unknown"),
            ((None, one, one), "unknown"),
            (("bad", one, one), "invalid"),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                assessment = client_host_lifecycle.assess_release_evidence(
                    client_host_lifecycle.ReleaseEvidence(
                        target_commit=values[0],
                        installed_commit=values[1],
                        running_commit=values[2],
                    )
                )
                self.assertEqual(assessment.state, expected)

    def test_owned_release_binding_uses_the_ownership_generation(self):
        managed = client_host_lifecycle.ReleaseEvidence(
            target_commit="2" * 40,
            installed_commit="2" * 40,
            running_commit="1" * 40,
        )
        bound = client_host_lifecycle.bind_owned_release(
            managed,
            "19-" + "3" * 40,
        )
        self.assertEqual(bound.installed_commit, "3" * 40)
        self.assertEqual(
            client_host_lifecycle.assess_release_evidence(bound).state,
            "update_required",
        )

    def test_managed_target_is_the_installed_protected_release_not_pending(self):
        state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=20,
            last_release_commit="2" * 40,
            last_manifest_sha256="3" * 64,
            last_version="0.20.0",
            target_commit="4" * 40,
            running_commit="1" * 40,
        )
        with mock.patch.object(updater, "_read_update_state", return_value=state):
            evidence = client_host_lifecycle.collect_managed_release_evidence(
                Path("C:/synthetic-managed-root")
            )

        self.assertEqual(evidence.target_commit, "2" * 40)
        self.assertEqual(evidence.running_commit, "1" * 40)

    def test_all_hosts_use_the_shared_managed_root_lifecycle(self):
        for host in ("claude-code", "claude-desktop", "codex", "cursor"):
            self.assertIsNone(mcp_config.CLIENT_SPECS[host].lifecycle_adapter)

    def test_cursor_owned_lifecycle_dispatch_is_retired(self):
        evidence = client_host_lifecycle.ReleaseEvidence(
            target_commit="2" * 40,
            installed_commit="2" * 40,
            running_commit="1" * 40,
        )
        with mock.patch(
            "installer.cursor_host_lifecycle.collect_owned_release_evidence",
            return_value=evidence,
        ):
            result = client_host_lifecycle.dispatch_registered_operation(
                "cursor",
                "status",
                request={"managed_root": Path("C:/synthetic-managed-root")},
            )
        self.assertEqual(result.status, "unsupported_for_host")

    def test_public_module_entrypoint_reports_cursor_owned_lifecycle_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = str(Path(tmp) / "managed-root")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "installer.client_host_lifecycle",
                    "status",
                    "--host",
                    "cursor",
                    "--managed-root",
                    private,
                ],
                cwd=Path(__file__).resolve().parents[2],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "unsupported_for_host")
        payload_strings = []

        def collect_strings(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    collect_strings(key)
                    collect_strings(item)
            elif isinstance(value, list):
                for item in value:
                    collect_strings(item)
            elif isinstance(value, str):
                payload_strings.append(value)

        collect_strings(payload)
        private_forms = {
            private,
            private.replace("\\", "/"),
            Path(private).name,
        }
        for private_form in private_forms:
            self.assertTrue(
                all(private_form not in value for value in payload_strings)
            )
            self.assertNotIn(private_form, completed.stderr)

    def test_cli_reports_unsupported_legacy_host_without_paths(self):
        output = io.StringIO()
        private = "C:/private-user/managed-root"
        with redirect_stdout(output):
            rc = client_host_lifecycle.main(
                [
                    "status",
                    "--host",
                    "codex",
                    "--managed-root",
                    private,
                ]
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(payload["status"], "unsupported_for_host")
        self.assertNotIn(private, output.getvalue())

    def test_retired_cursor_adapter_does_not_enter_legacy_lock_path(self):
        from installer import cursor_activation

        with mock.patch.object(
            cursor_activation,
            "uninstall_cursor_owned",
            side_effect=cursor_activation.CursorActivationError(
                "Cursor activation is already in progress"
            ),
        ):
            result = client_host_lifecycle.dispatch_registered_operation(
                "cursor",
                "uninstall",
                request={"managed_root": Path("C:/synthetic-managed-root")},
            )
        self.assertEqual(result.status, "unsupported_for_host")

    def test_retired_cursor_status_ignores_legacy_ownership_state(self):
        from installer import client_host_ownership

        private = "C:/private-user/managed-root"
        with mock.patch(
            "installer.cursor_host_lifecycle.collect_owned_release_evidence",
            side_effect=client_host_ownership.OwnershipError(private),
        ):
            result = client_host_lifecycle.dispatch_registered_operation(
                "cursor",
                "status",
                request={"managed_root": Path(private)},
            )

        self.assertEqual(result.status, "unsupported_for_host")
        self.assertNotIn(private, repr(result))

    def test_retired_cursor_uninstall_ignores_legacy_payload_state(self):
        private = "C:/private-user/managed-root"
        with mock.patch(
            "installer.cursor_host_lifecycle.cursor_activation.uninstall_cursor_owned",
            side_effect=config.ShellError(private),
        ):
            result = client_host_lifecycle.dispatch_registered_operation(
                "cursor",
                "uninstall",
                request={"managed_root": Path(private)},
            )

        self.assertEqual(result.status, "unsupported_for_host")
        self.assertNotIn(private, repr(result))

    def test_cli_normalizes_unexpected_type_error_without_traceback_or_path(self):
        output = io.StringIO()
        private = "C:/private-user/managed-root"
        with (
            mock.patch.object(
                client_host_lifecycle,
                "dispatch_registered_operation",
                side_effect=TypeError(private),
            ),
            redirect_stdout(output),
        ):
            rc = client_host_lifecycle.main(
                ["status", "--host", "cursor", "--managed-root", private]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["reason"], "TypeError")
        self.assertNotIn(private, output.getvalue())


if __name__ == "__main__":
    unittest.main()
