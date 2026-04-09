import SwiftUI
import CoreGraphics

struct SetupPanelView: View {
    @ObservedObject var monitor: MonitorState

    @State private var gwsEmail: String = ""
    @State private var gwsLoading: Bool = false
    @State private var gwsError: String = ""
    @State private var screenRecordingGranted: Bool = false
    @State private var fullDiskGranted: Bool = false
    @State private var pollTimer: Timer?

    private var googleDone: Bool { !gwsEmail.isEmpty }
    private var canStart: Bool { googleDone && screenRecordingGranted }

    var body: some View {
        VStack(spacing: 0) {
            // Header
            VStack(spacing: 8) {
                Text("Welcome to Déjà")
                    .font(.system(size: 22, weight: .bold))
                    .foregroundColor(.white)
                Text("Connect your accounts and grant permissions to get started.")
                    .font(.system(size: 13))
                    .foregroundColor(.white.opacity(0.5))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 20)
            }
            .padding(.top, 30)
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
        .frame(width: 420, height: 500)
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
                if !error.isEmpty {
                    Text(error)
                        .font(.system(size: 10))
                        .foregroundColor(.red.opacity(0.8))
                }
            }

            Spacer()

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
        CGRequestScreenCaptureAccess()
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

    func startDeja() {
        pollTimer?.invalidate()
        monitor.completeSetup()
        monitor.startBackfillAndPoll()
        NotificationCenter.default.post(name: .setupCompleted, object: nil)
    }

    // MARK: - Polling

    func checkPermissions() {
        screenRecordingGranted = CGPreflightScreenCaptureAccess()
        fullDiskGranted = FileManager.default.isReadableFile(
            atPath: NSHomeDirectory() + "/Library/Messages/chat.db"
        )
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
        }
    }
}