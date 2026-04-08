import SwiftUI

// MARK: - Agent Notification Bubble

struct NotificationBubbleView: View {
    @ObservedObject var monitor: MonitorState
    var onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "bell.fill")
                .font(.system(size: 11))
                .foregroundColor(.yellow.opacity(0.8))
            VStack(alignment: .leading, spacing: 2) {
                if monitor.notificationTitle != "Déjà" {
                    Text(monitor.notificationTitle)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundColor(.white)
                        .lineLimit(1)
                }
                Text(monitor.notificationMessage)
                    .font(.system(size: 12))
                    .foregroundColor(.white.opacity(0.75))
                    .lineLimit(3)
            }
            Spacer()
            Button(action: { onDismiss() }) {
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundColor(.white.opacity(0.25))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(width: 320)
        .background(Color.black)
    }
}
