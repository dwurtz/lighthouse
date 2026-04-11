import SwiftUI
import ScreenCaptureKit
import CoreMedia

// MARK: - Data Models

/// One row in the notch Activity feed — parsed from the wiki's
/// ``log.md`` by the backend's ``GET /api/activity`` endpoint.
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
