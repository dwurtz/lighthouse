import SwiftUI

// MARK: - Briefing View
//
// Compact "right now" panel shown at the top of CommandCenterView.
// Reads `MonitorState.briefing` (fetched from GET /api/briefing on
// the same 10s cadence as the activity feed) and renders four
// sections, each hidden when empty:
//
//   • Due reminders     — questions the agent scheduled for itself
//   • Overdue tasks     — user tasks whose deadline has passed
//   • Upcoming (3d)     — tasks due in the next few days
//   • Stale waiting for — items added 7–21 days ago (ping territory)
//
// When everything is empty the view collapses entirely — users who
// don't yet have anything in goals.md see nothing (not a dead panel).
// No LLM, no async state, pure data binding.

struct BriefingView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        if monitor.briefing.hasAnything {
            VStack(alignment: .leading, spacing: 6) {
                header

                if !monitor.briefing.due_reminders.isEmpty {
                    section(
                        title: "Due reminders",
                        icon: "bell.badge.fill",
                        color: .yellow,
                        rows: monitor.briefing.due_reminders.map { r in
                            BriefingRow(
                                id: r.id,
                                text: r.question,
                                accent: r.date,
                                topics: r.topics
                            )
                        }
                    )
                }

                if !monitor.briefing.overdue_tasks.isEmpty {
                    section(
                        title: "Overdue",
                        icon: "exclamationmark.triangle.fill",
                        color: .red,
                        rows: monitor.briefing.overdue_tasks.map { t in
                            BriefingRow(
                                id: t.id,
                                text: t.text,
                                accent: "\(t.days_overdue)d late",
                                topics: []
                            )
                        }
                    )
                }

                if !monitor.briefing.upcoming_tasks.isEmpty {
                    section(
                        title: "Due soon",
                        icon: "clock.fill",
                        color: .orange,
                        rows: monitor.briefing.upcoming_tasks.map { t in
                            BriefingRow(
                                id: t.id,
                                text: t.text,
                                accent: t.days_until == 0 ? "today" : (t.days_until == 1 ? "tomorrow" : "in \(t.days_until)d"),
                                topics: []
                            )
                        }
                    )
                }

                if !monitor.briefing.stale_waiting.isEmpty {
                    section(
                        title: "Waiting (stale)",
                        icon: "hourglass",
                        color: .cyan,
                        rows: monitor.briefing.stale_waiting.map { w in
                            BriefingRow(
                                id: w.id,
                                text: w.text,
                                accent: "\(w.days_stale)d",
                                topics: []
                            )
                        }
                    )
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Color.white.opacity(0.02))
            .overlay(
                Rectangle()
                    .frame(height: 1)
                    .foregroundColor(Color.white.opacity(0.06)),
                alignment: .bottom
            )
        }
    }

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "sparkles")
                .font(.system(size: 9))
                .foregroundColor(.white.opacity(0.4))
            Text("RIGHT NOW")
                .font(.system(size: 9, weight: .semibold))
                .tracking(1.0)
                .foregroundColor(.white.opacity(0.4))
            Spacer()
            Text(summaryCounts)
                .font(.system(size: 9))
                .foregroundColor(.white.opacity(0.35))
        }
        .padding(.bottom, 2)
    }

    private var summaryCounts: String {
        let c = monitor.briefing.counts
        var parts: [String] = []
        if c.tasks_open > 0 { parts.append("\(c.tasks_open) task\(c.tasks_open == 1 ? "" : "s")") }
        if c.waiting_open > 0 { parts.append("\(c.waiting_open) waiting") }
        if c.reminders_total > 0 { parts.append("\(c.reminders_total) reminder\(c.reminders_total == 1 ? "" : "s")") }
        return parts.joined(separator: " · ")
    }

    private func section(title: String, icon: String, color: Color, rows: [BriefingRow]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 9))
                    .foregroundColor(color)
                Text(title)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(.white.opacity(0.75))
            }
            ForEach(rows.prefix(4)) { row in
                HStack(alignment: .top, spacing: 6) {
                    Text("•")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.3))
                    Text(row.text)
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.75))
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    Text(row.accent)
                        .font(.system(size: 9, weight: .medium))
                        .foregroundColor(color.opacity(0.8))
                        .monospacedDigit()
                }
                .padding(.leading, 2)
            }
            if rows.count > 4 {
                Text("+ \(rows.count - 4) more")
                    .font(.system(size: 9))
                    .foregroundColor(.white.opacity(0.25))
                    .padding(.leading, 8)
            }
        }
    }
}

private struct BriefingRow: Identifiable {
    let id: String
    let text: String
    let accent: String
    let topics: [String]
}
