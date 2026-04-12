import AppKit
import Combine
import SwiftUI

/// Borderless, always-on-top floating panel anchored to the bottom-center.
///
/// The window has two visual modes:
///
///   • **collapsed** — 400×56, only the small voice pill is visible.
///     Idle state when nothing's happening, or showing recording /
///     transcribing / transcript-toast variants during voice capture.
///
///   • **expanded** — ~460×620, the full command-center panel is
///     hosted above the pill. Growth is anchored at the bottom-center
///     (the pill's position stays fixed) so the panel slides upward
///     from the pill toward the top of the screen.
///
/// Expansion is driven by ``MonitorState.pillExpanded``. The window
/// observes the flag via Combine and animates ``setFrame`` when it
/// flips. This replaces the former NSStatusItem + NSPopover surface
/// — the pill IS the app now.
class VoicePillWindow: NSPanel {
    private let monitor: MonitorState
    private var cancellables: Set<AnyCancellable> = []

    /// Dimensions of the collapsed pill. Matches historical sizing so
    /// the pill's resting shape stays unchanged.
    private static let collapsedSize = NSSize(width: 400, height: 56)

    /// Dimensions of the expanded panel. Width is slightly wider than
    /// the pill so the command-center content has breathing room;
    /// height is calibrated to fit header + briefing + activity feed
    /// + command input without scrolling on a 1080p display.
    private static let expandedSize = NSSize(width: 460, height: 640)

    init(monitor: MonitorState) {
        self.monitor = monitor
        super.init(
            contentRect: NSRect(origin: .zero, size: Self.collapsedSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
        hidesOnDeactivate = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        isMovableByWindowBackground = false
        ignoresMouseEvents = false
        acceptsMouseMovedEvents = true

        // The whole content is one SwiftUI hosting view whose body
        // renders either just the pill or pill-plus-panel based on
        // ``monitor.pillExpanded``. The window frame is separately
        // animated so SwiftUI's transition matches the AppKit window
        // resize.
        let rootView = VoicePillContainer(monitor: monitor)
        let hostView = NSHostingView(rootView: rootView)
        hostView.frame = NSRect(origin: .zero, size: Self.expandedSize)
        hostView.autoresizingMask = [.width, .height]

        let trackingOverlay = PillTrackingOverlay(monitor: monitor)
        trackingOverlay.autoresizingMask = [.width]

        let container = NSView(frame: NSRect(origin: .zero, size: Self.expandedSize))
        container.autoresizesSubviews = true
        container.addSubview(hostView)
        container.addSubview(trackingOverlay)
        contentView = container

        // Tracking overlay needs to sit over the pill portion only,
        // not the expanded panel (which has its own hit testing for
        // the text field, buttons, etc.). updateTrackingFrame pins it
        // to the bottom 56pt whenever the window is resized.
        updateTrackingFrame(overlay: trackingOverlay, height: Self.collapsedSize.height)

        applyFrame(for: false, animate: false)
        positionAtBottomCenter()

        // React to expand/collapse state changes. Using Combine
        // avoids a manual notification center dance.
        monitor.$pillExpanded
            .receive(on: DispatchQueue.main)
            .sink { [weak self, weak trackingOverlay] expanded in
                guard let self = self else { return }
                self.applyFrame(for: expanded, animate: true)
                if let overlay = trackingOverlay {
                    self.updateTrackingFrame(overlay: overlay, height: Self.collapsedSize.height)
                }
            }
            .store(in: &cancellables)

        // Error toast needs vertical headroom above the pill. When an
        // error is present we resize the window to the error-toast
        // height (if not already expanded); when it clears we collapse
        // back to whatever state pillExpanded dictates.
        monitor.$currentError
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self = self else { return }
                self.applyFrame(for: self.monitor.pillExpanded, animate: true)
            }
            .store(in: &cancellables)

        NotificationCenter.default.addObserver(
            self, selector: #selector(screenChanged),
            name: NSApplication.didChangeScreenParametersNotification, object: nil
        )
    }

    override var canBecomeKey: Bool { monitor.pillExpanded }
    override var canBecomeMain: Bool { false }

    override func resignKey() {
        super.resignKey()
        // Click-outside collapses the expanded panel. We only take key
        // status while expanded (see canBecomeKey), so losing it is a
        // reliable signal that the user clicked elsewhere.
        if monitor.pillExpanded {
            DispatchQueue.main.async { [weak self] in
                self?.monitor.setPillExpanded(false)
            }
        }
    }

    @objc private func screenChanged() { positionAtBottomCenter() }

    /// Extra height reserved for the error toast above the pill when
    /// the panel itself isn't expanded. Matches ErrorToast's rendered
    /// footprint (≈80pt) plus its 6pt bottom padding and some slack.
    private static let errorToastHeight: CGFloat = 110

    private func applyFrame(for expanded: Bool, animate: Bool) {
        var size = expanded ? Self.expandedSize : Self.collapsedSize
        if !expanded && monitor.currentError != nil {
            size = NSSize(width: Self.collapsedSize.width,
                          height: Self.collapsedSize.height + Self.errorToastHeight)
        }
        let mouseLocation = NSEvent.mouseLocation
        guard let screen = NSScreen.screens.first(where: { $0.frame.contains(mouseLocation) }) ?? NSScreen.main else { return }
        let screenFrame = screen.frame
        let visibleFrame = screen.visibleFrame
        let x = screenFrame.origin.x + (screenFrame.width - size.width) / 2
        // Anchor y at the bottom of the pill — same as collapsed so
        // the pill doesn't jump when the panel grows above it.
        let y = visibleFrame.origin.y + 24
        let newFrame = NSRect(x: x, y: y, width: size.width, height: size.height)
        setFrame(newFrame, display: true, animate: animate)

        if expanded {
            // The panel wants typing focus for the command input.
            // Activating the app is required so TextField becomes
            // first responder. Without this, keystrokes go nowhere.
            NSApp.activate(ignoringOtherApps: true)
            makeKey()
        }
    }

    /// Pin the tracking overlay to the bottom ``height`` pts of the
    /// window so pill clicks register even when the panel above is
    /// visible. When expanded the panel handles its own hit testing.
    private func updateTrackingFrame(overlay: NSView, height: CGFloat) {
        guard let content = contentView else { return }
        overlay.frame = NSRect(
            x: 0,
            y: 0,
            width: content.bounds.width,
            height: height
        )
    }

    func positionAtBottomCenter() {
        applyFrame(for: monitor.pillExpanded, animate: false)
    }
}

/// Transparent overlay that captures mouse events over the pill region.
///
/// Click behavior is now state-dependent: in ``.idle`` a click toggles
/// the expanded panel; in ``.expanded`` it collapses. Voice recording
/// has been moved exclusively to the global hotkey — clicking the
/// pill no longer starts a recording.
class PillTrackingOverlay: NSView {
    private let monitor: MonitorState

