import AppKit
import ApplicationServices
import CoreFoundation
import Foundation

// MARK: - Screen Capture Scheduler
//
// Replaces the old 6s fixed-interval screenshot timer with an
// event-driven policy that fires on three signals:
//
//   1. App focus change      (NSWorkspace.didActivateApplication)  — 500ms debounce
//   2. Typing pause          (KeystrokeMonitor.onTypingPause)       — 0ms (fire immediately)
//   3. Window change (AX)    (focused-window / title / size)        — 1000ms debounce
//
// A 60s floor timer acts as a fallback for passive reading (long
// documents, video playback, scrolling) where none of the above
// events fire. The floor timer is reset every time an event-driven
// capture actually lands, so it only fires when nothing else has.
//
// Capture fan-in point: every trigger routes through
// ``MonitorState.captureScreenshot(force:)`` — the existing
// typing-deferral gate, the voice-command stale-start fix, and the
// focused_frame_norm sidecar writes all live below that call and
// stay unchanged.
//
// Volume estimate: ~1k captures/day vs ~14k/day with the 6s timer,
// while improving signal quality (each capture marks a state
// transition, not a random snapshot).
//
// Threading: all debounce `DispatchWorkItem`s and the floor timer
// run on the main queue so cancel-and-reschedule can't race with
// firing. AX observer callbacks come in on the main run loop
// (we add the AXObserver's source there), which keeps everything
// on one thread.
//
// AX observer lifecycle: when the frontmost app changes we drop
// the previous observer's run-loop source and create a fresh
// observer scoped to the new app. AXObservers aren't freed by
// reassignment — we explicitly remove the source first.

final class ScreenCaptureScheduler {

    // Delegate-like reference to the owner that actually performs
    // the capture. Weak so we don't keep the MonitorState alive.
    private weak var monitorState: MonitorState?

    // Cancel-and-reschedule debounce work item. Always replaced as a
    // unit; any pending fire is cancelled before a new one is armed.
    private var pendingCapture: DispatchWorkItem?

    // 60s passive-reading floor. Armed on start(), reset after every
    // successful event-driven capture, and re-armed after it fires.
    private var floorTimer: DispatchSourceTimer?

    // Notification observer tokens for clean teardown.
    private var appActivationObserver: NSObjectProtocol?
    private var keystrokeMonitorRef: KeystrokeMonitor?

    // AX observer scoped to the current frontmost app. When the
    // frontmost changes we tear this down and build a new one.
    private var axObserver: AXObserver?
    private var axObservedPID: pid_t = 0
    private var axObservedElement: AXUIElement?
    private var axRunLoopSource: CFRunLoopSource?

    // Debounce windows — named for clarity at call sites.
    //
    // typingPauseDebounce was 0.0 ("fire immediately"); bumped to 0.5s
    // so the UI has time to settle after the user stops typing. The
    // 2s quiet threshold in KeystrokeMonitor already guarantees the
    // user isn't mid-burst; the extra 0.5s covers the message-sent
    // animation + "Marked Done" state update + any background
    // reflows. Also coheres with appFocusDebounce so rapid "type →
    // send → app switches focus" sequences don't stack 2-3 captures
    // where 1 suffices.
    private let appFocusDebounce: TimeInterval = 0.5
    private let typingPauseDebounce: TimeInterval = 0.5
    private let windowChangeDebounce: TimeInterval = 1.0

    // Floor interval (seconds of no event → force a capture).
    private let floorInterval: TimeInterval = 60.0

    private var started = false

    // MARK: - Lifecycle

    func start(monitorState: MonitorState, keystrokeMonitor: KeystrokeMonitor) {
        guard !started else { return }
        started = true

        self.monitorState = monitorState
        self.keystrokeMonitorRef = keystrokeMonitor

        registerAppActivationObserver()
        installKeystrokeCallback(on: keystrokeMonitor)
        registerAXObserverForFrontmostApp()
        startFloorTimer()

        // Seed with an immediate capture so the downstream vision
        // pipeline has a fresh frame to work with, matching the old
        // startScreenshotCapture() behaviour.
        scheduleCapture(after: 0.0, reason: "startup")
    }

    func stop() {
        guard started else { return }
        started = false

        pendingCapture?.cancel()
        pendingCapture = nil

        floorTimer?.cancel()
        floorTimer = nil

        if let obs = appActivationObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(obs)
        }
        appActivationObserver = nil

        keystrokeMonitorRef?.onTypingPause = nil
        keystrokeMonitorRef = nil

