import AppKit
import SwiftUI

/// Borderless, always-on-top floating panel anchored to the bottom-center.
/// Fixed size — small enough not to block much, large enough for all pill states.
class VoicePillWindow: NSPanel {
    private let monitor: MonitorState

    init(monitor: MonitorState) {
        self.monitor = monitor
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 56),
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

        let hostView = NSHostingView(rootView: VoicePillView(monitor: monitor))
        hostView.frame = NSRect(x: 0, y: 0, width: 400, height: 56)
        hostView.sizingOptions = []

        let trackingOverlay = PillTrackingOverlay(monitor: monitor)
        trackingOverlay.frame = NSRect(x: 0, y: 0, width: 400, height: 56)

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 400, height: 56))
        container.autoresizesSubviews = false
        container.addSubview(hostView)
        container.addSubview(trackingOverlay)
        contentView = container

        positionAtBottomCenter()

        NotificationCenter.default.addObserver(
            self, selector: #selector(screenChanged),
            name: NSApplication.didChangeScreenParametersNotification, object: nil
        )
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    @objc private func screenChanged() { positionAtBottomCenter() }

    func positionAtBottomCenter() {
        let mouseLocation = NSEvent.mouseLocation
        guard let screen = NSScreen.screens.first(where: { $0.frame.contains(mouseLocation) }) ?? NSScreen.main else { return }
        let screenFrame = screen.frame
        let visibleFrame = screen.visibleFrame
        let w = frame.width
        let x = screenFrame.origin.x + (screenFrame.width - w) / 2
        let y = visibleFrame.origin.y + 24
        setFrameOrigin(NSPoint(x: x, y: y))
    }
}

/// Transparent overlay that captures mouse events.
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
            options: [.mouseEnteredAndExited, .activeAlways],
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
            if self.monitor.voicePillActive {
                self.monitor.stopVoiceCapture()
            } else {
                self.monitor.startVoiceCapture()
            }
        }
    }
}
