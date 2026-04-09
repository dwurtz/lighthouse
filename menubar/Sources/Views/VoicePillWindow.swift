import AppKit
import SwiftUI

/// Borderless, always-on-top floating panel anchored to the bottom-center
/// of the screen. Hosts VoicePillView with mouse tracking for hover + click.
class VoicePillWindow: NSPanel {
    private let monitor: MonitorState

    init(monitor: MonitorState) {
        self.monitor = monitor
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 300, height: 56),
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

        // Container holds the SwiftUI view + transparent tracking overlay
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 300, height: 56))
        container.autoresizesSubviews = false

        let hostView = NSHostingView(rootView: VoicePillView(monitor: monitor))
        // Pin to fixed size — prevents constraint updates that crash borderless panels
        hostView.frame = NSRect(x: 0, y: 0, width: 300, height: 56)
        hostView.sizingOptions = []
        container.addSubview(hostView)

        let trackingOverlay = PillTrackingOverlay(monitor: monitor)
        trackingOverlay.frame = NSRect(x: 0, y: 0, width: 300, height: 56)
        container.addSubview(trackingOverlay)

        contentView = container

        positionAtBottomCenter()

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(screenChanged),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    @objc private func screenChanged() {
        positionAtBottomCenter()
    }

    func positionAtBottomCenter() {
        let mouseLocation = NSEvent.mouseLocation
        guard let screen = NSScreen.screens.first(where: { $0.frame.contains(mouseLocation) }) ?? NSScreen.main else { return }
        let visibleFrame = screen.visibleFrame
        let screenFrame = screen.frame
        let w = frame.width
        let x = screenFrame.origin.x + (screenFrame.width - w) / 2
        let y = visibleFrame.origin.y + 24
        setFrameOrigin(NSPoint(x: x, y: y))
    }
}

/// Transparent overlay that captures mouse events without interfering with SwiftUI layout.
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
        let area = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(area)
    }

    override func mouseEntered(with event: NSEvent) {
        DispatchQueue.main.async { self.monitor.voicePillHovered = true }
    }

    override func mouseExited(with event: NSEvent) {
        DispatchQueue.main.async { self.monitor.voicePillHovered = false }
    }

    override func mouseDown(with event: NSEvent) {
        DispatchQueue.main.async {
            if self.monitor.voicePillActive {
                self.monitor.stopVoiceCapture()
            } else {
                self.monitor.startVoiceCapture()
            }
        }
    }
}
