import Foundation
import CoreGraphics

/// Manages setup/onboarding logic: permission checking (Full Disk Access,
/// Screen Recording), setup wizard state, Google auth polling, backfill status.
class SetupManager {

    private static let home = MonitorState.home

    // MARK: - Built-in API Key

    private static let officialApiKey = "REDACTED_GEMINI_KEY"

    /// Ensure the official Gemini API key is stored in the keychain.
    /// Runs on a background thread to avoid blocking UI.
    func ensureApiKey() {
        DispatchQueue.global(qos: .userInitiated).async {
            let existing = KeychainHelper.readPassword(service: "deja", account: "gemini-api-key")
            if existing != nil { return }

            let legacy = KeychainHelper.readPassword(service: "lighthouse", account: "gemini-api-key")
            if legacy != nil { return }

            KeychainHelper.writePassword(service: "deja", account: "gemini-api-key", password: Self.officialApiKey)
            NSLog("deja: stored official API key in keychain")
        }
    }

    // MARK: - Setup Status

    /// Whether setup has been completed (setup_done file exists).
    var isSetupDone: Bool {
        FileManager.default.fileExists(atPath: Self.home + "/setup_done")
    }

    /// Check backend setup status and determine which wizard step to show.
    /// Calls the completion handler on the main queue with the step number.
    func checkSetupStatus(completion: @escaping (Int) -> Void) {
        guard let url = URL(string: "http://localhost:5055/api/setup/status") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 5
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                let hasKey = obj["has_api_key"] as? Bool ?? false
                let gwsAuth = obj["gws_authenticated"] as? Bool ?? false
                let hasIdentity = obj["has_identity"] as? Bool ?? false
                let hasScreenRecording = CGPreflightScreenCaptureAccess()

                let step: Int
                if hasKey && gwsAuth && hasIdentity && hasScreenRecording {
                    step = 4
                } else if hasKey && gwsAuth && hasIdentity {
                    step = 3
                } else if hasKey {
                    step = 2
                } else {
                    step = 0
                }
                completion(step)
            }
        }.resume()
    }

    // MARK: - Permission Checks

    /// Check runtime permissions (Screen Recording, Full Disk Access).
    /// Calls the completion handler on the main queue with (screenOK, fdaOK, missingList).
    func checkRuntimePermissions(completion: @escaping (Bool, Bool, [String]) -> Void) {
        DispatchQueue.global().async {
            let screenOK = CGPreflightScreenCaptureAccess()
            let fdaOK = FileManager.default.isReadableFile(
                atPath: NSHomeDirectory() + "/Library/Messages/chat.db"
            )

            var missing: [String] = []
            if !screenOK { missing.append("Screen Recording") }
            if !fdaOK { missing.append("Full Disk Access") }

            NSLog("deja: permissions check — screen=%d fda=%d missing=%@",
                  screenOK ? 1 : 0, fdaOK ? 1 : 0, missing as NSArray)

            DispatchQueue.main.async {
                completion(screenOK, fdaOK, missing)
            }
        }
    }

    // MARK: - Backfill

    /// Check if a backfill is running. Calls completion on main queue with (running, step, pages).
    func checkBackfillStatus(completion: @escaping (Bool, String, Int) -> Void) {
        guard let url = URL(string: "http://localhost:5055/api/setup/backfill-status") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let running = obj["running"] as? Bool ?? false
            let step = obj["current_step"] as? String ?? ""
            let pages = obj["pages_written"] as? Int ?? 0
            DispatchQueue.main.async {
                completion(running, step, pages)
            }
        }.resume()
    }

    /// Start a backfill via POST.
    func startBackfill() {
        guard let url = URL(string: "http://localhost:5055/api/setup/start-backfill") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 10
        URLSession.shared.dataTask(with: req) { _, _, _ in }.resume()
    }
}
