import SwiftUI
import CoreGraphics
import ScreenCaptureKit
import IOKit.hid
import ApplicationServices
import AVFoundation

struct SetupPanelView: View {
    @ObservedObject var monitor: MonitorState

    @State private var gwsEmail: String = ""
    @State private var gwsLoading: Bool = false
    @State private var gwsError: String = ""
    @State private var screenRecordingGranted: Bool = false
    @State private var accessibilityGranted: Bool = false
    @State private var fullDiskGranted: Bool = false
    @State private var micGranted: Bool = false
    @State private var modelStatus: String = "idle"  // idle, downloading, ready, error
    @State private var modelProgress: Double = 0.0
    @State private var modelMessage: String = ""
    @State private var modelBytesDownloaded: Int64 = 0
    @State private var modelBytesTotal: Int64 = 0
    @State private var modelPhase: String = "idle"
    @State private var pollTimer: Timer?

    private var googleDone: Bool { !gwsEmail.isEmpty }
    private var modelReady: Bool { modelStatus == "ready" }
    private var canStart: Bool { googleDone && screenRecordingGranted && accessibilityGranted && modelReady }

    // Brand palette — mirrors site/index.html :root{ --* } tokens.
    // text  = #ece8e1 warm off-white
    // text2 = rgba(255,255,255,0.50) muted
    // text3 = rgba(255,255,255,0.22) disabled
    // bg    = #050507 near-black
    private var brandText: Color { Color(red: 236/255, green: 232/255, blue: 225/255) }
    private var brandText2: Color { Color.white.opacity(0.50) }
    private var brandText3: Color { Color.white.opacity(0.22) }
    private var brandBg: Color { Color(red: 5/255, green: 5/255, blue: 7/255) }

    var body: some View {
        VStack(spacing: 0) {
            // Top bar with close button (right-aligned).
            // Plain xmark glyph with no background. The HStack itself
            // sits at y=16 with 16pt trailing padding so the entire
            // button is well clear of the panel's 16pt corner radius.
            HStack(spacing: 0) {
                Spacer()
                Button(action: hidePanel) {
                    Image(systemName: "xmark")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(brandText2)
                        .frame(width: 24, height: 24)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Close — reopen from the menu bar icon")
            }
            .frame(height: 24)
            .padding(.top, 16)
            .padding(.trailing, 16)

            // Branded header — matches trydeja.com (Cormorant Garamond →
            // New York system serif, light weight 300, warm off-white)
            VStack(spacing: 12) {
                // App icon as logo mark
                if let icon = NSImage(named: NSImage.applicationIconName) {
                    Image(nsImage: icon)
                        .resizable()
                        .interpolation(.high)
                        .frame(width: 56, height: 56)
                        .shadow(color: .black.opacity(0.4), radius: 8, y: 4)
                }

                // "Welcome to Déjà" — serif, light weight
                (
                    Text("Welcome to ")
                        .font(.system(size: 30, weight: .light, design: .serif))
                        .foregroundColor(brandText)
                    +
                    Text("Déjà")
                        .font(.system(size: 30, weight: .light, design: .serif))
                        .italic()
                        .foregroundColor(brandText)
                )
                .tracking(-0.5)

                Text("Connect your accounts and grant permissions to get started.")
                    .font(.system(size: 12, weight: .regular))
                    .foregroundColor(brandText2)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
                    .padding(.top, 2)
            }
            .padding(.top, 8)
            .padding(.bottom, 24)

            // Connections table
            VStack(spacing: 1) {
                // Google Account
                setupRow(
                    icon: "person.crop.circle",
                    title: "Google Account",
                    description: googleDone ? gwsEmail : "Gmail, Calendar, Drive, Meet",
                    done: googleDone,
                    required: true,
                    loading: gwsLoading,
                    error: gwsError,
                    action: connectGoogle
                )

                // Screen Recording
                setupRow(
                    icon: "rectangle.dashed.badge.record",
                    title: "Screen Recording",
                    description: "See what apps and documents are active",
                    done: screenRecordingGranted,
                    required: true,
                    loading: false,
                    error: "",
                    action: grantScreenRecording
                )

                // Accessibility (bundled with Input Monitoring)
                setupRow(
                    icon: "keyboard",
                    title: "Accessibility",
                    description: "Hold Option for push-to-talk and defer captures while typing",
                    done: accessibilityGranted,
                    required: true,
                    loading: false,
                    error: "",
                    action: grantAccessibility
                )

                // On-device AI model
                modelRow

                // Microphone (voice dictation)
                setupRow(
                    icon: "mic",
                    title: "Microphone",
                    description: "Voice dictation with the Listen pill",
                    done: micGranted,
                    required: false,
                    loading: false,
                    error: "",
                    action: grantMicrophone
                )

                // iMessage & WhatsApp
                setupRow(
                    icon: "bubble.left.and.bubble.right",
                    title: "iMessage & WhatsApp",
                    description: "Build wiki pages from your conversations",
                    done: fullDiskGranted,
                    required: false,
                    loading: false,
                    error: "",
                    action: grantFullDisk
                )
            }
            .padding(.horizontal, 20)

            Spacer()

            // Start button
            Button(action: startDeja) {
                Text("Start Déjà")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundColor(canStart ? .black : .white.opacity(0.3))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(canStart ? Color.white : Color.white.opacity(0.1))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
            }
            .buttonStyle(.plain)
            .disabled(!canStart)
            .padding(.horizontal, 20)
            .padding(.bottom, 24)
        }
        // Height bumped from 540 → 620 to fit the new Accessibility
        // row added to the permissions list. Quit lives in the tray
        // icon's right-click menu, not in the panel.
        .frame(width: 420, height: 620)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(Color.black)
                .shadow(color: .black.opacity(0.5), radius: 20)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(Color.white.opacity(0.1), lineWidth: 1)
        )
        .onAppear {
            checkPermissions()
            checkExistingAuth()
            startPolling()
        }
        .onDisappear {
            pollTimer?.invalidate()
        }
    }

