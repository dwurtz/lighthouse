import AVFoundation
import Foundation

/// Taps the mic input via AVAudioEngine to stream real-time RMS levels
/// for waveform visualization. Runs alongside ffmpeg (which does the
/// actual recording). This only reads levels — it doesn't record audio.
class AudioLevelMonitor {
    private var engine: AVAudioEngine?
    private var updateHandler: ((CGFloat) -> Void)?
    private var retryCount = 0

    func start(onLevel: @escaping (CGFloat) -> Void) {
        stop()
        retryCount = 0
        updateHandler = onLevel
        startEngine()
    }

    private func startEngine() {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)

        // If format has zero sample rate, the mic isn't ready yet — retry
        guard format.sampleRate > 0 else {
            retryCount += 1
            if retryCount <= 5 {
                NSLog("deja: AudioLevelMonitor — mic not ready (attempt %d), retrying...", retryCount)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
                    self?.startEngine()
                }
            }
            return
        }

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let data = buffer.floatChannelData?[0] else { return }
            let count = Int(buffer.frameLength)
            var sum: Float = 0
            for i in 0..<count {
                sum += data[i] * data[i]
            }
            let rms = sqrt(sum / Float(max(count, 1)))
            let level = CGFloat(min(rms * 4.0, 1.0))
            DispatchQueue.main.async {
                self?.updateHandler?(level)
            }
        }

        do {
            try engine.start()
            self.engine = engine
        } catch {
            NSLog("deja: AudioLevelMonitor failed to start: \(error)")
            // Retry once on failure
            retryCount += 1
            if retryCount <= 3 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                    self?.startEngine()
                }
            }
        }
    }

    func stop() {
        if let engine = engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        engine = nil
        updateHandler = nil
    }
}
