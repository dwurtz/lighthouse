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

# Resolve the real Python binary (follow symlinks).
# Use the venv's Python explicitly — Xcode's build environment may
# have a different python3 on PATH than the shell.
REAL_PYTHON="$("$VENV/bin/python3" -c "import sys; print(sys.executable)")"
if [ ! -f "$REAL_PYTHON" ]; then
    REAL_PYTHON="$(readlink -f "$VENV/bin/python3")"
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

# 2. Copy the Python standard library
echo "Copying Python stdlib..."
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='test' --exclude='tests' --exclude='idle_test' \
    --exclude='tkinter' --exclude='turtledemo' --exclude='turtle.py' \
    --exclude='ensurepip' --exclude='distutils' \
    --exclude='lib2to3' \
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
    --exclude='torch' --exclude='torch-*' \
    --exclude='torchvision' --exclude='torchvision-*' \
    --exclude='timm' --exclude='timm-*' \
    --exclude='sympy' --exclude='sympy-*' \
    --exclude='networkx' --exclude='networkx-*' \
    "$SITE_PACKAGES/" "$DEST/lib/python${PYTHON_VERSION}/site-packages/"

# Remove editable install .pth files (they point to the dev machine)
rm -f "$DEST/lib/python${PYTHON_VERSION}/site-packages/__editable__."*.pth

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

# 9. Sign all native extensions — macOS code signing monitor kills
# unsigned .so files loaded from inside a signed .app bundle.
echo "Signing native extensions..."
find "$DEST" -name '*.so' -exec codesign --force --sign "Deja Dev" {} \; 2>/dev/null || true
find "$DEST" -name '*.dylib' -exec codesign --force --sign "Deja Dev" {} \; 2>/dev/null || true
# Also sign the Python binary itself
codesign --force --sign "Deja Dev" "$DEST/bin/python3" 2>/dev/null || true

# Report size
BUNDLE_SIZE="$(du -sh "$DEST" | cut -f1)"
echo ""
echo "=== Done. Bundle size: $BUNDLE_SIZE ==="
echo "  Python env: $DEST"
echo "  Launch with: $DEST/bin/python3 -m deja web"
