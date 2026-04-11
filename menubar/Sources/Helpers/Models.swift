import SwiftUI
import ScreenCaptureKit
import CoreMedia

// MARK: - Data Models

/// Compact right-now briefing returned by ``GET /api/briefing``.
/// Pure derivation from goals.md — no LLM, no retrieval. Rendered by
/// the notch ``BriefingView`` at the top of the command center.
struct Briefing: Codable {
    struct Counts: Codable {
        let tasks_open: Int
        let waiting_open: Int
        let reminders_total: Int
        let reminders_due: Int
    }
    struct DueReminder: Codable, Identifiable {
        var id: String { date + question }
        let date: String
        let question: String
        let topics: [String]
    }
    struct OverdueTask: Codable, Identifiable {
        var id: String { text }
        let text: String
        let deadline: String
        let days_overdue: Int
    }
    struct UpcomingTask: Codable, Identifiable {
        var id: String { text }
        let text: String
        let deadline: String
        let days_until: Int
    }
    struct StaleWaiting: Codable, Identifiable {
        var id: String { text }
        let text: String
        let added: String
        let days_stale: Int
    }
    let counts: Counts
    let due_reminders: [DueReminder]
    let overdue_tasks: [OverdueTask]
    let upcoming_tasks: [UpcomingTask]
    let stale_waiting: [StaleWaiting]

    var hasAnything: Bool {
        !due_reminders.isEmpty
            || !overdue_tasks.isEmpty
            || !upcoming_tasks.isEmpty
            || !stale_waiting.isEmpty
    }

    static let empty = Briefing(
        counts: Counts(tasks_open: 0, waiting_open: 0, reminders_total: 0, reminders_due: 0),
        due_reminders: [],
        overdue_tasks: [],
        upcoming_tasks: [],
        stale_waiting: []
    )
}

/// One row in the notch Activity feed — returned by the backend's
/// ``GET /api/activity`` endpoint, which reads ``~/.deja/audit.jsonl``
/// (the single source of truth for agent mutations).
struct ActivityEntry: Identifiable, Codable {
    var id: String { "\(timestamp)-\(kind)-\(summary.prefix(32))" }
    let timestamp: String
    let kind: String
    let summary: String
}

/// Transient toast message shown after a command dispatch (green on
/// success, red on error). Auto-dismisses after a short delay.
struct Toast: Equatable {
    enum Style { case success, error }
    let style: Style
    let text: String
}

/// One MCP-compatible AI client detected on this machine. Mirrors the
/// JSON shape returned by ``GET /api/mcp/clients`` from the Python backend.
struct MCPClientInfo: Codable, Identifiable {
    var id: String { name }
    let name: String
    let installed: Bool
    var enabled: Bool
    let config_path: String
    let auto_configurable: Bool
    let note: String
}

struct AnalysisInsight: Identifiable {
    let id = UUID()
    let time: String
    let matches: [String]
    let facts: [String]
    let commitments: [String]
    let proposals: [String]
    let conversations: [String]  // "Chatting with Justin about Ruby's soccer options"
    let signalCount: Int
}

struct SignalInfo: Identifiable {
    let id = UUID()
    let source: String
    let text: String
    let time: String

    var sourceColor: Color {
        switch source {
        case "screenshot": return .purple
        case "email": return .red
        case "calendar": return .orange
        case "active_app": return .green
        case "clipboard": return .blue
        case "imessage": return .cyan
        case "whatsapp": return .mint
        case "drive": return .yellow
        case "tasks": return .teal
        default: return .gray
        }
    }
}

// MARK: - Notification Names

extension Notification.Name {
    static let meetingDetected = Notification.Name("dejaMeetingDetected")
    static let meetingDismissed = Notification.Name("dejaMeetingDismissed")
    static let agentNotification = Notification.Name("dejaAgentNotification")
    static let notificationDismissed = Notification.Name("dejaNotificationDismissed")
    static let voicePillToggled = Notification.Name("dejaVoicePillToggled")
    static let setupCompleted = Notification.Name("dejaSetupCompleted")
}

// MARK: - Screen Capture Delegate (minimal, for triggering TCC prompt)

class ScreenCaptureDelegate: NSObject, SCStreamOutput {
    static let shared = ScreenCaptureDelegate()
    func stream(_ stream: SCStream, didOutputSampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {}
}