    init(monitor: MonitorState) {
        self.monitor = monitor
        super.init(frame: .zero)
    }

    required init?(coder: NSCoder) { fatalError() }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for area in trackingAreas { removeTrackingArea(area) }
        addTrackingArea(NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil
        ))
    }

    override func mouseEntered(with event: NSEvent) {
        DispatchQueue.main.async { self.monitor.voicePillHovered = true }
    }

    override func mouseExited(with event: NSEvent) {
        DispatchQueue.main.async { self.monitor.voicePillHovered = false }
    }

    override func mouseDown(with event: NSEvent) {
        DispatchQueue.main.async {
            // If a voice capture is in progress, clicks on the pill are
            // a no-op — users end the recording by releasing the hotkey
            // or waiting for the auto-stop. Preserving this avoids
            // accidentally dropping an active recording with a stray
            // click while the expanded panel is being discovered.
            if self.monitor.voicePillActive || self.monitor.voicePillProcessing {
                return
            }
            // When Deja is missing something structural (revoked
            // permission, expired auth, first-launch setup pending)
            // the pill click re-opens the setup panel instead of
            // trying to expand the command center. The expanded
            // surface isn't meaningful until the setup checks pass.
            if self.monitor.isBlocked {
                if let delegate = NSApp.delegate as? AppDelegate {
                    delegate.reopenSetupPanel()
                }
                return
            }
            self.monitor.togglePillExpanded()
        }
    }
}
