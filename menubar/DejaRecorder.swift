// DejaRecorder — standalone CLI for meeting/call audio capture.
//
// Captures BOTH sides of audio:
//   - System audio via ScreenCaptureKit (what comes out of speakers)
//   - Microphone via ffmpeg/AVFoundation (your voice)
// Writes 5-minute WAV chunks mixing both sources.
//
// Usage:
//   DejaRecorder <session-dir>
//   Write a .stop file in session-dir to stop gracefully.

import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

// MARK: - Audio Recorder

class AudioRecorder: NSObject, SCStreamDelegate, SCStreamOutput {
    static let chunkDurationSec: Double = 300  // 5-minute chunks
    static let silenceTimeoutSec: Double = 300  // 5 min silence → auto-stop
    static let silenceThreshold: Float = 0.005
    static let sampleRate: Double = 16000
    static let channelCount: Int = 1

    private var stream: SCStream?
    private var outputDir: URL
    private var currentChunkIndex: Int = 0
    private var currentWriter: AVAssetWriter?
    private var currentWriterInput: AVAssetWriterInput?
    private var chunkStartTime: Date = Date()
    private var lastLoudTime: Date = Date()
    private var silenceTimer: Timer?
    private var isRecording: Bool = false

    // Mic capture via ffmpeg (reliable, proven path)
    private var micProcess: Process?
    private var micWavPath: URL?

    init(outputDir: URL) {
        self.outputDir = outputDir
        super.init()
    }

