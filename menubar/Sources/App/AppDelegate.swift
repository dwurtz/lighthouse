import SwiftUI
import AppKit
import AVFoundation
import Combine
import ScreenCaptureKit
import Sparkle

// MARK: - App Delegate
//
// Déjà's primary UI surface is the floating **voice pill** at the
// bottom of the screen. The pill has three visual states — idle,
// recording, and expanded — and the expanded state hosts the full
// command center (briefing + activity feed + command input), so
// there is no separate popover or main window. The menu-bar tray
// icon remains only as an escape hatch: right- or left-clicking it
// shows a minimal menu with Settings and Quit. Everything else
// flows through the pill. See ``VoicePillWindow`` +
// ``ExpandedNotchPanel``.
//
// The Python monitor + web backend are spawned and supervised by
// ``MonitorState``.

class AppDelegate: NSObject, NSApplicationDelegate {
    var monitor = MonitorState()
    var statusItem: NSStatusItem!
    var contextMenu: NSMenu!
    var isRecording: Bool = false
    var micStatusTimer: Timer?
    var updaterController: SPUStandardUpdaterController!
    var setupPanelWindow: SetupPanelWindow?
    var settingsPanelWindow: SettingsPanelWindow?
    var voicePillWindow: VoicePillWindow?
    var didSetupVoicePill: Bool = false
    let hotkeyManager = HotkeyManager()
    private var voiceDispatcher: VoiceCommandDispatcher?
    private var healthCancellables = Set<AnyCancellable>()


    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        setupCrashReporting()
        setupStatusItem()       // First — before any work that could trigger menu bar layout
        monitor.start()         // After UI is set up

        // Health service — polls ~/.deja/health.json + native TCC
        // probes and publishes the merged state for the notch chip
        // and the HealthPanel. The chip is the only user-facing
        // signal; we deliberately don't surface a toast on degraded/
        // broken because the chip already communicates it, and a
        // second pill with a fake request_id is just noise. Operational
        // errors with real request IDs still surface via the error
        // toast path. Structural issues (missing permissions) are
        // handled by `monitor.isBlocked` auto-opening the setup panel.
        HealthState.shared.start()

        // Voice recording runs in-process now (was a DejaRecorder
        // subprocess, which didn't hold mic TCC because it has no
        // bundle identity). The dispatcher polls ~/.deja/voice_cmd.json
        // so the Python mic_routes handlers can drive it. The level
        // callback feeds MonitorState.levelHistory so VoicePillView's
        // reactive bars animate from the same audio samples the WAV
        // is written from.
        let monitorRef = monitor
        let dispatcher = VoiceCommandDispatcher { [weak monitorRef] level in
            monitorRef?.recordVoiceLevel(level)
        }
        dispatcher.start()
        voiceDispatcher = dispatcher

