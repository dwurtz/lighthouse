import SwiftUI
import AppKit
import ScreenCaptureKit
import Sparkle

// MARK: - App Delegate
//
// Déjà is a pure menu-bar app. No notch, no floating panels. The
// icon in the menu bar is the entire UI surface: left-click
// opens a popover with Chat + Activity, right-click shows the options
// menu. The Python monitor + web backend are spawned and supervised by
// MonitorState.

class AppDelegate: NSObject, NSApplicationDelegate {
    var monitor = MonitorState()
    var statusItem: NSStatusItem!
    var popover: NSPopover!
    var contextMenu: NSMenu!
    var micToggleItem: NSMenuItem!
    var isRecording: Bool = false
    var micStatusTimer: Timer?
    var updaterController: SPUStandardUpdaterController!
    var setupPanelWindow: SetupPanelWindow?
    var voicePillWindow: VoicePillWindow?
    var didSetupVoicePill: Bool = false
    let hotkeyManager = HotkeyManager()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        setupCrashReporting()
        setupStatusItem()       // First — before any work that could trigger menu bar layout
        setupPopover()
        monitor.start()         // After UI is set up
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

    var meetingPillPopover: NSPopover?

    @objc private func showMeetingPrompt() {
        // Show a mini popover from the tray icon — same style as the main app
        guard meetingPillPopover == nil || !(meetingPillPopover!.isShown) else { return }
        guard let button = statusItem?.button else { return }

        let pillView = MeetingPillView(monitor: monitor, onDismiss: { [weak self] in
            self?.dismissMeetingPill()
        })
        .frame(width: 340)
        .background(Color.black)

        let pill = NSPopover()
        pill.behavior = .semitransient  // stays until user clicks away or dismisses
        pill.animates = true
        pill.contentSize = NSSize(width: 340, height: 56)
        pill.contentViewController = NSHostingController(rootView: pillView)
        pill.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)

        meetingPillPopover = pill
    }

    func dismissMeetingPill() {
        meetingPillPopover?.close()
        meetingPillPopover = nil
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

    // MARK: Popover setup

    private func setupPopover() {
        popover = NSPopover()
        popover.behavior = .transient          // auto-dismiss on click outside
        popover.animates = true
        popover.contentSize = NSSize(width: 420, height: 600)
        popover.contentViewController = NSHostingController(
            rootView: PopoverContentView(monitor: monitor)
        )
    }

    // MARK: Status item setup

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
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
        }

        // Tray menu — built dynamically per click so the primary item
        // reflects current state ("Resume Setup" vs "Open Déjà").
        contextMenu = NSMenu()
    }

    /// Build the tray icon menu fresh each time it's shown so the
    /// primary action reflects whether setup is incomplete (Resume Setup)
    /// or complete (Open Déjà).
    private func buildTrayMenu() -> NSMenu {
        let menu = NSMenu()

        if monitor.setupNeeded {
            let resume = NSMenuItem(
                title: "Resume Setup…",
                action: #selector(showSetupPanelFromMenu),
                keyEquivalent: ""
            )
            resume.target = self
            menu.addItem(resume)
        } else {
            let open = NSMenuItem(
                title: "Open Déjà",
                action: #selector(openPopoverFromMenu),
                keyEquivalent: ""
            )
            open.target = self
            menu.addItem(open)
        }

        menu.addItem(NSMenuItem.separator())

        let quitItem = NSMenuItem(
            title: "Quit Déjà",
            action: #selector(quitApp),
            keyEquivalent: "q"
        )
        quitItem.target = self
        menu.addItem(quitItem)

        return menu
    }

    @objc private func showSetupPanelFromMenu() {
        showSetupPanel()
    }

    @objc private func openPopoverFromMenu() {
        togglePopover()
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
        self.micToggleItem.title = recording ? "Stop Recording" : "Take Notes"
        // Icon stays the same — the popover shows recording state via the red timer.
        // Swapping the icon caused macOS to reposition the status item.
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
        // Both left and right click show the same dynamic menu. The
        // first item (Resume Setup / Open Déjà) is the primary action;
        // Quit always lives below the separator. This is the only
        // discoverable way for users to find Quit on a borderless
        // LSUIElement app — right-click on tray icons is invisible
        // unless you already know the convention.
        let menu = buildTrayMenu()
        statusItem.menu = menu
        statusItem.button?.performClick(nil)
        statusItem.menu = nil
    }

    private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            // Activate the app so TextField focus works for typing
            NSApp.activate(ignoringOtherApps: true)
            popover.contentViewController?.view.window?.makeKey()
        }
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
            self?.monitor.startVoiceCapture()
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
