import Foundation
import SwiftUI
import AVFoundation
import IOKit.hid
import ApplicationServices
import CoreGraphics

// MARK: - HealthState
//
// Single source of truth for the app's health banner. Merges native
// TCC probes (which only Swift can observe) with the Python-written
// ~/.deja/health.json. TCC rows come first in the ordered list
// because missing permissions block nearly everything downstream.
//
// Access as a singleton via ``HealthState.shared``. The instance is
// passed into SwiftUI as an ``@ObservedObject`` — matching the
// MonitorState pattern already used throughout the app.

final class HealthState: ObservableObject {
    static let shared = HealthState()

    @Published private(set) var overall: HealthStatus = .ok
    @Published private(set) var checks: [HealthCheck] = []
    @Published private(set) var appVersion: String?
    @Published private(set) var lastErrorRequestId: String?
    @Published private(set) var hasEverReported: Bool = false

    private let poller = HealthPollingService()
    private var tccTimer: Timer?
    private var pythonReport: HealthReport?
    private var tccChecks: [HealthCheck] = []

    func start() {
        poller.onReport = { [weak self] report in
            self?.pythonReport = report
            self?.hasEverReported = true
            self?.appVersion = report.appVersion
            self?.lastErrorRequestId = report.lastErrorRequestId
            self?.rebuild()
        }
        poller.start()

        // Native TCC probes — polled at 5s since the user flipping a
        // toggle in System Settings is a human-speed event.
        refreshTCCChecks()
        tccTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.refreshTCCChecks()
        }
    }

    func stop() {
        poller.stop()
        tccTimer?.invalidate()
        tccTimer = nil
    }

    // MARK: - TCC probes

    private func refreshTCCChecks() {
        let screenOK = CGPreflightScreenCaptureAccess()
        let axOK = AXIsProcessTrusted()
        let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        let micOK = (micStatus == .authorized)
        let inputMonOK = (IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted)
        let fdaOK = probeFullDiskAccess()

        let settingsURLScreen = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        let settingsURLAX = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        let settingsURLMic = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
        let settingsURLIM = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
        let settingsURLFDA = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"

        let rows: [HealthCheck] = [
            Self.tccRow(
                id: "tcc_screen",
                label: "Screen Recording",
                granted: screenOK,
                detail: screenOK ? "Granted" : "Déjà can't see your screen — vision and OCR are disabled.",
                fix: "Grant Screen Recording in System Settings → Privacy & Security.",
                fixURL: settingsURLScreen
            ),
            Self.tccRow(
                id: "tcc_accessibility",
                label: "Accessibility",
                granted: axOK,
                detail: axOK ? "Granted" : "Déjà can't read UI context — typing detection and AX sidecars are off.",
                fix: "Grant Accessibility in System Settings → Privacy & Security.",
                fixURL: settingsURLAX
            ),
            Self.tccRow(
                id: "tcc_microphone",
                label: "Microphone",
                granted: micOK,
                detail: micOK ? "Granted" : "Déjà can't record voice or meetings.",
                fix: "Grant Microphone in System Settings → Privacy & Security.",
                fixURL: settingsURLMic
            ),
            Self.tccRow(
                id: "tcc_input_monitoring",
                label: "Input Monitoring",
                granted: inputMonOK,
                detail: inputMonOK ? "Granted" : "Push-to-talk hotkey and typing-aware capture are disabled.",
                fix: "Grant Input Monitoring in System Settings → Privacy & Security.",
                fixURL: settingsURLIM
            ),
            Self.tccRow(
                id: "tcc_full_disk",
                label: "Full Disk Access",
                granted: fdaOK,
                detail: fdaOK ? "Granted" : "Déjà can't read iMessage/Safari history signals.",
                fix: "Grant Full Disk Access in System Settings → Privacy & Security.",
                fixURL: settingsURLFDA
            ),
        ]

        // Only republish + recompute `overall` when something actually changed.
        if rows != tccChecks {
            tccChecks = rows
            rebuild()
        }
    }

    /// Same technique as ``SetupPanelView.checkPermissions``: probe a
    /// file that's only readable under SystemPolicyAllFiles. Safari
    /// history is the canonical canary; fall back to chat.db if the
    /// user has never opened Safari.
    private func probeFullDiskAccess() -> Bool {
        let safariHistory = NSHomeDirectory() + "/Library/Safari/History.db"
        var fd = Darwin.open(safariHistory, O_RDONLY)
        if fd >= 0 {
            Darwin.close(fd)
            return true
        }
        if errno == ENOENT {
            let chatDB = NSHomeDirectory() + "/Library/Messages/chat.db"
            fd = Darwin.open(chatDB, O_RDONLY)
            if fd >= 0 {
                Darwin.close(fd)
                return true
            }
        }
        return false
    }

    private static func tccRow(id: String, label: String, granted: Bool,
                               detail: String, fix: String, fixURL: String) -> HealthCheck {
        HealthCheck(
            id: id,
            label: label,
            status: granted ? .ok : .broken,
            detail: detail,
            fix: granted ? nil : fix,
            fixURL: granted ? nil : fixURL
        )
    }

    // MARK: - Merge + publish

    private func rebuild() {
        // TCC rows first — they block everything downstream, so the user
        // should fix them before chasing a "proxy unreachable" banner
        // that's really just "Screen Recording is off".
        var merged: [HealthCheck] = tccChecks
        if let py = pythonReport {
            merged.append(contentsOf: py.checks)
        }

        // Worst wins. If Python hasn't reported yet, use the TCC rollup.
        var worst: HealthStatus = .ok
        for c in merged { worst = worst.worsen(with: c.status) }
        if let py = pythonReport {
            worst = worst.worsen(with: py.overall)
        }

        DispatchQueue.main.async {
            self.checks = merged
            self.overall = worst
        }
    }
}
