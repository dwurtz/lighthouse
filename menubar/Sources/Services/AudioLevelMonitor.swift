import AVFoundation
import Foundation

/// Taps the mic input via AVAudioEngine to stream real-time RMS levels
/// for waveform visualization. Runs alongside ffmpeg (which does the
/// actual recording). This only reads levels — it doesn't record audio.
class AudioLevelMonitor {
    private var engine: AVAudioEngine?
    private var updateHandler: ((CGFloat) -> Void)?

    func start(onLevel: @escaping (CGFloat) -> Void) {
        stop()
        updateHandler = onLevel

        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let data = buffer.floatChannelData?[0] else { return }
            let count = Int(buffer.frameLength)
            var sum: Float = 0
            for i in 0..<count {
                sum += data[i] * data[i]
            }
            let rms = sqrt(sum / Float(max(count, 1)))
            // Normalize to 0...1 range (mic RMS is typically 0..0.3)
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
        }
    }

    func stop() {
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        updateHandler = nil
    }
}
