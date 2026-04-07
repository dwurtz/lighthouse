import SwiftUI
import AppKit
import Foundation
import CoreGraphics
import ImageIO
import ScreenCaptureKit
import SQLite3

// MARK: - App Entry Point

@main
struct DejaApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        Settings { EmptyView() }
    }
}

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
                    guard let button = self.statusItem?.button else { return }
                    if !self.popover.isShown {
                        self.popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
                        NSApp.activate(ignoringOtherApps: true)
                        self.popover.contentViewController?.view.window?.makeKey()
                    }
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

    // Load the custom icon from a stable path OUTSIDE the app
    // bundle (~/.deja/icon.png). This is critical for macOS TCC
    // stability: modifying files inside Contents/Resources/ changes the
    // bundle's sealed-resources hash and therefore its CDHash, which
    // invalidates user-granted permissions like Screen Recording.
    //
    // By reading the icon from an external path, the app binary is never
    // touched when the icon changes — users can drop a new PNG into
    // ~/.deja/icon.png and restart the app, and macOS still sees
    // the same signed bundle.
    //
    // Falls back to ~/.deja/icon@2x.png for retina, then to a chain
    // of SF Symbols if the user-provided files don't exist.
    private static let fallbackSymbolNames = [
        "rays",                    // radial beam pattern
        "flashlight.on.fill",      // literal beam of light
        "location.north.fill",     // navigation beacon
        "sparkles",                // last-resort generic
    ]

    private var iconPath: URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".deja/icon.png")
    }

    private func loadPreferredIcon() -> NSImage? {
        // Prefer the user-provided icon at ~/.deja/icon.png
        if FileManager.default.fileExists(atPath: iconPath.path),
           let img = NSImage(contentsOf: iconPath) {
            img.isTemplate = true
            // Menu bar expects a ~22pt image; PIL renders it at 22×22 native
            img.size = NSSize(width: 22, height: 22)
            return img
        }
        for name in Self.fallbackSymbolNames {
            if let img = NSImage(systemSymbolName: name, accessibilityDescription: "Déjà") {
                return img
            }
        }
        return nil
    }

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            if let img = loadPreferredIcon() {
                img.isTemplate = true
                button.image = img
            } else {
                button.title = "L"
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
        // Always re-assert the icon — ScreenCaptureKit's recording indicator
        // can cause macOS to drop our status item icon during capture.
        if let button = statusItem?.button, button.image == nil {
            if let img = loadPreferredIcon() {
                img.isTemplate = true
                button.image = img
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
        let path = NSString(string: "~/Deja Wiki").expandingTildeInPath
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

// MARK: - Popover Content

struct PopoverContentView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        Group {
            if monitor.setupNeeded {
                SetupWizardView(monitor: monitor)
            } else {
                content
            }
        }
        .frame(width: 420, height: 600)
        .background(Color.black)
    }

    var content: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Image(systemName: "sparkles")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Text("Déjà")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Spacer()
                HStack(spacing: 10) {
                    // Take notes button. During recording
                    // shows elapsed time. Captures system audio + mic.
                    if monitor.meetingProcessing {
                        HStack(spacing: 5) {
                            ProgressView()
                                .scaleEffect(0.5)
                                .frame(width: 12, height: 12)
                            Text("Generating...")
                                .font(.system(size: 11, weight: .medium))
                        }
                        .foregroundColor(.white.opacity(0.4))
                        .padding(.horizontal, 9)
                        .padding(.vertical, 4)
                    } else {
                    Button(action: { monitor.toggleRecording() }) {
                        HStack(spacing: 5) {
                            Image(systemName: monitor.meetingRecording ? "stop.circle.fill" : "mic.fill")
                                .font(.system(size: 11, weight: .semibold))
                            if monitor.meetingRecording {
                                Text(formatDuration(monitor.meetingElapsed))
                                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                            } else {
                                Text("Take notes")
                                    .font(.system(size: 11, weight: .medium))
                            }
                        }
                        .foregroundColor(monitor.meetingRecording ? .red : .white.opacity(0.85))
                        .padding(.horizontal, 9)
                        .padding(.vertical, 4)
                        .background(
                            monitor.meetingRecording
                                ? Color.red.opacity(0.15)
                                : Color.white.opacity(0.08)
                        )
                        .overlay(
                            Capsule().stroke(
                                monitor.meetingRecording ? Color.red.opacity(0.4) : Color.white.opacity(0.15),
                                lineWidth: 1
                            )
                        )
                        .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    }

                    Circle()
                        .fill(monitor.running ? .green : .red)
                        .frame(width: 6, height: 6)
                    Button(action: { monitor.restart() }) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 11))
                            .foregroundColor(.white.opacity(0.4))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(Color.black)

            Divider().background(Color.white.opacity(0.1))

            // Meeting prompt banner — shows when a calendar meeting is
            // imminent/active and we're not recording yet
            if monitor.meetingAvailable && !monitor.meetingRecording {
                HStack(spacing: 10) {
                    Image(systemName: "calendar")
                        .font(.system(size: 12))
                        .foregroundColor(.orange)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(monitor.meetingTitle)
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white)
                            .lineLimit(1)
                        if !monitor.meetingAttendees.isEmpty {
                            Text(monitor.meetingAttendees.prefix(3).joined(separator: ", "))
                                .font(.system(size: 9))
                                .foregroundColor(.white.opacity(0.4))
                                .lineLimit(1)
                        }
                    }
                    Spacer()
                    Button(action: { monitor.startMeetingRecording() }) {
                        HStack(spacing: 4) {
                            Image(systemName: "mic.fill")
                                .font(.system(size: 9))
                            Text("Take notes")
                                .font(.system(size: 11, weight: .semibold))
                        }
                        .foregroundColor(.white)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Color.orange.opacity(0.25))
                        .overlay(Capsule().stroke(Color.orange.opacity(0.5), lineWidth: 1))
                        .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(Color.orange.opacity(0.06))
            }

            // Meeting context banner — shows when recording and linked
            // to a calendar event. User can disconnect to keep recording
            // without the meeting context.
            if monitor.meetingRecording && monitor.meetingLinked {
                HStack(spacing: 8) {
                    Image(systemName: "link")
                        .font(.system(size: 9))
                        .foregroundColor(.green.opacity(0.7))
                    Text(monitor.meetingTitle)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white.opacity(0.7))
                        .lineLimit(1)
                    if !monitor.meetingAttendees.isEmpty {
                        Text("· " + monitor.meetingAttendees.prefix(2).joined(separator: ", "))
                            .font(.system(size: 10))
                            .foregroundColor(.white.opacity(0.35))
                            .lineLimit(1)
                    }
                    Spacer()
                    Button(action: { monitor.unlinkMeeting() }) {
                        Text("Unlink")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundColor(.white.opacity(0.4))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .overlay(Capsule().stroke(Color.white.opacity(0.15), lineWidth: 1))
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
                .background(Color.green.opacity(0.05))
            }

            // Tab content
            VStack(spacing: 0) {
                // Tab bar
                HStack(spacing: 0) {
                    tabButton("Chat", icon: "message.fill", tab: .chat)
                    tabButton("Activity", icon: "list.bullet", tab: .activity)
                }
                .padding(.horizontal, 12)
                .padding(.top, 8)

                // Content
                ScrollView(.vertical, showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 10) {
                        switch monitor.activeTab {
                        case .chat:
                            chatTab
                        case .activity:
                            activityTab
                        }
                    }
                    .padding(12)
                }
                .background(Color.black)
            }
            .background(Color.black)

            // Chat input (always visible)
            chatInputBar
        }
        .background(Color.black)
    }

    func tabButton(_ title: String, icon: String, tab: NotchTab) -> some View {
        Button(action: { monitor.activeTab = tab }) {
            HStack(spacing: 4) {
                Image(systemName: icon)
                    .font(.system(size: 10))
                Text(title)
                    .font(.system(size: 11, weight: .medium))
            }
            .foregroundColor(monitor.activeTab == tab ? .white : .white.opacity(0.35))
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(monitor.activeTab == tab ? Color.white.opacity(0.1) : Color.clear)
            .clipShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    // MARK: Chat Tab


    // MARK: Chat Tab

    var chatTab: some View {
        VStack(alignment: .leading, spacing: 8) {
            if monitor.chatMessages.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "bubble.left.and.bubble.right")
                        .font(.system(size: 24))
                        .foregroundColor(.white.opacity(0.15))
                    Text("Ask your agent anything")
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.3))
                    Text("It knows your goals, signals, and memory")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.2))
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 40)
            } else {
                ForEach(monitor.chatMessages.suffix(10), id: \.id) { msg in
                    if msg.role == "user" {
                        HStack {
                            Spacer()
                            Text(msg.content)
                                .font(.system(size: 12))
                                .foregroundColor(.white)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .background(Color.blue.opacity(0.4))
                                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        }
                    } else {
                        Text(msg.content)
                            .font(.system(size: 12))
                            .foregroundColor(.white.opacity(0.85))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(Color.white.opacity(0.06))
                            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }

    // MARK: Activity Tab

    var activityTab: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Live signal pulse — proves the monitor is picking up activity in real time
            TimelineView(.periodic(from: .now, by: 1)) { _ in
                HStack(spacing: 8) {
                    Circle()
                        .fill(monitor.lastSignalISO.isEmpty ? Color.gray : Color.green)
                        .frame(width: 6, height: 6)
                        .shadow(color: .green.opacity(0.6), radius: monitor.lastSignalISO.isEmpty ? 0 : 3)
                    Text(monitor.lastSignalISO.isEmpty ? "no signals yet" :
                         "\(formatTimestamp(monitor.lastSignalISO, relative: true)) · \(monitor.lastSignalSource)")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundColor(.white.opacity(0.7))
                    if !monitor.lastSignalPreview.isEmpty {
                        Text("— \(monitor.lastSignalPreview)")
                            .font(.system(size: 10))
                            .foregroundColor(.white.opacity(0.35))
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                    Spacer()
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(Color.green.opacity(0.06))
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .stroke(Color.green.opacity(0.15), lineWidth: 1)
                )
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }

            // Live signals — only messages and conversations, not emails/screenshots
            let importantSignals = monitor.diverseSignals.filter { s in
                s.source == "imessage" || s.source == "whatsapp" || s.source == "calendar"
                || (s.source == "email" && !s.text.lowercased().contains("unsubscribe")
                    && !s.text.lowercased().contains("no-reply")
                    && !s.text.lowercased().contains("noreply"))
            }
            if !importantSignals.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("RECENT")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(.green.opacity(0.5))
                    ForEach(importantSignals.prefix(5), id: \.id) { signal in
                        HStack(alignment: .top, spacing: 6) {
                            Text(signal.source)
                                .font(.system(size: 8, weight: .bold, design: .monospaced))
                                .foregroundColor(signal.sourceColor)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(signal.sourceColor.opacity(0.15))
                                .clipShape(Capsule())
                            Text(signal.text)
                                .font(.system(size: 10))
                                .foregroundColor(.white.opacity(0.4))
                                .lineLimit(1)
                        }
                    }
                }
                .padding(8)
                .background(Color.white.opacity(0.02))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }

            if monitor.insights.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "sparkles")
                        .font(.system(size: 24))
                        .foregroundColor(.white.opacity(0.15))
                    Text("No analysis yet")
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.3))
                    Text("The agent thinks every 5 minutes")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.2))
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 30)
            } else {
                // Only show insights that have meaningful content (matches, conversations, or high-value facts)
                let meaningfulInsights = monitor.insights.filter { i in
                    !i.matches.isEmpty || !i.conversations.isEmpty ||
                    i.facts.contains(where: { !$0.lowercased().contains("is using") && !$0.lowercased().contains("was viewing") })
                }
                ForEach(meaningfulInsights, id: \.id) { insight in
                    VStack(alignment: .leading, spacing: 6) {
                        // Timestamp
                        Text(insight.time)
                            .font(.system(size: 9, weight: .medium, design: .monospaced))
                            .foregroundColor(.white.opacity(0.3))

                        // Matches — what the agent noticed
                        ForEach(insight.matches, id: \.self) { match in
                            HStack(alignment: .top, spacing: 6) {
                                Circle()
                                    .fill(.green)
                                    .frame(width: 5, height: 5)
                                    .padding(.top, 4)
                                Text(match)
                                    .font(.system(size: 11))
                                    .foregroundColor(.white.opacity(0.8))
                            }
                        }

                        // New facts extracted
                        ForEach(insight.facts, id: \.self) { fact in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "sparkles")
                                    .font(.system(size: 8))
                                    .foregroundColor(.purple)
                                    .padding(.top, 3)
                                Text(fact)
                                    .font(.system(size: 11))
                                    .foregroundColor(.purple.opacity(0.8))
                            }
                        }

                        // Commitments detected
                        ForEach(insight.commitments, id: \.self) { commitment in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "checkmark.circle")
                                    .font(.system(size: 9))
                                    .foregroundColor(.orange)
                                    .padding(.top, 2)
                                Text(commitment)
                                    .font(.system(size: 11))
                                    .foregroundColor(.orange.opacity(0.8))
                            }
                        }

                        // Proposed goals
                        ForEach(insight.proposals, id: \.self) { proposal in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "lightbulb.fill")
                                    .font(.system(size: 9))
                                    .foregroundColor(.yellow)
                                    .padding(.top, 2)
                                Text(proposal)
                                    .font(.system(size: 11))
                                    .foregroundColor(.yellow.opacity(0.8))
                            }
                        }

                        // Conversations detected
                        ForEach(insight.conversations, id: \.self) { convo in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "message.fill")
                                    .font(.system(size: 9))
                                    .foregroundColor(.cyan)
                                    .padding(.top, 2)
                                Text(convo)
                                    .font(.system(size: 11))
                                    .foregroundColor(.cyan.opacity(0.8))
                            }
                        }

                        // If nothing happened
                        if insight.matches.isEmpty && insight.facts.isEmpty && insight.commitments.isEmpty && insight.proposals.isEmpty && insight.conversations.isEmpty {
                            Text("Observed \(insight.signalCount) signals — nothing noteworthy")
                                .font(.system(size: 11))
                                .foregroundColor(.white.opacity(0.25))
                                .italic()
                        }
                    }
                    .padding(10)
                    .background(Color.white.opacity(0.03))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }

    // MARK: Chat Input

    var chatInputBar: some View {
        VStack(spacing: 0) {
            // Contact autocomplete dropdown
            if monitor.showContactPicker {
                VStack(spacing: 0) {
                    ForEach(monitor.contactResults) { contact in
                        Button(action: { monitor.insertContact(contact) }) {
                            HStack(spacing: 8) {
                                Image(systemName: "person.circle.fill")
                                    .font(.system(size: 14))
                                    .foregroundColor(.blue.opacity(0.6))
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(contact.name)
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundColor(.white.opacity(0.9))
                                    if !contact.phone.isEmpty || !contact.email.isEmpty {
                                        Text(contact.phone.isEmpty ? contact.email : contact.phone)
                                            .font(.system(size: 9))
                                            .foregroundColor(.white.opacity(0.3))
                                    }
                                }
                                Spacer()
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                        }
                        .buttonStyle(.plain)
                        Divider().background(Color.white.opacity(0.05))
                    }
                }
                .background(Color(white: 0.1))
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .padding(.horizontal, 12)
                .padding(.bottom, 4)
            }

            // Input bar
            HStack(spacing: 8) {
                TextField("Message (use @ to mention)...", text: $monitor.chatInput)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12))
                    .foregroundColor(.white)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(Color.white.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .onSubmit { monitor.sendChat() }
                    .onChange(of: monitor.chatInput) { newValue in
                        // Detect @query for contact autocomplete
                        if let atRange = newValue.range(of: "@", options: .backwards) {
                            let afterAt = String(newValue[atRange.upperBound...])
                            if !afterAt.contains(" ") && afterAt.count >= 2 {
                                monitor.searchContacts(afterAt)
                            } else if afterAt.isEmpty {
                                monitor.searchContacts("")
                            } else {
                                monitor.showContactPicker = false
                            }
                        } else {
                            monitor.showContactPicker = false
                        }
                    }

                if monitor.chatLoading {
                    ProgressView()
                        .scaleEffect(0.6)
                        .frame(width: 28, height: 28)
                } else {
                    Button(action: { monitor.sendChat() }) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 22))
                            .foregroundColor(monitor.chatInput.trimmingCharacters(in: .whitespaces).isEmpty ? .white.opacity(0.15) : .blue)
                    }
                    .buttonStyle(.plain)
                    .disabled(monitor.chatInput.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .background(Color(white: 0.05))
    }
}

