import AppKit
import SwiftUI
import Combine

/// Borderless, always-on-top floating panel anchored to the bottom-center
/// of the screen. Resizes dynamically to match pill state — tiny when
/// collapsed so it doesn't block clicks on other content.
class VoicePillWindow: NSPanel {
    private let monitor: MonitorState
    private var cancellables = Set<AnyCancellable>()

    // Sizes for each state
    private static let collapsedSize = NSSize(width: 140, height: 16)
    private static let hoveredSize = NSSize(width: 200, height: 36)
    private static let expandedSize = NSSize(width: 300, height: 56)
    private static let transcriptSize = NSSize(width: 400, height: 44)

    init(monitor: MonitorState) {
        self.monitor = monitor
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: Self.collapsedSize.width, height: Self.collapsedSize.height),
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
        hostView.sizingOptions = []

        let trackingOverlay = PillTrackingOverlay(monitor: monitor)

        let container = NSView()
        container.addSubview(hostView)
        container.addSubview(trackingOverlay)
        contentView = container

        applySize(Self.collapsedSize)
        positionAtBottomCenter()

        // Watch for state changes and resize the window
        monitor.$voicePillActive.sink { [weak self] _ in self?.updateWindowSize() }.store(in: &cancellables)
        monitor.$voicePillProcessing.sink { [weak self] _ in self?.updateWindowSize() }.store(in: &cancellables)
        monitor.$voicePillHovered.sink { [weak self] _ in self?.updateWindowSize() }.store(in: &cancellables)
        monitor.$voicePillTranscript.sink { [weak self] _ in self?.updateWindowSize() }.store(in: &cancellables)

        NotificationCenter.default.addObserver(
            self, selector: #selector(screenChanged),
            name: NSApplication.didChangeScreenParametersNotification, object: nil
        )
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    @objc private func screenChanged() { positionAtBottomCenter() }

    private func updateWindowSize() {
        let newSize: NSSize
        if !monitor.voicePillTranscript.isEmpty {
            newSize = Self.transcriptSize
        } else if monitor.voicePillActive || monitor.voicePillProcessing {
            newSize = Self.expandedSize
        } else if monitor.voicePillHovered {
            newSize = Self.hoveredSize
        } else {
            newSize = Self.collapsedSize
        }
        applySize(newSize)
        positionAtBottomCenter()
    }

    private func applySize(_ size: NSSize) {
        // Resize container, host view, and tracking overlay
        if let container = contentView {
            container.frame = NSRect(origin: .zero, size: size)
            for sub in container.subviews {
                sub.frame = NSRect(origin: .zero, size: size)
            }
        }
        setContentSize(size)
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
