import AppKit
import Foundation
import Darwin

let terminalStatuses: Set<String> = ["completed", "partial", "failed", "cancelled"]
let finishedLingerSeconds: TimeInterval = 30
let idleExitSeconds: TimeInterval = 2

func homePath(_ suffix: String) -> String {
    return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(suffix).path
}

func environmentPathList(_ key: String, fallback: [String]) -> [String]? {
    let env = ProcessInfo.processInfo.environment
    guard let raw = env[key] else { return fallback }
    guard let data = raw.data(using: .utf8),
          let object = try? JSONSerialization.jsonObject(with: data),
          let paths = object as? [String],
          !paths.isEmpty,
          paths.allSatisfy({ !$0.isEmpty }) else {
        return nil
    }
    return paths
}

func readJSON(path: String) -> [String: Any]? {
    guard let data = FileManager.default.contents(atPath: path) else { return nil }
    return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
}

@discardableResult
func writeJSON(path: String, object: [String: Any]) -> Bool {
    guard let data = try? JSONSerialization.data(
        withJSONObject: object, options: [.prettyPrinted, .sortedKeys]
    ) else {
        return false
    }
    do {
        try data.write(to: URL(fileURLWithPath: path), options: [.atomic])
        return true
    } catch {
        return false
    }
}

func deleteFile(path: String) {
    try? FileManager.default.removeItem(atPath: path)
}

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> NSColor {
    return NSColor(calibratedRed: red / 255, green: green / 255, blue: blue / 255, alpha: 1)
}

private final class TaskListDocumentView: NSView {
    override var isFlipped: Bool { true }
}

