import AppKit
import SwiftUI

// MARK: - ErrorToast
//
// Red-bordered toast shown when the Python side writes an error to
// ``~/.deja/latest_error.json``. Persists until the user taps ×, so
// the request ID stays copyable for support. The request ID shows 12
// visible chars + ellipsis; full ID copies to the pasteboard.

struct ErrorToast: View {
    let error: DejaError
    var onDismiss: () -> Void

    @State private var showCopied = false

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 13))
                .foregroundColor(.red.opacity(0.9))
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 6) {
                Text(error.message)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(.white)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)

                HStack(spacing: 6) {
                    Text("Error ID:")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.5))
                    Text(truncatedId)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(.white.opacity(0.75))

                    Button(action: copyId) {
                        Image(systemName: "doc.on.doc")
                            .font(.system(size: 10))
                            .foregroundColor(.white.opacity(0.55))
                    }
                    .buttonStyle(.plain)
                    .help("Share this ID if you report the issue")

                    if showCopied {
                        Text("Copied")
                            .font(.system(size: 10))
                            .foregroundColor(.green.opacity(0.85))
                            .transition(.opacity)
                    }

                    Spacer(minLength: 0)

                    Button(action: emailSupport) {
                        HStack(spacing: 3) {
                            Image(systemName: "envelope")
                                .font(.system(size: 9))
                            Text("Email support")
                                .font(.system(size: 10, weight: .medium))
                        }
                        .foregroundColor(.white.opacity(0.85))
                    }
                    .buttonStyle(.plain)
                    .help("Opens a pre-filled email to support@tryDeja.com")
                }
            }

            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundColor(.white.opacity(0.4))
            }
            .buttonStyle(.plain)
            .padding(.top, 2)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(width: 360, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color.black)
                .overlay(
                    RoundedRectangle(cornerRadius: 10)
                        .stroke(Color.red.opacity(0.55), lineWidth: 1)
                )
        )
        .overlay(
            // Red accent stripe along the left edge
            RoundedRectangle(cornerRadius: 2)
                .fill(Color.red.opacity(0.9))
                .frame(width: 3)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, 4)
                .allowsHitTesting(false)
        )
        .shadow(color: .black.opacity(0.35), radius: 10, y: 2)
    }

    private var truncatedId: String {
        let id = error.requestId
        if id.count <= 12 { return id }
        let head = id.prefix(12)
        return "\(head)…"
    }

    private func copyId() {
        copyToClipboard(error.requestId)
        withAnimation(.easeInOut(duration: 0.2)) { showCopied = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            withAnimation(.easeInOut(duration: 0.2)) { showCopied = false }
        }
    }

    private func emailSupport() {
        let id = error.requestId
        let subject = "Deja issue \(id)"
        let body = """
        (Describe what you were doing when this happened.)

        ---
        Request ID: \(id)
        Error code: \(error.code)
        Message: \(error.message)
        """
        let allowed = CharacterSet.urlQueryAllowed
        let encSubject = subject.addingPercentEncoding(withAllowedCharacters: allowed) ?? ""
        let encBody = body.addingPercentEncoding(withAllowedCharacters: allowed) ?? ""
        let urlStr = "mailto:support@tryDeja.com?subject=\(encSubject)&body=\(encBody)"
        if let url = URL(string: urlStr) {
            NSWorkspace.shared.open(url)
        }
    }
}

// MARK: - Clipboard helper

func copyToClipboard(_ s: String) {
    let pb = NSPasteboard.general
    pb.clearContents()
    pb.setString(s, forType: .string)
}
