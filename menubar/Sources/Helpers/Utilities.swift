import Foundation

// Parse an ISO 8601 timestamp and return a human-readable local time.
// Examples: "2026-04-04T18:15:45.995+00:00" → "11:15" (local) or "3s ago"
// `relative: true` forces the "Ns/Nm/Nh ago" form.
func formatTimestamp(_ iso: String, relative: Bool = false) -> String {
    guard !iso.isEmpty else { return "—" }
    let formatters: [ISO8601DateFormatter] = {
        let f1 = ISO8601DateFormatter()
        f1.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let f2 = ISO8601DateFormatter()
        f2.formatOptions = [.withInternetDateTime]
        return [f1, f2]
    }()
    var date: Date?
    for f in formatters {
        if let d = f.date(from: iso) { date = d; break }
    }
    if date == nil {
        // Try naive (no timezone) ISO: "2026-04-04T18:15:45.995023"
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        df.timeZone = TimeZone.current
        date = df.date(from: iso)
        if date == nil {
            df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            date = df.date(from: iso)
        }
    }
    guard let d = date else { return "—" }
    let delta = Date().timeIntervalSince(d)
    if relative || delta < 3600 {
        let secs = Int(max(0, delta))
        if secs < 10 { return "just now" }
        if secs < 60 { return "\(secs)s ago" }
        let mins = secs / 60
        if mins < 60 { return "\(mins)m ago" }
        let hours = mins / 60
        return "\(hours)h ago"
    }
    let df = DateFormatter()
    df.dateFormat = "HH:mm"
    return df.string(from: d)
}

func formatDuration(_ seconds: TimeInterval) -> String {
    let mins = Int(seconds) / 60
    let secs = Int(seconds) % 60
    return String(format: "%d:%02d", mins, secs)
}

// MARK: - Local API (Unix Domain Socket)

/// Make an HTTP request to the Python backend over the Unix domain socket
/// at ``~/.deja/deja.sock``. The socket is protected by filesystem
/// permissions (owner-only), so no shared secret is needed.
///
/// The completion handler is called on a background queue with the
/// response body (or nil on error) and an optional error.
func localAPICall(
    _ path: String,
    method: String = "GET",
    body: Data? = nil,
    timeoutInterval: TimeInterval = 10,
    completion: @escaping (Data?, Error?) -> Void
) {
    DispatchQueue.global(qos: .userInitiated).async {
        let socketPath = MonitorState.home + "/deja.sock"

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            completion(nil, NSError(domain: "deja.ipc", code: -1, userInfo: [NSLocalizedDescriptionKey: "socket() failed"]))
            return
        }

        // Set send/receive timeout
        var tv = timeval(tv_sec: Int(timeoutInterval), tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &addr.sun_path.0) { ptr in
            socketPath.withCString { cstr in
                _ = strcpy(ptr, cstr)
            }
        }

        let connectResult = withUnsafePointer(to: &addr, { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.connect(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        })
        guard connectResult == 0 else {
            Darwin.close(fd)
            completion(nil, NSError(domain: "deja.ipc", code: -2, userInfo: [NSLocalizedDescriptionKey: "connect() failed: \(String(cString: strerror(errno)))"]))
            return
        }

        // Build raw HTTP/1.1 request
        var http = "\(method) \(path) HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
        if let body = body {
            http += "Content-Type: application/json\r\nContent-Length: \(body.count)\r\n"
        }
        http += "\r\n"

        // Send headers
        let headerBytes = Array(http.utf8)
        headerBytes.withUnsafeBufferPointer { buf in
            _ = Darwin.write(fd, buf.baseAddress!, buf.count)
        }
        // Send body
        if let body = body {
            body.withUnsafeBytes { buf in
                _ = Darwin.write(fd, buf.baseAddress!, body.count)
            }
        }

        // Read full response
        var responseData = Data()
        let bufSize = 65536
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufSize)
        defer { buffer.deallocate() }

        while true {
            let n = Darwin.read(fd, buffer, bufSize)
            if n <= 0 { break }
            responseData.append(buffer, count: n)
        }
        Darwin.close(fd)

        // Split HTTP headers from body at \r\n\r\n
        let separator = Data("\r\n\r\n".utf8)
        if let range = responseData.range(of: separator) {
            let bodyData = responseData.subdata(in: range.upperBound..<responseData.endIndex)
            completion(bodyData, nil)
        } else {
            // No headers found — return raw data
            completion(responseData.isEmpty ? nil : responseData, nil)
        }
    }
}
