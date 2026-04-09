import AppKit
import SwiftUI

class SetupPanelWindow: NSPanel {
    init(monitor: MonitorState) {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 500),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        hidesOnDeactivate = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        isMovableByWindowBackground = false

        let hostView = NSHostingView(rootView: SetupPanelView(monitor: monitor))
        contentView = hostView

        positionAbovePill()

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(screenChanged),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    @objc private func screenChanged() {
        positionAbovePill()
    }

    private func positionAbovePill() {
        // Anchor at bottom-center, just above the voice pill position.
        // The pill sits at y=24 from the bottom; we stack above it.
        guard let screen = NSScreen.main else { return }
        let screenFrame = screen.visibleFrame
        let x = screenFrame.origin.x + (screenFrame.width - frame.width) / 2
        let y = screenFrame.origin.y + 80  // above the pill (pill is at y=24, height ~56)
        setFrameOrigin(NSPoint(x: x, y: y))
    }
}
