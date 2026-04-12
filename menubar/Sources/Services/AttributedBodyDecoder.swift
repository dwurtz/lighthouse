// AttributedBodyDecoder — pulls plain text out of the `attributedBody`
// BLOB column in chat.db (iMessage).
//
// WHY THIS EXISTS
// ---------------
// Modern macOS stores many Messages rows with `text IS NULL` and the
// real content packed into `attributedBody` — an NSAttributedString
// archived in the legacy NeXTSTEP *typedstream* format. Our previous
// SQL filter `m.text != ''` silently dropped those rows, which meant
// every message David sent today (and some inbound ones too) never
// reached the observations pipeline. See the `2026-04-11` carpool
// incident: integrate kept reasoning off an hours-old message because
// the replies stuck on the iPhone hadn't been surfaced by the reader.
//
// WHY NOT NSKeyedUnarchiver / NSUnarchiver
// ----------------------------------------
// chat.db uses the pre-keyed `typedstream` format (magic header
// `\x04\x0bstreamtyped\x81\xe8\x03`). NSKeyedUnarchiver cannot read
// it. NSUnarchiver CAN but is deprecated + Obj-C-only and awkward to
// bridge into Swift. Parsing the handful of bytes we actually need is
// simpler and fully testable.
//
// FORMAT (enough of it to extract the primary string)
// ----------------------------------------------------
// After the class declarations, the NSAttributedString's backing
// NSString appears as:
//
//     "NSString" <class-ref bytes> 0x01 0x2B <length> <UTF-8 bytes>
//
// where 0x01 0x2B is the typedstream type marker for `+` (char array),
// and `<length>` is a variable-width little-endian integer:
//   - 0x00..0x80  → value is the byte itself
//   - 0x81 XX YY  → value is uint16 little-endian (XX | YY<<8)
//   - 0x82 XX YY ZZ WW → value is uint32 little-endian
//
// The FIRST string after the NSString marker is always the message
// body. Later strings in the blob are NSDictionary keys like
// `__kIMMessagePartAttributeName` (encoded as raw length-prefixed
// strings with no `01 2B` type marker), which this decoder correctly
// ignores because it searches specifically for the `01 2B` marker.

import Foundation

enum AttributedBodyDecoder {
    /// Decode the primary UTF-8 string out of a chat.db `attributedBody`
    /// typedstream blob. Returns nil if the blob doesn't contain the
    /// expected NSString + `\x01+` type marker.
    static func extractString(from data: Data) -> String? {
        let nsStringMarker = Data("NSString".utf8)
        let charArrayMarker = Data([0x01, 0x2B])

        guard let nsStringRange = data.range(of: nsStringMarker) else {
            return nil
        }
        guard let typeMarkerRange = data[nsStringRange.upperBound...].range(of: charArrayMarker) else {
            return nil
        }

        var cursor = typeMarkerRange.upperBound
        guard cursor < data.endIndex else { return nil }

        let lenHead = data[cursor]
        cursor = data.index(after: cursor)

        let length: Int
        if lenHead < 0x81 {
            length = Int(lenHead)
        } else if lenHead == 0x81 {
            guard data.index(cursor, offsetBy: 2, limitedBy: data.endIndex) != nil else {
                return nil
            }
            let lo = Int(data[cursor])
            let hi = Int(data[data.index(after: cursor)])
            length = lo | (hi << 8)
            cursor = data.index(cursor, offsetBy: 2)
        } else if lenHead == 0x82 {
            guard data.index(cursor, offsetBy: 4, limitedBy: data.endIndex) != nil else {
                return nil
            }
            let b0 = Int(data[cursor])
            let b1 = Int(data[data.index(cursor, offsetBy: 1)])
            let b2 = Int(data[data.index(cursor, offsetBy: 2)])
            let b3 = Int(data[data.index(cursor, offsetBy: 3)])
            length = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
            cursor = data.index(cursor, offsetBy: 4)
        } else {
            return nil
        }

        guard length > 0 else { return nil }
        guard data.index(cursor, offsetBy: length, limitedBy: data.endIndex) != nil else {
            return nil
        }
        let end = data.index(cursor, offsetBy: length)
        return String(data: data.subdata(in: cursor..<end), encoding: .utf8)
    }
}
