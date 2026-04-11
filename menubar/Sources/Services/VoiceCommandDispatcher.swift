// VoiceCommandDispatcher — bridges the Python backend to the in-process
// VoiceRecorder via a file-marker protocol in ~/.deja/.
//
// Why files instead of a socket: we already use file-marker polling for
// other Swift ↔ Python handoffs (e.g. ~/.deja/integrate_trigger.json),
// so this keeps the IPC surface consistent and zero-dependency.
//
// Protocol:
//   Python → Swift via ~/.deja/voice_cmd.json
//       { "action": "start", "wav_path": "/abs/path.wav", "ts": "<iso>" }
//       { "action": "stop",  "ts": "<iso>" }
//
//   Swift → Python via ~/.deja/voice_status.json
//       { "status": "recording", "wav_path": "/abs/path.wav", "ts": "<iso>" }
//       { "status": "done",      "wav_path": "/abs/path.wav", "ts": "<iso>" }
//       { "status": "error",     "detail": "<reason>",        "ts": "<iso>" }
//
// This replaces the DejaRecorder subprocess for the voice-pill path.
// Doing the recording inside the main Deja.app binary means ONE mic
// TCC entry (com.deja.app) instead of two.

import Foundation

final class VoiceCommandDispatcher {
    private let cmdPath: URL
    private let statusPath: URL
    private let recorder = VoiceRecorder()
    private var pollTimer: Timer?
    private var lastStateKey: String = ""
    private var currentWavPath: URL?

    init() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        self.cmdPath = home.appendingPathComponent(".deja/voice_cmd.json")
        self.statusPath = home.appendingPathComponent(".deja/voice_status.json")
    }

    func start() {
        // Poll every 150ms. Cheap — this file only gets touched at most
        // a couple times per minute during active voice use.
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in
            self?.poll()
        }
    }

    func stop() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func poll() {
        guard FileManager.default.fileExists(atPath: cmdPath.path) else { return }
        guard let data = try? Data(contentsOf: cmdPath) else { return }
        guard let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }

        // Key used to detect change — action + timestamp. Without this
        // we'd re-fire start/stop on every tick.
        let action = dict["action"] as? String ?? ""
        let ts = dict["ts"] as? String ?? ""
        let key = "\(action):\(ts)"
        if key == lastStateKey { return }
        lastStateKey = key

        if action == "start" {
            guard let pathStr = dict["wav_path"] as? String else {
                writeStatus(status: "error", detail: "missing wav_path")
                NSLog("deja: voice cmd start missing wav_path")
                return
            }
            let url = URL(fileURLWithPath: pathStr)
            do {
                try recorder.start(outputPath: url)
                currentWavPath = url
                writeStatus(status: "recording", wavPath: pathStr)
                NSLog("deja: voice recording started → \(pathStr)")
            } catch {
                writeStatus(status: "error", detail: "recorder start failed: \(error)")
                NSLog("deja: voice recording start failed: \(error)")
            }
        } else if action == "stop" {
            recorder.stop()
            let pathStr = currentWavPath?.path ?? ""
            currentWavPath = nil
            writeStatus(status: "done", wavPath: pathStr)
            NSLog("deja: voice recording stopped → \(pathStr)")
        }
    }

    private func writeStatus(status: String, wavPath: String = "", detail: String = "") {
        var dict: [String: Any] = [
            "status": status,
            "ts": ISO8601DateFormatter().string(from: Date()),
        ]
        if !wavPath.isEmpty { dict["wav_path"] = wavPath }
        if !detail.isEmpty { dict["detail"] = detail }

        // Ensure ~/.deja exists — normally created by the Python backend
        // on startup, but don't fail voice recording because of ordering.
        let dir = statusPath.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        do {
            let data = try JSONSerialization.data(withJSONObject: dict)
            try data.write(to: statusPath, options: .atomic)
        } catch {
            NSLog("deja: failed to write voice_status.json: \(error)")
        }
    }
}
