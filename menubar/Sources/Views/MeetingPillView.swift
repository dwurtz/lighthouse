import SwiftUI

// MARK: - Floating Meeting Pill
//
// A small floating widget that appears on screen when a calendar meeting
// is imminent. Shows meeting title + time, with a "Take notes" button.
// During recording, shows elapsed time + Stop.

struct MeetingPillView: View {
    @ObservedObject var monitor: MonitorState
    var onDismiss: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // Processing state — shown after recording stops while AI generates the page
            if monitor.meetingProcessing {
                HStack(spacing: 10) {
                    ProgressView()
                        .scaleEffect(0.6)
                        .frame(width: 16, height: 16)
                    Text("Generating notes...")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(.white.opacity(0.6))
                    Spacer()
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }

            if !monitor.meetingProcessing {
            // Header bar
            HStack(spacing: 12) {
                // Color accent bar
                RoundedRectangle(cornerRadius: 2)
                    .fill(monitor.meetingRecording ? Color.red : Color.cyan)
                    .frame(width: 3, height: 32)

                // Meeting info
                VStack(alignment: .leading, spacing: 2) {
                    Text(monitor.meetingTitle.isEmpty ? "Call" : monitor.meetingTitle)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(.white)
                        .lineLimit(1)
                    if monitor.meetingRecording {
                        Text("Recording · \(formatDuration(monitor.meetingElapsed))")
                            .font(.system(size: 11))
                            .foregroundColor(.red.opacity(0.8))
                    } else {
                        Text(monitor.meetingTimeRange)
                            .font(.system(size: 11))
                            .foregroundColor(.white.opacity(0.4))
                            .lineLimit(1)
                    }
                }

                Spacer()

                if monitor.meetingRecording {
                    // Pause — stops recording but doesn't process yet
                    Button(action: { monitor.pauseMeetingRecording() }) {
                        HStack(spacing: 4) {
                            Image(systemName: monitor.meetingPaused ? "play.fill" : "pause.fill")
                                .font(.system(size: 9))
                            Text(monitor.meetingPaused ? "Resume" : "Pause")
                                .font(.system(size: 11, weight: .medium))
                        }
                        .foregroundColor(.white.opacity(0.6))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.white.opacity(0.06))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .stroke(Color.white.opacity(0.12), lineWidth: 1)
                        )
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)

                    // End meeting — stops recording and processes
                    Button(action: { monitor.stopMeetingRecording() }) {
                        HStack(spacing: 4) {
                            Image(systemName: "stop.fill")
                                .font(.system(size: 9))
                            Text("End")
                                .font(.system(size: 11, weight: .medium))
                        }
                        .foregroundColor(.red)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.red.opacity(0.1))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .stroke(Color.red.opacity(0.25), lineWidth: 1)
                        )
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                } else {
                    Button(action: { monitor.startMeetingRecording() }) {
                        HStack(spacing: 5) {
                            Image(systemName: "mic.fill")
                                .font(.system(size: 10))
                            Text("Take notes")
                                .font(.system(size: 12, weight: .semibold))
                        }
                        .foregroundColor(.white.opacity(0.9))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                        .background(Color.white.opacity(0.08))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .stroke(Color.white.opacity(0.2), lineWidth: 1)
                        )
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)

                    Button(action: { onDismiss() }) {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundColor(.white.opacity(0.25))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            // Scratchpad — visible during recording
            if monitor.meetingRecording {
                Divider().background(Color.white.opacity(0.08))
                ZStack(alignment: .topLeading) {
                    if monitor.meetingNotes.isEmpty {
                        Text("Jot notes here...")
                            .font(.system(size: 12))
                            .foregroundColor(.white.opacity(0.2))
                            .padding(.horizontal, 4)
                            .padding(.vertical, 8)
                    }
                    TextEditor(text: $monitor.meetingNotes)
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.85))
                        .scrollContentBackground(.hidden)
                        .background(Color.clear)
                        .frame(minHeight: 80, maxHeight: 200)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
            }
            } // end if !meetingProcessing
        }
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color(white: 0.12))
                .shadow(color: .black.opacity(0.5), radius: 20, y: 5)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.08), lineWidth: 1)
        )
    }
}
