import AppKit
import ApplicationServices
import AVFoundation
import CoreGraphics
import Foundation
import ServiceManagement
import SwiftUI

// MARK: - Monitor State

class MonitorState: ObservableObject {
    @Published var signals: Int = 0
    @Published var matches: Int = 0
    @Published var running: Bool = false
    @Published var recentSignals: [SignalInfo] = []
    @Published var lastSignalISO: String = ""
    @Published var lastSignalSource: String = ""
    @Published var lastSignalPreview: String = ""
    @Published var insights: [AnalysisInsight] = []
    @Published var isRecording: Bool = false

    // Command center state — replaces chat
    @Published var activityEntries: [ActivityEntry] = []
    @Published var briefing: Briefing = .empty
    @Published var commandInput: String = ""
    @Published var commandPending: Bool = false
    @Published var commandToast: Toast? = nil
    private var activityTimer: Timer?

    // Notch expansion state — the pill has three visual modes:
    //   .idle       small pill at the bottom of the screen
    //   .recording  waveform or meeting-recording UI in the pill
    //   .expanded   the full command-center panel hosted above the pill
    // Click on the pill toggles between idle and expanded. A voice or
    // command response can force-expand and display a classification
    // banner; for short confirmations the panel auto-collapses after
    // ``autoCollapseSeconds`` unless the user engages with it.
    @Published var pillExpanded: Bool = false
    @Published var lastResponseType: String = ""       // "query", "action", "goal", "automation", "context"
    @Published var lastResponseMessage: String = ""    // confirmation text or query answer (markdown ok)
    @Published var lastResponseIsQuery: Bool = false   // queries pin open; others auto-collapse
    @Published var lastResponseAt: Date? = nil
    private var autoCollapseTimer: Timer?
    private let autoCollapseSeconds: TimeInterval = 4.0

    /// Whether the expanded panel has any engagement that should cancel
    /// an in-flight auto-collapse (hover, focused text field, etc.).
    @Published var expandedEngagement: Bool = false

    // Voice pill state
    @Published var voicePillEnabled: Bool = true
    @Published var voicePillActive: Bool = false
    @Published var voicePillProcessing: Bool = false
    @Published var voicePillStatus: String = ""  // "Listening..." or "Transcribing..."
    @Published var voicePillTranscript: String = ""
    @Published var voicePillHovered: Bool = false
    // 16 rolling audio-level samples, 0.0–1.0 each. Newest sample gets
    // appended at the end and the oldest is dropped off the front, so
    // the bars animate right-to-left as you speak. Fed by VoiceRecorder's
    // tap via VoiceCommandDispatcher's level callback — one engine, one
    // tap, same samples that the WAV is written from.
    @Published var levelHistory: [CGFloat] = Array(repeating: 0, count: 16)

    // Permission state — checked on launch and periodically
    @Published var hasScreenRecording: Bool = false
    @Published var hasFullDiskAccess: Bool = false
    @Published var hasAccessibility: Bool = false
    @Published var hasMicrophone: Bool = false
    @Published var missingPermissions: [String] = []
    @Published var micBusy: Bool = false
    @Published var setupNeeded: Bool = false
    @Published var setupStep: Int = 0

    /// True whenever Deja is missing something it structurally needs to
    /// run — any revoked permission, missing Google auth, or first-launch
    /// setup still pending. When blocked, the app surfaces the setup
    /// panel and gates the pill (no voice, no command center expansion)
    /// so the user can't mistake Deja for "working" when it's not.
    /// Transient operational errors (proxy 502, one failed LLM call)
    /// are NOT structural and surface via the error toast + request id
    /// path instead.
    /// Default false — we haven't checked permissions yet, so we can't
    /// claim the app is blocked. The first ``recomputeBlockedState()``
    /// call after permission probes runs within milliseconds of launch
    /// and will flip this true if anything is actually missing. If we
    /// defaulted to true, the Combine subscriber in AppDelegate would
    /// briefly flash the setup panel on every launch before the probe
    /// completes.
    @Published var isBlocked: Bool = false

    // Meeting recording state
    @Published var meetingAvailable: Bool = false
    @Published var meetingTitle: String = ""
    @Published var meetingAttendees: [String] = []
    @Published var meetingRecording: Bool = false
    @Published var meetingLinked: Bool = false
    @Published var meetingPaused: Bool = false
    @Published var meetingElapsed: TimeInterval = 0
    @Published var meetingSessionId: String = ""
    @Published var meetingNotes: String = ""
    @Published var meetingTimeRange: String = ""
    @Published var showSettings: Bool = false
    @Published var launchAtLogin: Bool = true

    /// Whether the signed-in Google account is on the server-side
    /// admin allowlist (``DEJA_ADMIN_EMAILS`` env var on Render).
    /// Drives the "Open Admin Dashboard" tray menu item — hidden
    /// for non-admin users so they never see the option.
    @Published var isAdmin: Bool = false

    /// Signed-in Google account identity — shown at the top of the
    /// tray menu so the user can see which account Deja is bound to.
    /// Populated by ``fetchAdminStatus`` (which returns email + name
    /// in the same response from /api/me).
    @Published var signedInEmail: String = ""
    @Published var signedInName: String = ""
    @Published var backfillRunning: Bool = false
    @Published var backfillStep: String = ""
    @Published var backfillPages: Int = 0
    @Published var meetingProcessing: Bool = false

