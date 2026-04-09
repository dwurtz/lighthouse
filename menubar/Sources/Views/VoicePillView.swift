import SwiftUI

/// Floating voice pill — collapsed is a subtle dark capsule at screen bottom.
/// Expanded shows animated sine waveform, then processing, then transcript.
struct VoicePillView: View {
    @ObservedObject var monitor: MonitorState

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
        .animation(.easeInOut(duration: 0.15), value: monitor.voicePillHovered)
    }

    // MARK: - Collapsed: thin black pill with hover expand

    private var collapsedPill: some View {
        VStack(spacing: 4) {
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
    }

    // MARK: - Expanded: animated sine waveform (Voquill-style)

    private var expandedPill: some View {
        WaveformShape(phase: monitor.waveformPhase)
            .stroke(Color.white.opacity(0.85), lineWidth: 1.6)
            .frame(width: 160, height: 28)
            .padding(.horizontal, 24)
            .padding(.vertical, 14)
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
            Text(monitor.voicePillStatus.isEmpty ? "Processing..." : monitor.voicePillStatus)
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
            .stroke(Color.white.opacity(0.2), lineWidth: 1.5)
    }
}

// MARK: - Animated sine waveform shape (inspired by Voquill)

struct WaveformShape: Shape {
    var phase: Double

    var animatableData: Double {
        get { phase }
        set { phase = newValue }
    }

    func path(in rect: CGRect) -> Path {
        var path = Path()
        let midY = rect.midY
        let amplitude = rect.height * 0.4
        let frequency: Double = 1.5
        let segments = Int(rect.width / 2)

        for i in 0...segments {
            let x = rect.width * CGFloat(i) / CGFloat(segments)
            let normalizedX = Double(i) / Double(segments) * .pi * 2 * frequency

            // Three overlapping waves like Voquill
            let wave1 = sin(normalizedX + phase) * 1.0
            let wave2 = sin(normalizedX * 1.3 + phase * 1.2 + 0.85) * 0.7
            let wave3 = sin(normalizedX * 0.7 + phase * 0.8 + 1.7) * 0.5

            let combined = (wave1 + wave2 + wave3) / 2.2
            let y = midY + CGFloat(combined) * amplitude

            // Fade at edges
            let edgeFade = min(Double(i) / 8.0, Double(segments - i) / 8.0, 1.0)
            let fadedY = midY + (y - midY) * CGFloat(edgeFade)

            if i == 0 {
                path.move(to: CGPoint(x: x, y: fadedY))
            } else {
                path.addLine(to: CGPoint(x: x, y: fadedY))
            }
        }
        return path
    }
}