// MARK: - Data Models

enum NotchTab {
    case chat, activity
}

struct ChatMessage: Identifiable {
    let id = UUID()
    let role: String
    let content: String
}

struct ContactMatch: Identifiable {
    let id = UUID()
    let name: String
    let phone: String
    let email: String
}

struct AnalysisInsight: Identifiable {
    let id = UUID()
    let time: String
    let matches: [String]
    let facts: [String]
    let commitments: [String]
    let proposals: [String]
    let conversations: [String]  // "Chatting with Justin about Ruby's soccer options"
    let signalCount: Int
}

// Parse an ISO 8601 timestamp and return a human-readable local time.
// Examples: "2026-04-04T18:15:45.995+00:00" → "11:15" (local) or "3s ago"
// `relative: true` forces the "Ns/Nm/Nh ago" form.
func formatTimestamp(_ iso: String, relative: Bool = false) -> String {
    guard !iso.isEmpty else { return "—" }
    let formatters: [ISO8601DateFormatter] = {
        let f1 = ISO8601DateFormatter()
        f1.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let f2 = ISO8601DateFormatter()
        f2.formatOptions = [.withInternetDateTime]
        return [f1, f2]
    }()
    var date: Date?
    for f in formatters {
        if let d = f.date(from: iso) { date = d; break }
    }
    if date == nil {
        // Try naive (no timezone) ISO: "2026-04-04T18:15:45.995023"
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        df.timeZone = TimeZone.current
        date = df.date(from: iso)
        if date == nil {
            df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            date = df.date(from: iso)
        }
    }
    guard let d = date else { return "—" }
    let delta = Date().timeIntervalSince(d)
    if relative || delta < 3600 {
        let secs = Int(max(0, delta))
        if secs < 10 { return "just now" }
        if secs < 60 { return "\(secs)s ago" }
        let mins = secs / 60
        if mins < 60 { return "\(mins)m ago" }
        let hours = mins / 60
        return "\(hours)h ago"
    }
    let df = DateFormatter()
    df.dateFormat = "HH:mm"
    return df.string(from: d)
}