        // Force the mic TCC prompt at launch when status is .notDetermined.
        // AVAudioEngine does NOT trigger the prompt on its own — it just
        // returns zero-filled buffers silently when the grant is missing,
        // which is indistinguishable from a working mic until the WAV is
        // inspected. This happens after `tccutil reset Microphone
        // com.deja.app` or on first launch of a re-signed build where
        // the prior grant no longer matches the code signature.
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                NSLog("deja: mic TCC prompt result: granted=\(granted)")
            }
        }

        startMicStatusPolling()

        // Sparkle auto-updater — checks for updates on launch and periodically
        updaterController = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )

        // Auto-open popover on first launch. Poll until the web server
        // is ready, then check what's already configured and skip steps.
        if monitor.setupNeeded {
            // Wait for web server to be ready, then show setup panel
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
                self?.showSetupPanel()
            }
        }

        // Structural-blocked state — auto-show the setup panel whenever
        // Deja loses something it needs to run (permission revoked,
        // Google auth expired, etc.) so the user can't mistake a
        // partial-functionality state for "working." The panel closes
        // itself when every check goes green. Transient operational
        // errors (proxy 502, one failed LLM call) don't flip isBlocked
        // — they surface via the error toast + request id path and
        // never reach here.
        monitor.$isBlocked
            .removeDuplicates()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] blocked in
                guard let self = self else { return }
                if blocked {
                    self.showSetupPanel()
                } else {
                    self.setupPanelWindow?.orderOut(nil)
                }
            }
            .store(in: &healthCancellables)

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleSetupCompleted),
            name: .setupCompleted,
            object: nil
        )

        // Voice pill — persistent floating capsule at bottom of screen.
        // Only instantiate if setup is already complete; otherwise the pill
        // would appear behind/below the setup panel on first launch. The
        // .setupCompleted notification handler will call setupVoicePill()
        // once the wizard finishes.
        if !monitor.setupNeeded {
            setupVoicePill()
        }

        // Show floating pill when a meeting is about to start
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(showMeetingPrompt),
            name: .meetingDetected,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleMeetingDismissed),
            name: .meetingDismissed,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(showNotificationBubble),
            name: .agentNotification,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(dismissNotificationBubble),
            name: .notificationDismissed,
            object: nil
        )
    }

    @objc private func showMeetingPrompt() {
        // A meeting was detected (either imminent from calendar or
        // already running). Expand the notch panel so the user sees
        // the meeting banner + "Take notes" button in the command
        // center instead of a separate popover. The meeting banner
        // is part of ``PopoverContentView`` and keys off
        // ``monitor.meetingAvailable`` + ``monitor.meetingRecording``,
        // so expanding the pill is enough — no extra wiring needed.
        monitor.setPillExpanded(true)
    }

    func dismissMeetingPill() {
        // Collapse the pill unless the user is actively engaged with
        // the expanded panel. Engagement-cancelled auto-collapse is
        // handled inside ``MonitorState.setPillExpanded``.
        if !monitor.expandedEngagement {
            monitor.setPillExpanded(false)
        }
    }

    /// Re-open the setup panel from an external caller (pill click
    /// while blocked, hotkey while blocked). Delegates to the internal
    /// ``showSetupPanel`` so window-reuse and activation stay in one
    /// place.
    func reopenSetupPanel() {
        showSetupPanel()
    }

    private func showSetupPanel() {
        // Reuse the existing panel if the user previously closed it
        // (via the X button) — we only hid it with orderOut(nil), not
        // .close(), so the state is intact.
        if let existing = setupPanelWindow {
            existing.orderFront(nil)
            existing.makeKey()
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let panel = SetupPanelWindow(monitor: monitor)
        panel.orderFront(nil)
        panel.makeKey()
        NSApp.activate(ignoringOtherApps: true)
        setupPanelWindow = panel
    }

    @objc private func handleSetupCompleted() {
        setupPanelWindow?.close()
        setupPanelWindow = nil

        // Now that setup is done, bring up the voice pill / floating UI
        // that we deliberately suppressed during first-run.
        setupVoicePill()
    }

    @objc private func handleMeetingDismissed() {
        dismissMeetingPill()
    }



    var notificationPopover: NSPopover?

    @objc private func showNotificationBubble() {
        // Notification bubbles still anchor to the tray icon so the
        // user notices them even when the pill isn't expanded. This
        // is the one remaining tray-anchored popover — everything
        // else moved to the expanded notch panel.
        guard notificationPopover == nil || !(notificationPopover!.isShown) else { return }
        guard let button = statusItem?.button else { return }

        let view = NotificationBubbleView(monitor: monitor, onDismiss: { [weak self] in
            self?.dismissNotificationBubble()
        })
        .background(Color.black)

        let pop = NSPopover()
        pop.behavior = .semitransient
        pop.animates = true
        pop.contentSize = NSSize(width: 320, height: 60)
        pop.contentViewController = NSHostingController(rootView: view)
        pop.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        notificationPopover = pop
    }

    @objc private func dismissNotificationBubble() {
        notificationPopover?.close()
        notificationPopover = nil
    }

    // MARK: Status item setup

    private func setupStatusItem(attempt: Int = 1) {
        // Use a fixed length so macOS reserves the slot even when the
        // menu bar is crowded. variableLength is a hint that the system
        // can resolve to zero on constrained menu bars (e.g. MacBook Pro
        // notch with many apps installed), and in that case
        // `statusItem.button` comes back nil and the entire setup block
        // below silently no-ops. Fixed length is more aggressive and
        // retries (below) give the menu bar a chance to free up space
        // during the first few seconds of login.
        statusItem = NSStatusBar.system.statusItem(withLength: 22)
        statusItem.isVisible = true

        guard let button = statusItem.button else {
            NSLog(
                "deja: ERROR — NSStatusItem.button is nil (attempt %d). "
                + "Menu bar is likely full — macOS couldn't allocate a "
                + "slot. Will retry in 2s.",
                attempt
            )
            // Retry up to 3 times with a 2s delay — status items that
            // fail at app launch sometimes recover a few seconds later
            // once other apps finish claiming their slots.
            if attempt < 3 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
                    self?.setupStatusItem(attempt: attempt + 1)
                }
            } else {
                NSLog(
                    "deja: ERROR — giving up on status item after 3 attempts. "
                    + "The Déjà tray icon will not appear. Close some menu bar "
                    + "apps to free up a slot, then relaunch Déjà."
                )
            }
            return
        }

        NSLog("deja: status item created on attempt %d", attempt)

        if let resourcePath = Bundle.main.resourcePath {
            let iconURL = URL(fileURLWithPath: resourcePath).appendingPathComponent("tray-icon.png")
            if let img = NSImage(contentsOf: iconURL) {
                img.isTemplate = false
                img.size = NSSize(width: 22, height: 22)
                button.image = img
            } else {
                NSLog("deja: ERROR — tray-icon.png not found at \(iconURL.path)")
                button.title = "Déjà"
            }
        } else {
            NSLog("deja: ERROR — no resourcePath in bundle")
            button.title = "Déjà"
        }
        button.toolTip = "Déjà"
        button.target = self
        button.action = #selector(statusItemClicked(_:))
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])

        // Tray menu — built dynamically per click so the primary item
        // reflects current state ("Resume Setup" vs "Open Déjà").
        contextMenu = NSMenu()
    }

    /// Build the tray icon menu fresh each time it's shown. The tray
    /// is now an escape hatch only — Settings and Quit. The main UI
    /// (briefing, activity, command input) lives in the expanded
    /// notch pill, not in a tray popover.
    private func buildTrayMenu() -> NSMenu {
        let menu = NSMenu()

        // Header: signed-in identity. Disabled item (not clickable)
        // that shows the email Deja is bound to. Helps users notice
        // when they're logged in as the wrong account.
        if !monitor.signedInEmail.isEmpty {
            let label: String
            if !monitor.signedInName.isEmpty {
                label = "\(monitor.signedInName) — \(monitor.signedInEmail)"
            } else {
                label = monitor.signedInEmail
            }
            let identity = NSMenuItem(title: label, action: nil, keyEquivalent: "")
            identity.isEnabled = false
            menu.addItem(identity)
            menu.addItem(NSMenuItem.separator())
        }

        if monitor.setupNeeded {
            let resume = NSMenuItem(
                title: "Resume Setup…",
                action: #selector(showSetupPanelFromMenu),
                keyEquivalent: ""
            )
            resume.target = self
            menu.addItem(resume)
            menu.addItem(NSMenuItem.separator())
        }

        let settings = NSMenuItem(
            title: "Settings…",
            action: #selector(showSettingsFromMenu),
            keyEquivalent: ","
        )
        settings.target = self
        menu.addItem(settings)

        // Admin Dashboard — only shown to users on the server-side
        // DEJA_ADMIN_EMAILS allowlist. Non-admins never see this entry.
        // Admin status is fetched once at launch via /api/me; the menu
        // is rebuilt on every click so it reflects the current state.
        if monitor.isAdmin {
            let admin = NSMenuItem(
                title: "Open Admin Dashboard",
                action: #selector(openAdminDashboard),
                keyEquivalent: ""
            )
            admin.target = self
            menu.addItem(admin)
        }

        menu.addItem(NSMenuItem.separator())

        // Sign out — revokes the Google OAuth token and quits. On next
        // launch the user sees the setup wizard again and must re-auth.
        let signOut = NSMenuItem(
            title: "Sign out…",
            action: #selector(signOut),
            keyEquivalent: ""
        )
        signOut.target = self
        menu.addItem(signOut)

        let quitItem = NSMenuItem(
            title: "Quit Déjà",
            action: #selector(quitApp),
            keyEquivalent: "q"
        )
        quitItem.target = self
        menu.addItem(quitItem)

        return menu
    }

    @objc private func signOut() {
        // Revoke the Google OAuth token + delete the local credentials
        // so the next launch forces a fresh sign-in. Runs on a
        // background thread because gws auth revoke hits the network,
        // then quits cleanly on the main thread so NSApplication's
        // shutdown teardown happens.
        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            task.arguments = ["gws", "auth", "revoke"]
            try? task.run()
            task.waitUntilExit()
            DispatchQueue.main.async {
                NSApplication.shared.terminate(nil)
            }
        }
    }

    @objc private func openAdminDashboard() {
        // Build the login URL with the user's existing Google OAuth
        // token as a query param. The server validates the token via
        // the same code path every /v1/* route uses, checks the email
        // against DEJA_ADMIN_EMAILS, and sets a session cookie before
        // redirecting to /admin. The query-param token only lives in
        // browser history for one redirect — the cookie is what
        // persists, and it's HttpOnly + SameSite=Lax + Secure.
        monitor.openAdminDashboardInBrowser()
    }

    @objc private func showSetupPanelFromMenu() {
        showSetupPanel()
    }

    @objc private func showSettingsFromMenu() {
        showSettingsPanel()
    }

    private func showSettingsPanel() {
        if let existing = settingsPanelWindow {
            existing.orderFront(nil)
            existing.makeKey()
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let panel = SettingsPanelWindow(monitor: monitor)
        panel.orderFront(nil)
        panel.makeKey()
        NSApp.activate(ignoringOtherApps: true)
        settingsPanelWindow = panel
    }

    // MARK: Mic control — polls the shared MonitorState so the popover
    // button and the menu-bar icon both reflect the same recording state.

    private func startMicStatusPolling() {
        micStatusTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.refreshMicStatus()
        }
        refreshMicStatus()
    }

    private func refreshMicStatus() {
        let previous = isRecording
        let now = monitor.meetingRecording
        if now != previous {
            applyMicState(recording: now)
        }
        // Re-assert the icon if macOS dropped it (can happen during capture).
        if let button = statusItem?.button, button.image == nil {
            if let resourcePath = Bundle.main.resourcePath {
                let iconURL = URL(fileURLWithPath: resourcePath).appendingPathComponent("tray-icon.png")
                if let img = NSImage(contentsOf: iconURL) {
                    img.isTemplate = true
                    img.size = NSSize(width: 18, height: 18)
                    button.image = img
                }
            }
        }
    }

    private func applyMicState(recording: Bool) {
        self.isRecording = recording
        // Icon stays the same — the pill shows recording state via
        // its waveform animation. The tray item is a static escape
        // hatch and no longer reflects live recording state.
    }

    @objc private func toggleMic() {
        // The context-menu item routes through the same MonitorState call
        // as the popover button so both surfaces stay in sync.
        monitor.toggleMic()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.refreshMicStatus()
        }
    }

    // MARK: Click handling

    @objc private func statusItemClicked(_ sender: Any?) {
        // The tray icon is a minimal escape hatch: both left and
        // right click just show the Settings/Quit menu. The main
        // UI lives in the expanded notch pill.
        let menu = buildTrayMenu()
        statusItem.menu = menu
        statusItem.button?.performClick(nil)
        statusItem.menu = nil
    }

    // MARK: Menu actions

    @objc private func openWiki() {
        let path = NSString(string: "~/Deja").expandingTildeInPath
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    @objc private func checkForUpdates() {
        updaterController.checkForUpdates(nil)
    }

    @objc private func restartMonitor() {
        monitor.restart()
    }

    // MARK: Voice pill + hotkey

    private func setupVoicePill() {
        guard !didSetupVoicePill else { return }
        didSetupVoicePill = true
        monitor.loadVoicePillState()

        if monitor.voicePillEnabled {
            showVoicePill()
        }

        hotkeyManager.onKeyDown = { [weak self] in
            guard let self = self else { return }
            // Voice capture is gated while Deja is structurally
            // blocked — no point recording when the agent can't
            // process the transcript. Reopen the setup panel so the
            // user can see why their voice command didn't fire.
            if self.monitor.isBlocked {
                self.reopenSetupPanel()
                return
            }
            self.monitor.startVoiceCapture()
        }
        hotkeyManager.onKeyUp = { [weak self] in
            self?.monitor.stopVoiceCapture()
        }
        hotkeyManager.start()

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(voicePillToggled),
            name: .voicePillToggled,
            object: nil
        )
    }

    private func showVoicePill() {
        guard voicePillWindow == nil else { return }
        let window = VoicePillWindow(monitor: monitor)
        window.orderFront(nil)
        voicePillWindow = window
    }

    private func hideVoicePill() {
        voicePillWindow?.close()
        voicePillWindow = nil
    }

    @objc private func voicePillToggled(_ notification: Foundation.Notification) {
        let enabled = notification.userInfo?["enabled"] as? Bool ?? true
        if enabled {
            showVoicePill()
        } else {
            hideVoicePill()
        }
    }

    @objc private func quitApp() {
        hotkeyManager.stop()
        monitor.stop()
        cleanupSensitiveFiles()
        NSApp.terminate(nil)
    }

    /// Remove screenshots and other sensitive transient files on quit.
    private func cleanupSensitiveFiles() {
        let home = MonitorState.home
        let fm = FileManager.default
        for pattern in ["latest_screen.png", "screen_1.png", "screen_2.png", "screen_3.png", "screen_4.png"] {
            try? fm.removeItem(atPath: home + "/" + pattern)
        }
    }

    // MARK: - Crash Reporting

    private func setupCrashReporting() {
        // Catch uncaught ObjC/Swift exceptions
        NSSetUncaughtExceptionHandler { exception in
            CrashReporter.report(
                type: "uncaught_exception",
                name: exception.name.rawValue,
                reason: exception.reason ?? "unknown",
                stackTrace: exception.callStackSymbols.joined(separator: "\n")
            )
        }

        // Catch fatal signals (SIGTRAP, SIGABRT, SIGSEGV, SIGBUS, SIGFPE, SIGILL)
        // These cover crashes that exceptions don't — like the NSPanel constraint crash
        let signals: [Int32] = [SIGTRAP, SIGABRT, SIGSEGV, SIGBUS, SIGFPE, SIGILL]
        for sig in signals {
            signal(sig) { signum in
                // In a signal handler, only async-signal-safe functions are allowed.
                // We write directly to a file descriptor — no malloc, no ObjC.
                let name: String
                switch signum {
                case SIGTRAP:  name = "SIGTRAP"
                case SIGABRT:  name = "SIGABRT"
                case SIGSEGV:  name = "SIGSEGV"
                case SIGBUS:   name = "SIGBUS"
                case SIGFPE:   name = "SIGFPE"
                case SIGILL:   name = "SIGILL"
                default:       name = "SIG\(signum)"
                }

                // Write a minimal crash file synchronously (async-signal-safe)
                let dir = NSHomeDirectory() + "/.deja/crash-reports"
                mkdir(dir, 0o755)
                let ts = Int(Date().timeIntervalSince1970)
                let path = "\(dir)/signal-\(ts).json"
                let json = """
                {"type":"signal","name":"\(name)","reason":"Fatal signal \(signum)","timestamp":"\(ts)","app_version":"0.2.0"}
                """
                if let fd = fopen(path, "w") {
                    fputs(json, fd)
                    fclose(fd)
                }

                // Re-raise to get the default crash behavior (generates .ips file)
                signal(signum, SIG_DFL)
                raise(signum)
            }
        }

        // Send any crash reports from previous sessions
        CrashReporter.sendPendingReports()
    }
}

