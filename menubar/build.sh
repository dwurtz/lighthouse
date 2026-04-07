#!/bin/bash
# Build and sign Deja app + recorder helper.
# The cert-based signature ensures TCC permissions survive recompiles.

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../Deja.app"

echo "Compiling Deja..."
xcrun swiftc -parse-as-library \
    -framework SwiftUI -framework AppKit \
    -o "$DIR/Deja" "$DIR/Deja.swift"

echo "Compiling DejaRecorder..."
xcrun swiftc \
    -framework Foundation -framework ScreenCaptureKit \
    -framework AVFoundation -framework CoreMedia \
    -o "$DIR/DejaRecorder" "$DIR/DejaRecorder.swift"

echo "Copying to app bundle..."
cp "$DIR/Deja" "$APP/Contents/MacOS/Deja"

echo "Signing..."
codesign --force --sign "Deja Dev" --deep "$APP"
codesign --force --sign "Deja Dev" "$DIR/DejaRecorder"

echo "Done. Restart the app to pick up changes."
