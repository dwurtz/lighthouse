import SwiftUI

/// Floating voice pill — collapsed is a subtle dark capsule at screen bottom.
/// Expanded shows 3 animated sine waves (Voquill-style), then transcript.
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
        .frame(width: 400, height: 56, alignment: .bottom)
        .animation(.spring(response: 0.3, dampingFraction: 0.75), value: monitor.voicePillActive)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillProcessing)
        .animation(.easeInOut(duration: 0.2), value: monitor.voicePillTranscript.isEmpty)
        .animation(.easeInOut(duration: 0.15), value: monitor.voicePillHovered)
    }

    // MARK: - Collapsed

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

    // MARK: - Expanded: 3 overlapping sine waves

    private var expandedPill: some View {
        ZStack {
            // Wave 1: dominant, full opacity
            SingleWave(phase: monitor.waveformPhase, frequency: 0.8, phaseOffset: 0, amplitude: 0.75)
                .stroke(Color.white.opacity(1.0), lineWidth: 1.6)
            // Wave 2: mid layer
            SingleWave(phase: monitor.waveformPhase, frequency: 1.0, phaseOffset: 0.85, amplitude: 0.55)
                .stroke(Color.white.opacity(0.6), lineWidth: 1.6)
            // Wave 3: subtle background
            SingleWave(phase: monitor.waveformPhase, frequency: 1.25, phaseOffset: 1.7, amplitude: 0.38)
                .stroke(Color.white.opacity(0.35), lineWidth: 1.6)
        }
        .frame(width: 180, height: 32)
        .clipShape(Capsule())
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(Capsule().fill(Color.black))
        .overlay(Capsule().stroke(Color.white.opacity(0.2), lineWidth: 1.5))
        .transition(.scale(scale: 0.6).combined(with: .opacity))
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

// MARK: - Single sine wave path

struct SingleWave: Shape {
    var phase: Double
    var frequency: Double
    var phaseOffset: Double
    var amplitude: Double

    var animatableData: Double {
        get { phase }
        set { phase = newValue }
    }

    func path(in rect: CGRect) -> Path {
        var path = Path()
        let midY = rect.midY
        let maxAmp = rect.height * 0.5 * amplitude
        let segments = max(Int(rect.width / 2), 72)

        for i in 0...segments {
            let t = Double(i) / Double(segments)
            let x = rect.width * CGFloat(t)
            let angle = t * .pi * 2 * frequency + phase + phaseOffset

            // Edge fade: smooth taper at left and right
            let fadeIn = min(t * 6.0, 1.0)
            let fadeOut = min((1.0 - t) * 6.0, 1.0)
            let fade = fadeIn * fadeOut

            let y = midY + CGFloat(sin(angle) * maxAmp * fade)

            if i == 0 {
                path.move(to: CGPoint(x: x, y: y))
            } else {
                path.addLine(to: CGPoint(x: x, y: y))
            }
        }
        return path
    }
}
