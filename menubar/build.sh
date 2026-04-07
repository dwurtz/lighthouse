#!/bin/bash
# Build and sign Lighthouse app + recorder helper.
# The cert-based signature ensures TCC permissions survive recompiles.

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../Lighthouse.app"

echo "Compiling Lighthouse..."
xcrun swiftc -parse-as-library \
    -framework SwiftUI -framework AppKit \
    -o "$DIR/Lighthouse" "$DIR/Lighthouse.swift"

echo "Compiling LighthouseRecorder..."
xcrun swiftc \
    -framework Foundation -framework ScreenCaptureKit \
    -framework AVFoundation -framework CoreMedia \
    -o "$DIR/LighthouseRecorder" "$DIR/LighthouseRecorder.swift"

echo "Copying to app bundle..."
cp "$DIR/Lighthouse" "$APP/Contents/MacOS/Lighthouse"

echo "Signing..."
codesign --force --sign "Lighthouse Dev" --deep "$APP"
codesign --force --sign "Lighthouse Dev" "$DIR/LighthouseRecorder"

echo "Done. Restart the app to pick up changes."
