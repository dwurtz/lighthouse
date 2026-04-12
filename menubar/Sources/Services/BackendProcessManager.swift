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

        // Write per-display AX sidecars AT CAPTURE TIME (not later in
        // Python). The window list is a point-in-time truth about what
        // was on screen at the moment screencapture fired. Reading it
        // seconds later in Python means focus may have changed — which
        // is exactly how the Rob/HealthSpanMD conversation kept getting
        // mislabeled as "cmux" when screen_1 was actually Messages.
        writePerDisplayAXSidecars(screens: screens, home: home)
    }

    /// For each NSScreen, identify the frontmost app/window whose
    /// bounds fall mostly inside that screen's frame, and write a
    /// ``screen_<N>_ax.json`` sidecar with ``{app, window_title}``.
    ///
    /// Uses CGWindowListCopyWindowInfo (requires Screen Recording
    /// permission — we already have it; the screencapture above would
    /// fail without it). Filters to layer=0 (normal app windows) and
    /// skips windows owned by the screenshot process itself.
    private func writePerDisplayAXSidecars(screens: [NSScreen], home: String) {
        let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let cfList = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) else { return }
        let windows = cfList as NSArray

        // Convert NSScreen frames (bottom-left origin, all-screens coord
        // space) to the CG window coordinate system (top-left origin,
        // same all-screens space). CG uses an inverted Y relative to
        // the primary screen's top.
        guard let mainScreen = NSScreen.screens.first else { return }
        let mainHeight = mainScreen.frame.height

        for (i, screen) in screens.enumerated() {
            let displayNum = i + 1
            let frame = screen.frame
            // Convert to CG coords: top = mainHeight - (frame.minY + frame.height)
            let cgTop = mainHeight - (frame.origin.y + frame.height)
            let cgFrame = CGRect(
                x: frame.origin.x,
                y: cgTop,
                width: frame.width,
                height: frame.height
            )

            // Walk windows front-to-back (CGWindowList is ordered that
            // way). Pick the first non-trivial window that overlaps
            // this display meaningfully (>50% of its area on-screen).
            var chosenApp: String? = nil
            var chosenTitle: String? = nil
            for case let info as NSDictionary in windows {
                guard let layer = info[kCGWindowLayer as String] as? Int,
                      layer == 0 else { continue }
                guard let boundsDict = info[kCGWindowBounds as String] as? NSDictionary,
                      let winBounds = CGRect(dictionaryRepresentation: boundsDict as CFDictionary)
                else { continue }
                // Skip minuscule windows (menubar extras, widgets)
                if winBounds.width < 200 || winBounds.height < 100 { continue }
                // Skip our own app's windows
                if let owner = info[kCGWindowOwnerName as String] as? String,
                   owner == "Deja" { continue }
                // Require most of the window to be on this display
                let intersection = winBounds.intersection(cgFrame)
                let winArea = winBounds.width * winBounds.height
                let overlapArea = intersection.width * intersection.height
                if winArea <= 0 || overlapArea / winArea < 0.5 { continue }

                chosenApp = info[kCGWindowOwnerName as String] as? String
                chosenTitle = info[kCGWindowName as String] as? String
                break
            }

            var payload: [String: Any] = [:]
            if let app = chosenApp, !app.isEmpty { payload["app"] = app }
            if let title = chosenTitle, !title.isEmpty { payload["window_title"] = title }

            let sidecarPath = home + "/screen_\(displayNum)_ax.json"
            if payload.isEmpty {
                // No identifiable window — remove stale sidecar if any
                try? FileManager.default.removeItem(atPath: sidecarPath)
                continue
            }
            if let data = try? JSONSerialization.data(withJSONObject: payload, options: []) {
                try? data.write(to: URL(fileURLWithPath: sidecarPath), options: .atomic)
            }
        }
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