// MARK: - Crash Reporter

enum CrashReporter {
    private static let pendingDir = NSHomeDirectory() + "/.deja/crash-reports"

    /// Save a crash report to disk (called from signal/exception handlers).
    static func report(type: String, name: String, reason: String, stackTrace: String) {
        let fm = FileManager.default
        try? fm.createDirectory(atPath: pendingDir, withIntermediateDirectories: true)

        let report: [String: Any] = [
            "type": type,
            "name": name,
            "reason": reason,
            "stack_trace": String(stackTrace.prefix(2000)),
            "timestamp": ISO8601DateFormatter().string(from: Date()),
            "app_version": Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown",
            "os_version": ProcessInfo.processInfo.operatingSystemVersionString,
        ]

        if let data = try? JSONSerialization.data(withJSONObject: report),
           let json = String(data: data, encoding: .utf8) {
            let filename = "\(pendingDir)/crash-\(Int(Date().timeIntervalSince1970)).json"
            try? json.write(toFile: filename, atomically: true, encoding: .utf8)
        }
    }

    /// On next launch, send any saved crash reports to the telemetry server.
    static func sendPendingReports() {
        DispatchQueue.global(qos: .utility).async {
            let fm = FileManager.default
            guard let files = try? fm.contentsOfDirectory(atPath: pendingDir) else { return }

            for file in files where file.hasSuffix(".json") {
                let path = "\(pendingDir)/\(file)"
                guard let data = fm.contents(atPath: path),
                      let report = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    try? fm.removeItem(atPath: path)
                    continue
                }

                // Send to telemetry endpoint
                guard let url = URL(string: "https://deja-api.onrender.com/v1/telemetry") else { continue }
                var request = URLRequest(url: url)
                request.httpMethod = "POST"
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.timeoutInterval = 10

                let body: [String: Any] = [
                    "event": "crash",
                    "properties": report,
                    "client_version": report["app_version"] as? String ?? "unknown",
                ]
                request.httpBody = try? JSONSerialization.data(withJSONObject: body)

                let sem = DispatchSemaphore(value: 0)
                URLSession.shared.dataTask(with: request) { _, _, _ in
                    sem.signal()
                }.resume()
                _ = sem.wait(timeout: .now() + 10)

                // Remove after sending (or attempting)
                try? fm.removeItem(atPath: path)
            }
        }
    }
}
