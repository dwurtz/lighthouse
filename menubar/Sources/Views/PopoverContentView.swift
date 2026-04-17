import SwiftUI
import AppKit

// MARK: - Popover Content

struct PopoverContentView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        // Fill whatever frame the parent hands us. The old fixed
        // 420×600 box was baked in from the legacy popover era and
        // caused the expanded notch panel (460×584) to overflow its
        // window, clipping the top header. ExpandedNotchPanel now
        // sizes this view explicitly.
        Group {
            content
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
    }

    var content: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Déjà")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.white.opacity(0.9))
                Spacer()
                HStack(spacing: 10) {
                    // Take notes button. During recording
                    // shows elapsed time. Captures system audio + mic.
                    if monitor.meetingProcessing {
                        HStack(spacing: 5) {
                            ProgressView()
                                .scaleEffect(0.5)
                                .frame(width: 12, height: 12)
                            Text("Generating...")
                                .font(.system(size: 11, weight: .medium))
                        }
                        .foregroundColor(.white.opacity(0.4))
                        .padding(.horizontal, 9)
                        .padding(.vertical, 4)
                    } else {
                    Button(action: { monitor.toggleRecording() }) {
                        HStack(spacing: 5) {
                            Image(systemName: monitor.meetingRecording ? "stop.circle.fill" : "mic.fill")
                                .font(.system(size: 11, weight: .semibold))
                            if monitor.meetingRecording {
                                Text(formatDuration(monitor.meetingElapsed))
                                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                            } else {
                                Text("Take notes")
                                    .font(.system(size: 11, weight: .medium))
                            }
                        }
                        .foregroundColor(monitor.meetingRecording ? .red : .white.opacity(0.85))
                        .padding(.horizontal, 9)
                        .padding(.vertical, 4)
                        .background(
                            monitor.meetingRecording
                                ? Color.red.opacity(0.15)
                                : Color.white.opacity(0.08)
                        )
                        .overlay(
                            Capsule().stroke(
                                monitor.meetingRecording ? Color.red.opacity(0.4) : Color.white.opacity(0.15),
                                lineWidth: 1
                            )
                        )
                        .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    }

                    Circle()
                        .fill(monitor.running ? .green : .red)
                        .frame(width: 6, height: 6)
                    Button(action: { monitor.restart() }) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 11))
                            .foregroundColor(.white.opacity(0.4))
                    }
                    .buttonStyle(.plain)
                    Button(action: { withAnimation(.easeInOut(duration: 0.2)) { monitor.showSettings.toggle() } }) {
                        Image(systemName: "gearshape.fill")
                            .font(.system(size: 11))
                            .foregroundColor(monitor.showSettings ? .white.opacity(0.8) : .white.opacity(0.4))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(Color.black)

            Divider().background(Color.white.opacity(0.1))

            // Wiki generation banner — shown while backfill is running
            if monitor.backfillRunning {
                HStack(spacing: 8) {
                    ProgressView()
                        .scaleEffect(0.5)
                        .frame(width: 12, height: 12)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Building your wiki...")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundColor(.white)
                        Text(monitor.backfillStep.isEmpty ? "Starting..." : monitor.backfillStep)
                            .font(.system(size: 9))
                            .foregroundColor(.white.opacity(0.4))
                            .lineLimit(1)
                    }
                    Spacer()
                    if monitor.backfillPages > 0 {
                        Text("\(monitor.backfillPages) pages")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundColor(.green)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(Color.green.opacity(0.06))
            }

            // Permission warnings removed from main view.
            // Users can check and grant permissions in Settings.

            // Meeting prompt banner — shows when a calendar meeting is
            // imminent/active and we're not recording yet
            if monitor.meetingAvailable && !monitor.meetingRecording {
                HStack(spacing: 10) {
                    Image(systemName: "calendar")
                        .font(.system(size: 12))
                        .foregroundColor(.orange)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(monitor.meetingTitle)
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white)
                            .lineLimit(1)
                        if !monitor.meetingAttendees.isEmpty {
                            Text(monitor.meetingAttendees.prefix(3).joined(separator: ", "))
                                .font(.system(size: 9))
                                .foregroundColor(.white.opacity(0.4))
                                .lineLimit(1)
                        }
                    }
                    Spacer()
                    Button(action: { monitor.startMeetingRecording() }) {
                        HStack(spacing: 4) {
                            Image(systemName: "mic.fill")
                                .font(.system(size: 9))
                            Text("Take notes")
                                .font(.system(size: 11, weight: .semibold))
                        }
                        .foregroundColor(.white)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Color.orange.opacity(0.25))
                        .overlay(Capsule().stroke(Color.orange.opacity(0.5), lineWidth: 1))
                        .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(Color.orange.opacity(0.06))
            }

            // Meeting context banner — shows when recording and linked
            // to a calendar event. User can disconnect to keep recording
            // without the meeting context.
            if monitor.meetingRecording && monitor.meetingLinked {
                HStack(spacing: 8) {
                    Image(systemName: "link")
                        .font(.system(size: 9))
                        .foregroundColor(.green.opacity(0.7))
                    Text(monitor.meetingTitle)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white.opacity(0.7))
                        .lineLimit(1)
                    if !monitor.meetingAttendees.isEmpty {
                        Text("· " + monitor.meetingAttendees.prefix(2).joined(separator: ", "))
                            .font(.system(size: 10))
                            .foregroundColor(.white.opacity(0.35))
                            .lineLimit(1)
                    }
                    Spacer()
                    Button(action: { monitor.unlinkMeeting() }) {
                        Text("Unlink")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundColor(.white.opacity(0.4))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .overlay(Capsule().stroke(Color.white.opacity(0.15), lineWidth: 1))
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
                .background(Color.green.opacity(0.05))
            }

            if monitor.showSettings {
                SettingsView(monitor: monitor)
            } else {
                CommandCenterView(monitor: monitor)
            }
        }
        .background(Color.black)
    }
}
