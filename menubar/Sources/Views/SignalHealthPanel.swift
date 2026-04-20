import SwiftUI
import AppKit

// MARK: - Model
//
// Wire contract (GET /api/signal_health). Timestamps are ISO-8601 with
// `Z` suffix. Status is one of "ok" / "stalled" / "error". Fields are
// intentionally permissive — anything unknown decodes to nil and the
// row falls back to sensible defaults.

struct SignalHealthSource: Decodable, Identifiable {
    let id: String
    let status: String
    let last_signal_at: String?
    let last_ok_at: String?
    let last_error_at: String?
    let last_error_reason: String?
    let expected_interval_minutes: Double?
    let minutes_since_last_signal: Double?
}

struct SignalHealthResponse: Decodable {
    let generated_at: String
    let awake: Bool?
    let sources: [SignalHealthSource]
}

struct TimelineEntry: Codable, Hashable, Identifiable {
    let ts: String
    let action: String
    let target: String?
    let reason: String?
    let cycle: String?
    var id: String { "\(ts)|\(action)|\(target ?? "")" }
}

struct TimelineResponse: Codable {
    let source_id: String
    let entries: [TimelineEntry]
}

// MARK: - Panel view

struct SignalHealthPanel: View {
    @ObservedObject var monitor: MonitorState

    @State private var response: SignalHealthResponse?
    @State private var lastFetchedAt: Date?
    @State private var fetchError: String?
    @State private var loading: Bool = false
    @State private var pollTimer: Timer?
    @State private var expandedSourceId: String?
    @State private var timelines: [String: [TimelineEntry]] = [:]
    @State private var timelineLoading: Set<String> = []

