import AppKit
import Foundation
import ApplicationServices

/// Captures the *content* of the focused text field in any app via the
/// macOS Accessibility API and emits one record per "finished thought"
/// (user pauses ≥ 2s after editing) to `~/.deja/typed_content.jsonl`.
///
/// This is the most direct signal we have of what the user is working on:
/// what they're actually composing, searching, or writing — mails,
/// Slack messages, code, notes, chat prompts, etc.
///
/// ## Privacy
/// This captures every text field the user focuses on while typing,
/// EXCEPT:
///   * Secure text fields (password inputs) — detected via
///     kAXRoleDescriptionAttribute and kAXRoleAttribute containing
///     "SecureTextField" / "secure".
///   * When the system-wide keystroke monitor reports the user has
///     been idle > 3 seconds (we only peek during active typing).
///
/// Content is stored locally under `~/.deja/typed_content.jsonl` and
/// ingested into the same `observations.jsonl` pipeline as every other
/// signal. Requires Accessibility permission (already granted for the
/// hotkey + KeystrokeMonitor) — NO new TCC prompt.
///
/// ## Debounce strategy
///   1. Timer ticks every 2s.
///   2. Skip tick if KeystrokeMonitor.idleSeconds >= 3 (user not typing).
///   3. Snapshot focused element's AXValue.
///   4. If equal to last emitted value, skip.
///   5. If differs only by <= 1 trailing char (still mid-sentence), skip.
///   6. Otherwise, require that the previous snapshot is *stable* — i.e.
///      the current value has been unchanged across two consecutive
///      ticks (~2s pause). Only then emit.
///
/// Net effect: we emit roughly one record per paragraph / finished
/// message, not per keystroke.
class TypedContentMonitor {

    private weak var keystrokeMonitor: KeystrokeMonitor?
    private var timer: DispatchSourceTimer?
    private let queue = DispatchQueue(label: "com.deja.typed-content", qos: .utility)

    /// Last value we emitted to the jsonl — used to avoid duplicates.
    private var lastEmittedText: String = ""
    /// Candidate that we saw on the previous tick but haven't emitted
    /// yet (waiting for stability across ticks = "finished thought").
    private var pendingText: String = ""
    private var pendingTickCount: Int = 0

    /// De-noise AX errors: only log once per (app, role) pair.
    private var loggedErrorKeys: Set<String> = []

    /// Identity of the last focused element so a focus change forces
    /// a fresh capture cycle instead of comparing text across apps.
    private var lastFocusSignature: String = ""

    init(keystrokeMonitor: KeystrokeMonitor) {
        self.keystrokeMonitor = keystrokeMonitor
    }

    func start() {
        guard timer == nil else { return }
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 2.0, repeating: 2.0)
        t.setEventHandler { [weak self] in
            self?.tick()
        }
        t.resume()
        timer = t
        NSLog("deja: TypedContentMonitor started")
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    // MARK: - Tick

    private func tick() {
        // Only peek while user was recently typing — saves CPU and
        // avoids capturing idle static screens.
        let idle = keystrokeMonitor?.idleSeconds ?? .infinity
        guard idle < 3.0 else { return }

        guard let snap = captureFocusedSnapshot() else { return }
        if snap.isSecure { return }

        let sig = snap.focusSignature
        // Focus changed mid-cycle — reset debouncer so we don't emit
        // stale text from the previous element.
        if sig != lastFocusSignature {
            lastFocusSignature = sig
            pendingText = snap.text
            pendingTickCount = 1
            return
        }

        let current = snap.text

        // Unchanged OR only differs by <= 1 trailing char (still typing)
        if current == lastEmittedText { return }
        if differsByAtMostOneTrailingChar(current, lastEmittedText) { return }

        if current == pendingText {
            // Stable across at least one tick (~2s pause) — emit.
            pendingTickCount += 1
            if pendingTickCount >= 2 {
                emit(snap)
                lastEmittedText = current
                pendingTickCount = 0
            }
        } else {
            // Still actively changing; wait for a pause.
            pendingText = current
            pendingTickCount = 1
        }
    }

    // MARK: - AX snapshot

    private struct Snapshot {
        let text: String
        let app: String
        let windowTitle: String
        let role: String
        let isSecure: Bool
        let charCount: Int
        var focusSignature: String { "\(app)|\(role)|\(windowTitle)" }
    }

