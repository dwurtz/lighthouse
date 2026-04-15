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
    private let onLevel: ((CGFloat) -> Void)?

    // Commands authored before this instant are "stale" — they belong
    // to a previous app session and must not auto-fire on cold start.
    // Without this guard, a leftover {"action":"start"} from a prior
    // session causes the mic to silently activate at every launch.
    private var launchInstant: Date = .distantPast

    init(onLevel: ((CGFloat) -> Void)? = nil) {
        let home = FileManager.default.homeDirectoryForCurrentUser
        self.cmdPath = home.appendingPathComponent(".deja/voice_cmd.json")
        self.statusPath = home.appendingPathComponent(".deja/voice_status.json")
        self.onLevel = onLevel
    }

    func start() {
        launchInstant = Date()
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

    // The Python writer emits timestamps with fractional seconds
    // (e.g. "2026-04-14T19:32:36.581382+00:00"); the default
    // ISO8601DateFormatter rejects those. Try with-fractional first,
    // fall back to the standard form.
    private func parseISO8601(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
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

        // Drop stale commands authored before this app session began.
        // Stop commands are still honored regardless — defensive cleanup
        // of a leftover "recording" state is always safe.
        if action == "start", let cmdDate = parseISO8601(ts), cmdDate < launchInstant {
            NSLog("deja: ignoring stale voice cmd start (ts=\(ts) < launch=\(launchInstant))")
            return
        }

        if action == "start" {
            guard let pathStr = dict["wav_path"] as? String else {
                writeStatus(status: "error", detail: "missing wav_path")
                NSLog("deja: voice cmd start missing wav_path")
                return
            }
            let url = URL(fileURLWithPath: pathStr)
            do {
                try recorder.start(outputPath: url, onLevel: onLevel)
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
