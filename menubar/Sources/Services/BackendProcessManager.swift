import Foundation
import CoreGraphics
import AppKit

/// Manages the Python monitor and web backend subprocesses,
/// including starting, stopping, health-checking, and crash restart.
class BackendProcessManager {

    private var monitorProcess: Process?
    private var webProcess: Process?

    /// Whether the monitor process is currently running.
    var isMonitorRunning: Bool {
        monitorProcess?.isRunning ?? false
    }

    /// Path to the Unix domain socket used by the Python backend.
    /// Filesystem permissions on ~/.deja/ (0o700) serve as the auth boundary.
    static let socketPath = MonitorState.home + "/deja.sock"

    // MARK: - Environment

    private func makeEnv() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        if MonitorState.isBundledPython {
            if let resourceURL = Bundle.main.resourceURL {
                env["PYTHONPATH"] = resourceURL.appendingPathComponent("python-env/src").path
            }
        } else {
            env["PYTHONPATH"] = MonitorState.projectDir + "/src"
        }
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if env["GEMINI_API_KEY"] == nil, let key = readKeyFromEnv() { env["GEMINI_API_KEY"] = key }
        env["__CFBundleIdentifier"] = "com.deja.app"

        // CRITICAL: do not let the bundled Python write .pyc files to
        // __pycache__/ directories inside the .app bundle. Those files
        // would invalidate the bundle's code-signing seal at runtime,
        // causing Gatekeeper to reject every subsequent LaunchServices
        // launch with -600 ("sealed resource is missing or invalid").
        // We pre-compile every .py during bundle-python.sh so the .pyc
        // files are part of the seal — but this env var ensures Python
        // never writes new ones at runtime, even if it imports a module
        // that compileall missed.
        if MonitorState.isBundledPython {
            env["PYTHONDONTWRITEBYTECODE"] = "1"
        }

        return env
    }

    private func readKeyFromEnv() -> String? {
        for path in [NSHomeDirectory() + "/.zshrc", NSHomeDirectory() + "/.zprofile", NSHomeDirectory() + "/.bash_profile"] {
            if let content = try? String(contentsOfFile: path, encoding: .utf8) {
                for line in content.split(separator: "\n") {
                    let t = line.trimmingCharacters(in: .whitespaces)
                    if t.hasPrefix("export GEMINI_API_KEY=") {
                        return t.replacingOccurrences(of: "export GEMINI_API_KEY=", with: "").trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                    }
                }
            }
        }
        return nil
    }

    // MARK: - Monitor Process

    /// Start the Python monitor subprocess. Calls `onTermination` on the main queue when it exits.
    func startMonitor(onTermination: @escaping () -> Void) {
        guard monitorProcess == nil || !monitorProcess!.isRunning else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: MonitorState.backendPath)
        proc.arguments = ["-m", "deja", "monitor"]
        if !MonitorState.isBundledPython {
            proc.currentDirectoryURL = URL(fileURLWithPath: MonitorState.projectDir)
        }
        proc.environment = makeEnv()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        proc.terminationHandler = { _ in
            DispatchQueue.main.async { onTermination() }
        }
        do {
            try proc.run()
            monitorProcess = proc
        } catch {
            print("Monitor start failed: \(error)")
        }
    }

    // MARK: - Web Process

    func startWeb() {
        guard webProcess == nil || !webProcess!.isRunning else { return }

        // Remove stale socket from a previous run
        try? FileManager.default.removeItem(atPath: Self.socketPath)

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: MonitorState.backendPath)
        proc.arguments = ["-m", "deja", "web"]
        if !MonitorState.isBundledPython {
            proc.currentDirectoryURL = URL(fileURLWithPath: MonitorState.projectDir)
        }
        proc.environment = makeEnv()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            webProcess = proc
        } catch {
            print("Web start failed: \(error)")
        }
    }

    // MARK: - Screenshot Capture

    private var screenCaptureAttempted = false

    /// Capture a screenshot to ~/.deja/latest_screen.png. Runs synchronously on the calling thread.
    func captureScreenshot() {
        if screenCaptureAttempted {
            guard CGPreflightScreenCaptureAccess() else { return }
        }
        screenCaptureAttempted = true

        let home = MonitorState.home
        let screenshotPath = home + "/latest_screen.png"
        let timestampPath = home + "/latest_screen_ts.txt"

        try? FileManager.default.createDirectory(
            atPath: home,
            withIntermediateDirectories: true
        )

        // Capture all connected displays. Each -D flag captures a
        // specific display; omitting -D captures only the main one.
        // We capture each display to a temp file, then keep the set
        // for the Python backend to analyze.
        let screens = NSScreen.screens
        var capturedPaths: [String] = []

        for (i, _) in screens.enumerated() {
            let displayNum = i + 1  // screencapture uses 1-based display numbers
            let path = home + "/screen_\(displayNum).png"
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            proc.arguments = ["-x", "-D", "\(displayNum)", path]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice
            do {
                try proc.run()
                proc.waitUntilExit()
                if proc.terminationStatus == 0 {
                    capturedPaths.append(path)
                }
            } catch {
                continue
            }
        }

        // Copy the primary display capture as latest_screen.png for
        // backward compatibility with the Python vision pipeline.
        if let primary = capturedPaths.first {
            try? FileManager.default.removeItem(atPath: screenshotPath)
            try? FileManager.default.copyItem(atPath: primary, toPath: screenshotPath)
        }

        guard FileManager.default.fileExists(atPath: screenshotPath),
              (try? FileManager.default.attributesOfItem(atPath: screenshotPath)[.size] as? Int) ?? 0 > 1024
        else { return }

        let ts = String(format: "%.3f", Date().timeIntervalSince1970)
        try? ts.write(toFile: timestampPath, atomically: true, encoding: .utf8)
    }

    // MARK: - Lifecycle

    func stopAll() {
        monitorProcess?.terminate()
        monitorProcess = nil
        webProcess?.terminate()
        webProcess = nil

        // Clean up socket file
        try? FileManager.default.removeItem(atPath: Self.socketPath)
    }

    func restartAll(onMonitorTermination: @escaping () -> Void) {
        monitorProcess?.terminate()
        monitorProcess = nil
        webProcess?.terminate()
        webProcess = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startMonitor(onTermination: onMonitorTermination)
            self?.startWeb()
        }
    }
}
