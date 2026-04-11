#!/bin/bash
# Bundle the Python venv into Deja.app/Contents/Resources/python-env/
# This replaces the PyInstaller frozen binary approach.
#
# The bundled venv includes:
#   - A copy of the Homebrew Python binary + framework
#   - All site-packages (fastapi, uvicorn, etc.)
#   - The deja source package (via PYTHONPATH, set by Swift)
#
# The Swift app sets PYTHONPATH to .../python-env/src so that
# `python3 -m deja monitor` and `python3 -m deja web` work.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$DIR/.."
VENV="$PROJECT/venv"
# Use Xcode's BUILT_PRODUCTS_DIR if available (xcodebuild), otherwise project root (build.sh)
if [ -n "${BUILT_PRODUCTS_DIR:-}" ]; then
    APP="$BUILT_PRODUCTS_DIR/Deja.app"
else
    APP="$PROJECT/Deja.app"
fi
DEST="$APP/Contents/Resources/python-env"

PYTHON_VERSION="3.14"
SITE_PACKAGES="$VENV/lib/python${PYTHON_VERSION}/site-packages"

# Resolve the real Python binary (follow the venv symlink chain to the
# underlying Homebrew framework). NOTE: we cannot use `sys.executable`
# inside the venv — it returns the venv's own python path, which
# would point FRAMEWORK_LIB at the venv's lib/ instead of the framework's
# stdlib, dragging the entire site-packages directory (including the ML
# packages we explicitly exclude later) into the first stdlib rsync.
REAL_PYTHON="$(readlink -f "$VENV/bin/python3")"
if [ ! -f "$REAL_PYTHON" ]; then
    echo "ERROR: Cannot resolve real python3 from $VENV/bin/python3"
    exit 1
fi

# Find the Python framework/lib directory (stdlib)
FRAMEWORK_LIB="$(dirname "$(dirname "$REAL_PYTHON")")/lib/python${PYTHON_VERSION}"
if [ ! -d "$FRAMEWORK_LIB" ]; then
    echo "ERROR: Cannot find Python stdlib at $FRAMEWORK_LIB"
    exit 1
fi

echo "=== Bundling Python venv into app bundle ==="
echo "  Source venv: $VENV"
echo "  Python binary: $REAL_PYTHON"
echo "  Framework lib: $FRAMEWORK_LIB"
echo "  Destination: $DEST"

# Clean previous bundle
rm -rf "$DEST"
mkdir -p "$DEST/bin" "$DEST/lib/python${PYTHON_VERSION}"

# 1. Copy the Python binary
echo "Copying Python binary..."
cp "$REAL_PYTHON" "$DEST/bin/python3"
chmod +x "$DEST/bin/python3"

# 2. Copy the Python standard library.
# IMPORTANT: exclude site-packages here. The Homebrew framework ships
# its site-packages as a SYMLINK pointing outside the framework, which
# would (a) come across as a broken symlink in the bundle and (b) block
# the later site-packages rsync. We populate site-packages from the
# venv in step 4 instead.
echo "Copying Python stdlib..."
# Exclude config-*-darwin (Python C extension build headers — only used
# for compiling native modules against the framework at runtime, which
# we never do). These dirs contain broken symlinks like libpython3.14.a
# → ../../../Python that point into the framework's parent dirs and
# resolve cleanly in the Homebrew layout but become dangling in our
# bundled layout. codesign --deep walks into them, fails, and LaunchServices
# then rejects the bundle with error -600 ("bundle cannot be launched").
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='test' --exclude='tests' --exclude='idle_test' \
    --exclude='tkinter' --exclude='turtledemo' --exclude='turtle.py' \
    --exclude='ensurepip' --exclude='distutils' \
    --exclude='lib2to3' \
    --exclude='site-packages' \
    --exclude='config-*-darwin' \
    "$FRAMEWORK_LIB/" "$DEST/lib/python${PYTHON_VERSION}/"

