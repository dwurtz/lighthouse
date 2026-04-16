import AppKit
import Foundation
import CoreGraphics

/// Tracks the time of the most recent keystroke anywhere on the system.
/// Used to defer screenshot capture while the user is actively typing —
/// mid-sentence captures confuse the vision model.
///
/// Also emits a "typing pause" signal ``typingPauseThreshold`` seconds
/// after the last keystroke. Consumers register ``onTypingPause`` and
/// fire their own work (e.g. a screen capture) once the user has
/// finished composing. Rapid keystrokes cancel any pending fire, so
/// long typing sessions collapse to a single signal at the tail.
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

    /// Fired on the main queue roughly ``typingPauseThreshold`` seconds
    /// after the last keystroke. Set this to the closure that wants to
    /// react to "user just finished typing" — the scheduler uses it to
    /// capture the composed state of the editor. `nil` disables the
    /// callback entirely (no work item is scheduled).
    var onTypingPause: (() -> Void)?

    /// Delay from the last keystroke before ``onTypingPause`` fires. Also
    /// serves as the minimum idle before the callback actually invokes —
    /// we re-check at fire time to defend against races where a late
    /// keystroke slipped in before the dispatch deadline.
    private let typingPauseThreshold: TimeInterval = 2.0

    /// The pending "typing pause" work item. Always cancel-and-reschedule
    /// on each keystroke so bursts collapse to a single fire.
    private var pendingPauseFire: DispatchWorkItem?

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
        pendingPauseFire?.cancel()
        pendingPauseFire = nil
    }

    fileprivate func recordKeystroke() {
        _lastKeystrokeTime = Date().timeIntervalSince1970
        scheduleTypingPauseFire()
    }

    /// Cancel-and-reschedule the typing-pause fire. Called on every
    /// keystroke so only the LAST keystroke in a burst produces a fire.
    /// Runs on main (DispatchQueue.main.async) so consumers see the
    /// callback on the main thread and don't have to marshal.
    fileprivate func scheduleTypingPauseFire() {
        // Hop to main for mutation + dispatch — recordKeystroke runs
        // from the CGEvent tap thread, which is the main thread in
        // practice (we added the source to CFRunLoopGetMain), but we
        // don't want to rely on that.
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            self.pendingPauseFire?.cancel()

            let threshold = self.typingPauseThreshold
            let work = DispatchWorkItem { [weak self] in
                guard let self = self else { return }
                // Re-check idle at fire time: if a later keystroke
                // landed after the deadline was armed (rare but
                // possible under load), don't fire — the newer
                // keystroke's fire will.
                if self.idleSeconds + 0.05 >= threshold {
                    self.onTypingPause?()
                }
            }
            self.pendingPauseFire = work
            DispatchQueue.main.asyncAfter(deadline: .now() + threshold, execute: work)
        }
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
        if let refcon = refcon {
            let monitor = Unmanaged<KeystrokeMonitor>.fromOpaque(refcon).takeUnretainedValue()
            // Route through the instance method so the typing-pause
            // work-item scheduling can use per-instance state (the
            // cancel-and-reschedule DispatchWorkItem).
            monitor.scheduleTypingPauseFire()
        }
    }
    return Unmanaged.passUnretained(event)
}
