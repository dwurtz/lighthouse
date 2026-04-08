import SwiftUI
import AppKit
import CoreGraphics
import ScreenCaptureKit

// MARK: - Setup Wizard
//
// First-launch wizard shown in the popover instead of Chat+Activity.
// Five steps: API key, Google auth, identity, permissions, done.

struct SetupWizardView: View {
    @ObservedObject var monitor: MonitorState
    @State private var apiKey: String = ""
    @State private var userName: String = ""
    @State private var userEmail: String = ""
    @State private var loading: Bool = false
    @State private var error: String = ""
    @State private var gwsEmail: String = ""

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Déjà")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Spacer()
                // Step indicator
                Text("Step \(monitor.setupStep <= 0 ? 1 : monitor.setupStep) of 4")
                    .font(.system(size: 10))
                    .foregroundColor(.white.opacity(0.3))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            Divider().background(Color.white.opacity(0.1))

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch monitor.setupStep {
                    case 0: welcomeStep
                    case 1: apiKeyStep
                    case 2: googleAuthStep
                    case 3: permissionsStep
                    case 4: doneStep
                    default: doneStep
                    }
                }
                .padding(20)
            }
        }
    }

    // MARK: Step 0 — Welcome

    var welcomeStep: some View {
        VStack(spacing: 20) {
            Spacer().frame(height: 40)
            Text("Welcome to Déjà")
                .font(.system(size: 20, weight: .semibold))
                .foregroundColor(.white)
            Text("A personal AI agent for your Mac. It observes your digital life and maintains a living wiki about the people, projects, and events that matter to you.")
                .font(.system(size: 13))
                .foregroundColor(.white.opacity(0.5))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 20)

            Spacer().frame(height: 20)
            Button(action: { monitor.setupStep = 2 }) {
                Text("Get Started")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(.black)
                    .padding(.horizontal, 32)
                    .padding(.vertical, 10)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: Step 1 — API Key

    var apiKeyStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Connect to Gemini")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(.white)
            Text("Déjà uses Google's Gemini AI to understand your screen, messages, and meetings. Get a free API key from Google AI Studio.")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.5))

            Button(action: {
                if let url = URL(string: "https://aistudio.google.com/app/apikey") {
                    NSWorkspace.shared.open(url)
                }
            }) {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.up.right.square")
                        .font(.system(size: 11))
                    Text("Open Google AI Studio")
                        .font(.system(size: 12, weight: .medium))
                }
                .foregroundColor(.blue)
            }
            .buttonStyle(.plain)

            SecureField("Paste your API key here", text: $apiKey)
                .textFieldStyle(.plain)
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(.white)
                .padding(10)
                .background(Color.white.opacity(0.06))
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.white.opacity(0.1)))

            if !error.isEmpty {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundColor(.red)
            }

            HStack {
                Spacer()
                if loading {
                    ProgressView().scaleEffect(0.7)
                } else {
                    Button(action: { submitApiKey() }) {
                        Text("Continue")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundColor(.black)
                            .padding(.horizontal, 24)
                            .padding(.vertical, 8)
                            .background(apiKey.isEmpty ? Color.gray : Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .disabled(apiKey.isEmpty)
                }
            }
        }
    }

    func submitApiKey() {
        loading = true
        error = ""
        guard let url = URL(string: "http://localhost:5055/api/setup/api-key") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["key": apiKey])
        req.timeoutInterval = 15

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
                    monitor.setupStep = 2
                } else {
                    error = obj["error"] as? String ?? "Invalid key"
                }
            }
        }.resume()
    }

    // MARK: Step 2 — Sign in with Google

    @State private var gwsName: String = ""

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
                    Button(action: { monitor.setupStep = 3 }) {
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

                    Button(action: { monitor.setupStep = 3 }) {
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

    // MARK: Step 3 — Identity

    var identityStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("About you")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(.white)
            Text("Déjà needs to know who you are so it can distinguish your messages from everyone else's.")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.5))

            VStack(alignment: .leading, spacing: 8) {
                Text("Full name")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.white.opacity(0.4))
                TextField("David Wurtz", text: $userName)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .foregroundColor(.white)
                    .padding(10)
                    .background(Color.white.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                Text("Email")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.white.opacity(0.4))
                TextField("you@example.com", text: $userEmail)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .foregroundColor(.white)
                    .padding(10)
                    .background(Color.white.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }

            if !error.isEmpty {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundColor(.red)
            }

            HStack {
                Spacer()
                if loading {
                    ProgressView().scaleEffect(0.7)
                } else {
                    Button(action: { submitIdentity() }) {
                        Text("Continue")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundColor(.black)
                            .padding(.horizontal, 24)
                            .padding(.vertical, 8)
                            .background(userName.isEmpty || userEmail.isEmpty ? Color.gray : Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .disabled(userName.isEmpty || userEmail.isEmpty)
                }
            }
        }
    }

    func submitIdentity() {
        loading = true
        error = ""
        guard let url = URL(string: "http://localhost:5055/api/setup/identity") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "name": userName,
            "email": userEmail,
        ])
        req.timeoutInterval = 15

        URLSession.shared.dataTask(with: req) { data, _, err in
            DispatchQueue.main.async {
                loading = false
                if let err = err { error = err.localizedDescription; return }
                guard let data = data,
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ok = obj["ok"] as? Bool, ok else {
                    error = "Setup failed"
                    return
                }
                // Move to permissions step
                monitor.setupStep = 4
            }
        }.resume()
    }

    func completeSetup() {
        guard let url = URL(string: "http://localhost:5055/api/setup/complete") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 10
        URLSession.shared.dataTask(with: req) { _, _, _ in
            DispatchQueue.main.async {
                monitor.setupStep = 4
            }
        }.resume()
    }

    // MARK: Step 3 — Permissions

    // Permissions sub-step: 0 = screen recording, 1 = full disk, 2 = all done
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
            // Check if already granted and skip
            screenRecordingGranted = CGPreflightScreenCaptureAccess()
            fullDiskGranted = FileManager.default.isReadableFile(
                atPath: NSHomeDirectory() + "/Library/Messages/chat.db"
            )
            if screenRecordingGranted && fullDiskGranted {
                monitor.setupStep = 4
            } else if screenRecordingGranted {
                permSubStep = 1
            }
        }
        .onDisappear {
            permPollTimer?.invalidate()
            permPollTimer = nil
        }
    }

    // MARK: Screen Recording permission screen

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
                    // Trigger the OS modal dialog for Screen Recording
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

    // MARK: Full Disk Access permission screen

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
                    monitor.setupStep = 4
                }) {
                    HStack(spacing: 4) {
                        Text(fullDiskGranted ? "Continue" : "Skip for now")
                            .font(.system(size: 13, weight: fullDiskGranted ? .semibold : .regular))
                        if fullDiskGranted {
                            Image(systemName: "arrow.right")
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

    // MARK: Step 4 — Building your wiki

    @State private var backfillRunning = false
    @State private var backfillStep = ""
    @State private var backfillStepIndex = 0
    @State private var backfillTotalSteps = 0
    @State private var backfillBatch = 0
    @State private var backfillTotalBatches = 0
    @State private var backfillPages = 0
    @State private var backfillDone = false
    @State private var backfillTimer: Timer?

    var doneStep: some View {
        VStack(spacing: 16) {
            if !backfillRunning && !backfillDone {
                // Initial "Start" screen
                Spacer().frame(height: 20)
                Text("Ready to build your wiki")
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundColor(.white)
                Text("Déjà will scan your last 30 days of email, messages, and calendar to build a complete picture of your people, projects, and events.")
                    .font(.system(size: 13))
                    .foregroundColor(.white.opacity(0.5))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 20)
                Text("This takes a few minutes. You can watch pages appear in real time.")
                    .font(.system(size: 12))
                    .foregroundColor(.white.opacity(0.3))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 20)

                Button(action: { startBackfill() }) {
                    Text("Start Déjà")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundColor(.black)
                        .padding(.horizontal, 32)
                        .padding(.vertical, 10)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)

            } else if backfillRunning {
                // Progress screen
                Spacer().frame(height: 20)
                ProgressView()
                    .scaleEffect(1.2)
                    .padding(.bottom, 8)
                Text("Building your wiki...")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundColor(.white)

                if !backfillStep.isEmpty {
                    Text(backfillStep)
                        .font(.system(size: 13))
                        .foregroundColor(.white.opacity(0.6))
                }

                if backfillTotalBatches > 0 {
                    VStack(spacing: 6) {
                        ProgressView(value: Double(backfillBatch), total: Double(backfillTotalBatches))
                            .progressViewStyle(.linear)
                            .tint(.green)
                        HStack {
                            Text("Step \(backfillStepIndex)/\(backfillTotalSteps)")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundColor(.white.opacity(0.3))
                            Spacer()
                            Text("Batch \(backfillBatch)/\(backfillTotalBatches)")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundColor(.white.opacity(0.3))
                        }
                    }
                    .padding(.horizontal, 30)
                }

                if backfillPages > 0 {
                    Text("\(backfillPages) wiki pages created")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(.green)
                }

            } else {
                // Done
                Spacer().frame(height: 20)
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 48))
                    .foregroundColor(.green)
                Text("Your wiki is ready")
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundColor(.white)
                if backfillPages > 0 {
                    Text("\(backfillPages) pages created from your last 30 days")
                        .font(.system(size: 13))
                        .foregroundColor(.white.opacity(0.5))
                }
                Text("Open ~/Deja in Obsidian to browse your wiki.")
                    .font(.system(size: 12))
                    .foregroundColor(.white.opacity(0.3))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 20)

                Button(action: { monitor.completeSetup() }) {
                    Text("Open Déjà")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundColor(.black)
                        .padding(.horizontal, 32)
                        .padding(.vertical, 10)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
            }
        }
        .frame(maxWidth: .infinity)
    }

    func startBackfill() {
        // Complete setup and start the backfill — MonitorState handles
        // the polling and shows a banner in the main view.
        monitor.completeSetup()
        monitor.startBackfillAndPoll()
    }
}