// MARK: - Setup Wizard
//
// First-launch wizard shown in the popover instead of Chat+Activity.
// Five steps: API key, Google auth, identity, permissions, done.

struct SetupWizardView: View {
    @ObservedObject var monitor: MonitorState
    @State private var apiKey: String = ""
    @State private var userName: String = ""
    @State private var userEmail: String = ""
    @State private var loading: Bool = false
    @State private var error: String = ""
    @State private var gwsEmail: String = ""

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Image(systemName: "sparkles")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Text("Déjà")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Spacer()
                // Step indicator
                Text("Step \(monitor.setupStep + 1) of 6")
                    .font(.system(size: 10))
                    .foregroundColor(.white.opacity(0.3))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            Divider().background(Color.white.opacity(0.1))

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch monitor.setupStep {
                    case 0: welcomeStep
                    case 1: apiKeyStep
                    case 2: googleAuthStep
                    case 3: identityStep
                    case 4: permissionsStep
                    case 5: doneStep
                    default: doneStep
                    }
                }
                .padding(20)
            }
        }
    }

    // MARK: Step 0 — Welcome

    var welcomeStep: some View {
        VStack(spacing: 20) {
            Spacer().frame(height: 40)
            Image(systemName: "sparkles")
                .font(.system(size: 48))
                .foregroundColor(.white.opacity(0.6))
            Text("Welcome to Déjà")
                .font(.system(size: 20, weight: .semibold))
                .foregroundColor(.white)
            Text("A personal AI agent for your Mac. It observes your digital life and maintains a living wiki about the people, projects, and events that matter to you.")
                .font(.system(size: 13))
                .foregroundColor(.white.opacity(0.5))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 20)
            Spacer().frame(height: 20)
            Button(action: { monitor.setupStep = 1 }) {
                Text("Get Started")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(.black)
                    .padding(.horizontal, 32)
                    .padding(.vertical, 10)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: Step 1 — API Key

    var apiKeyStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Connect to Gemini")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(.white)
            Text("Déjà uses Google's Gemini AI to understand your screen, messages, and meetings. Get a free API key from Google AI Studio.")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.5))

            Button(action: {
                if let url = URL(string: "https://aistudio.google.com/app/apikey") {
                    NSWorkspace.shared.open(url)
                }
            }) {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.up.right.square")
                        .font(.system(size: 11))
                    Text("Open Google AI Studio")
                        .font(.system(size: 12, weight: .medium))
                }
                .foregroundColor(.blue)
            }
            .buttonStyle(.plain)

            SecureField("Paste your API key here", text: $apiKey)
                .textFieldStyle(.plain)
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(.white)
                .padding(10)
                .background(Color.white.opacity(0.06))
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.white.opacity(0.1)))

            if !error.isEmpty {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundColor(.red)
            }

            HStack {
                Spacer()
                if loading {
                    ProgressView().scaleEffect(0.7)
                } else {
                    Button(action: { submitApiKey() }) {
                        Text("Continue")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundColor(.black)
                            .padding(.horizontal, 24)
                            .padding(.vertical, 8)
                            .background(apiKey.isEmpty ? Color.gray : Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .disabled(apiKey.isEmpty)
                }
            }
        }
    }

    func submitApiKey() {
        loading = true
        error = ""
        guard let url = URL(string: "http://localhost:5055/api/setup/api-key") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["key": apiKey])
        req.timeoutInterval = 15

        URLSession.shared.dataTask(with: req) { data, _, err in
            DispatchQueue.main.async {
                loading = false
                if let err = err {
                    error = err.localizedDescription
                    return
                }
                guard let data = data,
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ok = obj["ok"] as? Bool else {
                    error = "Unexpected response"
                    return
                }
                if ok {
                    monitor.setupStep = 2
                } else {
                    error = obj["error"] as? String ?? "Invalid key"
                }
            }
        }.resume()
    }

    // MARK: Step 2 — Google Workspace

    var googleAuthStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Connect Google Workspace")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(.white)
            Text("Déjà reads your Gmail, Calendar, and Drive to keep your wiki up to date. This opens Google's sign-in page in your browser.")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.5))

            if !gwsEmail.isEmpty {
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(.green)
                    Text("Connected as \(gwsEmail)")
                        .font(.system(size: 12))
                        .foregroundColor(.green)
                }
            }

            if !error.isEmpty {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundColor(.red)
            }

            HStack {
                if !gwsEmail.isEmpty {
                    Spacer()
                    Button(action: { monitor.setupStep = 3 }) {
                        Text("Continue")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundColor(.black)
                            .padding(.horizontal, 24)
                            .padding(.vertical, 8)
                            .background(Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                } else if loading {
                    Text("Waiting for sign-in...")
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.4))
                    Spacer()
                    ProgressView().scaleEffect(0.7)
                } else {
                    Button(action: { startGwsAuth() }) {
                        HStack(spacing: 6) {
                            Image(systemName: "globe")
                                .font(.system(size: 11))
                            Text("Connect Google Account")
                                .font(.system(size: 13, weight: .semibold))
                        }
                        .foregroundColor(.white)
                        .padding(.horizontal, 20)
                        .padding(.vertical, 8)
                        .background(Color.blue.opacity(0.4))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)

                    Spacer()

                    Button(action: { monitor.setupStep = 3 }) {
                        Text("Skip for now")
                            .font(.system(size: 12))
                            .foregroundColor(.white.opacity(0.3))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    func startGwsAuth() {
        loading = true
        error = ""
        guard let url = URL(string: "http://localhost:5055/api/setup/gws-auth") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 120

        URLSession.shared.dataTask(with: req) { data, _, err in
            DispatchQueue.main.async {
                loading = false
                if let err = err {
                    error = err.localizedDescription
                    return
                }
                guard let data = data,
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ok = obj["ok"] as? Bool else {
                    error = "Unexpected response"
                    return
                }
                if ok {
                    gwsEmail = obj["email"] as? String ?? "Connected"
                    if gwsEmail.isEmpty { gwsEmail = "Connected" }
                } else {
                    error = obj["error"] as? String ?? "Auth failed"
                }
            }
        }.resume()
    }

    // MARK: Step 3 — Identity

    var identityStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("About you")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(.white)
            Text("Déjà needs to know who you are so it can distinguish your messages from everyone else's.")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.5))

            VStack(alignment: .leading, spacing: 8) {
                Text("Full name")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.white.opacity(0.4))
                TextField("David Wurtz", text: $userName)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .foregroundColor(.white)
                    .padding(10)
                    .background(Color.white.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                Text("Email")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.white.opacity(0.4))
                TextField("you@example.com", text: $userEmail)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .foregroundColor(.white)
                    .padding(10)
                    .background(Color.white.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }

            if !error.isEmpty {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundColor(.red)
            }

            HStack {
                Spacer()
                if loading {
                    ProgressView().scaleEffect(0.7)
                } else {
                    Button(action: { submitIdentity() }) {
                        Text("Continue")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundColor(.black)
                            .padding(.horizontal, 24)
                            .padding(.vertical, 8)
                            .background(userName.isEmpty || userEmail.isEmpty ? Color.gray : Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .disabled(userName.isEmpty || userEmail.isEmpty)
                }
            }
        }
    }

    func submitIdentity() {
        loading = true
        error = ""
        guard let url = URL(string: "http://localhost:5055/api/setup/identity") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "name": userName,
            "email": userEmail,
        ])
        req.timeoutInterval = 15

        URLSession.shared.dataTask(with: req) { data, _, err in
            DispatchQueue.main.async {
                loading = false
                if let err = err { error = err.localizedDescription; return }
                guard let data = data,
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ok = obj["ok"] as? Bool, ok else {
                    error = "Setup failed"
                    return
                }
                // Move to permissions step
                monitor.setupStep = 4
            }
        }.resume()
    }

    func completeSetup() {
        guard let url = URL(string: "http://localhost:5055/api/setup/complete") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 10
        URLSession.shared.dataTask(with: req) { _, _, _ in
            DispatchQueue.main.async {
                monitor.setupStep = 5
            }
        }.resume()
    }

    // MARK: Step 4 — Permissions

    @State private var screenRecordingGranted = false
    @State private var fullDiskGranted = false
    @State private var contactsGranted = false

    var permissionsStep: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Grant permissions")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(.white)
            Text("Déjà needs a few macOS permissions to observe your screen and messages. Grant each one below.")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.5))

            VStack(spacing: 12) {
                permissionRow(
                    icon: "rectangle.dashed.badge.record",
                    title: "Screen Recording",
                    description: "See what's on your screen to build context",
                    granted: screenRecordingGranted,
                    action: {
                        // Open Screen Recording pane
                        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
                            NSWorkspace.shared.open(url)
                        }
                    }
                )

                permissionRow(
                    icon: "lock.open.fill",
                    title: "Full Disk Access",
                    description: "Read iMessage and WhatsApp conversations",
                    granted: fullDiskGranted,
                    action: {
                        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") {
                            NSWorkspace.shared.open(url)
                        }
                    }
                )

                permissionRow(
                    icon: "person.crop.circle",
                    title: "Contacts",
                    description: "Match phone numbers to names",
                    granted: contactsGranted,
                    action: {
                        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts") {
                            NSWorkspace.shared.open(url)
                        }
                    }
                )
            }

            HStack {
                Button(action: { checkPermissions() }) {
                    HStack(spacing: 5) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 10))
                        Text("Check again")
                            .font(.system(size: 12))
                    }
                    .foregroundColor(.white.opacity(0.4))
                }
                .buttonStyle(.plain)

                Spacer()

                Button(action: {
                    completeSetup()
                }) {
                    Text(screenRecordingGranted && fullDiskGranted ? "Continue" : "Skip for now")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(.black)
                        .padding(.horizontal, 24)
                        .padding(.vertical, 8)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
            }
        }
        .onAppear { checkPermissions() }
    }

    func permissionRow(icon: String, title: String, description: String, granted: Bool, action: @escaping () -> Void) -> some View {
        HStack(spacing: 12) {
            Image(systemName: granted ? "checkmark.circle.fill" : icon)
                .font(.system(size: 16))
                .foregroundColor(granted ? .green : .white.opacity(0.4))
                .frame(width: 24)

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(.white)
                Text(description)
                    .font(.system(size: 11))
                    .foregroundColor(.white.opacity(0.35))
            }

            Spacer()

            if !granted {
                Button(action: action) {
                    Text("Grant")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundColor(.blue)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5)
                        .background(Color.blue.opacity(0.12))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(granted ? Color.green.opacity(0.2) : Color.white.opacity(0.06), lineWidth: 1)
        )
    }

    func checkPermissions() {
        // Screen Recording: try CGPreflightScreenCaptureAccess
        screenRecordingGranted = CGPreflightScreenCaptureAccess()

        // Full Disk Access: try reading iMessage database
        fullDiskGranted = FileManager.default.isReadableFile(
            atPath: NSHomeDirectory() + "/Library/Messages/chat.db"
        )

        // Contacts: try accessing AddressBook
        do {
            let abPath = NSHomeDirectory() + "/Library/Application Support/AddressBook/AddressBook-v22.abcddb"
            contactsGranted = FileManager.default.isReadableFile(atPath: abPath)
        }
    }

    // MARK: Step 5 — Done

    var doneStep: some View {
        VStack(spacing: 20) {
            Spacer().frame(height: 30)
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 48))
                .foregroundColor(.green)
            Text("You're all set")
                .font(.system(size: 20, weight: .semibold))
                .foregroundColor(.white)
            Text("Déjà is now watching your screen, messages, and calendar. Your wiki lives at ~/Deja Wiki — open it in Obsidian or any Markdown editor.")
                .font(.system(size: 13))
                .foregroundColor(.white.opacity(0.5))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 20)

            Button(action: {
                monitor.completeSetup()
            }) {
                Text("Start Déjà")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(.black)
                    .padding(.horizontal, 32)
                    .padding(.vertical, 10)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)
        }
        .frame(maxWidth: .infinity)
    }
}


