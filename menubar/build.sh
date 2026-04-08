#!/bin/bash
# Build and sign Deja app + recorder helper.
# Signs with a stable certificate + identifier so TCC permissions
# (Screen Recording, Full Disk Access) survive recompiles.

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../Deja.app"

echo "Compiling Deja..."
xcrun swiftc -parse-as-library \
    -framework SwiftUI -framework AppKit -framework ServiceManagement \
    -o "$DIR/Deja" "$DIR"/Sources/**/*.swift

echo "Compiling DejaRecorder..."
xcrun swiftc \
    -framework Foundation -framework ScreenCaptureKit \
    -framework AVFoundation -framework CoreMedia \
    -o "$DIR/DejaRecorder" "$DIR/DejaRecorder.swift"

echo "Signing binaries..."
# Use a stable certificate so TCC permissions (Screen Recording, Full Disk
# Access) survive recompiles. Ad-hoc signing (--sign -) creates a new identity
# each build, breaking permission detection. Falls back to ad-hoc for CI.
if security find-identity -v -p codesigning 2>/dev/null | grep -q "Deja Dev"; then
    codesign --force --sign "Deja Dev" --identifier com.deja.app "$DIR/Deja"
    codesign --force --sign "Deja Dev" --identifier com.deja.recorder "$DIR/DejaRecorder"
else
    echo "  (Deja Dev cert not found — using ad-hoc signing)"
    codesign --force --sign "-" --identifier com.deja.app "$DIR/Deja"
    codesign --force --sign "-" --identifier com.deja.recorder "$DIR/DejaRecorder"
fi

echo "Copying to app bundle..."
cp "$DIR/Deja" "$APP/Contents/MacOS/Deja"

# Ensure assets are in Resources
RESOURCES="$APP/Contents/Resources"
mkdir -p "$RESOURCES"
# Tray icon for menu bar
if [ -f "$DIR/../Resources/tray-icon.png" ]; then
    cp "$DIR/../Resources/tray-icon.png" "$RESOURCES/tray-icon.png"
fi
# App icon
if [ -f "$DIR/../Resources/AppIcon.icns" ]; then
    cp "$DIR/../Resources/AppIcon.icns" "$RESOURCES/AppIcon.icns"
fi
# OAuth client_secret.json for first-run setup
CLIENT_SECRET="$DIR/../src/deja/default_assets/client_secret.json"
if [ -f "$CLIENT_SECRET" ]; then
    cp "$CLIENT_SECRET" "$RESOURCES/client_secret.json"
fi

echo "Bundling Python environment..."
"$DIR/bundle-python.sh"

# Deploy to /Applications if it exists there
if [ -d "/Applications/Deja.app" ]; then
    echo "Deploying to /Applications..."
    rsync -a --delete "$APP/" "/Applications/Deja.app/"
fi

echo "Done. Restart the app to pick up changes."