    // Notifications from agent
    @Published var notificationTitle: String = ""
    @Published var notificationMessage: String = ""
    @Published var showNotification: Bool = false

    // User-facing error toast. Populated by ``errorPoller`` watching
    // ~/.deja/latest_error.json. Persists until the user dismisses so
    // the request ID stays copyable; dismissal removes the file so it
    // doesn't replay.
    @Published var currentError: DejaError?
    private let errorPoller = ErrorPollingService()

    // Connected AI assistants (MCP clients) — shown in Settings
    @Published var mcpClients: [MCPClientInfo] = []
    @Published var mcpClientErrors: [String: String] = [:]

    private var backfillTimer: Timer?
    private var meetingStartTime: Date?
    private var meetingTimer: Timer?

    private var statsTimer: Timer?
    private var stateSnapshotTimer: Timer?
    /// Event-driven screenshot scheduler. Owns the debounced
    /// DispatchWorkItem, the 60s passive-reading floor timer, the
    /// NSWorkspace app-activation observer, and the AX observer on
    /// the frontmost app. Replaces the old fixed 6s Timer.
    private let captureScheduler = ScreenCaptureScheduler()
    private var captureSchedulerStarted = false
    private let keystrokeMonitor = KeystrokeMonitor()
    /// Snapshots focused text-field contents via AX; emits "finished
    /// thought" records to ~/.deja/typed_content.jsonl. Reuses the
    /// existing Accessibility grant — no new TCC prompt.
    private lazy var typedContentMonitor = TypedContentMonitor(keystrokeMonitor: keystrokeMonitor)
    /// Timestamp of the last SUCCESSFUL capture. Updated only when a
    /// capture actually goes through — NOT on deferred ticks. This is
    /// what the max-deferral safety valve compares against, so the
    /// counter is monotonic: brief idle gaps can't reset it.
    private var lastSuccessfulCaptureTime: TimeInterval = 0
    /// Defer captures while the user is typing — mid-sentence frames confuse vision.
    private let typingIdleThreshold: TimeInterval = 3.5
    /// Hard cap: if it's been this long since the last successful capture,
    /// force one even if the user is still typing. Prevents vision staleness
    /// during long coding sessions with brief natural pauses.
    private let maxCaptureDeferral: TimeInterval = 60.0
    private var dbReaderTimer: Timer?
    // MARK: - Extracted services

    private let processManager = BackendProcessManager()
    private let databaseReader = DatabaseReader()
    private let setupManager = SetupManager()
    private let meetingCoordinator = MeetingCoordinator()

    // MARK: - Static paths (used by extracted services)

    static let home = NSHomeDirectory() + "/.deja"

    static var backendPath: String {
        if let resourceURL = Bundle.main.resourceURL {
            let bundled = resourceURL.appendingPathComponent("python-env/bin/python3").path
            if FileManager.default.fileExists(atPath: bundled) {
                return bundled
            }
        }
        #if DEBUG
        return NSHomeDirectory() + "/projects/deja/venv/bin/python3"
        #else
        fatalError("Bundled Python not found in app bundle — cannot start backend")
        #endif
    }

    static var isBundledPython: Bool {
        if let resourceURL = Bundle.main.resourceURL {
            return FileManager.default.fileExists(
                atPath: resourceURL.appendingPathComponent("python-env/bin/python3").path
            )
        }
        return false
    }

    static let projectDir = NSHomeDirectory() + "/projects/deja"

    // MARK: - Launch at Login (SMAppService)

    func loadLaunchAtLoginState() {
        let status = SMAppService.mainApp.status
        if status == .notRegistered {
            setLaunchAtLogin(true)
        } else {
            launchAtLogin = status == .enabled
        }
    }

    func setVoicePillEnabled(_ enabled: Bool) {
        voicePillEnabled = enabled
        UserDefaults.standard.set(enabled, forKey: "voicePillEnabled")
        NotificationCenter.default.post(
            name: .voicePillToggled,
            object: nil,
            userInfo: ["enabled": enabled]
        )
    }

