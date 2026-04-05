#!/bin/bash
# Standalone launcher for Lighthouse — starts the two child processes the
# Swift menu-bar app would normally spawn (monitor + web) in the background
# and waits so the script can be Ctrl-C'd to stop them together. For the
# full experience (menu bar icon + popover + Listen button) launch the
# Swift app bundle at Lighthouse.app instead.
set -e
cd "$(dirname "$0")"

# Load GEMINI_API_KEY from ~/.lighthouse/env if present, then fall back to
# whatever's already in the shell environment. Never committed to source.
if [ -f "$HOME/.lighthouse/env" ]; then
  set -a
  . "$HOME/.lighthouse/env"
  set +a
fi
if [ -z "$GEMINI_API_KEY" ]; then
  echo "error: GEMINI_API_KEY is not set." >&2
  echo "       Put it in ~/.lighthouse/env or export it in your shell profile." >&2
  exit 1
fi

VENV="$(pwd)/venv"
if [ ! -x "$VENV/bin/python3" ]; then
  echo "error: venv not found at $VENV. Run: python3 -m venv venv && ./venv/bin/pip install -e ." >&2
  exit 1
fi
export PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PY="$VENV/bin/python3"

# Propagate SIGINT/SIGTERM to children so Ctrl-C shuts everything down.
trap 'kill $(jobs -p) 2>/dev/null; exit 0' INT TERM

"$PY" -m lighthouse monitor &
"$PY" -m lighthouse web &
wait
