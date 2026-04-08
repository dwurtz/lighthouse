import SwiftUI
import AppKit
import Foundation
import CoreGraphics
import ServiceManagement

// MARK: - Monitor State

class MonitorState: ObservableObject {
    @Published var signals: Int = 0
    @Published var matches: Int = 0
    @Published var running: Bool = false
    @Published var recentSignals: [SignalInfo] = []
    @Published var lastSignalISO: String = ""
    @Published var lastSignalSource: String = ""
    @Published var lastSignalPreview: String = ""
    @Published var insights: [AnalysisInsight] = []
    @Published var chatMessages: [ChatMessage] = []
    @Published var chatInput: String = ""
    @Published var chatLoading: Bool = false
    @Published var contactResults: [ContactMatch] = []
    @Published var showContactPicker: Bool = false
    @Published var activeTab: NotchTab = .chat
    @Published var isRecording: Bool = false

    // Permission state — checked on launch and periodically
    @Published var hasScreenRecording: Bool = false
    @Published var hasFullDiskAccess: Bool = false
    @Published var missingPermissions: [String] = []
    @Published var micBusy: Bool = false
    @Published var setupNeeded: Bool = false
    @Published var setupStep: Int = 0

    // Meeting recording state
    @Published var meetingAvailable: Bool = false
    @Published var meetingTitle: String = ""
    @Published var meetingAttendees: [String] = []
    @Published var meetingRecording: Bool = false
    @Published var meetingLinked: Bool = false
    @Published var meetingPaused: Bool = false
    @Published var meetingElapsed: TimeInterval = 0
    @Published var meetingSessionId: String = ""
    @Published var meetingNotes: String = ""
    @Published var meetingTimeRange: String = ""
    @Published var showSettings: Bool = false
    @Published var launchAtLogin: Bool = true
    @Published var backfillRunning: Bool = false
    @Published var backfillStep: String = ""
    @Published var backfillPages: Int = 0
    @Published var meetingProcessing: Bool = false

    // Notifications from agent
    @Published var notificationTitle: String = ""
    @Published var notificationMessage: String = ""
    @Published var showNotification: Bool = false

    private var backfillTimer: Timer?
    private var meetingStartTime: Date?
    private var meetingTimer: Timer?

    private var statsTimer: Timer?
    private var screenshotTimer: Timer?
    private var dbReaderTimer: Timer?
    // MARK: - Extracted services

    private let processManager = BackendProcessManager()
    private let databaseReader = DatabaseReader()
    private let setupManager = SetupManager()
    private let meetingCoordinator = MeetingCoordinator()

    // MARK: - Static paths (used by extracted services)

    static let home = NSHomeDirectory() + "/.deja"

    static var backendPath: String {
        if let resourceURL = Bundle.main.resourceURL {
            let bundled = resourceURL.appendingPathComponent("python-env/bin/python3").path
            if FileManager.default.fileExists(atPath: bundled) {
                return bundled
            }
        }
        return NSHomeDirectory() + "/projects/deja/venv/bin/python3"
    }

    static var isBundledPython: Bool {
        if let resourceURL = Bundle.main.resourceURL {
            return FileManager.default.fileExists(
                atPath: resourceURL.appendingPathComponent("python-env/bin/python3").path
            )
        }
        return false
    }

    static let projectDir = NSHomeDirectory() + "/projects/deja"

    // MARK: - Launch at Login (SMAppService)

