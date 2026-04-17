import SwiftUI
import AppKit

// MARK: - Command Center View
//
// The body of the expanded notch panel. Two tabs, chat input pinned
// at the bottom:
//
//   Tab 1 — "Now":
//     • Latest observation narrative (one paragraph, pulled from the
//       tail of ``~/Deja/observations/YYYY-MM-DD.md``).
//     • Today's wiki updates — real state changes from audit.jsonl,
//       heartbeats filtered out. Tap a row to open the page in
//       Obsidian (``obsidian://open?vault=Deja&file=…``).
//
//   Tab 2 — "Open loops":
//     • Three grouped lists — Tasks, Waiting for, Reminders — from
//       goals.md. Unchecked items only. Tap a row to jump into the
//       first wiki entity mentioned.
//
// Signal Health (the old heartbeat stream) is still reachable via the
// tray menu's "Signal Health…" item and a small Debug link in the
// Now tab footer. It's no longer the default view.
//
// The chat input ("What should Déjà do?") is hoisted out of the tabs
// and pinned at the bottom of the whole view so it's always available.

struct CommandCenterView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(spacing: 0) {
            CommandStatusHeader(monitor: monitor)

            Divider().background(Color.white.opacity(0.05))

            NotchTabBar(monitor: monitor)

            Divider().background(Color.white.opacity(0.05))

            // Tab content fills the vertical space between the tab
            // bar and the command input bar. Both tabs poll their
            // data on the same 10s cadence — switching tabs is an
            // instant re-render with no network round-trip.
            Group {
                if monitor.notchTab == 0 {
                    NowTabView(monitor: monitor)
                        .transition(.opacity)
                } else {
                    OpenLoopsTabView(monitor: monitor)
                        .transition(.opacity)
                }
            }
            .animation(.easeInOut(duration: 0.15), value: monitor.notchTab)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.black)

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

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(statusColor)
                .frame(width: 6, height: 6)
                .shadow(color: statusColor.opacity(0.6), radius: 3)
            Text(statusLabel)
                .font(.system(size: 11, weight: .semibold))
                .foregroundColor(.white.opacity(0.85))
            Spacer()
        }
        .padding(.horizontal, 16)
        .frame(height: 24)
        .background(Color.black)
    }
}

// MARK: - Tab bar

