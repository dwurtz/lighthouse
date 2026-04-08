import Foundation

// Parse an ISO 8601 timestamp and return a human-readable local time.
// Examples: "2026-04-04T18:15:45.995+00:00" → "11:15" (local) or "3s ago"
// `relative: true` forces the "Ns/Nm/Nh ago" form.
func formatTimestamp(_ iso: String, relative: Bool = false) -> String {
    guard !iso.isEmpty else { return "—" }
    let formatters: [ISO8601DateFormatter] = {
        let f1 = ISO8601DateFormatter()
        f1.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let f2 = ISO8601DateFormatter()
        f2.formatOptions = [.withInternetDateTime]
        return [f1, f2]
    }()
    var date: Date?
    for f in formatters {
        if let d = f.date(from: iso) { date = d; break }
    }
    if date == nil {
        // Try naive (no timezone) ISO: "2026-04-04T18:15:45.995023"
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        df.timeZone = TimeZone.current
        date = df.date(from: iso)
        if date == nil {
            df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            date = df.date(from: iso)
        }
    }
    guard let d = date else { return "—" }
    let delta = Date().timeIntervalSince(d)
    if relative || delta < 3600 {
        let secs = Int(max(0, delta))
        if secs < 10 { return "just now" }
        if secs < 60 { return "\(secs)s ago" }
        let mins = secs / 60
        if mins < 60 { return "\(mins)m ago" }
        let hours = mins / 60
        return "\(hours)h ago"
    }
    let df = DateFormatter()
    df.dateFormat = "HH:mm"
    return df.string(from: d)
}

func formatDuration(_ seconds: TimeInterval) -> String {
    let mins = Int(seconds) / 60
    let secs = Int(seconds) % 60
    return String(format: "%d:%02d", mins, secs)
}
