"""The macOS stop panel's Swift source must compile, and its committed binary must not lag it.

Neither was true. `desktop/macos/DecisionEngineStopper.swift` referenced three file-scope globals
(`terminalStatuses`, `finishedLingerSeconds`) through `self.`, which does not compile — so the file
had been edited and committed without ever being built. The binary beside it was three weeks older,
which meant the change that introduced the break (`c160746`, queued-only cancellation) had never
shipped: the panel users actually ran did not contain it, and nothing in the suite said so.

Nothing here builds a product artefact. The first test type-checks the source; the second compares
git commit dates. Both skip where their tool is absent, so a Linux CI box or a tarball checkout is
unaffected.

Run:  python3 -m unittest installer.tests.test_macos_stopper_build
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "desktop" / "macos" / "DecisionEngineStopper.swift"
BINARY_ARM64 = REPO / "desktop" / "macos" / "bin" / "decision-engine-stopper-arm64"
BINARY_X86_64 = REPO / "desktop" / "macos" / "bin" / "decision-engine-stopper-x86_64"


def _macho_cpu_type(path: Path) -> int:
    with path.open("rb") as handle:
        header = handle.read(8)
    if len(header) != 8:
        raise AssertionError("Stopper binary has no complete Mach-O header: %s" % path)
    magic, cpu_type = struct.unpack("<II", header)
    if magic != 0xFEEDFACF:
        raise AssertionError("Stopper binary is not a little-endian 64-bit Mach-O: %s" % path)
    return cpu_type


def _last_commit_epoch(path: Path) -> int:
    """Committer date of the newest commit touching `path`, or 0 when git cannot say."""
    done = subprocess.run(["git", "log", "-1", "--format=%ct", "--", str(path)],
                          cwd=REPO, capture_output=True, text=True, timeout=60)
    out = done.stdout.strip()
    return int(out) if done.returncode == 0 and out.isdigit() else 0


class SwiftSourceCompiles(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin",
                         "Native model-row regression requires macOS")
    def test_native_model_rows_show_elapsed_and_freeze_terminal_durations(self):
        source = SOURCE.read_text(encoding="utf-8").split("let app = NSApplication.shared", 1)[0]
        harness = r'''
extension DecisionEngineStopper {
    func verifyModelRows() {
        let failed: [String: Any] = ["model_id": "model-a", "status": "failed",
            "started_at": 940.0, "completed_at": 1000.0]
        var run: [String: Any] = ["run_id": "test-run", "auditors": [failed]]
        precondition(debugAuditorRows(run).isEmpty, "Unauthorized rows leaked")
        run["debug_authorized"] = false
        precondition(debugAuditorRows(run).isEmpty)
        run["debug_authorized"] = true
        precondition(debugAuditorRows(run) == ["model-a · failed · 1m0s ×"])
        let completed: [String: Any] = ["model_id": "model-b", "status": "completed",
            "duration_ms": 12000]
        run["auditors"] = [failed, completed]
        precondition(debugAuditorRows(run) == ["model-a · failed · 1m0s ×",
            "model-b · completed · 12s ✓"])
        for status in ["completed", "failed"] {
            let runID = "legacy-\(status)"
            let cacheKey = "\(runID):0:legacy"
            let before = Date().timeIntervalSince1970
            let started = before - 60.0
            var legacy: [String: Any] = ["model_id": "legacy", "status": status,
                "started_at": started]
            let first = auditorElapsed(legacy, runID: runID, index: 0)
            let after = Date().timeIntervalSince1970
            let frozen = finishedElapsedByAuditor[cacheKey]!
            precondition(frozen >= before - started && frozen <= after - started)
            precondition(first == formatElapsed(frozen))
            // A fixed cached duration distinguishes reuse from recomputing wall time.
            finishedElapsedByAuditor[cacheKey] = 7.0
            precondition(auditorElapsed(legacy, runID: runID, index: 0) == "7s")
            legacy["status"] = "running"
            _ = auditorElapsed(legacy, runID: runID, index: 0)
            precondition(finishedElapsedByAuditor[cacheKey] == nil)
        }
        let pending: [String: Any] = ["model_id": "model-c", "status": "pending"]
        run["auditors"] = [pending]
        precondition(debugAuditorRows(run) == ["model-c · pending · 0s"])
        print("Native model rows OK")
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
DecisionEngineStopper().verifyModelRows()
'''
        with tempfile.TemporaryDirectory() as directory:
            test_source = Path(directory) / "main.swift"
            binary = Path(directory) / "model-row-test"
            test_source.write_text(source + harness, encoding="utf-8")
            built = subprocess.run(["xcrun", "swiftc", str(test_source), "-o", str(binary)],
                                   capture_output=True, text=True, encoding="utf-8", timeout=120)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True,
                                    encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Native model rows OK", result.stdout)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"),
                         "AppKit scrolling regression requires macOS + swiftc")
    def test_many_tasks_scroll_refresh_and_shrink_in_appkit(self):
        # Compile the real panel with a same-file extension; do not start its polling/registry loop.
        source = SOURCE.read_text(encoding="utf-8").split("let app = NSApplication.shared", 1)[0]
        harness = r'''
extension DecisionEngineStopper {
    func verifyTaskScrolling() {
        ensurePanel()
        for i in 0..<30 {
            let id = "scroll-\(i)"
            runsByID[id] = ["run_id": id, "title": "Task \(i)",
                            "status": "queued", "created_at": Double(100 + i)]
        }
        updatePanel()
        let window = panelWindow!
        window.contentView!.layoutSubtreeIfNeeded()
        guard let scroll = window.contentView!.subviews.compactMap({ $0 as? NSScrollView }).first,
              let document = scroll.documentView else { fatalError("Tasks have no scroll container") }
        precondition(window.frame.height <= window.screen!.visibleFrame.height)
        precondition(document.frame.height > scroll.contentSize.height)
        precondition(document.isFlipped && scroll.contentView.bounds.minY == 0)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 500))
        scroll.reflectScrolledClipView(scroll.contentView)
        let offset = scroll.contentView.bounds.minY
        updatePanel()
        precondition(abs(scroll.contentView.bounds.minY - offset) < 1, "Refresh jumped")
        let bottom = document.frame.height - scroll.contentSize.height
        scroll.contentView.scroll(to: NSPoint(x: 0, y: bottom))
        scroll.reflectScrolledClipView(scroll.contentView)
        let last = rowsStack!.arrangedSubviews.last!
        let lastRect = last.convert(last.bounds, to: document)
        precondition(scroll.documentVisibleRect.insetBy(dx: -1, dy: -1).contains(lastRect))
        window.setContentSize(NSSize(width: 320, height: 240))
        window.contentView!.layoutSubtreeIfNeeded()
        precondition(abs(document.frame.width - scroll.contentSize.width) < 1)
        runsByID = ["scroll-0": runsByID["scroll-0"]!]
        updatePanel()
        precondition(scroll.contentView.bounds.minY == 0, "Shrink left a blank viewport")
        precondition(scroll.documentVisibleRect.intersects(document.bounds))
        runsByID.removeAll()
        updatePanel()
        precondition(scroll.contentView.bounds.minY == 0)
        precondition(rowsStack!.arrangedSubviews.count == 1)
        window.close()
        print("AppKit scrolling OK")
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
DecisionEngineStopper().verifyTaskScrolling()
'''
        with tempfile.TemporaryDirectory() as directory:
            test_source = Path(directory) / "main.swift"
            binary = Path(directory) / "scroll-test"
            test_source.write_text(source + harness, encoding="utf-8")
            built = subprocess.run(["swiftc", str(test_source), "-o", str(binary)],
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("AppKit scrolling OK", result.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "swiftc + AppKit are macOS-only")
    @unittest.skipUnless(shutil.which("swiftc"), "swiftc is not installed")
    def test_the_source_type_checks(self):
        """`-typecheck`, not a full build: it catches this exact class of defect in seconds and
        leaves no artefact, so running it in the suite cannot be mistaken for producing a release."""
        for target in ("arm64-apple-macos11", "x86_64-apple-macos11"):
            with self.subTest(target=target):
                done = subprocess.run(
                    ["swiftc", "-typecheck", "-target", target, str(SOURCE)],
                    cwd=REPO, capture_output=True, text=True, timeout=600)
                self.assertEqual(
                    done.returncode,
                    0,
                    "DecisionEngineStopper.swift does not compile for %s:\n%s"
                    % (target, done.stderr[-3000:]),
                )


class ShippedBinaryIsNotStale(unittest.TestCase):
    def test_shipped_binaries_cover_apple_silicon_and_intel(self):
        self.assertEqual(_macho_cpu_type(BINARY_ARM64), 0x0100000C)
        self.assertEqual(_macho_cpu_type(BINARY_X86_64), 0x01000007)

    @unittest.skipUnless(sys.platform == "darwin", "codesign is macOS-only")
    @unittest.skipUnless(shutil.which("codesign"), "codesign is not installed")
    def test_shipped_binaries_have_valid_signatures(self):
        for binary in (BINARY_ARM64, BINARY_X86_64):
            with self.subTest(binary=binary.name):
                done = subprocess.run(
                    ["codesign", "--verify", "--strict", str(binary)],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    done.returncode,
                    0,
                    "%s has no valid code signature:\n%s" % (binary.name, done.stderr),
                )

    def test_the_binary_is_no_older_than_its_source(self):
        """The invariant is 'edit the Swift, rebuild the binary'. Committed dates are the only
        record of that here — nothing rebuilds on a user's machine, and the binary is what ships,
        so source changes that never reach it are invisible improvements at best and, as happened,
        a fix everyone believes is live while the panel has never contained it.

        Rebuild both signed architecture slices with:
            bash desktop/macos/build_stopper_binaries.sh
        """
        if not shutil.which("git") or not (REPO / ".git").exists():
            self.skipTest("not a git checkout")
        source_at = _last_commit_epoch(SOURCE)
        for binary in (BINARY_ARM64, BINARY_X86_64):
            with self.subTest(binary=binary.name):
                binary_at = _last_commit_epoch(binary)
                if not source_at or not binary_at:
                    self.skipTest("git has no history for source or %s" % binary.name)
                self.assertGreaterEqual(
                    binary_at, source_at,
                    "DecisionEngineStopper.swift was committed after %s was last rebuilt — the "
                    "shipped panel does not contain the newer source. Rebuild and commit both "
                    "binaries." % binary.name)


if __name__ == "__main__":
    unittest.main()
