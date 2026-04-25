#!/usr/bin/env bash
# Deja installer — one-liner setup for personal Macs.
#
# Curl-bash flow:
#   curl -fsSL https://raw.githubusercontent.com/dwurtz/deja/main/install.sh | bash
#
# Idempotent — re-runs as an updater. Compares the installed Deja.app
# version to the latest GitHub release; downloads + replaces only when
# newer. Pass --check to exit 0/1 on update-available without touching
# anything.
#
# Hard prereqs verified before any download:
#   - Apple Silicon (uname -m == arm64)
#   - macOS >= 14 (Sparkle floor)
#   - Claude Code CLI (`claude` on PATH) — required for the integrate cycle

set -euo pipefail

REPO="dwurtz/deja"
APP_PATH="/Applications/Deja.app"

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }

die() { red "✗ $*"; exit 1; }

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

# ---------- preflight ----------

bold "Deja installer"
echo

# 1. Apple Silicon
arch="$(uname -m)"
if [[ "$arch" != "arm64" ]]; then
  die "Apple Silicon required. Your Mac reports arch=$arch (Intel Macs are not supported)."
fi

# 2. macOS version
os_major="$(sw_vers -productVersion | cut -d. -f1)"
if (( os_major < 14 )); then
  die "macOS 14 (Sonoma) or newer required. You're on $(sw_vers -productVersion)."
fi

# 3. Claude Code
if ! command -v claude >/dev/null 2>&1; then
  red "✗ Claude Code is required for Deja's integrate cycle."
  echo
  echo "   Install it from https://claude.com/code, then run:"
  echo "     claude /login"
  echo
  echo "   Re-run this installer once \`claude\` is on your PATH."
  exit 1
fi

green "✓ arch=$arch, macOS $(sw_vers -productVersion), claude $(command -v claude)"

# ---------- version check ----------

installed_version=""
if [[ -f "$APP_PATH/Contents/Info.plist" ]]; then
  installed_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
    "$APP_PATH/Contents/Info.plist" 2>/dev/null || true)"
fi

latest_json="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest")"
latest_version="$(printf '%s' "$latest_json" | sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | head -1)"
download_url="$(printf '%s' "$latest_json" \
  | sed -n 's/.*"browser_download_url": *"\([^"]*Deja[^"]*\.dmg\)".*/\1/p' | head -1)"

if [[ -z "$latest_version" || -z "$download_url" ]]; then
  die "Couldn't read latest release from GitHub. Check https://github.com/$REPO/releases manually."
fi

if [[ "$installed_version" == "$latest_version" ]]; then
  green "✓ Deja v$installed_version is up to date."
  exit 0
fi

if (( CHECK_ONLY )); then
  if [[ -z "$installed_version" ]]; then
    yellow "Deja is not installed. Latest release: v$latest_version"
  else
    yellow "Update available: v$installed_version → v$latest_version"
  fi
  exit 1
fi

# ---------- download + install ----------

if [[ -z "$installed_version" ]]; then
  echo "Installing Deja v$latest_version…"
else
  echo "Updating Deja v$installed_version → v$latest_version…"
fi

tmp_dmg="$(mktemp -t deja-install).dmg"
trap 'rm -f "$tmp_dmg"; hdiutil detach "/Volumes/Deja" -quiet 2>/dev/null || true' EXIT

curl -fL --progress-bar -o "$tmp_dmg" "$download_url"

# Quit any running Deja so we can replace the bundle.
osascript -e 'tell application "Deja" to quit' 2>/dev/null || true
killall Deja 2>/dev/null || true
sleep 1

# Mount the DMG silently; the volume name comes from `hdiutil create -volname "Deja"`
# in the release workflow.
hdiutil attach "$tmp_dmg" -nobrowse -quiet
mount_point="/Volumes/Deja"
[[ -d "$mount_point/Deja.app" ]] || die "DMG didn't contain Deja.app at $mount_point/Deja.app"

# Replace the installed app.
rm -rf "$APP_PATH"
cp -R "$mount_point/Deja.app" "$APP_PATH"

hdiutil detach "$mount_point" -quiet
rm -f "$tmp_dmg"
trap - EXIT

# Strip macOS quarantine so Gatekeeper doesn't show "damaged" on launch.
# We do this because the build is ad-hoc signed (no Apple Developer
# Program). The trade-off is documented in INSTALL.md.
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

green "✓ Installed Deja v$latest_version to $APP_PATH"

# ---------- launch ----------

open -a Deja
green "✓ Launched Deja"
echo
echo "First-run setup: open the Deja icon in your menu bar (top-right, near"
echo "the clock) to grant permissions and connect Google Workspace."
echo
echo "To check for updates later, re-run this installer."