    private func captureFocusedSnapshot() -> Snapshot? {
        let system = AXUIElementCreateSystemWide()

        var focusedRef: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(
            system,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        )
        if err != .success { return nil }
        guard let focusedRaw = focusedRef else { return nil }
        // AXUIElement is a CF type; this cast is the standard pattern.
        let focused = focusedRaw as! AXUIElement

        let role = axString(focused, kAXRoleAttribute) ?? ""
        let roleDesc = axString(focused, kAXRoleDescriptionAttribute) ?? ""

        // Only care about text-holding roles. This also filters away
        // buttons, menus, etc. that happen to be "focused".
        let textyRoles: Set<String> = [
            "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField",
        ]
        let looksTexty = textyRoles.contains(role)
            || roleDesc.lowercased().contains("text")
            || roleDesc.lowercased().contains("search")
        if !looksTexty { return nil }

        let isSecure = role.lowercased().contains("secure")
            || roleDesc.lowercased().contains("secure")

        // Read value. Many Electron apps return empty / unsupported.
        var value = axString(focused, kAXValueAttribute) ?? ""
        // Trim huge buffers — we don't need code editor full-file values.
        if value.count > 8000 {
            value = String(value.suffix(8000))
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return nil }

        // Owner app + window title via NSWorkspace (cheap, no AX round-trip)
        let front = NSWorkspace.shared.frontmostApplication
        let appName = front?.localizedName ?? "Unknown"
        let windowTitle = frontWindowTitle(appName: appName)

        return Snapshot(
            text: value,
            app: appName,
            windowTitle: windowTitle,
            role: role.isEmpty ? (roleDesc.isEmpty ? "AXUnknown" : roleDesc) : role,
            isSecure: isSecure,
            charCount: value.count
        )
    }

    private func frontWindowTitle(appName: String) -> String {
        // Pull the focused window's title via AX instead of CGWindowList.
        // CGWindowListCopyWindowInfo re-triggers the Screen Recording TCC
        // prompt on macOS 15+ when the cert+identifier combo doesn't
        // exactly match an existing grant — and this monitor runs every
        // 2s while typing, which spams the dialog. AX is free here since
        // we already require Accessibility for AXValue capture.
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else { return "" }
        let app = AXUIElementCreateApplication(pid)
        var windowRef: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(
            app,
            kAXFocusedWindowAttribute as CFString,
            &windowRef
        )
        if err != .success { return "" }
        guard let raw = windowRef else { return "" }
        let window = raw as! AXUIElement
        return axString(window, kAXTitleAttribute) ?? ""
    }

    private func axString(_ el: AXUIElement, _ attr: String) -> String? {
        var ref: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(el, attr as CFString, &ref)
        if err != .success {
            return nil
        }
        return ref as? String
    }

    private func differsByAtMostOneTrailingChar(_ a: String, _ b: String) -> Bool {
        // If they're identical, caller already skipped; treat as "yes".
        if a == b { return true }
        // Absolute length difference must be ≤ 1.
        let diff = abs(a.count - b.count)
        if diff > 1 { return false }
        let shorter = a.count < b.count ? a : b
        let longer = a.count < b.count ? b : a
        // longer must start with shorter (trailing append of one char).
        return longer.hasPrefix(shorter)
    }

    // MARK: - Emit

    private func emit(_ snap: Snapshot) {
        let home = MonitorState.home
        let path = home + "/typed_content.jsonl"
        try? FileManager.default.createDirectory(
            atPath: home, withIntermediateDirectories: true
        )

        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime]
        let record: [String: Any] = [
            "timestamp": iso.string(from: Date()),
            "app": snap.app,
            "window_title": snap.windowTitle,
            "element_role": snap.role,
            "text": snap.text,
            "char_count": snap.charCount,
        ]

        guard let data = try? JSONSerialization.data(withJSONObject: record, options: []) else {
            let key = "\(snap.app)|\(snap.role)"
            if !loggedErrorKeys.contains(key) {
                loggedErrorKeys.insert(key)
                NSLog("deja: TypedContentMonitor — JSON encode failed for %{public}@", key)
            }
            return
        }

        guard let line = String(data: data, encoding: .utf8) else { return }
        let full = line + "\n"

        if let handle = FileHandle(forWritingAtPath: path) {
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: Data(full.utf8))
            try? handle.close()
        } else {
            try? Data(full.utf8).write(to: URL(fileURLWithPath: path), options: .atomic)
        }
    }
}
