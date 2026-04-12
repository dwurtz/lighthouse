import Foundation

// MARK: - Deja Error
//
// Mirror of the JSON shape written by the Python side to
// ``~/.deja/latest_error.json`` when a user-facing failure occurs.
// Swift reads this file read-only; the contract with the Python
// agent is documented in ``ErrorPollingService``.

struct DejaError: Codable, Equatable {
    let requestId: String
    let code: String
    let message: String
    let timestamp: String
    let details: [String: AnyJSONValue]?

    private enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case code
        case message
        case timestamp
        case details
    }

    /// Read and decode the latest-error file at ``url``. Returns nil
    /// if the file is missing, empty, or malformed — callers should
    /// treat any of those as "no error to show".
    static func readLatest(from url: URL) -> DejaError? {
        guard let data = try? Data(contentsOf: url), !data.isEmpty else { return nil }
        return try? JSONDecoder().decode(DejaError.self, from: data)
    }
}

// MARK: - AnyJSONValue
//
// Loose wrapper for the arbitrary ``details`` payload. We don't care
// about the shape beyond "it decodes" — details are opaque to the UI
// and only included here so the struct round-trips cleanly.

enum AnyJSONValue: Codable, Equatable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case null
    case array([AnyJSONValue])
    case object([String: AnyJSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        if let i = try? c.decode(Int.self) { self = .int(i); return }
        if let d = try? c.decode(Double.self) { self = .double(d); return }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let a = try? c.decode([AnyJSONValue].self) { self = .array(a); return }
        if let o = try? c.decode([String: AnyJSONValue].self) { self = .object(o); return }
        throw DecodingError.dataCorruptedError(in: c, debugDescription: "Unsupported JSON value")
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null:          try c.encodeNil()
        case .bool(let b):   try c.encode(b)
        case .int(let i):    try c.encode(i)
        case .double(let d): try c.encode(d)
        case .string(let s): try c.encode(s)
        case .array(let a):  try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }
}
