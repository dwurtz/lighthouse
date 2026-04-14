// deja-ocr — extract text from a screenshot via macOS Vision framework.
// Compiled into the app bundle and called by the Python vision pipeline
// as a subprocess. Outputs one recognized line per stdout line.
//
// Usage: deja-ocr /path/to/screenshot.png
//
// No TCC permissions needed — Vision framework operates on image files,
// not cameras or screen capture. The screenshot is already captured by
// the Swift app under its Screen Recording grant.

import AppKit
import Vision

@main
struct DejaOCR {
    static func main() {
        guard CommandLine.arguments.count > 1 else {
            fputs("usage: deja-ocr <image-path>\n", stderr)
            exit(1)
        }

        let path = CommandLine.arguments[1]
        let url = URL(fileURLWithPath: path)

        guard let image = NSImage(contentsOf: url),
              let tiffData = image.tiffRepresentation,
              let cgImage = NSBitmapImageRep(data: tiffData)?.cgImage else {
            fputs("error: could not load image at \(path)\n", stderr)
            exit(1)
        }

        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true

        let handler = VNImageRequestHandler(cgImage: cgImage)
        do {
            try handler.perform([request])
        } catch {
            fputs("error: OCR failed: \(error)\n", stderr)
            exit(1)
        }

        guard let results = request.results else { exit(0) }
        for observation in results {
            if let candidate = observation.topCandidates(1).first {
                print(candidate.string)
            }
        }
    }
}
