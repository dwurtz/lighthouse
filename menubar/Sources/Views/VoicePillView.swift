import SwiftUI

/// Floating voice pill — collapsed is a subtle dark capsule at screen bottom.
///
/// Three visual states drive the pill:
///
///   1. **Idle** — the subtle 100×5 capsule (or a hover-expanded "Click to
///      expand" hint).
///   2. **Recording** — the animated sine waveform. Shown whenever the
///      user is actively capturing audio: push-to-talk voice dictation
///      (``voicePillActive``) OR a meeting recording in progress
///      (``meetingRecording``). Both share this visual so there's a
///      single "Deja is listening" affordance.
///   3. **Processing / transcript** — spinner or the resulting transcript
///      toast, only for the dictation flow (meetings don't emit a
///      transcript when they stop).
struct VoicePillView: View {
    @ObservedObject var monitor: MonitorState

    private var isRecording: Bool {
        monitor.voicePillActive || monitor.meetingRecording
    }

    var body: some View {
        ZStack {
            if isRecording {
                expandedPill
            } else if monitor.voicePillProcessing {
                processingPill
            } else if !monitor.voicePillTranscript.isEmpty {
                transcriptPill
            } else {
                collapsedPill
            }
        }
        .frame(width: 400, height: 56, alignment: .bottom)
        .animation(.spring(response: 0.3, dampingFraction: 0.75), value: isRecording)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillProcessing)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillTranscript.isEmpty)
        .animation(.easeInOut(duration: 0.15), value: monitor.voicePillHovered)
    }

    // MARK: - Collapsed

    private var collapsedPill: some View {
        VStack(spacing: 4) {
            if monitor.isBlocked {
                // Blocked state — amber capsule signals that Deja is
                // missing something it needs to run. Click reopens the
                // setup panel; the hovered label says so explicitly.
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 10))
                        .foregroundColor(.orange)
                    if monitor.voicePillHovered {
                        Text("Click to fix Deja setup")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundColor(.orange.opacity(0.85))
                    }
                }
                .padding(.horizontal, monitor.voicePillHovered ? 14 : 10)
                .padding(.vertical, monitor.voicePillHovered ? 8 : 5)
                .background(Capsule().fill(Color.black))
                .overlay(Capsule().stroke(Color.orange.opacity(0.55), lineWidth: 1.5))
                .transition(.opacity)
            } else if monitor.voicePillHovered {
                HStack(spacing: 6) {
                    Image(systemName: monitor.pillExpanded ? "chevron.down" : "chevron.up")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.5))
                    Text(monitor.pillExpanded ? "Click to collapse" : "Click to expand · hotkey to dictate")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white.opacity(0.5))
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Capsule().fill(Color.black))
                .overlay(Capsule().stroke(Color.white.opacity(0.2), lineWidth: 1.5))
                .transition(.opacity)
            } else {
                Capsule()
                    .fill(Color.black)
                    .overlay(Capsule().stroke(Color.white.opacity(0.2), lineWidth: 1.5))
                    .frame(width: 100, height: 5)
                    .transition(.opacity)
            }
        }
    }

    // MARK: - Expanded: 16 reactive bars driven by live mic RMS

    private var expandedPill: some View {
        HStack(spacing: 3) {
            ForEach(0..<16, id: \.self) { i in
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.white.opacity(0.85))
                    .frame(width: 3, height: barHeight(for: i))
                    .animation(.easeOut(duration: 0.08), value: monitor.levelHistory[i])
            }
        }
        .frame(height: 32)
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .background(Capsule().fill(Color.black))
        .overlay(Capsule().stroke(Color.white.opacity(0.2), lineWidth: 1.5))
        .transition(.scale(scale: 0.6).combined(with: .opacity))
    }

    private func barHeight(for index: Int) -> CGFloat {
        let level = monitor.levelHistory[index]
        let minH: CGFloat = 4
        let maxH: CGFloat = 32
        return minH + level * (maxH - minH)
    }

    // MARK: - Processing

    private var processingPill: some View {
        HStack(spacing: 8) {
            ProgressView()
                .scaleEffect(0.6)
                .frame(width: 14, height: 14)
            Text(monitor.voicePillStatus.isEmpty ? "Processing..." : monitor.voicePillStatus)
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(.white.opacity(0.6))
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 10)
        .background(Capsule().fill(Color.black))
        .overlay(Capsule().stroke(Color.white.opacity(0.2), lineWidth: 1.5))
        .transition(.scale(scale: 0.8).combined(with: .opacity))
    }

    // MARK: - Transcript

    private var transcriptPill: some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 12))
                .foregroundColor(.green.opacity(0.8))
            Text(monitor.voicePillTranscript)
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(.white.opacity(0.85))
                .lineLimit(2)
                .truncationMode(.tail)
        }
        .frame(maxWidth: 280)
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Capsule().fill(Color.black))
        .overlay(Capsule().stroke(Color.white.opacity(0.2), lineWidth: 1.5))
        .transition(.scale(scale: 0.8).combined(with: .opacity))
    }
}

