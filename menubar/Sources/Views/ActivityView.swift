import SwiftUI

// MARK: - Activity Tab

struct ActivityView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Live signal pulse — proves the monitor is picking up activity in real time
            TimelineView(.periodic(from: .now, by: 1)) { _ in
                HStack(spacing: 8) {
                    Circle()
                        .fill(monitor.lastSignalISO.isEmpty ? Color.gray : Color.green)
                        .frame(width: 6, height: 6)
                        .shadow(color: .green.opacity(0.6), radius: monitor.lastSignalISO.isEmpty ? 0 : 3)
                    Text(monitor.lastSignalISO.isEmpty ? "no signals yet" :
                         "\(formatTimestamp(monitor.lastSignalISO, relative: true)) · \(monitor.lastSignalSource)")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundColor(.white.opacity(0.7))
                    if !monitor.lastSignalPreview.isEmpty {
                        Text("— \(monitor.lastSignalPreview)")
                            .font(.system(size: 10))
                            .foregroundColor(.white.opacity(0.35))
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                    Spacer()
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(Color.green.opacity(0.06))
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .stroke(Color.green.opacity(0.15), lineWidth: 1)
                )
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }

            // Live signals — only messages and conversations, not emails/screenshots
            let importantSignals = monitor.diverseSignals.filter { s in
                s.source == "imessage" || s.source == "whatsapp" || s.source == "calendar"
                || (s.source == "email" && !s.text.lowercased().contains("unsubscribe")
                    && !s.text.lowercased().contains("no-reply")
                    && !s.text.lowercased().contains("noreply"))
            }
            if !importantSignals.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("RECENT")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(.green.opacity(0.5))
                    ForEach(importantSignals.prefix(5), id: \.id) { signal in
                        HStack(alignment: .top, spacing: 6) {
                            Text(signal.source)
                                .font(.system(size: 8, weight: .bold, design: .monospaced))
                                .foregroundColor(signal.sourceColor)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(signal.sourceColor.opacity(0.15))
                                .clipShape(Capsule())
                            Text(signal.text)
                                .font(.system(size: 10))
                                .foregroundColor(.white.opacity(0.4))
                                .lineLimit(1)
                        }
                    }
                }
                .padding(8)
                .background(Color.white.opacity(0.02))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }

            if monitor.insights.isEmpty {
                VStack(spacing: 8) {
                    Text("d").font(.system(size: 24, weight: .bold, design: .serif))
                        .font(.system(size: 24))
                        .foregroundColor(.white.opacity(0.15))
                    Text("No analysis yet")
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.3))
                    Text("The agent thinks every 5 minutes")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.2))
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 30)
            } else {
                // Only show insights that have meaningful content (matches, conversations, or high-value facts)
                let meaningfulInsights = monitor.insights.filter { i in
                    !i.matches.isEmpty || !i.conversations.isEmpty ||
                    i.facts.contains(where: { !$0.lowercased().contains("is using") && !$0.lowercased().contains("was viewing") })
                }
                ForEach(meaningfulInsights, id: \.id) { insight in
                    VStack(alignment: .leading, spacing: 6) {
                        // Timestamp
                        Text(insight.time)
                            .font(.system(size: 9, weight: .medium, design: .monospaced))
                            .foregroundColor(.white.opacity(0.3))

                        // Matches — what the agent noticed
                        ForEach(insight.matches, id: \.self) { match in
                            HStack(alignment: .top, spacing: 6) {
                                Circle()
                                    .fill(.green)
                                    .frame(width: 5, height: 5)
                                    .padding(.top, 4)
                                Text(match)
                                    .font(.system(size: 11))
                                    .foregroundColor(.white.opacity(0.8))
                            }
                        }

                        // New facts extracted
                        ForEach(insight.facts, id: \.self) { fact in
                            HStack(alignment: .top, spacing: 6) {
                                Text("d").font(.system(size: 24, weight: .bold, design: .serif))
                                    .font(.system(size: 8))
                                    .foregroundColor(.purple)
                                    .padding(.top, 3)
                                Text(fact)
                                    .font(.system(size: 11))
                                    .foregroundColor(.purple.opacity(0.8))
                            }
                        }

                        // Commitments detected
                        ForEach(insight.commitments, id: \.self) { commitment in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "checkmark.circle")
                                    .font(.system(size: 9))
                                    .foregroundColor(.orange)
                                    .padding(.top, 2)
                                Text(commitment)
                                    .font(.system(size: 11))
                                    .foregroundColor(.orange.opacity(0.8))
                            }
                        }

                        // Proposed goals
                        ForEach(insight.proposals, id: \.self) { proposal in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "lightbulb.fill")
                                    .font(.system(size: 9))
                                    .foregroundColor(.yellow)
                                    .padding(.top, 2)
                                Text(proposal)
                                    .font(.system(size: 11))
                                    .foregroundColor(.yellow.opacity(0.8))
                            }
                        }

                        // Conversations detected
                        ForEach(insight.conversations, id: \.self) { convo in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "message.fill")
                                    .font(.system(size: 9))
                                    .foregroundColor(.cyan)
                                    .padding(.top, 2)
                                Text(convo)
                                    .font(.system(size: 11))
                                    .foregroundColor(.cyan.opacity(0.8))
                            }
                        }

                        // If nothing happened
                        if insight.matches.isEmpty && insight.facts.isEmpty && insight.commitments.isEmpty && insight.proposals.isEmpty && insight.conversations.isEmpty {
                            Text("Observed \(insight.signalCount) signals — nothing noteworthy")
                                .font(.system(size: 11))
                                .foregroundColor(.white.opacity(0.25))
                                .italic()
                        }
                    }
                    .padding(10)
                    .background(Color.white.opacity(0.03))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }
}
