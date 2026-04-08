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
codesign --force --sign "-" --identifier com.deja.app "$DIR/Deja"
codesign --force --sign "-" "$DIR/DejaRecorder"

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
# Bundled API key
if [ -f "$DIR/../Resources/default_api_key.txt" ]; then
    cp "$DIR/../Resources/default_api_key.txt" "$RESOURCES/default_api_key.txt"
fi

echo "Bundling Python environment..."
"$DIR/bundle-python.sh"

# Deploy to /Applications if it exists there
if [ -d "/Applications/Deja.app" ]; then
    echo "Deploying to /Applications..."
    rsync -a --delete "$APP/" "/Applications/Deja.app/"
fi

echo "Done. Restart the app to pick up changes."
