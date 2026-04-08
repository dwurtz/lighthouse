import SwiftUI
import ServiceManagement

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

            VStack(alignment: .leading, spacing: 16) {
                // General section
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
            }
            .padding(16)

            Divider().background(Color.white.opacity(0.1))

            VStack(alignment: .leading, spacing: 16) {
                Text("Account")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(.white.opacity(0.4))
                    .textCase(.uppercase)

                Button(action: {
                    // Revoke gws auth and clear local tokens
                    let task = Process()
                    task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                    task.arguments = ["gws", "auth", "revoke"]
                    try? task.run()
                    task.waitUntilExit()

                    // Quit the app so user can re-authenticate on next launch
                    NSApplication.shared.terminate(nil)
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

            Spacer()
        }
        .background(Color.black)
    }
}
