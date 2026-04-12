import SwiftUI

// MARK: - Container: pill + (optional) expanded panel
//
// Laid out inside ``VoicePillWindow``'s fixed-height content view.
// The window is always ``expandedSize`` tall internally; we just
// render the pill at the bottom and optionally a panel above it,
// and the NSWindow frame animates to the correct height so the
// unused area is clipped out.
//
// Hierarchy (bottom-anchored):
//
//     ┌────────────────────────────────┐
//     │                                │
//     │         ExpandedNotchPanel     │   (only when pillExpanded)
//     │                                │
//     ├────────────────────────────────┤
//     │          VoicePillView         │   (always)
//     └────────────────────────────────┘

struct VoicePillContainer: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(spacing: 0) {
            // Error toast pins above everything else so it can't be
            // hidden by an expanded panel. It auto-dismisses after
            // 8s via the timer in MonitorState; the × button calls
            // dismissCurrentError() which also removes the file.
            if let err = monitor.currentError {
                ErrorToast(error: err) {
                    monitor.dismissCurrentError()
                }
                .padding(.bottom, 6)
                .transition(.asymmetric(
                    insertion: .opacity.combined(with: .move(edge: .top)),
                    removal: .opacity
                ))
            }

            if monitor.pillExpanded {
                ExpandedNotchPanel(monitor: monitor)
                    .transition(.asymmetric(
                        insertion: .opacity.combined(with: .move(edge: .bottom)),
                        removal: .opacity
                    ))
            }
            VoicePillView(monitor: monitor)
                .frame(width: 400, height: 56)
        }
        .animation(.spring(response: 0.32, dampingFraction: 0.85), value: monitor.pillExpanded)
        .animation(.easeInOut(duration: 0.25), value: monitor.currentError)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
    }
}

// MARK: - Expanded panel

/// The full command-center view hosted above the pill when the notch
/// is expanded. Shares all its content with what used to live in the
/// tray popover — status header, briefing, activity feed, command
/// input — but lays an optional classification banner across the top
/// that shows the most recent voice/command response with its type
/// tag (``[query]``, ``[action]``, etc.) so the user can always see
/// how the classifier interpreted what they just said.
struct ExpandedNotchPanel: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(spacing: 0) {
            if !monitor.lastResponseMessage.isEmpty {
                ResponseBanner(monitor: monitor)
                Divider().background(Color.white.opacity(0.08))
            }

            PopoverContentView(monitor: monitor)
                .frame(width: 460, height: monitor.lastResponseMessage.isEmpty ? 584 : 440)
        }
        .frame(width: 460)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color.black)
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .stroke(Color.white.opacity(0.12), lineWidth: 1)
                )
        )
        .padding(.horizontal, 6)
        .padding(.bottom, 4)
        // Clicking anywhere inside the expanded panel counts as
        // engagement — cancels any in-flight auto-collapse timer.
        .contentShape(Rectangle())
        .onHover { hovering in
            if hovering { monitor.markEngagement() }
        }
    }
}

// MARK: - Classification banner

/// Top strip of the expanded panel showing the classification tag +
/// the last confirmation or query answer. For queries this renders
/// the full markdown body (up to several paragraphs); for short
/// acknowledgements it's a single line with a dismiss button.
private struct ResponseBanner: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("[\(monitor.lastResponseType.isEmpty ? "response" : monitor.lastResponseType)]")
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .foregroundColor(tagColor)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(
                        RoundedRectangle(cornerRadius: 4)
                            .fill(tagColor.opacity(0.12))
                            .overlay(
                                RoundedRectangle(cornerRadius: 4)
                                    .stroke(tagColor.opacity(0.35), lineWidth: 0.8)
                            )
                    )
                Spacer()
                Button(action: { monitor.setPillExpanded(false) }) {
                    Image(systemName: "xmark")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundColor(.white.opacity(0.4))
                        .frame(width: 16, height: 16)
                }
                .buttonStyle(.plain)
            }

            if monitor.lastResponseIsQuery {
                ScrollView {
                    Text(monitor.lastResponseMessage)
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.88))
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 180)
            } else {
                Text(monitor.lastResponseMessage)
                    .font(.system(size: 12))
                    .foregroundColor(.white.opacity(0.88))
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(Color.white.opacity(0.03))
    }

    private var tagColor: Color {
        switch monitor.lastResponseType {
        case "query":      return .cyan
        case "action":     return .green
        case "goal":       return .orange
        case "automation": return .purple
        case "context":    return .blue
        default:           return .white.opacity(0.6)
        }
    }
}
