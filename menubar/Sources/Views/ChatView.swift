import SwiftUI

// MARK: - Chat Tab

struct ChatView: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if monitor.chatMessages.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "bubble.left.and.bubble.right")
                        .font(.system(size: 24))
                        .foregroundColor(.white.opacity(0.15))
                    Text("Ask your agent anything")
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.3))
                    Text("It knows your goals, signals, and memory")
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.2))
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 40)
            } else {
                ForEach(monitor.chatMessages.suffix(10), id: \.id) { msg in
                    if msg.role == "user" {
                        HStack {
                            Spacer()
                            Text(msg.content)
                                .font(.system(size: 12))
                                .foregroundColor(.white)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .background(Color.blue.opacity(0.4))
                                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        }
                    } else {
                        Text(msg.content)
                            .font(.system(size: 12))
                            .foregroundColor(.white.opacity(0.85))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(Color.white.opacity(0.06))
                            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }
}

// MARK: - Chat Input Bar

struct ChatInputBar: View {
    @ObservedObject var monitor: MonitorState

    var body: some View {
        VStack(spacing: 0) {
            // Contact autocomplete dropdown
            if monitor.showContactPicker {
                VStack(spacing: 0) {
                    ForEach(monitor.contactResults) { contact in
                        Button(action: { monitor.insertContact(contact) }) {
                            HStack(spacing: 8) {
                                Image(systemName: "person.circle.fill")
                                    .font(.system(size: 14))
                                    .foregroundColor(.blue.opacity(0.6))
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(contact.name)
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundColor(.white.opacity(0.9))
                                    if !contact.phone.isEmpty || !contact.email.isEmpty {
                                        Text(contact.phone.isEmpty ? contact.email : contact.phone)
                                            .font(.system(size: 9))
                                            .foregroundColor(.white.opacity(0.3))
                                    }
                                }
                                Spacer()
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                        }
                        .buttonStyle(.plain)
                        Divider().background(Color.white.opacity(0.05))
                    }
                }
                .background(Color(white: 0.1))
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .padding(.horizontal, 12)
                .padding(.bottom, 4)
            }

            // Input bar
            HStack(spacing: 8) {
                TextField("Message (use @ to mention)...", text: $monitor.chatInput)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12))
                    .foregroundColor(.white)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(Color.white.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .onSubmit { monitor.sendChat() }
                    .onChange(of: monitor.chatInput) { newValue in
                        // Detect @query for contact autocomplete
                        if let atRange = newValue.range(of: "@", options: .backwards) {
                            let afterAt = String(newValue[atRange.upperBound...])
                            if !afterAt.contains(" ") && afterAt.count >= 2 {
                                monitor.searchContacts(afterAt)
                            } else if afterAt.isEmpty {
                                monitor.searchContacts("")
                            } else {
                                monitor.showContactPicker = false
                            }
                        } else {
                            monitor.showContactPicker = false
                        }
                    }

                if monitor.chatLoading {
                    ProgressView()
                        .scaleEffect(0.6)
                        .frame(width: 28, height: 28)
                } else {
                    Button(action: { monitor.sendChat() }) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 22))
                            .foregroundColor(monitor.chatInput.trimmingCharacters(in: .whitespaces).isEmpty ? .white.opacity(0.15) : .blue)
                    }
                    .buttonStyle(.plain)
                    .disabled(monitor.chatInput.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .background(Color(white: 0.05))
    }
}
