"""End-to-end + honesty tests for the local degraded audit path (design §11 / §17, PR6).

The other tests in this suite each cover ONE unit of the degrade path. These cover the
CONTRACT the whole path exists to keep, end to end and in the words a user actually sees:

  * the raw `de_audit` compatibility marker remains an opt-in JSON-RPC error and NEVER becomes
    a local result (§17.1: the local read closes no gate);
  * an explicit interactive request or verified AQG audit-before-commit gate is sufficient
    authorization and may return a local advisory result without a second confirmation;
  * only `/audit` defect review is ever offered one — every other intent / tool keeps the
    plain offline error (§11);
  * a headless caller is never offered a local fallback, even when an AQG gate may authorize
    the hosted path, pinned in routing prose because the shim cannot see whether a TTY exists;
  * nothing on the path can read as a passed audit: no `audit_id` in any status, no pass
    mark, no green, and every terminal row keeps its 仅参考 qualifier (§17.4 — honest
    marking is the one hard requirement left in v5).

SCOPE — what "end to end" means here, honestly (audit d6d63118 f2, 4/4 convergent): this
covers the DE CLIENT side, which is the whole of the DE-side degrade path. It does NOT
exercise a gate-closing consumer, because per v5 §17.2 the client HAS none: the trust root
sits with the human or the platform (a branch-protection audit check stays red until a real
panel runs), and the AQG-side closeout lives in the agent-quality-gates repo. "Never closes
a gate" is enforced structurally — nothing on this path ever emits an `audit_id` or a
verdict for a gate to read — and that structural absence is what the tests below assert.

Run:  python3 -m unittest installer.tests.test_local_degraded_e2e
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from client import i18n, runner
from client.stopper import panel
from installer import leak_scan, shim


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTING_DOCS = ("skills/audit/references/de-lite-routing.md",)
_AUDIT_ENTRY = _REPO_ROOT / "skills/audit/SKILL.md"

# The vocabulary a REAL cross-vendor panel uses for a finished, PASSING audit. None of it may
# ever appear on a local advisory row — that is the whole honesty contract (§17.4). Compared
# case-insensitively, so a future `PASS` / `Clean` cannot slip through (audit d6d63118 f4).
# `完成` is deliberately NOT banned outright: the render says 完成（仅参考）, which is honest.
# What must hold instead is the qualifier — see _ADVISORY_QUALIFIER.
_PASS_WORDS = ("✓", "已完成", "clean", "pass", "通过", "绿")

# Every terminal local row must keep this qualifier, else 完成 / 部分完成 (or "complete") alone
# would read as a finished audit while passing the banlist above (audit d6d63118 grok f2). The
# contract is locale-INDEPENDENT: both rendered languages must carry their own qualifier.
_ADVISORY_QUALIFIER = {"zh-CN": "仅参考", "en-US": "reference only"}

# Statuses whose rendered row is a FINISHED one, so it must carry the qualifier. `cancelled`
# and `failed` are terminal too but say so plainly (已取消 / 失败) — they cannot read as a pass.
# A missing status is NOT here: `overall_status` maps it to `queued` (排队中), an honest
# not-started row; only an UNRECOGNIZED status reaches the 状态未知（仅参考） fallback arm.
_FINISHED_STATUSES = ("completed", "partial", "bogus")

_ALL_STATUSES = ("queued", "running", "cancelling", "cancelled", "failed",
                 "completed", "partial", "bogus", None)

_SERVICE_UNAVAILABLE_KEYS = {
    "status", "reason", "retryable", "request_sent", "action",
}


class HostMCPUnavailableRoutingContractTests(unittest.TestCase):
    def test_shipped_audit_skill_carries_the_local_bridge_boundary(self):
        required = (
            "authorized interactive defect-review",
            "verified AQG audit-before-commit gate",
            "before any MCP invocation",
            "current task tool list",
            "conclusive pre-call evidence",
            "current audit attempt",
            "before reading or selecting any generic review skill",
            "`.system/review-agent`",
            "`.system/audit`",
            "de_lite_local_bridge.py",
            "mcp_unavailable",
            "request_outcome_unknown",
            "must not call the bridge",
            "never contacts the Hub",
        )
        for rel in _ROUTING_DOCS:
            with self.subTest(doc=rel):
                text = " ".join((_REPO_ROOT / rel).read_text(encoding="utf-8").split())
                for phrase in required:
                    self.assertIn(phrase, text)
                if rel == "skills/audit/references/de-lite-routing.md":
                    self.assertIn("Codex-only", text)

    def test_audit_skill_frontmatter_keeps_selection_signals_only(self):
        lines = _AUDIT_ENTRY.read_text(encoding="utf-8").splitlines()
        description = json.loads(lines[2].removeprefix("description: "))

        self.assertIn("explicit interactive user request", description)
        self.assertIn("verified AQG audit-before-commit gate", description)
        self.assertNotIn("MCP tools are absent", description)
        self.assertNotIn("DE Lite local bridge", description)
        self.assertNotIn("generic review fallback", description)

    def test_shipped_audit_skill_automates_only_verified_aqg_triggers(self):
        entry = _AUDIT_ENTRY.read_text(encoding="utf-8")
        frontmatter = json.loads(entry.splitlines()[2].removeprefix("description: "))
        entry_normalized = " ".join(entry.split()).lower()
        routing_normalized = " ".join(
            (_REPO_ROOT / _ROUTING_DOCS[0]).read_text(encoding="utf-8").split()
        ).lower()

        self.assertNotIn("after producing substantial output", frontmatter)
        self.assertIn("explicit interactive user request", frontmatter)
        self.assertIn("verified AQG audit-before-commit gate", frontmatter)
        self.assertIn("sufficient authorization", entry_normalized)
        self.assertIn("without asking the user for confirmation", entry_normalized)
        self.assertIn("no audit already covers that unchanged logical change", entry_normalized)
        self.assertIn("trusted installed aqg checkout", entry_normalized)
        self.assertIn("working-tree diff or named artifact", routing_normalized)
        self.assertIn("if the caller is headless, do not start the bridge", routing_normalized)
        self.assertIn("leave the audit gate open", routing_normalized)
        self.assertIn("ordinary non-audit work", entry_normalized)
        self.assertIn("do not authorize a run by themselves", entry_normalized)
        self.assertNotIn(
            "implicit completion gate, audit-before-commit reminder", entry_normalized
        )
        self.assertIn("ordinary non-audit work", entry_normalized)


def _audit_call(arguments=None, *, name="de_audit", msg_id=1):
    params = {"name": name}
    if arguments is not None:
        arguments = dict(arguments) if isinstance(arguments, dict) else arguments
        if name in {"de_audit", "submit_audit"} and isinstance(arguments, dict):
            arguments.setdefault("title", "Explicit audit topic")
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


def _serve_offline(message):
    """Drive the REAL forwarder + serve loop with the hub unreachable, and return the
    JSON-RPC envelope the caller receives. Nothing is sent: the probe short-circuits."""
    forwarder = shim.Forwarder("https://hub.example", "tok")
    stdout = io.StringIO()
    with mock.patch.object(shim, "hub_reachable", return_value=False), \
            mock.patch.object(shim, "_maybe_self_update"):
        shim.serve(forwarder,
                   stdin=io.StringIO(json.dumps(message) + "\n"),
                   stdout=stdout)
    lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
    return lines[0]


class OfflineHardStopTests(unittest.TestCase):
    """R2 — the degraded path never turns an outage into a result."""

    def test_opted_in_defect_review_gets_an_error_carrying_only_the_offer(self):
        out = _serve_offline(_audit_call({"accept_degrade": True}))
        self.assertNotIn("result", out, "an unreachable hub must never produce a result")
        self.assertEqual(out["error"]["code"], -32001)
        # The marker is a status enum and nothing more: no verdict, no findings, no audit id.
        # This exact-key assertion is the primary lock — a verdict cannot hide in a key that
        # is not there (audit d6d63118 grok f4).
        self.assertEqual(
            set(out["error"]["data"]),
            _SERVICE_UNAVAILABLE_KEYS | {"local_advisory_available"},
        )
        self.assertEqual(out["error"]["data"]["status"], "service_unavailable")
        self.assertEqual(out["error"]["data"]["reason"], "unreachable")
        self.assertIs(out["error"]["data"]["local_advisory_available"], True)

    def test_the_offer_never_carries_pass_semantics_or_an_audit_id(self):
        # Scoped to error.data: the human-readable message is prose about the outage and must
        # not be lexically policed (a "bypass"/"compass" substring would false-fail).
        data = json.dumps(_serve_offline(_audit_call({"accept_degrade": True}))["error"]["data"],
                          ensure_ascii=False).lower()
        self.assertNotIn("audit_id", data)
        for word in _PASS_WORDS + ("waived", "approved"):
            self.assertNotIn(word.lower(), data, "the offer must not read as a verdict")

    def test_nothing_is_sent_to_the_hub_when_it_is_unreachable(self):
        forwarder = shim.Forwarder("https://hub.example", "tok")
        with mock.patch.object(shim, "hub_reachable", return_value=False), \
                mock.patch.object(shim, "build_opener") as opener:
            with self.assertRaises(shim.OfflineError):
                forwarder.forward(_audit_call({"accept_degrade": True}))
        opener.assert_not_called()

    def test_a_reachable_hub_receives_the_opt_in_flag_verbatim(self):
        # The shim is transport: it does NOT strip or rewrite arguments on the healthy path.
        # Pinning it here means a future "helpfully filter accept_degrade out" change has to
        # be a deliberate contract decision, not a silent one (audit d6d63118 claude f3).
        sent = {}

        class _Resp:
            headers = {"Content-Length": "2"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *a, **kw):
                return b"{}"

        class _Opener:
            def open(self, request, timeout=None):
                sent["body"] = json.loads(request.data.decode("utf-8"))
                return _Resp()

        forwarder = shim.Forwarder("https://hub.example", "tok")
        message = _audit_call({"accept_degrade": True, "artifact": "x"})
        with mock.patch.object(shim, "hub_reachable", return_value=True), \
                mock.patch.object(shim, "build_opener", return_value=_Opener()), \
                mock.patch.object(shim, "read_within_budget", return_value=b"{}"):
            forwarder.forward(message)
        self.assertEqual(sent["body"], message, "arguments must reach the hub unmodified")


class AcceptDegradeGateTests(unittest.TestCase):
    """R1 — the offer is opt-in, and the opt-in is a real boolean."""

    def test_without_the_flag_the_offline_error_carries_no_offer(self):
        out = _serve_offline(_audit_call({}))
        self.assertEqual(out["error"]["code"], -32001)
        self.assertEqual(set(out["error"]["data"]), _SERVICE_UNAVAILABLE_KEYS)
        self.assertEqual(out["error"]["data"]["status"], "service_unavailable")
        self.assertNotIn("local_advisory_available", out["error"]["data"])

    def test_absent_arguments_carries_no_offer(self):
        self.assertIsNone(shim._local_advisory_offer(_audit_call()))

    def test_non_boolean_truthy_flag_is_refused(self):
        # JSON-RPC has a real boolean type, so "true" / 1 / ["yes"] is a caller bug. Fail closed:
        # the safe outcome is exactly today's plain offline error.
        for bad in ("true", "True", 1, 1.0, ["yes"], {"ok": 1}, "yes"):
            with self.subTest(flag=bad):
                self.assertIsNone(
                    shim._local_advisory_offer(_audit_call({"accept_degrade": bad})))

    def test_false_flag_is_refused(self):
        for falsy in (False, None, 0, ""):
            with self.subTest(flag=falsy):
                self.assertIsNone(
                    shim._local_advisory_offer(_audit_call({"accept_degrade": falsy})))

    def test_the_flag_alone_is_enough_for_a_defect_review_call(self):
        offer = shim._local_advisory_offer(_audit_call({"accept_degrade": True}))
        self.assertEqual(offer, {"reason": "unreachable", "local_advisory_available": True})
        # ... and with the intent spelled out explicitly, same answer.
        self.assertEqual(
            shim._local_advisory_offer(
                _audit_call({"accept_degrade": True, "artifact_intent": "prescriptive"})),
            {"reason": "unreachable", "local_advisory_available": True})

    def test_the_opt_in_contract_is_stated_in_the_audit_skill(self):
        # The bug PR6 repaired was prose promising an opt-in the code never enforced. Pin the
        # prose to the code so neither side can drift again silently
        # (audit d6d63118 claude f1).
        for rel in _ROUTING_DOCS:
            with self.subTest(doc=rel):
                bullet = _degrade_bullet(self, rel, "accept_degrade")
                self.assertIn("without", bullet,
                              "%s must say what happens WITHOUT the flag" % rel)
                self.assertIn("iserror", bullet.replace(" ", ""),
                              "%s must state the no-flag outcome is an isError hard stop" % rel)


class NonDefectReviewNeverDegradesTests(unittest.TestCase):
    """R3 — opting in does not widen the scope past `/audit` defect review (§11)."""

    def test_hypothesis_intent_hard_stops_even_when_opted_in(self):
        out = _serve_offline(_audit_call({"accept_degrade": True,
                                          "artifact_intent": "hypothesis"}))
        self.assertEqual(out["error"]["code"], -32001)
        self.assertEqual(set(out["error"]["data"]), _SERVICE_UNAVAILABLE_KEYS)

    def test_a_malformed_or_unknown_intent_hard_stops_without_crashing_the_loop(self):
        # A non-str intent used to be able to raise TypeError out of the frozenset membership
        # test, which would escape the serve loop's `except ShellError` and kill the transport.
        # An explicitly null intent is a malformed value, NOT an absent key, so it fails closed
        # too (audit d6d63118 codex f1). Driven through serve(), so a crash shows up as a
        # missing reply rather than a swallowed exception.
        for intent in ([], {}, ["prescriptive"], 1, True, None, "other", ""):
            with self.subTest(intent=intent):
                out = _serve_offline(_audit_call({"accept_degrade": True,
                                                  "artifact_intent": intent}))
                self.assertEqual(out["error"]["code"], -32001)
                self.assertEqual(set(out["error"]["data"]), _SERVICE_UNAVAILABLE_KEYS)

    def test_retrieval_grounded_and_display_tools_hard_stop_even_when_opted_in(self):
        # market-research / forecast need live retrieval, and the render tools are server-side:
        # a local model would have to invent the answer. Permanently excluded.
        for tool in ("de_market_research", "de_forecast", "ge_render", "de_wait_audit"):
            with self.subTest(tool=tool):
                out = _serve_offline(_audit_call({"accept_degrade": True}, name=tool))
                self.assertEqual(out["error"]["code"], -32001)
                self.assertEqual(set(out["error"]["data"]), _SERVICE_UNAVAILABLE_KEYS)

    def test_tools_list_exposes_status_and_explicit_local_audit_entry(self):
        out = _serve_offline({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                              "params": {"arguments": {"accept_degrade": True}}})
        self.assertEqual(
            [tool["name"] for tool in out["result"]["tools"]],
            ["service_unavailable", "audit_skill_submit", "audit_skill_complete"],
        )

    def test_a_non_dict_params_hard_stops_without_crashing_the_loop(self):
        out = _serve_offline({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": "oops"})
        self.assertEqual(out["error"]["code"], -32001)
        self.assertEqual(set(out["error"]["data"]), _SERVICE_UNAVAILABLE_KEYS)


def _degrade_bullet(case, rel, needle):
    """The ONE markdown bullet of `rel`'s degrade section that mentions `needle`, lowercased.

    Region-wide substring checks are too weak to pin a rule: "headless" in one bullet and
    "do not offer" in an unrelated one would satisfy them (audit d6d63118, 3/4 convergent).
    Binding every assertion to a single bullet is what makes the pin mean something."""
    text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
    regions = [region for region, _ in leak_scan._degrade_regions(text)]
    case.assertTrue(regions, "%s must still carry a degrade section" % rel)
    bullets, current = [], []
    for line in "\n".join(regions).splitlines():
        if line.lstrip().startswith("- "):
            if current:
                bullets.append(" ".join(current))
            current = [line.strip()]
        elif current and line.strip():
            current.append(line.strip())         # continuation line of the same bullet
        elif current:
            bullets.append(" ".join(current))
            current = []
    if current:
        bullets.append(" ".join(current))
    matches = [b.lower() for b in bullets if needle.lower() in b.lower()]
    case.assertEqual(len(matches), 1,
                     "%s must state the %s rule in exactly one bullet (found %d)"
                     % (rel, needle, len(matches)))
    return matches[0]


class HeadlessOfferRuleTests(unittest.TestCase):
    """R4 — a headless caller has nobody to ask, so it must not offer at all.

    The shim cannot detect a TTY on the caller's side (it only sees JSON-RPC), so this rule
    can only live in the routing prose. Pin it — bullet-scoped, both halves — so it cannot
    silently vanish, and keep the added prose routing-only (the PR5b leak gate)."""

    def test_headless_rule_present_in_the_audit_skill(self):
        for rel in _ROUTING_DOCS:
            with self.subTest(doc=rel):
                bullet = _degrade_bullet(self, rel, "headless")
                self.assertTrue(
                    "not offer" in bullet or "never offer" in bullet,
                    "%s: the headless bullet must forbid OFFERING" % rel)
                self.assertTrue(
                    "not run" in bullet or "never run" in bullet,
                    "%s: the headless bullet must forbid RUNNING a local read" % rel)
                self.assertIn("offline error", bullet,
                              "%s: the headless bullet must name the fallback behaviour" % rel)

    def test_the_degrade_prose_stays_routing_only(self):
        self.assertEqual(leak_scan.scan_degrade_prose_repo(_REPO_ROOT), [])


class PanelHonestyEndToEndTests(unittest.TestCase):
    """R5 — seed → registry → rendered row: a local read can never read as a passed audit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "active-runs.json"
        self._prev = os.environ.get("DE_ACTIVE_RUNS")
        os.environ["DE_ACTIVE_RUNS"] = str(self.registry)
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("DE_ACTIVE_RUNS", None)
        else:
            os.environ["DE_ACTIVE_RUNS"] = self._prev

    def _seeded(self, status):
        run_id = "local-e2e-%s" % (status if status is not None else "none")
        runner.save_local_advisory_run(run_id, depth="deep", status=status,
                                       started_at=1000.0)
        entry = json.loads(self.registry.read_text())["runs"][run_id]
        # Assert the round trip, so a writer that normalized an unknown/missing status could not
        # silently turn the fallback-arm subTests into duplicates of a valid one (audit d6d63118
        # claude f9).
        self.assertEqual(entry.get("status"), status)
        return entry

    def test_no_status_carries_an_audit_id(self):
        # Terminal states matter most: a finished local run is exactly where one could start
        # impersonating a real panel result (audit d6d63118 gemini f2).
        for status in _ALL_STATUSES:
            with self.subTest(status=status):
                entry = self._seeded(status)
                self.assertIs(entry["local"], True)
                self.assertNotIn("audit_id", entry)
                self.assertNotIn("audit_id", json.dumps(runner.active_run_payload(entry)))

    def test_no_reachable_status_renders_as_a_passed_audit(self):
        # Sweep every status the entry can hold, INCLUDING the ones a caller should never
        # write (a real panel's own vocabulary) and an unreadable one.
        for status in _ALL_STATUSES:
            with self.subTest(status=status):
                entry = self._seeded(status)
                for locale in _ADVISORY_QUALIFIER:
                    localized = dict(entry, ui_locale=locale)
                    line = panel.depth_line_text(localized, now=1100.0, frozen={})
                    self.assertIn("🔶", line, "every local row must carry the degraded marking")
                    lowered = line.lower()
                    for word in _PASS_WORDS:
                        self.assertNotIn(word.lower(), lowered)
                self.assertNotEqual(panel.depth_line_color(entry, now=1100.0), panel.FG_GREEN)

    def test_every_finished_row_keeps_its_advisory_qualifier(self):
        # Without this, dropping （仅参考） from 完成 would still satisfy the banlist above and
        # a finished local row would read as a finished audit (audit d6d63118 grok f2).
        for status in _FINISHED_STATUSES:
            entry = self._seeded(status)
            for locale, qualifier in _ADVISORY_QUALIFIER.items():
                with self.subTest(status=status, locale=locale):
                    line = panel.depth_line_text(dict(entry, ui_locale=locale),
                                                 now=1100.0, frozen={})
                    self.assertIn(qualifier, line.lower() if locale == "en-US" else line)

    def test_a_local_row_is_never_hub_polled(self):
        # A hub poll would 404 (the hub never saw this run) and reap the honest row mid-read.
        self.assertFalse(panel.should_hub_poll(self._seeded("running")))


