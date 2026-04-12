import Foundation

// MARK: - HealthPollingService
//
// Watches ``~/.deja/health.json`` and fires ``onReport`` whenever the
// file mtime changes AND the decoded contents differ from the last
// good report. Python is the sole writer; Swift is strictly
// read-only. Decode failures (partial writes) are swallowed so we
// don't clobber the last known-good report.
//
// Mirrors the shape of ``ErrorPollingService``: a DispatchSourceTimer
// on a utility queue, onMain-dispatch of the callback.

final class HealthPollingService {
    var onReport: ((HealthReport) -> Void)?

    private let fileURL: URL
    private let pollInterval: TimeInterval
    private let queue = DispatchQueue(label: "com.deja.health-polling", qos: .utility)
    private var timer: DispatchSourceTimer?
    private var lastMTime: Date?
    private var lastReport: HealthReport?

    init(fileURL: URL = HealthPollingService.defaultFileURL(),
         pollInterval: TimeInterval = 2.5) {
        self.fileURL = fileURL
        self.pollInterval = pollInterval
    }

    static func defaultFileURL() -> URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".deja/health.json")
    }

    func start() {
        stop()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 0.5, repeating: pollInterval)
        t.setEventHandler { [weak self] in self?.poll() }
        timer = t
        t.resume()
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    // MARK: - Private

    private func poll() {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return }

        // Dedupe: skip decode if mtime hasn't advanced since last check.
        // Python writes every ~15s so this saves a lot of JSON parses
        // at our 2.5s cadence.
        let attrs = try? FileManager.default.attributesOfItem(atPath: fileURL.path)
        let mtime = attrs?[.modificationDate] as? Date
        if let mtime, let last = lastMTime, mtime == last {
            return
        }

        guard let report = HealthReport.read(from: fileURL) else {
            // Partial write or malformed JSON — leave lastReport intact.
            return
        }

        lastMTime = mtime
        if report == lastReport { return }
        lastReport = report

        let cb = onReport
        DispatchQueue.main.async { cb?(report) }
    }
}
