"""Pacing of the audit STATUS-WAIT polls (`client.runner` helpers + the two pollers that use them).

These lock the request side only. What a poll DISPLAYS is covered by test_stopper_panel.py; what a
poll MERGES is `merge_poll_view` there too. Here the single question is: when is the next status
request allowed to leave, and when must none leave at all.

Background: both pollers used to run at a hard 1 Hz with no pacing input from the hub, no backoff
when the hub was down, and no stop on an authentication failure — so a revoked token produced one
request per second, per run, forever. The hub now also serves a lighter `/v1/audits/{id}/status`
route, which the CLI wait loop uses; the stop panel stays on the detail view because it renders
per-voice state the lighter route does not carry (see `runner.audit_status_path` /
`runner.audit_detail_path`).

Run:  python -m pytest installer/tests/test_audit_poll_pacing.py
"""

from __future__ import annotations

import contextlib
import math
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from client import runner
from client.stopper import panel


def _err(status_code=None, error_code=None):
    exc = runner.AuditError("boom")
    if status_code is not None:
        exc.status_code = status_code
    if error_code is not None:
        exc.error_code = error_code
    return exc


class PollDelayTests(unittest.TestCase):
    """R1/R2: the server's suggestion wins, but only inside a bounded range."""

    ZERO = staticmethod(lambda: 0.0)
    ONE = staticmethod(lambda: 1.0)
    HALF = staticmethod(lambda: 0.5)

    def test_server_suggestion_wins_over_the_client_base(self):
        self.assertAlmostEqual(runner.poll_delay_s(4000, base_s=1.0, rng=self.ZERO), 4.0)
        self.assertAlmostEqual(runner.poll_delay_s(4000, base_s=1.0, rng=self.ONE), 4.8)

    def test_missing_suggestion_uses_the_client_base(self):
        self.assertAlmostEqual(
            runner.poll_delay_s(None, base_s=2.0, rng=self.ZERO), 2.0
        )

    def test_unusable_poll_after_ms_values_fall_back_to_base(self):
        for value in (None, True, False, "5", -1, float("nan"), float("inf"), {}, [], object()):
            with self.subTest(value=repr(value)):
                self.assertAlmostEqual(
                    runner.poll_delay_s(value, base_s=3.0, rng=self.ZERO), 3.0
                )

    def test_a_tiny_suggestion_is_floored_so_it_cannot_become_a_storm(self):
        # The hub may slow us down; it may never speed us up past the caller's own base. A
        # `poll_after_ms: 1` against a 1 Hz poller stays 1 Hz — it does not become 4 Hz.
        for value in (0, 1, 10, 249, 999):
            with self.subTest(value=value):
                self.assertAlmostEqual(
                    runner.poll_delay_s(value, base_s=1.0, rng=self.ZERO), 1.0
                )

    def test_the_hub_cannot_overrule_a_user_who_asked_to_be_gentle(self):
        # `audit submit --wait --poll-s 60` is an instruction, not a hint. A hub answering
        # `poll_after_ms: 250` must not turn it into four requests a second.
        self.assertAlmostEqual(runner.poll_delay_s(250, base_s=60.0, rng=self.ZERO), 60.0)

    def test_the_absolute_floor_applies_when_the_callers_base_is_below_it(self):
        self.assertAlmostEqual(
            runner.poll_delay_s(1, base_s=0.001, rng=self.ZERO), runner.POLL_MIN_INTERVAL_S
        )

    def test_an_absurd_suggestion_is_capped_so_a_row_cannot_freeze(self):
        self.assertAlmostEqual(
            runner.poll_delay_s(3_600_000, rng=self.ZERO), runner.POLL_MAX_INTERVAL_S
        )

    def test_the_cap_holds_after_jitter_not_only_before_it(self):
        # A ceiling that the jitter is then allowed to exceed is not a ceiling.
        self.assertLessEqual(
            runner.poll_delay_s(3_600_000, rng=self.ONE), runner.POLL_MAX_INTERVAL_S
        )

    def test_an_integer_too_large_for_a_float_cannot_take_the_poller_down(self):
        # JSON ints are unbounded; `float()` RAISES OverflowError before `math.isfinite` can
        # reject them. Unguarded this threw inside the panel's locked success block and left
        # the run wedged in `_polling` forever — a remotely triggerable poll lockout.
        for value in (10**400, -(10**400), 2**2000):
            with self.subTest(value="10**%d-ish" % len(str(abs(value)))):
                self.assertAlmostEqual(
                    runner.poll_delay_s(value, base_s=2.0, rng=self.ZERO), 2.0
                )
        self.assertAlmostEqual(
            runner.poll_backoff_delay_s(1, base_s=10**400, rng=self.ZERO),
            runner.POLL_BASE_INTERVAL_S,
        )

    def test_a_users_gentler_base_is_never_capped_down(self):
        # `audit submit --wait --poll-s 60` is a deliberate request to be gentle. The ceiling
        # exists to stop a HUB from freezing a client, not to stop a user from being polite.
        self.assertAlmostEqual(runner.poll_delay_s(None, base_s=60.0, rng=self.ZERO), 60.0)

    def test_a_users_reckless_base_is_still_floored(self):
        self.assertAlmostEqual(
            runner.poll_delay_s(None, base_s=0.0001, rng=self.ZERO), runner.POLL_MIN_INTERVAL_S
        )

    def test_an_unusable_caller_base_falls_back_to_the_module_default(self):
        # `base_s` is not guaranteed to be a usable number: it arrives from `--poll-s`, from the
        # panel's own constant, and (via keyword) from any future caller. An unusable one must not
        # reach the arithmetic below as None/NaN and turn every subsequent sleep into nonsense —
        # the wait loop is the only thing reporting the run's outcome to the user.
        for base in (None, float("nan"), float("inf"), "1.0", -5.0, True, 10 ** 400):
            with self.subTest(base=base):
                self.assertAlmostEqual(
                    runner.poll_delay_s(None, base_s=base, rng=self.ZERO),
                    runner.POLL_BASE_INTERVAL_S,
                )

    def test_jitter_stays_inside_the_declared_fraction(self):
        import random

        for _ in range(200):
            delay = runner.poll_delay_s(1000, rng=random.random)
            self.assertGreaterEqual(delay, 1.0)
            self.assertLessEqual(delay, 1.0 * (1.0 + runner.POLL_JITTER_FRACTION))

    def test_two_pollers_do_not_get_the_same_delay(self):
        import random

        delays = {runner.poll_delay_s(1000, rng=random.random) for _ in range(50)}
        self.assertGreater(len(delays), 1, "jitter must spread clients, not align them")

    def test_a_broken_rng_cannot_break_pacing(self):
        def exploding():
            raise RuntimeError("no entropy")

        for bad in (exploding, lambda: "x", lambda: True, lambda: -1.0, lambda: 2.0, object()):
            with self.subTest(rng=repr(bad)):
                delay = runner.poll_delay_s(1000, rng=bad)
                self.assertGreaterEqual(delay, 1.0)
                self.assertLessEqual(delay, 1.0 * (1.0 + runner.POLL_JITTER_FRACTION))

    def test_the_default_rng_needs_no_injection(self):
        delay = runner.poll_delay_s(1000)
        self.assertGreaterEqual(delay, 1.0)
        self.assertLessEqual(delay, 1.0 * (1.0 + runner.POLL_JITTER_FRACTION))