/// Two-segment tab bar at the top of the panel. Purely visual — state
/// lives in ``MonitorState.notchTab`` so it survives panel open/close.
private struct NotchTabBar: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        HStack(spacing: 0) {
            tabButton(title: "Now", index: 0)
            tabButton(title: "Open loops", index: 1, badge: openLoopsBadge)
            Spacer()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Color.black)
    }

    private var openLoopsBadge: Int {
        monitor.openLoops.counts.tasks
            + monitor.openLoops.counts.waiting
            + monitor.openLoops.counts.reminders
    }

    private func tabButton(title: String, index: Int, badge: Int = 0) -> some View {
        let selected = monitor.notchTab == index
        return Button(action: { monitor.setNotchTab(index) }) {
            HStack(spacing: 5) {
                Text(title)
                    .font(.system(size: 11, weight: selected ? .semibold : .medium))
                    .foregroundColor(selected ? .white : .white.opacity(0.45))
                if badge > 0 {
                    Text("\(badge)")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundColor(.white.opacity(selected ? 0.85 : 0.5))
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(
                            Capsule()
                                .fill(Color.white.opacity(selected ? 0.18 : 0.1))
                        )
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(selected ? Color.white.opacity(0.08) : Color.clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Tab 1: Now

private struct NowTabView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 12) {
                observationsStream
                Spacer(minLength: 6)
                debugFooter
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)
            .padding(.bottom, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // The stream: every recent observation narrative, newest first.
    @ViewBuilder
    private var observationsStream: some View {
        if monitor.recentObservations.isEmpty {
            HStack(spacing: 8) {
                Image(systemName: "sparkles")
                    .font(.system(size: 11))
                    .foregroundColor(.white.opacity(0.3))
                Text("Déjà is watching — nothing worth narrating yet today.")
                    .font(.system(size: 11))
                    .foregroundColor(.white.opacity(0.4))
            }
            .padding(.vertical, 6)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(monitor.recentObservations) { entry in
                    ObservationCard(entry: entry)
                }
            }
        }
    }

    // Footer: a tiny, non-shouty link into the old heartbeat panel so
    // power users can still see collector health when they want to.
    private var debugFooter: some View {
        HStack {
            Spacer()
            Button(action: {
                (NSApp.delegate as? AppDelegate)?.showSignalHealth()
            }) {
                HStack(spacing: 4) {
                    Image(systemName: "waveform.path.ecg")
                        .font(.system(size: 9))
                    Text("Signal health")
                        .font(.system(size: 10))
                }
                .foregroundColor(.white.opacity(0.3))
            }
            .buttonStyle(.plain)
            .help("Show the per-collector heartbeat panel (debug view)")
        }
    }
}

/// One observation narrative entry, rendered as a card: a small timestamp
/// strip above a selectable body paragraph. Stacked newest-first in the
/// Now tab.
private struct ObservationCard: View {
    let entry: LatestObservation

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Circle()
                    .fill(Color.green.opacity(0.7))
                    .frame(width: 5, height: 5)
                Text(entry.time)
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .foregroundColor(.white.opacity(0.55))
                if !isToday(entry.date) {
                    Text("· \(shortDate(entry.date))")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.35))
                }
                Spacer()
            }
            Text(entry.text)
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.85))
                .lineLimit(nil)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.white.opacity(0.04))
        )
    }

    private func isToday(_ iso: String) -> Bool {
        let today = ISO8601DateFormatter().string(from: Date()).prefix(10)
        return String(today) == iso
    }

    private func shortDate(_ iso: String) -> String {
        // iso is "YYYY-MM-DD"; show "Apr 15" style for yesterday/earlier.
        let parts = iso.split(separator: "-")
        guard parts.count == 3 else { return iso }
        let months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        if let m = Int(parts[1]), m >= 1, m <= 12 {
            return "\(months[m-1]) \(parts[2])"
        }
        return iso
    }
}

/// One row in the "Today's wiki updates" list. Tapping a linkable row
/// hands off to Obsidian. Non-linkable rows (``startup/…``, ``cycle/…``)
/// render as plain text with no tap target.
private struct WikiUpdateRow: View {
    let update: WikiUpdate
    @ObservedObject var monitor: MonitorState

