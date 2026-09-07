"""Source-level safety contracts for the native macOS stopper.

The Windows test lane cannot compile AppKit code, but it can keep the lock protocol's
critical fail-closed invariants from silently regressing. A macOS release lane must
still compile and exercise the app itself.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "desktop"
    / "macos"
    / "DecisionEngineStopper.swift"
).read_text(encoding="utf-8")
_BINARY_DIR = (
    Path(__file__).resolve().parents[2]
    / "desktop"
    / "macos"
    / "bin"
)
_BINARIES = tuple(
    _BINARY_DIR / ("decision-engine-stopper-%s" % arch)
    for arch in ("arm64", "x86_64")
)


class MacOSStopperSourceContractTests(unittest.TestCase):
    def test_registry_lock_path_is_injected_by_the_runner(self):
        self.assertIn('"DE_ACTIVE_RUNS_LOCK_PATHS"', _SOURCE)
        self.assertIn('"DE_ACTIVE_RUNS_WRITE_PATHS"', _SOURCE)
        self.assertNotIn("private func pathKey", _SOURCE)

    def test_registry_lock_retries_interrupts_and_never_runs_body_unlocked(self):
        lock_body = _SOURCE.split("private func withRegistryLock", 1)[1].split(
            "private func latestActiveRun", 1
        )[0]
        self.assertGreaterEqual(lock_body.count("errno == EINTR"), 2)
        self.assertRegex(lock_body, r"if fd < 0\s*\{[\s\S]*?return false")
        self.assertRegex(lock_body, r"if lockResult != 0\s*\{[\s\S]*?return false")
        self.assertIn("O_NOFOLLOW", lock_body)
        self.assertIsNone(re.search(r"if fd < 0\s*\{\s*body\(\)", lock_body))

    def test_shutdown_uses_cocoa_deferred_termination_with_a_bound(self):
        self.assertIn("func applicationShouldTerminate", _SOURCE)
        self.assertIn("return .terminateLater", _SOURCE)
        self.assertIn("reply(toApplicationShouldTerminate: true)", _SOURCE)
        self.assertIn("drained.wait(timeout: .now() + 1.0)", _SOURCE)
        self.assertNotIn("registryQueue.sync {}", _SOURCE)

    def test_failed_registry_write_cannot_mutate_the_projection(self):
        save_body = _SOURCE.split("private func saveRunsRegistry", 1)[1].split(
            "private func scheduleRegistrySaveRetry", 1
        )[0]
        self.assertIn("if registrySaved {", save_body)
        self.assertLess(save_body.index("if registrySaved {"), save_body.index("activeRunPath"))

    def test_registry_retry_budget_is_bounded_and_logs_exhaustion(self):
        retry_body = _SOURCE.split("private func scheduleRegistrySaveRetry", 1)[1].split(
            "private func visibleRuns", 1
        )[0]
        self.assertIn("registrySaveRetryCount < 8", retry_body)
        self.assertIn("stopped registry retries", retry_body)
        self.assertIn("min(30.0", retry_body)

    def test_existing_invalid_registry_is_fail_closed(self):
        save_body = _SOURCE.split("private func saveRunsRegistry", 1)[1].split(
            "private func scheduleRegistrySaveRetry", 1
        )[0]
        self.assertIn("permanentFailure = true", save_body)
        self.assertIn("version == 1", save_body)
        self.assertIn("refused to overwrite an invalid registry", _SOURCE)

    def test_audit_id_matches_the_python_panels_presentation_rules(self):
        """client/stopper/panel.py's audit_id_text (Windows/Linux) and this file's auditIdText
        (macOS) are two independent implementations of the same presentation contract — nothing
        enforces they stay in sync (that gap is exactly how the macOS panel shipped without an
        audit-ID badge for months after panel.py grew one: commits 5156962 / a24d287 touched
        only panel.py). Lock the cap and the local/hidden rule here so a future edit to one side
        without the other fails a test instead of silently drifting again."""
        fn_body = _SOURCE.split("private func auditIdText", 1)[1].split(
            "private func auditIdLabel", 1
        )[0]
        self.assertIn('run["local"] as? Bool) == true { return ""', fn_body)
        self.assertIn("maxAuditIDLength", fn_body)
        self.assertIn('hasPrefix("aud_")', fn_body)
        self.assertIn("dropFirst(4)", fn_body)
        self.assertIn("private let maxAuditIDLength = 128", _SOURCE)
        # the label must actually be wired into the row, not just exist unused
        self.assertIn("let idText = auditIdText(run)", _SOURCE)
        self.assertIn("auditIdLabel(text:", _SOURCE)

    def test_de_lite_local_runs_match_python_panel_and_never_poll_hub(self):
        self.assertIn('run["local_surface"] as? String == "de_lite"', _SOURCE)
        self.assertIn('["DE_STOPPER_SINGLETON_LOCK"]', _SOURCE)
        self.assertIn("singletonLockPath as NSString", _SOURCE)
        self.assertIn('run["degrade_reason"]', _SOURCE)
        self.assertIn('run["ui_locale"]', _SOURCE)
        self.assertIn('MCP 不可用', _SOURCE)
        self.assertIn('MCP unavailable', _SOURCE)
        self.assertIn('DE Lite · 本地审核中', _SOURCE)
        self.assertIn('DE Lite · Local review in progress', _SOURCE)
        self.assertIn('DE Lite · 本地审核完成（仅供参考）', _SOURCE)
        self.assertIn('DE Lite · Local review completed (reference only)', _SOURCE)
        poll_body = _SOURCE.split("private func poll(runID: String)", 1)[1].split(
            "private func render()", 1
        )[0]
        self.assertIn("if isLocalRun", poll_body)
        depth_text = _SOURCE.split("private func depthLineText", 1)[1].split(
            "private func depthLineColor", 1
        )[0]
        de_lite_text = depth_text.split("if isDELiteRun(run)", 1)[1].split(
            "if isLocalRun(run)", 1
        )[0]
        terminal_text = {
            "completed": (
                'DE Lite · Local review completed (reference only)\\(suffix) · '
                '\\(elapsed) ✓',
                'DE Lite · 本地审核完成（仅供参考）\\(suffix) · \\(elapsed) ✓',
            ),
            "partial": (
                'DE Lite · Local review partially complete (reference only)\\(suffix) · '
                '\\(elapsed) ⚠',
                'DE Lite · 本地审核部分完成（仅供参考）\\(suffix) · \\(elapsed) ⚠',
            ),
            "failed": (
                'DE Lite · Local review failed\\(suffix) · \\(elapsed) ✗',
                'DE Lite · 本地审核失败\\(suffix) · \\(elapsed) ✗',
            ),
        }
        for status, (english, chinese) in terminal_text.items():
            with self.subTest(status=status):
                self.assertIn(english, de_lite_text)
                self.assertIn(chinese, de_lite_text)

        color_body = _SOURCE.split("private func depthLineColor", 1)[1].split(
            "private enum RowAction", 1
        )[0]
        local_colors = color_body.split("if isLocalRun(run)", 1)[1].split(
            "}", 1
        )[0]
        self.assertRegex(
            local_colors,
            r'case\s+"completed":\s*return\s+isDELiteRun\(run\)\s*\?\s*'
            r'paletteGreen\s*:\s*paletteAmber',
        )
        self.assertRegex(
            local_colors,
            r'case\s+"partial"(?:\s*,\s*"cancelled")?:\s*return\s+paletteAmber',
        )
        self.assertRegex(local_colors, r'case\s+"failed":\s*return\s+paletteRed')

    def test_native_row_renders_the_local_registry_title(self):
        detail_body = _SOURCE.split("private func makeRunDetailStack", 1)[1].split(
            "private func depthLabel", 1
        )[0]
        title_body = _SOURCE.split("private func runTitle", 1)[1].split(
            "private let maxAuditIDLength", 1
        )[0]
        self.assertIn("titleLabel(runTitle(run))", detail_body)
        self.assertIn('nonEmptyString(run["title"])', title_body)

    def test_native_stop_actions_and_depth_names_use_the_run_locale(self):
        self.assertIn("private func uiLocale", _SOURCE)
        self.assertIn("makeActionButton(action, locale: uiLocale(run))", _SOURCE)
        self.assertIn('locale == "en" ? "Fast" : "快速"', _SOURCE)
        self.assertIn('locale == "en" ? "Standard" : "标准"', _SOURCE)
        self.assertIn('locale == "en" ? "Deep" : "深度"', _SOURCE)
        self.assertIn('locale == "en" ? "Running" : "进行中"', _SOURCE)
        self.assertIn('locale == "en" ? "Finished" : "已完成"', _SOURCE)
        self.assertIn('private func makeActionButton(_ action: RowAction, locale: String)', _SOURCE)
        self.assertIn('case "completed":  return "DE Lite · Local review completed (reference only)', _SOURCE)
        self.assertIn('case "completed":  return "DE Lite · 本地审核完成（仅供参考）', _SOURCE)

    def test_native_locale_resolution_matches_the_python_bcp47_contract(self):
        """The AppKit panel cannot import Python, so pin the small shared locale contract here:
        run value first, host environment second, then the deterministic English default."""
        body = _SOURCE.split("private func uiLocale", 1)[1].split(
            "private func localReason", 1
        )[0]
        self.assertIn('nonEmptyString(run["ui_locale"])', body)
        self.assertIn('environment["DE_UI_LOCALE"]', body)
        self.assertIn('normalized == "en" || normalized.hasPrefix("en-")', body)
        self.assertIn('normalized == "zh" || normalized.hasPrefix("zh-")', body)
        self.assertIn('return "en"', body)

    def test_native_local_reason_labels_cover_every_degrade_boundary_in_both_locales(self):
        reason_body = _SOURCE.split("private func localReason", 1)[1].split(
            "/// Overall panel state", 1
        )[0]
        labels = {
            "unactivated": ("Unactivated", "未激活"),
            "subscription_expired": ("Subscription expired", "订阅到期"),
            "credits_exhausted": ("Insufficient credits", "积分不足"),
            "rate_limited": ("Rate limited", "服务限流"),
            "service_unavailable": ("Service unavailable", "服务不可用"),
            "mcp_unavailable": ("MCP unavailable", "MCP 不可用"),
        }
        for reason, (english, chinese) in labels.items():
            with self.subTest(reason=reason):
                self.assertIn('"%s": "%s"' % (reason, english), reason_body)
                self.assertIn('"%s": "%s"' % (reason, chinese), reason_body)

    def test_shipped_binary_contains_the_de_lite_product_surface(self):
        for path in _BINARIES:
            with self.subTest(binary=path.name):
                self.assertTrue(path.exists(), "%s must ship with the client" % path.name)
                binary = path.read_bytes()
                self.assertIn(
                    b"DE Lite",
                    binary,
                    "Swift source changed but %s was not rebuilt" % path.name,
                )
                self.assertIn(b"MCP unavailable", binary)
                self.assertIn("MCP 不可用".encode("utf-8"), binary)
                self.assertIn(b"Insufficient credits", binary)
                self.assertIn("积分不足".encode("utf-8"), binary)
                self.assertNotIn(b"Credits exhausted", binary)
                self.assertNotIn("积分已用完".encode("utf-8"), binary)

    def test_only_queued_runs_render_an_enabled_stop_action(self):
        row_body = _SOURCE.split("private func makeRunRow", 1)[1].split(
            "private func makeRunDetailStack", 1
        )[0]
        self.assertRegex(
            row_body,
            r'case\s+"queued":\s*action\s*=\s*serverVerifiedRunIDs\.contains\(runID\)'
            r'\s*\?\s*\.stop\s*:\s*\.checking',
        )
        self.assertIn('case "running":    action = .running', row_body)
        self.assertIn("case .running:", _SOURCE)
        self.assertIn('locale == "en" ? "Running" : "进行中"', _SOURCE)
        self.assertIn("serverVerifiedRunIDs.contains(runID)", row_body)
        self.assertIn("case .checking:", _SOURCE)

    def test_the_running_pill_is_red_and_still_disabled(self):
        """Parity with client/stopper/panel.py, which paints running #FFFFFF on FG_RED (#BC4936).
        The greyed-out fill made a live audit read as finished — a running row is the loudest thing
        the panel has to say.

        `enabled = false` is asserted in the same breath on purpose: the colour now matches STOP,
        and the server accepts cancellation only while queued, so the one thing this pill must not
        become is clickable. Colour is the state badge; the disabled flag is the contract."""
        pill = _SOURCE.split("private func makeActionButton", 1)[1].split("let button =", 1)[0]
        self.assertRegex(
            pill,
            r'case\s+\.running:\s*title\s*=\s*locale\s*==\s*"en"\s*\?\s*"Running"\s*:\s*"进行中";\s*fill\s*=\s*paletteRed;'
            r'\s*fg\s*=\s*\.white;\s*enabled\s*=\s*false',
        )
        # …and STOP is the only enabled pill, so red never means "clickable" on its own.
        self.assertEqual(pill.count("enabled = true"), 1)
        self.assertRegex(pill, r'case\s+\.stop:.*enabled\s*=\s*true')

    def test_the_running_red_matches_the_python_panel(self):
        """One warm red across both panels. panel.py names it FG_RED = #BC4936; Swift builds it
        from components, so the two can drift silently unless something compares them."""
        import re
        match = re.search(r"paletteRed\s*=\s*color\((\d+),\s*(\d+),\s*(\d+)\)", _SOURCE)
        self.assertIsNotNone(match, "paletteRed is no longer declared from RGB components")
        swift_rgb = tuple(int(g) for g in match.groups())
        from client.stopper import panel
        python_rgb = tuple(int(panel.FG_RED[i:i + 2], 16) for i in (1, 3, 5))
        self.assertEqual(swift_rgb, python_rgb,
                         "the macOS red drifted from panel.FG_RED (%s)" % panel.FG_RED)

    def test_the_menu_bar_icon_is_a_template_sized_by_its_own_aspect(self):
        """Two separate defects in one three-line block, both visible in the menu bar.

        `isTemplate = false` drew our green literally, so the icon sat among the system extras
        looking like a stray sticker; a template image is drawn from its alpha alone and tinted by
        AppKit, white on a dark bar and black on a light one, which is what every neighbour does.

        `NSSize(width: 18, height: 18)` squashed a 1.6:1 image into a square. A menu bar rations
        height and lets width run, so the width must follow the mark's own aspect — that is what
        lets the framing braces ride along for free."""
        icon = _SOURCE.split("private func statusIconImage", 1)[1].split("private func", 1)[0]
        self.assertIn("image.isTemplate = true", icon)
        self.assertNotIn("isTemplate = false", icon)
        self.assertRegex(icon, r"image\.size\s*=\s*NSSize\(\s*width:\s*menuBarIconHeight\s*\*\s*ratio")
        self.assertNotRegex(icon, r"NSSize\(width:\s*18,\s*height:\s*18\)")

    def test_cancel_response_is_authoritative_and_terminal_states_are_absorbing(self):
        stop_body = _SOURCE.split("private func stopAudit", 1)[1].split(
            "@objc private func quit", 1
        )[0]
        self.assertIn('(run["status"] as? String) == "queued"', stop_body)
        self.assertIn("serverVerifiedRunIDs.contains(runID)", stop_body)
        self.assertIn("cancelRequestsInFlight.insert(runID)", stop_body)
        self.assertIn('payload?["stopped"] as? Bool', stop_body)
        self.assertIn('currentStatus == "cancelling"', stop_body)
        self.assertIn("cancelRequestsInFlight.remove(runID)", stop_body)
        self.assertIn("serverVerifiedRunIDs.remove(runID)", stop_body)
        self.assertIn("stateGenerations[runID, default: 0] += 1", stop_body)
        self.assertIn("let httpOK =", stop_body)
        self.assertIn('updated["status"] = "unknown"', stop_body)
        before_request = stop_body.split("URLSession.shared.dataTask", 1)[0]
        self.assertNotIn("saveRunsRegistry()", before_request)

    def test_poll_preserves_cancelling_only_while_its_request_is_inflight(self):
        poll_body = _SOURCE.split("private func poll", 1)[1].split(
            "private func render", 1
        )[0]
        self.assertIn("cancelRequestsInFlight.contains(runID)", poll_body)
        self.assertIn("serverVerifiedRunIDs.insert(runID)", poll_body)
        self.assertIn("let expectedGeneration = stateGenerations", poll_body)
        guard_at = poll_body.index("self.stateGenerations[runID, default: 0] == expectedGeneration")
        verify_at = poll_body.index("self.serverVerifiedRunIDs.insert(runID)")
        self.assertLess(guard_at, verify_at)
        self.assertIn('updated["status"] as? String) == "cancelling"', poll_body)



class SwiftDebugAuditorDisplayTests(unittest.TestCase):
    def test_debug_authorized_gates_model_rows_and_row_height(self):
        self.assertIn("private func debugAuditorRows", _SOURCE)
        self.assertIn("run[\"debug_authorized\"] as? Bool) == true", _SOURCE)
        self.assertIn("let debugRows = debugAuditorRows(run).count", _SOURCE)
        self.assertIn("for rowText in debugAuditorRows(run)", _SOURCE)
        self.assertIn("return auditors.map { auditorDisplayText($0) }", _SOURCE)

if __name__ == "__main__":
    unittest.main()