// MARK: - Floating Meeting Pill
//
// A small floating widget that appears on screen when a calendar meeting
// is imminent. Shows meeting title + time, with a "Take notes" button.
// During recording, shows elapsed time + Stop.

struct MeetingPillView: View {
    @ObservedObject var monitor: MonitorState
    var onDismiss: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // Processing state — shown after recording stops while AI generates the page
            if monitor.meetingProcessing {
                HStack(spacing: 10) {
                    ProgressView()
                        .scaleEffect(0.6)
                        .frame(width: 16, height: 16)
                    Text("Generating notes...")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(.white.opacity(0.6))
                    Spacer()
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }

            if !monitor.meetingProcessing {
            // Header bar
            HStack(spacing: 12) {
                // Color accent bar
                RoundedRectangle(cornerRadius: 2)
                    .fill(monitor.meetingRecording ? Color.red : Color.cyan)
                    .frame(width: 3, height: 32)

                // Meeting info
                VStack(alignment: .leading, spacing: 2) {
                    Text(monitor.meetingTitle.isEmpty ? "Call" : monitor.meetingTitle)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(.white)
                        .lineLimit(1)
                    if monitor.meetingRecording {
                        Text("Recording · \(formatDuration(monitor.meetingElapsed))")
                            .font(.system(size: 11))
                            .foregroundColor(.red.opacity(0.8))
                    } else {
                        Text(monitor.meetingTimeRange)
                            .font(.system(size: 11))
                            .foregroundColor(.white.opacity(0.4))
                            .lineLimit(1)
                    }
                }

                Spacer()

                if monitor.meetingRecording {
                    // Pause — stops recording but doesn't process yet
                    Button(action: { monitor.pauseMeetingRecording() }) {
                        HStack(spacing: 4) {
                            Image(systemName: monitor.meetingPaused ? "play.fill" : "pause.fill")
                                .font(.system(size: 9))
                            Text(monitor.meetingPaused ? "Resume" : "Pause")
                                .font(.system(size: 11, weight: .medium))
                        }
                        .foregroundColor(.white.opacity(0.6))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.white.opacity(0.06))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .stroke(Color.white.opacity(0.12), lineWidth: 1)
                        )
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)

                    // End meeting — stops recording and processes
                    Button(action: { monitor.stopMeetingRecording() }) {
                        HStack(spacing: 4) {
                            Image(systemName: "stop.fill")
                                .font(.system(size: 9))
                            Text("End")
                                .font(.system(size: 11, weight: .medium))
                        }
                        .foregroundColor(.red)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.red.opacity(0.1))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .stroke(Color.red.opacity(0.25), lineWidth: 1)
                        )
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                } else {
                    Button(action: { monitor.startMeetingRecording() }) {
                        HStack(spacing: 5) {
                            Image(systemName: "mic.fill")
                                .font(.system(size: 10))
                            Text("Take notes")
                                .font(.system(size: 12, weight: .semibold))
                        }
                        .foregroundColor(.white.opacity(0.9))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                        .background(Color.white.opacity(0.08))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .stroke(Color.white.opacity(0.2), lineWidth: 1)
                        )
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)

                    Button(action: { onDismiss() }) {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundColor(.white.opacity(0.25))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            // Scratchpad — visible during recording
            if monitor.meetingRecording {
                Divider().background(Color.white.opacity(0.08))
                ZStack(alignment: .topLeading) {
                    if monitor.meetingNotes.isEmpty {
                        Text("Jot notes here...")
                            .font(.system(size: 12))
                            .foregroundColor(.white.opacity(0.2))
                            .padding(.horizontal, 4)
                            .padding(.vertical, 8)
                    }
                    TextEditor(text: $monitor.meetingNotes)
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.85))
                        .scrollContentBackground(.hidden)
                        .background(Color.clear)
                        .frame(minHeight: 80, maxHeight: 200)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
            }
            } // end if !meetingProcessing
        }
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color(white: 0.12))
                .shadow(color: .black.opacity(0.5), radius: 20, y: 5)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.08), lineWidth: 1)
        )
    }
}


// MARK: - Agent Notification Bubble

struct NotificationBubbleView: View {
    @ObservedObject var monitor: MonitorState
    var onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "bell.fill")
                .font(.system(size: 11))
                .foregroundColor(.yellow.opacity(0.8))
            VStack(alignment: .leading, spacing: 2) {
                if monitor.notificationTitle != "Déjà" {
                    Text(monitor.notificationTitle)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundColor(.white)
                        .lineLimit(1)
                }
                Text(monitor.notificationMessage)
                    .font(.system(size: 12))
                    .foregroundColor(.white.opacity(0.75))
                    .lineLimit(3)
            }
            Spacer()
            Button(action: { onDismiss() }) {
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundColor(.white.opacity(0.25))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(width: 320)
        .background(Color.black)
    }
}


extension Notification.Name {
    static let meetingDetected = Notification.Name("dejaMeetingDetected")
    static let meetingDismissed = Notification.Name("dejaMeetingDismissed")
    static let agentNotification = Notification.Name("dejaAgentNotification")
    static let notificationDismissed = Notification.Name("dejaNotificationDismissed")
}

func formatDuration(_ seconds: TimeInterval) -> String {
    let mins = Int(seconds) / 60
    let secs = Int(seconds) % 60
    return String(format: "%d:%02d", mins, secs)
}

struct SignalInfo: Identifiable {
    let id = UUID()
    let source: String
    let text: String
    let time: String

    var sourceColor: Color {
        switch source {
        case "screenshot": return .purple
        case "email": return .red
        case "calendar": return .orange
        case "active_app": return .green
        case "clipboard": return .blue
        case "imessage": return .cyan
        case "whatsapp": return .mint
        case "drive": return .yellow
        case "tasks": return .teal
        default: return .gray
        }
    }
}

