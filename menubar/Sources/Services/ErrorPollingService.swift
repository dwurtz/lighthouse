import Foundation

// MARK: - ErrorPollingService
//
// Watches ``~/.deja/latest_error.json`` and fires ``onError`` whenever
// a new error (different ``requestId`` from the last one we surfaced)
// appears. The Python side is the sole writer; Swift is strictly
// read-only except for deleting the file after dismissal so the same
// error doesn't replay on the next poll.
//
// Smoke test:
//   echo '{"request_id":"req_deadbeef1234","code":"proxy_unavailable","message":"Test error — Render is down.","timestamp":"2026-04-12T16:30:00Z","details":{}}' > ~/.deja/latest_error.json
//   — toast should appear within 2s with the test message and copyable ID.
//   — clicking copy should put "req_deadbeef1234" on the clipboard.
//   — clicking × or waiting 8s should remove the file.

final class ErrorPollingService {
    var onError: ((DejaError) -> Void)?

    private let fileURL: URL
    private let pollInterval: TimeInterval
    private let queue = DispatchQueue(label: "com.deja.error-polling", qos: .utility)
    private var timer: DispatchSourceTimer?
    private var lastSeenRequestId: String?

    init(fileURL: URL = ErrorPollingService.defaultFileURL(),
         pollInterval: TimeInterval = 2.0) {
        self.fileURL = fileURL
        self.pollInterval = pollInterval
    }

    static func defaultFileURL() -> URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".deja/latest_error.json")
    }

    func start() {
        stop()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + pollInterval, repeating: pollInterval)
        t.setEventHandler { [weak self] in self?.poll() }
        timer = t
        t.resume()
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    /// Delete the latest-error file so it doesn't re-surface next tick.
    /// Safe to call on any thread; runs on the polling queue so it
    /// interleaves correctly with ``poll()``.
    func dismissAndClear(_ error: DejaError) {
        queue.async { [weak self] in
            guard let self = self else { return }
            // Only clear if the file on disk still matches this error —
            // otherwise we might nuke a newer one that just arrived.
            if let current = DejaError.readLatest(from: self.fileURL),
               current.requestId != error.requestId {
                return
            }
            try? FileManager.default.removeItem(at: self.fileURL)
        }
    }

    // MARK: - Private

    private func poll() {
        guard FileManager.default.fileExists(atPath: fileURL.path),
              let err = DejaError.readLatest(from: fileURL) else {
            return
        }
        if err.requestId == lastSeenRequestId { return }
        lastSeenRequestId = err.requestId
        let cb = onError
        DispatchQueue.main.async { cb?(err) }
    }
}