    // Brand palette — matches SetupPanelView.
    private var brandText: Color { Color(red: 236/255, green: 232/255, blue: 225/255) }
    private var brandText2: Color { Color.white.opacity(0.50) }
    private var brandText3: Color { Color.white.opacity(0.22) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().background(Color.white.opacity(0.08))
            if let err = fetchError, response == nil {
                errorState(err)
            } else if let resp = response {
                sourceList(resp)
            } else {
                loadingState
            }
            Divider().background(Color.white.opacity(0.08))
            footer
        }
        .frame(width: 520, height: 540)
        .background(Color.black)
        .onAppear { startPolling() }
        .onDisappear { stopPolling() }
    }

    // MARK: Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Signal Health")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundColor(brandText)
                Spacer()
                Button(action: { fetch() }) {
                    HStack(spacing: 5) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 11, weight: .semibold))
                        Text("Refresh")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .foregroundColor(brandText)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(Color.white.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
                .disabled(loading)
                .opacity(loading ? 0.5 : 1.0)
            }

            HStack(spacing: 8) {
                Text(lastUpdatedLabel)
                    .font(.system(size: 11))
                    .foregroundColor(brandText2)
                Text("•")
                    .foregroundColor(brandText3)
                Text("Awake: \(awakeLabel)")
                    .font(.system(size: 11))
                    .foregroundColor(brandText2)
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 20)
        .padding(.bottom, 14)
        .padding(.trailing, 40) // leave room for the container's close X
    }

    private var lastUpdatedLabel: String {
        guard let t = lastFetchedAt else { return "Last updated: —" }
        let delta = Int(max(0, Date().timeIntervalSince(t)))
        if delta < 2 { return "Last updated: just now" }
        if delta < 60 { return "Last updated: \(delta)s ago" }
        let mins = delta / 60
        return "Last updated: \(mins)m \(delta % 60)s ago"
    }

    private var awakeLabel: String {
        guard let awake = response?.awake else { return "—" }
        return awake ? "Yes" : "No"
    }

    // MARK: Source list

    private func sourceList(_ resp: SignalHealthResponse) -> some View {
        ScrollView {
            VStack(spacing: 0) {
                ForEach(resp.sources) { src in
                    sourceRow(src)
                    Divider().background(Color.white.opacity(0.05))
                }
                if resp.sources.isEmpty {
                    Text("No sources reported.")
                        .font(.system(size: 12))
                        .foregroundColor(brandText2)
                        .padding(.vertical, 24)
                }
            }
        }
    }

    private func sourceRow(_ src: SignalHealthSource) -> some View {
        let expanded = expandedSourceId == src.id
        return VStack(alignment: .leading, spacing: 0) {
            Button(action: {
                withAnimation(.easeInOut(duration: 0.12)) {
                    expandedSourceId = expanded ? nil : src.id
                }
            }) {
                HStack(spacing: 12) {
                    Circle()
                        .fill(statusColor(src.status))
                        .frame(width: 9, height: 9)
                    Text(src.id)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundColor(brandText)
                        .frame(width: 110, alignment: .leading)
                    statusChip(src.status)
                        .frame(width: 80, alignment: .center)
                    Spacer()
                    Text(lastLabel(src))
                        .font(.system(size: 11))
                        .foregroundColor(brandText2)
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundColor(brandText3)
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 10)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(intervalTooltip(src))

            if src.status == "error", let reason = src.last_error_reason, !reason.isEmpty {
                Text(reason)
                    .font(.system(size: 10))
                    .foregroundColor(Color.red.opacity(0.75))
                    .padding(.horizontal, 20)
                    .padding(.bottom, 8)
                    .padding(.leading, 41) // align under source id
            }

            if expanded {
                timelineView(for: src)
                    .padding(.horizontal, 20)
                    .padding(.bottom, 12)
                    .padding(.leading, 41)
            }
        }
    }

    private func statusChip(_ status: String) -> some View {
        let (label, color) = statusDisplay(status)
        return Text(label)
            .font(.system(size: 9, weight: .bold))
            .tracking(0.5)
            .foregroundColor(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(color.opacity(0.14))
            .clipShape(RoundedRectangle(cornerRadius: 4))
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "ok": return Color.green.opacity(0.85)
        case "stalled": return Color.orange.opacity(0.9)
        case "error": return Color.red.opacity(0.9)
        default: return Color.gray.opacity(0.6)
        }
    }

    private func statusDisplay(_ status: String) -> (String, Color) {
        switch status {
        case "ok": return ("OK", Color.green.opacity(0.9))
        case "stalled": return ("STALE", Color.orange.opacity(0.95))
        case "error": return ("ERROR", Color.red.opacity(0.95))
        default: return (status.uppercased(), Color.gray.opacity(0.8))
        }
    }

    private func lastLabel(_ src: SignalHealthSource) -> String {
        // Healthy idle sources (status=ok but no recent signals) read as
        // alarming if we just say "last: 35m ago" — users assume
        // something is broken. Prefix "idle ·" when the source is OK
        // and genuinely quiet, so the stale number reads as expected
        // rather than as a failure. Stalled/error sources already have
        // their own status chip; keep their labels crisp.
        let ago: String
        if let ts = src.last_signal_at, !ts.isEmpty {
            ago = formatTimestamp(ts, relative: true)
        } else if let mins = src.minutes_since_last_signal, mins > 0 {
            ago = "\(Int(mins))m ago"
        } else {
            return "stable"
        }
        switch src.status {
        case "ok":
            if let mins = src.minutes_since_last_signal, mins >= 2 {
                return "idle · last signal \(ago)"
            }
            return "last: \(ago)"
        case "stalled":
            return "stalled · last signal \(ago)"
        default:
            return "last: \(ago)"
        }
    }

    private func intervalTooltip(_ src: SignalHealthSource) -> String {
        if let n = src.expected_interval_minutes, n > 0 {
            if n >= 60 {
                let h = Int(n / 60)
                return "Expected every \(h)h"
            }
            return "Expected every \(Int(n)) min"
        }
        return "No expected interval (event-driven)"
    }

    // Inline timeline: up to ~20 collector_* entries for this source.
    // Fetched lazily from /api/signal_health/source/<id>/timeline when
    // the row is expanded for the first time; cached per-source until
    // the panel is closed.
    private func timelineView(for src: SignalHealthSource) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Recent activity")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(brandText2)
                Spacer()
                if timelineLoading.contains(src.id) {
                    ProgressView().scaleEffect(0.5)
                }
            }

            if let entries = timelines[src.id] {
                if entries.isEmpty {
                    Text("No collector_* audit entries yet.")
                        .font(.system(size: 10))
                        .foregroundColor(brandText3)
                } else {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(entries.prefix(20)) { e in
                            HStack(alignment: .top, spacing: 8) {
                                Text(formatTimestamp(e.ts, relative: true))
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundColor(brandText2)
                                    .frame(width: 70, alignment: .leading)
                                Text(actionLabel(e.action))
                                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                                    .foregroundColor(actionColor(e.action))
                                    .frame(width: 80, alignment: .leading)
                                Text(e.reason ?? "")
                                    .font(.system(size: 10))
                                    .foregroundColor(brandText)
                                    .lineLimit(2)
                            }
                        }
                    }
                }
            } else if !timelineLoading.contains(src.id) {
                Text("Loading timeline…")
                    .font(.system(size: 10))
                    .foregroundColor(brandText3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color.white.opacity(0.04))
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .onAppear { fetchTimeline(sourceId: src.id) }
    }

    private func actionLabel(_ action: String) -> String {
        switch action {
        case "collector_ok":      return "ok"
        case "collector_error":   return "error"
        case "collector_stalled": return "stalled"
        default:                  return action
        }
    }
    private func actionColor(_ action: String) -> Color {
        switch action {
        case "collector_ok":      return Color.green.opacity(0.9)
        case "collector_error":   return Color.red.opacity(0.9)
        case "collector_stalled": return Color.yellow.opacity(0.9)
        default:                  return brandText2
        }
    }

    // MARK: States

    private var loadingState: some View {
        VStack {
            Spacer()
            ProgressView()
                .progressViewStyle(CircularProgressViewStyle(tint: brandText2))
            Text("Loading…")
                .font(.system(size: 11))
                .foregroundColor(brandText2)
                .padding(.top, 8)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ err: String) -> some View {
        VStack(spacing: 10) {
            Spacer()
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 22))
                .foregroundColor(.orange.opacity(0.8))
            Text("Couldn't load signal health")
                .font(.system(size: 13, weight: .semibold))
                .foregroundColor(brandText)
            Text(err)
                .font(.system(size: 11))
                .foregroundColor(brandText2)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: Footer

    private var footer: some View {
        HStack {
            Text("Click a row to expand timeline. Full history lives in audit.jsonl (included in diagnostic bundles).")
                .font(.system(size: 10))
                .foregroundColor(brandText3)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    // MARK: Polling

    private func startPolling() {
        fetch()
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { _ in
            // Pause polling when the window is neither key nor visible —
            // no point hitting the socket when the user isn't looking.
            guard let win = NSApp.windows.first(where: { $0 is SignalHealthPanelWindow }),
                  win.isVisible else { return }
            fetch()
        }
    }

    private func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func fetch() {
        loading = true
        localAPICall("/api/signal_health") { data, err in
            DispatchQueue.main.async {
                loading = false
                if let err = err {
                    fetchError = err.localizedDescription
                    return
                }
                guard let data = data, !data.isEmpty else {
                    fetchError = "Empty response"
                    return
                }
                do {
                    let decoded = try JSONDecoder().decode(SignalHealthResponse.self, from: data)
                    response = decoded
                    fetchError = nil
                    lastFetchedAt = Date()
                } catch {
                    fetchError = "Decode error: \(error.localizedDescription)"
                }
            }
        }
    }

    // Fetch the per-source timeline lazily when a row is expanded.
    // Cached in `timelines` for the life of the panel — reopens refetch.
    private func fetchTimeline(sourceId: String) {
        if timelines[sourceId] != nil { return }
        if timelineLoading.contains(sourceId) { return }
        timelineLoading.insert(sourceId)
        localAPICall("/api/signal_health/source/\(sourceId)/timeline?limit=20") { data, err in
            DispatchQueue.main.async {
                timelineLoading.remove(sourceId)
                guard let data = data, err == nil, !data.isEmpty else {
                    timelines[sourceId] = []
                    return
                }
                do {
                    let decoded = try JSONDecoder().decode(TimelineResponse.self, from: data)
                    timelines[sourceId] = decoded.entries
                } catch {
                    timelines[sourceId] = []
                }
            }
        }
    }
}
