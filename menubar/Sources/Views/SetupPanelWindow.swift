import AppKit
import SwiftUI

class SetupPanelWindow: NSPanel {
    init(monitor: MonitorState) {
        // Height matches SetupPanelView's .frame() — bumped from 500 to
        // 540 when the 2-line description wrapping landed.
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 540),
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
        // Draggable by any point in the window's background — since the
        // panel is borderless with no title bar, this is the only way for
        // users to move it out of the way of content they want to see.
        isMovableByWindowBackground = true
        isMovable = true

        let hostView = NSHostingView(rootView: SetupPanelView(monitor: monitor))
        hostView.layer?.cornerRadius = 16
        hostView.layer?.masksToBounds = true
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

    /// Center the panel on whichever screen currently holds the mouse cursor.
    /// Previously we anchored above the voice pill at y=80, but the pill is
    /// now hidden during first-run setup, so centering is the natural placement.
    private func centerOnScreen() {
        let mouseLocation = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { $0.frame.contains(mouseLocation) })
            ?? NSScreen.main
            ?? NSScreen.screens[0]
        let screenFrame = screen.visibleFrame
        let x = screenFrame.origin.x + (screenFrame.width - frame.width) / 2
        let y = screenFrame.origin.y + (screenFrame.height - frame.height) / 2
        setFrameOrigin(NSPoint(x: x, y: y))
    }
}
