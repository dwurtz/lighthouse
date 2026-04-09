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
        isMovableByWindowBackground = true

        let hostView = NSHostingView(rootView: SetupPanelView(monitor: monitor))
        contentView = hostView

        centerOnScreen()

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
        centerOnScreen()
    }

    private func centerOnScreen() {
        guard let screen = NSScreen.main else { return }
        let screenFrame = screen.visibleFrame
        let x = screenFrame.origin.x + (screenFrame.width - frame.width) / 2
        let y = screenFrame.origin.y + (screenFrame.height - frame.height) / 2
        setFrameOrigin(NSPoint(x: x, y: y))
    }
}
