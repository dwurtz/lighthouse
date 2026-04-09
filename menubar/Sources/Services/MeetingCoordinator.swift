import Foundation
import AppKit

/// Coordinates meeting recording lifecycle: prompt polling, starting/stopping
/// recordings, and communicating with the Python backend via HTTP.
/// State updates are communicated back to MonitorState via callbacks.
class MeetingCoordinator {

    private let recorder = MeetingRecorder()
    private var lastPromptedEventId: String = ""
    private var recordedEventIds: Set<String> = []

    // MARK: - Meeting Prompt Polling

    func refreshMeetingPrompt(
        isRecording: Bool,
        wasAvailable: Bool,
        onUpdate: @escaping (_ available: Bool, _ title: String, _ attendees: [String], _ timeRange: String, _ eventId: String, _ isNewEvent: Bool) -> Void,
        onDismiss: @escaping () -> Void
    ) {
        guard !isRecording else { return }
        let req = localAPIRequest("/api/meeting/prompt", timeoutInterval: 2)
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self = self, let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let available = obj["available"] as? Bool else { return }
            DispatchQueue.main.async {
                if available {
                    let title = obj["title"] as? String ?? "Meeting"
                    let attendees = (obj["attendees"] as? [[String: String]] ?? []).map { $0["name"] ?? $0["email"] ?? "" }
                    let startISO = obj["start"] as? String ?? ""
                    let endISO = obj["end"] as? String ?? ""
                    let timeRange = MonitorState.formatTimeRange(start: startISO, end: endISO)

                    let eventId = obj["event_id"] as? String ?? ""
                    let isNew = (!wasAvailable || eventId != self.lastPromptedEventId)
                        && !eventId.isEmpty
                        && !self.recordedEventIds.contains(eventId)
                    if isNew {
                        self.lastPromptedEventId = eventId
                    }

                    onUpdate(true, title, attendees, timeRange, eventId, isNew)
                } else {
                    onUpdate(false, "", [], "", "", false)
                    if !isRecording {
                        onDismiss()
                    }
                }
            }
        }.resume()
    }

    func refreshMeetingStatus(onElapsed: @escaping (TimeInterval) -> Void) {
        let req = localAPIRequest("/api/meeting/status", timeoutInterval: 2)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                if let elapsed = obj["elapsed_sec"] as? Int {
                    onElapsed(TimeInterval(elapsed))
                }
            }
        }.resume()
    }

    // MARK: - Start Recording

    func startRecording(
        title: String,
        attendees: [String],
        onStarted: @escaping (_ sessionId: String) -> Void
    ) {
        // Remember this event so we don't re-prompt after recording
        if !lastPromptedEventId.isEmpty {
            recordedEventIds.insert(lastPromptedEventId)
        }

        var req = localAPIRequest("/api/meeting/start", method: "POST", timeoutInterval: 10)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = [
            "title": title,
            "attendees": attendees.map { ["name": $0] },
        ]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)

        URLSession.shared.dataTask(with: req) { [weak self] data, _, error in
            if let error = error {
                NSLog("deja: meeting start failed: \(error)")
                return
            }
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let sessionId = obj["session_id"] as? String,
                  let sessionDir = obj["session_dir"] as? String else { return }

            DispatchQueue.main.async {
                self?.recorder.startRecording(sessionId: sessionId, outputDirPath: sessionDir)
                onStarted(sessionId)
                NSLog("deja: meeting recording started in Swift: \(sessionId)")
            }
        }.resume()
    }

    // MARK: - Stop Recording

    func stopRecording(notes: String, onProcessed: @escaping () -> Void) {
        recorder.stopRecording { [weak self] in
            guard self != nil else { return }
            var req = localAPIRequest("/api/meeting/stop", method: "POST", timeoutInterval: 300)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["notes": notes])

            URLSession.shared.dataTask(with: req) { data, _, error in
                DispatchQueue.main.async {
                    onProcessed()
                }

                if let error = error {
                    NSLog("deja: meeting stop failed: \(error)")
                    return
                }
                if let data = data,
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    NSLog("deja: meeting processed: \(obj)")

                    if let slug = obj["slug"] as? String, !slug.isEmpty {
                        let vaultName = "Deja"
                        let encodedPath = "events/\(slug)".addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? slug
                        if let obsidianURL = URL(string: "obsidian://open?vault=\(vaultName)&file=\(encodedPath)") {
                            DispatchQueue.main.async {
                                NSWorkspace.shared.open(obsidianURL)
                            }
                        }
                    }
                }
            }.resume()
        }
    }

    // MARK: - Pause / Resume

    func pauseOrResume(isPaused: Bool, sessionId: String) {
        if isPaused {
            // Resume
            if let dir = recorder.sessionDir as String?, !dir.isEmpty {
                recorder.startRecording(sessionId: sessionId, outputDirPath: dir)
            }
        } else {
            // Pause
            recorder.stopRecording(completion: nil)
        }
    }

    // MARK: - Unlink

    func unlinkMeeting() {
        let req = localAPIRequest("/api/meeting/unlink", method: "POST", timeoutInterval: 5)
        URLSession.shared.dataTask(with: req) { _, _, _ in }.resume()
    }

    // MARK: - Auto-stop callback

    var onAutoStop: (() -> Void)? {
        get { recorder.onAutoStop }
        set { recorder.onAutoStop = newValue }
    }
}
