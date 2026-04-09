import Foundation
import CoreGraphics

/// Manages setup/onboarding logic: permission checking, setup wizard state,
/// Google auth polling, backfill status.
class SetupManager {

    private static let home = MonitorState.home

    // MARK: - Setup Status

    /// Whether setup has been completed (setup_done file exists).
    var isSetupDone: Bool {
        FileManager.default.fileExists(atPath: Self.home + "/setup_done")
    }

    /// Check backend setup status and determine which wizard step to show.
    func checkSetupStatus(completion: @escaping (Int) -> Void) {
        localAPICall("/api/setup/status", timeoutInterval: 5) { data, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                let gwsAuth = obj["gws_authenticated"] as? Bool ?? false
                let hasIdentity = obj["has_identity"] as? Bool ?? false
                let hasScreenRecording = CGPreflightScreenCaptureAccess()

                let step: Int
                if gwsAuth && hasIdentity && hasScreenRecording {
                    step = 3  // done
                } else if gwsAuth && hasIdentity {
                    step = 2  // permissions
                } else {
                    step = 0  // sign in
                }
                completion(step)
            }
        }
    }

    // MARK: - Permission Checks

    func checkRuntimePermissions(completion: @escaping (Bool, Bool, [String]) -> Void) {
        DispatchQueue.global(qos: .utility).async {
            let screenOK = CGPreflightScreenCaptureAccess()
            let fdaOK = FileManager.default.isReadableFile(
                atPath: NSHomeDirectory() + "/Library/Messages/chat.db"
            )

            var missing: [String] = []
            if !screenOK { missing.append("Screen Recording") }
            if !fdaOK { missing.append("Full Disk Access") }

            DispatchQueue.main.async {
                completion(screenOK, fdaOK, missing)
            }
        }
    }

    // MARK: - Backfill

    func checkBackfillStatus(completion: @escaping (Bool, String, Int) -> Void) {
        localAPICall("/api/setup/backfill-status", timeoutInterval: 3) { data, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let running = obj["running"] as? Bool ?? false
            let step = obj["current_step"] as? String ?? ""
            let pages = obj["pages_written"] as? Int ?? 0
            DispatchQueue.main.async {
                completion(running, step, pages)
            }
        }
    }

    func startBackfill() {
        localAPICall("/api/setup/start-backfill", method: "POST", timeoutInterval: 10) { _, _ in }
    }
}
