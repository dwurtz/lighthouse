import Foundation
import SQLite3

/// Reads from macOS databases (iMessage, WhatsApp, Contacts) and writes
/// JSON buffers to ~/.deja/ so the Python backend doesn't need Full Disk Access.
class DatabaseReader {

    private static let home = MonitorState.home

    // Apple epoch: 2001-01-01 UTC = 978307200 seconds after Unix epoch
    private static let appleEpochOffset: Double = 978307200

    private var lastContactsRefresh: Date = .distantPast

    // MARK: - Public API

    /// Read all databases, writing JSON buffers. Contacts are refreshed every 5 minutes.
    func readAll() {
        readIMessages()
        readWhatsApp()
        if Date().timeIntervalSince(lastContactsRefresh) > 300 {
            readContacts()
            lastContactsRefresh = Date()
        }
    }

    // MARK: - iMessage

    private func readIMessages() {
        let dbPath = NSHomeDirectory() + "/Library/Messages/chat.db"
        guard FileManager.default.fileExists(atPath: dbPath) else { return }

        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return }
        defer { sqlite3_close(db) }

        // Cutoff: 5 minutes ago, in Apple nanoseconds
        let cutoffUnix = Date().timeIntervalSince1970 - 300
        let cutoffAppleNs = Int64((cutoffUnix - Self.appleEpochOffset) * 1_000_000_000)

        // Read BOTH `text` and `attributedBody`. Modern macOS stores many
        // rows (including ~every outbound message on this machine) with
        // text = NULL and the content packed into the attributedBody
        // typedstream blob. See AttributedBodyDecoder for format details.
        let sql = """
            SELECT m.text, m.date, m.is_from_me, h.id as handle_id, m.attributedBody
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.date > ?1 AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
            ORDER BY m.date DESC LIMIT 50
            """

        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_int64(stmt, 1, cutoffAppleNs)

        var messages: [[String: Any]] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            var text: String = ""
            if let cStr = sqlite3_column_text(stmt, 0) {
                text = String(cString: cStr)
            }

            if text.isEmpty {
                // Fall back to decoding attributedBody. The blob is a
                // typedstream-archived NSAttributedString and needs a
                // custom extractor.
                let blobLen = sqlite3_column_bytes(stmt, 4)
                if blobLen > 0, let blobPtr = sqlite3_column_blob(stmt, 4) {
                    let blob = Data(bytes: blobPtr, count: Int(blobLen))
                    if let decoded = AttributedBodyDecoder.extractString(from: blob) {
                        text = decoded
                    }
                }
            }

            if text.isEmpty { continue }

            let appleNs = sqlite3_column_int64(stmt, 1)
            let unixTs = Double(appleNs) / 1_000_000_000.0 + Self.appleEpochOffset
            let isFromMe = sqlite3_column_int(stmt, 2) == 1

            let handleId: String
            if let cStr = sqlite3_column_text(stmt, 3) {
                handleId = String(cString: cStr)
            } else {
                handleId = "unknown"
            }

            let sender = isFromMe ? "me" : handleId

            let date = Date(timeIntervalSince1970: unixTs)
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
            let dt = fmt.string(from: date)

