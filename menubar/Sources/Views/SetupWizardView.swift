import SwiftUI
import AppKit
import CoreGraphics
import ScreenCaptureKit

// MARK: - Setup Wizard
//
// First-launch wizard: Google sign-in → permissions → done.
// Wiki backfill kicks off automatically after setup completes
// and progress is shown in the main Activity view.

struct SetupWizardView: View {
    @ObservedObject var monitor: MonitorState
    @State private var loading: Bool = false
    @State private var error: String = ""
    @State private var gwsEmail: String = ""
    @State private var gwsName: String = ""

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Déjà")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Spacer()
                Text("Step \(max(monitor.setupStep, 1)) of 2")
                    .font(.system(size: 10))
                    .foregroundColor(.white.opacity(0.3))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            Divider().background(Color.white.opacity(0.1))

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch monitor.setupStep {
                    case 0, 1: googleAuthStep
                    case 2: permissionsStep
                    default: EmptyView()
                    }
                }
                .padding(20)
            }
        }
    }

    // MARK: Step 1 — Sign in with Google

    var googleAuthStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Sign in with Google")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(.white)
            Text("Déjà uses your Google account to read Gmail, Calendar, and Drive. This also tells Déjà who you are.")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.5))

            if !gwsEmail.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundColor(.green)
                        Text("Signed in as \(gwsEmail)")
                            .font(.system(size: 12))
                            .foregroundColor(.green)
                    }
                    if !gwsName.isEmpty {
                        Text("Welcome, \(gwsName.components(separatedBy: " ").first ?? gwsName)!")
                            .font(.system(size: 13, weight: .medium))
                            .foregroundColor(.white.opacity(0.7))
                    }
                }
            }

            if !error.isEmpty {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundColor(.red)
            }

            HStack {
                if !gwsEmail.isEmpty {
                    Spacer()
                    Button(action: { monitor.setupStep = 2 }) {
                        Text("Continue")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundColor(.black)
                            .padding(.horizontal, 24)
                            .padding(.vertical, 8)
                            .background(Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                } else if loading {
                    Text("Waiting for sign-in...")
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.4))
                    Spacer()
                    ProgressView().scaleEffect(0.7)
                } else {
                    Button(action: { startGwsAuth() }) {
                        HStack(spacing: 6) {
                            Image(systemName: "globe")
                                .font(.system(size: 11))
                            Text("Connect Google Account")
                                .font(.system(size: 13, weight: .semibold))
                        }
                        .foregroundColor(.white)
                        .padding(.horizontal, 20)
                        .padding(.vertical, 8)
                        .background(Color.blue.opacity(0.4))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)

                    Spacer()

                    Button(action: { monitor.setupStep = 2 }) {
                        Text("Skip for now")
                            .font(.system(size: 12))
                            .foregroundColor(.white.opacity(0.3))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    func startGwsAuth() {
        loading = true
        error = ""
        guard let url = URL(string: "http://localhost:5055/api/setup/gws-auth") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 120

        URLSession.shared.dataTask(with: req) { data, _, err in
            DispatchQueue.main.async {
                loading = false
                if let err = err {
                    error = err.localizedDescription
                    return
                }
                guard let data = data,
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ok = obj["ok"] as? Bool else {
                    error = "Unexpected response"
                    return
                }
                if ok {
                    gwsEmail = obj["email"] as? String ?? "Connected"
                    gwsName = obj["name"] as? String ?? ""
                    if gwsEmail.isEmpty { gwsEmail = "Connected" }
                } else {
                    error = obj["error"] as? String ?? "Auth failed"
                }
            }
        }.resume()
    }

    // MARK: Step 2 — Permissions

    @State private var permSubStep = 0
    @State private var screenRecordingGranted = false
    @State private var fullDiskGranted = false
    @State private var permPollTimer: Timer?

    var permissionsStep: some View {
        VStack(spacing: 0) {
            switch permSubStep {
            case 0: screenRecordingPermission
            case 1: fullDiskPermission
            default: EmptyView()
            }
        }
        .onAppear {
            screenRecordingGranted = CGPreflightScreenCaptureAccess()
            fullDiskGranted = FileManager.default.isReadableFile(
                atPath: NSHomeDirectory() + "/Library/Messages/chat.db"
            )
            if screenRecordingGranted && fullDiskGranted {
                finishSetup()
            } else if screenRecordingGranted {
                permSubStep = 1
            }
        }
        .onDisappear {
            permPollTimer?.invalidate()
            permPollTimer = nil
        }
    }

    var screenRecordingPermission: some View {
        VStack(spacing: 20) {
            Spacer().frame(height: 30)

            Image(systemName: "rectangle.dashed.badge.record")
                .font(.system(size: 40))
                .foregroundColor(screenRecordingGranted ? .green : .orange)

            Text("Set up screen recording")
                .font(.system(size: 20, weight: .semibold))
                .foregroundColor(.white)

            Text("Déjà reads your screen every few seconds to understand what you're working on. Nothing leaves your Mac without your permission.")
                .font(.system(size: 13))
                .foregroundColor(.white.opacity(0.5))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 30)

            if screenRecordingGranted {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(.green)
                    Text("Access granted")
                        .foregroundColor(.white.opacity(0.4))
                }
                .font(.system(size: 13))
            } else {
                Button(action: {
                    Task {
                        _ = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                    }
                    startPollingScreenRecording()
                }) {
                    HStack(spacing: 6) {
                        Text("Allow access")
                            .font(.system(size: 14, weight: .semibold))
                        Image(systemName: "arrow.up.right.square")
                            .font(.system(size: 12))
                    }
                    .foregroundColor(.black)
                    .padding(.horizontal, 24)
                    .padding(.vertical, 10)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
            }

            Spacer()

            HStack {
                Spacer()
                Button(action: {
                    permPollTimer?.invalidate()
                    permSubStep = 1
                }) {
                    HStack(spacing: 4) {
                        Text(screenRecordingGranted ? "Continue" : "Skip for now")
                            .font(.system(size: 13, weight: screenRecordingGranted ? .semibold : .regular))
                        if screenRecordingGranted {
                            Image(systemName: "arrow.right")
                                .font(.system(size: 11))
                        }
                    }
                    .foregroundColor(screenRecordingGranted ? .black : .white.opacity(0.3))
                    .padding(.horizontal, 24)
                    .padding(.vertical, 8)
                    .background(screenRecordingGranted ? Color.white : Color.clear)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 16)
        }
        .frame(maxWidth: .infinity)
    }

    func startPollingScreenRecording() {
        permPollTimer?.invalidate()
        permPollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [self] timer in
            let granted = CGPreflightScreenCaptureAccess()
            if granted {
                screenRecordingGranted = true
                timer.invalidate()
            }
        }
    }

    var fullDiskPermission: some View {
        VStack(spacing: 20) {
            Spacer().frame(height: 30)

            Image(systemName: "lock.open.fill")
                .font(.system(size: 40))
                .foregroundColor(fullDiskGranted ? .green : .orange)

            Text("Set up message access")
                .font(.system(size: 20, weight: .semibold))
                .foregroundColor(.white)

            Text("Déjà reads your iMessage and WhatsApp conversations to understand who you're talking to and what you're working on.")
                .font(.system(size: 13))
                .foregroundColor(.white.opacity(0.5))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 30)

            if fullDiskGranted {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(.green)
                    Text("Access granted")
                        .foregroundColor(.white.opacity(0.4))
                }
                .font(.system(size: 13))
            } else {
                VStack(spacing: 8) {
                    Button(action: {
                        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") {
                            NSWorkspace.shared.open(url)
                        }
                        startPollingFullDisk()
                    }) {
                        HStack(spacing: 6) {
                            Text("Open System Settings")
                                .font(.system(size: 14, weight: .semibold))
                            Image(systemName: "arrow.up.right.square")
                                .font(.system(size: 12))
                        }
                        .foregroundColor(.black)
                        .padding(.horizontal, 24)
                        .padding(.vertical, 10)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)

                    Text("Find Deja in the list and toggle it on")
                        .font(.system(size: 11))
                        .foregroundColor(.white.opacity(0.3))
                }
            }

            Spacer()

            HStack {
                Spacer()
                Button(action: {
                    permPollTimer?.invalidate()
                    finishSetup()
                }) {
                    HStack(spacing: 4) {
                        Text(fullDiskGranted ? "Done" : "Skip for now")
                            .font(.system(size: 13, weight: fullDiskGranted ? .semibold : .regular))
                        if fullDiskGranted {
                            Image(systemName: "checkmark")
                                .font(.system(size: 11))
                        }
                    }
                    .foregroundColor(fullDiskGranted ? .black : .white.opacity(0.3))
                    .padding(.horizontal, 24)
                    .padding(.vertical, 8)
                    .background(fullDiskGranted ? Color.white : Color.clear)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 16)
        }
        .frame(maxWidth: .infinity)
    }

    func startPollingFullDisk() {
        permPollTimer?.invalidate()
        permPollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [self] timer in
            let granted = FileManager.default.isReadableFile(
                atPath: NSHomeDirectory() + "/Library/Messages/chat.db"
            )
            if granted {
                fullDiskGranted = true
                timer.invalidate()
            }
        }
    }

    // MARK: Finish — complete setup and auto-start backfill

    func finishSetup() {
        monitor.completeSetup()
        monitor.startBackfillAndPoll()
    }
}
