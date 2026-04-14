// test_imessage_decoder.swift — standalone regression test for
// AttributedBodyDecoder. Run via `make test-swift` or:
//
//     swiftc -o /tmp/decoder_test \
//         scripts/test_imessage_decoder.swift \
//         menubar/Sources/Services/AttributedBodyDecoder.swift \
//     && /tmp/decoder_test
//
// WHY THIS EXISTS
// ---------------
// The `2026-04-11` carpool bug: DatabaseReader's SQL filter silently
// dropped every Messages row with `text IS NULL`, which on modern
// macOS is ~every outbound iMessage. A full afternoon of David's
// replies in the Molly/Ruby Carpool thread never reached Deja's
// observation stream and integrate kept re-reasoning off a stale
// snippet.
//
// The fix reads attributedBody and decodes it via
// AttributedBodyDecoder.extractString. This test pins the decoder
// against real chat.db blobs so a future refactor can't silently
// regress us back to the same bug.

import Foundation

@main
struct DecoderTest {
    static func hexToData(_ hex: String) -> Data {
        let chars = Array(hex)
        var data = Data()
        var i = 0
        while i + 1 < chars.count {
            let byteStr = String(chars[i]) + String(chars[i + 1])
            if let byte = UInt8(byteStr, radix: 16) {
                data.append(byte)
            }
            i += 2
        }
        return data
    }

    struct Case {
        let name: String
        let hex: String
        let expected: String
    }

    static func main() {
        // Fixtures captured from a real chat.db on 2026-04-11.
        // Each blob is the raw `attributedBody` BLOB column bytes.
        let cases: [Case] = [
            Case(
                name: "short message, 1-byte length (41 chars)",
                // Incoming "Ah ok. As long as you trust their testing"
                // Length encoding: single byte 0x29 (= 41)
                hex: "040B73747265616D747970656481E803840140848484124E5341747472696275746564537472696E67008484084E534F626A656374008592848484084E53537472696E67019484012B294168206F6B2E204173206C6F6E6720617320796F752074727573742074686569722074657374696E6786840269490129928484840C4E5344696374696F6E617279009484016901928496961D5F5F6B494D4D657373616765506172744174747269627574654E616D658692848484084E534E756D626572008484074E5356616C7565009484012A84999900868686",
                expected: "Ah ok. As long as you trust their testing"
            ),
            Case(
                name: "medium message, 1-byte length (75 chars)",
                // Incoming "4 hours before my noon apt with you guys? Or the 220 apt at AZ Diagnostics?"
                // Length encoding: single byte 0x4B (= 75)
                hex: "040B73747265616D747970656481E803840140848484124E5341747472696275746564537472696E67008484084E534F626A656374008592848484084E53537472696E67019484012B4B3420686F757273206265666F7265206D79206E6F6F6E20617074207769746820796F7520677579733F204F7220746865203232302061707420617420415A20446961676E6F73746963733F",
                expected: "4 hours before my noon apt with you guys? Or the 220 apt at AZ Diagnostics?"
            ),
            Case(
                name: "long message, 2-byte LE length (152 chars)",
                // Incoming "Hey 5901, Tim here with a local Paradise Valley tree crew..."
                // Length encoding: 0x81 0x98 0x00 (= uint16 LE 0x0098 = 152).
                // This is the case the old decoder would have fumbled — without
                // correct LE uint16 decoding we'd read 38912 as the length and
                // fail the UTF-8 conversion.
                hex: "040B73747265616D747970656481E803840140848484194E534D757461626C6541747472696275746564537472696E67008484124E5341747472696275746564537472696E67008484084E534F626A6563740085928484840F4E534D757461626C65537472696E67018484084E53537472696E67019584012B81980048657920353930312C2054696D206865726520776974682061206C6F63616C2050617261646973652056616C6C6579207472656520637265772E20576527726520646F696E67207472656520776F726B20696E20796F7572206E65696768626F72686F6F642074686973207765656B2E20416E7920747265657320796F752764206C696B652072656D6F766564206F72207472696D6D6564",
                expected: "Hey 5901, Tim here with a local Paradise Valley tree crew. We're doing tree work in your neighborhood this week. Any trees you'd like removed or trimmed"
            ),
        ]

        var failures = 0
        for c in cases {
            let blob = hexToData(c.hex)
            let result = AttributedBodyDecoder.extractString(from: blob)
            if result == c.expected {
                print("PASS  \(c.name)")
            } else {
                failures += 1
                print("FAIL  \(c.name)")
                print("      expected: \(c.expected)")
                print("      got:      \(result ?? "(nil)")")
            }
        }

        // Negative case: a blob with no NSString marker returns nil,
        // doesn't crash.
        let junk = Data([0x00, 0x01, 0x02, 0x03])
        if AttributedBodyDecoder.extractString(from: junk) != nil {
            failures += 1
            print("FAIL  junk blob should return nil")
        } else {
            print("PASS  junk blob returns nil")
        }

        if failures == 0 {
            print("\nAll \(cases.count + 1) tests passed.")
            exit(0)
        } else {
            print("\n\(failures) test(s) failed.")
            exit(1)
        }
    }
}
