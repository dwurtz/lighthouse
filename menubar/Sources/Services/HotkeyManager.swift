import AppKit
import Foundation
import CoreGraphics

/// Monitors for hold-to-talk via the Option (⌥) key.
/// Polls CGEventSource for modifier state at 60Hz.
class HotkeyManager {
    var onKeyDown: (() -> Void)?
    var onKeyUp: (() -> Void)?

    private var pollTimer: Timer?
    private var isHolding = false

    func start() {
        AXIsProcessTrustedWithOptions(
            [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
        )

        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            self?.checkModifiers()
        }
    }

    func stop() {
        pollTimer?.invalidate()
        pollTimer = nil
        isHolding = false
    }

    private func checkModifiers() {
        let flags = NSEvent.modifierFlags
        let optionDown = flags.contains(.option)
        let otherMods = !flags.intersection([.command, .control, .shift]).isEmpty

        if optionDown && !otherMods && !isHolding {
            isHolding = true
            onKeyDown?()
        } else if !optionDown && isHolding {
            isHolding = false
            onKeyUp?()
        } else if isHolding && otherMods {
            isHolding = false
            onKeyUp?()
        }
    }
}