    var body: some View {
        Button(action: {
            guard update.linkable else { return }
            monitor.openInObsidian(slug: update.slug)
        }) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: actionIcon)
                    .font(.system(size: 10))
                    .foregroundColor(actionColor)
                    .frame(width: 14, alignment: .center)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(actionLabel)
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundColor(actionColor.opacity(0.95))
                        Text(update.display)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundColor(.white.opacity(0.9))
                            .lineLimit(1)
                        Spacer()
                        Text(relativeTime)
                            .font(.system(size: 9))
                            .foregroundColor(.white.opacity(0.25))
                    }
                    if !update.reason.isEmpty {
                        Text(update.reason)
                            .font(.system(size: 10))
                            .foregroundColor(.white.opacity(0.5))
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(update.linkable ? "Open \(update.display) in Obsidian" : update.target)
        .opacity(update.linkable ? 1.0 : 0.75)
    }

    private var actionIcon: String {
        switch update.action {
        case "wiki_write":       return "doc.text.fill"
        case "wiki_create":      return "plus.square.fill"
        case "event_create":     return "calendar.badge.plus"
        case "reminder_create":  return "bell.badge.fill"
        case "reminder_resolve": return "checkmark.circle.fill"
        case "task_add":         return "plus.circle.fill"
        case "task_resolve":     return "checkmark.square.fill"
        case "dedup_merge":      return "rectangle.2.swap"
        case "nightly_reflect":  return "moon.stars.fill"
        case "command":          return "bolt.fill"
        case "chat":             return "text.bubble.fill"
        case "onboard":          return "sparkles"
        default:                 return "circle.fill"
        }
    }

    private var actionColor: Color {
        switch update.action {
        case "wiki_write":       return .blue
        case "wiki_create":      return .green
        case "event_create":     return .orange
        case "reminder_create":  return .yellow
        case "reminder_resolve": return .green
        case "task_add":         return .mint
        case "task_resolve":     return .green
        case "dedup_merge":      return .teal
        case "nightly_reflect":  return .purple
        case "command":          return .yellow
        case "chat":             return .white.opacity(0.5)
        case "onboard":          return .pink
        default:                 return .gray
        }
    }

    private var actionLabel: String {
        switch update.action {
        case "wiki_write":       return "Updated"
        case "wiki_create":      return "Created"
        case "event_create":     return "New event"
        case "reminder_create":  return "Reminder"
        case "reminder_resolve": return "Resolved"
        case "task_add":         return "Task"
        case "task_resolve":     return "Done"
        case "dedup_merge":      return "Merged"
        case "nightly_reflect":  return "Reflected"
        case "command":          return "Command"
        case "chat":             return "Chat"
        case "onboard":          return "Onboarding"
        default:                 return update.action.capitalized
        }
    }

    /// Convert the backend's "YYYY-MM-DD HH:MM" stamp into a short
    /// relative label. Falls back to the raw string on parse failure
    /// so we never silently hide a row behind an empty time column.
    private var relativeTime: String {
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd HH:mm"
        df.timeZone = TimeZone(identifier: "UTC")
        df.locale = Locale(identifier: "en_US_POSIX")
        guard let date = df.date(from: update.timestamp) else { return update.timestamp }
        let seconds = Int(Date().timeIntervalSince(date))
        if seconds < 0 { return "just now" }
        if seconds < 60 { return "just now" }
        if seconds < 3600 { return "\(seconds / 60)m ago" }
        if seconds < 86_400 { return "\(seconds / 3600)h ago" }
        return "\(seconds / 86_400)d ago"
    }
}

// MARK: - Tab 2: Open loops

private struct OpenLoopsTabView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 14) {
                if monitor.openLoops.isEmpty {
                    emptyState
                } else {
                    section(
                        title: "Tasks",
                        icon: "checkmark.circle",
                        color: .blue,
                        count: monitor.openLoops.counts.tasks,
                        items: monitor.openLoops.tasks.map { .init(text: $0.text, slug: $0.slug, accent: "") }
                    )
                    section(
                        title: "Waiting for",
                        icon: "hourglass",
                        color: .cyan,
                        count: monitor.openLoops.counts.waiting,
                        items: monitor.openLoops.waiting.map { .init(text: $0.text, slug: $0.slug, accent: "") }
                    )
                    reminderSection
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)
            .padding(.bottom, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var emptyState: some View {
        VStack(alignment: .center, spacing: 8) {
            Image(systemName: "checkmark.seal")
                .font(.system(size: 22))
                .foregroundColor(.white.opacity(0.2))
            Text("No open loops.")
                .font(.system(size: 12))
                .foregroundColor(.white.opacity(0.4))
            Text("Tasks, waiting-fors, and reminders from goals.md will appear here.")
                .font(.system(size: 10))
                .foregroundColor(.white.opacity(0.3))
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 40)
    }

    private struct ItemRow {
        let text: String
        let slug: String
        let accent: String
    }

    private func section(
        title: String,
        icon: String,
        color: Color,
        count: Int,
        items: [ItemRow]
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 9))
                    .foregroundColor(color)
                Text(title.uppercased())
                    .font(.system(size: 9, weight: .semibold))
                    .tracking(0.8)
                    .foregroundColor(.white.opacity(0.55))
                Spacer()
                Text("\(count)")
                    .font(.system(size: 9))
                    .foregroundColor(.white.opacity(0.3))
            }
            if items.isEmpty {
                Text("Nothing open.")
                    .font(.system(size: 10))
                    .foregroundColor(.white.opacity(0.3))
                    .padding(.leading, 14)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                        OpenLoopRow(
                            text: item.text,
                            slug: item.slug,
                            accent: item.accent,
                            accentColor: color,
                            monitor: monitor
                        )
                    }
                }
            }
        }
    }

    private var reminderSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 5) {
                Image(systemName: "bell")
                    .font(.system(size: 9))
                    .foregroundColor(.yellow)
                Text("REMINDERS")
                    .font(.system(size: 9, weight: .semibold))
                    .tracking(0.8)
                    .foregroundColor(.white.opacity(0.55))
                Spacer()
                Text("\(monitor.openLoops.counts.reminders)")
                    .font(.system(size: 9))
                    .foregroundColor(.white.opacity(0.3))
            }
            if monitor.openLoops.reminders.isEmpty {
                Text("Nothing scheduled.")
                    .font(.system(size: 10))
                    .foregroundColor(.white.opacity(0.3))
                    .padding(.leading, 14)
            } else {
                VStack(spacing: 0) {
                    ForEach(monitor.openLoops.reminders) { r in
                        OpenLoopRow(
                            text: r.text,
                            slug: r.slug,
                            accent: r.date,
                            accentColor: .yellow,
                            monitor: monitor
                        )
                    }
                }
            }
        }
    }
}

