import SwiftUI
import AppKit

// MARK: - Command Center View
//
// Replaces the old ChatView as the primary popover surface. Three
// stacked regions:
//   1. Status header (top) — green/amber dot + summary stats
//   2. Activity feed (middle, scrollable) — reverse-chron list of
//      things Déjà has done (commands, dedup merges, integrate writes,
//      meetings)
//   3. Command input bar (bottom) — one text field, Enter to submit,
//      dispatches to /api/command
//
// No tabs, no chat history, no tool loops — single-turn by design.

struct CommandCenterView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(spacing: 0) {
            CommandStatusHeader(monitor: monitor)

            Divider().background(Color.white.opacity(0.05))

            CommandActivityFeed(monitor: monitor)

            CommandInputBar(monitor: monitor)
        }
        .background(Color.black)
        .onAppear { monitor.startActivityPolling() }
        .onDisappear { monitor.stopActivityPolling() }
    }
}

// MARK: - Status Header

private struct CommandStatusHeader: View {
    @ObservedObject var monitor: MonitorState

    private var statusLabel: String {
        monitor.running ? "Monitoring" : "Paused"
    }

    private var statusColor: Color {
        monitor.running ? .green : .orange
    }

    private var statsLine: String {
        let sigCount = monitor.signals
        if sigCount > 0 {
            return "\(sigCount) signal\(sigCount == 1 ? "" : "s") collected"
        }
        return "waiting for signals..."
    }

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(statusColor)
                .frame(width: 6, height: 6)
                .shadow(color: statusColor.opacity(0.6), radius: 3)
            Text(statusLabel)
                .font(.system(size: 11, weight: .semibold))
                .foregroundColor(.white.opacity(0.85))
            Text("·")
                .font(.system(size: 10))
                .foregroundColor(.white.opacity(0.25))
            Text(statsLine)
                .font(.system(size: 10))
                .foregroundColor(.white.opacity(0.45))
                .lineLimit(1)
            Spacer()
        }
        .padding(.horizontal, 16)
        .frame(height: 28)
        .background(Color.black)
    }
}

// MARK: - Activity Feed

private struct CommandActivityFeed: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                if monitor.activityEntries.isEmpty {
                    emptyState
                } else {
                    ForEach(monitor.activityEntries) { entry in
                        ActivityRow(entry: entry)
                        Divider().background(Color.white.opacity(0.04))
                    }
                }
            }
            .padding(.vertical, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "sparkles")
                .font(.system(size: 22))
                .foregroundColor(.white.opacity(0.15))
            Text("No activity yet")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.3))
            Text("Déjà will populate this feed as it works")
                .font(.system(size: 10))
                .foregroundColor(.white.opacity(0.2))
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 50)
    }
}

private struct ActivityRow: View {
    let entry: ActivityEntry

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 11))
                .foregroundColor(iconColor)
                .frame(width: 16, alignment: .center)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(title)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundColor(.white.opacity(0.9))
                    Spacer()
                    Text(relativeTime)
                        .font(.system(size: 9))
                        .foregroundColor(.white.opacity(0.25))
                }
                Text(entry.summary)
                    .font(.system(size: 10))
                    .foregroundColor(.white.opacity(0.5))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
    }

    private var icon: String {
        switch entry.kind {
        case "command": return "bolt.fill"
        case "dedup": return "rectangle.2.swap"
        case "cycle": return "arrow.triangle.2.circlepath"
        case "nightly": return "moon.stars.fill"
        case "action": return "checkmark.circle.fill"
        case "goals": return "target"
        case "context": return "info.circle.fill"
        case "meeting": return "mic.fill"
        case "chat": return "text.bubble.fill"
        case "onboard": return "sparkles"
        default: return "circle.fill"
        }
    }

    private var iconColor: Color {
        switch entry.kind {
        case "command": return .yellow
        case "dedup": return .mint
        case "cycle": return .blue
        case "nightly": return .purple
        case "action": return .green
        case "goals": return .orange
        case "context": return .cyan
        case "meeting": return .red
        case "chat": return .white.opacity(0.5)
        case "onboard": return .pink
        default: return .gray
        }
    }

    private var title: String {
        switch entry.kind {
        case "command": return "Command"
        case "dedup": return "Merged pages"
        case "cycle": return "Integrate cycle"
        case "nightly": return "Nightly reflect"
        case "action": return "Action executed"
        case "goals": return "Goals updated"
        case "context": return "Context noted"
        case "meeting": return "Meeting recorded"
        case "chat": return "Dictation"
        case "onboard": return "Onboarding"
        default: return entry.kind.capitalized
        }
    }

    /// Convert "YYYY-MM-DD HH:MM" into a short relative label.
    private var relativeTime: String {
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd HH:mm"
        df.locale = Locale(identifier: "en_US_POSIX")
        guard let date = df.date(from: entry.timestamp) else { return entry.timestamp }
        let seconds = Int(Date().timeIntervalSince(date))
        if seconds < 60 { return "just now" }
        if seconds < 3600 { return "\(seconds / 60) min ago" }
        if seconds < 86_400 { return "\(seconds / 3600)h ago" }
        return "\(seconds / 86_400)d ago"
    }
}

// MARK: - Command Input Bar

private struct CommandInputBar: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(spacing: 0) {
            if let toast = monitor.commandToast {
                HStack(spacing: 8) {
                    Image(systemName: toast.style == .success ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.system(size: 11))
                        .foregroundColor(toast.style == .success ? .green : .red)
                    Text(toast.text)
                        .font(.system(size: 11))
                        .foregroundColor(.white.opacity(0.85))
                        .lineLimit(2)
                    Spacer()
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(
                    toast.style == .success
                        ? Color.green.opacity(0.08)
                        : Color.red.opacity(0.1)
                )
            }

            HStack(spacing: 8) {
                TextField("What should Déjà do?", text: $monitor.commandInput)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12))
                    .foregroundColor(.white)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 9)
                    .background(Color.white.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .onSubmit { monitor.submitCommand() }
                    .disabled(monitor.commandPending)

                Button(action: {
                    NotificationCenter.default.post(
                        name: .voicePillToggled,
                        object: nil,
                        userInfo: ["enabled": true, "activate": true]
                    )
                }) {
                    Image(systemName: "mic.fill")
                        .font(.system(size: 14))
                        .foregroundColor(.white.opacity(0.5))
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(.plain)
                .help("Hold-to-talk (Option key)")

                if monitor.commandPending {
                    ProgressView()
                        .scaleEffect(0.6)
                        .frame(width: 28, height: 28)
                } else {
                    Button(action: { monitor.submitCommand() }) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 22))
                            .foregroundColor(
                                monitor.commandInput.trimmingCharacters(in: .whitespaces).isEmpty
                                    ? .white.opacity(0.15)
                                    : .blue
                            )
                    }
                    .buttonStyle(.plain)
                    .disabled(monitor.commandInput.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .background(Color(white: 0.05))
    }
}
