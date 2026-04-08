import SwiftUI
import AppKit
import ScreenCaptureKit

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

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        monitor.start()
        setupPopover()
        setupStatusItem()
        startMicStatusPolling()

        // Auto-open popover on first launch. Poll until the web server
        // is ready, then check what's already configured and skip steps.
        if monitor.setupNeeded {
            pollForWebServerThenShowWizard()
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

    private func showWizardPopover() {
        guard let button = statusItem?.button else { return }
        if !popover.isShown {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    @objc private func handleMeetingDismissed() {
        dismissMeetingPill()
    }

    private func pollForWebServerThenShowWizard(attempts: Int = 0) {
        guard let url = URL(string: "http://localhost:5055/api/setup/status") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2

        URLSession.shared.dataTask(with: req) { [weak self] data, _, error in
            guard let self = self else { return }

            if error != nil || data == nil {
                // Server not ready yet — retry up to 15 times (30 seconds)
                if attempts < 15 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                        self.pollForWebServerThenShowWizard(attempts: attempts + 1)
                    }
                }
                return
            }

            // Server is up — check status and show wizard
            DispatchQueue.main.async {
                self.monitor.checkSetupStatus()

                // Give the status check a moment to update setupStep
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    self.showWizardPopover()
                }
            }
        }.resume()
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
            // Load tray icon from the app bundle. No fallback — if
            // this fails, the icon is missing from the build and we
            // need to know about it immediately.
            if let resourcePath = Bundle.main.resourcePath {
                let iconURL = URL(fileURLWithPath: resourcePath).appendingPathComponent("tray-icon.png")
                if let img = NSImage(contentsOf: iconURL) {
                    img.isTemplate = false
                    img.size = NSSize(width: 22, height: 22)
                    button.image = img
                } else {
                    NSLog("deja: ERROR — tray-icon.png not found in app bundle at \(iconURL.path)")
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

        // Context menu shown on right-click (assigned programmatically when needed)
        let menu = NSMenu()

        // Mic toggle — label flips based on recording state, updated live
        // by startMicStatusPolling().
        micToggleItem = NSMenuItem(title: "Start Listening", action: #selector(toggleMic), keyEquivalent: "l")
        micToggleItem.target = self
        menu.addItem(micToggleItem)
        menu.addItem(NSMenuItem.separator())

        let wikiItem = NSMenuItem(title: "Open Wiki in Finder", action: #selector(openWiki), keyEquivalent: "")
        wikiItem.target = self
        menu.addItem(wikiItem)
        menu.addItem(NSMenuItem.separator())
        let restartItem = NSMenuItem(title: "Restart Monitor", action: #selector(restartMonitor), keyEquivalent: "r")
        restartItem.target = self
        menu.addItem(restartItem)
        menu.addItem(NSMenuItem.separator())
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
                    img.isTemplate = false
                    img.size = NSSize(width: 22, height: 22)
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

    @objc private func restartMonitor() {
        monitor.restart()
    }

    @objc private func quitApp() {
        monitor.stop()
        NSApp.terminate(nil)
    }
}