final class DecisionEngineStopper: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let activeRunPath = ProcessInfo.processInfo.environment["DE_ACTIVE_RUN"]
        ?? homePath(".deeppattern/decision-engine/active-run.json")
    private let activeRunsPath = ProcessInfo.processInfo.environment["DE_ACTIVE_RUNS"]
        ?? homePath(".deeppattern/decision-engine/active-runs.json")
    // Direct runner launches provide the exact dual-write and dual-lock sets. A standalone
    // LaunchAgent/.app start has no runtime override and safely uses the legacy copy/lock.
    // A partial injected environment is rejected at launch rather than guessing and mis-locking.
    private let activeRunsPaths = environmentPathList(
        "DE_ACTIVE_RUNS_WRITE_PATHS",
        fallback: [homePath(".deeppattern/decision-engine/active-runs.json")]
    )
    private let activeRunPaths = environmentPathList(
        "DE_ACTIVE_RUN_WRITE_PATHS",
        fallback: [homePath(".deeppattern/decision-engine/active-run.json")]
    )
    private let activeRunsLockPaths = environmentPathList(
        "DE_ACTIVE_RUNS_LOCK_PATHS",
        fallback: [homePath(".deeppattern/decision-engine/active-runs.json.lock")]
    )
    private let configPath = ProcessInfo.processInfo.environment["DE_CONFIG_PATH"]
        ?? homePath(".deeppattern/decision-engine/config.json")
    private let uiStatePath = ProcessInfo.processInfo.environment["DE_STOPPER_UI_STATE"]
        ?? homePath(".deeppattern/decision-engine/stopper-ui.json")
    private let singletonLockPath = ProcessInfo.processInfo.environment["DE_STOPPER_SINGLETON_LOCK"]
        ?? homePath(".decision-engine/stopper.lock")
    private var timer: Timer?
    private var runsByID: [String: [String: Any]] = [:]
    private var pollingRunIDs: Set<String> = []
    private var cancelRequestsInFlight: Set<String> = []
    // Registry data discovers rows but is not authoritative. A queued row becomes cancellable only
    // after this process has observed it from the hub, so restart cannot revive a stale STOP button.
    private var serverVerifiedRunIDs: Set<String> = []
    // A cancel transition bumps this generation so an older GET cannot overwrite its outcome.
    private var stateGenerations: [String: Int] = [:]
    private var lastPollStartedAt: [String: TimeInterval] = [:]
    // HTTP codes that mean "this run is definitively gone" (not a transient blip) — mirrors
    // client/stopper/panel.py's `_GONE_STATUS_CODES`.
    private let goneStatusCodes: Set<Int> = [404, 410]
    // How many CONSECUTIVE hub gone-replies retire a run — mirrors panel.py's
    // NOT_FOUND_FORGET_THRESHOLD. Grace for the brief post-submit window where the hub can 404
    // before it has registered the run; a real orphan (e.g. a stuck `cancelling` the hub no
    // longer recognizes) has been gone for far longer than 3 one-second polls.
    private let notFoundForgetThreshold = 3
    private let scrollMaxScreenFraction: CGFloat = 0.618
    private var notFoundPolls: [String: Int] = [:]   // run_id → consecutive hub gone-replies
    private var autoShownRunIDs: Set<String> = []
    private var hiddenByUser = false
    private var noAuditItemsSince: TimeInterval?
    private var finishedElapsedByRun: [String: TimeInterval] = [:]   // freeze elapsed once a run is terminal
    private var panelWindow: NSPanel?
    private var rowsStack: NSStackView?
    private var rowsScrollView: NSScrollView?
    private var lockFD: Int32 = -1
    // Run ids this panel has finished showing (hidden_after passed). Mirrors client/stopper/panel.py's
    // `_retired`: a reaped run must be dropped from the on-disk registry AND never re-adopted from a
    // stale on-disk copy, or the finished row comes straight back on the next merge.
    private var reapedRunIDs: Set<String> = []
    private var registrySaveRetryCount = 0
    private var registrySaveRetryScheduled = false
    private var terminationReplyPending = false
    // Serial queue for the LOCKED registry writes. The shared active-runs lock is BLOCKING and the
    // Python runner holds it across a submit, so the write must never run on the main thread — a frozen
    // panel would be a worse bug than a slow reap (the same reason client/stopper/panel.py reaps off
    // the Tk thread).
    private let registryQueue = DispatchQueue(label: "com.deeppattern.stopper.registry")

    // Dark palette matching the reference A-repo stopper (stopper_app.py: BG_MAIN #262624,
    // FG_META #8E8D86, green/amber/red voice states) so the online client shows the SAME chrome.
    private let paletteBg = color(38, 38, 36)         // #262624 — panel background (A-repo BG_MAIN)
    private let paletteRow = color(38, 38, 36)        // rows share the panel bg (no white cards)
    private let paletteText = color(245, 245, 244)    // near-white title on dark
    private let paletteMuted = color(142, 141, 134)   // #8E8D86 — A-repo FG_META ("depth • elapsed")
    private let menuBarIconHeight: CGFloat = 18   // the conventional macOS menu-bar extra height
    private let paletteRed = color(188, 73, 54)       // #BC4936 — A-repo BTN_RED / FG_RED_FAIL (warm Claude red)
    private let paletteFinished = color(58, 58, 55)   // finished button fill on dark
    private let paletteBlue = color(120, 150, 255)    // running accent, lifted for dark bg
    private let paletteGreen = color(52, 199, 89)     // completed ✓ (A-repo FG_GREEN)
    private let paletteAmber = color(199, 161, 79)    // #C7A14F — A-repo FG_YELLOW_FB (partial + cancelled)

    func applicationDidFinishLaunching(_ notification: Notification) {
        let env = ProcessInfo.processInfo.environment
        if activeRunsPaths == nil || activeRunPaths == nil || activeRunsLockPaths == nil
            || (env["DE_ACTIVE_RUNS"] != nil && env["DE_ACTIVE_RUNS_LOCK_PATHS"] == nil) {
            NSLog("Decision Engine stopper refused a partial/invalid runtime path environment")
            NSApp.terminate(nil)
            return
        }
        if !acquireSingleInstanceLock() {
            NSApp.terminate(nil)
            return
        }
        NSApp.setActivationPolicy(.accessory)
        configureStatusButton()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    private func configureStatusButton() {
        guard let button = statusItem.button else { return }
        statusItem.length = NSStatusItem.variableLength
        button.title = ""
        button.imagePosition = .imageOnly
        button.imageScaling = .scaleProportionallyDown
        button.image = statusIconImage()
        button.contentTintColor = nil
        button.target = self
        button.action = #selector(statusItemClicked)
        button.toolTip = auditSurfaceTitle(locale: uiLocale([:]))
    }

    private func acquireSingleInstanceLock() -> Bool {
        let supportDir = (singletonLockPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(
            at: URL(fileURLWithPath: supportDir),
            withIntermediateDirectories: true
        )
        lockFD = Darwin.open(singletonLockPath, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        if lockFD < 0 {
            return true
        }
        if flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
            Darwin.close(lockFD)
            lockFD = -1
            return false
        }
        ftruncate(lockFD, 0)
        let pid = "\(ProcessInfo.processInfo.processIdentifier)\n"
        if let data = pid.data(using: .utf8) {
            data.withUnsafeBytes { bytes in
                if let base = bytes.baseAddress {
                    _ = Darwin.write(lockFD, base, bytes.count)
                }
            }
        }
        return true
    }

    @objc private func refresh() {
        mergeRunsFromDisk()
        pruneExpiredRuns()
        for run in visibleRuns() where isActive(run) {
            if let runID = run["run_id"] as? String, !runID.isEmpty {
                poll(runID: runID)
            }
        }
        render()
    }

    private func mergeRunsFromDisk() {
        var merged: [String: [String: Any]] = [:]
        let now = Date().timeIntervalSince1970

        if let registry = readJSON(path: activeRunsPath),
           let runs = registry["runs"] as? [String: Any] {
            for (runID, value) in runs {
                if var run = value as? [String: Any] {
                    run["run_id"] = (run["run_id"] as? String) ?? runID
                    merged[runID] = run
                }
            }
        }

        if let legacy = readJSON(path: activeRunPath),
           let runID = legacy["run_id"] as? String,
           !runID.isEmpty {
            merged[runID] = legacy
        }

        for (runID, run) in runsByID {
            let hiddenAfter = run["hidden_after"] as? Double ?? 0
            if terminalStatuses.contains((run["status"] as? String) ?? "") && hiddenAfter > now {
                merged[runID] = merged[runID] ?? run
            }
        }

        // Never re-adopt a run we already reaped: its on-disk entry may briefly outlive the async
        // locked persist that drops it, and re-merging it would resurrect the finished row (the
        // client-side counterpart of the A-repo panel not adopting a dead-owner state file).
        for runID in reapedRunIDs {
            merged.removeValue(forKey: runID)
        }

        // Freeze a stable ordering key the first time we see each run and never rewrite it. `updated_at`
        // is stamped on every poll (see poll()), so ordering rows by it makes two concurrent audits —
        // which poll in nondeterministic order — swap places every tick. Snapshotting submission/start
        // time here keeps the rows put. Mirrors client/stopper/panel.py's `_sort_at`.
        for runID in Array(merged.keys) {
            if var run = merged[runID], numeric(run["_sort_at"]) <= 0 {
                run["_sort_at"] = firstPositiveTimestamp(run)
                merged[runID] = run
            }
        }

        runsByID = merged
    }

    /// First present, positive of started_at / created_at / updated_at — the seed order of a run before
    /// it has been polled. Used only to snapshot `_sort_at` once; after that the frozen value is used.
    private func firstPositiveTimestamp(_ run: [String: Any]) -> Double {
        for key in ["started_at", "created_at", "updated_at"] {
            let value = numeric(run[key])
            if value > 0 { return value }
        }
        return 0
    }

    /// A definitive "gone" reply (404/410 — the hub purged the run, never registered it, or no
    /// longer recognizes it after a cancel) is NOT a transient blip: such a run can never reach a
    /// terminal status through `poll()`, so the sticky `cancelling` guard there would otherwise
    /// hold the row forever (this is what left a stopped audit reading "停止中" indefinitely).
    /// After `notFoundForgetThreshold` consecutive gone-replies, retire the run exactly like a
    /// finished run whose linger expired: drop it from memory and reap it off disk so a later
    /// `mergeRunsFromDisk` never re-adopts it. Mirrors client/stopper/panel.py's
    /// `_on_run_vanished` / NOT_FOUND_FORGET_THRESHOLD — this client had no equivalent before.
    private func handleRunVanished(runID: String) {
        let count = (notFoundPolls[runID] ?? 0) + 1
        notFoundPolls[runID] = count
        guard count >= notFoundForgetThreshold else { return }
        notFoundPolls.removeValue(forKey: runID)
        cancelRequestsInFlight.remove(runID)
        serverVerifiedRunIDs.remove(runID)
        stateGenerations.removeValue(forKey: runID)
        runsByID.removeValue(forKey: runID)
        reapedRunIDs.insert(runID)
        saveRunsRegistry()
    }

    private func pruneExpiredRuns() {
        let now = Date().timeIntervalSince1970
        var reapedAny = false
        for (runID, run) in runsByID {
            if let hiddenAfter = run["hidden_after"] as? Double, hiddenAfter <= now {
                runsByID.removeValue(forKey: runID)
                cancelRequestsInFlight.remove(runID)
                serverVerifiedRunIDs.remove(runID)
                stateGenerations.removeValue(forKey: runID)
                reapedRunIDs.insert(runID)          // remember so the locked persist drops it from disk
                reapedAny = true
            }
        }
        // Reaping only mutates memory; push the removal to disk so the entry's SOURCE goes away and it
        // cannot be re-adopted on the next start (mirrors panel.py -> runner.forget_active_run).
        if reapedAny {
            saveRunsRegistry()
        }
    }

    /// Run `body` holding the exact shared cross-process lock set exported by the runner.
    /// Lock failures are fail-closed: never perform an unlocked read-modify-write.
    /// EINTR is retried. MUST be called off the main thread (see registryQueue).
    private func withRegistryLocks(_ body: () -> Bool) -> Bool {
        guard let paths = activeRunsLockPaths else { return false }
        var descriptors: [Int32] = []
        for path in paths {
            let dir = (path as NSString).deletingLastPathComponent
            do {
                try FileManager.default.createDirectory(
                    atPath: dir, withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700])
            } catch {
                for fd in descriptors.reversed() {
                    _ = flock(fd, LOCK_UN)
                    Darwin.close(fd)
                }
                return false
            }
            var fd: Int32
            repeat {
                fd = Darwin.open(path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, S_IRUSR | S_IWUSR)
            } while fd < 0 && errno == EINTR
            if fd < 0 {
                for held in descriptors.reversed() {
                    _ = flock(held, LOCK_UN)
                    Darwin.close(held)
                }
                return false
            }
            var lockResult: Int32
            repeat {
                lockResult = flock(fd, LOCK_EX)
            } while lockResult != 0 && errno == EINTR
            if lockResult != 0 {
                Darwin.close(fd)
                for held in descriptors.reversed() {
                    _ = flock(held, LOCK_UN)
                    Darwin.close(held)
                }
                return false
            }
            descriptors.append(fd)
        }
        defer {
            for fd in descriptors.reversed() {
                _ = flock(fd, LOCK_UN)
                Darwin.close(fd)
            }
        }
        return body()
    }

    /// The newest ACTIVE run in the authoritative merged registry, used for the legacy
    /// `active-run.json` compatibility projection. Pure: reads only the passed-in dictionaries.
    private func latestActiveRun(from runs: [String: [String: Any]]) -> [String: Any]? {
        return runs.values.filter { isActive($0) }.sorted { left, right in
            let leftSort = sortTimestamp(left)
            let rightSort = sortTimestamp(right)
            if leftSort != rightSort { return leftSort > rightSort }
            return ((left["run_id"] as? String) ?? "") < ((right["run_id"] as? String) ?? "")
        }.first
    }

    /// Persist the panel's view to the shared registry as a LOCKED read-modify-write (off the main
    /// thread). Re-reads the registry under the lock and MERGES — so a run the runner submitted since
    /// our last poll is preserved instead of clobbered — then overlays the panel's fresher per-run
    /// state and drops everything we've reaped. This closes the lost-update race the old blind
    /// whole-`runsByID` write had against the runner (the client-side counterpart of the A-repo
    /// `_STATE_WRITE_LOCK` fix).
    private func saveRunsRegistry() {
        let snapshot = runsByID
        let reaped = reapedRunIDs
        registryQueue.async { [weak self] in
            guard let self = self else { return }
            var permanentFailure = false
            let saved = self.withRegistryLocks {
                var merged: [String: [String: Any]] = [:]
                let registryExists = FileManager.default.fileExists(atPath: self.activeRunsPath)
                let registry = readJSON(path: self.activeRunsPath)
                guard !registryExists || registry != nil else {
                    permanentFailure = true
                    return false
                }
                var registryObject = registry ?? ["schema_version": 1, "runs": [:]]
                if let registry = registry {
                    if let schema = registry["schema_version"] {
                        guard let version = schema as? Int, version == 1 else {
                            permanentFailure = true
                            return false
                        }
                    }
                    guard let runs = registry["runs"] as? [String: Any] else {
                        permanentFailure = true
                        return false
                    }
                    for (runID, value) in runs {
                        guard let run = value as? [String: Any] else {
                            permanentFailure = true
                            return false
                        }
                        merged[runID] = run
                    }
                }
                for runID in reaped {                       // reaped by this panel → drop from disk
                    merged.removeValue(forKey: runID)
                }
                for (runID, run) in snapshot where !reaped.contains(runID) {
                    guard let diskRun = merged[runID] else {
                        merged[runID] = run
                        continue
                    }
                    let diskTerminal = terminalStatuses.contains((diskRun["status"] as? String) ?? "")
                    let snapshotTerminal = terminalStatuses.contains((run["status"] as? String) ?? "")
                    if diskTerminal && !snapshotTerminal {
                        continue
                    }
                    if snapshotTerminal && !diskTerminal {
                        merged[runID] = run
                        continue
                    }
                    let diskUpdated = self.numeric(diskRun["updated_at"])
                    let snapshotUpdated = self.numeric(run["updated_at"])
                    if snapshotUpdated > diskUpdated {
                        merged[runID] = run
                    }
                }
                registryObject["schema_version"] = registryObject["schema_version"] ?? 1
                registryObject["runs"] = merged
                registryObject["updated_at"] = Date().timeIntervalSince1970
                var registrySaved = true
                for path in self.activeRunsPaths ?? [] {
                    if !writeJSON(path: path, object: registryObject) {
                        registrySaved = false
                    }
                }
                if registrySaved {
                    // Compatibility projection is best-effort and only follows an authoritative
                    // registry commit; never mutate it after a failed registry write.
                    if let latest = self.latestActiveRun(from: merged) {
                        for path in self.activeRunPaths ?? [] {
                            writeJSON(path: path, object: latest)
                        }
                    } else {
                        for path in self.activeRunPaths ?? [] {
                            deleteFile(path: path)
                        }
                    }
                }
                return registrySaved
            }
            DispatchQueue.main.async {
                if saved {
                    self.reapedRunIDs.subtract(reaped)
                    self.registrySaveRetryCount = 0
                } else if permanentFailure {
                    self.registrySaveRetryScheduled = false
                    NSLog("Decision Engine stopper refused to overwrite an invalid registry")
                } else {
                    self.scheduleRegistrySaveRetry()
                }
            }
        }
    }

    private func scheduleRegistrySaveRetry() {
        guard !registrySaveRetryScheduled else { return }
        guard registrySaveRetryCount < 8 else {
            NSLog("Decision Engine stopper stopped registry retries after repeated I/O failures")
            return
        }
        registrySaveRetryScheduled = true
        let exponent = min(registrySaveRetryCount, 6)
        let delay = min(30.0, 0.5 * Double(1 << exponent))
        registrySaveRetryCount += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self else { return }
            self.registrySaveRetryScheduled = false
            self.saveRunsRegistry()
        }
    }

    private func visibleRuns() -> [[String: Any]] {
        return runsByID.values.sorted { left, right in
            let leftActive = isActive(left)
            let rightActive = isActive(right)
            if leftActive != rightActive {
                return leftActive && !rightActive
            }
            // Order by the FROZEN snapshot, never by the live `updated_at` (a poll rewrites it every
            // second, so ties between two active runs flipped each tick and the rows jumped).
            let leftSort = sortTimestamp(left)
            let rightSort = sortTimestamp(right)
            if leftSort != rightSort {
                return leftSort > rightSort
            }
            // Final stable tie-breaker so equal timestamps (e.g. both seeded before their first poll)
            // still yield a deterministic order — the registry id is authoritative identity.
            return ((left["run_id"] as? String) ?? "") < ((right["run_id"] as? String) ?? "")
        }
    }

    /// The frozen ordering key (see mergeRunsFromDisk); falls back to the seed timestamps only if the
    /// snapshot has not been taken yet, so ordering is stable from the very first render.
    private func sortTimestamp(_ run: [String: Any]) -> Double {
        let snapshot = numeric(run["_sort_at"])
        return snapshot > 0 ? snapshot : firstPositiveTimestamp(run)
    }

    private func isActive(_ run: [String: Any]) -> Bool {
        let status = (run["status"] as? String) ?? ""
        let runID = (run["run_id"] as? String) ?? ""
        return !runID.isEmpty && !terminalStatuses.contains(status)
    }

    private func isLocalRun(_ run: [String: Any]) -> Bool {
        return (run["local"] as? Bool) == true
    }

    private func isDELiteRun(_ run: [String: Any]) -> Bool {
        return isLocalRun(run) && run["local_surface"] as? String == "de_lite"
    }

    private func poll(runID: String) {
        if let run = runsByID[runID] {
            if isLocalRun(run) {
                return
            }
        }
        if pollingRunIDs.contains(runID) {
            if Date().timeIntervalSince1970 - (lastPollStartedAt[runID] ?? 0) < 30 {
                return
            }
            pollingRunIDs.remove(runID)
        }
        guard let config = readJSON(path: configPath),
              let serverURL = config["server_endpoint"] as? String,
              let token = config["access_token"] as? String,
              let url = URL(string: serverURL + "/v1/audits/" + runID) else {
            return
        }

        let expectedGeneration = stateGenerations[runID, default: 0]
        pollingRunIDs.insert(runID)
        lastPollStartedAt[runID] = Date().timeIntervalSince1970
        var request = URLRequest(url: url, timeoutInterval: 20)
        request.httpMethod = "GET"
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        URLSession.shared.dataTask(with: request) { data, response, _ in
            if let http = response as? HTTPURLResponse, self.goneStatusCodes.contains(http.statusCode) {
                DispatchQueue.main.async {
                    self.pollingRunIDs.remove(runID)
                    self.lastPollStartedAt.removeValue(forKey: runID)
                    guard self.stateGenerations[runID, default: 0] == expectedGeneration else {
                        return
                    }
                    self.handleRunVanished(runID: runID)
                    self.render()
                }
                return
            }
            guard let data = data,
                  let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                DispatchQueue.main.async {
                    self.pollingRunIDs.remove(runID)
                    self.lastPollStartedAt.removeValue(forKey: runID)
                    self.render()
                }
                return
            }
            DispatchQueue.main.async {
                guard self.stateGenerations[runID, default: 0] == expectedGeneration else {
                    self.pollingRunIDs.remove(runID)
                    self.lastPollStartedAt.removeValue(forKey: runID)
                    return
                }
                self.notFoundPolls.removeValue(forKey: runID)   // a live view resets the reap streak
                self.serverVerifiedRunIDs.insert(runID)
                var updated = self.runsByID[runID] ?? [:]
                var status = payload["status"] as? String ?? (updated["status"] as? String) ?? "unknown"
                // `cancelling` is a client-only intermediate state set the instant the user clicks STOP.
                // A poll may race the request response. Keep the transient pill only while that request
                // is in flight; the response then supplies the authoritative queued-only outcome.
                if self.cancelRequestsInFlight.contains(runID)
                    && (updated["status"] as? String) == "cancelling"
                    && !terminalStatuses.contains(status) {
                    status = "cancelling"
                }
                updated["run_id"] = runID
                updated["status"] = status
                updated["title"] = payload["title"] as? String ?? updated["title"] ?? ""
                updated["profile"] = payload["profile"] as? String ?? updated["profile"] ?? ""
                updated["auditors"] = payload["auditors"] as? [[String: Any]] ?? updated["auditors"] ?? []
                updated["debug_authorized"] = payload["debug_authorized"] as? Bool ?? false
                // Carry the server's run timestamps forward — the live elapsed clock reads `started_at`,
                // so dropping it here (as this poll used to) froze the panel timer at 0s. A JSON null
                // arrives as NSNull, which must NOT overwrite a value we already have; only copy a real
                // number through.
                for key in ["started_at", "created_at", "completed_at"] {
                    if let value = payload[key], !(value is NSNull) {
                        updated[key] = value
                    }
                }
                updated["updated_at"] = Date().timeIntervalSince1970
                if terminalStatuses.contains(status) {
                    self.cancelRequestsInFlight.remove(runID)
                    updated["finished_at"] = updated["finished_at"] ?? Date().timeIntervalSince1970
                    updated["hidden_after"] = Date().timeIntervalSince1970 + finishedLingerSeconds
                }
                self.runsByID[runID] = updated
                self.pollingRunIDs.remove(runID)
                self.lastPollStartedAt.removeValue(forKey: runID)
                self.saveRunsRegistry()
                self.render()
            }
        }.resume()
    }

    private func render() {
        renderStatusIcon()
        updatePanel()
        for run in visibleRuns() where isActive(run) {
            guard let runID = run["run_id"] as? String, !autoShownRunIDs.contains(runID) else { continue }
            autoShownRunIDs.insert(runID)
            showPanel(activate: true)
        }
        updateIdleExitState()
    }

    private func renderStatusIcon() {
        guard let button = statusItem.button else { return }
        let status = aggregateStatus()
        let locale = visibleRuns().first.map { uiLocale($0) } ?? uiLocale([:])
        statusItem.length = NSStatusItem.variableLength
        button.title = ""
        button.imagePosition = .imageOnly
        button.image = statusIconImage()
        button.contentTintColor = nil
        button.toolTip = auditSurfaceTitle(locale: locale) + ": " + shortStatus(status, locale: locale)
    }

    private func statusIconImage() -> NSImage {
        if let url = Bundle.main.url(forResource: "stopper_icon", withExtension: "png"),
           let image = NSImage(contentsOf: url) {
            // Height is what a menu bar rations; width follows the mark's own aspect so the
            // framing braces cost nothing. Forcing a square here squashed a 1.6:1 image.
            let ratio = image.size.height > 0 ? image.size.width / image.size.height : 1
            image.size = NSSize(width: menuBarIconHeight * ratio, height: menuBarIconHeight)
            // A template image is drawn from its alpha alone, tinted by AppKit — white on a dark
            // menu bar, black on a light one, matching every neighbouring extra. Rendering our
            // green literally is what left it looking like a stray sticker among system icons.
            image.isTemplate = true
            return image
        }
        return makeStatusImage(status: aggregateStatus())
    }

    private func updateIdleExitState() {
        if !visibleRuns().isEmpty || registrySaveRetryScheduled {
            noAuditItemsSince = nil
            return
        }
        let now = Date().timeIntervalSince1970
        if noAuditItemsSince == nil {
            noAuditItemsSince = now
            return
        }
        if now - (noAuditItemsSince ?? now) >= idleExitSeconds {
            quit()
        }
    }

    private func aggregateStatus() -> String {
        let runs = visibleRuns()
        if runs.contains(where: { isActive($0) }) {
            if runs.contains(where: { ($0["status"] as? String) == "cancelling" }) {
                return "cancelling"
            }
            if runs.contains(where: { ($0["status"] as? String) == "queued" }) {
                return "queued"
            }
            return "running"
        }
        if let first = runs.first, isLocalRun(first),
           let terminal = first["status"] as? String, terminalStatuses.contains(terminal) {
            return "advisory"
        }
        if let terminal = runs.first?["status"] as? String, terminalStatuses.contains(terminal) {
            return terminal
        }
        return "idle"
    }

    private func makeStatusImage(status: String) -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18))
        image.lockFocus()
        let badge = NSBezierPath(roundedRect: NSRect(x: 1, y: 2, width: 16, height: 14), xRadius: 5, yRadius: 5)
        statusIconColor(status).setFill()
        badge.fill()
        let glyph = status == "completed" ? "✓" : "A"
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: glyph == "A" ? 9 : 10, weight: .bold),
            .foregroundColor: NSColor.white,
        ]
        NSString(string: glyph).draw(at: NSPoint(x: glyph == "A" ? 5.4 : 4.6, y: 3.4), withAttributes: attrs)
        image.unlockFocus()
        image.isTemplate = false
        return image
    }

    private func statusIconColor(_ status: String) -> NSColor {
        switch status {
        case "completed": return paletteGreen
        case "failed", "cancelled", "cancelling": return paletteRed
        case "queued", "advisory": return .systemOrange
        case "running": return paletteBlue
        default: return color(75, 85, 99)
        }
    }

    private func ensurePanel() {
        if panelWindow != nil { return }
        let initialWidth = savedPanelContentWidth() ?? 520
        let window = NSPanel(
            // Titled so the native traffic lights (close / minimize / zoom) show top-left (Owner
            // 2026-07-15). The title bar is transparent + the content is full-size, so the dark panel
            // reaches edge-to-edge under it and the traffic lights float on the dark chrome — the
            // A-repo dark look is kept, but the window is now a real, chromed macOS window.
            contentRect: NSRect(x: 0, y: 0, width: initialWidth, height: 92),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = auditSurfaceTitle(locale: currentChromeLocale())
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)   // dark title bar + light traffic-light glyphs
        window.backgroundColor = paletteBg
        window.isReleasedWhenClosed = false
        window.isFloatingPanel = true
        window.hidesOnDeactivate = false
        window.isMovableByWindowBackground = true
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.minSize = NSSize(width: 280, height: 72)

        let content = NSView(frame: NSRect(x: 0, y: 0, width: initialWidth, height: 92))
        content.wantsLayer = true
        content.layer?.backgroundColor = paletteBg.cgColor
        // No inner cornerRadius: a titled window draws (and rounds) its own frame; rounding the content
        // too would inset a second rounded rect under the chrome.

        let scroll = NSScrollView(frame: NSRect(x: 12, y: 10, width: initialWidth - 24, height: 54))
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.scrollerKnobStyle = .light
        content.addSubview(scroll)
        let document = TaskListDocumentView(
            frame: NSRect(x: 0, y: 0, width: scroll.contentSize.width, height: emptyRowHeight())
        )
        document.autoresizingMask = [.width]
        scroll.documentView = document

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(stack)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            // Top inset clears the traffic lights, which float over the full-size content view.
            scroll.topAnchor.constraint(equalTo: content.topAnchor, constant: 28),
            scroll.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -10),
            stack.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            stack.topAnchor.constraint(equalTo: document.topAnchor),
        ])

        window.contentView = content
        window.delegate = self
        panelWindow = window
        rowsStack = stack
        rowsScrollView = scroll
    }

    @objc private func statusItemClicked() {
        if panelIsFrontmost() {
            hidePanel()
        } else {
            showPanel(activate: true)
        }
    }

    private func panelIsFrontmost() -> Bool {
        guard let window = panelWindow else { return false }
        return window.isVisible && NSApp.isActive && (window.isKeyWindow || window.isMainWindow)
    }

    private func hidePanel() {
        hiddenByUser = true
        panelWindow?.orderOut(nil)
    }

    private func showPanel(activate: Bool) {
        ensurePanel()
        hiddenByUser = false
        updatePanel()
        if panelWindow?.isVisible == false {
            restorePanelPlacementOrCenter()
        }
        if activate {
            panelWindow?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            panelWindow?.orderFrontRegardless()
        } else {
            panelWindow?.orderFrontRegardless()
        }
    }

    private func updatePanel() {
        guard let rowsStack else { return }
        let scrollOffset = rowsScrollView?.contentView.bounds.origin ?? .zero
        for view in Array(rowsStack.arrangedSubviews) {
            rowsStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }

        let runs = visibleRuns()
        if runs.isEmpty {
            let row = emptyRow()
            rowsStack.addArrangedSubview(row)
            pinRowToStack(row)
        } else {
            for run in runs {
                let row = makeRunRow(run)
                rowsStack.addArrangedSubview(row)
                pinRowToStack(row)
            }
        }
        updatePanelSize(runs: runs)
        // Rows are rebuilt every poll. Restore the reading position after layout, clamping it
        // when completed tasks disappear so a shortened list cannot leave an empty viewport.
        if let scroll = rowsScrollView, let document = scroll.documentView {
            panelWindow?.contentView?.layoutSubtreeIfNeeded()
            let maxY = max(0, document.frame.height - scroll.contentSize.height)
            scroll.contentView.scroll(to: NSPoint(x: 0, y: min(max(0, scrollOffset.y), maxY)))
            scroll.reflectScrolledClipView(scroll.contentView)
        }
    }

    private func pinRowToStack(_ row: NSView) {
        guard let rowsStack else { return }
        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: rowsStack.leadingAnchor),
            row.trailingAnchor.constraint(equalTo: rowsStack.trailingAnchor),
        ])
    }

    private func updatePanelSize(runs: [[String: Any]]) {
        guard let panelWindow else { return }
        let width = max(panelWindow.contentView?.bounds.width ?? 520, 280)
        let rowHeights = runs.isEmpty ? [emptyRowHeight()] : runs.map { rowHeight(run: $0) }
        // 38 = 28 top inset (clears the traffic lights over the full-size content view) + 10 bottom.
        let listHeight = rowHeights.reduce(0, +) + CGFloat(max(0, rowHeights.count - 1) * 8)
        let screenHeight = (panelWindow.screen?.visibleFrame ?? bestVisibleFrame(for: panelWindow.frame))?.height ?? 800
        let maxHeight = max(CGFloat(72), floor(screenHeight * scrollMaxScreenFraction) - 24)
        setPanelContentSize(NSSize(width: width, height: min(38 + listHeight, maxHeight)))
        panelWindow.contentView?.layoutSubtreeIfNeeded()
        if let scroll = rowsScrollView {
            scroll.documentView?.setFrameSize(NSSize(width: scroll.contentSize.width, height: listHeight))
        }
    }

    private func setPanelContentSize(_ size: NSSize) {
        guard let panelWindow else { return }
        let top = panelWindow.frame.maxY
        panelWindow.setContentSize(size)
        var frame = panelWindow.frame
        frame.origin.y = top - frame.height
        panelWindow.setFrame(clampedPanelFrame(frame), display: panelWindow.isVisible)
    }

    private func savedPanelContentWidth() -> CGFloat? {
        guard let state = readJSON(path: uiStatePath) else { return nil }
        let width = CGFloat(numeric(state["width"]))
        if width <= 0 { return nil }
        return min(max(width, 280), maxPanelContentWidth())
    }

    private func restorePanelPlacementOrCenter() {
        guard let panelWindow else { return }
        guard let state = readJSON(path: uiStatePath),
              let savedWidth = savedPanelContentWidth() else {
            panelWindow.center()
            return
        }
        let x = CGFloat(numeric(state["x"]))
        let top = CGFloat(numeric(state["top"]))
        if top <= 0 {
            panelWindow.center()
            return
        }
        panelWindow.setContentSize(NSSize(width: savedWidth, height: panelWindow.contentView?.bounds.height ?? 92))
        var frame = panelWindow.frame
        frame.origin.x = x
        frame.origin.y = top - frame.height
        panelWindow.setFrame(clampedPanelFrame(frame), display: false)
    }

    private func savePanelPlacement() {
        guard let panelWindow else { return }
        if !panelWindow.isVisible { return }
        let contentWidth = max(panelWindow.contentView?.bounds.width ?? panelWindow.frame.width, 280)
        writeJSON(path: uiStatePath, object: [
            "x": Double(panelWindow.frame.origin.x),
            "top": Double(panelWindow.frame.maxY),
            "width": Double(min(contentWidth, maxPanelContentWidth())),
            "updated_at": Date().timeIntervalSince1970,
        ])
    }

    private func maxPanelContentWidth() -> CGFloat {
        let visibleWidth = NSScreen.screens.map { $0.visibleFrame.width }.max() ?? 1200
        return max(280, visibleWidth - 24)
    }

    private func clampedPanelFrame(_ frame: NSRect) -> NSRect {
        guard let visibleFrame = bestVisibleFrame(for: frame) else { return frame }
        var adjusted = frame
        if adjusted.width > visibleFrame.width {
            adjusted.size.width = visibleFrame.width
        }
        if adjusted.height > visibleFrame.height {
            adjusted.size.height = visibleFrame.height
        }
        adjusted.origin.x = min(max(adjusted.origin.x, visibleFrame.minX), visibleFrame.maxX - adjusted.width)
        adjusted.origin.y = min(max(adjusted.origin.y, visibleFrame.minY), visibleFrame.maxY - adjusted.height)
        return adjusted
    }

    private func bestVisibleFrame(for frame: NSRect) -> NSRect? {
        if let screen = NSScreen.screens.first(where: { $0.visibleFrame.intersects(frame) }) {
            return screen.visibleFrame
        }
        return NSScreen.main?.visibleFrame ?? NSScreen.screens.first?.visibleFrame
    }

    private func emptyRowHeight() -> CGFloat {
        return 52
    }

    private func rowHeight(run: [String: Any]) -> CGFloat {
        // One collapsed depth line (title + depth/status/elapsed) — fixed height, independent of the
        // number of voices (which is no longer rendered). +20 when the audit ID label is shown
        // (title + ID + depth line = 3 stacked labels instead of 2 — see makeRunDetailStack).
        let debugRows = debugAuditorRows(run).count
        return (auditIdText(run).isEmpty ? 72 : 92) + CGFloat(debugRows) * 18
    }

    private func emptyRow() -> NSView {
        let label = NSTextField(labelWithString: uiLocale([:]) == "en" ? "No active audits." : "没有进行中的审核")
        label.translatesAutoresizingMaskIntoConstraints = false
        label.font = NSFont.systemFont(ofSize: 13)
        label.textColor = paletteMuted
        label.alignment = .left
        let view = NSView()
        view.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(label)
        NSLayoutConstraint.activate([
            view.heightAnchor.constraint(equalToConstant: emptyRowHeight()),
            view.widthAnchor.constraint(greaterThanOrEqualToConstant: 256),
            label.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            label.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 10),
            label.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -10),
        ])
        return view
    }

    private func makeRunRow(_ run: [String: Any]) -> NSView {
        let row = NSView()
        row.translatesAutoresizingMaskIntoConstraints = false
        row.wantsLayer = true
        // The row shares the panel background (no card surface) — a 1px border here read as an
        // "obvious white frame" inside the dark panel (the separator tone is lighter than #262624).
        // Drop it; multiple concurrent runs stay separated by the rows-stack spacing, not an outline.
        row.layer?.backgroundColor = paletteRow.cgColor

        let status = (run["status"] as? String) ?? ""
        let runID = (run["run_id"] as? String) ?? ""
        let action: RowAction
        switch status {
        case "queued":     action = serverVerifiedRunIDs.contains(runID) ? .stop : .checking
        case "running":    action = .running
        case "cancelling": action = .cancelling
        case "cancelled":  action = .cancelled
        default:           action = isActive(run) ? .checking : .finished
        }
        let button = makeActionButton(action, locale: uiLocale(run))
        if !runID.isEmpty {
            button.identifier = NSUserInterfaceItemIdentifier(runID)
        }

        let detailStack = makeRunDetailStack(run)

        row.addSubview(button)
        row.addSubview(detailStack)

        NSLayoutConstraint.activate([
            row.heightAnchor.constraint(equalToConstant: rowHeight(run: run)),
            row.widthAnchor.constraint(greaterThanOrEqualToConstant: 256),

            button.leadingAnchor.constraint(equalTo: row.leadingAnchor, constant: 10),
            button.topAnchor.constraint(equalTo: row.topAnchor, constant: 18),
            button.widthAnchor.constraint(equalToConstant: 86),
            button.heightAnchor.constraint(equalToConstant: 34),

            detailStack.leadingAnchor.constraint(equalTo: button.trailingAnchor, constant: 16),
            detailStack.trailingAnchor.constraint(equalTo: row.trailingAnchor, constant: -10),
            detailStack.centerYAnchor.constraint(equalTo: row.centerYAnchor),
        ])
        return row
    }

    private func makeRunDetailStack(_ run: [String: Any]) -> NSStackView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let title = titleLabel(runTitle(run))
        stack.addArrangedSubview(title)
        title.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor).isActive = true

        let idText = auditIdText(run)
        if !idText.isEmpty {
            let idLabel = auditIdLabel(text: (uiLocale(run) == "en" ? "ID " : "编号 ") + idText)
            stack.addArrangedSubview(idLabel)
            idLabel.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor).isActive = true
        }

        // ONE collapsed line: DEPTH + overall status + live elapsed — NOT one row per voice. This shows
        // the audit DEPTH (Fast/Standard/Deep) the user chose and deliberately hides the number of voices
        // (a cross-vendor panel size is a privacy surface — never bake the count into user-facing UI).
        let label = auditorLabel(text: "· " + depthLineText(run), color: depthLineColor(run))
        stack.addArrangedSubview(label)
        label.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor).isActive = true
        for rowText in debugAuditorRows(run) {
            let rowLabel = auditorLabel(text: rowText, color: paletteText)
            stack.addArrangedSubview(rowLabel)
            rowLabel.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor).isActive = true
        }
        return stack
    }

    /// "Fast" / "Standard" / "Deep" from the run's audit tier; a generic label when the field is absent
    /// (an older hub that doesn't yet surface it — the line still renders, just without the tier word).
    /// The tier travels in `profile` (what save_active_run persists AND what the hub's /v1/audits view
    /// returns and poll() stores); reading `mode` here was a dead key, so the tier word never appeared.
    private func depthLabel(_ run: [String: Any]) -> String {
        if isDELiteRun(run) { return "DE Lite" }
        let locale = uiLocale(run)
        if isLocalRun(run) {
            return locale == "en" ? "🔶 Local fallback" : "🔶 本地降级"
        }
        switch (run["profile"] as? String ?? "").lowercased() {
        case "fast": return locale == "en" ? "Fast" : "快速"
        case "standard": return locale == "en" ? "Standard" : "标准"
        case "deep": return locale == "en" ? "Deep" : "深度"
        default: return locale == "en" ? "Cross-vendor review" : "跨厂商审核"
        }
    }

    private func uiLocale(_ run: [String: Any]) -> String {
        // Mirrors `runner.normalize_ui_locale`: walk run -> host env -> default, SKIPPING an
        // unsupported tag instead of letting it swallow the next candidate. Default is en-US.
        for candidate in [nonEmptyString(run["ui_locale"]),
                          ProcessInfo.processInfo.environment["DE_UI_LOCALE"]] {
            guard let normalized = candidate?
                .trimmingCharacters(in: .whitespacesAndNewlines).lowercased() else { continue }
            if normalized == "en" || normalized.hasPrefix("en-") { return "en" }
            if normalized == "zh" || normalized.hasPrefix("zh-") { return "zh" }
        }
        return "en"
    }

    private func currentChromeLocale() -> String {
        return visibleRuns().first.map { uiLocale($0) } ?? uiLocale([:])
    }

    private func auditSurfaceTitle(locale: String) -> String {
        return locale == "en" ? "Decision Engine - Audit" : "Decision Engine - 审计"
    }

    private func localReason(_ run: [String: Any]) -> String {
        guard let raw = nonEmptyString(run["degrade_reason"]) else { return "" }
        if uiLocale(run) == "en" {
            return [
                "unactivated": "Unactivated",
                "subscription_expired": "Subscription expired",
                "credits_exhausted": "Insufficient credits",
                "rate_limited": "Rate limited",
                "service_unavailable": "Service unavailable",
                "mcp_unavailable": "MCP unavailable",
            ][raw] ?? ""
        }
        return [
            "unactivated": "未激活",
            "subscription_expired": "订阅到期",
            "credits_exhausted": "积分不足",
            "rate_limited": "服务限流",
            "service_unavailable": "服务不可用",
            "mcp_unavailable": "MCP 不可用",
        ][raw] ?? ""
    }

    /// Overall panel state derived from the voices WITHOUT exposing them: running if any voice is
    /// non-terminal; else completed (all ✓), failed (all ✗), or partial (a mix — A-repo treats one
    /// failure among successes as a PARTIAL panel, not a red error).
    private func overallVoiceStatus(_ run: [String: Any]) -> String {
        // cancel is a run-level state the voices don't carry — surface it directly so the depth line
        // and the action pill agree (a cancelled run must not read as "已完成" off its auditors).
        let runStatus = (run["status"] as? String ?? "").lowercased()
        if runStatus == "cancelling" || runStatus == "cancelled" { return runStatus }
        let auditors = (run["auditors"] as? [[String: Any]]) ?? []
        if auditors.isEmpty { return (run["status"] as? String) ?? "queued" }
        let statuses = auditors.map { ($0["status"] as? String ?? "pending").lowercased() }
        let terminal: Set<String> = ["completed", "failed"]
        if statuses.contains(where: { !terminal.contains($0) }) { return "running" }
        let anyFail = statuses.contains("failed")
        let anyOk = statuses.contains("completed")
        if anyFail && anyOk { return "partial" }
        return anyFail ? "failed" : "completed"
    }

    /// Live elapsed seconds from `started_at`, FROZEN once the panel is terminal (so a finished run stops
    /// ticking during its 30s linger). Recomputed every 1s tick while running → the seconds count up.
    private func depthElapsedSeconds(_ run: [String: Any]) -> Double {
        let started = numeric(run["started_at"])
        let live = started > 0 ? max(0, Date().timeIntervalSince1970 - started) : 0
        let overall = overallVoiceStatus(run)
        guard let runID = run["run_id"] as? String else { return live }
        if overall == "running" || overall == "queued" {
            finishedElapsedByRun.removeValue(forKey: runID)
            return live
        }
        if let frozen = finishedElapsedByRun[runID] { return frozen }
        finishedElapsedByRun[runID] = live
        return live
    }

    private func elapsedString(_ seconds: Double) -> String {
        let s = Int(seconds.rounded())
        if s < 60 { return "\(s)s" }
        return "\(s / 60)m \(s % 60)s"
    }

    private func depthLineText(_ run: [String: Any]) -> String {
        let depth = depthLabel(run)
        let elapsed = elapsedString(depthElapsedSeconds(run))
        let reason = localReason(run)
        let suffix = reason.isEmpty ? "" : " · \(reason)"
        let locale = uiLocale(run)
        if isDELiteRun(run) {
            if locale == "en" {
                switch overallVoiceStatus(run) {
                case "completed":  return "DE Lite · Local review completed (reference only)\(suffix) · \(elapsed) ✓"
                case "partial":    return "DE Lite · Local review partially complete (reference only)\(suffix) · \(elapsed) ⚠"
                case "failed":     return "DE Lite · Local review failed\(suffix) · \(elapsed) ✗"
                case "cancelling": return "DE Lite · Local review stopping\(suffix) · \(elapsed) ⏱"
                case "cancelled":  return "DE Lite · Local review cancelled\(suffix) · \(elapsed)"
                case "queued":     return "DE Lite · Local review queued\(suffix) ⏱"
                default:           return "DE Lite · Local review in progress\(suffix) · \(elapsed) ⏱"
                }
            }
            switch overallVoiceStatus(run) {
            case "completed":  return "DE Lite · 本地审核完成（仅供参考）\(suffix) · \(elapsed) ✓"
            case "partial":    return "DE Lite · 本地审核部分完成（仅供参考）\(suffix) · \(elapsed) ⚠"
            case "failed":     return "DE Lite · 本地审核失败\(suffix) · \(elapsed) ✗"
            case "cancelling": return "DE Lite · 本地审核停止中\(suffix) · \(elapsed) ⏱"
            case "cancelled":  return "DE Lite · 本地审核已取消\(suffix) · \(elapsed)"
            case "queued":     return "DE Lite · 本地审核排队中\(suffix) ⏱"
            default:           return "DE Lite · 本地审核中\(suffix) · \(elapsed) ⏱"
            }
        }
        if isLocalRun(run) {
            let head = locale == "en" ? "🔶 Local fallback · single-model, non-panel" : "🔶 本地降级 · 单模型非跨厂商"
            if locale == "en" {
                switch overallVoiceStatus(run) {
                case "completed": return "\(head) · Complete (reference only) · \(elapsed)"
                case "partial": return "\(head) · Partially complete (reference only) · \(elapsed)"
                case "failed": return "\(head) · Failed · \(elapsed)"
                case "cancelling": return "\(head) · Stopping · \(elapsed) ⏱"
                case "cancelled": return "\(head) · Cancelled · \(elapsed)"
                case "queued": return "\(head) · Queued ⏱"
                case "running": return "\(head) · Local review in progress · \(elapsed) ⏱"
                default: return "\(head) · Unknown status (reference only) ⏱"
                }
            }
            switch overallVoiceStatus(run) {
            case "completed": return "\(head) · 完成（仅参考） · \(elapsed)"
            case "partial": return "\(head) · 部分完成（仅参考） · \(elapsed)"
            case "failed": return "\(head) · 失败 · \(elapsed)"
            case "cancelling": return "\(head) · 停止中 · \(elapsed) ⏱"
            case "cancelled": return "\(head) · 已取消 · \(elapsed)"
            case "queued": return "\(head) · 排队中 ⏱"
            case "running": return "\(head) · 本地审核中 · \(elapsed) ⏱"
            default: return "\(head) · 状态未知（仅参考） ⏱"
            }
        }
        if locale == "en" {
            switch overallVoiceStatus(run) {
            case "completed":  return "\(depth) · Completed · \(elapsed) ✓"
            case "partial":    return "\(depth) · Partially complete · \(elapsed) ⚠"
            case "failed":     return "\(depth) · Failed · \(elapsed) ✗"
            case "cancelling": return "\(depth) · Stopping · \(elapsed) ⏱"
            case "cancelled":  return "\(depth) · Cancelled · \(elapsed)"
            case "queued":     return "\(depth) · Queued ⏱"
            default:           return "\(depth) · Reviewing · \(elapsed) ⏱"
            }
        }
        switch overallVoiceStatus(run) {
        case "completed":  return "\(depth) · 已完成 · \(elapsed) ✓"
        case "partial":    return "\(depth) · 部分完成 · \(elapsed) ⚠"
        case "failed":     return "\(depth) · 失败 · \(elapsed) ✗"
        case "cancelling": return "\(depth) · 停止中 · \(elapsed) ⏱"
        case "cancelled":  return "\(depth) · 已取消 · \(elapsed)"
        case "queued":     return "\(depth) · 排队中 ⏱"
        default:           return "\(depth) · 审核中 · \(elapsed) ⏱"
        }
    }

    private func depthLineColor(_ run: [String: Any]) -> NSColor {
        if isLocalRun(run) {
            switch overallVoiceStatus(run) {
            case "failed": return paletteRed
            case "completed": return isDELiteRun(run) ? paletteGreen : paletteAmber
            case "partial", "cancelled": return paletteAmber
            default: return paletteMuted
            }
        }
        switch overallVoiceStatus(run) {
        case "completed": return paletteGreen
        case "partial":   return paletteAmber
        case "cancelled": return paletteAmber
        case "failed":    return paletteRed
        default:          return paletteMuted
        }
    }

    private enum RowAction { case stop, cancelling, cancelled, running, checking, finished }

    /// The row's action pill, matching the A-repo reference stopper (stopper_app.py):
    ///   * stop       → "⏹  STOP" on the warm BTN_RED (#BC4936), white, clickable
    ///   * cancelling → "Cancelling…" grey pill (#3A3A38), disabled — instant feedback after a click
    ///   * cancelled  → "Cancelled" grey pill with amber text (#C7A14F), disabled — a cancel is a
    ///                  distinct outcome, not a plain "Finished" (A-repo colours terminal pills by outcome)
    ///   * running    → "Running" on the same red as STOP, white, DISABLED — the server only accepts
    ///                  queued cancellation, so this is a state badge, not an offer. Red because a
    ///                  live audit is the loudest thing on the panel; the greyed-out fill made an
    ///                  active run read as finished. Parity with client/stopper/panel.py.
    ///   * checking   → "Checking…" grey pill, disabled — disk-seeded queue is not yet verified
    ///   * finished   → "Finished" grey pill, disabled
    private func makeActionButton(_ action: RowAction, locale: String) -> NSButton {
        let title: String
        let fill: NSColor
        let fg: NSColor
        let enabled: Bool
        switch action {
        case .stop:       title = locale == "en" ? "⏹  STOP" : "⏹  停止"; fill = paletteRed; fg = .white; enabled = true
        case .cancelling: title = locale == "en" ? "Cancelling…" : "停止中"; fill = paletteFinished; fg = paletteMuted; enabled = false
        case .cancelled:  title = locale == "en" ? "Cancelled" : "已取消"; fill = paletteFinished; fg = paletteAmber; enabled = false
        case .running:    title = locale == "en" ? "Running" : "进行中"; fill = paletteRed; fg = .white; enabled = false
        case .checking:   title = locale == "en" ? "Checking…" : "检查中"; fill = paletteFinished; fg = paletteMuted; enabled = false
        case .finished:   title = locale == "en" ? "Finished" : "已完成"; fill = paletteFinished; fg = paletteMuted; enabled = false
        }
        let button = NSButton(title: title, target: self, action: #selector(stopButtonClicked(_:)))
        button.translatesAutoresizingMaskIntoConstraints = false
        button.isBordered = false
        button.isEnabled = enabled
        button.wantsLayer = true
        // Match the A-repo pill's corner (stopper_app.py RoundedButton, BTN_RADIUS = 8). That radius is
        // fed to a tkinter `smooth=True` quadratic B-spline, NOT a circular arc — the spline rounds far
        // tighter, measuring an effective circular radius of ~3.4 (straight edges stop 4pt from the
        // corner). A real CALayer `cornerRadius = 8` is a true arc and reads ~2× rounder, which testers
        // flagged. 4 matches the A-repo pill's visual corner (its edge-tangent extent).
        button.layer?.cornerRadius = 4
        button.layer?.backgroundColor = fill.cgColor
        button.attributedTitle = NSAttributedString(
            string: title,
            attributes: [
                .foregroundColor: fg,
                .font: NSFont.systemFont(ofSize: 13, weight: .bold),
            ]
        )
        return button
    }

    private func titleLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = NSFont.systemFont(ofSize: 18, weight: .bold)
        label.textColor = paletteText
        label.alignment = .left
        label.lineBreakMode = .byTruncatingTail
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }

    private func auditorLabel(text: String, color: NSColor) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        label.textColor = color
        label.alignment = .left
        label.lineBreakMode = .byTruncatingMiddle
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }

    private func auditorDisplayText(_ auditor: [String: Any]) -> String {
        let model = structuredModelLabel(auditor)
        let status = auditor["status"] as? String ?? "pending"
        switch status {
        case "completed":
            return "\(model) · \(auditorElapsed(auditor)) ✓"
        case "failed":
            return "\(model) · failed"
        case "running":
            return "\(model) · \(auditorElapsed(auditor))"
        default:
            return "\(model) · pending"
        }
    }

    private func structuredModelLabel(_ auditor: [String: Any]) -> String {
        let model = nonEmptyString(auditor["model_id"])
            ?? nonEmptyString(auditor["model_alias"])
            ?? nonEmptyString(auditor["model"])
        return model ?? nonEmptyString(auditor["provider"]) ?? "auditor"
    }

    private func auditorElapsed(_ auditor: [String: Any]) -> String {
        if let durationMS = auditor["duration_ms"] as? Double, durationMS > 0 {
            return formatElapsed(durationMS / 1000.0)
        }
        if let durationMS = auditor["duration_ms"] as? Int, durationMS > 0 {
            return formatElapsed(Double(durationMS) / 1000.0)
        }
        if let startedAt = auditor["started_at"] as? Double, startedAt > 0 {
            return formatElapsed(max(0, Date().timeIntervalSince1970 - startedAt))
        }
        return "0s"
    }

    private func debugAuditorRows(_ run: [String: Any]) -> [String] {
        guard (run["debug_authorized"] as? Bool) == true else { return [] }
        let auditors = (run["auditors"] as? [[String: Any]]) ?? []
        if auditors.isEmpty { return [] }
        return auditors.map { auditorDisplayText($0) }
    }

    private func auditorColor(_ auditor: [String: Any]) -> NSColor {
        let status = auditor["status"] as? String ?? "pending"
        switch status {
        case "completed": return paletteGreen
        case "failed": return paletteRed
        default: return paletteMuted
        }
    }

    private func runTitle(_ run: [String: Any]) -> String {
        return clipped(nonEmptyString(run["title"]) ?? (uiLocale(run) == "en" ? "Audit" : "审核"), maxLength: 48)
    }

    // Cap mirrors client/stopper/panel.py's _MAX_AUDIT_ID_LENGTH.
    private let maxAuditIDLength = 128

    /// The user-facing audit ID, omitting one fixed `aud_` prefix. Mirrors
    /// client/stopper/panel.py's `audit_id_text` — the registry key remains
    /// authoritative for polling/cancellation; this only controls presentation.
    /// Local advisory identifiers and malformed/unbounded server values never render.
    private func auditIdText(_ run: [String: Any]) -> String {
        if (run["local"] as? Bool) == true { return "" }
        guard let raw = run["run_id"] as? String else { return "" }
        if raw != raw.trimmingCharacters(in: .whitespacesAndNewlines) { return "" }
        if raw.isEmpty || raw.count > maxAuditIDLength { return "" }
        let allowed = raw.unicodeScalars.allSatisfy { scalar in
            scalar.isASCII && (CharacterSet.alphanumerics.contains(scalar) || scalar == "_" || scalar == "-")
        }
        if !allowed { return "" }
        var runID = raw
        if runID.hasPrefix("aud_") { runID = String(runID.dropFirst(4)) }
        return runID
    }

    private func auditIdLabel(text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        label.textColor = paletteMuted
        label.alignment = .left
        label.lineBreakMode = .byTruncatingMiddle
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }

    private func clipped(_ text: String, maxLength: Int) -> String {
        if text.count <= maxLength { return text }
        let index = text.index(text.startIndex, offsetBy: maxLength - 1)
        return String(text[..<index]) + "…"
    }

    private func nonEmptyString(_ value: Any?) -> String? {
        guard let text = value as? String else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func numeric(_ value: Any?) -> Double {
        if let number = value as? Double { return number }
        if let number = value as? Int { return Double(number) }
        return 0
    }

    private func formatElapsed(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        if total >= 3600 {
            return "\(total / 3600)h\((total % 3600) / 60)m"
        }
        if total >= 60 {
            return "\(total / 60)m\(total % 60)s"
        }
        return "\(total)s"
    }

    private func shortStatus(_ status: String, locale: String) -> String {
        if locale == "zh" {
            switch status {
            case "queued": return "排队中"
            case "running": return "进行中"
            case "cancelling": return "停止中"
            case "completed": return "已完成"
            case "failed": return "失败"
            case "cancelled": return "已取消"
            case "advisory": return "本地参考"
            default: return status
            }
        }
        switch status {
        case "queued": return "Queued"
        case "running": return "Running"
        case "cancelling": return "Stopping"
        case "completed": return "Completed"
        case "failed": return "Failed"
        case "cancelled": return "Cancelled"
        case "advisory": return "Local reference"
        default: return status.prefix(1).uppercased() + status.dropFirst()
        }
    }

    @objc private func stopButtonClicked(_ sender: NSButton) {
        guard let runID = sender.identifier?.rawValue else { return }
        stopAudit(runID: runID)
    }

    private func stopAudit(runID: String) {
        guard let run = runsByID[runID],
              (run["status"] as? String) == "queued",
              serverVerifiedRunIDs.contains(runID),
              !cancelRequestsInFlight.contains(runID),
              let config = readJSON(path: configPath),
              let serverURL = config["server_endpoint"] as? String,
              let token = config["access_token"] as? String,
              let url = URL(string: serverURL + "/v1/audits/" + runID + "/cancel") else {
            return
        }

        var request = URLRequest(url: url, timeoutInterval: 20)
        request.httpMethod = "POST"
        request.httpBody = Data("{}".utf8)
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        cancelRequestsInFlight.insert(runID)
        serverVerifiedRunIDs.remove(runID)
        stateGenerations[runID, default: 0] += 1
        var requesting = run
        requesting["status"] = "cancelling"
        requesting["updated_at"] = Date().timeIntervalSince1970
        runsByID[runID] = requesting
        render()

        URLSession.shared.dataTask(with: request) { data, response, error in
            let httpOK = (response as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
            let payload: [String: Any]?
            if httpOK, let data = data {
                payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            } else {
                payload = nil
            }
            DispatchQueue.main.async {
                self.cancelRequestsInFlight.remove(runID)
                guard var updated = self.runsByID[runID],
                      let currentStatus = updated["status"] as? String,
                      currentStatus == "cancelling" else {
                    return  // a terminal poll won the race; terminal observations are absorbing
                }
                let now = Date().timeIntervalSince1970
                let stopped = payload?["stopped"] as? Bool
                self.serverVerifiedRunIDs.remove(runID)
                self.stateGenerations[runID, default: 0] += 1
                if error != nil || !httpOK || stopped == nil {
                    updated["status"] = "unknown"
                } else if stopped == true {
                    updated["status"] = "cancelled"
                    updated["finished_at"] = updated["finished_at"] ?? now
                    updated["hidden_after"] = now + finishedLingerSeconds
                } else {
                    let reported = (payload?["status"] as? String)?.lowercased() ?? ""
                    let allowed = terminalStatuses.contains(reported)
                        || reported == "running" || reported == "cancelling"
                    updated["status"] = allowed ? reported : "unknown"
                    if terminalStatuses.contains(updated["status"] as? String ?? "") {
                        updated["finished_at"] = updated["finished_at"] ?? now
                        updated["hidden_after"] = now + finishedLingerSeconds
                    }
                }
                updated["run_id"] = runID
                updated["updated_at"] = now
                self.runsByID[runID] = updated
                self.saveRunsRegistry()
                self.render()
            }
        }.resume()
    }

    @objc private func quit() {
        timer?.invalidate()
        timer = nil
        NSApp.terminate(nil)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminationReplyPending {
            return .terminateNow
        }
        terminationReplyPending = true
        let drained = DispatchSemaphore(value: 0)
        registryQueue.async {
            drained.signal()
        }
        DispatchQueue.global(qos: .utility).async {
            _ = drained.wait(timeout: .now() + 1.0)
            DispatchQueue.main.async {
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        NSStatusBar.system.removeStatusItem(statusItem)
        if lockFD >= 0 {
            flock(lockFD, LOCK_UN)
            Darwin.close(lockFD)
            lockFD = -1
        }
    }
}

extension DecisionEngineStopper: NSWindowDelegate {
    func windowWillClose(_ notification: Notification) {
        hiddenByUser = true
    }

    func windowDidMove(_ notification: Notification) {
        savePanelPlacement()
    }

    func windowDidEndLiveResize(_ notification: Notification) {
        savePanelPlacement()
    }

    func windowDidResize(_ notification: Notification) {
        savePanelPlacement()
    }
}

let app = NSApplication.shared
let delegate = DecisionEngineStopper()
app.delegate = delegate
app.run()
