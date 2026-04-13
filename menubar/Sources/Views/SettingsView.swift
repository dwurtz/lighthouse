import SwiftUI
import AppKit
import CoreGraphics
import ServiceManagement
import Sparkle

// MARK: - Settings View

struct SettingsView: View {
    @ObservedObject var monitor: MonitorState

    @State private var diagnosticStatus: String = ""
    @State private var diagnosticUploading: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Settings")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            Divider().background(Color.white.opacity(0.1))

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    // General
                    VStack(alignment: .leading, spacing: 16) {
                        Text("General")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white.opacity(0.4))
                            .textCase(.uppercase)

                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Launch Déjà at login")
                                    .font(.system(size: 13))
                                    .foregroundColor(.white)
                                Text("Automatically start when you log in to your Mac")
                                    .font(.system(size: 11))
                                    .foregroundColor(.white.opacity(0.35))
                            }
                            Spacer()
                            Toggle("", isOn: Binding(
                                get: { monitor.launchAtLogin },
                                set: { monitor.setLaunchAtLogin($0) }
                            ))
                            .labelsHidden()
                            .toggleStyle(SwitchToggleStyle(tint: .orange))
                            .frame(width: 78, alignment: .trailing)
                        }

                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Voice pill")
                                    .font(.system(size: 13))
                                    .foregroundColor(.white)
                                Text("Show floating dictation pill at the bottom of your screen")
                                    .font(.system(size: 11))
                                    .foregroundColor(.white.opacity(0.35))
                            }
                            Spacer()
                            Toggle("", isOn: Binding(
                                get: { monitor.voicePillEnabled },
                                set: { monitor.setVoicePillEnabled($0) }
                            ))
                            .labelsHidden()
                            .toggleStyle(SwitchToggleStyle(tint: .orange))
                            .frame(width: 78, alignment: .trailing)
                        }

                        Button(action: {
                            (NSApp.delegate as? AppDelegate)?.updaterController.checkForUpdates(nil)
                        }) {
                            HStack(spacing: 6) {
                                Image(systemName: "arrow.triangle.2.circlepath")
                                    .font(.system(size: 12))
                                Text("Check for Updates…")
                                    .font(.system(size: 13))
                            }
                            .foregroundColor(.white.opacity(0.7))
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(16)

                    Divider().background(Color.white.opacity(0.1))

                    // Permissions
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Permissions")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white.opacity(0.4))
                            .textCase(.uppercase)

                        permissionRow(
                            icon: "rectangle.dashed.badge.record",
                            title: "Screen Recording",
                            description: "See what apps and documents are active",
                            granted: monitor.hasScreenRecording,
                            settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
                        )

                        permissionRow(
                            icon: "bubble.left.and.bubble.right",
                            title: "iMessage & WhatsApp",
                            description: "Connect your messages to build people wiki pages",
                            granted: monitor.hasFullDiskAccess,
                            settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
                        )

                        permissionRow(
                            icon: "accessibility",
                            title: "Accessibility",
                            description: "Read window titles and global hotkeys",
                            granted: monitor.hasAccessibility,
                            settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
                        )

                        permissionRow(
                            icon: "mic",
                            title: "Microphone",
                            description: "Dictate commands and record meeting transcripts",
                            granted: monitor.hasMicrophone,
                            settingsURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
                        )
                    }
                    .padding(16)

                    Divider().background(Color.white.opacity(0.1))

                    // Wiki & Monitor
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Wiki")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white.opacity(0.4))
                            .textCase(.uppercase)

                        Button(action: {
                            let wikiPath = NSHomeDirectory() + "/Deja"
                            NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: wikiPath)
                        }) {
                            HStack(spacing: 6) {
                                Image(systemName: "folder")
                                    .font(.system(size: 12))
                                Text("Open Wiki in Finder")
                                    .font(.system(size: 13))
                            }
                            .foregroundColor(.white.opacity(0.7))
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(16)

                    Divider().background(Color.white.opacity(0.1))

                    // Connected AI Assistants (MCP clients)
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Connected AI Assistants")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white.opacity(0.4))
                            .textCase(.uppercase)

                        if monitor.mcpClients.isEmpty {
                            Text("Scanning for installed AI clients…")
                                .font(.system(size: 11))
                                .foregroundColor(.white.opacity(0.35))
                        } else {
                            let installedClients = monitor.mcpClients.filter { $0.installed }
                            if installedClients.isEmpty {
                                Text("No compatible AI assistants detected on this Mac")
                                    .font(.system(size: 11))
                                    .foregroundColor(.white.opacity(0.35))
                            } else {
                                ForEach(installedClients) { client in
                                    mcpClientRow(client)
                                }
                            }
                        }
                    }
                    .padding(16)

                    Divider().background(Color.white.opacity(0.1))

                    // Support
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Support")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white.opacity(0.4))
                            .textCase(.uppercase)

                        Button(action: { uploadDiagnostics() }) {
                            HStack(spacing: 6) {
                                Image(systemName: diagnosticUploading ? "arrow.up.circle" : "square.and.arrow.up")
                                    .font(.system(size: 12))
                                Text(diagnosticUploading ? "Uploading diagnostics…" : "Upload Diagnostics to Déjà Support")
                                    .font(.system(size: 13))
                            }
                            .foregroundColor(.white.opacity(0.7))
                        }
                        .buttonStyle(.plain)
                        .disabled(diagnosticUploading)

                        if !diagnosticStatus.isEmpty {
                            Text(diagnosticStatus)
                                .font(.system(size: 11))
                                .foregroundColor(.white.opacity(0.5))
                                .textSelection(.enabled)
                        }
                    }
                    .padding(16)

                    Divider().background(Color.white.opacity(0.1))

                    // Account
                    VStack(alignment: .leading, spacing: 16) {
                        Text("Account")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white.opacity(0.4))
                            .textCase(.uppercase)

                        Button(action: {
                            DispatchQueue.global(qos: .userInitiated).async {
                                let task = Process()
                                task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                                task.arguments = ["gws", "auth", "revoke"]
                                try? task.run()
                                task.waitUntilExit()
                                DispatchQueue.main.async {
                                    NSApplication.shared.terminate(nil)
                                }
                            }
                        }) {
                            HStack(spacing: 6) {
                                Image(systemName: "rectangle.portrait.and.arrow.right")
                                    .font(.system(size: 12))
                                Text("Sign out")
                                    .font(.system(size: 13))
                            }
                            .foregroundColor(.red.opacity(0.8))
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(16)
                }
            }
        }
        .background(Color.black)
        .onAppear {
            monitor.fetchMCPClients()
        }
    }

    // MARK: - MCP Client Row

    private func mcpIcon(_ name: String) -> String {
        switch name {
        case "Claude Desktop": return "app.badge"
        case "Claude Code": return "terminal"
        case "Cursor": return "cursorarrow.rays"
        case "Windsurf": return "wind"
        case "VS Code": return "chevron.left.forwardslash.chevron.right"
        case "ChatGPT": return "bubble.left.and.text.bubble.right"
        default: return "app"
        }
    }

    private func mcpSubtitle(_ client: MCPClientInfo) -> String {
        if !client.auto_configurable {
            return client.note.isEmpty
                ? "Manual setup — not auto-configurable"
                : client.note
        }
        if !client.installed { return "Not installed" }
        return client.enabled ? "Enabled" : "Disabled"
    }

    @ViewBuilder
    func mcpClientRow(_ client: MCPClientInfo) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                Image(systemName: mcpIcon(client.name))
                    .font(.system(size: 14))
                    .foregroundColor(
                        client.enabled && client.installed
                            ? .orange
                            : .white.opacity(0.45)
                    )
                    .frame(width: 20)

                VStack(alignment: .leading, spacing: 2) {
                    Text(client.name)
                        .font(.system(size: 13))
                        .foregroundColor(.white)
                    Text(mcpSubtitle(client))
                        .font(.system(size: 11))
                        .foregroundColor(.white.opacity(0.35))
                }

                Spacer()

                Toggle("", isOn: Binding(
                    get: { client.enabled },
                    set: { newValue in
                        monitor.setMCPClientEnabled(client.name, enabled: newValue)
                    }
                ))
                .labelsHidden()
                .toggleStyle(SwitchToggleStyle(tint: .orange))
                .disabled(!client.installed || !client.auto_configurable)
                .frame(width: 78, alignment: .trailing)
            }

            if let err = monitor.mcpClientErrors[client.name] {
                Text(err)
                    .font(.system(size: 10))
                    .foregroundColor(.red.opacity(0.85))
                    .padding(.leading, 30)
            }
        }
    }

    // MARK: - Upload Diagnostics

    func uploadDiagnostics() {
        diagnosticUploading = true
        diagnosticStatus = ""

        let payload = try? JSONSerialization.data(withJSONObject: ["note": ""])
        localAPICall("/api/diagnostics/upload", method: "POST", body: payload, timeoutInterval: 60) { data, err in
            DispatchQueue.main.async {
                diagnosticUploading = false
                if let err = err {
                    diagnosticStatus = "Upload failed: \(err.localizedDescription)"
                    return
                }
                guard let data = data,
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else {
                    diagnosticStatus = "Upload failed: invalid response"
                    return
                }
                if let id = obj["id"] as? String {
                    diagnosticStatus = "Uploaded. Reference ID: \(id)"
                } else if let e = obj["error"] as? String {
                    diagnosticStatus = "Upload failed: \(e)"
                } else {
                    diagnosticStatus = "Upload failed: unknown response"
                }
            }
        }
    }

    // MARK: - Permission Row

    func permissionRow(icon: String, title: String, description: String, granted: Bool, settingsURL: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 14))
                .foregroundColor(granted ? .green : .orange)
                .frame(width: 20)

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 13))
                    .foregroundColor(.white)
                Text(description)
                    .font(.system(size: 11))
                    .foregroundColor(.white.opacity(0.35))
            }

            Spacer()

            // Every row has the same right-side affordance: a button
            // that opens System Settings for this permission. Wrapped
            // in a fixed-width right-aligned frame that matches the
            // macOS toggle switch footprint used in the General and
            // Connected AI Assistants sections, so all three columns
            // of trailing controls align to the same right edge.
            HStack {
                Spacer()
                Button(action: {
                    if let url = URL(string: settingsURL) {
                        NSWorkspace.shared.open(url)
                    }
                }) {
                    HStack(spacing: 4) {
                        if granted {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.system(size: 11))
                                .foregroundColor(.green)
                        }
                        Text(granted ? "Manage" : "Grant")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundColor(.white.opacity(granted ? 0.75 : 1.0))
                    }
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Color.white.opacity(granted ? 0.06 : 0.15))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6)
                            .stroke(Color.white.opacity(granted ? 0.10 : 0.0), lineWidth: 1)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
            }
            .frame(width: 78, alignment: .trailing)
        }
    }
}
