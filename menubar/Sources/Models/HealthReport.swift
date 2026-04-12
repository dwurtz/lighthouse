import Foundation

// MARK: - HealthStatus
//
// Three-level traffic light used by both Python-side checks and the
// native TCC probes. String-backed so it round-trips JSON cleanly
// and matches the Python side verbatim.

enum HealthStatus: String, Codable, Equatable {
    case ok
    case degraded
    case broken

    /// Combine two statuses, returning the worst of the two.
    /// ``broken`` beats ``degraded`` beats ``ok``.
    func worsen(with other: HealthStatus) -> HealthStatus {
        switch (self, other) {
        case (.broken, _), (_, .broken): return .broken
        case (.degraded, _), (_, .degraded): return .degraded
        default: return .ok
        }
    }
}

// MARK: - HealthCheck
//
// One row in the health panel. The Python side writes these for
// proxy / signals / wiki / etc.; the Swift side synthesizes the
// same shape for the native TCC probes so the UI doesn't have to
// care where a row came from.

struct HealthCheck: Codable, Equatable, Identifiable {
    let id: String
    let label: String
    let status: HealthStatus
    let detail: String?
    let fix: String?
    let fixURL: String?

    private enum CodingKeys: String, CodingKey {
        case id
        case label
        case status
        case detail
        case fix
        case fixURL = "fix_url"
    }
}

// MARK: - HealthReport
//
// Mirror of ``~/.deja/health.json``. Python is the sole writer;
// Swift reads atomically and tolerates partial writes via the
// tryable decode in ``HealthPollingService``.

struct HealthReport: Codable, Equatable {
    let timestamp: String
    let overall: HealthStatus
    let checks: [HealthCheck]
    let appVersion: String?
    let lastErrorRequestId: String?

    private enum CodingKeys: String, CodingKey {
        case timestamp
        case overall
        case checks
        case appVersion = "app_version"
        case lastErrorRequestId = "last_error_request_id"
    }

    static func read(from url: URL) -> HealthReport? {
        guard let data = try? Data(contentsOf: url), !data.isEmpty else { return nil }
        return try? JSONDecoder().decode(HealthReport.self, from: data)
    }
}