# 3. Copy the Python dylib/framework so the binary can find it
FRAMEWORK_DIR="$(dirname "$(dirname "$REAL_PYTHON")")"
DYLIB="$FRAMEWORK_DIR/lib/libpython${PYTHON_VERSION}.dylib"
if [ -f "$DYLIB" ]; then
    echo "Copying Python dylib..."
    mkdir -p "$DEST/lib"
    cp "$DYLIB" "$DEST/lib/"
fi
# Also check for framework-style layout
FRAMEWORK_DYLIB="$FRAMEWORK_DIR/Python"
if [ -f "$FRAMEWORK_DYLIB" ]; then
    echo "Copying Python framework dylib..."
    # The binary links against the framework; we need to set up the same relative path
    # or use install_name_tool to rewrite
    FRAMEWORK_PARENT="$(dirname "$FRAMEWORK_DIR")"
    FRAMEWORK_NAME="$(basename "$FRAMEWORK_DIR")"
    # Copy the framework dylib next to where the binary expects it
    mkdir -p "$DEST/Frameworks/Python.framework/Versions/${PYTHON_VERSION}"
    cp "$FRAMEWORK_DYLIB" "$DEST/Frameworks/Python.framework/Versions/${PYTHON_VERSION}/Python"

    # Copy Python.app helper — Python on macOS uses this stub when
    # spawning the actual interpreter process. Without it, python3 in
    # the bundle dies with "posix_spawn: Python.app/.../Python: Undefined error: 0".
    if [ -d "$FRAMEWORK_DIR/Resources/Python.app" ]; then
        echo "Copying Python.app helper..."
        mkdir -p "$DEST/Frameworks/Python.framework/Versions/${PYTHON_VERSION}/Resources"
        rsync -a "$FRAMEWORK_DIR/Resources/Python.app" \
            "$DEST/Frameworks/Python.framework/Versions/${PYTHON_VERSION}/Resources/"
    fi
fi

# 4. Copy site-packages (excluding large unnecessary dirs)
echo "Copying site-packages..."
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='tests' --exclude='test' \
    --exclude='_pyinstaller_hooks_contrib' \
    --exclude='altgraph*' \
    --exclude='pyinstaller*' --exclude='PyInstaller*' \
    --exclude='pip' --exclude='pip-*' \
    --exclude='setuptools' --exclude='setuptools-*' \
    --exclude='_distutils_hack' \
    --exclude='pkg_resources' \
    --exclude='wheel' --exclude='wheel-*' \
    --exclude='*.dist-info/RECORD' \
    "$SITE_PACKAGES/" "$DEST/lib/python${PYTHON_VERSION}/site-packages/"

# Remove editable install .pth files (they point to the dev machine)
rm -f "$DEST/lib/python${PYTHON_VERSION}/site-packages/__editable__."*.pth

# Remove distutils-precedence.pth — it tries to import _distutils_hack
# which we exclude with setuptools, producing a harmless but noisy
# ModuleNotFoundError on every Python startup.
rm -f "$DEST/lib/python${PYTHON_VERSION}/site-packages/distutils-precedence.pth"

# 5. Copy the deja source package
echo "Copying deja source package..."
mkdir -p "$DEST/src"
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    "$PROJECT/src/deja" "$DEST/src/"

# 6. Write a pyvenv.cfg that makes the bundled dir work as a venv
cat > "$DEST/pyvenv.cfg" <<PYCFG
home = $DEST/bin
include-system-site-packages = false
version = ${PYTHON_VERSION}
PYCFG