            messages.append([
                "text": String(text.prefix(500)),
                "timestamp": unixTs,
                "dt": dt,
                "is_from_me": isFromMe,
                "sender": sender,
            ])
        }

        writeJSONBuffer(messages, to: "imessage_buffer.json")
    }

    // MARK: - WhatsApp

    private func readWhatsApp() {
        let dbPath = NSHomeDirectory() + "/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
        guard FileManager.default.fileExists(atPath: dbPath) else { return }

        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return }
        defer { sqlite3_close(db) }

        let cutoffUnix = Date().timeIntervalSince1970 - 300
        let cutoffApple = cutoffUnix - Self.appleEpochOffset

        let sql = """
            SELECT ZWAMESSAGE.ZTEXT, ZWAMESSAGE.ZMESSAGEDATE, ZWAMESSAGE.ZISFROMME,
                   ZWACHATSESSION.ZCONTACTJID
            FROM ZWAMESSAGE
            LEFT JOIN ZWACHATSESSION ON ZWAMESSAGE.ZCHATSESSION = ZWACHATSESSION.Z_PK
            WHERE ZWAMESSAGE.ZTEXT IS NOT NULL AND ZWAMESSAGE.ZTEXT != '' AND ZWAMESSAGE.ZMESSAGEDATE > ?1
            ORDER BY ZWAMESSAGE.ZMESSAGEDATE DESC LIMIT 50
            """

        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_double(stmt, 1, cutoffApple)

        var messages: [[String: Any]] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let text: String
            if let cStr = sqlite3_column_text(stmt, 0) {
                text = String(cString: cStr)
            } else { continue }

            let appleSec = sqlite3_column_double(stmt, 1)
            let unixTs = appleSec + Self.appleEpochOffset
            let isFromMe = sqlite3_column_int(stmt, 2) == 1

            let contactJid: String
            if let cStr = sqlite3_column_text(stmt, 3) {
                contactJid = String(cString: cStr)
            } else {
                contactJid = "unknown"
            }

            let sender = isFromMe ? "me" : contactJid

            let date = Date(timeIntervalSince1970: unixTs)
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
            let dt = fmt.string(from: date)

            messages.append([
                "text": String(text.prefix(500)),
                "timestamp": unixTs,
                "dt": dt,
                "is_from_me": isFromMe,
                "sender": sender,
            ])
        }

        writeJSONBuffer(messages, to: "whatsapp_buffer.json")
    }

    // MARK: - Contacts

    private func readContacts() {
        let abDir = NSHomeDirectory() + "/Library/Application Support/AddressBook/Sources"
        guard FileManager.default.fileExists(atPath: abDir) else { return }

        var contacts: [[String: Any]] = []

        let fm = FileManager.default
        guard let sources = try? fm.contentsOfDirectory(atPath: abDir) else { return }

        for source in sources {
            let dbPath = abDir + "/" + source + "/AddressBook-v22.abcddb"
            guard fm.fileExists(atPath: dbPath) else { continue }

            var db: OpaquePointer?
            guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { continue }

            let sql = """
                SELECT
                    COALESCE(r.ZFIRSTNAME, '') || ' ' || COALESCE(r.ZLASTNAME, '') as name,
                    GROUP_CONCAT(DISTINCT p.ZFULLNUMBER) as phones
                FROM ZABCDRECORD r
                LEFT JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                WHERE r.ZFIRSTNAME IS NOT NULL
                GROUP BY r.Z_PK
                """

            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                sqlite3_close(db)
                continue
            }

            while sqlite3_step(stmt) == SQLITE_ROW {
                let name: String
                if let cStr = sqlite3_column_text(stmt, 0) {
                    name = String(cString: cStr).trimmingCharacters(in: .whitespaces)
                } else { continue }

                if name.isEmpty { continue }

                let phones: String
                if let cStr = sqlite3_column_text(stmt, 1) {
                    phones = String(cString: cStr)
                } else {
                    phones = ""
                }

                contacts.append([
                    "name": name,
                    "phones": phones,
                ])
            }

            sqlite3_finalize(stmt)
            sqlite3_close(db)
        }

        writeJSONBuffer(contacts, to: "contacts_buffer.json")
    }

    // MARK: - Signal Log Reading

    struct StatsResult {
        let signals: Int
        let matches: Int
    }

    func readStats(isMonitorRunning: Bool) -> (stats: StatsResult, running: Bool) {
        let logPath = Self.home + "/signal_log.jsonl"
        guard FileManager.default.fileExists(atPath: logPath),
              let data = FileManager.default.contents(atPath: logPath) else {
            return (StatsResult(signals: 0, matches: 0), isMonitorRunning)
        }
        let lineCount = data.split(separator: UInt8(ascii: "\n")).count
        let analysisPath = Self.home + "/analysis_log.jsonl"
        var matchCount = 0
        if let ad = FileManager.default.contents(atPath: analysisPath) {
            for line in ad.split(separator: UInt8(ascii: "\n")) {
                if let json = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                   let m = json["matches"] as? [[String: Any]] { matchCount += m.count }
            }
        }
        return (StatsResult(signals: lineCount, matches: matchCount), isMonitorRunning)
    }

    struct RecentSignalsResult {
        let signals: [SignalInfo]
        let latestISO: String
        let latestSource: String
        let latestPreview: String
    }

    func readRecentSignals() -> RecentSignalsResult {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: Self.home + "/signal_log.jsonl")) else {
            return RecentSignalsResult(signals: [], latestISO: "", latestSource: "", latestPreview: "")
        }
        let lines = data.split(separator: UInt8(ascii: "\n"))
        let recent = lines.suffix(40).reversed().compactMap { line -> SignalInfo? in
            guard let json = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: String] else { return nil }
            let ts = json["timestamp"] ?? ""
            return SignalInfo(source: json["source"] ?? "?", text: String((json["text"] ?? "").prefix(120)), time: formatTimestamp(ts))
        }
        var latestISO = ""
        var latestSource = ""
        var latestPreview = ""
        if let last = lines.last,
           let json = try? JSONSerialization.jsonObject(with: Data(last)) as? [String: String] {
            latestISO = json["timestamp"] ?? ""
            latestSource = json["source"] ?? ""
            latestPreview = String((json["text"] ?? "").prefix(90))
        }
        return RecentSignalsResult(signals: Array(recent), latestISO: latestISO, latestSource: latestSource, latestPreview: latestPreview)
    }

    func readInsights() -> [AnalysisInsight] {
        // Reads ``~/.deja/audit.jsonl`` — the single audit log that replaced
        // the legacy ``analysis_log.jsonl`` / ``integrations.jsonl`` /
        // ``log.md`` triplet. Groups entries by ``cycle`` so the notch
        // Insights panel still renders one row per integrate cycle.
        let path = Self.home + "/audit.jsonl"
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else { return [] }
        let lines = data.split(separator: UInt8(ascii: "\n"))

        // Group by cycle id (newest cycles first). Only consider lines
        // emitted during an integrate cycle — lines without a cycle id
        // are one-off admin writes (startup, manual, onboarding).
        var order: [String] = []
        var byCycle: [String: [[String: Any]]] = [:]
        for raw in lines {
            guard let json = try? JSONSerialization.jsonObject(with: Data(raw)) as? [String: Any] else { continue }
            let cycle = (json["cycle"] as? String) ?? ""
            guard !cycle.isEmpty else { continue }
            if byCycle[cycle] == nil {
                order.append(cycle)
            }
            byCycle[cycle, default: []].append(json)
        }

        let recentCycles = order.suffix(15).reversed()
        return recentCycles.compactMap { cycleId -> AnalysisInsight? in
            guard let entries = byCycle[cycleId], let first = entries.first else { return nil }
            let timeStr = formatTimestamp(first["ts"] as? String ?? "")

            // "Matches" in the old schema were wiki mutations. Pull them
            // out of the audit entries whose action is a wiki op.
            let wikiActions: Set<String> = ["wiki_write", "wiki_delete", "event_create"]
            let matches: [String] = entries.compactMap { e in
                let action = e["action"] as? String ?? ""
                guard wikiActions.contains(action) else { return nil }
                let target = e["target"] as? String ?? ""
                let reason = e["reason"] as? String ?? ""
                return "[\(action)] \(target): \(reason)"
            }

            // goal_action entries surface as "commitments" in the notch
            // panel so they still get visibility.
            let commitments: [String] = entries.compactMap { e in
                let action = e["action"] as? String ?? ""
                guard action == "goal_action" else { return nil }
                let target = e["target"] as? String ?? ""
                let reason = e["reason"] as? String ?? ""
                return "\(target): \(reason)"
            }

            // Task / reminder / waiting ops surface as "facts" so the
            // Insights panel can show the goals churn.
            let goalOps: Set<String> = [
                "task_add", "task_complete", "task_archive",
                "waiting_add", "waiting_resolve", "waiting_archive",
                "reminder_add", "reminder_resolve", "reminder_archive",
            ]
            let facts: [String] = entries.compactMap { e in
                let action = e["action"] as? String ?? ""
                guard goalOps.contains(action) else { return nil }
                return "\(action): \(e["reason"] as? String ?? "")"
            }

            return AnalysisInsight(
                time: timeStr,
                matches: matches,
                facts: facts,
                commitments: commitments,
                proposals: [],
                conversations: [],
                signalCount: entries.count
            )
        }
    }

    private static func isPlaceholder(_ s: String) -> Bool {
        let lower = s.lowercased().trimmingCharacters(in: .whitespaces)
        return lower == "none" || lower == "none detected" || lower == "n/a"
            || lower == "no commitments" || lower == "no conversations"
            || lower.hasPrefix("none ") || lower == ":"
    }

    // MARK: - Buffer Writing

    private func writeJSONBuffer(_ data: Any, to filename: String) {
        let dirPath = Self.home
        try? FileManager.default.createDirectory(
            atPath: dirPath,
            withIntermediateDirectories: true
        )
        let filePath = dirPath + "/" + filename
        let tmpPath = filePath + ".tmp"

        guard let jsonData = try? JSONSerialization.data(
            withJSONObject: data,
            options: [.sortedKeys]
        ) else { return }

        guard FileManager.default.createFile(atPath: tmpPath, contents: jsonData) else { return }

        do {
            if FileManager.default.fileExists(atPath: filePath) {
                _ = try FileManager.default.replaceItemAt(
                    URL(fileURLWithPath: filePath),
                    withItemAt: URL(fileURLWithPath: tmpPath)
                )
            } else {
                try FileManager.default.moveItem(atPath: tmpPath, toPath: filePath)
            }
        } catch {
            try? FileManager.default.removeItem(atPath: tmpPath)
        }
    }
}
