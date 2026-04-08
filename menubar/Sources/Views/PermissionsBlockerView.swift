import SwiftUI
import AppKit
import CoreGraphics

// MARK: - Permissions Blocker

struct PermissionsBlockerView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Déjà")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            Divider().background(Color.white.opacity(0.1))

            VStack(alignment: .leading, spacing: 16) {
                Text("Permissions needed")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundColor(.white)
                Text("Déjà needs these permissions to work. Grant each one below.")
                    .font(.system(size: 12))
                    .foregroundColor(.white.opacity(0.5))

                if !monitor.hasScreenRecording {
                    permissionBlockerRow(
                        icon: "rectangle.dashed.badge.record",
                        title: "Screen Recording",
                        description: "See what's on your screen to build context",
                        action: {
                            CGRequestScreenCaptureAccess()
                        }
                    )
                }

                Button(action: { monitor.checkRuntimePermissions() }) {
                    HStack(spacing: 5) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 10))
                        Text("Check again")
                            .font(.system(size: 12))
                    }
                    .foregroundColor(.white.opacity(0.4))
                }
                .buttonStyle(.plain)
            }
            .padding(20)

            Spacer()
        }
    }

    func permissionBlockerRow(icon: String, title: String, description: String, action: @escaping () -> Void) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 16))
                .foregroundColor(.orange)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(.white)
                Text(description)
                    .font(.system(size: 11))
                    .foregroundColor(.white.opacity(0.35))
            }
            Spacer()
            Button(action: action) {
                Text("Grant")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(.orange)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 5)
                    .background(Color.orange.opacity(0.12))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.orange.opacity(0.2), lineWidth: 1)
        )
    }
}
