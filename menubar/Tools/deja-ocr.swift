// deja-ocr — extract text from a screenshot via macOS Vision framework.
// Compiled into the app bundle and called by the Python vision pipeline
// as a subprocess. Outputs one recognized line per stdout line.
//
// Usage:
//   deja-ocr <image-path>
//   deja-ocr <image-path> --region X Y W H
//       X Y W H are normalized 0..1 in the screenshot's coordinate
//       system, TOP-LEFT origin. Used to restrict OCR to the focused
//       window's bounds (kills sidebar / menu-bar / dock noise that
//       produces phantom entities downstream).
//
// No TCC permissions needed — Vision framework operates on image files,
// not cameras or screen capture. The screenshot is already captured by
// the Swift app under its Screen Recording grant.

import AppKit
import Vision

@main
struct DejaOCR {
    static func main() {
        let args = CommandLine.arguments
        guard args.count > 1 else {
            fputs("usage: deja-ocr <image-path> [--region x y w h]\n", stderr)
            exit(1)
        }

        let path = args[1]
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

        // Optional --region argument restricts OCR to the focused
        // window. Vision's regionOfInterest is normalized 0..1 with a
        // BOTTOM-LEFT origin, while our caller passes TOP-LEFT origin
        // (matching the screenshot's pixel layout). Flip y here.
        if let regionIdx = args.firstIndex(of: "--region"), args.count >= regionIdx + 5,
           let xTL = Double(args[regionIdx + 1]),
           let yTL = Double(args[regionIdx + 2]),
           let w = Double(args[regionIdx + 3]),
           let h = Double(args[regionIdx + 4]) {
            let x = max(0.0, min(1.0, xTL))
            let yBL = max(0.0, min(1.0, 1.0 - yTL - h))   // TL → BL flip
            let cw = max(0.0, min(1.0 - x, w))
            let ch = max(0.0, min(1.0 - yBL, h))
            if cw > 0 && ch > 0 {
                request.regionOfInterest = CGRect(x: x, y: yBL, width: cw, height: ch)
            }
        }

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
