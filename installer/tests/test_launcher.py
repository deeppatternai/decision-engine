"""Behavior locks for the managed updater and fresh-interpreter handoff."""

from __future__ import annotations

import contextlib
import io
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from installer import launcher, update_coordination, update_transaction


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "decision-engine"
        self.root.mkdir()
        fixed = mock.patch.object(
            launcher.managed_install, "_require_fixed_managed_root"
        )
        fixed.start()
        self.addCleanup(fixed.stop)
        self.state = launcher.updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=1,
            last_release_commit="1" * 40,
            last_manifest_sha256="2" * 64,
            last_version="0.1.0",
        )
        state_reader = mock.patch.object(
            launcher.updater, "_read_update_state", return_value=self.state
        )
        state_reader.start()
        self.addCleanup(state_reader.stop)
        self.native_startup_update_gate = update_coordination.startup_update_gate
        startup_gate = mock.patch.object(
            launcher.update_coordination,
            "startup_update_gate",
            side_effect=lambda *_args, **_kwargs: contextlib.nullcontext(),
        )
        startup_gate.start()
        self.addCleanup(startup_gate.stop)

    def test_default_update_startup_budget_is_sixty_seconds(self):
        self.assertEqual(launcher.STARTUP_UPDATE_BUDGET_SECONDS, 60.0)

    def test_launch_repairs_existing_windows_config_before_admission(self):
        with (
            mock.patch.object(
                launcher.config, "harden_existing_windows_config", return_value=True
            ) as harden,
            mock.patch.object(launcher, "_managed_control_present", return_value=False),
            mock.patch.object(launcher, "_serve_legacy_shim", return_value=0),
        ):
            self.assertEqual(launcher.launch(self.root), 0)

        harden.assert_called_once_with(self.root / "config.json")

    def test_client_host_resolution_preserves_explicit_unknown(self):
        with mock.patch.dict(
            launcher.os.environ,
            {launcher.CLIENT_HOST_ENV: "claude"},
            clear=False,
        ):
            self.assertEqual(launcher._resolved_client_host(" CODEX "), "codex")
            self.assertEqual(
                launcher._resolved_client_host("unknown-host"), "unknown-host"
            )
            self.assertEqual(launcher._resolved_client_host(None), "claude")

    def test_serve_only_reads_handoff_environment(self):
        with (
            mock.patch.dict(
                launcher.os.environ,
                {launcher.CLIENT_HOST_ENV: "codex"},
                clear=False,
            ),
            mock.patch.object(
                launcher.config, "harden_existing_windows_config", return_value=True
            ) as harden,
            mock.patch.object(launcher, "_serve_shim", return_value=0) as serve,
        ):
            result = launcher.main(
                ["--managed-root", str(self.root), "--serve-only"]
            )

        self.assertEqual(result, 0)
        harden.assert_called_once_with(self.root / "config.json")
        serve.assert_called_once_with(self.root, client_host="codex")

    def test_serve_only_preserves_unknown_explicit_environment(self):
        with (
            mock.patch.dict(
                launcher.os.environ,
                {launcher.CLIENT_HOST_ENV: "unknown-host"},
                clear=False,
            ),
            mock.patch.object(launcher, "_serve_shim", return_value=0) as serve,
        ):
            result = launcher.main(
                ["--managed-root", str(self.root), "--serve-only"]
            )

        self.assertEqual(result, 0)
        serve.assert_called_once_with(self.root, client_host="unknown-host")

    def test_missing_release_keys_serves_known_good_without_network_or_git_mutation(self):
        with (
            mock.patch.object(launcher, "_managed_control_present", return_value=True),
            mock.patch.object(launcher, "_head_commit", return_value="1" * 40),
            mock.patch.object(launcher, "_finalize_journal", return_value=None),
            mock.patch.object(launcher, "_updates_enabled", return_value=True),
            mock.patch.object(launcher, "load_trusted_release_keys", return_value={}),
            mock.patch.object(launcher, "_attempt_update") as update,
            mock.patch.object(launcher, "_serve_shim", return_value=7) as serve,
        ):
            result = launcher.launch(self.root, stdin=io.StringIO(), stdout=io.StringIO())

        self.assertEqual(result, 7)
        update.assert_not_called()
        serve.assert_called_once()

    def test_release_transport_failure_still_serves_known_good(self):
        with (
            mock.patch.object(launcher, "_managed_control_present", return_value=True),
            mock.patch.object(launcher, "_head_commit", return_value="1" * 40),
            mock.patch.object(launcher, "_finalize_journal", return_value=None),
            mock.patch.object(launcher, "_updates_enabled", return_value=True),
            mock.patch.object(
                launcher, "load_trusted_release_keys", return_value={"production": mock.sentinel.key}
            ),
            mock.patch.object(
                launcher,
                "_attempt_update",
                side_effect=launcher.release_acquisition.ReleaseTransportError("offline"),
            ),
            mock.patch.object(launcher, "_serve_shim", return_value=8) as serve,
        ):
            result = launcher.launch(self.root)
        self.assertEqual(result, 8)
        serve.assert_called_once()

    def test_attempt_update_falls_back_to_git_when_anonymous_mirrors_unreachable(self):
        # This is the ongoing, unattended per-MCP-start path — the knowingly
        # accepted private-beta-only exception to "never touch personal git
        # credentials in the background" (see discover_release_via_git's
        # docstring). Anonymous mirrors are tried first and must fail before
        # git is ever touched.
        acquired = mock.Mock()
        acquired.manifest.commit = self.state.last_release_commit
        acquired.manifest.tag = "v0.1.0"
        acquired.source.name = "github"
        keys = {"production": mock.sentinel.key}
        with (
            mock.patch.object(
                launcher.release_acquisition,
                "discover_release",
                side_effect=launcher.release_acquisition.ReleaseTransportError("offline"),
            ) as anonymous,
            mock.patch.object(
                launcher.updater, "_read_remotes", return_value={"gitee": "x", "github": "y"}
            ),
            mock.patch.object(launcher.updater, "_GitReader"),
            mock.patch.object(
                launcher.release_acquisition, "discover_release_via_git", return_value=acquired
            ) as via_git,
            mock.patch.object(
                launcher.release_contract,
                "manifest_sha256",
                return_value=self.state.last_manifest_sha256,
            ),
            mock.patch.object(launcher.release_acquisition, "fetch_release_objects") as fetch_objects,
            mock.patch.object(
                launcher.update_transaction, "apply_present_update", return_value=mock.sentinel.result
            ) as apply,
        ):
            result = launcher._attempt_update(self.root, keys, deadline=1e18)

        anonymous.assert_called_once_with(self.state, keys, deadline=1e18)
        # Official priority order (github before gitee), filtered to only the
        # remotes actually configured — never an unfiltered or reordered list.
        via_git.assert_called_once_with(
            self.root, ("github", "gitee"), self.state, keys, deadline=1e18
        )
        # Same commit/digest as protected state -> no redundant object fetch.
        fetch_objects.assert_not_called()
        apply.assert_called_once()
        self.assertIs(result, mock.sentinel.result)

    def test_update_transaction_failure_still_serves_known_good(self):
        with (
            mock.patch.object(launcher, "_managed_control_present", return_value=True),
            mock.patch.object(launcher, "_head_commit", return_value="1" * 40),
            mock.patch.object(launcher, "_finalize_journal", return_value=None),
            mock.patch.object(launcher, "_updates_enabled", return_value=True),
            mock.patch.object(
                launcher, "load_trusted_release_keys", return_value={"production": mock.sentinel.key}
            ),
            mock.patch.object(
                launcher,
                "_attempt_update",
                side_effect=launcher.update_transaction.UpdateTransactionError("busy"),
            ),
            mock.patch.object(launcher, "_serve_shim", return_value=9) as serve,
        ):
            result = launcher.launch(self.root)
        self.assertEqual(result, 9)
        serve.assert_called_once()

    def test_journal_finalization_precedes_network_and_serve(self):
        events = []

        @contextlib.contextmanager
        def gate(*_args, **_kwargs):
            events.append("gate-enter")
            yield
            events.append("gate-exit")

        def finalize(_root):
            events.append("finalize")

        def update(*_args, **_kwargs):
            events.append("update")

        with (
            mock.patch.object(launcher, "_managed_control_present", return_value=True),
            mock.patch.object(
                launcher.update_coordination, "startup_update_gate", side_effect=gate
            ),
            mock.patch.object(launcher, "_head_commit", return_value="1" * 40),
            mock.patch.object(launcher, "_finalize_journal", side_effect=finalize),
            mock.patch.object(launcher, "_updates_enabled", return_value=True),
            mock.patch.object(
                launcher, "load_trusted_release_keys", return_value={"test": mock.sentinel.key}
            ),
            mock.patch.object(launcher, "_attempt_update", side_effect=update),
            mock.patch.object(
                launcher, "_serve_shim", side_effect=lambda *_a, **_k: events.append("serve") or 0
            ),
        ):
            launcher.launch(self.root, stdin=io.StringIO(), stdout=io.StringIO())

        self.assertEqual(
            events, ["gate-enter", "finalize", "update", "gate-exit", "serve"]
        )

    def test_startup_update_gate_serializes_concurrent_launchers(self):
        first_entered = threading.Event()
        second_attempted = threading.Event()
        second_blocked = threading.Event()
        release_first = threading.Event()
        events = []
        native_try_lock = update_coordination._try_lock

        def observed_try_lock(fd):
            acquired = native_try_lock(fd)
            if threading.current_thread().name == "second-launcher" and not acquired:
                second_blocked.set()
            return acquired

        def first():
            with self.native_startup_update_gate(
                self.root, timeout_seconds=1.0
            ) as gate:
                events.append(("first-enter", gate.waited))
                first_entered.set()
                self.assertTrue(second_attempted.wait(1.0))
                self.assertTrue(second_blocked.wait(1.0))
                self.assertTrue(release_first.wait(1.0))
                events.append("first-exit")

        def second():
            self.assertTrue(first_entered.wait(1.0))
            second_attempted.set()
            with self.native_startup_update_gate(
                self.root, timeout_seconds=1.0
            ) as gate:
                events.append(("second-enter", gate.waited))
            events.append("second-exit")

        with (
            mock.patch.object(
                update_coordination.managed_install,
                "canonical_managed_root",
                return_value=self.root.resolve(),
            ),
            mock.patch.object(
                update_coordination, "_try_lock", side_effect=observed_try_lock
            ),
        ):
            first_thread = threading.Thread(target=first, name="first-launcher")
            second_thread = threading.Thread(target=second, name="second-launcher")
            first_thread.start()
            second_thread.start()
            self.assertTrue(second_attempted.wait(1.0))
            self.assertTrue(second_blocked.wait(1.0))
            release_first.set()
            first_thread.join(2.0)
            second_thread.join(2.0)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(
            events,
            [
                ("first-enter", False),
                "first-exit",
                ("second-enter", True),
                "second-exit",
            ],
        )

    def test_waiting_startup_follower_finalizes_journal_without_redundant_update(self):
        @contextlib.contextmanager
        def waited_gate(*_args, **_kwargs):
            yield types.SimpleNamespace(waited=True)

        with (
            mock.patch.object(launcher, "_managed_control_present", return_value=True),
            mock.patch.object(launcher, "_head_commit", return_value="1" * 40),
            mock.patch.object(
                launcher.update_coordination,
                "startup_update_gate",
                side_effect=waited_gate,
            ),
            mock.patch.object(launcher, "_finalize_journal", return_value=None) as finalize,
            mock.patch.object(launcher, "_attempt_update") as update,
            mock.patch.object(launcher, "_serve_shim", return_value=4) as serve,
        ):
            result = launcher.launch(self.root)

        self.assertEqual(result, 4)
        finalize.assert_called_once_with(self.root)
        update.assert_not_called()
        serve.assert_called_once()

    def test_serve_holds_lease_and_marks_the_actual_running_commit(self):
        events = []
        shim = mock.Mock()
        shim.__file__ = str(self.root / "installer" / "shim.py")
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        def serve(_forwarder, **kwargs):
            events.append("serve-ready")
            kwargs["on_ready"]()
            return 9

        shim.serve.side_effect = serve
        (self.root / "VERSION").write_text("0.2.3\n", encoding="utf-8")

        @contextlib.contextmanager
        def lease(_root, commit, **_kwargs):
            events.append(("lease", commit))
            yield mock.sentinel.lease

        def mark(_root, commit, version, **_kwargs):
            events.append(("mark", commit, version))

        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(
                launcher.update_coordination, "shim_session_lease", side_effect=lease
            ),
            mock.patch.object(
                launcher.update_transaction, "mark_running_release", side_effect=mark
            ),
            mock.patch.object(
                launcher, "_repair_codex_skill_routes", return_value=True
            ),
            mock.patch.object(
                launcher,
                "_retire_codex_global_routing",
                side_effect=lambda _root: events.append("retire-codex-global-routing"),
            ),
        ):
            result = launcher._serve_shim(
                self.root,
                shim=shim,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                client_host="codex",
            )

        self.assertEqual(result, 9)
        self.assertEqual(
            events,
            [
                ("lease", "2" * 40),
                "serve-ready",
                ("mark", "2" * 40, "0.2.3"),
                "retire-codex-global-routing",
            ],
        )
        shim.serve.assert_called_once()
        self.assertEqual(shim.serve.call_args.kwargs["client_host"], "codex")

    def test_managed_shim_falls_back_to_lite_on_activation_required(self):
        shim = mock.Mock()
        shim.ActivationRequiredError = type(
            "ActivationRequiredError", (launcher.ShellError,), {}
        )
        shim.Forwarder.from_config.side_effect = shim.ActivationRequiredError("activate")
        shim.LiteForwarder.return_value = mock.sentinel.lite
        shim.serve.return_value = 3
        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(launcher, "_version", return_value="0.2.3"),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(launcher.update_coordination, "shim_session_lease"),
        ):
            result = launcher._serve_shim(self.root, shim=shim)

        self.assertEqual(result, 3)
        shim.serve.assert_called_once()
        self.assertIs(shim.serve.call_args.args[0], mock.sentinel.lite)

    def test_dev_shim_falls_back_to_lite_on_activation_required(self):
        shim = mock.Mock()
        shim.ActivationRequiredError = type(
            "ActivationRequiredError", (launcher.ShellError,), {}
        )
        shim.Forwarder.from_config.side_effect = shim.ActivationRequiredError("activate")
        shim.LiteForwarder.return_value = mock.sentinel.lite
        shim.serve.return_value = 4
        with mock.patch.object(launcher, "_load_candidate_shim", return_value=shim):
            result = launcher._serve_dev_shim(self.root)

        self.assertEqual(result, 4)
        self.assertIs(shim.serve.call_args.args[0], mock.sentinel.lite)

    def test_legacy_shim_falls_back_to_lite_on_activation_required(self):
        shim = mock.Mock()
        shim.ActivationRequiredError = type(
            "ActivationRequiredError", (launcher.ShellError,), {}
        )
        shim.Forwarder.from_config.side_effect = shim.ActivationRequiredError("activate")
        shim.LiteForwarder.return_value = mock.sentinel.lite
        shim.serve.return_value = 5
        with (
            mock.patch.object(
                launcher, "_head_commit", side_effect=launcher.ShellError("no git")
            ),
            mock.patch.object(launcher, "_managed_control_present", return_value=False),
            mock.patch.object(launcher, "_load_candidate_shim", return_value=shim),
            mock.patch.object(launcher.update_coordination, "shim_session_lease"),
        ):
            result = launcher._serve_legacy_shim(self.root)

        self.assertEqual(result, 5)
        self.assertIs(shim.serve.call_args.args[0], mock.sentinel.lite)

    def test_pre_lite_shim_preserves_original_shell_error(self):
        shim = types.SimpleNamespace(
            Forwarder=types.SimpleNamespace(
                from_config=mock.Mock(side_effect=launcher.ShellError("activate"))
            )
        )

        with self.assertRaisesRegex(launcher.ShellError, "activate"):
            launcher._forwarder_from_config(shim)

    def test_non_activation_shell_error_never_falls_back_to_lite(self):
        activation_error = type("ActivationRequiredError", (launcher.ShellError,), {})
        shim = types.SimpleNamespace(
            ActivationRequiredError=activation_error,
            LiteForwarder=mock.Mock(),
            Forwarder=types.SimpleNamespace(
                from_config=mock.Mock(side_effect=launcher.ShellError("HTTP 401"))
            ),
        )

        with self.assertRaisesRegex(launcher.ShellError, "401"):
            launcher._forwarder_from_config(shim)
        shim.LiteForwarder.assert_not_called()

    def test_serve_new_launcher_accepts_pre_client_host_shim(self):
        calls = []

        def legacy_serve(_forwarder, *, stdin=None, stdout=None, on_ready=None):
            calls.append((stdin, stdout))
            on_ready()
            return 6

        shim = types.SimpleNamespace(
            Forwarder=types.SimpleNamespace(
                from_config=lambda: mock.sentinel.forwarder
            ),
            serve=legacy_serve,
        )
        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(launcher, "_version", return_value="0.2.3"),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(launcher.update_coordination, "shim_session_lease"),
            mock.patch.object(launcher.update_transaction, "mark_running_release"),
        ):
            result = launcher._serve_shim(
                self.root, shim=shim, client_host="codex"
            )

        self.assertEqual(result, 6)
        self.assertEqual(len(calls), 1)

    def test_running_confirmation_lock_contention_does_not_abort_serving(self):
        shim = mock.Mock()
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        shim.serve.side_effect = lambda _forwarder, **kwargs: kwargs["on_ready"]() or 5
        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(launcher, "_version", return_value="0.2.3"),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(launcher.update_coordination, "shim_session_lease"),
            mock.patch.object(
                launcher.update_transaction,
                "mark_running_release",
                side_effect=launcher.update_transaction.UpdateTransactionError("busy"),
            ),
            mock.patch.object(launcher, "_retire_codex_global_routing") as retire,
        ):
            result = launcher._serve_shim(self.root, shim=shim)
        self.assertEqual(result, 5)
        retire.assert_not_called()

    def test_legacy_routing_cleanup_failure_does_not_abort_mcp(self):
        with mock.patch(
            "installer.codex_routing.retire_routing",
            side_effect=launcher.ShellError("concurrent edit"),
        ):
            launcher._retire_codex_global_routing(self.root)

    def test_explicit_non_codex_host_never_runs_global_routing_cleanup(self):
        shim = mock.Mock()
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        shim.serve.side_effect = lambda _forwarder, **kwargs: kwargs["on_ready"]() or 0
        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(launcher, "_version", return_value="0.2.3"),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(launcher.update_coordination, "shim_session_lease"),
            mock.patch.object(launcher.update_transaction, "mark_running_release"),
            mock.patch.object(launcher, "_repair_codex_skill_routes", return_value=True),
            mock.patch.object(launcher, "_retire_codex_global_routing") as retire,
        ):
            launcher._serve_shim(self.root, shim=shim, client_host="claude-code")

        retire.assert_not_called()

    def test_missing_replacement_skill_never_runs_global_routing_cleanup(self):
        shim = mock.Mock()
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        shim.serve.side_effect = lambda _forwarder, **kwargs: kwargs["on_ready"]() or 0
        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(launcher, "_version", return_value="0.2.3"),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(launcher.update_coordination, "shim_session_lease"),
            mock.patch.object(launcher.update_transaction, "mark_running_release"),
            mock.patch.object(launcher, "_repair_codex_skill_routes", return_value=False),
            mock.patch.object(launcher, "_retire_codex_global_routing") as retire,
        ):
            launcher._serve_shim(self.root, shim=shim, client_host="codex")

        retire.assert_not_called()

    def test_partial_activation_refuses_before_import_or_running_confirmation(self):
        shim = mock.Mock()
        shim.__file__ = str(self.root / "installer" / "shim.py")
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        (self.root / "VERSION").write_text("0.2.3\n", encoding="utf-8")

        @contextlib.contextmanager
        def lease(*_args, **_kwargs):
            yield mock.sentinel.lease

        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(
                launcher,
                "_verify_managed_candidate",
                side_effect=launcher.ShellError("protocol is incomplete"),
            ),
            mock.patch.object(
                launcher.update_coordination, "shim_session_lease", side_effect=lease
            ),
            mock.patch.object(
                launcher.update_transaction, "mark_running_release"
            ) as mark,
        ):
            with self.assertRaisesRegex(launcher.ShellError, "protocol"):
                launcher._serve_shim(
                    self.root, shim=shim, stdin=io.StringIO(), stdout=io.StringIO()
                )

        mark.assert_not_called()
        shim.serve.assert_not_called()

    def test_repair_required_finalization_suppresses_new_update_attempt(self):
        repair = update_transaction.UpdateResult(
            "repair_required", "1" * 40, "2" * 40, "a" * 32, "recovery", "repair"
        )
        with (
            mock.patch.object(launcher, "_managed_control_present", return_value=True),
            mock.patch.object(launcher, "_head_commit", return_value="1" * 40),
            mock.patch.object(launcher, "_finalize_journal", return_value=repair),
            mock.patch.object(launcher, "_attempt_update") as update,
            mock.patch.object(launcher, "_serve_shim", return_value=0),
        ):
            launcher.launch(self.root, stdin=io.StringIO(), stdout=io.StringIO())
        update.assert_not_called()

    def test_head_change_hands_stdio_to_a_fresh_interpreter(self):
        with (
            mock.patch.object(launcher, "_managed_control_present", return_value=True),
            mock.patch.object(
                launcher,
                "_head_commit",
                side_effect=["1" * 40, "1" * 40, "2" * 40],
            ),
            mock.patch.object(launcher, "_finalize_journal", return_value=None),
            mock.patch.object(launcher, "_updates_enabled", return_value=True),
            mock.patch.object(
                launcher, "load_trusted_release_keys", return_value={"production": mock.sentinel.key}
            ),
            mock.patch.object(launcher, "_attempt_update"),
            mock.patch.object(launcher, "_handoff_to_fresh_launcher", return_value=6) as handoff,
            mock.patch.object(launcher, "_serve_shim") as serve,
        ):
            result = launcher.launch(self.root)

        self.assertEqual(result, 6)
        handoff.assert_called_once_with(self.root)
        serve.assert_not_called()

    def test_handoff_child_inherits_protocol_stdio(self):
        completed = mock.Mock(returncode=3)
        with mock.patch.object(launcher.subprocess, "run", return_value=completed) as run:
            self.assertEqual(
                launcher._handoff_to_fresh_launcher(self.root, client_host="codex"), 3
            )
        args, kwargs = run.call_args
        self.assertEqual(
            args[0][-3:], ["--managed-root", str(self.root), "--serve-only"]
        )
        self.assertEqual(
            kwargs["env"][launcher.CLIENT_HOST_ENV], "codex"
        )
        self.assertNotIn("stdin", kwargs)
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)

    def test_handoff_without_host_preserves_legacy_argv_and_environment(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(launcher.subprocess, "run", return_value=completed) as run:
            self.assertEqual(launcher._handoff_to_fresh_launcher(self.root), 0)

        args, kwargs = run.call_args
        self.assertEqual(
            args[0][-3:], ["--managed-root", str(self.root), "--serve-only"]
        )
        self.assertNotIn("env", kwargs)

    def test_shim_import_occurs_only_after_session_lease_admission(self):
        events = []
        shim = mock.Mock()
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        shim.serve.side_effect = lambda _forwarder, **kwargs: kwargs["on_ready"]() or 0
        (self.root / "VERSION").write_text("0.2.3\n", encoding="utf-8")

        @contextlib.contextmanager
        def lease(*_args, **_kwargs):
            events.append("lease")
            yield

        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(launcher, "_version", return_value="0.2.3"),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(launcher.update_transaction, "mark_running_release"),
            mock.patch.object(
                launcher.update_coordination, "shim_session_lease", side_effect=lease
            ),
            mock.patch.object(
                launcher, "_repair_codex_skill_routes",
                side_effect=lambda _root: events.append("repair-codex-skills") or False,
            ),
            mock.patch.object(
                launcher, "_load_candidate_shim",
                side_effect=lambda _root: events.append("import") or shim,
            ),
            mock.patch.object(launcher, "_updates_enabled", return_value=False),
        ):
            launcher._serve_shim(self.root)

        self.assertEqual(events, ["lease", "repair-codex-skills", "import"])

    def test_codex_skill_route_repair_failure_does_not_abort_mcp(self):
        with mock.patch(
            "installer.install.repair_codex_skill_routes",
            side_effect=launcher.ShellError("foreign route"),
        ):
            self.assertFalse(launcher._repair_codex_skill_routes(self.root))

    def test_unexpected_codex_skill_route_repair_failure_does_not_abort_mcp(self):
        with mock.patch(
            "installer.install.repair_codex_skill_routes",
            side_effect=RuntimeError("unexpected repair failure"),
        ):
            self.assertFalse(launcher._repair_codex_skill_routes(self.root))

    def test_codex_skill_route_refusal_does_not_block_managed_serving(self):
        shim = mock.Mock()
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        shim.serve.return_value = 12
        with (
            mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
            mock.patch.object(launcher, "_version", return_value="0.2.23"),
            mock.patch.object(launcher, "_verify_managed_candidate"),
            mock.patch.object(launcher.update_coordination, "shim_session_lease"),
            mock.patch(
                "installer.install.repair_codex_skill_routes",
                side_effect=launcher.ShellError("foreign route"),
            ),
        ):
            result = launcher._serve_shim(self.root, shim=shim)

        self.assertEqual(result, 12)
        shim.serve.assert_called_once()

    def test_cursor_start_does_not_trigger_explicit_setup_skill_routing(self):
        shim = mock.Mock()
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        shim.serve.return_value = 0
        cursor_config = self.root / "cursor-home" / "mcp.json"
        cursor_skills = self.root / "cursor-home" / "skills"
        with mock.patch.dict(
            launcher.os.environ,
            {
                "CURSOR_CONFIG": str(cursor_config),
                "CURSOR_SKILLS_DIR": str(cursor_skills),
            },
            clear=False,
        ):
            with (
                mock.patch.object(launcher, "_head_commit", return_value="2" * 40),
                mock.patch.object(launcher, "_version", return_value="0.2.23"),
                mock.patch.object(launcher, "_verify_managed_candidate"),
                mock.patch.object(
                    launcher.update_coordination, "shim_session_lease"
                ),
                mock.patch(
                    "installer.config.codex_skills_in_use", return_value=False
                ),
            ):
                result = launcher._serve_shim(
                    self.root, shim=shim, client_host="cursor"
                )

        self.assertEqual(result, 0)
        self.assertFalse(cursor_config.exists())
        self.assertFalse(cursor_skills.exists())
        shim.serve.assert_called_once()
        self.assertEqual(shim.serve.call_args.kwargs["client_host"], "cursor")

    def test_legacy_install_serves_without_network_or_managed_mutation(self):
        with (
            mock.patch.object(launcher, "_managed_control_present", return_value=False),
            mock.patch.object(launcher, "_finalize_journal") as finalize,
            mock.patch.object(launcher, "_attempt_update") as update,
            mock.patch.object(
                launcher, "_load_candidate_shim", return_value=mock.sentinel.shim
            ) as load,
            mock.patch.object(launcher, "_serve_legacy_shim", return_value=4) as serve,
        ):
            result = launcher.launch(
                self.root, stdin=io.StringIO(), stdout=io.StringIO()
            )

        self.assertEqual(result, 4)
        finalize.assert_not_called()
        update.assert_not_called()
        load.assert_not_called()
        serve.assert_called_once()

    def test_non_git_legacy_copy_serves_under_a_sentinel_lease(self):
        shim = mock.Mock()
        shim.Forwarder.from_config.return_value = mock.sentinel.forwarder
        shim.serve.return_value = 4
        leases = []

        @contextlib.contextmanager
        def lease(_root, commit):
            leases.append(commit)
            yield

        with (
            mock.patch.object(launcher, "_head_commit", side_effect=launcher.ShellError("no git")),
            mock.patch.object(launcher, "_managed_control_present", return_value=False),
            mock.patch.object(launcher, "_load_candidate_shim", return_value=shim),
            mock.patch.object(launcher.update_coordination, "shim_session_lease", side_effect=lease),
        ):
            result = launcher._serve_legacy_shim(self.root)
        self.assertEqual(result, 4)
        self.assertEqual(leases, ["0" * 40])

    def test_legacy_admission_accepts_pre_client_host_shim(self):
        def legacy_serve(_forwarder, *, stdin=None, stdout=None):
            return 4

        shim = types.SimpleNamespace(
            Forwarder=types.SimpleNamespace(
                from_config=lambda: mock.sentinel.forwarder
            ),
            serve=legacy_serve,
        )
        with (
            mock.patch.object(
                launcher, "_head_commit", side_effect=launcher.ShellError("no git")
            ),
            mock.patch.object(launcher, "_managed_control_present", return_value=False),
            mock.patch.object(launcher, "_load_candidate_shim", return_value=shim),
            mock.patch.object(launcher.update_coordination, "shim_session_lease"),
        ):
            result = launcher._serve_legacy_shim(
                self.root, client_host="codex"
            )

        self.assertEqual(result, 4)

    def test_out_of_root_shim_is_rejected_before_module_execution(self):
        spec = mock.Mock(origin=str(self.root.parent / "attacker" / "shim.py"))
        with (
            mock.patch.object(launcher.importlib.util, "find_spec", return_value=spec),
            mock.patch.object(launcher.importlib, "import_module") as execute,
        ):
            with self.assertRaisesRegex(launcher.ShellError, "outside"):
                launcher._load_candidate_shim(self.root)
        execute.assert_not_called()

    def test_hidden_serve_only_mode_still_enforces_the_fixed_managed_root(self):
        with (
            mock.patch.object(
                launcher.managed_install,
                "_require_fixed_managed_root",
                side_effect=launcher.ShellError("not fixed"),
            ),
            mock.patch.object(launcher, "_serve_shim") as serve,
        ):
            self.assertEqual(
                launcher.main(["--managed-root", str(self.root), "--serve-only"]),
                1,
            )
        serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