class DELiteExplicitAuditEndToEndTests(unittest.TestCase):
    """Unactivated explicit submit -> local registry -> honest running/completed UI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "active-runs.json"
        self._prev = os.environ.get("DE_ACTIVE_RUNS")
        os.environ["DE_ACTIVE_RUNS"] = str(self.registry)
        self.addCleanup(self.tmp.cleanup)
        # This run carries no `ui_locale`; the default path now consults the OS UI language. Pin it
        # OFF so "no locale → en-US" is deterministic on any host (incl. zh-CN CI machines).
        patcher = mock.patch.object(i18n, "detect_system_locale", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("DE_ACTIVE_RUNS", None)
        else:
            os.environ["DE_ACTIVE_RUNS"] = self._prev

    def test_explicit_lite_audit_never_forwards_and_round_trips_the_panel_lifecycle(self):
        request = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "audit_skill_submit",
                "arguments": {
                    "skill_name": "audit",
                    "args": {
                        "title": "Local contract review",
                        "content": "artifact bytes stay with the caller",
                        "artifact_intent": "prescriptive",
                        "accept_degrade": True,
                    },
                },
            },
        }
        with mock.patch.object(shim.secrets, "token_hex", return_value="e2e"), \
                mock.patch.object(shim.Forwarder, "forward") as hub_forward:
            response = shim.LiteForwarder().forward(request)
        hub_forward.assert_not_called()

        envelope = json.loads(response["result"]["content"][0]["text"])
        self.assertIsNone(envelope["payload"]["audit_id"])
        self.assertNotIn("artifact bytes", json.dumps(envelope))
        with mock.patch.object(runner, "launch_stopper_if_available") as launch:
            shim._spawn_stopper_for_audit(response)
        launch.assert_called_once_with(prefer_shipped=False)

        entry = json.loads(self.registry.read_text())["runs"]["local_e2e"]
        self.assertEqual(entry["local_surface"], "de_lite")
        self.assertFalse(panel.should_hub_poll(entry))
        # no `ui_locale` was passed at submit → the default (en-US) renders the row.
        self.assertIn("DE Lite · Local audit in progress",
                      panel.depth_line_text(entry, time.time(), {}))

        completion = {
            "jsonrpc": "2.0", "id": 13, "method": "tools/call",
            "params": {"name": "audit_skill_complete", "arguments": {
                "local_id": "local_e2e", "status": "completed"
            }},
        }
        stdout = io.StringIO()
        with mock.patch.object(shim, "_maybe_self_update"):
            shim.serve(
                shim.LiteForwarder(),
                stdin=io.StringIO(json.dumps(completion) + "\n"),
                stdout=stdout,
                client_host="codex",
            )
        completion_out = json.loads(stdout.getvalue().splitlines()[0])
        completion_payload = json.loads(
            completion_out["result"]["content"][0]["text"]
        )
        self.assertEqual(completion_payload["status"], "completed")
        done = json.loads(self.registry.read_text())["runs"]["local_e2e"]
        line = panel.depth_line_text(done, time.time(), {})
        self.assertIn("DE Lite · Local audit completed (reference only)", line)
        self.assertNotIn("✓", line)
        self.assertNotEqual(panel.depth_line_color(done), panel.FG_GREEN)
        self.assertNotIn("audit_id", done)

        # Replaying the original envelope after completion must not reopen the terminal run.
        with mock.patch.object(runner, "launch_stopper_if_available"):
            shim._spawn_stopper_for_audit(response)
        replayed_registry = json.loads(self.registry.read_text())
        self.assertEqual(replayed_registry["runs"]["local_e2e"]["status"], "completed")
        self.assertIn("local_e2e", replayed_registry["local_terminal_tombstones"])
