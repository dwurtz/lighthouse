import AppKit
import SwiftUI

/// Borderless floating panel that hosts ``SignalHealthPanel`` — the
/// diagnostic surface for per-source collector liveness. Mirrors
/// ``SettingsPanelWindow``'s shape so all escape-hatch windows feel
/// consistent. Reused across invocations — ``AppDelegate`` caches the
/// instance and simply orders it forward on subsequent shows.
class SignalHealthPanelWindow: NSPanel {
    init(monitor: MonitorState) {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 540),
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
        isMovable = true
        isReleasedWhenClosed = false

        let hostView = NSHostingView(rootView: SignalHealthPanelContainer(monitor: monitor))
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

/// Thin wrapper so the panel can close itself via the X button and so
/// the view can read an always-valid window reference for key/visible
/// state checks.
private struct SignalHealthPanelContainer: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        ZStack(alignment: .topTrailing) {
            SignalHealthPanel(monitor: monitor)
                .background(Color.black)

            Button(action: { NSApp.keyWindow?.close() }) {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(.white.opacity(0.5))
                    .frame(width: 22, height: 22)
                    .background(Color.white.opacity(0.06))
                    .clipShape(Circle())
            }
            .buttonStyle(.plain)
            .padding(10)
        }
        .frame(width: 520, height: 540)
    }
}
