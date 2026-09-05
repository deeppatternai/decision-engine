"""Behavior locks for fail-closed managed launcher activation."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from installer import managed_activation, release_contract, updater


class ManagedActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Resolved, because managed_install rejects a root whose path contains a symlink
        # (_reject_link_components) and macOS's default TMPDIR lives under /var, itself a
        # symlink to /private/var — so an unresolved tmp root fails that guard on every Mac.
        self.root = Path(self.tmp.name).resolve() / "decision-engine"
        self.root.mkdir()
        self.manifest = release_contract.ReleaseManifest(
            schema=1,
            repository_id="deeppatternai/decision-engine",
            channel="stable",
            release_sequence=2,
            version="0.2.0",
            tag="v0.2.0",
            commit="1" * 40,
            min_python="3.12",
            published_at="2026-07-22T00:00:00Z",
            key_id="production",
        )
        self.acquired = mock.Mock()
        self.acquired.manifest = self.manifest
        self.acquired.signature = mock.sentinel.signature
        self.acquired.source.name = "github"
        self.verified = release_contract.VerifiedRelease(
            manifest=self.manifest,
            key_id="production",
        )

    def test_cli_reports_core_ready_and_deferred_wiring(self):
        result = managed_activation.ActivationResult(
            self.root,
            "0.2.0",
            "1" * 40,
            "github",
            ("claude-code", "cursor"),
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(
                managed_activation,
                "activate_prepared_install",
                return_value=result,
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = managed_activation.main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("claude-code", stdout.getvalue())
        self.assertIn("MCP wiring is deferred", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def _boundary_patches(self):
        return (
            mock.patch.object(
                managed_activation.managed_install,
                "canonical_managed_root",
                return_value=self.root,
            ),
            mock.patch.object(
                managed_activation.managed_install, "_require_fixed_managed_root"
            ),
        )

    def test_missing_production_key_refuses_before_network_or_wiring(self):
        first, second = self._boundary_patches()
        with (
            first,
            second,
            mock.patch.object(managed_activation, "_require_prepared_remotes"),
            mock.patch.object(
                managed_activation.release_acquisition,
                "load_trusted_release_keys",
                return_value={},
            ),
            mock.patch.object(
                managed_activation.release_acquisition, "discover_initial_release"
            ) as discover,
            mock.patch.object(managed_activation.mcp_config, "write_entry") as write,
        ):
            with self.assertRaisesRegex(
                managed_activation.ManagedActivationError, "public keys"
            ):
                managed_activation.activate_prepared_install(
                    self.root, clients=["codex"]
                )
        discover.assert_not_called()
        write.assert_not_called()

    def test_protocol_marker_is_published_without_any_external_wiring(self):
        events = []
        state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=2,
            last_release_commit="1" * 40,
            last_manifest_sha256=release_contract.manifest_sha256(self.manifest),
            last_version="0.2.0",
        )
        first, second = self._boundary_patches()
        with (
            first,
            second,
            mock.patch.object(
                managed_activation.release_acquisition,
                "load_trusted_release_keys",
                return_value={"production": mock.sentinel.key},
            ),
            mock.patch.object(
                managed_activation.release_acquisition,
                "discover_initial_release",
                return_value=self.acquired,
            ),
            mock.patch.object(managed_activation, "_require_prepared_remotes"),
            mock.patch.object(
                managed_activation,
                "_verify_prepared_checkout",
                return_value=self.verified,
            ),
            mock.patch.object(
                managed_activation.managed_install,
                "write_managed_identity",
                side_effect=lambda *_args: events.append("identity"),
            ),
            mock.patch.object(
                managed_activation.updater,
                "_read_update_state",
                side_effect=managed_activation.updater.UpdateInspectionError("missing"),
            ),
            mock.patch.object(
                managed_activation.update_transaction,
                "initialize_release_state",
                side_effect=lambda *_args, **_kwargs: events.append("state") or state,
            ),
            mock.patch.object(managed_activation.mcp_config, "write_entry") as write,
            mock.patch.object(
                managed_activation.update_transaction,
                "_write_protocol_ready_locked",
                side_effect=lambda *_args: events.append("protocol"),
            ),
        ):
            result = managed_activation.activate_prepared_install(
                self.root, clients=["codex", "claude-code", "cursor"]
            )

        self.assertEqual(events, ["identity", "state", "protocol"])
        write.assert_not_called()
        self.assertEqual(result.clients, ("codex", "claude-code", "cursor"))
        self.assertEqual(result.failed_clients, ())
        self.assertEqual(result.commit, "1" * 40)

    def test_existing_state_is_reverified_before_any_wiring(self):
        state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=2,
            last_release_commit="1" * 40,
            last_manifest_sha256=release_contract.manifest_sha256(self.manifest),
            last_version="0.2.0",
        )
        first, second = self._boundary_patches()
        with (
            first,
            second,
            mock.patch.object(managed_activation, "_require_prepared_remotes"),
            mock.patch.object(
                managed_activation.release_acquisition,
                "load_trusted_release_keys",
                return_value={"production": mock.sentinel.key},
            ),
            mock.patch.object(
                managed_activation.release_acquisition,
                "discover_initial_release",
                return_value=self.acquired,
            ),
            mock.patch.object(
                managed_activation,
                "_verify_prepared_checkout",
                side_effect=managed_activation.ManagedActivationError(
                    "prepared HEAD does not match the signed release"
                ),
            ),
            mock.patch.object(managed_activation.updater, "_read_update_state", return_value=state),
            mock.patch.object(managed_activation.mcp_config, "write_entry") as write,
            mock.patch.object(
                managed_activation.update_transaction, "_write_protocol_ready_locked"
            ) as publish,
        ):
            with self.assertRaisesRegex(
                managed_activation.ManagedActivationError, "prepared HEAD"
            ):
                managed_activation.activate_prepared_install(self.root, clients=["codex"])

        write.assert_not_called()
        publish.assert_not_called()

    def test_no_detected_client_refuses_before_network(self):
        first, second = self._boundary_patches()
        with (
            first,
            second,
            mock.patch.object(
                managed_activation.mcp_config, "detect_clients", return_value=[]
            ),
            mock.patch.object(
                managed_activation.release_acquisition, "discover_initial_release"
            ) as discover,
        ):
            with self.assertRaisesRegex(
                managed_activation.ManagedActivationError, "at least one"
            ):
                managed_activation.activate_prepared_install(self.root)
        discover.assert_not_called()

    def test_anonymous_https_unreachable_falls_back_to_authenticated_git(self):
        # Same scoped exception as bootstrap_managed_install: a still-private
        # repository makes the anonymous mirrors unreachable for anyone, so
        # this one-time, human-triggered activation step falls back to the
        # already-authenticated git remotes on the prepared checkout, trying
        # them in official priority order and skipping any not configured.
        events = []
        state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=2,
            last_release_commit="1" * 40,
            last_manifest_sha256=release_contract.manifest_sha256(self.manifest),
            last_version="0.2.0",
        )
        # This test's own resolved root (not self.root — see module note above
        # about /tmp -> /var on macOS tripping the unrelated symlink-chain
        # check further down install_transaction; other tests in this class
        # pre-date that and are unaffected since they mock past that point).
        resolved_root = Path(self.tmp.name).resolve() / "decision-engine"
        with (
            mock.patch.object(
                managed_activation.managed_install,
                "canonical_managed_root",
                return_value=resolved_root,
            ),
            mock.patch.object(
                managed_activation.managed_install, "_require_fixed_managed_root"
            ),
            mock.patch.object(
                managed_activation.release_acquisition,
                "load_trusted_release_keys",
                return_value={"production": mock.sentinel.key},
            ),
            mock.patch.object(
                managed_activation.release_acquisition,
                "discover_initial_release",
                side_effect=managed_activation.release_acquisition.ReleaseTransportError(
                    "simulated anonymous 404"
                ),
            ),
            mock.patch.object(
                managed_activation.updater, "_read_remotes", return_value={"gitee": "x", "github": "y"}
            ),
            mock.patch.object(managed_activation.updater, "_GitReader"),
            mock.patch.object(
                managed_activation.release_acquisition,
                "discover_initial_release_via_git",
                return_value=self.acquired,
            ) as via_git,
            mock.patch.object(managed_activation, "_require_prepared_remotes"),
            mock.patch.object(managed_activation, "_verify_prepared_checkout"),
            mock.patch.object(
                managed_activation.managed_install,
                "write_managed_identity",
                side_effect=lambda *_args: events.append("identity"),
            ),
            mock.patch.object(
                managed_activation.updater,
                "_read_update_state",
                side_effect=managed_activation.updater.UpdateInspectionError("missing"),
            ),
            mock.patch.object(
                managed_activation.update_transaction,
                "initialize_release_state",
                side_effect=lambda *_args, **_kwargs: events.append("state") or state,
            ),
            mock.patch.object(managed_activation.mcp_config, "write_entry") as write,
            mock.patch.object(
                managed_activation.update_transaction,
                "_write_protocol_ready_locked",
                side_effect=lambda *_args: events.append("protocol"),
            ),
        ):
            result = managed_activation.activate_prepared_install(
                resolved_root, clients=["codex"]
            )

        # Official priority order (github before gitee), filtered to only the
        # remotes actually configured on this checkout — never an unfiltered
        # or reordered list.
        via_git.assert_called_once_with(
            resolved_root, ("github", "gitee"), {"production": mock.sentinel.key}
        )
        self.assertEqual(events, ["identity", "state", "protocol"])
        write.assert_not_called()
        self.assertEqual(result.commit, "1" * 40)


if __name__ == "__main__":
    unittest.main()
