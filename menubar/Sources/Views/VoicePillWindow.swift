import AppKit
import SwiftUI

/// Borderless, always-on-top floating panel anchored to the bottom-center
/// of the screen. Hosts VoicePillView.
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
        // Must accept mouse events for hover + click
        ignoresMouseEvents = false
        acceptsMouseMovedEvents = true

        let hostView = NSHostingView(rootView: VoicePillView(monitor: monitor))
        let trackingView = PillTrackingView(monitor: monitor, hostedView: hostView)
        contentView = trackingView

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
        let screenFrame = screen.frame
        let visibleFrame = screen.visibleFrame
        let w = frame.width
        let x = screenFrame.origin.x + (screenFrame.width - w) / 2
        let y = visibleFrame.origin.y + 24
        setFrameOrigin(NSPoint(x: x, y: y))
    }
}

/// NSView wrapper that handles mouse tracking without triggering SwiftUI constraint updates.
class PillTrackingView: NSView {
    private let monitor: MonitorState
    private let hostedView: NSView

    init(monitor: MonitorState, hostedView: NSView) {
        self.monitor = monitor
        self.hostedView = hostedView
        super.init(frame: .zero)
        addSubview(hostedView)
        hostedView.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            hostedView.leadingAnchor.constraint(equalTo: leadingAnchor),
            hostedView.trailingAnchor.constraint(equalTo: trailingAnchor),
            hostedView.topAnchor.constraint(equalTo: topAnchor),
            hostedView.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError() }

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
        DispatchQueue.main.async {
            self.monitor.voicePillHovered = true
        }
    }

    override func mouseExited(with event: NSEvent) {
        DispatchQueue.main.async {
            self.monitor.voicePillHovered = false
        }
    }

    override func mouseDown(with event: NSEvent) {
        // Toggle dictation on click
        DispatchQueue.main.async {
            if self.monitor.voicePillActive {
                self.monitor.stopVoiceCapture()
            } else {
                self.monitor.startVoiceCapture()
            }
        }
    }
}