    func loadLaunchAtLoginState() {
        let status = SMAppService.mainApp.status
        if status == .notRegistered {
            setLaunchAtLogin(true)
        } else {
            launchAtLogin = status == .enabled
        }
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            launchAtLogin = enabled
        } catch {
            NSLog("deja: launch-at-login toggle failed: \(error)")
            launchAtLogin = SMAppService.mainApp.status == .enabled
        }
    }

    // MARK: - Start / Stop / Restart

    func start() {
        setupNeeded = !setupManager.isSetupDone
        loadLaunchAtLoginState()

        startWeb()

        if !setupNeeded {
            startScreenshotCapture()
            startDatabaseReaders()

            DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
                self?.startMonitor()
            }

            statsTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
                self?.updateStats()
                self?.updateRecentSignals()
                self?.updateInsights()
            }
            updateStats()
            updateRecentSignals()
            updateInsights()
            startMeetingPolling()

            checkBackfillStatus()
            Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] timer in
                guard let self = self else { timer.invalidate(); return }
                if self.backfillRunning {
                    timer.invalidate()
                } else {
                    self.checkBackfillStatus()
                }
            }

            checkRuntimePermissions()
            Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
                self?.checkRuntimePermissions()
            }
        }
    }

    func stop() {
        statsTimer?.invalidate(); statsTimer = nil
        screenshotTimer?.invalidate(); screenshotTimer = nil
        dbReaderTimer?.invalidate(); dbReaderTimer = nil
        processManager.stopAll()
        running = false
    }

    func restart() {
        running = false
        processManager.restartAll { [weak self] in
            self?.running = false
        }
        // restartAll re-starts after 1s delay; update running when monitor launches
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            self?.running = self?.processManager.isMonitorRunning ?? false
        }
    }

    // MARK: - Setup Delegation

    func checkSetupStatus() {
        setupManager.checkSetupStatus { [weak self] step in
            self?.setupStep = step
        }
    }

    func completeSetup() {
        setupNeeded = false
        startScreenshotCapture()
        startDatabaseReaders()

        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            self?.startMonitor()
        }

        statsTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            self?.updateStats()
            self?.updateRecentSignals()
            self?.updateInsights()
        }
        startMeetingPolling()
        checkRuntimePermissions()

        Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
            self?.checkRuntimePermissions()
        }
    }

    func checkRuntimePermissions() {
        setupManager.checkRuntimePermissions { [weak self] screenOK, fdaOK, missing in
            self?.hasScreenRecording = screenOK
            self?.hasFullDiskAccess = fdaOK
            self?.missingPermissions = missing
        }
    }

    // MARK: - Backfill Delegation

    func checkBackfillStatus() {
        setupManager.checkBackfillStatus { [weak self] running, step, pages in
            guard let self = self, running else { return }
            self.backfillRunning = true
            self.backfillStep = step
            self.backfillPages = pages
            self.startBackfillPolling()
        }
    }

    private func startBackfillPolling() {
        backfillTimer?.invalidate()
        backfillTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] timer in
            guard let self = self else { timer.invalidate(); return }
            self.setupManager.checkBackfillStatus { running, step, pages in
                self.backfillStep = step
                self.backfillPages = pages
                if !running {
                    self.backfillRunning = false
                    timer.invalidate()
                }
            }
        }
    }

    func startBackfillAndPoll() {
        setupManager.startBackfill()
        backfillRunning = true
        backfillStep = "Starting..."
        startBackfillPolling()
    }

    // MARK: - Process Delegation

    private func startMonitor() {
        processManager.startMonitor { [weak self] in
            self?.running = false
        }
        running = true
    }

    func startWeb() {
        processManager.startWeb()
    }

    // MARK: - Screenshot Capture

    func startScreenshotCapture() {
        guard screenshotTimer == nil else { return }
        captureScreenshot()
        screenshotTimer = Timer.scheduledTimer(withTimeInterval: 6.0, repeats: true) { [weak self] _ in
            self?.captureScreenshot()
        }
    }

    private func captureScreenshot() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.processManager.captureScreenshot()
        }
    }

    // MARK: - Database Reader Delegation

    private func startDatabaseReaders() {
        readDatabases()
        dbReaderTimer = Timer.scheduledTimer(withTimeInterval: 15.0, repeats: true) { [weak self] _ in
            self?.readDatabases()
        }
    }

    private func readDatabases() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.databaseReader.readAll()
        }
    }

    // MARK: - Diverse signals

    var diverseSignals: [SignalInfo] {
        var result: [SignalInfo] = []
        var screenshotCount = 0
        for sig in recentSignals {
            if sig.source == "screenshot" {
                screenshotCount += 1
                if screenshotCount > 2 { continue }
            }
            result.append(sig)
        }
        return result
    }

    // MARK: - Stats & Insights (delegated to DatabaseReader)

    private func updateStats() {
        let isMonitorRunning = processManager.isMonitorRunning
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let result = self.databaseReader.readStats(isMonitorRunning: isMonitorRunning)
            DispatchQueue.main.async {
                self.signals = result.stats.signals
                self.matches = result.stats.matches
                self.running = result.running
            }
        }
    }

    private func updateInsights() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let parsed = self.databaseReader.readInsights()
            DispatchQueue.main.async { self.insights = parsed }
        }
    }

    private func updateRecentSignals() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let result = self.databaseReader.readRecentSignals()
            DispatchQueue.main.async {
                self.recentSignals = result.signals
                self.lastSignalISO = result.latestISO
                self.lastSignalSource = result.latestSource
                self.lastSignalPreview = result.latestPreview
            }
        }
    }

    // MARK: - Recording (unified)

    func toggleRecording() {
        if meetingRecording {
            stopMeetingRecording()
        } else {
            startMeetingRecording()
        }
    }

    func refreshMicStatus() {}

    func toggleMic() {
        toggleRecording()
    }

    // MARK: - Notifications from agent

    func pollNotifications() {
        let path = Self.home + "/notification.json"
        guard FileManager.default.fileExists(atPath: path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let message = obj["message"] as? String, !message.isEmpty else { return }

        try? FileManager.default.removeItem(atPath: path)

        DispatchQueue.main.async {
            self.notificationTitle = obj["title"] as? String ?? "Déjà"
            self.notificationMessage = message
            self.showNotification = true
            NotificationCenter.default.post(name: .agentNotification, object: nil)

            DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
                self.showNotification = false
                NotificationCenter.default.post(name: .notificationDismissed, object: nil)
            }
        }
    }

    // MARK: - Meeting Polling & Recording (delegated to MeetingCoordinator)

    func startMeetingPolling() {
        Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.refreshMeetingPrompt()
            self?.pollNotifications()
            if self?.meetingRecording == true {
                self?.refreshMeetingStatus()
            }
        }
        meetingCoordinator.onAutoStop = { [weak self] in
            self?.stopMeetingRecording()
        }
    }

    func refreshMeetingPrompt() {
        meetingCoordinator.refreshMeetingPrompt(
            isRecording: meetingRecording,
            wasAvailable: meetingAvailable,
            onUpdate: { [weak self] available, title, attendees, timeRange, eventId, isNew in
                guard let self = self else { return }
                self.meetingAvailable = available
                if available {
                    self.meetingTitle = title
                    self.meetingAttendees = attendees
                    self.meetingTimeRange = timeRange
                    if isNew {
                        NotificationCenter.default.post(name: .meetingDetected, object: nil)
                    }
                } else {
                    self.meetingTitle = ""
                    self.meetingAttendees = []
                }
            },
            onDismiss: { NotificationCenter.default.post(name: .meetingDismissed, object: nil) }
        )
    }

    func refreshMeetingStatus() {
        meetingCoordinator.refreshMeetingStatus { [weak self] elapsed in
            self?.meetingElapsed = elapsed
        }
    }

    func startMeetingRecording() {
        guard !meetingRecording else { return }

        let title = meetingTitle.isEmpty ? "" : meetingTitle
        let attendees = meetingAttendees.isEmpty ? [] : meetingAttendees
        meetingLinked = !title.isEmpty
        meetingNotes = ""

        meetingCoordinator.startRecording(title: title, attendees: attendees) { [weak self] sessionId in
            guard let self = self else { return }
            self.meetingRecording = true
            self.meetingSessionId = sessionId
            self.meetingStartTime = Date()
            self.meetingElapsed = 0

            self.meetingTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
                guard let self = self, let start = self.meetingStartTime else { return }
                self.meetingElapsed = Date().timeIntervalSince(start)
            }
        }
    }

    func unlinkMeeting() {
        meetingLinked = false
        meetingTitle = ""
        meetingAttendees = []
        meetingCoordinator.unlinkMeeting()
    }

    func pauseMeetingRecording() {
        meetingCoordinator.pauseOrResume(isPaused: meetingPaused, sessionId: meetingSessionId)
        meetingPaused = !meetingPaused
    }

    func stopMeetingRecording() {
        guard meetingRecording else { return }

        let notes = meetingNotes
        meetingTimer?.invalidate()
        meetingTimer = nil
        meetingRecording = false
        meetingPaused = false
        meetingProcessing = true
        NotificationCenter.default.post(name: .meetingDetected, object: nil)

        meetingCoordinator.stopRecording(notes: notes) { [weak self] in
            self?.meetingProcessing = false
            self?.meetingAvailable = false
            self?.meetingSessionId = ""
            self?.meetingStartTime = nil
            self?.meetingElapsed = 0
            self?.meetingNotes = ""
            NotificationCenter.default.post(name: .meetingDismissed, object: nil)
        }
    }

    static func formatTimeRange(start: String, end: String) -> String {
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        let outFmt = DateFormatter()
        outFmt.dateFormat = "h:mm a"

        func parse(_ iso: String) -> Date? {
            let clean = String(iso.prefix(19))
            return df.date(from: clean)
        }

        guard let s = parse(start), let e = parse(end) else {
            return ""
        }
        return "\(outFmt.string(from: s)) - \(outFmt.string(from: e))"
    }

    // MARK: - Contacts

    func searchContacts(_ query: String) {
        guard query.count >= 2 else {
            DispatchQueue.main.async { self.contactResults = []; self.showContactPicker = false }
            return
        }
        let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
        guard let url = URL(string: "http://localhost:5055/api/contacts/search?q=\(encoded)&limit=5") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data = data,
                  let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
            let results = arr.map { c in
                ContactMatch(
                    name: c["name"] as? String ?? "",
                    phone: (c["phones"] as? [String])?.first ?? "",
                    email: (c["emails"] as? [String])?.first ?? ""
                )
            }
            DispatchQueue.main.async {
                self?.contactResults = results
                self?.showContactPicker = !results.isEmpty
            }
        }.resume()
    }

    func insertContact(_ contact: ContactMatch) {
        if let atRange = chatInput.range(of: "@", options: .backwards) {
            chatInput = String(chatInput[chatInput.startIndex..<atRange.lowerBound]) + "@\(contact.name) "
        }
        contactResults = []
        showContactPicker = false
    }

    // MARK: - Chat

    func sendChat() {
        let message = chatInput.trimmingCharacters(in: .whitespaces)
        guard !message.isEmpty, !chatLoading else { return }
        chatMessages.append(ChatMessage(role: "user", content: message))
        chatInput = ""
        chatLoading = true
        activeTab = .chat

        let placeholderIdx = chatMessages.count
        chatMessages.append(ChatMessage(role: "agent", content: ""))

        DispatchQueue.global().async { [weak self] in
            guard let url = URL(string: "http://localhost:5055/api/chat") else { return }
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["message": message])
            request.timeoutInterval = 120

            let session = URLSession(configuration: .default)
            let task = session.dataTask(with: request) { data, response, error in
                guard let data = data else {
                    DispatchQueue.main.async {
                        self?.chatLoading = false
                        self?.chatMessages[placeholderIdx] = ChatMessage(role: "agent", content: "Error: \(error?.localizedDescription ?? "no response")")
                    }
                    return
                }

                var fullText = ""
                let text = String(data: data, encoding: .utf8) ?? ""
                for line in text.split(separator: "\n") {
                    let l = String(line)
                    if l.hasPrefix("data: ") {
                        let jsonStr = String(l.dropFirst(6))
                        if let jsonData = jsonStr.data(using: .utf8),
                           let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                            if let chunk = obj["chunk"] as? String {
                                fullText += chunk
                            }
                        }
                    }
                }

                let displayText = fullText.replacingOccurrences(of: "\\[ACTION:[^\\]]*\\]", with: "", options: .regularExpression)

                DispatchQueue.main.async {
                    self?.chatLoading = false
                    self?.chatMessages[placeholderIdx] = ChatMessage(role: "agent", content: displayText.trimmingCharacters(in: .whitespacesAndNewlines))
                }
            }
            task.resume()
        }
    }
}
