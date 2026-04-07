#!/bin/bash
# Déjà — one-command setup for a fresh clone.
#
# Checks prereqs, creates a venv, installs the package, then hands off
# to `deja configure` which prompts for your Gemini API key (stored
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
echo "${BOLD}Déjà setup${RESET}"
echo "${DIM}A personal AI agent for your Mac${RESET}"
echo

# --- Prereq checks -------------------------------------------------------

fail=0

if [ "$(uname)" != "Darwin" ]; then
  echo "${RED}✗${RESET} Déjà targets macOS. Other platforms are not supported."
  fail=1
else
  echo "${GREEN}✓${RESET} macOS detected"
fi

if command -v python3 >/dev/null 2>&1; then
  pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "${GREEN}✓${RESET} python3 $pyver"
  else
    echo "${RED}✗${RESET} python3 is $pyver but Déjà needs 3.10+. Install from https://www.python.org/downloads/"
    fail=1
  fi
else
  echo "${RED}✗${RESET} python3 not found. Install from https://www.python.org/downloads/"
  fail=1
fi

if command -v node >/dev/null 2>&1; then
  echo "${GREEN}✓${RESET} node $(node --version)"
else
  echo "${RED}✗${RESET} Node.js not found — required for gws (Google Workspace CLI)"
  echo "  install with: brew install node"
  fail=1
fi

if command -v ffmpeg >/dev/null 2>&1; then
  echo "${GREEN}✓${RESET} ffmpeg (push-to-record mic will work)"
else
  echo "${DIM}○${RESET} ffmpeg not found ${DIM}(optional — enables the Listen button in the popover)${RESET}"
  echo "  install with: brew install ffmpeg"
fi

if command -v gws >/dev/null 2>&1; then
  gws_ver=$(gws --version 2>&1 | head -1)
  echo "${GREEN}✓${RESET} gws $gws_ver"
else
  echo "${DIM}…${RESET} gws not installed — will install during setup"
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

echo "Installing Déjà + dependencies ..."
./venv/bin/pip install -e . --quiet --upgrade-strategy=only-if-needed

echo
echo "${GREEN}✓${RESET} package installed"
echo

# --- Google Workspace CLI (gws) -----------------------------------------
# Required for Gmail, Calendar, Drive, and Tasks observations. Installed
# globally via npm because it's a Node.js CLI tool, not a Python package.

if ! command -v gws >/dev/null 2>&1; then
  echo "Installing gws (Google Workspace CLI) ..."
  npm install -g @googleworkspace/cli --silent 2>&1 | tail -3
  if command -v gws >/dev/null 2>&1; then
    echo "${GREEN}✓${RESET} gws installed"
  else
    echo "${RED}✗${RESET} gws install failed — you can retry manually:"
    echo "  npm install -g @googleworkspace/cli"
  fi
fi

echo
# Check if gws is authenticated
if command -v gws >/dev/null 2>&1; then
  gws_auth=$(gws auth status 2>&1 | grep -c "encrypted_credentials_exists.*true")
  if [ "$gws_auth" = "0" ]; then
    echo "${BOLD}Google Workspace authentication${RESET}"
    echo
    echo "Déjà uses the gws CLI to read your Gmail, Calendar, Drive, and"
    echo "Tasks. You need to authenticate once with your Google Workspace account."
    echo
    echo "This opens a browser for Google OAuth — log in with the account you"
    echo "want Déjà to observe (work or personal)."
    echo
    read -p "Authenticate now? [Y/n] " auth_ans
    if [ "$auth_ans" != "n" ] && [ "$auth_ans" != "N" ]; then
      gws auth login 2>&1 || echo "${RED}auth failed — re-run: gws auth login${RESET}"
    else
      echo "Skipping. Run 'gws auth login' later to enable Workspace observations."
    fi
  else
    echo "${GREEN}✓${RESET} gws authenticated"
  fi
fi
echo

# --- Hand off to the interactive configure command ---------------------

./venv/bin/python -m deja configure
