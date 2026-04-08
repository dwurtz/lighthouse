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

        let sql = """
            SELECT m.text, m.date, m.is_from_me, h.id as handle_id
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL AND m.text != '' AND m.date > ?1
            ORDER BY m.date DESC LIMIT 50
            """

        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_int64(stmt, 1, cutoffAppleNs)

        var messages: [[String: Any]] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let text: String
            if let cStr = sqlite3_column_text(stmt, 0) {
                text = String(cString: cStr)
            } else { continue }

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
        let path = Self.home + "/analysis_log.jsonl"
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else { return [] }
        let lines = data.split(separator: UInt8(ascii: "\n"))

        return lines.suffix(15).reversed().compactMap { line in
            guard let json = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any] else { return nil }
            let ts = json["timestamp"] as? String ?? ""
            let timeStr = formatTimestamp(ts)

            let matches = (json["matches"] as? [[String: Any]] ?? []).map { m in
                let goal = m["goal"] as? String ?? ""
                let summary = m["signal_summary"] as? String ?? ""
                let conf = m["confidence"] as? String ?? ""
                let reasoning = m["reasoning"] as? String ?? ""
                return "[\(conf)] \(goal): \(summary)" + (reasoning.isEmpty ? "" : " — \(reasoning)")
            }

            let facts = (json["new_facts"] as? [[String: Any]] ?? []).map { f in
                f["fact"] as? String ?? ""
            }.filter { !$0.isEmpty && !Self.isPlaceholder($0) }

            let commitments = (json["commitments"] as? [[String: Any]] ?? []).map { c in
                let who = c["commitment"] as? String ?? ""
                let deadline = c["deadline"] as? String
                return deadline != nil ? "\(who) (by \(deadline!))" : who
            }.filter { !$0.isEmpty && !Self.isPlaceholder($0) }

            let proposals = (json["proposed_goals"] as? [[String: Any]] ?? []).map { p in
                let name = p["name"] as? String ?? ""
                let desc = p["description"] as? String ?? ""
                return "\(name): \(desc)"
            }.filter { !$0.isEmpty && !Self.isPlaceholder($0) }

            let conversations = (json["conversations"] as? [[String: Any]] ?? []).map { c in
                let with_ = c["with"] as? String ?? ""
                let summary = c["summary"] as? String ?? c["topic"] as? String ?? ""
                let underlying = c["underlying_goal"] as? String ?? ""
                var text = "\(with_): \(summary)"
                if !underlying.isEmpty { text += " → \(underlying)" }
                return text
            }.filter { !$0.isEmpty }

            let skipCount = (json["skips"] as? [[String: Any]])?.count ?? 0
            let signalCount = matches.count + skipCount

            return AnalysisInsight(
                time: timeStr,
                matches: matches,
                facts: facts,
                commitments: commitments,
                proposals: proposals,
                conversations: conversations,
                signalCount: signalCount
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