    // MARK: - On-Device AI Row

    private var modelRow: some View {
        let downloading = modelStatus == "downloading"
        let errored = modelStatus == "error"
        // When we've polled and know the real total, show it; otherwise
        // fall back to the current hardcoded estimate. This keeps the copy
        // accurate if _FILE_SIZES_MB gets bumped on the Python side.
        let totalStr: String = {
            if modelBytesTotal > 0 {
                return formatBytes(modelBytesTotal)
            }
            return "~8.3 GB"
        }()
        let description = modelReady
            ? "Models ready — private on-device inference"
            : "Download AI models (\(totalStr))"

        return HStack(alignment: .top, spacing: 12) {
            Image(systemName: "cpu")
                .font(.system(size: 16))
                .foregroundColor(modelReady ? .green : .white.opacity(0.5))
                .frame(width: 24)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 2) {
                Text("On-Device AI")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(.white)
                Text(description)
                    .font(.system(size: 11))
                    .foregroundColor(.white.opacity(0.35))

                if downloading {
                    ProgressView(value: max(0.0, min(modelProgress, 1.0)))
                        .progressViewStyle(.linear)
                        .tint(Color.white.opacity(0.6))
                        .background(Color.white.opacity(0.15))
                        .frame(height: 4)
                        .clipShape(Capsule())
                        .padding(.top, 6)

                    HStack(spacing: 6) {
                        Text("\(formatBytes(modelBytesDownloaded)) / \(formatBytes(modelBytesTotal))")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundColor(.white.opacity(0.55))
                        Spacer(minLength: 0)
                        Text("\(Int((modelProgress * 100).rounded()))%")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundColor(.white.opacity(0.55))
                    }
                    .padding(.top, 2)

                    if !modelMessage.isEmpty {
                        Text(modelMessage)
                            .font(.system(size: 10))
                            .foregroundColor(.white.opacity(0.4))
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }

                if errored && !modelMessage.isEmpty {
                    Text(modelMessage)
                        .font(.system(size: 10))
                        .foregroundColor(.red.opacity(0.8))
                        .padding(.top, 2)
                }
            }

            Spacer(minLength: 8)

            if modelReady {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 18))
                    .foregroundColor(.green)
                    .padding(.top, 1)
            } else if downloading {
                ProgressView()
                    .scaleEffect(0.6)
                    .padding(.top, 1)
            } else if errored {
                Button(action: downloadModel) {
                    Text("Retry")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5)
                        .background(Color.white.opacity(0.15))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
                .padding(.top, 1)
            } else {
                Button(action: downloadModel) {
                    Text("Download")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5)
                        .background(Color.white.opacity(0.15))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
                .padding(.top, 1)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private func formatBytes(_ b: Int64) -> String {
        let gb = Double(b) / 1_073_741_824.0
        return String(format: "%.1f GB", gb)
    }

    // MARK: - Row View

    func setupRow(icon: String, title: String, description: String, done: Bool, required: Bool, loading: Bool, error: String, action: @escaping () -> Void) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 16))
                .foregroundColor(done ? .green : .white.opacity(0.5))
                .frame(width: 24)

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(title)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(.white)
                    if !required {
                        Text("Optional")
                            .font(.system(size: 9, weight: .medium))
                            .foregroundColor(.white.opacity(0.3))
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color.white.opacity(0.08))
                            .clipShape(RoundedRectangle(cornerRadius: 3))
                    }
                }
                Text(description)
                    .font(.system(size: 11))
                    .foregroundColor(.white.opacity(0.35))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                if !error.isEmpty {
                    Text(error)
                        .font(.system(size: 10))
                        .foregroundColor(.red.opacity(0.8))
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            Spacer(minLength: 8)

            if done {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 18))
                    .foregroundColor(.green)
            } else if loading {
                ProgressView()
                    .scaleEffect(0.6)
            } else {
                Button(action: action) {
                    Text("Connect")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5)
                        .background(Color.white.opacity(0.15))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    // MARK: - Actions

    func connectGoogle() {
        gwsLoading = true
        gwsError = ""
        localAPICall("/api/setup/gws-auth", method: "POST", timeoutInterval: 120) { data, err in
            DispatchQueue.main.async {
                gwsLoading = false
                if let err = err {
                    gwsError = err.localizedDescription
                    return
                }
                guard let data = data,
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ok = obj["ok"] as? Bool else {
                    gwsError = "Unexpected response"
                    return
                }
                if ok {
                    gwsEmail = obj["email"] as? String ?? "Connected"
                    if gwsEmail.isEmpty { gwsEmail = "Connected" }
                } else {
                    gwsError = obj["error"] as? String ?? "Auth failed"
                }
            }
        }
    }

    func grantScreenRecording() {
        // The ONLY way Deja gets registered as a Screen Recording client
        // in System Settings is to actually run a screen capture as the
        // host app. Deja's production code path uses `/usr/sbin/screencapture`
        // (via BackendProcessManager.captureScreenshot()) and macOS
        // evaluates the spawning app's TCC for screen recording when
        // that subprocess runs. Calling Apple's CGRequestScreenCaptureAccess
        // and SCShareableContent APIs has not been reliable on direct-launch
        // dev builds — only screencapture has been observed to register the
        // app in the System Settings list every time.
        //
        // So: trigger a real screencapture run first, then open System
        // Settings so the user can flip the toggle. The polling in
        // checkPermissions() picks up the change automatically.
        let home = NSHomeDirectory() + "/.deja"
        let probePath = home + "/screen_recording_probe.png"
        try? FileManager.default.createDirectory(
            atPath: home,
            withIntermediateDirectories: true
        )

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        // -x silent, -D 1 main display, output path
        proc.arguments = ["-x", "-D", "1", probePath]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            // Ignore — even if the spawn fails, macOS records the attempt
        }
        // Clean up the probe file — we don't need it
        try? FileManager.default.removeItem(atPath: probePath)

        // Belt + suspenders: also call the documented APIs in case
        // they help on a future macOS revision
        _ = CGRequestScreenCaptureAccess()

        // Open System Settings → Screen Recording. Deja should now be
        // in the list (added by the screencapture spawn above).
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
    }

    func grantFullDisk() {
        // Use low-level open() syscall on protected files — this is the
        // most reliable way to trigger macOS TCC to add the app to the
        // Full Disk Access list.
        let paths = [
            (NSHomeDirectory() as NSString).appendingPathComponent("Library/Messages/chat.db"),
            (NSHomeDirectory() as NSString).appendingPathComponent("Library/Safari/History.db"),
        ]
        for path in paths {
            let fd = Darwin.open(path, O_RDONLY)
            if fd >= 0 { Darwin.close(fd) }
        }

        // Also try NSFileHandle which goes through a different TCC path
        let chatDB = NSHomeDirectory() + "/Library/Messages/chat.db"
        let _ = try? FileHandle(forReadingFrom: URL(fileURLWithPath: chatDB))

        // Open System Settings to the FDA pane
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") {
            NSWorkspace.shared.open(url)
        }
    }

    func grantAccessibility() {
        // 1) Prompt for Accessibility and register Deja under that pane.
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)

        // 2) Prompt for Input Monitoring and register Deja under that pane.
        // IOHIDRequestAccess is the documented API; it fires the system
        // prompt the first time and registers the app in the Input
        // Monitoring list. It's safe to call repeatedly.
        _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)

        // 3) Open System Settings → Accessibility. Input Monitoring is
        // one click away in the same Privacy sidebar; the 1Hz poll
        // picks up either toggle flipping.
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    func grantMicrophone() {
        // Trigger macOS's standard mic TCC prompt under the main Deja
        // bundle identity. Because DejaRecorder (the helper that actually
        // opens the audio engine) is signed with the same --identifier
        // com.deja.app, the grant here carries over to the helper without
        // a second prompt when the user first clicks Listen.
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            micGranted = true
        case .notDetermined:
            // Async — the system shows the prompt and calls back on an
            // arbitrary queue. Bounce to main before mutating @State.
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async {
                    self.micGranted = granted
                }
            }
        case .denied, .restricted:
            // User previously denied. We can't re-prompt — send them to
            // System Settings so they can flip the toggle.
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
                NSWorkspace.shared.open(url)
            }
        @unknown default:
            break
        }
    }

    func downloadModel() {
        modelStatus = "downloading"
        modelMessage = "Starting download..."
        localAPICall("/api/setup/download-model", method: "POST") { _, _ in }
    }

    func startDeja() {
        pollTimer?.invalidate()
        monitor.completeSetup()
        monitor.startBackfillAndPoll()
        NotificationCenter.default.post(name: .setupCompleted, object: nil)
    }

    // MARK: - Polling

    func checkPermissions() {
        screenRecordingGranted = CGPreflightScreenCaptureAccess()

        // Accessibility + Input Monitoring: both must be granted for
        // push-to-talk and typing-aware capture deferral to work. We
        // treat them as a single row in the UI, so AND the two probes.
        let axOk = AXIsProcessTrusted()
        let imOk = (IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted)
        accessibilityGranted = axOk && imOk

        // Microphone — pure read-only status probe; never triggers the
        // TCC prompt. The prompt only fires from grantMicrophone() when
        // the user explicitly clicks the Microphone row.
        micGranted = (AVCaptureDevice.authorizationStatus(for: .audio) == .authorized)

        // Full Disk Access check: FileManager.isReadableFile is a POSIX
        // check that returns true for any file the user owns — useless
        // for detecting TCC. Even actually open()ing chat.db is not
        // strict enough because macOS grants a NARROWER permission
        // (SystemPolicyAppData) to Messages separately from the big
        // Full Disk Access toggle (SystemPolicyAllFiles).
        //
        // The canonical canary for SystemPolicyAllFiles is
        // ~/Library/Safari/History.db — it's ONLY accessible when the
        // user has granted true Full Disk Access in System Settings.
        // If we can open it, we have FDA.
        let safariHistory = NSHomeDirectory() + "/Library/Safari/History.db"
        let fd = Darwin.open(safariHistory, O_RDONLY)
        if fd >= 0 {
            Darwin.close(fd)
            fullDiskGranted = true
        } else {
            // EPERM / EACCES = TCC denied. ENOENT = user has never
            // opened Safari; fall back to checking chat.db, and if
            // that also doesn't exist, we genuinely can't tell so
            // we leave it as not-granted.
            if errno == ENOENT {
                let chatDB = NSHomeDirectory() + "/Library/Messages/chat.db"
                let fd2 = Darwin.open(chatDB, O_RDONLY)
                if fd2 >= 0 {
                    Darwin.close(fd2)
                    fullDiskGranted = true
                } else {
                    fullDiskGranted = false
                }
            } else {
                fullDiskGranted = false
            }
        }
    }

    // MARK: - Close panel (not the same as Start Déjà)

    private func hidePanel() {
        // IMPORTANT: do NOT kill the polling timer here. The view is
        // hidden but not destroyed, so when the user re-opens the panel
        // via the tray icon, .onAppear does NOT fire (the same view
        // instance is reused). If we invalidate pollTimer, the panel
        // re-opens with stale state and never updates again until the
        // app restarts. Letting the 1Hz poll keep running while the
        // panel is hidden costs nothing and keeps state current.
        for window in NSApplication.shared.windows {
            if window is SetupPanelWindow {
                window.orderOut(nil)
            }
        }
    }

    /// Fully terminate Deja from the setup panel. LSUIElement apps
    /// don't have a system menu bar, so we expose Quit here (and via
    /// the menu bar icon's right-click menu) so users can always
    /// abort during setup.
    private func quitApp() {
        NSApplication.shared.terminate(nil)
    }

    func checkModelStatus() {
        localAPICall("/api/setup/model-status", timeoutInterval: 3) { data, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                self.modelStatus = obj["status"] as? String ?? "idle"
                self.modelProgress = obj["progress"] as? Double ?? 0.0
                self.modelMessage = obj["message"] as? String ?? ""
                self.modelPhase = obj["phase"] as? String ?? "idle"
                if let n = obj["bytes_downloaded"] as? NSNumber {
                    self.modelBytesDownloaded = n.int64Value
                }
                if let n = obj["bytes_total"] as? NSNumber {
                    self.modelBytesTotal = n.int64Value
                }
            }
        }
    }

    func checkExistingAuth() {
        localAPICall("/api/setup/status", timeoutInterval: 3) { data, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                if obj["gws_authenticated"] as? Bool == true {
                    self.gwsEmail = obj["gws_email"] as? String ?? "Connected"
                    if self.gwsEmail.isEmpty { self.gwsEmail = "Connected" }
                }
            }
        }
    }

    func startPolling() {
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            checkPermissions()
            checkModelStatus()
        }
    }
}