    func start() {
        isRecording = true
        lastLoudTime = Date()

        // Start mic capture via ffmpeg (separate file, merged later)
        startMicCapture()

        // Start system audio capture via ScreenCaptureKit
        Task {
            await startSystemCapture()
        }

        silenceTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            self?.checkSilence()
        }
    }

    func stop() {
        guard isRecording else { return }
        isRecording = false

        silenceTimer?.invalidate()
        silenceTimer = nil

        // Stop system audio
        if let stream = stream {
            stream.stopCapture { _ in }
            self.stream = nil
        }
        finalizeCurrentChunk()

        // Stop mic
        stopMicCapture()

        // Merge system audio chunks with mic audio
        mergeAudio()

        log("Recording stopped")
    }

    // MARK: - Mic capture (ffmpeg)

    private func startMicCapture() {
        micWavPath = outputDir.appendingPathComponent("mic-raw.wav")

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/ffmpeg")
        proc.arguments = [
            "-f", "avfoundation",
            "-i", ":0",           // default audio device (mic)
            "-ar", "16000",
            "-ac", "1",
            "-y",
            "-loglevel", "error",
            micWavPath!.path,
        ]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice

        do {
            try proc.run()
            micProcess = proc
            log("Mic capture started (ffmpeg)")
        } catch {
            log("WARNING: mic capture failed: \(error) — system audio only")
        }
    }

    private func stopMicCapture() {
        guard let proc = micProcess, proc.isRunning else { return }
        proc.interrupt()  // SIGINT → ffmpeg writes WAV trailer cleanly
        proc.waitUntilExit()
        micProcess = nil
        log("Mic capture stopped")
    }

    // MARK: - Merge system + mic audio

    private func mergeAudio() {
        // For each system audio chunk, mix in the corresponding portion
        // of the mic recording using ffmpeg amerge
        guard let micPath = micWavPath,
              FileManager.default.fileExists(atPath: micPath.path) else {
            log("No mic audio to merge")
            return
        }

        let chunks = (try? FileManager.default.contentsOfDirectory(at: outputDir, includingPropertiesForKeys: nil))?.filter {
            $0.lastPathComponent.hasPrefix("chunk-") && $0.pathExtension == "wav"
        }.sorted(by: { $0.path < $1.path }) ?? []

        if chunks.isEmpty {
            // No system audio — just rename mic to chunk-000
            let dest = outputDir.appendingPathComponent("chunk-000.wav")
            try? FileManager.default.moveItem(at: micPath, to: dest)
            let done = outputDir.appendingPathComponent("chunk-000.done")
            FileManager.default.createFile(atPath: done.path, contents: nil)
            log("No system audio, using mic-only as chunk-000")
            return
        }

        // Merge each chunk with the mic audio
        for chunk in chunks {
            let merged = outputDir.appendingPathComponent("merged-" + chunk.lastPathComponent)
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/ffmpeg")
            proc.arguments = [
                "-i", chunk.path,
                "-i", micPath.path,
                "-filter_complex", "amix=inputs=2:duration=shortest:dropout_transition=0",
                "-ar", "16000", "-ac", "1",
                "-y", "-loglevel", "error",
                merged.path,
            ]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice

            do {
                try proc.run()
                proc.waitUntilExit()
                if proc.terminationStatus == 0 {
                    // Replace original chunk with merged version
                    try? FileManager.default.removeItem(at: chunk)
                    try? FileManager.default.moveItem(at: merged, to: chunk)
                    log("Merged mic into \(chunk.lastPathComponent)")
                } else {
                    log("Merge failed for \(chunk.lastPathComponent), keeping system-only")
                    try? FileManager.default.removeItem(at: merged)
                }
            } catch {
                log("Merge error: \(error)")
            }
        }

        // Clean up mic raw file
        try? FileManager.default.removeItem(at: micPath)
    }

    // MARK: - System audio capture (ScreenCaptureKit)

    private func startSystemCapture() async {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else {
                log("ERROR: no display found")
                // Still have mic capture, so don't exit
                return
            }

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            config.capturesAudio = true
            config.sampleRate = Int(Self.sampleRate)
            config.channelCount = Self.channelCount
            config.excludesCurrentProcessAudio = true

            let stream = SCStream(filter: filter, configuration: config, delegate: self)
            try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: .global(qos: .userInitiated))
            try await stream.startCapture()

            self.stream = stream
            log("System audio capture started")
            startNewChunk()

        } catch {
            log("System audio capture failed: \(error) — mic-only mode")
        }
    }

    // MARK: - Chunks

    private func startNewChunk() {
        let chunkName = String(format: "chunk-%03d.wav", currentChunkIndex)
        let chunkURL = outputDir.appendingPathComponent(chunkName)

        do {
            let writer = try AVAssetWriter(outputURL: chunkURL, fileType: .wav)
            let audioSettings: [String: Any] = [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: Self.sampleRate,
                AVNumberOfChannelsKey: Self.channelCount,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false,
            ]
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
            input.expectsMediaDataInRealTime = true
            writer.add(input)
            writer.startWriting()
            writer.startSession(atSourceTime: .zero)

            currentWriter = writer
            currentWriterInput = input
            chunkStartTime = Date()
            log("Started chunk \(currentChunkIndex)")
        } catch {
            log("ERROR: chunk writer failed: \(error)")
        }
    }

    private func finalizeCurrentChunk() {
        guard let writer = currentWriter, let input = currentWriterInput else { return }

        input.markAsFinished()
        let sem = DispatchSemaphore(value: 0)
        writer.finishWriting { sem.signal() }
        sem.wait()

        let doneName = String(format: "chunk-%03d.done", currentChunkIndex)
        let doneURL = outputDir.appendingPathComponent(doneName)
        FileManager.default.createFile(atPath: doneURL.path, contents: nil)

        log("Finalized chunk \(currentChunkIndex)")
        currentWriter = nil
        currentWriterInput = nil
    }

    private func rotateChunkIfNeeded() {
        let elapsed = Date().timeIntervalSince(chunkStartTime)
        if elapsed >= Self.chunkDurationSec {
            finalizeCurrentChunk()
            currentChunkIndex += 1
            startNewChunk()
        }
    }

    // MARK: - Silence detection

    private func checkSilence() {
        let silenceDuration = Date().timeIntervalSince(lastLoudTime)
        if silenceDuration >= Self.silenceTimeoutSec {
            log("5 minutes of silence — auto-stopping")
            stop()
            exit(0)
        }
    }

    // MARK: - SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, isRecording else { return }

        // Silence tracking from system audio
        if let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) {
            var length = 0
            var dataPointer: UnsafeMutablePointer<Int8>?
            CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &dataPointer)
            if let data = dataPointer, length > 0 {
                let sampleCount = length / 2
                if sampleCount > 0 {
                    let samples = UnsafeBufferPointer(
                        start: UnsafeRawPointer(data).bindMemory(to: Int16.self, capacity: sampleCount),
                        count: sampleCount
                    )
                    var sumSquares: Float = 0
                    for sample in samples {
                        let f = Float(sample) / 32768.0
                        sumSquares += f * f
                    }
                    let rms = sqrt(sumSquares / Float(sampleCount))
                    if rms > Self.silenceThreshold {
                        lastLoudTime = Date()
                    }
                }
            }
        }

        rotateChunkIfNeeded()

        if let input = currentWriterInput, input.isReadyForMoreMediaData {
            let elapsed = Date().timeIntervalSince(chunkStartTime)
            let pts = CMTime(seconds: elapsed, preferredTimescale: Int32(Self.sampleRate))
            if let adjusted = adjustTimestamp(sampleBuffer, to: pts) {
                input.append(adjusted)
            }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        log("SCStream error: \(error)")
        // Don't exit — mic capture may still be working
    }

    private func adjustTimestamp(_ buffer: CMSampleBuffer, to pts: CMTime) -> CMSampleBuffer? {
        var timing = CMSampleTimingInfo(
            duration: CMSampleBufferGetDuration(buffer),
            presentationTimeStamp: pts,
            decodeTimeStamp: .invalid
        )
        var newBuffer: CMSampleBuffer?
        let status = CMSampleBufferCreateCopyWithNewTiming(
            allocator: nil, sampleBuffer: buffer,
            sampleTimingEntryCount: 1, sampleTimingArray: &timing,
            sampleBufferOut: &newBuffer
        )
        return status == noErr ? newBuffer : nil
    }
}