class PollBackoffTests(unittest.TestCase):
    """R3: consecutive failures grow the delay, under a fixed ceiling."""

    HALF = staticmethod(lambda: 0.5)

    def test_backoff_grows_with_each_consecutive_failure(self):
        delays = [runner.poll_backoff_delay_s(n, rng=self.HALF) for n in range(1, 6)]
        self.assertEqual(delays, sorted(delays))
        self.assertGreater(delays[-1], delays[0])
        for earlier, later in zip(delays, delays[1:]):
            self.assertGreater(later, earlier)

    def test_the_first_failure_waits_at_least_the_base(self):
        self.assertGreaterEqual(runner.poll_backoff_delay_s(1, base_s=1.0, rng=lambda: 0.0), 1.0)

    def test_backoff_is_capped(self):
        # The ceiling bounds the value actually slept, jitter included.
        for n in (8, 20, 500, 10**6):
            with self.subTest(failures=n):
                self.assertLessEqual(
                    runner.poll_backoff_delay_s(n, rng=lambda: 1.0),
                    runner.POLL_BACKOFF_CEILING_S,
                )

    def test_failing_never_makes_a_gentle_caller_poll_faster(self):
        # base 60 s vs a 30 s ceiling: capping to the ceiling would SPEED UP a user who asked
        # to be gentle, turning a failure into more load than success.
        for n in (1, 3, 20):
            with self.subTest(failures=n):
                self.assertGreaterEqual(
                    runner.poll_backoff_delay_s(n, base_s=60.0, rng=lambda: 0.0), 60.0
                )

    def test_a_nonsense_failure_count_is_treated_as_the_first_failure(self):
        expected = runner.poll_backoff_delay_s(1, rng=lambda: 0.0)
        for value in (0, -3, True, None, "2", 1.5):
            with self.subTest(value=repr(value)):
                self.assertAlmostEqual(
                    runner.poll_backoff_delay_s(value, rng=lambda: 0.0), expected
                )


class PollFailureClassTests(unittest.TestCase):
    """R4: which failures are worth repeating and which are the hub's final answer."""

    def test_transport_and_server_side_failures_are_retryable(self):
        for code in (None, 429, 500, 502, 503, 504, 599):
            with self.subTest(code=code):
                self.assertTrue(runner.poll_failure_is_transient(code))

    def test_client_side_failures_are_not_retryable(self):
        for code in (400, 401, 403, 404, 409, 410, 422):
            with self.subTest(code=code):
                self.assertFalse(runner.poll_failure_is_transient(code))

    def test_auth_codes_are_the_ones_that_stop_a_poller(self):
        self.assertEqual(runner.POLL_AUTH_STATUS_CODES, frozenset({401, 403}))
        self.assertGreaterEqual(runner.POLL_AUTH_FAILURE_LIMIT, 2)


