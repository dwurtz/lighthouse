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
    let hotkeyManager = HotkeyManager()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
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

        // Voice pill — persistent floating capsule at bottom of screen
        setupVoicePill()

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
        let panel = SetupPanelWindow(monitor: monitor)
        panel.orderFront(nil)
        setupPanelWindow = panel
    }

    @objc private func handleSetupCompleted() {
        setupPanelWindow?.close()
        setupPanelWindow = nil
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

        // Right-click menu — just Quit. Everything else is in Settings.
        let menu = NSMenu()
        let quitItem = NSMenuItem(title: "Quit Déjà", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        contextMenu = menu
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
        guard let event = NSApp.currentEvent else { return }
        if event.type == .rightMouseUp || event.modifierFlags.contains(.control) {
            // Right-click or ctrl-click: show the options menu
            statusItem.menu = contextMenu
            statusItem.button?.performClick(nil)
            // Unset the menu immediately so the next left-click opens the popover
            statusItem.menu = nil
        } else {
            togglePopover()
        }
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
        // Delete all screenshot files
        for pattern in ["latest_screen.png", "screen_1.png", "screen_2.png", "screen_3.png", "screen_4.png"] {
            try? fm.removeItem(atPath: home + "/" + pattern)
        }
    }
}
