#!/bin/bash
# Lighthouse — one-command setup for a fresh clone.
#
# Checks prereqs, creates a venv, installs the package, then hands off
# to `lighthouse configure` which prompts for your Gemini API key (stored
# in macOS Keychain), creates your identity self-page, copies the default
# prompts into your wiki, and runs a health check.
#
# Safe to re-run — idempotent. Won't clobber existing keys or self-pages
# without asking.
set -e
cd "$(dirname "$0")"

BOLD=$(tput bold 2>/dev/null || echo)
DIM=$(tput dim 2>/dev/null || echo)
GREEN=$(tput setaf 2 2>/dev/null || echo)
RED=$(tput setaf 1 2>/dev/null || echo)
RESET=$(tput sgr0 2>/dev/null || echo)

echo
echo "${BOLD}Lighthouse setup${RESET}"
echo "${DIM}A personal AI agent for your Mac${RESET}"
echo

# --- Prereq checks -------------------------------------------------------

fail=0

if [ "$(uname)" != "Darwin" ]; then
  echo "${RED}✗${RESET} Lighthouse targets macOS. Other platforms are not supported."
  fail=1
else
  echo "${GREEN}✓${RESET} macOS detected"
fi

if command -v python3 >/dev/null 2>&1; then
  pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "${GREEN}✓${RESET} python3 $pyver"
  else
    echo "${RED}✗${RESET} python3 is $pyver but Lighthouse needs 3.10+. Install from https://www.python.org/downloads/"
    fail=1
  fi
else
  echo "${RED}✗${RESET} python3 not found. Install from https://www.python.org/downloads/"
  fail=1
fi

if command -v ffmpeg >/dev/null 2>&1; then
  echo "${GREEN}✓${RESET} ffmpeg (push-to-record mic will work)"
else
  echo "${DIM}○${RESET} ffmpeg not found ${DIM}(optional — enables the Listen button in the popover)${RESET}"
  echo "  install with: brew install ffmpeg"
fi

if command -v gws >/dev/null 2>&1; then
  echo "${GREEN}✓${RESET} gws (Gmail / Calendar / Drive / Tasks observations will work)"
else
  echo "${DIM}○${RESET} gws not found ${DIM}(optional — enables Google Workspace observations)${RESET}"
fi

echo
if [ "$fail" = "1" ]; then
  echo "${RED}Setup cannot continue — fix the errors above and re-run.${RESET}"
  exit 1
fi

# --- Virtual environment -------------------------------------------------

if [ ! -d venv ]; then
  echo "Creating virtual environment at ./venv ..."
  python3 -m venv venv
fi

echo "Installing Lighthouse + dependencies ..."
./venv/bin/pip install -e . --quiet --upgrade-strategy=only-if-needed

echo
echo "${GREEN}✓${RESET} package installed"
echo

# --- Hand off to the interactive configure command ---------------------

./venv/bin/python -m lighthouse configure
