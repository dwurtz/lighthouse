// VoiceRecorder — in-process one-shot mic capture for the voice pill.
//
// This class used to live in `menubar/DejaRecorder.swift` as
// `OneShotMicRecorder`, spawned as a subprocess by the Python backend.
// DejaRecorder is a command-line tool with no bundle identifier and no
// NSMicrophoneUsageDescription, so macOS TCC doesn't recognize it as
// holding mic permission — AVAudioEngine runs without error but the
// input tap returns zero-filled buffers, which Whisper transcribes as
// "you" (its silence hallucination) and our filter drops.
//
// Moving the recording into the main Deja.app binary (which has a real
// `com.deja.app` TCC entry) fixes the root cause with ONE mic permission
// instead of two. The orange mic indicator still goes dark between
// recordings because we fully start/stop AVAudioEngine per recording —
// there's no persistent daemon.
//
// Meeting recording (long captures via ScreenCaptureKit + ffmpeg) still
// lives in DejaRecorder for now — that's a separate code path.

import Foundation
import AVFoundation

/// One-shot microphone recorder driven by the Python backend via the
/// voice_cmd.json / voice_status.json file-marker protocol. Each
/// recording gets a fresh AVAudioEngine; `stop()` tears everything
/// down so the macOS mic indicator goes dark between recordings.
final class VoiceRecorder {
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?

    func start(outputPath: URL) throws {
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)

        guard inputFormat.sampleRate > 0 else {
            throw NSError(
                domain: "VoiceRecorder",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "no audio input available"],
            )
        }

        let wavSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: inputFormat.sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]

        guard let monoFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: inputFormat.sampleRate,
            channels: 1,
            interleaved: false,
        ) else {
            throw NSError(
                domain: "VoiceRecorder",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "failed to build mono format"],
            )
        }

        self.file = try AVAudioFile(forWriting: outputPath, settings: wavSettings)

        input.installTap(onBus: 0, bufferSize: 4096, format: monoFormat) { [weak self] buffer, _ in
            guard let self = self, let f = self.file else { return }
            try? f.write(from: buffer)
        }

        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        // Dropping the last strong ref to AVAudioFile triggers deinit,
        // which flushes the WAV header (sample count, chunk sizes).
        file = nil
    }
}