// MARK: - Meeting Recorder (spawns DejaRecorder helper)
//
// The actual ScreenCaptureKit capture runs in a SEPARATE binary
// (DejaRecorder) so that importing the framework doesn't
// trigger TCC Screen Recording prompts on every app launch.
// The helper is only spawned when the user clicks Record.

class MeetingRecorder {
    private var process: Process?
    var sessionDir: String = ""
    var isRecording: Bool = false
    var onAutoStop: (() -> Void)?

    private static var recorderPath: String {
        // Bundled inside the app, next to the main executable
        let bundled = Bundle.main.executableURL!
            .deletingLastPathComponent()
            .appendingPathComponent("DejaRecorder").path
        if FileManager.default.fileExists(atPath: bundled) {
            return bundled
        }
        // Dev fallback
        return NSHomeDirectory() + "/projects/workagents/workagent/menubar/DejaRecorder"
    }

    func startRecording(sessionId: String, outputDirPath: String) {
        guard !isRecording else { return }
        self.sessionDir = outputDirPath

        // Spawn the recorder on a background queue so it doesn't
        // interfere with the main thread / menu bar status item.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: Self.recorderPath)
            proc.arguments = [outputDirPath]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice

            proc.terminationHandler = { [weak self] process in
                NSLog("deja: recorder exited with code \(process.terminationStatus)")
                DispatchQueue.main.async {
                    if self?.isRecording == true {
                        self?.isRecording = false
                        self?.onAutoStop?()
                    }
                }
            }

            do {
                try proc.run()
                DispatchQueue.main.async {
                    self?.process = proc
                    self?.isRecording = true
                }
                NSLog("deja: recorder started (pid \(proc.processIdentifier), session: \(sessionId))")
            } catch {
                NSLog("deja: recorder spawn failed: \(error)")
            }
        }
    }

    func stopRecording() {
        guard isRecording, let proc = process else { return }
        isRecording = false

        // Write .stop sentinel — the recorder polls for this
        let stopFile = URL(fileURLWithPath: sessionDir).appendingPathComponent(".stop")
        FileManager.default.createFile(atPath: stopFile.path, contents: nil)

        // Wait for the recorder to exit (it merges mic audio on shutdown).
        // Block up to 10 seconds — the merge is typically < 2 seconds.
        proc.waitUntilExit()

        process = nil
        NSLog("deja: recorder exited (merge complete)")
    }
}


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
    @Published var chatMessages: [ChatMessage] = []
    @Published var chatInput: String = ""
    @Published var chatLoading: Bool = false
    @Published var contactResults: [ContactMatch] = []
    @Published var showContactPicker: Bool = false
    @Published var activeTab: NotchTab = .chat
    @Published var isRecording: Bool = false
    @Published var micBusy: Bool = false
    @Published var setupNeeded: Bool = false
    @Published var setupStep: Int = 0

    // Meeting recording state
    @Published var meetingAvailable: Bool = false
    @Published var meetingTitle: String = ""
    @Published var meetingAttendees: [String] = []
    @Published var meetingRecording: Bool = false
    @Published var meetingLinked: Bool = false   // linked to a calendar event
    @Published var meetingPaused: Bool = false   // recording paused but not ended
    @Published var meetingElapsed: TimeInterval = 0
    @Published var meetingSessionId: String = ""
    @Published var meetingNotes: String = ""  // scratchpad during recording
    @Published var meetingTimeRange: String = ""  // e.g. "5:54 PM - 6:10 PM"
    private var meetingStartTime: Date?
    private var meetingTimer: Timer?
    private let meetingRecorder = MeetingRecorder()

    static let home = NSHomeDirectory() + "/.deja"

    // The frozen Python backend lives inside the app bundle.
    // Falls back to the dev venv path if the bundled binary doesn't exist
    // (for development without PyInstaller).
    static var backendPath: String {
        // onedir mode: deja-backend/deja-backend inside the app bundle
        let macosDir = Bundle.main.executableURL!.deletingLastPathComponent()
        let onedir = macosDir.appendingPathComponent("deja-backend/deja-backend").path
        if FileManager.default.fileExists(atPath: onedir) {
            return onedir
        }
        // onefile fallback
        let onefile = macosDir.appendingPathComponent("deja-backend").path
        if FileManager.default.fileExists(atPath: onefile) {
            return onefile
        }
        // Dev fallback
        return NSHomeDirectory() + "/projects/workagents/workagent/venv/bin/python3"
    }

    static var usesFrozenBackend: Bool {
        let macosDir = Bundle.main.executableURL!.deletingLastPathComponent()
        let onedir = macosDir.appendingPathComponent("deja-backend/deja-backend").path
        let onefile = macosDir.appendingPathComponent("deja-backend").path
        return FileManager.default.fileExists(atPath: onedir) || FileManager.default.fileExists(atPath: onefile)
    }

    static let projectDir = NSHomeDirectory() + "/projects/workagents/workagent"

    private var process: Process?
    private var webProcess: Process?
    private var statsTimer: Timer?
    private var screenshotTimer: Timer?
    private var dbReaderTimer: Timer?
    private var lastContactsRefresh: Date = .distantPast

    func start() {
        // Check if first-launch setup is needed
        let setupDone = FileManager.default.fileExists(atPath: Self.home + "/setup_done")
        setupNeeded = !setupDone

        // Always start web server (setup wizard needs it for API calls)
        startWeb()

        // Start screenshot capture timer (runs regardless of setup state
        // so the Python observer can read screenshots immediately)
        startScreenshotCapture()

        // Start database readers (iMessage, WhatsApp, Contacts)
        // Runs regardless of setup state so buffers are ready when Python starts
        startDatabaseReaders()

        if setupDone {
            startMonitor()
            statsTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
                self?.updateStats()
                self?.updateRecentSignals()
                self?.updateInsights()
            }
            updateStats()
            updateRecentSignals()
            updateInsights()
            startMeetingPolling()
        }
    }

    // MARK: - Screenshot Capture

    private func startScreenshotCapture() {
        // Capture immediately, then every 6 seconds
        Task { await captureScreenshot() }
        screenshotTimer = Timer.scheduledTimer(withTimeInterval: 6.0, repeats: true) { [weak self] _ in
            Task { await self?.captureScreenshot() }
        }
    }

    private func captureScreenshot() async {
        // Only capture if we have Screen Recording permission
        guard CGPreflightScreenCaptureAccess() else { return }

        // Use ScreenCaptureKit to capture the main display
        let image: CGImage
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: true
            )
            guard let display = content.displays.first else { return }

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.width = display.width * 2
            config.height = display.height * 2
            config.capturesAudio = false
            config.showsCursor = false

            image = try await SCScreenshotManager.captureImage(
                contentFilter: filter, configuration: config
            )
        } catch {
            return
        }

        let width = image.width
        let height = image.height
        if width < 100 || height < 100 { return }

        let screenshotPath = Self.home + "/latest_screen.png"
        let timestampPath = Self.home + "/latest_screen_ts.txt"
        let tmpPath = screenshotPath + ".tmp"

        // Ensure ~/.deja directory exists
        try? FileManager.default.createDirectory(
            atPath: Self.home,
            withIntermediateDirectories: true
        )

        // Write PNG to a temp file, then atomically move into place
        guard let dest = CGImageDestinationCreateWithURL(
            URL(fileURLWithPath: tmpPath) as CFURL,
            "public.png" as CFString,
            1,
            nil
        ) else { return }

        CGImageDestinationAddImage(dest, image, nil)
        guard CGImageDestinationFinalize(dest) else {
            try? FileManager.default.removeItem(atPath: tmpPath)
            return
        }

        // Atomic move to avoid partial reads from Python
        do {
            if FileManager.default.fileExists(atPath: screenshotPath) {
                _ = try FileManager.default.replaceItemAt(
                    URL(fileURLWithPath: screenshotPath),
                    withItemAt: URL(fileURLWithPath: tmpPath)
                )
            } else {
                try FileManager.default.moveItem(atPath: tmpPath, toPath: screenshotPath)
            }
        } catch {
            try? FileManager.default.removeItem(atPath: tmpPath)
            return
        }

        // Write timestamp
        let ts = String(format: "%.3f", Date().timeIntervalSince1970)
        try? ts.write(toFile: timestampPath, atomically: true, encoding: .utf8)
    }

    func checkSetupStatus() {
        // Ask the backend what's already configured and skip completed steps
        guard let url = URL(string: "http://localhost:5055/api/setup/status") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 5
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self = self, let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                let hasKey = obj["has_api_key"] as? Bool ?? false
                let gwsAuth = obj["gws_authenticated"] as? Bool ?? false
                let hasIdentity = obj["has_identity"] as? Bool ?? false

                // Skip to the first incomplete step
                if hasKey && gwsAuth && hasIdentity {
                    self.setupStep = 5  // done screen
                } else if hasKey && gwsAuth {
                    self.setupStep = 3  // identity
                } else if hasKey {
                    self.setupStep = 2  // google auth
                } else {
                    self.setupStep = 0  // welcome
                }
            }
        }.resume()
    }

    func completeSetup() {
        setupNeeded = false
        startMonitor()
        statsTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            self?.updateStats()
            self?.updateRecentSignals()
            self?.updateInsights()
        }
        startMeetingPolling()
    }

    func stop() {
        statsTimer?.invalidate()
        statsTimer = nil
        screenshotTimer?.invalidate()
        screenshotTimer = nil
        dbReaderTimer?.invalidate()
        dbReaderTimer = nil
        process?.terminate()
        process = nil
        webProcess?.terminate()
        webProcess = nil
        running = false
    }

    func restart() {
        process?.terminate()
        process = nil
        webProcess?.terminate()
        webProcess = nil
        running = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startMonitor()
            self?.startWeb()
        }
    }

    // MARK: - Unified recording (ScreenCaptureKit — system audio + mic)
    //
    // One button handles both voice notes and meeting recording.
    // Always captures system audio + mic via ScreenCaptureKit so both
    // sides of any call are recorded. Python decides post-processing:
    //   < 2 min → voice note → transcribe → chat
    //   >= 2 min or calendar event → meeting → chunked transcribe → wiki event

    func toggleRecording() {
        if meetingRecording {
            stopMeetingRecording()
        } else {
            startMeetingRecording()
        }
    }

    // Legacy mic status — keep for compatibility but recording now
    // goes through ScreenCaptureKit via MeetingRecorder
    func refreshMicStatus() {
        // No-op: recording state is tracked locally via meetingRecording
    }

    func toggleMic() {
        toggleRecording()
    }

    // MARK: - Meeting recording

    // MARK: - Notifications from agent
    @Published var notificationTitle: String = ""
    @Published var notificationMessage: String = ""
    @Published var showNotification: Bool = false

    func pollNotifications() {
        let path = Self.home + "/notification.json"
        guard FileManager.default.fileExists(atPath: path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let message = obj["message"] as? String, !message.isEmpty else { return }

        // Remove the file so we don't show it again
        try? FileManager.default.removeItem(atPath: path)

        DispatchQueue.main.async {
            self.notificationTitle = obj["title"] as? String ?? "Déjà"
            self.notificationMessage = message
            self.showNotification = true
            NotificationCenter.default.post(name: .agentNotification, object: nil)

            // Auto-dismiss after 10 seconds
            DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
                self.showNotification = false
                NotificationCenter.default.post(name: .notificationDismissed, object: nil)
            }
        }
    }

    func startMeetingPolling() {
        // Poll for meeting prompts + notifications every 5 seconds
        Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.refreshMeetingPrompt()
            self?.pollNotifications()
            if self?.meetingRecording == true {
                self?.refreshMeetingStatus()
            }
        }

        // Set up auto-stop callback
        meetingRecorder.onAutoStop = { [weak self] in
            self?.stopMeetingRecording()
        }
    }

    private var lastPromptedEventId: String = ""
    private var recordedEventIds: Set<String> = []  // don't re-prompt for recorded meetings

    func refreshMeetingPrompt() {
        guard !meetingRecording else { return }
        guard let url = URL(string: "http://localhost:5055/api/meeting/prompt") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self = self, let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let available = obj["available"] as? Bool else { return }
            DispatchQueue.main.async {
                let wasAvailable = self.meetingAvailable
                self.meetingAvailable = available
                if available {
                    self.meetingTitle = obj["title"] as? String ?? "Meeting"
                    let attendees = obj["attendees"] as? [[String: String]] ?? []
                    self.meetingAttendees = attendees.map { $0["name"] ?? $0["email"] ?? "" }

                    // Build time range string
                    let startISO = obj["start"] as? String ?? ""
                    let endISO = obj["end"] as? String ?? ""
                    self.meetingTimeRange = Self.formatTimeRange(start: startISO, end: endISO)

                    // Auto-show pill when a NEW meeting is detected
                    // Skip if we already recorded for this event
                    let eventId = obj["event_id"] as? String ?? ""
                    if (!wasAvailable || eventId != self.lastPromptedEventId)
                        && !eventId.isEmpty
                        && !self.recordedEventIds.contains(eventId) {
                        self.lastPromptedEventId = eventId
                        NotificationCenter.default.post(name: .meetingDetected, object: nil)
                    }
                } else {
                    self.meetingTitle = ""
                    self.meetingAttendees = []
                    // Dismiss floating pill if meeting is no longer active
                    if !self.meetingRecording {
                        NotificationCenter.default.post(name: .meetingDismissed, object: nil)
                    }
                }
            }
        }.resume()
    }

    func refreshMeetingStatus() {
        guard let url = URL(string: "http://localhost:5055/api/meeting/status") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self = self, let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                if let elapsed = obj["elapsed_sec"] as? Int {
                    self.meetingElapsed = TimeInterval(elapsed)
                }
            }
        }.resume()
    }

    func startMeetingRecording() {
        guard !meetingRecording else { return }

        // Tell Python to start the session
        guard let url = URL(string: "http://localhost:5055/api/meeting/start") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")

        // Use calendar meeting info if available
        let title = meetingTitle.isEmpty ? "" : meetingTitle
        let attendees = meetingAttendees.isEmpty ? [] : meetingAttendees
        meetingLinked = !title.isEmpty
        meetingNotes = ""  // clear scratchpad

        // Remember this event so we don't re-prompt after recording
        if let eventId = lastPromptedEventId as String?, !eventId.isEmpty {
            recordedEventIds.insert(eventId)
        }

        let body: [String: Any] = [
            "title": title,
            "attendees": attendees.map { ["name": $0] },
        ]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 10

        URLSession.shared.dataTask(with: req) { [weak self] data, _, error in
            guard let self = self else { return }
            if let error = error {
                NSLog("deja: meeting start failed: \(error)")
                return
            }
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let sessionId = obj["session_id"] as? String,
                  let sessionDir = obj["session_dir"] as? String else { return }

            DispatchQueue.main.async {
                self.meetingRecording = true
                self.meetingSessionId = sessionId
                self.meetingStartTime = Date()
                self.meetingElapsed = 0

                // Start audio capture via ScreenCaptureKit
                self.meetingRecorder.startRecording(sessionId: sessionId, outputDirPath: sessionDir)

                // Start elapsed time counter
                self.meetingTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
                    guard let self = self, let start = self.meetingStartTime else { return }
                    self.meetingElapsed = Date().timeIntervalSince(start)
                }

                NSLog("deja: meeting recording started in Swift: \(sessionId)")
            }
        }.resume()
    }

    func unlinkMeeting() {
        // Disconnect from calendar event but keep recording.
        // The event page will be titled by AI based on transcript content.
        meetingLinked = false
        meetingTitle = ""
        meetingAttendees = []

        // Tell Python to clear the calendar metadata
        guard let url = URL(string: "http://localhost:5055/api/meeting/unlink") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 5
        URLSession.shared.dataTask(with: req) { _, _, _ in }.resume()
    }

    @Published var meetingProcessing: Bool = false  // true while generating event page

    func pauseMeetingRecording() {
        if meetingPaused {
            // Resume — restart the recorder
            if let dir = meetingRecorder.sessionDir as String?, !dir.isEmpty {
                meetingRecorder.startRecording(sessionId: meetingSessionId, outputDirPath: dir)
            }
            meetingPaused = false
        } else {
            // Pause — stop the recorder but don't process
            meetingRecorder.stopRecording()
            meetingPaused = true
        }
    }

    func stopMeetingRecording() {
        guard meetingRecording else { return }

        let notes = meetingNotes  // capture before clearing

        // Update UI — show processing state
        meetingTimer?.invalidate()
        meetingTimer = nil
        meetingRecording = false
        meetingPaused = false
        meetingProcessing = true  // shows "Generating notes..." in pill

        // Show the processing pill
        NotificationCenter.default.post(name: .meetingDetected, object: nil)

        // Stop recorder + wait for merge + tell Python — all on background thread
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.meetingRecorder.stopRecording()

            // Send notes along with the stop request
            guard let url = URL(string: "http://localhost:5055/api/meeting/stop") else { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["notes": notes])
            req.timeoutInterval = 300

            URLSession.shared.dataTask(with: req) { data, _, error in
                DispatchQueue.main.async {
                    self?.meetingProcessing = false
                    self?.meetingAvailable = false
                    self?.meetingSessionId = ""
                    self?.meetingStartTime = nil
                    self?.meetingElapsed = 0
                    self?.meetingNotes = ""
                    NotificationCenter.default.post(name: .meetingDismissed, object: nil)
                }

                if let error = error {
                    NSLog("deja: meeting stop failed: \(error)")
                    return
                }
                if let data = data,
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    NSLog("deja: meeting processed: \(obj)")

                    if let slug = obj["slug"] as? String, !slug.isEmpty {
                        let vaultName = "Deja"
                        let encodedPath = "events/\(slug)".addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? slug
                        if let obsidianURL = URL(string: "obsidian://open?vault=\(vaultName)&file=\(encodedPath)") {
                            DispatchQueue.main.async {
                                NSWorkspace.shared.open(obsidianURL)
                            }
                        }
                    }
                }
            }.resume()
        }
    }

    static func formatTimeRange(start: String, end: String) -> String {
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        let outFmt = DateFormatter()
        outFmt.dateFormat = "h:mm a"

        func parse(_ iso: String) -> Date? {
            // Strip timezone offset for parsing
            let clean = String(iso.prefix(19))
            return df.date(from: clean)
        }

        guard let s = parse(start), let e = parse(end) else {
            return ""
        }
        return "\(outFmt.string(from: s)) - \(outFmt.string(from: e))"
    }

    func searchContacts(_ query: String) {
        guard query.count >= 2 else {
            DispatchQueue.main.async { self.contactResults = []; self.showContactPicker = false }
            return
        }
        let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
        guard let url = URL(string: "http://localhost:5055/api/contacts/search?q=\(encoded)&limit=5") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data = data,
                  let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
            let results = arr.map { c in
                ContactMatch(
                    name: c["name"] as? String ?? "",
                    phone: (c["phones"] as? [String])?.first ?? "",
                    email: (c["emails"] as? [String])?.first ?? ""
                )
            }
            DispatchQueue.main.async {
                self?.contactResults = results
                self?.showContactPicker = !results.isEmpty
            }
        }.resume()
    }

    func insertContact(_ contact: ContactMatch) {
        // Replace the @query with @Name
        if let atRange = chatInput.range(of: "@", options: .backwards) {
            chatInput = String(chatInput[chatInput.startIndex..<atRange.lowerBound]) + "@\(contact.name) "
        }
        contactResults = []
        showContactPicker = false
    }

    func sendChat() {
        let message = chatInput.trimmingCharacters(in: .whitespaces)
        guard !message.isEmpty, !chatLoading else { return }
        chatMessages.append(ChatMessage(role: "user", content: message))
        chatInput = ""
        chatLoading = true
        activeTab = .chat

        // Add a placeholder message that we'll update with streaming chunks
        let placeholderIdx = chatMessages.count
        chatMessages.append(ChatMessage(role: "agent", content: ""))

        DispatchQueue.global().async { [weak self] in
            guard let url = URL(string: "http://localhost:5055/api/chat") else { return }
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["message": message])
            request.timeoutInterval = 120

            let session = URLSession(configuration: .default)
            let task = session.dataTask(with: request) { data, response, error in
                guard let data = data else {
                    DispatchQueue.main.async {
                        self?.chatLoading = false
                        self?.chatMessages[placeholderIdx] = ChatMessage(role: "agent", content: "Error: \(error?.localizedDescription ?? "no response")")
                    }
                    return
                }

                // Parse SSE chunks
                var fullText = ""
                let text = String(data: data, encoding: .utf8) ?? ""
                for line in text.split(separator: "\n") {
                    let l = String(line)
                    if l.hasPrefix("data: ") {
                        let jsonStr = String(l.dropFirst(6))
                        if let jsonData = jsonStr.data(using: .utf8),
                           let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                            if let chunk = obj["chunk"] as? String {
                                fullText += chunk
                            }
                        }
                    }
                }

                // Clean ACTION commands from display
                let displayText = fullText.replacingOccurrences(of: "\\[ACTION:[^\\]]*\\]", with: "", options: .regularExpression)

                DispatchQueue.main.async {
                    self?.chatLoading = false
                    self?.chatMessages[placeholderIdx] = ChatMessage(role: "agent", content: displayText.trimmingCharacters(in: .whitespacesAndNewlines))
                }
            }
            task.resume()
        }
    }

    private func startMonitor() {
        guard process == nil || !process!.isRunning else { return }
        let proc = Process()
        if Self.usesFrozenBackend {
            // Frozen binary — single executable, no Python needed
            proc.executableURL = URL(fileURLWithPath: Self.backendPath)
            proc.arguments = ["monitor"]
        } else {
            // Dev mode — use venv Python
            proc.executableURL = URL(fileURLWithPath: Self.backendPath)
            proc.arguments = ["-m", "deja", "monitor"]
            proc.currentDirectoryURL = URL(fileURLWithPath: Self.projectDir)
        }
        proc.environment = makeEnv()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.running = false }
        }
        do { try proc.run(); process = proc; running = true }
        catch { print("Monitor start failed: \(error)") }
    }

    func startWeb() {
        guard webProcess == nil || !webProcess!.isRunning else { return }
        let proc = Process()
        if Self.usesFrozenBackend {
            proc.executableURL = URL(fileURLWithPath: Self.backendPath)
            proc.arguments = ["web"]
        } else {
            proc.executableURL = URL(fileURLWithPath: Self.backendPath)
            proc.arguments = ["-m", "deja", "web"]
            proc.currentDirectoryURL = URL(fileURLWithPath: Self.projectDir)
        }
        proc.environment = makeEnv()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do { try proc.run(); webProcess = proc }
        catch { print("Web start failed: \(error)") }
    }

    private func makeEnv() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        if !Self.usesFrozenBackend {
            env["PYTHONPATH"] = Self.projectDir + "/src"
        }
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if env["GEMINI_API_KEY"] == nil, let key = readKeyFromEnv() { env["GEMINI_API_KEY"] = key }
        // Tell macOS to associate child processes with this app for TCC.
        // Without this, deja-backend and DejaRecorder appear as separate
        // apps in System Settings, requiring 3 separate permission grants.
        env["__CFBundleIdentifier"] = "com.deja.app"
        return env
    }

    // Diverse signals — cap screenshots at 2, prioritize messages/email/calendar
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

    private func updateStats() {
        let logPath = Self.home + "/signal_log.jsonl"
        guard FileManager.default.fileExists(atPath: logPath),
              let data = FileManager.default.contents(atPath: logPath) else { return }
        let lineCount = data.split(separator: UInt8(ascii: "\n")).count
        let analysisPath = Self.home + "/analysis_log.jsonl"
        var matchCount = 0
        if let ad = FileManager.default.contents(atPath: analysisPath) {
            for line in ad.split(separator: UInt8(ascii: "\n")) {
                if let json = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                   let m = json["matches"] as? [[String: Any]] { matchCount += m.count }
            }
        }
        let isRunning = process?.isRunning ?? false
        DispatchQueue.main.async { self.signals = lineCount; self.matches = matchCount; self.running = isRunning }
    }

    private func isPlaceholder(_ s: String) -> Bool {
        let lower = s.lowercased().trimmingCharacters(in: .whitespaces)
        return lower == "none" || lower == "none detected" || lower == "n/a"
            || lower == "no commitments" || lower == "no conversations"
            || lower.hasPrefix("none ") || lower == ":"
    }

    private func updateInsights() {
        let path = Self.home + "/analysis_log.jsonl"
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else { return }
        let lines = data.split(separator: UInt8(ascii: "\n"))

        // Last 15 analysis cycles, newest first
        let parsed: [AnalysisInsight] = lines.suffix(15).reversed().compactMap { line in
            guard let json = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any] else { return nil }
            let ts = json["timestamp"] as? String ?? ""
            let timeStr = formatTimestamp(ts)

            let matches = (json["matches"] as? [[String: Any]] ?? []).map { m in
                let goal = m["goal"] as? String ?? ""
                let summary = m["signal_summary"] as? String ?? ""
                let conf = m["confidence"] as? String ?? ""
                let reasoning = m["reasoning"] as? String ?? ""
                return "[\(conf)] \(goal): \(summary)" + (reasoning.isEmpty ? "" : " — \(reasoning)")
            }

            let facts = (json["new_facts"] as? [[String: Any]] ?? []).map { f in
                f["fact"] as? String ?? ""
            }.filter { !$0.isEmpty && !isPlaceholder($0) }

            let commitments = (json["commitments"] as? [[String: Any]] ?? []).map { c in
                let who = c["commitment"] as? String ?? ""
                let deadline = c["deadline"] as? String
                return deadline != nil ? "\(who) (by \(deadline!))" : who
            }.filter { !$0.isEmpty && !isPlaceholder($0) }

            let proposals = (json["proposed_goals"] as? [[String: Any]] ?? []).map { p in
                let name = p["name"] as? String ?? ""
                let desc = p["description"] as? String ?? ""
                return "\(name): \(desc)"
            }.filter { !$0.isEmpty && !isPlaceholder($0) }

            let conversations = (json["conversations"] as? [[String: Any]] ?? []).map { c in
                let with_ = c["with"] as? String ?? ""
                let summary = c["summary"] as? String ?? c["topic"] as? String ?? ""
                let underlying = c["underlying_goal"] as? String ?? ""
                var text = "\(with_): \(summary)"
                if !underlying.isEmpty { text += " → \(underlying)" }
                return text
            }.filter { !$0.isEmpty }

            let skipCount = (json["skips"] as? [[String: Any]])?.count ?? 0
            let signalCount = matches.count + skipCount

            return AnalysisInsight(
                time: timeStr,
                matches: matches,
                facts: facts,
                commitments: commitments,
                proposals: proposals,
                conversations: conversations,
                signalCount: signalCount
            )
        }

        DispatchQueue.main.async { self.insights = parsed }
    }

    private func updateRecentSignals() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: Self.home + "/signal_log.jsonl")) else { return }
        let lines = data.split(separator: UInt8(ascii: "\n"))
        let recent = lines.suffix(40).reversed().compactMap { line -> SignalInfo? in
            guard let json = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: String] else { return nil }
            let ts = json["timestamp"] ?? ""
            return SignalInfo(source: json["source"] ?? "?", text: String((json["text"] ?? "").prefix(120)), time: formatTimestamp(ts))
        }
        // Also pull raw ISO from the newest line so we can render "Ns ago"
        var latestISO = ""
        var latestSource = ""
        var latestPreview = ""
        if let last = lines.last,
           let json = try? JSONSerialization.jsonObject(with: Data(last)) as? [String: String] {
            latestISO = json["timestamp"] ?? ""
            latestSource = json["source"] ?? ""
            latestPreview = String((json["text"] ?? "").prefix(90))
        }
        DispatchQueue.main.async {
            self.recentSignals = Array(recent)
            self.lastSignalISO = latestISO
            self.lastSignalSource = latestSource
            self.lastSignalPreview = latestPreview
        }
    }

    // MARK: - Database Readers (iMessage, WhatsApp, Contacts)
    //
    // Read from macOS databases and write JSON buffers to ~/.deja/
    // so Python observers don't need Full Disk Access.

    private func startDatabaseReaders() {
        // Read immediately, then every 15 seconds
        readDatabases()
        dbReaderTimer = Timer.scheduledTimer(withTimeInterval: 15.0, repeats: true) { [weak self] _ in
            self?.readDatabases()
        }
    }

    private func readDatabases() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.readIMessages()
            self?.readWhatsApp()
            // Contacts only every 5 minutes
            if let self = self, Date().timeIntervalSince(self.lastContactsRefresh) > 300 {
                self.readContacts()
                self.lastContactsRefresh = Date()
            }
        }
    }

    // Apple epoch: 2001-01-01 UTC = 978307200 seconds after Unix epoch
    private static let appleEpochOffset: Double = 978307200

    private func readIMessages() {
        let dbPath = NSHomeDirectory() + "/Library/Messages/chat.db"
        guard FileManager.default.fileExists(atPath: dbPath) else { return }

        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return }
        defer { sqlite3_close(db) }

        // Cutoff: 5 minutes ago, in Apple nanoseconds
        let cutoffUnix = Date().timeIntervalSince1970 - 300
        let cutoffAppleNs = Int64((cutoffUnix - Self.appleEpochOffset) * 1_000_000_000)

        let sql = """
            SELECT m.text, m.date, m.is_from_me, h.id as handle_id
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL AND m.text != '' AND m.date > ?1
            ORDER BY m.date DESC LIMIT 50
            """

        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_int64(stmt, 1, cutoffAppleNs)

        var messages: [[String: Any]] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let text: String
            if let cStr = sqlite3_column_text(stmt, 0) {
                text = String(cString: cStr)
            } else { continue }

            let appleNs = sqlite3_column_int64(stmt, 1)
            let unixTs = Double(appleNs) / 1_000_000_000.0 + Self.appleEpochOffset
            let isFromMe = sqlite3_column_int(stmt, 2) == 1

            let handleId: String
            if let cStr = sqlite3_column_text(stmt, 3) {
                handleId = String(cString: cStr)
            } else {
                handleId = "unknown"
            }

            let sender = isFromMe ? "me" : handleId

            // Format datetime in local time
            let date = Date(timeIntervalSince1970: unixTs)
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
            let dt = fmt.string(from: date)

            messages.append([
                "text": String(text.prefix(500)),
                "timestamp": unixTs,
                "dt": dt,
                "is_from_me": isFromMe,
                "sender": sender,
            ])
        }

        writeJSONBuffer(messages, to: "imessage_buffer.json")
    }

    private func readWhatsApp() {
        let dbPath = NSHomeDirectory() + "/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
        guard FileManager.default.fileExists(atPath: dbPath) else { return }

        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return }
        defer { sqlite3_close(db) }

        // Cutoff: 5 minutes ago, in Apple epoch seconds
        let cutoffUnix = Date().timeIntervalSince1970 - 300
        let cutoffApple = cutoffUnix - Self.appleEpochOffset

        let sql = """
            SELECT ZWAMESSAGE.ZTEXT, ZWAMESSAGE.ZMESSAGEDATE, ZWAMESSAGE.ZISFROMME,
                   ZWACHATSESSION.ZCONTACTJID
            FROM ZWAMESSAGE
            LEFT JOIN ZWACHATSESSION ON ZWAMESSAGE.ZCHATSESSION = ZWACHATSESSION.Z_PK
            WHERE ZWAMESSAGE.ZTEXT IS NOT NULL AND ZWAMESSAGE.ZTEXT != '' AND ZWAMESSAGE.ZMESSAGEDATE > ?1
            ORDER BY ZWAMESSAGE.ZMESSAGEDATE DESC LIMIT 50
            """

        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_double(stmt, 1, cutoffApple)

        var messages: [[String: Any]] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let text: String
            if let cStr = sqlite3_column_text(stmt, 0) {
                text = String(cString: cStr)
            } else { continue }

            let appleSec = sqlite3_column_double(stmt, 1)
            let unixTs = appleSec + Self.appleEpochOffset
            let isFromMe = sqlite3_column_int(stmt, 2) == 1

            let contactJid: String
            if let cStr = sqlite3_column_text(stmt, 3) {
                contactJid = String(cString: cStr)
            } else {
                contactJid = "unknown"
            }

            let sender = isFromMe ? "me" : contactJid

            let date = Date(timeIntervalSince1970: unixTs)
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
            let dt = fmt.string(from: date)

            messages.append([
                "text": String(text.prefix(500)),
                "timestamp": unixTs,
                "dt": dt,
                "is_from_me": isFromMe,
                "sender": sender,
            ])
        }

        writeJSONBuffer(messages, to: "whatsapp_buffer.json")
    }

    private func readContacts() {
        let abDir = NSHomeDirectory() + "/Library/Application Support/AddressBook/Sources"
        guard FileManager.default.fileExists(atPath: abDir) else { return }

        var contacts: [[String: Any]] = []

        let fm = FileManager.default
        guard let sources = try? fm.contentsOfDirectory(atPath: abDir) else { return }

        for source in sources {
            let dbPath = abDir + "/" + source + "/AddressBook-v22.abcddb"
            guard fm.fileExists(atPath: dbPath) else { continue }

            var db: OpaquePointer?
            guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { continue }

            let sql = """
                SELECT
                    COALESCE(r.ZFIRSTNAME, '') || ' ' || COALESCE(r.ZLASTNAME, '') as name,
                    GROUP_CONCAT(DISTINCT p.ZFULLNUMBER) as phones
                FROM ZABCDRECORD r
                LEFT JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                WHERE r.ZFIRSTNAME IS NOT NULL
                GROUP BY r.Z_PK
                """

            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                sqlite3_close(db)
                continue
            }

            while sqlite3_step(stmt) == SQLITE_ROW {
                let name: String
                if let cStr = sqlite3_column_text(stmt, 0) {
                    name = String(cString: cStr).trimmingCharacters(in: .whitespaces)
                } else { continue }

                if name.isEmpty { continue }

                let phones: String
                if let cStr = sqlite3_column_text(stmt, 1) {
                    phones = String(cString: cStr)
                } else {
                    phones = ""
                }

                contacts.append([
                    "name": name,
                    "phones": phones,
                ])
            }

            sqlite3_finalize(stmt)
            sqlite3_close(db)
        }

        writeJSONBuffer(contacts, to: "contacts_buffer.json")
    }

    private func writeJSONBuffer(_ data: Any, to filename: String) {
        let dirPath = Self.home
        try? FileManager.default.createDirectory(
            atPath: dirPath,
            withIntermediateDirectories: true
        )
        let filePath = dirPath + "/" + filename
        let tmpPath = filePath + ".tmp"

        guard let jsonData = try? JSONSerialization.data(
            withJSONObject: data,
            options: [.sortedKeys]
        ) else { return }

        guard FileManager.default.createFile(atPath: tmpPath, contents: jsonData) else { return }

        // Atomic rename
        do {
            if FileManager.default.fileExists(atPath: filePath) {
                _ = try FileManager.default.replaceItemAt(
                    URL(fileURLWithPath: filePath),
                    withItemAt: URL(fileURLWithPath: tmpPath)
                )
            } else {
                try FileManager.default.moveItem(atPath: tmpPath, toPath: filePath)
            }
        } catch {
            try? FileManager.default.removeItem(atPath: tmpPath)
        }
    }

    private func readKeyFromEnv() -> String? {
        for path in [NSHomeDirectory() + "/.zshrc", NSHomeDirectory() + "/.zprofile", NSHomeDirectory() + "/.bash_profile"] {
            if let content = try? String(contentsOfFile: path, encoding: .utf8) {
                for line in content.split(separator: "\n") {
                    let t = line.trimmingCharacters(in: .whitespaces)
                    if t.hasPrefix("export GEMINI_API_KEY=") {
                        return t.replacingOccurrences(of: "export GEMINI_API_KEY=", with: "").trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                    }
                }
            }
        }
        return nil
    }

}