        tearDownAXObserver()
    }

    // MARK: - Public notifications

    /// Called by MonitorState right after a successful capture (voice,
    /// scheduler, anywhere). Resets the 60s floor so passive-reading
    /// fallback only fires when nothing else is happening.
    func noteCaptureLanded() {
        restartFloorTimer()
    }

    // MARK: - Trigger plumbing

    private func registerAppActivationObserver() {
        appActivationObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            guard let self = self else { return }
            // Frontmost app changed — re-scope the AX observer to the
            // new process BEFORE scheduling the capture so any
            // subsequent title/resize events land on the right obs.
            self.registerAXObserverForFrontmostApp()
            self.scheduleCapture(after: self.appFocusDebounce, reason: "app-focus")
        }
    }

    private func installKeystrokeCallback(on keystrokeMonitor: KeystrokeMonitor) {
        keystrokeMonitor.onTypingPause = { [weak self] in
            self?.scheduleCapture(after: self?.typingPauseDebounce ?? 0, reason: "typing-pause")
        }
    }

    // MARK: - Debounced capture dispatch

    /// Cancel-and-reschedule: any pending capture is dropped and a
    /// fresh one is armed ``delay`` seconds out. Rapid bursts (Cmd-Tab
    /// through 5 apps in 2 seconds, title churn on a busy terminal)
    /// collapse to a single capture at the tail.
    private func scheduleCapture(after delay: TimeInterval, reason: String) {
        pendingCapture?.cancel()

        let work = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            guard let state = self.monitorState else { return }
            NSLog("deja: ScreenCaptureScheduler firing capture (reason=\(reason))")
            state.captureScreenshot()
            // captureScreenshot defers silently while the user is
            // mid-keystroke; we reset the floor anyway because the
            // next typing-pause event will fire a capture the moment
            // the user takes a breath. Leaving the floor armed would
            // pile on a redundant capture within 60s.
            self.restartFloorTimer()
        }
        pendingCapture = work

        if delay <= 0 {
            DispatchQueue.main.async(execute: work)
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        }
    }

    // MARK: - Floor timer

    private func startFloorTimer() {
        restartFloorTimer()
    }

    private func restartFloorTimer() {
        floorTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + floorInterval, repeating: .never)
        timer.setEventHandler { [weak self] in
            guard let self = self else { return }
            NSLog("deja: ScreenCaptureScheduler firing capture (reason=floor)")
            self.monitorState?.captureScreenshot()
            // Re-arm for the next quiet window. If an event-driven
            // capture comes in first it'll reset us via noteCaptureLanded.
            self.restartFloorTimer()
        }
        timer.resume()
        floorTimer = timer
    }

    // MARK: - AX observer

    /// (Re)install an AX observer on the currently-frontmost app so
    /// we fire on focused-window changes, window creation, title
    /// changes and window resizes. Called from `start()` and every
    /// time the frontmost app changes.
    private func registerAXObserverForFrontmostApp() {
        // If we already have an observer for the same PID, keep it.
        let frontPID = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
        if frontPID == 0 { return }
        if frontPID == axObservedPID && axObserver != nil { return }

        // Drop the old observer's run-loop source before building a
        // new one — otherwise we leak both the CFRunLoopSource and
        // keep receiving stale callbacks from the previous app.
        tearDownAXObserver()

        var observer: AXObserver?
        let createErr = AXObserverCreate(frontPID, axNotificationCallback, &observer)
        guard createErr == .success, let newObs = observer else {
            NSLog("deja: AXObserverCreate failed (pid=\(frontPID), err=\(createErr.rawValue))")
            return
        }

        let appElement = AXUIElementCreateApplication(frontPID)
        let refcon = UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())

        let notifications: [String] = [
            kAXFocusedWindowChangedNotification as String,
            kAXWindowCreatedNotification as String,
            kAXTitleChangedNotification as String,
            kAXWindowResizedNotification as String,
        ]

        for name in notifications {
            let err = AXObserverAddNotification(newObs, appElement, name as CFString, refcon)
            // kAXErrorNotificationUnsupported is common for apps that
            // don't emit that particular signal (e.g. some Electron
            // apps). Not fatal — we keep the observer for the
            // notifications that did register.
            if err != .success && err != .notificationAlreadyRegistered {
                NSLog("deja: AXObserverAddNotification(\(name)) -> \(err.rawValue) for pid=\(frontPID)")
            }
        }

        let source = AXObserverGetRunLoopSource(newObs)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)

        axObserver = newObs
        axObservedPID = frontPID
        axObservedElement = appElement
        axRunLoopSource = source
    }

    private func tearDownAXObserver() {
        if let source = axRunLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
        }
        axRunLoopSource = nil
        axObserver = nil           // releasing the AXObserver releases its source internally too
        axObservedElement = nil
        axObservedPID = 0
    }

    // Called from the C callback (below) to avoid burying policy in
    // a global function.
    fileprivate func handleAXNotification(_ name: String) {
        scheduleCapture(after: windowChangeDebounce, reason: "ax-\(name)")
    }
}

// MARK: - AX C callback trampoline

private func axNotificationCallback(
    observer: AXObserver,
    element: AXUIElement,
    notification: CFString,
    refcon: UnsafeMutableRawPointer?
) {
    guard let refcon = refcon else { return }
    let scheduler = Unmanaged<ScreenCaptureScheduler>.fromOpaque(refcon).takeUnretainedValue()
    let name = notification as String
    // Marshal back to main — AX callbacks already arrive on the main
    // run loop thread because we added the source to CFRunLoopGetMain,
    // but being explicit here keeps the debouncer safe even if that
    // ever changes.
    if Thread.isMainThread {
        scheduler.handleAXNotification(name)
    } else {
        DispatchQueue.main.async {
            scheduler.handleAXNotification(name)
        }
    }
}