/// One row in the Open loops lists. Tapping opens the first wiki
/// entity mentioned (stored in ``slug``) in Obsidian. Rows without a
/// slug render as plain text with no tap target.
private struct OpenLoopRow: View {
    let text: String
    let slug: String
    let accent: String
    let accentColor: Color
    @ObservedObject var monitor: MonitorState

    var body: some View {
        let linkable = !slug.isEmpty
        return Button(action: {
            guard linkable else { return }
            monitor.openInObsidian(slug: resolvedSlug)
        }) {
            HStack(alignment: .top, spacing: 8) {
                Text("•")
                    .font(.system(size: 10))
                    .foregroundColor(.white.opacity(0.3))
                Text(displayText(text))
                    .font(.system(size: 11))
                    .foregroundColor(.white.opacity(0.8))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !accent.isEmpty {
                    Text(accent)
                        .font(.system(size: 9, weight: .medium))
                        .foregroundColor(accentColor.opacity(0.8))
                        .monospacedDigit()
                }
            }
            .padding(.vertical, 4)
            .padding(.horizontal, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(linkable ? "Open \(resolvedSlug) in Obsidian" : text)
        .opacity(linkable ? 1.0 : 0.8)
    }

    /// The slug we hand to Obsidian. The backend already picks the
    /// first wikilink; we accept it as-is. If it lacks a folder
    /// prefix (e.g., ``matt-brock``), Obsidian's default search-by-
    /// name still finds it in the vault.
    private var resolvedSlug: String { slug }

    /// Render ``[[slug|label]]`` as ``label`` (or ``slug`` if no label),
    /// and strip ``(added YYYY-MM-DD)`` suffixes — those are parser
    /// metadata, not user-facing text.
    private func displayText(_ raw: String) -> String {
        var s = raw
        // Strip "(added YYYY-MM-DD)" annotations. They're useful for
        // the briefing algorithm but noisy in a compact list view.
        if let range = s.range(of: #"\s*\(added \d{4}-\d{2}-\d{2}\)"#, options: .regularExpression) {
            s.removeSubrange(range)
        }
        // Convert [[slug|Label]] → Label and [[slug]] → slug.
        let pattern = #"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return s }
        let range = NSRange(s.startIndex..., in: s)
        let matches = regex.matches(in: s, range: range).reversed()
        var result = s
        for m in matches {
            guard let full = Range(m.range, in: result) else { continue }
            let slug = Range(m.range(at: 1), in: result).map { String(result[$0]) } ?? ""
            let label = m.range(at: 2).location != NSNotFound
                ? Range(m.range(at: 2), in: result).map { String(result[$0]) } ?? slug
                : slug
            result.replaceSubrange(full, with: label)
        }
        return result
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