class AuditStatusPathTests(unittest.TestCase):
    """Which poller addresses which route, and why the split is not a free choice.

    `/v1/audits/{id}/status` carries `status` and `poll_after_ms` and nothing else the client
    renders. `/v1/audits/{id}` carries `auditors`, `title`, `profile` and `debug_authorized`
    but NOT `poll_after_ms`. So a caller that only prints a status line belongs on the former
    (it is the only route the hub can pace at all), and a caller that draws per-voice rows
    belongs on the latter. Swapping either direction is a silent regression, so both are
    pinned here.
    """

    def test_status_path_is_the_lighter_route(self):
        self.assertEqual(runner.audit_status_path("aud_x"), "/v1/audits/aud_x/status")

    def test_detail_path_is_the_full_run_view(self):
        self.assertEqual(runner.audit_detail_path("aud_x"), "/v1/audits/aud_x")

    def test_the_cli_wait_loop_polls_the_lighter_route(self):
        source = (ROOT / "client" / "runner.py").read_text(encoding="utf-8")
        loop = source.split("def wait_and_print_result(", 1)[1]
        self.assertIn('request_json("GET", audit_status_path(run_id))', loop)

    def test_the_panel_polls_the_detail_route_because_it_renders_per_voice_state(self):
        source = (ROOT / "client" / "stopper" / "panel.py").read_text(encoding="utf-8")
        self.assertIn('runner.request_json("GET", runner.audit_detail_path(run_id))', source)
        # Not merely "the detail call exists" — the panel must name the status route NOWHERE.
        self.assertNotIn("audit_status_path", source)

    def test_the_macos_panel_polls_the_detail_route_too(self):
        source = (ROOT / "desktop" / "macos" / "DecisionEngineStopper.swift").read_text(
            encoding="utf-8"
        )
        self.assertIn('URL(string: serverURL + "/v1/audits/" + runID)', source)
        self.assertNotIn('"/v1/audits/" + runID + "/status"', source)

    def test_the_detail_reads_do_not_route_through_the_status_seam(self):
        source = (ROOT / "client" / "runner.py").read_text(encoding="utf-8")
        self.assertIn('"/v1/audits/%s/result" % run_id', source)
        self.assertIn('"/v1/audits/%s/events" % args.run_id', source)

    def test_the_status_seam_has_exactly_one_caller(self):
        # A named seam invites adoption, and adopting it is how a caller that renders `auditors`
        # or `title` silently loses them. Pinning the file-level allow-list — rather than only
        # asserting that panel.py abstains — is what makes a THIRD caller fail here instead of
        # in front of a user watching a blank row.
        namers = sorted(
            path.relative_to(ROOT).as_posix()
            for path in ROOT.joinpath("client").rglob("*.py")
            if "audit_status_path" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(namers, ["client/runner.py"])


class _ActiveRunsHomeMixin:
    """A throwaway active-runs registry on disk, seeded with one hub row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "de"
        self.env = mock.patch.dict(
            os.environ,
            {
                "DE_CONFIG_PATH": str(root / "config.json"),
                "DE_ACTIVE_RUNS": str(root / "active-runs.json"),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("DE_ACTIVE_RUN", None)

    def _row(self):
        return runner.load_active_runs_registry()["runs"]["r1"]

    def _seed(self, **extra):
        runner.save_active_run(
            {
                "run_id": "r1",
                "status": "queued",
                "title": "Ledger review",
                "caller": "cli",
                "profile": "deep",
                "auditors": [{"status": "running"}, {"status": "queued"}],
                **extra,
            }
        )


class StatusPollPersistenceTests(_ActiveRunsHomeMixin, unittest.TestCase):
    """A lighter poll must not thin the row the stop panel is rendering.

    `active_run_payload` is a whitelist with hard defaults and `_save_active_run_locked`
    REPLACES the entry, so persisting a `/status` view through `save_active_run` would blank
    `title`, `profile`, `caller`, `mode` and `auditors` for the whole wait — the panel would
    show an untitled, tier-less, voice-less row next to a perfectly healthy run.
    """

    def test_a_status_view_keeps_the_display_metadata_it_does_not_carry(self):
        self._seed()
        runner.save_active_run_status("r1", {"run_id": "r1", "status": "running"})
        row = self._row()
        self.assertEqual(row["status"], "running")
        for field, expected in (
            ("title", "Ledger review"),
            ("caller", "cli"),
            ("profile", "deep"),
            ("mode", "deep"),
        ):
            with self.subTest(field=field):
                self.assertEqual(row[field], expected)
        self.assertEqual(len(row["auditors"]), 2)

    def test_a_terminal_status_view_still_finishes_the_row(self):
        self._seed()
        runner.save_active_run_status("r1", {"run_id": "r1", "status": "completed"})
        row = self._row()
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row.get("hidden_after"))
        self.assertEqual(row["title"], "Ledger review")

    def test_a_poll_never_creates_a_row_the_submit_path_did_not_write(self):
        # `de wait <id>` on a run submitted elsewhere has no display metadata to preserve, and
        # a reaped or expired run must not be resurrected by its own in-flight poll.
        runner.save_active_run_status("r1", {"run_id": "r1", "status": "running"})
        self.assertEqual(runner.load_active_runs_registry()["runs"], {})

    def test_a_poll_cannot_write_back_a_row_that_expires_mid_save(self):
        # `_save_active_run_locked` prunes AGAIN on a fresh clock reading, after this helper has
        # already pruned and found the row alive. A row that expires between the two readings is
        # dropped by the inner prune and then written straight back by the unconditional
        # assignment — the helper's docstring promises `existing_only` semantics, so pin them
        # here rather than leaving the guarantee to the narrowness of the window.
        self._seed(hidden_after=150.0)
        real_prune = runner.prune_active_runs
        readings = iter((100.0, 200.0))

        def pinned_prune(registry, now=None):
            return real_prune(registry, next(readings, 200.0))

        with mock.patch.object(runner, "prune_active_runs", side_effect=pinned_prune):
            runner.save_active_run_status("r1", {"run_id": "r1", "status": "running"})
        self.assertEqual(
            self._row()["status"], "queued",
            "an expired row must not be revived by the status poll still in flight over it",
        )

    def test_timing_fields_are_only_taken_when_they_are_real_numbers(self):
        self._seed()
        runner.save_active_run_status(
            "r1",
            {"run_id": "r1", "status": "running", "started_at": "not-a-number",
             "completed_at": float("nan")},
        )
        row = self._row()
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["completed_at"])

    def test_real_epoch_timestamps_from_a_status_view_are_taken(self):
        # The rejection case above was pinned; the ACCEPT case was not, so the guard could have
        # been inverted and every test still passed while the panel's elapsed clock stayed empty
        # for the whole wait. A live hub sends these as floats — see the status-route probe.
        self._seed()
        runner.save_active_run_status(
            "r1",
            {"run_id": "r1", "status": "completed",
             "started_at": 1789552818.5, "completed_at": 1789552900.25},
        )
        row = self._row()
        self.assertEqual(row["started_at"], 1789552818.5)
        self.assertEqual(row["completed_at"], 1789552900.25)

    def test_a_boolean_is_not_a_timestamp(self):
        # The guard is `type(value) in (int, float)`, NOT isinstance: bool subclasses int, so an
        # isinstance check would silently accept `started_at: true` and render 1970 as the start.
        self._seed()
        runner.save_active_run_status(
            "r1", {"run_id": "r1", "status": "running", "started_at": True}
        )
        self.assertIsNone(self._row()["started_at"])


class HostedCompletionSyncTests(_ActiveRunsHomeMixin, unittest.TestCase):
    """`sync_hosted_run_completion` — the terminal read that ends a row the wait loop was pacing.

    This is load-bearing for the merge-not-replace design, not an incidental helper:
    `save_active_run_status` deliberately stops refreshing `auditors` for the length of the wait
    (the status route does not carry them), and the docstring justifies that by pointing here —
    the terminal read is what puts the final per-voice state back. Nothing exercised it, so the
    justification rested on an unverified claim. Its only caller is installer/shim.py:2620, on an
    audit follow-up MCP response.
    """

    def test_a_terminal_view_finishes_the_row_and_refreshes_the_voices(self):
        self._seed()
        runner.sync_hosted_run_completion(
            "r1",
            {"run_id": "r1", "status": "completed",
             "started_at": 1789552818.0, "completed_at": 1789552900.0,
             "auditors": [{"status": "completed"}, {"status": "failed"}]},
        )
        row = self._row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["started_at"], 1789552818.0)
        self.assertEqual(row["completed_at"], 1789552900.0)
        self.assertEqual([a["status"] for a in row["auditors"]], ["completed", "failed"])
        self.assertEqual(row["title"], "Ledger review", "submit-time metadata must survive")
        self.assertIsNotNone(row.get("hidden_after"))

    def test_a_non_terminal_view_is_ignored(self):
        # Completion sync is not a general-purpose writer: a `running` view here would finish the
        # row (stamping hidden_after) while the run is still going, hiding a live run's stop button.
        self._seed()
        runner.sync_hosted_run_completion("r1", {"run_id": "r1", "status": "running"})
        row = self._row()
        self.assertEqual(row["status"], "queued")
        self.assertIsNone(row.get("hidden_after"))

    def test_a_local_advisory_view_is_ignored(self):
        self._seed()
        runner.sync_hosted_run_completion(
            "r1", {"run_id": "r1", "status": "completed", "local": True}
        )
        self.assertEqual(self._row()["status"], "queued")

    def test_a_local_advisory_row_is_never_completed_by_a_hub_view(self):
        # A local advisory run has no hub run behind it. Letting a same-id hub view finish it
        # would relabel an offline single-model read as a completed committee audit.
        self._seed(local=True, run_id="r1")
        runner.sync_hosted_run_completion("r1", {"run_id": "r1", "status": "completed"})
        self.assertEqual(self._row()["status"], "queued")

    def test_it_never_creates_a_row(self):
        runner.sync_hosted_run_completion("r1", {"run_id": "r1", "status": "completed"})
        self.assertEqual(runner.load_active_runs_registry()["runs"], {})

    def test_unusable_timestamps_are_left_alone(self):
        self._seed()
        runner.sync_hosted_run_completion(
            "r1",
            {"run_id": "r1", "status": "completed",
             "started_at": float("nan"), "completed_at": "2026-09-16T00:00:00Z"},
        )
        row = self._row()
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["completed_at"])

    def test_a_hostile_auditors_payload_cannot_bloat_or_poison_the_row(self):
        # The view comes off an MCP result envelope that may carry the entire review. Only the
        # per-voice STATUS is display state; everything else is dropped, and the list is bounded.
        self._seed()
        runner.sync_hosted_run_completion(
            "r1",
            {"run_id": "r1", "status": "completed",
             "auditors": [{"status": "completed", "markdown": "x" * 4096}] * 100
                         + ["not-a-dict", {"no_status": 1}, {"status": "bogus"}]},
        )
        row = self._row()
        self.assertEqual(len(row["auditors"]), 64)
        self.assertEqual({tuple(a) for a in row["auditors"]}, {("status",)})

    def test_a_view_without_auditors_keeps_the_ones_the_row_already_had(self):
        self._seed()
        runner.sync_hosted_run_completion("r1", {"run_id": "r1", "status": "completed"})
        self.assertEqual(len(self._row()["auditors"]), 2)


class _PanelAppMixin:
    def _app(self, status="running", **extra):
        app = object.__new__(panel.StopPanelApp)
        app.runs = {"r1": {"run_id": "r1", "status": status, "profile": "standard", **extra}}
        app._cancel_inflight = set()
        app._server_verified = set()
        app._state_epochs = {}
        app._polling = set()
        app._retired = set()
        app._not_found_polls = {}
        app._next_poll_at = {}
        app._poll_failures = {}
        app._auth_failures = {}
        app._lock = threading.Lock()
        app._frozen = {}
        app._auditor_frozen = {}
        app._no_runs_since = None
        app._render = mock.Mock()
        app.root = mock.Mock()
        return app

    @contextlib.contextmanager
    def _clock(self, now):
        """Pin BOTH clocks: display values read `time.time`, poll scheduling reads `monotonic`."""
        with mock.patch.object(panel.time, "time", return_value=now), \
             mock.patch.object(panel.time, "monotonic", return_value=now):
            yield

    def _tick(self, app, now):
        """Run one panel tick with disk seeding and rendering stubbed out; return polled run ids."""
        started = []

        class _Thread:
            def __init__(self, target=None, args=(), daemon=False, **kwargs):
                self._args = args

            def start(self):
                started.append(self._args[0])

        with mock.patch.object(app, "_merge_disk"), \
             mock.patch.object(panel.threading, "Thread", _Thread), \
             self._clock(now):
            app._tick()
        return started


class PanelPollGateTests(_PanelAppMixin, unittest.TestCase):
    """R1/R3/R5/R6 on the stop panel: the render tick stays 1 Hz, the REQUEST is gated per run."""

    def test_render_tick_interval_is_unchanged(self):
        # The same tick drives the live elapsed clock. Slowing it would freeze the seconds count.
        self.assertEqual(panel.POLL_INTERVAL_MS, 1000)

    def test_a_first_sight_run_is_polled_immediately(self):
        app = self._app()
        self.assertEqual(self._tick(app, now=100.0), ["r1"])

    def test_a_successful_poll_schedules_the_next_one_with_the_hubs_pacing(self):
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json",
            return_value={"status": "running", "profile": "standard", "poll_after_ms": 5000},
        ), self._clock(100.0):
            app._poll("r1")
        self.assertGreaterEqual(app._next_poll_at["r1"], 105.0)
        self.assertLessEqual(app._next_poll_at["r1"], 106.0)

    def test_a_poll_without_server_pacing_falls_back_to_the_base_interval(self):
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"status": "running", "profile": "standard"}
        ), self._clock(100.0):
            app._poll("r1")
        self.assertGreaterEqual(app._next_poll_at["r1"], 100.0 + runner.POLL_BASE_INTERVAL_S)

    def test_the_tick_issues_no_request_before_the_scheduled_time(self):
        app = self._app()
        app._next_poll_at["r1"] = 150.0
        self.assertEqual(self._tick(app, now=149.9), [])
        self.assertEqual(self._tick(app, now=150.0), ["r1"])

    def test_a_transient_failure_backs_the_next_poll_off(self):
        app = self._app()
        schedule = []
        for expected_failures in (1, 2, 3):
            app._polling.add("r1")
            with mock.patch.object(runner, "request_json", side_effect=_err(503)), \
                 self._clock(100.0):
                app._poll("r1")
            self.assertEqual(app._poll_failures["r1"], expected_failures)
            schedule.append(app._next_poll_at["r1"])
        for earlier, later in zip(schedule, schedule[1:]):
            self.assertGreater(later, earlier, "each consecutive failure must wait longer")
        self.assertIn("r1", app.runs, "a transient failure must not drop the row")

    def test_a_transport_failure_with_no_status_code_also_backs_off(self):
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(runner, "request_json", side_effect=_err()), \
             self._clock(100.0):
            app._poll("r1")
        self.assertEqual(app._poll_failures["r1"], 1)
        self.assertGreater(app._next_poll_at["r1"], 100.0)

    def test_a_429_backs_off_rather_than_hammering(self):
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(runner, "request_json", side_effect=_err(429)), \
             self._clock(100.0):
            app._poll("r1")
        self.assertGreaterEqual(
            app._next_poll_at["r1"], 100.0 + runner.POLL_BASE_INTERVAL_S
        )

    def test_a_successful_poll_clears_the_backoff(self):
        app = self._app()
        app._poll_failures["r1"] = 4
        app._auth_failures["r1"] = 1
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"status": "running", "profile": "standard"}
        ), self._clock(100.0):
            app._poll("r1")
        self.assertNotIn("r1", app._poll_failures)
        self.assertNotIn("r1", app._auth_failures)

    def _exhaust_auth(self, app, code=401, at=100.0):
        for _ in range(runner.POLL_AUTH_FAILURE_LIMIT):
            app._polling.add("r1")
            with mock.patch.object(runner, "request_json", side_effect=_err(code)), \
                 self._clock(at):
                app._poll("r1")

    def test_repeated_auth_failures_stop_the_normal_poll_cadence(self):
        app = self._app()
        self._exhaust_auth(app)
        self.assertEqual(
            self._tick(app, now=100.0 + runner.POLL_AUTH_PROBE_INTERVAL_S - 1.0), [],
            "a revoked token must not produce one request per second forever",
        )
        self.assertIn("r1", app.runs, "the row stays visible and goes stale; it is not reaped")

    def test_an_exhausted_auth_streak_still_probes_so_a_repaired_token_is_noticed(self):
        # Without this the stop is an ABSORBING state: the streak can only be cleared by a
        # successful poll, and no poll is ever issued again. Fixing the token on disk would
        # then require restarting the panel, because `request_json` re-reads the config per call.
        app = self._app()
        self._exhaust_auth(app)
        self.assertEqual(
            self._tick(app, now=100.0 + runner.POLL_AUTH_PROBE_INTERVAL_S), ["r1"]
        )

    def test_a_probe_that_succeeds_restores_the_normal_cadence(self):
        app = self._app()
        self._exhaust_auth(app)
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"status": "running", "profile": "standard"}
        ), self._clock(160.0):
            app._poll("r1")
        self.assertNotIn("r1", app._auth_failures)
        self.assertEqual(self._tick(app, now=170.0), ["r1"])

    def test_the_auth_streak_means_CONSECUTIVE_not_cumulative(self):
        # 401, 500, 401, 500, 401 is three auth failures but never three IN A ROW; treating it
        # as exhausted would drop a merely flaky hub to one probe a minute.
        app = self._app()
        for code in (401, 500, 401, 500, 401):
            app._polling.add("r1")
            with mock.patch.object(runner, "request_json", side_effect=_err(code)), \
                 self._clock(100.0):
                app._poll("r1")
        self.assertEqual(app._auth_failures.get("r1", 0), 1)

    def test_a_definitive_non_auth_failure_is_not_retried_at_the_backoff_rate(self):
        # A 400/422 on a status GET is the hub's final answer on this request; repeating it on
        # the transient ladder is the same "ask forever" the auth stop exists to prevent.
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(runner, "request_json", side_effect=_err(422)), \
             self._clock(100.0):
            app._poll("r1")
        self.assertAlmostEqual(
            app._next_poll_at["r1"], 100.0 + runner.POLL_AUTH_PROBE_INTERVAL_S
        )

    def test_a_late_failure_cannot_resurrect_a_retired_runs_bookkeeping(self):
        # The success path is epoch/retired-guarded; the failure path was not, so a worker that
        # started before a prune re-created state nothing would ever forget again.
        app = self._app()
        app._polling.add("r1")
        app._retired.add("r1")
        app.runs.pop("r1")
        app._defer_poll("r1", auth_failure=True)
        self.assertNotIn("r1", app._next_poll_at)
        self.assertNotIn("r1", app._poll_failures)
        self.assertNotIn("r1", app._auth_failures)
        self.assertNotIn("r1", app._polling, "the in-flight flag is still released")

    def test_a_hostile_poll_after_ms_cannot_wedge_the_run_in_polling(self):
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json",
            return_value={"status": "running", "profile": "standard", "poll_after_ms": 10**400},
        ), self._clock(100.0):
            app._poll("r1")
        self.assertNotIn("r1", app._polling, "an unusable field must not strand the run")
        self.assertEqual(self._tick(app, now=200.0), ["r1"])

    def test_a_single_auth_failure_does_not_stop_the_poll(self):
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(runner, "request_json", side_effect=_err(403)), \
             self._clock(100.0):
            app._poll("r1")
        # Deliberately BEFORE the probe interval: one 403 must still be on the fast ladder.
        self.assertEqual(
            self._tick(app, now=100.0 + runner.POLL_AUTH_PROBE_INTERVAL_S - 1.0), ["r1"]
        )

    def test_a_successful_poll_resets_the_auth_streak(self):
        app = self._app()
        for _ in range(runner.POLL_AUTH_FAILURE_LIMIT - 1):
            app._polling.add("r1")
            with mock.patch.object(runner, "request_json", side_effect=_err(401)), \
                 self._clock(100.0):
                app._poll("r1")
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"status": "running", "profile": "standard"}
        ), self._clock(100.0):
            app._poll("r1")
        app._polling.add("r1")
        with mock.patch.object(runner, "request_json", side_effect=_err(401)), \
             self._clock(100.0):
            app._poll("r1")
        self.assertEqual(self._tick(app, now=10_000.0), ["r1"])

    def test_poll_scheduling_survives_the_wall_clock_jumping_backwards(self):
        # An NTP correction, a VM resume or a user changing the system clock must not suspend
        # every poll for the size of the jump. Intervals read the monotonic clock.
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"status": "running", "profile": "standard"}
        ), mock.patch.object(panel.time, "time", return_value=1_000_000.0), \
             mock.patch.object(panel.time, "monotonic", return_value=500.0):
            app._poll("r1")
        started = []

        class _Thread:
            def __init__(self, target=None, args=(), daemon=False, **kwargs):
                self._args = args

            def start(self):
                started.append(self._args[0])

        with mock.patch.object(app, "_merge_disk"), \
             mock.patch.object(panel.threading, "Thread", _Thread), \
             mock.patch.object(panel.time, "time", return_value=1.0), \
             mock.patch.object(panel.time, "monotonic", return_value=510.0):
            app._tick()
        self.assertEqual(started, ["r1"], "a backwards wall clock must not freeze polling")

    def test_a_gone_reply_is_reaped_rather_than_backed_off(self):
        app = self._app()
        for expected in range(1, panel.NOT_FOUND_FORGET_THRESHOLD):
            app._polling.add("r1")
            with mock.patch.object(runner, "request_json", side_effect=_err(404)):
                app._poll("r1")
            self.assertEqual(app._not_found_polls["r1"], expected)
        app._polling.add("r1")
        with mock.patch.object(runner, "request_json", side_effect=_err(404)), \
             mock.patch.object(panel.threading, "Thread"):
            app._poll("r1")
        self.assertNotIn("r1", app.runs)
        self.assertIn("r1", app._retired)

    def test_a_terminal_status_issues_no_further_request(self):
        for status in sorted(panel.TERMINAL_STATUSES):
            with self.subTest(status=status):
                app = self._app()
                app._polling.add("r1")
                with mock.patch.object(
                    runner, "request_json",
                    return_value={"status": status, "profile": "standard", "poll_after_ms": 250},
                ), mock.patch.object(runner, "save_active_run"), \
                     self._clock(100.0):
                    app._poll("r1")
                self.assertEqual(self._tick(app, now=101.0), [],
                                 "a finished run must never be polled again")

    def test_one_inflight_request_per_run(self):
        app = self._app()
        app._polling.add("r1")
        self.assertEqual(self._tick(app, now=10_000.0), [],
                         "a poll already in flight must not be joined by a second one")

    def test_a_local_advisory_run_is_still_never_polled(self):
        app = self._app(local=True)
        self.assertEqual(self._tick(app, now=100.0), [])

    def test_a_non_audit_exception_defers_the_poll_instead_of_killing_it(self):
        # `_poll` runs on a daemon thread. Anything that is not an AuditError — a urllib internal,
        # a MemoryError, a defect in our own request path — used to leave the run in `_polling`
        # with nothing to clear it, so that row was never polled again for the life of the panel.
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(runner, "request_json", side_effect=RuntimeError("boom")), \
             self._clock(100.0):
            app._poll("r1")
        self.assertNotIn("r1", app._polling, "the in-flight marker must always be released")
        self.assertIn("r1", app._next_poll_at, "the run must be rescheduled, not abandoned")
        self.assertGreater(app._next_poll_at["r1"], 100.0)

    def test_a_registry_write_failure_does_not_wedge_the_poll(self):
        # Persisting the row is best-effort UI state: the active-runs file may be read-only, on a
        # full disk, or locked by another process. None of that may cost the panel a live poller.
        app = self._app()
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json",
            return_value={"status": "completed", "profile": "standard"},
        ), mock.patch.object(runner, "save_active_run", side_effect=OSError("read-only")), \
             self._clock(100.0):
            app._poll("r1")
        self.assertNotIn("r1", app._polling)
        self.assertEqual(app.runs["r1"]["status"], "completed",
                         "the view still merged; only the disk write failed")

    def test_the_idle_exit_countdown_is_wall_clock_while_the_poll_gate_is_monotonic(self):
        # The two clocks in `_tick` are deliberately different: the poll gate reads `monotonic`
        # so a clock jump cannot suspend polling, while idle-exit and the elapsed display stay on
        # the wall clock they describe. Freeze monotonic and advance only the wall clock: the
        # countdown must still run, which is what proves the split is real and not an accident.
        app = self._app()
        app.runs = {}
        app._visible_runs = mock.Mock(return_value=[])
        app._prune = mock.Mock()

        def _tick_at(wall):
            with mock.patch.object(panel.time, "time", return_value=wall), \
                 mock.patch.object(panel.time, "monotonic", return_value=500.0), \
                 mock.patch.object(app, "_merge_disk"):
                app._tick()

        _tick_at(100.0)
        self.assertEqual(app._no_runs_since, 100.0)
        app.root.destroy.assert_not_called()
        app.root.after.assert_called_with(panel.POLL_INTERVAL_MS, app._tick)

        app.root.reset_mock()
        _tick_at(100.0 + panel.IDLE_EXIT_S)
        app.root.destroy.assert_called_once_with()
        app.root.after.assert_not_called()

    def test_a_reappearing_run_restarts_the_idle_countdown(self):
        app = self._app()
        app.runs = {}
        app._prune = mock.Mock()
        app._visible_runs = mock.Mock(return_value=[])
        with mock.patch.object(app, "_merge_disk"), self._clock(100.0):
            app._tick()
        self.assertEqual(app._no_runs_since, 100.0)
        app._visible_runs = mock.Mock(return_value=[("r1",)])
        with mock.patch.object(app, "_merge_disk"), self._clock(101.0):
            app._tick()
        self.assertIsNone(app._no_runs_since, "a visible run must reset the countdown, not pause it")
        app.root.destroy.assert_not_called()

    def test_poll_bookkeeping_is_dropped_when_a_run_is_pruned(self):
        app = self._app(status="completed")
        app.runs["r1"]["hidden_after"] = 50.0
        app._next_poll_at["r1"] = 100.0
        app._poll_failures["r1"] = 2
        app._auth_failures["r1"] = 1
        with mock.patch.object(panel.threading, "Thread"):
            app._prune(100.0)
        self.assertNotIn("r1", app._next_poll_at)
        self.assertNotIn("r1", app._poll_failures)
        self.assertNotIn("r1", app._auth_failures)


class WaitLoopTests(unittest.TestCase):
    """R1/R3/R4/R5 on `audit submit --wait`: the CLI loop paces, retries and stops the same way."""

    def _wait(self, statuses, *, poll_s=1.0, result=None, json_output=False):
        """Drive wait_and_print_result over `statuses` (dicts or raised AuditErrors)."""
        calls = []
        sleeps = []

        def fake_request(method, path, **kwargs):
            calls.append(path)
            if path.endswith("/result"):
                return result or {"markdown": "ok"}
            nxt = statuses.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

        with mock.patch.object(runner, "request_json", side_effect=fake_request), \
             mock.patch.object(runner, "save_active_run"), \
             mock.patch.object(runner, "clear_active_run"), \
             mock.patch.object(runner.time, "sleep", side_effect=sleeps.append), \
             mock.patch("builtins.print"):
            code = runner.wait_and_print_result("aud_1", poll_s, json_output=json_output)
        return code, calls, sleeps

    def test_queued_running_completed_reaches_the_result(self):
        code, calls, _ = self._wait([
            {"run_id": "aud_1", "status": "queued"},
            {"run_id": "aud_1", "status": "running"},
            {"run_id": "aud_1", "status": "completed"},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [runner.audit_status_path("aud_1")] * 3 + ["/v1/audits/aud_1/result"])

    def test_queued_failed_returns_nonzero(self):
        code, calls, _ = self._wait([
            {"run_id": "aud_1", "status": "queued"},
            {"run_id": "aud_1", "status": "failed"},
        ])
        self.assertEqual(code, 1)
        self.assertEqual(calls.count(runner.audit_status_path("aud_1")), 2)

    def test_running_cancelling_cancelled_returns_nonzero_and_stops(self):
        code, calls, _ = self._wait([
            {"run_id": "aud_1", "status": "running"},
            {"run_id": "aud_1", "status": "cancelling"},
            {"run_id": "aud_1", "status": "cancelled"},
        ])
        self.assertEqual(code, 1)
        self.assertEqual(calls.count(runner.audit_status_path("aud_1")), 3,
                         "no status request may follow a terminal one")

    def test_the_hubs_poll_after_ms_paces_the_wait(self):
        _, _, sleeps = self._wait([
            {"run_id": "aud_1", "status": "running", "poll_after_ms": 5000},
            {"run_id": "aud_1", "status": "completed"},
        ])
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 5.0)
        self.assertLessEqual(sleeps[0], 5.0 * (1.0 + runner.POLL_JITTER_FRACTION))

    def test_without_server_pacing_the_cli_poll_s_is_the_base(self):
        _, _, sleeps = self._wait([
            {"run_id": "aud_1", "status": "running"},
            {"run_id": "aud_1", "status": "completed"},
        ], poll_s=3.0)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 3.0)
        self.assertLessEqual(sleeps[0], 3.0 * (1.0 + runner.POLL_JITTER_FRACTION))

    def test_a_fixed_one_second_sleep_is_no_longer_the_only_strategy(self):
        _, _, sleeps = self._wait([
            {"run_id": "aud_1", "status": "running", "poll_after_ms": 7000},
            {"run_id": "aud_1", "status": "completed"},
        ])
        self.assertNotEqual(sleeps, [1.0])

    def test_a_transient_failure_is_retried_with_backoff(self):
        code, calls, sleeps = self._wait([
            _err(503),
            _err(None),
            {"run_id": "aud_1", "status": "completed"},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(calls.count(runner.audit_status_path("aud_1")), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertGreater(sleeps[1], sleeps[0])

    def test_a_429_is_retried_with_backoff(self):
        code, _, sleeps = self._wait([
            _err(429),
            {"run_id": "aud_1", "status": "completed"},
        ])
        self.assertEqual(code, 0)
        self.assertGreaterEqual(sleeps[0], runner.POLL_BASE_INTERVAL_S)

    def test_wait_does_not_retry_definitive_failures(self):
        for code in sorted(runner.POLL_AUTH_STATUS_CODES) + [404, 409]:
            with self.subTest(status_code=code):
                with self.assertRaises(runner.AuditError):
                    self._wait([_err(code)])

    def test_the_retry_budget_is_bounded(self):
        with self.assertRaises(runner.AuditError):
            self._wait([_err(503)] * (runner.POLL_WAIT_MAX_CONSECUTIVE_FAILURES + 5))

    def test_a_success_between_failures_restores_the_full_budget(self):
        statuses = []
        for _ in range(3):
            statuses += [_err(503)] * (runner.POLL_WAIT_MAX_CONSECUTIVE_FAILURES - 1)
            statuses.append({"run_id": "aud_1", "status": "running"})
        statuses.append({"run_id": "aud_1", "status": "completed"})
        code, _, _ = self._wait(statuses)
        self.assertEqual(code, 0)

    def test_json_output_emits_the_envelope_and_not_the_markdown(self):
        # `--json` is the machine-readable contract: a scripted caller parses stdout. The pacing
        # change rewrote this loop's body, and only the human branch was pinned — the JSON branch
        # could have been dropped or fed the wrong object with the suite still green.
        envelope = {"run_id": "aud_1", "status": "completed", "markdown": "ok"}
        with mock.patch.object(runner, "print_json") as print_json:
            code, _, _ = self._wait(
                [{"run_id": "aud_1", "status": "completed"}], result=envelope, json_output=True,
            )
        self.assertEqual(code, 0)
        print_json.assert_called_once_with(envelope)

    def test_without_json_output_the_envelope_is_not_machine_printed(self):
        with mock.patch.object(runner, "print_json") as print_json:
            self._wait([{"run_id": "aud_1", "status": "completed"}])
        print_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
