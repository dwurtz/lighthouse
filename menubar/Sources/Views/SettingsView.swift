import SwiftUI
import AppKit
import CoreGraphics
import ServiceManagement
import Sparkle

// MARK: - Settings View

struct SettingsView: View {
    @ObservedObject var monitor: MonitorState

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

                        Toggle(isOn: Binding(
                            get: { monitor.launchAtLogin },
                            set: { monitor.setLaunchAtLogin($0) }
                        )) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Launch Déjà at login")
                                    .font(.system(size: 13))
                                    .foregroundColor(.white)
                                Text("Automatically start when you log in to your Mac")
                                    .font(.system(size: 11))
                                    .foregroundColor(.white.opacity(0.35))
                            }
                        }
                        .toggleStyle(SwitchToggleStyle(tint: .orange))

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

                        Button(action: {
                            monitor.restart()
                        }) {
                            HStack(spacing: 6) {
                                Image(systemName: "arrow.clockwise")
                                    .font(.system(size: 12))
                                Text("Restart Monitor")
                                    .font(.system(size: 13))
                            }
                            .foregroundColor(.white.opacity(0.7))
                        }
                        .buttonStyle(.plain)
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

            if granted {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 14))
                    .foregroundColor(.green)
            } else {
                Button(action: {
                    if let url = URL(string: settingsURL) {
                        NSWorkspace.shared.open(url)
                    }
                }) {
                    Text("Grant")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .background(Color.white.opacity(0.15))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
            }
        }
    }
}
