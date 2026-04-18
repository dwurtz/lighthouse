import SwiftUI

/// Floating voice pill — collapsed is a subtle dark capsule at screen bottom.
///
/// Shape and sizing mirror Voquill (48×6 idle capsule; 120×32 hover /
/// active capsule) so that when both apps run in parallel the two bars
/// look like siblings. The four visual states are:
///
///   1. **Idle** — the subtle 48×6 capsule. Hovering the window grows
///      it to a 120×32 capsule with a chevron hint (no text label).
///   2. **Recording** — the 16-bar live waveform. Shown whenever the
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

    /// Voquill parity.
    private static let idleCapsuleWidth: CGFloat = 48
    private static let idleCapsuleHeight: CGFloat = 6
    private static let hoverCapsuleWidth: CGFloat = 120
    private static let hoverCapsuleHeight: CGFloat = 32
    private static let hoverCornerRadius: CGFloat = 16

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
        .frame(width: 320, height: 96, alignment: .bottom)
        .animation(.spring(response: 0.3, dampingFraction: 0.75), value: isRecording)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillProcessing)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillTranscript.isEmpty)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillConfirmation.isEmpty)
        .animation(.easeInOut(duration: 0.15), value: monitor.voicePillHovered)
    }

    // MARK: - Collapsed

    // Hover only changes the visual when the command panel is closed;
    // with the panel open we leave the bottom capsule alone.
    private var showHoverVisual: Bool {
        monitor.voicePillHovered && !monitor.pillExpanded
    }

    private var collapsedPill: some View {
        VStack(spacing: 4) {
            if monitor.isBlocked {
                // Blocked state — amber capsule signals that Deja is
                // missing something it needs to run. Uses the hover
                // geometry so the warning icon has room to breathe.
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor(.orange)
                    .frame(width: Self.hoverCapsuleWidth, height: Self.hoverCapsuleHeight)
                    .background(
                        RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                            .fill(Color.black.opacity(0.92))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                            .stroke(Color.orange.opacity(0.55), lineWidth: 1)
                    )
                    .transition(.opacity)
            } else if showHoverVisual {
                Image(systemName: monitor.pillExpanded ? "chevron.down" : "chevron.up")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor(.white.opacity(0.75))
                    .frame(width: Self.hoverCapsuleWidth, height: Self.hoverCapsuleHeight)
                    .background(
                        RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                            .fill(Color.black.opacity(0.92))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                            .stroke(Color.white.opacity(0.3), lineWidth: 1)
                    )
                    .transition(.opacity)
            } else {
                Capsule()
                    .fill(Color.black.opacity(0.6))
                    .overlay(Capsule().stroke(Color.white.opacity(0.3), lineWidth: 1))
                    .frame(width: Self.idleCapsuleWidth, height: Self.idleCapsuleHeight)
                    .transition(.opacity)
            }
        }
    }

    // MARK: - Expanded: 16 reactive bars driven by live mic RMS

    private var expandedPill: some View {
        HStack(spacing: 2) {
            ForEach(0..<16, id: \.self) { i in
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(Color.white.opacity(0.85))
                    .frame(width: 2.5, height: barHeight(for: i))
                    .animation(.easeOut(duration: 0.08), value: monitor.levelHistory[i])
            }
        }
        .frame(width: Self.hoverCapsuleWidth, height: Self.hoverCapsuleHeight)
        .background(
            RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                .fill(Color.black.opacity(0.92))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                .stroke(Color.white.opacity(0.3), lineWidth: 1)
        )
        .transition(.scale(scale: 0.6).combined(with: .opacity))
    }

    private func barHeight(for index: Int) -> CGFloat {
        let level = monitor.levelHistory[index]
        let minH: CGFloat = 3
        let maxH: CGFloat = 22
        return minH + level * (maxH - minH)
    }

    // MARK: - Processing

    private var processingPill: some View {
        HStack(spacing: 6) {
            ProgressView()
                .scaleEffect(0.5)
                .frame(width: 12, height: 12)
            Text(monitor.voicePillStatus.isEmpty ? "…" : monitor.voicePillStatus)
                .font(.system(size: 10, weight: .medium))
                .foregroundColor(.white.opacity(0.7))
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .frame(width: Self.hoverCapsuleWidth, height: Self.hoverCapsuleHeight)
        .background(
            RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                .fill(Color.black.opacity(0.92))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                .stroke(Color.white.opacity(0.3), lineWidth: 1)
        )
        .transition(.scale(scale: 0.8).combined(with: .opacity))
    }

    // MARK: - Transcript

    /// Width budget for the echo pill — wider than the idle/hover capsule
    /// so we can actually fit a dictated sentence without shrinking to a
    /// single word. Still tight enough to stay a "pill" rather than a
    /// card. Confirmation layout re-uses the same width and grows in
    /// height when a second line is present.
    private static let transcriptPillWidth: CGFloat = 300

    private var transcriptPill: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                if !monitor.voicePillBadge.isEmpty {
                    Text(monitor.voicePillBadge)
                        .font(.system(size: 12))
                } else {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 11))
                        .foregroundColor(.green.opacity(0.85))
                }
                Text(monitor.voicePillTranscript)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.white.opacity(0.95))
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 4)
                if !monitor.voicePillUndoToken.isEmpty {
                    Button(action: { monitor.undoLastVoiceDispatch() }) {
                        Text("Undo")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundColor(.white.opacity(0.9))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(
                                RoundedRectangle(cornerRadius: 6)
                                    .fill(Color.white.opacity(0.15))
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: 6)
                                    .stroke(Color.white.opacity(0.35), lineWidth: 0.5)
                            )
                    }
                    .buttonStyle(.plain)
                }
            }
            if !monitor.voicePillUndoStatus.isEmpty {
                Text(monitor.voicePillUndoStatus)
                    .font(.system(size: 9, weight: .regular))
                    .foregroundColor(.orange.opacity(0.85))
                    .padding(.leading, 18)
            } else if !monitor.voicePillConfirmation.isEmpty {
                Text(monitor.voicePillConfirmation)
                    .font(.system(size: 9, weight: .regular))
                    .foregroundColor(.white.opacity(0.6))
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .padding(.leading, 18)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .frame(maxWidth: Self.transcriptPillWidth, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                .fill(Color.black.opacity(0.92))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Self.hoverCornerRadius)
                .stroke(Color.white.opacity(0.3), lineWidth: 1)
        )
        .transition(.scale(scale: 0.8).combined(with: .opacity))
    }
}
