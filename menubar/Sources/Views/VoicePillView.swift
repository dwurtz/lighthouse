import SwiftUI

/// Floating voice pill — collapsed is a subtle dark capsule at screen bottom.
/// Expanded shows waveform, then processing, then transcript.
struct VoicePillView: View {
    @ObservedObject var monitor: MonitorState
    @State private var levelHistory: [CGFloat] = Array(repeating: 0, count: 16)

    var body: some View {
        ZStack {
            if monitor.voicePillActive {
                expandedPill
            } else if monitor.voicePillProcessing {
                processingPill
            } else if !monitor.voicePillTranscript.isEmpty {
                transcriptPill
            } else {
                collapsedPill
            }
        }
        .frame(width: 300, height: 56, alignment: .bottom)
        .animation(.spring(response: 0.3, dampingFraction: 0.75), value: monitor.voicePillActive)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillProcessing)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillTranscript.isEmpty)
        .onChange(of: monitor.audioLevel) { _, newLevel in
            guard monitor.voicePillActive else { return }
            levelHistory.removeFirst()
            levelHistory.append(newLevel)
        }
        .onChange(of: monitor.voicePillActive) { _, active in
            if !active {
                levelHistory = Array(repeating: 0, count: 16)
            }
        }
    }

    // MARK: - Collapsed: thin black pill, expands on hover

    private var collapsedPill: some View {
        Group {
            if monitor.voicePillHovered {
                HStack(spacing: 6) {
                    Image(systemName: "mic.fill")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.5))
                    Text("Click to dictate")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white.opacity(0.5))
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(
                    Capsule()
                        .fill(Color.black)
                )
                .overlay(
                    Capsule()
                        .stroke(Color.white.opacity(0.2), lineWidth: 1.5)
                )
                .transition(.opacity)
            } else {
                Capsule()
                    .fill(Color.black)
                    .overlay(
                        Capsule()
                            .stroke(Color.white.opacity(0.2), lineWidth: 1.5)
                    )
                    .frame(width: 100, height: 5)
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.15), value: monitor.voicePillHovered)
    }

    // MARK: - Expanded: waveform while recording

    private var expandedPill: some View {
        HStack(spacing: 3) {
            ForEach(0..<16, id: \.self) { i in
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.white.opacity(0.85))
                    .frame(width: 3, height: barHeight(for: i))
                    .animation(.easeOut(duration: 0.08), value: levelHistory[i])
            }
        }
        .frame(height: 32)
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .background(pillBackground)
        .overlay(pillBorder)
        .transition(.scale(scale: 0.6).combined(with: .opacity))
    }

    // MARK: - Processing: spinner after release

    private var processingPill: some View {
        HStack(spacing: 8) {
            ProgressView()
                .scaleEffect(0.6)
                .frame(width: 14, height: 14)
            Text("Transcribing...")
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(.white.opacity(0.6))
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 10)
        .background(pillBackground)
        .overlay(pillBorder)
        .transition(.scale(scale: 0.8).combined(with: .opacity))
    }

    // MARK: - Transcript: show what was heard

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
        .background(pillBackground)
        .overlay(pillBorder)
        .transition(.scale(scale: 0.8).combined(with: .opacity))
    }

    // MARK: - Shared styling

    private var pillBackground: some View {
        Capsule()
            .fill(Color.black)
            .shadow(color: .black.opacity(0.5), radius: 12, y: 2)
    }

    private var pillBorder: some View {
        Capsule()
            .stroke(Color.white.opacity(0.08), lineWidth: 1)
    }

    private func barHeight(for index: Int) -> CGFloat {
        let level = levelHistory[index]
        let minH: CGFloat = 4
        let maxH: CGFloat = 32
        return minH + level * (maxH - minH)
    }
}