func log(_ message: String) {
    let ts = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write("[\(ts)] \(message)\n".data(using: .utf8)!)
}

// MARK: - Main

import AppKit

// MARK: - Simple Mic Recorder (for voice pill — replaces ffmpeg)

class MicRecorder {
    private var engine: AVAudioEngine?
    private var outputFile: AVAudioFile?
    private let outputPath: URL

    init(outputPath: URL) {
        self.outputPath = outputPath
    }

    func start() {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)

        guard inputFormat.sampleRate > 0 else {
            log("MicRecorder: no audio input available")
            exit(1)
        }

        // Record in the mic's native format — Whisper handles any sample rate
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: inputFormat.sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]

        do {
            outputFile = try AVAudioFile(forWriting: outputPath, settings: settings)
        } catch {
            log("MicRecorder: failed to create output file: \(error)")
            exit(1)
        }

        // Mono format for the tap
        guard let monoFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: inputFormat.sampleRate,
            channels: 1,
            interleaved: false
        ) else {
            log("MicRecorder: failed to create mono format")
            exit(1)
        }

        input.installTap(onBus: 0, bufferSize: 4096, format: monoFormat) { [weak self] buffer, _ in
            guard let file = self?.outputFile else { return }
            do {
                try file.write(from: buffer)
            } catch {
                // Drop buffer on write error
            }
        }

        do {
            try engine.start()
            self.engine = engine
            log("MicRecorder: recording to \(outputPath.lastPathComponent) (sr=\(inputFormat.sampleRate) ch=1)")
        } catch {
            log("MicRecorder: engine start failed: \(error)")
            exit(1)
        }
    }

    func stop() {
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        outputFile = nil
        log("MicRecorder: stopped")
    }
}

// MARK: - Main

var _globalMicRecorder: MicRecorder?

@main
struct DejaRecorderApp {
    static func main() {
        NSApplication.shared.setActivationPolicy(.accessory)

        // --mic <output.wav> mode: simple mic recording for the voice pill
        if CommandLine.arguments.count >= 3 && CommandLine.arguments[1] == "--mic" {
            let outputPath = URL(fileURLWithPath: CommandLine.arguments[2])
            _globalMicRecorder = MicRecorder(outputPath: outputPath)
            _globalMicRecorder?.start()

            signal(SIGINT) { _ in
                _globalMicRecorder?.stop()
                Thread.sleep(forTimeInterval: 0.1)
                exit(0)
            }
            signal(SIGTERM) { _ in
                _globalMicRecorder?.stop()
                Thread.sleep(forTimeInterval: 0.1)
                exit(0)
            }

            RunLoop.main.run()
            return
        }

        // Default: meeting recording mode
        guard CommandLine.arguments.count >= 2 else {
            log("Usage: DejaRecorder <session-dir>")
            log("       DejaRecorder --mic <output.wav>")
            exit(1)
        }

        let sessionDir = URL(fileURLWithPath: CommandLine.arguments[1])
        try? FileManager.default.createDirectory(at: sessionDir, withIntermediateDirectories: true)

        let recorder = AudioRecorder(outputDir: sessionDir)

        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            let stopFile = sessionDir.appendingPathComponent(".stop")
            if FileManager.default.fileExists(atPath: stopFile.path) {
                try? FileManager.default.removeItem(at: stopFile)
                recorder.stop()
                exit(0)
            }
        }

        recorder.start()
        RunLoop.main.run()
    }
}