    func loadVoicePillState() {
        // Default to true if never set
        if UserDefaults.standard.object(forKey: "voicePillEnabled") == nil {
            voicePillEnabled = true
        } else {
            voicePillEnabled = UserDefaults.standard.bool(forKey: "voicePillEnabled")
        }
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            launchAtLogin = enabled
        } catch {
            NSLog("deja: launch-at-login toggle failed: \(error)")
            launchAtLogin = SMAppService.mainApp.status == .enabled
        }
    }

    // MARK: - Start / Stop / Restart

    func start() {
        setupNeeded = !setupManager.isSetupDone
        recomputeBlockedState()
        loadLaunchAtLoginState()

        // Debug snapshot: ~/.deja/swift_state.json refreshed every 5s so
        // the Python side (and log archives) can inspect what the Swift
        // UI believes is true without IPC round-trips.
        writeStateSnapshot()
        stateSnapshotTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.writeStateSnapshot()
        }

        startErrorPolling()

        startWeb()

        // Graphiti ingest worker — runs in its own process, tails
        // ~/.deja/graphiti_queue.jsonl. Safe to start unconditionally:
        // if the queue file doesn't exist yet, the worker just polls
        // until it appears. Keeping it out of the main monitor process
        // is intentional — Kuzu AsyncConnection was deadlocking when
        // add_episode ran inside the agent loop.
        startGraphitiWorker()

        // Always start database readers — even during setup.
        // The sqlite3 access to chat.db triggers macOS to add Deja
        // to the Full Disk Access list in System Settings.
        startDatabaseReaders()

        if !setupNeeded {
            startScreenshotCapture()

            DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
                self?.startMonitor()
            }

            statsTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
                self?.updateStats()
                self?.updateRecentSignals()
                self?.updateInsights()
            }
            updateStats()
            updateRecentSignals()
            updateInsights()
            startMeetingPolling()

            checkBackfillStatus()
            Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] timer in
                guard let self = self else { timer.invalidate(); return }
                if self.backfillRunning {
                    timer.invalidate()
                } else {
                    self.checkBackfillStatus()
                }
            }

            checkRuntimePermissions()
            Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
                self?.checkRuntimePermissions()
            }

            // Probe /api/me once the local backend is reachable. Drives
            // the "Open Admin Dashboard" tray item and surfaces the
            // signed-in identity at the top of the menu. Only ran from
            // completeSetup() before, which meant returning users (no
            // wizard) never got isAdmin populated and the menu item
            // stayed hidden forever.
            DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
                self?.fetchAdminStatus()
            }
            // Refetch every 5 min so a transient Render blip on launch
            // (502 / cold start) self-heals without the user having to
            // relaunch the app. Admin-list membership rarely changes, so
            // the cost is tiny and the reliability win is real.
            Timer.scheduledTimer(withTimeInterval: 300.0, repeats: true) { [weak self] _ in
                self?.fetchAdminStatus()
            }
        }
    }

    func stop() {
        statsTimer?.invalidate(); statsTimer = nil
        stateSnapshotTimer?.invalidate(); stateSnapshotTimer = nil
        captureScheduler.stop()
        captureSchedulerStarted = false
        keystrokeMonitor.stop()
        typedContentMonitor.stop()
        dbReaderTimer?.invalidate(); dbReaderTimer = nil
        errorPoller.stop()
        processManager.stopAll()
        running = false
    }

    // MARK: - Error toast

    private func startErrorPolling() {
        errorPoller.onError = { [weak self] err in
            self?.currentError = err
        }
        errorPoller.start()
    }

    /// Called by the toast × button or on shutdown. Removes the
    /// underlying latest_error.json so the same error doesn't re-
    /// surface on next poll.
    func dismissCurrentError() {
        guard let err = currentError else { return }
        currentError = nil
        errorPoller.dismissAndClear(err)
    }

    func restart() {
        running = false
        processManager.restartAll { [weak self] in
            self?.running = false
        }
        // restartAll re-starts after 1s delay; update running when monitor launches
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            self?.running = self?.processManager.isMonitorRunning ?? false
        }
    }

    // MARK: - Setup Delegation

    func checkSetupStatus() {
        setupManager.checkSetupStatus { [weak self] step in
            self?.setupStep = step
        }
    }

    func completeSetup() {
        setupNeeded = false
        recomputeBlockedState()

        // Tell the backend to write the setup_done marker so the
        // wizard doesn't reappear on next launch.
        localAPICall("/api/setup/complete", method: "POST", timeoutInterval: 5) { _, _ in }

        startScreenshotCapture()
        startDatabaseReaders()

        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            self?.startMonitor()
        }

        statsTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            self?.updateStats()
            self?.updateRecentSignals()
            self?.updateInsights()
        }
        startMeetingPolling()
        checkRuntimePermissions()
        // Wait briefly for the local backend to be reachable before
        // probing /api/me — at startup the FastAPI server may still
        // be booting. Fetched once; admin list membership changes
        // rarely enough that polling isn't worth the network chatter.
        // Users can relaunch to pick up a freshly-granted admin.
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            self?.fetchAdminStatus()
        }

        Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
            self?.checkRuntimePermissions()
        }

        NotificationCenter.default.post(name: .setupCompleted, object: nil)
    }

    func checkRuntimePermissions() {
        setupManager.checkRuntimePermissions { [weak self] screenOK, fdaOK, missing in
            guard let self = self else { return }
            self.hasScreenRecording = screenOK
            self.hasFullDiskAccess = fdaOK
            self.missingPermissions = missing
            self.recomputeBlockedState()
        }
        // Accessibility can be probed directly via AXIsProcessTrusted.
        // Microphone has no non-prompting probe on macOS — AVCaptureDevice
        // authorizationStatus returns the cached decision the system made.
        self.hasAccessibility = AXIsProcessTrusted()
        let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        self.hasMicrophone = (micStatus == .authorized)
        self.recomputeBlockedState()
    }

    /// Derive ``isBlocked`` from the structural checks. Called after
    /// every permission / setup state change so the UI reacts within
    /// one poll cycle of anything the user revokes or grants.
    func recomputeBlockedState() {
        let blocked = setupNeeded
            || !hasScreenRecording
            || !hasFullDiskAccess
            || !hasAccessibility
            || !hasMicrophone
        if blocked != isBlocked {
            isBlocked = blocked
        }
    }

    // MARK: - Backfill Delegation

    func checkBackfillStatus() {
        setupManager.checkBackfillStatus { [weak self] running, step, pages in
            guard let self = self, running else { return }
            self.backfillRunning = true
            self.backfillStep = step
            self.backfillPages = pages
            self.startBackfillPolling()
        }
    }

    private func startBackfillPolling() {
        backfillTimer?.invalidate()
        backfillTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] timer in
            guard let self = self else { timer.invalidate(); return }
            self.setupManager.checkBackfillStatus { running, step, pages in
                self.backfillStep = step
                self.backfillPages = pages
                if !running {
                    self.backfillRunning = false
                    timer.invalidate()
                }
            }
        }
    }

    func startBackfillAndPoll() {
        setupManager.startBackfill()
        backfillRunning = true
        backfillStep = "Starting..."
        startBackfillPolling()
    }

    // MARK: - Process Delegation

    private func startMonitor() {
        processManager.startMonitor { [weak self] in
            self?.running = false
        }
        running = true
    }

    func startWeb() {
        processManager.startWeb()
    }

    /// Launch the Graphiti ingest worker subprocess. Runs alongside the
    /// monitor/web processes; tails the on-disk queue file. Out-of-process
    /// to isolate Kuzu writes from the main agent loop.
    func startGraphitiWorker() {
        processManager.startGraphitiWorker()
    }

    // MARK: - Screenshot Capture

    /// Kick off event-driven screen capture. Replaces the old 6s
    /// fixed-interval timer with three triggers (app focus change,
    /// typing pause, AX window change) plus a 60s passive-reading
    /// floor. Idempotent — calling twice is a no-op.
    func startScreenshotCapture() {
        guard !captureSchedulerStarted else { return }
        captureSchedulerStarted = true
        keystrokeMonitor.start()
        typedContentMonitor.start()
        captureScheduler.start(monitorState: self, keystrokeMonitor: keystrokeMonitor)
    }

    /// Capture a screenshot, but defer if the user is currently typing.
    /// Voice transcription bypasses the gate via captureScreenshot(force: true).
    ///
    /// Defer rule is monotonic against the last SUCCESSFUL capture time,
    /// not a transient deferral counter — brief idle pauses during a long
    /// typing session must not reset the safety valve.
    func captureScreenshot(force: Bool = false) {
        // Never attempt a capture while Deja is structurally blocked.
        // Screen Recording permission is the most common block
        // reason — if we call `screencapture` without a valid grant
        // macOS shows a modal permission prompt. After a rebuild the
        // cdhash changes, so TCC re-prompts on every attempt until
        // the user re-grants (every 6 seconds from the capture
        // timer). Skipping the call avoids that loop entirely.
        if isBlocked {
            return
        }

        let now = Date().timeIntervalSince1970

        if !force {
            let idle = keystrokeMonitor.idleSeconds
            let secondsSinceLastCapture = lastSuccessfulCaptureTime > 0
                ? now - lastSuccessfulCaptureTime
                : .infinity

            // Defer if user is mid-typing AND we haven't gone too long
            // without a successful capture. After maxCaptureDeferral
            // seconds of no capture we force one regardless of typing.
            if idle < typingIdleThreshold && secondsSinceLastCapture < maxCaptureDeferral {
                NSLog(
                    "deja: screenshot deferred (typing — idle=%.1fs, since_last_capture=%.1fs)",
                    idle, secondsSinceLastCapture
                )
                return
            }
        }

        lastSuccessfulCaptureTime = now
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.processManager.captureScreenshot()
        }
        // Reset the 60s passive-reading floor — any successful
        // capture (voice command, event-driven trigger, manual force)
        // counts as "we saw the screen recently," so the floor
        // fallback shouldn't pile on a redundant capture.
        captureScheduler.noteCaptureLanded()
    }

    // MARK: - Database Reader Delegation

    private func startDatabaseReaders() {
        readDatabases()
        dbReaderTimer = Timer.scheduledTimer(withTimeInterval: 15.0, repeats: true) { [weak self] _ in
            self?.readDatabases()
        }
    }

    private func readDatabases() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.databaseReader.readAll()
        }
    }

    // MARK: - Diverse signals

    var diverseSignals: [SignalInfo] {
        var result: [SignalInfo] = []
        var screenshotCount = 0
        for sig in recentSignals {
            if sig.source == "screenshot" {
                screenshotCount += 1
                if screenshotCount > 2 { continue }
            }
            result.append(sig)
        }
        return result
    }

    // MARK: - Stats & Insights (delegated to DatabaseReader)

    private func updateStats() {
        let isMonitorRunning = processManager.isMonitorRunning
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let result = self.databaseReader.readStats(isMonitorRunning: isMonitorRunning)
            DispatchQueue.main.async {
                self.signals = result.stats.signals
                self.matches = result.stats.matches
                self.running = result.running
            }
        }
    }

    private func updateInsights() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let parsed = self.databaseReader.readInsights()
            DispatchQueue.main.async { self.insights = parsed }
        }
    }

    private func updateRecentSignals() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let result = self.databaseReader.readRecentSignals()
            DispatchQueue.main.async {
                self.recentSignals = result.signals
                self.lastSignalISO = result.latestISO
                self.lastSignalSource = result.latestSource
                self.lastSignalPreview = result.latestPreview
            }
        }
    }

    // MARK: - Recording (unified)

    func toggleRecording() {
        if meetingRecording {
            stopMeetingRecording()
        } else {
            startMeetingRecording()
        }
    }

    func refreshMicStatus() {}

    func toggleMic() {
        toggleRecording()
    }

    // MARK: - Notifications from agent

    func pollNotifications() {
        let path = Self.home + "/notification.json"
        guard FileManager.default.fileExists(atPath: path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let message = obj["message"] as? String, !message.isEmpty else { return }

        try? FileManager.default.removeItem(atPath: path)

        DispatchQueue.main.async {
            self.notificationTitle = obj["title"] as? String ?? "Déjà"
            self.notificationMessage = message
            self.showNotification = true
            NotificationCenter.default.post(name: .agentNotification, object: nil)

            DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
                self.showNotification = false
                NotificationCenter.default.post(name: .notificationDismissed, object: nil)
            }
        }
    }

    // MARK: - Meeting Polling & Recording (delegated to MeetingCoordinator)

    func startMeetingPolling() {
        Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.refreshMeetingPrompt()
            self?.pollNotifications()
            if self?.meetingRecording == true {
                self?.refreshMeetingStatus()
            }
        }
        meetingCoordinator.onAutoStop = { [weak self] in
            self?.stopMeetingRecording()
        }
    }

    func refreshMeetingPrompt() {
        meetingCoordinator.refreshMeetingPrompt(
            isRecording: meetingRecording,
            wasAvailable: meetingAvailable,
            onUpdate: { [weak self] available, title, attendees, timeRange, eventId, isNew in
                guard let self = self else { return }
                self.meetingAvailable = available
                if available {
                    self.meetingTitle = title
                    self.meetingAttendees = attendees
                    self.meetingTimeRange = timeRange
                    if isNew {
                        NotificationCenter.default.post(name: .meetingDetected, object: nil)
                    }
                } else {
                    self.meetingTitle = ""
                    self.meetingAttendees = []
                }
            },
            onDismiss: { NotificationCenter.default.post(name: .meetingDismissed, object: nil) }
        )
    }

    func refreshMeetingStatus() {
        meetingCoordinator.refreshMeetingStatus { [weak self] elapsed in
            self?.meetingElapsed = elapsed
        }
    }

    func startMeetingRecording() {
        guard !meetingRecording else { return }

        let title = meetingTitle.isEmpty ? "" : meetingTitle
        let attendees = meetingAttendees.isEmpty ? [] : meetingAttendees
        meetingLinked = !title.isEmpty
        meetingNotes = ""

        meetingCoordinator.startRecording(title: title, attendees: attendees) { [weak self] sessionId in
            guard let self = self else { return }
            self.meetingRecording = true
            self.meetingSessionId = sessionId
            self.meetingStartTime = Date()
            self.meetingElapsed = 0

            self.meetingTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
                guard let self = self, let start = self.meetingStartTime else { return }
                self.meetingElapsed = Date().timeIntervalSince(start)
            }
        }
    }

    func unlinkMeeting() {
        meetingLinked = false
        meetingTitle = ""
        meetingAttendees = []
        meetingCoordinator.unlinkMeeting()
    }

    func pauseMeetingRecording() {
        meetingCoordinator.pauseOrResume(isPaused: meetingPaused, sessionId: meetingSessionId)
        meetingPaused = !meetingPaused
    }

    func stopMeetingRecording() {
        guard meetingRecording else { return }

        let notes = meetingNotes
        meetingTimer?.invalidate()
        meetingTimer = nil
        meetingRecording = false
        meetingPaused = false
        meetingProcessing = true
        NotificationCenter.default.post(name: .meetingDetected, object: nil)

        meetingCoordinator.stopRecording(notes: notes) { [weak self] in
            self?.meetingProcessing = false
            self?.meetingAvailable = false
            self?.meetingSessionId = ""
            self?.meetingStartTime = nil
            self?.meetingElapsed = 0
            self?.meetingNotes = ""
            NotificationCenter.default.post(name: .meetingDismissed, object: nil)
        }
    }

    static func formatTimeRange(start: String, end: String) -> String {
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        let outFmt = DateFormatter()
        outFmt.dateFormat = "h:mm a"

        func parse(_ iso: String) -> Date? {
            let clean = String(iso.prefix(19))
            return df.date(from: clean)
        }

        guard let s = parse(start), let e = parse(end) else {
            return ""
        }
        return "\(outFmt.string(from: s)) - \(outFmt.string(from: e))"
    }

    // MARK: - MCP Clients (Connected AI Assistants)

    func fetchMCPClients() {
        localAPICall("/api/mcp/clients") { [weak self] data, _ in
            guard let self = self,
                  let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let arr = obj["clients"] as? [[String: Any]] else { return }
            let decoded: [MCPClientInfo] = arr.compactMap { dict in
                guard let name = dict["name"] as? String,
                      let installed = dict["installed"] as? Bool,
                      let enabled = dict["enabled"] as? Bool,
                      let configPath = dict["config_path"] as? String,
                      let autoConf = dict["auto_configurable"] as? Bool else { return nil }
                return MCPClientInfo(
                    name: name,
                    installed: installed,
                    enabled: enabled,
                    config_path: configPath,
                    auto_configurable: autoConf,
                    note: (dict["note"] as? String) ?? ""
                )
            }
            DispatchQueue.main.async {
                self.mcpClients = decoded
            }
        }
    }

    func setMCPClientEnabled(_ clientName: String, enabled: Bool) {
        // Optimistically update the UI so the toggle feels instant.
        let previous = mcpClients
        if let idx = mcpClients.firstIndex(where: { $0.name == clientName }) {
            mcpClients[idx].enabled = enabled
        }
        mcpClientErrors[clientName] = nil

        let payload: [String: Any] = ["client_name": clientName, "enabled": enabled]
        let body = try? JSONSerialization.data(withJSONObject: payload)
        localAPICall("/api/mcp/clients/toggle", method: "POST", body: body, timeoutInterval: 10) { [weak self] data, error in
            guard let self = self else { return }

            // Revert on transport error.
            if let error = error {
                DispatchQueue.main.async {
                    self.mcpClients = previous
                    self.mcpClientErrors[clientName] = error.localizedDescription
                }
                return
            }

            // Revert on backend error response (has "error" field).
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let errMsg = obj["error"] as? String {
                DispatchQueue.main.async {
                    self.mcpClients = previous
                    self.mcpClientErrors[clientName] = errMsg
                }
                return
            }

            // Success — refetch to pick up authoritative state.
            DispatchQueue.main.async {
                self.fetchMCPClients()
            }
        }
    }

    // MARK: - Voice Pill (hold-to-talk)

    func startVoiceCapture() {
        guard !voicePillActive, !voicePillProcessing else { return }
        voicePillActive = true
        voicePillTranscript = ""

        // Reset the bar history so the pill starts at rest. New RMS
        // samples will flow in via recordVoiceLevel() as VoiceRecorder's
        // tap fires. Start the mic recorder by POSTing to the backend
        // (which in turn writes the voice_cmd.json marker that the
        // in-process VoiceCommandDispatcher picks up).
        levelHistory = Array(repeating: 0, count: 16)
        localAPICall("/api/mic/start", method: "POST", timeoutInterval: 5) { _, _ in }
    }

    /// Called from VoiceCommandDispatcher's level callback for every
    /// audio buffer the VoiceRecorder tap processes. Shifts levelHistory
    /// left and appends the new level so VoicePillView's bars march
    /// right-to-left as new samples arrive.
    func recordVoiceLevel(_ level: CGFloat) {
        var next = levelHistory
        next.removeFirst()
        next.append(level)
        levelHistory = next
    }

    func stopVoiceCapture() {
        guard voicePillActive else { return }
        voicePillActive = false
        levelHistory = Array(repeating: 0, count: 16)

        // Show processing state
        voicePillProcessing = true
        voicePillStatus = "Transcribing..."
        voicePillTranscript = ""

        // Stop recording and run the transcript through the command
        // classifier (backend routes mic transcripts through the same
        // /api/command dispatch path, so voice can emit goal_actions —
        // calendar/email/task — not just observations).
        localAPICall("/api/mic/stop", method: "POST", timeoutInterval: 60) { [weak self] data, error in
            guard let self = self else { return }

            if let error = error {
                DispatchQueue.main.async {
                    self.voicePillProcessing = false
                    self.voicePillTranscript = ""
                    self.commandToast = Toast(
                        style: .error,
                        text: error.localizedDescription
                    )
                    self.scheduleToastDismiss()
                }
                return
            }

            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                DispatchQueue.main.async {
                    self.voicePillProcessing = false
                    self.voicePillTranscript = ""
                    self.commandToast = Toast(
                        style: .error,
                        text: "Empty response from backend"
                    )
                    self.scheduleToastDismiss()
                }
                return
            }

            // Structured failure: transcription dropped as hallucination,
            // classifier blew up, dispatch blew up, etc. Surface as a red
            // toast in the same place as submitCommand() errors.
            if let ok = obj["ok"] as? Bool, ok == false {
                let detail = (obj["detail"] as? String)
                    ?? (obj["error"] as? String)
                    ?? "Voice command failed."
                DispatchQueue.main.async {
                    self.voicePillProcessing = false
                    self.voicePillTranscript = ""
                    self.commandToast = Toast(style: .error, text: detail)
                    self.scheduleToastDismiss()
                }
                return
            }

            let transcript = (obj["transcript"] as? String) ?? ""
            guard !transcript.isEmpty else {
                DispatchQueue.main.async {
                    self.voicePillProcessing = false
                    self.voicePillTranscript = ""
                }
                return
            }

            let confirmation = (obj["confirmation"] as? String) ?? ""
            let cmdType = (obj["type"] as? String) ?? ""

            DispatchQueue.main.async {
                self.voicePillProcessing = false
                self.voicePillTranscript = transcript

                // Force a fresh screenshot — the user just spoke, so this
                // is the moment we want vision to see (and the next vision
                // cycle will correlate the voice context with it).
                self.captureScreenshot(force: true)

                // Show the transcript briefly on the pill, then clear.
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                    self.voicePillTranscript = ""
                }

                // Expand the notch to show the classification banner +
                // the result. Context-type voice notes stay quiet — they
                // don't deserve their own panel pop-up because they're
                // just observations the next integrate cycle will
                // process. Query-type responses pin the panel open until
                // the user dismisses it (the answer is worth reading).
                // Everything else (action/goal/automation) auto-collapses
                // after ~4 seconds.
                if !confirmation.isEmpty && cmdType != "context" {
                    self.showResponse(
                        type: cmdType,
                        message: confirmation,
                        isQuery: cmdType == "query"
                    )
                }

                // Refresh the activity feed so downstream effects
                // (command row, integrate writes) show up quickly.
                self.fetchActivity()
            }
        }
    }

    // MARK: - Command Center

    /// POST /api/command with the current input. On success, shows a
    /// green toast with the confirmation and refreshes the activity feed.
    /// On failure, shows a red toast and keeps the input populated so the
    /// user can edit and retry.
    func submitCommand() {
        let text = commandInput.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty, !commandPending else { return }

        commandPending = true
        let payload: [String: Any] = ["input": text, "source": "text"]
        let body = try? JSONSerialization.data(withJSONObject: payload)
        localAPICall(
            "/api/command",
            method: "POST",
            body: body,
            timeoutInterval: 60
        ) { [weak self] data, error in
            guard let self = self else { return }
            DispatchQueue.main.async {
                self.commandPending = false
            }
            if let error = error {
                DispatchQueue.main.async {
                    self.commandToast = Toast(
                        style: .error,
                        text: error.localizedDescription
                    )
                    self.scheduleToastDismiss()
                }
                return
            }
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                DispatchQueue.main.async {
                    self.commandToast = Toast(
                        style: .error,
                        text: "Empty response from backend"
                    )
                    self.scheduleToastDismiss()
                }
                return
            }
            if let errMsg = obj["error"] as? String, !errMsg.isEmpty {
                DispatchQueue.main.async {
                    self.commandToast = Toast(style: .error, text: errMsg)
                    self.scheduleToastDismiss()
                }
                return
            }
            if let detail = obj["detail"] as? String, !detail.isEmpty {
                DispatchQueue.main.async {
                    self.commandToast = Toast(style: .error, text: detail)
                    self.scheduleToastDismiss()
                }
                return
            }
            let confirmation = (obj["confirmation"] as? String) ?? "Done."
            let cmdType = (obj["type"] as? String) ?? ""
            DispatchQueue.main.async {
                self.commandInput = ""
                self.showResponse(
                    type: cmdType,
                    message: confirmation,
                    isQuery: cmdType == "query"
                )
                self.fetchActivity()
            }
        }
    }

    private func scheduleToastDismiss() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            self?.commandToast = nil
        }
    }

    /// Fetch the last 50 activity entries from the backend log.
    func fetchActivity() {
        localAPICall("/api/activity?limit=50", method: "GET", timeoutInterval: 5) { [weak self] data, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let raw = obj["entries"] as? [[String: Any]] else { return }
            let parsed: [ActivityEntry] = raw.compactMap { dict in
                guard let ts = dict["timestamp"] as? String,
                      let kind = dict["kind"] as? String,
                      let summary = dict["summary"] as? String else { return nil }
                return ActivityEntry(timestamp: ts, kind: kind, summary: summary)
            }
            DispatchQueue.main.async {
                self?.activityEntries = parsed
            }
        }
    }

    /// Fetch the right-now briefing (due reminders, overdue tasks,
    /// stale waiting-fors). Pure JSON, no LLM — safe to poll on the
    /// same cadence as the activity feed.
    func fetchBriefing() {
        localAPICall("/api/briefing", method: "GET", timeoutInterval: 5) { [weak self] data, _ in
            guard let data = data,
                  let decoded = try? JSONDecoder().decode(Briefing.self, from: data) else { return }
            DispatchQueue.main.async {
                self?.briefing = decoded
            }
        }
    }

    /// Write a JSON snapshot of key UI state to ~/.deja/swift_state.json.
    /// Atomic (temp file + rename) so concurrent readers never see a
    /// half-written file. Best-effort: silently drops on serialization
    /// or IO failure — this is debugging scaffolding, not a feature.
    func writeStateSnapshot() {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let obj: [String: Any] = [
            "updated_at": iso.string(from: Date()),
            "isAdmin": isAdmin,
            "signedInEmail": signedInEmail,
            "signedInName": signedInName,
            "setupNeeded": setupNeeded,
            "isBlocked": isBlocked,
            "hasScreenRecording": hasScreenRecording,
            "hasFullDiskAccess": hasFullDiskAccess,
            "hasAccessibility": hasAccessibility,
            "hasMicrophone": hasMicrophone,
            "voicePillEnabled": voicePillEnabled,
            "voicePillActive": voicePillActive,
            "running": running,
            "meetingAvailable": meetingAvailable,
            "meetingRecording": meetingRecording,
            "backfillRunning": backfillRunning,
            "lastSignalISO": lastSignalISO,
            "signals": signals,
            "matches": matches,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted]) else { return }
        let finalPath = MonitorState.home + "/swift_state.json"
        let tmpPath = finalPath + ".tmp"
        do {
            try data.write(to: URL(fileURLWithPath: tmpPath), options: .atomic)
            _ = rename(tmpPath, finalPath)
        } catch {
            // Best-effort; no logging to avoid feedback loops with swiftLog.
        }
    }

    /// Fetch the user's identity + admin status from the server.
    /// Called once at launch — admin-list membership changes rarely
    /// enough that polling isn't worth the constant network chatter.
    /// If an admin grant is applied to a live app, the user can
    /// relaunch to pick it up (or the tray menu's "Sign out" →
    /// "Sign in" flow refetches when that lands).
    func fetchAdminStatus(attempt: Int = 1) {
        swiftLog(component: "admin", event: "fetch_me", fields: ["attempt": attempt])
        // 20s timeout: /api/me internally does an httpx call to the
        // proxy with its own 5s timeout. If we use 5s here too, we race
        // the proxy call and often read nil when Render is warming up.
        localAPICall("/api/me", method: "GET", timeoutInterval: 20) { [weak self] data, err in
            // Single retry helper shared across every failure path
            // (socket error, nil data, empty body, parse error, HTTP
            // error). Any one of these previously left isAdmin stuck
            // at false forever.
            let retry: () -> Void = {
                if attempt < 10 {
                    let delay = Double(attempt) * 2.0
                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                        self?.fetchAdminStatus(attempt: attempt + 1)
                    }
                }
            }
            if let err = err {
                swiftLog(component: "admin", event: "fetch_me_error", ok: false, fields: ["attempt": attempt, "error": err.localizedDescription])
                retry()
                return
            }
            guard let data = data, !data.isEmpty else {
                swiftLog(component: "admin", event: "fetch_me_nil_data", ok: false, fields: ["attempt": attempt])
                retry()
                return
            }
            let body = String(data: data, encoding: .utf8) ?? "<non-utf8>"
            guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                swiftLog(component: "admin", event: "fetch_me_parse_failed", ok: false, fields: ["attempt": attempt, "body": String(body.prefix(200))])
                retry()
                return
            }
            // The Python endpoint catches its own httpx exceptions and
            // returns `{"is_admin": false, "signed_in": false, "error":
            // "..."}` as valid JSON rather than propagating a 5xx.
            // Without this guard, a transient Render blip on launch
            // poisons isAdmin=false for the entire session.
            let errorField = (obj["error"] as? String) ?? ""
            let signedIn = (obj["signed_in"] as? Bool) ?? true
            if !errorField.isEmpty || !signedIn {
                swiftLog(component: "admin", event: "fetch_me_soft_error", ok: false, fields: ["attempt": attempt, "error": errorField, "signed_in": signedIn])
                retry()
                return
            }
            let admin = (obj["is_admin"] as? Bool) ?? ((obj["is_admin"] as? NSNumber)?.boolValue ?? false)
            let email = (obj["email"] as? String) ?? ""
            let name = (obj["name"] as? String) ?? ""
            swiftLog(component: "admin", event: "fetch_me_ok", fields: ["admin": admin, "email": email])
            DispatchQueue.main.async {
                self?.isAdmin = admin
                self?.signedInEmail = email
                self?.signedInName = name
            }
        }
    }

    /// Open the admin dashboard in the user's default browser.
    /// Resolves the login URL (with embedded OAuth token) via the
    /// local Python backend, then hands off to NSWorkspace.
    func openAdminDashboardInBrowser() {
        localAPICall("/api/admin-dashboard-url", method: "GET", timeoutInterval: 5) { data, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let urlStr = obj["url"] as? String,
                  let url = URL(string: urlStr) else { return }
            DispatchQueue.main.async {
                NSWorkspace.shared.open(url)
            }
        }
    }

    // MARK: - Pill expand / collapse

    /// Toggle the expanded notch panel. Called from the pill click handler.
    func togglePillExpanded() {
        setPillExpanded(!pillExpanded)
    }

    func setPillExpanded(_ expanded: Bool) {
        // Cancel any pending auto-collapse whenever the state is
        // explicitly driven from outside the timer path.
        cancelAutoCollapse()
        // Defense in depth: refuse to expand while structurally
        // blocked. The pill click handler and voice response handler
        // are the primary gates; this catches anything that tries to
        // expand programmatically (e.g. meeting-detected auto-expand)
        // while setup is incomplete.
        if expanded && isBlocked {
            return
        }
        if pillExpanded == expanded { return }
        pillExpanded = expanded
        if expanded {
            // Kick off the briefing + activity poll so the panel has
            // fresh data the moment it's visible — otherwise it would
            // show whatever was cached on last close.
            startActivityPolling()
        } else {
            stopActivityPolling()
            // Clear the transient response banner so the next open
            // doesn't show a stale "Calendar event created" line.
            lastResponseType = ""
            lastResponseMessage = ""
            lastResponseIsQuery = false
            lastResponseAt = nil
            expandedEngagement = false
        }
    }

    /// Show a command/query response in the expanded panel. Called from
    /// the voice dispatch completion and from typed-command responses.
    /// ``type`` is the classification tag (action/goal/automation/context/query).
    /// ``isQuery`` true ⇒ the panel stays open until the user dismisses;
    /// false ⇒ auto-collapse after ``autoCollapseSeconds`` unless engaged.
    func showResponse(type: String, message: String, isQuery: Bool) {
        lastResponseType = type
        lastResponseMessage = message
        lastResponseIsQuery = isQuery
        lastResponseAt = Date()
        if !pillExpanded {
            setPillExpanded(true)
        } else {
            // Already expanded — refresh activity so the new cycle
            // entry shows up in the feed below the banner.
            fetchActivity()
        }
        if !isQuery {
            scheduleAutoCollapse()
        }
    }

    /// Cancel an in-flight auto-collapse (called on hover / focus /
    /// typing in the expanded panel).
    func markEngagement() {
        expandedEngagement = true
        cancelAutoCollapse()
    }

    private func scheduleAutoCollapse() {
        cancelAutoCollapse()
        let seconds = autoCollapseSeconds
        autoCollapseTimer = Timer.scheduledTimer(withTimeInterval: seconds, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            DispatchQueue.main.async {
                // Final engagement check — if the user grabbed focus
                // between the timer firing and now, keep it open.
                if !self.expandedEngagement {
                    self.setPillExpanded(false)
                }
                self.autoCollapseTimer = nil
            }
        }
    }

    private func cancelAutoCollapse() {
        autoCollapseTimer?.invalidate()
        autoCollapseTimer = nil
    }

    /// Start polling the activity feed + briefing every 10s while the popover is open.
    func startActivityPolling() {
        activityTimer?.invalidate()
        fetchActivity()
        fetchBriefing()
        activityTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            self?.fetchActivity()
            self?.fetchBriefing()
        }
    }

    func stopActivityPolling() {
        activityTimer?.invalidate()
        activityTimer = nil
    }
}
