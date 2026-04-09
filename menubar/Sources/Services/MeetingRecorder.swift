import Foundation

// MARK: - Meeting Recorder (spawns DejaRecorder helper)
//
// The actual ScreenCaptureKit capture runs in a SEPARATE binary
// (DejaRecorder) so that importing the framework doesn't
// trigger TCC Screen Recording prompts on every app launch.
// The helper is only spawned when the user clicks Record.

class MeetingRecorder {
    private var process: Process?
    var sessionDir: String = ""
    var isRecording: Bool = false
    var onAutoStop: (() -> Void)?

    private static var recorderPath: String {
        // Bundled inside the app, next to the main executable
        if let execURL = Bundle.main.executableURL {
            let bundled = execURL.deletingLastPathComponent()
                .appendingPathComponent("DejaRecorder").path
            if FileManager.default.fileExists(atPath: bundled) {
                return bundled
            }
        }
        #if DEBUG
        return NSHomeDirectory() + "/projects/deja/menubar/DejaRecorder"
        #else
        fatalError("DejaRecorder not found in app bundle")
        #endif
    }

    func startRecording(sessionId: String, outputDirPath: String) {
        guard !isRecording else { return }
        self.sessionDir = outputDirPath

        // Spawn the recorder on a background queue so it doesn't
        // interfere with the main thread / menu bar status item.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: Self.recorderPath)
            proc.arguments = [outputDirPath]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice

            proc.terminationHandler = { [weak self] process in
                NSLog("deja: recorder exited with code \(process.terminationStatus)")
                DispatchQueue.main.async {
                    if self?.isRecording == true {
                        self?.isRecording = false
                        self?.onAutoStop?()
                    }
                }
            }

            do {
                try proc.run()
                DispatchQueue.main.async {
                    self?.process = proc
                    self?.isRecording = true
                }
                NSLog("deja: recorder started (pid \(proc.processIdentifier), session: \(sessionId))")
            } catch {
                NSLog("deja: recorder spawn failed: \(error)")
            }
        }
    }

    func stopRecording(completion: (() -> Void)? = nil) {
        guard isRecording, let proc = process else {
            completion?()
            return
        }
        isRecording = false

        // Write .stop sentinel — the recorder polls for this
        let stopFile = URL(fileURLWithPath: sessionDir).appendingPathComponent(".stop")
        FileManager.default.createFile(atPath: stopFile.path, contents: nil)

        // Wait on a background thread to avoid blocking the UI
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            proc.waitUntilExit()
            DispatchQueue.main.async {
                self?.process = nil
                NSLog("deja: recorder exited (merge complete)")
                completion?()
            }
        }
    }
}
