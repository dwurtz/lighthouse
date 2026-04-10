import AppKit
import Foundation
import CoreGraphics

/// Tracks the time of the most recent keystroke anywhere on the system.
/// Used to defer screenshot capture while the user is actively typing —
/// mid-sentence captures confuse the vision model.
///
/// Requires Accessibility permission (already granted for the hotkey).
class KeystrokeMonitor {
    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?

    /// Wall-clock time of the last observed keyDown. 0 means "never".
    var lastKeystrokeTime: TimeInterval {
        return _lastKeystrokeTime
    }

    /// Seconds since the last keystroke. Returns .infinity if no keystroke yet.
    var idleSeconds: TimeInterval {
        guard _lastKeystrokeTime > 0 else { return .infinity }
        return Date().timeIntervalSince1970 - _lastKeystrokeTime
    }

    func start() {
        let mask: CGEventMask = (1 << CGEventType.keyDown.rawValue)
        let refcon = UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: keystrokeTapCallback,
            userInfo: refcon
        ) else {
            NSLog("deja: KeystrokeMonitor — CGEvent tap creation failed (Accessibility?)")
            return
        }

        eventTap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        NSLog("deja: KeystrokeMonitor started")
    }

    func stop() {
        if let tap = eventTap {
            CGEvent.tapEnable(tap: tap, enable: false)
            if let source = runLoopSource {
                CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
            }
        }
        eventTap = nil
        runLoopSource = nil
    }

    fileprivate func recordKeystroke() {
        _lastKeystrokeTime = Date().timeIntervalSince1970
    }
}

// Stored as a file-private global because the C tap callback can't capture context.
// One process = one keystroke monitor in practice; if we need multiple, switch
// to a refcon-keyed dictionary.
private var _lastKeystrokeTime: TimeInterval = 0

private func keystrokeTapCallback(
    proxy: CGEventTapProxy,
    type: CGEventType,
    event: CGEvent,
    refcon: UnsafeMutableRawPointer?
) -> Unmanaged<CGEvent>? {
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        // Re-enable on disable (system can disable taps under load)
        if let refcon = refcon {
            let monitor = Unmanaged<KeystrokeMonitor>.fromOpaque(refcon).takeUnretainedValue()
            monitor.start()
        }
        return Unmanaged.passUnretained(event)
    }

    if type == .keyDown {
        _lastKeystrokeTime = Date().timeIntervalSince1970
    }
    return Unmanaged.passUnretained(event)
}