# 7. Fix the Python binary's dylib references to be relocatable
echo "Fixing dylib references..."
# Check what the binary links against
LINKED_FRAMEWORK="$(otool -L "$DEST/bin/python3" | grep -o '/.*Python.framework[^ ]*' | head -1 || true)"
if [ -n "$LINKED_FRAMEWORK" ] && [ -f "$DEST/Frameworks/Python.framework/Versions/${PYTHON_VERSION}/Python" ]; then
    install_name_tool -change \
        "$LINKED_FRAMEWORK" \
        "@executable_path/../Frameworks/Python.framework/Versions/${PYTHON_VERSION}/Python" \
        "$DEST/bin/python3" 2>/dev/null || true
fi

# 8. Strip .so files to reduce size
echo "Stripping .so files..."
find "$DEST" -name '*.so' -exec strip -x {} \; 2>/dev/null || true

# 8b. Pre-compile every .py to .pyc so the bundle's seal includes
# the bytecode caches. Without this, Python would write __pycache__/
# files at runtime AFTER the app is signed, invalidating the seal
# and causing macOS Gatekeeper to reject launches via LaunchServices
# with "sealed resource is missing or invalid" → error -600.
#
# We use the FREESHLY-COPIED bundled Python to compile, which writes
# the .pyc files in-place inside the bundle. After this step, every
# .py file has a matching .pyc and the seal will cover both.
echo "Pre-compiling .pyc bytecode (so Python doesn't write into the seal at runtime)..."
"$DEST/bin/python3" -m compileall -q -j 0 \
    "$DEST/lib/python${PYTHON_VERSION}" \
    "$DEST/src" 2>&1 | tail -5 || echo "  WARN: compileall had errors (some .pyc may be missing)"

# 9. Sign all native extensions — macOS code signing monitor kills
# unsigned .so files loaded from inside a signed .app bundle.
echo "Signing native extensions..."
find "$DEST" -name '*.so' -exec codesign --force --sign "Deja Dev" {} \; 2>/dev/null || true
find "$DEST" -name '*.dylib' -exec codesign --force --sign "Deja Dev" {} \; 2>/dev/null || true
# Also sign the Python binary itself
codesign --force --sign "Deja Dev" "$DEST/bin/python3" 2>/dev/null || true

# 10. Re-seal the outer .app bundle (Release only).
#
# Xcode runs this script AFTER it has already signed the .app, so all
# the nested binaries we just modified (Python interpreter, .so files,
# .dylibs) have invalidated the outer seal. Without this step macOS's
# Gatekeeper / LaunchServices rejects the launch with "a sealed resource
# is missing or invalid" and `open` fails with -600.
#
# Skip in Debug config: Xcode generates a Deja.debug.dylib alongside
# the main binary that isn't signed by us, and codesign refuses to
# re-seal a bundle with unsigned nested code. Debug builds aren't
# packaged into a DMG anyway, so they don't need to pass Gatekeeper.
if [ "${CONFIGURATION:-Release}" = "Release" ]; then
    echo "Re-sealing outer .app bundle..."
    ENTITLEMENTS="$PROJECT/Deja.entitlements"
    if [ -f "$ENTITLEMENTS" ]; then
        codesign --force --sign "Deja Dev" \
            --entitlements "$ENTITLEMENTS" \
            "$APP" 2>&1 | tail -3 || echo "  WARN: re-seal failed"
    else
        codesign --force --sign "Deja Dev" "$APP" 2>&1 | tail -3 || echo "  WARN: re-seal failed"
    fi

    # Verify (informational only — never fail the build on this)
    codesign --verify --deep --strict "$APP" >/dev/null 2>&1 \
        && echo "  codesign verify: OK" \
        || echo "  WARNING: codesign verify failed — bundle may not launch via open/Finder"
else
    echo "Skipping outer re-seal (Debug build — only Release needs Gatekeeper compatibility)"
fi

# Report size
BUNDLE_SIZE="$(du -sh "$DEST" | cut -f1)"
echo ""
echo "=== Done. Bundle size: $BUNDLE_SIZE ==="
echo "  Python env: $DEST"
echo "  Launch with: $DEST/bin/python3 -m deja web"
